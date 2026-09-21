"""Operational container logging: contract, redaction and lifecycle sequence.

No Docker daemon is required. Python lifecycle tests drive a scripted fake
runtime; adapter EventHub transitions are covered in
runtime/opencode-adapter/test/oplog.test.mjs.
"""
from __future__ import annotations

import json
import logging

import pytest
import yaml
from pathlib import Path

from workspace_bridge import oplog
from workspace_bridge.api import Handoff
from workspace_bridge.security import BridgeError

from runtime_fakes import permission_event, permission_replied_event

OPS_LOGGER = logging.getLogger("workspace_bridge.ops")


class Capture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


@pytest.fixture
def captured():
    handler = Capture()
    previous = OPS_LOGGER.level
    OPS_LOGGER.setLevel(logging.DEBUG)
    OPS_LOGGER.addHandler(handler)
    try:
        yield handler
    finally:
        OPS_LOGGER.removeHandler(handler)
        OPS_LOGGER.setLevel(previous)


def records(handler: Capture) -> list[dict]:
    out = []
    for line in handler.lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def publish(agent_env, payload, **overrides):
    body = {**payload, **overrides}
    return agent_env["service"].call(agent_env["id"], agent_env["token"], "prepare_handoff",
                                     Handoff.model_validate(body).model_dump())


def start(agent_env, job_id, request_id="run-request-1", model=None):
    return agent_env["service"].call(agent_env["id"], agent_env["token"], "start_opencode_run",
                                     {"job_id": job_id, "request_id": request_id,
                                      "model": model, "parent_run_id": None})


def call(agent_env, tool, **args):
    return agent_env["service"].call(agent_env["id"], agent_env["token"], tool, args)


# ------------------------------------------------------------------ contract
def test_log_level_default_and_valid():
    assert oplog.log_level_from_env({}) == "INFO"
    assert oplog.log_level_from_env({"WB_LOG_LEVEL": ""}) == "INFO"
    assert oplog.log_level_from_env({"WB_LOG_LEVEL": "debug"}) == "DEBUG"
    assert oplog.parse_log_level("WARNING") == "WARNING"


@pytest.mark.parametrize("raw", ["VERBOSE", "nope", "123", "INFO WARNING", "", "inf"])
def test_log_level_invalid_fails_fast(raw):
    with pytest.raises(BridgeError):
        oplog.parse_log_level(raw)


def test_build_record_keeps_allowed_scalars():
    record = oplog.build_record("bridge", "run_state", "INFO", run_id="run_abc",
                                state="completed", count=3, session_reused=True,
                                duration_ms=1.234)
    assert record["component"] == "bridge" and record["event"] == "run_state"
    assert record["run_id"] == "run_abc" and record["count"] == 3
    assert "timestamp" in record and record["level"] == "INFO"


def test_build_record_rejects_unknown_event():
    assert oplog.build_record("bridge", "prompt_text", "INFO", run_id="x") is None


def test_build_record_drops_unsafe_fields_entirely():
    secret = "sk-proj-abcdefghijklmnop123456"
    record = oplog.build_record(
        "bridge", "permission_asked", "INFO",
        run_id="run_1", session_id="ses_1", request_id="per_1",
        action="external_directory", source="event",
        prompt="do the thing", message="hello world",
        resource="/tmp/secret-scratch/file.txt", pattern=["/tmp/**"],
        requested_patterns=["/etc/passwd"], metadata={"k": "v"},
        tool={"name": "edit"}, directory="/Users/me/projects",
        server_url="http://host:4096", password="hunter2",
        token="tok_abc", error_body="boom", text=secret,
        absolute_path="/Users/me/secret.txt",
    )
    dumped = json.dumps(record)
    for forbidden in ("prompt", "message", "resource", "requested_patterns",
                      "metadata", "tool", "directory", "server_url", "password",
                      "token", "error_body", "text", "absolute_path"):
        assert forbidden not in record, forbidden
    assert "/tmp/secret-scratch" not in dumped and secret not in dumped
    assert "pattern" not in record  # singular pattern is not an allowed field
    assert record["request_id"] == "per_1" and record["action"] == "external_directory"


