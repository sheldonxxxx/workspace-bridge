"""Runtime Protocol v1 run coordination and durable Bridge-side snapshots.

Only this module knows the protocol client. It contains no Pi or Codex names.
Handoff and workspace authorization remain in Service. Snapshots, rather than
events, decide state after disconnection or adapter restart.
"""
from __future__ import annotations

import json
import re
import secrets
import threading
from datetime import datetime, timezone
from typing import Any

from .runtime import RuntimeRejected, RuntimeUnavailable, RuntimeUnsupported
from .security import BridgeError, HANDOFF, digest, redact
from .notifications import codex_quota_summary
from .wbrp import validate_run_state

_DESCRIPTOR_CODE_RE = re.compile(r"[a-z][a-z0-9_]{0,79}")


def _sanitized_descriptor_code(code: Any, fallback: str = "descriptor_error") -> str:
    if isinstance(code, str) and _DESCRIPTOR_CODE_RE.fullmatch(code):
        return code
    return fallback


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _id(prefix: str) -> str:
    return prefix + secrets.token_hex(12)


def _safe(value: Any, limit: int = 20000) -> str:
    text = value if isinstance(value, str) else "" if value is None else str(value)
    return redact(text[:limit])[0]


def _safe_payload(value: dict, limit: int = 16000) -> str:
    """Store bounded, redacted evidence; identifiers stay in their own columns."""
    def clean(item: Any, depth: int = 0) -> Any:
        if depth > 6:
            return "[truncated]"
        if isinstance(item, str):
            return _safe(item, 2000)
        if isinstance(item, list):
            return [clean(child, depth + 1) for child in item[:50]]
        if isinstance(item, dict):
            return {str(key)[:100]: clean(child, depth + 1)
                    for key, child in list(item.items())[:50]}
        if isinstance(item, (bool, int, float)) or item is None:
            return item
        return "[unsupported]"

    result = clean(value)
    raw = json.dumps(result, ensure_ascii=False)
    if len(raw) > limit:
        result = {key: result.get(key) for key in
                  ("id", "runId", "kind", "state", "status", "title") if key in result}
        result["truncated"] = True
        raw = json.dumps(result, ensure_ascii=False)
    return raw


def _effective_security(binding: dict, *, node_id: str, node_revision: str,
                        adapter_id: str, adapter_revision: str) -> dict:
    source = binding.get("source")
    if source == "profile":
        profile = binding.get("profile", {})
        result = {"source": "profile", "profile_id": profile.get("id"),
                  "bound_revision": profile.get("revision"),
                  "effective_revision": profile.get("revision")}
    else:
        summary = binding.get("resolvedSummary")
        safe_summary = {}
        if isinstance(summary, dict):
            for key in ("activePermissionProfile", "approvalPolicy", "approvalsReviewer", "provenance"):
                value = summary.get(key)
                if value is None or (isinstance(value, str) and len(value) <= 128
                                     and all(ord(c) >= 32 for c in value)):
                    safe_summary[key] = value
        result = {"source": "runtime-config", "profile_id": None,
                  "bound_revision": binding.get("revision"),
                  "effective_revision": binding.get("revision"),
                  "resolved_summary": safe_summary}
    return {**result, "node_id": node_id, "node_revision": node_revision,
            "adapter_id": adapter_id, "adapter_revision": adapter_revision}


EXECUTION_ACTIVITY_KINDS = frozenset({
    "command", "file_change", "tool_call", "search", "subagent",
})
MAX_SAFE_INTEGER = 9007199254740991
RUN_USAGE_TO_BRIDGE = {
    "inputTokens": "input_tokens",
    "cachedInputTokens": "cached_input_tokens",
    "cacheWriteInputTokens": "cache_write_input_tokens",
    "outputTokens": "output_tokens",
    "reasoningOutputTokens": "reasoning_output_tokens",
    "totalTokens": "total_tokens",
}


def _normalize_bridge_usage(native: Any) -> dict | None:
    """Normalize a validated Runtime Protocol usage snapshot to Bridge storage.

    The snapshot is run-scoped native provider counters for exactly one
    Bridge run (continuation runs start a fresh boundary) and may be partial
    while active. Only reported counters are stored; absent counters stay
    absent and are never synthesized as zero. Malformed input yields None so
    callers preserve the existing durable snapshot instead of persisting it.
    """
    if not isinstance(native, dict) or not native:
        return None
    result: dict[str, int] = {}
    for runtime_key, bridge_key in RUN_USAGE_TO_BRIDGE.items():
        if runtime_key not in native:
            continue
        counter = native[runtime_key]
        if (isinstance(counter, bool) or not isinstance(counter, int)
                or counter < 0 or counter > MAX_SAFE_INTEGER):
            return None
        result[bridge_key] = int(counter)
    return result or None


def _stored_bridge_usage(stored: Any) -> dict | None:
    if stored is None:
        return None
    if isinstance(stored, dict):
        candidate = stored
    elif isinstance(stored, str) and stored:
        try:
            candidate = json.loads(stored)
        except (TypeError, ValueError):
            return None
    else:
        return None
    if not isinstance(candidate, dict) or not candidate:
        return None
    allowed = set(RUN_USAGE_TO_BRIDGE.values())
    if set(candidate) - allowed:
        return None
    result: dict[str, int] = {}
    for key, counter in candidate.items():
        if (isinstance(counter, bool) or not isinstance(counter, int)
                or counter < 0 or counter > MAX_SAFE_INTEGER):
            return None
        result[key] = int(counter)
    return result or None


