"""Claude Code host adapter speaking Runtime Protocol v1 through the Claude Agent SDK.

Run this as a separate host process. The Bridge service contacts only its
token-authenticated HTTP surface; it never imports SDK types. Each run drives
one Claude Code CLI child through ``ClaudeSDKClient``, including accepted live
corrections and queued follow-ups, and resumes the adapter-created session for
later runs, so idle conversations survive
an adapter restart while active runs are marked interrupted and never replayed.

Claude Code runs as it does in the terminal: user, project and local settings,
CLAUDE.md, skills, sub-agents, plugins and MCP servers load from disk (the
layers are chosen with ``WB_CLAUDE_SETTING_SOURCES``). Security stays
Bridge-managed and is enforced here: every run carries the Bridge profile's
rules, and they give a closed built-in tool list, a ``PreToolUse`` hook that
evaluates every tool call
including sub-agent and MCP calls, and a ``can_use_tool`` callback that turns
``ask`` decisions into Bridge interactions. Settings can add behavior but
cannot widen the profile, and edits never reach ``.claude`` or ``.mcp.json``.
Hooks and MCP servers configured in a loaded layer run with the host user's
authority, as they do in the CLI.
"""
from __future__ import annotations

import asyncio
import fcntl
import importlib
import json
import logging
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from contextlib import nullcontext
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any


from .codex_rpc import sanitize_diagnostic
from .login_path import runtime_env_with_login_path, safe_summary
from . import security_profiles as profiles
from .security import redact


_LOG = logging.getLogger(__name__)
_REQUEST_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}\Z")

RUNTIME_ID = "claude"
DEFAULT_PORT = 8774
SDK_INSTALL_HINT = ("Claude Agent SDK is not installed; reinstall with "
                    "`uv tool install 'workspace-bridge[claude]'`")


def _ensure_operational_stderr_handler() -> None:
    """Attach a minimal stderr handler that survives Uvicorn reconfiguration.

    Only already-sanitized bounded summaries are ever emitted via ``_LOG``.
    Idempotent: never adds a duplicate stderr handler.
    """
    try:
        for existing in _LOG.handlers:
            if isinstance(existing, logging.StreamHandler):
                try:
                    if existing.stream is sys.stderr:
                        return
                except Exception:
                    return
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("%(message)s"))
        _LOG.addHandler(handler)
    except Exception:
        pass


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


# --------------------------------------------------------------------------
# Security profiles
# --------------------------------------------------------------------------

# Closed built-in tool allowlist. Claude Code extensions (sub-agents, skills,
# MCP servers, plugins) are available as in the CLI, but every tool call they
# make, including a sub-agent's, passes this same gate. Meta tools only
# orchestrate other tools or read instructions, so they are always allowed;
# MCP tools follow the profile's ``extensions`` mode; anything else (worktree,
# plan-mode and question tools) is never offered and is denied if it appears.
READ_TOOLS = ("Read", "Glob", "Grep")
EDIT_TOOLS = ("Edit", "Write", "NotebookEdit")
SHELL_TOOLS = ("Bash", "Monitor")
WEB_TOOLS = ("WebFetch", "WebSearch")
META_TOOLS = ("Agent", "Task", "Skill", "TodoWrite", "ToolSearch")
_SHELL_COMPANIONS = ("BashOutput", "KillShell", "TaskStop")
MCP_TOOL_PREFIX = "mcp__"
_MCP_TOOL_RE = re.compile(r"mcp__[A-Za-z0-9_.:-]{1,190}\Z")
_TOOL_CATEGORY = {**{name: "read" for name in READ_TOOLS},
                  **{name: "edits" for name in EDIT_TOOLS},
                  **{name: "shell" for name in SHELL_TOOLS + _SHELL_COMPANIONS},
                  **{name: "web" for name in WEB_TOOLS},
                  **{name: "meta" for name in META_TOOLS}}
# Claude Code configuration that runs with host authority: settings can carry
# hooks, and ``.mcp.json`` starts MCP servers. Tool edits never touch them.
_CONFIG_DIRS = frozenset({".claude"})
_CONFIG_FILES = frozenset({".mcp.json"})

SETTING_SOURCES = ("user", "project", "local")
DEFAULT_SETTING_SOURCES = SETTING_SOURCES
MAX_SHELL_COMMAND_CHARS = 16384
_PATH_INPUT_KEYS = ("file_path", "notebook_path", "path")
_GLOB_CHARS = frozenset("*?[]{}")


def parse_setting_sources(raw: Any) -> tuple[str, ...]:
    """Parse the Claude Code settings layers to load, e.g. ``user,project``.

    ``none`` loads no settings, CLAUDE.md, skills, agents, hooks or MCP
    configuration from disk. Order and duplicates are normalized.
    """
    if raw is None:
        return DEFAULT_SETTING_SOURCES
    if isinstance(raw, str):
        items = [part.strip().lower() for part in raw.split(",") if part.strip()]
    elif isinstance(raw, (list, tuple)):
        items = [str(part).strip().lower() for part in raw]
    else:
        raise AdapterFailure("Claude setting sources are invalid")
    if items == ["none"]:
        return ()
    if not items or any(item not in SETTING_SOURCES for item in items):
        raise AdapterFailure("Claude setting sources must be none or a list of "
                             "user, project and local")
    return tuple(source for source in SETTING_SOURCES if source in items)


def allowed_tools(rules: dict) -> list[str]:
    """Built-in tools offered to the model for one profile (closed list)."""
    tools = list(READ_TOOLS) + list(META_TOOLS)
    if rules["write_inside"] != "deny" or rules["write_outside"] != "deny":
        tools += EDIT_TOOLS
    if rules["shell"] != "deny" or any(rule["mode"] != "deny" for rule in rules["commands"]):
        tools += SHELL_TOOLS
    if rules["web"] != "deny":
        tools += WEB_TOOLS
    return tools


def _glob_prefix(pattern: str) -> str:
    parts = []
    for part in pattern.split("/"):
        if any(char in _GLOB_CHARS for char in part):
            break
        parts.append(part)
    return "/".join(parts) or "/"


def _claude_config(root_real: str, raw: Any) -> bool:
    """Whether a write would land in Claude Code configuration in the workspace."""
    resolved = profiles.resolve_path(root_real, raw)
    if resolved is None or not (resolved == root_real or resolved.startswith(root_real + os.sep)):
        return False
    parts = os.path.relpath(resolved, root_real).replace(os.sep, "/").split("/")
    return any(part in _CONFIG_DIRS for part in parts) or parts[-1] in _CONFIG_FILES