def test_build_record_drops_non_scalar_values():
    record = oplog.build_record("bridge", "run_state", "INFO", run_id="r",
                                state="running", count={"nested": 1},
                                matched=["a"], code=["x"])
    assert "count" not in record and "matched" not in record and "code" not in record


def test_error_code_never_carries_raw_message():
    exc = BridgeError("upstream exploded with sk-secret /tmp/x", "upstream_broken")
    assert oplog.error_code(exc) == "upstream_broken"
    assert "exploded" not in oplog.error_code(exc)
    assert oplog.error_code(RuntimeError("raw body {token}")) == "RuntimeError"


def test_emit_never_raises(captured):
    class Broken(Capture):
        def emit(self, record):
            raise RuntimeError("handler blew up")
    broken = Broken()
    OPS_LOGGER.addHandler(broken)
    try:
        oplog.emit(OPS_LOGGER, "INFO", "bridge", "run_state", run_id="r", state="running")
    finally:
        OPS_LOGGER.removeHandler(broken)
    # The capture handler still received the record; nothing propagated.
    assert any(json.loads(line).get("event") == "run_state" for line in captured.lines)


def test_one_line_json_records(captured):
    oplog.emit(OPS_LOGGER, "INFO", "bridge", "run_state", run_id="r1", state="running")
    assert len(captured.lines) == 1
    assert captured.lines[0].endswith("}") and "\n" not in captured.lines[0].strip()
    assert json.loads(captured.lines[0])["run_id"] == "r1"


# ------------------------------------------------------- lifecycle sequence
def test_lifecycle_logs_are_ordered_and_safe(agent_env, payload, captured):
    secret_text = "final answer contains sk-proj-SECRETVALUE1234567890"
    sensitive_pattern = "/tmp/secret-scratch-XYZ/**"
    from workspace_bridge.runtime import MessageInfo
    agent_env["runtime"].messages_script = [
        MessageInfo(id="m1", role="user", created=1),
        MessageInfo(id="m2", role="assistant", created=2, completed=3,
                    text=secret_text, tools=("edit",)),
    ]
    job = publish(agent_env, payload)
    run = start(agent_env, job["id"], "log-flow-1")
    asked = permission_event(
        run["session_id"], "per_log1", permission="external_directory",
        patterns=[sensitive_pattern], always=[sensitive_pattern],
        tool={"name": "edit", "args": {"path": "/tmp/secret-scratch-XYZ/file.txt"}},
        metadata={"authorization": "Bearer tok_secret_XYZ", "path": "/tmp/secret-scratch-XYZ"},
    )
    agent_env["service"].orchestrator.handle_event(asked)
    agent_env["service"].orchestrator.handle_event(
        permission_replied_event(run["session_id"], "per_log1", reply="once"))
    agent_env["service"].orchestrator.handle_event(
        {"type": "session.idle", "session_id": run["session_id"]})
    detail = call(agent_env, "read_opencode_run", run_id=run["run_id"])
    assert detail["state"] == "completed"

    events = [r.get("event") for r in records(captured)]
    for expected in ("run_created", "dispatch_started", "permission_asked",
                     "permission_replied", "run_state"):
        assert expected in events, expected
    order = [events.index(name) for name in
             ("run_created", "dispatch_started", "permission_asked", "permission_replied")]
    assert order == sorted(order)
    # Completed state is observable with reason/count, never response text.
    completed = [r for r in records(captured)
                 if r.get("event") == "run_state" and r.get("state") == "completed"]
    assert completed and completed[0]["run_id"] == run["run_id"]

    dumped = "\n".join(captured.lines)
    assert secret_text not in dumped
    assert "SECRETVALUE" not in dumped
    assert sensitive_pattern not in dumped
    assert "/tmp/secret-scratch-XYZ" not in dumped
    assert "tok_secret_XYZ" not in dumped
    assert "requested_patterns" not in dumped
    assert str(agent_env["root"]) not in dumped


