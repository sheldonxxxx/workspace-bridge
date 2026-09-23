"""Operational container logging contract (Python side).

One-line JSON records for Docker json-file logs. No logging dependency beyond
the standard library.

Shared level semantics (Python Bridge and native Pi adapter):
- DEBUG: high-frequency/repeated internals, polls/resyncs/probes, normal SDK
  tool-event tracing. Temporary troubleshooting only.
- INFO: healthy/expected lifecycle transitions (service ready, run/session
  created, dispatch started, permission state transitions, successful
  completion/recovery). Normal production level.
- WARNING: recoverable degradation, policy/input rejection worth operator
  attention, event-stream/runtime unavailability after grace, dispatch
  refusal, orphaning, SDK dispatch stall/journal anomaly. Alert candidates.
- ERROR: unexpected internal/runtime exception or unsafe startup condition
  requiring operator action. Alert candidates.
Supported levels are DEBUG/INFO/WARNING/ERROR with INFO default.

Security boundary: only explicit allowlisted scalar fields are ever emitted.
Prompts, message/final-response text, file contents, absolute paths,
permission resource/pattern/requested_patterns/metadata, tool arguments,
workspace roots/names, user queries, webhook URLs, tokens, usernames/passwords,
raw HTTP/error bodies and arbitrary input dictionaries are never accepted into
a record. Unknown keyword arguments are dropped, not serialized.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

from .security import BridgeError

LOG_LEVEL_ENV = "WB_LOG_LEVEL"
DEFAULT_LOG_LEVEL = "INFO"
ALLOWED_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")

COMPONENT = "bridge"

# Stable event names emitted by the bridge process.
EVENTS = frozenset({
    "bridge_ready",
    "run_created",
    "dispatch_started",
    "dispatch_failed",
    "boundary_reject",
    "request_error",
    "run_state",
    "permission_asked",
    "permission_replied",
    "permission_resync",
    "question_resync",
    "completion_probe",
    "completion_reconcile",
    "event_stream_health",
    "event_pump_error",
    "event_rejected",
    "reconcile_start",
    "reconcile_result",
    "startup_reconcile",
    "process_error",
})

# Bounded scalar fields only. IDs/state names/counts/durations/sanitized
# codes and boolean health flags are acceptable; everything else is dropped.
ALLOWED_FIELDS = frozenset({
    "run_id", "session_id", "job_id", "workspace_id", "request_id",
    "state", "reason", "code", "status", "action", "source", "decision",
    "generation",
    "matched", "count", "examined", "duration_ms", "transitions",
    "consecutive_failures", "version", "adapter_version", "runtime_configured",
    "server_configured", "locked", "workspace_count", "enabled_count",
    "model", "session_reused", "message_count", "pending_count",
})

_MAX_STR = 200


INVALID_LOG_LEVEL_CODE = "invalid_log_level"


def parse_log_level(raw: Any) -> str:
    """Validate WB_LOG_LEVEL. Invalid values fail fast with BridgeError."""
    text = str(raw if raw is not None else DEFAULT_LOG_LEVEL).strip().upper()
    if text not in ALLOWED_LEVELS:
        # Stable sanitized code; the bad value is never echoed (it could
        # carry secrets in a misconfigured environment).
        raise BridgeError(
            f"{LOG_LEVEL_ENV} must be one of {', '.join(ALLOWED_LEVELS)} "
            f"(default {DEFAULT_LOG_LEVEL})", INVALID_LOG_LEVEL_CODE)
    return text


def log_level_from_env(environ: dict | None = None) -> str:
    env = environ if environ is not None else os.environ
    raw = env.get(LOG_LEVEL_ENV, DEFAULT_LOG_LEVEL)
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return DEFAULT_LOG_LEVEL
    return parse_log_level(raw)


def configure_operational_logging(level: str | None = None,
                                  environ: dict | None = None) -> str:
    """Configure application logging so INFO records reach stdout/stderr.

    Uvicorn itself stays warning-level (callers keep access_log=False and
    log_level="warning"); this only ensures the workspace_bridge loggers
    emit at the configured level with a plain one-line format.
    """
    resolved = parse_log_level(level) if level is not None else log_level_from_env(environ)
    numeric = getattr(logging, resolved, logging.INFO)
    handler = logging.StreamHandler()
    handler.setLevel(numeric)
    handler.setFormatter(logging.Formatter("%(message)s"))
    for name in ("workspace_bridge", "workspace_bridge.ops", "workspace_bridge.boundary"):
        logger = logging.getLogger(name)
        logger.setLevel(numeric)
        if not any(isinstance(h, logging.StreamHandler) for h in logger.handlers):
            logger.addHandler(handler)
        logger.propagate = False
    return resolved


def _clean_value(value: Any) -> Any:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return max(-10 ** 12, min(10 ** 12, value))
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return None
        return round(value, 3)
    if isinstance(value, str):
        return value[:_MAX_STR]
    return None


def build_record(component: str, event: str, level: str = "INFO",
                 **fields: Any) -> dict | None:
    """Build a bounded structured record, or None for unknown events.

    Only allowlisted field names with scalar values are kept. Unknown fields
    and non-scalar values are dropped entirely (never serialized).
    """
    if event not in EVENTS:
        return None
    record: dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "level": level if level in ALLOWED_LEVELS else "INFO",
        "component": str(component)[:64] or COMPONENT,
        "event": event,
    }
    for key, value in fields.items():
        if key not in ALLOWED_FIELDS:
            continue
        if value is None:
            record[key] = None
            continue
        cleaned = _clean_value(value)
        if cleaned is None:
            # Non-scalar (dict/list), NaN/inf: drop rather than serialize input.
            continue
        record[key] = cleaned
    return record


def format_record(record: dict) -> str:
    return json.dumps(record, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def emit(logger: logging.Logger, level: str, component: str, event: str,
         **fields: Any) -> None:
    """Emit one structured record. Logging failures never raise."""
    try:
        record = build_record(component, event, level, **fields)
        if record is None:
            return
        line = format_record(record)
        numeric = getattr(logging, record["level"], logging.INFO)
        logger.log(numeric, "%s", line)
    except Exception:  # noqa: BLE001 - logging must never change run state
        pass


def error_code(exc: BaseException) -> str:
    """Sanitized error code/category only; never the raw message."""
    if isinstance(exc, BridgeError):
        return str(exc.code or "bridge_error")[:80]
    name = type(exc).__name__ or "error"
    # Keep only alphanumerics/underscore so class names cannot smuggle text.
    clean = "".join(c if (c.isalnum() or c == "_") else "_" for c in name)[:80]
    return clean or "error"
