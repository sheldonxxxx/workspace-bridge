"""Bridge-owned, runtime-neutral notifications with a durable channel outbox.

Events contain bounded display metadata only. Runtime orchestration writes the
event and per-channel delivery intent to SQLite before any channel is called;
channel adapters own formatting, network behavior, and retry policy.
"""
from __future__ import annotations

import json
import hashlib
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from .security import BridgeError, redact

DEFAULT_TIMEOUT = 6.0
DEFAULT_ATTEMPTS = 3
MAX_DIAGNOSTIC_CHARS = 160
MAX_ERROR_BODY_BYTES = 2048
MAX_SUMMARY_EVENTS = 20
EVENT_TYPES = frozenset({"run_completed", "run_failed", "run_cancelled",
                         "run_interrupted", "run_orphaned", "run_blocked",
                         "run_needs_attention"})
EVENT_TO_STATE = {
    "run_completed": "completed", "run_failed": "failed",
    "run_cancelled": "cancelled", "run_interrupted": "interrupted",
    "run_orphaned": "orphaned", "run_blocked": "blocked",
    "run_needs_attention": "needs_attention",
}
STATE_EVENTS = {
    "completed": "run_completed", "failed": "run_failed",
    "cancelled": "run_cancelled", "interrupted": "run_interrupted",
    "orphaned": "run_orphaned", "blocked": "run_blocked",
    "waiting_interaction": "run_needs_attention",
}
SAFE_KINDS = frozenset({"permission", "question", "choice", "approval",
                        "form", "interaction"})
BRIDGE_USER_AGENT = "Workspace-Bridge/0.8.4 (+local-admin; notification channel)"


def _safe_label(value: object, fallback: str, limit: int) -> str:
    text = value if isinstance(value, str) else ""
    text = "".join(char for char in text if char == " " or ord(char) >= 32)
    text, _ = redact(text[:limit * 2])
    text = text.strip()[:limit]
    lowered = text.lower()
    if (not text or "[REDACTED_SECRET]" in text or "http://" in lowered
            or "https://" in lowered or re.search(r"(?:^|\s)(?:/|~[/\\]|[a-z]:[/\\])", text)):
        return fallback
    return text


def _safe_id(value: object, fallback: str = "") -> str:
    text = value if isinstance(value, str) else ""
    if len(text) > 200 or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", text):
        return fallback
    return text


def _safe_code(value: object) -> str:
    text = value if isinstance(value, str) else ""
    return text[:60] if re.fullmatch(r"[a-zA-Z0-9_.-]{1,60}", text) else "channel_error"


def _safe_detail(value: object) -> str:
    text = value if isinstance(value, str) else ""
    text, _ = redact(text[:MAX_DIAGNOSTIC_CHARS * 2])
    text = "".join(char for char in text if char == " " or ord(char) >= 32)[:MAX_DIAGNOSTIC_CHARS]
    lowered = text.lower()
    if ("[REDACTED_SECRET]" in text or "http://" in lowered or "https://" in lowered
            or re.search(r"(?:^|\s)(?:/|~[/\\]|[a-z]:[/\\])", text)):
        return ""
    return text


