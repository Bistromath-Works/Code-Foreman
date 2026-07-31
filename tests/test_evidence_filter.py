#!/usr/bin/env python3
"""Evidence-packet safety: files referenced in a disputed transcript are only
included if they resolve inside the project AND are not sensitive — secrets
must never be shipped to a (possibly third-party) arbiter endpoint."""

import importlib.util
import shutil
import sys
import tempfile
from pathlib import Path

MODULE_PATH = str(Path(__file__).resolve().parent.parent / "scripts" / "foreman-runner.py")
spec = importlib.util.spec_from_file_location("foreman_runner_ev", MODULE_PATH)
fr = importlib.util.module_from_spec(spec)
sys.modules["foreman_runner_ev"] = fr
spec.loader.exec_module(fr)


class NullLedger:
    def __init__(self, project):
        self.path = project / ".foreman" / "traffic.jsonl"

    def record(self, *a, **k):
        pass


project = Path(tempfile.mkdtemp(prefix="foreman-evidence-"))
try:
    (project / "config").mkdir()
    (project / "src").mkdir()
    (project / "src" / "auth.py").write_text("def login(): pass\n")
    (project / "config" / ".env").write_text("DB_PASSWORD=hunter2\n")
    (project / "config" / "credentials.json").write_text('{"aws_secret": "xyz"}\n')
    (project / "server.pem").write_text("-----BEGIN PRIVATE KEY-----\n")
    (project / "CURRENT_PLAN.md").write_text("# Plan\n")

    det = fr.LoopDetector(
        "foreman-circuit-breaker", NullLedger(project), None, {}, str(project), project
    )

    transcript = (
        "[foreman-worker-1] the bug is in src/auth.py, check config/.env for the DB settings\n"
        "[foreman-worker-2] no, look at config/credentials.json and server.pem instead\n"
        "[foreman-worker-1] src/auth.py is definitely the problem"
    )

    refs = det._referenced_files(transcript)
    names = [p.name for p in refs]
    assert names == ["auth.py"], f"only the code file should survive the filter, got {names}"
    print("PASS: sensitive files (.env, credentials.json, .pem) excluded from evidence")

    packet = det._build_evidence_packet("foreman-worker-1", "foreman-worker-2", transcript)
    assert "hunter2" not in packet, "secret leaked into evidence packet"
    assert "aws_secret" not in packet, "credential file leaked into evidence packet"
    assert "BEGIN PRIVATE KEY" not in packet, "private key leaked into evidence packet"
    assert "def login" in packet, "legitimate referenced code file missing from evidence packet"
    assert "# Plan" in packet, "CURRENT_PLAN.md missing from evidence packet"
    print("PASS: evidence packet contains the code and plan but no secrets")

    # Escape attempts resolve outside the project and are rejected.
    outside = det._referenced_files("[a] see ../../etc/passwd and /etc/hostname ok")
    assert outside == [], f"paths escaping the project must be rejected, got {outside}"
    print("PASS: path-escape attempts rejected")
finally:
    shutil.rmtree(project, ignore_errors=True)
