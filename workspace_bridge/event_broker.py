"""Durable run-event subscriptions and a separate signed-webhook outbox.

The notification journal remains canonical. Events contain identifiers and
state only, and delivery never starts a run or replays a prompt/approval.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import threading
import time
from datetime import datetime, timezone

from .notifications import SAFE_KINDS
from .security import BridgeError
from .webhook_transport import CallbackError, WebhookTransport, callback_url, signed_headers, signing_key

FINISHED = "workspace_bridge.run.finished"
NEEDS_ATTENTION = "workspace_bridge.run.needs_attention"
EVENT_NAMES = (FINISHED, NEEDS_ATTENTION)
MAX_SUBSCRIPTIONS = 1000
DEFAULT_TTL_MS = 24 * 60 * 60 * 1000
MIN_TTL_MS = 60 * 1000
MAX_TTL_MS = DEFAULT_TTL_MS
VERIFICATION_TTL = 300
KEY_ROTATION_SECONDS = 300
MAX_ATTEMPTS = 6
MAX_PENDING = 10000
OUTCOMES = {"run_completed": "succeeded", "run_failed": "failed", "run_cancelled": "cancelled",
            "run_interrupted": "interrupted", "run_orphaned": "orphaned", "run_blocked": "blocked"}


def _identity(value, prefix: str) -> bool:
    return isinstance(value, str) and re.fullmatch(prefix + r"_[0-9a-f]{24}", value) is not None


def _json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _stamp(value: float) -> str:
    return datetime.fromtimestamp(value, timezone.utc).isoformat().replace("+00:00", "Z")


class EventSubscriptionStore:
    """Private destinations/keys and delivery intent in the Bridge's 0600 DB."""

    def __init__(self, service):
        self.service = service
        columns = {row["name"] for row in service.db.execute("PRAGMA table_info(mcp_event_subscriptions)")}
        if "cursor_key" in columns:
            # Remove the unreleased polling prototype and its obsolete keys.
            service.db.execute("DROP TABLE mcp_event_subscriptions")
        service.db.executescript("""
          CREATE TABLE IF NOT EXISTS mcp_event_subscriptions (
            id TEXT PRIMARY KEY, principal TEXT NOT NULL, event_name TEXT NOT NULL,
            arguments TEXT NOT NULL, workspace_id TEXT NOT NULL, run_id TEXT NOT NULL,
            callback_url TEXT NOT NULL, signing_key BLOB NOT NULL, previous_key BLOB,
            previous_key_until REAL NOT NULL, expires_at REAL NOT NULL,
            verified_until REAL NOT NULL, last_sequence INTEGER NOT NULL,
            state TEXT NOT NULL, revision INTEGER NOT NULL,
            created_at REAL NOT NULL, updated_at REAL NOT NULL);
          CREATE INDEX IF NOT EXISTS ix_mcp_event_subscriptions_expiry
            ON mcp_event_subscriptions(expires_at);
          CREATE TABLE IF NOT EXISTS event_journal_positions (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL UNIQUE REFERENCES notification_events(id));
          CREATE TABLE IF NOT EXISTS mcp_event_deliveries (
            subscription_id TEXT NOT NULL REFERENCES mcp_event_subscriptions(id) ON DELETE CASCADE,
            event_id TEXT NOT NULL REFERENCES notification_events(id),
            status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
            next_attempt REAL NOT NULL, code TEXT NOT NULL DEFAULT '', updated_at REAL NOT NULL,
            PRIMARY KEY(subscription_id,event_id));
          CREATE INDEX IF NOT EXISTS ix_mcp_event_deliveries_pending
            ON mcp_event_deliveries(status,next_attempt);
          CREATE INDEX IF NOT EXISTS ix_notification_events_workspace_type
            ON notification_events(workspace_id,event_type);
        """)
        with service.lock, service.db:
            service.db.execute(
                "INSERT INTO event_journal_positions(event_id) SELECT e.id FROM notification_events e "
                "WHERE NOT EXISTS (SELECT 1 FROM event_journal_positions p WHERE p.event_id=e.id) ORDER BY e.rowid")
            service.db.execute("UPDATE mcp_event_deliveries SET status='pending' WHERE status='sending'")

    def head_locked(self) -> int:
        return self.service.db.execute("SELECT COALESCE(MAX(sequence),0) FROM event_journal_positions").fetchone()[0]


