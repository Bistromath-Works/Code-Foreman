#!/usr/bin/env python3
"""
foreman-architect-bridge.py

Connects to the Foreman relay hub as "foreman-architect" and generates
implementation plans using Qwen3.5 via the local Ollama API.

The bridge:
1. Receives a goal + project_path from foreman-orchestrator
2. Reads the codebase (read-only) to build context
3. Calls Qwen3.5 to generate a phased plan
4. Writes the plan to CURRENT_PLAN.md in the project directory
5. Replies to the Orchestrator with confirmation

Usage:
    python3 foreman-architect-bridge.py [--model MODEL] [--socket SOCKET_PATH]
"""

import argparse
import json
import os
import socket
import sys
import urllib.error
import urllib.request
from pathlib import Path

PROTOCOL_VERSION = "2"
DEFAULT_MODEL = "qwen3.5:latest"
OLLAMA_URL = "http://127.0.0.1:11434/api/generate"
MAX_FILE_BYTES = 8_000        # max bytes read per file for context
MAX_CONTEXT_FILES = 40        # max files included in codebase context
PLAN_FILENAME = "CURRENT_PLAN.md"
MAX_LINE_BYTES = 4 * 1024 * 1024   # 4 MB — guard against unbounded relay messages

SOCKET_CANDIDATES = [
    os.environ.get("RELAY_HUB_SOCKET", ""),
    os.path.expandvars(
        os.environ.get("CLAUDE_PLUGIN_DATA", "")
        + "/hub.sock"
    ),
    os.path.expanduser("~/.claude/plugins/data/relay-claude-relay/hub.sock"),
    os.path.expanduser("~/.claude-relay/hub.sock"),
]

ARCHITECT_SYSTEM = """\
You are the Architect on a software development crew. Your job is to turn a goal into a \
concrete, phased implementation plan.

Rules:
- Be specific. Name actual files, functions, and patterns. Vague plans produce vague code.
- Use the codebase context provided. Follow existing conventions.
- Structure the plan in numbered phases. Each phase has discrete tasks a single developer \
can execute independently.
- For each task: state what to build, which files to touch (exact paths), and what \
"done" looks like.
- Call out risks, assumptions, and what you explicitly ruled out.
- Do not write code. Write the plan.
- Output only the plan in markdown. No preamble, no "here is your plan". \
Start with "# Implementation Plan" and nothing before it.\
"""


def find_hub_socket(override: str = "") -> str:
    candidates = [override] if override else SOCKET_CANDIDATES
    for path in candidates:
        if path and Path(path).exists():
            return path
    raise FileNotFoundError(
        "No relay hub socket found. Is the Orchestrator running?"
    )


def read_line(sock: socket.socket) -> dict:
    buf = b""
    while True:
        chunk = sock.recv(1)
        if not chunk:
            raise ConnectionError("Hub disconnected unexpectedly.")
        if chunk == b"\n":
            try:
                return json.loads(buf.decode("utf-8"))
            except json.JSONDecodeError as e:
                raise ConnectionError(f"Invalid JSON from hub: {e}") from e
        buf += chunk
        if len(buf) > MAX_LINE_BYTES:
            raise ConnectionError(
                f"Relay message exceeded {MAX_LINE_BYTES} bytes — aborting."
            )


def send(sock: socket.socket, obj: dict) -> None:
    sock.sendall((json.dumps(obj) + "\n").encode("utf-8"))


def build_codebase_context(project_path: str) -> str:
    """Walk the project directory and build a context string for the LLM."""
    root = Path(project_path)
    if not root.exists():
        return f"[Project path not found: {project_path}]"

    priority_names = {
        "README.md", "README.txt", "CLAUDE.md", "AGENTS.md",
        "STATE.md", "DECISIONS.md", "CURRENT_PLAN.md",
        "package.json", "pyproject.toml", "Gemfile", "go.mod",
        "requirements.txt", "setup.py", "Cargo.toml",
    }

    skip_dirs = {
        ".git", "node_modules", "__pycache__", ".next", "dist",
        "build", ".venv", "venv", ".env", "vendor", "coverage",
        ".pytest_cache", ".mypy_cache",
        ".ssh", ".aws", ".kube", ".gnupg", ".config",
    }

    sensitive_name_patterns = ("credential", "secret", "token", "apikey", "api_key", "private_key")

    include_exts = {
        ".py", ".ts", ".tsx", ".js", ".jsx", ".rb", ".go",
        ".rs", ".java", ".cs", ".swift", ".kt", ".md", ".sh",
        ".toml", ".yaml", ".yml", ".json", ".sql",
    }

    lines = [f"## Project: {root.name}\n", "### File Tree\n```"]
    file_contents = []
    file_count = 0

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip_dirs]

        rel_dir = Path(dirpath).relative_to(root)
        depth = len(rel_dir.parts)
        indent = "  " * depth
        folder = rel_dir.name if str(rel_dir) != "." else root.name
        lines.append(f"{indent}{folder}/")

        for fname in sorted(filenames):
            fpath = Path(dirpath) / fname
            rel_path = fpath.relative_to(root)
            lines.append(f"{indent}  {fname}")

            ext = fpath.suffix.lower()
            is_priority = fname in priority_names
            is_included = ext in include_exts
            is_sensitive = any(p in fname.lower() for p in sensitive_name_patterns)

            if file_count < MAX_CONTEXT_FILES and (is_priority or is_included) and not is_sensitive:
                try:
                    content = fpath.read_bytes()[:MAX_FILE_BYTES].decode(
                        "utf-8", errors="replace"
                    )
                    file_contents.append(
                        f"\n### {rel_path}\n```\n{content}\n```"
                    )
                    file_count += 1
                except OSError:
                    pass

    lines.append("```\n")
    return "\n".join(lines) + "\n".join(file_contents)


