"""Canonical diagnostics keyed by exact workspace and AdapterInstance routes."""
from __future__ import annotations

import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .runtime import RuntimeUnsupported
from .security import BridgeError, redact
from .wbrp import CORE_FEATURES, Descriptor, HttpRuntimeAdapter

STATUSES = frozenset({"pass", "warning", "unknown", "action_required", "failed"})
SECTIONS = frozenset({"core", "nodes", "workspaces", "adapters", "runnable_routes",
                      "models_profiles", "git_evidence", "release"})
STATUS_ORDER = ("pass", "warning", "unknown", "action_required", "failed")
MAX_WORKSPACES = 100
MAX_ADAPTERS = 100
MAX_ROUTES = 400
MAX_PROBES = 120
_SEVERITY = {"pass": 0, "warning": 1, "unknown": 2,
             "action_required": 3, "failed": 4}


def _stamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe(value: object, limit: int, fallback: str = "") -> str:
    if not isinstance(value, str):
        return fallback
    text, _ = redact(value[:limit * 2])
    text = "".join(char for char in text if char == " " or ord(char) >= 32).strip()
    if (not text or "[REDACTED_SECRET]" in text or "http://" in text.lower()
            or "https://" in text.lower()
            or re.search(r"(?:^|\s)(?:/|~[/\\]|[a-z]:[/\\])", text)):
        return fallback
    return text[:limit]


def _status(statuses: list[str]) -> str:
    return max(statuses, key=lambda value: _SEVERITY[value], default="pass")


@dataclass(frozen=True)
class DiagnosticCheck:
    id: str
    code: str
    section: str
    status: str
    summary: str
    detail: str | None = None
    remediation: str | None = None
    workspace_id: str | None = None
    adapter_id: str | None = None
    runtime_type: str | None = None

    def to_dict(self) -> dict[str, Any]:
        value = {"id": self.id, "code": self.code, "section": self.section,
                 "status": self.status, "summary": self.summary[:160]}
        for key in ("detail", "remediation", "workspace_id", "adapter_id", "runtime_type"):
            item = getattr(self, key)
            if item is not None:
                value[key] = item[:240] if key in {"detail", "remediation"} else item
        return value


@dataclass(frozen=True)
class RunnableRoute:
    id: str
    workspace_id: str
    workspace_name: str
    adapter_id: str
    adapter_name: str
    node_id: str
    node_name: str
    runtime_type: str
    ready: bool
    status: str
    summary: str
    blockers: list[str] = field(default_factory=list)
    is_default: bool = False
    security_source: str | None = None
    profile_id: str | None = None
    profile_revision: str | None = None
    default_model_selector: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "workspace_id": self.workspace_id,
                "workspace_name": self.workspace_name,
                "adapter_id": self.adapter_id, "adapter_name": self.adapter_name,
                "node_id": self.node_id, "node_name": self.node_name,
                "runtime_type": self.runtime_type, "ready": self.ready,
                "status": self.status, "summary": self.summary[:160],
                "blockers": list(self.blockers),
                "is_default": self.is_default,
                "security_source": self.security_source,
                "profile": ({"id": self.profile_id, "revision": self.profile_revision}
                            if self.profile_id is not None else None),
                "default_model_selector": self.default_model_selector}


@dataclass(frozen=True)
class DiagnosticReport:
    generated_at: str
    mode: str
    overall_status: str
    overall_summary: str
    counts: dict[str, int]
    checks: list[DiagnosticCheck]
    runnable_routes: list[RunnableRoute]
    release: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"generated_at": self.generated_at, "mode": self.mode,
                "overall": {"status": self.overall_status,
                            "summary": self.overall_summary,
                            "counts": dict(self.counts)},
                "checks": [item.to_dict() for item in self.checks],
                "runnable_routes": [item.to_dict() for item in self.runnable_routes],
                "release": dict(self.release)}


def failure_report(*, mode: str = "offline", code: str = "core.config_state_readable",
                   summary: str = "Configuration or local state could not be read.",
                   section: str = "core") -> dict:
    check = DiagnosticCheck("doctor.initialization", code, section, "failed", summary,
                            remediation="Check the local configuration and private state files.")
    try:
        from .release import bridge_release
        bridge = bridge_release()
    except Exception:
        bridge = None
    release_data: dict[str, Any] = {"bridge": bridge, "manager": None,
                                     "nodes": {}, "adapters": {}}
    return DiagnosticReport(_stamp(), mode, "failed", summary,
                            {status: int(status == "failed") for status in STATUS_ORDER},
                            [check], [], release_data).to_dict()


