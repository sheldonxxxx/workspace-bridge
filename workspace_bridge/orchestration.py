"""OpenCode run orchestration: handoff-bound sessions, resumable permission waits.

This module keeps long-running runtime/SDK lifecycle logic out of service.py.
It is the only place that turns a prepared handoff into a bounded OpenCode
session, persists run/request state, mediates permission decisions, reconciles
state after restart, and emits completion/attention notifications.

Trust model: the bridge is the orchestration client, not a sandbox. Agent
claims are untrusted evidence. ``always`` approvals pass through OpenCode's own
proposed pattern unchanged; if that scope cannot be reviewed, always fails
closed while once/reject remain available.
"""
from __future__ import annotations

import json
import os
import secrets
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any

from .notifications import NOTIFIED_STATES, Notifier, NullNotifier
from .runtime import (MAX_TRANSCRIPT_CHARS, ModelInfo, OpenCodeRuntime, RuntimeUnavailable,
                      sanitize_metadata)
from .security import BridgeError, HANDOFF, digest

RUN_STATES = ("starting", "running", "waiting_permission", "waiting_question",
              "completed", "blocked", "failed", "cancelled", "orphaned")
ACTIVE_RUN_STATES = frozenset({"starting", "running", "waiting_permission", "waiting_question"})
TERMINAL_RUN_STATES = frozenset({"completed", "blocked", "failed", "cancelled", "orphaned"})
WAITING_RUN_STATES = frozenset({"waiting_permission", "waiting_question"})
MAX_RESULT_CHARS = 20000
MAX_RUNS_PER_WORKSPACE = 500
PERMISSIONS = ("once", "always", "reject")
RECONCILE_INTERVAL = 15.0

RUN_COLUMNS = ("id,workspace,job,request_id,request_hash,parent_run,session,model,state,"
               "error_code,error_message,result,notification,created,started,updated,finished,"
               "message_floor_ms,session_reused,transcript")
REQUEST_COLUMNS = ("id,run,workspace,session,opencode_request,kind,action,resource,pattern,"
                   "metadata,explanation,redacted,state,decision,created,updated,resolved")


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def uid(prefix: str) -> str:
    return prefix + secrets.token_hex(12)


