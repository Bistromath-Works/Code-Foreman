#!/usr/bin/env python3
"""
foreman-muse-bridge.py

Connects to the Foreman relay hub as "foreman-muse" and answers lateral-thinking
questions using a locally-running model via the Ollama API.

This bypasses Hermes entirely — no MCP needed. It speaks the relay hub's
line-delimited JSON protocol directly over a Unix socket.

The Muse should run on a model with different weights than the Claude crew.
The value of the Muse comes from genuinely different reasoning — pick any
Ollama-compatible model that isn't Claude. Good options include Gemma, Llama,
Mistral, Phi, and similar open-weight models.

Usage:
    python3 foreman-muse-bridge.py [--model MODEL] [--socket SOCKET_PATH]
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

# Set this to the Ollama model name you want the Muse to use.
# Must be pulled locally first: ollama pull <model-name>
DEFAULT_MODEL = "your-muse-model-here"

OLLAMA_URL = "http://127.0.0.1:11434/api/generate"

# Hub socket candidate paths (tried in order)
SOCKET_CANDIDATES = [
    os.environ.get("RELAY_HUB_SOCKET", ""),
    os.path.expandvars(
        os.environ.get("CLAUDE_PLUGIN_DATA", "")
        + "/hub.sock"
    ),
    os.path.expanduser("~/.claude/plugins/data/relay-claude-relay/hub.sock"),
    os.path.expanduser("~/.claude-relay/hub.sock"),
]

MUSE_SYSTEM = """\
You are the Muse on a software development crew. You think sideways, not forwards.

Rules:
- ONE observation, question, or metaphor. Then stop. Do not list options.
- Never write code. Never pick a solution. Never be comprehensive.
- Reframe the problem — challenge the assumptions, not the implementation.
- 1–3 sentences maximum.
- Be human. Dry humor is welcome if it fits.

If asked to choose between approaches, ask why those are the only two.
If someone is stuck, ask whether the thing needs to exist at all.
If someone is optimising, ask what they're avoiding by optimizing instead of rethinking.\
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
            return json.loads(buf.decode("utf-8"))
        buf += chunk


def send(sock: socket.socket, obj: dict) -> None:
    sock.sendall((json.dumps(obj) + "\n").encode("utf-8"))


def ask_model(question: str, from_peer: str, model: str) -> str:
    prompt = f'Crew member "{from_peer}" asks:\n{question}'
    payload = json.dumps({
        "model": model,
        "system": MUSE_SYSTEM,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0.9},
    }).encode("utf-8")
    req = urllib.request.Request(
        OLLAMA_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            result = json.loads(resp.read())
            return result.get("response", "").strip()
    except urllib.error.URLError as e:
        return f"[Muse unavailable — Ollama not reachable: {e.reason}]"
    except Exception as e:
        return f"[Muse error: {e}]"


def run(model: str, socket_path: str) -> None:
    path = find_hub_socket(socket_path)
    print(f"[muse] Connecting to hub at {path}", flush=True)

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.connect(path)
    print("[muse] Connected.", flush=True)

    send(sock, {
        "type": "register",
        "name": "foreman-muse",
        "cwd": str(Path.cwd()),
        "git_branch": "",
        "protocol_version": PROTOCOL_VERSION,
    })
    ack = read_line(sock)
    if ack.get("type") == "err":
        print(f"[muse] Registration failed: {ack.get('code')}", flush=True)
        sys.exit(1)

    print(f"[muse] Ready. Listening for questions (model: {model})...", flush=True)

    req_id = 0
    while True:
        req_id += 1
        send(sock, {
            "type": "inbox_wait",
            "timeout_ms": 300_000,
            "req_id": f"m{req_id}",
        })

        msg = read_line(sock)
        msg_type = msg.get("type")

        if msg_type == "inbox_timeout":
            print("[muse] Still here, still listening...", flush=True)
            continue

        if msg_type != "inbox_deliver":
            print(f"[muse] Unexpected message type: {msg_type}", flush=True)
            continue

        err_code = msg.get("err_code")
        if err_code:
            print(f"[muse] Error notification: {err_code}", flush=True)
            continue

        from_peer = msg.get("from", "unknown")
        content = msg.get("content", "").strip()
        ask_id = msg.get("ask_id")

        if not content:
            continue

        print(f"\n[muse] {from_peer} asks: {content}", flush=True)
        response = ask_model(content, from_peer, model)
        print(f"[muse] Response: {response}\n", flush=True)

        if ask_id:
            send(sock, {
                "type": "reply",
                "ask_id": ask_id,
                "text": response,
            })


def main() -> None:
    parser = argparse.ArgumentParser(description="Foreman Muse bridge (via Ollama)")
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"Ollama model name (default: {DEFAULT_MODEL})")
    parser.add_argument("--socket", default="", help="Override hub socket path")
    args = parser.parse_args()

    try:
        run(args.model, args.socket)
    except KeyboardInterrupt:
        print("\n[muse] Shutting down.", flush=True)
    except FileNotFoundError as e:
        print(f"[muse] {e}", flush=True)
        sys.exit(1)
    except ConnectionError as e:
        print(f"[muse] Lost hub connection: {e}", flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
