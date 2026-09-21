"""Web-configurable Pi permission policy (milestone 3C1, policy v3).

The Bridge is the source of truth for the *operational* permission policy:
write-tool exposure switch, per-tool allow/ask/deny for the six file tools,
bounded workspace-relative protected glob lists, session-always
availability, the external (outside-workspace) file scope: a default
mode plus bounded absolute host root overrides, and a single shell
authority mode (``shell_mode`` = deny|ask|allow, default deny).

Protocol integrity invariants (exact session+toolCallId correlation,
stale/forged approvals never authorize, immutable session policy revision,
package-owned trusted extension loading identity, malformed fail-closed,
no silent authority widening/fallback) are non-configurable and enforced
natively by the adapter. There are no project-name/path-specific rules,
no permission-source or control-directory fixed denies, and no command
rule list: ordinary configurable file/external policy applies to normal
project edits, and shell authority is a single Deny/Ask/Allow selector
over the Pi built-in bash tool only (no powershell, no regex/prefix
allowlist/denylist).

Shell Allow runs with native macOS-user authority and can bypass
structured file path controls; Ask pauses each bash invocation with the
existing suspended-call permission flow.

Storage: the existing ``settings`` table under key
``runtime_permission_policy:pi``. Policy changes apply to NEW Pi sessions
only; each Pi session carries an immutable snapshot plus a stable
``policy_revision`` (sha256 of the canonical JSON). Continuation requires
the source run revision to equal the current revision.

Version history:
- v1 (3B1): ``enabled`` master switch; outside-workspace hardcoded deny.
- v2 (3B2): ``write_tools_enabled`` switch; web-configurable
  ``external_access``; read policy enforced in both modes. Stored v1
  policies migrate in memory to v2 (never silently persisted on GET).
- v3 (3C1): generic authority; adds ``shell_mode`` (default deny),
  removes fixed filesystem denies (self-protection/control-roots and
  project-specific rules). Stored v1/v2 policies migrate in memory to v3
  preserving every prior field exactly; the first v3 save persists v3 and
  changes the revision so older sessions cannot continue.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from .security import BridgeError

PI_PERMISSION_POLICY_SETTING = "runtime_permission_policy:pi"
PI_PERMISSION_POLICY_VERSION = 3
# Previous schema versions accepted ONLY for in-memory migration on read.
# Saves accept v3 exclusively.
PI_PERMISSION_POLICY_VERSION_V2 = 2
PI_PERMISSION_POLICY_VERSION_V1 = 1

SUPPORTED_PI_PERMISSION_TOOLS = ("read", "grep", "find", "ls", "edit", "write")
PI_PERMISSION_MODES = ("allow", "ask", "deny")
# Single shell authority selector over the Pi built-in bash tool only.
# No powershell, no command regex/prefix/allowlist/denylist.
PI_SHELL_MODES = ("deny", "ask", "allow")
PI_SHELL_TOOL = "bash"
DEFAULT_SHELL_MODE = "deny"

MAX_PROTECTED_PATTERNS = 64
MAX_PATTERN_CHARS = 400
MAX_POLICY_BYTES = 32768

MAX_EXTERNAL_ROOTS = 32
MAX_EXTERNAL_ROOT_CHARS = 1024

FIXED_INVARIANTS = (
    "Exact session + toolCallId + UI-request correlation",
    "Stale or forged approvals never authorize",
    "Session policy revision is immutable; changes apply to new sessions only",
    "Trusted extension path is package-owned, never admin/project/env-selectable",
    "Malformed protocol or tool input fails closed",
    "No silent authority widening or fallback",
)


def safe_defaults() -> dict:
    """Safe deployment default: Pi stays read-only, external denied, shell denied."""
    return {
        "version": PI_PERMISSION_POLICY_VERSION,
        "write_tools_enabled": False,
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
        "external_access": {
            "default_mode": "deny",
            "roots": [],
        },
        "shell_mode": DEFAULT_SHELL_MODE,
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


def _check_external_root(value: Any, *, index: int) -> dict:
    if not isinstance(value, dict):
        raise BridgeError(
            f"Pi permission policy external root #{index} must be an object",
            "invalid_arguments")
    if set(value.keys()) != {"path", "mode"}:
        raise BridgeError(
            f"Pi permission policy external root #{index} must have exactly 'path' and 'mode'",
            "invalid_arguments")
    raw_path = value.get("path")
    mode = value.get("mode")
    if not isinstance(raw_path, str) or not raw_path:
        raise BridgeError(
            f"Pi permission policy external root #{index} 'path' must be a non-empty string",
            "invalid_arguments")
    if "\x00" in raw_path:
        raise BridgeError(
            f"Pi permission policy external root #{index} 'path' is invalid",
            "invalid_arguments")
    text = raw_path.strip()
    if not text or len(text) > MAX_EXTERNAL_ROOT_CHARS:
        raise BridgeError(
            f"Pi permission policy external root #{index} 'path' must be "
            f"1..{MAX_EXTERNAL_ROOT_CHARS} chars",
            "invalid_arguments")
    # Absolute POSIX host path only. The Bridge validates syntax here; the
    # native adapter canonicalizes (realpath) and requires an existing
    # directory when a NEW session starts.
    if not text.startswith("/"):
        raise BridgeError(
            f"Pi permission policy external root #{index} 'path' must be an absolute path",
            "invalid_arguments")
    if "//" in text or text.endswith("/") and text != "/":
        # Normalize trailing slashes: only "/" may end with one.
        raise BridgeError(
            f"Pi permission policy external root #{index} 'path' is not normalized",
            "invalid_arguments")
    segments = [seg for seg in text.split("/") if seg not in ("", ".")]
    if ".." in segments:
        raise BridgeError(
            f"Pi permission policy external root #{index} 'path' must not traverse with '..'",
            "invalid_arguments")
    if mode not in PI_PERMISSION_MODES:
        raise BridgeError(
            f"Pi permission policy external root #{index} 'mode' must be one of allow, ask, deny",
            "invalid_arguments")
    return {"path": text, "mode": mode}


def _check_external_access(value: Any) -> dict:
    if not isinstance(value, dict):
        raise BridgeError(
            "Pi permission policy 'external_access' must be an object", "invalid_arguments")
    if set(value.keys()) != {"default_mode", "roots"}:
        raise BridgeError(
            "Pi permission policy 'external_access' must have exactly "
            "'default_mode' and 'roots'",
            "invalid_arguments")
    if value.get("default_mode") not in PI_PERMISSION_MODES:
        raise BridgeError(
            "Pi permission policy external 'default_mode' must be one of allow, ask, deny",
            "invalid_arguments")
    roots = value.get("roots")
    if not isinstance(roots, list):
        raise BridgeError(
            "Pi permission policy external 'roots' must be a list", "invalid_arguments")
    if len(roots) > MAX_EXTERNAL_ROOTS:
        raise BridgeError(
            f"Pi permission policy external 'roots' allows at most {MAX_EXTERNAL_ROOTS} roots",
            "invalid_arguments")
    cleaned = [_check_external_root(item, index=i) for i, item in enumerate(roots)]
    # Duplicate configured root paths fail rather than guess precedence.
    # (Canonical duplicates after realpath are rejected natively at
    # session creation.)
    seen: set[str] = set()
    for entry in cleaned:
        if entry["path"] in seen:
            raise BridgeError(
                f"Pi permission policy external root {entry['path'][:80]!r} is duplicated",
                "invalid_arguments")
        seen.add(entry["path"])
    return {"default_mode": value["default_mode"], "roots": cleaned}


def _check_shell_mode(value: Any) -> str:
    if value not in PI_SHELL_MODES:
        raise BridgeError(
            "Pi permission policy 'shell_mode' must be one of deny, ask, allow",
            "invalid_arguments")
    return str(value)


def validate_policy(raw: Any) -> dict:
    """Strictly validate a full v3 policy object; never partially applies.

    Raises BridgeError on any violation. Returns the canonical policy dict.
    v1/v2 payloads are rejected here; use migration helpers for reads.
    There is no command rule list: ``shell_mode`` is the only shell control
    (deny|ask|allow over the Pi built-in bash tool, no powershell).
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
    if set(raw.keys()) - {"version", "write_tools_enabled", "tools", "protected_patterns",
                           "protected_template_exceptions", "allow_session_always",
                           "external_access", "shell_mode"}:
        raise BridgeError("Pi permission policy has unknown fields", "invalid_arguments")
    if raw.get("version") != PI_PERMISSION_POLICY_VERSION:
        raise BridgeError(
            f"Pi permission policy version must be {PI_PERMISSION_POLICY_VERSION}",
            "invalid_arguments")
    if not isinstance(raw.get("write_tools_enabled"), bool):
        raise BridgeError(
            "Pi permission policy 'write_tools_enabled' must be a boolean", "invalid_arguments")
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
    external = _check_external_access(raw.get("external_access"))
    shell_mode = _check_shell_mode(raw.get("shell_mode"))
    return {
        "version": PI_PERMISSION_POLICY_VERSION,
        "write_tools_enabled": bool(raw["write_tools_enabled"]),
        "tools": {name: tools[name] for name in SUPPORTED_PI_PERMISSION_TOOLS},
        "protected_patterns": protected,
        "protected_template_exceptions": exceptions,
        "allow_session_always": bool(raw["allow_session_always"]),
        "external_access": external,
        "shell_mode": shell_mode,
    }


