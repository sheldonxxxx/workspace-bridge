"""Read-only release/version compatibility contract (informational only).

Bridge-first upgrades are an explicit supported state: older compatible
Nodes/adapters remain fully usable for the functionality they advertise.
Release skew is informational metadata, not an execution gate and not an
update plan: component updates are manual local operations on each host.

The running Bridge is the active target after a Bridge-first upgrade:

- ``target_product_version`` is the running Bridge product version.
- Target Python-core build for Node and Codex is the Bridge build ID.
- Manager target is the Bridge-served Manager identity.
- Pi target product version is the Bridge product version; the exact Pi
  build is unknown, so Pi uses ``product-version-only`` precision rather
  than a fabricated build.

Component states separate execution compatibility from release/update
state: ``current``, ``update_available``, ``unsupported_build``,
``target_mismatch``, ``incompatible``, ``unavailable``. Every entry
carries ``execution_compatible``, current/target product versions,
current/target build IDs where known, target precision, runtime
type/instance ID where relevant, and a bounded reason code.

This module performs no mutation: no catalog refresh, no DB writes, no
backup, no artifact download, no lock access. Live observations
are bounded ``status``/``descriptor`` reads only. Failures are
per-component; topology enumeration failure fails the whole response
safely without leaking raw errors, paths, tokens, or URLs.
"""
from __future__ import annotations

import re
from typing import Any

STAGED_SCHEMA_VERSION = 1

STATES = frozenset({
    "current",
    "update_available",
    "unsupported_build",
    "target_mismatch",
    "incompatible",
    "unavailable",
})

REASONS = frozenset({
    "exact-match",
    "product-older",
    "build-skew",
    "missing-release",
    "release-invalid",
    "release-unsupported",
    "product-newer",
    "non-comparable",
    "protocol-mismatch",
    "core-feature-mismatch",
    "node-protocol-mismatch",
    "node-unavailable",
    "node-disabled",
    "adapter-unavailable",
    "adapter-disabled",
    "unobserved",
    "target-unobserved",
    "bridge-unavailable",
    "manager-unavailable",
})

_NUMERIC_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


