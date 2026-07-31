#!/usr/bin/env python3
"""End-to-end test: foreman-runner.py against a fake relay hub and a fake
OpenAI-compatible model server. Exercises register, the readiness ask,
inbox_wait/deliver, the openai-compatible backend, the @ask directive loop,
the final reply, and the traffic ledger contents."""

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

RUNNER = str(Path(__file__).resolve().parent.parent / "scripts" / "foreman-runner.py")

results = {"model_calls": [], "hub_events": [], "final_reply": None, "errors": []}


class FakeModelHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        results["model_calls"].append(body["messages"][-1]["content"])
        n = len(results["model_calls"])
        if n == 1:
            content = "Let me check.\n@ask foreman-architect: what is the plan?"
        else:
            content = "FINAL ANSWER: done"
        resp = json.dumps({"choices": [{"message": {"role": "assistant", "content": content}}]})
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(resp.encode())

    def log_message(self, *a):
        pass


_rx = b""


def read_line(conn):
    global _rx
    while b"\n" not in _rx:
        chunk = conn.recv(4096)
        if not chunk:
            return None
        _rx += chunk
    line, _rx = _rx.split(b"\n", 1)
    return json.loads(line.decode())


def send(conn, obj):
    conn.sendall((json.dumps(obj) + "\n").encode())


def fake_hub(sock_path):
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(sock_path)
    srv.listen(1)
    srv.settimeout(30)
    conn, _ = srv.accept()
    conn.settimeout(30)
    try:
        # 1. register
        msg = read_line(conn)
        results["hub_events"].append(("register", msg.get("name"), msg.get("protocol_version")))
        send(conn, {"type": "registered"})
        # 2. readiness ask to orchestrator
        msg = read_line(conn)
        results["hub_events"].append((msg.get("type"), msg.get("to")))
        send(conn, {"type": "response", "req_id": msg.get("req_id"), "text": "orchestrator: noted"})
        # 3. inbox_wait -> deliver a task
        msg = read_line(conn)
        results["hub_events"].append((msg.get("type"),))
        send(conn, {"type": "inbox_deliver", "from": "foreman-orchestrator",
                    "content": "Reframe this problem for me.", "ask_id": "ASK-1"})
        # 4. the @ask directive from the backend
        msg = read_line(conn)
        results["hub_events"].append((msg.get("type"), msg.get("to"), msg.get("text")))
        assert msg.get("type") == "ask", f"expected ask, got {msg}"
        send(conn, {"type": "response", "req_id": msg.get("req_id"), "text": "the plan is X"})
        # 5. the final reply
        msg = read_line(conn)
        results["hub_events"].append((msg.get("type"), msg.get("ask_id")))
        assert msg.get("type") == "reply", f"expected reply, got {msg}"
        results["final_reply"] = msg.get("text")
        # 6. next inbox_wait -> timeout, then close
        msg = read_line(conn)
        send(conn, {"type": "inbox_timeout"})
    except Exception as e:
        results["errors"].append(f"hub: {e!r}")
    finally:
        conn.close()
        srv.close()


def main():
    http = HTTPServer(("127.0.0.1", 0), FakeModelHandler)
    port = http.server_address[1]
    threading.Thread(target=http.serve_forever, daemon=True).start()

    tmp = Path(tempfile.mkdtemp(prefix="foreman-e2e-"))
    sock_path = str(tmp / "hub.sock")
    project = tmp / "project"
    (project / ".foreman").mkdir(parents=True)
    (project / ".foreman" / "config.json").write_text(json.dumps({
        "roles": {"muse": {"backend": "openai-compatible",
                           "base_url": f"http://127.0.0.1:{port}/v1",
                           "model": "fake-model", "api_key_env": ""}}
    }))

    hub_thread = threading.Thread(target=fake_hub, args=(sock_path,), daemon=True)
    hub_thread.start()
    time.sleep(0.2)

    env = dict(os.environ, RELAY_HUB_SOCKET=sock_path)
    proc = subprocess.Popen(
        [sys.executable, RUNNER, "--role", "muse", "--project", str(project)],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    hub_thread.join(timeout=25)
    proc.terminate()
    out, _ = proc.communicate(timeout=10)

    ledger_path = project / ".foreman" / "traffic.jsonl"
    ledger = []
    if ledger_path.exists():
        for raw in ledger_path.read_text().splitlines():
            if raw.strip():
                ledger.append(json.loads(raw))

    ok = True

    def check(cond, label):
        nonlocal ok
        print(("PASS " if cond else "FAIL ") + label)
        if not cond:
            ok = False

    check(not results["errors"], f"no hub errors {results['errors']}")
    check(("register", "foreman-muse", "2") in results["hub_events"], "registered as foreman-muse, protocol v2")
    check(any(e[0] == "ask" and e[1] == "foreman-orchestrator" for e in results["hub_events"]), "readiness ask sent")
    check(any(e[0] == "ask" and e[1] == "foreman-architect" for e in results["hub_events"]), "@ask directive relayed to architect")
    check(len(results["model_calls"]) == 2, "backend called twice (task + followup)")
    check(len(results["model_calls"]) > 1 and "the plan is X" in results["model_calls"][1], "ask answer fed back to backend")
    check(results["final_reply"] == "FINAL ANSWER: done", f"final reply correct: {results['final_reply']!r}")

    # Traffic ledger: exactly the duplicate-free record — readiness ask +
    # its answer + the inbound task + the final reply. The @ask to the
    # architect must NOT appear (the architect's runner would log it).
    expected = [
        ("foreman-muse", "foreman-orchestrator", "ask"),
        ("foreman-orchestrator", "foreman-muse", "reply"),
        ("foreman-orchestrator", "foreman-muse", "ask"),
        ("foreman-muse", "foreman-orchestrator", "reply"),
    ]
    got = [(e["from"], e["to"], e["kind"]) for e in ledger]
    check(got == expected, f"ledger has exactly the 4 expected entries in order: {got}")
    check(not any("foreman-architect" in (e["from"], e["to"]) for e in ledger),
          "@ask to architect not logged (receiver's runner owns that record)")

    if not ok:
        print("--- runner output ---")
        print(out)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