def _validate_v1(raw: Any) -> dict:
    """Strictly validate a stored v1 policy (3B1 schema) for migration."""
    if not isinstance(raw, dict):
        raise BridgeError("Pi permission policy must be a JSON object", "invalid_arguments")
    if set(raw.keys()) - {"version", "enabled", "tools", "protected_patterns",
                           "protected_template_exceptions", "allow_session_always"}:
        raise BridgeError("Pi permission policy has unknown fields", "invalid_arguments")
    if raw.get("version") != PI_PERMISSION_POLICY_VERSION_V1:
        raise BridgeError("Pi permission policy version is not v1", "invalid_arguments")
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
        "version": PI_PERMISSION_POLICY_VERSION_V1,
        "enabled": bool(raw["enabled"]),
        "tools": {name: tools[name] for name in SUPPORTED_PI_PERMISSION_TOOLS},
        "protected_patterns": protected,
        "protected_template_exceptions": exceptions,
        "allow_session_always": bool(raw["allow_session_always"]),
    }


def _validate_v2(raw: Any) -> dict:
    """Strictly validate a stored v2 policy (3B2 schema) for migration to v3."""
    if not isinstance(raw, dict):
        raise BridgeError("Pi permission policy must be a JSON object", "invalid_arguments")
    if set(raw.keys()) - {"version", "write_tools_enabled", "tools", "protected_patterns",
                           "protected_template_exceptions", "allow_session_always",
                           "external_access"}:
        raise BridgeError("Pi permission policy has unknown fields", "invalid_arguments")
    if raw.get("version") != PI_PERMISSION_POLICY_VERSION_V2:
        raise BridgeError("Pi permission policy version is not v2", "invalid_arguments")
    if not isinstance(raw.get("write_tools_enabled"), bool):
        raise BridgeError(
            "Pi permission policy 'write_tools_enabled' must be a boolean", "invalid_arguments")
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
    external = _check_external_access(raw.get("external_access"))
    return {
        "version": PI_PERMISSION_POLICY_VERSION_V2,
        "write_tools_enabled": bool(raw["write_tools_enabled"]),
        "tools": {name: tools[name] for name in SUPPORTED_PI_PERMISSION_TOOLS},
        "protected_patterns": protected,
        "protected_template_exceptions": exceptions,
        "allow_session_always": bool(raw["allow_session_always"]),
        "external_access": external,
    }