@dataclass(frozen=True)
class NotificationEvent:
    """Immutable safe event envelope shared by every channel adapter."""

    id: str
    dedupe_key: str
    event_type: str
    run_id: str
    workspace_id: str
    workspace_name: str
    handoff_title: str
    runtime: str
    occurred_at: str
    subject_id: str = ""
    request_kind: str = ""
    action: str = ""

    @classmethod
    def build(cls, *, event_type: str, run_id: str, workspace_id: str,
              workspace_name: str, handoff_title: str, runtime: str,
              occurred_at: str | None = None, subject_id: str = "",
              request_kind: str = "", action: str = "") -> "NotificationEvent":
        if event_type not in EVENT_TYPES:
            raise BridgeError("Unknown notification event type", "invalid_notification")
        safe_run = _safe_id(run_id)
        safe_workspace = _safe_id(workspace_id)
        safe_runtime = _safe_id(runtime)
        raw_subject = subject_id if isinstance(subject_id, str) else ""
        safe_subject = _safe_id(raw_subject)
        if raw_subject and not safe_subject:
            safe_subject = "sub_" + hashlib.sha256(raw_subject.encode("utf-8", errors="replace")).hexdigest()[:32]
        if not safe_run or not safe_workspace or not safe_runtime:
            raise BridgeError("Invalid notification identity", "invalid_notification")
        if event_type == "run_needs_attention" and not safe_subject:
            raise BridgeError("Attention notifications require a subject id", "invalid_notification")
        kind = request_kind if request_kind in SAFE_KINDS else ""
        safe_action = _safe_id(action)
        stamp = ""
        if isinstance(occurred_at, str) and len(occurred_at) <= 40:
            try:
                datetime.fromisoformat(occurred_at.replace("Z", "+00:00"))
                stamp = occurred_at
            except ValueError:
                pass
        if not stamp:
            stamp = datetime.now(timezone.utc).isoformat()
        dedupe = (f"attention:{safe_run}:{safe_subject}" if event_type == "run_needs_attention"
                  else f"run:{safe_run}:{event_type}")
        return cls(
            id="nev_" + secrets.token_hex(12), dedupe_key=dedupe,
            event_type=event_type, run_id=safe_run, workspace_id=safe_workspace,
            workspace_name=_safe_label(workspace_name, "workspace", 100),
            handoff_title=_safe_label(handoff_title, "handoff", 100),
            runtime=safe_runtime, occurred_at=stamp, subject_id=safe_subject,
            request_kind=kind, action=safe_action)


@dataclass(frozen=True)
class NotificationResult:
    status: str
    attempts: int
    code: str = ""
    detail: str = ""

    def safe(self) -> "NotificationResult":
        status = self.status if self.status in {"sent", "failed", "disabled"} else "failed"
        try:
            attempts = max(0, min(int(self.attempts), 20))
        except (TypeError, ValueError):
            attempts = 0
        return NotificationResult(status, attempts,
                                 _safe_code(self.code) if self.code else "",
                                 _safe_detail(self.detail))

    def public(self) -> dict:
        result = self.safe()
        public = {"status": result.status, "attempts": result.attempts,
                  "code": result.code}
        if result.detail:
            public["detail"] = result.detail
        return public


class NotificationChannel(Protocol):
    channel_id: str
    name: str
    enabled: bool

    def deliver(self, event: NotificationEvent) -> NotificationResult: ...


def _safe_diagnostic(body: bytes, content_type: str = "") -> str:
    """Extract a bounded public diagnostic from a JSON Discord error body."""
    if not body:
        return ""
    ctype = (content_type or "").split(";")[0].strip().lower()
    if ctype and "json" not in ctype:
        return ""
    try:
        payload = json.loads(body[:MAX_ERROR_BODY_BYTES].decode("utf-8", errors="replace"))
    except (UnicodeDecodeError, ValueError):
        return ""
    if not isinstance(payload, dict):
        return ""
    parts: list[str] = []
    code = payload.get("code")
    if isinstance(code, int) and not isinstance(code, bool):
        parts.append(f"discord_{code}")
    message = payload.get("message")
    if isinstance(message, str) and message.strip():
        safe = _safe_detail(message.strip())
        if safe:
            parts.append(safe[:120])
    detail = ": ".join(parts)[:MAX_DIAGNOSTIC_CHARS]
    lowered = detail.lower()
    if any(token in lowered for token in ("discord.com", "/home/", "/users/")):
        return parts[0][:MAX_DIAGNOSTIC_CHARS] if parts else ""
    return detail


