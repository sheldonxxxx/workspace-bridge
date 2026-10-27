"""Operational logging contract (Python side).

One-line JSON records on stderr in the foreground; a managed service
(``WB_SERVICE=1``) writes them to its own size-capped file under
``<state>/logs`` instead. No logging dependency beyond the standard library.

Shared level semantics (Python Bridge and native adapters):
- DEBUG: high-frequency troubleshooting details.
- INFO: service readiness and expected lifecycle transitions.
- WARNING: recoverable degradation or input rejection needing attention.
- ERROR: unexpected exceptions or unsafe startup conditions.
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
import logging.handlers
import os
from pathlib import Path
from datetime import datetime, timezone
from typing import Any

from .security import BridgeError

LOG_LEVEL_ENV = "WB_LOG_LEVEL"
DEFAULT_LOG_LEVEL = "INFO"
ALLOWED_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")

COMPONENT = "bridge"

# Stable event names emitted by the bridge process.
EVENTS = frozenset({
    "bridge_ready", "boundary_reject", "request_error", "process_error",
    "node_link_connected", "node_link_disconnected", "node_link_retry", "mcp_client",
})

# Bounded scalar fields only. IDs/state names/counts/durations/sanitized
# codes and boolean health flags are acceptable; everything else is dropped.
ALLOWED_FIELDS = frozenset({
    "workspace_id", "reason", "code", "action", "source", "version",
    "runtime_configured", "workspace_count", "enabled_count", "client", "protocol",
})

_MAX_STR = 200
LOG_FILE_BYTES = 10 * 1024 * 1024
LOG_FILE_BACKUPS = 2
CRASH_LOG_BYTES = 1024 * 1024


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


def _service_handler(log_file: Path) -> logging.Handler:
    """Size-capped private log file for a managed service.

    The service manager's own stdout/stderr file only receives what escapes
    logging (an interpreter crash), so it is trimmed here at each start.
    """
    log_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    crash = log_file.with_name(log_file.stem + ".stderr.log")
    try:
        if crash.stat().st_size > CRASH_LOG_BYTES:
            os.truncate(crash, 0)
    except OSError:
        pass
    return logging.handlers.RotatingFileHandler(
        log_file, maxBytes=LOG_FILE_BYTES, backupCount=LOG_FILE_BACKUPS, encoding="utf-8")


def configure_operational_logging(level: str | None = None, environ: dict | None = None,
                                  *, log_file: Path | None = None) -> str:
    """Configure application logging at the resolved level, one record per line.

    In the foreground records go to stderr. A managed service (``WB_SERVICE=1``)
    with a ``log_file`` writes to that size-capped file instead. Uvicorn itself
    stays warning-level (callers keep access_log=False and log_level="warning").
    """
    env = environ if environ is not None else os.environ
    resolved = parse_log_level(level) if level is not None else log_level_from_env(env)
    numeric = getattr(logging, resolved, logging.INFO)
    service = log_file is not None and env.get("WB_SERVICE") == "1"
    handler = _service_handler(log_file) if service else logging.StreamHandler()
    handler.setLevel(numeric)
    handler.setFormatter(logging.Formatter("%(message)s"))
    for name in ("workspace_bridge", "workspace_bridge.ops", "workspace_bridge.boundary"):
        logger = logging.getLogger(name)
        logger.setLevel(numeric)
        if service:
            for old in [h for h in logger.handlers if isinstance(h, logging.StreamHandler)]:
                logger.removeHandler(old)
                if old is not handler and isinstance(old, logging.FileHandler):
                    old.close()
            logger.addHandler(handler)
        elif not any(isinstance(h, logging.StreamHandler) for h in logger.handlers):
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
