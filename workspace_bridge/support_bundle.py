"""Deterministic, strictly sanitized support bundle (M4.3B).

``workspace-bridge support bundle`` is a local-admin read/export operation
only: no service/package mutation, no upload, no network beyond the same
bounded diagnostics probes used by ``doctor`` unless ``--offline`` is set.

Output is a new private (0600) ZIP with fixed generic entry names, never
absolute paths or display names::

    manifest.json
    diagnostics.json
    services.json
    logs/node.jsonl
    logs/adapter-01-pi.jsonl
    ...

Every log line goes through :func:`sanitize_support_line` before inclusion.
The bundle specifically excludes tokens/hashes, credentials,
prompts/responses/tool args/results, provider payloads, absolute paths,
SSH agent paths, environment dumps, webhook URLs, raw HTTP bodies/headers,
private keys, and SQLite/config/token files. It never reads arbitrary
project files.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import platform
import re
import stat
import subprocess
import tempfile
import zipfile
from datetime import datetime, timezone
from typing import Any

from .security import BridgeError, redact


SCHEMA_VERSION = 1
BUNDLE_FORMAT = "workspace-bridge-support-bundle"
BUNDLE_VERSION = 1
MAX_TOTAL_BYTES = 5 * 1024 * 1024
MAX_LINES_PER_SERVICE = 200
MAX_BYTES_PER_SERVICE = 256 * 1024
MAX_LINE_OUT = 500
MAX_ADAPTER_STATES = 16
MAX_JSON_INPUT = 8192
JOURNAL_LINES = 200
JOURNAL_TIMEOUT = 8
SUPPORT_STATUSES = frozenset({"pass", "warning", "unknown", "action_required", "failed"})

_JOURNALCTL_CANDIDATES = ("/bin/journalctl", "/usr/bin/journalctl")
_URL_RE = re.compile(r"https?://\S+")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_WS_RE = re.compile(r"\s+")

# Explicit union of safe operational fields already used by Bridge (oplog)
# and Pi (logging.mjs) plus timestamp/level/component/event. Everything
# else -- prompts, messages, results, tool args, roots, model/user text,
# HTTP/body/header/env fields -- is dropped. Each allowlisted string field
# is additionally validated by semantics (timestamp ISO-8601, level enum,
# conservative token/version patterns, strict opaque IDs). Values failing
# their semantic validator are dropped; free-form strings never survive.
SAFE_JSON_FIELDS = frozenset({
    "timestamp", "level", "component", "event",
    "workspace_id", "reason", "code", "action", "source", "version",
    "runtime_configured", "workspace_count", "enabled_count",
    "adapter_version", "agent_dir_allowed", "agent_dir_explicit",
    "status", "session_id", "instance", "locked", "pi_usable",
    "pi_version", "projects_configured", "login_path_resolved",
    "login_shell", "login_path_entries", "login_path_code",
    "stage", "event_type", "tool_call_id", "pending_tool_count",
    "journal_state", "journal_update_seq", "duration_ms",
    "attempt", "max_attempts", "delay_ms", "success", "state",
})
SAFE_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR"})
_MAX_SAFE_STR = 200

_TIMESTAMP_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?(Z|[+-]\d{2}:?\d{2})$")
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")
_OPAQUE_ID_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[^\s,;]+")
_BOOL_FIELDS = frozenset({
    "runtime_configured", "agent_dir_allowed", "agent_dir_explicit",
    "locked", "pi_usable", "projects_configured", "login_path_resolved",
    "success",
})
_INT_FIELDS = frozenset({
    "workspace_count", "enabled_count", "login_path_entries",
    "pending_tool_count", "journal_update_seq", "attempt",
    "max_attempts", "delay_ms",
})
_FLOAT_OR_INT_FIELDS = frozenset({"duration_ms"})
_TOKEN64_FIELDS = frozenset({
    "component", "event", "reason", "code", "action", "source",
    "status", "stage", "event_type", "journal_state",
    "login_path_code", "login_shell", "state",
})
_VERSION_FIELDS = frozenset({"version", "adapter_version", "pi_version"})
_OPAQUE_ID_FIELDS = frozenset({
    "workspace_id", "session_id", "instance", "tool_call_id",
})

def _validate_structured_string(key: str, item: Any) -> str | None:
    """Validate one allowlisted JSON string field by semantics.

    Timestamp must be bounded ISO-8601-like UTC/offset; level is handled
    by the caller; token/version fields must match conservative bounded
    token patterns; opaque IDs must use strict opaque-ID characters only.
    Any value failing its validator -- including URLs, absolute paths,
    emails, bearer/secret material, private-key fragments, control or
    non-ASCII content, bare high-entropy secrets, or overlong values --
    is dropped (None). No free-form strings survive.
    """
    if not isinstance(item, str):
        return None
    if not item or len(item) > (64 if key == "timestamp" else 32 if key in _VERSION_FIELDS else 128 if key in _OPAQUE_ID_FIELDS else 64):
        return None
    # Bare high-entropy alphanumeric secrets without affixes (e.g. a raw
    # bearer token body) are indistinguishable from IDs by pattern alone;
    # reject long pure-alphanumeric strings to keep them out of the bundle.
    # Legitimate operational IDs/tokens use hyphens/underscores/dots.
    if len(item) >= 20 and re.fullmatch(r"[A-Za-z0-9]+", item):
        return None
    # Control characters and DEL are never valid in operational values.
    if any(ord(c) < 32 or ord(c) == 127 for c in item):
        return None
    # Non-ASCII (adversarial Unicode) is never valid in these fields.
    try:
        item.encode("ascii")
    except UnicodeEncodeError:
        return None
    if key == "timestamp":
        if not _TIMESTAMP_RE.fullmatch(item):
            return None
        return item
    if key in _VERSION_FIELDS:
        if not _TOKEN_RE.fullmatch(item):
            return None
        if _URL_RE.search(item) or _EMAIL_RE.search(item):
            return None
        if "://" in item or "file://" in item.lower():
            return None
        if "/" in item or "\\" in item:
            return None
        if "PRIVATE KEY" in item.upper():
            return None
        if _BEARER_RE.search(item):
            return None
        try:
            _, changed = redact(item)
            if changed:
                return None
        except Exception:
            pass
        return item
    if key in _TOKEN64_FIELDS:
        if not _TOKEN_RE.fullmatch(item):
            return None
        if _URL_RE.search(item) or _EMAIL_RE.search(item):
            return None
        if "://" in item or "file://" in item.lower():
            return None
        if "PRIVATE KEY" in item.upper():
            return None
        if _BEARER_RE.search(item):
            return None
        try:
            _, changed = redact(item)
            if changed:
                return None
        except Exception:
            pass
        return item
    if key in _OPAQUE_ID_FIELDS:
        if not _OPAQUE_ID_RE.fullmatch(item):
            return None
        if _URL_RE.search(item) or _EMAIL_RE.search(item):
            return None
        if "://" in item or "file://" in item.lower():
            return None
        if "/" in item or "\\" in item:
            return None
        if "PRIVATE KEY" in item.upper():
            return None
        if _BEARER_RE.search(item):
            return None
        try:
            _, changed = redact(item)
            if changed:
                return None
        except Exception:
            pass
        # Bare bearer-like long hex without affixes cannot be distinguished
        # from legitimate IDs by pattern alone, but values containing the
        # literal "Bearer" prefix (with required space) already fail the
        # opaque pattern (spaces forbidden) and bearer check above. Keep
        # the strict pattern gate as the authority.
        return item
    return None


# Plain-text lines containing these substrings are treated as unsafe and
# dropped rather than shared. Operational JSON logs are unaffected (they go
# through the allowlisted JSON projection above). This guarantees prompts,
# tool args/results, messages, responses, payloads and model text never
# leave the host via a plain-text excerpt.
_PLAIN_DROP_SUBSTRINGS = (
    "prompt", "tool_arg", "tool result", "tool_result", "tool_call",
    "final_response", "final response", "message", "result", "response",
    "payload", "webhook", "private key", "authorization",
)


def _stamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _platform_category(platform_name: str | None = None) -> str:
    name = platform_name if platform_name is not None else platform.system()
    if name == "Darwin":
        return "macos"
    if name == "Linux":
        return "linux"
    return "unknown"


def sanitize_support_line(line: Any) -> str | None:
    """Sanitize one native log line for safe sharing.

    Structured JSON retains only :data:`SAFE_JSON_FIELDS` with strict scalar
    bounds; unknown keys are dropped. Non-JSON lines use
    ``security.redact`` plus path/URL/secret/private-key removal at least as
    strong as ``codex_rpc.sanitize_diagnostic``, are bounded to
    ``MAX_LINE_OUT`` chars, and empty/unsafe lines are dropped. Never
    returns a raw line merely because parsing failed.
    """
    if not isinstance(line, str):
        return None
    text = line.strip()
    if not text:
        return None
    if len(text) > MAX_JSON_INPUT:
        text = text[:MAX_JSON_INPUT]
    # Structured path first.
    try:
        value = json.loads(text)
    except (ValueError, UnicodeError):
        value = None
    if isinstance(value, dict):
        projected: dict[str, Any] = {}
        for key in SAFE_JSON_FIELDS:
            if key not in value:
                continue
            item = value[key]
            # Strict per-field types: booleans/numerics never accept strings.
            if key in _BOOL_FIELDS:
                if item is None or isinstance(item, bool):
                    projected[key] = item
                continue
            if key in _INT_FIELDS:
                if isinstance(item, bool):
                    continue
                if isinstance(item, int):
                    projected[key] = max(-10 ** 12, min(10 ** 12, item))
                continue
            if key in _FLOAT_OR_INT_FIELDS:
                if isinstance(item, bool):
                    continue
                if isinstance(item, int):
                    projected[key] = max(-10 ** 12, min(10 ** 12, item))
                elif isinstance(item, float):
                    if item != item or item in (float("inf"), float("-inf")):
                        continue
                    projected[key] = round(item, 3)
                continue
            if key == "level":
                if isinstance(item, str) and item in SAFE_LEVELS:
                    projected[key] = item
                continue
            if key == "timestamp":
                validated = _validate_structured_string(key, item)
                if validated is not None:
                    projected[key] = validated
                continue
            if key in _TOKEN64_FIELDS or key in _VERSION_FIELDS or key in _OPAQUE_ID_FIELDS:
                validated = _validate_structured_string(key, item)
                if validated is not None:
                    projected[key] = validated
                continue
            # Any other allowlisted key with unexpected type is dropped.
            continue
        if not projected:
            return None
        try:
            out = json.dumps(projected, sort_keys=True, ensure_ascii=False,
                             separators=(",", ":"))
        except (TypeError, ValueError, UnicodeError):
            return None
        if len(out) > MAX_LINE_OUT:
            out = out[:MAX_LINE_OUT]
        out = out.strip()
        if not out:
            return None
        if "BEGIN PRIVATE KEY" in out or "END PRIVATE KEY" in out:
            return None
        return out
    # Plain-text path: never share raw lines with hostile content.
    upper = text.upper()
    if "PRIVATE KEY" in upper:
        return None
    lowered = text.lower()
    for marker in _PLAIN_DROP_SUBSTRINGS:
        if marker in lowered:
            return None
    # Redact URLs/emails first so absolute-path removal does not mangle
    # them into leaky fragments like "https:/[PATH]".
    pre = _URL_RE.sub("[URL]", text)
    pre = _EMAIL_RE.sub("[EMAIL]", pre)
    try:
        from .codex_rpc import sanitize_diagnostic as _san
        cleaned = _san(pre, limit=MAX_LINE_OUT)
    except Exception:
        cleaned, _ = redact(pre)
        cleaned = " ".join(cleaned.split())[:MAX_LINE_OUT]
    if not cleaned or not cleaned.strip():
        return None
    cleaned = _URL_RE.sub("[URL]", cleaned)
    cleaned = _EMAIL_RE.sub("[EMAIL]", cleaned)
    if "BEGIN PRIVATE KEY" in cleaned or "END PRIVATE KEY" in cleaned:
        return None
    # Re-check drop markers after redaction (e.g. "[URL]" lines are safe).
    lowered_clean = cleaned.lower()
    # Allow redacted placeholders through even if they contain "response"?
    # Placeholders are "[URL]"/"[EMAIL]"/"[PATH]"/"[REDACTED_SECRET]" only.
    # If the cleaned line still carries hostile keywords, drop it.
    for marker in _PLAIN_DROP_SUBSTRINGS:
        if marker in lowered_clean:
            # "[REDACTED_SECRET]" contains "secret" but not in the drop
            # list; only drop genuine hostile keywords.
            return None
    cleaned = cleaned[:MAX_LINE_OUT].strip()
    if not cleaned:
        return None
    return cleaned


def _safe_id(value: Any, *, limit: int = 128) -> str | None:
    if not isinstance(value, str) or not 1 <= len(value) <= limit:
        return None
    if any(ord(c) < 33 or ord(c) == 127 or c.isspace() for c in value):
        return None
    if "/" in value or "\\" in value or "\x00" in value:
        return None
    # IDs are opaque hex/uuid-like; allow bounded alphanumerics plus -_.
    if not re.fullmatch(r"[A-Za-z0-9_.\-]+", value):
        return None
    return value


def _safe_code(value: Any, *, limit: int = 128) -> str | None:
    if not isinstance(value, str) or not 1 <= len(value) <= limit:
        return None
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        return None
    if not re.fullmatch(r"[A-Za-z0-9_.\-]+", value):
        return None
    return value


def _safe_summary(value: Any, *, limit: int = 300) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        from .codex_rpc import sanitize_diagnostic as _san
        cleaned = _san(value, limit=limit)
    except Exception:
        cleaned, _ = redact(value[:limit * 2])
        cleaned = " ".join(cleaned.split())[:limit]
    if not cleaned:
        return None
    # Summaries must never carry URLs, paths, or secrets after sanitizing.
    if "http://" in cleaned.lower() or "https://" in cleaned.lower():
        # sanitize should have removed absolute URLs, but be explicit.
        cleaned = _URL_RE.sub("[URL]", cleaned)
    return cleaned[:limit] if cleaned else None


def project_diagnostics(report: Any) -> dict:
    """Project a canonical DiagnosticReport through a strict allowlist.

    Keeps overall status/counts, check code/section/status plus sanitized
    summaries/remediation, stable IDs/runtime types, and validated release
    identities. Drops absolute paths, raw endpoints, display names,
    default-model labels, arbitrary details, and any secrets.
    """
    if not isinstance(report, dict):
        return {"overall": {"status": "unknown", "counts": {}},
                "checks": [], "runnable_routes": [], "release": {}}
    overall = report.get("overall") if isinstance(report.get("overall"), dict) else {}
    status = overall.get("status") if overall.get("status") in SUPPORT_STATUSES else "unknown"
    counts: dict[str, int] = {}
    raw_counts = overall.get("counts") if isinstance(overall.get("counts"), dict) else {}
    for key in ("pass", "warning", "unknown", "action_required", "failed"):
        try:
            counts[key] = max(0, min(100000, int(raw_counts.get(key, 0))))
        except (TypeError, ValueError):
            counts[key] = 0
    overall_out: dict[str, Any] = {"status": status, "counts": counts}
    summary = _safe_summary(overall.get("summary"), limit=300)
    if summary:
        overall_out["summary"] = summary
    checks_out: list[dict] = []
    raw_checks = report.get("checks") if isinstance(report.get("checks"), list) else []
    for row in raw_checks[:5000]:
        if not isinstance(row, dict):
            continue
        code = _safe_code(row.get("code"))
        section = _safe_code(row.get("section"))
        check_status = row.get("status")
        if code is None or section is None or check_status not in SUPPORT_STATUSES:
            continue
        entry: dict[str, Any] = {"code": code, "section": section, "status": check_status}
        summary_val = _safe_summary(row.get("summary"), limit=160)
        if summary_val:
            entry["summary"] = summary_val
        remediation = _safe_summary(row.get("remediation"), limit=240)
        if remediation:
            entry["remediation"] = remediation
        wid = _safe_id(row.get("workspace_id")) if row.get("workspace_id") is not None else None
        if wid:
            entry["workspace_id"] = wid
        aid = _safe_id(row.get("adapter_id")) if row.get("adapter_id") is not None else None
        if aid:
            entry["adapter_id"] = aid
        rt = row.get("runtime_type")
        if isinstance(rt, str) and rt in {"pi", "codex", "claude"}:
            entry["runtime_type"] = rt
        # Never copy id/detail/names/endpoints/secrets.
        checks_out.append(entry)
        if len(checks_out) >= 5000:
            break
    routes_out: list[dict] = []
    raw_routes = report.get("runnable_routes") if isinstance(report.get("runnable_routes"), list) else []
    for row in raw_routes[:400]:
        if not isinstance(row, dict):
            continue
        wid = _safe_id(row.get("workspace_id"))
        aid = _safe_id(row.get("adapter_id"))
        nid = _safe_id(row.get("node_id"))
        rt = row.get("runtime_type")
        if wid is None or aid is None or nid is None:
            continue
        if rt not in {"pi", "codex", "claude"}:
            continue
        ready = row.get("ready")
        route_status = row.get("status")
        if not isinstance(ready, bool) or route_status not in {"ready", "blocked"}:
            continue
        blockers: list[str] = []
        raw_blockers = row.get("blockers")
        if isinstance(raw_blockers, list):
            for item in raw_blockers[:20]:
                code_item = _safe_code(item, limit=80)
                if code_item:
                    blockers.append(code_item)
        routes_out.append({"workspace_id": wid, "adapter_id": aid,
                           "node_id": nid, "runtime_type": rt,
                           "ready": ready, "status": route_status,
                           "blockers": blockers})
    release_out: dict[str, Any] = {}
    raw_release = report.get("release") if isinstance(report.get("release"), dict) else {}
    try:
        from .release import validate_release as _validate
        for key in ("bridge", "manager"):
            candidate = raw_release.get(key)
            if candidate is None:
                continue
            try:
                release_out[key] = _validate(candidate)
            except Exception:
                continue
        for key in ("nodes", "adapters"):
            bucket = raw_release.get(key)
            if not isinstance(bucket, dict):
                continue
            projected_bucket: dict[str, Any] = {}
            for ident, candidate in list(bucket.items())[:100]:
                safe_ident = _safe_id(ident)
                if safe_ident is None or candidate is None:
                    continue
                try:
                    projected_bucket[safe_ident] = _validate(candidate)
                except Exception:
                    continue
            if projected_bucket:
                release_out[key] = projected_bucket
    except Exception:
        pass
    return {"overall": overall_out, "checks": checks_out,
            "runnable_routes": routes_out, "release": release_out}


def _project_health(raw: Any) -> dict:
    if not isinstance(raw, dict):
        return {"status": "unknown", "code": "unknown"}
    status = raw.get("status")
    if status not in {"healthy", "degraded", "unavailable", "not_probed",
                      "auth_failed", "invalid_response", "unknown"}:
        # Adapter health uses healthy/degraded/unavailable; Node uses
        # healthy/degraded/unavailable/not_probed etc. Normalize unknown.
        if not isinstance(status, str):
            status = "unknown"
        else:
            status = _safe_code(status, limit=32) or "unknown"
    code = _safe_code(raw.get("code"), limit=64) or "unknown"
    out: dict[str, Any] = {"status": status, "code": code}
    protocol = raw.get("protocol")
    if isinstance(protocol, int) and not isinstance(protocol, bool) and 0 <= protocol <= 99:
        out["protocol"] = protocol
    for key in ("adapter_version", "native_version", "node_version"):
        candidate = raw.get(key)
        if isinstance(candidate, str) and 1 <= len(candidate) <= 80:
            cleaned = re.sub(r"[^A-Za-z0-9_.\-+]", "_", candidate)[:80]
            if cleaned and "/" not in cleaned and " " not in cleaned:
                out[key] = cleaned
    # Node root_status: keep counts only, drop labels/paths.
    root_status = raw.get("root_status")
    if isinstance(root_status, dict):
        rs_status = root_status.get("status")
        if rs_status in {"ready", "degraded", "unknown"}:
            try:
                total = max(0, min(10000, int(root_status.get("total", 0))))
                available = max(0, min(10000, int(root_status.get("available", 0))))
                unavailable = max(0, min(10000, int(root_status.get("unavailable", 0))))
            except (TypeError, ValueError):
                total = available = unavailable = 0
            out["root_status"] = {"status": rs_status, "total": total,
                                  "available": available, "unavailable": unavailable}
    return out


def _project_listen(raw: Any) -> dict | None:
    if not isinstance(raw, dict):
        return None
    port = raw.get("port")
    if not isinstance(port, int) or isinstance(port, bool) or not 1024 <= port <= 65535:
        return None
    # Only the loopback port is shareable; hosts/endpoints/paths are dropped.
    return {"port": int(port)}


def project_node_status(raw: Any) -> dict:
    """Project Node launchd/systemd status through the shared allowlist."""
    if not isinstance(raw, dict):
        return {"service_manager": "unknown", "installed": False,
                "state": "unknown", "health": {"status": "unknown", "code": "unknown"}}
    manager = raw.get("service_manager")
    if manager not in {"launchd", "systemd"}:
        # Node launchd status omits service_manager; infer launchd when a
        # fixed label is present, else unknown.
        if raw.get("label") == "com.workspace-bridge.node":
            manager = "launchd"
        else:
            manager = "unknown"
    out: dict[str, Any] = {"service_manager": manager}
    # Fixed/generated unit/label only.
    if manager == "launchd":
        if raw.get("label") == "com.workspace-bridge.node":
            out["label"] = "com.workspace-bridge.node"
    else:
        unit = raw.get("unit")
        if unit == "workspace-bridge-node.service":
            out["unit"] = "workspace-bridge-node.service"
    out["installed"] = bool(raw.get("installed"))
    # Managed/high-level states only.
    for key in ("plist", "unit_state"):
        candidate = raw.get(key)
        if isinstance(candidate, str) and _safe_code(candidate, limit=32):
            out["managed"] = _safe_code(candidate, limit=32)
            break
    state = _safe_code(raw.get("state"), limit=32) or "unknown"
    out["state"] = state
    running = raw.get("running")
    if isinstance(running, bool):
        out["running"] = running
    else:
        # Launchd shape derives running from launchd.running.
        launchd = raw.get("launchd")
        if isinstance(launchd, dict) and isinstance(launchd.get("running"), bool):
            out["running"] = bool(launchd["running"])
    enabled = raw.get("enabled")
    if isinstance(enabled, str) and _safe_code(enabled, limit=32):
        out["enabled"] = _safe_code(enabled, limit=32)
    out["health"] = _project_health(raw.get("health"))
    listen = _project_listen(raw.get("listen"))
    if listen is not None:
        out["listen"] = listen
    return out


def project_adapter_status(raw: Any) -> dict:
    """Project adapter launchd/systemd status through the shared allowlist."""
    if not isinstance(raw, dict):
        return {"service_manager": "unknown", "installed": False,
                "state": "unknown", "health": {"status": "unknown", "code": "unknown"}}
    manager = raw.get("service_manager")
    if manager not in {"launchd", "systemd"}:
        manager = "unknown"
    out: dict[str, Any] = {"service_manager": manager}
    label = raw.get("label")
    unit = raw.get("unit")
    try:
        from .adapter_service import validate_label as _vlabel, validate_unit as _vunit
        if isinstance(label, str):
            try:
                out["label"] = _vlabel(label)
            except BridgeError:
                pass
        if isinstance(unit, str):
            try:
                out["unit"] = _vunit(unit)
            except BridgeError:
                pass
    except Exception:
        pass
    # Fall back to deriving from runtime_type/service_id when the raw label
    # is absent but identity fields validate.
    if "label" not in out and "unit" not in out:
        try:
            from .adapter_service import adapter_label as _alabel, adapter_unit as _aunit
            rt = raw.get("runtime_type")
            sid = raw.get("service_id")
            if isinstance(rt, str) and isinstance(sid, str):
                try:
                    if manager == "launchd":
                        out["label"] = _alabel(rt, sid)
                    elif manager == "systemd":
                        out["unit"] = _aunit(rt, sid)
                except BridgeError:
                    pass
        except Exception:
            pass
    rt = raw.get("runtime_type")
    if isinstance(rt, str) and rt in {"pi", "codex", "claude"}:
        out["runtime_type"] = rt
    sid = raw.get("service_id")
    if _safe_id(sid, limit=12) and isinstance(sid, str) and re.fullmatch(r"[0-9a-f]{12}", sid):
        out["service_id"] = sid
    out["installed"] = bool(raw.get("installed"))
    for key in ("plist", "unit_state"):
        candidate = raw.get(key)
        if isinstance(candidate, str) and _safe_code(candidate, limit=32):
            out["managed"] = _safe_code(candidate, limit=32)
            break
    state = _safe_code(raw.get("state"), limit=32) or "unknown"
    out["state"] = state
    running = raw.get("running")
    if isinstance(running, bool):
        out["running"] = running
    else:
        launchd = raw.get("launchd")
        if isinstance(launchd, dict) and isinstance(launchd.get("running"), bool):
            out["running"] = bool(launchd["running"])
    enabled = raw.get("enabled")
    if isinstance(enabled, str) and _safe_code(enabled, limit=32):
        out["enabled"] = _safe_code(enabled, limit=32)
    out["health"] = _project_health(raw.get("health"))
    # Promote adapter/native versions from the top-level safe status.
    for key in ("adapter_version", "native_version"):
        candidate = raw.get(key)
        if isinstance(candidate, str) and 1 <= len(candidate) <= 80:
            cleaned = re.sub(r"[^A-Za-z0-9_.\-+]", "_", candidate)[:80]
            if cleaned and "/" not in cleaned and " " not in cleaned:
                out[key] = cleaned
    listen = _project_listen(raw.get("listen"))
    if listen is not None:
        out["listen"] = listen
    return out


def _absolute_path(value: str | os.PathLike[str]) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return Path(os.path.normpath(str(path)))


def _read_private_log_lines(path: Path, *, limit_lines: int = MAX_LINES_PER_SERVICE,
                            limit_bytes: int = MAX_BYTES_PER_SERVICE) -> tuple[list[str], str | None]:
    """Read the newest bounded lines from one private log file.

    Returns (raw_lines, omission_code). Never follows symlinks and never
    reads arbitrary paths; the caller guarantees ``path`` is an owned
    ``stdout.log``/``stderr.log`` or ``.1``/``.2`` file.
    """
    try:
        if path.is_symlink() or not path.is_file():
            return [], "omitted-unsafe-log"
        try:
            st = path.stat()
        except OSError:
            return [], "omitted-unreadable-log"
        if not stat.S_ISREG(st.st_mode):
            return [], "omitted-unsafe-log"
        uid = getattr(os, "getuid", None)
        if callable(uid) and int(st.st_uid) != int(uid()):
            return [], "omitted-unsafe-log"
        if stat.S_IMODE(st.st_mode) != 0o600:
            return [], "omitted-unsafe-log"
        size = int(st.st_size)
        if size <= 0:
            return [], None
        # Read at most the last limit_bytes to bound memory.
        start = max(0, size - limit_bytes)
        try:
            with open(path, "rb") as stream:
                stream.seek(start)
                data = stream.read(limit_bytes + 1)
        except OSError:
            return [], "omitted-unreadable-log"
        text = data.decode("utf-8", "replace")
        # If we started mid-line, drop the first partial line.
        if start > 0:
            newline = text.find("\n")
            if newline != -1:
                text = text[newline + 1:]
        lines = text.splitlines()
        # Newest lines only.
        if len(lines) > limit_lines:
            lines = lines[-limit_lines:]
        return lines, None
    except Exception:
        return [], "omitted-unreadable-log"


def collect_file_excerpts(state: str | os.PathLike[str]) -> tuple[list[str], list[str]]:
    """Collect sanitized excerpts across active + retained macOS files.

    Returns (sanitized_lines, omission_codes). At most newest ~200 lines /
    256 KiB per service. Every line goes through
    :func:`sanitize_support_line`.
    """
    state_path = _absolute_path(state)
    log_dir = state_path / "logs"
    # Reuse private-file checks; never traverse arbitrary paths.
    try:
        if log_dir.is_symlink() or not log_dir.is_dir():
            return [], ["omitted-unavailable-logs"]
        st = log_dir.stat()
        if not stat.S_ISDIR(st.st_mode) or stat.S_IMODE(st.st_mode) != 0o700:
            return [], ["omitted-unsafe-logs"]
        uid = getattr(os, "getuid", None)
        if callable(uid) and int(st.st_uid) != int(uid()):
            return [], ["omitted-unsafe-logs"]
    except OSError:
        return [], ["omitted-unavailable-logs"]
    candidates: list[Path] = []
    for name in ("stdout.log.2", "stderr.log.2", "stdout.log.1", "stderr.log.1",
                 "stdout.log", "stderr.log"):
        candidate = log_dir / name
        # Lexical allowlist only; never follow caller-supplied filenames.
        if candidate.parent != log_dir or candidate.name != name:
            continue
        candidates.append(candidate)
    raw_all: list[str] = []
    omissions: list[str] = []
    # Older archives first so newest active lines dominate the tail cap.
    for candidate in candidates:
        if not candidate.exists():
            continue
        lines, omission = _read_private_log_lines(candidate, limit_lines=MAX_LINES_PER_SERVICE,
                                                  limit_bytes=MAX_BYTES_PER_SERVICE)
        if omission and omission not in omissions:
            # Unsafe logs are reported once; missing files are not omissions.
            if "unsafe" in omission or "unreadable" in omission:
                omissions.append(omission)
        raw_all.extend(lines)
    # Newest ~200 lines across all streams.
    if len(raw_all) > MAX_LINES_PER_SERVICE:
        raw_all = raw_all[-MAX_LINES_PER_SERVICE:]
    # Bound total bytes.
    total = sum(len(line.encode("utf-8")) + 1 for line in raw_all)
    while raw_all and total > MAX_BYTES_PER_SERVICE:
        removed = raw_all.pop(0)
        total -= len(removed.encode("utf-8")) + 1
    sanitized: list[str] = []
    for line in raw_all:
        cleaned = sanitize_support_line(line)
        if cleaned:
            sanitized.append(cleaned)
            if sum(len(item.encode("utf-8")) + 1 for item in sanitized) > MAX_BYTES_PER_SERVICE:
                sanitized.pop(0)
        if len(sanitized) >= MAX_LINES_PER_SERVICE:
            # Keep newest; drop oldest sanitized.
            sanitized = sanitized[-MAX_LINES_PER_SERVICE:]
    return sanitized, omissions


def _resolve_journalctl() -> Path | None:
    for candidate in _JOURNALCTL_CANDIDATES:
        path = Path(candidate)
        try:
            if path.is_absolute() and path.is_file() and os.access(path, os.X_OK):
                return path
        except OSError:
            continue
    return None


def collect_journal_excerpts(unit: str) -> tuple[list[str], list[str]]:
    """Collect sanitized excerpts via fixed journalctl (Linux only).

    Uses ``journalctl -u <validated exact unit> --no-pager --output=cat
    -n 200`` with a fixed absolute binary, short timeout, no sudo/shell.
    Journal access failure is an omission code, not fatal.
    """
    try:
        from .adapter_service import validate_unit as _vunit
        node_unit = "workspace-bridge-node.service"
        if unit != node_unit:
            try:
                _vunit(unit)
            except BridgeError:
                return [], ["omitted-invalid-unit"]
    except Exception:
        return [], ["omitted-invalid-unit"]
    binary = _resolve_journalctl()
    if binary is None:
        return [], ["omitted-journal-unavailable"]
    argv = [str(binary), "-u", unit, "--no-pager", "--output=cat", "-n", str(JOURNAL_LINES)]
    try:
        completed = subprocess.run(argv, capture_output=True, text=True,
                                   check=False, timeout=JOURNAL_TIMEOUT, shell=False)
    except FileNotFoundError:
        return [], ["omitted-journal-unavailable"]
    except subprocess.TimeoutExpired:
        return [], ["omitted-journal-unavailable"]
    except OSError:
        return [], ["omitted-journal-unavailable"]
    if completed.returncode != 0:
        return [], ["omitted-journal-unavailable"]
    raw = completed.stdout or ""
    lines = raw.splitlines()[-MAX_LINES_PER_SERVICE:]
    total = sum(len(line.encode("utf-8")) + 1 for line in lines)
    while lines and total > MAX_BYTES_PER_SERVICE:
        removed = lines.pop(0)
        total -= len(removed.encode("utf-8")) + 1
    sanitized: list[str] = []
    for line in lines:
        cleaned = sanitize_support_line(line)
        if cleaned:
            sanitized.append(cleaned)
        if len(sanitized) >= MAX_LINES_PER_SERVICE:
            sanitized = sanitized[-MAX_LINES_PER_SERVICE:]
    if not sanitized and not lines:
        return [], ["omitted-journal-empty"]
    if not sanitized and lines:
        return [], ["omitted-journal-unsanitized"]
    return sanitized, []


def _default_node_state() -> Path:
    try:
        from .node_launchd import default_node_state as _default
        return _absolute_path(_default())
    except Exception:
        return _absolute_path(Path.home() / ".local/state/workspace-bridge-node")


def _collect_service_evidence(*, node_state: Path | None,
                              adapter_states: list[Path],
                              offline: bool,
                              platform_name: str | None = None) -> tuple[dict, dict[str, list[str]]]:
    """Call platform backend status functions read-only and project safely."""
    probe = not offline
    services: dict[str, Any] = {}
    log_map: dict[str, list[str]] = {}
    omissions: list[str] = []
    category = _platform_category(platform_name)
    # Node: default when omitted and valid, else explicit.
    node_entry: dict[str, Any] | None = None
    node_omission: str | None = None
    if node_state is not None:
        try:
            if category == "linux":
                from .node_systemd import SystemdManager, service_status as _nstatus
                manager = SystemdManager()
                raw = _nstatus(node_state, manager=manager, probe=probe)
            elif category == "macos":
                from .node_launchd import LaunchdManager, service_status as _nstatus
                manager = LaunchdManager()
                raw = _nstatus(node_state, manager=manager, probe=probe)
            else:
                raw = {"service_manager": "unknown", "installed": False,
                       "state": "unknown", "health": {"status": "unknown", "code": "unsupported-platform"}}
            node_entry = project_node_status(raw)
            node_entry["source"] = "node"
        except BridgeError as exc:
            node_entry = None
            node_omission = str(getattr(exc, "code", "node-unavailable") or "node-unavailable")[:64]
        except Exception:
            node_entry = None
            node_omission = "node-unavailable"
        if node_entry is not None:
            services["node"] = node_entry
            # Logs for the Node state.
            try:
                if category == "macos":
                    lines, log_omissions = collect_file_excerpts(node_state)
                    log_map["logs/node.jsonl"] = lines
                    if log_omissions:
                        node_entry.setdefault("log_omissions", log_omissions)
                elif category == "linux":
                    lines, log_omissions = collect_journal_excerpts("workspace-bridge-node.service")
                    log_map["logs/node.jsonl"] = lines
                    if log_omissions:
                        node_entry.setdefault("log_omissions", log_omissions)
                else:
                    node_entry.setdefault("log_omissions", ["omitted-unsupported-platform"])
                    log_map["logs/node.jsonl"] = []
            except Exception:
                log_map["logs/node.jsonl"] = []
        elif node_omission:
            omissions.append(node_omission)
    # Adapters: explicit only, deterministically ordered, capped.
    ordered = sorted({str(_absolute_path(p)) for p in adapter_states})[:MAX_ADAPTER_STATES]
    if len(adapter_states) > MAX_ADAPTER_STATES:
        omissions.append("omitted-adapter-limit")
    adapters_out: list[dict] = []
    for index, raw_path in enumerate(ordered, start=1):
        state_path = _absolute_path(raw_path)
        # Determine runtime for the entry name without leaking paths.
        runtime = "unknown"
        try:
            from .adapter_service import load_adapter_config as _load
            cfg = _load(state_path, require_roots=False)
            candidate = cfg.get("runtime_type")
            if candidate in {"pi", "codex", "claude"}:
                runtime = candidate
        except Exception:
            pass
        entry_name = f"logs/adapter-{index:02d}-{runtime}.jsonl"
        try:
            if category == "linux":
                from .adapter_systemd import SystemdManager as _AManager, service_status as _astatus
                manager = _AManager()
                raw = _astatus(state_path, manager=manager, probe=probe)
            elif category == "macos":
                from .adapter_launchd import LaunchdManager as _ALManager, service_status as _astatus
                manager = _ALManager()
                raw = _astatus(state_path, manager=manager, probe=probe)
            else:
                raw = {"service_manager": "unknown", "installed": False,
                       "state": "unknown", "health": {"status": "unknown", "code": "unsupported-platform"}}
            projected = project_adapter_status(raw)
            projected["source"] = f"adapter-{index:02d}"
            projected["runtime_type"] = runtime if runtime in {"pi", "codex", "claude"} else projected.get("runtime_type", runtime)
            adapters_out.append(projected)
            # Logs per adapter.
            try:
                if category == "macos":
                    lines, log_omissions = collect_file_excerpts(state_path)
                    log_map[entry_name] = lines
                    if log_omissions:
                        projected.setdefault("log_omissions", log_omissions)
                elif category == "linux":
                    # Derive the exact validated unit for journalctl.
                    unit: str | None = None
                    try:
                        from .adapter_service import adapter_unit as _aunit, validate_service_id as _vsid, validate_runtime_type as _vrt
                        # Prefer the projected identity when valid.
                        sid = projected.get("service_id")
                        rt = projected.get("runtime_type")
                        if isinstance(rt, str) and isinstance(sid, str):
                            try:
                                unit = _aunit(_vrt(rt), _vsid(sid))
                            except BridgeError:
                                unit = None
                    except Exception:
                        unit = None
                    if unit is None:
                        log_map[entry_name] = []
                        projected.setdefault("log_omissions", ["omitted-invalid-unit"])
                    else:
                        lines, log_omissions = collect_journal_excerpts(unit)
                        log_map[entry_name] = lines
                        if log_omissions:
                            projected.setdefault("log_omissions", log_omissions)
                else:
                    projected.setdefault("log_omissions", ["omitted-unsupported-platform"])
                    log_map[entry_name] = []
            except Exception:
                log_map[entry_name] = []
        except BridgeError as exc:
            code = str(getattr(exc, "code", "adapter-unavailable") or "adapter-unavailable")[:64]
            adapters_out.append({"source": f"adapter-{index:02d}", "runtime_type": runtime,
                                 "state": "unavailable", "omission": code})
            log_map[entry_name] = []
            omissions.append(code)
        except Exception:
            adapters_out.append({"source": f"adapter-{index:02d}", "runtime_type": runtime,
                                 "state": "unavailable", "omission": "adapter-unavailable"})
            log_map[entry_name] = []
            omissions.append("adapter-unavailable")
    if adapters_out:
        services["adapters"] = adapters_out
    if omissions:
        services["omissions"] = sorted(set(omissions))[:20]
    return services, log_map


def _build_manifest(*, mode: str, included: list[str], omitted: list[str],
                    release_bridge: dict | None,
                    platform_name: str | None = None) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _stamp(),
        "bundle_format": BUNDLE_FORMAT,
        "bundle_version": BUNDLE_VERSION,
        "platform": _platform_category(platform_name),
        "collection_mode": mode,
        "bridge_release": release_bridge,
        "included": sorted(set(included)),
        "omitted": sorted(set(omitted))[:20],
    }


def _validate_parent(parent: Path) -> Path:
    """Canonicalize/validate the existing output parent safely.

    Resolves the parent once (so /tmp on macOS becomes /private/tmp),
    requires an existing directory, and applies descriptor-relative
    directory checks where compatible. Never creates directories.
    Returns the resolved parent for same-filesystem temp files.
    """
    from .security import open_absolute_dir
    lexical = _absolute_path(parent)
    try:
        resolved = lexical.resolve()
    except OSError:
        raise SupportBundleError("Bundle parent directory is unavailable",
                                 code="bundle-unsafe-path") from None
    try:
        if not resolved.exists() or not resolved.is_dir():
            raise SupportBundleError("Bundle parent directory is unavailable",
                                     code="bundle-unsafe-path")
    except OSError:
        raise SupportBundleError("Bundle output is unavailable",
                                 code="bundle-unavailable") from None
    try:
        fd = open_absolute_dir(str(resolved))
        os.close(fd)
    except BridgeError:
        raise SupportBundleError("Bundle parent directory is unavailable",
                                 code="bundle-unsafe-path") from None
    return resolved


def _publish_no_clobber(temp_path: Path, out_path: Path) -> None:
    """Atomically publish temp ZIP to final path without clobbering.

    Uses same-directory hard-link (os.link then unlink temp) so a
    destination appearing after preflight causes FileExistsError instead
    of overwrite. Never replaces an existing destination.
    """
    try:
        os.link(str(temp_path), str(out_path))
    except FileExistsError:
        raise SupportBundleError("Refusing to overwrite existing bundle output",
                                 code="bundle-exists") from None
    except OSError as exc:
        import errno as _errno
        if getattr(exc, "errno", None) in (_errno.EEXIST, 17):
            raise SupportBundleError("Refusing to overwrite existing bundle output",
                                     code="bundle-exists") from None
        raise SupportBundleError("Bundle output is unavailable",
                                 code="bundle-unavailable") from None
    try:
        os.chmod(out_path, 0o600)
    except OSError:
        pass
    try:
        temp_path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


def build_support_bundle(*, output: str | os.PathLike[str],
                         offline: bool = False,
                         node_state: str | os.PathLike[str] | None = None,
                         adapter_states: list[str | os.PathLike[str]] | None = None,
                         platform_name: str | None = None,
                         bridge_state: str | os.PathLike[str] | None = None) -> dict:
    """Build a new private ZIP support bundle.

    Returns ``{"path": ..., "entries": [...], "omitted": [...]}``. Raises
    :class:`BundleError` (a :class:`ValueError` with ``code``) on
    fail-closed conditions. Never mutates services and never uploads.
    ``bridge_state`` selects the Bridge state for diagnostics (global
    ``--state``); when omitted it defaults to normal DEFAULT_STATE.
    """
    out_path = _absolute_path(output)
    # New-file-only preflight: refuse overwrite/symlink. Final publication
    # uses atomic no-clobber link so a race still fails bundle-exists.
    if out_path.is_symlink() or os.path.lexists(out_path):
        raise SupportBundleError("Refusing to overwrite existing bundle output",
                                 code="bundle-exists")
    parent_resolved = _validate_parent(out_path.parent)
    if out_path.suffix.lower() != ".zip":
        raise SupportBundleError("Bundle output must be a .zip file",
                                 code="bundle-unsafe-path")
    # Resolve states: explicit local-admin paths only, no home search.
    # Node defaults to the standard state only when omitted and valid.
    requested_node: Path | None
    if node_state is None:
        candidate = _default_node_state()
        try:
            # Include the default Node state only when it exists and passes
            # normal validation; otherwise omit without failing the bundle.
            if candidate.exists() and not candidate.is_symlink():
                # Light validation: try loading config without network.
                try:
                    from .node_launchd import load_node_config as _nload
                    _nload(candidate, require_roots=False)
                    requested_node = candidate
                except Exception:
                    try:
                        # Systemd-backed validation already covered by
                        # service_status; treat unreadable as omitted.
                        requested_node = None
                    except Exception:
                        requested_node = None
                    # Still attempt service evidence; _collect handles errors.
                    # Prefer omission over failure: only include when the
                    # config loads.
                    requested_node = None
            else:
                requested_node = None
        except Exception:
            requested_node = None
    else:
        requested_node = _absolute_path(node_state)
    requested_adapters: list[Path] = []
    for item in (adapter_states or []):
        try:
            requested_adapters.append(_absolute_path(item))
        except Exception:
            continue
    # Collection mode mirrors doctor: offline skips network probes.
    mode = "offline" if offline else "live"
    # Diagnostics: canonical report projected strictly; missing Bridge state
    # still yields a sanitized initialization diagnostic.
    diagnostics_raw: dict
    bridge_release_identity: dict | None = None
    try:
        from .release import bridge_release as _bridge
        try:
            bridge_release_identity = _bridge()
        except Exception:
            bridge_release_identity = None
    except Exception:
        bridge_release_identity = None
    try:
        # Local import to avoid loading Service unless needed.
        from .cli import load_config as _load_config
        from .service import Service as _Service
        # Honor the global Bridge --state (passed as bridge_state); when
        # omitted in direct calls default to normal DEFAULT_STATE.
        from .cli import DEFAULT_STATE as _DEFAULT
        if bridge_state is None:
            bridge_state_path = _absolute_path(_DEFAULT)
        else:
            bridge_state_path = _absolute_path(bridge_state)
        try:
            config = _load_config(bridge_state_path)
        except Exception:
            raise FileNotFoundError("bridge state unavailable")
        service = _Service(bridge_state_path, config, read_only=True,
                           run_coordinator_background=False)
        try:
            diagnostics_raw = service.diagnostic_report(offline=offline)
        finally:
            try:
                service.close()
            except Exception:
                pass
    except Exception:
        try:
            from .diagnostics import failure_report as _failure
            diagnostics_raw = _failure(mode=mode,
                                       summary="Configuration or local state could not be read.")
        except Exception:
            diagnostics_raw = {"overall": {"status": "failed", "counts": {}},
                               "checks": [], "runnable_routes": [], "release": {}}
    diagnostics = project_diagnostics(diagnostics_raw)
    services, log_map = _collect_service_evidence(node_state=requested_node,
                                                  adapter_states=requested_adapters,
                                                  offline=offline,
                                                  platform_name=platform_name)
    # Assemble fixed generic entries, deterministically ordered.
    entries: dict[str, bytes] = {}
    included: list[str] = []
    omitted: list[str] = []
    # Manifest is built last so it can record included/omitted categories.
    diagnostics_bytes = (json.dumps(diagnostics, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":")) + "\n").encode("utf-8")
    services_bytes = (json.dumps(services, sort_keys=True, ensure_ascii=False,
                                 separators=(",", ":")) + "\n").encode("utf-8")
    entries["diagnostics.json"] = diagnostics_bytes
    included.append("diagnostics")
    entries["services.json"] = services_bytes
    if "node" in services or "adapters" in services:
        included.append("services")
    else:
        omitted.append("omitted-no-local-services")
    for code in (services.get("omissions") or []):
        if isinstance(code, str) and code:
            omitted.append(code[:64])
    for value in (services.get("node") or {}).get("log_omissions", []) if isinstance(services.get("node"), dict) else []:
        if isinstance(value, str) and value:
            omitted.append(value[:64])
    for adapter in services.get("adapters", []) if isinstance(services.get("adapters"), list) else []:
        if isinstance(adapter, dict):
            for value in adapter.get("log_omissions", []) or []:
                if isinstance(value, str) and value:
                    omitted.append(value[:64])
            if isinstance(adapter.get("omission"), str) and adapter["omission"]:
                omitted.append(adapter["omission"][:64])
    # Log entries: fixed generic names, sanitized JSONL.
    for name in sorted(log_map):
        lines = log_map[name]
        # Enforce generic naming: logs/node.jsonl or logs/adapter-NN-<rt>.jsonl
        if name not in {"logs/node.jsonl"} and not re.fullmatch(
                r"logs/adapter-\d{2}-(pi|codex|claude|unknown)\.jsonl", name):
            continue
        payload = ("".join(line + "\n" for line in lines)).encode("utf-8")
        entries[name] = payload
        if lines:
            included.append(name)
        else:
            omitted.append(f"omitted-empty-{name}")
    def _manifest_bytes(cur_included: list[str], cur_omitted: list[str]) -> bytes:
        manifest_obj = _build_manifest(mode=mode, included=cur_included,
                                       omitted=cur_omitted,
                                       release_bridge=bridge_release_identity,
                                       platform_name=platform_name)
        return (json.dumps(manifest_obj, sort_keys=True, ensure_ascii=False,
                           separators=(',', ':')) + '\n').encode('utf-8')
    # True <=5 MiB bound INCLUDING manifest: iteratively finalize manifest
    # after each omission decision. manifest included/omitted stay
    # authoritative: oversize logs are removed from both entries and
    # included with one bounded omission code.
    manifest_bytes = _manifest_bytes(included, omitted)
    total = sum(len(data) for data in entries.values()) + len(manifest_bytes)
    while total > MAX_TOTAL_BYTES:
        log_names = sorted([n for n in entries if n.startswith('logs/')],
                           key=lambda n: len(entries[n]), reverse=True)
        if not log_names:
            raise SupportBundleError('Support bundle payload exceeds the 5 MiB bound',
                                     code='bundle-too-large')
        victim = log_names[0]
        omitted.append(f'omitted-oversize-{victim}'[:64])
        entries.pop(victim, None)
        if victim in included:
            included.remove(victim)
        manifest_bytes = _manifest_bytes(included, omitted)
        total = sum(len(data) for data in entries.values()) + len(manifest_bytes)
    entries['manifest.json'] = manifest_bytes
    ordered_names = sorted(entries.keys())
    # Write temp ZIP in the resolved parent, then enforce physical <=5 MiB
    # before atomic no-clobber publication. If the container still exceeds
    # the bound, omit more logs and rebuild rather than silently exceeding.
    def _write_temp(current_entries: dict[str, bytes],
                    current_ordered: list[str]) -> Path:
        tfd, tname = tempfile.mkstemp(prefix='.support-bundle-',
                                      suffix='.zip', dir=str(parent_resolved))
        tpath = Path(tname)
        try:
            os.chmod(tpath, 0o600)
            with os.fdopen(tfd, 'wb') as raw_stream:
                with zipfile.ZipFile(raw_stream, 'w', zipfile.ZIP_DEFLATED,
                                      compresslevel=9) as archive:
                    for ename in current_ordered:
                        info = zipfile.ZipInfo(ename, date_time=(1980, 1, 1, 0, 0, 0))
                        info.compress_type = zipfile.ZIP_DEFLATED
                        info.external_attr = (0o600 << 16)
                        archive.writestr(info, current_entries[ename])
                raw_stream.flush()
                try:
                    os.fsync(raw_stream.fileno())
                except OSError:
                    pass
            os.chmod(tpath, 0o600)
        except BaseException:
            try:
                Path(tname).unlink()
            except OSError:
                pass
            raise
        return tpath
    temporary_path: Path | None = None
    try:
        temporary_path = _write_temp(entries, ordered_names)
        # Physical container bound (compressed size on disk).
        while True:
            try:
                physical = int(temporary_path.stat().st_size)
            except OSError:
                raise SupportBundleError('Bundle output is unavailable',
                                         code='bundle-unavailable') from None
            if physical <= MAX_TOTAL_BYTES:
                break
            log_names = sorted([n for n in entries if n.startswith('logs/')],
                               key=lambda n: len(entries[n]), reverse=True)
            if not log_names:
                try:
                    temporary_path.unlink()
                except OSError:
                    pass
                temporary_path = None
                raise SupportBundleError('Support bundle payload exceeds the 5 MiB bound',
                                         code='bundle-too-large')
            victim = log_names[0]
            omitted.append(f'omitted-oversize-{victim}'[:64])
            entries.pop(victim, None)
            if victim in included:
                included.remove(victim)
            manifest_bytes = _manifest_bytes(included, omitted)
            entries['manifest.json'] = manifest_bytes
            ordered_names = sorted(entries.keys())
            try:
                temporary_path.unlink()
            except OSError:
                pass
            temporary_path = _write_temp(entries, ordered_names)
        # Atomic no-clobber publication: hard-link then unlink temp.
        # A destination appearing after preflight fails bundle-exists and
        # the existing bytes remain untouched (never replaced).
        try:
            _publish_no_clobber(temporary_path, out_path)
        except SupportBundleError:
            try:
                temporary_path.unlink()
            except OSError:
                pass
            temporary_path = None
            raise
        temporary_path = None
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass
    try:
        if stat.S_IMODE(out_path.stat().st_mode) != 0o600:
            try:
                os.chmod(out_path, 0o600)
            except OSError:
                pass
    except OSError:
        pass
    return {'path': str(out_path), 'entries': ordered_names,
            'omitted': sorted(set(omitted))[:20], 'mode': mode}


class SupportBundleError(ValueError):
    """Fail-closed support-bundle failure with a stable safe code."""

    def __init__(self, message: str, *, code: str = "bundle_failed"):
        super().__init__(message)
        self.code = code
