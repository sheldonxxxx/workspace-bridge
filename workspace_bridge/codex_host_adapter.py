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
from .login_path import runtime_env_with_login_path, safe_summary
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
    "read-only": {"permissions": ":read-only", "approvalPolicy": "on-request",
                  "approvalsReviewer": "user"},
    "workspace-write-reviewed": {"permissions": ":workspace",
                                 "approvalPolicy": "on-request",
                                 "approvalsReviewer": "user"},
}
PROFILE_CONTRACT_VERSION = 2
PROFILE_ID_RE = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
_LEGACY_SANDBOX_MAP = {
    "read-only": ":read-only",
    "workspace-write": ":workspace",
    "danger-full-access": ":danger-full-access",
}
_APPROVAL_POLICIES = frozenset({"on-request", "never"})
_APPROVAL_REVIEWERS = frozenset({"user", "auto_review", "guardian_subagent"})
_PROFILE_CACHE_SECONDS = 2.0
_PROFILE_CACHE_LIMIT = 128
_MAX_SECURITY_JSON_BYTES = 2 * 1024 * 1024
_SECURITY_CONFIG_KEYS = frozenset({
    "default_permissions", "permissions", "approval_policy", "approvals_reviewer",
    "sandbox_mode", "sandbox_workspace_write", "trusted_projects",
})
_SECURITY_REQUIREMENT_KEYS = frozenset({
    "allowedPermissionProfiles", "defaultPermissions", "allowedApprovalPolicies",
    "allowedApprovalsReviewers", "allowedSandboxModes",
})


def _normalize_profile_config(raw: Any, *, allow_legacy: bool = False) -> dict:
    """Validate Codex's small Bridge wrapper without mirroring native config."""
    if not isinstance(raw, dict):
        raise AdapterFailure("Codex profile must be an object")
    if set(raw) == {"sandbox", "approvalPolicy", "approvalsReviewer"}:
        if not allow_legacy:
            raise AdapterFailure("New Codex profiles must use permissions")
        legacy_sandbox = raw.get("sandbox")
        permission_id = (_LEGACY_SANDBOX_MAP.get(legacy_sandbox)
                         if isinstance(legacy_sandbox, str) else None)
        if permission_id is None:
            raise AdapterFailure("Unsupported legacy Codex sandbox")
        raw = {"permissions": permission_id,
               "approvalPolicy": raw.get("approvalPolicy"),
               "approvalsReviewer": raw.get("approvalsReviewer")}
    if set(raw) != {"permissions", "approvalPolicy", "approvalsReviewer"}:
        raise AdapterFailure("Codex profile requires permissions, approvalPolicy and approvalsReviewer")
    permission_id = raw.get("permissions")
    if (not isinstance(permission_id, str) or not permission_id
            or len(permission_id) > 128
            or any(ord(char) < 33 or char.isspace() for char in permission_id)
            or any(ord(char) < 32 for char in permission_id)
            or "/" in permission_id or "\\" in permission_id):
        raise AdapterFailure("Codex permission profile ID must be a bounded nonempty name")
    if (not isinstance(raw["approvalPolicy"], str)
            or raw["approvalPolicy"] not in _APPROVAL_POLICIES):
        raise AdapterFailure("Unsupported Codex approval policy")
    if (not isinstance(raw["approvalsReviewer"], str)
            or raw["approvalsReviewer"] not in _APPROVAL_REVIEWERS):
        raise AdapterFailure("Unsupported Codex approval reviewer")
    if raw["approvalPolicy"] == "never" and raw["approvalsReviewer"] != "user":
        raise AdapterFailure("A reviewer requires on-request approvals")
    return dict(raw)


def _validate_profile_config(raw: Any) -> dict:
    return _normalize_profile_config(raw)


def _effective_profile_revision(profile_id: str, config: dict,
                                security_fingerprint: str = "") -> str:
    payload = {"version": PROFILE_CONTRACT_VERSION, "id": profile_id,
               "wrapper": config, "security": security_fingerprint}
    return sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"),
                             ensure_ascii=False).encode()).hexdigest()


def _profile_revision(profile_id: str, security_fingerprint: str = "") -> str:
    return _effective_profile_revision(profile_id, PROFILES[profile_id],
                                       security_fingerprint)


def _custom_profile_revision(config: dict, security_fingerprint: str = "",
                             profile_id: str = "") -> str:
    return _effective_profile_revision(profile_id, config, security_fingerprint)


def _approval_category(value: Any) -> str:
    if value is None:
        return "default"
    if isinstance(value, str) and value in {"untrusted", "on-request", "never"}:
        return value
    if isinstance(value, dict) and isinstance(value.get("granular"), dict):
        return "granular"
    return "other"


def _reviewer_category(value: Any) -> str:
    if value is None:
        return "default"
    if isinstance(value, str) and value in _APPROVAL_REVIEWERS:
        return value
    return "other"


def _security_summary(active_profile: Any, approval_policy: Any,
                      approvals_reviewer: Any, provenance: str) -> dict:
    profile_id = None
    if isinstance(active_profile, dict):
        candidate = active_profile.get("id")
        if (isinstance(candidate, str) and 0 < len(candidate) <= 128
                and not any(ord(char) < 33 or char.isspace() for char in candidate)
                and "/" not in candidate and "\\" not in candidate):
            profile_id = candidate
    if provenance not in {"named-profile", "implicit/default", "legacy-sandbox"}:
        provenance = "implicit/default"
    return {"activePermissionProfile": profile_id,
            "approvalPolicy": _approval_category(approval_policy),
            "approvalsReviewer": _reviewer_category(approvals_reviewer),
            "provenance": provenance}


_MAX_SAFE_INTEGER = 9007199254740991
_CODEX_USAGE_FIELDS = (
    "inputTokens", "cachedInputTokens", "cacheWriteInputTokens",
    "outputTokens", "reasoningOutputTokens", "totalTokens")
_CODEX_USAGE_ALIASES: dict[str, tuple[str, ...]] = {
    "inputTokens": ("inputTokens", "input_tokens", "input"),
    "cachedInputTokens": ("cachedInputTokens", "cached_input_tokens",
                            "cacheRead", "cache_read"),
    "cacheWriteInputTokens": ("cacheWriteInputTokens",
                                "cache_write_input_tokens",
                                "cacheWrite", "cache_write"),
    "outputTokens": ("outputTokens", "output_tokens", "output"),
    "reasoningOutputTokens": ("reasoningOutputTokens",
                                "reasoning_output_tokens", "reasoning",
                                "reasoning_tokens", "reasoningTokens"),
    "totalTokens": ("totalTokens", "total_tokens", "total"),
}


def _normalize_codex_usage(raw: Any) -> dict | None:
    """Normalize a native per-turn usage snapshot to Runtime Protocol counters.

    Only ``tokenUsage.last`` is accepted; cumulative thread totals are never
    used. Repeated notifications are snapshots and replace prior usage. Only
    non-negative safe-integer counters are retained; absent counters stay
    absent (never synthesized as zero). A present-but-malformed recognized
    counter invalidates the whole snapshot; unknown provider fields are
    ignored for forward compatibility.
    """
    if not isinstance(raw, dict):
        return None
    result: dict[str, int] = {}
    for field in _CODEX_USAGE_FIELDS:
        for alias in _CODEX_USAGE_ALIASES[field]:
            if alias not in raw:
                continue
            candidate = raw[alias]
            if (isinstance(candidate, bool) or not isinstance(candidate, int)
                    or candidate < 0 or candidate > _MAX_SAFE_INTEGER):
                return None
            result[field] = int(candidate)
            break
    return result or None


def _parse_stored_codex_usage(stored: Any) -> dict | None:
    if not isinstance(stored, str) or not stored:
        return None
    try:
        value = json.loads(stored)
    except (TypeError, ValueError):
        return None
    normalized = _normalize_codex_usage(value) if isinstance(value, dict) else None
    return normalized


def _extract_codex_last(token_usage: Any) -> dict | None:
    if not isinstance(token_usage, dict):
        return None
    last = token_usage.get("last")
    return _normalize_codex_usage(last)


def _codex_cli_version(executable: Any, env: dict | None = None) -> str:
    """Read a fallback version from the same Codex executable as app-server.

    Uses the same resolved runtime environment as the app-server child so the
    version probe discovers the executable the same way the server did.
    """
    if not isinstance(executable, str) or not executable:
        return ""
    try:
        result = subprocess.run([executable, "--version"], stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, encoding="utf-8",
                                errors="replace", timeout=3, check=False, env=env)
    except (OSError, subprocess.SubprocessError):
        return ""
    if result.returncode != 0:
        return ""
    output = result.stdout or result.stderr
    match = re.search(r"(?<![\w.])v?(\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?)\b",
                      output)
    return match.group(1)[:80] if match else ""