class DiscordChannel:
    """Discord webhook adapter; endpoint and embed details stay channel-local."""

    channel_id = "discord"
    name = "Discord"
    enabled = True

    def __init__(self, webhook_url: str, *, timeout: float = DEFAULT_TIMEOUT,
                 attempts: int = DEFAULT_ATTEMPTS, sleep=time.sleep):
        parsed = urllib.parse.urlsplit(webhook_url)
        if parsed.scheme not in {"https", "http"} or not parsed.netloc:
            raise BridgeError("WB_DISCORD_WEBHOOK_URL must be an http(s) URL")
        self._webhook = webhook_url
        self.timeout = max(1.0, min(float(timeout), 20.0))
        self.attempts = max(1, min(int(attempts), 5))
        self._sleep = sleep

    @staticmethod
    def _payload(event: NotificationEvent) -> dict:
        state = EVENT_TO_STATE[event.event_type]
        attention = event.event_type == "run_needs_attention"
        headline = "Agent run needs attention" if attention else f"Agent run {state}"
        fields = [
            {"name": "Workspace", "value": event.workspace_name, "inline": True},
            {"name": "Handoff", "value": event.handoff_title, "inline": True},
            {"name": "Run", "value": event.run_id[:80], "inline": True},
            {"name": "Outcome", "value": state[:40], "inline": True},
        ]
        if event.request_kind:
            fields.append({"name": "Request", "value": event.request_kind[:60], "inline": True})
        if event.action:
            fields.append({"name": "Action", "value": event.action[:80], "inline": True})
        colors = {"run_needs_attention": 0xE0A800, "run_completed": 0x2E9E5B,
                  "run_blocked": 0x9E6B2E, "run_failed": 0xC0392B,
                  "run_cancelled": 0x6B7280, "run_interrupted": 0x6B7280,
                  "run_orphaned": 0x6B7280}
        description = ("Review the pending request in ChatGPT/local manager; details are not sent to Discord."
                       if attention else "Read the final run result in ChatGPT/local manager.")
        embed = {"title": headline, "color": colors[event.event_type],
                 "description": description, "fields": fields,
                 "timestamp": event.occurred_at}
        return {"embeds": [embed], "allowed_mentions": {"parse": []}}

    def deliver(self, event: NotificationEvent) -> NotificationResult:
        body = json.dumps(self._payload(event), ensure_ascii=False).encode("utf-8")
        last_code = ""
        last_detail = ""
        attempt = 0
        for attempt in range(1, self.attempts + 1):
            request = urllib.request.Request(
                self._webhook, data=body, method="POST",
                headers={"Content-Type": "application/json", "Accept": "application/json",
                         "User-Agent": BRIDGE_USER_AGENT})
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    if 200 <= response.status < 300:
                        return NotificationResult("sent", attempt)
                    last_code = f"http_{response.status}"
                    last_detail = ""
            except urllib.error.HTTPError as exc:
                last_code = f"http_{exc.code}"
                try:
                    content_type = str(getattr(exc, "headers", None) and
                                       exc.headers.get_content_type() or "")
                    raw_body = exc.read(MAX_ERROR_BODY_BYTES) if exc.fp else b""
                except Exception:  # noqa: BLE001 - error response content is untrusted
                    content_type, raw_body = "", b""
                last_detail = _safe_diagnostic(raw_body, content_type)
                if 400 <= exc.code < 500 and exc.code != 429:
                    break
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
                last_code, last_detail = "unreachable", ""
            if attempt < self.attempts:
                self._sleep(min(2 ** attempt * 0.5, 4.0))
        return NotificationResult("failed", attempt, last_code, last_detail).safe()


def notification_channels_from_environment(environ: dict | None = None,
                                           discord_factory=DiscordChannel) -> list[NotificationChannel]:
    """Construct named channel adapters from local environment configuration."""
    env = environ if environ is not None else os.environ
    url = (env.get("WB_DISCORD_WEBHOOK_URL") or "").strip()
    if not url:
        return []
    try:
        return [discord_factory(url)]
    except (BridgeError, ValueError):
        return []


