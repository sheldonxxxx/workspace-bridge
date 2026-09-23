"""Runtime-neutral run orchestration: handoff-bound sessions, resumable waits.

This module keeps long-running runtime/SDK lifecycle logic out of service.py.
It is the only place that turns a prepared handoff into a bounded agent
session, persists run/request state, mediates permission decisions, reconciles
state after restart, and emits completion/attention notifications. It programs
against the generic ``AgentRuntime`` contract (normalized ``RuntimeEvent`` /
``RuntimeInteraction`` objects plus explicit ``RuntimeCapabilities``); the
currently installed Pi backend is selected at runtime and identified per
run by its stable runtime id (``"pi"``).

Trust model: the bridge is the orchestration client, not a sandbox. Agent
claims are untrusted evidence. ``always`` approvals pass through the backend's
own proposed pattern unchanged; if that scope cannot be reviewed, always fails
closed while once/reject remain available.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import sqlite3
import threading
import time
from datetime import datetime, timezone
from typing import Any

from .notifications import NOTIFIED_STATES, Notifier, NullNotifier
from .oplog import emit, error_code
from .runtime import (MAX_TRANSCRIPT_CHARS, PI_RUNTIME_ID, AgentRuntime, ModelInfo,
                       RuntimeEvent, RuntimeInteraction, RuntimeUnavailable,
                       coerce_runtime_event, is_valid_runtime_id, sanitize_metadata)
from .security import BridgeError, HANDOFF, digest

_ops_log = logging.getLogger("workspace_bridge.ops")

RUN_STATES = ("starting", "running", "waiting_permission", "waiting_question",
              "completed", "blocked", "failed", "cancelled", "orphaned")
ACTIVE_RUN_STATES = frozenset({"starting", "running", "waiting_permission", "waiting_question"})
TERMINAL_RUN_STATES = frozenset({"completed", "blocked", "failed", "cancelled", "orphaned"})
WAITING_RUN_STATES = frozenset({"waiting_permission", "waiting_question"})
MAX_RESULT_CHARS = 20000
MAX_RUNS_PER_WORKSPACE = 500
PERMISSIONS = ("once", "always", "reject")
RECONCILE_INTERVAL = 15.0
# Authoritative background polling while runs are active. The event stream
# is an optional latency hint only (live validation showed only
# server.connected/heartbeat in this environment), so permission and
# completion state converge from these bounded sweeps, independent of the
# 25s event long poll. At most one listing/read per distinct session per
# sweep; no work when no relevant active run exists.
PERMISSION_RESYNC_INTERVAL = 4.0
PERMISSION_RESYNC_SESSION_LIMIT = 50
# Durable completed assistant messages are the completion evidence.
COMPLETION_RECONCILE_INTERVAL = 7.0
COMPLETION_RECONCILE_SESSION_LIMIT = 50
COMPLETION_MESSAGE_LIMIT = 40
# Official V2 session-scoped pending-question snapshot
# (GET /api/session/{sessionID}/question), same cadence/bounds as
# permissions. No TUI scraping, internal state reads, private endpoints, or
# text inference is ever used for questions.
QUESTION_RESYNC_INTERVAL = 4.0
QUESTION_RESYNC_SESSION_LIMIT = 50
# Dedicated reconciliation-loop scheduling: small tick so each cadence is
# met independently of the event long poll and of the others.
RECONCILE_POLL_TICK = 1.0
# Functional event-stream health: while runs are active, transport/control
# traffic without any functional event for this long exposes
# functional_status=degraded (diagnostic only, never blocks polling).
FUNCTIONAL_DEGRADED_AFTER = 45.0
# Startup grace: the bridge may start before the adapter. Runtime
# unavailability inside this window logs at DEBUG; persistent
# unavailability afterwards logs throttled WARNINGs (never spam).
STARTUP_UNAVAILABLE_GRACE = 30.0
UNAVAILABLE_WARN_EVERY = 60.0

RUN_COLUMNS = ("id,workspace,runtime,job,request_id,request_hash,parent_run,session,model,state,"
               "error_code,error_message,result,notification,created,started,updated,finished,"
               "message_floor_ms,session_reused,transcript,permission_revision,"
               "execution_floor,execution_cursor,execution_audit_status,execution_audit_error,"
               "enforcement_fingerprint,adapter_version,pi_version,"
               "extension_revision,extension_snapshot")
REQUEST_COLUMNS = ("id,run,workspace,session,runtime_request,"
                   "kind,action,resource,pattern,"
                   "metadata,explanation,redacted,state,decision,created,updated,resolved,"
                   "generation")


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


def _label_for_runtime(runtime_id: str | None) -> str:
    """Short human label for a persisted runtime identity."""
    if not runtime_id:
        return "Pi"
    return {"pi": "Pi"}.get(runtime_id, runtime_id)


def neutral_request_public(request: dict, label: str) -> dict:
    """Sanitized public shape for one persisted request row (any runtime).

    Same bounds and semantics as the orchestrator view, without a backend:
    native request identity, bounded patterns/metadata, no secrets beyond
    what persistence already sanitized.
    """
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
    generation = request.get("generation")
    if generation not in ("v1", "v2"):
        generation = "v1"
    native_id = request.get("runtime_request") or ""
    return {"request_id": native_id, "kind": request["kind"],
            "state": request["state"], "decision": request["decision"],
            "action": request["action"], "resource": request["resource"],
            "title": request["explanation"], "pattern": pattern,
            "requested_patterns": requested,
            "metadata": metadata, "redacted": bool(request["redacted"]),
            "created": request["created"], "resolved": request["resolved"],
            "generation": generation,
            "always_allowed": bool(pattern),
            "note": (f"'always' uses {label}'s exact proposed pattern above "
                     "and is never broadened.")}


def neutral_run_summary(run: dict) -> dict:
    """Runtime-neutral persisted-run summary for cross-runtime listings.

    Built from persisted ``agent_runs`` columns only: no backend calls, no
    transcript/result bodies, no permission scopes. Safe to use for rows of
    any runtime.
    """
    try:
        notification = json.loads(run.get("notification") or "{}")
    except ValueError:
        notification = {}
    reused = bool(run.get("session_reused"))
    return {"run_id": run["id"], "workspace_id": run["workspace"],
            "runtime": run.get("runtime") or PI_RUNTIME_ID, "job_id": run["job"],
            "parent_run_id": run["parent_run"], "request_id": run["request_id"],
            "continue_from_run_id": run["parent_run"] if reused else None,
            "session_reused": reused,
            "state": run["state"], "model": run["model"], "session_id": run["session"],
            "created": run["created"], "started": run["started"], "updated": run["updated"],
            "finished": run["finished"],
            "permission_revision": run.get("permission_revision") or "",
            "extension_revision": run.get("extension_revision") or "",
            "duration_seconds": _duration(run["started"], run["finished"]),
            "notification": notification,
            "active": run["state"] in ACTIVE_RUN_STATES}


class AgentOrchestrator:
    def __init__(self, service, runtime: AgentRuntime | None = None,
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
        # Transient, non-terminal permission-resync diagnostics keyed by run
        # id. Records the last resync outcome (ok with matched count, list
        # failure, or binding mismatch/missing) plus an event-stream degraded
        # mark captured at start. Bounded sanitized dicts only: status,
        # reason/code strings, matched count, checked_at timestamp. Never
        # event contents, permission scopes, or secrets. Cleared/replaced by
        # each later resync; lost on process restart and recomputed on read.
        self._sync_diagnostics: dict[str, dict] = {}
        self._sync_lock = threading.Lock()
        # Same shape for the official question snapshot path: last
        # question-resync outcome per run id (ok with matched count, list
        # failure, or binding failure). Question bodies/options/answers are
        # never stored.
        self._question_diagnostics: dict[str, dict] = {}
        self._question_lock = threading.Lock()
        # Dedicated authoritative reconciliation loop (separate from the
        # event pump and the startup-retry loop). Lifecycle-owned: start()
        # starts it, stop() joins it.
        self._poll: threading.Thread | None = None
        self._started_at: float | None = None
        # Functional event-stream health, Bridge-observed: monotonic time of
        # the last event that could affect run state or transcript, plus the
        # last exposed functional status (for transition-only logging).
        # Adapter counters (when reported) are the primary source;
        # this timestamp is the fallback/confirmation.
        self._last_functional_event_at: float | None = None
        self._functional_status: str = "unknown"
        self._functional_lock = threading.Lock()
        self._last_unavailable_log = 0.0
        # Per-(check, run) throttle state for background-sweep logging:
        # (last status, last WARNING monotonic). Bounds log volume while
        # sweeps run every few seconds; direct reads never consult it.
        self._resync_warn_at: dict[tuple[str, str], tuple[str | None, float]] = {}

    # ---------------------------------------------------------------- lifecycle
    @property
    def configured(self) -> bool:
        return self.runtime is not None

    def _cursor_setting_keys(self) -> tuple[str, str]:
        """(instance_key, cursor_key) for persisted event-stream state.

        Every runtime uses its exact id as the suffix, so two distinct
        configured ids can never collapse to the same keys. Configured ids
        are canonical by registry construction; an invalid id outside a
        registered path fails closed instead of normalizing to a colliding
        key.
        """
        try:
            configured = self.runtime.runtime_id if self.runtime is not None else PI_RUNTIME_ID
        except NotImplementedError:
            configured = PI_RUNTIME_ID
        if not is_valid_runtime_id(configured):
            raise BridgeError(f"Runtime {configured!r} has an invalid identity "
                              "for cursor state", "invalid_arguments")
        return (f"runtime_instance:{configured}", f"runtime_cursor:{configured}")

    def _event_polling_supported(self) -> bool:
        """Whether the configured backend supports event polling.

        Explicitly False capabilities (Pi) skip all event-pump work;
        unknown shapes default to True to preserve legacy behavior.
        """
        try:
            if self.runtime is None:
                return True
            return bool(self.runtime.capabilities.event_polling)
        except (AttributeError, NotImplementedError):
            return True

    def start(self) -> None:
        if not self.background or self.runtime is None:
            return
        if self._started_at is None:
            self._started_at = time.monotonic()
        if self._event_polling_supported():
            if self._pump is None:
                self._cursor = self._start_cursor()
                self._pump = threading.Thread(target=self._pump_loop, name=f"{self._list_runtime_id()}-events", daemon=True)
                self._pump.start()
        if self._poll is None:
            self._poll = threading.Thread(target=self._poll_loop,
                                          name=f"{self._list_runtime_id()}-reconcile-poll", daemon=True)
            self._poll.start()
        if self._reconcile_pending and self._reconcile_thread is None:
            self._reconcile_thread = threading.Thread(target=self._reconcile_loop,
                                                      name=f"{self._list_runtime_id()}-reconcile", daemon=True)
            self._reconcile_thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._pump is not None:
            self._pump.join(timeout=5)
            self._pump = None
        if self._poll is not None:
            self._poll.join(timeout=5)
            self._poll = None
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
        instance_key, cursor_key = self._cursor_setting_keys()
        try:
            head = self.runtime.health()
        except BridgeError:
            return 0
        instance = str(head.get("instance", ""))
        stored_instance = self.service.setting(instance_key) or ""
        if instance and instance == stored_instance:
            stored = self.service.setting(cursor_key)
            try:
                return int(stored) if stored else int(head.get("cursor") or 0)
            except (TypeError, ValueError):
                return int(head.get("cursor") or 0)
        if instance:
            self.service.set_setting(instance_key, instance)
        return int(head.get("cursor") or 0)

    # ------------------------------------------------------------------ helpers
    def _require_runtime(self) -> AgentRuntime:
        if self.runtime is None:
            raise BridgeError("Agent runtime is not configured for this bridge", "runtime_unavailable")
        return self.runtime

    def _runtime_id(self) -> str:
        """Stable identity of the configured backend (``"pi"`` today)."""
        runtime = self._require_runtime()
        try:
            return runtime.runtime_id
        except NotImplementedError:
            return PI_RUNTIME_ID

    def _runtime_label(self) -> str:
        """Short human label for the configured backend (``"Pi"`` today)."""
        try:
            configured = self.runtime.runtime_id if self.runtime is not None else PI_RUNTIME_ID
        except NotImplementedError:
            configured = PI_RUNTIME_ID
        return _label_for_runtime(configured)

    def _list_runtime_id(self) -> str:
        """Persisted-runtime filter for list paths.

        The configured backend id when available; the ``"pi"`` fallback
        for the unconfigured compatibility orchestrator. Never raises.
        """
        try:
            if self.runtime is None:
                return PI_RUNTIME_ID
            return self.runtime.runtime_id
        except NotImplementedError:
            return PI_RUNTIME_ID
        except BridgeError:
            return PI_RUNTIME_ID

    def _is_owned_run(self, run: dict) -> bool:
        """Whether a persisted run belongs to the configured runtime.

        Pure persisted-state comparison; never calls the backend. Used in
        loop/thread contexts that must silently skip foreign rows.
        """
        if self.runtime is None:
            return False
        try:
            configured = self.runtime.runtime_id
        except NotImplementedError:
            configured = PI_RUNTIME_ID
        return (run.get("runtime") or PI_RUNTIME_ID) == configured

    def _require_run_runtime(self, run: dict) -> str:
        """Fail closed unless a persisted run belongs to the configured runtime.

        Compares run.runtime (fallback ``"pi"`` for rows missing the column)
        to the configured AgentRuntime.runtime_id before any runtime API
        call.
        Returns the validated identity. Raises ``runtime_mismatch`` without
        touching the backend and without mutating or orphaning the run.
        """
        runtime = self._require_runtime()
        try:
            configured = runtime.runtime_id
        except NotImplementedError:
            configured = PI_RUNTIME_ID
        persisted = run.get("runtime") or PI_RUNTIME_ID
        if persisted != configured:
            raise BridgeError(
                f"Run belongs to runtime {persisted!r}; this bridge owns {configured!r}",
                "runtime_mismatch")
        return configured

    def _require_capability(self, name: str) -> None:
        """Fail closed when the installed backend does not support a path.

        The current Pi backend advertises every capability its
        adapter/orchestration path actually supports, so these gates never
        trigger there; a future backend without the flag gets an explicit
        ``runtime_unsupported`` instead of a silent fallback.
        """
        try:
            supported = bool(getattr(self._require_runtime().capabilities, name))
        except NotImplementedError:
            supported = name not in ("question_response", "session_branching")
        if not supported:
            raise BridgeError(f"Installed runtime does not support {name}", "runtime_unsupported")

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
        """Return the latest credible completed, non-error assistant response text.

        Only messages after the run's floor are considered, so a stale or
        duplicate idle with solely pre-continuation output returns None.
        Completion evidence means the latest relevant assistant message is
        completed/non-error: when the newest in-scope assistant message is
        still incomplete (or errored), an earlier completed turn must not
        complete a still-active later turn, so None is returned. Empty text
        from a genuinely completed assistant is allowed.
        """
        for message in reversed(list(messages)):
            stamp = None
            for value in (message.created, message.completed):
                if isinstance(value, int) and not isinstance(value, bool):
                    stamp = value if stamp is None else max(stamp, value)
            if floor_ms and (stamp is None or stamp <= floor_ms):
                continue
            if message.role != "assistant":
                continue
            # The first assistant message seen in reverse order is the
            # latest in-scope assistant turn; it alone decides.
            if message.completed and not message.error:
                return message.text or ""
            return None
        return None

    def _workspace(self, run: dict) -> dict:
        with self.service.lock:
            return self.service.workspace(run["workspace"], False)

    def _revalidate_binding(self, ws: dict, run: dict):
        """Positively confirm the recorded session still resolves to this workspace.

        The run's persisted runtime identity is validated first: a foreign
        run raises ``runtime_mismatch`` before any backend call, so this
        orchestrator never inspects another runtime's session. A missing
        session raises ``session_missing``; an observed directory that is
        absent or different raises ``session_mismatch``. Transient connectivity
        failures propagate as ``runtime_unavailable`` so callers can retry.
        """
        self._require_run_runtime(run)
        runtime = self._require_runtime()
        directory = self._directory(ws)
        session = runtime.get_session(directory, run["session"])
        label = self._runtime_label()
        if session is None:
            raise BridgeError(f"{label} session no longer exists", "session_missing")
        if not session.directory or not _same_directory(session.directory, directory):
            raise BridgeError(f"{label} session is bound to another directory", "session_mismatch")
        return session

    def _orphan(self, run: dict, code: str, message: str) -> None:
        with self.service.lock, self.service.db:
            self.service.db.execute(
                "UPDATE agent_requests SET state='orphaned',resolved=?,updated=? "
                "WHERE run=? AND state='pending'", (self._clock(), self._clock(), run["id"]))
        self._set_state(run["id"], "orphaned", error_code=code, error_message=message, finished=True)
        self.service.event(run["workspace"], "agent_orphaned", code)
        emit(_ops_log, "INFO", "bridge", "run_state", run_id=run["id"],
             session_id=run.get("session"), workspace_id=run.get("workspace"),
             state="orphaned", code=str(code)[:80])

    def _complete(self, run: dict, summary: str, reason: str, *, message_count: int = 0,
                  transcript: list | None = None) -> dict:
        result = json.dumps({"summary": summary[:MAX_RESULT_CHARS], "reason": reason,
                             "message_count": message_count, "has_final_response": True})
        snapshot = json.dumps(transcript if transcript is not None else [])
        with self.service.lock, self.service.db:
            self.service.db.execute("UPDATE agent_runs SET result=?,transcript=? WHERE id=?",
                                    (result, snapshot, run["id"]))
        self._set_state(run["id"], "completed", finished=True)
        self.service.event(run["workspace"], "agent_completed")
        # Never include final response text or transcript contents: counts
        # and reason only.
        emit(_ops_log, "INFO", "bridge", "run_state", run_id=run["id"],
             session_id=run.get("session"), workspace_id=run.get("workspace"),
             state="completed", reason=str(reason)[:80], count=message_count)
        self._notify(run, "completed")
        return self._summary(self._row(self._workspace(run), run["id"]))

    def _apply_permission_result(self, run: dict, permission_id: str, *,
                                 preferred_decision: str | None,
                                 resolved_state: str) -> tuple[dict | None, int]:
        """Resolve a pending request idempotently and return its final row + remaining count.

        Binds the exact native request id within this run only: the same
        native id in another run never resolves here.
        """
        with self.service.lock, self.service.db:
            row = self.service.db.execute(
                f"SELECT {REQUEST_COLUMNS} FROM agent_requests "
                "WHERE workspace=? AND run=? AND runtime_request=?",
                (run["workspace"], run["id"], permission_id)).fetchone()
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

    def _insert_pending_request(self, run: dict, native_id: str, kind: str,
                                action: str, resource: str, pattern_json: str,
                                metadata_json: str, explanation: str,
                                redacted: int, generation: str) -> None:
        """Insert one pending request row keyed by its native request id."""
        request_id = uid("req_")
        self.service.db.execute(
            "INSERT OR IGNORE INTO agent_requests (id,run,workspace,session,"
            "runtime_request,kind,"
            "action,resource,pattern,metadata,explanation,redacted,state,decision,created,updated,resolved,"
            "generation) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (request_id, run["id"], run["workspace"], run["session"],
             native_id, kind,
             action, resource, pattern_json, metadata_json,
             explanation, redacted, "pending", None,
             self._clock(), self._clock(), None, generation))

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
    # Model discovery is workspace-scoped for the installed Pi backend: the
    # mapped workspace directory resolves Pi configuration per workspace.
    # ``ws`` is accepted on list_models because the project-facing MCP
    # schema requires workspace_id on every tool; the result always states
    # scope="workspace".
    MODEL_POLICY_SETTING = "model_policy"
    MAX_POLICY_MODELS = 200

    def _policy_setting(self) -> str:
        """Storage key for this orchestrator's model policy.

        Every runtime uses a runtime-scoped key
        (``model_policy:<runtime_id>``) so policies never leak across
        backends.
        """
        try:
            configured = self.runtime.runtime_id if self.runtime is not None else PI_RUNTIME_ID
        except NotImplementedError:
            configured = PI_RUNTIME_ID
        if configured == PI_RUNTIME_ID:
            return self.MODEL_POLICY_SETTING
        return f"{self.MODEL_POLICY_SETTING}:{configured}"

    @staticmethod
    def _check_selector_shape(selector: Any) -> str:
        text = selector if isinstance(selector, str) else ""
        if (not text or len(text) > 260 or any(c.isspace() for c in text)
                or any(ord(c) < 32 or ord(c) == 127 for c in text)):
            raise BridgeError(f"Model selector {selector!r} is malformed; "
                              "use an exact canonical provider/model selector",
                              "invalid_arguments")
        return text

    def _global_models(self, directory: str | None = None) -> list[ModelInfo]:
        self._require_capability("model_discovery")
        runtime = self._require_runtime()
        try:
            return runtime.list_models(directory)
        except RuntimeUnavailable as exc:
            raise BridgeError(str(exc), exc.code) from None

    def _model_directory(self, ws: dict | None) -> str | None:
        """Discovery directory for the configured runtime.

        Pi discovery is workspace-scoped: resolve the mapped workspace
        root; without a workspace return None and fail closed downstream.
        """
        if ws is None:
            return None
        return self._directory(ws)

    def get_model_policy(self) -> dict | None:
        """Return the saved global policy or None when never configured.

        The legacy ``default_model`` setting is deliberately never consulted:
        it must not silently grant a model after upgrade. Orchestrators read
        their runtime-scoped key (see _policy_setting).
        """
        raw = self.service.setting(self._policy_setting())
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

    def set_model_policy(self, enabled: list, default: str, ws: dict | None = None) -> dict:
        """Atomically save the global enabled set + mandatory default.

        Local-admin only (exposed solely on the loopback manager route; MCP
        has no mutation path). Every selector must currently exist in the
        runtime model list; the default must be enabled.

        The policy stays GLOBAL per runtime (``model_policy:<runtime>``).
        Because Pi discovery is workspace-bound in the Bridge contract,
        saving a Pi policy requires an explicit enabled workspace as
        validation context. This does NOT create a per-workspace policy:
        at run time each Pi run revalidates against that run's own
        workspace directory.
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
        label = self._runtime_label()
        try:
            configured = self.runtime.runtime_id if self.runtime is not None else PI_RUNTIME_ID
        except NotImplementedError:
            configured = PI_RUNTIME_ID
        if configured == PI_RUNTIME_ID:
            # Pi discovery is workspace-scoped: saving the Pi policy
            # requires an enabled workspace as validation context.
            if ws is None or not isinstance(ws, dict) or not ws.get("id"):
                raise BridgeError(
                    f"Saving the {label} model policy requires an enabled workspace "
                    "as discovery context; pass an explicit workspace_id",
                    "invalid_arguments")
            with self.service.lock:
                resolved = self.service.workspace(ws["id"])
            available = {m.selector for m in self._global_models(self._directory(resolved))}
            missing_code = "model_unavailable"
            missing_message = (f"{label} model {{selector!r}} is not available "
                               "for this workspace")
        else:
            available = {m.selector for m in self._global_models()}
            missing_code: str = "model_unavailable"
            missing_message = (f"{label} model {{selector!r}} is not available globally")
        for selector in clean:
            if selector not in available:
                raise BridgeError(missing_message.format(selector=selector),
                                  missing_code)
        with self.service.lock, self.service.db:
            self.service.set_setting(self._policy_setting(),
                                     json.dumps({"enabled": clean, "default": default}))
        self.service.event(None, "set_model_policy")
        return self.model_policy_status()

    def _policy_public(self, models: list[ModelInfo]) -> list[dict]:
        policy = self.get_model_policy()
        enabled = set(policy["enabled"]) if policy else set()
        default = policy["default"] if policy else None
        return [{**m.public(), "enabled": m.selector in enabled,
                 "policy_default": m.selector == default} for m in models]

    # ------------------------------------------- Pi permission policy (3B1)
    def _is_pi_runtime(self) -> bool:
        """Whether this orchestrator owns the Pi backend."""
        try:
            configured = self.runtime.runtime_id if self.runtime is not None else PI_RUNTIME_ID
        except NotImplementedError:
            configured = PI_RUNTIME_ID
        try:
            from .runtime import PI_RUNTIME_ID as _PI
        except ImportError:  # pragma: no cover - defensive
            _PI = "pi"
        return configured == _PI

    def _require_pi_runtime(self) -> None:
        if not self._is_pi_runtime():
            raise BridgeError("Pi permission policy is only available on the Pi runtime",
                              "unknown_runtime")

    def get_pi_permission_policy(self) -> tuple[dict, str, bool]:
        """Current Pi permission policy snapshot (Bridge is source of truth)."""
        self._require_pi_runtime()
        from .pi_permissions import get_policy as _get_policy
        return _get_policy(self.service)

    def pi_permission_status(self) -> dict:
        """Bounded admin status summary for the Pi permission policy."""
        self._require_pi_runtime()
        from .pi_permissions import status_summary as _summary
        return _summary(self.service)

    def pi_permission_view(self) -> dict:
        """Full admin GET view for the Pi permission policy."""
        self._require_pi_runtime()
        from .pi_permissions import full_view as _view
        return _view(self.service)

    def set_pi_permission_policy(self, raw: Any) -> dict:
        """Validate strictly and persist; local-admin only (no MCP path)."""
        self._require_pi_runtime()
        from .pi_permissions import set_policy as _set_policy
        policy, revision = _set_policy(self.service, raw)
        from .pi_permissions import status_summary as _summary
        void = _summary(self.service)
        return {**void, "policy": policy}

    def _pi_session_options(self) -> dict | None:
        """Immutable policy snapshot options for a NEW Pi session.

        Returns None only when the Pi runtime is unavailable (fail closed
        upstream). The permission snapshot is validated again by the
        adapter; both revisions are persisted on the agent run for
        continuation binding. The extension snapshot is default-empty
        (no third-party extension) until the local admin enables packages.
        """
        from .pi_extensions import get_policy as _get_ext_policy
        from .pi_permissions import get_policy as _get_policy
        policy, revision, _ = _get_policy(self.service)
        ext_policy, ext_revision, _ = _get_ext_policy(self.service)
        return {"permission_policy": policy, "policy_revision": revision,
                "extension_policy": ext_policy, "extension_revision": ext_revision}

    # ---------------------------- Pi extension policy (3C2)
    def _require_pi_runtime_ext(self) -> None:
        if not self._is_pi_runtime():
            raise BridgeError("Pi extension policy is only available on the Pi runtime",
                              "unknown_runtime")

    def get_pi_extension_policy(self) -> tuple[dict, str, bool]:
        """Current Pi extension policy snapshot (Bridge is source of truth)."""
        self._require_pi_runtime_ext()
        from .pi_extensions import get_policy as _get_policy
        return _get_policy(self.service)

    def pi_extension_status(self) -> dict:
        """Bounded admin status summary: counts + revision prefix only.

        Never carries package names, IDs, versions, or host paths. The
        live inventory is consulted for installed_count/readiness; when
        the adapter inventory is unavailable the counts report
        installed_count=None with ready=False instead of failing.
        """
        self._require_pi_runtime_ext()
        from .pi_extensions import status_summary as _summary
        try:
            self._require_capability("extension_inventory")
            inventory = self._require_runtime().list_extensions()
            return _summary(self.service, inventory, inventory_available=True)
        except BridgeError as exc:
            if exc.code in ("runtime_unsupported", "runtime_unavailable"):
                return _summary(self.service, None, inventory_available=False)
            raise
        except Exception:  # noqa: BLE001 - status degrades, never fails the read
            return _summary(self.service, None, inventory_available=False)

    def pi_extension_view(self) -> dict:
        """Full admin GET view: effective policy plus bounded live inventory."""
        self._require_pi_runtime_ext()
        from .pi_extensions import full_view as _view
        self._require_capability("extension_inventory")
        try:
            inventory = self._require_runtime().list_extensions()
        except Exception as exc:  # noqa: BLE001 - inventory failure is explicit
            raise BridgeError(
                "Pi extension inventory is unavailable from the adapter",
                "inventory_unavailable") from exc
        return _view(self.service, inventory, inventory_available=True)

    def set_pi_extension_policy(self, raw: Any) -> dict:
        """Validate strictly against the live inventory and persist.

        Local-admin only (no MCP path). When the adapter inventory is
        unavailable the save fails rather than persisting an unvalidated
        broadened policy.
        """
        self._require_pi_runtime_ext()
        from .pi_extensions import set_policy as _set_policy
        from .pi_extensions import status_summary as _summary
        self._require_capability("extension_inventory")
        try:
            inventory = self._require_runtime().list_extensions()
        except Exception as exc:  # noqa: BLE001
            raise BridgeError(
                "Pi extension inventory is unavailable; refusing to save an "
                "unvalidated policy", "inventory_unavailable") from exc
        policy, revision = _set_policy(self.service, raw, inventory)
        void = _summary(self.service, inventory, inventory_available=True)
        return {**void, "policy": policy, "extension_revision": revision}

    # ---------------------------- Pi execution ledger (3C1)
    def _pi_execution_head(self, ws: dict, session_id: str) -> int:
        """Adapter execution head for one Pi session (floor capture).

        Poll failures raise; callers treat failure as incomplete, never as
        proof of no executions. Only records after the floor belong to the
        new Bridge run.
        """
        runtime = self._require_runtime()
        directory = self._directory(ws)
        payload = runtime.read_executions(directory, session_id, after=0, limit=1)
        head = payload.get("head") if isinstance(payload, dict) else 0
        try:
            return max(0, int(head or 0))
        except (TypeError, ValueError):
            return 0

    def _upsert_pi_executions(self, run: dict, updates: list[dict]) -> int:
        """Idempotently persist journal updates for one run.

        ONE comparable sequence space: execution_floor, record start_seq
        and record update_seq are ALL global update-cursor values from
        the adapter journal (see executions.mjs). Ownership uses the
        stable start cursor: only records with start_seq greater than
        the run's execution_floor belong to this run; older
        same-session records are never misattributed. The run cursor
        tracks update_seq so later completion/error/decision mutations
        of an already-consumed start remain observable. Persisted
        agent_executions.seq stores the stable start cursor (never the
        moving update cursor) for deterministic ordering. Legacy
        payloads without start_seq/update_seq fall back to seq; such
        payloads only ever meet floors captured from the same legacy
        adapter, so the comparison stays within one space. Returns the
        number of rows written.
        """
        from .pi_executions import bounded_json, sanitize_input_summary, sanitize_result_summary
        if not updates:
            return 0
        floor = 0
        try:
            floor = int(run.get("execution_floor") or 0)
        except (TypeError, ValueError):
            floor = 0
        written = 0
        max_update = 0
        created = self._clock()
        with self.service.lock, self.service.db:
            for item in updates:
                if not isinstance(item, dict):
                    continue
                try:
                    start_seq = int(item.get("start_seq", item.get("seq")) or 0)
                except (TypeError, ValueError):
                    continue
                try:
                    update_seq = int(item.get("update_seq", item.get("seq")) or 0)
                except (TypeError, ValueError):
                    update_seq = start_seq
                if start_seq <= floor:
                    continue
                if update_seq > max_update:
                    max_update = update_seq
                seq = start_seq
                tool_call_id = str(item.get("tool_call_id") or item.get("toolCallId") or "")[:200]
                tool = str(item.get("tool") or "")[:40]
                if not tool_call_id or not tool:
                    continue
                state = str(item.get("state") or "started")[:20]
                if state not in ("started", "completed", "interrupted"):
                    state = "started"
                input_summary = sanitize_input_summary(
                    tool, item.get("input_summary") if isinstance(item.get("input_summary"), dict) else {})
                result_summary = sanitize_result_summary(
                    tool, item.get("result_summary") if isinstance(item.get("result_summary"), dict) else {})
                is_error = 1 if bool(item.get("is_error") or result_summary.get("is_error")) else 0
                permission_effect = str(item.get("permission_effect") or "")[:20]
                permission_decision = str(item.get("permission_decision") or "")[:20]
                if permission_decision not in ("once", "always", "reject", ""):
                    permission_decision = ""
                truncated = 1 if bool(item.get("truncated")) else 0
                started = str(item.get("started_at") or item.get("started") or "")[:60]
                ended = str(item.get("ended_at") or item.get("ended") or "")[:60]
                duration = item.get("duration_ms")
                try:
                    duration_ms = int(duration) if duration is not None else None
                except (TypeError, ValueError):
                    duration_ms = None
                exec_id = f"ex_{run['id'][4:]}_{tool_call_id}"[:64] if run["id"].startswith("run_") else uid("ex_")
                # Deterministic id per (run, toolCallId) for idempotent upsert.
                import hashlib as _hashlib
                digest = _hashlib.sha256(f"{run['id']}:{tool_call_id}".encode()).hexdigest()[:24]
                exec_id = f"ex_{digest}"
                self.service.db.execute(
                    "INSERT INTO agent_executions (id,run,workspace,runtime,session,tool_call_id,"
                    "seq,tool,state,started,ended,duration_ms,input_summary,result_summary,"
                    "is_error,permission_effect,permission_decision,truncated,created,updated) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(run, tool_call_id) DO UPDATE SET "
                    "seq=excluded.seq, state=excluded.state, started=excluded.started, "
                    "ended=excluded.ended, duration_ms=excluded.duration_ms, "
                    "input_summary=excluded.input_summary, result_summary=excluded.result_summary, "
                    "is_error=excluded.is_error, permission_effect=excluded.permission_effect, "
                    "permission_decision=CASE WHEN excluded.permission_decision<>'' "
                    "THEN excluded.permission_decision ELSE agent_executions.permission_decision END, "
                    "truncated=excluded.truncated, updated=excluded.updated",
                    (exec_id, run["id"], run["workspace"], run.get("runtime") or "pi",
                     run.get("session") or "", tool_call_id, seq, tool, state,
                     started or None, ended or None, duration_ms,
                     bounded_json(input_summary), bounded_json(result_summary),
                     is_error, permission_effect, permission_decision, truncated,
                     created, created))
                written += 1
            # Advance the run cursor to the highest synced UPDATE sequence
            # (never back). The cursor tracks update_seq/head, not start
            # order, so later mutations of a consumed start stay visible.
            if max_update:
                self.service.db.execute(
                    "UPDATE agent_runs SET execution_cursor=max(execution_cursor, ?), updated=? WHERE id=?",
                    (max_update, self._clock(), run["id"]))
        return written

    def _sync_pi_executions(self, ws: dict, run: dict, *, final: bool = False) -> dict:
        """Reconcile journal updates for one active Pi run into the DB.

        Poll failures never imply no executions: they return ok=False with
        an explicit error and leave audit status unchanged (or incomplete
        on final drain). Returns {ok, written, head, cursor, gap, error}.
        """
        if not self._is_pi_runtime() or run.get("state") not in ACTIVE_RUN_STATES:
            return {"ok": True, "written": 0, "gap": False, "error": ""}
        try:
            self._require_capability("execution_history")
        except BridgeError:
            return {"ok": False, "written": 0, "gap": False, "error": "execution_unsupported"}
        try:
            cursor = int(run.get("execution_cursor") or 0)
        except (TypeError, ValueError):
            cursor = 0
        try:
            runtime = self._require_runtime()
            payload = runtime.read_executions(
                self._directory(ws), run["session"], after=cursor, limit=100)
        except Exception as exc:  # noqa: BLE001 - poll failure is metadata, never proof
            from .oplog import error_code as _error_code
            return {"ok": False, "written": 0, "gap": False,
                    "error": f"execution_poll_failed: {_error_code(exc)}"[:200]}
        updates = payload.get("updates") if isinstance(payload, dict) else []
        if not isinstance(updates, list):
            updates = []
        gap = bool(payload.get("audit_gap") or payload.get("cursor_too_old"))
        head = payload.get("head")
        try:
            head = int(head or 0)
        except (TypeError, ValueError):
            head = cursor
        written = 0
        try:
            fresh = self._row(ws, run["id"])
            written = self._upsert_pi_executions(fresh, updates)
        except Exception:  # noqa: BLE001 - persistence failure is explicit, never silent
            return {"ok": False, "written": 0, "gap": gap, "error": "execution_persist_failed"}
        if gap:
            with self.service.lock, self.service.db:
                self.service.db.execute(
                    "UPDATE agent_runs SET execution_audit_status='incomplete', "
                    "execution_audit_error='execution_gap: journal history evicted', updated=? "
                    "WHERE id=?", (self._clock(), run["id"]))
        return {"ok": True, "written": written, "head": head, "cursor": cursor,
                "gap": gap, "error": "execution_gap" if gap else ""}

    def _final_pi_drain(self, ws: dict, run: dict) -> None:
        """Final bounded drain to the adapter head for one Pi run.

        Called after terminal assistant + idle state, before marking
        audit-complete. Loops boundedly (max 5 pages of 100) until the
        persisted cursor reaches the adapter head, a gap is proven, or a
        poll fails. Even at head, audit becomes complete ONLY when no
        persisted execution owned by the run remains started/unsettled;
        otherwise incomplete with execution_unsettled. Sets
        execution_audit_status accordingly; the run itself may still
        complete.
        """
        for _ in range(5):
            try:
                fresh = self._row(ws, run["id"])
            except BridgeError:
                return
            if fresh.get("state") in TERMINAL_RUN_STATES:
                return
            result = self._sync_pi_executions(ws, fresh, final=True)
            if not result.get("ok"):
                with self.service.lock, self.service.db:
                    self.service.db.execute(
                        "UPDATE agent_runs SET execution_audit_status='incomplete', "
                        "execution_audit_error=?, updated=? WHERE id=?",
                        (str(result.get("error") or "execution_final_drain_failed")[:200],
                         self._clock(), run["id"]))
                return
            if result.get("gap"):
                return
            try:
                after = self._row(ws, run["id"])
                cursor = int(after.get("execution_cursor") or 0)
                head = int(result.get("head") or cursor)
            except (TypeError, ValueError):
                return
            if cursor >= head:
                try:
                    with self.service.lock:
                        unsettled = self.service.db.execute(
                            "SELECT count(*) FROM agent_executions WHERE run=? AND state='started'",
                            (run["id"],)).fetchone()[0]
                except Exception:  # noqa: BLE001 - persistence failure blocks complete
                    with self.service.lock, self.service.db:
                        self.service.db.execute(
                            "UPDATE agent_runs SET execution_audit_status='incomplete', "
                            "execution_audit_error='execution_final_check_failed', updated=? WHERE id=?",
                            (self._clock(), run["id"]))
                    return
                if unsettled:
                    with self.service.lock, self.service.db:
                        self.service.db.execute(
                            "UPDATE agent_runs SET execution_audit_status='incomplete', "
                            "execution_audit_error='execution_unsettled: tool result pending', "
                            "updated=? WHERE id=?",
                            (self._clock(), run["id"]))
                    return
                with self.service.lock, self.service.db:
                    self.service.db.execute(
                        "UPDATE agent_runs SET execution_audit_status='complete', "
                        "execution_audit_error='', updated=? WHERE id=?",
                        (self._clock(), run["id"]))
                return
        with self.service.lock, self.service.db:
            self.service.db.execute(
                "UPDATE agent_runs SET execution_audit_status='incomplete', "
                "execution_audit_error='execution_drain_bounded: head not reached', updated=? "
                "WHERE id=?", (self._clock(), run["id"]))

    def _pi_audit_summary(self, run: dict) -> dict:
        """Persisted-only audit summary for read_agent_run (no backend calls).

        Status is complete only when a final drain proved completeness;
        otherwise incomplete with an explicit reason. Historical runs
        report not_recorded. Includes counts, gap indicator and
        enforcement fingerprint; never embeds execution bodies.
        """
        from .pi_executions import audit_counts
        status = run.get("execution_audit_status") or "not_recorded"
        if status not in ("pending", "complete", "incomplete", "not_recorded"):
            status = "not_recorded"
        runtime_id = run.get("runtime") or ""
        if runtime_id != "pi":
            status = "not_recorded"
        rows: list[dict] = []
        try:
            with self.service.lock:
                rows = [dict(r) for r in self.service.db.execute(
                    "SELECT tool, is_error FROM agent_executions WHERE run=?", (run["id"],)).fetchall()]
        except Exception:  # noqa: BLE001 - summary degrades, never fails the read
            rows = []
        counts = audit_counts([{"tool": r.get("tool"), "is_error": bool(r.get("is_error"))} for r in rows])
        try:
            floor = int(run.get("execution_floor") or 0)
        except (TypeError, ValueError):
            floor = 0
        try:
            cursor = int(run.get("execution_cursor") or 0)
        except (TypeError, ValueError):
            cursor = 0
        # 3C2: active extension snapshot so ChatGPT can audit which code
        # capabilities were loaded (bounded id/name/version/fingerprint
        # rows, no host paths). Historical runs report an empty snapshot.
        extensions: list[dict] = []
        try:
            raw_snapshot = run.get("extension_snapshot") or "[]"
            parsed = json.loads(raw_snapshot) if isinstance(raw_snapshot, str) else []
            if isinstance(parsed, list):
                for row in parsed[:64]:
                    if not isinstance(row, dict) or not row.get("id"):
                        continue
                    extensions.append({
                        "id": str(row.get("id"))[:218],
                        "name": str(row.get("name") or "")[:214],
                        "version": str(row.get("version") or "")[:80],
                        "fingerprint": str(row.get("fingerprint") or "")[:64],
                    })
        except (TypeError, ValueError):
            extensions = []
        return {
            "status": status,
            "counts": counts,
            "incomplete_reason": str(run.get("execution_audit_error") or "")[:200] if status == "incomplete" else "",
            "has_gap": status == "incomplete" and "gap" in str(run.get("execution_audit_error") or ""),
            "execution_floor": floor,
            "execution_cursor": cursor,
            "enforcement_fingerprint": str(run.get("enforcement_fingerprint") or "")[:64],
            "adapter_version": str(run.get("adapter_version") or "")[:40],
            "pi_version": str(run.get("pi_version") or "")[:80],
            "permission_revision": str(run.get("permission_revision") or "")[:64],
            "extension_revision": str(run.get("extension_revision") or "")[:64],
            "extensions": extensions,
        }

    def list_models(self, ws: dict | None = None, query: str = "", limit: int = 25) -> dict:
        models = self._global_models(self._model_directory(ws))
        needle = (query or "").strip().casefold()
        if needle:
            models = [m for m in models if needle in m.selector.casefold()
                      or needle in m.provider.casefold() or needle in m.model.casefold()
                      or needle in (m.name or "").casefold()]
        models = models[:max(1, min(int(limit), 100))]
        policy = self.model_policy_status()
        try:
            configured = self.runtime.runtime_id if self.runtime is not None else PI_RUNTIME_ID
        except NotImplementedError:
            configured = PI_RUNTIME_ID
        discovery_scope = "workspace"
        result: dict = {"runtime": configured,
                        "discovery_scope": discovery_scope,
                        "policy_scope": "runtime_global",
                        "scope": "workspace",
                        "query": query, "count": len(models),
                        "models": self._policy_public(models),
                        "policy": policy,
                        "selection": ("New runs use the configured runtime-global default when no model is given; "
                                      "an explicit model is allowed only when its exact selector is in the "
                                      "admin-enabled list and currently available. MCP cannot change "
                                      "the policy.")}
        if ws is not None:
            result["workspace_id"] = ws["id"]
        return result

    def _resolve_model(self, selector: str, directory: str | None = None) -> ModelInfo:
        models = self._global_models(directory)
        for model in models:
            if selector == model.selector or selector == f"{model.provider}/{model.model}":
                return model
        candidates = [m.selector for m in models if selector.casefold() in m.selector.casefold()][:10]
        if not candidates:
            candidates = [m.selector for m in models][:10]
        hint = ", ".join(candidates) or "none"
        label = self._runtime_label()
        try:
            configured = self.runtime.runtime_id if self.runtime is not None else PI_RUNTIME_ID
        except NotImplementedError:
            configured = PI_RUNTIME_ID
        if configured == PI_RUNTIME_ID:
            raise BridgeError(f"{label} model {selector!r} is not available for this workspace; "
                              f"candidates: {hint}", "model_unavailable")
        raise BridgeError(f"{label} model {selector!r} is not available for this workspace; "
                          f"candidates: {hint}", "model_unavailable")

    def _require_policy_selector(self, model: str | None, directory: str | None = None) -> str:
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
        label = self._runtime_label()
        try:
            configured = self.runtime.runtime_id if self.runtime is not None else PI_RUNTIME_ID
        except NotImplementedError:
            configured = PI_RUNTIME_ID
        scope_word = "workspace"
        if model is None:
            selector = policy["default"]
        else:
            selector = self._check_selector_shape(model)
            if selector not in policy["enabled"]:
                if configured == PI_RUNTIME_ID:
                    raise BridgeError(f"{label} model {selector!r} is not enabled in the runtime-global policy; "
                                      "the local administrator must enable it or choose an enabled model",
                                      "model_not_enabled")
                raise BridgeError(f"{label} model {selector!r} is not enabled in the runtime-global policy; "
                                  "the local administrator must enable it or choose an enabled model",
                                  "model_not_enabled")
        resolved = self._resolve_model(selector, directory)
        if resolved.selector not in policy["enabled"]:
            # The policy changed (or discovery shrank) after the policy was
            # saved: the exact default/enabled selector is currently unavailable.
            if selector == policy["default"] or model is None:
                raise BridgeError("The configured default model is no longer enabled",
                                  "model_disabled")
            if configured == PI_RUNTIME_ID:
                raise BridgeError(f"{label} model {selector!r} is enabled but currently unavailable",
                                  "model_unavailable")
            raise BridgeError(f"{label} model {selector!r} is enabled but currently unavailable "
                              f"in this {scope_word}",
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
        # Fail closed before any agent session is created: the global
        # enabled allowlist + default decides the exact model. The unconfigured
        # check runs before the runtime is required so a missing policy is
        # reported even when the adapter is down. Workspace-scoped runtimes
        # (Pi) validate against their own directory-scoped discovery; an
        # unconfigured Pi policy fails here before any session exists.
        selector = self._require_policy_selector(model, self._model_directory(ws))
        runtime = self._require_runtime()
        request_hash = digest(json.dumps({"job": job_id, "model": selector,
                                          "parent": parent_run_id or ""},
                                         sort_keys=True).encode())
        existing = self.service.db.execute(
            f"SELECT {RUN_COLUMNS} FROM agent_runs WHERE workspace=? AND request_id=?",
            (ws["id"], request_id)).fetchone()
        if existing:
            # Runtime-safe idempotency: a request_id is workspace-unique, but
            # an idempotent replay may only return a run owned by the
            # selected runtime. The same request_id under a different
            # runtime fails closed even when job/model/hash match.
            if (existing["runtime"] or PI_RUNTIME_ID) != self._runtime_id():
                raise BridgeError(
                    f"request_id is already used by a run owned by runtime "
                    f"{(existing['runtime'] or PI_RUNTIME_ID)!r}",
                    "runtime_mismatch")
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
        # 3B1: Pi sessions carry the immutable permission policy snapshot.
        # Pi sessions carry the immutable policy snapshot as options.
        session_options = self._pi_session_options() if self._is_pi_runtime() else None
        session = runtime.create_session(directory, title=job["title"], options=session_options)
        if not _same_directory(session.directory, directory):
            try:
                runtime.abort_session(directory, session.id)
            except BridgeError:
                pass
            raise BridgeError(f"{self._runtime_label()} session is not bound to the mapped workspace",
                              "session_mismatch")

        run_id = uid("run_")
        created = self._clock()
        permission_revision = (session_options or {}).get("policy_revision", "") if session_options else ""
        # 3C2 extension snapshot: revision binds continuation; the active
        # snapshot persists bounded rows (id/name/version/fingerprint,
        # no host paths) so ChatGPT can audit which code capabilities
        # were loaded.
        extension_revision = (session_options or {}).get("extension_revision", "") if session_options else ""
        extension_snapshot = "[]"
        if self._is_pi_runtime():
            try:
                rows = getattr(session, "extensions", []) or []
                bounded = []
                for row in rows[:64]:
                    if not isinstance(row, dict) or not row.get("id"):
                        continue
                    bounded.append({
                        "id": str(row.get("id"))[:218],
                        "name": str(row.get("name") or "")[:214],
                        "version": str(row.get("version") or "")[:80],
                        "fingerprint": str(row.get("fingerprint") or "")[:64],
                    })
                extension_snapshot = json.dumps(bounded, separators=(",", ":"))
            except Exception:  # noqa: BLE001 - snapshot failure never fails start
                extension_snapshot = "[]"
        # 3C1 execution audit init: fresh sessions start at floor/cursor 0.
        # Managed Pi runs are pending (require final drain for complete);
        # legacy stay not_recorded. Fingerprint/versions persist
        # with the run (no full paths); a changed fingerprint is evidence,
        # not an automatic refusal.
        is_pi = self._is_pi_runtime()
        managed_pi = is_pi and isinstance(session_options, dict)
        _audit_status = "pending" if managed_pi else "not_recorded"
        _fingerprint = getattr(session, "enforcement_fingerprint", "") or ""
        _adapter_version = getattr(session, "adapter_version", "") or ""
        _pi_version = getattr(session, "pi_version", "") or ""
        with self.service.lock, self.service.db:
            self.service.db.execute(
                "INSERT INTO agent_runs (id,workspace,runtime,job,request_id,request_hash,parent_run,session,"
                "model,state,error_code,error_message,result,notification,created,started,updated,finished,"
                "message_floor_ms,session_reused,transcript,permission_revision,"
                "execution_floor,execution_cursor,execution_audit_status,execution_audit_error,"
                "enforcement_fingerprint,adapter_version,pi_version,"
                "extension_revision,extension_snapshot) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, ws["id"], self._runtime_id(), job_id, request_id, request_hash, parent_run_id,
                 session.id, selector, "starting", None, None, "{}", "{}", created, None, created,
                 None, 0, 0, "[]", permission_revision or "",
                 0, 0, _audit_status, "", _fingerprint[:64], _adapter_version[:40], _pi_version[:80],
                 extension_revision or "", extension_snapshot))
        # One bounded event-health probe at start: never blocks, waits, or
        # polls. When the adapter stream is not confirmed subscribed the new
        # run is marked degraded so live asks are known to be at risk.
        try:
            self._mark_event_stream_degraded(run_id)
        except Exception:  # noqa: BLE001 - a probe failure must never fail start
            pass
        self.service.event(ws["id"], "start_agent_run")
        emit(_ops_log, "INFO", "bridge", "run_created", run_id=run_id,
             session_id=session.id, job_id=job_id, workspace_id=ws["id"],
             model=selector, session_reused=False)
        prompt = self.handoff_prompt(ws, job)
        if self.background:
            thread = threading.Thread(target=self._submit, args=(ws["id"], run_id, prompt, model_ref),
                                      name=f"{self._list_runtime_id()}-submit-{run_id}", daemon=True)
            with self._submission_condition:
                self._submissions.add(thread)
            thread.start()
        else:
            self._submit(ws["id"], run_id, prompt, model_ref)
        return self._summary(self._row(ws, run_id), idempotent=False)

    def _start_continuation(self, ws: dict, job: dict, request_id: str, *,
                            model: str | None, parent_run_id: str | None,
                            continue_from_run_id: str) -> dict:
        """Create a new Bridge run that reuses a completed run's agent session.

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
            raise BridgeError(f"Continuation source run has no {self._runtime_label()} session",
                              "continuation_unavailable")
        # Runtime identity is part of the continuation binding: a session id
        # from another backend must never be reused here. Fail closed.
        self._require_capability("session_reuse")
        if (source.get("runtime") or PI_RUNTIME_ID) != self._runtime_id():
            raise BridgeError("Continuation source run belongs to another runtime; "
                              "continuation refused", "continuation_unavailable")
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
            raise BridgeError(f"{self._runtime_label()} model {effective!r} is not enabled in the global policy",
                              "model_not_enabled")
        resolved = self._resolve_model(effective, self._model_directory(ws))
        if resolved.selector not in policy["enabled"]:
            raise BridgeError(f"{self._runtime_label()} model {effective!r} is enabled but currently unavailable",
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
            # Runtime-safe idempotency: a request_id is workspace-unique, but
            # an idempotent replay may only return a run owned by the
            # selected runtime. The same request_id under a different
            # runtime fails closed even when job/model/hash match.
            if (existing["runtime"] or PI_RUNTIME_ID) != self._runtime_id():
                raise BridgeError(
                    f"request_id is already used by a run owned by runtime "
                    f"{(existing['runtime'] or PI_RUNTIME_ID)!r}",
                    "runtime_mismatch")
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
            raise BridgeError(f"{self._runtime_label()} session no longer exists", "session_missing")
        if not session.directory or not _same_directory(session.directory, directory):
            raise BridgeError(f"{self._runtime_label()} session is bound to another directory", "session_mismatch")
        with self.service.lock:
            active = self.service.db.execute(
                "SELECT id FROM agent_runs WHERE runtime=? AND session=? AND state IN "
                "('starting','running','waiting_permission','waiting_question')",
                (self._runtime_id(), source["session"])).fetchall()
        if active:
            raise BridgeError(f"{self._runtime_label()} session already has an active Bridge run; "
                              "wait for it or cancel it before continuing",
                              "continuation_unavailable")
        # Never send a follow-up into a busy session: an open v1.18.x issue
        # can persist the prompt without scheduling it, losing the iteration.
        self._require_capability("session_status")
        try:
            status = runtime.session_status(directory, source["session"])
        except BridgeError as exc:
            if exc.code in ("runtime_unsupported", "not_found"):
                raise BridgeError(f"{self._runtime_label()} session status is not available from the "
                                  "installed runtime; continuation refused",
                                  "continuation_unavailable") from None
            raise BridgeError(f"{self._runtime_label()} session status is unavailable; continuation refused",
                              "continuation_unavailable") from None
        if status in ("busy", "retry"):
            raise BridgeError(f"{self._runtime_label()} session is busy; continuation refused",
                              "session_busy")
        if status != "idle":
            raise BridgeError(f"{self._runtime_label()} session status is unknown; continuation refused",
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
        # 3B1: Pi continuation requires the source run's permission
        # revision to equal the current policy revision. A policy change
        # never silently broadens an already-running Pi child: mismatch
        # fails closed and requires a fresh session.
        source_revision = ""
        current_revision = ""
        if self._is_pi_runtime():
            from .pi_permissions import get_policy as _get_pi_policy
            _policy, current_revision, _ = _get_pi_policy(self.service)
            source_revision = (source.get("permission_revision") or "")
            if source_revision != current_revision:
                raise BridgeError(
                    "The Pi permission policy changed since the source run; "
                    "continuation refused because the session snapshot differs. "
                    "Start a fresh session to pick up the current policy.",
                    "permission_scope_changed")
        # 3C2: Pi continuation requires the source run's extension
        # revision to equal the current extension policy revision, just
        # like the permission revision. Mismatch fails closed with
        # extension_scope_changed and requires a fresh session: active
        # sessions never hot-reload extensions.
        source_ext_revision = ""
        current_ext_revision = ""
        if self._is_pi_runtime():
            from .pi_extensions import get_policy as _get_pi_ext_policy
            _ext_policy, current_ext_revision, _ = _get_pi_ext_policy(self.service)
            source_ext_revision = (source.get("extension_revision") or "")
            if source_ext_revision != current_ext_revision:
                raise BridgeError(
                    "The Pi extension policy changed since the source run; "
                    "continuation refused because the session extension snapshot "
                    "differs. Start a fresh session to pick up the current policy.",
                    "extension_scope_changed")
        # 3C1: capture the adapter execution head as this run's floor
        # BEFORE promptAsync. Only later records belong to the new run;
        # prior same-session calls cannot be misattributed. Poll failures
        # never imply no executions: on failure the floor stays 0 and the
        # audit is marked incomplete with an explicit reason.
        _exec_floor = 0
        _exec_error = ""
        _fingerprint = str(source.get("enforcement_fingerprint") or "")
        _adapter_version = str(source.get("adapter_version") or "")
        _pi_version = str(source.get("pi_version") or "")
        if self._is_pi_runtime():
            try:
                _head = self._pi_execution_head(ws, source["session"])
                _exec_floor = max(0, int(_head or 0))
            except Exception as exc:  # noqa: BLE001 - floor failure is audit metadata, never a start refusal
                _exec_floor = 0
                _exec_error = f"execution_floor_unavailable: {type(exc).__name__}"[:200]
        run_id = uid("run_")
        created = self._clock()
        _audit_status = "pending" if self._is_pi_runtime() else "not_recorded"
        if _exec_error:
            _audit_status = "incomplete"
        try:
            with self.service.lock, self.service.db:
                self.service.db.execute(
                    "INSERT INTO agent_runs (id,workspace,runtime,job,request_id,request_hash,parent_run,session,"
                    "model,state,error_code,error_message,result,notification,created,started,updated,finished,"
                    "message_floor_ms,session_reused,transcript,permission_revision,"
                    "execution_floor,execution_cursor,execution_audit_status,execution_audit_error,"
                    "enforcement_fingerprint,adapter_version,pi_version,"
                    "extension_revision,extension_snapshot) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (run_id, ws["id"], self._runtime_id(), job["id"], request_id, request_hash,
                      continue_from_run_id, source["session"], selector, "starting", None, None,
                      "{}", "{}", created, None, created, None, floor, 1, "[]",
                      current_revision if self._is_pi_runtime() else "",
                      _exec_floor, _exec_floor, _audit_status, _exec_error,
                      _fingerprint[:64], _adapter_version[:40], _pi_version[:80],
                      current_ext_revision if self._is_pi_runtime() else "",
                      str(source.get("extension_snapshot") or "[]")[:8192]))
        except sqlite3.IntegrityError:
            raise BridgeError(f"{self._runtime_label()} session already has an active Bridge run",
                              "continuation_unavailable") from None
        try:
            self._mark_event_stream_degraded(run_id)
        except Exception:  # noqa: BLE001 - a probe failure must never fail start
            pass
        self.service.event(ws["id"], "start_agent_run")
        emit(_ops_log, "INFO", "bridge", "run_created", run_id=run_id,
             session_id=source["session"], job_id=job["id"], workspace_id=ws["id"],
             model=selector, session_reused=True)
        prompt = self.handoff_prompt(ws, job, continuation=True)
        if self.background:
            thread = threading.Thread(target=self._submit, args=(ws["id"], run_id, prompt, model_ref),
                                      name=f"{self._list_runtime_id()}-submit-{run_id}", daemon=True)
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
            if not self._is_owned_run(run):
                # Never prompt into a foreign runtime's session from a stale
                # dispatch; leave the row untouched (no state change, no send).
                return
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
                    emit(_ops_log, "INFO", "bridge", "dispatch_failed", run_id=run_id,
                         code="continuation_unavailable")
                    emit(_ops_log, "INFO", "bridge", "run_state", run_id=run_id,
                         state="failed", code="continuation_unavailable")
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
                    emit(_ops_log, "INFO", "bridge", "dispatch_failed", run_id=run_id,
                         code="session_busy")
                    emit(_ops_log, "INFO", "bridge", "run_state", run_id=run_id,
                         state="failed", code="session_busy")
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
            emit(_ops_log, "INFO", "bridge", "dispatch_started", run_id=run_id,
                 session_id=run["session"], workspace_id=run["workspace"],
                 session_reused=bool(run.get("session_reused")))
        except BridgeError as exc:
            self._set_state(run_id, "failed", error_code=exc.code, error_message=str(exc), finished=True)
            emit(_ops_log, "INFO", "bridge", "dispatch_failed", run_id=run_id,
                 code=exc.code or "bridge_error")
            emit(_ops_log, "INFO", "bridge", "run_state", run_id=run_id,
                 state="failed", code=exc.code or "bridge_error")
            run = None
            with self.service.lock:
                row = self.service.db.execute("SELECT workspace FROM agent_runs WHERE id=?",
                                              (run_id,)).fetchone()
            if row:
                self._notify({"id": run_id, "workspace": row["workspace"], "job": ""}, "failed")
        except Exception as exc:  # noqa: BLE001
            self._set_state(run_id, "failed", error_code="runtime_error",
                            error_message="Prompt submission failed", finished=True)
            emit(_ops_log, "INFO", "bridge", "dispatch_failed", run_id=run_id,
                 code=error_code(exc))
            emit(_ops_log, "INFO", "bridge", "run_state", run_id=run_id,
                 state="failed", code="runtime_error")
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
        # Event long poll only: normalized events are latency hints that
        # feed handle_event. Authoritative reconciliation runs on the
        # dedicated _poll_loop, so a blocked/empty 25s poll can never delay
        # permission/completion/question convergence. An exception here must
        # never terminate the reconciliation loop (and vice versa).
        while not self._stop.is_set():
            try:
                try:
                    supported = bool(getattr(self.runtime.capabilities, "event_polling"))
                except (AttributeError, NotImplementedError):
                    supported = True
                if not supported:
                    self._stop.wait(5.0)
                    continue
                events, cursor = self.runtime.poll_events(self._cursor, timeout=25.0)
            except BridgeError as exc:
                emit(_ops_log, "WARNING", "bridge", "event_pump_error",
                     code=exc.code or "runtime_error", reason="poll_failed")
                self._stop.wait(5.0)
                continue
            except Exception as exc:  # noqa: BLE001
                emit(_ops_log, "WARNING", "bridge", "event_pump_error",
                     code=error_code(exc), reason="poll_failed")
                self._stop.wait(5.0)
                continue
            self._cursor = cursor
            try:
                _, cursor_key = self._cursor_setting_keys()
                self.service.set_setting(cursor_key, str(cursor))
            except BridgeError:
                pass
            for event in events:
                try:
                    self.handle_event(event)
                except BridgeError as exc:
                    self.service.event(None, "agent_event_rejected", "failed")
                    emit(_ops_log, "WARNING", "bridge", "event_rejected",
                         code=exc.code or "rejected")
                except Exception:  # noqa: BLE001 - one bad event never breaks the pump
                    emit(_ops_log, "WARNING", "bridge", "event_rejected",
                         code="event_error")

    def _poll_loop(self) -> None:
        # Dedicated authoritative reconciliation loop, independent of the
        # event pump: bounded permission/question/completion polling while
        # runs are active, each on its own cadence, sharing one run
        # enumeration per sweep but never merging semantic failure
        # handling. Logging/exception handling here never terminates the
        # event pump.
        last_permission = 0.0
        last_completion = 0.0
        last_question = 0.0
        while not self._stop.is_set():
            try:
                tick = time.monotonic()
                due_permission = tick - last_permission >= PERMISSION_RESYNC_INTERVAL
                due_completion = tick - last_completion >= COMPLETION_RECONCILE_INTERVAL
                due_question = tick - last_question >= QUESTION_RESYNC_INTERVAL
                if due_permission or due_completion or due_question:
                    self._poll_sweep(due_permission=due_permission,
                                     due_completion=due_completion,
                                     due_question=due_question)
                    tick = time.monotonic()
                    if due_permission:
                        last_permission = tick
                    if due_completion:
                        last_completion = tick
                    if due_question:
                        last_question = tick
                    self._update_functional_health()
            except BridgeError:
                self.service.event(None, "agent_reconcile", "failed")
            except Exception:  # noqa: BLE001 - background recovery never breaks the loop
                pass
            self._stop.wait(RECONCILE_POLL_TICK)

    def _poll_sweep(self, *, due_permission: bool = True,
                    due_completion: bool = True, due_question: bool = True) -> dict:
        """One bounded authoritative sweep over currently active runs.

        Enumerates only rows persisted for the configured runtime: one
        orchestrator instance owns exactly one backend and must never
        inspect foreign-runtime rows. Shares a single starting/running
        enumeration across the permission, completion and question checks
        due on this tick (at most ~50 distinct sessions), but each check
        keeps its own cadence gating, API calls and failure semantics. No
        active runs means no runtime calls at all. Terminal runs never
        appear (the query filters state), so they disappear from future
        sweeps immediately. Returns a bounded outcome summary (counts only).
        """
        if self.runtime is None:
            return {"checked": 0, "recovered": 0}
        try:
            configured = self.runtime.runtime_id
        except NotImplementedError:
            configured = PI_RUNTIME_ID
        try:
            sweep_limit = max(PERMISSION_RESYNC_SESSION_LIMIT,
                              COMPLETION_RECONCILE_SESSION_LIMIT,
                              QUESTION_RESYNC_SESSION_LIMIT)
            with self.service.lock:
                rows = self.service.db.execute(
                    f"SELECT {RUN_COLUMNS} FROM agent_runs WHERE runtime=? AND state IN "
                    "('starting','running')"
                    f" LIMIT {sweep_limit}", (configured,)).fetchall()
        except BridgeError:
            self._note_unavailable("sweep_enumeration")
            return {"checked": 0, "recovered": 0}
        runs = [dict(row) for row in rows if row["session"]]
        if not runs:
            return {"checked": 0, "recovered": 0}
        # One pass per distinct session: several Bridge runs must never
        # share a session while active (continuation guards this), but the
        # sweep stays bounded and idempotent regardless.
        seen: set[str] = set()
        scoped: list[dict] = []
        for run in runs:
            if run["session"] in seen:
                continue
            seen.add(run["session"])
            scoped.append(run)
        recovered = 0
        for run in scoped:
            if self._stop.is_set():
                break
            try:
                ws = self._workspace(run)
            except BridgeError:
                continue
            if due_permission:
                try:
                    if self._resync_permissions(ws, run, via="sweep") == "recovered":
                        recovered += 1
                except BridgeError:
                    pass
                except Exception:  # noqa: BLE001 - one session never breaks the sweep
                    pass
            if due_question:
                try:
                    if self._resync_questions(ws, run, via="sweep") == "recovered":
                        recovered += 1
                except BridgeError:
                    pass
                except Exception:  # noqa: BLE001 - one session never breaks the sweep
                    pass
            if due_completion:
                # 3C1: reconcile execution-journal updates idempotently
                # during active Pi runs. Poll failures never imply no
                # executions (sync returns ok=False and leaves audit alone).
                if self._is_pi_runtime() and run.get("started"):
                    try:
                        self._sync_pi_executions(ws, run)
                    except Exception:  # noqa: BLE001 - one session never breaks the sweep
                        pass
                if not run.get("started"):
                    continue
                try:
                    completed = self._probe_completion(run, reason="background_reconcile",
                                                       allow_orphan=False)
                except BridgeError:
                    continue
                except Exception:  # noqa: BLE001 - one session never breaks the sweep
                    continue
                if completed is not None and completed.get("state") == "completed":
                    recovered += 1
        return {"checked": len(scoped), "recovered": recovered}

    def _note_unavailable(self, reason: str) -> None:
        """Throttled runtime-unavailable diagnostic for background loops.

        DEBUG inside the startup grace window (the adapter may simply not
        be up yet); afterwards a WARNING at most every minute so persistent
        unavailability stays visible without spamming. Never raises.
        """
        try:
            now = time.monotonic()
            in_grace = (self._started_at is not None
                        and now - self._started_at < STARTUP_UNAVAILABLE_GRACE)
            if in_grace:
                emit(_ops_log, "DEBUG", "bridge", "event_pump_error",
                     code="runtime_unavailable", reason=str(reason)[:80])
                return
            if now - self._last_unavailable_log >= UNAVAILABLE_WARN_EVERY:
                self._last_unavailable_log = now
                emit(_ops_log, "WARNING", "bridge", "event_pump_error",
                     code="runtime_unavailable", reason=str(reason)[:80])
            else:
                emit(_ops_log, "DEBUG", "bridge", "event_pump_error",
                     code="runtime_unavailable", reason=str(reason)[:80])
        except Exception:  # noqa: BLE001 - logging never breaks the loop
            pass

    # Event kinds that can affect Bridge run state or transcript
    # (heartbeats/control frames never reach here: they are dropped as
    # unsupported before normalization). Used for Bridge-observed
    # functional event-stream health.
    FUNCTIONAL_EVENT_KINDS = frozenset({
        "permission.asked", "permission.updated", "permission.v2.asked",
        "permission.replied", "permission.v2.replied",
        "session.idle", "session.status", "session.error",
    })

    def _note_functional_event(self, kind: str | None, event: RuntimeEvent | dict | None = None) -> None:
        """Record a Bridge-observed functional event (timestamp only)."""
        text = str(kind or "")
        if text in self.FUNCTIONAL_EVENT_KINDS or "question" in text.casefold():
            try:
                with self._functional_lock:
                    self._last_functional_event_at = time.monotonic()
            except Exception:  # noqa: BLE001 - health tracking never breaks events
                pass

    def handle_event(self, event: RuntimeEvent | dict) -> None:
        """Consume one normalized runtime event.

        The runtime boundary supplies ``RuntimeEvent`` objects. Plain dicts
        are accepted only through the same fail-closed normalization (scripted
        tests, queued fixtures) and never trusted as raw backend payloads.
        """
        if isinstance(event, dict):
            normalized = coerce_runtime_event(event)
            if normalized is None:
                return
            event = normalized
        elif not isinstance(event, RuntimeEvent):
            return
        kind = event.type
        self._note_functional_event(kind, event)
        session_id = event.session_id
        if not session_id and event.permission is not None:
            session_id = event.permission.session_id
        if not session_id:
            return
        run = self._active_run_for_session(session_id)
        if run is None:
            return  # No sole active owner: historical, unowned or ambiguous.
        if run["state"] in TERMINAL_RUN_STATES:
            return
        if kind in ("permission.asked", "permission.updated", "permission.v2.asked"):
            if event.permission is not None:
                self._on_permission(run, event.permission, source="event")
        elif kind in ("permission.replied", "permission.v2.replied"):
            self._on_permission_replied(run, event.permission_id, event.response)
        elif kind and "question" in kind.casefold():
            self._on_question(run, event)
        elif kind == "session.idle":
            self._on_idle(run)
        elif kind == "session.status":
            self._on_status(run, event)
        elif kind == "session.error":
            self._on_error(run, event.error or {})

    def _active_run_for_session(self, session_id: str) -> dict | None:
        """Resolve the single Bridge run that currently owns a backend session.

        Only starting/running/waiting rows of the configured runtime are
        owners, so a future backend may reuse the same native session id
        without collision. Zero matches means the event is historical or
        unowned and must not mutate anything; more than one match is
        recorded and mutates nothing (fail closed).
        """
        try:
            runtime_id = self._runtime_id() if self.runtime is not None else None
        except BridgeError:
            runtime_id = None
        with self.service.lock:
            if runtime_id is None:
                rows = self.service.db.execute(
                    f"SELECT {RUN_COLUMNS} FROM agent_runs WHERE session=? AND state IN "
                    "('starting','running','waiting_permission','waiting_question')",
                    (session_id,)).fetchall()
            else:
                rows = self.service.db.execute(
                    f"SELECT {RUN_COLUMNS} FROM agent_runs WHERE runtime=? AND session=? AND state IN "
                    "('starting','running','waiting_permission','waiting_question')",
                    (runtime_id, session_id)).fetchall()
        if not rows:
            return None
        if len(rows) > 1:
            self.service.event(None, "agent_ambiguous_session", "failed")
            return None
        return dict(rows[0])

    def _record_sync(self, run_id: str, payload: dict) -> None:
        """Store a bounded sanitized diagnostic for one run (replaces prior)."""
        clean: dict[str, Any] = {"checked_at": self._clock()}
        status = payload.get("status")
        clean["status"] = status if status in ("ok", "degraded") else "degraded"
        for key in ("reason", "code"):
            value = payload.get(key)
            clean[key] = str(value)[:80] if isinstance(value, str) and value else None
        matched = payload.get("matched")
        clean["matched"] = int(matched) if isinstance(matched, int) and matched >= 0 else None
        source = payload.get("source")
        clean["source"] = source if source in ("v1", "v2") else None
        with self._sync_lock:
            self._sync_diagnostics[str(run_id)] = clean

    def _sync_view(self, run_id: str) -> dict | None:
        with self._sync_lock:
            current = self._sync_diagnostics.get(str(run_id))
            return dict(current) if current is not None else None

    def _active_run_count(self) -> int:
        """Count starting/running rows of the configured runtime only.

        Foreign-runtime rows must not degrade this backend's event-stream
        health classification.
        """
        try:
            if self.runtime is None:
                return 0
            try:
                configured = self.runtime.runtime_id
            except NotImplementedError:
                configured = PI_RUNTIME_ID
            with self.service.lock:
                row = self.service.db.execute(
                    "SELECT count(*) FROM agent_runs WHERE runtime=? AND state IN "
                    "('starting','running')", (configured,)).fetchone()
            return int(row[0]) if row else 0
        except Exception:  # noqa: BLE001 - health classification never breaks callers
            return 0

    def _classify_functional(self, stream: dict | None, active: int) -> tuple[str, str | None]:
        """Bridge-level functional event-stream health (diagnostic only).

        healthy: a functional event (one that could affect run state or
        transcript) was observed recently via adapter counters or
        Bridge-observed normalized events.
        degraded: runs are active and transport/control traffic is alive
        (subscribed), but no functional event was seen within
        FUNCTIONAL_DEGRADED_AFTER. Heartbeat-only streams land here.
        unknown: no active runs, transport never subscribed, or no
        counters/observations exist yet. Never mislabeled healthy.
        This never blocks runs: polling is authoritative regardless.
        """
        now = time.monotonic()
        with self._functional_lock:
            bridge_last = self._last_functional_event_at
            previous_count = getattr(self, "_last_adapter_functional_count", None)
        if not active:
            return "unknown", "no_active_runs"
        if not isinstance(stream, dict) or stream.get("status") != "subscribed":
            return "unknown", "transport_not_subscribed"
        last_seen = bridge_last
        adapter_count = stream.get("functional_event_count")
        if isinstance(adapter_count, int) and not isinstance(adapter_count, bool):
            with self._functional_lock:
                if previous_count is None:
                    self._last_adapter_functional_count = adapter_count
                elif adapter_count > previous_count:
                    self._last_adapter_functional_count = adapter_count
                    self._last_functional_event_at = now
                    last_seen = now
        baseline = self._started_at if self._started_at is not None else now
        reference = last_seen if last_seen is not None else baseline
        if now - reference >= FUNCTIONAL_DEGRADED_AFTER:
            return "degraded", "no_functional_events"
        return "healthy", None

    def _update_functional_health(self) -> None:
        """Refresh and transition-log functional health (poll loop only).

        INFO on recovery, WARNING on degradation; transitions to unknown
        (runs drained/transport down) stay silent. Diagnostic only.
        """
        try:
            active = self._active_run_count()
            stream: dict | None = None
            if active and self.runtime is not None:
                try:
                    health = self.runtime.health()
                    if isinstance(health, dict):
                        raw = health.get("event_stream")
                        stream = dict(raw) if isinstance(raw, dict) else None
                except BridgeError as exc:
                    if (exc.code or "") != "runtime_unavailable":
                        raise
                    self._note_unavailable("functional_health")
                except Exception:  # noqa: BLE001 - health never breaks the loop
                    pass
            status, reason = self._classify_functional(stream, active)
        except Exception:  # noqa: BLE001 - health never breaks the loop
            return
        with self._functional_lock:
            previous = self._functional_status
            self._functional_status = status
        if status == previous or status == "unknown":
            return
        # State transitions only: no counts, no contents.
        if status == "degraded":
            emit(_ops_log, "WARNING", "bridge", "event_stream_health",
                 status="degraded", reason=str(reason or "no_functional_events")[:80])
        elif status == "healthy" and previous == "degraded":
            emit(_ops_log, "INFO", "bridge", "event_stream_health",
                 status="healthy", reason="functional_events_resumed")

    def _sweep_emit(self, via: str, level: str, component: str, event: str,
                    run: dict, **fields: Any) -> None:
        """Log policy split between direct reads and background sweeps.

        Direct reads keep immediate INFO/WARNING semantics. Sweeps run
        every few seconds while work is active, so steady-state repeats
        go to DEBUG: `ok` outcomes, and `degraded` repeats (WARNING at
        most every minute per check/run, plus an INFO on degraded<->ok
        transitions). Inside the startup grace window even the first
        sweep WARNING stays DEBUG without updating throttle state, so
        the first post-grace failure still warns immediately and
        persistent unavailability is never hidden. Never raises.
        """
        base = {"run_id": run.get("id"), "session_id": run.get("session"),
                "workspace_id": run.get("workspace"), **fields}
        if via != "sweep":
            emit(_ops_log, level, component, event, **base)
            return
        try:
            status = fields.get("status")
            status_text = status if isinstance(status, str) else None
            key = (str(event), str(run.get("id")))
            now = time.monotonic()
            in_grace = (self._started_at is not None
                        and now - self._started_at < STARTUP_UNAVAILABLE_GRACE)
            last = self._resync_warn_at.get(key)
            transition = last is None or last[0] != status_text
            if level == "WARNING":
                if in_grace:
                    emit(_ops_log, "DEBUG", component, event, **base)
                elif transition or last is None or now - last[1] >= UNAVAILABLE_WARN_EVERY:
                    self._resync_warn_at[key] = (status_text, now)
                    emit(_ops_log, "WARNING", component, event, **base)
                else:
                    emit(_ops_log, "DEBUG", component, event, **base)
            elif transition and status_text == "ok":
                self._resync_warn_at[key] = (status_text, now)
                emit(_ops_log, "INFO", component, event, **base)
            else:
                if last is None:
                    self._resync_warn_at[key] = (status_text, now)
                emit(_ops_log, "DEBUG", component, event, **base)
        except Exception:  # noqa: BLE001 - logging never breaks the sweep
            pass

    def _mark_event_stream_degraded(self, run_id: str) -> None:
        """Mark a new run degraded when the event stream is not subscribed.

        One bounded health read at start; never waits, polls, or fails the
        start. A missing/unreadable health response leaves no mark (unknown,
        not degraded). Callers must never raise from here.
        """
        if self.runtime is None:
            return
        try:
            health = self.runtime.health()
        except BridgeError:
            return
        except Exception:  # noqa: BLE001 - start must never fail on a health probe
            return
        stream = health.get("event_stream") if isinstance(health, dict) else None
        status = stream.get("status") if isinstance(stream, dict) else None
        if status is None:
            return  # unknown: do not claim degraded or healthy
        if status == "subscribed":
            return
        self._record_sync(run_id, {"status": "degraded",
                                   "reason": "event_stream_not_subscribed",
                                   "code": str(status)[:80], "matched": None})

    def _resync_permissions(self, ws: dict, run: dict, *, via: str = "read") -> str:
        """Recover a missed permission.asked from the backend's pending list.

        `via="sweep"` marks background-loop calls so repeats log at DEBUG
        with throttled WARNINGs; direct reads keep immediate semantics.

        For an active, positively revalidated run/session, each pending
        permission for that exact session is fed into _on_permission so
        existing dedupe, persistence, waiting_permission state,
        notification, exact always scope and respond behavior stay
        authoritative. Idempotent: already-persisted or repeated listings
        create nothing new.

        Fail-closed: any binding/list failure leaves run/request state
        unchanged and stays retryable. Absence from the listing never
        resolves an already persisted request. Every non-skipped outcome is
        recorded as a bounded sanitized diagnostic (ok with matched count,
        list failure, or binding mismatch/missing) so read_agent_run can
        distinguish a successful empty list from a failed listing without
        overloading terminal completion/failure semantics. A later
        successful resync replaces the transient failure diagnostic.

        Remote recovery is best-effort while upstream GET /permission
        listing is broken (it can fail its entire response encoding when
        one pending request carries an undefined metadata object); the
        official permission.list surface remains the sole listing source.
        No TUI scraping, internal DB/state reads, private endpoints, or
        scope inference from shell text is used.
        """
        if self.runtime is None or run.get("state") not in ACTIVE_RUN_STATES:
            return "skipped"
        try:
            self._require_capability("pending_snapshot")
        except BridgeError as exc:
            self._record_sync(run["id"], {"status": "degraded", "reason": "capability_unsupported",
                                          "code": exc.code or "runtime_unsupported", "matched": None})
            return "unsupported"
        try:
            self._revalidate_binding(ws, run)
        except BridgeError as exc:
            self._record_sync(run["id"], {"status": "degraded", "reason": "session_binding",
                                          "code": exc.code or "binding_error", "matched": None})
            self._sweep_emit(via, "WARNING", "bridge", "permission_resync", run,
                             status="degraded", reason="session_binding",
                             code=str(exc.code or "binding_error")[:80])
            return "binding_failed"
        try:
            listed = self.runtime.list_pending_permissions(self._directory(ws), run["session"])
        except BridgeError as exc:
            self._record_sync(run["id"], {"status": "degraded", "reason": "permission_list_failed",
                                          "code": exc.code or "runtime_error", "matched": None,
                                          "source": None})
            self._sweep_emit(via, "WARNING", "bridge", "permission_resync", run,
                             status="degraded", reason="permission_list_failed",
                             code=str(exc.code or "runtime_error")[:80])
            return "list_failed"
        except Exception as exc:  # noqa: BLE001 - any listing failure is degraded, never empty
            self._record_sync(run["id"], {"status": "degraded", "reason": "permission_list_failed",
                                          "code": "runtime_error", "matched": None,
                                          "source": None})
            self._sweep_emit(via, "WARNING", "bridge", "permission_resync", run,
                             status="degraded", reason="permission_list_failed",
                             code=error_code(exc))
            return "list_failed"
        items = listed or []
        # Which wire generation produced this snapshot: the V2 primary or the
        # V1 compatibility fallback. Reported by the runtime when known;
        # inferred from item generations otherwise. Generation/source plus
        # matched count only; never scopes, resources, or metadata.
        source = getattr(self.runtime, "last_permission_source", None)
        if source not in ("v1", "v2"):
            source = "v2" if any(getattr(i, "generation", "v1") == "v2" for i in items) else None
            if items and source is None:
                source = "v1"
        self._record_sync(run["id"], {"status": "ok", "reason": None, "code": None,
                                      "matched": len(items), "source": source})
        self._sweep_emit(via, "INFO", "bridge", "permission_resync", run,
                         status="ok", matched=len(items), source=source)
        for item in items:
            # The runtime boundary already yields normalized interactions;
            # a legacy-shaped object is coerced through the same boundary.
            if isinstance(item, dict):
                coerced = self._interaction_from_snapshot(item, run["session"])
                if coerced is None:
                    continue
                item = coerced
            try:
                self._on_permission(dict(run), item, source="resync")
            except BridgeError:
                continue
        return "recovered" if items else "empty"

    @staticmethod
    def _interaction_from_snapshot(raw: Any, session_id: str) -> RuntimeInteraction | None:
        """Coerce a legacy snapshot mapping into a normalized interaction."""
        if not isinstance(raw, dict):
            return None
        from .runtime import PendingPermission as _Permission
        permission_id = str(raw.get("id") or "")
        owner = str(raw.get("session_id") or "")
        if not permission_id or owner != session_id:
            return None
        scope = _bounded_str_list(raw.get("pattern"))
        requested = _bounded_str_list(raw.get("requested_patterns") or raw.get("patterns")
                                      or raw.get("pattern"))
        metadata, _ = sanitize_metadata(raw.get("metadata") or {})
        if not isinstance(metadata, dict):
            metadata = {}
        generation = raw.get("generation") or "v1"
        if generation not in ("v1", "v2"):
            generation = "v1"
        return _Permission(id=permission_id, session_id=owner,
                           action=str(raw.get("action") or "")[:120],
                           title=str(raw.get("title") or "")[:300],
                           pattern=tuple(scope), requested_patterns=tuple(requested),
                           tool=raw.get("tool"), call_id=raw.get("call_id"),
                           metadata=metadata, redacted=bool(raw.get("redacted")),
                           created=str(raw.get("created") or "")[:60],
                           generation=generation)

    def _record_question_sync(self, run_id: str, payload: dict) -> None:
        """Store a bounded sanitized question-resync diagnostic for one run."""
        clean: dict[str, Any] = {"checked_at": self._clock()}
        status = payload.get("status")
        clean["status"] = status if status in ("ok", "degraded", "not_applicable") else "degraded"
        for key in ("reason", "code"):
            value = payload.get(key)
            clean[key] = str(value)[:80] if isinstance(value, str) and value else None
        matched = payload.get("matched")
        clean["matched"] = int(matched) if isinstance(matched, int) and matched >= 0 else None
        source = payload.get("source")
        clean["source"] = source if source in ("v1", "v2") else None
        with self._question_lock:
            self._question_diagnostics[str(run_id)] = clean

    def _question_view(self, run_id: str) -> dict | None:
        with self._question_lock:
            current = self._question_diagnostics.get(str(run_id))
            return dict(current) if current is not None else None

    def _resync_questions(self, ws: dict, run: dict, *, via: str = "read") -> str:
        """Recover a missed question.asked from the backend's snapshot.

        `via="sweep"` marks background-loop calls so repeats log at DEBUG
        with throttled WARNINGs; direct reads keep immediate semantics.

        Uses only the verified V2 session-scoped surface
        (GET /api/session/{sessionID}/question on installed
        the backend SDK): exact-session binding, dedupe by
        request id, persistence with waiting_question state and
        notification — mirroring the permission path. Idempotent:
        already-persisted or repeated listings create nothing new.

        Fail-closed: any binding/list failure leaves run/request state
        unchanged and stays retryable. Absence from the listing never
        resolves an already persisted request. Question bodies, headers,
        options and answers never cross this boundary: only request ids,
        the owning session, counts and call references are kept. No TUI
        scraping, internal state reads, private endpoints, or text
        inference is used.
        """
        if self.runtime is None or run.get("state") not in ACTIVE_RUN_STATES:
            return "skipped"
        try:
            self._require_capability("pending_snapshot")
            self._require_capability("question_detection")
        except BridgeError as exc:
            # 3B1 diagnostic semantics: an installed backend without
            # question support (Pi) reports not_applicable, not a scary
            # degraded; genuine list/binding errors below stay degraded.
            self._record_question_sync(run["id"], {"status": "not_applicable",
                                                   "reason": "capability_unsupported",
                                                   "code": exc.code or "runtime_unsupported",
                                                   "matched": None})
            return "unsupported"
        try:
            self._revalidate_binding(ws, run)
        except BridgeError as exc:
            self._record_question_sync(run["id"], {"status": "degraded",
                                                   "reason": "session_binding",
                                                   "code": exc.code or "binding_error",
                                                   "matched": None})
            self._sweep_emit(via, "WARNING", "bridge", "question_resync", run,
                             status="degraded", reason="session_binding",
                             code=str(exc.code or "binding_error")[:80])
            return "binding_failed"
        try:
            listed = self.runtime.list_pending_questions(self._directory(ws), run["session"])
        except BridgeError as exc:
            self._record_question_sync(run["id"], {"status": "degraded",
                                                   "reason": "question_list_failed",
                                                   "code": exc.code or "runtime_error",
                                                   "matched": None})
            self._sweep_emit(via, "WARNING", "bridge", "question_resync", run,
                             status="degraded", reason="question_list_failed",
                             code=str(exc.code or "runtime_error")[:80])
            return "list_failed"
        except Exception as exc:  # noqa: BLE001 - any listing failure is degraded, never empty
            self._record_question_sync(run["id"], {"status": "degraded",
                                                   "reason": "question_list_failed",
                                                   "code": "runtime_error", "matched": None})
            self._sweep_emit(via, "WARNING", "bridge", "question_resync", run,
                             status="degraded", reason="question_list_failed",
                             code=error_code(exc))
            return "list_failed"
        items = listed or []
        # Which snapshot produced this result: the V2 primary or the V1
        # compatibility fallback. Reported by the runtime when known.
        # Source plus matched count only; never question contents.
        snap_source = getattr(self.runtime, "last_question_source", None)
        if snap_source not in ("v1", "v2"):
            snap_source = None
        self._record_question_sync(run["id"], {"status": "ok", "reason": None, "code": None,
                                               "matched": len(items), "source": snap_source})
        self._sweep_emit(via, "INFO", "bridge", "question_resync", run,
                         status="ok", matched=len(items), source=snap_source)
        for item in items:
            try:
                self._on_question_snapshot(dict(run), item, source="resync",
                                           snapshot_source=snap_source)
            except BridgeError:
                continue
        return "recovered" if items else "empty"

    def _on_question_snapshot(self, run: dict, item: RuntimeInteraction | Any, *,
                                source: str = "resync",
                                snapshot_source: str | None = None) -> None:
        """Persist one official snapshot question idempotently.

        Strict exact-session binding (another session's request is never
        attached) and dedupe by backend request id. Only the request id,
        question count and tool call reference are persisted; question
        text/options/answers are never stored or logged.
        """
        if run["state"] in TERMINAL_RUN_STATES:
            return
        question_id = str(getattr(item, "id", "") or "")
        if not question_id:
            return
        owner = getattr(item, "session_id", None)
        if owner and owner != run["session"]:
            return
        origin = source if source in ("event", "resync") else "resync"
        with self.service.lock:
            existing = self.service.db.execute(
                "SELECT id,state FROM agent_requests WHERE workspace=? AND run=? AND runtime_request=?",
                (run["workspace"], run["id"], question_id)).fetchone()
        if existing:
            return
        try:
            count = int(getattr(item, "question_count", 0) or 0)
        except (TypeError, ValueError):
            count = 0
        call_id = getattr(item, "call_id", None)
        metadata = {"question_count": max(0, min(count, 100))}
        if isinstance(call_id, str) and call_id:
            metadata["call_id"] = call_id[:200]
        with self.service.lock, self.service.db:
            self._insert_pending_request(
                run, question_id, "question",
                "question", "", "[]", json.dumps(metadata)[:8000],
                "Question waits are visible but the installed runtime API exposes no question reply; "
                "inspect the session transcript locally.",
                0, "v2")
        self._set_state(run["id"], "waiting_question")
        self.service.event(run["workspace"], "agent_waiting_question")
        # Request id + count + source only: never question bodies/options.
        emit(_ops_log, "INFO", "bridge", "question_resync", run_id=run["id"],
             session_id=run.get("session"), workspace_id=run.get("workspace"),
             status="asked", reason=origin, matched=max(0, min(count, 100)),
             source=snapshot_source if snapshot_source in ("v1", "v2") else None)
        emit(_ops_log, "INFO", "bridge", "run_state", run_id=run["id"],
             session_id=run.get("session"), workspace_id=run.get("workspace"),
             state="waiting_question", reason=origin)
        self._notify(run, "waiting_question", request_kind="question",
                     request_action="question")

    def _background_permission_resync(self) -> None:
        """Permission-only sweep so a missed ask can notify without a status poll.

        Delegates to the shared bounded sweep (only starting/running
        runs; waiting runs already persist their ask). Kept for direct
        callers; the poll loop drives this on cadence.
        """
        self._poll_sweep(due_permission=True, due_completion=False, due_question=False)

    def _background_question_resync(self) -> None:
        """Question-only sweep over the official V2 session snapshot.

        Delegates to the shared bounded sweep. Kept for direct callers;
        the poll loop drives this on cadence.
        """
        self._poll_sweep(due_permission=False, due_completion=False, due_question=True)

    def _background_completion_reconcile(self) -> None:
        """Completion-only sweep so a missed idle completes without read/restart.

        Bounded: only starting/running runs with prompt acceptance proven,
        one durable probe per distinct session, capped per sweep. Waiting
        runs never complete here (the probe itself refuses them);
        terminal-state/idempotency guards keep exactly one completion
        notification. Restarted in-flight runs stay eligible: startup
        reconciliation stops retrying once the session is alive, and this
        sweep observes any later durable completion.
        """
        outcome = self._poll_sweep(due_permission=False, due_completion=True, due_question=False)
        recovered = int(outcome.get("recovered") or 0)
        checked = int(outcome.get("checked") or 0)
        if recovered:
            emit(_ops_log, "INFO", "bridge", "completion_reconcile",
                 status="recovered", count=recovered, checked=checked)
        else:
            emit(_ops_log, "DEBUG", "bridge", "completion_reconcile",
                 status="no_final", checked=checked)

    def _on_permission(self, run: dict, interaction: RuntimeInteraction, *,
                       source: str = "event") -> None:
        """Persist one normalized permission ask idempotently.

        Consumes the generic ``RuntimeInteraction`` (kind ``"permission"``),
        never a backend-shaped dict: scope/resource splitting, bounding and
        sanitization already happened at the runtime boundary. ``pattern``
        remains the backend's exact proposed always scope used for
        always_allowed; ``requested_patterns`` stays separately reviewable.
        """
        if run["state"] in TERMINAL_RUN_STATES:
            return
        if not isinstance(interaction, RuntimeInteraction) or interaction.kind != "permission":
            return
        permission_id = str(interaction.id or "")
        if not permission_id:
            return
        owner = interaction.session_id
        if owner and owner != run["session"]:
            # Never attach another session's permission to this run. Child /
            # subagent sessions carry their own child session ID; without a
            # verified public parent relationship binding them to this run,
            # they are discarded here rather than attached to the root run.
            return
        action = str(interaction.action or "")[:120]
        origin = source if source in ("event", "resync") else "event"
        # Wire generation owning the reply endpoint. Persisted verbatim so
        # respond_agent_permission routes without guessing from the
        # request id. Unknown values fail closed to "v1" only when absent
        # (legacy rows); an explicitly unknown generation is rejected below.
        generation = interaction.generation or "v1"
        if generation not in ("v1", "v2"):
            return
        with self.service.lock:
            existing = self.service.db.execute(
                "SELECT id,state FROM agent_requests WHERE workspace=? AND run=? AND runtime_request=?",
                (run["workspace"], run["id"], permission_id)).fetchone()
        if existing:
            if existing["state"] != "pending":
                return
            request_id = existing["id"]
        else:
            scope = [str(p)[:400] for p in (interaction.pattern or [])][:32]
            requested = [str(p)[:400] for p in (interaction.requested_patterns or [])][:32]
            raw_tool = interaction.tool
            if isinstance(raw_tool, str):
                tool: Any = raw_tool[:200]
            elif isinstance(raw_tool, (dict, list)):
                tool, _ = sanitize_metadata(raw_tool)
            else:
                tool = None
            base_metadata = interaction.metadata or {}
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
            # 3C1: link permission asks to executions by toolCallId when
            # positively known; never guess session-grant decisions.
            try:
                _call_id = getattr(interaction, "call_id", None)
            except Exception:  # noqa: BLE001
                _call_id = None
            if isinstance(_call_id, str) and _call_id:
                metadata["tool_call_id"] = _call_id[:200]
            metadata_json = json.dumps(metadata, default=str)[:8000]
            title = str(interaction.title or "")[:300]
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
            with self.service.lock, self.service.db:
                self._insert_pending_request(
                    run, permission_id, "permission",
                    str(interaction.action or "")[:120],
                    resource,
                    json.dumps(scope)[:4000], metadata_json,
                    explanation, 1 if interaction.redacted else 0,
                    generation)
            self._set_state(run["id"], "waiting_permission")
            self.service.event(run["workspace"], "agent_waiting_permission")
            # Generation + request id + action + source only: never resource,
            # patterns, paths, metadata or tool arguments.
            emit(_ops_log, "INFO", "bridge", "permission_asked", run_id=run["id"],
                 session_id=run.get("session"), workspace_id=run.get("workspace"),
                 request_id=permission_id[:200], action=action[:120], source=origin,
                 generation=generation)
            emit(_ops_log, "INFO", "bridge", "run_state", run_id=run["id"],
                 session_id=run.get("session"), workspace_id=run.get("workspace"),
                 state="waiting_permission", reason=origin)
            self._notify(run, "waiting_permission", request_kind="permission",
                         request_action=str(interaction.action or "")[:80])

    def _on_question(self, run: dict, event: RuntimeEvent) -> None:
        """Record a live question hint as a visible wait (no reply path).

        The installed backend exposes no question reply, so the wait is
        informational: only the request/session reference is persisted, never
        question bodies, options or answers.
        """
        if run["state"] in TERMINAL_RUN_STATES:
            return
        if not isinstance(event, RuntimeEvent):
            return
        data = event.data if isinstance(event.data, dict) else {}
        _hint_id = uid("req_")
        native_request = str(event.event_id or data.get("id") or _hint_id)[:200]
        with self.service.lock, self.service.db:
            self._insert_pending_request(
                run, native_request, "question",
                str(data.get("action") or event.type or "question")[:120], "", "[]", "{}",
                "Question waits are visible but the installed runtime API exposes no question reply; "
                "inspect the session transcript locally.", 0, "v1")
        self._set_state(run["id"], "waiting_question")
        self.service.event(run["workspace"], "agent_waiting_question")
        emit(_ops_log, "INFO", "bridge", "run_state", run_id=run["id"],
             session_id=run.get("session"), workspace_id=run.get("workspace"),
             state="waiting_question", reason="question")
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
        # Request id + decision only; no scope.
        emit(_ops_log, "INFO", "bridge", "permission_replied", run_id=run["id"],
             session_id=run.get("session"), workspace_id=run.get("workspace"),
             request_id=str(permission_id)[:200],
             decision=decision if decision in PERMISSIONS else "unknown")
        current = self._row(self._workspace(run), run["id"])
        if remaining == 0 and current["state"] in WAITING_RUN_STATES:
            self._set_state(run["id"], "running")
            emit(_ops_log, "INFO", "bridge", "run_state", run_id=run["id"],
                 session_id=run.get("session"), workspace_id=run.get("workspace"),
                 state="running", reason="permission_replied")

    def _on_idle(self, run: dict) -> None:
        # session.idle is only a hint to probe durable completion evidence.
        if run["state"] in TERMINAL_RUN_STATES:
            return
        if not run.get("started"):
            # Prompt acceptance is not yet proven; a pre-start idle must never complete.
            return
        if self._pending(run):
            return
        self._probe_completion(run, reason="idle")

    @staticmethod
    def _status_hint(event: RuntimeEvent) -> str:
        """Extract the native session status hint from a session.status event."""
        status = event.status
        if not status:
            data = event.data
            if isinstance(data, dict):
                raw = data.get("status")
                status = raw.get("type") if isinstance(raw, dict) else raw
        return status if isinstance(status, str) else ""

    def _on_status(self, run: dict, event: RuntimeEvent) -> None:
        """Treat session.status idle as a hint to probe durable completion.

        Status alone never completes: only idle triggers the same durable
        probe as session.idle, and busy/retry/unknown statuses change
        nothing. A lagging busy status therefore cannot block completion
        when durable final evidence already exists (probes never consult
        status).
        """
        if run["state"] in TERMINAL_RUN_STATES:
            return
        if self._status_hint(event) != "idle":
            return
        if not run.get("started"):
            return
        if self._pending(run):
            return
        self._probe_completion(run, reason="status_idle")

    def _on_error(self, run: dict, error: dict) -> None:
        if run["state"] in TERMINAL_RUN_STATES:
            return
        name = str(error.get("name") or "error")[:80]
        message = str(error.get("message") or f"{self._runtime_label()} session error")[:300]
        self._set_state(run["id"], "failed", error_code=name or "session_error",
                        error_message=message, finished=True)
        self.service.event(run["workspace"], "agent_failed")
        emit(_ops_log, "INFO", "bridge", "run_state", run_id=run["id"],
             session_id=run.get("session"), workspace_id=run.get("workspace"),
             state="failed", code=name or "session_error")
        self._notify(run, "failed")

    def _eventless_idle_confirmed(self, ws: dict, run: dict) -> bool:
        """Whether a runtime without event polling is provably idle.

        Event-driven backends complete from durable message evidence alone:
        the event stream reports Quiescence. A backend without event polling
        (Pi) may still do steering/follow-up work after an assistant
        response, so a terminal-looking message must not complete the run
        while the session is busy. Completion then additionally requires
        session_status support and status == "idle". Busy/retry/unknown or
        unavailable status means "not complete yet": the run stays active
        and retryable, never forced. Event-capable runtimes always pass.
        """
        try:
            polling = self.runtime.capabilities.event_polling if self.runtime is not None else True
        except (AttributeError, NotImplementedError):
            polling = True
        if polling:
            return True
        try:
            self._require_capability("session_status")
        except BridgeError:
            return False
        try:
            status = self._require_runtime().session_status(
                self._directory(ws), run["session"])
        except BridgeError:
            return False
        return status == "idle"

    def _probe_completion(self, run: dict, *, reason: str,
                            allow_orphan: bool = True) -> dict | None:
        """One bounded durable-completion probe for an active run.

        Preconditions: the run is starting/running (waiting runs never
        complete), started is set, no pending requests, and the recorded
        session binding revalidates. Completion evidence is a completed,
        non-error assistant message after the run floor (see
        _final_answer); for runtimes without event polling, durable
        evidence additionally requires a provably idle session (see
        _eventless_idle_confirmed), so steering/follow-up work after an
        assistant response cannot be mistaken for completion. Event-driven
        session status is never consulted, so a lagging busy status cannot
        block durable completion there and an idle status alone never
        completes.

        Returns the completed summary when the probe recovers completion
        (or the current summary when already terminal), else None with the
        run left explicitly active. Transient runtime errors leave state
        active and retryable with a DEBUG diagnostic. A positively
        missing/mismatched session orphans exactly like the existing
        finalize/reconcile paths, except when allow_orphan is False: read
        and background sweeps are best-effort recovery and must preserve
        the long-standing fail-closed read semantics (degraded diagnostic,
        no state change), leaving orphaning to explicit paths and startup
        reconciliation. Terminal-state/idempotency guards make repeated
        probes notification-safe. Never logs response text.
        """
        current = self._row(self._workspace(run), run["id"])
        if current["state"] in TERMINAL_RUN_STATES:
            return self._summary(current)
        if current["state"] not in ("starting", "running"):
            return None
        if not current.get("started") or self._pending(current):
            return None
        try:
            ws = self._workspace(current)
            self._revalidate_binding(ws, current)
            if not self._eventless_idle_confirmed(ws, current):
                # A terminal-looking message alone must not complete a
                # no-event session that is still busy (or whose status is
                # unknown/unavailable): stay active for retry.
                emit(_ops_log, "DEBUG", "bridge", "completion_probe", run_id=current["id"],
                     session_id=current.get("session"), workspace_id=current.get("workspace"),
                     status="not_idle", reason=str(reason)[:80])
                return None
            messages = self._require_runtime().messages(
                self._directory(ws), current["session"], limit=COMPLETION_MESSAGE_LIMIT)
        except BridgeError as exc:
            if exc.code in ("session_missing", "session_mismatch") and allow_orphan:
                self._orphan(current, exc.code, str(exc))
            else:
                # Transient runtime/connectivity failure, or a binding
                # failure on a non-orphaning (read/background) probe: stay
                # active for retry; the resync diagnostic already records it.
                emit(_ops_log, "DEBUG", "bridge", "completion_probe", run_id=current["id"],
                     session_id=current.get("session"), workspace_id=current.get("workspace"),
                     status="retryable", reason=str(reason)[:80],
                     code=str(exc.code or "runtime_error")[:80])
            return None
        floor = self._floor_ms(current)
        scoped = self._messages_after(messages, floor)
        final = self._final_answer(scoped)
        if final is None:
            # No completed, non-error assistant response after this run's
            # boundary: do not invent completion from stale history or
            # status alone.
            emit(_ops_log, "DEBUG", "bridge", "completion_probe", run_id=current["id"],
                 session_id=current.get("session"), workspace_id=current.get("workspace"),
                 status="no_final", reason=str(reason)[:80], count=len(scoped))
            return None
        # 3C1: for Pi runs, final bounded drain to the adapter head after
        # terminal assistant + idle state. Audit-complete requires proven
        # completeness; otherwise the run still completes but audit is
        # incomplete with an explicit reason. Poll failures never imply no
        # executions.
        if self._is_pi_runtime():
            try:
                self._final_pi_drain(ws, current)
            except Exception:  # noqa: BLE001 - drain failure is audit metadata, never blocks completion
                try:
                    with self.service.lock, self.service.db:
                        self.service.db.execute(
                            "UPDATE agent_runs SET execution_audit_status='incomplete', "
                            "execution_audit_error='execution_final_drain_failed', updated=? WHERE id=?",
                            (self._clock(), current["id"]))
                except Exception:  # noqa: BLE001
                    pass
        transcript, _ = self._transcript(scoped)
        return self._complete(current, final, reason, message_count=len(scoped),
                              transcript=transcript)

    def finalize_run(self, run: dict, *, reason: str = "result") -> dict:
        """Complete only on credible final evidence; otherwise preserve the active state."""
        probed = self._probe_completion(run, reason=reason)
        if probed is not None:
            return probed
        # No transition: report current state without inventing completion.
        try:
            return self._summary(self._row(self._workspace(run), run["id"]))
        except BridgeError:
            return self._summary(run)

    # ------------------------------------------------------------- permission
    def respond_permission(self, ws: dict, run_id: str, request_id: str, decision: str) -> dict:
        if decision not in PERMISSIONS:
            raise BridgeError("decision must be once, always or reject", "invalid_arguments")
        run = self._row(ws, run_id)
        # Fail closed on foreign-runtime rows before any capability or
        # backend work.
        self._require_run_runtime(run)
        # The installed backend answers permission waits; a backend without
        # permission-response support fails closed here (and question waits
        # stay unanswerable: kind != permission raises runtime_unsupported).
        self._require_capability("permission_response")
        # The caller-supplied id is the exact native backend request id; it
        # resolves only within this run.
        with self.service.lock:
            row = self.service.db.execute(
                f"SELECT {REQUEST_COLUMNS} FROM agent_requests "
                "WHERE workspace=? AND run=? AND runtime_request=?",
                (ws["id"], run_id, request_id)).fetchone()
        if not row:
            raise BridgeError(f"Pending {self._runtime_label()} request not found for this run", "not_found")
        request = dict(row)
        if request["kind"] != "permission":
            raise BridgeError("Only permission requests can be answered remotely", "runtime_unsupported")
        if request["state"] != "pending":
            raise BridgeError("Request is no longer pending", "conflict")
        if request["session"] != run["session"] or run["state"] not in WAITING_RUN_STATES:
            raise BridgeError("Request is not bound to an active wait for this run", "conflict")
        pattern = json.loads(request["pattern"] or "[]")
        if decision == "always" and not pattern:
            raise BridgeError(f"{self._runtime_label()} did not expose an approval scope for 'always'; "
                              "use once or reject", "always_scope_unknown")
        # Wire endpoint selection comes from the persisted generation, never
        # from request-id formatting. Legacy rows without the marker keep
        # working as V1; an explicitly unknown generation fails closed.
        generation = request.get("generation")
        if generation is None:
            generation = "v1"
        if generation not in ("v1", "v2"):
            raise BridgeError("Stored permission generation is unknown; reply refused",
                              "incompatible_request")
        # Reconfirm the recorded session still belongs to this mapped workspace.
        self._revalidate_binding(ws, run)
        runtime = self._require_runtime()
        ok = runtime.respond_permission(self._directory(ws), run["session"], request_id, decision,
                                        generation)
        if not ok:
            raise BridgeError(f"{self._runtime_label()} did not confirm the permission response", "runtime_rejected")
        resolved_state = "approved" if decision in ("once", "always") else "rejected"
        final, remaining = self._apply_permission_result(
            run, request_id, preferred_decision=decision, resolved_state=resolved_state)
        if final is None:
            raise BridgeError(f"Pending {self._runtime_label()} request not found for this run", "not_found")
        current = self._row(ws, run_id)
        if remaining == 0 and current["state"] in WAITING_RUN_STATES:
            self._set_state(run_id, "running")
            current = self._row(ws, run_id)
            emit(_ops_log, "INFO", "bridge", "run_state", run_id=run_id,
                 session_id=run.get("session"), workspace_id=ws["id"],
                 state="running", reason="permission_reply")
        self.service.event(ws["id"], "respond_agent_permission", decision)
        emit(_ops_log, "INFO", "bridge", "permission_replied", run_id=run_id,
             session_id=run.get("session"), workspace_id=ws["id"],
             request_id=str(request_id)[:200], decision=decision,
             generation=generation)
        # 3C1: link the decision to the execution journal by toolCallId
        # when positively known; never guess session-grant decisions.
        try:
            _meta = json.loads(final.get("metadata") or request.get("metadata") or "{}")
        except ValueError:
            _meta = {}
        _tool_call_id = _meta.get("tool_call_id") if isinstance(_meta, dict) else None
        if isinstance(_tool_call_id, str) and _tool_call_id:
            try:
                with self.service.lock, self.service.db:
                    self.service.db.execute(
                        "UPDATE agent_executions SET permission_decision=?, updated=? "
                        "WHERE run=? AND tool_call_id=?",
                        (decision, self._clock(), run_id, _tool_call_id[:200]))
            except Exception:  # noqa: BLE001 - linkage is best-effort, never blocks the reply
                pass
        return {"run_id": run_id, "request_id": request_id, "decision": final["decision"] or decision,
                "request_state": final["state"], "run_state": current["state"],
                "resumed_same_session": True, "scope": pattern,
                "generation": generation,
                "note": f"The same {self._runtime_label()} session remains the execution owner."}

    # ------------------------------------------------------------------ cancel
    def cancel_run(self, ws: dict, run_id: str) -> dict:
        run = self._row(ws, run_id)
        self._require_run_runtime(run)
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
            self.service.event(ws["id"], "cancel_agent_run", "abort_uncertain")
            raise BridgeError("Abort outcome was not confirmed; run state is unchanged", "abort_uncertain")
        self._set_state(run_id, "cancelled", finished=True)
        self.service.event(ws["id"], "cancel_agent_run")
        emit(_ops_log, "INFO", "bridge", "run_state", run_id=run_id,
             session_id=run.get("session"), workspace_id=ws["id"],
             state="cancelled", reason="cancel")
        self._notify(run, "cancelled")
        return {"run_id": run_id, "state": "cancelled", "cancelled": True}

    # ------------------------------------------------------------------- read
    def _summary(self, run: dict, *, idempotent: bool = False) -> dict:
        pending = [r for r in self._requests(run) if r["state"] == "pending"]
        view = neutral_run_summary(run)
        try:
            audit = self._pi_audit_summary(run)
        except Exception:  # noqa: BLE001
            audit = {"status": "not_recorded",
                     "counts": {"total": 0, "failed": 0, "shell": 0, "mutating": 0}}
        view.update({
                "pending_request_count": len(pending), "idempotent_replay": idempotent,
                "permission_sync": self._sync_view(run["id"]),
                "question_sync": self._question_view(run["id"]),
                "execution_audit": audit})
        return view

    def list_runs(self, ws: dict, offset: int = 0, limit: int = 20) -> dict:
        """List runs owned by the configured runtime only (legacy Pi view).

        An orchestrator enumerates only rows persisted with its own runtime
        identity, so this compatibility path never surfaces another
        runtime's rows. The neutral cross-runtime view lives in
        ``Service.list_agent_runs``.
        """
        with self.service.lock:
            rows = self.service.db.execute(
                f"SELECT {RUN_COLUMNS} FROM agent_runs WHERE workspace=? AND runtime=? "
                "ORDER BY created DESC LIMIT ? OFFSET ?",
                (ws["id"], self._list_runtime_id(), limit + 1, offset)).fetchall()
        return {"workspace_id": ws["id"],
                "runs": [self._summary(dict(r)) for r in rows[:limit]],
                "next_offset": offset + limit if len(rows) > limit else None}

    MAX_GLOBAL_SESSIONS_LIMIT = 50

    def list_all_runs(self, offset: int = 0, limit: int = 25) -> dict:
        """Global operational overview: Bridge-owned runs across all workspaces.

        Newest first, bounded. Only rows the bridge persisted itself are
        returned; sessions created directly in the native agent server are
        never enumerated and no arbitrary session IDs are accepted here.
        Scoped to rows owned by the configured runtime (agent
        compatibility view); the neutral cross-runtime overview lives in
        ``Service.list_agent_runs``.
        """
        limit = max(1, min(int(limit), self.MAX_GLOBAL_SESSIONS_LIMIT))
        offset = max(0, int(offset))
        with self.service.lock:
            rows = self.service.db.execute(
                f"SELECT {RUN_COLUMNS} FROM agent_runs WHERE runtime=? "
                "ORDER BY created DESC LIMIT ? OFFSET ?",
                (self._list_runtime_id(), limit + 1, offset)).fetchall()
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
        # Legacy compatibility path: fail closed on rows owned by another
        # runtime before any backend or state work.
        self._require_run_runtime(run)
        if run["state"] in ACTIVE_RUN_STATES and self.runtime is not None:
            # A status check repairs a lost ask: resync backend-side
            # pending permissions and official pending questions for this
            # exact session before reporting.
            self._resync_permissions(ws, run)
            run = self._row(ws, run_id)
            if run["state"] in ACTIVE_RUN_STATES:
                self._resync_questions(ws, run)
                run = self._row(ws, run_id)
            # Self-heal a missed completion during normal operation: when
            # the run is still starting/running with no pending request,
            # one bounded durable probe completes it without a restart.
            # Waiting runs never complete here; runtime errors leave the
            # run active and retryable.
            if run["state"] in ("starting", "running") and run.get("started"):
                try:
                    if not self._pending(run):
                        self._probe_completion(run, reason="read_reconcile",
                                               allow_orphan=False)
                        run = self._row(ws, run_id)
                except BridgeError:
                    run = self._row(ws, run_id)
        view = self._summary(run)
        view["agent_evidence"] = "unverified"
        # 3C1: persisted-only execution audit summary (no backend calls,
        # no execution bodies). Proves what Pi invoked/reported, not that
        # a test result is semantically correct.
        try:
            view["execution_audit"] = self._pi_audit_summary(run)
        except Exception:  # noqa: BLE001 - audit summary never fails the read
            view["execution_audit"] = {"status": "not_recorded",
                                       "counts": {"total": 0, "failed": 0, "shell": 0, "mutating": 0}}
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
        """Whether a newer same-runtime Bridge run shares this run's session.

        Scoped by the run's persisted runtime identity plus session id: a
        future backend reusing the same native session id must never count
        as a later run for transcript safety.
        """
        with self.service.lock:
            count = self.service.db.execute(
                "SELECT count(*) FROM agent_runs WHERE runtime=? AND session=? AND id<>? AND created>?",
                (run.get("runtime") or PI_RUNTIME_ID, run["session"],
                 run["id"], run["created"])).fetchone()[0]
        return count > 0

    def read_request(self, ws: dict, run_id: str, request_id: str) -> dict:
        run = self._row(ws, run_id)
        self._require_run_runtime(run)
        with self.service.lock:
            row = self.service.db.execute(
                f"SELECT {REQUEST_COLUMNS} FROM agent_requests "
                "WHERE workspace=? AND run=? AND runtime_request=?",
                (ws["id"], run_id, request_id)).fetchone()
        if not row:
            raise BridgeError(f"Pending {self._runtime_label()} request not found for this run", "not_found")
        view = self._request_public(dict(row))
        view["run_state"] = run["state"]
        view["decisions"] = list(PERMISSIONS)
        view["always_allowed"] = bool(json.loads(row["pattern"] or "[]"))
        return view

    def _request_public(self, request: dict) -> dict:
        return neutral_request_public(request, self._runtime_label())

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
        stream = health.get("event_stream") if isinstance(health, dict) else None
        stream_view = dict(stream) if isinstance(stream, dict) else None
        if stream_view is not None:
            # Bridge-level functional classification, diagnostic only:
            # heartbeat-only transport reports functional_status=degraded
            # without ever blocking polling-based correctness.
            try:
                active = self._active_run_count()
                status, reason = self._classify_functional(stream_view, active)
                stream_view["functional_status"] = status
                stream_view["functional_reason"] = reason
            except Exception:  # noqa: BLE001 - status reads never fail on health
                pass
        return {"configured": True, "healthy": bool(health.get("ok")) and not locked,
                "locked": locked, "version": health.get("version"),
                "adapter_version": health.get("adapter_version"),
                "server_configured": health.get("server_configured"),
                "event_stream": stream_view,
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
        """Reconcile interrupted runs owned by the configured runtime.

        Foreign-runtime rows are never enumerated, mutated, orphaned, or
        added to this orchestrator's retry set.
        """
        if self.runtime is None:
            rows: list[dict] = []
        else:
            try:
                configured = self.runtime.runtime_id
            except NotImplementedError:
                configured = PI_RUNTIME_ID
            with self.service.lock:
                rows = [dict(r) for r in self.service.db.execute(
                    f"SELECT {RUN_COLUMNS} FROM agent_runs WHERE runtime=? AND state IN "
                    "('starting','running','waiting_permission','waiting_question')",
                    (configured,))]
        emit(_ops_log, "INFO", "bridge", "reconcile_start", examined=len(rows))
        pending: set[str] = set()
        outcomes: dict[str, int] = {}
        for run in rows:
            resolved = self._safe_reconcile(run)
            if not resolved:
                pending.add(run["id"])
            try:
                with self.service.lock:
                    current = self.service.db.execute(
                        "SELECT state FROM agent_runs WHERE id=?", (run["id"],)).fetchone()
                state = current["state"] if current else "unknown"
            except Exception:  # noqa: BLE001 - reconcile logging never fails startup
                state = "unknown"
            outcomes[state] = outcomes.get(state, 0) + 1
            emit(_ops_log, "INFO", "bridge", "reconcile_result", run_id=run["id"],
                 session_id=run.get("session"), workspace_id=run.get("workspace"),
                 state=str(state)[:80], reason="startup")
        with self._reconcile_guard:
            self._reconcile_pending |= pending
        emit(_ops_log, "INFO", "bridge", "startup_reconcile", examined=len(rows),
             count=len(rows) - len(pending), reason=f"pending={len(pending)}")

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
        except BridgeError as exc:
            if (exc.code or "") == "runtime_mismatch":
                # A stale/foreign id in the retry set is never this
                # orchestrator's work: drop it without backend calls,
                # mutation, or retry.
                return True
            emit(_ops_log, "WARNING", "bridge", "reconcile_result", run_id=run.get("id"),
                 session_id=run.get("session"), workspace_id=run.get("workspace"),
                 state=str(run.get("state"))[:80], reason="retry",
                 code=str(exc.code or "reconcile_error")[:80])
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
                self.service.event(None, "agent_reconcile", "failed")

    def _reconcile_run(self, run: dict) -> bool:
        """Return True when resolved (no further retry needed), False to retry later.

        Only a positive runtime result can orphan a run. Transient connectivity
        (adapter still starting) leaves the run explicitly active for retry.
        A foreign-runtime run raises ``runtime_mismatch`` before any backend
        call (defensive: startup enumeration already filters by runtime).
        """
        if self.runtime is None:
            return False
        self._require_run_runtime(run)
        ws = self._workspace(run)
        directory = self._directory(ws)
        try:
            session = self.runtime.get_session(directory, run["session"])
        except BridgeError:
            return False  # transient; retry when the runtime is reachable
        if session is None:
            self._orphan(run, "session_missing", f"{self._runtime_label()} session no longer exists")
            return True
        if not session.directory or not _same_directory(session.directory, directory):
            self._orphan(run, "session_mismatch",
                         f"{self._runtime_label()} session is bound to another directory")
            return True
        # Restart reconciliation also repairs a missed ask while the backend
        # still holds it; absence from the listing resolves nothing.
        self._resync_permissions(ws, run)
        run = self._row(ws, run["id"])
        pending = self._pending(run)
        if pending:
            # Keep waiting, but keep verifying the session so a later loss is explicit.
            desired = "waiting_question" if any(p["kind"] == "question" for p in pending) else "waiting_permission"
            if run["state"] != desired:
                self._set_state(run["id"], desired)
            return False
        if not self._eventless_idle_confirmed(ws, run):
            # A no-event session that is still busy (or whose status is
            # unknown) keeps no final evidence yet: stay explicitly active
            # and stop retrying; the background completion sweep observes
            # any later durable completion.
            return True
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
        # and stop retrying. A later durable completion is still observed by
        # the live event pump (idle/status hints) and the normal background
        # completion sweep, so no further restart is required.
        return True


#: Compatibility alias for the pre-neutral name; new code uses AgentOrchestrator.