def decide_tool_use(rules: dict, cwd: str, tool_name: Any,
                    tool_input: Any) -> tuple[str, str]:
    """Evaluate one tool call against a run's profile rules, failing closed.

    Returns ``(decision, reason)`` where decision is ``allow``, ``ask`` or
    ``deny``. File tools are judged per path (inside or outside the
    workspace, protected paths first); shell commands by the profile's
    command rules and then its shell mode.
    """
    if isinstance(tool_name, str) and tool_name.startswith(MCP_TOOL_PREFIX):
        category = "extensions" if _MCP_TOOL_RE.fullmatch(tool_name) else None
    else:
        category = (_TOOL_CATEGORY.get(tool_name)
                    if isinstance(tool_name, str) else None)
    if category is None:
        return "deny", "Tool is not permitted by this security profile"
    if not isinstance(tool_input, dict):
        return "deny", "Tool input is invalid"
    if category == "meta":
        return "allow", ""
    if category in ("extensions", "web"):
        mode = rules[category]
        return mode, "" if mode != "deny" else "Tool is disabled by this security profile"
    try:
        root_real = os.path.realpath(cwd)
    except (OSError, ValueError):
        return "deny", "Workspace directory is unavailable"
    if category == "shell":
        if tool_name in ("Bash", "Monitor"):
            return profiles.command_mode(rules, tool_input.get("command"))
        # Output and stop companions act on a shell this run already started.
        mode = "allow" if rules["shell"] != "deny" or rules["commands"] else "deny"
        return mode, "" if mode == "allow" else "Shell is disabled by this security profile"
    write = category == "edits"
    paths: list[Any] = [tool_input[key] for key in _PATH_INPUT_KEYS if key in tool_input]
    if tool_name in ("Read", "Edit", "Write") and not paths:
        return "deny", "Tool path is required"
    if tool_name == "NotebookEdit" and "notebook_path" not in tool_input:
        return "deny", "Tool path is required"
    if tool_name == "Glob":
        pattern = tool_input.get("pattern")
        if (not isinstance(pattern, str) or not pattern or len(pattern) > 4096
                or "\x00" in pattern):
            return "deny", "Glob pattern is invalid"
        if ".." in pattern.replace("\\", "/").split("/"):
            return "deny", "Glob pattern must not traverse parents"
        if pattern.startswith("/") or pattern.startswith("~"):
            paths.append(_glob_prefix(pattern))
    if not paths:
        paths.append(".")
    decision, reason = "allow", ""
    for raw in paths:
        if write and _claude_config(root_real, raw):
            return "deny", "Claude Code configuration is protected"
        mode, why = profiles.path_mode(rules, root_real, raw, write=write)
        if mode == "deny":
            return "deny", why
        decision = profiles.stricter(decision, mode)
    return decision, reason


# --------------------------------------------------------------------------
# Usage normalization
# --------------------------------------------------------------------------

_MAX_SAFE_INTEGER = 9007199254740991
_USAGE_WINDOW_MINUTES = {"five_hour": 300, "seven_day": 10080}


