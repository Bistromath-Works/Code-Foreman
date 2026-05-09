#!/usr/bin/env python3
"""
foreman-dissenter-bridge.py

Connects to the Foreman relay hub as "foreman-dissenter" and provides
adversarial plan/result review using an external model API.

The default implementation uses Google Gemini via the google-genai SDK.
To use a different provider, replace the ask_model() function and the
client setup in run() with your preferred SDK.

Requires:
    pip install google-genai
    GEMINI_API_KEY or GOOGLE_API_KEY environment variable set

Usage:
    python3 foreman-dissenter-bridge.py [--model MODEL] [--socket SOCKET_PATH]
"""

import argparse
import json
import os
import socket
import sys
from pathlib import Path

PROTOCOL_VERSION = "2"

# Set this to your chosen model's identifier.
# e.g. "gemini-2.0-flash", "gemini-1.5-pro", etc.
DEFAULT_MODEL = "your-dissenter-model-here"

SOCKET_CANDIDATES = [
    os.environ.get("RELAY_HUB_SOCKET", ""),
    os.path.expandvars(
        os.environ.get("CLAUDE_PLUGIN_DATA", "")
        + "/hub.sock"
    ),
    os.path.expanduser("~/.claude/plugins/data/relay-claude-relay/hub.sock"),
    os.path.expanduser("~/.claude-relay/hub.sock"),
]


def load_dissenter_system() -> str:
    role_file = Path(__file__).parent.parent / "references" / "roles" / "dissenter.md"
    return role_file.read_text(encoding="utf-8")


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


def ask_model(question: str, from_peer: str, chat) -> str:
    prompt = f'[From {from_peer}]: {question}'
    try:
        response = chat.send_message(prompt)
        return response.text.strip()
    except Exception as e:
        return f"[Dissenter unavailable — model error: {e}]"


def run(model: str, socket_path: str) -> None:
    try:
        from google import genai
        from google.genai import types
    except ImportError:
        print("[dissenter] Error: google-genai package not installed.")
        print("[dissenter] Install with: pip install google-genai")
        sys.exit(1)

    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        print("[dissenter] Error: GEMINI_API_KEY or GOOGLE_API_KEY environment variable not set.")
        print("[dissenter] Copy .env.local.example to .env.local and add your key.")
        sys.exit(1)

    client = genai.Client(api_key=api_key)
    system_prompt = load_dissenter_system()
    chat_config = types.GenerateContentConfig(
        system_instruction=system_prompt,
    )

    path = find_hub_socket(socket_path)
    print(f"[dissenter] Connecting to hub at {path}", flush=True)

    relay = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    relay.connect(path)
    print("[dissenter] Connected.", flush=True)

    send(relay, {
        "type": "register",
        "name": "foreman-dissenter",
        "cwd": str(Path.cwd()),
        "git_branch": "",
        "protocol_version": PROTOCOL_VERSION,
    })
    ack = read_line(relay)
    if ack.get("type") == "err":
        print(f"[dissenter] Registration failed: {ack.get('code')}", flush=True)
        sys.exit(1)

    print(f"[dissenter] Ready. Listening for reviews (model: {model})...", flush=True)

    # One chat session per peer preserves conversation history across exchanges.
    # History resets on bridge restart.
    chats: dict[str, object] = {}
    req_id = 0

    while True:
        req_id += 1
        send(relay, {
            "type": "inbox_wait",
            "timeout_ms": 300_000,
            "req_id": f"d{req_id}",
        })

        msg = read_line(relay)
        msg_type = msg.get("type")

        if msg_type == "inbox_timeout":
            print("[dissenter] Still here, still listening...", flush=True)
            continue

        if msg_type != "inbox_deliver":
            print(f"[dissenter] Unexpected message type: {msg_type}", flush=True)
            continue

        err_code = msg.get("err_code")
        if err_code:
            print(f"[dissenter] Error notification: {err_code}", flush=True)
            continue

        from_peer = msg.get("from", "unknown")
        content = msg.get("content", "").strip()
        ask_id = msg.get("ask_id")

        if not content:
            continue

        print(f"\n[dissenter] {from_peer} asks: {content[:120]}...", flush=True)

        if from_peer not in chats:
            chats[from_peer] = client.chats.create(model=model, config=chat_config)

        response = ask_model(content, from_peer, chats[from_peer])
        print(f"[dissenter] Response: {response[:120]}...\n", flush=True)

        if ask_id:
            send(relay, {
                "type": "reply",
                "ask_id": ask_id,
                "text": response,
            })


def main() -> None:
    parser = argparse.ArgumentParser(description="Foreman Dissenter bridge")
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"Model name (default: {DEFAULT_MODEL})")
    parser.add_argument("--socket", default="", help="Override hub socket path")
    args = parser.parse_args()

    try:
        run(args.model, args.socket)
    except KeyboardInterrupt:
        print("\n[dissenter] Shutting down.", flush=True)
    except FileNotFoundError as e:
        print(f"[dissenter] {e}", flush=True)
        sys.exit(1)
    except ConnectionError as e:
        print(f"[dissenter] Lost hub connection: {e}", flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
