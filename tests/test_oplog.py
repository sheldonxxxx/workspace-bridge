"""Operational container logging contract and redaction."""
from __future__ import annotations

import json
import logging

import pytest
import yaml
from pathlib import Path

from workspace_bridge import oplog
from workspace_bridge.security import BridgeError


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
    record = oplog.build_record("bridge", "bridge_ready", "INFO",
                                version="0.8.4", runtime_configured=True,
                                workspace_count=3, enabled_count=2)
    assert record["component"] == "bridge" and record["event"] == "bridge_ready"
    assert record["version"] == "0.8.4" and record["workspace_count"] == 3
    assert "timestamp" in record and record["level"] == "INFO"


def test_build_record_rejects_unknown_event():
    assert oplog.build_record("bridge", "prompt_text", "INFO", run_id="x") is None


def test_build_record_drops_unsafe_fields_entirely():
    secret = "sk-proj-abcdefghijklmnop123456"
    record = oplog.build_record(
        "bridge", "boundary_reject", "INFO",
        workspace_id="ws_1", reason="out_of_scope",
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
    assert record["workspace_id"] == "ws_1" and record["action"] == "external_directory"


def test_build_record_drops_non_scalar_values():
    record = oplog.build_record("bridge", "boundary_reject", "INFO",
                                workspace_id={"nested": 1}, reason=["unsafe"],
                                code=["unsafe"])
    assert "workspace_id" not in record and "reason" not in record and "code" not in record


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
        oplog.emit(OPS_LOGGER, "INFO", "bridge", "bridge_ready", version="0.8.4")
    finally:
        OPS_LOGGER.removeHandler(broken)
    # The capture handler still received the record; nothing propagated.
    assert any(json.loads(line).get("event") == "bridge_ready" for line in captured.lines)


def test_one_line_json_records(captured):
    oplog.emit(OPS_LOGGER, "INFO", "bridge", "bridge_ready", version="0.8.4")
    assert len(captured.lines) == 1
    assert captured.lines[0].endswith("}") and "\n" not in captured.lines[0].strip()
    assert json.loads(captured.lines[0])["version"] == "0.8.4"


# ------------------------------------------------------------------ config
def test_compose_passes_log_level_to_bridge():
    root = Path(__file__).resolve().parents[1]
    cfg = yaml.safe_load((root / "compose.yaml").read_text())
    assert set(cfg["services"]) == {"bridge", "mcp-tunnel"}
    env = cfg["services"]["bridge"]["environment"]
    assert "WB_LOG_LEVEL" in env
    assert "WB_RUNTIME_ADAPTERS" not in env
    assert "WB_RUNTIME_TOKEN" not in env


def test_env_example_documents_log_level():
    root = Path(__file__).resolve().parents[1]
    text = (root / ".env.example").read_text()
    assert "WB_LOG_LEVEL" in text and "INFO" in text


def test_uvicorn_access_log_stays_disabled():
    root = Path(__file__).resolve().parents[1]
    text = (root / "workspace_bridge/cli.py").read_text()
    assert text.count("access_log=False") >= 2
    assert 'log_level="warning"' in text
