"""Persisted Pi execution ledger (milestone 3C1).

One logical record per toolCallId per Bridge run, synced idempotently
from the native adapter execution journal. Bounded tool-specific
evidence only; never raw event objects, reasoning, environment, runtime
tokens, or fullOutputPath. File audit never persists read contents or
complete edit/write inputs. Bash command/output can contain sensitive
data: list/summary surfaces never include result output, only exact
execution detail does. No perfect secret detection is claimed.

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
- Unknown tools fail safely (recorded as {error} without arbitrary args).
"""
from __future__ import annotations

import hashlib
import json
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
        return {"error": "unknown_tool"}
    except Exception:  # noqa: BLE001 - fail safely, never dump args
        return {"error": "malformed_input"}


def sanitize_result_summary(tool: str, summary: Any, is_error: bool | None = None) -> dict:
    """Validate and bound an adapter-supplied result summary.

    Never persists fullOutputPath, reasoning, environment, or tokens.
    Read carries metadata only, no contents.
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
        if isinstance(summary.get("status"), str):
            out["status"] = summary["status"][:80]
        if isinstance(summary.get("message"), str):
            out["message"] = summary["message"][:500]
        return out
    except Exception:  # noqa: BLE001
        return {"is_error": bool(summary.get("is_error")) if isinstance(summary, dict) else False}


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
    else:
        target = str(input_summary.get("target") or "")[:200]
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
                      "thinking", "provider"):
        result_summary.pop(forbidden, None)
        input_summary.pop(forbidden, None)
    base["input_summary"] = sanitize_input_summary(row.get("tool") or "", input_summary)
    # Re-sanitize result to enforce bounds even for historical rows.
    base["result_summary"] = sanitize_result_summary(row.get("tool") or "", result_summary)
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
