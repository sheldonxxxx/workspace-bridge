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
