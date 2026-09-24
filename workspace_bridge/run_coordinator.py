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

from .runtime import RuntimeRejected, RuntimeUnavailable
from .security import BridgeError, HANDOFF, digest, redact
from .wbrp import HttpRuntimeAdapter, validate_run_state


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


EXECUTION_ACTIVITY_KINDS = frozenset({
    "command", "file_change", "tool_call", "search", "subagent",
})


class RunCoordinator:
    """One generic coordinator for every WBRP adapter in the registry."""

    def __init__(self, service, adapters: dict[str, HttpRuntimeAdapter], *,
                 background: bool = True):
        self.service = service
        self.adapters = dict(adapters)
        self.background = background
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._sweep_failures: set[str] = set()
        service.db.executescript("""
          CREATE TABLE IF NOT EXISTS runtime_profiles (
            workspace TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
            runtime TEXT NOT NULL, profile TEXT NOT NULL,
            revision TEXT NOT NULL, updated TEXT NOT NULL,
            PRIMARY KEY(workspace,runtime));
          CREATE TABLE IF NOT EXISTS runtime_conversations (
            id TEXT PRIMARY KEY, workspace TEXT NOT NULL REFERENCES workspaces(id),
            runtime TEXT NOT NULL, native_id TEXT NOT NULL,
            profile TEXT NOT NULL, revision TEXT NOT NULL,
            instance_id TEXT NOT NULL, created TEXT NOT NULL,
            UNIQUE(runtime,native_id));
          CREATE TABLE IF NOT EXISTS runtime_runs (
            id TEXT PRIMARY KEY, conversation TEXT NOT NULL REFERENCES runtime_conversations(id),
            workspace TEXT NOT NULL REFERENCES workspaces(id),
            runtime TEXT NOT NULL, handoff TEXT NOT NULL REFERENCES jobs(id),
            request_id TEXT NOT NULL, request_hash TEXT NOT NULL,
            continue_from TEXT, parent_run TEXT,
            native_id TEXT, model TEXT NOT NULL, reasoning TEXT,
            phase TEXT NOT NULL, active_state TEXT, outcome TEXT,
            result TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '',
            created TEXT NOT NULL, updated TEXT NOT NULL,
            UNIQUE(workspace,request_id));
          CREATE UNIQUE INDEX IF NOT EXISTS ux_runtime_conversation_active
            ON runtime_runs(conversation) WHERE phase IN ('starting','active');
          CREATE TABLE IF NOT EXISTS runtime_interactions (
            id TEXT PRIMARY KEY, run TEXT NOT NULL REFERENCES runtime_runs(id),
            native_id TEXT NOT NULL, kind TEXT NOT NULL,
            state TEXT NOT NULL, payload TEXT NOT NULL,
            response TEXT, created TEXT NOT NULL, updated TEXT NOT NULL,
            UNIQUE(run,native_id));
          CREATE TABLE IF NOT EXISTS runtime_activities (
            id TEXT PRIMARY KEY, run TEXT NOT NULL REFERENCES runtime_runs(id),
            native_id TEXT NOT NULL, kind TEXT NOT NULL,
            status TEXT NOT NULL, payload TEXT NOT NULL,
            created TEXT NOT NULL, updated TEXT NOT NULL,
            UNIQUE(run,native_id));
        """)
        columns = {row["name"] for row in service.db.execute("PRAGMA table_info(runtime_runs)")}
        if "continue_from" not in columns:
            service.db.execute("ALTER TABLE runtime_runs ADD COLUMN continue_from TEXT")
        if "parent_run" not in columns:
            service.db.execute("ALTER TABLE runtime_runs ADD COLUMN parent_run TEXT")

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
            runtime=run["runtime"], event_type=event_type, occurred_at=_now())

    def configured(self, runtime_id: str) -> bool:
        return runtime_id in self.adapters

    def adapter(self, runtime_id: str) -> HttpRuntimeAdapter:
        try:
            return self.adapters[runtime_id]
        except KeyError:
            raise BridgeError(f"Runtime {runtime_id!r} is not configured", "unknown_runtime") from None

    def diagnostics(self) -> dict:
        rows = {}
        for runtime_id, adapter in self.adapters.items():
            try:
                descriptor = adapter.descriptor()
                rows[runtime_id] = {"configured": True, "healthy": True,
                                    "protocol": 1, "features": descriptor.features,
                                    "instance": descriptor.instance_id,
                                    "adapter_version": descriptor.adapter_version,
                                    "native_version": descriptor.native_version}
            except BridgeError as exc:
                rows[runtime_id] = {"configured": True, "healthy": False,
                                    "detail": _safe(str(exc), 200)}
        return rows

    def set_profile(self, ws: dict, runtime_id: str, profile_id: str) -> dict:
        adapter = self.adapter(runtime_id)
        profiles = adapter.profiles()
        profile = next((item for item in profiles if item.get("id") == profile_id), None)
        if profile is None or not isinstance(profile.get("revision"), str):
            raise BridgeError("Security profile is unavailable", "profile_unavailable")
        with self.service.lock, self.service.db:
            self.service.db.execute(
                "INSERT INTO runtime_profiles VALUES(?,?,?,?,?) ON CONFLICT(workspace,runtime) "
                "DO UPDATE SET profile=excluded.profile,revision=excluded.revision,"
                "updated=excluded.updated",
                (ws["id"], runtime_id, profile_id, profile["revision"], _now()))
        return {"runtime": runtime_id, "profile": profile_id,
                "revision": profile["revision"]}

    def save_profile(self, runtime_id: str, profile_id: str, config: dict,
                     expected_revision: str | None) -> dict:
        if (not isinstance(profile_id, str)
                or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", profile_id)
                or not isinstance(config, dict)
                or len(json.dumps(config)) > 32768
                or (expected_revision is not None
                    and (not isinstance(expected_revision, str)
                         or len(expected_revision) > 100))):
            raise BridgeError("Invalid security profile", "invalid_arguments")
        saved = self.adapter(runtime_id).save_profile(
            profile_id, config, expected_revision)
        if saved.get("id") != profile_id or not isinstance(saved.get("revision"), str):
            raise BridgeError("Runtime returned an invalid security profile",
                              "binding_mismatch")
        with self.service.lock, self.service.db:
            self.service.db.execute(
                "UPDATE runtime_profiles SET revision=?,updated=? "
                "WHERE runtime=? AND profile=?",
                (saved["revision"], _now(), runtime_id, profile_id))
        self.service.event(None, "save_runtime_profile", "saved")
        return saved

    def delete_profile(self, runtime_id: str, profile_id: str) -> dict:
        if (not isinstance(profile_id, str)
                or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", profile_id)):
            raise BridgeError("Invalid security profile ID", "invalid_arguments")
        with self.service.lock:
            assigned = self.service.db.execute(
                "SELECT 1 FROM runtime_profiles WHERE runtime=? AND profile=? LIMIT 1",
                (runtime_id, profile_id)).fetchone()
        if assigned:
            raise BridgeError("Assign another profile before deleting this one", "conflict")
        result = self.adapter(runtime_id).delete_profile(profile_id)
        self.service.event(None, "delete_runtime_profile", "deleted")
        return result

    def profile(self, ws: dict, runtime_id: str) -> dict:
        with self.service.lock:
            row = self.service.db.execute(
                "SELECT profile,revision FROM runtime_profiles WHERE workspace=? AND runtime=?",
                (ws["id"], runtime_id)).fetchone()
        if row is None:
            raise BridgeError("Workspace runtime security profile is not configured",
                              "profile_unconfigured")
        available = self.adapter(runtime_id).profiles()
        if not any(item.get("id") == row["profile"] and
                   item.get("revision") == row["revision"] for item in available):
            raise BridgeError("Workspace runtime security profile changed",
                              "profile_changed")
        return {"id": row["profile"], "revision": row["revision"]}

    def _model_key(self, runtime_id: str) -> str:
        return f"runtime_model_policy:{runtime_id}"

    def model_policy(self, runtime_id: str) -> dict:
        self.adapter(runtime_id)
        raw = self.service.setting(self._model_key(runtime_id))
        if not raw:
            return {"configured": False, "enabled": [], "default": None,
                    "reasoning_defaults": {}}
        try:
            policy = json.loads(raw)
        except ValueError:
            policy = None
        if (not isinstance(policy, dict) or not isinstance(policy.get("enabled"), list)
                or not isinstance(policy.get("default"), str)
                or policy["default"] not in policy["enabled"]):
            return {"configured": False, "enabled": [], "default": None,
                    "reasoning_defaults": {}}
        reasoning_defaults = policy.get("reasoning_defaults", {})
        if (not isinstance(reasoning_defaults, dict) or len(reasoning_defaults) > 200
                or any(not isinstance(selector, str) or not selector or len(selector) > 260
                       or not isinstance(effort, str) or not effort or len(effort) > 40
                       for selector, effort in reasoning_defaults.items())):
            return {"configured": False, "enabled": [], "default": None,
                    "reasoning_defaults": {}}
        return {"configured": True, "enabled": policy["enabled"],
                "default": policy["default"],
                "reasoning_defaults": reasoning_defaults}

    def models(self, ws: dict, runtime_id: str, query: str = "", limit: int = 25) -> dict:
        rows = self.adapter(runtime_id).models(ws["id"])
        policy = self.model_policy(runtime_id)
        selected = []
        for model in rows:
            selector = model.get("selector")
            if not isinstance(selector, str):
                continue
            if query and query.casefold() not in (selector + " " +
                    str(model.get("displayName") or "")).casefold():
                continue
            selected.append({**model, "enabled": selector in policy["enabled"],
                             "policy_default": selector == policy["default"]})
        return {"runtime": runtime_id, "workspace_id": ws["id"],
                "models": selected[:max(1, min(limit, 100))], "policy": policy,
                "count": len(selected)}

    def set_model_policy(self, runtime_id: str, enabled: list[str], default: str,
                         ws: dict | None = None,
                         reasoning_defaults: dict[str, str] | None = None) -> dict:
        if not isinstance(enabled, list) or not enabled or len(enabled) > 200:
            raise BridgeError("Model policy requires 1..200 selectors", "invalid_arguments")
        if any(not isinstance(item, str) or not item or len(item) > 260
               for item in enabled) or len(set(enabled)) != len(enabled):
            raise BridgeError("Invalid model selectors", "invalid_arguments")
        if default not in enabled:
            raise BridgeError("Default model must be enabled", "invalid_arguments")
        rows = self.adapter(runtime_id).models(ws["id"] if ws else "")
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
            self.service.set_setting(self._model_key(runtime_id),
                                     json.dumps({"enabled": enabled, "default": default,
                                                 "reasoning_defaults": reasoning_defaults}))
        return self.model_policy(runtime_id)

    def _select_model(self, ws: dict, runtime_id: str, requested: str | None) -> str:
        policy = self.model_policy(runtime_id)
        if not policy["configured"]:
            raise BridgeError("Runtime model policy is not configured",
                              "model_policy_unconfigured")
        selector = requested if requested is not None else policy["default"]
        if selector not in policy["enabled"]:
            raise BridgeError("Model is not enabled", "model_not_enabled")
        available = {item["selector"] for item in self.adapter(runtime_id).models(ws["id"])}
        if selector not in available:
            raise BridgeError("Model is unavailable", "model_unavailable")
        return selector

    def _select_reasoning(self, ws: dict, runtime_id: str, selector: str) -> str | None:
        policy = self.model_policy(runtime_id)
        effort = policy["reasoning_defaults"].get(selector)
        if effort is None:
            return None
        row = next((item for item in self.adapter(runtime_id).models(ws["id"])
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
            "in the same folder. Preserve pre-existing edits. Follow the plan "
            "and satisfy every acceptance criterion. Run the agreed checks. "
            "Stop and report a blocker when assumptions fail or the work exceeds "
            "scope. Do not commit, push, tag, publish, deploy, rotate credentials, "
            "or edit the handoff documents. Report changed files, exact checks "
            "and outcomes, and remaining risks."
        )

    def _run_row(self, ws: dict, run_id: str) -> dict:
        with self.service.lock:
            row = self.service.db.execute(
                "SELECT * FROM runtime_runs WHERE id=? AND workspace=?",
                (run_id, ws["id"])).fetchone()
        if row is None:
            raise BridgeError("Run not found in this workspace", "not_found")
        return dict(row)

    def has_run(self, run_id: str) -> bool:
        with self.service.lock:
            return self.service.db.execute(
                "SELECT 1 FROM runtime_runs WHERE id=?", (run_id,)).fetchone() is not None

    def _conversation(self, run: dict) -> dict:
        with self.service.lock:
            row = self.service.db.execute(
                "SELECT * FROM runtime_conversations WHERE id=?",
                (run["conversation"],)).fetchone()
        if row is None:
            raise BridgeError("Conversation record is missing", "internal_error")
        return dict(row)

    def start(self, ws: dict, runtime_id: str, job_id: str, request_id: str,
              model: str | None = None, parent_run_id: str | None = None,
              continue_from_run_id: str | None = None) -> dict:
        self.service.require_workspace_runtime(ws, runtime_id)
        if not ws.get("agent_enabled"):
            raise BridgeError("Agent execution is disabled", "agent_disabled")
        job = self.service.job(ws, job_id)
        if job["state"] != "prepared":
            raise BridgeError("Only a prepared handoff may run", "conflict")
        with self.service.lock:
            existing = self.service.db.execute(
                "SELECT * FROM runtime_runs WHERE workspace=? AND request_id=?",
                (ws["id"], request_id)).fetchone()
        if existing:
            if (existing["runtime"] != runtime_id or existing["handoff"] != job_id
                    or existing["continue_from"] != continue_from_run_id
                    or existing["parent_run"] != parent_run_id
                    or (model is not None and existing["model"] != model)):
                raise BridgeError("request_id has different content", "conflict")
            return {**self._public(dict(existing)), "idempotent": True}
        adapter = self.adapter(runtime_id)
        if parent_run_id:
            self._run_row(ws, parent_run_id)
        selector = self._select_model(ws, runtime_id, model)
        reasoning = self._select_reasoning(ws, runtime_id, selector)
        profile = self.profile(ws, runtime_id)
        request_hash = digest(json.dumps({"job": job_id, "runtime": runtime_id,
                                          "model": selector,
                                          "parent": parent_run_id or "",
                                          "continue": continue_from_run_id or ""},
                                         sort_keys=True).encode())
        if continue_from_run_id:
            source = self._run_row(ws, continue_from_run_id)
            if (source["runtime"] != runtime_id or source["phase"] != "terminal"
                    or source["outcome"] != "succeeded"
                    or source["model"] != selector or source["handoff"] != job_id):
                raise BridgeError("Continuation source is unavailable",
                                  "continuation_unavailable")
            conversation = self._conversation(source)
            if (conversation["profile"] != profile["id"]
                    or conversation["revision"] != profile["revision"]):
                raise BridgeError("Security profile changed; start a new conversation",
                                  "profile_changed")
            native_conversation = adapter.conversation(conversation["native_id"])
            if native_conversation.get("status") != "idle":
                raise BridgeError("Conversation is not idle", "conversation_busy")
        else:
            descriptor = adapter.descriptor()
            native_conversation = adapter.create_conversation({
                "workspaceId": ws["id"], "directory": str(ws["root"]),
                "securityProfile": profile,
            })
            if (native_conversation.get("workspaceId") != ws["id"]
                    or (native_conversation.get("securityProfile") or {}) != profile
                    or not isinstance(native_conversation.get("id"), str)
                    or not native_conversation["id"]):
                raise BridgeError("Runtime conversation binding is invalid",
                                  "binding_mismatch")
            conversation = {"id": _id("conv_"), "workspace": ws["id"],
                            "runtime": runtime_id, "native_id": native_conversation["id"],
                            "profile": profile["id"], "revision": profile["revision"],
                            "instance_id": descriptor.instance_id, "created": _now()}
            with self.service.lock, self.service.db:
                self.service.db.execute(
                    "INSERT INTO runtime_conversations VALUES(?,?,?,?,?,?,?,?)",
                    tuple(conversation.values()))
        bridge_run_id = _id("run_")
        timestamp = _now()
        with self.service.lock, self.service.db:
            self.service.db.execute(
                "INSERT INTO runtime_runs(id,conversation,workspace,runtime,handoff,request_id,"
                "request_hash,continue_from,parent_run,native_id,model,reasoning,phase,active_state,outcome,result,error,"
                "created,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (bridge_run_id, conversation["id"], ws["id"], runtime_id, job_id,
                 request_id, request_hash, continue_from_run_id, parent_run_id,
                 None, selector, reasoning, "starting", None,
                 None, "", "", timestamp, timestamp))
        try:
            run_payload = {
                "input": [{"type": "text", "text": self._prompt(ws, job)}],
                "model": selector,
                "clientRunId": bridge_run_id,
            }
            if reasoning is not None:
                run_payload["reasoning"] = reasoning
            native_run = validate_run_state(adapter.start_run(
                conversation["native_id"], run_payload))
            if (native_run.get("conversationId") != conversation["native_id"]
                    or native_run.get("clientRunId") != bridge_run_id):
                raise BridgeError("Runtime run binding is invalid", "binding_mismatch")
        except RuntimeRejected as exc:
            with self.service.lock, self.service.db:
                self.service.db.execute(
                    "UPDATE runtime_runs SET phase='terminal',outcome='failed',error=?,updated=? "
                    "WHERE id=?", (_safe(str(exc), 300), _now(), bridge_run_id))
                self._record_outcome_event_locked({
                    "id": bridge_run_id, "workspace": ws["id"], "handoff": job_id,
                    "runtime": runtime_id}, "failed")
            raise
        with self.service.lock, self.service.db:
            self.service.db.execute(
                "UPDATE runtime_runs SET native_id=?,phase=?,active_state=?,outcome=?,"
                "result=?,error=?,updated=? WHERE id=?",
                (native_run["id"], native_run["phase"], native_run.get("activeState"),
                 native_run.get("outcome"), _safe(native_run.get("result")),
                 _safe(native_run.get("error"), 300), _now(), bridge_run_id))
            if native_run["phase"] == "terminal":
                self._record_outcome_event_locked({
                    "id": bridge_run_id, "workspace": ws["id"], "handoff": job_id,
                    "runtime": runtime_id}, native_run.get("outcome"))
        return self.read(ws, bridge_run_id, sync=False)

    def _public(self, run: dict) -> dict:
        return {"run_id": run["id"], "conversation_id": run["conversation"],
                "runtime": run["runtime"], "job_id": run["handoff"],
                "parent_run_id": run["parent_run"],
                "continue_from_run_id": run["continue_from"],
                "request_id": run["request_id"], "model": run["model"],
                "reasoning": run["reasoning"],
                "phase": run["phase"], "active_state": run["active_state"],
                "outcome": run["outcome"], "result": run["result"],
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
        with self.service.lock, self.service.db:
            current = self.service.db.execute(
                "SELECT phase,outcome FROM runtime_runs WHERE id=?", (run["id"],)).fetchone()
            if current is None:
                raise BridgeError("Runtime run not found", "not_found")
            was_terminal = current["phase"] == "terminal"
            self.service.db.execute(
                "UPDATE runtime_runs SET phase=?,active_state=?,outcome=?,result=?,error=?,updated=? "
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
                    "SELECT state FROM runtime_interactions WHERE run=? AND native_id=?",
                    (run["id"], native_id)).fetchone()
                self.service.db.execute(
                    "INSERT OR IGNORE INTO runtime_interactions VALUES(?,?,?,?,?,?,?,?,?)",
                    (bridge_id, run["id"], native_id, str(item.get("kind"))[:40],
                     "pending", payload, None, _now(), _now()))
                if existing is None:
                    raw_kind = str(item.get("kind") or "")[:40]
                    request_kind = raw_kind if raw_kind in {"choice", "approval", "form"} else "interaction"
                    self.service.notification_manager.record_run_event_locked(
                        run_id=run["id"], workspace_id=run["workspace"],
                        job_id=run["handoff"], runtime=run["runtime"],
                        event_type="run_needs_attention", subject_id=bridge_id,
                        request_kind=request_kind, occurred_at=_now())
                elif existing["state"] == "pending":
                    self.service.db.execute(
                        "UPDATE runtime_interactions SET payload=?,updated=? WHERE run=? "
                        "AND native_id=? AND state='pending'",
                        (payload, _now(), run["id"], native_id))
            # Successful empty snapshot means earlier requests are stale.
            rows = self.service.db.execute(
                "SELECT id,native_id FROM runtime_interactions WHERE run=? AND state='pending'",
                (run["id"],)).fetchall()
            for item in rows:
                if item["native_id"] not in live_ids:
                    self.service.db.execute(
                        "UPDATE runtime_interactions SET state='stale',updated=? WHERE id=?",
                        (_now(), item["id"]))
            for item in activities[:1000]:
                if item.get("runId") != run["native_id"] or not isinstance(item.get("id"), str):
                    continue
                native_id = item["id"]
                bridge_id = "act_" + digest((run["id"] + native_id).encode())[:24]
                payload = _safe_payload(item)
                self.service.db.execute(
                    "INSERT INTO runtime_activities VALUES(?,?,?,?,?,?,?,?) "
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
                native = self.adapter(run["runtime"]).find_run(
                    self._conversation(run)["native_id"], run["id"])
            except RuntimeRejected as exc:
                if exc.code != "not_found":
                    raise
                with self.service.lock, self.service.db:
                    self.service.db.execute(
                        "UPDATE runtime_runs SET phase='terminal',outcome='orphaned',"
                        "error='native_run_not_confirmed',updated=? WHERE id=?",
                        (_now(), run_id))
                    self._record_outcome_event_locked(run, "orphaned")
                return self._public(self._run_row(ws, run_id))
            if (native.get("clientRunId") != run["id"] or
                    native.get("conversationId") != self._conversation(run)["native_id"]):
                raise RuntimeUnavailable("Recovered runtime run binding changed")
            with self.service.lock, self.service.db:
                self.service.db.execute(
                    "UPDATE runtime_runs SET native_id=?,updated=? WHERE id=?",
                    (native["id"], _now(), run_id))
            run = self._run_row(ws, run_id)
        adapter = self.adapter(run["runtime"])
        try:
            native = adapter.run(run["native_id"])
        except RuntimeRejected as exc:
            if exc.code != "not_found" or run["phase"] == "terminal":
                raise
            with self.service.lock, self.service.db:
                self.service.db.execute(
                    "UPDATE runtime_runs SET phase='terminal',active_state=NULL,"
                    "outcome='orphaned',error='native_run_missing',updated=? WHERE id=?",
                    (_now(), run_id))
                self._record_outcome_event_locked(run, "orphaned")
            return self._public(self._run_row(ws, run_id))
        interactions = adapter.interactions(run["native_id"])
        activities = adapter.activities(run["native_id"])
        self._persist_snapshot(run, native, interactions, activities)
        return self._public(self._run_row(ws, run_id))

    def read(self, ws: dict, run_id: str, *, sync: bool = True) -> dict:
        if sync:
            try:
                self.reconcile(ws, run_id)
            except RuntimeUnavailable:
                pass  # Durable snapshot remains readable while adapter is down.
            except BridgeError as exc:
                if exc.code != "unknown_runtime":
                    raise
        run = self._run_row(ws, run_id)
        with self.service.lock:
            interactions = self.service.db.execute(
                "SELECT id,kind,state,payload FROM runtime_interactions WHERE run=? "
                "ORDER BY created", (run_id,)).fetchall()
        return {**self._public(run), "notifications": self.service.notification_manager.summary(run_id),
                "interactions": [
            {"id": item["id"], "kind": item["kind"], "state": item["state"],
             "details": json.loads(item["payload"])} for item in interactions]}

    def list(self, ws: dict, offset: int = 0, limit: int = 20,
             runtime: str | None = None) -> dict:
        sql = "SELECT * FROM runtime_runs WHERE workspace=?"
        args: list = [ws["id"]]
        if runtime:
            sql += " AND runtime=?"
            args.append(runtime)
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
                "SELECT * FROM runtime_interactions WHERE id=? AND run=?",
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
        adapter = self.adapter(run["runtime"])
        live = {item["id"]: item for item in adapter.interactions(run["native_id"])}
        with self.service.lock:
            native_row = self.service.db.execute(
                "SELECT native_id FROM runtime_interactions WHERE id=? AND run=?",
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
                "UPDATE runtime_interactions SET state='resolved',response=?,updated=? WHERE id=?",
                (_safe_payload(response), _now(), interaction_id))
        return self.read(ws, run_id)

    def cancel(self, ws: dict, run_id: str) -> dict:
        run = self._run_row(ws, run_id)
        if run["phase"] == "terminal":
            return self._public(run)
        self.adapter(run["runtime"]).cancel(run["native_id"])
        return self.read(ws, run_id)

    def activities(self, ws: dict, run_id: str, offset: int = 0,
                   limit: int = 50) -> dict:
        self._run_row(ws, run_id)
        with self.service.lock:
            rows = self.service.db.execute(
                "SELECT id,kind,status,payload FROM runtime_activities WHERE run=? "
                "ORDER BY created,id LIMIT ? OFFSET ?", (run_id, limit + 1, offset)).fetchall()
        return {"run_id": run_id, "activities": [{"id": row["id"],
                "kind": row["kind"], "status": row["status"],
                "details": json.loads(row["payload"])} for row in rows[:limit]],
                "next_offset": offset + limit if len(rows) > limit else None}

    def activity(self, ws: dict, run_id: str, activity_id: str) -> dict:
        self._run_row(ws, run_id)
        with self.service.lock:
            row = self.service.db.execute(
                "SELECT * FROM runtime_activities WHERE id=? AND run=?",
                (activity_id, run_id)).fetchone()
        if row is None:
            raise BridgeError("Activity not found", "not_found")
        return {"id": row["id"], "run_id": run_id,
                "details": json.loads(row["payload"])}

    def executions(self, ws: dict, run_id: str, offset: int = 0,
                   limit: int = 50) -> dict:
        """Project recorded runtime tool activities into the execution view."""
        run = self._run_row(ws, run_id)
        offset = max(0, int(offset or 0))
        limit = max(1, min(int(limit or 50), 50))
        kinds = tuple(sorted(EXECUTION_ACTIVITY_KINDS))
        placeholders = ",".join("?" for _ in kinds)
        with self.service.lock:
            rows = self.service.db.execute(
                f"SELECT id,kind,status,payload,created,updated FROM runtime_activities "
                f"WHERE run=? AND kind IN ({placeholders}) ORDER BY created,id LIMIT ? OFFSET ?",
                (run_id, *kinds, limit + 1, offset)).fetchall()
        page = [dict(row) for row in rows[:limit]]
        return {"workspace_id": ws["id"], "run_id": run_id,
                "runtime": run["runtime"],
                "executions": [self._execution_public(row, offset + index + 1)
                               for index, row in enumerate(page)],
                "next_offset": offset + limit if len(rows) > limit else None}

    def execution(self, ws: dict, run_id: str, execution_id: str) -> dict:
        self._run_row(ws, run_id)
        kinds = tuple(sorted(EXECUTION_ACTIVITY_KINDS))
        placeholders = ",".join("?" for _ in kinds)
        with self.service.lock:
            row = self.service.db.execute(
                f"SELECT id,kind,status,payload,created,updated FROM runtime_activities "
                f"WHERE id=? AND run=? AND kind IN ({placeholders})",
                (execution_id, run_id, *kinds)).fetchone()
            if row is None:
                raise BridgeError("Execution not found in this run", "not_found")
            sequence = self.service.db.execute(
                f"SELECT count(*) FROM runtime_activities WHERE run=? AND kind IN ({placeholders}) "
                "AND (created<? OR (created=? AND id<=?))",
                (run_id, *kinds, row["created"], row["created"], row["id"])).fetchone()[0]
        return self._execution_public(dict(row), int(sequence), detail=True)

    @staticmethod
    def _execution_public(row: dict, sequence: int, *, detail: bool = False) -> dict:
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
                    "SELECT id,workspace FROM runtime_runs WHERE phase IN ('starting','active') "
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
