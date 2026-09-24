"""Dedicated Codex app-server v2 host adapter speaking Runtime Protocol v1.

Run this as a separate host process. The Bridge service contacts only its
token-authenticated HTTP surface; it does not import Codex RPC types.
"""
from __future__ import annotations

import json
import fcntl
import logging
import os
import re
import secrets
import sqlite3
import subprocess
import threading
import time
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any

import uvicorn
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .codex_rpc import CodexRpc, CodexRpcError, sanitize_diagnostic
from .security import redact


_LOG = logging.getLogger("uvicorn.error")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _id(prefix: str) -> str:
    return prefix + secrets.token_hex(12)


def _clean(value: Any, limit: int = 1000) -> str:
    text = value if isinstance(value, str) else "" if value is None else str(value)
    safe, _ = redact(text[:limit])
    return safe


class AdapterFailure(Exception):
    def __init__(self, message: str, status: int = 400, code: str = "invalid_arguments"):
        super().__init__(message)
        self.status = status
        self.code = code


PROFILES = {
    "read-only": {"sandbox": "read-only", "approvalPolicy": "on-request"},
    "workspace-write-reviewed": {"sandbox": "workspace-write", "approvalPolicy": "on-request"},
}
PROFILE_CONTRACT_VERSION = 1
PROFILE_ID_RE = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")


def _validate_profile_config(raw: Any) -> dict:
    if not isinstance(raw, dict) or set(raw) != {
            "sandbox", "approvalPolicy", "approvalsReviewer"}:
        raise AdapterFailure("Codex profile requires sandbox, approvalPolicy and approvalsReviewer")
    if raw["sandbox"] not in ("read-only", "workspace-write", "danger-full-access"):
        raise AdapterFailure("Unsupported Codex sandbox")
    if raw["approvalPolicy"] not in ("on-request", "never"):
        raise AdapterFailure("Unsupported Codex approval policy")
    if raw["approvalsReviewer"] not in ("user", "auto_review"):
        raise AdapterFailure("Unsupported Codex approval reviewer")
    if raw["approvalPolicy"] == "never" and raw["approvalsReviewer"] != "user":
        raise AdapterFailure("A reviewer requires on-request approvals")
    return dict(raw)


def _profile_revision(profile_id: str) -> str:
    return sha256(json.dumps({"version": PROFILE_CONTRACT_VERSION,
                              "policy": PROFILES[profile_id]},
                             sort_keys=True).encode()).hexdigest()


def _custom_profile_revision(config: dict) -> str:
    return sha256(json.dumps({"version": PROFILE_CONTRACT_VERSION,
                              "policy": config}, sort_keys=True).encode()).hexdigest()


def _codex_cli_version(executable: Any) -> str:
    """Read a fallback version from the same Codex executable as app-server."""
    if not isinstance(executable, str) or not executable:
        return ""
    try:
        result = subprocess.run([executable, "--version"], stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, encoding="utf-8",
                                errors="replace", timeout=3, check=False)
    except (OSError, subprocess.SubprocessError):
        return ""
    if result.returncode != 0:
        return ""
    output = result.stdout or result.stderr
    match = re.search(r"(?<![\w.])v?(\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?)\b",
                      output)
    return match.group(1)[:80] if match else ""


