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
from collections import deque
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
TRAFFIC_CONTENT_CAP = 2000

# Circuit breaker: sliding-window loop detection tuning (see architecture.md,
# "Traffic Ledger and the Circuit Breaker").
BREAKER_POLL_TIMEOUT_MS = 2_000
LOOP_WINDOW_SECONDS = 15 * 60       # 900s
LOOP_WINDOW_MAX = 20
LOOP_TRIP_TOTAL = 6
LOOP_TRIP_PER_SIDE = 3
LOOP_SUPPRESS_RECHECK_MESSAGES = 4
LOOP_ARBITRATION_AFTER_MESSAGES = 4
EVIDENCE_FILE_CAP_BYTES = 16 * 1024
EVIDENCE_REF_FILE_CAP_BYTES = 8 * 1024
EVIDENCE_MAX_REF_FILES = 5
ARBITER_SYSTEM_CONTEXT = (
    "You are the arbiter of last resort for a Foreman software delivery crew. "
    "Two AI agents have been stuck in a repetitive disagreement, were flagged by the "
    "Circuit Breaker, and have failed to resolve it themselves. You will be given the "
    "disputed transcript plus supporting project evidence. Pick the position with the "
    "stronger justification and issue a short, final, binding ruling with brief "
    "reasoning. Do not hedge, do not ask follow-up questions, and do not propose a "
    "third option — commit to one of the two positions and say so plainly."
)

ASK_DIRECTIVE_RE = re.compile(r"^@ask\s+(\S+):\s*(.+)$", re.MULTILINE)
PATH_TOKEN_RE = re.compile(r"[A-Za-z0-9_./-]+\.[A-Za-z0-9]{1,8}")


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
# Traffic ledger — flight recorder for post-mortems and the Circuit Breaker.
# --------------------------------------------------------------------------

class TrafficLedger:
    """Appends one JSON line per message to `<project>/.foreman/traffic.jsonl`.

    Writes are a single `os.open(O_APPEND|O_CREAT|O_WRONLY)` + `os.write`,
    atomic for small lines with no locking needed. Best-effort: every failure
    is logged and swallowed — a broken ledger must never crash the runner.
    """

    def __init__(self, project_path: Path, name: str):
        self.name = name
        self.path = project_path / ".foreman" / "traffic.jsonl"
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            log(name, f"traffic ledger: could not create {self.path.parent}: {e}")

    def record(self, sender: str, to: str, kind: str, content: str) -> None:
        try:
            line = json.dumps({
                "ts": time.time(),
                "from": sender,
                "to": to,
                "kind": kind,
                "content": (content or "")[:TRAFFIC_CONTENT_CAP],
            }) + "\n"
            data = line.encode("utf-8")
            fd = os.open(str(self.path), os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o644)
            try:
                os.write(fd, data)
            finally:
                os.close(fd)
        except Exception as e:  # noqa: BLE001 — ledger writes must never crash the runner
            log(self.name, f"traffic ledger write failed: {e}")


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


def make_backend(
    role: str,
    resolved_cfg: Dict[str, Any],
    cwd: str,
    system_context: str,
    name: str,
    strict: bool = True,
) -> Backend:
    """Build a Backend from a resolved role config.

    strict=True (default, used for a crew member's own backend): a bad config
    is fatal — sys.exit, matching prior behavior. strict=False (used for the
    Circuit Breaker's arbiter, which must degrade to escalation rather than
    take the breaker down): raises RuntimeError instead so the caller can
    catch it and escalate.
    """

    def fail(msg: str) -> None:
        if strict:
            sys.exit(msg)
        raise RuntimeError(msg)

    backend_name = resolved_cfg.get("backend")
    if not backend_name:
        fail(
            f"[{name}] Role '{role}' has no backend configured. "
            f"Check foreman.config.json and .foreman/config.json."
        )
    if backend_name == "claude-interactive":
        fail(
            f"[{name}] backend 'claude-interactive' is launched by foreman.sh for the "
            f"Orchestrator only — foreman-runner.py does not run it."
        )
    if not resolved_cfg.get("model"):
        fail(f"[{name}] Role '{role}' (backend '{backend_name}') has no model configured.")

    if backend_name == "claude-cli":
        return ClaudeCliBackend(resolved_cfg, cwd, system_context, name)
    if backend_name == "codex-cli":
        return CodexCliBackend(resolved_cfg, cwd, system_context, name)
    if backend_name == "openai-compatible":
        if not resolved_cfg.get("base_url"):
            fail(f"[{name}] Role '{role}' backend 'openai-compatible' has no base_url configured.")
        return OpenAICompatibleBackend(resolved_cfg, cwd, system_context, name)

    fail(f"[{name}] Unknown backend '{backend_name}' for role '{role}'.")
    raise AssertionError("unreachable")  # fail() always raises/exits