class _Builder:
    def __init__(self):
        self.checks: list[DiagnosticCheck] = []
        self.routes: list[RunnableRoute] = []
        self.release_data: dict[str, Any] = {"bridge": None, "manager": None,
                                              "nodes": {}, "adapters": {}}

    def add(self, code: str, section: str, status: str, summary: str, *,
            detail: str | None = None, remediation: str | None = None,
            workspace_id: str | None = None, adapter_id: str | None = None,
            runtime_type: str | None = None) -> None:
        scope = ":".join(x for x in (workspace_id, adapter_id) if x)
        self.checks.append(DiagnosticCheck(
            f"{code}:{scope}" if scope else code, code, section, status, summary,
            detail, remediation, workspace_id, adapter_id, runtime_type))

    def report(self, mode: str) -> dict:
        counts = {key: sum(row.status == key for row in self.checks) for key in STATUS_ORDER}
        overall = _status([row.status for row in self.checks])
        summary = {"pass": "All observed local and adapter checks passed.",
                   "warning": "Diagnostics completed with warnings.",
                   "unknown": "Some adapter freshness was not observed.",
                   "action_required": "Configuration or runnable-route prerequisites need administrator action.",
                   "failed": "One or more required diagnostic checks failed."}[overall]
        return DiagnosticReport(_stamp(), mode, overall, summary, counts,
                                self.checks[:5000], self.routes[:MAX_ROUTES],
                                dict(self.release_data)).to_dict()


def _adapter_probe(service, row: dict) -> tuple[Descriptor | None, dict | None]:
    try:
        client = service.adapter_registry.client(row["id"], timeout=3)
        descriptor = client.descriptor()
        return descriptor, None
    except RuntimeUnsupported as exc:
        return None, {"kind": "protocol", "message": str(exc)}
    except Exception:  # remote details never enter diagnostic JSON
        return None, {"kind": "unavailable"}


def _profile_catalog(service, ws: dict, adapter_id: str) -> dict | None:
    try:
        return service.run_coordinator.profile_catalog(adapter_id, ws, fresh=True)
    except Exception:
        return None


def _models(service, ws: dict, adapter_id: str) -> list[dict] | None:
    try:
        rows = service.adapter_registry.client(adapter_id, timeout=3, workspace=ws).models(ws["id"])
        return rows if isinstance(rows, list) else None
    except Exception:
        return None