def _parse_numeric(version: Any) -> tuple[int, int, int] | None:
    if not isinstance(version, str):
        return None
    match = _NUMERIC_RE.fullmatch(version)
    if not match:
        return None
    try:
        return (int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


def compare_product_versions(current: Any, target: Any) -> str:
    """Compare two product versions for staged-rollout classification.

    Returns ``equal`` when strings match, ``older``/``newer`` for strict
    numeric ``X.Y.Z`` ordering, otherwise ``non-comparable``. Never raises
    and never inspects build IDs.
    """
    if not isinstance(current, str) or not isinstance(target, str):
        return "non-comparable"
    if current == target:
        return "equal"
    left = _parse_numeric(current)
    right = _parse_numeric(target)
    if left is None or right is None:
        return "non-comparable"
    if left < right:
        return "older"
    if left > right:
        return "newer"
    return "equal"


def _safe_release(value: Any) -> dict | None:
    if not isinstance(value, dict):
        return None
    # Only copy the bounded release fields; never forward extra keys.
    keys = ("contract", "product", "product_version",
            "component", "component_version", "build_id")
    if any(key not in value for key in keys):
        return None
    return {key: value[key] for key in keys}


def classify_component(
    *,
    component: str,
    instance: str,
    current: dict | None,
    observation: str,
    target_product_version: str | None,
    target_build_id: str | None,
    target_precision: str,
    reachable: bool,
    protocol_compatible: bool,
    protocol_reason: str | None = None,
    runtime_type: str | None = None,
    instance_id: str | None = None,
) -> dict:
    """Pure deterministic classifier for one staged-rollout component.

    ``current`` is a sanitized release identity or ``None``. ``observation``
    is one of ``valid``/``missing``/``invalid``/``unsupported``/
    ``unobserved``/``disabled``. ``reachable`` is False when the instance
    could not be observed (disabled/unreachable). Missing/invalid/unsupported
    Workspace Bridge release identity on a first-party component means the
    build is not a supported release build (0.1.0 is the first supported
    baseline). ``protocol_compatible`` is False only for true
    protocol/core-feature incompatibility proven by a descriptor validation
    failure.
    ``target_precision`` is ``exact`` or ``product-version-only`` (Pi).
    Never raises and never includes raw errors, paths, or secrets.
    """
    if target_precision not in ("exact", "product-version-only"):
        target_precision = "exact"
    if observation not in ("valid", "missing", "invalid",
                           "unsupported", "unobserved", "disabled"):
        observation = "unobserved"
    if protocol_reason not in (None, "protocol-mismatch",
                               "core-feature-mismatch",
                               "node-protocol-mismatch"):
        protocol_reason = "protocol-mismatch"

    base: dict[str, Any] = {
        "component": component,
        "instance": instance,
        "target_precision": target_precision,
        "target_product_version": target_product_version,
        "target_build_id": target_build_id,
    }
    if runtime_type is not None:
        base["runtime_type"] = runtime_type
    if instance_id is not None:
        base["instance_id"] = instance_id[:200]

    # Current version/build for display where known.
    current_product: str | None = None
    current_build: str | None = None
    if isinstance(current, dict):
        raw_product = current.get("product_version")
        raw_build = current.get("build_id")
        current_product = raw_product if isinstance(raw_product, str) else None
        current_build = raw_build if isinstance(raw_build, str) else None
    base["current_product_version"] = current_product
    base["current_build_id"] = current_build
    base["current"] = _safe_release(current)

    def _final(*, state: str, execution: bool, reason: str) -> dict:
        assert state in STATES, state
        assert reason in REASONS, reason
        return {**base, "state": state,
                "execution_compatible": execution, "reason": reason}

    # 1. Reachability comes first: disabled/unreachable instances are
    # unavailable and never silently treated as compatible skew.
    if not reachable:
        if observation == "disabled":
            reason = ("adapter-disabled" if component in {
                "pi-host-adapter", "codex-host-adapter", "adapter"}
                else "node-disabled" if component == "node"
                else "unobserved")
            if component in ("bridge", "manager"):
                reason = ("bridge-unavailable" if component == "bridge"
                          else "manager-unavailable")
            return _final(state="unavailable", execution=False,
                          reason=reason if reason in REASONS else "unobserved")
        # Disabled registries aside, unreachable/unobserved topology is
        # per-component unavailable. Node vs adapter wording stays bounded.
        if component == "node":
            return _final(state="unavailable", execution=False,
                          reason="node-unavailable")
        if component in ("pi-host-adapter", "codex-host-adapter", "adapter"):
            return _final(state="unavailable", execution=False,
                          reason="adapter-unavailable")
        if component == "bridge":
            return _final(state="unavailable", execution=False,
                          reason="bridge-unavailable")
        if component == "manager":
            return _final(state="unavailable", execution=False,
                          reason="manager-unavailable")
        return _final(state="unavailable", execution=False,
                      reason="unobserved")

    # 2. True protocol incompatibility blocks the affected route/feature.
    if not protocol_compatible:
        reason = protocol_reason or "protocol-mismatch"
        if reason not in REASONS:
            reason = "protocol-mismatch"
        return _final(state="incompatible", execution=False, reason=reason)

    # 3. Target unknown: cannot classify skew. This is per-component
    # unavailable (fail-closed informationally) but never a fake current.
    # Pi with product-version-only precision still needs a product target.
    if not isinstance(target_product_version, str) or not target_product_version:
        return _final(state="unavailable", execution=False,
                      reason="target-unobserved")

    # 4. Bridge/Manager missing identity is unavailable (local file/state),
    # not a supported release build: there is no protocol to remain compatible with.
    if component in ("bridge", "manager"):
        if observation != "valid" or not isinstance(current, dict):
            reason = ("bridge-unavailable" if component == "bridge"
                      else "manager-unavailable")
            return _final(state="unavailable", execution=False,
                          reason=reason)
        # Manager target is the served identity itself, so a valid served
        # identity is always current. Bridge target is itself likewise.
        # Product/build comparison still runs for completeness; a valid
        # served Bridge/Manager that somehow differs from target is an
        # update rather than a silent match.
        if current_product == target_product_version and (
                target_precision == "product-version-only"
                or current_build == target_build_id):
            return _final(state="current", execution=True,
                          reason="exact-match")
        # Any Bridge/Manager skew from its own target is an update, never
        # a downgrade offer. Numeric ordering decides update vs mismatch.
        relation = compare_product_versions(current_product,
                                            target_product_version)
        if relation == "older":
            return _final(state="update_available", execution=True,
                          reason="product-older")
        if relation == "equal":
            return _final(state="update_available", execution=True,
                          reason="build-skew")
        return _final(state="target_mismatch", execution=True,
                      reason="product-newer" if relation == "newer"
                      else "non-comparable")

    # 5. Supported first-party release baseline: 0.1.0 is the first
    # supported release. A successfully proven Node/Runtime Protocol peer
    # with missing/invalid/unsupported Workspace Bridge release identity
    # is not a supported release build, but ordinary protocol-supported
    # execution is not blocked by that fact alone. There is no automatic
    # updater; component updates are manual local host operations.
    if observation == "missing":
        return _final(state="unsupported_build", execution=True,
                      reason="missing-release")
    if observation == "invalid":
        return _final(state="unsupported_build", execution=True,
                      reason="release-invalid")
    if observation == "unsupported":
        return _final(state="unsupported_build", execution=True,
                      reason="release-unsupported")
    if observation in ("unobserved", "disabled") or not isinstance(current, dict):
        # Reachable + protocol-ok but no valid identity is unsupported only when
        # explicitly missing/invalid/unsupported above; otherwise fail
        # closed as unavailable without inventing compatibility.
        if component == "node":
            return _final(state="unavailable", execution=False,
                          reason="node-unavailable")
        return _final(state="unavailable", execution=False,
                      reason="adapter-unavailable")

    # 6. Valid release: compare product/build. Pi with product-version-only
    # precision is current on product match regardless of build.
    if current_product != target_product_version:
        relation = compare_product_versions(current_product,
                                            target_product_version)
        if relation == "older":
            return _final(state="update_available", execution=True,
                          reason="product-older")
        if relation == "newer":
            return _final(state="target_mismatch", execution=True,
                          reason="product-newer")
        return _final(state="target_mismatch", execution=True,
                      reason="non-comparable")
    # Same product version.
    if target_precision == "product-version-only":
        return _final(state="current", execution=True,
                      reason="exact-match")
    if current_build != target_build_id:
        return _final(state="update_available", execution=True,
                      reason="build-skew")
    return _final(state="current", execution=True, reason="exact-match")


def _sorted(entries: list[dict]) -> list[dict]:
    return sorted(entries,
                  key=lambda e: (e.get("component", ""),
                                 e.get("instance", "")))


def build_staged_status(
    *,
    bridge_current: dict | None,
    manager_current: dict | None,
    node_observations: dict[str, dict],
    adapter_observations: dict[str, dict],
) -> dict:
    """Build deterministic staged-rollout status from injected observations.

    ``node_observations`` maps node IDs to ``{current, observation,
    reachable, protocol_compatible, protocol_reason}``. ``adapter_observations``
    maps adapter IDs to the same plus ``runtime_type``/``instance_id``.
    ``bridge_current``/``manager_current`` are sanitized identities or None.
    The Bridge product/build is the active target; Pi uses
    product-version-only precision. No I/O occurs here.
    """
    if isinstance(bridge_current, dict):
        target_product = bridge_current.get("product_version")
        target_build = bridge_current.get("build_id")
        if not isinstance(target_product, str):
            target_product = None
        if not isinstance(target_build, str):
            target_build = None
    else:
        target_product = None
        target_build = None

    # Bridge itself: reachable when its local identity is valid.
    if isinstance(bridge_current, dict) and isinstance(target_product, str):
        bridge_entry = classify_component(
            component="bridge", instance="bridge",
            current=bridge_current, observation="valid",
            target_product_version=target_product,
            target_build_id=target_build, target_precision="exact",
            reachable=True, protocol_compatible=True)
    else:
        bridge_entry = classify_component(
            component="bridge", instance="bridge",
            current=None, observation="unobserved",
            target_product_version=target_product,
            target_build_id=target_build, target_precision="exact",
            reachable=False, protocol_compatible=True)

    # Manager target is the served identity itself.
    if isinstance(manager_current, dict):
        manager_entry = classify_component(
            component="manager", instance="manager",
            current=manager_current, observation="valid",
            target_product_version=manager_current.get("product_version"),
            target_build_id=manager_current.get("build_id"),
            target_precision="exact",
            reachable=True, protocol_compatible=True)
    else:
        manager_entry = classify_component(
            component="manager", instance="manager",
            current=None, observation="unobserved",
            target_product_version=None, target_build_id=None,
            target_precision="exact",
            reachable=False, protocol_compatible=True)

    nodes: list[dict] = []
    for node_id in sorted(node_observations):
        obs = node_observations[node_id] or {}
        nodes.append(classify_component(
            component="node", instance=node_id,
            current=obs.get("current"),
            observation=obs.get("observation", "unobserved"),
            target_product_version=target_product,
            target_build_id=target_build,
            target_precision="exact",
            reachable=bool(obs.get("reachable", False)),
            protocol_compatible=bool(obs.get("protocol_compatible", False)),
            protocol_reason=obs.get("protocol_reason")))

    adapters: list[dict] = []
    for adapter_id in sorted(adapter_observations):
        obs = adapter_observations[adapter_id] or {}
        runtime_type = obs.get("runtime_type")
        # Unknown runtime types never coerce: they are unavailable with a
        # bounded reason, never a fabricated Pi/Codex target.
        if runtime_type not in ("pi", "codex"):
            adapters.append(classify_component(
                component="adapter", instance=adapter_id,
                current=obs.get("current"),
                observation="unobserved",
                target_product_version=target_product,
                target_build_id=target_build,
                target_precision="exact",
                reachable=False, protocol_compatible=True,
                runtime_type=runtime_type if isinstance(runtime_type, str)
                else None,
                instance_id=obs.get("instance_id")))
            continue
        precision = ("product-version-only" if runtime_type == "pi"
                     else "exact")
        target_build_for = (None if runtime_type == "pi" else target_build)
        desired_component = ("pi-host-adapter" if runtime_type == "pi"
                             else "codex-host-adapter")
        adapters.append(classify_component(
            component=desired_component, instance=adapter_id,
            current=obs.get("current"),
            observation=obs.get("observation", "unobserved"),
            target_product_version=target_product,
            target_build_id=target_build_for,
            target_precision=precision,
            reachable=bool(obs.get("reachable", False)),
            protocol_compatible=bool(obs.get("protocol_compatible", False)),
            protocol_reason=obs.get("protocol_reason"),
            runtime_type=runtime_type,
            instance_id=obs.get("instance_id")))

    return {"schema_version": STAGED_SCHEMA_VERSION,
            "target": {"product_version": target_product,
                       "build_id": target_build,
                       "manager": _safe_release(manager_current),
                       "note": ("Bridge is the active target after a "
                                "Bridge-first upgrade; Pi build is "
                                "product-version-only.")},
            "bridge": bridge_entry,
            "manager": manager_entry,
            "nodes": _sorted(nodes),
            "adapters": _sorted(adapters)}


def _observe_node(service: Any, node_id: str) -> dict:
    """Bounded live Node observation without mutation or catalog refresh."""
    try:
        row = service.node_registry.get(node_id)
    except Exception:
        return {"current": None, "observation": "unobserved",
                "reachable": False, "protocol_compatible": False,
                "protocol_reason": "protocol-mismatch"}
    if not row.get("enabled"):
        return {"current": None, "observation": "disabled",
                "reachable": False, "protocol_compatible": True}
    try:
        client = service.node_registry.client(node_id, timeout=3)
        status = client.status()
    except Exception as exc:
        # Central validator raises node_protocol_error for protocol
        # mismatch; every other BridgeError is unreachability.
        code = getattr(exc, "code", "")
        if code == "node_protocol_error":
            return {"current": None, "observation": "unobserved",
                    "reachable": True, "protocol_compatible": False,
                    "protocol_reason": "node-protocol-mismatch"}
        return {"current": None, "observation": "unobserved",
                "reachable": False, "protocol_compatible": False,
                "protocol_reason": "protocol-mismatch"}
    # Reachable + protocol-ok. Release skew never affects reachability.
    raw = status.get("release") if isinstance(status, dict) else None
    if raw is None:
        return {"current": None, "observation": "missing",
                "reachable": True, "protocol_compatible": True}
    try:
        from .release import ReleaseError, validate_release
        current = validate_release(raw)
        return {"current": current, "observation": "valid",
                "reachable": True, "protocol_compatible": True}
    except Exception as exc:
        kind = getattr(exc, "kind", "invalid")
        return {"current": None,
                "observation": ("unsupported" if kind == "unsupported"
                                else "invalid"),
                "reachable": True, "protocol_compatible": True}


def _observe_adapter(service: Any, adapter_id: str) -> dict:
    """Bounded live adapter observation without mutation or catalog refresh."""
    try:
        row = service.adapter_registry.get(adapter_id)
    except Exception:
        return {"current": None, "observation": "unobserved",
                "reachable": False, "protocol_compatible": False,
                "protocol_reason": "protocol-mismatch",
                "runtime_type": None, "instance_id": None}
    runtime_type = row.get("runtime_type")
    if not row.get("enabled") or not row.get("node_enabled"):
        return {"current": None, "observation": "disabled",
                "reachable": False, "protocol_compatible": True,
                "runtime_type": runtime_type, "instance_id": None}
    try:
        client = service.adapter_registry.client(adapter_id, timeout=3)
        descriptor = client.descriptor()
    except Exception as exc:
        name = type(exc).__name__
        code = getattr(exc, "code", "")
        # Classify only from structured Runtime Protocol semantics. True
        # major/core incompatibility is incompatible; transport failure and
        # unavailable descriptors are unavailable. No exception-message text
        # is used to infer release compatibility.
        if name == "RuntimeUnsupported" or code == "runtime_unsupported":
            message_lower = str(exc).lower()
            if "core feature" in message_lower or "required v1 feature" in message_lower:
                reason = "core-feature-mismatch"
            else:
                reason = "protocol-mismatch"
            return {"current": None, "observation": "unobserved",
                    "reachable": True, "protocol_compatible": False,
                    "protocol_reason": reason,
                    "runtime_type": runtime_type, "instance_id": None}
        return {"current": None, "observation": "unobserved",
                "reachable": False, "protocol_compatible": False,
                "protocol_reason": "protocol-mismatch",
                "runtime_type": runtime_type, "instance_id": None}
    # Usable descriptor: release_status carries degraded update metadata.
    release = getattr(descriptor, "release", None)
    status = getattr(descriptor, "release_status", None)
    if status not in ("valid", "missing", "invalid", "unsupported"):
        status = "valid" if isinstance(release, dict) else "missing"
    instance_id = getattr(descriptor, "instance_id", None)
    if release is None and status == "valid":
        status = "missing"
    current = release if isinstance(release, dict) else None
    # Re-validate the observed release through the canonical validator so
    # a forged in-process descriptor cannot smuggle extra fields.
    if isinstance(current, dict):
        try:
            from .release import validate_release
            current = validate_release(current)
            status = "valid"
        except Exception as exc:
            kind = getattr(exc, "kind", "invalid")
            current = None
            status = "unsupported" if kind == "unsupported" else "invalid"
    return {"current": current, "observation": status,
            "reachable": True, "protocol_compatible": True,
            "runtime_type": runtime_type,
            "instance_id": instance_id if isinstance(instance_id, str)
            else None}


def rollout_live(service: Any) -> dict:
    """Build read-only version/compatibility status from bounded live observations.

    Opens no writers, refreshes no caches/catalogs, mutates no routes,
    creates no backups, downloads no artifacts, and never touches locks. Node observations are bounded ``status`` reads;
    adapter observations are bounded ``descriptor`` reads. Failures are
    per-component; topology enumeration failure fails the whole response
    safely without raw exception text.
    """
    from .release import bridge_release, read_manager_release

    try:
        bridge_current = bridge_release()
    except Exception:
        bridge_current = None
    try:
        manager_current = read_manager_release()
    except Exception:
        manager_current = None

    try:
        node_rows = service.node_registry.rows()
        if not isinstance(node_rows, list):
            raise TypeError("Node topology is unavailable")
    except Exception:
        return {"schema_version": STAGED_SCHEMA_VERSION,
                "status": "failed",
                "target": {"product_version": None, "build_id": None,
                           "manager": None,
                           "note": ("Bridge is the active target after a "
                                    "Bridge-first upgrade; Pi build is "
                                    "product-version-only.")},
                "bridge": None, "manager": None, "nodes": [],
                "adapters": [],
                "error": {"code": "topology-unavailable",
                          "summary": "Node topology is unavailable."}}
    try:
        adapter_rows = service.adapter_registry.rows()
        if not isinstance(adapter_rows, list):
            raise TypeError("Adapter topology is unavailable")
    except Exception:
        return {"schema_version": STAGED_SCHEMA_VERSION,
                "status": "failed",
                "target": {"product_version": None, "build_id": None,
                           "manager": None,
                           "note": ("Bridge is the active target after a "
                                    "Bridge-first upgrade; Pi build is "
                                    "product-version-only.")},
                "bridge": None, "manager": None, "nodes": [],
                "adapters": [],
                "error": {"code": "topology-unavailable",
                          "summary": "Adapter topology is unavailable."}}

    node_ids = sorted(row["id"] for row in node_rows
                      if isinstance(row, dict) and isinstance(row.get("id"), str))
    adapter_ids = sorted(row["id"] for row in adapter_rows
                         if isinstance(row, dict) and isinstance(row.get("id"), str))

    node_observations: dict[str, dict] = {}
    for node_id in node_ids:
        try:
            node_observations[node_id] = _observe_node(service, node_id)
        except Exception:
            node_observations[node_id] = {
                "current": None, "observation": "unobserved",
                "reachable": False, "protocol_compatible": False,
                "protocol_reason": "protocol-mismatch"}

    adapter_observations: dict[str, dict] = {}
    for adapter_id in adapter_ids:
        try:
            adapter_observations[adapter_id] = _observe_adapter(service,
                                                                adapter_id)
        except Exception:
            adapter_observations[adapter_id] = {
                "current": None, "observation": "unobserved",
                "reachable": False, "protocol_compatible": False,
                "protocol_reason": "protocol-mismatch",
                "runtime_type": None, "instance_id": None}

    status = build_staged_status(
        bridge_current=bridge_current if isinstance(bridge_current, dict)
        else None,
        manager_current=manager_current if isinstance(manager_current, dict)
        else None,
        node_observations=node_observations,
        adapter_observations=adapter_observations)
    return {"schema_version": status["schema_version"],
            "status": "ok",
            "target": status["target"],
            "bridge": status["bridge"],
            "manager": status["manager"],
            "nodes": status["nodes"],
            "adapters": status["adapters"]}
