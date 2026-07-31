#!/usr/bin/env python3
"""
foreman-runner.py

Generic headless agent runner for a Foreman crew member. One process per
crew member; the process owns the relay connection and the message loop so
the backing model never has to remember to keep listening.

Usage:
    foreman-runner.py --role <role> --project <abs-path>
                       [--name <session-name>] [--cwd <workdir>]
                       [--config <path>]

See references/architecture.md for the full contract this script implements.
"""

import argparse
import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

PROTOCOL_VERSION = "2"
MAX_LINE_BYTES = 4 * 1024 * 1024   # 4 MB — guard against unbounded relay messages
INBOX_WAIT_TIMEOUT_MS = 300_000
DEFAULT_ASK_TIMEOUT_MS = 120_000
READINESS_ASK_TIMEOUT_MS = 5_000
MAX_ASK_HOPS = 5
RECONNECT_BACKOFF_MIN = 2
RECONNECT_BACKOFF_MAX = 60
MAX_CONSECUTIVE_FAILURES = 5
HUB_WAIT_INTERVAL = 5              # poll interval while waiting for the hub to first appear
CLI_SUBPROCESS_TIMEOUT = 15 * 60    # 15 minutes
OPENAI_HTTP_TIMEOUT = 300           # 5 minutes
STDERR_TAIL_BYTES = 2000

ASK_DIRECTIVE_RE = re.compile(r"^@ask\s+(\S+):\s*(.+)$", re.MULTILINE)


def log(name: str, message: str) -> None:
    print(f"[{name}] {message}", flush=True)


# --------------------------------------------------------------------------
# Relay client — all wire-format knowledge for the relay hub lives here.
# --------------------------------------------------------------------------

