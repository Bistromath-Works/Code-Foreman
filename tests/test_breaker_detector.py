#!/usr/bin/env python3
"""Circuit Breaker mechanical loop detector: trip threshold, flag delivery to
both agents, and SET-ME arbiter escalation — no real models, no live hub."""

import importlib.util
import shutil
import sys
import tempfile
from pathlib import Path

MODULE_PATH = str(Path(__file__).resolve().parent.parent / "scripts" / "foreman-runner.py")
spec = importlib.util.spec_from_file_location("foreman_runner", MODULE_PATH)
fr = importlib.util.module_from_spec(spec)
sys.modules["foreman_runner"] = fr
spec.loader.exec_module(fr)


class StubBackend:
    """Stand-in for the confirm backend: returns a fixed response, records calls."""

    def __init__(self, response: str):
        self.response = response
        self.calls = []

    def respond(self, message: str) -> str:
        self.calls.append(message)
        return self.response


class StubClient:
    """Stand-in for RelayClient.ask: captures every outbound ask, no network."""

    def __init__(self):
        self.asks = []  # list of (to, question)

    def ask(self, to, question, timeout_ms=None):
        self.asks.append((to, question))
        return f"ack from {to}"


def make_project() -> Path:
    return Path(tempfile.mkdtemp(prefix="foreman-breaker-test-"))


def arbiter_unset_cfg():
    return {
        "backend": "claude-cli",
        "model": "haiku",
        "arbiter": {
            "backend": "openai-compatible",
            "base_url": "http://127.0.0.1:11434/v1",
            "model": "SET-ME",
            "api_key_env": "",
        },
    }


def test_three_messages_never_trips():
    project = make_project()
    try:
        ledger = fr.TrafficLedger(project, "foreman-circuit-breaker")
        a, b = "foreman-worker-1", "foreman-worker-2"
        ledger.record(a, b, "ask", "I think we should use approach A")
        ledger.record(b, a, "reply", "No, approach B is better")
        ledger.record(a, b, "ask", "I still think A is right")

        confirm_backend = StubBackend("LOOP: YES\nPOSITIONS: worker-1 wants A; worker-2 wants B")
        client = StubClient()
        detector = fr.LoopDetector(
            "foreman-circuit-breaker", ledger, confirm_backend, arbiter_unset_cfg(), str(project), project
        )
        detector.poll(client)

        key = tuple(sorted((a, b)))
        state = detector.pairs[key]
        assert state.status == "WATCHING", f"expected WATCHING with 3 messages, got {state.status}"
        assert client.asks == [], f"expected no interventions below threshold, got {client.asks}"
        assert confirm_backend.calls == [], "confirm backend must not be invoked below threshold"
        print("PASS: pair with only 3 messages never trips")
    finally:
        shutil.rmtree(project, ignore_errors=True)


def test_flag_fires_at_threshold_and_goes_to_both():
    project = make_project()
    ledger = fr.TrafficLedger(project, "foreman-circuit-breaker")
    a, b = "foreman-worker-1", "foreman-worker-2"
    for i in range(3):
        ledger.record(a, b, "ask", f"I still think approach A is correct (round {i})")
        ledger.record(b, a, "reply", f"No, approach B is better (round {i})")

    confirm_backend = StubBackend("LOOP: YES\nPOSITIONS: worker-1 wants A; worker-2 wants B")
    client = StubClient()
    detector = fr.LoopDetector(
        "foreman-circuit-breaker", ledger, confirm_backend, arbiter_unset_cfg(), str(project), project
    )
    detector.poll(client)

    key = tuple(sorted((a, b)))
    state = detector.pairs[key]
    assert state.status == "FLAGGED", f"expected FLAGGED at 6-message trip, got {state.status}"
    assert len(confirm_backend.calls) == 1, "confirm backend should be called exactly once at trip"
    targets = {t for t, _ in client.asks}
    assert targets == {a, b}, f"flag should go to both agents, got {targets}"
    assert detector.flags_count == 1
    print("PASS: flag fires at the threshold and is sent to both agents")
    return project, ledger, detector, key


def test_arbitration_escalates_when_arbiter_is_set_me(project, ledger, detector, key):
    a, b = key
    for i in range(2):
        ledger.record(a, b, "ask", f"post-flag round {i}")
        ledger.record(b, a, "reply", f"post-flag reply {i}")

    client = StubClient()
    detector.poll(client)

    state = detector.pairs[key]
    assert state.status == "RESOLVED", f"expected RESOLVED after arbitration, got {state.status}"
    orchestrator_asks = [q for to, q in client.asks if to == "foreman-orchestrator"]
    assert orchestrator_asks, f"expected escalation to foreman-orchestrator, got {client.asks}"
    assert "ESCALATION" in orchestrator_asks[0]
    assert not any(to in (a, b) for to, _ in client.asks), (
        f"arbiter is unset (SET-ME) -> must escalate, never force a ruling on the agents; got {client.asks}"
    )
    assert detector.rulings_count == 1
    print("PASS: arbitration escalates to the orchestrator when the arbiter model is SET-ME")
    shutil.rmtree(project, ignore_errors=True)


if __name__ == "__main__":
    test_three_messages_never_trips()
    project, ledger, detector, key = test_flag_fires_at_threshold_and_goes_to_both()
    test_arbitration_escalates_when_arbiter_is_set_me(project, ledger, detector, key)
    print("ALL TESTS PASSED")
