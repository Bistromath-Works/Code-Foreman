#!/usr/bin/env python3
"""Startup-race regression: a runner launched before the hub exists must wait
patiently (not burn its reconnect failure budget), then connect once the hub
socket appears."""

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

RUNNER = str(Path(__file__).resolve().parent.parent / "scripts" / "foreman-runner.py")

tmp = Path(tempfile.mkdtemp(prefix="foreman-startup-"))
sock_path = str(tmp / "hub.sock")
project = tmp / "project"
(project / ".foreman").mkdir(parents=True)
(project / ".foreman" / "config.json").write_text(json.dumps({
    "roles": {"muse": {"backend": "openai-compatible",
                       "base_url": "http://127.0.0.1:1/v1",
                       "model": "fake", "api_key_env": ""}}
}))

env = dict(os.environ, RELAY_HUB_SOCKET=sock_path)
proc = subprocess.Popen(
    [sys.executable, RUNNER, "--role", "muse", "--project", str(project)],
    env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
)

registered = {"ok": False}


def hub():
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(sock_path)
    srv.listen(1)
    srv.settimeout(20)
    conn, _ = srv.accept()
    conn.settimeout(20)
    buf = b""
    while b"\n" not in buf:
        buf += conn.recv(4096)
    msg = json.loads(buf.split(b"\n", 1)[0])
    registered["ok"] = msg.get("type") == "register" and msg.get("name") == "foreman-muse"
    conn.sendall(b'{"type":"registered"}\n')
    time.sleep(1)
    conn.close()
    srv.close()


# Let the runner spin against a missing socket well past the point where the
# old (buggy) behavior would already be counting reconnect failures.
time.sleep(12)
alive_before_hub = proc.poll() is None

t = threading.Thread(target=hub, daemon=True)
t.start()
t.join(timeout=20)

proc.terminate()
out, _ = proc.communicate(timeout=10)

waited_patiently = "hub not up yet" in out
pre_connect = out.split("connected and registered")[0] if "connected and registered" in out else out
no_failure_counting = "failure 1/5" not in pre_connect
connected = "connected and registered." in out

ok = True
for label, cond in [
    ("runner still alive after 12s with no hub", alive_before_hub),
    ("logged patient wait ('hub not up yet')", waited_patiently),
    ("no failure-budget burn before first connection", no_failure_counting),
    ("connected once hub appeared", connected),
    ("hub saw a valid registration", registered["ok"]),
]:
    print(("PASS " if cond else "FAIL ") + label)
    if not cond:
        ok = False

if not ok:
    print("--- runner output ---")
    print(out)
    sys.exit(1)