# --------------------------------------------------------------------------
# @ask directive loop + main message loop
# --------------------------------------------------------------------------

def run_ask_loop(
    client: RelayClient, backend: Backend, message: str, name: str, ledger: TrafficLedger
) -> str:
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
            # Ledger rule: an outbound ask is only logged when addressed to
            # foreman-orchestrator (the one peer with no runner of its own to
            # log the other end) — see architecture.md.
            if peer == "foreman-orchestrator":
                ledger.record(name, peer, "ask", question)
            answer = client.ask(peer, question)
            if peer == "foreman-orchestrator":
                ledger.record(peer, name, "reply", answer)
            answers.append(f"- {peer}: {answer}")

        followup = "Answers to your asks:\n" + "\n".join(answers)
        response = backend.respond(followup)

    if ASK_DIRECTIVE_RE.search(response):
        log(name, f"max @ask hops ({MAX_ASK_HOPS}) reached; replying with directives still present")
    return response


def message_loop(client: RelayClient, backend: Backend, name: str, ledger: TrafficLedger) -> None:
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
        # Ledger rule: every inbound delivery is logged by the receiver's own
        # runner (the sender's runner does not log it) — see architecture.md.
        ledger.record(from_peer, name, "ask", content)
        final_text = run_ask_loop(client, backend, content, name, ledger)

        if ask_id:
            client.reply(ask_id, final_text)
            ledger.record(name, from_peer, "reply", final_text)
            log(name, f"replied to {from_peer}")
        else:
            log(name, "no ask_id on delivery; nothing to reply to")


def announce_readiness(client: RelayClient, name: str, role: str, ledger: TrafficLedger) -> None:
    question = f"{name} ({role}) is online and ready."
    ledger.record(name, "foreman-orchestrator", "ask", question)
    result = client.ask("foreman-orchestrator", question, timeout_ms=READINESS_ASK_TIMEOUT_MS)
    ledger.record("foreman-orchestrator", name, "reply", result)
    log(name, f"readiness announcement to foreman-orchestrator: {result}")


# --------------------------------------------------------------------------
# Circuit Breaker: mechanical detection, LLM judgment.
#
# The breaker does not run the generic message_loop. It tails the traffic
# ledger, detects loops with pure code (sliding 15-minute window per agent
# pair), and only calls a model at the moment of judgment (confirm a trip,
# or arbitrate past the flag). See architecture.md, "Traffic Ledger and the
# Circuit Breaker".
# --------------------------------------------------------------------------

class PairState:
    """Sliding-window state for one unordered agent pair."""

    def __init__(self) -> None:
        self.window: "deque[Tuple[float, str, str]]" = deque()  # (ts, from, content)
        self.status: str = "WATCHING"          # WATCHING | FLAGGED | RESOLVED
        self.suppress_until_count: Optional[int] = None
        self.flag_at_count: Optional[int] = None