class RunCoordinator:
    """One generic coordinator for every AdapterInstance in the registry."""

    def __init__(self, service, *, background: bool = True, read_only: bool = False):
        self.service = service
        self.background = background
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._sweep_failures: set[str] = set()
        if not read_only:
            service.db.executescript("""
          CREATE TABLE IF NOT EXISTS agent_conversations (
            id TEXT PRIMARY KEY, workspace TEXT NOT NULL REFERENCES workspaces(id),
            node_id TEXT NOT NULL REFERENCES nodes(id), node_revision TEXT NOT NULL,
            adapter_id TEXT NOT NULL REFERENCES node_adapters(adapter_id), runtime_type TEXT NOT NULL,
            adapter_revision TEXT NOT NULL, native_id TEXT NOT NULL,
            profile TEXT NOT NULL, revision TEXT NOT NULL,
            instance_id TEXT NOT NULL, created TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'profile', security_snapshot TEXT,
            permission_revision TEXT NOT NULL DEFAULT '',
            approval_revision TEXT NOT NULL DEFAULT '', replacement_reason TEXT,
            UNIQUE(adapter_id,native_id));
          CREATE TABLE IF NOT EXISTS agent_runs (
            id TEXT PRIMARY KEY, conversation TEXT NOT NULL REFERENCES agent_conversations(id),
            workspace TEXT NOT NULL REFERENCES workspaces(id),
            node_id TEXT NOT NULL REFERENCES nodes(id), node_revision TEXT NOT NULL,
            adapter_id TEXT NOT NULL REFERENCES node_adapters(adapter_id), runtime_type TEXT NOT NULL,
            adapter_revision TEXT NOT NULL,
            handoff TEXT NOT NULL REFERENCES jobs(id),
            request_id TEXT NOT NULL, request_hash TEXT NOT NULL,
            continue_from TEXT, parent_run TEXT,
            native_id TEXT, model TEXT NOT NULL, reasoning TEXT,
            phase TEXT NOT NULL, active_state TEXT, outcome TEXT,
            result TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '',
            token_usage TEXT,
            effective_security TEXT NOT NULL,
            created TEXT NOT NULL, updated TEXT NOT NULL,
            UNIQUE(workspace,adapter_id,request_id));
          CREATE UNIQUE INDEX IF NOT EXISTS ux_agent_conversation_active
            ON agent_runs(conversation) WHERE phase IN ('starting','active');
          CREATE TABLE IF NOT EXISTS agent_interactions (
            id TEXT PRIMARY KEY, run TEXT NOT NULL REFERENCES agent_runs(id),
            native_id TEXT NOT NULL, kind TEXT NOT NULL,
            state TEXT NOT NULL, payload TEXT NOT NULL,
            response TEXT, created TEXT NOT NULL, updated TEXT NOT NULL,
            UNIQUE(run,native_id));
          CREATE TABLE IF NOT EXISTS agent_activities (
            id TEXT PRIMARY KEY, run TEXT NOT NULL REFERENCES agent_runs(id),
            native_id TEXT NOT NULL, kind TEXT NOT NULL,
            status TEXT NOT NULL, payload TEXT NOT NULL,
            created TEXT NOT NULL, updated TEXT NOT NULL,
            UNIQUE(run,native_id));
        """)
            # Bridge schema v4 has no migration framework. Fresh state already
            # includes agent_runs.token_usage above; a v3 database is rejected
            # by Service before this point. A v4 database that is
            # structurally incomplete fails closed here instead of being
            # silently migrated.
            columns = {row["name"] for row in service.db.execute(
                "PRAGMA table_info(agent_runs)")}
            if "token_usage" not in columns:
                raise BridgeError(
                    "Bridge state is incomplete for schema v4; use a fresh state path",
                    "state_schema_incompatible")

    @staticmethod
    def _outcome_event_type(outcome: str | None) -> str | None:
        return {"succeeded": "run_completed", "failed": "run_failed",
                "cancelled": "run_cancelled", "interrupted": "run_interrupted",
                "orphaned": "run_orphaned"}.get(outcome or "")

    def _record_outcome_event_locked(self, run: dict, outcome: str | None) -> None:
        event_type = self._outcome_event_type(outcome)
        if event_type is None:
            return
        self.service.notification_manager.record_run_event_locked(
            run_id=run["id"], workspace_id=run["workspace"], job_id=run["handoff"],
            adapter_id=run["adapter_id"], runtime_type=run["runtime_type"],
            event_type=event_type, occurred_at=_now())

    def configured(self, adapter_id: str) -> bool:
        try:
            self.service.adapter_registry.get(adapter_id)
            return True
        except BridgeError:
            return False

    def adapter(self, adapter_id: str, *, require_enabled: bool = False,
                ws: dict | None = None):
        return self.service.adapter_registry.client(adapter_id, require_enabled=require_enabled,
                                                    workspace=ws)

    def probe_descriptor(self, adapter) -> dict:
        """Return a bounded sanitized descriptor capability summary.

        Never raises and never includes secrets, URLs, paths, profiles,
        prompts, raw remote messages, or provider payloads. Success carries
        only the feature map plus instance/adapter/native versions; failure
        carries only a safe status and sanitized code.
        """
        try:
            descriptor = adapter.descriptor()
        except RuntimeUnsupported:
            return {"status": "unsupported", "code": "runtime_unsupported"}
        except RuntimeUnavailable:
            return {"status": "unavailable", "code": "runtime_unavailable"}
        except BridgeError as exc:
            return {"status": "error",
                    "code": _sanitized_descriptor_code(getattr(exc, "code", ""))}
        except Exception:  # noqa: BLE001 - unexpected probe failure stays a safe code
            return {"status": "error", "code": "descriptor_error"}
        try:
            features = getattr(descriptor, "features", None)
            if not isinstance(features, dict):
                return {"status": "error", "code": "descriptor_error"}
            safe_features: dict[str, int] = {}
            for name, version in list(features.items())[:20]:
                if not isinstance(name, str) or not name or len(name) > 64:
                    continue
                if isinstance(version, bool) or not isinstance(version, int):
                    continue
                if version < 0 or version > 1000:
                    continue
                safe_features[name[:64]] = int(version)
            return {"status": "ok", "features": safe_features,
                    "instance_id": _safe(getattr(descriptor, "instance_id", ""), 200),
                    "adapter_version": _safe(getattr(descriptor, "adapter_version", ""), 80),
                    "native_version": _safe(getattr(descriptor, "native_version", ""), 80)}
        except Exception:  # noqa: BLE001 - malformed descriptor stays a safe code
            return {"status": "error", "code": "descriptor_error"}

    def descriptor_summary(self, adapter_id: str, *, ws: dict | None = None) -> dict:
        """Registry-aware descriptor probe that never raises.

        Disabled adapters report an unprobed disabled status without a
        network call. Enabled adapters return the bounded sanitized probe
        from :meth:`probe_descriptor`.
        """
        try:
            info = self.service.adapter_registry.get(adapter_id)
        except BridgeError as exc:
            return {"status": "error",
                    "code": _sanitized_descriptor_code(getattr(exc, "code", ""))}
        except Exception:  # noqa: BLE001 - registry failure stays a safe code
            return {"status": "error", "code": "descriptor_error"}
        if not info.get("enabled") or not info.get("node_enabled", True):
            return {"status": "disabled", "code": "adapter_disabled"}
        try:
            adapter = self.adapter(adapter_id, ws=ws)
        except BridgeError as exc:
            return {"status": "error",
                    "code": _sanitized_descriptor_code(getattr(exc, "code", ""))}
        except Exception:  # noqa: BLE001 - client construction stays a safe code
            return {"status": "error", "code": "descriptor_error"}
        return self.probe_descriptor(adapter)

    def _bound_run_adapter(self, ws: dict, run: dict):
        """Return the adapter only while the run's captured authority still holds.

        A run is a durable record of the Node and adapter revisions that were
        used to start it.  Every later native request must re-check those
        exact bindings before constructing a proxy or making network I/O.
        """
        if ws.get("node_id") != run["node_id"]:
            raise BridgeError("The workspace Node changed since this run started",
                              "node_changed")
        try:
            node = self.service.node_registry.get(run["node_id"])
        except BridgeError:
            raise BridgeError("The run's Node is no longer available", "node_changed") from None
        if not node["enabled"] or node["revision"] != run["node_revision"]:
            raise BridgeError("The run's Node authority changed", "node_changed")
        try:
            adapter_info = self.service.adapter_registry.get(run["adapter_id"])
        except BridgeError:
            raise BridgeError("The run's adapter is no longer available", "adapter_changed") from None
        if adapter_info["node_id"] != run["node_id"]:
            raise BridgeError("The run's adapter belongs to another Node",
                              "adapter_node_mismatch")
        if (not adapter_info["enabled"] or not adapter_info["node_enabled"]
                or adapter_info["revision"] != run["adapter_revision"]):
            raise BridgeError("The run's adapter authority changed", "adapter_changed")
        return self.adapter(run["adapter_id"], require_enabled=True, ws=ws)

    def diagnostics(self) -> dict:
        rows = {}
        for instance in self.service.adapter_registry.rows():
            adapter_id = instance["id"]
            if not instance["enabled"]:
                rows[adapter_id] = {"configured": True, "enabled": False, "healthy": None,
                                    "runtime_type": instance["runtime_type"]}
                continue
            try:
                adapter = self.adapter(adapter_id)
                descriptor = adapter.descriptor()
                entry: dict = {"configured": True, "enabled": True, "healthy": True,
                                    "runtime_type": instance["runtime_type"],
                                    "protocol": 1, "features": descriptor.features,
                                    "instance": descriptor.instance_id,
                                    "adapter_version": descriptor.adapter_version,
                                    "native_version": descriptor.native_version}
                if descriptor.release is not None:
                    entry["release"] = descriptor.release
                rows[adapter_id] = entry
            except BridgeError as exc:
                rows[adapter_id] = {"configured": True, "enabled": True, "healthy": False,
                                    "runtime_type": instance["runtime_type"],
                                    "detail": _safe(str(exc), 200)}
        return rows

    def profile_catalog(self, adapter_id: str, ws: dict | None = None, *,
                        fresh: bool = False) -> dict:
        adapter = self.adapter(adapter_id, ws=ws)
        return (adapter.profile_catalog(ws["id"], ws["root"], fresh=fresh)
                if ws is not None else adapter.profile_catalog())

    def profiles(self, adapter_id: str, ws: dict | None = None, *,
                 fresh: bool = False) -> list[dict]:
        return self.profile_catalog(adapter_id, ws, fresh=fresh)["profiles"]

    def usage_limits(self, adapter_id: str) -> dict:
        """Read the optional account usage-limits snapshot for one adapter.

        Account quota belongs to the AdapterInstance/account, so no
        workspace authority context is required or accepted. Unsupported
        adapters fail as unsupported, never as unhealthy.
        """
        return self.adapter(adapter_id, require_enabled=True).usage_limits()

    def quota_summary(self, adapter_id: str, *, timeout: float = 8.0) -> str:
        """Best-effort one-line current-quota summary; empty when unavailable.

        Never raises and never touches run state; used only for ephemeral
        notification enrichment at delivery time.
        """
        try:
            limits = self.service.adapter_registry.client(
                adapter_id, require_enabled=True,
                timeout=timeout).usage_limits()
        except Exception:  # noqa: BLE001 - quota enrichment is best-effort only
            return ""
        return codex_quota_summary(limits)

    def set_profile(self, ws: dict, adapter_id: str, profile_id: str) -> dict:
        profiles = self.profiles(adapter_id, ws, fresh=True)
        profile = next((item for item in profiles if item.get("id") == profile_id), None)
        if (profile is None or profile.get("available") is False
                or not isinstance(profile.get("revision"), str)):
            raise BridgeError("Security profile is unavailable", "profile_unavailable")
        with self.service.lock, self.service.db:
            self.service.db.execute(
                "INSERT INTO workspace_routes(workspace,adapter_id,enabled,security_source,profile_id,profile_revision,updated) "
                "VALUES(?,?,0,'profile',?,?,?) ON CONFLICT(workspace,adapter_id) DO UPDATE SET "
                "security_source='profile',profile_id=excluded.profile_id,"
                "profile_revision=excluded.profile_revision,updated=excluded.updated",
                (ws["id"], adapter_id, profile_id, profile["revision"], _now()))
        return {"adapter_id": adapter_id, "profile": profile_id,
                "revision": profile["revision"]}

    def set_runtime_config(self, ws: dict, adapter_id: str) -> dict:
        if self.service.adapter_registry.get(adapter_id)["runtime_type"] != "codex":
            raise BridgeError("Runtime config security is supported only by Codex adapters",
                              "runtime_config_unavailable")
        catalog = self.profile_catalog(adapter_id, ws, fresh=True)
        native = catalog.get("runtimeConfig")
        if (not isinstance(native, dict) or native.get("supported") is not True
                or native.get("available") is not True
                or not isinstance(native.get("revision"), str)
                or not native["revision"] or len(native["revision"]) > 100):
            raise BridgeError("Runtime config security is unavailable",
                              "runtime_config_unavailable")
        with self.service.lock, self.service.db:
            self.service.db.execute(
                "INSERT INTO workspace_routes(workspace,adapter_id,enabled,security_source,profile_id,profile_revision,updated) "
                "VALUES(?,?,0,'runtime-config','',?,?) ON CONFLICT(workspace,adapter_id) DO UPDATE SET "
                "security_source='runtime-config',profile_id='',profile_revision=excluded.profile_revision,"
                "updated=excluded.updated",
                (ws["id"], adapter_id, native["revision"], _now()))
        return {"adapter_id": adapter_id, "security_binding": {
            "source": "runtime-config", "revision": native["revision"],
            "resolved_summary": native.get("resolvedSummary")}}

    def set_security_binding(self, ws: dict, adapter_id: str, source: str,
                             profile_id: str | None = None) -> dict:
        if source == "profile" and isinstance(profile_id, str) and profile_id:
            return self.set_profile(ws, adapter_id, profile_id)
        if source == "runtime-config" and profile_id is None:
            return self.set_runtime_config(ws, adapter_id)
        raise BridgeError("Invalid runtime security binding", "invalid_arguments")

    def save_profile(self, adapter_id: str, profile_id: str, config: dict,
                     expected_revision: str | None) -> dict:
        if (not isinstance(profile_id, str)
                or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", profile_id)
                or not isinstance(config, dict)
                or len(json.dumps(config)) > 32768
                or (expected_revision is not None
                    and (not isinstance(expected_revision, str)
                         or len(expected_revision) > 100))):
            raise BridgeError("Invalid security profile", "invalid_arguments")
        adapter = self.adapter(adapter_id)
        saved = adapter.save_profile(
            profile_id, config, expected_revision)
        if saved.get("id") != profile_id or not isinstance(saved.get("revision"), str):
            raise BridgeError("Runtime returned an invalid security profile",
                              "binding_mismatch")
        # A deliberate profile edit refreshes assignments to the new opaque
        # revision so the recorded binding stays tidy. Stored revisions are
        # informational only; drift never blocks routes or runs.
        with self.service.lock:
            assigned = [row["workspace"] for row in self.service.db.execute(
                "SELECT workspace FROM workspace_routes WHERE adapter_id=? AND security_source='profile' AND profile_id=? "
                "ORDER BY workspace", (adapter_id, profile_id))]
        for workspace_id in assigned[:100]:
            try:
                ws = self.service.workspace(workspace_id)
                current = next((item for item in self.profiles(
                    adapter_id, ws, fresh=True)
                    if item.get("id") == profile_id
                    and item.get("available") is not False), None)
            except Exception:  # noqa: BLE001 - a failed refresh keeps the old recorded revision
                current = None
            if current and isinstance(current.get("revision"), str):
                with self.service.lock, self.service.db:
                    self.service.db.execute(
                        "UPDATE workspace_routes SET profile_revision=?,updated=? "
                        "WHERE workspace=? AND adapter_id=? AND profile_id=?",
                        (current["revision"], _now(), workspace_id, adapter_id, profile_id))
        self.service.event(None, "save_adapter_profile", "saved")
        return saved

    def delete_profile(self, adapter_id: str, profile_id: str) -> dict:
        if (not isinstance(profile_id, str)
                or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", profile_id)):
            raise BridgeError("Invalid security profile ID", "invalid_arguments")
        with self.service.lock:
            assigned = self.service.db.execute(
                "SELECT 1 FROM workspace_routes WHERE adapter_id=? AND security_source='profile' AND profile_id=? LIMIT 1",
                (adapter_id, profile_id)).fetchone()
        if assigned:
            raise BridgeError("Assign another profile before deleting this one", "conflict")
        result = self.adapter(adapter_id).delete_profile(profile_id)
        self.service.event(None, "delete_adapter_profile", "deleted")
        return result

    def security_binding(self, ws: dict, adapter_id: str) -> dict:
        with self.service.lock:
            row = self.service.db.execute(
                "SELECT profile_id,profile_revision,security_source FROM workspace_routes WHERE workspace=? AND adapter_id=?",
                (ws["id"], adapter_id)).fetchone()
        if row is None:
            raise BridgeError("Workspace route security binding is not configured",
                              "profile_unconfigured")
        source = row["security_source"]
        if source == "runtime-config":
            native = self.profile_catalog(adapter_id, ws, fresh=True).get("runtimeConfig")
            if (not isinstance(native, dict) or native.get("supported") is not True
                    or native.get("available") is not True):
                raise BridgeError("Runtime config security is unavailable",
                                  "runtime_config_unavailable")
            revision = native.get("revision")
            if not isinstance(revision, str) or not revision or len(revision) > 100:
                raise BridgeError("Runtime config security is unavailable",
                                  "runtime_config_unavailable")
            return {"source": "runtime-config", "revision": revision,
                    "resolvedSummary": native.get("resolvedSummary"),
                    "status": native.get("status", "ready")}
        if source != "profile":
            raise BridgeError("Workspace runtime security binding is invalid",
                              "profile_unconfigured")
        available = self.profiles(adapter_id, ws, fresh=True)
        # Revision drift never blocks: adapter redeploys rotate opaque
        # profile revisions, so only a missing or unavailable profile fails.
        # Fresh runs must use the LIVE observed revision, not the stale
        # stored workspace_routes.profile_revision. The stored value is kept
        # as last-bound evidence (bound_revision) while the live catalog
        # revision is authoritative (observed_revision).
        live = next((item for item in available
                     if item.get("id") == row["profile_id"]
                     and item.get("available") is not False
                     and isinstance(item.get("revision"), str)
                     and item.get("revision")), None)
        if live is None:
            raise BridgeError("Workspace runtime security profile is unavailable",
                              "profile_unavailable")
        stored_revision = row["profile_revision"]
        observed_revision = live["revision"]
        return {"source": "profile", "profile": {
            "id": row["profile_id"], "revision": observed_revision},
            "bound_revision": stored_revision,
            "observed_revision": observed_revision}

    def profile(self, ws: dict, adapter_id: str) -> dict:
        binding = self.security_binding(ws, adapter_id)
        if binding["source"] != "profile":
            raise BridgeError("Workspace runtime uses native runtime config security",
                              "profile_unconfigured")
        return binding["profile"]

    def model_policy(self, adapter_id: str) -> dict:
        self.adapter(adapter_id)
        with self.service.lock:
            row = self.service.db.execute(
                "SELECT enabled_models_json,default_model,reasoning_defaults_json "
                "FROM adapter_model_policies WHERE adapter_id=?", (adapter_id,)).fetchone()
        if not row:
            return {"configured": False, "enabled": [], "default": None,
                    "reasoning_defaults": {}}
        try:
            enabled = json.loads(row["enabled_models_json"])
            default = row["default_model"]
            reasoning_defaults = json.loads(row["reasoning_defaults_json"])
        except ValueError:
            enabled, default, reasoning_defaults = None, None, None
        if (not isinstance(enabled, list) or not isinstance(default, str)
                or default not in enabled):
            return {"configured": False, "enabled": [], "default": None,
                    "reasoning_defaults": {}}
        if (not isinstance(reasoning_defaults, dict) or len(reasoning_defaults) > 200
                or any(not isinstance(selector, str) or not selector or len(selector) > 260
                       or not isinstance(effort, str) or not effort or len(effort) > 40
                       for selector, effort in reasoning_defaults.items())):
            return {"configured": False, "enabled": [], "default": None,
                    "reasoning_defaults": {}}
        return {"configured": True, "enabled": enabled,
                "default": default,
                "reasoning_defaults": reasoning_defaults}

    def models(self, ws: dict, adapter_id: str, query: str = "", limit: int = 25) -> dict:
        rows = self.adapter(adapter_id, ws=ws).models(ws["id"])
        policy = self.model_policy(adapter_id)
        selected = []
        for model in rows:
            selector = model.get("selector")
            if not isinstance(selector, str):
                continue
            if query and query.casefold() not in (selector + " " +
                    str(model.get("displayName") or "")).casefold():
                continue
            selected.append({**model, "enabled": (selector in policy["enabled"]
                                 if policy["configured"] else True),
                             "policy_enabled": (selector in policy["enabled"]
                                                if policy["configured"] else None),
                             "policy_default": selector == policy["default"]})
        return {"adapter_id": adapter_id, "workspace_id": ws["id"],
                "models": selected[:max(1, min(limit, 100))], "policy": policy,
                "policy_restricted": bool(policy["configured"]),
                "count": len(selected)}

    def set_model_policy(self, adapter_id: str, enabled: list[str], default: str,
                         ws: dict | None = None,
                         reasoning_defaults: dict[str, str] | None = None) -> dict:
        if not isinstance(enabled, list) or not enabled or len(enabled) > 200:
            raise BridgeError("Model policy requires 1..200 selectors", "invalid_arguments")
        if any(not isinstance(item, str) or not item or len(item) > 260
               for item in enabled) or len(set(enabled)) != len(enabled):
            raise BridgeError("Invalid model selectors", "invalid_arguments")
        if default not in enabled:
            raise BridgeError("Default model must be enabled", "invalid_arguments")
        rows = self.adapter(adapter_id, ws=ws).models(ws["id"] if ws else "")
        available = {item["selector"]: item for item in rows}
        if not set(enabled).issubset(available):
            raise BridgeError("An enabled model is unavailable", "model_unavailable")
        if reasoning_defaults is None:
            reasoning_defaults = {}
        if (not isinstance(reasoning_defaults, dict) or len(reasoning_defaults) > 200
                or any(not isinstance(selector, str) or not selector or len(selector) > 260
                       or not isinstance(effort, str) or not effort or len(effort) > 40
                       for selector, effort in reasoning_defaults.items())):
            raise BridgeError("Invalid per-model reasoning defaults", "invalid_arguments")
        for selector, effort in reasoning_defaults.items():
            model = available.get(selector)
            if model is None:
                raise BridgeError("A reasoning default refers to an unavailable model",
                                  "model_unavailable")
            options = model.get("reasoningOptions")
            if not isinstance(options, list) or effort not in options:
                raise BridgeError(f"Reasoning level {effort!r} is not supported by {selector!r}",
                                  "reasoning_unavailable")
        with self.service.lock, self.service.db:
            self.service.db.execute(
                "INSERT INTO adapter_model_policies(adapter_id,enabled_models_json,default_model,reasoning_defaults_json,updated) "
                "VALUES(?,?,?,?,?) ON CONFLICT(adapter_id) DO UPDATE SET "
                "enabled_models_json=excluded.enabled_models_json,default_model=excluded.default_model,"
                "reasoning_defaults_json=excluded.reasoning_defaults_json,updated=excluded.updated",
                (adapter_id, json.dumps(enabled), default, json.dumps(reasoning_defaults), _now()))
        return self.model_policy(adapter_id)

    def _select_model(self, ws: dict, adapter_id: str, requested: str | None) -> str:
        """Resolve the run model selector.

        A configured adapter policy keeps its exact allowlist/default
        semantics. With no Bridge policy the models are unrestricted by
        Bridge governance: an omitted request sends no selector so the
        adapter/native runtime chooses its own default (represented durably
        by an empty selector), and an explicit request only has to name a
        model in the live adapter catalog.
        """
        policy = self.model_policy(adapter_id)
        if not policy["configured"]:
            if requested is None:
                return ""
            available = {item["selector"] for item in self.adapter(
                adapter_id, ws=ws).models(ws["id"]) if isinstance(item.get("selector"), str)}
            if requested not in available:
                raise BridgeError("Model is unavailable", "model_unavailable")
            return requested
        selector = requested if requested is not None else policy["default"]
        if selector not in policy["enabled"]:
            raise BridgeError("Model is not enabled", "model_not_enabled")
        available = {item["selector"] for item in self.adapter(adapter_id, ws=ws).models(ws["id"])}
        if selector not in available:
            raise BridgeError("Model is unavailable", "model_unavailable")
        return selector

    def _select_reasoning(self, ws: dict, adapter_id: str, selector: str) -> str | None:
        policy = self.model_policy(adapter_id)
        effort = policy["reasoning_defaults"].get(selector)
        # Reasoning defaults are configured-policy features only; a native
        # default selector (empty) never applies a Bridge reasoning default.
        if effort is None or not selector:
            return None
        row = next((item for item in self.adapter(adapter_id, ws=ws).models(ws["id"])
                    if item.get("selector") == selector), None)
        if row is None:
            raise BridgeError("Model is unavailable", "model_unavailable")
        options = row.get("reasoningOptions")
        if not isinstance(options, list) or effort not in options:
            raise BridgeError("The configured reasoning level is no longer supported by this model",
                              "reasoning_unavailable")
        return effort

    def _prompt(self, ws: dict, job: dict) -> str:
        directory = str(ws["root"])
        folder = f"{directory}/{HANDOFF}/jobs/{job['id']}"
        return (
            f"Work only in this project: {json.dumps(directory)}. "
            f"Read {json.dumps(folder + '/TASK.md')}, CONTEXT.md and ACCEPTANCE.md "
            "in the same folder. Preserve pre-existing edits and unrelated work. "
            "Follow the plan and satisfy every acceptance criterion. Run the agreed checks. "
            "Do not access secrets unless the handoff explicitly requires and authorizes it. "
            "Do not make unrelated destructive actions, expand scope, or edit the handoff "
            "documents unless the handoff explicitly permits them. "
            "Stop and report a blocker when assumptions fail or the work exceeds "
            "scope. Report changed files, exact checks "
            "and outcomes, and remaining risks."
        )

    def _run_row(self, ws: dict, run_id: str) -> dict:
        with self.service.lock:
            row = self.service.db.execute(
                "SELECT * FROM agent_runs WHERE id=? AND workspace=?",
                (run_id, ws["id"])).fetchone()
        if row is None:
            raise BridgeError("Run not found in this workspace", "not_found")
        return dict(row)

    def has_run(self, run_id: str) -> bool:
        with self.service.lock:
            return self.service.db.execute(
                "SELECT 1 FROM agent_runs WHERE id=?", (run_id,)).fetchone() is not None

    def _conversation(self, run: dict) -> dict:
        with self.service.lock:
            row = self.service.db.execute(
                "SELECT * FROM agent_conversations WHERE id=?",
                (run["conversation"],)).fetchone()
        if row is None:
            raise BridgeError("Conversation record is missing", "internal_error")
        return dict(row)

    def start(self, ws: dict, adapter_id: str, job_id: str, request_id: str,
              model: str | None = None, parent_run_id: str | None = None,
              continue_from_run_id: str | None = None) -> dict:
        # Refresh the authoritative Node inventory and prove the Node is
        # reachable before resolving an adapter or making any native runtime
        # call.  A stale Bridge cache must never turn a Node outage into a
        # local or cross-Node execution attempt.
        self.service.node_registry.refresh_adapters(ws["node_id"])
        self.service.node_registry.client(ws["node_id"], timeout=5).status()
        self.service.require_workspace_route(ws, adapter_id)
        job = self.service.job(ws, job_id)
        if job["state"] != "prepared":
            raise BridgeError("Only a prepared handoff may run", "conflict")
        if continue_from_run_id is not None:
            if parent_run_id is None:
                parent_run_id = continue_from_run_id
            elif parent_run_id != continue_from_run_id:
                raise BridgeError("Continuation source and parent run differ",
                                  "continuation_parent_mismatch")
        with self.service.lock:
            existing = self.service.db.execute(
                "SELECT * FROM agent_runs WHERE workspace=? AND adapter_id=? AND request_id=?",
                (ws["id"], adapter_id, request_id)).fetchone()
        if existing:
            if (existing["adapter_id"] != adapter_id or existing["handoff"] != job_id
                    or existing["continue_from"] != continue_from_run_id
                    or existing["parent_run"] != parent_run_id
                    or (model is not None and existing["model"] != model)):
                raise BridgeError("request_id has different content", "conflict")
            return {**self._public(dict(existing)), "idempotent": True}
        adapter_info = self.service.adapter_registry.get(adapter_id, require_enabled=True)
        if adapter_info["node_id"] != ws["node_id"]:
            raise BridgeError("Adapter belongs to another Node", "adapter_node_mismatch")
        node_info = self.service.node_registry.get(ws["node_id"])
        adapter = self.adapter(adapter_id, require_enabled=True, ws=ws)
        if parent_run_id:
            self._run_row(ws, parent_run_id)
        selector = self._select_model(ws, adapter_id, model)
        reasoning = self._select_reasoning(ws, adapter_id, selector)
        binding = self.security_binding(ws, adapter_id)
        request_hash = digest(json.dumps({"job": job_id, "adapter_id": adapter_id,
                                          "model": selector,
                                          "parent": parent_run_id or "",
                                          "continue": continue_from_run_id or ""},
                                         sort_keys=True).encode())
        conversation = None
        prior_conversation_id = None
        replacement_reason = None
        rebind_old = None
        rebind_new = None
        rebind_conversation_id = None
        if continue_from_run_id:
            source = self._run_row(ws, continue_from_run_id)
            if source["adapter_id"] != adapter_id or source["phase"] != "terminal":
                raise BridgeError("Continuation source is unavailable",
                                  "continuation_unavailable")
            # Any terminal outcome may continue (succeeded, failed,
            # cancelled, interrupted): ownership is proven below against the
            # live adapter, not by the historical outcome, model, or
            # recorded Node/adapter revisions.
            conversation = self._conversation(source)
            if conversation["node_id"] != node_info["id"]:
                raise BridgeError("The conversation belongs to another Node",
                                  "node_changed")
            if (conversation["adapter_id"] != adapter_id
                    or conversation["runtime_type"] != adapter_info["runtime_type"]):
                raise BridgeError("Adapter connection changed since this conversation was created",
                                  "adapter_changed")
            stored_source = conversation.get("source", "profile")
            if binding["source"] != stored_source:
                raise BridgeError("Workspace security source changed; a fresh conversation is required",
                                  "continuation_security_source_changed")
            # Bounded security-rebind transition. When a named-profile ID
            # or revision changes, the existing conversation is rebound at an
            # idle boundary before the new prompt is sent. The activity below
            # records only identifiers and revisions.
            if binding["source"] == "profile":
                desired_id = binding["profile"]["id"]
                desired_revision = binding["profile"]["revision"]
                stored_id = conversation.get("profile", "")
                stored_revision = conversation.get("revision", "")
                if stored_id != desired_id or stored_revision != desired_revision:
                    probe = self.probe_descriptor(adapter)
                    if probe.get("status") == "ok":
                        if probe.get("features", {}).get("securityRebind") != 1:
                            raise BridgeError("Workspace security profile changed and the adapter does not support idle security rebind",
                                              "continuation_security_rebind_unsupported")
                    else:
                        # Descriptor transport/validation failure is distinct
                        # from a genuine missing capability. Fail closed before
                        # any prompt or rebind with a sanitized generic message.
                        raise BridgeError("Workspace security profile changed and the adapter descriptor is currently unavailable",
                                          "continuation_descriptor_unavailable")
                    # The owned revision may already be stale, so a direct
                    # conversation read would fail closed with profile_mismatch.
                    # The rebind operation itself proves idle (busy, pending,
                    # or unconfirmed states fail before any prompt) and only
                    # then is the Bridge record updated.
                    desired_binding = {"source": "profile", "profile": {
                        "id": desired_id, "revision": desired_revision}}
                    try:
                        rebound = adapter.rebind_conversation(
                            conversation["native_id"], desired_binding)
                    except BridgeError as exc:
                        # Strict client-side validation already fails closed;
                        # preserve its code without sending any prompt.
                        raise
                    except Exception as exc:
                        code = getattr(exc, "code", "")
                        if code in {"conversation_busy", "binding_mismatch",
                                    "security_rebind_unavailable", "profile_mismatch",
                                    "profile_unavailable", "not_found"}:
                            raise BridgeError(str(exc) or "Security rebind was rejected",
                                              code or "binding_mismatch") from None
                        raise
                    # Narrow success: same runtime conversation, idle, and the
                    # exact requested profile binding. Never fall back to a
                    # blank conversation.
                    rebound_binding = rebound.get("securityBinding")
                    if rebound_binding is None and isinstance(rebound.get("securityProfile"), dict):
                        rebound_binding = {"source": "profile",
                                           "profile": rebound.get("securityProfile")}
                    if (rebound.get("id") != conversation["native_id"]
                            or rebound.get("status") != "idle"
                            or not isinstance(rebound_binding, dict)
                            or rebound_binding.get("source") != "profile"
                            or rebound_binding.get("profile") != {
                                "id": desired_id, "revision": desired_revision}):
                        raise BridgeError("Runtime security rebind did not prove the requested binding",
                                          "binding_mismatch")
                    rebind_old = {"source": "profile", "profile_id": stored_id,
                                  "revision": stored_revision}
                    rebind_new = {"source": "profile", "profile_id": desired_id,
                                  "revision": desired_revision}
                    rebind_conversation_id = conversation["id"]
                    with self.service.lock, self.service.db:
                        self.service.db.execute(
                            "UPDATE agent_conversations SET profile=?,revision=? WHERE id=?",
                            (desired_id, desired_revision, conversation["id"]))
                    conversation = self._conversation(source)
                    # Validated `rebound` already proves same conversation ID,
                    # idle status, and exact requested binding. Do not issue a
                    # redundant post-rebind read that would widen an unaudited
                    # successful-transition window. The rebind itself proves
                    # current runtime ownership, so only the conversation's
                    # current Node/adapter metadata is refreshed; historical
                    # run evidence stays immutable.
                    _refresh_metadata = {}
                    if conversation["node_revision"] != node_info["revision"]:
                        _refresh_metadata["node_revision"] = node_info["revision"]
                    if conversation["adapter_revision"] != adapter_info["revision"]:
                        _refresh_metadata["adapter_revision"] = adapter_info["revision"]
                    try:
                        _instance_id = adapter.descriptor().instance_id
                    except Exception:
                        _instance_id = None
                    if _instance_id and conversation["instance_id"] != _instance_id:
                        _refresh_metadata["instance_id"] = _instance_id
                    if _refresh_metadata:
                        assignments = ",".join(
                            f"{key}=?" for key in _refresh_metadata)
                        with self.service.lock, self.service.db:
                            self.service.db.execute(
                                f"UPDATE agent_conversations SET {assignments} WHERE id=?",
                                (*_refresh_metadata.values(), conversation["id"]))
                        conversation = {**conversation, **_refresh_metadata}
            # Both runtime-config here: opaque native revision drift is
            # resolved by the adapter below (thread refresh or explicit
            # runtime-owned replacement), not by a Bridge revision check.
            if rebind_old is None:
                # Current-runtime ownership proof: the stored native
                # conversation must exist on the live same-Node adapter,
                # belong to this workspace, and be idle before any prompt.
                # Missing, foreign, mismatched, or busy conversations fail
                # closed here and no prompt is sent.
                try:
                    native_conversation = adapter.conversation(
                        conversation["native_id"])
                except Exception as exc:
                    code = getattr(exc, "code", "")
                    if code in {"not_found", "binding_mismatch", "profile_mismatch",
                                "profile_unavailable", "conversation_unavailable"}:
                        raise BridgeError(
                            "The runtime conversation is no longer owned by this adapter",
                            "conversation_unavailable") from None
                    raise
                if native_conversation.get("workspaceId") != ws["id"]:
                    raise BridgeError(
                        "The runtime conversation is not owned by this workspace",
                        "conversation_unavailable")
                if native_conversation.get("status") != "idle":
                    raise BridgeError("Conversation is not idle", "conversation_busy")
                # Benign Node/adapter revision drift is proven safe: the live
                # adapter still owns the idle conversation. Refresh only the
                # conversation's current Node/adapter metadata; historical run
                # effective_security and revision evidence stay immutable.
                metadata: dict[str, str] = {}
                if conversation["node_revision"] != node_info["revision"]:
                    metadata["node_revision"] = node_info["revision"]
                if conversation["adapter_revision"] != adapter_info["revision"]:
                    metadata["adapter_revision"] = adapter_info["revision"]
                continuation_descriptor = adapter.descriptor()
                if conversation["instance_id"] != continuation_descriptor.instance_id:
                    metadata["instance_id"] = continuation_descriptor.instance_id
                if metadata:
                    assignments = ",".join(f"{key}=?" for key in metadata)
                    with self.service.lock, self.service.db:
                        self.service.db.execute(
                            f"UPDATE agent_conversations SET {assignments} WHERE id=?",
                            (*metadata.values(), conversation["id"]))
                    conversation = {**conversation, **metadata}
        if conversation is None:
            descriptor = adapter.descriptor()
            native_conversation = adapter.create_conversation({
                "workspaceId": ws["id"], "directory": str(ws["root"]),
                "securityBinding": binding,
                **({"securityProfile": binding["profile"]}
                   if binding["source"] == "profile" else {}),
            })
            native_binding = native_conversation.get("securityBinding")
            if binding["source"] == "profile" and not isinstance(native_binding, dict):
                legacy = native_conversation.get("securityProfile")
                if isinstance(legacy, dict):
                    native_binding = {"source": "profile", "profile": legacy}
            binding_matches = (isinstance(native_binding, dict)
                               and native_binding.get("source") == binding["source"])
            if binding["source"] == "profile":
                binding_matches = (binding_matches
                                   and native_binding.get("profile") == binding["profile"])
            else:
                binding_matches = (binding_matches
                                   and isinstance(native_binding.get("revision"), str)
                                   and bool(native_binding.get("revision"))
                                   and isinstance(native_binding.get("resolvedSummary"), dict))
            if (native_conversation.get("workspaceId") != ws["id"]
                    or not binding_matches
                    or not isinstance(native_conversation.get("id"), str)
                    or not native_conversation["id"]):
                raise BridgeError("Runtime conversation binding is invalid",
                                  "binding_mismatch")
            summary = (native_binding.get("resolvedSummary")
                       if binding["source"] == "runtime-config" else None)
            bound_revision = (binding["profile"]["revision"]
                              if binding["source"] == "profile"
                              else native_binding["revision"])
            conversation = {"id": _id("conv_"), "workspace": ws["id"],
                            "node_id": node_info["id"], "node_revision": node_info["revision"],
                            "adapter_id": adapter_id, "runtime_type": adapter_info["runtime_type"],
                            "adapter_revision": adapter_info["revision"],
                            "native_id": native_conversation["id"],
                            "profile": binding.get("profile", {}).get("id", ""),
                            "revision": bound_revision,
                            "instance_id": descriptor.instance_id, "created": _now(),
                            "source": binding["source"],
                            "security_snapshot": (json.dumps(summary, sort_keys=True)
                                                  if isinstance(summary, dict) else None),
                            "permission_revision": (native_binding.get("permissionRevision", "")
                                                    if isinstance(native_binding, dict) else ""),
                            "approval_revision": (native_binding.get("approvalRevision", "")
                                                  if isinstance(native_binding, dict) else ""),
                            "replacement_reason": replacement_reason}
            with self.service.lock, self.service.db:
                self.service.db.execute(
                    "INSERT INTO agent_conversations"
                    "(id,workspace,node_id,node_revision,adapter_id,runtime_type,adapter_revision,native_id,profile,revision,instance_id,created,source,"
                    "security_snapshot,permission_revision,approval_revision,replacement_reason) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (conversation["id"], conversation["workspace"], conversation["node_id"],
                     conversation["node_revision"], conversation["adapter_id"],
                     conversation["runtime_type"], conversation["adapter_revision"],
                     conversation["native_id"], conversation["profile"], conversation["revision"],
                     conversation["instance_id"], conversation["created"], conversation["source"],
                     conversation["security_snapshot"], conversation["permission_revision"],
                     conversation["approval_revision"], conversation["replacement_reason"]))
        bridge_run_id = _id("run_")
        timestamp = _now()
        effective_security = _effective_security(binding, node_id=node_info["id"],
            node_revision=node_info["revision"], adapter_id=adapter_id,
            adapter_revision=adapter_info["revision"])
        with self.service.lock, self.service.db:
            self.service.db.execute(
                "INSERT INTO agent_runs(id,conversation,workspace,node_id,node_revision,adapter_id,runtime_type,adapter_revision,handoff,request_id,"
                "request_hash,continue_from,parent_run,native_id,model,reasoning,phase,active_state,outcome,result,error,token_usage,"
                "effective_security,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (bridge_run_id, conversation["id"], ws["id"], node_info["id"],
                 node_info["revision"], adapter_id, adapter_info["runtime_type"],
                 adapter_info["revision"], job_id,
                 request_id, request_hash, continue_from_run_id, parent_run_id,
                 None, selector, reasoning, "starting", None,
                 None, "", "", None, json.dumps(effective_security, sort_keys=True), timestamp, timestamp))
        if rebind_old is not None and rebind_new is not None:
            # The runtime security transition is a real state mutation
            # independent of whether the next prompt succeeds. Persist it
            # BEFORE calling start_run so a rejected/failed start still
            # leaves the failed Bridge run auditable. Bounded identifiers
            # and revisions only; no config, prompt, paths, or secrets.
            rebind_payload = _safe_payload({
                "old_source": rebind_old.get("source"),
                "old_profile_id": rebind_old.get("profile_id"),
                "old_revision": rebind_old.get("revision"),
                "new_source": rebind_new.get("source"),
                "new_profile_id": rebind_new.get("profile_id"),
                "new_revision": rebind_new.get("revision"),
                "conversation_id": rebind_conversation_id or conversation["id"],
            }, 2000)
            with self.service.lock, self.service.db:
                self.service.db.execute(
                    "INSERT INTO agent_activities(id,run,native_id,kind,status,payload,created,updated) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (_id("act_"), bridge_run_id, "security-rebind:" + bridge_run_id,
                     "security_binding_rebound", "completed", rebind_payload, _now(), _now()))
        try:
            run_payload = {
                "input": [{"type": "text", "text": self._prompt(ws, job)}],
                "clientRunId": bridge_run_id,
            }
            # An empty selector means no Bridge model policy and no explicit
            # request: send no selector so the adapter/native runtime chooses
            # its own default.
            if selector:
                run_payload["model"] = selector
            if reasoning is not None:
                run_payload["reasoning"] = reasoning
            native_run = validate_run_state(adapter.start_run(
                conversation["native_id"], run_payload))
            native_security = native_run.get("securityBinding")
            runtime_replacement = (native_security.get("replacementReason")
                                   if isinstance(native_security, dict) else None)
            replacement_native_id = native_run.get("conversationId")
            if (native_run.get("clientRunId") != bridge_run_id
                    or (replacement_native_id != conversation["native_id"]
                        and (binding["source"] != "runtime-config"
                             or not isinstance(runtime_replacement, str)
                             or not runtime_replacement
                             or not isinstance(native_security.get(
                                 "replacedConversationId"), str)))):
                raise BridgeError("Runtime run binding is invalid", "binding_mismatch")
            if binding["source"] == "runtime-config":
                if (not isinstance(native_security, dict)
                        or native_security.get("source") != "runtime-config"
                        or not isinstance(native_security.get("revision"), str)
                        or not isinstance(native_security.get("resolvedSummary"), dict)):
                    raise BridgeError("Runtime run security state is invalid", "binding_mismatch")
                new_summary = native_security["resolvedSummary"]
                new_revision = native_security["revision"]
                new_permission_revision = native_security.get("permissionRevision", "")
                new_approval_revision = native_security.get("approvalRevision", "")
                if replacement_native_id != conversation["native_id"]:
                    if native_security.get("replacedConversationId") != conversation["native_id"]:
                        raise BridgeError("Runtime conversation replacement is invalid",
                                          "binding_mismatch")
                    current_native = adapter.conversation(replacement_native_id)
                    if (current_native.get("workspaceId") != ws["id"]
                            or current_native.get("status") not in {"idle", "active", "notLoaded"}):
                        raise BridgeError("Replacement conversation binding is invalid",
                                          "binding_mismatch")
                    prior_conversation_id = conversation["id"]
                    descriptor = adapter.descriptor()
                    conversation = {
                        "id": _id("conv_"), "workspace": ws["id"],
                        "node_id": node_info["id"], "node_revision": node_info["revision"],
                        "adapter_id": adapter_id, "runtime_type": adapter_info["runtime_type"],
                        "adapter_revision": adapter_info["revision"],
                        "native_id": replacement_native_id,
                        "profile": "", "revision": new_revision,
                        "instance_id": descriptor.instance_id, "created": _now(),
                        "source": "runtime-config",
                        "security_snapshot": json.dumps(new_summary, sort_keys=True),
                        "permission_revision": new_permission_revision,
                        "approval_revision": new_approval_revision,
                        "replacement_reason": runtime_replacement,
                    }
                    replacement_reason = runtime_replacement
                    with self.service.lock, self.service.db:
                        self.service.db.execute(
                            "INSERT INTO agent_conversations"
                            "(id,workspace,node_id,node_revision,adapter_id,runtime_type,adapter_revision,native_id,profile,revision,instance_id,created,source,"
                            "security_snapshot,permission_revision,approval_revision,replacement_reason) "
                            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (conversation["id"], conversation["workspace"], conversation["node_id"],
                             conversation["node_revision"], conversation["adapter_id"],
                             conversation["runtime_type"], conversation["adapter_revision"],
                             conversation["native_id"], conversation["profile"], conversation["revision"],
                             conversation["instance_id"], conversation["created"], conversation["source"],
                             conversation["security_snapshot"], conversation["permission_revision"],
                             conversation["approval_revision"], conversation["replacement_reason"]))
                        self.service.db.execute(
                            "UPDATE agent_runs SET conversation=? WHERE id=?",
                            (conversation["id"], bridge_run_id))
                else:
                    conversation["revision"] = new_revision
                    conversation["security_snapshot"] = json.dumps(new_summary, sort_keys=True)
                    conversation["permission_revision"] = new_permission_revision
                    conversation["approval_revision"] = new_approval_revision
                    with self.service.lock, self.service.db:
                        self.service.db.execute(
                            "UPDATE agent_conversations SET revision=?,security_snapshot=?,"
                            "permission_revision=?,approval_revision=? WHERE id=?",
                            (new_revision, conversation["security_snapshot"],
                             new_permission_revision, new_approval_revision, conversation["id"]))
                # The native adapter's confirmed binding is the security that
                # this run actually used.  Persist that bounded observation on
                # the run itself; later runtime-config drift must not rewrite
                # historical evidence.
                effective_security = _effective_security(
                    {"source": "runtime-config", "revision": new_revision,
                     "resolvedSummary": new_summary},
                    node_id=node_info["id"], node_revision=node_info["revision"],
                    adapter_id=adapter_id, adapter_revision=adapter_info["revision"])
                with self.service.lock, self.service.db:
                    self.service.db.execute(
                        "UPDATE agent_runs SET effective_security=?,updated=? WHERE id=?",
                        (json.dumps(effective_security, sort_keys=True), _now(), bridge_run_id))
        except RuntimeRejected as exc:
            with self.service.lock, self.service.db:
                self.service.db.execute(
                    "UPDATE agent_runs SET phase='terminal',outcome='failed',error=?,updated=? "
                    "WHERE id=?", (_safe(str(exc), 300), _now(), bridge_run_id))
                self._record_outcome_event_locked({
                    "id": bridge_run_id, "workspace": ws["id"], "handoff": job_id,
                    "adapter_id": adapter_id, "runtime_type": adapter_info["runtime_type"]}, "failed")
            raise
        if replacement_reason is not None:
            activity_id = _id("act_")
            payload = _safe_payload({
                "reason": replacement_reason,
                "previous_conversation_id": prior_conversation_id,
                "conversation_id": conversation["id"],
            }, 2000)
            with self.service.lock, self.service.db:
                self.service.db.execute(
                    "INSERT INTO agent_activities(id,run,native_id,kind,status,payload,created,updated) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (activity_id, bridge_run_id, "security-replacement:" + bridge_run_id,
                     "security_conversation_replaced", "completed", payload, _now(), _now()))
        initial_usage = _normalize_bridge_usage(native_run.get("usage"))
        initial_usage_json = (json.dumps(initial_usage, sort_keys=True)
                              if initial_usage is not None else None)
        with self.service.lock, self.service.db:
            self.service.db.execute(
                "UPDATE agent_runs SET native_id=?,phase=?,active_state=?,outcome=?,"
                "result=?,error=?,token_usage=?,updated=? WHERE id=?",
                (native_run["id"], native_run["phase"], native_run.get("activeState"),
                 native_run.get("outcome"), _safe(native_run.get("result")),
                 _safe(native_run.get("error"), 300), initial_usage_json,
                 _now(), bridge_run_id))
            if native_run["phase"] == "terminal":
                self._record_outcome_event_locked({
                    "id": bridge_run_id, "workspace": ws["id"], "handoff": job_id,
                    "adapter_id": adapter_id, "runtime_type": adapter_info["runtime_type"]},
                    native_run.get("outcome"))
        return self.read(ws, bridge_run_id, sync=False)

    def _public(self, run: dict) -> dict:
        try:
            adapter_name = self.service.adapter_registry.get(run["adapter_id"])["name"]
        except BridgeError:
            adapter_name = ""
        try:
            effective_security = json.loads(run["effective_security"])
        except (TypeError, ValueError, KeyError):
            effective_security = {"status": "unavailable"}
        try:
            node_name = self.service.node_registry.get(run["node_id"])["name"]
        except BridgeError:
            node_name = ""
        token_usage = _stored_bridge_usage(run.get("token_usage"))
        return {"run_id": run["id"], "conversation_id": run["conversation"],
                "node_id": run["node_id"], "node_name": node_name[:80],
                "node_revision": run["node_revision"],
                "adapter_id": run["adapter_id"], "adapter_name": adapter_name[:80],
                "adapter_revision": run["adapter_revision"],
                "runtime_type": run["runtime_type"], "job_id": run["handoff"],
                "parent_run_id": run["parent_run"],
                "continue_from_run_id": run["continue_from"],
                "request_id": run["request_id"],
                # Empty stored selector = native runtime default model.
                "model": run["model"] or None,
                "reasoning": run["reasoning"],
                "phase": run["phase"], "active_state": run["active_state"],
                "outcome": run["outcome"], "result": run["result"],
                "token_usage": token_usage,
                "effective_security": effective_security,
                "error": run["error"], "created": run["created"],
                "updated": run["updated"]}

    def _persist_snapshot(self, run: dict, native: dict,
                          interactions: list[dict], activities: list[dict]) -> None:
        if native.get("id") != run["native_id"] or native.get("conversationId") != self._conversation(run)["native_id"]:
            raise BridgeError("Runtime snapshot identity changed", "binding_mismatch")
        validate_run_state(native)
        if run["phase"] == "terminal" and (native["phase"] != "terminal"
                or native["outcome"] != run["outcome"]):
            raise RuntimeUnavailable("Runtime terminal run changed state")
        fresh_usage = _normalize_bridge_usage(native.get("usage"))
        with self.service.lock, self.service.db:
            current = self.service.db.execute(
                "SELECT phase,outcome,token_usage FROM agent_runs WHERE id=?", (run["id"],)).fetchone()
            if current is None:
                raise BridgeError("Runtime run not found", "not_found")
            was_terminal = current["phase"] == "terminal"
            if fresh_usage is not None:
                self.service.db.execute(
                    "UPDATE agent_runs SET phase=?,active_state=?,outcome=?,result=?,error=?,token_usage=?,updated=? "
                    "WHERE id=?",
                    (native["phase"], native.get("activeState"), native.get("outcome"),
                     _safe(native.get("result")), _safe(native.get("error"), 300),
                     json.dumps(fresh_usage, sort_keys=True), _now(), run["id"]))
            else:
                self.service.db.execute(
                    "UPDATE agent_runs SET phase=?,active_state=?,outcome=?,result=?,error=?,updated=? "
                    "WHERE id=?",
                    (native["phase"], native.get("activeState"), native.get("outcome"),
                     _safe(native.get("result")), _safe(native.get("error"), 300),
                     _now(), run["id"]))
            live_ids = set()
            for item in interactions[:100]:
                if item.get("runId") != run["native_id"] or not isinstance(item.get("id"), str):
                    continue
                native_id = item["id"]
                live_ids.add(native_id)
                bridge_id = "int_" + digest((run["id"] + native_id).encode())[:24]
                payload = _safe_payload(item)
                existing = self.service.db.execute(
                    "SELECT state FROM agent_interactions WHERE run=? AND native_id=?",
                    (run["id"], native_id)).fetchone()
                self.service.db.execute(
                    "INSERT OR IGNORE INTO agent_interactions VALUES(?,?,?,?,?,?,?,?,?)",
                    (bridge_id, run["id"], native_id, str(item.get("kind"))[:40],
                     "pending", payload, None, _now(), _now()))
                if existing is None:
                    raw_kind = str(item.get("kind") or "")[:40]
                    request_kind = raw_kind if raw_kind in {"choice", "approval", "form"} else "interaction"
                    self.service.notification_manager.record_run_event_locked(
                        run_id=run["id"], workspace_id=run["workspace"],
                        job_id=run["handoff"], adapter_id=run["adapter_id"],
                        runtime_type=run["runtime_type"],
                        event_type="run_needs_attention", subject_id=bridge_id,
                        request_kind=request_kind, occurred_at=_now())
                elif existing["state"] == "pending":
                    self.service.db.execute(
                        "UPDATE agent_interactions SET payload=?,updated=? WHERE run=? "
                        "AND native_id=? AND state='pending'",
                        (payload, _now(), run["id"], native_id))
            # Successful empty snapshot means earlier requests are stale.
            rows = self.service.db.execute(
                "SELECT id,native_id FROM agent_interactions WHERE run=? AND state='pending'",
                (run["id"],)).fetchall()
            for item in rows:
                if item["native_id"] not in live_ids:
                    self.service.db.execute(
                        "UPDATE agent_interactions SET state='stale',updated=? WHERE id=?",
                        (_now(), item["id"]))
            for item in activities[:1000]:
                if item.get("runId") != run["native_id"] or not isinstance(item.get("id"), str):
                    continue
                native_id = item["id"]
                bridge_id = "act_" + digest((run["id"] + native_id).encode())[:24]
                payload = _safe_payload(item)
                self.service.db.execute(
                    "INSERT INTO agent_activities VALUES(?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(run,native_id) DO UPDATE SET status=excluded.status,"
                    "payload=excluded.payload,updated=excluded.updated",
                    (bridge_id, run["id"], native_id, str(item.get("kind"))[:40],
                     str(item.get("status"))[:40], payload, _now(), _now()))
            if native["phase"] == "terminal" and not was_terminal:
                self._record_outcome_event_locked(
                    {**run, "id": run["id"]}, native.get("outcome"))

    def reconcile(self, ws: dict, run_id: str) -> dict:
        run = self._run_row(ws, run_id)
        if not run["native_id"]:
            if run["phase"] == "terminal":
                return self._public(run)
            try:
                adapter = self._bound_run_adapter(ws, run)
                native = adapter.find_run(
                    self._conversation(run)["native_id"], run["id"])
            except RuntimeRejected as exc:
                if exc.code != "not_found":
                    raise
                with self.service.lock, self.service.db:
                    self.service.db.execute(
                        "UPDATE agent_runs SET phase='terminal',outcome='orphaned',"
                        "error='native_run_not_confirmed',updated=? WHERE id=?",
                        (_now(), run_id))
                    self._record_outcome_event_locked(run, "orphaned")
                return self._public(self._run_row(ws, run_id))
            if (native.get("clientRunId") != run["id"] or
                    native.get("conversationId") != self._conversation(run)["native_id"]):
                raise RuntimeUnavailable("Recovered runtime run binding changed")
            with self.service.lock, self.service.db:
                self.service.db.execute(
                    "UPDATE agent_runs SET native_id=?,updated=? WHERE id=?",
                    (native["id"], _now(), run_id))
            run = self._run_row(ws, run_id)
        adapter = self._bound_run_adapter(ws, run)
        try:
            native = adapter.run(run["native_id"])
        except RuntimeRejected as exc:
            if exc.code != "not_found" or run["phase"] == "terminal":
                raise
            with self.service.lock, self.service.db:
                self.service.db.execute(
                    "UPDATE agent_runs SET phase='terminal',active_state=NULL,"
                    "outcome='orphaned',error='native_run_missing',updated=? WHERE id=?",
                    (_now(), run_id))
                self._record_outcome_event_locked(run, "orphaned")
            return self._public(self._run_row(ws, run_id))
        interactions = adapter.interactions(run["native_id"])
        activities = adapter.activities(run["native_id"])
        self._persist_snapshot(run, native, interactions, activities)
        return self._public(self._run_row(ws, run_id))

    def read(self, ws: dict, run_id: str, *, sync: bool = True) -> dict:
        sync_warning = None
        if sync:
            try:
                self.reconcile(ws, run_id)
            except RuntimeUnavailable:
                sync_warning = {
                    "status": "unavailable", "code": "runtime_unavailable",
                    "message": "Live run sync was unavailable; showing the durable snapshot.",
                }
            except BridgeError as exc:
                if exc.code in {"node_changed", "adapter_changed", "adapter_node_mismatch"}:
                    sync_warning = {
                        "status": "stale", "code": exc.code,
                        "message": "Live run sync was skipped because runtime authority changed; "
                                   "showing the durable snapshot.",
                    }
                elif exc.code != "unknown_adapter":
                    raise
        run = self._run_row(ws, run_id)
        with self.service.lock:
            interactions = self.service.db.execute(
                "SELECT id,kind,state,payload FROM agent_interactions WHERE run=? "
                "ORDER BY created", (run_id,)).fetchall()
        result = {**self._public(run), "notifications": self.service.notification_manager.summary(run_id),
                "interactions": [
            {"id": item["id"], "kind": item["kind"], "state": item["state"],
             "details": json.loads(item["payload"])} for item in interactions]}
        if sync_warning is not None:
            result["sync_warning"] = sync_warning
        return result

    def list(self, ws: dict, offset: int = 0, limit: int = 20,
             adapter_id: str | None = None) -> dict:
        sql = "SELECT * FROM agent_runs WHERE workspace=?"
        args: list = [ws["id"]]
        if adapter_id:
            sql += " AND adapter_id=?"
            args.append(adapter_id)
        sql += " ORDER BY created DESC LIMIT ? OFFSET ?"
        args.extend([limit + 1, offset])
        with self.service.lock:
            rows = self.service.db.execute(sql, args).fetchall()
        runs = []
        for row in rows[:limit]:
            view = self._public(dict(row))
            view["notifications"] = {
                "overall": self.service.notification_manager.summary(view["run_id"])["overall"]}
            runs.append(view)
        return {"workspace_id": ws["id"], "runs": runs,
                "next_offset": offset + limit if len(rows) > limit else None}

    def interaction(self, ws: dict, run_id: str, interaction_id: str) -> dict:
        self._run_row(ws, run_id)
        with self.service.lock:
            row = self.service.db.execute(
                "SELECT * FROM agent_interactions WHERE id=? AND run=?",
                (interaction_id, run_id)).fetchone()
        if row is None:
            raise BridgeError("Interaction not found in this run", "not_found")
        return {"id": row["id"], "run_id": run_id, "kind": row["kind"],
                "state": row["state"], "details": json.loads(row["payload"])}

    def resolve(self, ws: dict, run_id: str, interaction_id: str,
                response: dict) -> dict:
        run = self._run_row(ws, run_id)
        interaction = self.interaction(ws, run_id, interaction_id)
        if interaction["state"] != "pending" or run["phase"] != "active":
            raise BridgeError("Interaction is stale", "interaction_stale")
        adapter = self._bound_run_adapter(ws, run)
        live = {item["id"]: item for item in adapter.interactions(run["native_id"])}
        with self.service.lock:
            native_row = self.service.db.execute(
                "SELECT native_id FROM agent_interactions WHERE id=? AND run=?",
                (interaction_id, run_id)).fetchone()
        native_id = native_row["native_id"]
        if native_id not in live or live[native_id].get("runId") != run["native_id"]:
            raise BridgeError("Interaction is no longer live", "interaction_stale")
        if interaction["kind"] == "choice":
            choice_id = response.get("choiceId")
            if choice_id not in {choice.get("id") for choice in live[native_id].get("choices", [])}:
                raise BridgeError("Choice is not available", "invalid_arguments")
        adapter.resolve(native_id, response)
        with self.service.lock, self.service.db:
            self.service.db.execute(
                "UPDATE agent_interactions SET state='resolved',response=?,updated=? WHERE id=?",
                (_safe_payload(response), _now(), interaction_id))
        return self.read(ws, run_id)

    def cancel(self, ws: dict, run_id: str) -> dict:
        run = self._run_row(ws, run_id)
        if run["phase"] == "terminal":
            return self._public(run)
        self._bound_run_adapter(ws, run).cancel(run["native_id"])
        return self.read(ws, run_id)

    def activities(self, ws: dict, run_id: str, offset: int = 0,
                   limit: int = 50, *, newest_first: bool = False,
                   before_created: str | None = None,
                   before_id: str | None = None) -> dict:
        self._run_row(ws, run_id)
        offset = max(0, int(offset or 0))
        limit = max(1, min(int(limit or 50), 50))
        if newest_first and ((before_created is None) != (before_id is None)):
            raise BridgeError("Invalid timeline cursor", "invalid_arguments")
        if newest_first and before_created is not None and (
                not before_created or len(before_created) > 64
                or not before_id or len(before_id) > 200):
            raise BridgeError("Invalid timeline cursor", "invalid_arguments")
        where = "run=?"
        params: list[Any] = [run_id]
        if newest_first and before_created is not None and before_id is not None:
            where += " AND (created<? OR (created=? AND id<?))"
            params.extend((before_created, before_created, before_id))
        order = "created DESC,id DESC" if newest_first else "created,id"
        with self.service.lock:
            rows = self.service.db.execute(
                f"SELECT id,kind,status,payload,created FROM agent_activities "
                f"WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?",
                (*params, limit + 1, offset)).fetchall()
        page = rows[:limit]
        result = {"run_id": run_id, "activities": [{"id": row["id"],
                "kind": row["kind"], "status": row["status"],
                "created": row["created"],
                "details": json.loads(row["payload"])} for row in page]}
        if newest_first:
            last = page[-1] if len(rows) > limit and page else None
            result["next_cursor"] = ({"created": last["created"], "id": last["id"]}
                                     if last is not None else None)
        else:
            result["next_offset"] = offset + limit if len(rows) > limit else None
        return result

    def activity(self, ws: dict, run_id: str, activity_id: str) -> dict:
        self._run_row(ws, run_id)
        with self.service.lock:
            row = self.service.db.execute(
                "SELECT * FROM agent_activities WHERE id=? AND run=?",
                (activity_id, run_id)).fetchone()
        if row is None:
            raise BridgeError("Activity not found", "not_found")
        return {"id": row["id"], "run_id": run_id,
                "details": json.loads(row["payload"])}

    def executions(self, ws: dict, run_id: str, offset: int = 0,
                   limit: int = 50, *, newest_first: bool = False,
                   before_created: str | None = None,
                   before_id: str | None = None,
                   include_previews: bool = False) -> dict:
        """Project recorded runtime tool activities into the execution view."""
        run = self._run_row(ws, run_id)
        offset = max(0, int(offset or 0))
        limit = max(1, min(int(limit or 50), 50))
        if newest_first and ((before_created is None) != (before_id is None)):
            raise BridgeError("Invalid execution cursor", "invalid_arguments")
        if newest_first and before_created is not None and (
                not before_created or len(before_created) > 64
                or not before_id or len(before_id) > 200):
            raise BridgeError("Invalid execution cursor", "invalid_arguments")
        kinds = tuple(sorted(EXECUTION_ACTIVITY_KINDS))
        placeholders = ",".join("?" for _ in kinds)
        where = f"run=? AND kind IN ({placeholders})"
        params: list[Any] = [run_id, *kinds]
        if newest_first and before_created is not None and before_id is not None:
            where += " AND (created<? OR (created=? AND id<?))"
            params.extend((before_created, before_created, before_id))
        order = "created DESC,id DESC" if newest_first else "created,id"
        with self.service.lock:
            rows = self.service.db.execute(
                f"SELECT id,kind,status,payload,created,updated FROM agent_activities "
                f"WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?",
                (*params, limit + 1, offset)).fetchall()
            sequence_start = None
            if newest_first:
                total = self.service.db.execute(
                    f"SELECT count(*) FROM agent_activities WHERE run=? AND kind IN ({placeholders})",
                    (run_id, *kinds)).fetchone()[0]
                newer = 0
                if before_created is not None and before_id is not None:
                    newer = self.service.db.execute(
                        f"SELECT count(*) FROM agent_activities WHERE run=? AND kind IN ({placeholders}) "
                        "AND (created>? OR (created=? AND id>=?))",
                        (run_id, *kinds, before_created, before_created, before_id)).fetchone()[0]
                sequence_start = int(total) - int(newer) - offset
        page = [dict(row) for row in rows[:limit]]
        executions = [
            self._execution_public(
                row,
                sequence_start - index if sequence_start is not None
                else offset + index + 1,
                include_preview=include_previews,
            )
            for index, row in enumerate(page)
        ]
        result = {"workspace_id": ws["id"], "run_id": run_id,
                  "adapter_id": run["adapter_id"],
                  "runtime_type": run["runtime_type"], "executions": executions}
        if newest_first:
            last = page[-1] if len(rows) > limit and page else None
            result["next_cursor"] = ({"created": last["created"], "id": last["id"]}
                                     if last is not None else None)
        else:
            result["next_offset"] = offset + limit if len(rows) > limit else None
        return result

    def execution(self, ws: dict, run_id: str, execution_id: str) -> dict:
        self._run_row(ws, run_id)
        kinds = tuple(sorted(EXECUTION_ACTIVITY_KINDS))
        placeholders = ",".join("?" for _ in kinds)
        with self.service.lock:
            row = self.service.db.execute(
                f"SELECT id,kind,status,payload,created,updated FROM agent_activities "
                f"WHERE id=? AND run=? AND kind IN ({placeholders})",
                (execution_id, run_id, *kinds)).fetchone()
            if row is None:
                raise BridgeError("Execution not found in this run", "not_found")
            sequence = self.service.db.execute(
                f"SELECT count(*) FROM agent_activities WHERE run=? AND kind IN ({placeholders}) "
                "AND (created<? OR (created=? AND id<=?))",
                (run_id, *kinds, row["created"], row["created"], row["id"])).fetchone()[0]
        return self._execution_public(dict(row), int(sequence), detail=True)

    @staticmethod
    def _execution_public(row: dict, sequence: int, *, detail: bool = False,
                          include_preview: bool = False) -> dict:
        try:
            payload = json.loads(row.get("payload") or "{}")
        except (TypeError, ValueError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        input_data = payload.get("input")
        input_data = input_data if isinstance(input_data, dict) else {}
        result_data = payload.get("result")
        result_data = result_data if isinstance(result_data, dict) else {}
        summary = _safe(input_data.get("summary"), 200)
        status = str(row.get("status") or "recorded")[:40]
        tool = {
            "command": "command", "file_change": "file_change",
            "tool_call": "tool_call", "search": "search",
            "subagent": "subagent",
        }.get(row.get("kind"), "tool")
        duration = result_data.get("durationMs", result_data.get("duration_ms"))
        if not isinstance(duration, int) or isinstance(duration, bool):
            duration = None
        terminal = status in {"completed", "failed", "declined", "interrupted"}
        record = {"execution_id": row["id"], "sequence": sequence,
                  "tool": tool, "state": status, "target_preview": summary,
                  "started": payload.get("createdAt") or row.get("created"),
                  "ended": ((payload.get("updatedAt") or row.get("updated"))
                            if terminal else None),
                  "duration_ms": duration,
                  "is_error": status == "failed" or (
                      isinstance(result_data.get("exitCode"), int)
                      and not isinstance(result_data.get("exitCode"), bool)
                      and result_data["exitCode"] != 0),
                  "permission_effect": "", "permission_decision": "",
                  "truncated": bool(payload.get("truncated"))}
        if include_preview:
            input_details = input_data.get("details")
            input_details = input_details if isinstance(input_details, dict) else {}
            input_details = {
                key: value for key, value in input_details.items()
                if not re.search(r"(?:_sha256|_bytes)$|^truncated$", str(key), re.I)
            }
            if isinstance(input_details.get("command"), str):
                input_preview = f"$ {input_details['command']}"
            elif input_details:
                input_preview = json.dumps(input_details, ensure_ascii=False)
            else:
                input_preview = summary
            input_preview = _safe(input_preview, 1200)

            output_preview = ""
            for field in ("output_preview", "outputPreview", "aggregatedOutput",
                          "stdout", "output", "preview", "message", "status"):
                candidate = result_data.get(field)
                if isinstance(candidate, str) and candidate:
                    output_preview = candidate
                    break
            if not output_preview:
                result_metadata = {
                    key: value for key, value in result_data.items()
                    if key not in {"is_error", "truncated", "output_bytes",
                                   "preview_bytes", "durationMs", "duration_ms"}
                }
                if result_metadata:
                    output_preview = "\n".join(
                        f"{key.replace('_', ' ').replace('Code', ' code').title()}: "
                        f"{value}"
                        for key, value in result_metadata.items()
                        if isinstance(value, (str, int, float, bool))
                    )
            record["input_preview"] = input_preview
            record["output_preview"] = _safe(output_preview, 900)
            record["output_truncated"] = bool(result_data.get("truncated")) or (
                isinstance(output_preview, str) and len(output_preview) > 900)
        if detail:
            record["input_summary"] = {"summary": summary} if summary else {}
            try:
                record["result_summary"] = json.loads(_safe_payload(result_data))
            except (TypeError, ValueError):
                record["result_summary"] = {}
        return record

    def start_background(self) -> None:
        if not self.background or self._thread is not None:
            return
        self._thread = threading.Thread(target=self._sweep, name="wbrp-reconcile",
                                        daemon=True)
        self._thread.start()

    def _sweep(self) -> None:
        while not self._stop.wait(5):
            with self.service.lock:
                rows = self.service.db.execute(
                    "SELECT id,workspace FROM agent_runs WHERE phase IN ('starting','active') "
                    "ORDER BY created LIMIT 100").fetchall()
            for row in rows:
                if self._stop.is_set():
                    return
                try:
                    ws = self.service.workspace(row["workspace"], False)
                    self.reconcile(ws, row["id"])
                    self._sweep_failures.discard(row["id"])
                except Exception:
                    if self._stop.is_set():
                        return
                    if row["id"] not in self._sweep_failures:
                        self._sweep_failures.add(row["id"])
                        with self.service.lock:
                            self.service.event(row["workspace"],
                                               "runtime_reconcile", "failed")
                    continue

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