def migrate_v2_policy(v2: dict) -> dict:
    """Deterministically migrate a validated v2 policy to v3 in memory.

    Every v2 field is preserved exactly; ``shell_mode`` defaults to deny.
    3B2 external/root behavior migrates losslessly.
    """
    return {
        "version": PI_PERMISSION_POLICY_VERSION,
        "write_tools_enabled": bool(v2["write_tools_enabled"]),
        "tools": {name: v2["tools"][name] for name in SUPPORTED_PI_PERMISSION_TOOLS},
        "protected_patterns": list(v2["protected_patterns"]),
        "protected_template_exceptions": list(v2["protected_template_exceptions"]),
        "allow_session_always": bool(v2["allow_session_always"]),
        "external_access": {
            "default_mode": v2["external_access"]["default_mode"],
            "roots": [dict(r) for r in v2["external_access"]["roots"]],
        },
        "shell_mode": DEFAULT_SHELL_MODE,
    }


def migrate_v1_policy(v1: dict) -> dict:
    """Deterministically migrate a validated v1 policy to v3 in memory.

    ``write_tools_enabled`` inherits v1 ``enabled``; external access starts
    at the safe default (deny, no roots); ``shell_mode`` defaults to deny.
    Tool modes, protected patterns/exceptions, and session-always are
    preserved exactly.
    """
    return {
        "version": PI_PERMISSION_POLICY_VERSION,
        "write_tools_enabled": bool(v1["enabled"]),
        "tools": {name: v1["tools"][name] for name in SUPPORTED_PI_PERMISSION_TOOLS},
        "protected_patterns": list(v1["protected_patterns"]),
        "protected_template_exceptions": list(v1["protected_template_exceptions"]),
        "allow_session_always": bool(v1["allow_session_always"]),
        "external_access": {
            "default_mode": "deny",
            "roots": [],
        },
        "shell_mode": DEFAULT_SHELL_MODE,
    }


