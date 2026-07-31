#!/usr/bin/env python3
"""Regression: a FLAGGED pair whose window drains must reset to WATCHING, so a
later healthy exchange re-enters through confirmation instead of walking
straight into binding arbitration on a stale flag."""

import importlib.util
import sys
import time
from pathlib import Path

MODULE_PATH = str(Path(__file__).resolve().parent.parent / "scripts" / "foreman-runner.py")
spec = importlib.util.spec_from_file_location("foreman_runner_fr", MODULE_PATH)
runner = importlib.util.module_from_spec(spec)
sys.modules["foreman_runner_fr"] = runner
spec.loader.exec_module(runner)


class StubClient:
    def __init__(self):
        self.asks = []

    def ask(self, to, question, timeout_ms=0):
        self.asks.append((to, question))
        return "ok"


class StubBackend:
    def __init__(self, reply):
        self.reply = reply
        self.calls = 0

    def respond(self, message):
        self.calls += 1
        return self.reply


class NullLedger:
    path = Path("/nonexistent/traffic.jsonl")

    def record(self, *a, **k):
        pass


det = runner.LoopDetector(
    "foreman-circuit-breaker", NullLedger(), StubBackend("LOOP: YES\nPOSITIONS: A vs B"),
    {"arbiter": {"model": "SET-ME"}}, "/tmp", Path("/tmp"))
client = StubClient()

key = ("foreman-worker-1", "foreman-worker-2")
state = runner.PairState()
det.pairs[key] = state

# Simulate: pair was flagged at 8 messages, then the flag WORKED and the
# window aged out completely.
state.status = "FLAGGED"
state.flag_at_count = 8
old = time.time() - runner.LOOP_WINDOW_SECONDS - 60
for i in range(8):
    state.window.append((old, key[i % 2], f"stale loop msg {i}"))

det.poll(client)  # aging drains the window -> must reset to WATCHING
assert state.status == "WATCHING", f"expected WATCHING after drain, got {state.status}"
assert state.flag_at_count is None
print("PASS: FLAGGED pair resets to WATCHING when window drains")

# A later healthy exchange of 4 messages must NOT trigger arbitration
# (the old bug sent it straight to _arbitrate on stale flag_at_count math).
now = time.time()
for i in range(4):
    state.window.append((now, key[i % 2], f"healthy msg {i}"))
det.poll(client)
assert not client.asks, f"no intervention expected on 4 healthy messages, got {client.asks}"
assert det.confirm_backend.calls == 0, "confirm must not fire below threshold"
print("PASS: healthy 4-message exchange after reset causes no intervention")

# And at the real threshold it goes through CONFIRMATION, not arbitration.
for i in range(4, 6):
    state.window.append((now, key[i % 2], f"healthy msg {i}"))
det.poll(client)
assert det.confirm_backend.calls == 1, "trip must re-enter via the confirm step"
print("PASS: threshold re-entry goes through confirm, not stale arbitration")