class NotificationManager:
    """Bridge-owned durable outbox and independent channel fanout."""

    def __init__(self, service, channels: list[NotificationChannel] | None = None):
        self.service = service
        self.channels = {}
        for channel in channels or []:
            channel_id = getattr(channel, "channel_id", "")
            if not isinstance(channel_id, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,39}", channel_id):
                raise BridgeError("Invalid notification channel id", "invalid_notification")
            if channel_id in self.channels:
                raise BridgeError("Duplicate notification channel id", "invalid_notification")
            if not callable(getattr(channel, "deliver", None)):
                raise BridgeError("Notification channel must implement deliver(event)",
                                  "invalid_notification")
            self.channels[channel_id] = channel
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._signal_lock = threading.Lock()
        self._idle = threading.Event()
        self._idle.set()
        self._thread: threading.Thread | None = None
        service.db.executescript("""
          CREATE TABLE IF NOT EXISTS notification_events (
            id TEXT PRIMARY KEY, dedupe_key TEXT NOT NULL UNIQUE,
            run_id TEXT NOT NULL, workspace_id TEXT NOT NULL,
            workspace_name TEXT NOT NULL, handoff_title TEXT NOT NULL,
            runtime TEXT NOT NULL, event_type TEXT NOT NULL,
            subject_id TEXT NOT NULL DEFAULT '', request_kind TEXT NOT NULL DEFAULT '',
            action TEXT NOT NULL DEFAULT '', occurred_at TEXT NOT NULL,
            created_at TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS notification_deliveries (
            event_id TEXT NOT NULL, channel_id TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('pending','sending','sent','failed','disabled')),
            attempts INTEGER NOT NULL DEFAULT 0, code TEXT NOT NULL DEFAULT '',
            detail TEXT NOT NULL DEFAULT '', updated TEXT NOT NULL,
            PRIMARY KEY(event_id,channel_id));
          CREATE INDEX IF NOT EXISTS ix_notification_deliveries_status
            ON notification_deliveries(status,updated);
          CREATE INDEX IF NOT EXISTS ix_notification_events_run
            ON notification_events(run_id,occurred_at DESC);
        """)
        # A process exit while a channel is in flight is ambiguous. Requeue
        # those intents on restart; persisted sent rows remain untouched.
        with service.lock, service.db:
            service.db.execute(
                "UPDATE notification_deliveries SET status='pending',updated=? WHERE status='sending'",
                (self._now(),))

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def status(self) -> dict:
        return {"configured": bool(self.channels), "channel_count": len(self.channels),
                "channels": {key: {"name": _safe_label(getattr(channel, "name", key), key, 60),
                                   "configured": bool(getattr(channel, "enabled", True)),
                                   "ready": bool(getattr(channel, "ready", True))}
                             for key, channel in sorted(self.channels.items())}}

    def _record_event_locked(self, event: NotificationEvent) -> tuple[str, bool]:
        """Record event intent; caller owns the service DB lock/transaction."""
        event = NotificationEvent.build(
            event_type=event.event_type, run_id=event.run_id,
            workspace_id=event.workspace_id, workspace_name=event.workspace_name,
            handoff_title=event.handoff_title, runtime=event.runtime,
            occurred_at=event.occurred_at, subject_id=event.subject_id,
            request_kind=event.request_kind, action=event.action)
        self.service.db.execute(
            "INSERT OR IGNORE INTO notification_events(id,dedupe_key,run_id,workspace_id,"
            "workspace_name,handoff_title,runtime,event_type,subject_id,request_kind,action,"
            "occurred_at,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (event.id, event.dedupe_key, event.run_id, event.workspace_id,
             event.workspace_name, event.handoff_title, event.runtime, event.event_type,
             event.subject_id, event.request_kind, event.action, event.occurred_at, self._now()))
        row = self.service.db.execute(
            "SELECT id FROM notification_events WHERE dedupe_key=?", (event.dedupe_key,)).fetchone()
        event_id = row["id"]
        created = event_id == event.id
        if created:
            for channel_id in self.channels:
                self.service.db.execute(
                    "INSERT OR IGNORE INTO notification_deliveries(event_id,channel_id,status,updated) "
                    "VALUES(?,?,'pending',?)", (event_id, channel_id, self._now()))
        self.wake()
        return event_id, created

    def record_run_event_locked(self, *, run_id: str, workspace_id: str,
                                job_id: str, runtime: str, event_type: str,
                                subject_id: str = "", request_kind: str = "",
                                action: str = "", occurred_at: str | None = None) -> tuple[str, bool]:
        """Insert a run event inside the caller's state transaction."""
        row = self.service.db.execute(
            "SELECT w.name,j.title FROM workspaces w LEFT JOIN jobs j ON j.id=? "
            "WHERE w.id=?", (job_id, workspace_id)).fetchone()
        event = NotificationEvent.build(
            event_type=event_type, run_id=run_id, workspace_id=workspace_id,
            workspace_name=row["name"] if row else "workspace",
            handoff_title=row["title"] if row and row["title"] else "handoff",
            runtime=runtime, occurred_at=occurred_at, subject_id=subject_id,
            request_kind=request_kind, action=action)
        return self._record_event_locked(event)

    def publish(self, event: NotificationEvent) -> str:
        with self.service.lock, self.service.db:
            event_id, _ = self._record_event_locked(event)
        return event_id

    def publish_run_event(self, *, run_id: str, workspace_id: str, job_id: str,
                          runtime: str, event_type: str, subject_id: str = "",
                          request_kind: str = "", action: str = "",
                          occurred_at: str | None = None) -> str:
        with self.service.lock, self.service.db:
            event_id, _ = self.record_run_event_locked(
                run_id=run_id, workspace_id=workspace_id, job_id=job_id,
                runtime=runtime, event_type=event_type, subject_id=subject_id,
                request_kind=request_kind, action=action, occurred_at=occurred_at)
        return event_id

    def wake(self) -> None:
        """Signal the worker after durable intent is recorded; never deliver inline."""
        with self._signal_lock:
            if self._stop.is_set():
                return
            self._idle.clear()
            self._wake.set()

    def _drain_pending(self) -> None:
        """Deliver pending rows independently, with channel calls outside DB locks."""
        while not self._stop.is_set():
            with self.service.lock:
                if self._stop.is_set():
                    return
                with self.service.db:
                    rows = self.service.db.execute(
                        "SELECT d.event_id,d.channel_id,e.* FROM notification_deliveries d "
                        "JOIN notification_events e ON e.id=d.event_id "
                        "WHERE d.status='pending' ORDER BY e.created_at,d.channel_id LIMIT 1").fetchall()
                    if not rows:
                        return
                    row = dict(rows[0])
                    channel = self.channels.get(row["channel_id"])
                    if channel is None:
                        self.service.db.execute(
                            "UPDATE notification_deliveries SET status='disabled',"
                            "code='channel_unconfigured',detail='',updated=? "
                            "WHERE event_id=? AND channel_id=? AND status='pending'",
                            (self._now(), row["event_id"], row["channel_id"]))
                        continue
                    cursor = self.service.db.execute(
                        "UPDATE notification_deliveries SET status='sending',updated=? "
                        "WHERE event_id=? AND channel_id=? AND status='pending'",
                        (self._now(), row["event_id"], row["channel_id"]))
                    if cursor.rowcount != 1:
                        continue
            event = NotificationEvent(
                id=row["id"], dedupe_key=row["dedupe_key"], event_type=row["event_type"],
                run_id=row["run_id"], workspace_id=row["workspace_id"],
                workspace_name=row["workspace_name"], handoff_title=row["handoff_title"],
                runtime=row["runtime"], occurred_at=row["occurred_at"],
                subject_id=row["subject_id"], request_kind=row["request_kind"],
                action=row["action"])
            try:
                result = channel.deliver(event)
                if not isinstance(result, NotificationResult):
                    raise TypeError("channel returned an invalid result")
                result = result.safe()
            except Exception:  # noqa: BLE001 - delivery must never affect run state
                result = NotificationResult("failed", 1, "channel_error")
            # If shutdown begins during remote delivery, leave the row as
            # 'sending'. Startup recovery treats that as ambiguous and retries.
            if self._stop.is_set():
                return
            with self.service.lock:
                if self._stop.is_set():
                    return
                with self.service.db:
                    self.service.db.execute(
                        "UPDATE notification_deliveries SET status=?,attempts=?,code=?,detail=?,updated=? "
                        "WHERE event_id=? AND channel_id=? AND status='sending'",
                        (result.status, result.attempts, result.code, result.detail,
                         self._now(), row["event_id"], row["channel_id"]))

    def summary(self, run_id: str) -> dict:
        with self.service.lock:
            events = self.service.db.execute(
                "SELECT * FROM notification_events WHERE run_id=? "
                "ORDER BY created_at DESC,id DESC LIMIT ?", (run_id, MAX_SUMMARY_EVENTS)).fetchall()
            all_rows = self.service.db.execute(
                "SELECT d.channel_id,d.status,d.attempts,d.code,d.detail,d.updated "
                "FROM notification_deliveries d JOIN notification_events e ON e.id=d.event_id "
                "WHERE e.run_id=? ORDER BY d.updated DESC", (run_id,)).fetchall()
            event_deliveries = {}
            for event in events:
                event_deliveries[event["id"]] = self.service.db.execute(
                    "SELECT channel_id,status FROM notification_deliveries WHERE event_id=?",
                    (event["id"],)).fetchall()
        delivery_rows: dict[str, list[dict]] = {}
        for row in all_rows:
            delivery_rows.setdefault(row["channel_id"], []).append(dict(row))
        event_rows = []
        for event in events:
            delivery = event_deliveries.get(event["id"], [])
            event_rows.append({"id": event["id"], "event_type": event["event_type"],
                               "subject_id": event["subject_id"] or None,
                               "occurred_at": event["occurred_at"],
                               "channels": {item["channel_id"]: item["status"] for item in delivery}})
        channel_summary = {}
        all_statuses: list[str] = []
        for channel_id, rows in delivery_rows.items():
            statuses = [row["status"] for row in rows]
            all_statuses.extend(statuses)
            latest = rows[0]
            if "pending" in statuses or "sending" in statuses:
                status = "pending"
            elif "failed" in statuses and "sent" in statuses:
                status = "partial"
            elif "sent" in statuses and "disabled" in statuses:
                status = "partial"
            elif "failed" in statuses:
                status = "failed"
            elif all(item == "disabled" for item in statuses):
                status = "disabled"
            else:
                status = "sent"
            channel_summary[channel_id] = {
                "status": status, "attempts": min(sum(row["attempts"] for row in rows), 1000),
                "code": latest["code"], "detail": latest["detail"], "updated": latest["updated"]}
        if not events:
            overall = "none"
        elif not all_statuses:
            overall = "disabled"
        elif "pending" in all_statuses or "sending" in all_statuses:
            overall = "pending"
        elif all(item == "disabled" for item in all_statuses):
            overall = "disabled"
        elif all(item == "sent" for item in all_statuses):
            overall = "sent"
        elif "sent" in all_statuses:
            overall = "partial"
        else:
            overall = "failed"
        return {"events": event_rows, "channels": channel_summary, "overall": overall}

    def start(self) -> None:
        with self._signal_lock:
            if self._thread is not None or self._stop.is_set():
                return
            self._thread = threading.Thread(target=self._worker, name="notification-outbox",
                                            daemon=True)
            self._thread.start()
            self._idle.clear()
            self._wake.set()

    def _worker(self) -> None:
        # Startup recovery and every later publication use the same persistent
        # worker. The signal lock prevents a wake arriving during drain from
        # being cleared before the next pass.
        while True:
            self._wake.wait()
            with self._signal_lock:
                if self._stop.is_set():
                    self._idle.set()
                    return
                self._wake.clear()
            try:
                self._drain_pending()
            except Exception:  # noqa: BLE001 - durable rows remain for a later wake/restart
                pass
            with self._signal_lock:
                if self._stop.is_set():
                    self._idle.set()
                    return
                if not self._wake.is_set():
                    self._idle.set()

    def close(self) -> None:
        with self._signal_lock:
            self._stop.set()
            self._wake.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)


def notification_manager_from_environment(service, environ: dict | None = None) -> NotificationManager:
    return NotificationManager(service, notification_channels_from_environment(environ))