class EventBroker:
    def __init__(self, service, *, transport=None, clock=time.time):
        self.service = service
        self.store = EventSubscriptionStore(service)
        self.transport = transport or WebhookTransport()
        self._clock = clock
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._send_lock = threading.Lock()
        self._subscribe_lock = threading.Lock()
        self._thread = None

    def _principal_locked(self, token: str) -> str:
        if self._stop.is_set():
            raise BridgeError("Event broker unavailable", "events_unavailable")
        self.service.authenticate_bridge(token)
        return self.service.db.execute("SELECT token_hash FROM gateway WHERE id=1").fetchone()[0]

    @staticmethod
    def filters(arguments: dict) -> dict:
        if (not isinstance(arguments, dict) or set(arguments) - {"workspace_id", "run_id"}
                or not _identity(arguments.get("workspace_id"), "ws")
                or ("run_id" in arguments and not _identity(arguments["run_id"], "run"))):
            raise BridgeError("Invalid event filters", "invalid_event_arguments")
        return dict(arguments)

    @staticmethod
    def subscription_id(principal: str, name: str, arguments: dict, url: str) -> str:
        return "sub_" + hashlib.sha256(_json([principal, url, name, arguments]).encode()).hexdigest()[:24]

    def catalog(self, token: str) -> dict:
        with self.service.lock:
            self._principal_locked(token)
        filters = {"type": "object", "additionalProperties": False,
                   "required": ["workspace_id"], "properties": {
                       "workspace_id": {"type": "string", "pattern": "^ws_[0-9a-f]{24}$",
                                        "description": "Exact enabled workspace ID returned by list_workspaces."},
                       "run_id": {"type": "string", "pattern": "^run_[0-9a-f]{24}$",
                                  "description": "Optional exact Bridge run ID; omit to monitor all runs in the workspace."}}}
        base = {"workspace_id": {"type": "string"}, "run_id": {"type": "string"},
                "adapter_id": {"type": "string"}, "runtime_type": {"enum": ["pi", "codex", "claude"]}}
        return {"events": [
            {"name": FINISHED, "description": "A run finished in the selected workspace. Read read_agent_run for its authoritative result and audit evidence.",
             "delivery": ["webhook"], "inputSchema": filters,
             "payloadSchema": {"type": "object", "additionalProperties": False,
                               "required": [*base, "outcome"],
                               "properties": {**base, "outcome": {"enum": list(OUTCOMES.values())}}}},
            {"name": NEEDS_ATTENTION, "description": "A run in the selected workspace has an interaction to review. Read the current interaction before responding; events never approve it.",
             "delivery": ["webhook"], "inputSchema": filters,
             "payloadSchema": {"type": "object", "additionalProperties": False, "required": list(base),
                               "properties": {**base, "interaction_id": {"type": "string"},
                                              "request_kind": {"enum": sorted(SAFE_KINDS)}}}},
        ], "nextCursor": None}

    def subscribe(self, token: str, name: str, arguments: dict, delivery: dict,
                  *, ttl_ms: int | None = None, cursor=None) -> dict:
        if name not in EVENT_NAMES or cursor is not None:
            raise BridgeError("Invalid event subscription", "invalid_event_arguments")
        arguments = self.filters(arguments)
        if (not isinstance(delivery, dict) or set(delivery) != {"mode", "url", "secret"}
                or delivery["mode"] != "webhook"):
            raise BridgeError("Only webhook delivery is supported", "invalid_event_arguments")
        if ttl_ms is not None and (type(ttl_ms) is not int or not 1 <= ttl_ms <= 2**53 - 1):
            raise BridgeError("Invalid subscription lifetime", "invalid_event_arguments")
        try:
            url, key = callback_url(delivery["url"]), signing_key(delivery["secret"])
        except CallbackError:
            raise BridgeError("Invalid webhook destination or signing secret", "invalid_event_arguments") from None
        duration = min(max(DEFAULT_TTL_MS if ttl_ms is None else ttl_ms, MIN_TTL_MS), MAX_TTL_MS) / 1000
        # Network verification never holds the shared Service DB lock.
        with self._subscribe_lock:
            with self.service.lock:
                principal = self._principal_locked(token)
                self._authorize_filters_locked(arguments)
                ident = self.subscription_id(principal, name, arguments, url)
                verified = self.service.db.execute(
                    "SELECT MAX(verified_until) FROM mcp_event_subscriptions WHERE principal=? AND callback_url=?",
                    (principal, url)).fetchone()[0]
            if not verified or verified <= self._clock():
                self.transport.verify(url, ident, key)
                verified = self._clock() + VERIFICATION_TTL
            with self._send_lock, self.service.lock, self.service.db:
                # Auth/access may have changed while the callback was checked.
                if self._principal_locked(token) != principal:
                    raise BridgeError("Bridge credential changed", "unauthorized")
                self._authorize_filters_locked(arguments)
                timestamp = self._clock()
                # The worker may have advanced the journal while verification
                # was in flight. Refresh its current position, never a stale one.
                existing = self.service.db.execute("SELECT * FROM mcp_event_subscriptions WHERE id=?", (ident,)).fetchone()
                self.service.db.execute(
                    "DELETE FROM mcp_event_subscriptions WHERE id!=? AND (expires_at<=? OR principal!=? OR state!='active')",
                    (ident, timestamp, principal))
                if existing is None and self.service.db.execute("SELECT COUNT(*) FROM mcp_event_subscriptions").fetchone()[0] >= MAX_SUBSCRIPTIONS:
                    raise BridgeError("Event subscription limit reached", "subscription_limit")
                old = bytes(existing["signing_key"]) if existing else None
                previous = old if old is not None and not secrets.compare_digest(old, key) else (
                    existing["previous_key"] if existing and existing["previous_key_until"] > timestamp else None)
                previous_until = timestamp + KEY_ROTATION_SECONDS if old is not None and old != key else (
                    existing["previous_key_until"] if previous is not None else 0)
                live = existing is not None and existing["state"] == "active" and existing["expires_at"] > timestamp
                sequence = existing["last_sequence"] if live else self.store.head_locked()
                if not live:
                    self.service.db.execute("DELETE FROM mcp_event_deliveries WHERE subscription_id=?", (ident,))
                self.service.db.execute(
                    "INSERT INTO mcp_event_subscriptions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(id) DO UPDATE SET signing_key=excluded.signing_key,previous_key=excluded.previous_key,"
                    "previous_key_until=excluded.previous_key_until,expires_at=excluded.expires_at,"
                    "verified_until=excluded.verified_until,last_sequence=excluded.last_sequence,state='active',"
                    "revision=mcp_event_subscriptions.revision+1,updated_at=excluded.updated_at",
                    (ident, principal, name, _json(arguments), arguments["workspace_id"], arguments.get("run_id", ""),
                     url, key, previous, previous_until, timestamp + duration, verified, sequence, "active", 1,
                     existing["created_at"] if existing else timestamp, timestamp))
            self.wake()
            return {"id": ident, "refreshBefore": _stamp(timestamp + duration), "cursor": None, "truncated": False}

    def _authorize_filters_locked(self, arguments: dict) -> None:
        ws = self.service.workspace(arguments["workspace_id"])
        if arguments.get("run_id"):
            row = self.service.db.execute("SELECT workspace FROM agent_runs WHERE id=?", (arguments["run_id"],)).fetchone()
            if row is None or row["workspace"] != ws["id"]:
                raise BridgeError("Run unavailable in this workspace", "unavailable")

    def unsubscribe(self, token: str, name: str, arguments: dict, delivery: dict) -> dict:
        arguments = self.filters(arguments)
        if name not in EVENT_NAMES or not isinstance(delivery, dict) or set(delivery) != {"mode", "url"} or delivery["mode"] != "webhook":
            raise BridgeError("Invalid event unsubscribe", "invalid_event_arguments")
        try:
            url = callback_url(delivery["url"])
        except CallbackError:
            raise BridgeError("Invalid webhook destination", "invalid_event_arguments") from None
        with self._send_lock, self.service.lock, self.service.db:
            principal = self._principal_locked(token)
            ident = self.subscription_id(principal, name, arguments, url)
            self.service.db.execute("DELETE FROM mcp_event_subscriptions WHERE id=? AND principal=?", (ident, principal))
        return {}

    def record_locked(self, event_id: str) -> None:
        self.service.db.execute("INSERT OR IGNORE INTO event_journal_positions(event_id) VALUES(?)", (event_id,))
        self.wake()

    @staticmethod
    def project(row) -> dict | None:
        if (not _identity(row["workspace_id"], "ws") or not _identity(row["run_id"], "run")
                or not _identity(row["adapter_id"], "adapter") or not _identity(row["id"], "nev")
                or row["runtime_type"] not in {"pi", "codex", "claude"}):
            return None
        data = {"workspace_id": row["workspace_id"], "run_id": row["run_id"],
                "adapter_id": row["adapter_id"], "runtime_type": row["runtime_type"]}
        name = NEEDS_ATTENTION if row["event_type"] == "run_needs_attention" else FINISHED
        if name == FINISHED:
            if row["event_type"] not in OUTCOMES:
                return None
            data["outcome"] = OUTCOMES[row["event_type"]]
        else:
            if _identity(row["subject_id"], "int"):
                data["interaction_id"] = row["subject_id"]
            if row["request_kind"] in SAFE_KINDS:
                data["request_kind"] = row["request_kind"]
        return {"eventId": row["id"], "name": name, "timestamp": row["occurred_at"], "data": data, "cursor": None}

    def _active_locked(self, subscription, timestamp: float) -> bool:
        gateway = self.service.db.execute("SELECT enabled,token_hash FROM gateway WHERE id=1").fetchone()
        workspace = self.service.db.execute("SELECT enabled FROM workspaces WHERE id=?", (subscription["workspace_id"],)).fetchone()
        return (subscription["state"] == "active" and subscription["expires_at"] > timestamp
                and gateway["enabled"] and secrets.compare_digest(gateway["token_hash"], subscription["principal"])
                and workspace is not None and workspace["enabled"])

    def _queue_locked(self, timestamp: float) -> None:
        self.service.db.execute("UPDATE mcp_event_subscriptions SET previous_key=NULL,previous_key_until=0 "
                                "WHERE previous_key_until>0 AND previous_key_until<=?", (timestamp,))
        head, queued = self.store.head_locked(), 0
        pending = self.service.db.execute("SELECT COUNT(*) FROM mcp_event_deliveries WHERE status IN ('pending','sending')").fetchone()[0]
        subscriptions = self.service.db.execute("SELECT * FROM mcp_event_subscriptions ORDER BY last_sequence,id").fetchall()
        for sub in subscriptions:
            if not self._active_locked(sub, timestamp):
                if sub["state"] == "active":
                    self.service.db.execute("UPDATE mcp_event_subscriptions SET state=? WHERE id=?",
                                            ("expired" if sub["expires_at"] <= timestamp else "revoked", sub["id"]))
                self.service.db.execute("UPDATE mcp_event_deliveries SET status='disabled',code='access_revoked' "
                                        "WHERE subscription_id=? AND status='pending'", (sub["id"],))
                continue
            if pending + queued >= MAX_PENDING or queued >= 200:
                break
            types = list(OUTCOMES) if sub["event_name"] == FINISHED else ["run_needs_attention"]
            rows = self.service.db.execute(
                "SELECT p.sequence,e.id FROM event_journal_positions p JOIN notification_events e ON e.id=p.event_id "
                "WHERE p.sequence>? AND e.workspace_id=? AND (?='' OR e.run_id=?) AND e.event_type IN ("
                + ",".join("?" for _ in types) + ") ORDER BY p.sequence LIMIT ?",
                (sub["last_sequence"], sub["workspace_id"], sub["run_id"], sub["run_id"], *types,
                 min(50, MAX_PENDING - pending - queued, 200 - queued))).fetchall()
            for row in rows:
                self.service.db.execute("INSERT OR IGNORE INTO mcp_event_deliveries(subscription_id,event_id,status,next_attempt,updated_at) "
                                        "VALUES(?,?,'pending',?,?)", (sub["id"], row["id"], timestamp, timestamp))
            queued += len(rows)
            sequence = rows[-1]["sequence"] if rows else head
            if sequence != sub["last_sequence"]:
                self.service.db.execute("UPDATE mcp_event_subscriptions SET last_sequence=? WHERE id=?", (sequence, sub["id"]))
        self.service.db.execute("DELETE FROM mcp_event_deliveries WHERE status IN ('sent','failed','disabled') AND rowid<"
                                "(SELECT COALESCE(MAX(rowid),0)-10000 FROM mcp_event_deliveries)")

    def drain_once(self, *, limit: int = 100) -> None:
        with self.service.lock, self.service.db:
            if self._stop.is_set():
                return
            self._queue_locked(self._clock())
        for _ in range(limit):
            if self._stop.is_set():
                return
            # Unsubscribe waits for an in-flight request before returning. This
            # dedicated lock never blocks run state or ordinary Service tools.
            with self._send_lock:
                with self.service.lock, self.service.db:
                    timestamp = self._clock()
                    candidate = self.service.db.execute(
                        "SELECT d.subscription_id,d.event_id,d.attempts FROM mcp_event_deliveries d "
                        "JOIN event_journal_positions p ON p.event_id=d.event_id "
                        "WHERE d.status='pending' AND d.next_attempt<=? ORDER BY p.sequence,d.subscription_id LIMIT 1",
                        (timestamp,)).fetchone()
                    if candidate is None:
                        return
                    sub = self.service.db.execute("SELECT * FROM mcp_event_subscriptions WHERE id=?", (candidate["subscription_id"],)).fetchone()
                    if sub is None or not self._active_locked(sub, timestamp):
                        self.service.db.execute("UPDATE mcp_event_deliveries SET status='disabled',code='access_revoked' "
                                                "WHERE subscription_id=? AND event_id=?", (candidate["subscription_id"], candidate["event_id"]))
                        continue
                    row = self.service.db.execute("SELECT id,event_type,workspace_id,run_id,adapter_id,runtime_type,occurred_at,"
                                                  "subject_id,request_kind FROM notification_events WHERE id=?", (candidate["event_id"],)).fetchone()
                    event = self.project(row)
                    attempts = candidate["attempts"] + 1
                    self.service.db.execute("UPDATE mcp_event_deliveries SET status='sending',attempts=?,updated_at=? "
                                            "WHERE subscription_id=? AND event_id=?", (attempts, timestamp, sub["id"], row["id"]))
                    sub = dict(sub)
                code, status, transient = "invalid_event", 0, False
                if event is not None:
                    body = _json(event).encode("ascii")
                    previous = bytes(sub["previous_key"]) if sub["previous_key"] is not None and sub["previous_key_until"] > timestamp else None
                    headers = signed_headers(sub["id"], event["eventId"], body, bytes(sub["signing_key"]),
                                             previous_key=previous, timestamp=int(timestamp))
                    try:
                        response = self.transport.post(sub["callback_url"], body, headers)
                        status = response.status
                        code = "accepted" if 200 <= status < 300 else f"http_{status}"
                        transient = status in {408, 429} or 500 <= status < 600
                    except CallbackError as exc:
                        code = exc.reason
                        transient = code in {"timeout", "connection_failed", "dns_failed"}
                    except Exception:
                        code, transient = "delivery_failed", True
                with self.service.lock, self.service.db:
                    if self._stop.is_set():
                        return  # sending intent is recovered on next startup
                    outcome = "sent" if 200 <= status < 300 else "pending" if transient and attempts < MAX_ATTEMPTS else "failed"
                    self.service.db.execute("UPDATE mcp_event_deliveries SET status=?,next_attempt=?,code=?,updated_at=? "
                                            "WHERE subscription_id=? AND event_id=?",
                                            (outcome, self._clock() + min(2 ** (attempts - 1), 60), code,
                                             self._clock(), sub["id"], candidate["event_id"]))
                    if status == 410:
                        self.service.db.execute("UPDATE mcp_event_subscriptions SET state='terminated' WHERE id=?", (sub["id"],))
                        self.service.db.execute("UPDATE mcp_event_deliveries SET status='disabled',code='endpoint_gone' "
                                                "WHERE subscription_id=? AND status='pending'", (sub["id"],))

    def status(self) -> dict:
        with self.service.lock:
            states = {row["state"]: row["count"] for row in self.service.db.execute(
                "SELECT state,COUNT(*) AS count FROM mcp_event_subscriptions GROUP BY state")}
            deliveries = {row["status"]: row["count"] for row in self.service.db.execute(
                "SELECT status,COUNT(*) AS count FROM mcp_event_deliveries GROUP BY status")}
        return {"delivery": "webhook", "subscriptions": states, "deliveries": deliveries}

    def wake(self) -> None:
        self._wake.set()

    def start(self) -> None:
        if self._thread is None and not self._stop.is_set():
            self._thread = threading.Thread(target=self._worker, name="mcp-event-outbox", daemon=True)
            self._thread.start()

    def _worker(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(0.5)
            self._wake.clear()
            try:
                self.drain_once()
            except Exception:
                # Never log raw callback failures; durable intent remains.
                pass

    def close(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=11)