def canonical_json(policy: dict) -> str:
    return json.dumps(policy, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def policy_revision(policy: dict) -> str:
    """Stable revision: sha256 hex of the canonical policy JSON."""
    return hashlib.sha256(canonical_json(policy).encode("utf-8")).hexdigest()


def load_policy(service) -> tuple[dict, str, bool, int | None]:
    """Return (policy, revision, configured, migrated_from_version).

    A valid stored v2 policy migrates in memory to v3 (preserving every
    v2 field exactly, adding shell deny) and reports
    ``migrated_from_version=2``; a valid stored v1 policy migrates to v3
    and reports 1. Migration is never silently persisted here; the first
    v3 save persists v3 and changes the revision so older sessions cannot
    continue.
    """
    raw = service.setting(PI_PERMISSION_POLICY_SETTING)
    if not raw:
        policy = safe_defaults()
        return policy, policy_revision(policy), False, None
    try:
        decoded = json.loads(raw)
    except ValueError:
        policy = safe_defaults()
        return policy, policy_revision(policy), False, None
    # v3 path first.
    try:
        policy = validate_policy(decoded)
        return policy, policy_revision(policy), True, None
    except BridgeError:
        pass
    # Bounded v2 compatibility on read: migrate in memory only.
    try:
        v2 = _validate_v2(decoded)
    except (BridgeError, ValueError):
        pass
    else:
        policy = migrate_v2_policy(v2)
        return policy, policy_revision(policy), True, PI_PERMISSION_POLICY_VERSION_V2
    # Bounded v1 compatibility on read: migrate in memory only.
    try:
        v1 = _validate_v1(decoded)
    except (BridgeError, ValueError):
        policy = safe_defaults()
        return policy, policy_revision(policy), False, None
    policy = migrate_v1_policy(v1)
    return policy, policy_revision(policy), True, PI_PERMISSION_POLICY_VERSION_V1


def get_policy(service) -> tuple[dict, str, bool]:
    """Return (policy, revision, configured).

    Missing or corrupt stored values fail closed to safe defaults;
    ``configured`` reports whether a valid stored policy exists. Never raises
    for storage content; validation errors on save raise instead.
    """
    policy, revision, configured, _ = load_policy(service)
    return policy, revision, configured


def set_policy(service, raw: Any) -> tuple[dict, str]:
    """Validate strictly (v3 only), then persist atomically. Invalid input never applies."""
    policy = validate_policy(raw)
    service.set_setting(PI_PERMISSION_POLICY_SETTING, canonical_json(policy))
    service.event(None, "set_runtime_permission_policy")
    return policy, policy_revision(policy)


def status_summary(service) -> dict:
    """Bounded admin status summary: modes + counts, no patterns/paths."""
    policy, revision, configured, _ = load_policy(service)
    writable = bool(policy["write_tools_enabled"])
    external = policy.get("external_access") or {}
    return {
        "runtime": "pi",
        "policy_scope": "runtime_global",
        "configured": configured,
        "enabled": writable,
        "write_tools_enabled": writable,
        "effective_writable": writable,
        "tools": dict(policy["tools"]),
        "allow_session_always": bool(policy["allow_session_always"]),
        "protected_pattern_count": len(policy["protected_patterns"]),
        "protected_template_exception_count": len(policy["protected_template_exceptions"]),
        "external_default_mode": external.get("default_mode", "deny"),
        "external_root_count": len(external.get("roots") or []),
        "shell_mode": policy.get("shell_mode", DEFAULT_SHELL_MODE),
        "supported_shell": [PI_SHELL_TOOL],
        "policy_revision": revision,
        "supported_tools": list(SUPPORTED_PI_PERMISSION_TOOLS),
        "fixed_invariants": list(FIXED_INVARIANTS),
        "session_note": "Policy changes apply to NEW Pi sessions only; "
                        "active sessions keep their snapshot.",
        "shell_note": "Shell Allow runs with native macOS-user authority and "
                      "can bypass structured file path controls; Ask pauses "
                      "each bash invocation.",
    }


def full_view(service) -> dict:
    """Full admin GET view: effective policy plus metadata (local-admin only)."""
    policy, revision, configured, migrated_from = load_policy(service)
    view: dict[str, Any] = {
        "runtime": "pi",
        "policy_scope": "runtime_global",
        "configured": configured,
        "policy": policy,
        "policy_revision": revision,
        "supported_tools": list(SUPPORTED_PI_PERMISSION_TOOLS),
        "supported_shell": [PI_SHELL_TOOL],
        "fixed_invariants": list(FIXED_INVARIANTS),
        "session_note": "Policy changes apply to NEW Pi sessions only; "
                        "active sessions keep their snapshot.",
        "shell_note": "Shell Allow runs with native macOS-user authority and "
                      "can bypass structured file path controls; Ask pauses "
                      "each bash invocation.",
    }
    if migrated_from is not None:
        view["migrated_from_version"] = migrated_from
    return view
