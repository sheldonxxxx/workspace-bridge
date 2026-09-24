"""Canonical, bounded diagnostics for the local administrator and Doctor CLI.

The evaluator owns runnable-route semantics. Its checks report observations;
RunnableRoute keeps every prerequisite attached to the exact workspace and
runtime pair that would execute.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import os
import re
from typing import Any

from .git_evidence import GitEvidence
from .runtime import RuntimeUnsupported
from .security import BridgeError, redact
from .wbrp import CORE_FEATURES, Descriptor, HttpRuntimeAdapter

STATUSES = frozenset({"pass", "warning", "action_required", "failed", "unknown"})
STATUS_ORDER = ("pass", "warning", "unknown", "action_required", "failed")
SECTIONS = frozenset({"core", "workspaces", "runtimes", "runnable_routes",
                       "models_profiles", "git_evidence"})
MAX_WORKSPACES = 100
MAX_RUNTIMES = 20
MAX_ROUTES = 400
MAX_MODEL_PROBES = 80
MAX_PROFILE_PROBES = 80
MAX_CONVERSATION_OBSERVATIONS = 2000
MAX_CHECKS = 5000
_SEVERITY = {"pass": 0, "warning": 1, "unknown": 2,
             "action_required": 3, "failed": 4}


def _stamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _bounded_text(value: object, limit: int, fallback: str = "") -> str:
    if not isinstance(value, str):
        return fallback
    text, _ = redact(value[:limit * 2])
    text = "".join(char for char in text if char == " " or ord(char) >= 32).strip()
    if (not text or "[REDACTED_SECRET]" in text or "http://" in text.lower()
            or "https://" in text.lower()
            or re.search(r"(?:^|\s)(?:/|~[/\\]|[a-z]:[/\\])", text)):
        return fallback
    return text[:limit]


def _safe_name(value: object) -> str:
    return _bounded_text(value, 80, "workspace")


def _safe_model(value: object) -> str | None:
    text = _bounded_text(value, 160)
    return text or None


def _severity_status(statuses: list[str]) -> str:
    return max(statuses, key=lambda value: _SEVERITY[value], default="pass")


def _bounded_adapter(adapter: HttpRuntimeAdapter) -> HttpRuntimeAdapter:
    """Use a short-lived client with the same private endpoint and credential."""
    return HttpRuntimeAdapter(adapter.runtime_id, adapter.base_url, adapter.token,
                              timeout=3.0)


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
    runtime: str | None = None

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "id": self.id, "code": self.code, "section": self.section,
            "status": self.status, "summary": self.summary[:160],
        }
        for key in ("detail", "remediation", "workspace_id", "runtime"):
            item = getattr(self, key)
            if item is not None:
                value[key] = item[:240] if key in ("detail", "remediation") else item
        return value


@dataclass(frozen=True)
class RunnableRoute:
    id: str
    workspace_id: str
    workspace_name: str
    runtime: str
    ready: bool
    status: str
    summary: str
    blockers: list[str] = field(default_factory=list)
    profile_id: str | None = None
    profile_revision: str | None = None
    default_model_selector: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "workspace_id": self.workspace_id,
            "workspace_name": self.workspace_name, "runtime": self.runtime,
            "ready": self.ready, "status": self.status,
            "summary": self.summary[:160], "blockers": list(self.blockers),
            "profile": ({"id": self.profile_id, "revision": self.profile_revision}
                        if self.profile_id is not None else None),
            "default_model_selector": self.default_model_selector,
        }


@dataclass(frozen=True)
class DiagnosticReport:
    generated_at: str
    mode: str
    overall_status: str
    overall_summary: str
    counts: dict[str, int]
    checks: list[DiagnosticCheck]
    runnable_routes: list[RunnableRoute]

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "mode": self.mode,
            "overall": {"status": self.overall_status,
                        "summary": self.overall_summary,
                        "counts": dict(self.counts)},
            "checks": [check.to_dict() for check in self.checks],
            "runnable_routes": [route.to_dict() for route in self.runnable_routes],
        }


def failure_report(*, mode: str = "offline", code: str = "core.config_state_readable",
                   summary: str = "Configuration or local state could not be read.",
                   section: str = "core") -> dict[str, Any]:
    """A canonical safe report for Doctor when it cannot construct a Service."""
    check = DiagnosticCheck("doctor.initialization", code, section, "failed", summary,
                            remediation="Check the local configuration and private state files.")
    return DiagnosticReport(_stamp(), mode, "failed", summary,
                            {status: int(status == "failed") for status in STATUS_ORDER},
                            [check], []).to_dict()


class _ReportBuilder:
    def __init__(self):
        self.checks: list[DiagnosticCheck] = []
        self.routes: list[RunnableRoute] = []

    def add(self, code: str, section: str, status: str, summary: str, *,
            detail: str | None = None, remediation: str | None = None,
            workspace_id: str | None = None, runtime: str | None = None) -> DiagnosticCheck:
        if status not in STATUSES or section not in SECTIONS:
            raise ValueError("invalid diagnostic check")
        scope = ":".join(item for item in (workspace_id, runtime) if item)
        ident = f"{code}:{scope}" if scope else code
        check = DiagnosticCheck(ident, code, section, status, summary, detail,
                                remediation, workspace_id, runtime)
        self.checks.append(check)
        return check

    def report(self, mode: str) -> dict[str, Any]:
        checks = self.checks[:MAX_CHECKS]
        counts = {status: sum(check.status == status for check in checks)
                  for status in STATUS_ORDER}
        overall = _severity_status([check.status for check in checks])
        summaries = {
            "pass": "All observed local and runtime checks passed.",
            "warning": "Diagnostics completed with warnings.",
            "unknown": "Some runtime freshness was not observed.",
            "action_required": "Configuration or runnable-route prerequisites need administrator action.",
            "failed": "One or more required diagnostic checks failed.",
        }
        return DiagnosticReport(_stamp(), mode, overall, summaries[overall], counts,
                                checks, self.routes[:MAX_ROUTES]).to_dict()


def _profile_rows(adapter, service, workspace: dict) -> dict | None:
    try:
        if isinstance(adapter, HttpRuntimeAdapter):
            with service.safe_root(workspace) as safe:
                return _bounded_adapter(adapter).profile_catalog(
                    str(workspace["id"]), safe.path, fresh=True)
        catalog = getattr(adapter, "profile_catalog", None)
        if callable(catalog):
            value = catalog(workspace, fresh=True)
            return value if isinstance(value, dict) else None
        rows = adapter.profiles()
        return {"profiles": rows} if isinstance(rows, list) else None
    except Exception:  # noqa: BLE001 - one runtime must not abort the report
        return None


def _descriptor(adapter, runtime: str) -> tuple[Descriptor | None, str | None]:
    try:
        client = _bounded_adapter(adapter) if isinstance(adapter, HttpRuntimeAdapter) else adapter
        value = client.descriptor()
        if isinstance(value, Descriptor):
            descriptor = value
            if descriptor.runtime_id != runtime:
                return None, "unavailable"
        else:
            descriptor = Descriptor.parse(value, expected_runtime=runtime)
        return descriptor, None
    except RuntimeUnsupported as exc:
        return None, "features" if str(exc) in {
            "Runtime omits a required v1 feature",
            "Runtime core feature version is unsupported",
        } else "protocol"
    except Exception:  # noqa: BLE001 - remote payloads are never copied to output
        return None, "unavailable"


def _model_rows(adapter, workspace_id: str) -> list[dict] | None:
    try:
        if isinstance(adapter, HttpRuntimeAdapter):
            rows = _bounded_adapter(adapter).models(workspace_id)
        else:
            rows = adapter.models(workspace_id)
        return rows if isinstance(rows, list) else None
    except Exception:  # noqa: BLE001 - per-route model failures are isolated
        return None


def evaluate(service, *, offline: bool = False, listener: dict | None = None,
             runtime_configuration_error: bool = False) -> dict[str, Any]:
    """Build the single canonical DiagnosticReport without holding DB locks over probes."""
    mode = "offline" if offline else "live"
    builder = _ReportBuilder()
    listener = listener or {}
    adapters = service.run_coordinator.adapters
    runtime_ids = sorted(adapters)[:MAX_RUNTIMES]
    runtime_truncated = len(adapters) > len(runtime_ids)

    # Copy the finite, relevant database state while locked. Every filesystem,
    # Git, and Runtime Protocol check below happens after releasing this lock.
    try:
        with service.lock:
            service.db.execute("BEGIN")
            try:
                gateway = service.db.execute(
                    "SELECT token_hash,enabled FROM gateway WHERE id=1").fetchone()
                workspaces = [dict(row) for row in service.db.execute(
                    "SELECT id,name,root,enabled,write_scope,agent_enabled,excludes FROM workspaces "
                    "ORDER BY name,id LIMIT ?", (MAX_WORKSPACES + 1,))]
                profile_columns = {row["name"] for row in service.db.execute(
                    "PRAGMA table_info(runtime_profiles)")}
                conversation_columns = {row["name"] for row in service.db.execute(
                    "PRAGMA table_info(runtime_conversations)")}
                has_profile_source = "source" in profile_columns
                has_conversation_source = "source" in conversation_columns
                profile_select = "workspace,runtime,profile,revision"
                if has_profile_source:
                    profile_select += ",source"
                profile_rows = [dict(row) for row in service.db.execute(
                    f"SELECT {profile_select} FROM runtime_profiles "
                    "ORDER BY workspace,runtime LIMIT ?", (MAX_WORKSPACES * MAX_RUNTIMES + 1,))]
                if not has_profile_source:
                    for row in profile_rows:
                        row["source"] = "profile"
                conversation_select = "workspace,runtime,revision"
                if has_conversation_source:
                    conversation_select += ",source"
                conversation_rows = [dict(row) for row in service.db.execute(
                    f"SELECT {conversation_select} FROM runtime_conversations "
                    "ORDER BY created DESC LIMIT ?", (MAX_CONVERSATION_OBSERVATIONS,))]
                if not has_conversation_source:
                    for row in conversation_rows:
                        row["source"] = "profile"
                grant_rows = [dict(row) for row in service.db.execute(
                    "SELECT workspace,runtime,enabled FROM workspace_runtimes "
                    "ORDER BY workspace,runtime LIMIT ?", (MAX_WORKSPACES * MAX_RUNTIMES + 1,))]
                setting_rows = {row["key"]: row["value"] for row in service.db.execute(
                    "SELECT key,value FROM settings WHERE key LIKE 'runtime_model_policy:%' LIMIT ?",
                    (MAX_RUNTIMES + 1,))}
                service.db.execute("COMMIT")
            except Exception:
                service.db.execute("ROLLBACK")
                raise
    except Exception:  # noqa: BLE001 - state read failures become a check
        builder.add("core.state_readable", "core", "failed",
                    "Private Bridge state could not be read.",
                    remediation="Check the local state database and its file permissions.")
        return builder.report(mode)

    ws_truncated = len(workspaces) > MAX_WORKSPACES
    workspaces = workspaces[:MAX_WORKSPACES]
    config_valid = (isinstance(service.config, dict)
                    and service.config.get("schema_version") == 1
                    and isinstance(service.config.get("allowed_parents"), list)
                    and bool(service.config.get("allowed_parents")))
    if config_valid:
        builder.add("core.config_state_readable", "core", "pass",
                    "Local configuration and private state are readable.")
    else:
        builder.add("core.config_state_readable", "core", "failed",
                    "Local configuration is unavailable.",
                    remediation="Check the private Bridge configuration file.")

    gateway_configured = bool(gateway and gateway["token_hash"])
    gateway_enabled = bool(gateway and gateway["enabled"])
    builder.add("core.gateway_credential_configured", "core",
                "pass" if gateway_configured else "action_required",
                "Shared MCP gateway credential is configured." if gateway_configured
                else "Shared MCP gateway credential is not configured.",
                remediation=None if gateway_configured else "Create the shared credential in the local manager.")
    builder.add("core.gateway_enabled", "core",
                "pass" if gateway_enabled else "action_required",
                "Local MCP gateway is enabled." if gateway_enabled
                else "Local MCP gateway is disabled.",
                remediation=None if gateway_enabled else "Enable the gateway in the local manager.")

    config = service.config or {}
    mcp_port = listener.get("mcp_port", config.get("mcp_port"))
    admin_port = listener.get("admin_port", config.get("admin_port"))
    valid_ports = all(isinstance(port, int) and not isinstance(port, bool)
                      and 1024 <= port <= 65535 for port in (mcp_port, admin_port))
    valid_ports = valid_ports and mcp_port != admin_port
    builder.add("core.listener_ports", "core", "pass" if valid_ports else "failed",
                (f"MCP port {mcp_port}; admin port {admin_port}." if valid_ports
                 else "Configured MCP and admin ports are invalid or conflict."),
                remediation=None if valid_ports else "Choose distinct TCP ports in 1024..65535.")
    container_mode = listener.get("container_mode") is True
    extra_host_count = listener.get("extra_admin_host_count", 0)
    widened = container_mode or bool(extra_host_count)
    invalid_host_config = listener.get("invalid_admin_host_config") is True
    exposure_status = "action_required" if invalid_host_config else "warning" if widened else "pass"
    if invalid_host_config:
        exposure_summary = "Admin listener host allowlist is invalid."
    elif container_mode:
        exposure_summary = "Listeners use container interfaces; host publishing should stay on loopback."
    elif extra_host_count:
        exposure_summary = "The local admin listener is widened by an explicit host allowlist."
    else:
        exposure_summary = "MCP and local admin listeners are loopback-only."
    builder.add("core.listener_exposure", "core", exposure_status, exposure_summary,
                detail=f"MCP listen scope: {'container interfaces' if container_mode else 'loopback'}; "
                       f"admin listen scope: {'container interfaces' if container_mode else 'widened' if extra_host_count else 'loopback'}.",
                remediation=("Correct the WB_ADMIN_ALLOWED_HOSTS setting." if invalid_host_config
                             else "Publish Docker ports on host 127.0.0.1 only." if container_mode
                             else "Prefer loopback, SSH forwarding, or VPN for local administration."
                             if extra_host_count else None))

    try:
        from .notifications import notification_channels_from_environment
        channels = notification_channels_from_environment()
        configured = bool(channels)
        invalid_config = bool(os.environ.get("WB_DISCORD_WEBHOOK_URL")) and not configured
        builder.add("core.notifications", "core", "warning" if invalid_config else "pass",
                    "Notification channel configuration could not be validated." if invalid_config
                    else f"{len(channels)} notification channel(s) configured; informational only.",
                    remediation="Review the local notification channel configuration." if invalid_config else None)
    except Exception:  # noqa: BLE001 - notification settings are informational only
        builder.add("core.notifications", "core", "warning",
                    "Notification channel configuration could not be validated.")

    parents_accessible = all(path.is_dir() for path in service.parents)
    builder.add("core.allowed_parents_accessible", "core",
                "pass" if parents_accessible else "warning",
                "Approved project parent directories are accessible." if parents_accessible
                else "An approved project parent is unavailable.",
                remediation=None if parents_accessible else "Restore the approved project parent directory.")

    builder.add("runtime.adapters_configured", "runtimes",
                "action_required" if runtime_configuration_error or not adapters else "pass",
                "Runtime adapter configuration is valid." if adapters and not runtime_configuration_error
                else "No valid Runtime Protocol adapter is configured.",
                remediation="Configure a private adapter and its shared Runtime Protocol credential.")
    if runtime_truncated:
        builder.add("diagnostics.runtime_limit", "runtimes", "warning",
                    "Runtime checks were limited to the first configured adapters.")

    profiles_by_key = {(row["workspace"], row["runtime"]): row for row in profile_rows}
    conversations_by_key: dict[tuple[str, str], list[dict]] = {}
    for row in conversation_rows:
        if row.get("source", "profile") == "runtime-config":
            conversations_by_key.setdefault(
                (row["workspace"], row["runtime"]), []).append(row)
    grants_by_key = {(row["workspace"], row["runtime"]): bool(row["enabled"])
                     for row in grant_rows}
    policies: dict[str, dict] = {}
    for runtime in runtime_ids:
        raw = setting_rows.get(f"runtime_model_policy:{runtime}")
        try:
            value = json.loads(raw) if raw else None
        except (TypeError, ValueError):
            value = None
        if (isinstance(raw, str) and len(raw) <= 64_000
                and isinstance(value, dict) and isinstance(value.get("enabled"), list)
                and isinstance(value.get("default"), str)
                and value["default"] in value["enabled"]
                and isinstance(value.get("reasoning_defaults", {}), dict)):
            policies[runtime] = {"enabled": value["enabled"], "default": value["default"],
                                 "reasoning_defaults": value.get("reasoning_defaults", {})}
        else:
            policies[runtime] = {"enabled": [], "default": None, "reasoning_defaults": {}}

    # Runtime descriptors are shared; security profile discovery is scoped to
    # the exact workspace/runtime pair below.
    descriptors: dict[str, Descriptor | None] = {}
    descriptor_errors: dict[str, str | None] = {}
    if offline:
        for runtime in runtime_ids:
            descriptors[runtime] = None
            descriptor_errors[runtime] = "offline"
            builder.add("runtime.freshness", "runtimes", "unknown",
                        "Runtime reachability and descriptor freshness were not checked offline.",
                        remediation="Run workspace-bridge doctor for bounded live runtime checks.",
                        runtime=runtime)
    else:
        def probe(runtime: str) -> tuple[Descriptor | None, str | None]:
            adapter = adapters[runtime]
            descriptor, error = _descriptor(adapter, runtime)
            return descriptor, error

        runtime_probes: dict[str, tuple[Descriptor | None, str | None]] = {}
        with ThreadPoolExecutor(max_workers=8, thread_name_prefix="bridge-runtime-diagnostic") as pool:
            futures = {pool.submit(probe, runtime): runtime for runtime in runtime_ids}
            for future in as_completed(futures):
                runtime = futures[future]
                try:
                    runtime_probes[runtime] = future.result()
                except Exception:  # noqa: BLE001 - isolate malformed adapters
                    runtime_probes[runtime] = (None, "unavailable")
        for runtime in runtime_ids:
            descriptor, error = runtime_probes[runtime]
            descriptors[runtime] = descriptor
            descriptor_errors[runtime] = error
            if error in {"protocol", "features"}:
                builder.add("runtime.protocol_compatible", "runtimes", "action_required",
                            "Runtime Protocol descriptor is incompatible." if error == "protocol"
                            else "Runtime Protocol v1 descriptor omits a required run capability.",
                            remediation="Update the configured adapter to a compatible Runtime Protocol v1 implementation.",
                            runtime=runtime)
                if error == "features":
                    builder.add("runtime.required_features", "runtimes", "action_required",
                                "Required run, conversation, interaction, model, and activity capabilities are missing.",
                                remediation="Update the adapter to expose all required Runtime Protocol v1 capabilities.",
                                runtime=runtime)
                builder.add("runtime.reachable", "runtimes", "pass",
                            "Configured runtime adapter responded.", runtime=runtime)
            elif error:
                builder.add("runtime.reachable", "runtimes", "failed",
                            "Configured runtime adapter is unavailable.",
                            remediation="Check the private runtime adapter process and local connection settings.",
                            runtime=runtime)
                builder.add("runtime.protocol_compatible", "runtimes", "unknown",
                            "Protocol compatibility could not be observed.", runtime=runtime)
            else:
                missing = sorted(feature for feature in CORE_FEATURES
                                 if not descriptor.supports(feature))
                builder.add("runtime.reachable", "runtimes", "pass",
                            "Configured runtime adapter responded.", runtime=runtime)
                builder.add("runtime.protocol_compatible", "runtimes",
                            "pass" if descriptor else "action_required",
                            "Runtime Protocol v1 descriptor is compatible." if descriptor else
                            "Runtime Protocol descriptor is invalid.", runtime=runtime)
                builder.add("runtime.required_features", "runtimes",
                            "pass" if not missing else "action_required",
                            "Required run, conversation, interaction, model, and activity capabilities are present."
                            if not missing else "Runtime omits a required run capability.",
                            remediation="Update the adapter to expose all required Runtime Protocol v1 capabilities."
                            if missing else None, runtime=runtime)

    if ws_truncated:
        builder.add("diagnostics.workspace_limit", "workspaces", "warning",
                    "Workspace checks and routes were limited to the first configured workspaces.")
    if len(profile_rows) > MAX_WORKSPACES * MAX_RUNTIMES or len(grant_rows) > MAX_WORKSPACES * MAX_RUNTIMES:
        builder.add("diagnostics.policy_limit", "models_profiles", "warning",
                    "Workspace runtime policy checks were bounded.")

    workspace_prerequisites: dict[str, list[str]] = {}
    for ws in workspaces:
        ws_id = str(ws["id"])
        enabled = bool(ws.get("enabled"))
        builder.add("workspace.enabled", "workspaces", "pass" if enabled else "action_required",
                    "Workspace mapping is enabled." if enabled else "Workspace mapping is disabled.",
                    remediation=None if enabled else "Enable this workspace mapping in the local manager.",
                    workspace_id=ws_id)
        try:
            with service.safe_root(ws):
                root_ok = True
        except Exception:  # noqa: BLE001 - no path or raw OS error is exposed
            root_ok = False
        builder.add("workspace.root_accessible", "workspaces",
                    "pass" if root_ok else "action_required",
                    "Workspace root is currently accessible." if root_ok else
                    "Workspace root is unavailable or outside the approved scope.",
                    remediation=None if root_ok else "Restore the mapped project directory beneath an approved parent.",
                    workspace_id=ws_id)
        write_ok = ws.get("write_scope") in {"handoff", "workspace"}
        builder.add("workspace.write_scope", "workspaces", "pass" if write_ok else "action_required",
                    "Workspace can write handoff artifacts." if write_ok else
                    "Workspace write scope does not permit handoff artifacts.",
                    remediation=None if write_ok else "Set workspace write scope to handoff or workspace.",
                    workspace_id=ws_id)
        agent_ok = bool(ws.get("agent_enabled"))
        builder.add("workspace.agent_enabled", "workspaces", "pass" if agent_ok else "action_required",
                    "Agent runs are enabled for this workspace." if agent_ok else
                    "Agent runs are disabled for this workspace.",
                    remediation=None if agent_ok else "Enable agent runs for this workspace in the local manager.",
                    workspace_id=ws_id)
        workspace_prerequisites[ws_id] = [code for code, ok in (
            ("workspace.enabled", enabled), ("workspace.root_accessible", root_ok),
            ("workspace.write_scope", write_ok), ("workspace.agent_enabled", agent_ok)) if not ok]
        if enabled:
            git_status = "pass"
            git_summary = "Read-only Git Evidence is available."
            git_remediation = None
            if not root_ok:
                git_status, git_summary = "warning", "Git Evidence could not inspect the unavailable workspace root."
            else:
                try:
                    with service.safe_root(ws) as safe:
                        result = GitEvidence.status(safe, ws, offset=0, limit=1)
                    if result.get("available") is False and result.get("reason") == "not_a_repository":
                        git_summary = "Workspace is not a Git repository; execution routes are unaffected."
                except BridgeError as exc:
                    git_status, git_summary = "warning", "Git Evidence is unavailable for this workspace."
                    git_remediation = ("Repair the Git executable or supported repository layout."
                                       if exc.code in {"git_unavailable", "unsupported_repository_layout",
                                                       "repository_unavailable", "git_evidence_failed"}
                                       else "Review Git Evidence availability in the local manager.")
                except Exception:  # noqa: BLE001
                    git_status, git_summary = "warning", "Git Evidence is unavailable for this workspace."
                    git_remediation = "Review Git Evidence availability in the local manager."
            builder.add("git.evidence", "git_evidence", git_status, git_summary,
                        remediation=git_remediation, workspace_id=ws_id)

    routes_omitted = max(0, len(workspaces) * len(runtime_ids) - MAX_ROUTES)
    route_pairs = [(ws, runtime) for ws in workspaces for runtime in runtime_ids][:MAX_ROUTES]
    profile_probe_candidates = [
        (ws, runtime) for ws, runtime in route_pairs
        if (str(ws["id"]), runtime) in profiles_by_key
        and descriptors.get(runtime) is not None
        and descriptor_errors.get(runtime) is None
    ]
    profile_results: dict[tuple[str, str], dict | None] = {}
    profile_probe_skipped: set[tuple[str, str]] = set()
    for ws, runtime in profile_probe_candidates[:MAX_PROFILE_PROBES]:
        profile_results[(str(ws["id"]), runtime)] = None
    profile_probe_skipped.update(
        (str(ws["id"]), runtime)
        for ws, runtime in profile_probe_candidates[MAX_PROFILE_PROBES:])
    if not offline and profile_results:
        with ThreadPoolExecutor(max_workers=8, thread_name_prefix="bridge-profile-diagnostic") as pool:
            futures = {
                pool.submit(_profile_rows, adapters[runtime], service, ws):
                    (str(ws["id"]), runtime)
                for ws, runtime in profile_probe_candidates[:MAX_PROFILE_PROBES]
            }
            for future in as_completed(futures):
                key = futures[future]
                try:
                    profile_results[key] = future.result()
                except Exception:  # noqa: BLE001 - per-workspace probes are isolated
                    profile_results[key] = None
    model_candidates: list[tuple[dict, str, dict, dict | None]] = []
    for ws, runtime in route_pairs:
        ws_id = str(ws["id"])
        grant = grants_by_key.get((ws_id, runtime), False)
        grant_ok = grant
        builder.add("workspace.runtime_grant", "workspaces",
                    "pass" if grant_ok else "action_required",
                    "Workspace grant for this runtime is enabled." if grant_ok else
                    "Workspace grant for this runtime is disabled.",
                    remediation=None if grant_ok else "Enable this runtime for the workspace in the local manager.",
                    workspace_id=ws_id, runtime=runtime)
        profile = profiles_by_key.get((ws_id, runtime))
        policy = policies[runtime]
        policy_ok = policy["default"] is not None
        builder.add("model.policy", "models_profiles",
                    "pass" if policy_ok else "action_required",
                    "Runtime model policy has an enabled default." if policy_ok else
                    "Runtime model policy or its default is not configured.",
                    remediation=None if policy_ok else "Enable a model and choose a runtime default in the local manager.",
                    workspace_id=ws_id, runtime=runtime)
        blockers = list(workspace_prerequisites[ws_id])
        if not gateway_configured:
            blockers.append("core.gateway_credential_configured")
        if not gateway_enabled:
            blockers.append("core.gateway_enabled")
        if not grant_ok:
            blockers.append("workspace.runtime_grant")
        if profile is None:
            builder.add("profile.binding", "models_profiles", "action_required",
                        "Workspace has no security binding for this runtime.",
                        remediation="Choose a Workspace Bridge profile or current Codex config in the local manager.",
                        workspace_id=ws_id, runtime=runtime)
            blockers.append("profile.binding")
        else:
            profile_source = profile.get("source", "profile")
            if profile_source == "runtime-config":
                profile_id = None
                profile_revision = None
                builder.add("profile.binding", "models_profiles", "pass",
                            "Workspace follows current Codex config security.",
                            workspace_id=ws_id, runtime=runtime)
            else:
                profile_id = _bounded_text(profile.get("profile"), 100)
                profile_revision = _bounded_text(profile.get("revision"), 100)
                builder.add("profile.binding", "models_profiles", "pass",
                            "Workspace security profile binding is stored.",
                            workspace_id=ws_id, runtime=runtime)
        if not policy_ok:
            blockers.append("model.policy")
        descriptor = descriptors.get(runtime)
        runtime_error = descriptor_errors.get(runtime)
        if offline:
            if profile is not None:
                if profile.get("source", "profile") == "runtime-config":
                    builder.add("runtime.config_resolvable", "models_profiles", "unknown",
                                "Current Codex security config was not checked offline.",
                                workspace_id=ws_id, runtime=runtime)
                    blockers.append("runtime.config_resolvable")
                else:
                    builder.add("profile.freshness", "models_profiles", "unknown",
                                "Bound security profile revision was not checked offline.",
                                workspace_id=ws_id, runtime=runtime)
                    blockers.append("profile.freshness")
            if policy_ok:
                builder.add("model.default_freshness", "models_profiles", "unknown",
                            "Default model availability was not checked offline.",
                            workspace_id=ws_id, runtime=runtime)
                blockers.append("model.default_freshness")
        elif runtime_error in {"protocol", "features"}:
            blockers.append("runtime.required_features" if runtime_error == "features"
                            else "runtime.protocol_compatible")
            if profile is not None:
                if profile.get("source", "profile") == "runtime-config":
                    builder.add("runtime.config_resolvable", "models_profiles", "unknown",
                                "Codex security config could not be checked because the runtime descriptor is incompatible.",
                                workspace_id=ws_id, runtime=runtime)
                    blockers.append("runtime.config_resolvable")
                else:
                    builder.add("profile.freshness", "models_profiles", "unknown",
                                "Bound security profile revision could not be checked because the runtime descriptor is incompatible.",
                                workspace_id=ws_id, runtime=runtime)
            if policy_ok:
                builder.add("model.default_freshness", "models_profiles", "unknown",
                            "Default model availability could not be checked because the runtime descriptor is incompatible.",
                            workspace_id=ws_id, runtime=runtime)
        elif runtime_error:
            blockers.append("runtime.reachable")
            if profile is not None:
                if profile.get("source", "profile") == "runtime-config":
                    builder.add("runtime.config_resolvable", "models_profiles", "unknown",
                                "Codex security config could not be checked.",
                                workspace_id=ws_id, runtime=runtime)
                    blockers.append("runtime.config_resolvable")
                else:
                    builder.add("profile.freshness", "models_profiles", "unknown",
                                "Bound security profile revision could not be checked.",
                                workspace_id=ws_id, runtime=runtime)
            if policy_ok:
                builder.add("model.default_freshness", "models_profiles", "unknown",
                            "Default model availability could not be checked.",
                            workspace_id=ws_id, runtime=runtime)
        elif descriptor:
            features_ok = all(descriptor.supports(feature) for feature in CORE_FEATURES)
            if not features_ok:
                blockers.append("runtime.required_features")
                if policy_ok:
                    builder.add("model.default_freshness", "models_profiles", "unknown",
                                "Default model availability could not be checked because a required runtime capability is missing.",
                                workspace_id=ws_id, runtime=runtime)
            if profile is not None:
                profile_key = (ws_id, runtime)
                available_profiles = profile_results.get(profile_key)
                if profile.get("source", "profile") == "runtime-config":
                    if profile_key in profile_probe_skipped:
                        builder.add("runtime.config_resolvable", "models_profiles", "unknown",
                                    "Codex security discovery was skipped after the per-report workspace/runtime limit.",
                                    remediation="Reduce the number of bound workspace/runtime pairs or inspect them in smaller groups.",
                                    workspace_id=ws_id, runtime=runtime)
                        blockers.append("runtime.config_resolvable")
                    elif available_profiles is None:
                        builder.add("runtime.config_resolvable", "models_profiles", "unknown",
                                    "Current Codex security config could not be checked for this workspace.",
                                    workspace_id=ws_id, runtime=runtime)
                        blockers.append("runtime.config_resolvable")
                    else:
                        native = available_profiles.get("runtimeConfig")
                        config_ok = bool(isinstance(native, dict)
                                         and native.get("supported") is True
                                         and native.get("available") is True
                                         and isinstance(native.get("revision"), str))
                        builder.add("runtime.config_resolvable", "models_profiles",
                                    "pass" if config_ok else "action_required",
                                    "Codex config resolves a usable security state." if config_ok else
                                    "Codex config cannot currently resolve a usable security state.",
                                    remediation=None if config_ok else "Resolve the native Codex security settings or managed requirements.",
                                    workspace_id=ws_id, runtime=runtime)
                        if not config_ok:
                            blockers.append("runtime.config_resolvable")
                        elif any(row.get("revision") != native.get("revision")
                                 for row in conversations_by_key.get(profile_key, [])):
                            builder.add("conversation.security_update_pending", "models_profiles",
                                        "warning",
                                        "A prior conversation will refresh its security settings or be replaced before its next turn.",
                                        workspace_id=ws_id, runtime=runtime)
                else:
                    if profile_key in profile_probe_skipped:
                        builder.add("profile.freshness", "models_profiles", "unknown",
                                    "Profile discovery was skipped after the per-report workspace/runtime limit.",
                                    remediation="Reduce the number of bound workspace/runtime pairs or inspect them in smaller groups.",
                                    workspace_id=ws_id, runtime=runtime)
                        blockers.append("profile.freshness")
                    elif available_profiles is None:
                        builder.add("profile.freshness", "models_profiles", "unknown",
                                    "Bound security profile revision could not be checked for this workspace.",
                                    workspace_id=ws_id, runtime=runtime)
                        blockers.append("profile.freshness")
                    else:
                        rows = available_profiles.get("profiles", [])
                        current = next((item for item in rows
                                        if item.get("id") == profile.get("profile")), None)
                        profile_ok = bool(current and current.get("available") is not False
                                           and current.get("revision") == profile.get("revision"))
                        builder.add("profile.freshness", "models_profiles",
                                    "pass" if profile_ok else "action_required",
                                    "Bound security profile revision is current." if profile_ok else
                                    "Bound security profile is missing, disallowed, or its revision has changed.",
                                    remediation=None if profile_ok else "Reassign a currently available security profile for this workspace.",
                                    workspace_id=ws_id, runtime=runtime)
                        if not profile_ok:
                            blockers.append("profile.freshness")
            if policy_ok and features_ok:
                model_candidates.append((ws, runtime, policy, profile))
        route_profile = profiles_by_key.get((ws_id, runtime))
        is_native_config = bool(route_profile and
                                route_profile.get("source") == "runtime-config")
        profile_id = (_bounded_text(route_profile.get("profile"), 100)
                      if route_profile and not is_native_config else None)
        profile_revision = (_bounded_text(route_profile.get("revision"), 100)
                            if route_profile and not is_native_config else None)
        default_selector = _safe_model(policy.get("default"))
        if policy_ok and default_selector is None:
            blockers.append("model.policy")
        # De-duplicate stable blockers while preserving their readable order.
        blockers = list(dict.fromkeys(blockers))
        builder.routes.append(RunnableRoute(
            f"{ws_id}:{runtime}", ws_id, _safe_name(ws.get("name")), runtime,
            False, "blocked", (f"Runnable path has {len(blockers)} blocker(s)."
                                if blockers else "Runtime freshness is being evaluated."), blockers,
            profile_id, profile_revision, default_selector))

    # Discover workspace-specific models only for otherwise runnable candidates.
    # A report has a fixed probe budget and bounded HTTP deadlines.
    model_results: dict[tuple[str, str], list[dict] | None] = {}
    for ws, runtime, _policy, _profile in model_candidates[:MAX_MODEL_PROBES]:
        model_results[(str(ws["id"]), runtime)] = None
    if not offline and model_results:
        with ThreadPoolExecutor(max_workers=8, thread_name_prefix="bridge-diagnostic") as pool:
            futures = {pool.submit(_model_rows, adapters[runtime], ws_id): (ws_id, runtime)
                       for ws_id, runtime in model_results}
            for future in as_completed(futures):
                key = futures[future]
                try:
                    model_results[key] = future.result()
                except Exception:  # noqa: BLE001
                    model_results[key] = None

    route_index = {(route.workspace_id, route.runtime): index
                   for index, route in enumerate(builder.routes)}
    probed = set(model_results)
    for ws, runtime, policy, _profile in model_candidates:
        ws_id = str(ws["id"])
        route = builder.routes[route_index[(ws_id, runtime)]]
        if (ws_id, runtime) not in probed:
            builder.add("model.default_freshness", "models_profiles", "unknown",
                        "Model discovery was skipped after the per-report probe limit.",
                        remediation="Reduce the number of runnable workspace/runtime pairs or inspect them in smaller groups.",
                        workspace_id=ws_id, runtime=runtime)
            blockers = list(dict.fromkeys([*route.blockers, "model.default_freshness"]))
            route = RunnableRoute(route.id, route.workspace_id, route.workspace_name,
                                  route.runtime, False, "blocked",
                                  "Model discovery freshness is unknown.", blockers,
                                  route.profile_id, route.profile_revision,
                                  route.default_model_selector)
            builder.routes[route_index[(ws_id, runtime)]] = route
            continue
        rows = model_results[(ws_id, runtime)]
        if rows is None:
            builder.add("model.default_freshness", "models_profiles", "failed",
                        "Workspace-specific model discovery failed.",
                        remediation="Check the private runtime adapter and retry diagnostics.",
                        workspace_id=ws_id, runtime=runtime)
            blockers = list(dict.fromkeys([*route.blockers, "model.default_freshness"]))
            route = RunnableRoute(route.id, route.workspace_id, route.workspace_name,
                                  route.runtime, False, "blocked",
                                  "Workspace-specific model availability could not be confirmed.", blockers,
                                  route.profile_id, route.profile_revision,
                                  route.default_model_selector)
            builder.routes[route_index[(ws_id, runtime)]] = route
            continue
        default = policy["default"]
        model = next((item for item in rows if isinstance(item, dict)
                      and item.get("selector") == default), None)
        model_ok = model is not None and default in policy["enabled"]
        builder.add("model.default_freshness", "models_profiles",
                    "pass" if model_ok else "action_required",
                    "Configured default model is available for this workspace." if model_ok else
                    "Configured default model is unavailable for this workspace.",
                    remediation=None if model_ok else "Choose a currently discovered model as the runtime default.",
                    workspace_id=ws_id, runtime=runtime)
        blockers = list(route.blockers)
        if not model_ok:
            blockers.append("model.default_freshness")
        effort = policy["reasoning_defaults"].get(default)
        reasoning_ok = True
        if effort is not None:
            options = model.get("reasoningOptions") if model else None
            reasoning_ok = isinstance(options, list) and effort in options
            builder.add("model.reasoning_default", "models_profiles",
                        "pass" if reasoning_ok else "action_required",
                        "Configured reasoning default is available." if reasoning_ok else
                        "Configured reasoning default is unavailable for the default model.",
                        remediation=None if reasoning_ok else "Choose a reasoning level supported by the current default model.",
                        workspace_id=ws_id, runtime=runtime)
            if not reasoning_ok:
                blockers.append("model.reasoning_default")
        blockers = list(dict.fromkeys(blockers))
        ready = not blockers and model_ok and reasoning_ok
        builder.routes[route_index[(ws_id, runtime)]] = RunnableRoute(
            route.id, route.workspace_id, route.workspace_name, route.runtime,
            ready, "ready" if ready else "blocked",
            "This exact workspace/runtime path is ready to start." if ready else
            f"Runnable path has {len(blockers)} blocker(s).", blockers,
            route.profile_id, route.profile_revision, route.default_model_selector)

    if routes_omitted:
        builder.add("diagnostics.route_limit", "runnable_routes", "warning",
                    "Runnable route output was capped; some workspace/runtime pairs are omitted.")
    if len(model_candidates) > MAX_MODEL_PROBES:
        builder.add("diagnostics.model_probe_limit", "models_profiles", "warning",
                    "Workspace-specific model discovery reached its per-report limit.")
    if profile_probe_skipped:
        builder.add("diagnostics.profile_probe_limit", "models_profiles", "warning",
                    "Workspace-specific profile discovery reached its per-report limit.")
    return builder.report(mode)