class CodexHostAdapter:
    def __init__(self, state: Path, projects_root: Path, *, rpc: CodexRpc | None = None,
                 _exit_process: Any | None = None,
                 _login_path_resolver: Any | None = None,
                 _runtime_env: dict | None | bool = False):
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
            created TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'profile',
            applied_revision TEXT NOT NULL DEFAULT '', security_snapshot TEXT,
            permission_revision TEXT NOT NULL DEFAULT '',
            approval_revision TEXT NOT NULL DEFAULT '');
          CREATE TABLE IF NOT EXISTS conversation_replacements (
            previous TEXT PRIMARY KEY REFERENCES conversations(id),
            replacement TEXT NOT NULL REFERENCES conversations(id),
            reason TEXT NOT NULL, created TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS security_profiles (
            id TEXT PRIMARY KEY, config TEXT NOT NULL, revision TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS runs (
            id TEXT PRIMARY KEY, conversation TEXT NOT NULL REFERENCES conversations(id),
            client_run TEXT, input_hash TEXT,
            security_binding TEXT,
            turn TEXT UNIQUE, phase TEXT NOT NULL, active_state TEXT,
            outcome TEXT, result TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '',
            usage TEXT,
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
        if "security_binding" not in columns:
            self.db.execute("ALTER TABLE runs ADD COLUMN security_binding TEXT")
        if "usage" not in columns:
            self.db.execute("ALTER TABLE runs ADD COLUMN usage TEXT")
        conversation_columns = {row["name"] for row in
                               self.db.execute("PRAGMA table_info(conversations)")}
        if "source" not in conversation_columns:
            self.db.execute(
                "ALTER TABLE conversations ADD COLUMN source TEXT NOT NULL DEFAULT 'profile'")
        if "applied_revision" not in conversation_columns:
            self.db.execute(
                "ALTER TABLE conversations ADD COLUMN applied_revision TEXT NOT NULL DEFAULT ''")
            self.db.execute(
                "UPDATE conversations SET applied_revision=revision WHERE source='profile'")
        if "security_snapshot" not in conversation_columns:
            self.db.execute("ALTER TABLE conversations ADD COLUMN security_snapshot TEXT")
        if "permission_revision" not in conversation_columns:
            self.db.execute(
                "ALTER TABLE conversations ADD COLUMN permission_revision TEXT NOT NULL DEFAULT ''")
        if "approval_revision" not in conversation_columns:
            self.db.execute(
                "ALTER TABLE conversations ADD COLUMN approval_revision TEXT NOT NULL DEFAULT ''")
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
        self._profile_context_cache: dict[tuple[str, str], tuple[float, dict]] = {}
        self._settings_updates: dict[str, dict[str, Any]] = {}
        self._migrate_legacy_profile_rows()
        # A pending server callback is process-local. A prior instance cannot
        # prove it still owns that callback after restart.
        with self.lock, self.db:
            self.db.execute(
                "UPDATE runs SET phase='terminal',active_state=NULL,outcome='interrupted',"
                "error='adapter_restarted',updated=? WHERE phase IN ('starting','active')",
                (_now(),))
        self._fatal_lock = threading.Lock()
        self._fatal_reported = False
        self._exit_process = _exit_process if _exit_process is not None else os._exit
        # Resolve the terminal-equivalent executable search path once for the
        # runtime environment. Only PATH is imported; all other shell output
        # and environment values are discarded. On failure the inherited
        # service PATH is kept. `_runtime_env=False` (default) means resolve
        # now; an explicit dict (or None for inherit) is used directly so
        # unit tests stay deterministic without spawning a login shell.
        if _runtime_env is not False:
            self._runtime_env = dict(_runtime_env) if isinstance(_runtime_env, dict) else None
            self._login_path_result = {"resolved": False, "path": None,
                                         "shell": "/bin/sh", "shell_basename": "sh",
                                         "entry_count": 0, "code": "spawn_error"}
        elif _login_path_resolver is not None:
            try:
                env, result = runtime_env_with_login_path(
                    None, _resolve=_login_path_resolver)
            except Exception:
                env, result = dict(os.environ), {"resolved": False, "path": None,
                                                  "shell": "/bin/sh", "shell_basename": "sh",
                                                  "entry_count": 0, "code": "spawn_error"}
            self._runtime_env = env
            self._login_path_result = result
        elif rpc is not None:
            # Injected RPC (tests): reuse its explicit env when present so the
            # version probe shares the app-server environment; otherwise the
            # inherited environment is the safe fallback with no shell spawn.
            existing = getattr(rpc, "_runtime_env", None)
            self._runtime_env = dict(existing) if isinstance(existing, dict) else None
            self._login_path_result = {"resolved": isinstance(existing, dict),
                                         "path": None, "shell": "/bin/sh",
                                         "shell_basename": "sh", "entry_count": 0,
                                         "code": "ok" if isinstance(existing, dict) else "spawn_error"}
        else:
            try:
                env, result = runtime_env_with_login_path()
            except Exception:
                env, result = dict(os.environ), {"resolved": False, "path": None,
                                                  "shell": "/bin/sh", "shell_basename": "sh",
                                                  "entry_count": 0, "code": "spawn_error"}
            self._runtime_env = env
            self._login_path_result = result
        try:
            summary = safe_summary(self._login_path_result)
            _LOG.info("Codex login PATH %s (shell=%s entries=%s code=%s)",
                      "resolved" if summary.get("resolved") else "not-resolved",
                      summary.get("shell_basename"), summary.get("entry_count"),
                      summary.get("code"))
        except Exception:
            pass
        self.rpc = rpc or CodexRpc(on_notification=self._notification,
                                   on_request=self._request,
                                   on_unexpected_exit=self._on_native_engine_lost,
                                   env=self._runtime_env)
        self._native_cli_version_checked = False
        self._native_cli_version = ""
        if rpc is not None:
            rpc.on_notification = self._notification
            rpc.on_request = self._request
            try:
                rpc.on_unexpected_exit = self._on_native_engine_lost
            except Exception:
                pass

    def _on_native_engine_lost(self) -> None:
        """Terminate the adapter so the supervisor restarts one failure domain."""
        with self._fatal_lock:
            if self._fatal_reported:
                return
            self._fatal_reported = True
        _LOG.critical("Codex native app-server exited unexpectedly; "
                      "terminating adapter for supervisor recovery")
        try:
            self._exit_process(1)
        except Exception:
            pass

    def _migrate_legacy_profile_rows(self) -> None:
        """Normalize persisted v1 sandbox wrappers to the equivalent v2 selector."""
        with self.lock, self.db:
            rows = self.db.execute(
                "SELECT id,config FROM security_profiles ORDER BY id").fetchall()
            for row in rows:
                try:
                    raw = json.loads(row["config"])
                    config = _normalize_profile_config(raw, allow_legacy=True)
                except (ValueError, TypeError, AdapterFailure):
                    continue
                revision = _custom_profile_revision(config, profile_id=row["id"])
                self.db.execute(
                    "UPDATE security_profiles SET config=?,revision=? WHERE id=?",
                    (json.dumps(config, sort_keys=True), revision, row["id"]))

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
                    runtime_env = getattr(self, "_runtime_env", None)
                    if not isinstance(runtime_env, dict) and runtime_env is not None:
                        runtime_env = None
                    self._native_cli_version = _codex_cli_version(executable, runtime_env)
                    self._native_cli_version_checked = True
                native_version = self._native_cli_version or "unknown"
        native_version = native_version[:80]
        from .release import CODEX_ADAPTER_VERSION, codex_release
        return {"protocol": {"major": 1, "minor": 0},
                "runtime": {"id": "codex", "displayName": "Codex",
                            "adapterVersion": CODEX_ADAPTER_VERSION, "nativeVersion": native_version,
                            "instanceId": self.instance_id},
                "features": {"models": 1, "conversations": 1, "runs": 1,
                             "activities": 1, "interactions": 1, "events": 1,
                             "steering": 1, "securityRebind": 1},
                "release": codex_release()}

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

    def _profile_definition(self, profile_id: str) -> tuple[dict, str, bool]:
        if profile_id in PROFILES:
            config = dict(PROFILES[profile_id])
            return config, _profile_revision(profile_id), False
        with self.lock:
            row = self.db.execute("SELECT config,revision FROM security_profiles WHERE id=?",
                                  (profile_id,)).fetchone()
        if row is None:
            raise AdapterFailure("Unknown security profile", 409, "profile_mismatch")
        try:
            config = _validate_profile_config(json.loads(row["config"]))
        except (ValueError, TypeError, AdapterFailure):
            raise AdapterFailure("Security profile is invalid", 409, "profile_mismatch") from None
        definition_revision = _custom_profile_revision(config, profile_id=profile_id)
        if definition_revision != row["revision"]:
            raise AdapterFailure("Security profile changed", 409, "profile_mismatch")
        return config, definition_revision, True

    @staticmethod
    def _security_json_size(value: Any) -> int:
        try:
            return len(json.dumps(value, ensure_ascii=False, separators=(",", ":"),
                                  sort_keys=True, allow_nan=False).encode("utf-8"))
        except (TypeError, ValueError, UnicodeError):
            raise AdapterFailure("Codex security configuration is invalid", 502,
                                 "profile_unavailable") from None

    @staticmethod
    def _requirements_allow(requirements: dict | None, config: dict) -> None:
        if requirements is None:
            return
        checks = (("allowedApprovalPolicies", "approvalPolicy"),
                  ("allowedApprovalsReviewers", "approvalsReviewer"))
        for field, selected in checks:
            if field not in requirements:
                continue
            allowed = requirements[field]
            if allowed is None:
                continue
            if (not isinstance(allowed, list)
                    or any(not isinstance(value, str) for value in allowed)):
                raise AdapterFailure("Codex managed security requirements are invalid", 502,
                                     "profile_unavailable")
            if config[selected] not in allowed:
                raise AdapterFailure("Codex security profile is disallowed by managed requirements",
                                     409, "profile_unavailable")
        if "allowedPermissionProfiles" in requirements:
            allowed_profiles = requirements["allowedPermissionProfiles"]
            if allowed_profiles is None:
                return
            if (not isinstance(allowed_profiles, dict)
                    or any(not isinstance(value, bool)
                           for value in allowed_profiles.values())):
                raise AdapterFailure("Codex managed security requirements are invalid", 502,
                                     "profile_unavailable")
            if allowed_profiles.get(config["permissions"]) is not True:
                raise AdapterFailure("Codex permission profile is disallowed by managed requirements",
                                     409, "profile_unavailable")

    @staticmethod
    def _legacy_policy_conflict(config: dict) -> bool:
        # A legacy workspace-write overlay may carry extra roots or network
        # policy that a native permission profile cannot inherit implicitly.
        legacy_workspace = config.get("sandbox_workspace_write")
        if legacy_workspace not in (None, {}, False):
            return True
        legacy_mode = config.get("sandbox_mode")
        if legacy_mode is not None and not isinstance(legacy_mode, str):
            return True
        # Explicit thread permissions supersede a configured legacy default;
        # its effective value remains in the fingerprint. thread/start must
        # then confirm the selected profile through activePermissionProfile.
        return False

    def _native_security_context(self, workspace_id: str, cwd: str, *,
                                 force_refresh: bool = False) -> dict:
        key = (workspace_id, cwd)
        now = time.monotonic()
        if not force_refresh:
            cached = self._profile_context_cache.get(key)
            if cached and now - cached[0] <= _PROFILE_CACHE_SECONDS:
                return cached[1]
        try:
            permission_profiles = self.rpc.permission_profiles(cwd)
            config_result = self.rpc.read_security_config(cwd)
            requirements = self.rpc.read_config_requirements()
        except CodexRpcError:
            raise AdapterFailure("Codex permission profiles or security settings could not be read",
                                 502, "profile_unavailable") from None
        if self._security_json_size(permission_profiles) > _MAX_SECURITY_JSON_BYTES:
            raise AdapterFailure("Codex permission profile catalog is too large", 502,
                                 "profile_unavailable")
        if not isinstance(config_result, dict) or not isinstance(config_result.get("config"), dict):
            raise AdapterFailure("Codex effective security configuration is invalid", 502,
                                 "profile_unavailable")
        if self._security_json_size(config_result) > _MAX_SECURITY_JSON_BYTES:
            raise AdapterFailure("Codex effective security configuration is too large", 502,
                                 "profile_unavailable")
        if requirements is not None and not isinstance(requirements, dict):
            raise AdapterFailure("Codex managed security requirements are invalid", 502,
                                 "profile_unavailable")
        if requirements is not None and self._security_json_size(requirements) > _MAX_SECURITY_JSON_BYTES:
            raise AdapterFailure("Codex managed security requirements are too large", 502,
                                 "profile_unavailable")
        config = config_result["config"]
        if not isinstance(permission_profiles, list):
            raise AdapterFailure("Codex permission profile catalog is invalid", 502,
                                 "profile_unavailable")
        catalog = {row["id"]: {"allowed": row["allowed"]}
                   for row in permission_profiles
                   if isinstance(row, dict) and isinstance(row.get("id"), str)
                   and isinstance(row.get("allowed"), bool)}
        if len(catalog) != len(permission_profiles):
            raise AdapterFailure("Codex permission profile catalog is invalid", 502,
                                 "profile_unavailable")
        security_config = {name: config[name] for name in _SECURITY_CONFIG_KEYS
                           if name in config}
        security_requirements = ({name: requirements[name]
                                  for name in _SECURITY_REQUIREMENT_KEYS
                                  if name in requirements}
                                 if requirements is not None else {})
        origins = config_result.get("origins")
        if origins is not None and not isinstance(origins, dict):
            raise AdapterFailure("Codex effective security origins are invalid", 502,
                                 "profile_unavailable")
        security_origins = ({name: origins[name] for name in _SECURITY_CONFIG_KEYS
                             if name in origins} if isinstance(origins, dict) else {})
        fingerprint_payload = {"workspace": workspace_id, "directory": cwd,
                               "catalog": catalog, "config": security_config,
                               "origins": security_origins,
                               "requirements": security_requirements}
        permission_payload = {"catalog": catalog,
                              "permissions": config.get("permissions"),
                              "default_permissions": config.get("default_permissions"),
                              "requirements": {name: security_requirements[name]
                                              for name in ("allowedPermissionProfiles",
                                                           "defaultPermissions",
                                                           "allowedSandboxModes")
                                              if name in security_requirements}}
        approval = config.get("approval_policy") or "on-request"
        reviewer = config.get("approvals_reviewer") or "user"
        approval_payload = {"approval_policy": approval, "approvals_reviewer": reviewer,
                            "requirements": {name: security_requirements[name]
                                            for name in ("allowedApprovalPolicies",
                                                         "allowedApprovalsReviewers")
                                            if name in security_requirements}}
        try:
            fingerprint = sha256(json.dumps(
                fingerprint_payload, sort_keys=True, separators=(",", ":"),
                ensure_ascii=False, allow_nan=False).encode("utf-8")).hexdigest()
            permission_fingerprint = sha256(json.dumps(
                permission_payload, sort_keys=True, separators=(",", ":"),
                ensure_ascii=False, allow_nan=False).encode("utf-8")).hexdigest()
            approval_fingerprint = sha256(json.dumps(
                approval_payload, sort_keys=True, separators=(",", ":"),
                ensure_ascii=False, allow_nan=False).encode("utf-8")).hexdigest()
        except (TypeError, ValueError, UnicodeError):
            raise AdapterFailure("Codex security configuration is invalid", 502,
                                 "profile_unavailable") from None

        configured_profile = config.get("default_permissions")
        if configured_profile is not None and (
                not isinstance(configured_profile, str) or not configured_profile):
            configured_profile = None
            valid = False
        else:
            valid = True
        requirements_default = security_requirements.get("defaultPermissions")
        if requirements_default is not None and (
                not isinstance(requirements_default, str) or not requirements_default):
            valid = False
            requirements_default = None
        selected_profile = configured_profile or requirements_default
        if selected_profile is not None:
            selected = catalog.get(selected_profile)
            if selected is None or selected.get("allowed") is not True:
                valid = False
        else:
            # Codex's implicit built-in profile is the workspace profile. An
            # unavailable implicit choice means the runtime cannot resolve a
            # usable native security state for this exact cwd.
            selected = catalog.get(":workspace")
            if selected is None or selected.get("allowed") is not True:
                valid = False
        allowed_profiles = security_requirements.get("allowedPermissionProfiles")
        if allowed_profiles is not None:
            if (not isinstance(allowed_profiles, dict)
                    or any(not isinstance(value, bool)
                           for value in allowed_profiles.values())):
                valid = False
            elif selected_profile is not None and allowed_profiles.get(selected_profile) is not True:
                valid = False
            elif selected_profile is None and allowed_profiles.get(":workspace") is not True:
                valid = False

        approval_category = _approval_category(approval)
        if approval_category == "other":
            valid = False
        reviewer_category = _reviewer_category(reviewer)
        if reviewer_category == "other":
            valid = False
        allowed_approvals = security_requirements.get("allowedApprovalPolicies")
        expected_approval = approval
        if allowed_approvals is not None:
            if (not isinstance(allowed_approvals, list)
                    or any(not isinstance(value, str) for value in allowed_approvals)
                    or expected_approval not in allowed_approvals):
                valid = False
        allowed_reviewers = security_requirements.get("allowedApprovalsReviewers")
        expected_reviewer = reviewer
        if allowed_reviewers is not None:
            if (not isinstance(allowed_reviewers, list)
                    or any(not isinstance(value, str) for value in allowed_reviewers)
                    or expected_reviewer not in allowed_reviewers):
                valid = False
        allowed_sandboxes = security_requirements.get("allowedSandboxModes")
        sandbox_mode = config.get("sandbox_mode")
        if allowed_sandboxes is not None and sandbox_mode is not None:
            if (not isinstance(allowed_sandboxes, list)
                    or any(not isinstance(value, str) for value in allowed_sandboxes)
                    or sandbox_mode not in allowed_sandboxes):
                valid = False

        legacy = selected_profile is None and (
            config.get("sandbox_mode") is not None
            or config.get("sandbox_workspace_write") not in (None, {}, False))
        provenance = ("named-profile" if selected_profile is not None else
                      "legacy-sandbox" if legacy else "implicit/default")
        summary = {"activePermissionProfile": selected_profile,
                   "approvalPolicy": approval_category,
                   "approvalsReviewer": reviewer_category,
                   "provenance": provenance}
        context = {"catalog": catalog, "profiles": permission_profiles,
                   "config": config, "origins": origins or {},
                   "requirements": requirements,
                   "securityRequirements": security_requirements,
                   "fingerprint": fingerprint,
                   "permissionFingerprint": permission_fingerprint,
                   "approvalFingerprint": approval_fingerprint,
                   "selectedProfile": selected_profile,
                   "provenance": provenance,
                   "approvalValue": approval, "reviewerValue": reviewer,
                   "approvalExplicit": ("approval_policy" in config and
                                        ("approval_policy" in (origins or {})
                                         if isinstance(origins, dict) else True)),
                   "reviewerExplicit": ("approvals_reviewer" in config and
                                        ("approvals_reviewer" in (origins or {})
                                         if isinstance(origins, dict) else True)),
                   "summary": summary, "available": valid}
        self._profile_context_cache[key] = (now, context)
        if len(self._profile_context_cache) > _PROFILE_CACHE_LIMIT:
            oldest = min(self._profile_context_cache,
                         key=lambda cache_key: self._profile_context_cache[cache_key][0])
            self._profile_context_cache.pop(oldest, None)
        return context

    def _profile(self, profile_id: str, workspace_id: str, cwd: str, *,
                 force_refresh: bool = True) -> tuple[dict, str]:
        config, _definition_revision, _mutable = self._profile_definition(profile_id)
        context = self._native_security_context(workspace_id, cwd,
                                                force_refresh=force_refresh)
        native_id = config["permissions"]
        native_profile = context["catalog"].get(native_id)
        if native_profile is None or native_profile.get("allowed") is not True:
            raise AdapterFailure("Codex permission profile is missing or disallowed for this workspace",
                                 409, "profile_unavailable")
        self._requirements_allow(context["requirements"], config)
        if self._legacy_policy_conflict(context["config"]):
            raise AdapterFailure("Codex profile conflicts with effective legacy sandbox_workspace_write settings; remove those settings or select a native profile that defines the required access",
                                 409, "profile_unavailable")
        revision = _effective_profile_revision(profile_id, config,
                                               context["fingerprint"])
        return config, revision

    @staticmethod
    def _runtime_config_public(context: dict) -> dict:
        return {"supported": True, "available": context["available"],
                "status": "ready" if context["available"] else "unavailable",
                "revision": context["fingerprint"],
                "resolvedSummary": dict(context["summary"])}

    def profiles(self, workspace_id: str | None = None,
                 directory: str | None = None, *, fresh: bool = False) -> dict:
        if (workspace_id is None) != (directory is None):
            raise AdapterFailure("Workspace ID and directory must be supplied together")
        if workspace_id is not None:
            if not isinstance(workspace_id, str) or not workspace_id or len(workspace_id) > 100:
                raise AdapterFailure("Invalid workspace id")
            cwd = self._cwd(directory)
            context = self._native_security_context(workspace_id, cwd,
                                                    force_refresh=fresh)
        else:
            context = None
        rows = []
        definitions = [(key, dict(value), _profile_revision(key), False)
                       for key, value in PROFILES.items()]
        with self.lock:
            custom = self.db.execute(
                "SELECT id,config,revision FROM security_profiles ORDER BY id").fetchall()
        for row in custom:
            try:
                config = _validate_profile_config(json.loads(row["config"]))
            except (ValueError, TypeError, AdapterFailure):
                continue
            definition_revision = _custom_profile_revision(config, profile_id=row["id"])
            if definition_revision != row["revision"]:
                continue
            definitions.append((row["id"], config, definition_revision, True))
        for profile_id, config, definition_revision, mutable in definitions:
            revision = definition_revision
            available = True
            if context is not None:
                native_id = config["permissions"]
                native_profile = context["catalog"].get(native_id)
                available = bool(native_profile and native_profile.get("allowed") is True)
                try:
                    self._requirements_allow(context["requirements"], config)
                    if self._legacy_policy_conflict(context["config"]):
                        available = False
                    if available:
                        revision = _effective_profile_revision(
                            profile_id, config, context["fingerprint"])
                except AdapterFailure:
                    available = False
            profile_row = {"id": profile_id, "revision": revision,
                           "definitionRevision": definition_revision,
                           "config": config, "mutable": mutable,
                           "enforcement": ["native-permission-profile", "approval-policy"]}
            if context is not None:
                profile_row["available"] = available
            rows.append(profile_row)
        result = {"profiles": rows}
        if context is not None:
            requirements = context["requirements"]
            required_permissions = None
            requirements_valid = True
            if requirements is not None and "allowedPermissionProfiles" in requirements:
                required_permissions = requirements["allowedPermissionProfiles"]
                requirements_valid = (required_permissions is None or
                    isinstance(required_permissions, dict)
                    and all(isinstance(value, bool)
                            for value in required_permissions.values()))
            result["permissionProfiles"] = [
                {"id": item["id"], "description": item["description"],
                 "allowed": item["allowed"]}
                for item in context["profiles"]
                if (item["allowed"] is True and requirements_valid
                    and (required_permissions is None
                         or required_permissions.get(item["id"]) is True))]
            result["runtimeConfig"] = self._runtime_config_public(context)
        return result

    def save_profile(self, body: dict) -> dict:
        profile_id = body.get("id")
        if (not isinstance(profile_id, str) or not PROFILE_ID_RE.fullmatch(profile_id)
                or profile_id in PROFILES):
            raise AdapterFailure("Choose a new profile ID using lowercase letters, numbers, _ or -")
        config = _validate_profile_config(body.get("config"))
        expected = body.get("expectedRevision")
        if expected is not None and not isinstance(expected, str):
            raise AdapterFailure("Invalid expected revision")
        revision = _custom_profile_revision(config, profile_id=profile_id)
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
        return {"id": profile_id, "revision": revision,
                "definitionRevision": revision, "config": config,
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

    REBIND_PENDING = "rebind-pending"

    @staticmethod
    def _is_invalidated(owned: dict) -> bool:
        thread = owned.get("thread", "")
        return (owned.get("revision") == "invalidated"
                or owned.get("applied_revision") in ("invalidated", "rebind-pending")
                or (isinstance(thread, str) and thread.startswith("invalidated-")))

    def _write_rebind_pending(self, conversation_id: str) -> dict:
        """Commit a durable rebind-pending sentinel BEFORE native mutation.

        Caches and returns the exact old row values for a proven pre-update
        failure rollback. Raises bounded `unavailable` if the marker cannot
        be persisted; the caller must not touch native state in that case.
        The marker contains no profile config, paths, prompts, or secrets.
        """
        try:
            with self.lock, self.db:
                row = self.db.execute("SELECT * FROM conversations WHERE id=?",
                                      (conversation_id,)).fetchone()
                if row is None:
                    raise AdapterFailure("Conversation not found", 404, "not_found")
                cached = dict(row)
                self.db.execute(
                    "UPDATE conversations SET applied_revision='rebind-pending' WHERE id=?",
                    (conversation_id,))
                return cached
        except AdapterFailure:
            raise
        except Exception as exc:
            raise AdapterFailure("Conversation is unavailable", 502, "unavailable") from exc

    def _restore_pending_to_cached(self, cached: dict) -> None:
        """Restore exact old DB fields ONLY when proven no native call occurred.

        The ONLY safe caller is a branch that raised before invoking any
        native mutation RPC (e.g. an in-memory rebind conflict detected
        before transport send). A generic `CodexRpcError` from an attempted
        `thread/settings/update` or `thread/resume` is ambiguous: `CodexRpc`
        sends the request before waiting, so timeout/transport/server errors
        can occur after the native mutation was applied. Such errors must
        NEVER restore old metadata; they leave pending/invalidation.
        If this restore commit fails, the durable pending sentinel remains
        and the conversation stays unavailable.
        """
        try:
            with self.lock, self.db:
                self.db.execute(
                    "UPDATE conversations SET thread=?,profile=?,revision=?,applied_revision=? "
                    "WHERE id=?",
                    (cached["thread"], cached["profile"], cached["revision"],
                     cached.get("applied_revision", cached["revision"]), cached["id"]))
        except Exception as exc:
            # Restore failed: ensure the durable pending sentinel remains
            # for restart fail-closed, then report unavailable.
            try:
                with self.lock, self.db:
                    self.db.execute(
                        "UPDATE conversations SET applied_revision='rebind-pending' WHERE id=?",
                        (cached["id"],))
            except Exception:
                pass
            raise AdapterFailure("Conversation is unavailable", 502, "unavailable") from exc

    def _invalidate_owned_conversation(self, conversation_id: str) -> None:
        """Fail closed on ambiguous native security state.

        An accepted-but-unconfirmed `thread/settings/update` (or an
        unproven `thread/resume`) may already have mutated native settings.
        The owned row must never keep advertising the old binding as proven.
        Ownership of the native thread is invalidated: the native thread may
        remain in Codex but becomes unowned/inaccessible through this adapter
        conversation ID. No blank replacement thread is created. Historical
        terminal runs remain readable via `_run` (terminal `run()` does not
        require the conversation row). A durable rebind-pending sentinel was
        committed before mutation, so even if this tombstone fails, restart
        remains fail-closed via pending.
        """
        with self.lock, self.db:
            row = self.db.execute("SELECT thread FROM conversations WHERE id=?",
                                  (conversation_id,)).fetchone()
            if row is None:
                return
            old_thread = row["thread"]
            self._settings_updates.pop(old_thread, None)
            tombstone = "invalidated-" + secrets.token_hex(12)
            try:
                self.db.execute(
                    "UPDATE conversations SET thread=?,profile='',revision='invalidated',"
                    "applied_revision='invalidated' WHERE id=?",
                    (tombstone, conversation_id))
            except Exception:
                # Tombstone failed: fall back to the pending sentinel so
                # restart still fails closed. Never return success.
                try:
                    self.db.execute(
                        "UPDATE conversations SET applied_revision='rebind-pending' WHERE id=?",
                        (conversation_id,))
                except Exception:
                    pass

    def _replacement_target(self, conversation_id: str) -> str:
        current = conversation_id
        seen = {current}
        with self.lock:
            for _ in range(8):
                row = self.db.execute(
                    "SELECT replacement FROM conversation_replacements WHERE previous=?",
                    (current,)).fetchone()
                if row is None:
                    return current
                current = row["replacement"]
                if current in seen:
                    raise AdapterFailure("Conversation replacement chain is invalid", 502,
                                         "binding_mismatch")
                seen.add(current)
        raise AdapterFailure("Conversation replacement chain is too long", 502,
                             "binding_mismatch")

    def _thread_start_failure(self, exc: CodexRpcError) -> AdapterFailure:
        detail = sanitize_diagnostic(str(exc), limit=300) or "native request failed"
        stderr_summary = getattr(self.rpc, "stderr_summary", None)
        stderr = sanitize_diagnostic(stderr_summary(), limit=2048) if callable(
            stderr_summary) else ""
        summary = f"Codex thread/start failed: {detail}"
        if stderr:
            summary += f"; app-server stderr: {stderr}"
        _LOG.warning("%s", summary[:2600])
        return AdapterFailure("Codex thread/start failed", 502, "runtime_unavailable")

    def _runtime_thread_summary(self, native: dict, context: dict, cwd: str) -> dict:
        thread = (native.get("thread") or {}).get("id")
        if not isinstance(thread, str) or not thread or native.get("cwd") != cwd:
            raise AdapterFailure("Codex thread binding was not confirmed", 502,
                                 "binding_mismatch")
        active_profile = native.get("activePermissionProfile")
        if "activePermissionProfile" in native and not isinstance(active_profile, dict):
            raise AdapterFailure("Codex returned invalid active security state", 502,
                                 "binding_mismatch")
        active_id = active_profile.get("id") if isinstance(active_profile, dict) else None
        expected_id = context.get("selectedProfile")
        if expected_id is not None and active_id != expected_id:
            raise AdapterFailure("Codex resolved a different security profile", 502,
                                 "binding_mismatch")
        if active_id is not None:
            profile = context["catalog"].get(active_id)
            if profile is None or profile.get("allowed") is not True:
                raise AdapterFailure("Codex resolved a disallowed security profile", 502,
                                     "binding_mismatch")
        elif expected_id is not None:
            raise AdapterFailure("Codex did not confirm its security profile", 502,
                                 "binding_mismatch")
        approval = native.get("approvalPolicy")
        reviewer = native.get("approvalsReviewer")
        if "approvalPolicy" not in native or "approvalsReviewer" not in native:
            raise AdapterFailure("Codex did not confirm effective approval settings", 502,
                                 "binding_mismatch")
        summary = _security_summary(active_profile, approval, reviewer,
                                    context["provenance"])
        if summary["approvalPolicy"] == "other" or summary["approvalsReviewer"] == "other":
            raise AdapterFailure("Codex returned unsupported effective security settings", 502,
                                 "binding_mismatch")
        config = context["config"]
        expected_approval = config.get("approval_policy")
        if (expected_approval is not None and
                _approval_category(expected_approval) != summary["approvalPolicy"]):
            raise AdapterFailure("Codex approval policy differs from resolved config", 502,
                                 "binding_mismatch")
        expected_reviewer = config.get("approvals_reviewer")
        if (expected_reviewer is not None and
                _reviewer_category(expected_reviewer) != summary["approvalsReviewer"]):
            raise AdapterFailure("Codex approval reviewer differs from resolved config", 502,
                                 "binding_mismatch")
        requirements = context["securityRequirements"]
        allowed = requirements.get("allowedApprovalPolicies")
        if allowed is not None and isinstance(approval, str) and approval not in allowed:
            raise AdapterFailure("Codex approval policy is disallowed by managed requirements",
                                 502, "binding_mismatch")
        allowed = requirements.get("allowedApprovalsReviewers")
        if allowed is not None and isinstance(reviewer, str) and reviewer not in allowed:
            raise AdapterFailure("Codex approval reviewer is disallowed by managed requirements",
                                 502, "binding_mismatch")
        if expected_id is None and active_id is None:
            # Legacy sandbox responses retain their compatibility policy here;
            # Bridge records only the provenance and never mirrors its details.
            if not isinstance(native.get("sandbox"), dict):
                raise AdapterFailure("Codex effective security state was not confirmed", 502,
                                     "binding_mismatch")
        return summary

    def _start_runtime_thread(self, workspace: str, cwd: str,
                              context: dict) -> tuple[str, dict]:
        if context.get("available") is not True:
            raise AdapterFailure("Codex config cannot resolve a usable security state",
                                 409, "runtime_config_unavailable")
        try:
            # Intentionally omit permissions, sandbox, approvalPolicy, and
            # approvalsReviewer so Codex resolves its own layered config.
            native = self.rpc.call("thread/start", {"cwd": cwd, "ephemeral": False},
                                   timeout=30)
        except CodexRpcError as exc:
            raise self._thread_start_failure(exc) from None
        summary = self._runtime_thread_summary(native, context, cwd)
        thread = (native.get("thread") or {}).get("id")
        return thread, summary

    def _insert_runtime_conversation(self, workspace: str, cwd: str,
                                     context: dict) -> dict:
        thread, summary = self._start_runtime_thread(workspace, cwd, context)
        conversation_id = _id("conv_")
        snapshot = json.dumps(summary, sort_keys=True, separators=(",", ":"))
        with self.lock, self.db:
            self.db.execute(
                "INSERT INTO conversations"
                "(id,thread,workspace,cwd,profile,revision,created,source,applied_revision,"
                "security_snapshot,permission_revision,approval_revision) "
                "VALUES(?,?,?,?,'',?,?,'runtime-config',?,?,?,?)",
                (conversation_id, thread, workspace, cwd, context["fingerprint"],
                 _now(), context["fingerprint"], snapshot,
                 context["permissionFingerprint"], context["approvalFingerprint"]))
            self._emit("conversation.created", conversation_id)
        return {"id": conversation_id, "runtime": "codex", "nativeId": thread,
                "workspaceId": workspace,
                "securityBinding": {"source": "runtime-config",
                                    "revision": context["fingerprint"],
                                    "permissionRevision": context["permissionFingerprint"],
                                    "approvalRevision": context["approvalFingerprint"],
                                    "resolvedSummary": summary},
                "status": "idle"}

    @staticmethod
    def _stored_security_summary(owned: dict) -> dict:
        try:
            value = json.loads(owned.get("security_snapshot") or "{}")
        except (TypeError, ValueError):
            value = {}
        return value if isinstance(value, dict) else {}

    def create_conversation(self, body: dict) -> dict:
        workspace = body.get("workspaceId")
        if not isinstance(workspace, str) or not workspace or len(workspace) > 100:
            raise AdapterFailure("Invalid workspace id")
        cwd = self._cwd(body.get("directory"))
        binding = body.get("securityBinding")
        if isinstance(binding, dict) and binding.get("source") == "runtime-config":
            context = self._native_security_context(workspace, cwd, force_refresh=True)
            return self._insert_runtime_conversation(workspace, cwd, context)
        profile = body.get("securityProfile") or {}
        profile_id = profile.get("id") if isinstance(profile, dict) else None
        if not isinstance(profile_id, str):
            raise AdapterFailure("Unknown or changed security profile", 409, "profile_mismatch")
        options, revision = self._profile(profile_id, workspace, cwd)
        if profile.get("revision") != revision:
            raise AdapterFailure("Unknown or changed security profile", 409, "profile_mismatch")
        try:
            native = self.rpc.call("thread/start", {
                "cwd": cwd, "permissions": options["permissions"],
                "approvalPolicy": options["approvalPolicy"],
                "approvalsReviewer": options["approvalsReviewer"], "ephemeral": False,
            }, timeout=30)
        except CodexRpcError as exc:
            raise self._thread_start_failure(exc) from None
        thread = (native.get("thread") or {}).get("id")
        if not isinstance(thread, str) or not thread or native.get("cwd") != cwd:
            raise AdapterFailure("Codex thread binding was not confirmed", 502,
                                 "binding_mismatch")
        if "activePermissionProfile" in native:
            active_profile = native.get("activePermissionProfile")
            if (not isinstance(active_profile, dict)
                    or active_profile.get("id") != options["permissions"]):
                raise AdapterFailure("Codex started the thread with a different permission profile",
                                     502, "binding_mismatch")
        conversation_id = _id("conv_")
        with self.lock, self.db:
            self.db.execute(
                "INSERT INTO conversations"
                "(id,thread,workspace,cwd,profile,revision,created,source,applied_revision) "
                "VALUES(?,?,?,?,?,?,?,'profile',?)",
                (conversation_id, thread, workspace, cwd, profile_id, revision,
                 _now(), revision))
            self._emit("conversation.created", conversation_id)
        return {"id": conversation_id, "runtime": "codex", "nativeId": thread,
                "workspaceId": workspace,
                "securityProfile": {"id": profile_id, "revision": revision},
                "securityBinding": {"source": "profile", "profile": {
                    "id": profile_id, "revision": revision}},
                "status": "idle"}

    def conversation(self, conversation_id: str) -> dict:
        owned = self._conversation(conversation_id)
        if self._is_invalidated(owned):
            raise AdapterFailure("Conversation is unavailable", 404, "not_found")
        source = owned.get("source", "profile")
        if source == "profile":
            options, revision = self._profile(owned["profile"], owned["workspace"],
                                              owned["cwd"])
            if owned["revision"] != revision:
                raise AdapterFailure("Conversation security profile changed", 409,
                                     "profile_mismatch")
        elif source == "runtime-config":
            options, revision = None, owned.get("applied_revision") or owned["revision"]
        else:
            raise AdapterFailure("Conversation security source is invalid", 502,
                                 "binding_mismatch")
        try:
            native = self.rpc.call("thread/read", {"threadId": owned["thread"],
                                                   "includeTurns": False})
            status = ((native.get("thread") or {}).get("status") or {}).get("type")
        except CodexRpcError as exc:
            raise AdapterFailure(_clean(str(exc), 300), 502, "runtime_unavailable") from None
        if status == "notLoaded":
            resume_params = {"threadId": owned["thread"], "cwd": owned["cwd"],
                             "excludeTurns": True}
            if source == "profile":
                resume_params.update({"permissions": options["permissions"],
                                      "approvalPolicy": options["approvalPolicy"],
                                      "approvalsReviewer": options["approvalsReviewer"]})
            try:
                resumed = self.rpc.call("thread/resume", resume_params, timeout=30)
            except CodexRpcError:
                status = "unavailable"
            else:
                if ((resumed.get("thread") or {}).get("id") != owned["thread"]
                        or resumed.get("cwd") != owned["cwd"]):
                    raise AdapterFailure("Resumed thread binding changed", 502,
                                         "binding_mismatch")
                if source == "profile" and "activePermissionProfile" in resumed:
                    active_profile = resumed.get("activePermissionProfile")
                    if (not isinstance(active_profile, dict)
                            or active_profile.get("id") != options["permissions"]):
                        raise AdapterFailure("Resumed thread has a different permission profile",
                                             502, "binding_mismatch")
                status = ((resumed.get("thread") or {}).get("status") or {}).get("type")
        elif (native.get("thread") or {}).get("id") != owned["thread"]:
            raise AdapterFailure("Thread identity changed", 502, "binding_mismatch")
        result = {"id": owned["id"], "runtime": "codex", "nativeId": owned["thread"],
                  "workspaceId": owned["workspace"], "status": status}
        if source == "profile":
            profile_binding = {"id": owned["profile"], "revision": owned["revision"]}
            result["securityProfile"] = profile_binding
            result["securityBinding"] = {"source": "profile", "profile": profile_binding}
        else:
            result["securityBinding"] = {
                "source": "runtime-config", "revision": revision,
                "permissionRevision": owned.get("permission_revision", ""),
                "approvalRevision": owned.get("approval_revision", ""),
                "resolvedSummary": self._stored_security_summary(owned)}
        return result

    def rebind_conversation(self, conversation_id: str, security_binding: dict) -> dict:
        """Idle-boundary rebind of a named-profile conversation to a new profile.

        Preserves the same WBRP conversation ID and native thread/history.
        Requires the owned conversation and requested binding to both be
        ``source: profile``. The thread must be idle (or ``notLoaded`` for
        the same-thread resume path). A confirmed native settings update or
        a verified same-thread resume is required before the owned
        profile/revision is updated. Never creates a blank replacement
        thread; failures raise ``security_rebind_unavailable`` (or
        ``conversation_busy``/``binding_mismatch``) with no turn started.
        """
        if (not isinstance(security_binding, dict)
                or security_binding.get("source") != "profile"
                or not isinstance(security_binding.get("profile"), dict)):
            raise AdapterFailure("Invalid security rebind binding")
        target = security_binding.get("profile", {})
        target_id = target.get("id")
        requested_revision = target.get("revision")
        if (not isinstance(target_id, str) or not target_id or len(target_id) > 100
                or not isinstance(requested_revision, str) or not requested_revision
                or len(requested_revision) > 100):
            raise AdapterFailure("Invalid security rebind binding")
        owned = self._conversation(conversation_id)
        if self._is_invalidated(owned):
            raise AdapterFailure("Conversation is unavailable", 404, "not_found")
        if owned.get("source", "profile") != "profile":
            raise AdapterFailure("Security source change requires a fresh conversation",
                                 409, "security_rebind_unavailable")
        with self.lock:
            busy = self.db.execute(
                "SELECT id FROM runs WHERE conversation=? AND phase IN ('starting','active')",
                (owned["id"],)).fetchone()
            if busy:
                raise AdapterFailure("Conversation is busy", 409, "conversation_busy")
        # Resolve the live target profile for this exact workspace/cwd.
        try:
            options, live_revision = self._profile(
                target_id, owned["workspace"], owned["cwd"])
        except AdapterFailure:
            raise
        except Exception as exc:
            raise AdapterFailure(_clean(str(exc), 200), 502, "binding_mismatch") from None
        if live_revision != requested_revision:
            raise AdapterFailure("Requested security revision is not current", 409,
                                 "binding_mismatch")
        if owned["profile"] == target_id and owned["revision"] == live_revision:
            # Already bound; prove idle and return the current binding.
            try:
                native = self.rpc.call("thread/read", {"threadId": owned["thread"],
                                                       "includeTurns": False})
            except CodexRpcError as exc:
                raise AdapterFailure(_clean(str(exc), 300), 502,
                                     "runtime_unavailable") from None
            status = ((native.get("thread") or {}).get("status") or {}).get("type")
            if status == "notLoaded":
                # Same-thread resume path still verifies the binding below.
                pass
            elif status != "idle":
                raise AdapterFailure("Conversation is not idle", 409, "conversation_busy")
            elif (native.get("thread") or {}).get("id") != owned["thread"]:
                raise AdapterFailure("Thread identity changed", 502, "binding_mismatch")
            if status != "notLoaded":
                return self.conversation(owned["id"])
        try:
            native = self.rpc.call("thread/read", {"threadId": owned["thread"],
                                                   "includeTurns": False})
        except CodexRpcError as exc:
            raise AdapterFailure(_clean(str(exc), 300), 502, "runtime_unavailable") from None
        if (native.get("thread") or {}).get("id") != owned["thread"] and (
                (native.get("thread") or {}).get("status") or {}).get("type") != "notLoaded":
            # A mismatched thread identity fails closed; notLoaded carries no id.
            if ((native.get("thread") or {}).get("status") or {}).get("type") != "notLoaded":
                raise AdapterFailure("Thread identity changed", 502, "binding_mismatch")
        status = ((native.get("thread") or {}).get("status") or {}).get("type")
        thread = owned["thread"]
        if status == "notLoaded":
            resume_params = {"threadId": thread, "cwd": owned["cwd"],
                             "excludeTurns": True,
                             "permissions": options["permissions"],
                             "approvalPolicy": options["approvalPolicy"],
                             "approvalsReviewer": options["approvalsReviewer"]}
            # Durable pending marker BEFORE native mutation; abort on save
            # failure with zero native calls.
            try:
                self._write_rebind_pending(owned["id"])
            except AdapterFailure:
                raise
            except Exception as exc:
                raise AdapterFailure("Conversation is unavailable", 502, "unavailable") from exc
            try:
                resumed = self.rpc.call("thread/resume", resume_params, timeout=30)
            except CodexRpcError as exc:
                # Ambiguous by default: the resume request was transmitted
                # before waiting, so a timeout/transport/server error can
                # occur after the native thread was resumed/reconfigured.
                # Keep the durable pending sentinel (fail-closed); never
                # restore old metadata on this path.
                raise AdapterFailure(_clean(str(exc), 300), 502,
                                     "security_rebind_unavailable") from None
            if ((resumed.get("thread") or {}).get("id") != thread
                    or resumed.get("cwd") != owned["cwd"]):
                self._invalidate_owned_conversation(owned["id"])
                raise AdapterFailure("Resumed thread binding changed", 502,
                                     "security_rebind_unavailable")
            if "activePermissionProfile" in resumed:
                active = resumed.get("activePermissionProfile")
                if (not isinstance(active, dict)
                        or active.get("id") != options["permissions"]):
                    self._invalidate_owned_conversation(owned["id"])
                    raise AdapterFailure("Resumed thread has a different permission profile",
                                         502, "security_rebind_unavailable")
            resumed_status = ((resumed.get("thread") or {}).get("status") or {}).get("type")
            if resumed_status != "idle":
                self._invalidate_owned_conversation(owned["id"])
                if resumed_status == "active":
                    raise AdapterFailure("Conversation is not idle", 409, "conversation_busy")
                raise AdapterFailure("Security rebind could not be confirmed", 502,
                                     "security_rebind_unavailable")
        elif status != "idle":
            raise AdapterFailure("Conversation is not idle", 409, "conversation_busy")
        else:
            # Durable pending marker BEFORE native mutation; abort on save
            # failure with zero native calls. The cached old row is used ONLY
            # for the proven no-native-call conflict below (in-memory waiter
            # already present: this attempt made no native call, and the
            # cache was read after any prior marker commit, so restoring it
            # cannot clear another attempt's durability).
            try:
                cached_old = self._write_rebind_pending(owned["id"])
            except AdapterFailure:
                raise
            except Exception as exc:
                raise AdapterFailure("Conversation is unavailable", 502, "unavailable") from exc
            waiter = {"event": threading.Event(), "notification": None}
            with self.lock:
                if thread in self._settings_updates:
                    # Proven no-native-call conflict: this attempt invoked no
                    # native RPC, so restoring exact old DB state is safe.
                    try:
                        self._restore_pending_to_cached(cached_old)
                    except AdapterFailure:
                        raise AdapterFailure("Conversation is unavailable", 502, "unavailable") from None
                    raise AdapterFailure("Security rebind is already pending", 409,
                                         "security_rebind_unavailable")
                self._settings_updates[thread] = waiter
            try:
                settings = {"permissions": options["permissions"],
                            "approvalPolicy": options["approvalPolicy"],
                            "approvalsReviewer": options["approvalsReviewer"]}
                update = getattr(self.rpc, "update_thread_settings", None)
                if callable(update):
                    try:
                        update(thread, settings, timeout=10)
                    except CodexRpcError as exc:
                        # Ambiguous by default: the update request was
                        # transmitted before waiting, so timeout/transport/
                        # server errors can occur after native mutation.
                        # Keep pending/invalidate; never restore old metadata.
                        raise AdapterFailure(_clean(str(exc), 200), 502,
                                             "security_rebind_unavailable") from None
                else:
                    try:
                        self.rpc.call("thread/settings/update",
                                      {"threadId": thread, **settings}, timeout=10)
                    except CodexRpcError as exc:
                        # Same ambiguity as above; never restore.
                        raise AdapterFailure(_clean(str(exc), 200), 502,
                                             "security_rebind_unavailable") from None
                if not waiter["event"].wait(2.0):
                    self._invalidate_owned_conversation(owned["id"])
                    raise AdapterFailure("Security rebind was not confirmed", 502,
                                         "security_rebind_unavailable")
                notification = waiter.get("notification")
                if (not isinstance(notification, dict)
                        or notification.get("threadId") != thread
                        or not isinstance(notification.get("threadSettings"), dict)):
                    self._invalidate_owned_conversation(owned["id"])
                    raise AdapterFailure("Security rebind was not confirmed", 502,
                                         "security_rebind_unavailable")
                applied = notification["threadSettings"]
                active_profile = applied.get("activePermissionProfile")
                if (not isinstance(active_profile, dict)
                        or active_profile.get("id") != options["permissions"]
                        or applied.get("approvalPolicy") != options["approvalPolicy"]
                        or applied.get("approvalsReviewer") != options["approvalsReviewer"]):
                    self._invalidate_owned_conversation(owned["id"])
                    raise AdapterFailure("Security rebind settings mismatch", 502,
                                         "security_rebind_unavailable")
            finally:
                with self.lock:
                    self._settings_updates.pop(thread, None)
        # Confirmed success: atomically replace pending with target binding.
        # If this finalization fails, durable pending remains fail-closed and
        # the current process must not return success.
        try:
            with self.lock, self.db:
                self.db.execute(
                    "UPDATE conversations SET profile=?,revision=?,applied_revision=? WHERE id=?",
                    (target_id, live_revision, live_revision, owned["id"]))
        except Exception as exc:
            try:
                with self.lock, self.db:
                    self.db.execute(
                        "UPDATE conversations SET applied_revision='rebind-pending' WHERE id=?",
                        (owned["id"],))
            except Exception:
                pass
            raise AdapterFailure("Conversation is unavailable", 502, "unavailable") from exc
        return self.conversation(owned["id"])

    def _run(self, run_id: str) -> dict:
        with self.lock:
            row = self.db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise AdapterFailure("Run not found", 404, "not_found")
        return dict(row)

    def _run_public(self, run: dict) -> dict:
        result = {"id": run["id"], "conversationId": run["conversation"],
                "clientRunId": run.get("client_run"),
                "nativeId": run["turn"], "phase": run["phase"],
                "activeState": run["active_state"], "outcome": run["outcome"],
                "result": _clean(run["result"], 20000),
                "error": _clean(run["error"], 300),
                "createdAt": run["created"], "updatedAt": run["updated"]}
        try:
            binding = json.loads(run.get("security_binding") or "null")
        except (TypeError, ValueError):
            binding = None
        if isinstance(binding, dict):
            result["securityBinding"] = binding
        usage = _parse_stored_codex_usage(run.get("usage"))
        if usage is not None:
            result["usage"] = usage
        return result

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

    def _store_runtime_security(self, conversation_id: str, context: dict,
                                summary: dict) -> None:
        snapshot = json.dumps(summary, sort_keys=True, separators=(",", ":"))
        with self.lock, self.db:
            self.db.execute(
                "UPDATE conversations SET revision=?,applied_revision=?,security_snapshot=?,"
                "permission_revision=?,approval_revision=? WHERE id=? AND source='runtime-config'",
                (context["fingerprint"], context["fingerprint"], snapshot,
                 context["permissionFingerprint"], context["approvalFingerprint"],
                 conversation_id))

    def _refresh_runtime_security(self, owned: dict, context: dict) -> tuple[dict, str | None]:
        """Apply an idle-boundary Codex settings update, or return a fallback reason."""
        if context.get("available") is not True:
            raise AdapterFailure("Codex config cannot resolve a usable security state",
                                 409, "runtime_config_unavailable")
        old_summary = self._stored_security_summary(owned)
        target_profile = context.get("selectedProfile") or old_summary.get(
            "activePermissionProfile")
        if not isinstance(target_profile, str) or not target_profile:
            return old_summary, "unresolved-permission-profile"
        target_catalog = context["catalog"].get(target_profile)
        if target_catalog is None or target_catalog.get("allowed") is not True:
            return old_summary, "permission-profile-unavailable"
        if context.get("provenance") == "legacy-sandbox":
            return old_summary, "legacy-sandbox-transition"

        permission_changed = (
            owned.get("permission_revision") != context["permissionFingerprint"]
            or old_summary.get("activePermissionProfile") != target_profile)
        approval_changed = (
            owned.get("approval_revision") != context["approvalFingerprint"])
        target_summary = {"activePermissionProfile": target_profile,
                          "approvalPolicy": context["summary"]["approvalPolicy"],
                          "approvalsReviewer": context["summary"]["approvalsReviewer"],
                          "provenance": context["provenance"]}
        settings: dict[str, Any] = {}
        if permission_changed:
            settings["permissions"] = target_profile
        if approval_changed:
            settings["approvalPolicy"] = context["approvalValue"]
            settings["approvalsReviewer"] = context["reviewerValue"]
        if not settings:
            self._store_runtime_security(owned["id"], context, target_summary)
            return target_summary, None

        waiter = {"event": threading.Event(), "notification": None}
        thread = owned["thread"]
        with self.lock:
            if thread in self._settings_updates:
                return old_summary, "settings-update-already-pending"
            self._settings_updates[thread] = waiter
        try:
            update = getattr(self.rpc, "update_thread_settings", None)
            if callable(update):
                update(thread, settings, timeout=10)
            else:
                self.rpc.call("thread/settings/update",
                              {"threadId": thread, **settings}, timeout=10)
            if not waiter["event"].wait(2.0):
                return old_summary, "settings-update-unconfirmed"
            notification = waiter.get("notification")
            if (not isinstance(notification, dict)
                    or notification.get("threadId") != thread
                    or not isinstance(notification.get("threadSettings"), dict)):
                return old_summary, "settings-update-unconfirmed"
            applied = notification["threadSettings"]
            active_profile = applied.get("activePermissionProfile")
            if (not isinstance(active_profile, dict)
                    or active_profile.get("id") != target_profile
                    or _approval_category(applied.get("approvalPolicy"))
                    != target_summary["approvalPolicy"]
                    or _reviewer_category(applied.get("approvalsReviewer"))
                    != target_summary["approvalsReviewer"]):
                return old_summary, "settings-update-mismatch"
            applied_summary = _security_summary(
                active_profile, applied.get("approvalPolicy"),
                applied.get("approvalsReviewer"), context["provenance"])
            self._store_runtime_security(owned["id"], context, applied_summary)
            return applied_summary, None
        except CodexRpcError:
            return old_summary, "settings-update-failed"
        finally:
            with self.lock:
                self._settings_updates.pop(thread, None)

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
        original_conversation_id = conversation_id
        while True:
            resolved_id = self._replacement_target(original_conversation_id)
            with self.lock:
                owned = self._conversation(resolved_id)
                admission = self._admission.setdefault(resolved_id, threading.Lock())
            admission.acquire()
            if resolved_id == self._replacement_target(original_conversation_id):
                conversation_id = resolved_id
                break
            admission.release()

        if self._is_invalidated(owned):
            admission.release()
            raise AdapterFailure("Conversation is unavailable", 404, "not_found")
        # The same per-conversation admission lock spans idle verification,
        # security refresh/replacement, and turn/start. Settings can therefore
        # never race ahead of an already admitted turn.
        try:
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
                raise AdapterFailure(_clean(str(exc), 300), 502,
                                     "runtime_unavailable") from None
            if ((native.get("thread") or {}).get("status") or {}).get("type") != "idle":
                raise AdapterFailure("Conversation is not idle", 409, "conversation_busy")
            if (native.get("thread") or {}).get("id") != owned["thread"]:
                raise AdapterFailure("Thread identity changed", 502, "binding_mismatch")

            replacement_reason = None
            replacement_from_id = None
            security_binding = None
            if owned.get("source", "profile") == "profile":
                _options, current_revision = self._profile(
                    owned["profile"], owned["workspace"], owned["cwd"])
                if current_revision != owned["revision"]:
                    raise AdapterFailure("Conversation security profile changed", 409,
                                         "profile_mismatch")
                security_binding = {"source": "profile", "profile": {
                    "id": owned["profile"], "revision": owned["revision"]}}
            elif owned.get("source") == "runtime-config":
                context = self._native_security_context(
                    owned["workspace"], owned["cwd"], force_refresh=True)
                if context.get("available") is not True:
                    raise AdapterFailure("Codex config cannot resolve a usable security state",
                                         409, "runtime_config_unavailable")
                applied_revision = owned.get("applied_revision") or owned["revision"]
                summary = self._stored_security_summary(owned)
                if applied_revision != context["fingerprint"]:
                    summary, replacement_reason = self._refresh_runtime_security(owned, context)
                    if replacement_reason is not None:
                        context = self._native_security_context(
                            owned["workspace"], owned["cwd"], force_refresh=True)
                        replacement = self._insert_runtime_conversation(
                            owned["workspace"], owned["cwd"], context)
                        replacement_from_id = conversation_id
                        owned = self._conversation(replacement["id"])
                        conversation_id = owned["id"]
                        summary = replacement["securityBinding"]["resolvedSummary"]
                else:
                    summary = summary or context["summary"]
                security_binding = {"source": "runtime-config",
                                    "revision": context["fingerprint"],
                                    "permissionRevision": context["permissionFingerprint"],
                                    "approvalRevision": context["approvalFingerprint"],
                                    "resolvedSummary": summary}
                if replacement_reason is not None:
                    security_binding["replacementReason"] = replacement_reason
                    security_binding["replacedConversationId"] = (
                        replacement_from_id or original_conversation_id)
            else:
                raise AdapterFailure("Conversation security source is invalid", 502,
                                     "binding_mismatch")

            run_id = _id("run_")
            now = _now()
            with self.lock, self.db:
                self.db.execute(
                    "INSERT INTO runs(id,conversation,client_run,input_hash,security_binding,turn,phase,"
                    "active_state,outcome,result,error,created,updated) "
                    "VALUES(?,?,?,?,?,NULL,'starting',NULL,NULL,'','',?,?)",
                    (run_id, conversation_id, client_run_id, input_hash,
                     json.dumps(security_binding, sort_keys=True,
                                separators=(",", ":")) if security_binding else None,
                     now, now))
                if replacement_from_id is not None:
                    self.db.execute(
                        "INSERT INTO conversation_replacements(previous,replacement,reason,created) "
                        "VALUES(?,?,?,?) ON CONFLICT(previous) DO UPDATE SET "
                        "replacement=excluded.replacement,reason=excluded.reason,created=excluded.created",
                        (replacement_from_id, conversation_id, replacement_reason, now))
            if replacement_reason is not None:
                with self.lock, self.db:
                    self._emit("conversation.security_replaced", conversation_id, run_id)
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
                raise AdapterFailure(_clean(str(exc), 300), 502,
                                     "runtime_unavailable") from None
            with self.lock, self.db:
                self.db.execute(
                    "UPDATE runs SET turn=?,phase='active',active_state='running',updated=? WHERE id=?",
                    (turn, _now(), run_id))
                self._emit("run.started", conversation_id, run_id)
                def _early_turn(params: dict) -> str | None:
                    raw = params.get("turn")
                    ident = raw.get("id") if isinstance(raw, dict) else None
                    return ident or params.get("turnId")
                early_notifications = [entry for entry in self._early_notifications
                                       if entry[1].get("threadId") == owned["thread"]
                                       and _early_turn(entry[1]) == turn]
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
        finally:
            admission.release()

    def find_run(self, conversation_id: str, client_run_id: str) -> dict:
        _owned = self._conversation(conversation_id)
        if self._is_invalidated(_owned):
            raise AdapterFailure("Conversation is unavailable", 404, "not_found")
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
                if self._is_invalidated(owned):
                    raise AdapterFailure("Conversation is unavailable", 404, "not_found")
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

    def _notification_token_usage(self, params: dict) -> None:
        """Capture the owned per-turn usage snapshot, replacing prior usage.

        Only ``tokenUsage.last`` for the exact owned thread/turn is stored;
        cumulative thread totals are never used. Duplicate notifications
        replace rather than add, wrong thread/turn notifications are ignored,
        and already-consumed usage survives terminal outcomes because the
        usage column is never cleared on completion/failure paths.
        """
        thread = params.get("threadId")
        if not isinstance(thread, str):
            raw_thread = params.get("thread")
            thread = raw_thread.get("id") if isinstance(raw_thread, dict) else None
        raw_turn = params.get("turn")
        turn = (raw_turn.get("id") if isinstance(raw_turn, dict) else None)
        if not isinstance(turn, str):
            turn = params.get("turnId")
        if not isinstance(turn, str):
            token_block = params.get("tokenUsage")
            if isinstance(token_block, dict):
                candidate = token_block.get("turnId")
                if isinstance(candidate, str):
                    turn = candidate
        if not isinstance(thread, str) or not isinstance(turn, str):
            return
        token_usage = params.get("tokenUsage")
        if token_usage is None:
            token_usage = params.get("token_usage")
        if token_usage is None:
            token_usage = params.get("usage")
        normalized = _extract_codex_last(token_usage)
        if normalized is None:
            return
        payload = json.dumps(normalized, sort_keys=True, separators=(",", ":"))
        with self.lock:
            row = self.db.execute(
                "SELECT runs.id FROM runs JOIN conversations "
                "ON runs.conversation=conversations.id WHERE conversations.thread=? "
                "AND runs.turn=?", (thread, turn)).fetchone()
            if row is None:
                starting = self.db.execute(
                    "SELECT 1 FROM runs JOIN conversations ON runs.conversation=conversations.id "
                    "WHERE conversations.thread=? AND runs.phase='starting'",
                    (thread,)).fetchone()
                if starting and len(self._early_notifications) < 100:
                    self._early_notifications.append(("thread/tokenUsage/updated", params))
                return
            with self.db:
                self.db.execute(
                    "UPDATE runs SET usage=?,updated=? WHERE id=?",
                    (payload, _now(), row["id"]))

    def _notification(self, method: str, params: dict) -> None:
        if not isinstance(params, dict):
            return
        if method == "thread/settings/updated":
            thread = params.get("threadId")
            if not isinstance(thread, str):
                return
            with self.lock:
                waiter = self._settings_updates.get(thread)
                if waiter is not None:
                    waiter["notification"] = params
                    waiter["event"].set()
            return
        if method == "thread/tokenUsage/updated":
            self._notification_token_usage(params)
            return
        thread = params.get("threadId")
        raw_turn = params.get("turn")
        turn = (raw_turn.get("id") if isinstance(raw_turn, dict) else None) or params.get("turnId")
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
            try:
                read_only = self._profile_definition(owned["profile"])[0][
                    "permissions"] == ":read-only"
            except AdapterFailure:
                read_only = False
            if read_only and method.endswith("requestApproval"):
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
                workspace_id = request.query_params.get("workspaceId")
                directory = request.query_params.get("directory")
                fresh_value = request.query_params.get("fresh", "0")
                if fresh_value not in ("0", "1"):
                    raise AdapterFailure("Invalid profile freshness option")
                result = await run_in_threadpool(
                    adapter.profiles, workspace_id, directory,
                    fresh=fresh_value == "1")
            elif path == "/v1/profiles" and request.method == "POST":
                result = await run_in_threadpool(adapter.save_profile, body)
            elif len(parts) == 3 and parts[:2] == ["v1", "profiles"] and request.method == "DELETE":
                result = await run_in_threadpool(adapter.delete_profile, parts[2])
            elif path == "/v1/conversations" and request.method == "POST":
                result = await run_in_threadpool(adapter.create_conversation, body)
            elif len(parts) == 3 and parts[:2] == ["v1", "conversations"] and request.method == "GET":
                result = await run_in_threadpool(adapter.conversation, parts[2])
            elif len(parts) == 4 and parts[:2] == ["v1", "conversations"] and parts[3] == "security" and request.method == "POST":
                binding = body.get("securityBinding")
                if not isinstance(binding, dict):
                    raise AdapterFailure("Invalid security rebind binding")
                result = await run_in_threadpool(adapter.rebind_conversation, parts[2], binding)
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