class LoopDetector:
    """Pure-code loop detection over the traffic ledger, with LLM judgment
    calls only at trip (confirm) and past-flag (arbitrate) moments.

    Takes the relay client and both backends as injected collaborators so it
    is testable without a live hub or real models.
    """

    def __init__(
        self,
        name: str,
        ledger: TrafficLedger,
        confirm_backend: Backend,
        resolved_cfg: Dict[str, Any],
        cwd: str,
        project_path: Path,
    ) -> None:
        self.name = name
        self.ledger = ledger
        self.confirm_backend = confirm_backend
        self.resolved_cfg = resolved_cfg
        self.cwd = cwd
        self.project_path = project_path

        self.ledger_path = ledger.path
        self.offset = 0
        self.pairs: Dict[Tuple[str, str], PairState] = {}

        self.total_messages = 0
        self.flags_count = 0
        self.rulings_count = 0  # forced rulings + escalations

    # -- status ------------------------------------------------------------

    def status_summary(self) -> str:
        active = sum(1 for s in self.pairs.values() if s.window)
        return (
            f"Circuit Breaker: monitoring — {self.total_messages} ledger messages, "
            f"{active} active pairs, {self.flags_count} flags, "
            f"{self.rulings_count} rulings/escalations this job."
        )

    # -- ledger ingestion ----------------------------------------------------

    def ingest_new_lines(self) -> None:
        if not self.ledger_path.exists():
            return
        try:
            with self.ledger_path.open("rb") as f:
                f.seek(self.offset)
                data = f.read()
        except OSError as e:
            log(self.name, f"traffic ledger read failed: {e}")
            return
        if not data:
            return

        # Only consume complete lines; a trailing partial line (the ledger
        # writer mid-write) is left for the next poll.
        last_nl = data.rfind(b"\n")
        if last_nl == -1:
            return
        complete, self.offset = data[: last_nl + 1], self.offset + last_nl + 1

        for raw in complete.split(b"\n"):
            if not raw.strip():
                continue
            try:
                entry = json.loads(raw.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as e:
                log(self.name, f"skipping malformed ledger line: {e}")
                continue
            self.total_messages += 1
            self._observe(entry)

    def _observe(self, entry: Dict[str, Any]) -> None:
        frm, to = entry.get("from"), entry.get("to")
        if not frm or not to:
            return
        # The breaker is a party to its own status replies and readiness
        # traffic — excluded from detection since neither side of a loop can
        # be the breaker itself.
        if frm == self.name or to == self.name:
            return
        content = entry.get("content", "") or ""
        ts = entry.get("ts", time.time())
        try:
            ts = float(ts)
        except (TypeError, ValueError):
            ts = time.time()

        key = tuple(sorted((frm, to)))
        state = self.pairs.setdefault(key, PairState())
        state.window.append((ts, frm, content))

    # -- aging + dispatch ----------------------------------------------------

    def poll(self, client: RelayClient) -> None:
        self.ingest_new_lines()
        for key, state in list(self.pairs.items()):
            self._age(state)
            if not state.window and state.status != "WATCHING":
                # Window emptied naturally — the dispute is over (RESOLVED, or
                # a flag that worked). Reset so a later, healthy exchange
                # between the same pair starts from scratch instead of walking
                # straight into arbitration on a stale flag.
                state.status = "WATCHING"
                state.flag_at_count = None
                state.suppress_until_count = None
            try:
                self._process_pair(client, key, state)
            except Exception as e:  # noqa: BLE001 — one bad pair must not kill the breaker
                log(self.name, f"loop detector error on pair {key} (continuing): {e}")

    @staticmethod
    def _age(state: PairState) -> None:
        cutoff = time.time() - LOOP_WINDOW_SECONDS
        while state.window and state.window[0][0] < cutoff:
            state.window.popleft()
        while len(state.window) > LOOP_WINDOW_MAX:
            state.window.popleft()

    def _process_pair(self, client: RelayClient, key: Tuple[str, str], state: PairState) -> None:
        a, b = key
        if state.status == "WATCHING":
            total = len(state.window)
            if state.suppress_until_count is not None:
                if total < state.suppress_until_count:
                    return
                state.suppress_until_count = None
            count_a = sum(1 for _, frm, _ in state.window if frm == a)
            count_b = sum(1 for _, frm, _ in state.window if frm == b)
            if total >= LOOP_TRIP_TOTAL and count_a >= LOOP_TRIP_PER_SIDE and count_b >= LOOP_TRIP_PER_SIDE:
                self._confirm(client, key, state)
        elif state.status == "FLAGGED":
            since_flag = len(state.window) - (state.flag_at_count or 0)
            if since_flag >= LOOP_ARBITRATION_AFTER_MESSAGES:
                self._arbitrate(client, key, state)
        # RESOLVED: nothing to do until poll()'s aging pass empties the window.

    # -- transcript helpers ---------------------------------------------------

    @staticmethod
    def _format_transcript(window: "deque[Tuple[float, str, str]]") -> str:
        return "\n".join(f"[{frm}] {content}" for _, frm, content in window)

    @staticmethod
    def _extract_positions(confirm_output: str) -> str:
        m = re.search(r"POSITIONS:\s*(.+)", confirm_output, re.IGNORECASE | re.DOTALL)
        if m:
            return f"Positions — {m.group(1).strip()[:500]}"
        return "Both agents appear to be repeating their positions without new information."

    # -- trip -> confirm ------------------------------------------------------

    def _confirm(self, client: RelayClient, key: Tuple[str, str], state: PairState) -> None:
        a, b = key
        transcript = self._format_transcript(state.window)
        prompt = (
            f"Transcript between {a} and {b}:\n\n{transcript}\n\n"
            "Are these two agents stuck in a repetitive loop (repeating the same "
            "disagreement without progress)? First line: LOOP: YES or LOOP: NO. "
            "If YES, then POSITIONS: <one-sentence summary of each side>."
        )
        try:
            result = self.confirm_backend.respond(prompt) or ""
        except Exception as e:  # noqa: BLE001 — judgment failures must not crash the breaker
            log(self.name, f"confirm backend call failed for pair {key}: {e}")
            result = ""

        if re.search(r"LOOP:\s*YES", result, re.IGNORECASE):
            self._flag(client, key, state, result)
        else:
            # NO, or unparseable — suppress until 4 more messages accumulate.
            state.suppress_until_count = len(state.window) + LOOP_SUPPRESS_RECHECK_MESSAGES

    def _flag(self, client: RelayClient, key: Tuple[str, str], state: PairState, confirm_output: str) -> None:
        a, b = key
        positions = self._extract_positions(confirm_output)
        flag_text = (
            f"Circuit Breaker: a loop has been detected between {a} and {b}. "
            f"{positions} Resolve this directly in your next exchange, or a "
            f"binding resolution will be forced."
        )
        for agent in (a, b):
            self.ledger.record(self.name, agent, "ask", flag_text)
            answer = client.ask(agent, flag_text)
            self.ledger.record(agent, self.name, "reply", answer)

        state.status = "FLAGGED"
        state.flag_at_count = len(state.window)
        self.flags_count += 1

    # -- flag -> arbitrate ------------------------------------------------------

    def _arbitrate(self, client: RelayClient, key: Tuple[str, str], state: PairState) -> None:
        a, b = key
        transcript = self._format_transcript(state.window)

        if "foreman-orchestrator" in (a, b):
            msg = (
                f"CIRCUIT BREAKER ESCALATION: loop between {a} and {b} persists past the "
                f"flag; per protocol the owner must decide.\n\nTranscript:\n{transcript}"
            )
            self._escalate(client, msg)
            state.status = "RESOLVED"
            self.rulings_count += 1
            return

        arbiter_backend, unavailable_reason = self._build_arbiter_backend()
        if arbiter_backend is None:
            msg = (
                f"CIRCUIT BREAKER ESCALATION: loop between {a} and {b} persists past the "
                f"flag, and {unavailable_reason}; per protocol the owner must decide.\n\n"
                f"Transcript:\n{transcript}"
            )
            self._escalate(client, msg)
            state.status = "RESOLVED"
            self.rulings_count += 1
            return

        evidence = self._build_evidence_packet(a, b, transcript)
        arb_prompt = (
            f"{evidence}\n\nPick the position with the stronger justification and issue "
            f"a final, binding ruling with brief reasoning."
        )
        try:
            ruling = arbiter_backend.respond(arb_prompt) or ""
        except Exception as e:  # noqa: BLE001
            ruling = f"[arbiter exception: {e}]"

        if not ruling.strip() or ruling.lstrip().startswith("["):
            msg = (
                f"CIRCUIT BREAKER ESCALATION: loop between {a} and {b} persists past the "
                f"flag, and the arbiter failed to produce a ruling ({ruling[:300]}); per "
                f"protocol the owner must decide.\n\nTranscript:\n{transcript}"
            )
            self._escalate(client, msg)
            state.status = "RESOLVED"
            self.rulings_count += 1
            return

        ruling_text = "CIRCUIT BREAKER FORCED RESOLUTION (binding): " + ruling
        for agent in (a, b):
            self.ledger.record(self.name, agent, "ask", ruling_text)
            answer = client.ask(agent, ruling_text)
            self.ledger.record(agent, self.name, "reply", answer)

        notify = f"Circuit Breaker forced resolution between {a} and {b}: {ruling[:800]}"
        self.ledger.record(self.name, "foreman-orchestrator", "ask", notify)
        ans = client.ask("foreman-orchestrator", notify)
        self.ledger.record("foreman-orchestrator", self.name, "reply", ans)

        state.status = "RESOLVED"
        self.rulings_count += 1

    def _escalate(self, client: RelayClient, message: str) -> None:
        self.ledger.record(self.name, "foreman-orchestrator", "ask", message)
        answer = client.ask("foreman-orchestrator", message)
        self.ledger.record("foreman-orchestrator", self.name, "reply", answer)

    def _build_arbiter_backend(self) -> Tuple[Optional[Backend], Optional[str]]:
        """Build the arbiter backend from the role's `arbiter` sub-config.
        Defaults do NOT apply — it stands alone. Never raises: returns
        (None, reason) on any misconfiguration or construction failure so the
        caller can degrade to escalation."""
        arbiter_cfg = self.resolved_cfg.get("arbiter")
        if not arbiter_cfg or not isinstance(arbiter_cfg, dict):
            return None, "the arbiter is not configured"

        arbiter_cfg = _strip_comments(arbiter_cfg)
        model = arbiter_cfg.get("model")
        if not model or model == "SET-ME":
            return None, "the arbiter model is not set (still SET-ME)"

        try:
            backend = make_backend(
                "circuit-breaker-arbiter",
                arbiter_cfg,
                self.cwd,
                ARBITER_SYSTEM_CONTEXT,
                self.name,
                strict=False,
            )
        except Exception as e:  # noqa: BLE001 — arbiter misconfiguration must degrade, not crash
            return None, f"the arbiter could not be constructed ({e})"
        return backend, None

    def _build_evidence_packet(self, a: str, b: str, transcript: str) -> str:
        parts = [f"Transcript between {a} and {b}:\n{transcript}"]
        for fname in ("CURRENT_PLAN.md", "DECISIONS.md"):
            fpath = self.project_path / fname
            if fpath.is_file():
                try:
                    text = fpath.read_text(encoding="utf-8", errors="replace")[:EVIDENCE_FILE_CAP_BYTES]
                    parts.append(f"--- {fname} ---\n{text}")
                except OSError as e:
                    log(self.name, f"could not read {fname} for evidence packet: {e}")
        for ref in self._referenced_files(transcript):
            try:
                text = ref.read_text(encoding="utf-8", errors="replace")[:EVIDENCE_REF_FILE_CAP_BYTES]
                try:
                    label = str(ref.relative_to(self.project_path))
                except ValueError:
                    label = str(ref)
                parts.append(f"--- {label} ---\n{text}")
            except OSError as e:
                log(self.name, f"could not read referenced file {ref} for evidence packet: {e}")
        return "\n\n".join(parts)

    def _referenced_files(self, transcript: str) -> List[Path]:
        """Path-like tokens with an extension, resolved under the project
        directory only — anything resolving outside it is rejected. Up to
        EVIDENCE_MAX_REF_FILES existing files."""
        project_root = self.project_path.resolve()
        found: List[Path] = []
        seen = set()
        for token in PATH_TOKEN_RE.findall(transcript):
            token = token.strip(".,:;()[]{}\"'")
            if not token or token in seen:
                continue
            seen.add(token)
            try:
                candidate = (project_root / token).resolve()
                candidate.relative_to(project_root)  # raises if it escapes the project dir
            except (OSError, ValueError):
                continue
            if candidate.is_file():
                found.append(candidate)
            if len(found) >= EVIDENCE_MAX_REF_FILES:
                break
        return found


def breaker_loop(
    client: RelayClient,
    confirm_backend: Backend,
    name: str,
    resolved_cfg: Dict[str, Any],
    cwd: str,
    project_path: Path,
    ledger: TrafficLedger,
) -> None:
    """Circuit Breaker's loop, run instead of message_loop. Answers status
    asks mechanically (no LLM); otherwise tails the ledger and runs the loop
    detector. Runs until the hub connection drops (raises ConnectionError)."""
    detector = LoopDetector(name, ledger, confirm_backend, resolved_cfg, cwd, project_path)

    while True:
        msg = client.inbox_wait(timeout_ms=BREAKER_POLL_TIMEOUT_MS)
        msg_type = msg.get("type")

        if msg_type == "inbox_deliver":
            err_code = msg.get("err_code")
            if err_code:
                log(name, f"err_code notification: {err_code}")
                continue

            from_peer = msg.get("from", "unknown")
            content = (msg.get("content") or "").strip()
            ask_id = msg.get("ask_id")
            if content:
                ledger.record(from_peer, name, "ask", content)

            if ask_id:
                status = detector.status_summary()
                client.reply(ask_id, status)
                ledger.record(name, from_peer, "reply", status)
                log(name, f"status reply to {from_peer}: {status}")
            continue

        if msg_type == "err":
            log(name, f"err notification: {msg.get('code')}")
            continue

        if msg_type != "inbox_timeout":
            log(name, f"unexpected message type from hub: {msg_type}")
            continue

        # inbox_timeout: no message waiting — process the ledger.
        try:
            detector.poll(client)
        except Exception as e:  # noqa: BLE001 — the breaker must survive a bad detector cycle
            log(name, f"breaker detector cycle failed (continuing): {e}")


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

    ledger = TrafficLedger(project_path, name)
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

                announce_readiness(client, name, role, ledger)
                log(name, f"ready. role={role} backend={resolved_cfg.get('backend')} model={resolved_cfg.get('model')}")
                if role == "circuit-breaker":
                    breaker_loop(client, backend, name, resolved_cfg, cwd, project_path, ledger)
                else:
                    message_loop(client, backend, name, ledger)
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