def _duration(start: str | None, end: str | None) -> float | None:
    if not start or not end:
        return None
    try:
        return round((datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds(), 3)
    except ValueError:
        return None


def _same_directory(left: str, right: str) -> bool:
    # An absent/empty observed directory is never proof of ownership.
    if not left or not right:
        return False
    try:
        return os.path.realpath(left) == os.path.realpath(right)
    except (OSError, ValueError):
        return False


def _bounded_str_list(value: Any, limit: int = 32) -> list[str]:
    if isinstance(value, str):
        return [value[:400]]
    if isinstance(value, (list, tuple)):
        return [str(p)[:400] for p in value if isinstance(p, str)][:limit]
    return []


class OpenCodeOrchestrator:
    def __init__(self, service, runtime: OpenCodeRuntime | None = None,
                 notifier: Notifier | None = None, *, background: bool = True, clock=now):
        self.service = service
        self.runtime = runtime
        self.notifier = notifier or NullNotifier()
        self.background = background
        self._clock = clock
        self._cursor = 0
        self._stop = threading.Event()
        self._pump: threading.Thread | None = None
        self._submissions: set[threading.Thread] = set()
        self._submission_condition = threading.Condition()
        # Runs whose startup reconciliation needs retrying once the runtime is
        # reachable (e.g. the adapter is still starting). Never used to infer loss.
        self._reconcile_pending: set[str] = set()
        self._reconcile_guard = threading.Lock()
        self._reconcile_thread: threading.Thread | None = None

    # ---------------------------------------------------------------- lifecycle
    @property
    def configured(self) -> bool:
        return self.runtime is not None

    def start(self) -> None:
        if not self.background or self.runtime is None:
            return
        if self._pump is None:
            self._cursor = self._start_cursor()
            self._pump = threading.Thread(target=self._pump_loop, name="opencode-events", daemon=True)
            self._pump.start()
        if self._reconcile_pending and self._reconcile_thread is None:
            self._reconcile_thread = threading.Thread(target=self._reconcile_loop,
                                                      name="opencode-reconcile", daemon=True)
            self._reconcile_thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._pump is not None:
            self._pump.join(timeout=5)
            self._pump = None
        if self._reconcile_thread is not None:
            self._reconcile_thread.join(timeout=5)
            self._reconcile_thread = None
        self.flush()

    def flush(self, timeout: float = 5.0) -> None:
        """Wait for background prompt submissions (used by tests and shutdown)."""
        deadline = threading.TIMEOUT_MAX if timeout is None else timeout
        with self._submission_condition:
            for thread in list(self._submissions):
                thread.join(timeout=deadline)

    def _start_cursor(self) -> int:
        try:
            head = self.runtime.health()
        except BridgeError:
            return 0
        instance = str(head.get("instance", ""))
        stored_instance = self.service.setting("runtime_instance") or ""
        if instance and instance == stored_instance:
            stored = self.service.setting("runtime_cursor")
            try:
                return int(stored) if stored else int(head.get("cursor") or 0)
            except (TypeError, ValueError):
                return int(head.get("cursor") or 0)
        if instance:
            self.service.set_setting("runtime_instance", instance)
        return int(head.get("cursor") or 0)

    # ------------------------------------------------------------------ helpers
    def _require_runtime(self) -> OpenCodeRuntime:
        if self.runtime is None:
            raise BridgeError("OpenCode runtime is not configured for this bridge", "runtime_unavailable")
        return self.runtime

    def _directory(self, ws: dict) -> str:
        return str(ws["root"])

    def _row(self, ws: dict, run_id: str) -> dict:
        with self.service.lock:
            row = self.service.db.execute(
                f"SELECT {RUN_COLUMNS} FROM agent_runs WHERE id=? AND workspace=?",
                (run_id, ws["id"])).fetchone()
        if not row:
            raise BridgeError("Run not found in this workspace", "not_found")
        return dict(row)

    def _requests(self, run: dict) -> list[dict]:
        with self.service.lock:
            rows = self.service.db.execute(
                f"SELECT {REQUEST_COLUMNS} FROM agent_requests WHERE run=? ORDER BY created",
                (run["id"],)).fetchall()
        return [dict(r) for r in rows]

    def _pending(self, run: dict) -> list[dict]:
        return [r for r in self._requests(run) if r["state"] == "pending"]

    def _set_state(self, run_id: str, state: str, *, error_code=None,
                   error_message=None, finished=False) -> None:
        assert state in RUN_STATES
        assignments = ["state=?", "updated=?"]
        values: list = [state, self._clock()]
        if error_code is not None:
            assignments.append("error_code=?")
            values.append(error_code)
        if error_message is not None:
            assignments.append("error_message=?")
            values.append(error_message[:400])
        if finished:
            assignments.append("finished=?")
            values.append(self._clock())
        values.append(run_id)
        with self.service.lock, self.service.db:
            self.service.db.execute(f"UPDATE agent_runs SET {','.join(assignments)} WHERE id=?", values)

    def _set_state_if(self, run_id: str, expected: str, state: str) -> bool:
        """Compare-and-set transition; never overwrites a concurrent event state."""
        with self.service.lock, self.service.db:
            cursor = self.service.db.execute(
                "UPDATE agent_runs SET state=?,updated=? WHERE id=? AND state=?",
                (state, self._clock(), run_id, expected))
        return cursor.rowcount == 1

    def _mark_started(self, run_id: str) -> None:
        """Record prompt acceptance without erasing a concurrent waiting/terminal state."""
        self._set_state_if(run_id, "starting", "running")
        with self.service.lock, self.service.db:
            self.service.db.execute(
                "UPDATE agent_runs SET started=COALESCE(started,?) WHERE id=?",
                (self._clock(), run_id))

    @staticmethod
    def _floor_ms(run: dict) -> int:
        """Durable per-run message boundary in integer milliseconds."""
        try:
            return max(0, int(run.get("message_floor_ms") or 0))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _message_ts(message) -> int | None:
        """Latest credible integer-millisecond timestamp of one message."""
        latest = None
        for value in (message.created, message.completed):
            if isinstance(value, int) and not isinstance(value, bool):
                latest = value if latest is None else max(latest, value)
        return latest

    def _messages_after(self, messages, floor_ms: int) -> list:
        """Keep only messages created after this run's boundary.

        A floor of 0 (every fresh session) keeps the whole history. A
        positive floor drops anything without a credible post-floor
        timestamp so stale pre-continuation history can never complete,
        count for, or leak into the new iteration.
        """
        if not floor_ms:
            return list(messages)
        scoped = []
        for message in messages:
            stamp = self._message_ts(message)
            if stamp is not None and stamp > floor_ms:
                scoped.append(message)
        return scoped

    @staticmethod
    def _final_answer(messages, floor_ms: int = 0) -> str | None:
        """Return the last credible completed, non-error assistant response text.

        Only messages after the run's floor are considered, so a stale or
        duplicate idle with solely pre-continuation output returns None.
        Returns None when there is no such evidence (an empty/stale idle must
        not complete a run). Empty text from a genuinely completed assistant
        is allowed.
        """
        for message in reversed(list(messages)):
            stamp = None
            for value in (message.created, message.completed):
                if isinstance(value, int) and not isinstance(value, bool):
                    stamp = value if stamp is None else max(stamp, value)
            if floor_ms and (stamp is None or stamp <= floor_ms):
                continue
            if message.role == "assistant" and message.completed and not message.error:
                return message.text or ""
        return None

    def _workspace(self, run: dict) -> dict:
        with self.service.lock:
            return self.service.workspace(run["workspace"], False)

    def _revalidate_binding(self, ws: dict, run: dict):
        """Positively confirm the recorded session still resolves to this workspace.

        A missing session raises ``session_missing``; an observed directory that is
        absent or different raises ``session_mismatch``. Transient connectivity
        failures propagate as ``runtime_unavailable`` so callers can retry.
        """
        runtime = self._require_runtime()
        directory = self._directory(ws)
        session = runtime.get_session(directory, run["session"])
        if session is None:
            raise BridgeError("OpenCode session no longer exists", "session_missing")
        if not session.directory or not _same_directory(session.directory, directory):
            raise BridgeError("OpenCode session is bound to another directory", "session_mismatch")
        return session

    def _orphan(self, run: dict, code: str, message: str) -> None:
        with self.service.lock, self.service.db:
            self.service.db.execute(
                "UPDATE agent_requests SET state='orphaned',resolved=?,updated=? "
                "WHERE run=? AND state='pending'", (self._clock(), self._clock(), run["id"]))
        self._set_state(run["id"], "orphaned", error_code=code, error_message=message, finished=True)
        self.service.event(run["workspace"], "opencode_orphaned", code)

    def _complete(self, run: dict, summary: str, reason: str, *, message_count: int = 0,
                  transcript: list | None = None) -> dict:
        result = json.dumps({"summary": summary[:MAX_RESULT_CHARS], "reason": reason,
                             "message_count": message_count, "has_final_response": True})
        snapshot = json.dumps(transcript if transcript is not None else [])
        with self.service.lock, self.service.db:
            self.service.db.execute("UPDATE agent_runs SET result=?,transcript=? WHERE id=?",
                                    (result, snapshot, run["id"]))
        self._set_state(run["id"], "completed", finished=True)
        self.service.event(run["workspace"], "opencode_completed")
        self._notify(run, "completed")
        return self._summary(self._row(self._workspace(run), run["id"]))

    def _apply_permission_result(self, run: dict, permission_id: str, *,
                                 preferred_decision: str | None,
                                 resolved_state: str) -> tuple[dict | None, int]:
        """Resolve a pending request idempotently and return its final row + remaining count."""
        with self.service.lock, self.service.db:
            row = self.service.db.execute(
                f"SELECT {REQUEST_COLUMNS} FROM agent_requests WHERE workspace=? AND opencode_request=?",
                (run["workspace"], permission_id)).fetchone()
            if row is None:
                return None, self.service.db.execute(
                    "SELECT count(*) FROM agent_requests WHERE run=? AND state='pending'",
                    (run["id"],)).fetchone()[0]
            if row["state"] == "pending":
                self.service.db.execute(
                    "UPDATE agent_requests SET state=?,decision=?,resolved=?,updated=? "
                    "WHERE id=? AND state='pending'",
                    (resolved_state, preferred_decision, self._clock(), self._clock(), row["id"]))
            elif preferred_decision and not row["decision"]:
                # A concurrent permission.replied preserved state but no decision.
                self.service.db.execute(
                    "UPDATE agent_requests SET decision=?,updated=? WHERE id=? AND decision IS NULL",
                    (preferred_decision, self._clock(), row["id"]))
            final = self.service.db.execute(
                f"SELECT {REQUEST_COLUMNS} FROM agent_requests WHERE id=?", (row["id"],)).fetchone()
            remaining = self.service.db.execute(
                "SELECT count(*) FROM agent_requests WHERE run=? AND state='pending'",
                (run["id"],)).fetchone()[0]
        return (dict(final) if final else None), remaining

    def _notify(self, run: dict, state: str, *, request_kind: str = "", request_action: str = "") -> None:
        if state not in NOTIFIED_STATES:
            return
        try:
            with self.service.lock:
                ws = self.service.workspace(run["workspace"], False)
                job = self.service.db.execute("SELECT title FROM jobs WHERE id=?",
                                              (run["job"],)).fetchone()
            result = self.notifier.notify(state=state, workspace_name=ws["name"],
                                          handoff_title=job["title"] if job else "",
                                          run_id=run["id"], request_kind=request_kind,
                                          request_action=request_action, at=self._clock())
        except Exception:  # noqa: BLE001 - notification failure must never change run state
            result = None
        if result is None:
            return
        try:
            with self.service.lock, self.service.db:
                self.service.db.execute("UPDATE agent_runs SET notification=? WHERE id=?",
                                        (json.dumps({**result.public(), "at": self._clock()}),
                                         run["id"]))
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------- models
    # Model discovery is GLOBAL: the native server/provider configuration is
    # the source of available models and no workspace directory influences it.
    # ``ws`` is accepted on list_models only so the existing project-facing MCP
    # schema (which requires workspace_id on every tool) keeps working; it is
    # never used for discovery and the result always states scope="global".
    MODEL_POLICY_SETTING = "model_policy"
    MAX_POLICY_MODELS = 200

    @staticmethod
    def _check_selector_shape(selector: Any) -> str:
        text = selector if isinstance(selector, str) else ""
        if (not text or len(text) > 260 or any(c.isspace() for c in text)
                or any(ord(c) < 32 or ord(c) == 127 for c in text)):
            raise BridgeError(f"Model selector {selector!r} is malformed; "
                              "use an exact canonical provider/model selector",
                              "invalid_arguments")
        return text

    def _global_models(self) -> list[ModelInfo]:
        runtime = self._require_runtime()
        try:
            return runtime.list_models()
        except RuntimeUnavailable as exc:
            raise BridgeError(str(exc), exc.code) from None

    def get_model_policy(self) -> dict | None:
        """Return the saved global policy or None when never configured.

        The legacy ``default_model`` setting is deliberately never consulted:
        it must not silently grant a model after upgrade.
        """
        raw = self.service.setting(self.MODEL_POLICY_SETTING)
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except ValueError:
            return None
        if not isinstance(data, dict):
            return None
        enabled = data.get("enabled")
        default = data.get("default")
        if (not isinstance(enabled, list) or not enabled
                or not isinstance(default, str) or not default):
            return None
        if any(not isinstance(item, str) or not item for item in enabled):
            return None
        if default not in enabled:
            return None
        return {"enabled": list(enabled), "default": default}

    def model_policy_status(self) -> dict:
        policy = self.get_model_policy()
        if policy is None:
            return {"configured": False, "enabled": [], "default": None, "enabled_count": 0}
        return {"configured": True, "enabled": policy["enabled"],
                "default": policy["default"], "enabled_count": len(policy["enabled"])}

    def set_model_policy(self, enabled: list, default: str) -> dict:
        """Atomically save the global enabled set + mandatory default.

        Local-admin only (exposed solely on the loopback manager route; MCP
        has no mutation path). Every selector must currently exist in the
        global runtime model list; the default must be enabled.
        """
        if not isinstance(enabled, list) or not enabled or len(enabled) > self.MAX_POLICY_MODELS:
            raise BridgeError("Model policy requires 1..200 enabled selectors", "invalid_arguments")
        clean = [self._check_selector_shape(item) for item in enabled]
        if len(set(clean)) != len(clean):
            raise BridgeError("Model policy contains duplicate selectors", "invalid_arguments")
        default = self._check_selector_shape(default)
        if default not in clean:
            raise BridgeError("The default model must be one of the enabled models",
                              "invalid_arguments")
        available = {m.selector for m in self._global_models()}
        for selector in clean:
            if selector not in available:
                raise BridgeError(f"OpenCode model {selector!r} is not available globally",
                                  "model_unavailable")
        with self.service.lock, self.service.db:
            self.service.set_setting(self.MODEL_POLICY_SETTING,
                                     json.dumps({"enabled": clean, "default": default}))
        self.service.event(None, "set_model_policy")
        return self.model_policy_status()

    def _policy_public(self, models: list[ModelInfo]) -> list[dict]:
        policy = self.get_model_policy()
        enabled = set(policy["enabled"]) if policy else set()
        default = policy["default"] if policy else None
        return [{**m.public(), "enabled": m.selector in enabled,
                 "policy_default": m.selector == default} for m in models]

    def list_models(self, ws: dict | None = None, query: str = "", limit: int = 25) -> dict:
        models = self._global_models()
        needle = (query or "").strip().casefold()
        if needle:
            models = [m for m in models if needle in m.selector.casefold()
                      or needle in m.provider.casefold() or needle in m.model.casefold()
                      or needle in (m.name or "").casefold()]
        models = models[:max(1, min(int(limit), 100))]
        policy = self.model_policy_status()
        result: dict = {"scope": "global", "query": query, "count": len(models),
                        "models": self._policy_public(models),
                        "policy": policy,
                        "selection": ("New runs use the configured global default when no model is given; "
                                      "an explicit model is allowed only when its exact selector is in the "
                                      "admin-enabled list and currently available. MCP cannot change "
                                      "the policy.")}
        if ws is not None:
            result["workspace_id"] = ws["id"]
        return result

    def _resolve_model(self, selector: str) -> ModelInfo:
        models = self._global_models()
        for model in models:
            if selector == model.selector or selector == f"{model.provider}/{model.model}":
                return model
        candidates = [m.selector for m in models if selector.casefold() in m.selector.casefold()][:10]
        if not candidates:
            candidates = [m.selector for m in models][:10]
        hint = ", ".join(candidates) or "none"
        raise BridgeError(f"OpenCode model {selector!r} is not available globally; "
                          f"candidates: {hint}", "model_unavailable")

    def _require_policy_selector(self, model: str | None) -> str:
        """Fail closed: no policy means no new run; the enabled list is the boundary.

        - model omitted -> configured default;
        - explicit model in the enabled list and currently available -> allowed;
        - explicit model not in the enabled list -> model_not_enabled;
        - enabled selector currently unavailable -> model_unavailable;
        - no policy -> model_policy_unconfigured.
        Whether ChatGPT *should* choose a non-default enabled model is a
        project-lead skill/intent rule, not a server lock.
        """
        policy = self.get_model_policy()
        if policy is None:
            raise BridgeError("No global model policy is configured; the local administrator "
                              "must save enabled models and a default in the manager first",
                              "model_policy_unconfigured")
        if model is None:
            selector = policy["default"]
        else:
            selector = self._check_selector_shape(model)
            if selector not in policy["enabled"]:
                raise BridgeError(f"OpenCode model {selector!r} is not enabled in the global policy; "
                                  "the local administrator must enable it or choose an enabled model",
                                  "model_not_enabled")
        resolved = self._resolve_model(selector)
        if resolved.selector not in policy["enabled"]:
            # The policy changed (or discovery shrank) after the policy was
            # saved: the exact default/enabled selector is currently unavailable.
            if selector == policy["default"] or model is None:
                raise BridgeError("The configured default model is no longer enabled",
                                  "model_disabled")
            raise BridgeError(f"OpenCode model {selector!r} is enabled but currently unavailable",
                              "model_unavailable")
        return resolved.selector

    # --------------------------------------------------------------------- start
    def start_run(self, ws: dict, job_id: str, request_id: str, model: str | None = None,
                  parent_run_id: str | None = None,
                  continue_from_run_id: str | None = None) -> dict:
        if not ws.get("agent_enabled"):
            raise BridgeError("Agent execution is not enabled for this workspace", "agent_disabled")
        job = self.service.job(ws, job_id)
        if job["state"] != "prepared":
            raise BridgeError("Only a prepared handoff can start a run", "conflict")
        if continue_from_run_id is not None:
            return self._start_continuation(ws, job, request_id, model=model,
                                             parent_run_id=parent_run_id,
                                             continue_from_run_id=continue_from_run_id)
        # Fail closed before any OpenCode session is created: the global
        # enabled allowlist + default decides the exact model. The unconfigured
        # check runs before the runtime is required so a missing policy is
        # reported even when the adapter is down.
        selector = self._require_policy_selector(model)
        runtime = self._require_runtime()
        request_hash = digest(json.dumps({"job": job_id, "model": selector,
                                          "parent": parent_run_id or ""},
                                         sort_keys=True).encode())
        existing = self.service.db.execute(
            f"SELECT {RUN_COLUMNS} FROM agent_runs WHERE workspace=? AND request_id=?",
            (ws["id"], request_id)).fetchone()
        if existing:
            if existing["request_hash"] != request_hash:
                raise BridgeError("request_id already used with different content", "conflict")
            return self._summary(dict(existing), idempotent=True)
        count = self.service.db.execute("SELECT count(*) FROM agent_runs WHERE workspace=?",
                                        (ws["id"],)).fetchone()[0]
        if count >= MAX_RUNS_PER_WORKSPACE:
            raise BridgeError("Run quota reached for this workspace", "storage_limit")
        if parent_run_id is not None:
            self._row(ws, parent_run_id)  # must belong to this workspace

        provider, _, name = selector.partition("/")
        model_ref = {"providerID": provider, "modelID": name}

        directory = self._directory(ws)
        session = runtime.create_session(directory, title=job["title"])
        if not _same_directory(session.directory, directory):
            try:
                runtime.abort_session(directory, session.id)
            except BridgeError:
                pass
            raise BridgeError("OpenCode session is not bound to the mapped workspace", "session_mismatch")

        run_id = uid("run_")
        created = self._clock()
        with self.service.lock, self.service.db:
            self.service.db.execute(
                "INSERT INTO agent_runs (id,workspace,job,request_id,request_hash,parent_run,session,"
                "model,state,error_code,error_message,result,notification,created,started,updated,finished,"
                "message_floor_ms,session_reused,transcript) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, ws["id"], job_id, request_id, request_hash, parent_run_id, session.id,
                 selector, "starting", None, None, "{}", "{}", created, None, created, None,
                 0, 0, "[]"))
        self.service.event(ws["id"], "start_opencode_run")
        prompt = self.handoff_prompt(ws, job)
        if self.background:
            thread = threading.Thread(target=self._submit, args=(ws["id"], run_id, prompt, model_ref),
                                      name=f"opencode-submit-{run_id}", daemon=True)
            with self._submission_condition:
                self._submissions.add(thread)
            thread.start()
        else:
            self._submit(ws["id"], run_id, prompt, model_ref)
        return self._summary(self._row(ws, run_id), idempotent=False)

    def _start_continuation(self, ws: dict, job: dict, request_id: str, *,
                            model: str | None, parent_run_id: str | None,
                            continue_from_run_id: str) -> dict:
        """Create a new Bridge run that reuses a completed run's OpenCode session.

        Never creates a session and never falls back to a fresh one: every
        validation failure raises before any row, session or prompt exists.
        """
        try:
            source = self._row(ws, continue_from_run_id)
        except BridgeError:
            raise BridgeError("Continuation source run not found in this workspace",
                              "continuation_unavailable") from None
        if parent_run_id is not None and parent_run_id != continue_from_run_id:
            raise BridgeError("parent_run_id conflicts with continue_from_run_id; "
                              "continuation implies parent_run_id=continue_from_run_id",
                              "invalid_arguments")
        if source["state"] != "completed":
            raise BridgeError(f"Continuation source run is {source['state']}; "
                              "only a completed run can be continued",
                              "continuation_unavailable")
        if not source["session"]:
            raise BridgeError("Continuation source run has no OpenCode session",
                              "continuation_unavailable")
        if self._pending(source):
            raise BridgeError("Continuation source run still has pending requests",
                              "continuation_unavailable")
        # Continuation keeps the source run's exact model; a model change
        # requires a fresh run/session.
        if model is not None and model != source["model"]:
            raise BridgeError("Continuation model must exactly equal the source run's model; "
                              "use a fresh run to change models",
                              "continuation_model_mismatch")
        effective = source["model"]
        policy = self.get_model_policy()
        if policy is None:
            raise BridgeError("No global model policy is configured; the local administrator "
                              "must save enabled models and a default in the manager first",
                              "model_policy_unconfigured")
        if effective not in policy["enabled"]:
            raise BridgeError(f"OpenCode model {effective!r} is not enabled in the global policy",
                              "model_not_enabled")
        resolved = self._resolve_model(effective)
        if resolved.selector not in policy["enabled"]:
            raise BridgeError(f"OpenCode model {effective!r} is enabled but currently unavailable",
                              "model_unavailable")
        selector = resolved.selector
        request_hash = digest(json.dumps({"job": job["id"], "model": selector,
                                          "parent": continue_from_run_id,
                                          "continue_from": continue_from_run_id},
                                         sort_keys=True).encode())
        existing = self.service.db.execute(
            f"SELECT {RUN_COLUMNS} FROM agent_runs WHERE workspace=? AND request_id=?",
            (ws["id"], request_id)).fetchone()
        if existing:
            if existing["request_hash"] != request_hash:
                raise BridgeError("request_id already used with different content", "conflict")
            return self._summary(dict(existing), idempotent=True)
        count = self.service.db.execute("SELECT count(*) FROM agent_runs WHERE workspace=?",
                                        (ws["id"],)).fetchone()[0]
        if count >= MAX_RUNS_PER_WORKSPACE:
            raise BridgeError("Run quota reached for this workspace", "storage_limit")
        runtime = self._require_runtime()
        directory = self._directory(ws)
        # Binding revalidation immediately before insert/submit: missing or
        # rebound sessions keep their own fail-closed codes.
        session = runtime.get_session(directory, source["session"])
        if session is None:
            raise BridgeError("OpenCode session no longer exists", "session_missing")
        if not session.directory or not _same_directory(session.directory, directory):
            raise BridgeError("OpenCode session is bound to another directory", "session_mismatch")
        with self.service.lock:
            active = self.service.db.execute(
                "SELECT id FROM agent_runs WHERE session=? AND state IN "
                "('starting','running','waiting_permission','waiting_question')",
                (source["session"],)).fetchall()
        if active:
            raise BridgeError("OpenCode session already has an active Bridge run; "
                              "wait for it or cancel it before continuing",
                              "continuation_unavailable")
        # Never send a follow-up into a busy session: an open v1.18.x issue
        # can persist the prompt without scheduling it, losing the iteration.
        try:
            status = runtime.session_status(directory, source["session"])
        except BridgeError as exc:
            if exc.code in ("runtime_unsupported", "not_found"):
                raise BridgeError("OpenCode session status is not available from the "
                                  "installed runtime; continuation refused",
                                  "continuation_unavailable") from None
            raise BridgeError("OpenCode session status is unavailable; continuation refused",
                              "continuation_unavailable") from None
        if status in ("busy", "retry"):
            raise BridgeError("OpenCode session is busy; continuation refused",
                              "session_busy")
        if status != "idle":
            raise BridgeError("OpenCode session status is unknown; continuation refused",
                              "continuation_unavailable")
        # Durable pre-prompt message boundary from the validated idle
        # session: the new run owns only messages after this timestamp.
        try:
            history = runtime.messages(directory, source["session"], limit=100)
        except BridgeError:
            raise BridgeError("Prior session history is unavailable; continuation refused",
                              "continuation_unavailable") from None
        floor = 0
        seen_stamp = False
        for message in history:
            stamp = self._message_ts(message)
            if stamp is not None:
                seen_stamp = True
                floor = max(floor, stamp)
        if history and not seen_stamp:
            raise BridgeError("No reliable message boundary in the existing session; "
                              "continuation refused", "continuation_unavailable") from None
        provider, _, name = selector.partition("/")
        model_ref = {"providerID": provider, "modelID": name}
        run_id = uid("run_")
        created = self._clock()
        try:
            with self.service.lock, self.service.db:
                self.service.db.execute(
                    "INSERT INTO agent_runs (id,workspace,job,request_id,request_hash,parent_run,session,"
                    "model,state,error_code,error_message,result,notification,created,started,updated,finished,"
                    "message_floor_ms,session_reused,transcript) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (run_id, ws["id"], job["id"], request_id, request_hash, continue_from_run_id,
                     source["session"], selector, "starting", None, None, "{}", "{}",
                     created, None, created, None, floor, 1, "[]"))
        except sqlite3.IntegrityError:
            raise BridgeError("OpenCode session already has an active Bridge run",
                              "continuation_unavailable") from None
        self.service.event(ws["id"], "start_opencode_run")
        prompt = self.handoff_prompt(ws, job, continuation=True)
        if self.background:
            thread = threading.Thread(target=self._submit, args=(ws["id"], run_id, prompt, model_ref),
                                      name=f"opencode-submit-{run_id}", daemon=True)
            with self._submission_condition:
                self._submissions.add(thread)
            thread.start()
        else:
            self._submit(ws["id"], run_id, prompt, model_ref)
        return self._summary(self._row(ws, run_id), idempotent=False)

    def _submit(self, workspace_id: str, run_id: str, prompt: str, model_ref: dict | None) -> None:
        try:
            with self.service.lock:
                ws = self.service.workspace(workspace_id, False)
                run = self.service.db.execute(
                    f"SELECT {RUN_COLUMNS} FROM agent_runs WHERE id=?", (run_id,)).fetchone()
            if not run:
                return
            run = dict(run)
            if run.get("session_reused"):
                # The session was idle at validation; recheck at dispatch so
                # a race can never send a follow-up into a busy session and
                # lose it to the v1.18.x prompt_async scheduling issue.
                try:
                    dispatch_status = self.runtime.session_status(
                        self._directory(ws), run["session"])
                except BridgeError as exc:
                    self._set_state(run_id, "failed", error_code="continuation_unavailable",
                                    error_message="Session status unavailable at dispatch; "
                                                  "prompt was not sent", finished=True)
                    with self.service.lock:
                        row = self.service.db.execute(
                            "SELECT workspace FROM agent_runs WHERE id=?", (run_id,)).fetchone()
                    if row:
                        self._notify({"id": run_id, "workspace": row["workspace"], "job": ""}, "failed")
                    return
                if dispatch_status != "idle":
                    self._set_state(run_id, "failed", error_code="session_busy",
                                    error_message="Session became busy before dispatch; "
                                                  "prompt was not sent", finished=True)
                    with self.service.lock:
                        row = self.service.db.execute(
                            "SELECT workspace FROM agent_runs WHERE id=?", (run_id,)).fetchone()
                    if row:
                        self._notify({"id": run_id, "workspace": row["workspace"], "job": ""}, "failed")
                    return
            self.runtime.prompt_async(self._directory(ws), run["session"], prompt, model_ref)
            # The event pump may already have moved the run to waiting_permission,
            # failed or cancelled while prompt_async was in flight. Only a run still
            # starting is advanced to running; any event-written state is preserved.
            self._mark_started(run_id)
        except BridgeError as exc:
            self._set_state(run_id, "failed", error_code=exc.code, error_message=str(exc), finished=True)
            run = None
            with self.service.lock:
                row = self.service.db.execute("SELECT workspace FROM agent_runs WHERE id=?",
                                              (run_id,)).fetchone()
            if row:
                self._notify({"id": run_id, "workspace": row["workspace"], "job": ""}, "failed")
        except Exception:  # noqa: BLE001
            self._set_state(run_id, "failed", error_code="runtime_error",
                            error_message="Prompt submission failed", finished=True)
        finally:
            with self._submission_condition:
                self._submissions.discard(threading.current_thread())

    def handoff_prompt(self, ws: dict, job: dict, *, continuation: bool = False) -> str:
        directory = str(ws["root"])
        folder = f"{HANDOFF}/jobs/{job['id']}"
        task = f"{directory}/{folder}/TASK.md"
        prompt = (
            f"Work only in this project: {json.dumps(directory)}. "
            f"Read {json.dumps(task)}, CONTEXT.md and ACCEPTANCE.md in the same folder. "
            "Preserve pre-existing edits. Follow the ordered plan and satisfy every acceptance criterion. "
            "Run the agreed tests and checks locally. Do not weaken tests or invent results. "
            "Stop and report a blocker rather than guess if the current source contradicts the plan, "
            "the installed API differs from the handoff assumptions, the change would exceed scope, "
            "or agreed checks keep failing after a bounded in-scope correction. "
            "Do not commit, push, tag, publish, deploy, rotate credentials, or perform unrelated cleanup. "
            "Do not edit the handoff documents. When finished, reply in this conversation with a concise "
            "implementation summary, every affected file path, the exact test/check commands and outcomes, "
            "any failures or checks not run, and remaining risks or blockers. "
            "No RESULT.md or TESTS.json file is required."
        )
        if continuation:
            prompt += (" This is a corrective follow-up handoff in this existing session: "
                       "the new handoff documents and acceptance criteria control this iteration.")
        return prompt

    # ------------------------------------------------------------------ events
    def _pump_loop(self) -> None:
        while not self._stop.is_set():
            try:
                events, cursor = self.runtime.poll_events(self._cursor, timeout=25.0)
            except BridgeError:
                self._stop.wait(5.0)
                continue
            except Exception:  # noqa: BLE001
                self._stop.wait(5.0)
                continue
            self._cursor = cursor
            try:
                self.service.set_setting("runtime_cursor", str(cursor))
            except BridgeError:
                pass
            for event in events:
                try:
                    self.handle_event(event)
                except BridgeError:
                    self.service.event(None, "opencode_event_rejected", "failed")

    def handle_event(self, event: dict) -> None:
        kind = event.get("type")
        session_id = event.get("session_id")
        if not session_id:
            permission = event.get("permission")
            if isinstance(permission, dict):
                session_id = permission.get("session_id")
        if not session_id:
            return
        run = self._active_run_for_session(session_id)
        if run is None:
            return  # No sole active owner: historical, unowned or ambiguous.
        if run["state"] in TERMINAL_RUN_STATES:
            return
        if kind in ("permission.asked", "permission.updated"):
            self._on_permission(run, event.get("permission") or {})
        elif kind == "permission.replied":
            self._on_permission_replied(run, event.get("permission_id"), event.get("response"))
        elif kind and "question" in kind.casefold():
            self._on_question(run, event)
        elif kind == "session.idle":
            self._on_idle(run)
        elif kind == "session.error":
            self._on_error(run, event.get("error") or {})

    def _active_run_for_session(self, session_id: str) -> dict | None:
        """Resolve the single Bridge run that currently owns an OpenCode session.

        Only starting/running/waiting rows are owners. Zero matches means the
        event is historical or unowned and must not mutate anything; more
        than one match is recorded and mutates nothing (fail closed).
        """
        with self.service.lock:
            rows = self.service.db.execute(
                f"SELECT {RUN_COLUMNS} FROM agent_runs WHERE session=? AND state IN "
                "('starting','running','waiting_permission','waiting_question')",
                (session_id,)).fetchall()
        if not rows:
            return None
        if len(rows) > 1:
            self.service.event(None, "opencode_ambiguous_session", "failed")
            return None
        return dict(rows[0])

    def _on_permission(self, run: dict, permission: dict) -> None:
        if run["state"] in TERMINAL_RUN_STATES:
            return
        permission_id = str(permission.get("id") or "")
        if not permission_id:
            return
        with self.service.lock:
            existing = self.service.db.execute(
                "SELECT id,state FROM agent_requests WHERE workspace=? AND opencode_request=?",
                (run["workspace"], permission_id)).fetchone()
        if existing:
            if existing["state"] != "pending":
                return
            request_id = existing["id"]
        else:
            # Real V1 ask: `pattern` is OpenCode's exact proposed always
            # scope; `requested_patterns` is what is being requested and
            # stays separately reviewable in metadata. agent_requests.pattern
            # remains the exact always scope used for always_allowed.
            scope = _bounded_str_list(permission.get("pattern")
                                      if "pattern" in permission else permission.get("always"))
            requested = _bounded_str_list(
                permission.get("requested_patterns")
                if "requested_patterns" in permission else permission.get("patterns")
                if "patterns" in permission else permission.get("pattern")
                if "pattern" in permission else permission.get("always"))
            raw_tool = permission.get("tool")
            if isinstance(raw_tool, str):
                tool: Any = raw_tool[:200]
            elif isinstance(raw_tool, (dict, list)):
                tool, _ = sanitize_metadata(raw_tool)
            else:
                tool = None
            base_metadata = permission.get("metadata") or {}
            if not isinstance(base_metadata, dict):
                base_metadata = {}
            metadata, _ = sanitize_metadata(base_metadata)
            if not isinstance(metadata, dict):
                metadata = {}
            # Keep the requested target human-reviewable without touching
            # the exact always scope stored in `pattern`.
            metadata = dict(metadata)
            metadata["requested_patterns"] = list(requested)
            if tool is not None:
                metadata["tool"] = tool
            metadata_json = json.dumps(metadata, default=str)[:8000]
            title = str(permission.get("title") or "")[:300]
            tool_text = ""
            if isinstance(tool, str):
                tool_text = tool[:120]
            elif isinstance(tool, dict):
                for key in ("name", "tool", "title"):
                    value = tool.get(key)
                    if isinstance(value, str) and value.strip():
                        tool_text = value.strip()[:120]
                        break
            requested_text = ", ".join(requested[:4])[:300]
            if title:
                resource = title
                explanation = title
            elif tool_text and requested_text:
                resource = f"{tool_text} {requested_text}"[:300]
                explanation = resource
            elif tool_text:
                resource = tool_text[:300]
                explanation = resource
            elif requested_text:
                # Safe display only: the first requested targets, never a
                # broadened approval scope.
                resource = requested_text[:300]
                explanation = resource
            else:
                resource = ""
                explanation = ""
            request_id = uid("req_")
            with self.service.lock, self.service.db:
                self.service.db.execute(
                    "INSERT OR IGNORE INTO agent_requests (id,run,workspace,session,opencode_request,kind,"
                    "action,resource,pattern,metadata,explanation,redacted,state,decision,created,updated,resolved) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (request_id, run["id"], run["workspace"], run["session"], permission_id, "permission",
                     str(permission.get("action") or permission.get("permission") or "")[:120],
                     resource,
                     json.dumps(scope)[:4000], metadata_json,
                     explanation, 1 if permission.get("redacted") else 0,
                     "pending", None, self._clock(), self._clock(), None))
            self._set_state(run["id"], "waiting_permission")
            self.service.event(run["workspace"], "opencode_waiting_permission")
            self._notify(run, "waiting_permission", request_kind="permission",
                         request_action=str(permission.get("action")
                                            or permission.get("permission") or "")[:80])

    def _on_question(self, run: dict, event: dict) -> None:
        if run["state"] in TERMINAL_RUN_STATES:
            return
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        request_id = uid("req_")
        opencode_request = str(event.get("id") or data.get("id") or request_id)[:200]
        with self.service.lock, self.service.db:
            self.service.db.execute(
                "INSERT OR IGNORE INTO agent_requests (id,run,workspace,session,opencode_request,kind,"
                "action,resource,pattern,metadata,explanation,redacted,state,decision,created,updated,resolved) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (request_id, run["id"], run["workspace"], run["session"], opencode_request, "question",
                 str(data.get("action") or event.get("type") or "question")[:120], "", "[]", "{}",
                 "Question waits are visible but the installed OpenCode API exposes no question reply; "
                 "inspect the session transcript locally.", 0, "pending", None,
                 self._clock(), self._clock(), None))
        self._set_state(run["id"], "waiting_question")
        self.service.event(run["workspace"], "opencode_waiting_question")
        self._notify(run, "waiting_question", request_kind="question",
                     request_action=str(data.get("action") or "")[:80])

    def _on_permission_replied(self, run: dict, permission_id: str | None,
                               response: str | None = None) -> None:
        if not permission_id:
            return
        decision = response if response in PERMISSIONS else None
        resolved_state = {"once": "approved", "always": "approved", "reject": "rejected"}.get(
            decision, "resolved")
        final, remaining = self._apply_permission_result(
            run, permission_id, preferred_decision=decision, resolved_state=resolved_state)
        if final is None:
            return
        current = self._row(self._workspace(run), run["id"])
        if remaining == 0 and current["state"] in WAITING_RUN_STATES:
            self._set_state(run["id"], "running")

    def _on_idle(self, run: dict) -> None:
        if run["state"] in TERMINAL_RUN_STATES:
            return
        if not run.get("started"):
            # Prompt acceptance is not yet proven; a pre-start idle must never complete.
            return
        if self._pending(run):
            return
        self.finalize_run(run, reason="idle")

    def _on_error(self, run: dict, error: dict) -> None:
        if run["state"] in TERMINAL_RUN_STATES:
            return
        name = str(error.get("name") or "error")[:80]
        message = str(error.get("message") or "OpenCode session error")[:300]
        self._set_state(run["id"], "failed", error_code=name or "session_error",
                        error_message=message, finished=True)
        self.service.event(run["workspace"], "opencode_failed")
        self._notify(run, "failed")

    def finalize_run(self, run: dict, *, reason: str = "result") -> dict:
        """Complete only on credible final evidence; otherwise preserve the active state."""
        current = self._row(self._workspace(run), run["id"])
        if current["state"] in TERMINAL_RUN_STATES:
            return self._summary(current)
        if not current.get("started") or self._pending(current):
            return self._summary(current)
        try:
            ws = self._workspace(current)
            self._revalidate_binding(ws, current)
            messages = self._require_runtime().messages(self._directory(ws), current["session"], limit=40)
        except BridgeError as exc:
            if exc.code in ("session_missing", "session_mismatch"):
                self._orphan(current, exc.code, str(exc))
            # Transient runtime failures leave the run explicitly active for retry.
            return self._summary(self._row(self._workspace(current), current["id"]))
        floor = self._floor_ms(current)
        scoped = self._messages_after(messages, floor)
        final = self._final_answer(scoped)
        if final is None:
            # No completed, non-error assistant response after this run's
            # boundary: do not invent completion from stale history.
            return self._summary(current)
        transcript, _ = self._transcript(scoped)
        return self._complete(current, final, reason, message_count=len(scoped),
                              transcript=transcript)

    # ------------------------------------------------------------- permission
    def respond_permission(self, ws: dict, run_id: str, request_id: str, decision: str) -> dict:
        if decision not in PERMISSIONS:
            raise BridgeError("decision must be once, always or reject", "invalid_arguments")
        run = self._row(ws, run_id)
        with self.service.lock:
            row = self.service.db.execute(
                f"SELECT {REQUEST_COLUMNS} FROM agent_requests "
                "WHERE workspace=? AND run=? AND opencode_request=?",
                (ws["id"], run_id, request_id)).fetchone()
        if not row:
            raise BridgeError("Pending OpenCode request not found for this run", "not_found")
        request = dict(row)
        if request["kind"] != "permission":
            raise BridgeError("Only permission requests can be answered remotely", "runtime_unsupported")
        if request["state"] != "pending":
            raise BridgeError("Request is no longer pending", "conflict")
        if request["session"] != run["session"] or run["state"] not in WAITING_RUN_STATES:
            raise BridgeError("Request is not bound to an active wait for this run", "conflict")
        pattern = json.loads(request["pattern"] or "[]")
        if decision == "always" and not pattern:
            raise BridgeError("OpenCode did not expose an approval scope for 'always'; "
                              "use once or reject", "always_scope_unknown")
        # Reconfirm the recorded session still belongs to this mapped workspace.
        self._revalidate_binding(ws, run)
        runtime = self._require_runtime()
        ok = runtime.respond_permission(self._directory(ws), run["session"], request_id, decision)
        if not ok:
            raise BridgeError("OpenCode did not confirm the permission response", "runtime_rejected")
        resolved_state = "approved" if decision in ("once", "always") else "rejected"
        final, remaining = self._apply_permission_result(
            run, request_id, preferred_decision=decision, resolved_state=resolved_state)
        if final is None:
            raise BridgeError("Pending OpenCode request not found for this run", "not_found")
        current = self._row(ws, run_id)
        if remaining == 0 and current["state"] in WAITING_RUN_STATES:
            self._set_state(run_id, "running")
            current = self._row(ws, run_id)
        self.service.event(ws["id"], "respond_opencode_permission", decision)
        return {"run_id": run_id, "request_id": request_id, "decision": final["decision"] or decision,
                "request_state": final["state"], "run_state": current["state"],
                "resumed_same_session": True, "scope": pattern,
                "note": "The same OpenCode session remains the execution owner."}

    # ------------------------------------------------------------------ cancel
    def cancel_run(self, ws: dict, run_id: str) -> dict:
        run = self._row(ws, run_id)
        if run["state"] in TERMINAL_RUN_STATES:
            return {"run_id": run_id, "state": run["state"], "cancelled": False,
                    "note": "Run is already terminal"}
        runtime = self._require_runtime()
        try:
            self._revalidate_binding(ws, run)
        except BridgeError as exc:
            if exc.code in ("session_missing", "session_mismatch"):
                self._orphan(run, exc.code, str(exc))
                return {"run_id": run_id, "state": "orphaned", "cancelled": False,
                        "note": "Recorded session is no longer usable; the run is orphaned"}
            raise
        try:
            ok = runtime.abort_session(self._directory(ws), run["session"])
        except BridgeError:
            ok = False
        if not ok:
            with self.service.lock, self.service.db:
                self.service.db.execute(
                    "UPDATE agent_runs SET error_code='abort_uncertain',error_message=? WHERE id=?",
                    ("Abort outcome was not confirmed", run_id))
            self.service.event(ws["id"], "cancel_opencode_run", "abort_uncertain")
            raise BridgeError("Abort outcome was not confirmed; run state is unchanged", "abort_uncertain")
        self._set_state(run_id, "cancelled", finished=True)
        self.service.event(ws["id"], "cancel_opencode_run")
        self._notify(run, "cancelled")
        return {"run_id": run_id, "state": "cancelled", "cancelled": True}

    # ------------------------------------------------------------------- read
    def _summary(self, run: dict, *, idempotent: bool = False) -> dict:
        pending = [r for r in self._requests(run) if r["state"] == "pending"]
        try:
            notification = json.loads(run.get("notification") or "{}")
        except ValueError:
            notification = {}
        reused = bool(run.get("session_reused"))
        return {"run_id": run["id"], "workspace_id": run["workspace"], "job_id": run["job"],
                "parent_run_id": run["parent_run"], "request_id": run["request_id"],
                "continue_from_run_id": run["parent_run"] if reused else None,
                "session_reused": reused,
                "state": run["state"], "model": run["model"], "session_id": run["session"],
                "created": run["created"], "started": run["started"], "updated": run["updated"],
                "finished": run["finished"],
                "duration_seconds": _duration(run["started"], run["finished"]),
                "notification": notification,
                "pending_request_count": len(pending), "idempotent_replay": idempotent,
                "active": run["state"] in ACTIVE_RUN_STATES}

    def list_runs(self, ws: dict, offset: int = 0, limit: int = 20) -> dict:
        with self.service.lock:
            rows = self.service.db.execute(
                f"SELECT {RUN_COLUMNS} FROM agent_runs WHERE workspace=? "
                "ORDER BY created DESC LIMIT ? OFFSET ?",
                (ws["id"], limit + 1, offset)).fetchall()
        return {"workspace_id": ws["id"],
                "runs": [self._summary(dict(r)) for r in rows[:limit]],
                "next_offset": offset + limit if len(rows) > limit else None}

    MAX_GLOBAL_SESSIONS_LIMIT = 50

    def list_all_runs(self, offset: int = 0, limit: int = 25) -> dict:
        """Global operational overview: Bridge-owned runs across all workspaces.

        Newest first, bounded. Only rows the bridge persisted itself are
        returned; sessions created directly in the native OpenCode server are
        never enumerated and no arbitrary session IDs are accepted here.
        """
        limit = max(1, min(int(limit), self.MAX_GLOBAL_SESSIONS_LIMIT))
        offset = max(0, int(offset))
        with self.service.lock:
            rows = self.service.db.execute(
                f"SELECT {RUN_COLUMNS} FROM agent_runs "
                "ORDER BY created DESC LIMIT ? OFFSET ?",
                (limit + 1, offset)).fetchall()
            enriched = []
            for row in rows[:limit]:
                run = dict(row)
                view = self._summary(run)
                try:
                    view["workspace_name"] = self.service.workspace(run["workspace"], False)["name"]
                except BridgeError:
                    view["workspace_name"] = ""
                job = self.service.db.execute("SELECT title FROM jobs WHERE id=?",
                                              (run["job"],)).fetchone()
                view["handoff_title"] = job["title"] if job else ""
                enriched.append(view)
        return {"scope": "global", "runs": enriched,
                "next_offset": offset + limit if len(rows) > limit else None}

    def read_run(self, ws: dict, run_id: str, *, include_transcript: bool = False,
                 limit: int = 40) -> dict:
        run = self._row(ws, run_id)
        view = self._summary(run)
        view["agent_evidence"] = "unverified"
        view["error"] = ({"code": run["error_code"], "message": run["error_message"]}
                         if run["error_code"] or run["error_message"] else None)
        try:
            result = json.loads(run["result"] or "{}")
        except ValueError:
            result = {}
        view["result"] = {"summary": result.get("summary", ""),
                          "reason": result.get("reason", ""),
                          "has_final_response": bool(result.get("has_final_response")),
                          "message_count": result.get("message_count", 0)}
        view["pending_requests"] = [self._request_public(r) for r in self._pending(run)]
        view["requests"] = [self._request_public(r) for r in self._requests(run)]
        if include_transcript and run["session"]:
            try:
                persisted = json.loads(run.get("transcript") or "[]")
            except ValueError:
                persisted = []
            if run["state"] in TERMINAL_RUN_STATES and persisted:
                # A completed run reports its own iteration snapshot so a
                # later continuation sharing the session cannot contaminate it.
                view["transcript"] = persisted
            elif run["state"] in TERMINAL_RUN_STATES and self._session_has_later_run(run):
                # Legacy row without a snapshot whose session was reused
                # later: fail safe instead of showing another iteration.
                view["transcript"] = [{"error": "transcript unavailable"}]
            else:
                try:
                    self._revalidate_binding(ws, run)
                    runtime = self._require_runtime()
                    messages = runtime.messages(self._directory(ws), run["session"], limit=limit)
                    scoped = self._messages_after(messages, self._floor_ms(run))
                    transcript, _ = self._transcript(scoped)
                    view["transcript"] = transcript
                except BridgeError:
                    view["transcript"] = [{"error": "transcript unavailable"}]
        return view

    def _session_has_later_run(self, run: dict) -> bool:
        """Whether a newer Bridge run shares this run's OpenCode session."""
        with self.service.lock:
            count = self.service.db.execute(
                "SELECT count(*) FROM agent_runs WHERE session=? AND id<>? AND created>?",
                (run["session"], run["id"], run["created"])).fetchone()[0]
        return count > 0

    def read_request(self, ws: dict, run_id: str, request_id: str) -> dict:
        run = self._row(ws, run_id)
        with self.service.lock:
            row = self.service.db.execute(
                f"SELECT {REQUEST_COLUMNS} FROM agent_requests "
                "WHERE workspace=? AND run=? AND opencode_request=?",
                (ws["id"], run_id, request_id)).fetchone()
        if not row:
            raise BridgeError("Pending OpenCode request not found for this run", "not_found")
        view = self._request_public(dict(row))
        view["run_state"] = run["state"]
        view["decisions"] = list(PERMISSIONS)
        view["always_allowed"] = bool(json.loads(row["pattern"] or "[]"))
        return view

    @staticmethod
    def _request_public(request: dict) -> dict:
        try:
            pattern = json.loads(request["pattern"] or "[]")
        except ValueError:
            pattern = []
        try:
            metadata = json.loads(request["metadata"] or "{}")
        except ValueError:
            metadata = {}
        requested = metadata.get("requested_patterns") if isinstance(metadata, dict) else None
        if not isinstance(requested, list):
            requested = []
        requested = [p for p in requested if isinstance(p, str)][:32]
        return {"request_id": request["opencode_request"], "kind": request["kind"],
                "state": request["state"], "decision": request["decision"],
                "action": request["action"], "resource": request["resource"],
                "title": request["explanation"], "pattern": pattern,
                "requested_patterns": requested,
                "metadata": metadata, "redacted": bool(request["redacted"]),
                "created": request["created"], "resolved": request["resolved"],
                "always_allowed": bool(pattern),
                "note": ("'always' uses OpenCode's exact proposed pattern above and is never broadened.")}

    def _transcript(self, messages) -> tuple[list[dict], int]:
        entries, total = [], 0
        for message in messages:
            text = message.text or ""
            total += len(text)
            if total > MAX_TRANSCRIPT_CHARS:
                text = text[: max(0, MAX_TRANSCRIPT_CHARS - (total - len(text)))]
            entries.append({"id": message.id, "role": message.role, "text": text,
                            "tools": list(message.tools)[:24], "error": message.error,
                            "created": message.created, "completed": message.completed})
        return entries, total

    def runtime_status(self) -> dict:
        if self.runtime is None:
            return {"configured": False, "healthy": False, "locked": False, "version": None,
                    "discord_configured": bool(getattr(self.notifier, "enabled", False))}
        try:
            health = self.runtime.health()
        except BridgeError as exc:
            return {"configured": True, "healthy": False, "locked": False, "version": None,
                    "detail": str(exc), "discord_configured": bool(getattr(self.notifier, "enabled", False))}
        locked = bool(health.get("locked"))
        return {"configured": True, "healthy": bool(health.get("ok")) and not locked,
                "locked": locked, "version": health.get("version"),
                "adapter_version": health.get("adapter_version"),
                "server_configured": health.get("server_configured"),
                "discord_configured": bool(getattr(self.notifier, "enabled", False))}

    # --------------------------------------------------------------- admin views
    def _run_workspace(self, run_id: str) -> tuple[dict, dict]:
        with self.service.lock:
            row = self.service.db.execute(
                f"SELECT {RUN_COLUMNS} FROM agent_runs WHERE id=?", (run_id,)).fetchone()
            if not row:
                raise BridgeError("Run not found", "not_found")
            ws = self.service.workspace(row["workspace"], False)
        return dict(row), ws

    def admin_read_run(self, run_id: str, *, include_transcript: bool = False, limit: int = 40) -> dict:
        _, ws = self._run_workspace(run_id)
        return self.read_run(ws, run_id, include_transcript=include_transcript, limit=limit)

    def admin_stop(self, run_id: str) -> dict:
        _, ws = self._run_workspace(run_id)
        return self.cancel_run(ws, run_id)

    def admin_respond(self, run_id: str, request_id: str, decision: str) -> dict:
        _, ws = self._run_workspace(run_id)
        return self.respond_permission(ws, run_id, request_id, decision)

    # -------------------------------------------------------------- reconcile
    def reconcile_startup(self) -> None:
        with self.service.lock:
            rows = [dict(r) for r in self.service.db.execute(
                f"SELECT {RUN_COLUMNS} FROM agent_runs WHERE state IN "
                "('starting','running','waiting_permission','waiting_question')")]
        pending: set[str] = set()
        for run in rows:
            if not self._safe_reconcile(run):
                pending.add(run["id"])
        with self._reconcile_guard:
            self._reconcile_pending |= pending

    def retry_reconcile(self) -> list[str]:
        """One retry pass over runs whose reconciliation was inconclusive."""
        with self._reconcile_guard:
            ids = sorted(self._reconcile_pending)
        remaining: set[str] = set()
        for run_id in ids:
            with self.service.lock:
                row = self.service.db.execute(
                    f"SELECT {RUN_COLUMNS} FROM agent_runs WHERE id=?", (run_id,)).fetchone()
            if not row or row["state"] not in ACTIVE_RUN_STATES:
                continue
            if not self._safe_reconcile(dict(row)):
                remaining.add(run_id)
        with self._reconcile_guard:
            self._reconcile_pending = remaining
        return sorted(remaining)

    def _safe_reconcile(self, run: dict) -> bool:
        try:
            return self._reconcile_run(run)
        except BridgeError:
            return False

    def _reconcile_loop(self) -> None:
        while not self._stop.is_set():
            with self._reconcile_guard:
                if not self._reconcile_pending:
                    return
            self._stop.wait(RECONCILE_INTERVAL)
            if self._stop.is_set():
                return
            try:
                self.retry_reconcile()
            except BridgeError:
                self.service.event(None, "opencode_reconcile", "failed")

    def _reconcile_run(self, run: dict) -> bool:
        """Return True when resolved (no further retry needed), False to retry later.

        Only a positive runtime result can orphan a run. Transient connectivity
        (adapter still starting) leaves the run explicitly active for retry.
        """
        if self.runtime is None:
            return False
        ws = self._workspace(run)
        directory = self._directory(ws)
        try:
            session = self.runtime.get_session(directory, run["session"])
        except BridgeError:
            return False  # transient; retry when the runtime is reachable
        if session is None:
            self._orphan(run, "session_missing", "OpenCode session no longer exists")
            return True
        if not session.directory or not _same_directory(session.directory, directory):
            self._orphan(run, "session_mismatch", "OpenCode session is bound to another directory")
            return True
        pending = self._pending(run)
        if pending:
            # Keep waiting, but keep verifying the session so a later loss is explicit.
            desired = "waiting_question" if any(p["kind"] == "question" for p in pending) else "waiting_permission"
            if run["state"] != desired:
                self._set_state(run["id"], desired)
            return False
        try:
            messages = self.runtime.messages(directory, run["session"], limit=40)
        except BridgeError:
            return False
        scoped = self._messages_after(messages, self._floor_ms(run))
        final = self._final_answer(scoped)
        if final is not None:
            transcript, _ = self._transcript(scoped)
            self._complete(run, final, "restart_reconcile", message_count=len(scoped),
                           transcript=transcript)
            return True
        # Session is alive but no credible final response yet: stay explicitly active
        # (the live event pump will observe any later completion) and stop retrying.
        return True