def evaluate(service, *, offline: bool = False, listener: dict | None = None,
             runtime_configuration_error: bool = False) -> dict[str, Any]:
    """Observe local prerequisites and each explicit (workspace, adapter) route."""
    del runtime_configuration_error  # No environment adapter registry exists in v2.
    mode = "offline" if offline else "live"
    out = _Builder()
    listener = listener or {}
    with service.lock:
        gateway = service.db.execute("SELECT token_hash,enabled FROM gateway WHERE id=1").fetchone()
        workspaces = [dict(row) for row in service.db.execute(
            "SELECT * FROM workspaces ORDER BY name,id LIMIT ?", (MAX_WORKSPACES + 1,))]
        adapters = service.adapter_registry.rows()[:MAX_ADAPTERS + 1]
        routes = [dict(row) for row in service.db.execute(
            "SELECT r.*,w.name AS workspace_name,w.root,w.node_id,w.enabled AS workspace_enabled,"
            "w.write_scope,w.agent_enabled,a.name AS adapter_name,a.runtime_type,"
            "a.enabled AS adapter_enabled,a.revision AS adapter_revision,a.node_id AS adapter_node_id,"
            "n.name AS node_name,n.enabled AS node_enabled,n.revision AS node_revision "
            "FROM workspace_routes r JOIN workspaces w ON w.id=r.workspace "
            "JOIN node_adapters a ON a.adapter_id=r.adapter_id JOIN nodes n ON n.id=w.node_id "
            "ORDER BY w.name,a.name COLLATE NOCASE,a.adapter_id LIMIT ?", (MAX_ROUTES + 1,))]
    if len(workspaces) > MAX_WORKSPACES:
        out.add("diagnostics.workspace_limit", "workspaces", "warning",
                "Workspace checks were capped at the report limit.")
    workspaces = workspaces[:MAX_WORKSPACES]
    adapter_truncated = len(adapters) > MAX_ADAPTERS
    adapters = adapters[:MAX_ADAPTERS]
    routes_omitted = max(0, len(routes) - MAX_ROUTES)
    routes = routes[:MAX_ROUTES]

    config_valid = (isinstance(service.config, dict)
                    and service.config.get("schema_version") == 1)
    out.add("core.config_state_readable", "core", "pass" if config_valid else "failed",
            "Local configuration and private state are readable." if config_valid else
            "Local Bridge configuration is unavailable.",
            remediation=None if config_valid else "Check the local Bridge configuration.")
    out.add("core.state_schema_v4", "core", "pass",
            "Private Bridge state uses the Node-authority schema with per-run token usage.")
    out.add("core.gateway_credential_configured", "core",
            "pass" if gateway and gateway["token_hash"] else "action_required",
            "Shared MCP credential is configured." if gateway and gateway["token_hash"] else
            "Shared MCP credential is not configured.",
            remediation=None if gateway and gateway["token_hash"] else "Create the shared credential in the local manager.")
    out.add("core.gateway_enabled", "core",
            "pass" if gateway and gateway["enabled"] else "action_required",
            "Local MCP gateway is enabled." if gateway and gateway["enabled"] else
            "Local MCP gateway is disabled.",
            remediation=None if gateway and gateway["enabled"] else "Enable the gateway in the local manager.")
    ports = (listener.get("mcp_port", service.config.get("mcp_port")),
             listener.get("admin_port", service.config.get("admin_port")))
    valid_ports = all(isinstance(p, int) and not isinstance(p, bool) and 1024 <= p <= 65535
                      for p in ports) and ports[0] != ports[1]
    out.add("core.listener_ports", "core", "pass" if valid_ports else "failed",
            "MCP and local Manager ports are valid." if valid_ports else
            "MCP and local Manager ports are invalid or conflict.")
    channels = service.notification_manager.status()
    out.add("core.notifications", "core", "pass",
            f"{channels['channel_count']} notification channel(s) configured; informational only.")
    # M4.1 release identity: Bridge core is always observed locally.
    # Product vs component versions stay distinct; build IDs are
    # content-addressed source/package identities, not image digests.
    try:
        from .release import (ReleaseError, bridge_release, read_manager_release,
                              short_build_id, validate_release)
        _bridge_release = bridge_release()
    except Exception:
        _bridge_release = None
    try:
        _manager_release = read_manager_release()
    except Exception:
        _manager_release = None
    out.release_data["bridge"] = _bridge_release
    out.release_data["manager"] = _manager_release
    if _bridge_release is not None:
        out.add("release.bridge_identity", "release", "pass",
                f"Bridge release {_bridge_release['product_version']} "
                f"build {short_build_id(_bridge_release['build_id'])}.")
    else:
        out.add("release.bridge_identity", "release", "failed",
                "Bridge release identity is unavailable.")
    if _manager_release is not None:
        out.add("release.manager_identity", "release", "pass",
                f"Manager release {_manager_release['product_version']} "
                f"build {short_build_id(_manager_release['build_id'])}.")
    else:
        out.add("release.manager_identity", "release", "unknown",
                "Manager build identity was not observed.")
    _bridge_product = (_bridge_release["product_version"]
                       if isinstance(_bridge_release, dict) else None)
    _bridge_build = (_bridge_release["build_id"]
                     if isinstance(_bridge_release, dict) else None)
    nodes = service.node_registry.rows()
    node_health: dict[str, bool] = {}
    node_releases: dict[str, dict | None] = {}
    for node in nodes:
        healthy = False
        observed: dict | None = None
        release_error: str | None = None
        if node["enabled"] and not offline:
            try:
                state = service.node_registry.client(node["id"], timeout=3).status()
                healthy = True
                raw = state.get("release") if isinstance(state, dict) else None
                if raw is None:
                    release_error = "missing"
                else:
                    try:
                        observed = validate_release(raw)
                    except ReleaseError as exc:
                        release_error = getattr(exc, "kind", "invalid")
            except Exception:
                healthy = False
        node_health[node["id"]] = healthy
        node_releases[node["id"]] = observed
        out.release_data["nodes"][node["id"]] = observed
        out.add("node.reachable", "nodes",
                "pass" if healthy else ("unknown" if offline and node["enabled"] else
                                         "action_required" if not node["enabled"] else "failed"),
                "Authoritative Node is reachable." if healthy else
                ("Node reachability was not checked offline." if offline and node["enabled"] else
                 "Authoritative Node is disabled or unavailable."),
                remediation=None if healthy else "Check the Node endpoint, credential and Node service.")
        # Release compatibility never blocks routes; protocol/security/model
        # readiness remains the execution authority.
        if offline and node["enabled"]:
            out.add("release.node_identity", "release", "unknown",
                    "Node release identity was not checked offline.")
        elif not node["enabled"]:
            out.add("release.node_identity", "release", "unknown",
                    "Node release identity is unobserved while disabled.")
        elif not healthy:
            out.add("release.node_identity", "release", "unknown",
                    "Node release identity is unobserved while unreachable.")
        elif release_error == "missing":
            out.add("release.node_identity", "release", "warning",
                    "Node release identity is missing; staged rollout.",
                    remediation="Update the Node to a release-identity build.")
        elif release_error == "unsupported":
            out.add("release.node_identity", "release", "action_required",
                    "Node release contract is incompatible.",
                    remediation="Align the Node release contract with the Bridge.")
        elif release_error is not None:
            out.add("release.node_identity", "release", "warning",
                    "Node release identity is invalid.",
                    remediation="Update the Node to a release-identity build.")
        elif observed is not None and _bridge_product is not None and observed.get("product_version") != _bridge_product:
            out.add("release.node_product_skew", "release", "warning",
                    "Node product version differs from the Bridge.",
                    remediation="Align Node and Bridge product versions.")
        elif observed is not None and _bridge_build is not None and observed.get("build_id") != _bridge_build:
            out.add("release.node_build_skew", "release", "warning",
                    "Node Python-core build differs from the Bridge.",
                    remediation="Deploy the same Workspace Bridge package to Node and Bridge.")
        else:
            out.add("release.node_identity", "release", "pass",
                    "Node release identity matches the Bridge Python core.")

    root_access: dict[str, bool] = {}
    for ws in workspaces:
        wid = ws["id"]
        root_ok = False
        if node_health.get(ws["node_id"], False):
            try:
                service.node_registry.client(ws["node_id"], timeout=5).validate_root(
                    ws["root"], __import__("json").loads(ws["excludes"]))
                root_ok = True
            except Exception:
                root_ok = False
        root_access[wid] = root_ok
        out.add("workspace.root_accessible", "workspaces",
                "pass" if root_ok else "action_required",
                f"Workspace root is authorized by Node {ws['node_id']}." if root_ok else
                "Workspace root is unavailable or outside its Node allowed roots.",
                workspace_id=wid)
        out.add("workspace.enabled", "workspaces",
                "pass" if ws["enabled"] else "action_required",
                "Workspace mapping is enabled." if ws["enabled"] else "Workspace mapping is disabled.",
                workspace_id=wid)
        out.add("workspace.agent_execution", "workspaces", "pass",
                "Agent execution is governed by the exact enabled workspace route; "
                "the legacy workspace-wide agent switch is no longer a run gate.",
                workspace_id=wid)
        write_ok = ws["write_scope"] in {"handoff", "workspace"}
        out.add("workspace.write_scope", "workspaces",
                "pass" if write_ok else "action_required",
                "Workspace permits handoff writes." if write_ok else
                "Workspace write scope blocks prepared handoffs.", workspace_id=wid)
        if ws["enabled"]:
            git_status, git_summary = "pass", "Read-only Git evidence is available."
            if root_ok:
                try:
                    result = service.git_status(ws, offset=0, limit=1)
                    if result.get("available") is False:
                        git_summary = "Workspace is not a Git repository; execution readiness is unaffected."
                except Exception:
                    git_status, git_summary = "warning", "Git evidence is unavailable for this workspace."
            else:
                git_status, git_summary = "warning", "Git evidence could not inspect this workspace root."
            out.add("git.evidence", "git_evidence", git_status, git_summary, workspace_id=wid)

    adapter_health: dict[str, tuple[Descriptor | None, dict | None]] = {}
    if not offline:
        with ThreadPoolExecutor(max_workers=8, thread_name_prefix="bridge-adapter-diagnostic") as pool:
            futures = {pool.submit(_adapter_probe, service, row): row for row in adapters
                       if row["enabled"]}
            for future in as_completed(futures):
                adapter_health[futures[future]["id"]] = future.result()
    for row in adapters:
        aid = row["id"]
        if not row["enabled"]:
            out.add("adapter.enabled", "adapters", "action_required", "Adapter is disabled.",
                    remediation="Enable the adapter in the local Manager.", adapter_id=aid,
                    runtime_type=row["runtime_type"])
            adapter_health[aid] = (None, {"kind": "disabled"})
        elif offline:
            out.add("adapter.freshness", "adapters", "unknown",
                    "Adapter reachability was not checked offline.", adapter_id=aid,
                    runtime_type=row["runtime_type"])
            adapter_health[aid] = (None, {"kind": "offline"})
        else:
            descriptor, error = adapter_health.get(aid, (None, {"kind": "unavailable"}))
            if descriptor:
                out.add("adapter.reachable", "adapters", "pass",
                        "This adapter instance responded.", adapter_id=aid,
                        runtime_type=row["runtime_type"])
                out.add("adapter.protocol_compatible", "adapters", "pass",
                        "Runtime Protocol v1 descriptor is compatible.", adapter_id=aid,
                        runtime_type=row["runtime_type"])
            else:
                out.add("adapter.reachable", "adapters", "failed",
                        "This adapter instance is unavailable.",
                        remediation="Check this adapter's local connection and daemon.",
                        adapter_id=aid, runtime_type=row["runtime_type"])
                out.add("adapter.protocol_compatible", "adapters",
                        "action_required" if error and error.get("kind") == "protocol" else "unknown",
                        "Adapter protocol descriptor is incompatible or unavailable.",
                        adapter_id=aid, runtime_type=row["runtime_type"])

    for row in adapters:
        aid = row["id"]
        runtime_type = row["runtime_type"]
        descriptor, error = adapter_health.get(aid, (None, None))
        observed = descriptor.release if descriptor is not None else None
        out.release_data["adapters"][aid] = observed
        if offline or (error and error.get("kind") in {"offline", "disabled"}):
            out.add("release.adapter_identity", "release", "unknown",
                    "Adapter release identity was not checked offline." if offline else
                    "Adapter release identity is unobserved while disabled.",
                    adapter_id=aid, runtime_type=runtime_type)
        elif descriptor is None:
            message = (error or {}).get("message", "") if isinstance(error, dict) else ""
            if "release contract" in str(message).lower():
                out.add("release.adapter_identity", "release", "action_required",
                        "Adapter release contract is incompatible.",
                        remediation="Align the adapter release contract with the Bridge.",
                        adapter_id=aid, runtime_type=runtime_type)
            else:
                out.add("release.adapter_identity", "release", "unknown",
                        "Adapter release identity is unobserved while unreachable.",
                        adapter_id=aid, runtime_type=runtime_type)
        elif observed is None:
            out.add("release.adapter_identity", "release", "warning",
                    "Adapter release identity is missing; staged rollout.",
                    remediation="Update the adapter to a release-identity build.",
                    adapter_id=aid, runtime_type=runtime_type)
        elif _bridge_product is not None and observed.get("product_version") != _bridge_product:
            out.add("release.adapter_product_skew", "release", "warning",
                    "Adapter product version differs from the Bridge.",
                    remediation="Align adapter and Bridge product versions.",
                    adapter_id=aid, runtime_type=runtime_type)
        elif (runtime_type == "codex" and _bridge_build is not None
                and observed.get("build_id") != _bridge_build):
            out.add("release.adapter_build_skew", "release", "warning",
                    "Codex Python-core build differs from the Bridge.",
                    remediation="Deploy the same Workspace Bridge package to Codex and Bridge.",
                    adapter_id=aid, runtime_type=runtime_type)
        else:
            out.add("release.adapter_identity", "release", "pass",
                    "Adapter release identity is present.",
                    adapter_id=aid, runtime_type=runtime_type)
    health_by_id = {row["id"]: adapter_health.get(row["id"], (None, None))
                    for row in adapters}
    # write_scope is deliberately absent from run prerequisites: run
    # admission does not check it. A read-only workspace may still have an
    # existing prepared handoff/conversation that is runnable. Handoff
    # publication capability stays its own separate diagnostic below.
    prerequisites = {ws["id"]: [
        code for code, okay in (("workspace.root_accessible", bool(ws.get("enabled") and ws["root"])),
                                ("workspace.enabled", bool(ws["enabled"])))
        if not okay] for ws in workspaces}
    for ws in workspaces:
        codes = prerequisites[ws["id"]]
        if root_access.get(ws["id"]):
            prerequisites[ws["id"]] = [code for code in codes
                                         if code != "workspace.root_accessible"]
        elif "workspace.root_accessible" not in codes:
            codes.append("workspace.root_accessible")
    route_probes = routes[:MAX_PROBES]
    probe_results: dict[tuple[str, str], tuple[dict | None, list[dict] | None]] = {}
    if not offline:
        with ThreadPoolExecutor(max_workers=8, thread_name_prefix="bridge-route-diagnostic") as pool:
            futures = {}
            for route in route_probes:
                key = (route["workspace"], route["adapter_id"])
                futures[pool.submit(_profile_catalog, service,
                                    service.workspace(route["workspace"], False),
                                    route["adapter_id"])] = ("profile", key)
                futures[pool.submit(_models, service,
                                    service.workspace(route["workspace"], False),
                                    route["adapter_id"])] = ("models", key)
            temp: dict[tuple[str, str], list] = {}
            for future in as_completed(futures):
                kind, key = futures[future]
                try:
                    value = future.result()
                except Exception:
                    value = None
                temp.setdefault(key, [None, None])[0 if kind == "profile" else 1] = value
            probe_results = {key: (value[0], value[1]) for key, value in temp.items()}

    for route in routes:
        wid, aid = route["workspace"], route["adapter_id"]
        runtime_type = route["runtime_type"]
        key = (wid, aid)
        descriptor, health_error = health_by_id.get(aid, (None, None))
        policy = service.run_coordinator.model_policy(aid)
        catalog, model_rows = probe_results.get(key, (None, None))
        blockers = list(prerequisites.get(wid, []))
        for core_code in ("core.gateway_credential_configured", "core.gateway_enabled"):
            if not gateway or (not gateway["token_hash"] if core_code.endswith("configured") else not gateway["enabled"]):
                blockers.append(core_code)
        if not route["enabled"]:
            blockers.append("workspace_route.enabled")
        if not route["adapter_enabled"]:
            blockers.append("adapter.enabled")
        if route["adapter_node_id"] != route["node_id"]:
            blockers.append("adapter_node_mismatch")
        if not route["node_enabled"]:
            blockers.append("node.disabled")
        if not node_health.get(route["node_id"], False):
            blockers.append("node.unavailable")
        if route["adapter_enabled"] and not descriptor:
            blockers.append("adapter.reachable")
        # An unconfigured model policy is optional governance: models are
        # unrestricted by Bridge policy and the runtime chooses its default.
        out.add("workspace_route.enabled", "workspaces",
                "pass" if route["enabled"] else "action_required",
                "Workspace route is enabled." if route["enabled"] else "Workspace route is disabled.",
                workspace_id=wid, adapter_id=aid, runtime_type=runtime_type)
        out.add("model.policy", "models_profiles", "pass",
                "Adapter model policy restricts model selection." if policy["configured"] else
                "No Bridge model policy; models are unrestricted and the runtime chooses its default.",
                workspace_id=wid, adapter_id=aid, runtime_type=runtime_type)

        source = route["security_source"]
        profile_id = route["profile_id"] if source == "profile" else None
        profile_revision = route["profile_revision"] if source == "profile" else None
        if not source:
            out.add("route.security_binding", "models_profiles", "action_required",
                    "Workspace route has no security binding.", workspace_id=wid,
                    adapter_id=aid, runtime_type=runtime_type)
            blockers.append("route.security_binding")
        elif source == "runtime-config" and runtime_type != "codex":
            out.add("route.security_binding", "models_profiles", "action_required",
                    "Runtime config security requires a Codex adapter.", workspace_id=wid,
                    adapter_id=aid, runtime_type=runtime_type)
            blockers.append("route.security_binding")
        elif offline:
            out.add("route.security_freshness", "models_profiles", "unknown",
                    "Route security discovery was not checked offline.", workspace_id=wid,
                    adapter_id=aid, runtime_type=runtime_type)
            blockers.append("route.security_freshness")
        elif source == "runtime-config":
            native = catalog.get("runtimeConfig") if isinstance(catalog, dict) else None
            ok = bool(isinstance(native, dict) and native.get("supported") is True
                      and native.get("available") is True
                      and isinstance(native.get("revision"), str)
                      and bool(native.get("revision")))
            out.add("route.security_freshness", "models_profiles",
                    "pass" if ok else "action_required",
                    "Selected Codex adapter security config resolves." if ok else
                    "Selected Codex adapter security config is unavailable.",
                    workspace_id=wid, adapter_id=aid, runtime_type=runtime_type)
            if not ok:
                blockers.append("route.security_freshness")
        else:
            # Revision drift never blocks: adapter redeploys rotate opaque
            # revisions, so only a missing or unavailable profile does.
            rows = catalog.get("profiles", []) if isinstance(catalog, dict) else []
            current = next((row for row in rows if row.get("id") == profile_id), None)
            ok = bool(current and current.get("available") is not False)
            out.add("route.security_freshness", "models_profiles",
                    "pass" if ok else "action_required",
                    "Selected adapter profile is assigned and available." if ok else
                    "Selected adapter profile is missing or unavailable.",
                    workspace_id=wid, adapter_id=aid, runtime_type=runtime_type)
            if not ok:
                blockers.append("route.security_freshness")

        if offline:
            out.add("model.default_freshness", "models_profiles", "unknown",
                    "Default model availability was not checked offline.",
                    workspace_id=wid, adapter_id=aid, runtime_type=runtime_type)
            blockers.append("model.default_freshness")
        elif policy["configured"]:
            default = policy["default"]
            model = next((item for item in (model_rows or [])
                          if item.get("selector") == default), None)
            ok = bool(model and default in policy["enabled"])
            out.add("model.default_freshness", "models_profiles",
                    "pass" if ok else ("unknown" if model_rows is None else "action_required"),
                    "Configured default model is available for this route." if ok else
                    "Configured default model is unavailable for this route.",
                    workspace_id=wid, adapter_id=aid, runtime_type=runtime_type)
            if not ok:
                blockers.append("model.default_freshness")
            effort = policy["reasoning_defaults"].get(default)
            if effort is not None and model is not None and effort not in model.get("reasoningOptions", []):
                out.add("model.reasoning_default", "models_profiles", "action_required",
                        "Configured reasoning default is no longer available.",
                        workspace_id=wid, adapter_id=aid, runtime_type=runtime_type)
                blockers.append("model.reasoning_default")

        blockers = list(dict.fromkeys(blockers))
        ready = not blockers
        ws_name = _safe(route["workspace_name"], 80, "workspace")
        adapter_name = _safe(route["adapter_name"], 80, "adapter")
        out.routes.append(RunnableRoute(
            f"{wid}:{aid}", wid, ws_name, aid, adapter_name,
            route["node_id"], _safe(route["node_name"], 80, "Node"), runtime_type,
            ready, "ready" if ready else "blocked",
            "This exact workspace and adapter route is ready." if ready else
            f"Route has {len(blockers)} blocker(s).", blockers,
            bool(route["is_default"]), source,
            _safe(profile_id, 100) if profile_id else None,
            _safe(profile_revision, 100) if profile_revision else None,
            _safe(policy.get("default"), 160) if policy.get("default") else None))

    if routes_omitted:
        out.add("diagnostics.route_limit", "runnable_routes", "warning",
                "Some workspace and adapter routes were omitted at the report limit.")
    if len(routes) > MAX_PROBES:
        out.add("diagnostics.probe_limit", "models_profiles", "warning",
                "Workspace adapter discovery reached the per-report probe limit.")
    if adapter_truncated:
        out.add("diagnostics.adapter_limit", "adapters", "warning",
                "Adapter checks were capped at the report limit.")
    return out.report(mode)
