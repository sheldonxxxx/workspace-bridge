"""Web-configurable Pi file-tool permission policy (milestone 3B1).

The Bridge is the source of truth for the *operational* permission policy:
master enable, per-tool allow/ask/deny for the six file tools, bounded
workspace-relative protected glob lists, and session-always availability.
Protocol/security invariants (exact session+toolCallId correlation,
canonical/symlink-aware confinement, outside-workspace deny, malformed
fail-closed, self-protection of the permission implementation, no bash,
no project/global extension discovery, no approval persistence) are
non-configurable and enforced natively by the adapter.

Storage: the existing ``settings`` table under key
``runtime_permission_policy:pi``. Policy changes apply to NEW Pi sessions
only; each Pi session carries an immutable snapshot plus a stable
``policy_revision`` (sha256 of the canonical JSON). Continuation requires
the source run revision to equal the current revision.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from .security import BridgeError

PI_PERMISSION_POLICY_SETTING = "runtime_permission_policy:pi"
PI_PERMISSION_POLICY_VERSION = 1

SUPPORTED_PI_PERMISSION_TOOLS = ("read", "grep", "find", "ls", "edit", "write")
PI_PERMISSION_MODES = ("allow", "ask", "deny")

MAX_PROTECTED_PATTERNS = 64
MAX_PATTERN_CHARS = 400
MAX_POLICY_BYTES = 32768

FIXED_INVARIANTS = (
    "Exact session + toolCallId + UI-request correlation",
    "Canonical/symlink-aware confinement to the mapped workspace",
    "Outside-workspace access denied",
    "Malformed or unknown tool input denied",
    "Permission implementation is self-protected from edit/write",
    "Trusted extension path is package-owned, never admin/project/env-selectable",
    "No bash tool",
    "No project or global extension discovery",
    "Approvals are session-local and never persisted to disk",
)


def safe_defaults() -> dict:
    """Safe deployment default: Pi stays read-only."""
    return {
        "version": PI_PERMISSION_POLICY_VERSION,
        "enabled": False,
        "tools": {
            "read": "allow",
            "grep": "allow",
            "find": "allow",
            "ls": "allow",
            "edit": "ask",
            "write": "ask",
        },
        "protected_patterns": [
            ".git/**",
            ".env",
            ".env.*",
            ".workspace-handoff/**",
        ],
        "protected_template_exceptions": [
            ".env.example",
            ".env.sample",
            ".env.template",
        ],
        "allow_session_always": True,
    }


def _check_pattern_list(value: Any, *, name: str) -> list[str]:
    if not isinstance(value, list):
        raise BridgeError(f"Pi permission policy {name} must be a list", "invalid_arguments")
    if len(value) > MAX_PROTECTED_PATTERNS:
        raise BridgeError(
            f"Pi permission policy {name} allows at most {MAX_PROTECTED_PATTERNS} patterns",
            "invalid_arguments")
    cleaned: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item or len(item) > MAX_PATTERN_CHARS:
            raise BridgeError(
                f"Pi permission policy {name} entries must be 1..{MAX_PATTERN_CHARS} chars",
                "invalid_arguments")
        if "\x00" in item:
            raise BridgeError(f"Pi permission policy {name} entry is invalid", "invalid_arguments")
        text = item.strip()
        if not text or len(text) > MAX_PATTERN_CHARS:
            raise BridgeError(f"Pi permission policy {name} entry is invalid", "invalid_arguments")
        if text.startswith("/") or text.startswith("\\"):
            raise BridgeError(
                f"Pi permission policy {name} must be workspace-relative, not absolute: {text[:80]}",
                "invalid_arguments")
        parts = [p for p in text.replace("\\", "/").split("/") if p not in ("", ".")]
        if ".." in parts:
            raise BridgeError(
                f"Pi permission policy {name} must not traverse with '..': {text[:80]}",
                "invalid_arguments")
        # Reject glob syntax beyond the documented simple subset
        # (* ? ** and [...] character classes only).
        allowed_chars = set(
            "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
            "-_. /+*{ }?[],!@#%^()=:")
        if any(c not in allowed_chars for c in text):
            raise BridgeError(
                f"Pi permission policy {name} entry uses unsupported syntax: {text[:80]}",
                "invalid_arguments")
        if "[" in text and "]" not in text:
            raise BridgeError(
                f"Pi permission policy {name} entry has an unterminated character class: {text[:80]}",
                "invalid_arguments")
        cleaned.append(text)
    # Dedupe preserving order.
    seen: set[str] = set()
    result: list[str] = []
    for entry in cleaned:
        if entry not in seen:
            seen.add(entry)
            result.append(entry)
    return result


def validate_policy(raw: Any) -> dict:
    """Strictly validate a full v1 policy object; never partially applies.

    Raises BridgeError on any violation. Returns the canonical policy dict.
    """
    if raw is None:
        raise BridgeError("Pi permission policy is required", "invalid_arguments")
    if isinstance(raw, str):
        if len(raw.encode("utf-8")) > MAX_POLICY_BYTES:
            raise BridgeError("Pi permission policy body is too large", "invalid_arguments")
        try:
            raw = json.loads(raw)
        except ValueError:
            raise BridgeError("Pi permission policy must be a JSON object", "invalid_arguments") from None
    if not isinstance(raw, dict):
        raise BridgeError("Pi permission policy must be a JSON object", "invalid_arguments")
    if set(raw.keys()) - {"version", "enabled", "tools", "protected_patterns",
                           "protected_template_exceptions", "allow_session_always"}:
        raise BridgeError("Pi permission policy has unknown fields", "invalid_arguments")
    if raw.get("version") != PI_PERMISSION_POLICY_VERSION:
        raise BridgeError(
            f"Pi permission policy version must be {PI_PERMISSION_POLICY_VERSION}",
            "invalid_arguments")
    if not isinstance(raw.get("enabled"), bool):
        raise BridgeError("Pi permission policy 'enabled' must be a boolean", "invalid_arguments")
    tools = raw.get("tools")
    if not isinstance(tools, dict) or set(tools.keys()) != set(SUPPORTED_PI_PERMISSION_TOOLS):
        raise BridgeError(
            "Pi permission policy 'tools' must cover exactly: "
            + ", ".join(SUPPORTED_PI_PERMISSION_TOOLS),
            "invalid_arguments")
    for name in SUPPORTED_PI_PERMISSION_TOOLS:
        if tools[name] not in PI_PERMISSION_MODES:
            raise BridgeError(
                f"Pi permission policy tool {name!r} must be one of allow, ask, deny",
                "invalid_arguments")
    protected = _check_pattern_list(raw.get("protected_patterns"), name="protected_patterns")
    exceptions = _check_pattern_list(raw.get("protected_template_exceptions"),
                                     name="protected_template_exceptions")
    if not isinstance(raw.get("allow_session_always"), bool):
        raise BridgeError(
            "Pi permission policy 'allow_session_always' must be a boolean", "invalid_arguments")
    return {
        "version": PI_PERMISSION_POLICY_VERSION,
        "enabled": bool(raw["enabled"]),
        "tools": {name: tools[name] for name in SUPPORTED_PI_PERMISSION_TOOLS},
        "protected_patterns": protected,
        "protected_template_exceptions": exceptions,
        "allow_session_always": bool(raw["allow_session_always"]),
    }


def canonical_json(policy: dict) -> str:
    return json.dumps(policy, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def policy_revision(policy: dict) -> str:
    """Stable revision: sha256 hex of the canonical policy JSON."""
    return hashlib.sha256(canonical_json(policy).encode("utf-8")).hexdigest()


def get_policy(service) -> tuple[dict, str, bool]:
    """Return (policy, revision, configured).

    Missing or corrupt stored values fail closed to safe defaults;
    ``configured`` reports whether a valid stored policy exists. Never raises
    for storage content; validation errors on save raise instead.
    """
    raw = service.setting(PI_PERMISSION_POLICY_SETTING)
    if not raw:
        policy = safe_defaults()
        return policy, policy_revision(policy), False
    try:
        policy = validate_policy(json.loads(raw))
    except (BridgeError, ValueError):
        policy = safe_defaults()
        return policy, policy_revision(policy), False
    return policy, policy_revision(policy), True


def set_policy(service, raw: Any) -> tuple[dict, str]:
    """Validate strictly, then persist atomically. Invalid input never applies."""
    policy = validate_policy(raw)
    service.set_setting(PI_PERMISSION_POLICY_SETTING, canonical_json(policy))
    service.event(None, "set_runtime_permission_policy")
    return policy, policy_revision(policy)


def status_summary(service) -> dict:
    """Bounded admin status summary: modes + counts, no patterns/paths."""
    policy, revision, configured = get_policy(service)
    writable = bool(policy["enabled"])
    return {
        "runtime": "pi",
        "policy_scope": "runtime_global",
        "configured": configured,
        "enabled": writable,
        "effective_writable": writable,
        "tools": dict(policy["tools"]),
        "allow_session_always": bool(policy["allow_session_always"]),
        "protected_pattern_count": len(policy["protected_patterns"]),
        "protected_template_exception_count": len(policy["protected_template_exceptions"]),
        "policy_revision": revision,
        "supported_tools": list(SUPPORTED_PI_PERMISSION_TOOLS),
        "fixed_invariants": list(FIXED_INVARIANTS),
        "session_note": "Policy changes apply to NEW Pi sessions only; "
                        "active sessions keep their snapshot.",
    }


def full_view(service) -> dict:
    """Full admin GET view: effective policy plus metadata (local-admin only)."""
    policy, revision, configured = get_policy(service)
    return {
        "runtime": "pi",
        "policy_scope": "runtime_global",
        "configured": configured,
        "policy": policy,
        "policy_revision": revision,
        "supported_tools": list(SUPPORTED_PI_PERMISSION_TOOLS),
        "fixed_invariants": list(FIXED_INVARIANTS),
        "session_note": "Policy changes apply to NEW Pi sessions only; "
                        "active sessions keep their snapshot.",
    }