class CodexHostAdapter:
    def __init__(self, state: Path, projects_root: Path, *, rpc: CodexRpc | None = None):
        self.state = state.resolve()
        self.state.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.state, 0o700)
        lock_path = self.state / "adapter.lock"
        self._lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(self._lock_fd)
            raise RuntimeError("Another Codex host adapter owns this state") from None
        self._closed = False
        self.projects_root = projects_root.resolve(strict=True)
        if not self.projects_root.is_dir():
            raise ValueError("Projects root must be a directory")
        self.db = sqlite3.connect(self.state / "codex-adapter.sqlite3", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
          PRAGMA journal_mode=WAL;
          PRAGMA foreign_keys=ON;
          CREATE TABLE IF NOT EXISTS conversations (
            id TEXT PRIMARY KEY, thread TEXT NOT NULL UNIQUE,
            workspace TEXT NOT NULL, cwd TEXT NOT NULL,
            profile TEXT NOT NULL, revision TEXT NOT NULL,
            created TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS security_profiles (
            id TEXT PRIMARY KEY, config TEXT NOT NULL, revision TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS runs (
            id TEXT PRIMARY KEY, conversation TEXT NOT NULL REFERENCES conversations(id),
            client_run TEXT, input_hash TEXT,
            turn TEXT UNIQUE, phase TEXT NOT NULL, active_state TEXT,
            outcome TEXT, result TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '',
            created TEXT NOT NULL, updated TEXT NOT NULL);
          CREATE UNIQUE INDEX IF NOT EXISTS ux_active_conversation
            ON runs(conversation) WHERE phase IN ('starting','active');
          CREATE TABLE IF NOT EXISTS activities (
            id TEXT PRIMARY KEY, run TEXT NOT NULL REFERENCES runs(id),
            native_id TEXT NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL,
            summary TEXT NOT NULL, result TEXT NOT NULL DEFAULT '{}',
            created TEXT NOT NULL, updated TEXT NOT NULL,
            UNIQUE(run,native_id));
        """)
        columns = {row["name"] for row in self.db.execute("PRAGMA table_info(runs)")}
        if "client_run" not in columns:
            self.db.execute("ALTER TABLE runs ADD COLUMN client_run TEXT")
        if "input_hash" not in columns:
            self.db.execute("ALTER TABLE runs ADD COLUMN input_hash TEXT")
        self.db.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_client_run "
                        "ON runs(conversation,client_run) WHERE client_run IS NOT NULL")
        os.chmod(self.state / "codex-adapter.sqlite3", 0o600)
        self.lock = threading.RLock()
        self.condition = threading.Condition(self.lock)
        self.instance_id = _id("codex_instance_")
        self.cursor = 0
        self.events_buffer: list[dict] = []
        self.pending: dict[str, dict] = {}
        self._admission: dict[str, threading.Lock] = {}
        self._early_notifications: list[tuple[str, dict]] = []
        self._early_requests: list[tuple[int | str, str, dict]] = []
        # A pending server callback is process-local. A prior instance cannot
        # prove it still owns that callback after restart.
        with self.lock, self.db:
            self.db.execute(
                "UPDATE runs SET phase='terminal',active_state=NULL,outcome='interrupted',"
                "error='adapter_restarted',updated=? WHERE phase IN ('starting','active')",
                (_now(),))
        self.rpc = rpc or CodexRpc(on_notification=self._notification,
                                   on_request=self._request)
        self._native_cli_version_checked = False
        self._native_cli_version = ""
        if rpc is not None:
            rpc.on_notification = self._notification
            rpc.on_request = self._request

    def _emit(self, event_type: str, conversation: str, run: str = "",
              activity: str = "", interaction: str = "") -> None:
        self.cursor += 1
        self.events_buffer.append({"instanceId": self.instance_id, "cursor": self.cursor,
                                   "type": event_type, "conversationId": conversation,
                                   "runId": run, "activityId": activity,
                                   "interactionId": interaction})
        if len(self.events_buffer) > 1000:
            self.events_buffer = self.events_buffer[-1000:]
        self.condition.notify_all()

    def descriptor(self) -> dict:
        if getattr(self.rpc, "alive", True) is False:
            raise AdapterFailure("Codex app-server is unavailable", 503,
                                 "runtime_unavailable")
        initialized = getattr(self.rpc, "initialize_result", {}) or {}
        agent = initialized.get("userAgent") or ""
        match = re.search(r"Codex [^/\s]+/([0-9][^\s(]+)", agent)
        native_version = (match.group(1) if match else
                          (initialized.get("serverInfo") or {}).get("version") or
                          "")
        if not native_version:
            with self.lock:
                if not self._native_cli_version_checked:
                    process = getattr(self.rpc, "_process", None)
                    command = getattr(process, "args", ())
                    executable = (command[0] if isinstance(command, (list, tuple))
                                  and command else None)
                    self._native_cli_version = _codex_cli_version(executable)
                    self._native_cli_version_checked = True
                native_version = self._native_cli_version or "unknown"
        native_version = native_version[:80]
        return {"protocol": {"major": 1, "minor": 0},
                "runtime": {"id": "codex", "displayName": "Codex",
                            "adapterVersion": "1.0.0", "nativeVersion": native_version,
                            "instanceId": self.instance_id},
                "features": {"models": 1, "conversations": 1, "runs": 1,
                             "activities": 1, "interactions": 1, "events": 1,
                             "steering": 1}}

    def models(self) -> dict:
        result = self.rpc.call("model/list", {"limit": 1000})
        models = []
        for model in result.get("data", [])[:1000]:
            if not isinstance(model, dict) or not isinstance(model.get("id"), str):
                continue
            raw_efforts = model.get("supportedReasoningEfforts") or []
            efforts = []
            if isinstance(raw_efforts, list):
                for item in raw_efforts[:20]:
                    effort = (item if isinstance(item, str) else
                              item.get("reasoningEffort", item.get("effort"))
                              if isinstance(item, dict) else None)
                    if isinstance(effort, str) and effort and len(effort) <= 40:
                        if effort not in efforts:
                            efforts.append(effort)
            default_effort = model.get("defaultReasoningEffort")
            models.append({"selector": model["id"],
                           "displayName": _clean(model.get("displayName") or model["id"], 120),
                           "inputModalities": model.get("inputModalities") or ["text"],
                           "reasoningOptions": efforts,
                           "defaultReasoningEffort": (default_effort
                               if isinstance(default_effort, str)
                               and default_effort in efforts else None),
                           "default": model.get("isDefault") is True})
        return {"models": models}

    def _profile(self, profile_id: str) -> tuple[dict, str]:
        if profile_id in PROFILES:
            return {**PROFILES[profile_id], "approvalsReviewer": "user"}, _profile_revision(profile_id)
        with self.lock:
            row = self.db.execute("SELECT config,revision FROM security_profiles WHERE id=?",
                                  (profile_id,)).fetchone()
        if row is None:
            raise AdapterFailure("Unknown security profile", 409, "profile_mismatch")
        try:
            config = _validate_profile_config(json.loads(row["config"]))
        except (ValueError, TypeError, AdapterFailure):
            raise AdapterFailure("Security profile is invalid", 409, "profile_mismatch") from None
        if _custom_profile_revision(config) != row["revision"]:
            raise AdapterFailure("Security profile changed", 409, "profile_mismatch")
        return config, row["revision"]

    def profiles(self) -> dict:
        rows = [{"id": key, "revision": _profile_revision(key),
                 "config": {**value, "approvalsReviewer": "user"},
                 "mutable": False, "enforcement": ["native-sandbox", "approval-policy"]}
                for key, value in PROFILES.items()]
        with self.lock:
            custom = self.db.execute(
                "SELECT id,config,revision FROM security_profiles ORDER BY id").fetchall()
        rows.extend({"id": row["id"], "revision": row["revision"],
                     "config": json.loads(row["config"]), "mutable": True,
                     "enforcement": ["native-sandbox", "approval-policy"]}
                    for row in custom)
        return {"profiles": rows}

    def save_profile(self, body: dict) -> dict:
        profile_id = body.get("id")
        if (not isinstance(profile_id, str) or not PROFILE_ID_RE.fullmatch(profile_id)
                or profile_id in PROFILES):
            raise AdapterFailure("Choose a new profile ID using lowercase letters, numbers, _ or -")
        config = _validate_profile_config(body.get("config"))
        expected = body.get("expectedRevision")
        if expected is not None and not isinstance(expected, str):
            raise AdapterFailure("Invalid expected revision")
        revision = _custom_profile_revision(config)
        with self.lock, self.db:
            existing = self.db.execute(
                "SELECT revision FROM security_profiles WHERE id=?", (profile_id,)).fetchone()
            if (existing is None and expected is not None) or (existing is not None
                    and existing["revision"] != expected):
                raise AdapterFailure("Security profile changed; reload it", 409, "profile_mismatch")
            if existing is None and self.db.execute(
                    "SELECT COUNT(*) FROM security_profiles").fetchone()[0] >= 98:
                raise AdapterFailure("Too many security profiles")
            if existing is not None and self.db.execute(
                    "SELECT 1 FROM conversations c JOIN runs r ON r.conversation=c.id "
                    "WHERE c.profile=? AND r.phase IN ('starting','active') LIMIT 1",
                    (profile_id,)).fetchone():
                raise AdapterFailure("Profile has an active run", 409, "conflict")
            self.db.execute("INSERT INTO security_profiles(id,config,revision) VALUES(?,?,?) "
                            "ON CONFLICT(id) DO UPDATE SET config=excluded.config,"
                            "revision=excluded.revision",
                            (profile_id, json.dumps(config, sort_keys=True), revision))
        return {"id": profile_id, "revision": revision, "config": config,
                "mutable": True}

    def delete_profile(self, profile_id: str) -> dict:
        if profile_id in PROFILES:
            raise AdapterFailure("Built-in profiles cannot be deleted", 409, "conflict")
        with self.lock, self.db:
            active = self.db.execute(
                "SELECT 1 FROM conversations c JOIN runs r ON r.conversation=c.id "
                "WHERE c.profile=? AND r.phase IN ('starting','active') LIMIT 1",
                (profile_id,)).fetchone()
            if active:
                raise AdapterFailure("Profile has an active run", 409, "conflict")
            deleted = self.db.execute("DELETE FROM security_profiles WHERE id=?",
                                      (profile_id,)).rowcount
        if not deleted:
            raise AdapterFailure("Security profile not found", 404, "not_found")
        return {"deleted": profile_id}

    def _cwd(self, requested: Any) -> str:
        if not isinstance(requested, str) or not requested:
            raise AdapterFailure("Workspace directory is required")
        try:
            cwd = Path(requested).resolve(strict=True)
            if not cwd.is_dir() or not cwd.is_relative_to(self.projects_root):
                raise ValueError()
            return str(cwd)
        except (OSError, RuntimeError, ValueError):
            raise AdapterFailure("Workspace directory is outside the configured projects root") from None

    def _conversation(self, conversation_id: str) -> dict:
        with self.lock:
            row = self.db.execute("SELECT * FROM conversations WHERE id=?",
                                  (conversation_id,)).fetchone()
        if row is None:
            raise AdapterFailure("Conversation not found", 404, "not_found")
        return dict(row)

    def create_conversation(self, body: dict) -> dict:
        workspace = body.get("workspaceId")
        if not isinstance(workspace, str) or not workspace or len(workspace) > 100:
            raise AdapterFailure("Invalid workspace id")
        cwd = self._cwd(body.get("directory"))
        profile = body.get("securityProfile") or {}
        profile_id = profile.get("id") if isinstance(profile, dict) else None
        if not isinstance(profile_id, str):
            raise AdapterFailure("Unknown or changed security profile", 409, "profile_mismatch")
        options, revision = self._profile(profile_id)
        if profile.get("revision") != revision:
            raise AdapterFailure("Unknown or changed security profile", 409, "profile_mismatch")
        try:
            native = self.rpc.call("thread/start", {
                "cwd": cwd, "sandbox": options["sandbox"],
                "approvalPolicy": options["approvalPolicy"],
                "approvalsReviewer": options["approvalsReviewer"], "ephemeral": False,
            }, timeout=30)
        except CodexRpcError as exc:
            detail = sanitize_diagnostic(str(exc), limit=300) or "native request failed"
            stderr_summary = getattr(self.rpc, "stderr_summary", None)
            stderr = sanitize_diagnostic(stderr_summary(), limit=2048) if callable(
                stderr_summary) else ""
            summary = f"Codex thread/start failed: {detail}"
            if stderr:
                summary += f"; app-server stderr: {stderr}"
            _LOG.warning("%s", summary[:2600])
            raise AdapterFailure("Codex thread/start failed", 502,
                                 "runtime_unavailable") from None
        thread = (native.get("thread") or {}).get("id")
        observed_cwd = native.get("cwd")
        if not isinstance(thread, str) or not thread or observed_cwd != cwd:
            raise AdapterFailure("Codex thread binding was not confirmed", 502,
                                 "binding_mismatch")
        conversation_id = _id("conv_")
        with self.lock, self.db:
            self.db.execute(
                "INSERT INTO conversations VALUES(?,?,?,?,?,?,?)",
                (conversation_id, thread, workspace, cwd, profile_id,
                 revision, _now()))
            self._emit("conversation.created", conversation_id)
        return {"id": conversation_id, "runtime": "codex", "nativeId": thread,
                "workspaceId": workspace,
                "securityProfile": {"id": profile_id,
                                    "revision": revision},
                "status": "idle"}

    def conversation(self, conversation_id: str) -> dict:
        owned = self._conversation(conversation_id)
        options, revision = self._profile(owned["profile"])
        if owned["revision"] != revision:
            raise AdapterFailure("Conversation security profile changed", 409,
                                 "profile_mismatch")
        try:
            native = self.rpc.call("thread/read", {"threadId": owned["thread"],
                                                   "includeTurns": False})
            status = ((native.get("thread") or {}).get("status") or {}).get("type")
        except CodexRpcError as exc:
            raise AdapterFailure(_clean(str(exc), 300), 502, "runtime_unavailable") from None
        if status == "notLoaded":
            # Resume only a thread recorded as created by this adapter.
            # The exact immutable profile is applied again; no arbitrary
            # Desktop/TUI thread ID can enter through this path.
            try:
                resumed = self.rpc.call("thread/resume", {
                    "threadId": owned["thread"], "cwd": owned["cwd"],
                    "sandbox": options["sandbox"],
                    "approvalPolicy": options["approvalPolicy"],
                    "approvalsReviewer": options["approvalsReviewer"], "excludeTurns": True,
                }, timeout=30)
            except CodexRpcError:
                status = "unavailable"
            else:
                if ((resumed.get("thread") or {}).get("id") != owned["thread"]
                        or resumed.get("cwd") != owned["cwd"]):
                    raise AdapterFailure("Resumed thread binding changed", 502,
                                         "binding_mismatch")
                status = ((resumed.get("thread") or {}).get("status") or {}).get("type")
        elif (native.get("thread") or {}).get("id") != owned["thread"]:
            raise AdapterFailure("Thread identity changed", 502, "binding_mismatch")
        return {"id": owned["id"], "runtime": "codex", "nativeId": owned["thread"],
                "workspaceId": owned["workspace"], "securityProfile": {
                    "id": owned["profile"], "revision": owned["revision"]},
                "status": status}

    def _run(self, run_id: str) -> dict:
        with self.lock:
            row = self.db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise AdapterFailure("Run not found", 404, "not_found")
        return dict(row)

    def _run_public(self, run: dict) -> dict:
        return {"id": run["id"], "conversationId": run["conversation"],
                "clientRunId": run.get("client_run"),
                "nativeId": run["turn"], "phase": run["phase"],
                "activeState": run["active_state"], "outcome": run["outcome"],
                "result": _clean(run["result"], 20000),
                "error": _clean(run["error"], 300),
                "createdAt": run["created"], "updatedAt": run["updated"]}

    def _input(self, items: Any) -> list[dict]:
        if not isinstance(items, list) or not items or len(items) > 10:
            raise AdapterFailure("Input requires 1..10 typed items")
        mapped = []
        for item in items:
            if not isinstance(item, dict) or item.get("type") != "text":
                raise AdapterFailure("Only text input is supported by this adapter version",
                                     501, "unsupported_input")
            text = item.get("text")
            if not isinstance(text, str) or not text or len(text) > 60000:
                raise AdapterFailure("Invalid text input")
            mapped.append({"type": "text", "text": text})
        return mapped

    def start_run(self, conversation_id: str, body: dict) -> dict:
        input_items = self._input(body.get("input"))
        client_run_id = body.get("clientRunId")
        if client_run_id is not None and (not isinstance(client_run_id, str)
                or not client_run_id or len(client_run_id) > 200):
            raise AdapterFailure("Invalid client run id")
        input_hash = sha256(json.dumps({"input": input_items, "model": body.get("model"),
            "reasoning": body.get("reasoning")}, sort_keys=True).encode()).hexdigest()
        params: dict = {"input": input_items}
        model = body.get("model")
        if model is not None:
            if not isinstance(model, str) or not model or len(model) > 260:
                raise AdapterFailure("Invalid model selector")
            params["model"] = model
        effort = body.get("reasoning")
        if effort is not None:
            if not isinstance(effort, str) or effort not in (
                    "minimal", "low", "medium", "high", "xhigh", "max", "ultra"):
                raise AdapterFailure("Invalid reasoning effort")
            if model is not None:
                model_row = next((item for item in self.models()["models"]
                                  if item["selector"] == model), None)
                if model_row is None or effort not in model_row["reasoningOptions"]:
                    raise AdapterFailure("Reasoning effort is not supported by this model",
                                         400, "unsupported_reasoning")
            params["effort"] = effort
        with self.lock:
            owned = self._conversation(conversation_id)
            admission = self._admission.setdefault(conversation_id, threading.Lock())
        # This lock serializes only admissions to one conversation. Native RPC
        # calls must run outside self.lock so notifications can be consumed.
        with admission:
            with self.lock:
                if client_run_id:
                    previous = self.db.execute(
                        "SELECT * FROM runs WHERE conversation=? AND client_run=?",
                        (conversation_id, client_run_id)).fetchone()
                    if previous is not None:
                        if previous["input_hash"] != input_hash:
                            raise AdapterFailure("Client run id has different input", 409,
                                                 "idempotency_conflict")
                        return self._run_public(dict(previous))
                busy = self.db.execute(
                    "SELECT id FROM runs WHERE conversation=? AND phase IN ('starting','active')",
                    (conversation_id,)).fetchone()
                if busy:
                    raise AdapterFailure("Conversation is busy", 409, "conversation_busy")
            try:
                native = self.rpc.call("thread/read", {"threadId": owned["thread"],
                                                       "includeTurns": False})
            except CodexRpcError as exc:
                raise AdapterFailure(_clean(str(exc), 300), 502, "runtime_unavailable") from None
            if ((native.get("thread") or {}).get("status") or {}).get("type") != "idle":
                raise AdapterFailure("Conversation is not idle", 409, "conversation_busy")
            run_id = _id("run_")
            now = _now()
            with self.lock, self.db:
                if self._profile(owned["profile"])[1] != owned["revision"]:
                    raise AdapterFailure("Conversation security profile changed", 409,
                                         "profile_mismatch")
                self.db.execute(
                    "INSERT INTO runs(id,conversation,client_run,input_hash,turn,phase,"
                    "active_state,outcome,result,error,created,updated) "
                    "VALUES(?,?,?,?,NULL,'starting',NULL,NULL,'','',?,?)",
                    (run_id, conversation_id, client_run_id, input_hash, now, now))
            params["threadId"] = owned["thread"]
            try:
                result = self.rpc.call("turn/start", params, timeout=45)
                turn = (result.get("turn") or {}).get("id")
                if not isinstance(turn, str) or not turn:
                    raise CodexRpcError("Turn identity is missing")
            except CodexRpcError as exc:
                with self.lock, self.db:
                    self.db.execute(
                        "UPDATE runs SET phase='terminal',outcome='failed',error=?,updated=? WHERE id=?",
                        (_clean(str(exc), 300), _now(), run_id))
                    orphan_requests = [entry for entry in self._early_requests
                                       if entry[2].get("threadId") == owned["thread"]]
                    self._early_requests = [entry for entry in self._early_requests
                                            if entry not in orphan_requests]
                    self._early_notifications = [entry for entry in self._early_notifications
                                                 if entry[1].get("threadId") != owned["thread"]]
                for request_id, _, _ in orphan_requests:
                    try:
                        self.rpc.respond(request_id, error={"code": -32602,
                            "message": "Native run start was not confirmed"})
                    except CodexRpcError:
                        pass
                raise AdapterFailure(_clean(str(exc), 300), 502, "runtime_unavailable") from None
            with self.lock, self.db:
                self.db.execute(
                    "UPDATE runs SET turn=?,phase='active',active_state='running',updated=? WHERE id=?",
                    (turn, _now(), run_id))
                self._emit("run.started", conversation_id, run_id)
                early_notifications = [entry for entry in self._early_notifications
                                       if entry[1].get("threadId") == owned["thread"]
                                       and ((entry[1].get("turn") or {}).get("id")
                                            or entry[1].get("turnId")) == turn]
                self._early_notifications = [entry for entry in self._early_notifications
                                             if entry not in early_notifications]
                early_requests = [entry for entry in self._early_requests
                                  if entry[2].get("threadId") == owned["thread"]
                                  and entry[2].get("turnId") == turn]
                self._early_requests = [entry for entry in self._early_requests
                                        if entry not in early_requests]
            for method, event in early_notifications:
                self._notification(method, event)
            for request_id, method, event in early_requests:
                self._request(request_id, method, event)
            return self._run_public(self._run(run_id))

    def find_run(self, conversation_id: str, client_run_id: str) -> dict:
        self._conversation(conversation_id)
        with self.lock:
            row = self.db.execute(
                "SELECT id FROM runs WHERE conversation=? AND client_run=?",
                (conversation_id, client_run_id)).fetchone()
        if row is None:
            raise AdapterFailure("Run not found", 404, "not_found")
        return self.run(row["id"])

    def run(self, run_id: str) -> dict:
        run = self._run(run_id)
        if run["phase"] == "active" and run["turn"]:
            if getattr(self.rpc, "alive", True) is False:
                with self.lock, self.db:
                    self.db.execute(
                        "UPDATE runs SET phase='terminal',active_state=NULL,"
                        "outcome='interrupted',error='native_server_exited',updated=? WHERE id=?",
                        (_now(), run_id))
            else:
                owned = self._conversation(run["conversation"])
                try:
                    snapshot = self.rpc.call("thread/turns/list", {
                        "threadId": owned["thread"], "limit": 20,
                        "sortDirection": "desc", "itemsView": "full"}, timeout=15)
                except CodexRpcError:
                    pass
                else:
                    found = False
                    for turn in snapshot.get("data", []):
                        if not isinstance(turn, dict) or turn.get("id") != run["turn"]:
                            continue
                        found = True
                        for item in turn.get("items", [])[:1000]:
                            if isinstance(item, dict):
                                status = item.get("status")
                                event = ("item/completed" if status in
                                         ("completed", "failed", "declined")
                                         else "item/started")
                                self._notification(event, {
                                    "threadId": owned["thread"], "turnId": run["turn"],
                                    "item": item})
                        if turn.get("status") in ("completed", "interrupted", "failed"):
                            self._notification("turn/completed", {
                                "threadId": owned["thread"], "turn": turn})
                        break
                    if not found:
                        try:
                            thread_state = self.rpc.call("thread/read", {
                                "threadId": owned["thread"], "includeTurns": False},
                                timeout=10)
                        except CodexRpcError:
                            thread_state = {}
                        status = ((thread_state.get("thread") or {}).get("status") or {}).get("type")
                        age = time.time() - datetime.fromisoformat(run["created"]).timestamp()
                        if status == "idle" and age > 30:
                            with self.lock, self.db:
                                self.db.execute(
                                    "UPDATE runs SET phase='terminal',active_state=NULL,"
                                    "outcome='orphaned',error='native_turn_missing',updated=? "
                                    "WHERE id=? AND phase='active'",
                                    (_now(), run_id))
        return self._run_public(self._run(run_id))

    def cancel(self, run_id: str) -> dict:
        run = self._run(run_id)
        if run["phase"] == "terminal":
            return self._run_public(run)
        owned = self._conversation(run["conversation"])
        if not run["turn"]:
            raise AdapterFailure("Native run identity is not confirmed", 409, "run_starting")
        try:
            self.rpc.call("turn/interrupt", {"threadId": owned["thread"],
                                             "turnId": run["turn"]})
        except CodexRpcError as exc:
            raise AdapterFailure(_clean(str(exc), 300), 502, "runtime_unavailable") from None
        # The completion notification decides the terminal outcome. A request
        # acknowledgement is not evidence that the turn has stopped.
        return self._run_public(self._run(run_id))

    def steer(self, run_id: str, body: dict) -> dict:
        run = self._run(run_id)
        if run["phase"] != "active" or body.get("expectedRunId") != run_id:
            raise AdapterFailure("Expected run is not active", 409, "run_mismatch")
        owned = self._conversation(run["conversation"])
        try:
            self.rpc.call("turn/steer", {"threadId": owned["thread"],
                                         "expectedTurnId": run["turn"],
                                         "input": self._input(body.get("input"))})
        except CodexRpcError as exc:
            raise AdapterFailure(_clean(str(exc), 300), 409, "steer_rejected") from None
        return self._run_public(self._run(run_id))

    def _notification(self, method: str, params: dict) -> None:
        if not isinstance(params, dict):
            return
        thread = params.get("threadId")
        turn = (params.get("turn") or {}).get("id") or params.get("turnId")
        if not isinstance(thread, str) or not isinstance(turn, str):
            return
        with self.lock:
            row = self.db.execute(
                "SELECT runs.*, conversations.thread FROM runs JOIN conversations "
                "ON runs.conversation=conversations.id WHERE conversations.thread=? "
                "AND runs.turn=?", (thread, turn)).fetchone()
            if row is None:
                starting = self.db.execute(
                    "SELECT 1 FROM runs JOIN conversations ON runs.conversation=conversations.id "
                    "WHERE conversations.thread=? AND runs.phase='starting'",
                    (thread,)).fetchone()
                if starting and len(self._early_notifications) < 100:
                    self._early_notifications.append((method, params))
                return
            run = dict(row)
            if method == "turn/completed":
                native_status = (params.get("turn") or {}).get("status")
                outcome = {"completed": "succeeded", "interrupted": "interrupted",
                           "failed": "failed"}.get(native_status, "failed")
                native_error = (params.get("turn") or {}).get("error")
                if isinstance(native_error, dict):
                    native_error = native_error.get("message")
                with self.db:
                    self.db.execute(
                        "UPDATE runs SET phase='terminal',active_state=NULL,outcome=?,"
                        "error=?,updated=? "
                        "WHERE id=? AND phase='active'",
                        (outcome, _clean(native_error, 300), _now(), run["id"]))
                for interaction_id, entry in list(self.pending.items()):
                    if entry["public"]["runId"] == run["id"]:
                        self.pending.pop(interaction_id, None)
                self._emit("run.completed", run["conversation"], run["id"])
                return
            if method not in ("item/started", "item/completed"):
                return
            item = params.get("item") or {}
            if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                return
            kind = {"commandExecution": "command", "fileChange": "file_change",
                    "mcpToolCall": "tool_call", "webSearch": "search",
                    "collabToolCall": "subagent", "agentMessage": "other"}.get(
                        item.get("type"), "other")
            native_id = item["id"][:200]
            summary = _clean(item.get("command") or item.get("text") or item.get("type"), 500)
            native_status = item.get("status")
            status = (native_status if native_status in ("completed", "failed", "declined")
                      else "completed" if method == "item/completed" else "running")
            result_summary = {}
            if method == "item/completed":
                if isinstance(item.get("exitCode"), int):
                    result_summary["exitCode"] = item["exitCode"]
                if isinstance(item.get("durationMs"), int):
                    result_summary["durationMs"] = item["durationMs"]
                if isinstance(item.get("aggregatedOutput"), str):
                    result_summary["outputPreview"] = _clean(item["aggregatedOutput"], 1000)
            activity_id = "act_" + sha256((run["id"] + ":" + native_id).encode()).hexdigest()[:24]
            with self.db:
                self.db.execute(
                    "INSERT INTO activities(id,run,native_id,kind,status,summary,result,created,updated) "
                    "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(run,native_id) DO UPDATE SET "
                    "status=CASE WHEN activities.status IN ('completed','failed','declined') "
                    "THEN activities.status ELSE excluded.status END,"
                    "summary=excluded.summary,"
                    "result=CASE WHEN activities.status IN ('completed','failed','declined') "
                    "AND excluded.status='running' THEN activities.result "
                    "ELSE excluded.result END,"
                    "updated=excluded.updated",
                    (activity_id, run["id"], native_id, kind, status, summary,
                     json.dumps(result_summary), _now(), _now()))
                if item.get("type") == "agentMessage" and method == "item/completed":
                    self.db.execute("UPDATE runs SET result=?,updated=? WHERE id=?",
                                    (_clean(item.get("text"), 20000), _now(), run["id"]))
            self._emit("activity." + status, run["conversation"], run["id"], activity_id)

    def _request(self, request_id: int | str, method: str, params: dict) -> None:
        if not isinstance(params, dict):
            params = {}
        if method not in {
            "item/commandExecution/requestApproval", "item/fileChange/requestApproval",
            "item/permissions/requestApproval", "item/tool/requestUserInput",
        }:
            self.rpc.respond(request_id, error={"code": -32601,
                                                "message": "Unsupported adapter interaction"})
            return
        thread = params.get("threadId")
        turn = params.get("turnId")
        with self.lock:
            row = self.db.execute(
                "SELECT runs.id,runs.conversation FROM runs JOIN conversations "
                "ON runs.conversation=conversations.id WHERE conversations.thread=? "
                "AND runs.turn=? AND runs.phase='active'", (thread, turn)).fetchone()
            if row is None:
                starting = self.db.execute(
                    "SELECT 1 FROM runs JOIN conversations ON runs.conversation=conversations.id "
                    "WHERE conversations.thread=? AND runs.phase='starting'",
                    (thread,)).fetchone()
                if starting and len(self._early_requests) < 100:
                    self._early_requests.append((request_id, method, params))
                    return
                self.rpc.respond(request_id, error={"code": -32602,
                                                    "message": "Unowned native interaction"})
                return
            run_id = row["id"]
            owned = self._conversation(row["conversation"])
            if self._profile(owned["profile"])[0]["sandbox"] == "read-only" and method.endswith("requestApproval"):
                if method == "item/permissions/requestApproval":
                    self.rpc.respond(request_id, {"permissions": {}, "scope": "turn"})
                else:
                    available = params.get("availableDecisions")
                    if not isinstance(available, list):
                        available = []
                    deny = next((choice for choice in ("decline", "cancel")
                                 if choice in available), None)
                    if deny is None:
                        self.rpc.respond(request_id, error={"code": -32602,
                            "message": "Read-only profile cannot approve this request"})
                    else:
                        self.rpc.respond(request_id, {"decision": deny})
                return
            interaction_id = _id("int_")
            choices = []
            responses = {}
            if method.endswith("requestApproval") and method != "item/permissions/requestApproval":
                decisions = params.get("availableDecisions")
                if not isinstance(decisions, list):
                    decisions = ["accept", "decline", "cancel"]
                for decision in decisions[:20]:
                    # Session-wide grants and policy amendments outlive this
                    # exact request, so this profile exposes only one-shot
                    # approval or denial.
                    if decision not in ("accept", "decline", "cancel"):
                        continue
                    choice_id = _id("choice_")
                    semantic = "approve" if decision == "accept" else "deny" if decision in ("decline", "cancel") else "other"
                    choices.append({"id": choice_id, "label": _clean(decision, 120),
                                    "semantic": semantic})
                    responses[choice_id] = {"decision": decision}
                if not choices:
                    self.rpc.respond(request_id, error={"code": -32602,
                        "message": "No bounded approval decision is available"})
                    return
            elif method == "item/permissions/requestApproval":
                requested = params.get("permissions") or {}
                for label, response in (
                    ("Grant requested permissions for this turn",
                     {"permissions": requested, "scope": "turn"}),
                    ("Deny", {"permissions": {}, "scope": "turn"}),
                ):
                    choice_id = _id("choice_")
                    choices.append({"id": choice_id, "label": label,
                                    "semantic": "approve" if requested and response["permissions"] else "deny"})
                    responses[choice_id] = response
            else:
                # Form response validation is intentionally handled on resolve.
                pass
            raw_fields = params.get("questions") if not choices else None
            fields = None
            if isinstance(raw_fields, list):
                fields = []
                for question in raw_fields[:20]:
                    if not isinstance(question, dict):
                        continue
                    options = question.get("options")
                    if not isinstance(options, list):
                        options = []
                    fields.append({"id": _clean(question.get("id"), 100),
                                   "header": _clean(question.get("header"), 100),
                                   "question": _clean(question.get("question"), 1000),
                                   "options": [{"label": _clean(option.get("label"), 100),
                                                "description": _clean(option.get("description"), 300)}
                                               for option in options[:20]
                                               if isinstance(option, dict)]})
            requested = params.get("permissions") if method == "item/permissions/requestApproval" else None
            if isinstance(requested, dict):
                requested = {str(key)[:100]: _clean(value, 300)
                             for key, value in list(requested.items())[:20]}
            else:
                requested = None
            interaction = {"id": interaction_id, "runId": run_id,
                           "kind": "choice" if choices else "form",
                           "state": "pending", "title": _clean(params.get("reason") or method, 200),
                           "resource": _clean(params.get("command") or params.get("cwd") or
                                              params.get("path"), 1000),
                           "requested": requested,
                           "choices": choices, "fields": fields,
                           "nativeRequestId": str(request_id),
                           "requestMethod": method}
            self.pending[interaction_id] = {"public": interaction,
                                            "rpc_id": request_id,
                                            "responses": responses,
                                            "params": params}
            with self.db:
                self.db.execute(
                    "UPDATE runs SET active_state='waiting_interaction',updated=? WHERE id=?",
                    (_now(), run_id))
            self._emit("interaction.pending", row["conversation"], run_id,
                       interaction=interaction_id)

    def interactions(self, run_id: str) -> dict:
        self._run(run_id)
        with self.lock:
            return {"interactions": [entry["public"] for entry in self.pending.values()
                                     if entry["public"]["runId"] == run_id]}

    def resolve(self, interaction_id: str, body: dict) -> dict:
        with self.lock:
            entry = self.pending.get(interaction_id)
            if entry is None:
                raise AdapterFailure("Interaction is stale or unknown", 409,
                                     "interaction_stale")
            interaction = entry["public"]
            run = self._run(interaction["runId"])
            if run["phase"] != "active":
                raise AdapterFailure("Interaction run is no longer active", 409,
                                     "interaction_stale")
            if interaction["kind"] == "choice":
                response = entry["responses"].get(body.get("choiceId"))
                if response is None:
                    raise AdapterFailure("Choice is not available")
            else:
                answers = body.get("answers")
                if not isinstance(answers, dict) or len(json.dumps(answers)) > 16000:
                    raise AdapterFailure("Invalid form response")
                method = interaction["requestMethod"]
                if method == "item/tool/requestUserInput":
                    question_ids = {q.get("id") for q in entry["params"].get("questions", [])
                                    if isinstance(q, dict)}
                    if set(answers) != question_ids:
                        raise AdapterFailure("Form answers do not match the request")
                    if any(not isinstance(answer, dict)
                           or not isinstance(answer.get("answers"), list)
                           or len(answer["answers"]) > 20
                           or any(not isinstance(item, str) or len(item) > 2000
                                  for item in answer["answers"])
                           for answer in answers.values()):
                        raise AdapterFailure("Invalid form answers")
                    response = {"answers": answers}
                else:
                    raise AdapterFailure("Form kind is unsupported", 501, "unsupported_interaction")
            try:
                self.rpc.respond(entry["rpc_id"], response)
            except CodexRpcError as exc:
                raise AdapterFailure(_clean(str(exc), 300), 502, "runtime_unavailable") from None
            self.pending.pop(interaction_id, None)
            remaining = any(p["public"]["runId"] == run["id"] for p in self.pending.values())
            with self.db:
                self.db.execute("UPDATE runs SET active_state=?,updated=? WHERE id=?",
                                ("waiting_interaction" if remaining else "running", _now(), run["id"]))
            self._emit("interaction.resolved", run["conversation"], run["id"],
                       interaction=interaction_id)
            return {"id": interaction_id, "state": "resolved", "runId": run["id"]}

    def activities(self, run_id: str) -> dict:
        self._run(run_id)
        with self.lock:
            rows = self.db.execute(
                "SELECT * FROM activities WHERE run=? ORDER BY created,id LIMIT 1000",
                (run_id,)).fetchall()
        return {"activities": [self._activity_public(dict(row)) for row in rows]}

    def _activity_public(self, row: dict) -> dict:
        return {"id": row["id"], "runId": row["run"], "nativeId": row["native_id"],
                "kind": row["kind"], "status": row["status"],
                "input": {"summary": row["summary"]},
                "result": json.loads(row["result"]),
                "createdAt": row["created"], "updatedAt": row["updated"]}

    def activity(self, activity_id: str) -> dict:
        with self.lock:
            row = self.db.execute("SELECT * FROM activities WHERE id=?",
                                  (activity_id,)).fetchone()
        if row is None:
            raise AdapterFailure("Activity not found", 404, "not_found")
        return self._activity_public(dict(row))

    def events(self, after: int, wait_ms: int) -> dict:
        wait_seconds = min(max(wait_ms, 0), 25000) / 1000
        with self.condition:
            if after >= self.cursor and wait_seconds:
                self.condition.wait(wait_seconds)
            events = [event for event in self.events_buffer if event["cursor"] > after]
            return {"instanceId": self.instance_id, "cursor": self.cursor,
                    "events": events[:100]}

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.rpc.close()
        self.db.close()
        fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
        os.close(self._lock_fd)


def make_app(adapter: CodexHostAdapter, token: str) -> Starlette:
    if not token:
        raise ValueError("Runtime token is required")

    async def endpoint(request: Request) -> JSONResponse:
        provided = request.headers.get("x-runtime-token") or ""
        if not secrets.compare_digest(token, provided):
            return JSONResponse({"error": "Unauthorized", "code": "unauthorized"}, 401)
        path = request.url.path
        parts = path.split("/")[1:]
        try:
            body = {}
            if request.method == "POST":
                raw = await request.body()
                if len(raw) > 512 * 1024:
                    raise AdapterFailure("Request is too large", 413, "too_large")
                body = json.loads(raw or b"{}")
                if not isinstance(body, dict):
                    raise AdapterFailure("Invalid request body")
            if path == "/v1/descriptor" and request.method == "GET":
                result = await run_in_threadpool(adapter.descriptor)
            elif path == "/v1/models" and request.method == "GET":
                result = await run_in_threadpool(adapter.models)
            elif path == "/v1/profiles" and request.method == "GET":
                result = await run_in_threadpool(adapter.profiles)
            elif path == "/v1/profiles" and request.method == "POST":
                result = await run_in_threadpool(adapter.save_profile, body)
            elif len(parts) == 3 and parts[:2] == ["v1", "profiles"] and request.method == "DELETE":
                result = await run_in_threadpool(adapter.delete_profile, parts[2])
            elif path == "/v1/conversations" and request.method == "POST":
                result = await run_in_threadpool(adapter.create_conversation, body)
            elif len(parts) == 3 and parts[:2] == ["v1", "conversations"] and request.method == "GET":
                result = await run_in_threadpool(adapter.conversation, parts[2])
            elif len(parts) == 4 and parts[:2] == ["v1", "conversations"] and parts[3] == "runs" and request.method == "POST":
                result = await run_in_threadpool(adapter.start_run, parts[2], body)
            elif len(parts) == 5 and parts[:2] == ["v1", "conversations"] and parts[3] == "runs" and request.method == "GET":
                result = await run_in_threadpool(adapter.find_run, parts[2], parts[4])
            elif len(parts) == 3 and parts[:2] == ["v1", "runs"] and request.method == "GET":
                result = await run_in_threadpool(adapter.run, parts[2])
            elif len(parts) == 4 and parts[:2] == ["v1", "runs"] and parts[3] == "cancel" and request.method == "POST":
                result = await run_in_threadpool(adapter.cancel, parts[2])
            elif len(parts) == 4 and parts[:2] == ["v1", "runs"] and parts[3] == "steer" and request.method == "POST":
                result = await run_in_threadpool(adapter.steer, parts[2], body)
            elif len(parts) == 4 and parts[:2] == ["v1", "runs"] and parts[3] == "interactions" and request.method == "GET":
                result = await run_in_threadpool(adapter.interactions, parts[2])
            elif len(parts) == 4 and parts[:2] == ["v1", "runs"] and parts[3] == "activities" and request.method == "GET":
                result = await run_in_threadpool(adapter.activities, parts[2])
            elif len(parts) == 4 and parts[:2] == ["v1", "interactions"] and parts[3] == "resolve" and request.method == "POST":
                result = await run_in_threadpool(adapter.resolve, parts[2], body)
            elif len(parts) == 3 and parts[:2] == ["v1", "activities"] and request.method == "GET":
                result = await run_in_threadpool(adapter.activity, parts[2])
            elif path == "/v1/events" and request.method == "GET":
                result = await run_in_threadpool(adapter.events,
                    int(request.query_params.get("after", "0")),
                    int(request.query_params.get("waitMs", "0")))
            else:
                raise AdapterFailure("Unknown route", 404, "not_found")
            return JSONResponse(result, headers={"Cache-Control": "no-store"})
        except AdapterFailure as exc:
            return JSONResponse({"error": str(exc), "code": exc.code}, exc.status)
        except (ValueError, TypeError):
            return JSONResponse({"error": "Invalid request", "code": "invalid_arguments"}, 400)
        except CodexRpcError:
            return JSONResponse({"error": "Codex runtime unavailable", "code": "runtime_unavailable"}, 502)

    return Starlette(routes=[Route("/{path:path}", endpoint,
                                   methods=["GET", "POST", "DELETE"])])


def main() -> None:
    state = os.environ.get("WB_CODEX_ADAPTER_STATE")
    root = os.environ.get("WB_CODEX_PROJECTS_ROOT")
    token = os.environ.get("WB_RUNTIME_TOKEN")
    if not state or not root or not token:
        raise SystemExit("WB_CODEX_ADAPTER_STATE, WB_CODEX_PROJECTS_ROOT, and WB_RUNTIME_TOKEN are required")
    adapter = CodexHostAdapter(Path(state), Path(root))
    try:
        app = make_app(adapter, token)
        uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("WB_CODEX_ADAPTER_PORT", "8772")))
    finally:
        adapter.close()


if __name__ == "__main__":
    main()
