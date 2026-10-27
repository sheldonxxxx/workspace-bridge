"""Node side of Agent Protocol v2: start, supervise and talk to agent plugins.

Each plugin adapter on a Node is one child process started from its
manifest's command, in its own process group, with a private state folder.
The Node writes requests to the child's stdin and reads responses and pushed
events from its stdout; stderr goes to a size-capped log file in the state
folder. A plugin that exits is restarted on the next call with exponential
backoff. Pushed events are buffered so the Bridge's event reader keeps its
long-poll contract, and nothing is replayed after a restart: run recovery is
the plugin's own durable state plus the Bridge's snapshot reconciliation.
"""
from __future__ import annotations

import collections
import json
import logging
import os
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from .login_path import resolve_login_path, runtime_env_with_login_path
from .plugin_protocol import MAX_LINE, PROTOCOL, encode, raise_for_error
from .runtime import RuntimeUnavailable

_LOG = logging.getLogger("workspace_bridge.ops")
INITIALIZE_TIMEOUT = 120.0
EVENT_BUFFER = 1000
MAX_BACKOFF = 60.0
LOG_LIMIT = 5 * 1024 * 1024
STABLE_AFTER = 60.0


_login_path_lock = threading.Lock()
_login_path: dict | None = None


def _cached_login_path() -> dict:
    # A launchd or systemd service starts with a minimal PATH that misses the
    # user's agent installs (Homebrew, npm, ~/.local/bin). Probe the login
    # shell once per process; a failed probe is retried on the next spawn.
    global _login_path
    with _login_path_lock:
        if _login_path is None:
            result = resolve_login_path()
            if not result.get("resolved"):
                return result
            _login_path = result
        return _login_path


def _plugin_env(log_level: str | None) -> dict:
    # The Node's own WB_* settings never reach an agent or the shells it runs.
    env = {key: value for key, value in os.environ.items() if not key.startswith("WB_")}
    env, _ = runtime_env_with_login_path(env, _resolve=_cached_login_path)
    if log_level:
        env["WB_LOG_LEVEL"] = log_level
    return env


def _open_log(path: Path):
    try:
        if path.stat().st_size > LOG_LIMIT:
            os.replace(path, path.with_name(path.name + ".1"))
    except FileNotFoundError:
        pass
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    return os.fdopen(fd, "ab", buffering=0)