def test_permission_logs_carry_ids_action_source_decision_only(agent_env, payload, captured):
    job = publish(agent_env, payload)
    run = start(agent_env, job["id"], "log-flow-2")
    captured.lines.clear()
    asked = permission_event(run["session_id"], "per_log2", permission="external_directory",
                             patterns=["/data/requested/**"], always=["/data/always/**"])
    agent_env["service"].orchestrator.handle_event(asked)
    ask_logs = [r for r in records(captured) if r.get("event") == "permission_asked"]
    assert len(ask_logs) == 1
    assert ask_logs[0]["request_id"] == "per_log2"
    assert ask_logs[0]["action"] == "external_directory"
    assert ask_logs[0]["source"] == "event"
    assert ask_logs[0]["generation"] == "v1"
    assert set(ask_logs[0]) <= {"timestamp", "level", "component", "event", "run_id",
                                "session_id", "workspace_id", "request_id", "action",
                                "source", "generation"}

    captured.lines.clear()
    agent_env["service"].orchestrator.handle_event(
        permission_replied_event(run["session_id"], "per_log2", reply="once"))
    reply_logs = [r for r in records(captured) if r.get("event") == "permission_replied"]
    assert reply_logs and reply_logs[0]["decision"] == "once"
    assert reply_logs[0]["request_id"] == "per_log2"


def test_resync_failure_warns_and_stays_non_terminal(agent_env, payload, captured):
    from workspace_bridge.runtime import MessageInfo
    from workspace_bridge.security import BridgeError as BE
    # No durable completion evidence: this test isolates resync logging.
    agent_env["runtime"].messages_script = [MessageInfo(id="u", role="user", created=1)]
    job = publish(agent_env, payload)
    run = start(agent_env, job["id"], "log-flow-3")
    agent_env["runtime"].set_list_pending_error(
        BE("upstream exploded with secret /tmp/boom", "upstream_broken"))
    captured.lines.clear()
    detail = call(agent_env, "read_opencode_run", run_id=run["run_id"])
    assert detail["state"] in ("starting", "running")
    assert detail["permission_sync"]["status"] == "degraded"
    warnings = [r for r in records(captured) if r.get("event") == "permission_resync"]
    assert warnings and warnings[0]["status"] == "degraded"
    assert warnings[0]["level"] == "WARNING"
    assert warnings[0]["code"] == "upstream_broken"
    dumped = "\n".join(captured.lines)
    assert "exploded" not in dumped and "/tmp/boom" not in dumped


def test_unknown_event_leaves_state_unchanged_and_rejection_record_is_safe(agent_env, payload, captured):
    from workspace_bridge.runtime import MessageInfo
    # No durable completion evidence: this test isolates unknown-event handling.
    agent_env["runtime"].messages_script = [MessageInfo(id="u", role="user", created=1)]
    job = publish(agent_env, payload)
    run = start(agent_env, job["id"], "log-flow-4")
    captured.lines.clear()
    # Unknown event kinds are ignored without mutation.
    agent_env["service"].orchestrator.handle_event(
        {"type": "message.updated", "session_id": run["session_id"]})
    detail = call(agent_env, "read_opencode_run", run_id=run["run_id"])
    assert detail["state"] in ("starting", "running")
    # The pump rejection record carries a sanitized code only.
    record = oplog.build_record("bridge", "event_rejected", "WARNING", code="rejected")
    assert record["code"] == "rejected"
    assert "rejected" in json.dumps(record)


# ------------------------------------------------------------------ config
def test_compose_passes_log_level_to_both_services():
    root = Path(__file__).resolve().parents[1]
    cfg = yaml.safe_load((root / "compose.yaml").read_text())
    for name in ("bridge", "opencode-adapter"):
        env = cfg["services"][name]["environment"]
        assert "WB_LOG_LEVEL" in env, name


def test_env_example_documents_log_level():
    root = Path(__file__).resolve().parents[1]
    text = (root / ".env.example").read_text()
    assert "WB_LOG_LEVEL" in text and "INFO" in text


def test_uvicorn_access_log_stays_disabled():
    root = Path(__file__).resolve().parents[1]
    text = (root / "workspace_bridge/cli.py").read_text()
    assert text.count("access_log=False") >= 2
    assert 'log_level="warning"' in text
