"""Persisted Pi execution ledger (milestone 3C1, extended 3C2).

One logical record per toolCallId per Bridge run, synced idempotently
from the native adapter execution journal. Bounded tool-specific
evidence only; never raw event objects, reasoning, environment, runtime
tokens, or fullOutputPath in structural form. File audit never persists
read contents or complete edit/write inputs. Bash command/output can contain
sensitive data: list/summary surfaces never include result output, only exact
execution detail does. Third-party extension tools (3C2) carry bounded
generic evidence with best-effort credential redaction; redaction is
best-effort, never perfect secret detection, and detail views stay
labeled potentially sensitive. No perfect secret detection is claimed.

Bounds (mirroring the native adapter):
- bash input: exact bounded command (16 KiB max), SHA-256, verified timeout.
- bash result: bounded final output preview (32 KiB), isError + only
  exit/cancel/truncation fields; never fullOutputPath.
- edit input: target + old/new byte counts + SHA-256; result: bounded
  preview (<=16 KiB).
- write input: target + byte/line count + SHA-256; result: bounded
  status/metadata.
- read: input target + range/options; result status/count/truncation only.
- grep/find/ls: input target + bounded query/options; result bounded
  preview + truncation flag.
- Extension tools: generic args hash/size/keys + redacted safe
  selectors; result preview (~8 KiB) with best-effort credential
  redaction. Redaction is best-effort, never perfect detection.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

MAX_COMMAND_CHARS = 16384
MAX_OUTPUT_CHARS = 32768
MAX_EDIT_PREVIEW_CHARS = 16384
MAX_LIST_PREVIEW_CHARS = 8192
MAX_TARGET_CHARS = 1024
MAX_QUERY_CHARS = 2048
MAX_SUMMARY_JSON_BYTES = 65536

AUDIT_STATUSES = ("pending", "complete", "incomplete", "not_recorded")
MUTATING_TOOLS = ("edit", "write", "bash")

# Bridge-managed built-ins with tool-specific audit shapes. Any other tool
# name is a third-party extension capability with the bounded generic
# extension-tool evidence (3C2). Managed file/shell policy is NOT a sandbox
# for extension internals or extension tools.
MANAGED_TOOLS = ("read", "grep", "find", "ls", "edit", "write", "bash")

# Generic third-party extension-tool audit bounds (mirroring the native
# adapter): input carries canonical args hash + size + top-level keys plus
# only bounded safe selector fields; result carries a bounded ToolResult
# text preview (~8 KiB). Never full args, fullOutputPath, env, auth,
# provider payloads, reasoning, cookies, tokens, or nested objects.
MAX_EXTENSION_ARGS_BYTES = 16384
MAX_EXTENSION_KEYS = 20
MAX_EXTENSION_KEY_CHARS = 80
MAX_EXTENSION_SELECTOR_CHARS = 500
MAX_EXTENSION_PREVIEW_CHARS = 8192
EXTENSION_SELECTOR_FIELDS = ("query", "url", "server", "tool", "name", "method", "path")

#: Sensitivity label for exact extension-tool result previews shown in
#: execution detail views.
EXTENSION_RESULT_SENSITIVITY = "potentially sensitive"

_SENSITIVE_PARAM_RE = re.compile(
    r"^(?:.*?(?:token|secret|passwd|password|auth|cookie|session|api[_-]?key|apikey|client[_-]?secret)"
    r"|key|.*?[{_-]key)$", re.IGNORECASE)
_CREDENTIAL_KV_RE = re.compile(
    r"\b(password|passwd|secret|api[_-]?key|apikey|access[_-]?token|auth[_-]?token|client[_-]?secret)"
    r"(\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|[^\s\"'{},;]+)", re.IGNORECASE)


def redact_extension_text(value: Any) -> str:
    """Conservative best-effort credential redaction for extension strings.

    Mirrors the native adapter normalizer so a compromised or old adapter
    cannot bypass it at the persistence/detail boundary: URL userinfo,
    sensitive query params, Bearer/Basic forms, and obvious key=value
    credential patterns. Best-effort only, never perfect secret
    detection; detail views remain labeled potentially sensitive.
    """
    if not isinstance(value, str) or not value:
        return ""
    out = re.sub(r"([a-zA-Z][a-zA-Z0-9+.-]*://)[^/\s?#@]+@", r"\1[redacted]@", value)

    def _param(match: re.Match) -> str:
        sep, name, val = match.group(1), match.group(2), match.group(3)
        if not val:
            return match.group(0)
        if _SENSITIVE_PARAM_RE.match(name):
            return f"{sep}{name}=[redacted]"
        return match.group(0)

    out = re.sub(r"([?&])([^?&#=\s;]+)=([^&#\s;]*)", _param, out)
    out = re.sub(r"\bBearer\s+[A-Za-z0-9\-._~+/=]+", "Bearer [redacted]", out)
    out = re.sub(r"\bBasic\s+[A-Za-z0-9+/=]+", "Basic [redacted]", out)
    out = _CREDENTIAL_KV_RE.sub(r"\1\2[redacted]", out)
    return out


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _bounded_str(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return value[:limit]


def sanitize_input_summary(tool: str, summary: Any) -> dict:
    """Validate and bound an adapter-supplied input summary.

    Drops arbitrary fields; enforces per-tool allowlists and bounds.
    Unknown tools return {error} safely.
    """
    if not isinstance(summary, dict):
        return {"error": "malformed_input"}
    try:
        if tool == "bash":
            command = summary.get("command")
            if not isinstance(command, str) or not command:
                return {"error": "malformed_input"}
            command = command[:MAX_COMMAND_CHARS]
            out: dict[str, Any] = {
                "command": command,
                "command_sha256": summary.get("command_sha256") or _sha256(command),
                "timeout_ms": 30000,
                "truncated": bool(summary.get("truncated")),
            }
            sha = out["command_sha256"]
            if not isinstance(sha, str) or len(sha) != 64:
                out["command_sha256"] = _sha256(command)
            try:
                timeout = int(summary.get("timeout_ms") or 30000)
            except (TypeError, ValueError):
                timeout = 30000
            out["timeout_ms"] = max(1000, min(timeout, 300000))
            if isinstance(summary.get("command_bytes"), int):
                out["command_bytes"] = max(0, summary["command_bytes"])
            return out
        if tool == "read":
            target = _bounded_str(summary.get("target"), MAX_TARGET_CHARS)
            if not target:
                return {"error": "malformed_input"}
            out = {"target": target}
            for key in ("range", "encoding"):
                if isinstance(summary.get(key), str):
                    out[key] = summary[key][:200]
            for key in ("offset", "limit"):
                if isinstance(summary.get(key), int):
                    out[key] = max(0, min(summary[key], 10000))
            return out
        if tool in ("grep", "find", "ls"):
            out = {"target": _bounded_str(summary.get("target", "."), MAX_TARGET_CHARS)}
            if isinstance(summary.get("query"), str):
                out["query"] = summary["query"][:MAX_QUERY_CHARS]
            if isinstance(summary.get("options"), str):
                out["options"] = summary["options"][:500]
            if isinstance(summary.get("limit"), int):
                out["limit"] = max(0, min(summary["limit"], 10000))
            if isinstance(summary.get("recursive"), bool):
                out["recursive"] = summary["recursive"]
            return out
        if tool == "edit":
            target = _bounded_str(summary.get("target"), MAX_TARGET_CHARS)
            if not target:
                return {"error": "malformed_input"}
            out = {"target": target}
            for key in ("old_bytes", "new_bytes"):
                if isinstance(summary.get(key), int):
                    out[key] = max(0, summary[key])
            for key in ("old_sha256", "new_sha256"):
                if isinstance(summary.get(key), str) and len(summary[key]) == 64:
                    out[key] = summary[key]
            return out
        if tool == "write":
            target = _bounded_str(summary.get("target"), MAX_TARGET_CHARS)
            if not target:
                return {"error": "malformed_input"}
            out = {"target": target}
            for key in ("content_bytes", "content_lines"):
                if isinstance(summary.get(key), int):
                    out[key] = max(0, summary[key])
            if isinstance(summary.get("content_sha256"), str) and len(summary["content_sha256"]) == 64:
                out["content_sha256"] = summary["content_sha256"]
            return out
        # Third-party extension tools (3C2): bounded generic evidence only.
        if tool not in MANAGED_TOOLS:
            return sanitize_extension_input(summary)
        return {"error": "unknown_tool"}
    except Exception:  # noqa: BLE001 - fail safely, never dump args
        return {"error": "malformed_input"}


def sanitize_extension_input(summary: Any) -> dict:
    """Validate and bound an extension-tool input summary (3C2).

    Accepts the adapter-normalized shape (canonical args hash + size +
    top-level keys plus bounded safe selectors) and drops everything
    else, including any forbidden keys a compromised adapter might have
    sent. A raw (non-normalized) mapping is derived defensively into the
    same bounded shape instead of being persisted verbatim.
    """
    if not isinstance(summary, dict):
        return {"error": "malformed_input"}
    try:
        keys = summary.get("top_keys")
        sha = summary.get("args_sha256")
        if (isinstance(keys, list) and isinstance(sha, str) and len(sha) == 64):
            out: dict[str, Any] = {}
            out["args_sha256"] = sha
            try:
                size = int(summary.get("args_bytes", 0))
            except (TypeError, ValueError):
                size = 0
            out["args_bytes"] = max(0, min(size, MAX_EXTENSION_ARGS_BYTES * 4))
            out["top_keys"] = [str(k)[:MAX_EXTENSION_KEY_CHARS]
                               for k in keys if isinstance(k, str)][:MAX_EXTENSION_KEYS]
            for field in EXTENSION_SELECTOR_FIELDS:
                value = summary.get(field)
                if isinstance(value, str) and value:
                    out[field] = redact_extension_text(value)[:MAX_EXTENSION_SELECTOR_CHARS]
            return out
        # Defensive derivation from a raw mapping (never verbatim).
        try:
            canonical = json.dumps(summary, sort_keys=True, ensure_ascii=False,
                                   separators=(",", ":"), default=str)
        except (TypeError, ValueError):
            return {"error": "malformed_input"}
        out = {
            "args_sha256": _sha256(canonical),
            "args_bytes": len(canonical.encode("utf-8")),
            "top_keys": [str(k)[:MAX_EXTENSION_KEY_CHARS]
                         for k in summary.keys() if isinstance(k, str)][:MAX_EXTENSION_KEYS],
        }
        for field in EXTENSION_SELECTOR_FIELDS:
            value = summary.get(field)
            if isinstance(value, str) and value:
                out[field] = redact_extension_text(value)[:MAX_EXTENSION_SELECTOR_CHARS]
        return out
    except Exception:  # noqa: BLE001
        return {"error": "malformed_input"}


def sanitize_result_summary(tool: str, summary: Any, is_error: bool | None = None) -> dict:
    """Validate and bound an adapter-supplied result summary.

    Never persists fullOutputPath, reasoning, or environment in
    structural form. Extension-tool previews get best-effort credential
    redaction (a result body can itself print secrets); redaction is
    best-effort, never perfect detection. Read carries metadata only,
    no contents.
    """
    if not isinstance(summary, dict):
        return {"is_error": bool(is_error) if is_error is not None else False}
    try:
        if is_error is not None:
            out: dict[str, Any] = {"is_error": bool(is_error)}
        else:
            out = {"is_error": bool(summary.get("is_error"))}
        if isinstance(summary.get("exit_code"), int):
            out["exit_code"] = summary["exit_code"]
        if summary.get("cancelled") is True:
            out["cancelled"] = True
        if summary.get("truncated") is True:
            out["truncated"] = True
        if tool == "bash":
            preview = summary.get("output_preview", "")
            if not isinstance(preview, str) or not preview:
                for fallback in ("output", "stdout", "text"):
                    candidate = summary.get(fallback)
                    if isinstance(candidate, str) and candidate:
                        preview = candidate
                        break
            if not isinstance(preview, str):
                preview = ""
            truncated = len(preview) > MAX_OUTPUT_CHARS or bool(summary.get("truncated"))
            out["output_preview"] = preview[:MAX_OUTPUT_CHARS]
            if isinstance(summary.get("output_bytes"), int):
                out["output_bytes"] = max(0, summary["output_bytes"])
            out["truncated"] = truncated
            return out
        if tool == "read":
            for key in ("count", "bytes"):
                if isinstance(summary.get(key), int):
                    out[key] = max(0, summary[key])
            if isinstance(summary.get("status"), str):
                out["status"] = summary["status"][:80]
            return out
        if tool == "edit":
            preview = summary.get("preview", "")
            if isinstance(preview, str) and preview:
                truncated = len(preview) > MAX_EDIT_PREVIEW_CHARS or bool(summary.get("truncated"))
                out["preview"] = preview[:MAX_EDIT_PREVIEW_CHARS]
                out["truncated"] = truncated
            if isinstance(summary.get("status"), str):
                out["status"] = summary["status"][:80]
            return out
        if tool == "write":
            if isinstance(summary.get("status"), str):
                out["status"] = summary["status"][:80]
            if isinstance(summary.get("bytes"), int):
                out["bytes"] = max(0, summary["bytes"])
            if isinstance(summary.get("message"), str):
                out["message"] = summary["message"][:500]
            return out
        if tool in ("grep", "find", "ls"):
            preview = summary.get("preview", "")
            if isinstance(preview, str) and preview:
                truncated = len(preview) > MAX_LIST_PREVIEW_CHARS or bool(summary.get("truncated"))
                out["preview"] = preview[:MAX_LIST_PREVIEW_CHARS]
                out["truncated"] = truncated
            if isinstance(summary.get("count"), int):
                out["count"] = max(0, summary["count"])
            return out
        # Third-party extension tools (3C2): bounded generic result only.
        if tool not in MANAGED_TOOLS:
            return sanitize_extension_result(summary,
                                             is_error if is_error is not None
                                             else bool(summary.get("is_error")))
        if isinstance(summary.get("status"), str):
            out["status"] = summary["status"][:80]
        if isinstance(summary.get("message"), str):
            out["message"] = summary["message"][:500]
        return out
    except Exception:  # noqa: BLE001
        return {"is_error": bool(summary.get("is_error")) if isinstance(summary, dict) else False}


def sanitize_extension_result(summary: Any, is_error: bool = False) -> dict:
    """Validate and bound an extension-tool result summary (3C2).

    Keeps only the verified ToolResult text preview (~8 KiB) with
    best-effort credential redaction, plus truncation/error flags.
    Structural args/details/auth/env/provider/fullOutputPath are never
    kept. Redaction is best-effort, never perfect secret detection:
    detail views stay labeled potentially sensitive.
    """
    out: dict[str, Any] = {"is_error": bool(is_error)}
    if not isinstance(summary, dict):
        return out
    try:
        if summary.get("cancelled") is True:
            out["cancelled"] = True
        if summary.get("truncated") is True:
            out["truncated"] = True
        preview = summary.get("preview", "")
        if not isinstance(preview, str) or not preview:
            for fallback in ("output_preview", "output", "stdout", "text"):
                candidate = summary.get(fallback)
                if isinstance(candidate, str) and candidate:
                    preview = candidate
                    break
        if (not isinstance(preview, str) or not preview) and isinstance(
                summary.get("content"), list):
            # Defensive: verified Pi ToolResult content blocks only.
            parts = [b.get("text") for b in summary["content"][:32]
                     if isinstance(b, dict) and b.get("type") == "text"
                     and isinstance(b.get("text"), str)]
            preview = "".join(parts)
        if not isinstance(preview, str):
            preview = ""
        truncated = len(preview) > MAX_EXTENSION_PREVIEW_CHARS or bool(summary.get("truncated"))
        redacted = redact_extension_text(preview)
        if redacted:
            out["preview"] = redacted[:MAX_EXTENSION_PREVIEW_CHARS]
        if isinstance(summary.get("preview_bytes"), int):
            out["preview_bytes"] = max(0, summary["preview_bytes"])
        out["truncated"] = truncated
        return out
    except Exception:  # noqa: BLE001
        return {"is_error": bool(is_error)}


def bounded_json(value: dict) -> str:
    """Canonical bounded JSON for persistence (never exceeds budget)."""
    try:
        text = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        text = "{}"
    encoded = text.encode("utf-8")
    if len(encoded) > MAX_SUMMARY_JSON_BYTES:
        # Truncate safely: keep a bounded error marker, never partial secrets.
        return json.dumps({"error": "summary_too_large"}, separators=(",", ":"))
    return text


def summary_record(row: dict) -> dict:
    """List/summary surface: no output body or raw source content.

    Returns id/sequence/tool/state/target-or-command preview/timing/
    duration/error/permission effect+decision/truncation only. The
    sequence field is the STABLE start cursor (global update-sequence
    value assigned at the tool's start), so list ordering never shifts
    when a record later completes; the moving update cursor is tracked
    only in the run's execution_cursor and never exposed here.
    """
    try:
        input_summary = json.loads(row.get("input_summary") or "{}")
    except ValueError:
        input_summary = {}
    if not isinstance(input_summary, dict):
        input_summary = {}
    tool = row.get("tool") or ""
    if tool == "bash":
        target = str(input_summary.get("command") or "")[:200]
    elif tool in MANAGED_TOOLS:
        target = str(input_summary.get("target") or "")[:200]
    else:
        # Extension-tool list surface: no result body; a bounded safe
        # selector (or nothing) as the target preview, with the same
        # best-effort credential redaction as detail views.
        target = ""
        for field in EXTENSION_SELECTOR_FIELDS:
            candidate = input_summary.get(field)
            if isinstance(candidate, str) and candidate:
                target = redact_extension_text(candidate)[:200]
                break
    return {
        "execution_id": row["tool_call_id"],
        "tool_call_id": row["tool_call_id"],
        "seq": row.get("seq"),
        "sequence": row.get("seq"),
        "tool": tool,
        "state": row.get("state"),
        "target_preview": target,
        "started": row.get("started"),
        "ended": row.get("ended"),
        "duration_ms": row.get("duration_ms"),
        "is_error": bool(row.get("is_error")),
        "permission_effect": row.get("permission_effect") or "",
        "permission_decision": row.get("permission_decision") or "",
        "truncated": bool(row.get("truncated")),
    }


def detail_record(row: dict) -> dict:
    """Detail surface: bounded sanitized input/result evidence.

    Includes bash output preview when present, but never reasoning,
    environment, runtime tokens, or fullOutputPath.
    """
    base = summary_record(row)
    try:
        input_summary = json.loads(row.get("input_summary") or "{}")
    except ValueError:
        input_summary = {}
    try:
        result_summary = json.loads(row.get("result_summary") or "{}")
    except ValueError:
        result_summary = {}
    if not isinstance(input_summary, dict):
        input_summary = {}
    if not isinstance(result_summary, dict):
        result_summary = {}
    # Defensive: strip any forbidden keys that a compromised adapter
    # might have sent (fullOutputPath, tokens, env, reasoning).
    for forbidden in ("fullOutputPath", "full_output_path", "token",
                      "runtime_token", "environment", "env", "reasoning",
                      "thinking", "provider", "cookie", "cookies",
                      "authorization", "auth", "secret"):
        result_summary.pop(forbidden, None)
        input_summary.pop(forbidden, None)
    tool_name = row.get("tool") or ""
    base["input_summary"] = sanitize_input_summary(tool_name, input_summary)
    # Re-sanitize result to enforce bounds even for historical rows.
    base["result_summary"] = sanitize_result_summary(tool_name, result_summary)
    if tool_name not in MANAGED_TOOLS and isinstance(
            base["result_summary"], dict) and base["result_summary"].get("preview"):
        # Exact extension-tool result previews are shown only here and
        # are labeled as potentially sensitive.
        base["result_sensitivity"] = EXTENSION_RESULT_SENSITIVITY
    # For list safety the base summary already excludes bodies; detail
    # explicitly includes the bounded bash preview above.
    return base


def audit_counts(rows: list[dict]) -> dict:
    """Counts for run audit summary: total/failed/shell/mutating."""
    total = len(rows)
    failed = sum(1 for r in rows if r.get("is_error"))
    shell = sum(1 for r in rows if (r.get("tool") or "") == "bash")
    mutating = sum(1 for r in rows if (r.get("tool") or "") in MUTATING_TOOLS)
    return {"total": total, "failed": failed, "shell": shell, "mutating": mutating}
