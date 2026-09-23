"""Production logging contract v2: severity mapping and redaction.

Covers structured boundary rejection, sanitized request errors, and
orphaned vs unexpected dispatch failure severity. No secrets or paths are
asserted as logged.
"""
from __future__ import annotations

import json
import logging

from workspace_bridge import oplog

OPS_LOGGER = logging.getLogger("workspace_bridge.ops")


class Capture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


def _records(handler: Capture) -> list[dict]:
    out = []
    for line in handler.lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def _capture():
    handler = Capture()
    previous = OPS_LOGGER.level
    OPS_LOGGER.setLevel(logging.DEBUG)
    OPS_LOGGER.addHandler(handler)
    return handler, previous


def test_boundary_reject_event_is_allowlisted_and_bounded():
    record = oplog.build_record("bridge", "boundary_reject", "WARNING",
                                reason="untrusted-host")
    assert record is not None
    assert record["event"] == "boundary_reject"
    assert record["level"] == "WARNING"
    assert record["reason"] == "untrusted-host"
    assert "timestamp" in record
    # Unsafe classification values are never accepted as fields.
    hostile = oplog.build_record(
        "bridge", "boundary_reject", "WARNING",
        reason="untrusted-host",
        host="someone-else.example",
        origin="https://evil.example",
        path="/api/status",
        authorization="Bearer tok_secret_XYZ",
    )
    assert hostile is not None
    dumped = json.dumps(hostile)
    assert "someone-else.example" not in dumped
    assert "evil.example" not in dumped
    assert "/api/status" not in dumped
    assert "tok_secret_XYZ" not in dumped
    assert "host" not in hostile and "origin" not in hostile and "path" not in hostile


def test_request_error_event_carries_sanitized_code_only():
    from workspace_bridge.security import BridgeError
    exc = BridgeError("upstream exploded with sk-secret /tmp/x", "upstream_broken")
    handler, previous = _capture()
    try:
        oplog.emit(OPS_LOGGER, "ERROR", "bridge", "request_error",
                   code=oplog.error_code(exc), source="mcp", action="read_file")
        recs = _records(handler)
    finally:
        OPS_LOGGER.removeHandler(handler)
        OPS_LOGGER.setLevel(previous)
    assert len(recs) == 1
    assert recs[0]["event"] == "request_error"
    assert recs[0]["level"] == "ERROR"
    assert recs[0]["code"] == "upstream_broken"
    dumped = json.dumps(recs[0])
    assert "exploded" not in dumped and "/tmp/x" not in dumped and "sk-secret" not in dumped


def test_orphan_emits_warning(agent_env, payload):
    from workspace_bridge.api import Handoff
    handler, previous = _capture()
    try:
        job = agent_env["service"].call(
            agent_env["id"], agent_env["token"], "prepare_handoff",
            Handoff.model_validate(payload).model_dump())
        run = agent_env["service"].call(
            agent_env["id"], agent_env["token"], "start_agent_run",
            {"runtime": "pi", "job_id": job["id"], "request_id": "orphan-sev-1",
             "model": None, "parent_run_id": None})
        handler.lines.clear()
        # Cancel with a positively missing session takes the explicit
        # orphan path, which must warn (recoverable degradation).
        agent_env["runtime"].session_missing = True
        try:
            detail = agent_env["service"].call(
                agent_env["id"], agent_env["token"], "cancel_agent_run",
                {"run_id": run["run_id"]})
        finally:
            agent_env["runtime"].session_missing = False
        assert detail["state"] == "orphaned"
        orphans = [r for r in _records(handler)
                   if r.get("event") == "run_state" and r.get("state") == "orphaned"]
        assert orphans and all(r["level"] == "WARNING" for r in orphans)
    finally:
        OPS_LOGGER.removeHandler(handler)
        OPS_LOGGER.setLevel(previous)