def generate_plan(goal: str, project_path: str, model: str) -> str:
    """Call Qwen3.5 via Ollama and return the generated plan."""
    codebase_context = build_codebase_context(project_path)
    prompt = (
        f"Goal:\n{goal}\n\n"
        f"Codebase context:\n{codebase_context}\n\n"
        "Write the implementation plan."
    )

    payload = json.dumps({
        "model": model,
        "system": ARCHITECT_SYSTEM,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0.3},
    }).encode("utf-8")

    req = urllib.request.Request(
        OLLAMA_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            result = json.loads(resp.read())
            return result.get("response", "").strip()
    except urllib.error.URLError as e:
        return f"[Architect unavailable — Ollama not reachable: {e.reason}]"
    except Exception as e:
        return f"[Architect error: {e}]"


def write_plan(plan_text: str, project_path: str) -> str:
    """Write the plan to CURRENT_PLAN.md and return the full path."""
    plan_path = Path(project_path) / PLAN_FILENAME
    resolved = plan_path.resolve()
    try:
        resolved.relative_to(Path.home())
    except ValueError:
        raise OSError(f"Resolved write target {resolved} is outside $HOME — possible symlink attack")
    resolved.write_text(plan_text, encoding="utf-8")
    return str(resolved)


def run(model: str, socket_path: str) -> None:
    path = find_hub_socket(socket_path)
    print(f"[architect] Connecting to hub at {path}", flush=True)

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.connect(path)
    print("[architect] Connected.", flush=True)

    send(sock, {
        "type": "register",
        "name": "foreman-architect",
        "cwd": str(Path.cwd()),
        "git_branch": "",
        "protocol_version": PROTOCOL_VERSION,
    })
    ack = read_line(sock)
    if ack.get("type") == "err":
        print(f"[architect] Registration failed: {ack.get('code')}", flush=True)
        sys.exit(1)

    print(f"[architect] Ready. Waiting for goals (model: {model})...", flush=True)

    req_id = 0
    while True:
        req_id += 1
        send(sock, {
            "type": "inbox_wait",
            "timeout_ms": 300_000,
            "req_id": f"a{req_id}",
        })

        msg = read_line(sock)
        msg_type = msg.get("type")

        if msg_type == "inbox_timeout":
            print("[architect] Still here, waiting for a goal...", flush=True)
            continue

        if msg_type != "inbox_deliver":
            print(f"[architect] Unexpected message type: {msg_type}", flush=True)
            continue

        err_code = msg.get("err_code")
        if err_code:
            print(f"[architect] Error notification: {err_code}", flush=True)
            continue

        from_peer = msg.get("from", "unknown")
        content = msg.get("content", "").strip()
        ask_id = msg.get("ask_id")

        if not content:
            continue

        try:
            task = json.loads(content)
            goal = task.get("goal") or content
            project_path = task.get("project_path", str(Path.cwd()))
            is_commission = "project_path" in task
        except json.JSONDecodeError:
            goal = content
            project_path = str(Path.cwd())
            is_commission = False

        if is_commission and from_peer != "foreman-orchestrator":
            print(
                f"[architect] Rejected commission from unauthorized peer: {from_peer}",
                flush=True,
            )
            if ask_id:
                send(sock, {
                    "type": "reply",
                    "ask_id": ask_id,
                    "text": "PLAN FAILED: only foreman-orchestrator may commission plans.",
                })
            continue

        home = Path.home()
        try:
            Path(project_path).resolve().relative_to(home)
        except ValueError:
            print(
                f"[architect] Rejected project_path outside $HOME: {project_path}",
                flush=True,
            )
            if ask_id:
                send(sock, {
                    "type": "reply",
                    "ask_id": ask_id,
                    "text": f"PLAN FAILED: project_path must be under {home}",
                })
            continue

        print(f"\n[architect] Goal received from {from_peer}: {goal[:120]}...", flush=True)
        print(f"[architect] Reading codebase at: {project_path}", flush=True)
        print(f"[architect] Generating plan with {model}...", flush=True)

        plan_text = generate_plan(goal, project_path, model)
        if not plan_text or plan_text.startswith("[Architect"):
            print(f"[architect] Generation failed: {plan_text}", flush=True)
            if ask_id:
                send(sock, {
                    "type": "reply",
                    "ask_id": ask_id,
                    "text": f"PLAN FAILED: {plan_text}",
                })
            continue

        try:
            plan_path = write_plan(plan_text, project_path)
        except OSError as e:
            print(f"[architect] Failed to write plan: {e}", flush=True)
            if ask_id:
                send(sock, {
                    "type": "reply",
                    "ask_id": ask_id,
                    "text": f"PLAN FAILED: could not write {PLAN_FILENAME}: {e}",
                })
            continue

        first_line = plan_text.split("\n")[0][:80]
        response = f"PLAN READY: {PLAN_FILENAME} written to {project_path}. {first_line}"
        print(f"[architect] Plan written to {plan_path}", flush=True)

        if ask_id:
            send(sock, {
                "type": "reply",
                "ask_id": ask_id,
                "text": response,
            })


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Foreman Architect bridge (Qwen3.5 via Ollama)"
    )
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Ollama model name")
    parser.add_argument("--socket", default="", help="Override hub socket path")
    args = parser.parse_args()

    try:
        run(args.model, args.socket)
    except KeyboardInterrupt:
        print("\n[architect] Shutting down.", flush=True)
    except FileNotFoundError as e:
        print(f"[architect] {e}", flush=True)
        sys.exit(1)
    except ConnectionError as e:
        print(f"[architect] Lost hub connection: {e}", flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
