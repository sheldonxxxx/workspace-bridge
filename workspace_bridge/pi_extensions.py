"""Web-managed Pi extension policy (milestone 3C2, extension policy v1).

The Bridge owns a runtime-global default extension set for isolated Bridge
Pi sessions: local-admin enable/disable per installed Pi npm package for NEW
sessions. No install/update/remove UI exists in v1.

Protocol summary:
- IDs are normalized npm package identities (``npm:package-id``, scoped
  names supported), not host paths and not versions. Deterministic order
  follows the isolated Pi inventory/settings order; there is no reorder UI.
- Default when unconfigured: ``enabled=[]`` (no third-party extension).
  Newly installed code is never silently auto-enabled.
- Canonical SHA-256 ``extension_revision`` over the canonical policy JSON.
  Saving is local-admin web only; there is no MCP mutation route.
- Save validates the full v1 enabled-ID list against the LIVE native
  inventory: duplicates, unknown IDs, not-installed packages and
  non-extension packages are rejected. When the adapter inventory is
  unavailable, saving fails rather than persisting an unvalidated
  broadened policy.
- Changes apply to NEW sessions only. Continuation requires the source
  run revision to equal the current revision (``extension_scope_changed``).

Trust semantics: enabled extension packages execute arbitrary native code
with the macOS user's authority and may access filesystem/network
independent of Bridge file/shell policy. Bridge file/shell policy is NOT
a sandbox for extension internals or extension tools.

Storage: the existing ``settings`` table under key
``runtime_extension_policy:pi``.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from .security import BridgeError

PI_EXTENSION_POLICY_SETTING = "runtime_extension_policy:pi"
PI_EXTENSION_POLICY_VERSION = 1

MAX_ENABLED_PACKAGES = 64
MAX_POLICY_BYTES = 32768
MAX_ID_CHARS = 214

_EXTENSION_ID_RE = re.compile(
    r"^npm:(@[a-z0-9][a-z0-9._~-]*/)?[a-z0-9][a-z0-9._~-]*$",
    re.IGNORECASE,
)

EXTENSION_WARNING = (
    "Extension packages execute arbitrary native code with your macOS user "
    "authority and may access filesystem/network independent of Bridge "
    "file/shell policy. Enable only packages you trust."
)
EXTENSION_SESSION_NOTE = (
    "Extension policy changes apply to NEW Pi sessions only; "
    "active sessions keep their snapshot."
)


def is_valid_extension_id(value: Any) -> bool:
    """Whether a value is a normalized npm extension identity (no version)."""
    return (isinstance(value, str) and len(value) <= 4 + MAX_ID_CHARS
            and _EXTENSION_ID_RE.fullmatch(value) is not None)


def safe_defaults() -> dict:
    """Safe deployment default: no third-party extension is enabled."""
    return {"version": PI_EXTENSION_POLICY_VERSION, "enabled": []}


def validate_policy(raw: Any) -> dict:
    """Strictly validate a full v1 policy object; never partially applies.

    Raises BridgeError on any violation. Returns the canonical policy dict
    preserving deterministic caller order.
    """
    if raw is None:
        raise BridgeError("Pi extension policy is required", "invalid_arguments")
    if isinstance(raw, str):
        if len(raw.encode("utf-8")) > MAX_POLICY_BYTES:
            raise BridgeError("Pi extension policy body is too large", "invalid_arguments")
        try:
            raw = json.loads(raw)
        except ValueError:
            raise BridgeError("Pi extension policy must be a JSON object",
                              "invalid_arguments") from None
    if not isinstance(raw, dict):
        raise BridgeError("Pi extension policy must be a JSON object", "invalid_arguments")
    if set(raw.keys()) != {"version", "enabled"}:
        raise BridgeError("Pi extension policy must have exactly 'version' and 'enabled'",
                          "invalid_arguments")
    if raw.get("version") != PI_EXTENSION_POLICY_VERSION:
        raise BridgeError(
            f"Pi extension policy version must be {PI_EXTENSION_POLICY_VERSION}",
            "invalid_arguments")
    enabled = raw.get("enabled")
    if not isinstance(enabled, list):
        raise BridgeError("Pi extension policy 'enabled' must be a list", "invalid_arguments")
    if len(enabled) > MAX_ENABLED_PACKAGES:
        raise BridgeError(
            f"Pi extension policy allows at most {MAX_ENABLED_PACKAGES} packages",
            "invalid_arguments")
    seen: set[str] = set()
    cleaned: list[str] = []
    for item in enabled:
        if not is_valid_extension_id(item):
            raise BridgeError(
                "Pi extension policy entries must be npm package identities "
                "(e.g. npm:package-id)", "invalid_arguments")
        if item in seen:
            raise BridgeError(
                f"Pi extension policy entry {item[:80]!r} is duplicated",
                "invalid_arguments")
        seen.add(item)
        cleaned.append(item)
    return {"version": PI_EXTENSION_POLICY_VERSION, "enabled": cleaned}


def canonical_json(policy: dict) -> str:
    return json.dumps(policy, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def policy_revision(policy: dict) -> str:
    """Stable revision: sha256 hex of the canonical policy JSON."""
    return hashlib.sha256(canonical_json(policy).encode("utf-8")).hexdigest()


def normalize_inventory_row(row: Any) -> dict | None:
    """Strictly normalize one adapter inventory row; None when unusable.

    Keeps only bounded display/identity fields. Host paths, agentDir,
    tokens, settings fields, file contents and dependency lists never
    cross this boundary (rows carrying them are dropped to the bounded
    subset below).
    """
    if not isinstance(row, dict):
        return None
    ident = row.get("id")
    if not is_valid_extension_id(ident):
        return None
    name = row.get("name") if isinstance(row.get("name"), str) else ""
    version = row.get("version") if isinstance(row.get("version"), str) else ""
    extensions = row.get("extensions") if isinstance(row.get("extensions"), list) else []
    extensions = [e[:400] for e in extensions if isinstance(e, str)][:32]
    fingerprint = row.get("package_json_sha256")
    if not isinstance(fingerprint, str) or len(fingerprint) != 64:
        fingerprint = ""
    try:
        count = int(row.get("extension_count", len(extensions)))
    except (TypeError, ValueError):
        count = len(extensions)
    return {
        "id": ident,
        "name": name[:214],
        "version": version[:80],
        "has_extensions": bool(row.get("has_extensions")),
        "extensions": extensions,
        "extension_count": max(0, min(count, 32)),
        "package_json_sha256": fingerprint,
        "supported": bool(row.get("supported")),
        "reason": str(row.get("reason") or "")[:200],
    }


def validate_against_inventory(policy: dict, inventory: Any) -> dict:
    """Reject duplicate/unknown/not-installed/non-extension IDs.

    ``inventory`` is the normalized adapter row list. Raises BridgeError
    when the inventory is unavailable (None) or any enabled ID is not a
    supported, extension-bearing installed package. Never silently skips
    an enabled package.
    """
    if inventory is None:
        raise BridgeError(
            "Pi extension inventory is unavailable; refusing to save an "
            "unvalidated policy", "inventory_unavailable")
    if not isinstance(inventory, list):
        raise BridgeError("Pi extension inventory was invalid", "inventory_unavailable")
    by_id = {}
    for row in inventory:
        normalized = normalize_inventory_row(row)
        if normalized is not None:
            by_id[normalized["id"]] = normalized
    for ident in policy.get("enabled", []):
        row = by_id.get(ident)
        if row is None:
            raise BridgeError(
                f"Pi extension {ident[:80]!r} is unknown: not installed in "
                "the isolated Pi profile", "unknown_extension")
        if not row["supported"] or not row["has_extensions"]:
            reason = row["reason"] or "no_extension_resources"
            raise BridgeError(
                f"Pi extension {ident[:80]!r} cannot be enabled ({reason})",
                "unsupported_extension")
    return policy


def canonicalize_enabled_order(inventory: Any, enabled: list[str]) -> list[str]:
    """Reorder enabled IDs into live inventory/settings order.

    The revision represents the active set/load order, so semantically
    identical sets share one stored policy and extension_revision
    regardless of checkbox/API input ordering. IDs missing from the
    inventory keep their relative order at the end (validation rejects
    them elsewhere).
    """
    order: dict[str, int] = {}
    if isinstance(inventory, list):
        for row in inventory:
            normalized = normalize_inventory_row(row)
            if normalized is not None and normalized["id"] not in order:
                order[normalized["id"]] = len(order)
    return sorted(enabled, key=lambda ident: order.get(ident, 2 ** 60))


def load_policy(service) -> tuple[dict, str, bool]:
    """Return (policy, revision, configured).

    Missing or corrupt stored values fail closed to the default-empty
    policy; ``configured`` reports whether a valid stored policy exists.
    Never raises for storage content; validation errors on save raise.
    """
    raw = service.setting(PI_EXTENSION_POLICY_SETTING)
    if not raw:
        policy = safe_defaults()
        return policy, policy_revision(policy), False
    try:
        policy = validate_policy(json.loads(raw))
        return policy, policy_revision(policy), True
    except (BridgeError, ValueError):
        policy = safe_defaults()
        return policy, policy_revision(policy), False


def get_policy(service) -> tuple[dict, str, bool]:
    """Return (policy, revision, configured)."""
    return load_policy(service)


def set_policy(service, raw: Any, inventory: Any) -> tuple[dict, str]:
    """Validate strictly (v1 only + live inventory), then persist atomically.

    Invalid input or an unavailable inventory never applies. ``inventory``
    must be the normalized adapter row list (None fails closed). Enabled
    IDs are canonicalized into live inventory/settings order before
    persistence so the revision represents the active set, not input
    ordering.
    """
    policy = validate_policy(raw)
    validate_against_inventory(policy, inventory)
    policy = {"version": PI_EXTENSION_POLICY_VERSION,
              "enabled": canonicalize_enabled_order(inventory, policy["enabled"])}
    service.set_setting(PI_EXTENSION_POLICY_SETTING, canonical_json(policy))
    service.event(None, "set_runtime_extension_policy")
    return policy, policy_revision(policy)


def status_summary(service, inventory: Any = None,
                   inventory_available: bool = False) -> dict:
    """Bounded admin status summary: counts + revision prefix only.

    Never carries package names, IDs, versions, or host paths.
    """
    policy, revision, configured = load_policy(service)
    installed: int | None = None
    if isinstance(inventory, list):
        installed = len([r for r in inventory
                         if normalize_inventory_row(r) is not None])
    return {
        "runtime": "pi",
        "policy_scope": "runtime_global",
        "configured": configured,
        "installed_count": installed,
        "enabled_count": len(policy["enabled"]),
        "extension_revision_prefix": revision[:12],
        "ready": bool(inventory_available),
        "session_note": EXTENSION_SESSION_NOTE,
    }


def full_view(service, inventory: Any = None,
              inventory_available: bool = False) -> dict:
    """Full admin GET view: effective policy plus bounded live inventory."""
    policy, revision, configured = load_policy(service)
    rows = []
    if isinstance(inventory, list):
        for row in inventory:
            normalized = normalize_inventory_row(row)
            if normalized is not None:
                rows.append(normalized)
    return {
        "runtime": "pi",
        "policy_scope": "runtime_global",
        "configured": configured,
        "policy": policy,
        "extension_revision": revision,
        "inventory": rows,
        "inventory_available": bool(inventory_available),
        "session_note": EXTENSION_SESSION_NOTE + " Active sessions keep their snapshot.",
        "warning": EXTENSION_WARNING,
    }