def test_unexpected_dispatch_failure_is_error_and_recoverable_is_warning(agent_env, payload):
    from workspace_bridge.api import Handoff
    from workspace_bridge.security import BridgeError
    handler, previous = _capture()
    try:
        job = agent_env["service"].call(
            agent_env["id"], agent_env["token"], "prepare_handoff",
            Handoff.model_validate(payload).model_dump())
        # Recoverable BridgeError dispatch refusal must warn, not info/error.
        agent_env["runtime"].prompt_error = BridgeError("busy", "session_busy")
        try:
            run = agent_env["service"].call(
                agent_env["id"], agent_env["token"], "start_agent_run",
                {"runtime": "pi", "job_id": job["id"], "request_id": "sev-warn-1",
                 "model": None, "parent_run_id": None})
        finally:
            agent_env["runtime"].prompt_error = None
        failures = [r for r in _records(handler) if r.get("event") == "dispatch_failed"]
        assert failures and all(r["level"] == "WARNING" for r in failures)
        assert run["run_id"]
        handler.lines.clear()
        # Unexpected internal failure must surface as ERROR without raw text.
        agent_env["runtime"].prompt_error = RuntimeError("boom raw /tmp/secret body")
        try:
            job2 = agent_env["service"].call(
                agent_env["id"], agent_env["token"], "prepare_handoff",
                Handoff.model_validate({**payload, "request_id": "sev-err-req-2"}).model_dump())
            run2 = agent_env["service"].call(
                agent_env["id"], agent_env["token"], "start_agent_run",
                {"runtime": "pi", "job_id": job2["id"], "request_id": "sev-err-2",
                 "model": None, "parent_run_id": None})
        finally:
            agent_env["runtime"].prompt_error = None
        errors = [r for r in _records(handler) if r.get("event") == "dispatch_failed"]
        assert errors and all(r["level"] == "ERROR" for r in errors)
        dumped = "\n".join(handler.lines)
        assert "boom raw" not in dumped and "/tmp/secret" not in dumped
        assert run2["run_id"]
    finally:
        OPS_LOGGER.removeHandler(handler)
        OPS_LOGGER.setLevel(previous)


def test_invalid_log_level_has_stable_code_without_echo():
    from workspace_bridge.security import BridgeError
    with __import__("pytest").raises(BridgeError) as exc_info:
        oplog.parse_log_level("sk-secret-VALUE-XYZ")
    assert exc_info.value.code == "invalid_log_level"
    assert "sk-secret-VALUE-XYZ" not in str(exc_info.value)
    assert oplog.error_code(exc_info.value) == "invalid_log_level"


def test_serve_startup_failure_is_structured_without_raw_text(tmp_path, monkeypatch, capsys):
    import pytest
    from workspace_bridge.cli import main as cli_main
    secret = "sk-secret-SERVE-XYZ-12345"
    monkeypatch.setenv("WB_LOG_LEVEL", secret)
    handler, previous = _capture()
    try:
        with pytest.raises(SystemExit) as exc_info:
            cli_main(["--state", str(tmp_path / "state"), "serve"])
        assert exc_info.value.code == 1
        recs = _records(handler)
        assert len(recs) == 1
        assert recs[0]["event"] == "process_error"
        assert recs[0]["level"] == "ERROR"
        assert recs[0]["code"] == "invalid_log_level"
        assert recs[0]["source"] == "startup"
        assert recs[0]["action"] == "serve"
        dumped = "\n".join(handler.lines) + capsys.readouterr().err
        assert secret not in dumped
    finally:
        OPS_LOGGER.removeHandler(handler)
        OPS_LOGGER.setLevel(previous)


def test_non_serve_cli_keeps_human_readable_error(tmp_path, monkeypatch, capsys):
    import pytest
    from workspace_bridge.cli import main as cli_main
    monkeypatch.setenv("WB_LOG_LEVEL", "VERBOSE-NOPE")
    with pytest.raises(SystemExit) as exc_info:
        cli_main(["--state", str(tmp_path / "state"), "doctor"])
    assert exc_info.value.code == 1
    err = capsys.readouterr().err
    assert "workspace-bridge:" in err
    assert "VERBOSE-NOPE" not in err