class PluginProcess:
    """One supervised plugin child process for one adapter instance."""

    def __init__(self, adapter_id: str, manifest: dict, state: Path, *,
                 projects_root: str, settings: dict, log_level: str | None = None,
                 worktrees_root: str | None = None):
        self.adapter_id = adapter_id
        self.manifest = manifest
        self.state = state
        self.projects_root = projects_root
        self.worktrees_root = worktrees_root
        self.settings = settings
        self.log_level = log_level
        self._lock = threading.Lock()          # start/stop
        self._write_lock = threading.Lock()
        self._events = threading.Condition()
        self._process: subprocess.Popen | None = None
        self._pending: dict[int, list] = {}    # id -> [Event, message]
        self._next_id = 0
        self._failures = 0
        self._retry_at = 0.0
        self._started_at = 0.0
        self._closed = False
        self.instance_id = ""
        self._buffer: collections.deque = collections.deque(maxlen=EVENT_BUFFER)
        self._head = 0

    # -- lifecycle ---------------------------------------------------------

    def _command(self, env: dict) -> list[str]:
        command = list(self.manifest["command"])
        if not os.path.isabs(command[0]):
            found = shutil.which(command[0], path=env.get("PATH"))
            if found is None:
                raise RuntimeUnavailable("Plugin command is not installed on this Node")
            command[0] = found
        return command

    def _spawn(self) -> None:
        if self._closed:
            raise RuntimeUnavailable("Plugin supervisor is closed")
        if time.monotonic() < self._retry_at:
            raise RuntimeUnavailable("Plugin is restarting")
        self.state.mkdir(mode=0o700, parents=True, exist_ok=True)
        (self.state / "state").mkdir(mode=0o700, exist_ok=True)
        log = _open_log(self.state / "plugin.log")
        env = _plugin_env(self.log_level)
        try:
            process = subprocess.Popen(
                self._command(env), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log,
                cwd=self.state, env=env, start_new_session=True, close_fds=True)
        except OSError:
            self._failed()
            raise RuntimeUnavailable("Plugin could not be started") from None
        finally:
            log.close()
        self._process = process
        self._started_at = time.monotonic()
        with self._events:
            self._buffer.clear()
            self._head = 0
            self.instance_id = ""
        threading.Thread(target=self._read, args=(process,), name="plugin-reader",
                         daemon=True).start()
        # A plugin that answers initialize is running, even if its native
        # runtime is not usable yet; that is reported per request.
        params = {"protocol": PROTOCOL, "state": str(self.state / "state"),
                  "projectsRoot": self.projects_root, "settings": self.settings,
                  "adapterId": self.adapter_id}
        if self.worktrees_root:
            # Runs in isolation work in Node-owned worktrees under this folder.
            params["worktreesRoot"] = self.worktrees_root
        try:
            ready = self._call(process, "initialize", params, INITIALIZE_TIMEOUT)
        except BaseException:
            self._stop_process(process)
            self._failed()
            raise
        with self._events:
            self.instance_id = str(ready.get("instanceId") or "")[:200]
        _LOG.info("plugin started", extra={"event": "plugin_started"})

    def _failed(self) -> None:
        self._failures += 1
        self._retry_at = time.monotonic() + min(MAX_BACKOFF, 2 ** (self._failures - 1))

    def _ensure(self) -> subprocess.Popen:
        with self._lock:
            process = self._process
            if process is not None and process.poll() is None:
                if time.monotonic() - self._started_at > STABLE_AFTER:
                    self._failures = 0
                return process
            if process is not None:
                # A crash after a stable run restarts quickly; repeated
                # crashes back off exponentially.
                self._process = None
                if time.monotonic() - self._started_at > STABLE_AFTER:
                    self._failures = 0
                self._failed()
            self._spawn()
            assert self._process is not None
            return self._process

    @staticmethod
    def _stop_process(process: subprocess.Popen) -> None:
        if process.poll() is not None:
            return
        try:
            process.stdin.write(encode({"jsonrpc": "2.0", "method": "shutdown"}))
            process.stdin.flush()
            process.stdin.close()
        except (OSError, ValueError):
            pass
        try:
            process.wait(5)
            return
        except subprocess.TimeoutExpired:
            pass
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(process.pid, sig)
            except (ProcessLookupError, PermissionError):
                return
            try:
                process.wait(5)
                return
            except subprocess.TimeoutExpired:
                continue

    def close(self) -> None:
        with self._lock:
            self._closed = True
            process, self._process = self._process, None
        if process is not None:
            self._stop_process(process)
        with self._events:
            self._events.notify_all()

    # -- wire --------------------------------------------------------------

    def _read(self, process: subprocess.Popen) -> None:
        stream = process.stdout
        try:
            while True:
                line = stream.readline(MAX_LINE + 1)
                if not line or len(line) > MAX_LINE:
                    break
                try:
                    message = json.loads(line)
                except (ValueError, UnicodeError):
                    continue
                if not isinstance(message, dict):
                    continue
                if message.get("method") == "event":
                    self._push(message.get("params"))
                    continue
                ident = message.get("id")
                waiter = self._pending.get(ident) if isinstance(ident, int) else None
                if waiter is not None:
                    waiter[1] = message
                    waiter[0].set()
        finally:
            # Fail every request still waiting on this process.
            for ident, waiter in list(self._pending.items()):
                if waiter[2] is process:
                    waiter[0].set()
            with self._events:
                self._events.notify_all()

    def _push(self, params: Any) -> None:
        if not isinstance(params, dict) or not isinstance(params.get("event"), dict):
            return
        event = params["event"]
        cursor = event.get("cursor")
        if not isinstance(cursor, int) or isinstance(cursor, bool):
            return
        with self._events:
            if params.get("instanceId") and params["instanceId"] != self.instance_id:
                if self.instance_id:
                    return
                self.instance_id = str(params["instanceId"])[:200]
            if cursor <= self._head:
                return
            self._buffer.append(event)
            self._head = cursor
            self._events.notify_all()

    def _call(self, process: subprocess.Popen, method: str, params: dict, timeout: float) -> dict:
        with self._write_lock:
            self._next_id += 1
            ident = self._next_id
            waiter = [threading.Event(), None, process]
            self._pending[ident] = waiter
            try:
                process.stdin.write(encode({"jsonrpc": "2.0", "id": ident,
                                            "method": method, "params": params}))
                process.stdin.flush()
            except (OSError, ValueError):
                self._pending.pop(ident, None)
                raise RuntimeUnavailable("Plugin is unavailable") from None
        try:
            if not waiter[0].wait(timeout):
                raise RuntimeUnavailable("Plugin did not answer in time")
        finally:
            self._pending.pop(ident, None)
        message = waiter[1]
        if message is None:
            raise RuntimeUnavailable("Plugin exited")
        if "error" in message:
            raise_for_error(message["error"])
        result = message.get("result")
        if not isinstance(result, dict):
            raise RuntimeUnavailable("Runtime response is invalid")
        return result

    def call(self, method: str, params: dict, timeout: float = 30) -> dict:
        return self._call(self._ensure(), method, params, timeout)

    def events(self, after: int, wait_ms: int) -> dict:
        """Serve buffered pushed events with the v1 long-poll page shape."""
        self._ensure()
        deadline = time.monotonic() + min(max(wait_ms, 0), 25000) / 1000
        with self._events:
            while self._head <= after and not self._closed:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or self._process is None or self._process.poll() is not None:
                    break
                self._events.wait(remaining)
            page = [event for event in self._buffer if event["cursor"] > after][:100]
            return {"instanceId": self.instance_id, "cursor": max(self._head, after),
                    "events": page}


class PluginRegistry:
    """The Node's running plugins, keyed by adapter id and configuration revision."""

    def __init__(self, root: Path, *, worktrees_root: Path | None = None):
        self.root = root
        self.worktrees_root = worktrees_root
        self._lock = threading.Lock()
        self._processes: dict[str, tuple[str, PluginProcess]] = {}

    def process(self, row: dict, manifest: dict) -> PluginProcess:
        stale = None
        with self._lock:
            current = self._processes.get(row["id"])
            if current is not None and current[0] == row["revision"]:
                return current[1]
            if current is not None:
                stale = current[1]
            settings = json.loads(row.get("settings") or "{}")
            process = PluginProcess(row["id"], manifest, self.root / row["id"],
                                    projects_root=row["projects_root"], settings=settings,
                                    log_level=settings.get("log_level"),
                                    worktrees_root=str(self.worktrees_root) if self.worktrees_root else None)
            self._processes[row["id"]] = (row["revision"], process)
        if stale is not None:
            # Stopping can take seconds; callers may hold the Node lock.
            threading.Thread(target=stale.close, name="plugin-stop", daemon=True).start()
        return process

    def stop(self, adapter_id: str) -> None:
        with self._lock:
            current = self._processes.pop(adapter_id, None)
        if current is not None:
            current[1].close()

    def close(self) -> None:
        with self._lock:
            processes = [process for _, process in self._processes.values()]
            self._processes.clear()
        for process in processes:
            process.close()