def _counter(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return int(value) if 0 <= value <= _MAX_SAFE_INTEGER else None


def _normalize_claude_usage(raw: Any) -> dict | None:
    """Normalize a native Claude usage object to Runtime Protocol counters.

    Absent counters stay absent. A present-but-malformed recognized counter
    invalidates the whole snapshot. ``totalTokens`` is the arithmetic sum of
    the provider-reported input, cache and output counters, never an estimate.
    """
    if not isinstance(raw, dict):
        return None
    mapping = (("inputTokens", "input_tokens"),
               ("cachedInputTokens", "cache_read_input_tokens"),
               ("cacheWriteInputTokens", "cache_creation_input_tokens"),
               ("outputTokens", "output_tokens"))
    result: dict[str, int] = {}
    for field, source in mapping:
        if source not in raw or raw[source] is None:
            continue
        value = _counter(raw[source])
        if value is None:
            return None
        result[field] = value
    details = raw.get("output_tokens_details")
    if isinstance(details, dict) and details.get("thinking_tokens") is not None:
        thinking = _counter(details.get("thinking_tokens"))
        if thinking is None:
            return None
        result["reasoningOutputTokens"] = thinking
    if not result:
        return None
    if "inputTokens" in result and "outputTokens" in result:
        total = sum(result.get(key, 0) for key in (
            "inputTokens", "cachedInputTokens", "cacheWriteInputTokens", "outputTokens"))
        if total <= _MAX_SAFE_INTEGER:
            result["totalTokens"] = total
    return result


def _parse_stored_usage(stored: Any) -> dict | None:
    if not isinstance(stored, str) or not stored:
        return None
    try:
        value = json.loads(stored)
    except (TypeError, ValueError):
        return None
    if not isinstance(value, dict) or not value:
        return None
    fields = {"inputTokens", "cachedInputTokens", "cacheWriteInputTokens",
              "outputTokens", "reasoningOutputTokens", "totalTokens"}
    if set(value) - fields or any(_counter(item) is None for item in value.values()):
        return None
    return {key: int(item) for key, item in value.items()}


def _sum_usage(parts: list[dict]) -> dict | None:
    merged: dict[str, int] = {}
    for part in parts:
        for key, value in part.items():
            merged[key] = merged.get(key, 0) + value
    if any(value > _MAX_SAFE_INTEGER for value in merged.values()):
        return None
    return merged or None


# --------------------------------------------------------------------------
# Native message helpers
# --------------------------------------------------------------------------

_ACTIVITY_KIND = {"Bash": "command", "BashOutput": "command", "KillShell": "command",
                  "Monitor": "command",
                  "Edit": "file_change", "Write": "file_change",
                  "NotebookEdit": "file_change", "Glob": "search", "Grep": "search",
                  "WebSearch": "search"}
_EFFORTS = ("low", "medium", "high", "xhigh", "max")


def _tool_summary(name: str, tool_input: Any) -> str:
    """Bounded, redacted one-line description of a tool call."""
    if not isinstance(tool_input, dict):
        return _clean(name, 200)
    for key in ("command", "file_path", "notebook_path", "pattern", "url", "query", "path"):
        value = tool_input.get(key)
        if isinstance(value, str) and value:
            return _clean(f"{name}: {value}", 500)
    return _clean(name, 200)


def _result_preview(content: Any) -> str:
    if isinstance(content, str):
        return _clean(content, 1000)
    if isinstance(content, list):
        texts = [item.get("text") for item in content
                 if isinstance(item, dict) and isinstance(item.get("text"), str)]
        return _clean("\n".join(texts), 1000)
    return ""


def _claude_cli_version(executable: str, env: dict | None) -> str:
    try:
        result = subprocess.run([executable, "--version"], stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, encoding="utf-8",
                                errors="replace", timeout=5, check=False, env=env)
    except (OSError, subprocess.SubprocessError):
        return ""
    if result.returncode != 0:
        return ""
    match = re.search(r"(?<![\w.])v?(\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?)\b",
                      result.stdout or result.stderr)
    return match.group(1)[:80] if match else ""


class _RunState:
    """Process-local driver state for one live run."""

    def __init__(self, run_id: str, conversation_id: str, cwd: str, rules: dict,
                 expected_session: str):
        self.run_id = run_id
        self.conversation_id = conversation_id
        self.cwd = cwd
        self.rules = rules
        self.expected_session = expected_session
        self.client: Any = None
        self.task: Any = None
        self.started = threading.Event()
        self.start_error: str | None = None
        self.cancel_requested = False
        self.done = threading.Event()
        self.done_async = asyncio.Event()
        self.message_usage: dict[str, dict] = {}
        self.tool_names: dict[str, str] = {}
        self.input_texts: dict[str, str] = {}
        self.delivery_lock = threading.Lock()
        self.steering = False
        self.control_used = False
        self.response_index = 0
        self.in_response = False
        self.usage_parts: list[dict] = []


class ClaudeHostAdapter:
    def __init__(self, state: Path, projects_root: Path, *, sdk: Any = None,
                 binary: str | None = None, setting_sources: Any = None,
                 _runtime_env: dict | None | bool = False,
                 _login_path_resolver: Any | None = None):
        self.state = state.resolve()
        self.state.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.state, 0o700)
        lock_path = self.state / "adapter.lock"
        self._lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(self._lock_fd)
            raise RuntimeError("Another Claude host adapter owns this state") from None
        self._closed = False
        self.projects_root = projects_root.resolve(strict=True)
        if not self.projects_root.is_dir():
            raise ValueError("Projects root must be a directory")
        self._sdk = sdk
        self._binary = binary
        self.setting_sources = parse_setting_sources(setting_sources)
        self._plan_type: str | None = None
        self.db = sqlite3.connect(self.state / "claude-adapter.sqlite3",
                                  check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
          PRAGMA journal_mode=WAL;
          PRAGMA foreign_keys=ON;
          CREATE TABLE IF NOT EXISTS conversations (
            id TEXT PRIMARY KEY, session TEXT NOT NULL UNIQUE,
            workspace TEXT NOT NULL, cwd TEXT NOT NULL,
            profile TEXT NOT NULL, revision TEXT NOT NULL,
            started INTEGER NOT NULL DEFAULT 0, created TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS runs (
            id TEXT PRIMARY KEY, conversation TEXT NOT NULL REFERENCES conversations(id),
            client_run TEXT, input_hash TEXT, security_binding TEXT,
            phase TEXT NOT NULL, active_state TEXT, outcome TEXT,
            result TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '',
            usage TEXT, created TEXT NOT NULL, updated TEXT NOT NULL);
          CREATE UNIQUE INDEX IF NOT EXISTS ux_active_conversation
            ON runs(conversation) WHERE phase IN ('starting','active');
          CREATE UNIQUE INDEX IF NOT EXISTS ux_client_run
            ON runs(conversation,client_run) WHERE client_run IS NOT NULL;
          CREATE TABLE IF NOT EXISTS usage_windows (
            window TEXT PRIMARY KEY, utilization REAL NOT NULL,
            resets_at INTEGER NOT NULL, status TEXT, observed INTEGER NOT NULL);
          CREATE TABLE IF NOT EXISTS activities (
            id TEXT PRIMARY KEY, run TEXT NOT NULL REFERENCES runs(id),
            native_id TEXT NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL,
            summary TEXT NOT NULL, result TEXT NOT NULL DEFAULT '{}',
            created TEXT NOT NULL, updated TEXT NOT NULL,
            UNIQUE(run,native_id));
          CREATE TABLE IF NOT EXISTS input_messages (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            run TEXT NOT NULL REFERENCES runs(id),
            request_id TEXT NOT NULL, mode TEXT NOT NULL,
            input_hash TEXT NOT NULL, status TEXT NOT NULL,
            UNIQUE(run,request_id));
        """)
        os.chmod(self.state / "claude-adapter.sqlite3", 0o600)
        self.lock = threading.RLock()
        self.condition = threading.Condition(self.lock)
        self.instance_id = _id("claude_instance_")
        self.cursor = 0
        self.events_buffer: list[dict] = []
        self.pending: dict[str, dict] = {}
        self._admission: dict[str, threading.Lock] = {}
        self._states: dict[str, _RunState] = {}
        self._catalog_cache: tuple[float, list[dict]] | None = None
        self._native_version = ""
        self._native_version_checked = False
        # A prior process cannot prove it still owns a native run, so any run
        # left active becomes interrupted. Sessions stay resumable.
        with self.lock, self.db:
            self.db.execute("UPDATE input_messages SET status='discarded' WHERE status='queued'")
            self.db.execute(
                "UPDATE runs SET phase='terminal',active_state=NULL,outcome='interrupted',"
                "error='adapter_restarted',updated=? WHERE phase IN ('starting','active')",
                (_now(),))
        self._resolve_runtime_env(_runtime_env, _login_path_resolver)
        self._loop = asyncio.new_event_loop()
        ready = threading.Event()

        def _run_loop() -> None:
            asyncio.set_event_loop(self._loop)
            ready.set()
            self._loop.run_forever()

        self._loop_thread = threading.Thread(target=_run_loop, daemon=True,
                                             name="claude-adapter-loop")
        self._loop_thread.start()
        ready.wait()

    # -- environment -------------------------------------------------------

    def _resolve_runtime_env(self, runtime_env: Any, resolver: Any) -> None:
        """Resolve the terminal-equivalent PATH once; only PATH is imported."""
        failure = {"resolved": False, "path": None, "shell": "/bin/sh",
                   "shell_basename": "sh", "entry_count": 0, "code": "spawn_error"}
        if runtime_env is not False:
            self._runtime_env = dict(runtime_env) if isinstance(runtime_env, dict) else None
            self._login_path_result = failure
            return
        try:
            env, result = (runtime_env_with_login_path(None, _resolve=resolver)
                           if resolver is not None else runtime_env_with_login_path())
        except Exception:
            env, result = dict(os.environ), failure
        self._runtime_env = env
        self._login_path_result = result
        try:
            summary = safe_summary(result)
            _LOG.info("Claude login PATH %s (shell=%s entries=%s code=%s)",
                      "resolved" if summary.get("resolved") else "not-resolved",
                      summary.get("shell_basename"), summary.get("entry_count"),
                      summary.get("code"))
        except Exception:
            pass

    def _sdk_module(self) -> Any:
        if self._sdk is None:
            try:
                self._sdk = importlib.import_module("claude_agent_sdk")
            except ImportError:
                raise AdapterFailure(SDK_INSTALL_HINT, 503, "runtime_unavailable") from None
        return self._sdk

    def _cli_path(self) -> str | None:
        """Configured Claude Code executable, or None for the SDK's bundled CLI."""
        if not self._binary:
            return None
        if os.path.isabs(self._binary):
            return self._binary
        path = (self._runtime_env or {}).get("PATH") if isinstance(
            self._runtime_env, dict) else None
        found = shutil.which(self._binary, path=path)
        if not found:
            raise AdapterFailure("Configured Claude Code executable was not found", 503,
                                 "runtime_unavailable")
        return found

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

    # -- descriptor and catalog -------------------------------------------

    def _native_cli_version(self) -> str:
        with self.lock:
            if self._native_version_checked:
                return self._native_version
        version = ""
        sdk = self._sdk_module()
        if self._binary:
            executable = self._cli_path()
            version = _claude_cli_version(executable, self._runtime_env) if executable else ""
        else:
            try:
                module = importlib.import_module(sdk.__name__ + "._cli_version")
                version = str(getattr(module, "__cli_version__", ""))[:80]
            except ImportError:
                version = ""
        with self.lock:
            self._native_version = version or "unknown"
            self._native_version_checked = True
            return self._native_version

    def descriptor(self) -> dict:
        self._sdk_module()
        from .release import CLAUDE_ADAPTER_VERSION, claude_release
        return {"protocol": {"major": 1, "minor": 0},
                "runtime": {"id": RUNTIME_ID, "displayName": "Claude Code",
                            "adapterVersion": CLAUDE_ADAPTER_VERSION,
                            "nativeVersion": self._native_cli_version(),
                            "instanceId": self.instance_id},
                "features": {"models": 1, "conversations": 1, "runs": 1,
                             "activities": 1, "interactions": 1, "events": 1,
                             "usageLimits": 1,
                             "steering": 1, "queueing": 1, "inputControls": 1},
                "release": claude_release()}

    def usage_limits(self) -> dict:
        """Last observed Claude subscription windows, normalized at this boundary.

        Claude Code has no on-demand quota call. The CLI reports the five-hour
        and seven-day windows in a rate-limit event on every run, so this is
        the most recent snapshot, not a live read. Windows that already reset
        are dropped rather than shown as stale usage, and a window that was
        never observed is never invented.
        """
        now = int(time.time())
        with self.lock:
            rows = self.db.execute(
                "SELECT window,utilization,resets_at,status FROM usage_windows "
                "ORDER BY window").fetchall()
            plan = self._plan_type
        windows = []
        rejected: str | None = None
        for row in rows:
            minutes = _USAGE_WINDOW_MINUTES.get(row["window"])
            if minutes is None or row["resets_at"] <= now:
                continue
            used = int(round(max(0.0, min(10.0, row["utilization"])) * 100))
            windows.append({"usedPercent": used, "windowDurationMins": minutes,
                            "resetsAt": row["resets_at"]})
            if row["status"] == "rejected":
                rejected = rejected or row["window"]
        windows.sort(key=lambda item: item["windowDurationMins"])
        if not windows:
            return {"available": False, "ordinaryUsageAllowed": None, "buckets": []}
        return {"available": True,
                "ordinaryUsageAllowed": False if rejected else None,
                "buckets": [{"limitId": "claude", "limitName": "Claude Code",
                             "planType": plan, "rateLimitReachedType": rejected,
                             "spendControlReached": None, "credits": None,
                             "individualLimit": None, "windows": windows}]}

    def _record_rate_limit(self, message: Any) -> None:
        info = getattr(message, "rate_limit_info", None)
        raw = getattr(info, "raw", None)
        if not isinstance(raw, dict):
            raw = {}
        found: dict[str, tuple[Any, Any]] = {}
        unified = raw.get("unifiedWindows")
        if isinstance(unified, dict):
            for key, value in unified.items():
                if isinstance(value, dict) and key in _USAGE_WINDOW_MINUTES:
                    found[key] = (value.get("utilization"), value.get("resetsAt"))
        kind = getattr(info, "rate_limit_type", None)
        if kind in _USAGE_WINDOW_MINUTES and kind not in found:
            found[kind] = (getattr(info, "utilization", None),
                           getattr(info, "resets_at", None))
        status = getattr(info, "status", None)
        now = int(time.time())
        with self.lock, self.db:
            for key, (utilization, resets_at) in found.items():
                if (isinstance(utilization, bool) or not isinstance(utilization, (int, float))
                        or isinstance(resets_at, bool) or not isinstance(resets_at, int)
                        or not 0 <= utilization <= 10 or not 0 < resets_at < _MAX_SAFE_INTEGER):
                    continue
                self.db.execute(
                    "INSERT INTO usage_windows(window,utilization,resets_at,status,observed) "
                    "VALUES(?,?,?,?,?) ON CONFLICT(window) DO UPDATE SET "
                    "utilization=excluded.utilization,resets_at=excluded.resets_at,"
                    "status=excluded.status,observed=excluded.observed",
                    (key, float(utilization), resets_at,
                     status if key == kind and status in ("allowed", "allowed_warning",
                                                          "rejected") else None, now))

    def _client_options(self, **overrides: Any) -> Any:
        sdk = self._sdk_module()
        env = {}
        if isinstance(self._runtime_env, dict) and self._runtime_env.get("PATH"):
            env["PATH"] = self._runtime_env["PATH"]
        kwargs: dict[str, Any] = {"setting_sources": list(self.setting_sources),
                                  "permission_mode": "default", "env": env}
        cli_path = self._cli_path()
        if cli_path:
            kwargs["cli_path"] = cli_path
        kwargs.update(overrides)
        try:
            return sdk.ClaudeAgentOptions(**kwargs)
        except TypeError:
            raise AdapterFailure("Claude Agent SDK is too old for this adapter", 503,
                                 "runtime_unavailable") from None

    def _live_catalog(self) -> list[dict]:
        sdk = self._sdk_module()

        async def probe() -> list[dict]:
            # The model probe runs in the projects root, not a workspace, so it
            # never loads workspace-controlled settings, hooks or MCP servers.
            client = sdk.ClaudeSDKClient(self._client_options(
                cwd=str(self.projects_root), tools=[], strict_mcp_config=True,
                setting_sources=[s for s in self.setting_sources if s == "user"]))
            try:
                await asyncio.wait_for(client.connect(), 45)
                info = await asyncio.wait_for(client.get_server_info(), 15)
            finally:
                try:
                    await asyncio.wait_for(client.disconnect(), 10)
                except Exception:
                    pass
            account = (info or {}).get("account")
            plan = account.get("subscriptionType") if isinstance(account, dict) else None
            if isinstance(plan, str) and plan and len(plan) <= 60:
                with self.lock:
                    self._plan_type = plan
            rows = (info or {}).get("models")
            return rows if isinstance(rows, list) else []

        try:
            future = asyncio.run_coroutine_threadsafe(probe(), self._loop)
            rows = future.result(timeout=75)
        except AdapterFailure:
            raise
        except Exception:
            raise AdapterFailure("Claude Code model list is unavailable", 502,
                                 "runtime_unavailable") from None
        models = []
        for model in rows[:200]:
            if not isinstance(model, dict) or not isinstance(model.get("value"), str):
                continue
            selector = model["value"]
            if not selector or len(selector) > 260:
                continue
            raw_efforts = model.get("supportedEffortLevels")
            efforts = [effort for effort in (raw_efforts if isinstance(raw_efforts, list)
                                             else []) if effort in _EFFORTS]
            models.append({"selector": selector,
                           "displayName": _clean(model.get("displayName") or selector, 120),
                           "inputModalities": ["text"],
                           "reasoningOptions": efforts,
                           "defaultReasoningEffort": None,
                           "default": selector == "default"})
        return models

    def _catalog(self) -> list[dict]:
        with self.lock:
            cached = self._catalog_cache
        if cached is not None and time.monotonic() - cached[0] < 300:
            return cached[1]
        try:
            models = self._live_catalog()
        except AdapterFailure:
            if cached is not None:
                return cached[1]
            raise
        with self.lock:
            self._catalog_cache = (time.monotonic(), models)
        return models

    def models(self, workspace_id: str | None = None) -> dict:
        return {"models": self._catalog()}

    # -- conversations -----------------------------------------------------

    def _cwd(self, requested: Any) -> str:
        if not isinstance(requested, str) or not requested:
            raise AdapterFailure("Workspace directory is required")
        try:
            cwd = Path(requested).resolve(strict=True)
            if not cwd.is_dir() or not cwd.is_relative_to(self.projects_root):
                raise ValueError()
            return str(cwd)
        except (OSError, RuntimeError, ValueError):
            raise AdapterFailure(
                "Workspace directory is outside the configured projects root") from None

    def _conversation(self, conversation_id: str) -> dict:
        with self.lock:
            row = self.db.execute("SELECT * FROM conversations WHERE id=?",
                                  (conversation_id,)).fetchone()
        if row is None:
            raise AdapterFailure("Conversation not found", 404, "not_found")
        return dict(row)

    def _is_busy(self, conversation_id: str) -> bool:
        with self.lock:
            return self.db.execute(
                "SELECT 1 FROM runs WHERE conversation=? AND phase IN ('starting','active')",
                (conversation_id,)).fetchone() is not None

    def _session_present(self, owned: dict) -> bool:
        """Whether Claude's own store currently holds this adapter-created session."""
        try:
            info = self._sdk_module().get_session_info(owned["session"],
                                                       directory=owned["cwd"])
        except AdapterFailure:
            raise
        except Exception:
            return False
        return info is not None

    def _session_exists(self, owned: dict) -> bool:
        """Prove a started session still exists in Claude's own store.

        A conversation whose first run has not created its session yet is
        trivially valid. The stored flag only records that a session was seen;
        the live lookup is the authority, because a run turns terminal slightly
        before its owner records the flag.
        """
        return self._session_present(owned) or not owned["started"]

    @staticmethod
    def _security(body: dict) -> tuple[str, str, dict]:
        """The profile a conversation or run carries; every run names one."""
        try:
            return profiles.from_wire(body.get("security"))
        except Exception as exc:  # BridgeError from validation
            raise AdapterFailure(_clean(str(exc), 200) or "Security profile is invalid",
                                 400, "invalid_profile") from None

    @staticmethod
    def _binding(owned: dict) -> dict:
        return {"source": "profile", "profile": {"id": owned["profile"],
                                                 "revision": owned["revision"]}}

    def create_conversation(self, body: dict) -> dict:
        workspace = body.get("workspaceId")
        if not isinstance(workspace, str) or not workspace or len(workspace) > 100:
            raise AdapterFailure("Invalid workspace id")
        cwd = self._cwd(body.get("directory"))
        profile_id, revision, _rules = self._security(body)
        self._sdk_module()
        conversation_id = _id("conv_")
        session = str(uuid.uuid4())
        with self.lock, self.db:
            self.db.execute(
                "INSERT INTO conversations(id,session,workspace,cwd,profile,revision,started,created) "
                "VALUES(?,?,?,?,?,?,0,?)",
                (conversation_id, session, workspace, cwd, profile_id, revision, _now()))
            self._emit("conversation.created", conversation_id)
        return {"id": conversation_id, "runtime": RUNTIME_ID, "nativeId": session,
                "workspaceId": workspace,
                "securityBinding": {"source": "profile", "profile": {
                    "id": profile_id, "revision": revision}},
                "status": "idle"}

    def conversation(self, conversation_id: str) -> dict:
        owned = self._conversation(conversation_id)
        if not self._session_exists(owned):
            raise AdapterFailure("Conversation is unavailable", 404, "not_found")
        binding = self._binding(owned)
        return {"id": owned["id"], "runtime": RUNTIME_ID, "nativeId": owned["session"],
                "workspaceId": owned["workspace"],
                "status": "active" if self._is_busy(owned["id"]) else "idle",
                "securityBinding": binding}

    # -- runs --------------------------------------------------------------

    def _run(self, run_id: str) -> dict:
        with self.lock:
            row = self.db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise AdapterFailure("Run not found", 404, "not_found")
        return dict(row)

    def _run_public(self, run: dict) -> dict:
        result = {"id": run["id"], "conversationId": run["conversation"],
                  "clientRunId": run.get("client_run"), "nativeId": run["id"],
                  "phase": run["phase"], "activeState": run["active_state"],
                  "outcome": run["outcome"], "result": _clean(run["result"], 20000),
                  "error": _clean(run["error"], 300),
                  "createdAt": run["created"], "updatedAt": run["updated"]}
        try:
            binding = json.loads(run.get("security_binding") or "null")
        except (TypeError, ValueError):
            binding = None
        if isinstance(binding, dict):
            result["securityBinding"] = binding
        usage = _parse_stored_usage(run.get("usage"))
        if usage is not None:
            result["usage"] = usage
        with self.lock:
            rows = self.db.execute(
                "SELECT request_id,mode,status FROM input_messages WHERE run=? "
                "ORDER BY sequence LIMIT 50", (run["id"],)).fetchall()
        if rows:
            result["inputMessages"] = [{"requestId": row["request_id"],
                                        "mode": row["mode"], "status": row["status"]}
                                       for row in rows]
        return result

    @staticmethod
    def _input(items: Any) -> str:
        if not isinstance(items, list) or not items or len(items) > 10:
            raise AdapterFailure("Input requires 1..10 typed items")
        texts = []
        for item in items:
            if not isinstance(item, dict) or item.get("type") != "text":
                raise AdapterFailure("Only text input is supported by this adapter version",
                                     501, "unsupported_input")
            text = item.get("text")
            if not isinstance(text, str) or not text or len(text) > 60000:
                raise AdapterFailure("Invalid text input")
            texts.append(text)
        return "\n\n".join(texts)

    def _validate_model(self, model: Any, effort: Any) -> tuple[str | None, str | None]:
        if model is not None and (not isinstance(model, str) or not model
                                  or len(model) > 260):
            raise AdapterFailure("Invalid model selector")
        if effort is not None and (not isinstance(effort, str) or effort not in _EFFORTS):
            raise AdapterFailure("Invalid reasoning effort")
        if effort is not None:
            catalog = self._catalog()
            selector = model if model is not None else "default"
            row = next((item for item in catalog if item["selector"] == selector), None)
            if row is None or effort not in row["reasoningOptions"]:
                raise AdapterFailure("Reasoning effort is not supported by this model",
                                     400, "unsupported_reasoning")
        # "default" is the CLI's own default model: send no selector.
        return (None if model in (None, "default") else model), effort

    def start_run(self, conversation_id: str, body: dict) -> dict:
        prompt = self._input(body.get("input"))
        client_run_id = body.get("clientRunId")
        if client_run_id is not None and (not isinstance(client_run_id, str)
                                          or not client_run_id or len(client_run_id) > 200):
            raise AdapterFailure("Invalid client run id")
        input_hash = sha256(json.dumps({"input": body.get("input"),
            "model": body.get("model"), "reasoning": body.get("reasoning")},
            sort_keys=True).encode()).hexdigest()
        model, effort = self._validate_model(body.get("model"), body.get("reasoning"))
        profile_id, revision, rules = self._security(body)
        owned = self._conversation(conversation_id)
        admission = self._admission.setdefault(owned["id"], threading.Lock())
        admission.acquire()
        try:
            with self.lock:
                if client_run_id:
                    previous = self.db.execute(
                        "SELECT * FROM runs WHERE conversation=? AND client_run=?",
                        (owned["id"], client_run_id)).fetchone()
                    if previous is not None:
                        if previous["input_hash"] != input_hash:
                            raise AdapterFailure("Client run id has different input", 409,
                                                 "idempotency_conflict")
                        return self._run_public(dict(previous))
            if self._is_busy(owned["id"]):
                raise AdapterFailure("Conversation is busy", 409, "conversation_busy")
            if not self._session_exists(owned):
                raise AdapterFailure("Conversation is unavailable", 404, "not_found")
            self._sdk_module()
            owned["resume"] = bool(owned["started"] or self._session_present(owned))
            run_id = _id("run_")
            now = _now()
            owned["profile"], owned["revision"] = profile_id, revision
            binding = json.dumps(self._binding(owned), sort_keys=True,
                                 separators=(",", ":"))
            with self.lock, self.db:
                self.db.execute("UPDATE conversations SET profile=?,revision=? WHERE id=?",
                                (profile_id, revision, owned["id"]))
                self.db.execute(
                    "INSERT INTO runs(id,conversation,client_run,input_hash,security_binding,"
                    "phase,active_state,outcome,result,error,usage,created,updated) "
                    "VALUES(?,?,?,?,?,'starting',NULL,NULL,'','',NULL,?,?)",
                    (run_id, owned["id"], client_run_id, input_hash, binding, now, now))
            state = _RunState(run_id, owned["id"], owned["cwd"], rules, owned["session"])
            try:
                options = self._run_options(state, owned, model, effort)
            except AdapterFailure:
                self._finalize(run_id, "failed", "options_unavailable")
                raise
            with self.lock:
                self._states[run_id] = state
            state.task = asyncio.run_coroutine_threadsafe(
                self._drive(state, owned, options, prompt), self._loop)
            if not state.started.wait(90):
                state.start_error = state.start_error or "Claude Code did not start in time"
                self._cancel_driver(state)
            if state.start_error is not None:
                self._finalize(run_id, "failed", state.start_error)
                raise AdapterFailure(_clean(state.start_error, 300), 502,
                                     "runtime_unavailable")
            return self._run_public(self._run(run_id))
        finally:
            admission.release()

    def _run_options(self, state: _RunState, owned: dict, model: str | None,
                     effort: str | None) -> Any:
        sdk = self._sdk_module()
        loop = self._loop

        async def pre_tool_use(hook_input: Any, _tool_use_id: Any, _context: Any) -> dict:
            try:
                name = hook_input.get("tool_name")
                decision, reason = decide_tool_use(
                    state.rules, state.cwd, name, hook_input.get("tool_input"))
            except Exception:
                decision, reason = "deny", "Tool call could not be evaluated"
            output: dict[str, Any] = {"hookEventName": "PreToolUse"}
            if decision == "allow":
                output["permissionDecision"] = "allow"
            elif decision == "ask":
                output["permissionDecision"] = "ask"
                output["permissionDecisionReason"] = "Awaiting Bridge approval"
            else:
                output["permissionDecision"] = "deny"
                output["permissionDecisionReason"] = _clean(reason, 200)
            return {"hookSpecificOutput": output}

        async def can_use_tool(tool_name: str, tool_input: dict, context: Any) -> Any:
            try:
                decision, reason = decide_tool_use(state.rules, state.cwd, tool_name,
                                                   tool_input)
            except Exception:
                decision, reason = "deny", "Tool call could not be evaluated"
            if decision == "deny":
                return sdk.PermissionResultDeny(message=_clean(reason, 200))
            if decision == "allow":
                return sdk.PermissionResultAllow()
            return await self._ask(state, tool_name, tool_input, context, loop)

        kwargs: dict[str, Any] = {
            "cwd": state.cwd, "tools": allowed_tools(state.rules),
            "can_use_tool": can_use_tool, "verbatim_prompts": True,
            # The Claude Code system prompt and its CLAUDE.md, skill and agent
            # loading, as in the CLI. MCP servers start only when the profile
            # permits extensions; the hook gates every call either way.
            "system_prompt": {"type": "preset", "preset": "claude_code"},
            "strict_mcp_config": state.rules["extensions"] == "deny",
            "settings": json.dumps({"permissions": {
                "disableBypassPermissionsMode": "disable"}}),
            "hooks": {"PreToolUse": [sdk.HookMatcher(matcher=None, hooks=[pre_tool_use])]},
            "stderr": self._stderr_sink,
        }
        if owned.get("resume"):
            kwargs["resume"] = owned["session"]
        else:
            kwargs["session_id"] = owned["session"]
        if model is not None:
            kwargs["model"] = model
        if effort is not None:
            kwargs["effort"] = effort
        return self._client_options(**kwargs)

    @staticmethod
    def _stderr_sink(line: str) -> None:
        # Native stderr can echo user input. It is not a safe diagnostic channel.
        _LOG.debug("claude-cli: diagnostic suppressed")

    async def _drive(self, state: _RunState, owned: dict, options: Any, prompt: str) -> None:
        sdk = self._sdk_module()
        client = None
        try:
            client = sdk.ClaudeSDKClient(options)
            state.client = client
            await client.connect()
            await client.query(prompt)
            with self.lock:
                state.response_index += 1
                state.in_response = True
            while True:
                with self.lock:
                    state.message_usage.clear()
                native_result = None
                async for message in client.receive_response():
                    if type(message).__name__ == "ResultMessage":
                        native_result = message
                    elif native_result is None:
                        self._on_message(state, message)
                with self.lock:
                    state.in_response = False
                if native_result is None:
                    self._finalize(state.run_id, "interrupted", "native_stream_ended")
                    break
                outcome, error, usage, result = self._result_state(state, native_result)
                if usage is not None:
                    state.usage_parts.append(usage)
                total_usage = _sum_usage(state.usage_parts)
                with self.lock, self.db:
                    self.db.execute(
                        "UPDATE runs SET result=?,usage=?,updated=? WHERE id=? AND phase='active'",
                        (_clean(result, 20000) if result else self._run(state.run_id)["result"],
                         json.dumps(total_usage, sort_keys=True) if total_usage else None,
                         _now(), state.run_id))
                    steering = state.steering
                if state.cancel_requested:
                    self._finalize(state.run_id, "cancelled", usage=total_usage)
                    break
                if outcome != "succeeded" and not (outcome == "interrupted" and steering):
                    self._finalize(state.run_id, outcome, error, usage=total_usage)
                    break
                if outcome == "interrupted":
                    self._clear_interactions(state.run_id)
                with state.delivery_lock:
                    with self.lock:
                        next_row = self.db.execute(
                            "SELECT request_id,mode FROM input_messages "
                            "WHERE run=? AND status='queued' "
                            "ORDER BY CASE mode WHEN 'steer' THEN 0 ELSE 1 END,sequence LIMIT 1",
                            (state.run_id,)).fetchone()
                        if next_row is None:
                            self._finalize(state.run_id, outcome, error, usage=total_usage)
                            break
                        request_id = next_row["request_id"]
                        live = self.db.execute("SELECT phase FROM runs WHERE id=?",
                                               (state.run_id,)).fetchone()
                        if state.cancel_requested or live is None or live["phase"] == "terminal":
                            break
                        next_prompt = state.input_texts.pop(request_id, None)
                        if next_prompt is None:
                            self._finalize(state.run_id, "failed", "queued_input_unavailable",
                                           usage=total_usage)
                            break
                        with self.db:
                            # A crash or ambiguous query error must never cause a replay.
                            self.db.execute(
                                "UPDATE input_messages SET status='unknown' "
                                "WHERE run=? AND request_id=?", (state.run_id, request_id))
                        state.response_index += 1
                        state.in_response = True
                    try:
                        await client.query(next_prompt)
                    except Exception:  # noqa: BLE001 - delivery is ambiguous; never retry
                        self._control_activity(state, request_id, next_row["mode"], "failed")
                        self._finalize(state.run_id, "failed", "native_control_delivery_failed",
                                       usage=total_usage)
                        break
                    with self.lock, self.db:
                        self.db.execute(
                            "UPDATE input_messages SET status='delivered' "
                            "WHERE run=? AND request_id=?", (state.run_id, request_id))
                        if next_row["mode"] == "steer":
                            state.steering = False
                self._control_activity(state, request_id, next_row["mode"], "completed")
        except asyncio.CancelledError:
            self._finalize(state.run_id,
                           "cancelled" if state.cancel_requested else "interrupted",
                           "" if state.cancel_requested else "native_run_cancelled")
        except Exception as exc:  # noqa: BLE001 - every native failure is bounded below
            detail = ("native_error" if state.control_used else
                      sanitize_diagnostic(str(exc) or type(exc).__name__, limit=300))
            _ensure_operational_stderr_handler()
            _LOG.warning("Claude run failed: %s", detail)
            if not state.started.is_set():
                state.start_error = detail or "Claude Code failed to start"
            else:
                self._finalize(state.run_id, "failed", detail or "native_error")
        finally:
            state.started.set()
            if client is not None:
                try:
                    await asyncio.wait_for(client.disconnect(), 10)
                except BaseException:  # noqa: BLE001 - disconnect is best effort
                    pass
            self._finalize(state.run_id, "interrupted", "native_stream_ended")
            self._release_run(state)
            state.done.set()
            state.done_async.set()

    def _release_run(self, state: _RunState) -> None:
        with self.lock:
            self._states.pop(state.run_id, None)
            state.input_texts.clear()
            for interaction_id, entry in list(self.pending.items()):
                if entry["public"]["runId"] == state.run_id:
                    self.pending.pop(interaction_id, None)
                    self._settle(entry, deny=True, interrupt=True)
        owned = self._conversation_or_none(state.conversation_id)
        if owned is not None and not owned["started"]:
            try:
                info = self._sdk_module().get_session_info(owned["session"],
                                                           directory=owned["cwd"])
            except Exception:
                info = None
            if info is not None:
                with self.lock, self.db:
                    self.db.execute("UPDATE conversations SET started=1 WHERE id=?",
                                    (owned["id"],))

    def _conversation_or_none(self, conversation_id: str) -> dict | None:
        try:
            return self._conversation(conversation_id)
        except AdapterFailure:
            return None

    def _settle(self, entry: dict, *, deny: bool, interrupt: bool = False) -> None:
        sdk = self._sdk_module()
        result = (sdk.PermissionResultDeny(message="Run ended before approval",
                                           interrupt=interrupt)
                  if deny else sdk.PermissionResultAllow())
        future, loop = entry["future"], entry["loop"]

        def _set() -> None:
            if not future.done():
                future.set_result(result)

        loop.call_soon_threadsafe(_set)

    def _finalize(self, run_id: str, outcome: str, error: str = "",
                  *, usage: dict | None = None, result: str | None = None) -> bool:
        with self.lock:
            row = self.db.execute("SELECT conversation,phase FROM runs WHERE id=?",
                                  (run_id,)).fetchone()
            if row is None or row["phase"] == "terminal":
                return False
            discarded = self.db.execute(
                "SELECT request_id,mode FROM input_messages WHERE run=? AND status='queued'",
                (run_id,)).fetchall()
            with self.db:
                self.db.execute(
                    "UPDATE runs SET phase='terminal',active_state=NULL,outcome=?,error=?,"
                    "updated=? WHERE id=? AND phase IN ('starting','active')",
                    (outcome, _clean(error, 300), _now(), run_id))
                if usage is not None:
                    self.db.execute("UPDATE runs SET usage=? WHERE id=?",
                                    (json.dumps(usage, sort_keys=True), run_id))
                if result is not None:
                    self.db.execute("UPDATE runs SET result=? WHERE id=?",
                                    (_clean(result, 20000), run_id))
                self.db.execute(
                    "UPDATE input_messages SET status='discarded' WHERE run=? AND status='queued'",
                    (run_id,))
            state = self._states.get(run_id)
            if state is not None:
                state.input_texts.clear()
                for entry in discarded:
                    self._control_activity(state, entry["request_id"], entry["mode"],
                                           "failed")
            for interaction_id, entry in list(self.pending.items()):
                if entry["public"]["runId"] == run_id:
                    self.pending.pop(interaction_id, None)
                    self._settle(entry, deny=True, interrupt=True)
            self._emit("run.completed", row["conversation"], run_id)
        return True

    # -- native message handling ------------------------------------------

    def _on_message(self, state: _RunState, message: Any) -> None:
        kind = type(message).__name__
        run_id = state.run_id
        if kind == "SystemMessage":
            if getattr(message, "subtype", "") != "init":
                return
            data = getattr(message, "data", None)
            session = data.get("session_id") if isinstance(data, dict) else None
            if session != state.expected_session:
                state.start_error = "Claude session binding was not confirmed"
                state.started.set()
                self._cancel_driver(state)
                return
            with self.lock, self.db:
                self.db.execute(
                    "UPDATE runs SET phase='active',active_state='running',updated=? "
                    "WHERE id=? AND phase='starting'", (_now(), run_id))
                self._emit("run.started", state.conversation_id, run_id)
            state.started.set()
        elif kind == "AssistantMessage":
            if getattr(message, "parent_tool_use_id", None):
                return
            self._on_assistant(state, message)
        elif kind == "UserMessage":
            self._on_user(state, message)
        elif kind == "ResultMessage":
            return  # The driver owns each response boundary.
        elif kind == "RateLimitEvent":
            try:
                self._record_rate_limit(message)
            except Exception:  # noqa: BLE001 - quota telemetry must never fail a run
                _LOG.debug("Claude rate-limit event ignored")

    def _record_usage(self, state: _RunState, message: Any) -> None:
        message_id = getattr(message, "message_id", None)
        usage = _normalize_claude_usage(getattr(message, "usage", None))
        if not isinstance(message_id, str) or usage is None:
            return
        state.message_usage[message_id] = usage
        total = _sum_usage(state.usage_parts + list(state.message_usage.values()))
        if total is not None:
            with self.lock, self.db:
                self.db.execute("UPDATE runs SET usage=?,updated=? WHERE id=? AND phase='active'",
                                (json.dumps(total, sort_keys=True), _now(), state.run_id))

    def _on_assistant(self, state: _RunState, message: Any) -> None:
        self._record_usage(state, message)
        for block in getattr(message, "content", None) or []:
            block_kind = type(block).__name__
            if block_kind == "TextBlock":
                text = getattr(block, "text", "")
                if isinstance(text, str) and text:
                    with self.lock, self.db:
                        self.db.execute(
                            "UPDATE runs SET result=?,updated=? WHERE id=? AND phase='active'",
                            (_clean(text, 20000), _now(), state.run_id))
            elif block_kind == "ToolUseBlock":
                native_id = getattr(block, "id", None)
                name = getattr(block, "name", None)
                if not isinstance(native_id, str) or not isinstance(name, str):
                    continue
                state.tool_names[native_id] = name
                self._upsert_activity(state, native_id[:200],
                                      _ACTIVITY_KIND.get(name, "tool_call"), "running",
                                      _tool_summary(name, getattr(block, "input", None)), {})

    def _on_user(self, state: _RunState, message: Any) -> None:
        content = getattr(message, "content", None)
        if not isinstance(content, list):
            return
        for block in content:
            if type(block).__name__ != "ToolResultBlock":
                continue
            native_id = getattr(block, "tool_use_id", None)
            if not isinstance(native_id, str) or native_id not in state.tool_names:
                continue
            name = state.tool_names[native_id]
            failed = bool(getattr(block, "is_error", False))
            summary = {}
            preview = _result_preview(getattr(block, "content", None))
            if preview:
                summary["outputPreview"] = preview
            self._upsert_activity(state, native_id[:200], _ACTIVITY_KIND.get(name, "tool_call"),
                                  "failed" if failed else "completed", None, summary)

    def _upsert_activity(self, state: _RunState, native_id: str, kind: str, status: str,
                         summary: str | None, result: dict) -> None:
        activity_id = "act_" + sha256((state.run_id + ":" + native_id).encode()).hexdigest()[:24]
        now = _now()
        with self.lock, self.db:
            self.db.execute(
                "INSERT INTO activities(id,run,native_id,kind,status,summary,result,created,updated) "
                "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(run,native_id) DO UPDATE SET "
                "status=CASE WHEN activities.status IN ('completed','failed') "
                "THEN activities.status ELSE excluded.status END,"
                "summary=CASE WHEN ?='' THEN activities.summary ELSE excluded.summary END,"
                "result=CASE WHEN excluded.status='running' THEN activities.result "
                "ELSE excluded.result END,updated=excluded.updated",
                (activity_id, state.run_id, native_id, kind, status, summary or "",
                 json.dumps(result), now, now, summary or ""))
            self._emit("activity." + status, state.conversation_id, state.run_id, activity_id)

    def _result_state(self, state: _RunState, message: Any) -> tuple[str, str, dict | None, str | None]:
        usage = _normalize_claude_usage(getattr(message, "usage", None))
        if usage is None:
            usage = _sum_usage(list(state.message_usage.values()))
        terminal_reason = getattr(message, "terminal_reason", None)
        aborted = terminal_reason in ("aborted_streaming", "aborted_tools")
        failed = bool(getattr(message, "is_error", False)) or str(
            getattr(message, "subtype", "")).startswith("error")
        if state.cancel_requested:
            outcome, error = "cancelled", ""
        elif aborted:
            outcome, error = "interrupted", "native_run_aborted"
        elif failed:
            errors = getattr(message, "errors", None)
            detail = ("; ".join(item for item in errors if isinstance(item, str))
                      if isinstance(errors, list) else "")
            status = getattr(message, "api_error_status", None)
            outcome = "failed"
            error = ("native_error" if state.control_used else sanitize_diagnostic(
                detail or getattr(message, "result", "") or
                (f"api_error_{status}" if status else "native_error"), limit=300))
        else:
            outcome, error = "succeeded", ""
        text = getattr(message, "result", None)
        return (outcome, error, usage,
                text if isinstance(text, str) and text and
                not (state.control_used and outcome == "failed") else None)

    # -- lifecycle control -------------------------------------------------

    def _cancel_driver(self, state: _RunState) -> None:
        task = state.task
        if task is not None:
            task.cancel()

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
        if run["phase"] == "active":
            with self.lock:
                live = run_id in self._states
            if not live:
                self._finalize(run_id, "interrupted", "native_run_lost")
                run = self._run(run_id)
        return self._run_public(run)

    def cancel(self, run_id: str) -> dict:
        run = self._run(run_id)
        if run["phase"] == "terminal":
            return self._run_public(run)
        with self.lock:
            state = self._states.get(run_id)
        if state is None:
            self._finalize(run_id, "interrupted", "native_run_lost")
            return self._run_public(self._run(run_id))
        with state.delivery_lock:
            state.cancel_requested = True
        with self.lock:
            for interaction_id, entry in list(self.pending.items()):
                if entry["public"]["runId"] == run_id:
                    self.pending.pop(interaction_id, None)
                    self._settle(entry, deny=True, interrupt=True)

        async def interrupt() -> None:
            client = state.client
            try:
                if client is not None:
                    await asyncio.wait_for(client.interrupt(), 10)
            except Exception:  # noqa: BLE001 - the watchdog below still ends the run
                pass
            try:
                await asyncio.wait_for(state.done_async.wait(), 15)
            except asyncio.TimeoutError:
                self._cancel_driver(state)

        asyncio.run_coroutine_threadsafe(interrupt(), self._loop)
        # The terminal result decides the outcome; acknowledging a request is
        # not evidence the native run has stopped.
        return self._run_public(self._run(run_id))

    def steer(self, run_id: str, body: dict) -> dict:
        return self._admit_input(run_id, body, "steer")

    def queue(self, run_id: str, body: dict) -> dict:
        return self._admit_input(run_id, body, "queue")

    def _admit_input(self, run_id: str, body: dict, mode: str) -> dict:
        if body.get("expectedRunId") != run_id:
            raise AdapterFailure("Expected run id does not match", 409, "run_mismatch")
        request_id = body.get("requestId")
        if not isinstance(request_id, str) or not _REQUEST_ID_RE.fullmatch(request_id):
            raise AdapterFailure("Invalid request id")
        prompt = self._input(body.get("input"))
        input_hash = sha256(json.dumps(body["input"], sort_keys=True,
                                       separators=(",", ":")).encode()).hexdigest()
        with self.lock:
            state = self._states.get(run_id)
        with (state.delivery_lock if mode == "steer" and state is not None
              else nullcontext()):
            with self.lock:
                run = self._run(run_id)
                prior = self.db.execute(
                    "SELECT mode,input_hash FROM input_messages WHERE run=? AND request_id=?",
                    (run_id, request_id)).fetchone()
                if prior is not None:
                    if prior["mode"] != mode or prior["input_hash"] != input_hash:
                        raise AdapterFailure("Request id has different input", 409,
                                             "idempotency_conflict")
                    return self._run_public(run)
                state = self._states.get(run_id)
                if run["phase"] != "active" or state is None or state.cancel_requested:
                    raise AdapterFailure("Run is no longer active", 409, "run_not_active")
                count = self.db.execute(
                    "SELECT COUNT(*) AS total,SUM(status='queued') AS pending "
                    "FROM input_messages WHERE run=?", (run_id,)).fetchone()
                if count["total"] >= 50 or (count["pending"] or 0) >= 20:
                    raise AdapterFailure("Run input limit reached", 409, "input_limit")
                if mode == "steer" and state.steering:
                    raise AdapterFailure("A steering correction is already pending", 409,
                                         "steering_busy")
                with self.db:
                    self.db.execute(
                        "INSERT INTO input_messages(run,request_id,mode,input_hash,status) "
                        "VALUES(?,?,?,?,'queued')", (run_id, request_id, mode, input_hash))
                state.input_texts[request_id] = prompt
                state.control_used = True
                if mode == "steer":
                    state.steering = True
                    epoch = state.response_index
                    in_response = state.in_response
                    self._clear_interactions(run_id)
                self._control_activity(state, request_id, mode, "running")
                snapshot = self._run_public(self._run(run_id))
        if mode == "steer" and in_response:
            async def interrupt() -> None:
                with self.lock:
                    current = state.in_response and state.response_index == epoch
                    live = not state.cancel_requested and state.steering
                if not (current and live):
                    return
                try:
                    await asyncio.wait_for(state.client.interrupt(), 10)
                except Exception:  # noqa: BLE001 - ambiguous native control fails closed
                    with self.lock:
                        current = (state.in_response and state.response_index == epoch
                                   and state.steering and not state.cancel_requested)
                    if not current:
                        return
                    with self.lock, self.db:
                        self.db.execute(
                            "UPDATE input_messages SET status='unknown' "
                            "WHERE run=? AND request_id=? AND status='queued'",
                            (run_id, request_id))
                    self._control_activity(state, request_id, mode, "failed")
                    self._finalize(run_id, "failed", "steering_interrupt_failed")
                    self._cancel_driver(state)

            asyncio.run_coroutine_threadsafe(interrupt(), self._loop)
        return snapshot

    def _clear_interactions(self, run_id: str) -> None:
        with self.lock:
            for interaction_id, entry in list(self.pending.items()):
                if entry["public"]["runId"] == run_id:
                    self.pending.pop(interaction_id, None)
                    self._settle(entry, deny=True, interrupt=True)
            with self.db:
                self.db.execute(
                    "UPDATE runs SET active_state='running',updated=? "
                    "WHERE id=? AND phase='active'", (_now(), run_id))

    def _control_activity(self, state: _RunState, request_id: str, mode: str,
                          status: str) -> None:
        # Hash only the public request id; no prompt or prompt hash is published.
        native_id = "input:" + sha256(request_id.encode()).hexdigest()[:24]
        self._upsert_activity(state, native_id, "message", status,
                              f"{mode} input", {})

    # -- interactions ------------------------------------------------------

    async def _ask(self, state: _RunState, tool_name: str, tool_input: dict,
                   context: Any, loop: Any) -> Any:
        sdk = self._sdk_module()
        future = loop.create_future()
        interaction_id = _id("int_")
        approve, deny = _id("choice_"), _id("choice_")
        resource = ""
        for key in ("command", "file_path", "notebook_path", "url", "query"):
            value = tool_input.get(key) if isinstance(tool_input, dict) else None
            if isinstance(value, str) and value:
                resource = _clean(value, 1000)
                break
        title = getattr(context, "title", None) or f"Claude Code wants to use {tool_name}"
        interaction = {"id": interaction_id, "runId": state.run_id, "kind": "choice",
                       "state": "pending", "title": _clean(title, 200),
                       "resource": resource, "requested": None,
                       "choices": [{"id": approve, "label": "Approve once",
                                    "semantic": "approve"},
                                   {"id": deny, "label": "Deny", "semantic": "deny"}],
                       "fields": None,
                       "nativeRequestId": _clean(getattr(context, "tool_use_id", "") or "", 200),
                       "requestMethod": "tool/permission"}
        with self.lock:
            run = self._run(state.run_id)
            if run["phase"] != "active" or state.cancel_requested or state.steering:
                return sdk.PermissionResultDeny(message="Run response is ending",
                                                interrupt=True)
            self.pending[interaction_id] = {
                "public": interaction, "future": future, "loop": loop,
                "responses": {approve: sdk.PermissionResultAllow(),
                              deny: sdk.PermissionResultDeny(message="Denied by Bridge user")}}
            with self.db:
                self.db.execute(
                    "UPDATE runs SET active_state='waiting_interaction',updated=? "
                    "WHERE id=? AND phase='active'", (_now(), state.run_id))
            self._emit("interaction.pending", state.conversation_id, state.run_id,
                       interaction=interaction_id)
        return await future

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
            response = entry["responses"].get(body.get("choiceId"))
            if response is None:
                raise AdapterFailure("Choice is not available")
            self.pending.pop(interaction_id, None)
            future, loop = entry["future"], entry["loop"]

            def _set() -> None:
                if not future.done():
                    future.set_result(response)

            loop.call_soon_threadsafe(_set)
            remaining = any(item["public"]["runId"] == run["id"]
                            for item in self.pending.values())
            with self.db:
                self.db.execute("UPDATE runs SET active_state=?,updated=? WHERE id=?",
                                ("waiting_interaction" if remaining else "running",
                                 _now(), run["id"]))
            self._emit("interaction.resolved", run["conversation"], run["id"],
                       interaction=interaction_id)
            return {"id": interaction_id, "state": "resolved", "runId": run["id"]}

    # -- activities and events --------------------------------------------

    def activities(self, run_id: str) -> dict:
        self._run(run_id)
        with self.lock:
            rows = self.db.execute(
                "SELECT * FROM activities WHERE run=? ORDER BY created,id LIMIT 1000",
                (run_id,)).fetchall()
        return {"activities": [self._activity_public(dict(row)) for row in rows]}

    @staticmethod
    def _activity_public(row: dict) -> dict:
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
        with self.lock:
            states = list(self._states.values())
        for state in states:
            self._cancel_driver(state)
        for state in states:
            state.done.wait(15)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._loop_thread.join(timeout=5)
        self.db.close()
        fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
        os.close(self._lock_fd)