class RelayClient:
    """Connection to the Foreman relay hub: register, inbox_wait, reply, ask."""

    def __init__(self, name: str, cwd: str, socket_override: str = ""):
        self.name = name
        self.cwd = cwd
        self.socket_override = socket_override
        self.sock: Optional[socket.socket] = None
        self._req_counter = 0

    # -- socket discovery ---------------------------------------------------

    def _socket_candidates(self) -> List[str]:
        return [
            self.socket_override or os.environ.get("RELAY_HUB_SOCKET", ""),
            os.path.expandvars(os.environ.get("CLAUDE_PLUGIN_DATA", "") + "/hub.sock"),
            os.path.expanduser("~/.claude/plugins/data/relay-claude-relay/hub.sock"),
            os.path.expanduser("~/.claude-relay/hub.sock"),
        ]

    def _find_hub_socket(self) -> str:
        for path in self._socket_candidates():
            if path and Path(path).exists():
                return path
        raise FileNotFoundError("No relay hub socket found. Is the Orchestrator running?")

    # -- low-level line protocol --------------------------------------------

    def _read_line(self) -> Dict[str, Any]:
        assert self.sock is not None
        buf = b""
        while True:
            chunk = self.sock.recv(1)
            if not chunk:
                raise ConnectionError("Hub disconnected unexpectedly.")
            if chunk == b"\n":
                try:
                    return json.loads(buf.decode("utf-8"))
                except json.JSONDecodeError as e:
                    raise ConnectionError(f"Invalid JSON from hub: {e}") from e
            buf += chunk
            if len(buf) > MAX_LINE_BYTES:
                raise ConnectionError(f"Relay message exceeded {MAX_LINE_BYTES} bytes — aborting.")

    def _send(self, obj: Dict[str, Any]) -> None:
        assert self.sock is not None
        try:
            self.sock.sendall((json.dumps(obj) + "\n").encode("utf-8"))
        except OSError as e:
            raise ConnectionError(f"Failed to send to hub: {e}") from e

    def _next_req_id(self) -> str:
        self._req_counter += 1
        return f"{self.name}-{self._req_counter}"

    def _git_branch(self) -> str:
        try:
            proc = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=self.cwd, capture_output=True, text=True, timeout=5,
            )
            if proc.returncode == 0:
                return proc.stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            pass
        return ""

    # -- lifecycle ------------------------------------------------------

    def connect(self) -> None:
        # May raise FileNotFoundError (no hub socket yet — the caller treats
        # that differently from a lost connection) or ConnectionError.
        path = self._find_hub_socket()

        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.connect(path)
        except OSError as e:
            raise ConnectionError(f"Failed to connect to hub at {path}: {e}") from e
        self.sock = sock

        try:
            self._register()
        except ConnectionError:
            self.close()
            raise

    def _register(self) -> None:
        self._send({
            "type": "register",
            "name": self.name,
            "cwd": self.cwd,
            "git_branch": self._git_branch(),
            "protocol_version": PROTOCOL_VERSION,
        })
        ack = self._read_line()
        if ack.get("type") == "err":
            raise ConnectionError(f"Registration failed: {ack.get('code')}")

    def close(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

    # -- protocol operations ----------------------------------------------

    def inbox_wait(self, timeout_ms: int = INBOX_WAIT_TIMEOUT_MS) -> Dict[str, Any]:
        req_id = self._next_req_id()
        self._send({"type": "inbox_wait", "timeout_ms": timeout_ms, "req_id": req_id})
        return self._read_line()

    def reply(self, ask_id: str, text: str) -> None:
        self._send({"type": "reply", "ask_id": ask_id, "text": text})

    def ask(self, to: str, question: str, timeout_ms: int = DEFAULT_ASK_TIMEOUT_MS) -> str:
        """Outbound ask to a peer. Best-effort per architecture.md's Known
        Limitations: never raises — an err response or timeout comes back as
        a bracketed error string instead.
        """
        req_id = self._next_req_id()
        try:
            self._send({"type": "ask", "to": to, "text": question, "req_id": req_id})
        except ConnectionError as e:
            return f"[ask failed: send_error:{e}]"

        assert self.sock is not None
        original_timeout = self.sock.gettimeout()
        deadline = time.monotonic() + timeout_ms / 1000.0
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return "[ask failed: timeout]"
                self.sock.settimeout(remaining)
                try:
                    msg = self._read_line()
                except socket.timeout:
                    return "[ask failed: timeout]"

                msg_type = msg.get("type")
                references_us = msg.get("req_id") == req_id or msg.get("in_reply_to") == req_id

                if msg_type == "err" and references_us:
                    return f"[ask failed: {msg.get('code', 'unknown')}]"
                if msg_type in ("response", "ask_response", "reply") and references_us:
                    return str(msg.get("text", msg.get("content", "")))
                if msg_type == "inbox_deliver" and (references_us or msg.get("ask_id") == req_id):
                    return str(msg.get("content", ""))
                # Unrelated traffic (e.g. a stray notification) while we wait
                # for our own answer — best effort: log and keep waiting.
                log(self.name, f"ignoring unrelated message while awaiting ask reply: {msg_type}")
        except ConnectionError as e:
            return f"[ask failed: connection_error:{e}]"
        finally:
            try:
                self.sock.settimeout(original_timeout)
            except OSError:
                pass


# --------------------------------------------------------------------------
# Config loading
# --------------------------------------------------------------------------

def _strip_comments(d: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in d.items() if not k.startswith("//")}


def _merge_dicts(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(_strip_comments(base))
    merged.update(_strip_comments(override))
    return merged


def _load_json_file(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        sys.exit(f"Invalid JSON in config file {path}: {e}")
    except OSError as e:
        sys.exit(f"Could not read config file {path}: {e}")


def load_config(skill_config_path: Path, project_path: Path) -> Dict[str, Any]:
    """Load skill-level config, merge project-level override per role."""
    skill_config = _load_json_file(skill_config_path)
    project_config = _load_json_file(project_path / ".foreman" / "config.json")

    skill_roles = _strip_comments(skill_config.get("roles") or {})
    project_roles = _strip_comments(project_config.get("roles") or {})

    merged_roles: Dict[str, Any] = {}
    for role_name in set(skill_roles) | set(project_roles):
        merged_roles[role_name] = _merge_dicts(
            skill_roles.get(role_name, {}), project_roles.get(role_name, {})
        )

    defaults = _merge_dicts(
        skill_config.get("defaults") or {}, project_config.get("defaults") or {}
    )

    return {"defaults": defaults, "roles": merged_roles}


def resolve_role_config(config: Dict[str, Any], role: str) -> Dict[str, Any]:
    """Defaults overlaid by the role's entry (which is itself skill-role
    overlaid by project-role, per load_config)."""
    role_cfg = config.get("roles", {}).get(role, {})
    return _merge_dicts(config.get("defaults", {}), role_cfg)


# --------------------------------------------------------------------------
# System context assembly
# --------------------------------------------------------------------------

def build_system_context(skill_root: Path, role: str, name: str) -> str:
    protocol_path = skill_root / "references" / "protocol.md"
    role_path = skill_root / "references" / "roles" / f"{role}.md"

    parts = []
    if protocol_path.exists():
        parts.append(protocol_path.read_text(encoding="utf-8"))
    else:
        log(name, f"warning: protocol file not found at {protocol_path}")
    if role_path.exists():
        parts.append(role_path.read_text(encoding="utf-8"))
    else:
        log(name, f"warning: role file not found at {role_path}")

    context = "\n\n---\n\n".join(parts)

    ask_paragraph = (
        "\n\n---\n\n## Outbound Asks\n\n"
        f"Your session name on the relay is `{name}`. You have no direct relay tools "
        "in this headless session. To ask another crew member a question, put a line "
        "in your response in exactly this form: `@ask <peer-session-name>: <your "
        "question>` — for example `@ask foreman-architect: What shape is the auth "
        "token object in the plan?`. You may include several `@ask` lines in one "
        "response. The runner will detect them, relay each question, and feed the "
        "answers back to you as a new turn (\"Answers to your asks: ...\"). Once a "
        "response of yours contains no `@ask` lines, it is sent as your final reply "
        "to whoever messaged you. You get at most 5 rounds of `@ask` per incoming "
        "message, so batch your questions rather than trickling them out one at a time."
    )
    return context + ask_paragraph


# --------------------------------------------------------------------------
# Backend adapters
# --------------------------------------------------------------------------

class Backend(ABC):
    """A backend turns one input message into one final text response."""

    def __init__(self, cfg: Dict[str, Any], cwd: str, system_context: str, name: str):
        self.cfg = cfg
        self.cwd = cwd
        self.system_context = system_context
        self.name = name

    @abstractmethod
    def respond(self, message: str) -> str:
        """Return the backend's response text. Never raises — failures come
        back as bracketed error strings so the runner can relay them."""


class ClaudeCliBackend(Backend):
    """Headless `claude -p` per task, `--resume` for session continuity."""

    def __init__(self, cfg: Dict[str, Any], cwd: str, system_context: str, name: str):
        super().__init__(cfg, cwd, system_context, name)
        self.model = cfg["model"]
        self.allowed_tools = cfg.get("allowed_tools")  # None = absent, [] = none, [...] = list
        self.extra_args = cfg.get("extra_args") or []
        self.session_id: Optional[str] = None

    def _permission_args(self) -> List[str]:
        if self.allowed_tools is None:
            return ["--dangerously-skip-permissions"]
        if len(self.allowed_tools) == 0:
            return []
        return ["--allowedTools", ",".join(self.allowed_tools)]

    def respond(self, message: str) -> str:
        if self.session_id is None:
            cmd = [
                "claude", "--model", self.model,
                "-p", message,
                "--output-format", "json",
                "--append-system-prompt", self.system_context,
            ]
        else:
            cmd = [
                "claude", "--model", self.model,
                "--resume", self.session_id,
                "-p", message,
                "--output-format", "json",
            ]
        cmd += self._permission_args()
        cmd += self.extra_args

        log(self.name, f"claude-cli: invoking model={self.model} resume={bool(self.session_id)}")
        try:
            proc = subprocess.run(
                cmd, cwd=self.cwd, capture_output=True, text=True,
                timeout=CLI_SUBPROCESS_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            return f"[claude-cli error: timed out after {CLI_SUBPROCESS_TIMEOUT}s]"
        except FileNotFoundError:
            return "[claude-cli error: 'claude' binary not found on PATH]"

        if proc.returncode != 0:
            tail = (proc.stderr or "")[-STDERR_TAIL_BYTES:]
            return f"[claude-cli error: exit {proc.returncode}: {tail}]"

        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return f"[claude-cli error: non-JSON output: {proc.stdout[-STDERR_TAIL_BYTES:]}]"

        session_id = data.get("session_id")
        if session_id:
            self.session_id = session_id

        result = data.get("result")
        if not result:
            return f"[claude-cli error: no 'result' in output: {json.dumps(data)[:STDERR_TAIL_BYTES]}]"
        return result


class CodexCliBackend(Backend):
    """`codex exec` per task; system context + message sent on stdin."""

    def __init__(self, cfg: Dict[str, Any], cwd: str, system_context: str, name: str):
        super().__init__(cfg, cwd, system_context, name)
        self.bin = cfg.get("bin") or os.environ.get("FOREMAN_CODEX_BIN") or "codex"
        self.model = cfg["model"]
        self.reasoning_effort = cfg.get("reasoning_effort", "high")

    def respond(self, message: str) -> str:
        stdin_payload = f"{self.system_context}\n\n{message}"
        cmd = [
            self.bin, "exec",
            "-s", "workspace-write",
            "-m", self.model,
            "-c", f'model_reasoning_effort="{self.reasoning_effort}"',
            "-",
        ]

        log(self.name, f"codex-cli: invoking model={self.model} effort={self.reasoning_effort}")
        try:
            proc = subprocess.run(
                cmd, cwd=self.cwd, input=stdin_payload, capture_output=True, text=True,
                timeout=CLI_SUBPROCESS_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            return f"[codex-cli error: timed out after {CLI_SUBPROCESS_TIMEOUT}s]"
        except FileNotFoundError:
            return f"[codex-cli error: binary not found: {self.bin}]"

        if proc.returncode != 0:
            tail = (proc.stderr or "")[-STDERR_TAIL_BYTES:]
            return f"[codex-cli error: exit {proc.returncode}: {tail}]"

        return self._strip_chrome(proc.stdout)

    @staticmethod
    def _strip_chrome(stdout: str) -> str:
        """Best-effort strip of codex's banner/metadata lines. Falls back to
        the raw (stripped) output if nothing trivially identifiable is found."""
        known_prefixes = (
            "OpenAI Codex", "workdir:", "model:", "provider:", "approval:",
            "sandbox:", "reasoning effort:", "reasoning summaries:", "-----",
        )
        lines = stdout.splitlines()
        content_start = 0
        for i, line in enumerate(lines):
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith(known_prefixes):
                continue
            if stripped.startswith("[") and "]" in stripped[:40]:
                continue  # timestamped log line, e.g. "[2026-01-01T00:00:00] ..."
            content_start = i
            break
        else:
            return stdout.strip()
        return "\n".join(lines[content_start:]).strip()


class OpenAICompatibleBackend(Backend):
    """POST to {base_url}/chat/completions; history kept in-process."""

    def __init__(self, cfg: Dict[str, Any], cwd: str, system_context: str, name: str):
        super().__init__(cfg, cwd, system_context, name)
        self.base_url = cfg["base_url"].rstrip("/")
        self.model = cfg["model"]
        self.api_key_env = cfg.get("api_key_env", "")
        self.temperature = cfg.get("temperature")
        self.max_tokens = cfg.get("max_tokens")
        self.messages: List[Dict[str, str]] = [{"role": "system", "content": system_context}]

    def _auth_headers(self) -> Dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key_env:
            key = os.environ.get(self.api_key_env, "")
            if key:
                headers["Authorization"] = f"Bearer {key}"
        return headers

    def respond(self, message: str) -> str:
        self.messages.append({"role": "user", "content": message})

        payload: Dict[str, Any] = {"model": self.model, "messages": self.messages}
        if self.temperature is not None:
            payload["temperature"] = self.temperature
        if self.max_tokens is not None:
            payload["max_tokens"] = self.max_tokens

        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers=self._auth_headers(),
            method="POST",
        )

        log(self.name, f"openai-compatible: POST {self.base_url}/chat/completions model={self.model}")
        try:
            with urllib.request.urlopen(req, timeout=OPENAI_HTTP_TIMEOUT) as resp:
                body = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            self.messages.pop()
            detail = e.read().decode("utf-8", errors="replace")[:STDERR_TAIL_BYTES] if e.fp else ""
            return f"[openai-compatible error: HTTP {e.code}: {detail}]"
        except urllib.error.URLError as e:
            self.messages.pop()
            return f"[openai-compatible error: {e.reason}]"
        except (TimeoutError, json.JSONDecodeError, OSError) as e:
            self.messages.pop()
            return f"[openai-compatible error: {e}]"

        try:
            content = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            self.messages.pop()
            return f"[openai-compatible error: unexpected response shape: {json.dumps(body)[:STDERR_TAIL_BYTES]}]"

        self.messages.append({"role": "assistant", "content": content})
        return content


def make_backend(role: str, resolved_cfg: Dict[str, Any], cwd: str, system_context: str, name: str) -> Backend:
    backend_name = resolved_cfg.get("backend")
    if not backend_name:
        sys.exit(
            f"[{name}] Role '{role}' has no backend configured. "
            f"Check foreman.config.json and .foreman/config.json."
        )
    if backend_name == "claude-interactive":
        sys.exit(
            f"[{name}] backend 'claude-interactive' is launched by foreman.sh for the "
            f"Orchestrator only — foreman-runner.py does not run it."
        )
    if not resolved_cfg.get("model"):
        sys.exit(f"[{name}] Role '{role}' (backend '{backend_name}') has no model configured.")

    if backend_name == "claude-cli":
        return ClaudeCliBackend(resolved_cfg, cwd, system_context, name)
    if backend_name == "codex-cli":
        return CodexCliBackend(resolved_cfg, cwd, system_context, name)
    if backend_name == "openai-compatible":
        if not resolved_cfg.get("base_url"):
            sys.exit(f"[{name}] Role '{role}' backend 'openai-compatible' has no base_url configured.")
        return OpenAICompatibleBackend(resolved_cfg, cwd, system_context, name)

    sys.exit(f"[{name}] Unknown backend '{backend_name}' for role '{role}'.")


# --------------------------------------------------------------------------
# @ask directive loop + main message loop
# --------------------------------------------------------------------------

def run_ask_loop(client: RelayClient, backend: Backend, message: str, name: str) -> str:
    """Run backend.respond, resolving @ask directives against peers until the
    response is directive-free or MAX_ASK_HOPS is reached."""
    response = backend.respond(message)

    for hop in range(1, MAX_ASK_HOPS + 1):
        directives = ASK_DIRECTIVE_RE.findall(response)
        if not directives:
            return response

        log(name, f"resolving {len(directives)} @ask directive(s) (hop {hop}/{MAX_ASK_HOPS})")
        answers = []
        for peer, question in directives:
            peer = peer.strip()
            question = question.strip()
            log(name, f"@ask -> {peer}: {question[:120]}")
            answer = client.ask(peer, question)
            answers.append(f"- {peer}: {answer}")

        followup = "Answers to your asks:\n" + "\n".join(answers)
        response = backend.respond(followup)

    if ASK_DIRECTIVE_RE.search(response):
        log(name, f"max @ask hops ({MAX_ASK_HOPS}) reached; replying with directives still present")
    return response


def message_loop(client: RelayClient, backend: Backend, name: str) -> None:
    """Runs until the hub connection drops (raises ConnectionError)."""
    while True:
        msg = client.inbox_wait(timeout_ms=INBOX_WAIT_TIMEOUT_MS)
        msg_type = msg.get("type")

        if msg_type == "inbox_timeout":
            log(name, "heartbeat: still waiting for messages...")
            continue

        if msg_type == "err":
            log(name, f"err notification: {msg.get('code')}")
            continue

        if msg_type != "inbox_deliver":
            log(name, f"unexpected message type from hub: {msg_type}")
            continue

        err_code = msg.get("err_code")
        if err_code:
            log(name, f"err_code notification: {err_code}")
            continue

        from_peer = msg.get("from", "unknown")
        content = (msg.get("content") or "").strip()
        ask_id = msg.get("ask_id")

        if not content:
            continue

        log(name, f"message from {from_peer}: {content[:120]}")
        final_text = run_ask_loop(client, backend, content, name)

        if ask_id:
            client.reply(ask_id, final_text)
            log(name, f"replied to {from_peer}")
        else:
            log(name, "no ask_id on delivery; nothing to reply to")


def announce_readiness(client: RelayClient, name: str, role: str) -> None:
    result = client.ask(
        "foreman-orchestrator",
        f"{name} ({role}) is online and ready.",
        timeout_ms=READINESS_ASK_TIMEOUT_MS,
    )
    log(name, f"readiness announcement to foreman-orchestrator: {result}")


# --------------------------------------------------------------------------
# CLI / entry point
# --------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Foreman generic headless agent runner")
    parser.add_argument("--role", required=True, help="Crew role, e.g. worker, architect, cleaner")
    parser.add_argument("--project", required=True, help="Absolute path to the project directory")
    parser.add_argument("--name", default=None, help="Session name (default: foreman-<role>)")
    parser.add_argument("--cwd", default=None, help="Backend working directory (default: --project)")
    parser.add_argument("--config", default=None, help="Override the skill-level config file path")
    args = parser.parse_args()

    if not os.path.isabs(args.project):
        parser.error("--project must be an absolute path")
    if args.cwd is not None and not os.path.isabs(args.cwd):
        parser.error("--cwd must be an absolute path")
    if args.config is not None and not os.path.isabs(args.config):
        parser.error("--config must be an absolute path")

    return args


def main() -> None:
    args = parse_args()

    role = args.role
    name = args.name or f"foreman-{role}"
    project_path = Path(args.project).resolve()
    cwd = str(Path(args.cwd).resolve()) if args.cwd else str(project_path)

    skill_root = Path(__file__).resolve().parent.parent
    skill_config_path = Path(args.config).resolve() if args.config else skill_root / "foreman.config.json"

    config = load_config(skill_config_path, project_path)
    resolved_cfg = resolve_role_config(config, role)

    system_context = build_system_context(skill_root, role, name)
    backend = make_backend(role, resolved_cfg, cwd, system_context, name)

    client = RelayClient(name, cwd)

    def handle_sigterm(signum: int, frame: Any) -> None:
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, handle_sigterm)

    consecutive_failures = 0
    backoff = RECONNECT_BACKOFF_MIN
    ever_connected = False
    try:
        while True:
            try:
                log(name, "connecting to relay hub...")
                client.connect()
                log(name, "connected and registered.")
                ever_connected = True
                consecutive_failures = 0
                backoff = RECONNECT_BACKOFF_MIN

                announce_readiness(client, name, role)
                log(name, f"ready. role={role} backend={resolved_cfg.get('backend')} model={resolved_cfg.get('model')}")
                message_loop(client, backend, name)
            except FileNotFoundError as e:
                if not ever_connected:
                    # Startup phase: foreman.sh launches runners before the
                    # Orchestrator (whose relay plugin starts the hub). Wait
                    # patiently instead of burning the failure budget.
                    client.close()
                    log(name, f"hub not up yet ({e}); waiting {HUB_WAIT_INTERVAL}s...")
                    time.sleep(HUB_WAIT_INTERVAL)
                    continue
                # The hub was up before and its socket vanished — treat it as
                # a lost connection below.
                consecutive_failures += 1
                client.close()
                log(name, f"hub socket disappeared: {e}")
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    log(name, f"{MAX_CONSECUTIVE_FAILURES} consecutive connection failures; exiting.")
                    sys.exit(1)
                log(name, f"reconnecting in {backoff}s (failure {consecutive_failures}/{MAX_CONSECUTIVE_FAILURES})...")
                time.sleep(backoff)
                backoff = min(backoff * 2, RECONNECT_BACKOFF_MAX)
            except ConnectionError as e:
                consecutive_failures += 1
                client.close()
                log(name, f"relay connection lost: {e}")
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    log(name, f"{MAX_CONSECUTIVE_FAILURES} consecutive connection failures; exiting.")
                    sys.exit(1)
                log(name, f"reconnecting in {backoff}s (failure {consecutive_failures}/{MAX_CONSECUTIVE_FAILURES})...")
                time.sleep(backoff)
                backoff = min(backoff * 2, RECONNECT_BACKOFF_MAX)
    except (KeyboardInterrupt, SystemExit) as e:
        code = e.code if isinstance(e, SystemExit) else 0
        log(name, "shutting down.")
        client.close()
        sys.exit(code if isinstance(code, int) else 0)


if __name__ == "__main__":
    main()
