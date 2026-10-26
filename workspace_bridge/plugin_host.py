"""Plugin side of Agent Protocol v2 for the built-in Python agents.

``python -m workspace_bridge.plugin_host claude|codex|opencode`` serves one
agent over stdin/stdout for a supervising Node. Requests run on a small
worker pool, so a long native call never blocks the pipe; responses and
pushed events are written whole, one line each, under one lock. Nothing
else may write to the real stdout: fd 1 is redirected to stderr at start,
so stray prints and child processes cannot corrupt the protocol stream.
"""
from __future__ import annotations

import inspect
import json
import logging
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, BinaryIO, Callable

from .plugin_protocol import MAX_LINE, METHODS, PROTOCOL, encode, error_payload

_LOG = logging.getLogger("workspace_bridge.plugin")
MAX_WORKERS = 16
EVENT_WAIT_MS = 25000


def _body(params: dict) -> dict:
    body = params.get("body", {})
    if not isinstance(body, dict):
        raise ValueError("body must be an object")
    return body


def _models(adapter, params: dict):
    # Only adapters whose models() takes a workspace receive one.
    if params.get("workspaceId") is not None and inspect.signature(adapter.models).parameters:
        return adapter.models(params["workspaceId"])
    return adapter.models()


def _rebind(adapter, params: dict):
    binding = _body(params).get("securityBinding")
    if not isinstance(binding, dict):
        raise ValueError("Invalid security rebind binding")
    return adapter.rebind_conversation(params["conversationId"], binding)


HANDLERS: dict[str, Callable[[Any, dict], Any]] = {
    "descriptor": lambda a, p: a.descriptor(),
    "models": _models,
    "usageLimits": lambda a, p: a.usage_limits(),
    "profiles.list": lambda a, p: a.profiles(p.get("workspaceId"), p.get("directory"),
                                             fresh=bool(p.get("fresh", False))),
    "profiles.save": lambda a, p: a.save_profile(_body(p)),
    "profiles.delete": lambda a, p: a.delete_profile(p["profileId"]),
    "conversations.create": lambda a, p: a.create_conversation(_body(p)),
    "conversations.get": lambda a, p: a.conversation(p["conversationId"]),
    "conversations.rebind": _rebind,
    "runs.start": lambda a, p: a.start_run(p["conversationId"], _body(p)),
    "runs.find": lambda a, p: a.find_run(p["conversationId"], p["clientRunId"]),
    "runs.get": lambda a, p: a.run(p["runId"]),
    "runs.cancel": lambda a, p: a.cancel(p["runId"]),
    "runs.steer": lambda a, p: a.steer(p["runId"], _body(p)),
    "runs.queue": lambda a, p: a.queue(p["runId"], _body(p)),
    "runs.interactions": lambda a, p: a.interactions(p["runId"]),
    "runs.activities": lambda a, p: a.activities(p["runId"]),
    "interactions.resolve": lambda a, p: a.resolve(p["interactionId"], _body(p)),
    "activities.get": lambda a, p: a.activity(p["activityId"]),
    "events": lambda a, p: a.events(int(p.get("after", 0)), int(p.get("waitMs", 0))),
}
assert set(HANDLERS) == METHODS


class PluginServer:
    """Serve one adapter object over the v2 stdio protocol."""

    def __init__(self, factory: Callable[[dict], Any], *,
                 result_filter: Callable[[Any], Any] | None = None,
                 unavailable: tuple[type[BaseException], ...] = ()):
        self.factory = factory
        self.result_filter = result_filter or (lambda value: value)
        self.unavailable = unavailable
        self.adapter = None
        self._out: BinaryIO | None = None
        self._write_lock = threading.Lock()
        self._stop = threading.Event()
        self._pool = ThreadPoolExecutor(MAX_WORKERS, thread_name_prefix="plugin-request")

    def _send(self, message: dict) -> None:
        line = encode(message)
        with self._write_lock:
            assert self._out is not None
            self._out.write(line)
            self._out.flush()

    def _failure(self, exc: BaseException) -> dict:
        status, code = getattr(exc, "status", None), getattr(exc, "code", None)
        if isinstance(status, int) and not isinstance(status, bool) and isinstance(code, str):
            return error_payload(status, code, str(exc))
        if isinstance(exc, (ValueError, TypeError, KeyError)):
            return error_payload(400, "invalid_arguments", "Invalid request")
        if self.unavailable and isinstance(exc, self.unavailable):
            return error_payload(502, "runtime_unavailable", "Native runtime is unavailable")
        _LOG.error("plugin request failed: %s: %s", type(exc).__name__, str(exc)[:300])
        return error_payload(502, "runtime_unavailable", "Native runtime is unavailable")

    def _handle(self, ident: Any, method: str, params: dict) -> None:
        try:
            if self.adapter is None:
                raise ValueError("initialize first")
            if method not in HANDLERS:
                self._send({"jsonrpc": "2.0", "id": ident,
                            "error": error_payload(404, "not_found", "Unknown method")})
                return
            result = self.result_filter(HANDLERS[method](self.adapter, params))
            if not isinstance(result, dict):
                raise TypeError("result must be an object")
            self._send({"jsonrpc": "2.0", "id": ident, "result": result})
        except Exception as exc:  # noqa: BLE001 - every failure becomes a response
            try:
                self._send({"jsonrpc": "2.0", "id": ident, "error": self._failure(exc)})
            except Exception:  # noqa: BLE001 - the pipe is gone; the Node restarts us
                self._stop.set()

    def _pump_events(self) -> None:
        """Push native events as they happen; the Node buffers them."""
        after = 0
        while not self._stop.is_set():
            try:
                value = self.adapter.events(after, EVENT_WAIT_MS)
            except Exception:  # noqa: BLE001 - snapshots stay authoritative
                if self._stop.wait(1):
                    return
                continue
            events = value.get("events") if isinstance(value, dict) else None
            for event in events or []:
                if isinstance(event, dict) and isinstance(event.get("cursor"), int):
                    after = max(after, event["cursor"])
                self._send({"jsonrpc": "2.0", "method": "event",
                            "params": {"instanceId": value.get("instanceId"), "event": event}})

    def _initialize(self, ident: Any, params: dict) -> None:
        if self.adapter is not None or params.get("protocol") != PROTOCOL:
            self._send({"jsonrpc": "2.0", "id": ident, "error": error_payload(
                400, "protocol_mismatch", f"Expected protocol {PROTOCOL}")})
            return
        try:
            self.adapter = self.factory(params)
        except Exception as exc:  # noqa: BLE001 - reported, then the Node retries later
            # The plugin log is local to the Node; the reason helps the admin.
            _LOG.error("plugin initialize failed: %s: %s", type(exc).__name__, str(exc)[:300])
            self._send({"jsonrpc": "2.0", "id": ident, "error": self._failure(exc)})
            self._stop.set()
            return
        instance = getattr(self.adapter, "instance_id", "")
        self._send({"jsonrpc": "2.0", "id": ident, "result": {
            "protocol": PROTOCOL, "instanceId": instance if isinstance(instance, str) else ""}})
        if callable(getattr(self.adapter, "events", None)):
            threading.Thread(target=self._pump_events, name="plugin-events", daemon=True).start()

    def serve(self, stdin: BinaryIO, stdout: BinaryIO) -> None:
        self._out = stdout
        try:
            while not self._stop.is_set():
                line = stdin.readline(MAX_LINE + 1)
                if not line:
                    break
                if len(line) > MAX_LINE:
                    break
                try:
                    message = json.loads(line)
                except (ValueError, UnicodeError):
                    continue
                if not isinstance(message, dict):
                    continue
                method, ident = message.get("method"), message.get("id")
                params = message.get("params") or {}
                if not isinstance(method, str) or not isinstance(params, dict):
                    continue
                if method == "initialize":
                    self._initialize(ident, params)
                elif method == "shutdown":
                    break
                elif ident is not None:
                    self._pool.submit(self._handle, ident, method, params)
        finally:
            self._stop.set()
            self._pool.shutdown(wait=False, cancel_futures=True)
            if self.adapter is not None and callable(getattr(self.adapter, "close", None)):
                try:
                    self.adapter.close()
                except Exception:  # noqa: BLE001 - exiting anyway
                    pass


def _setting(params: dict, key: str) -> str | None:
    value = (params.get("settings") or {}).get(key)
    return value if isinstance(value, str) and value else None


def _claude() -> PluginServer:
    from .claude_host_adapter import ClaudeHostAdapter, parse_setting_sources
    return PluginServer(lambda p: ClaudeHostAdapter(
        Path(p["state"]), Path(p["projectsRoot"]), binary=_setting(p, "binary"),
        setting_sources=parse_setting_sources(_setting(p, "setting_sources"))))


def _codex() -> PluginServer:
    from .codex_host_adapter import CodexHostAdapter
    from .codex_rpc import CodexRpcError
    return PluginServer(lambda p: CodexHostAdapter(Path(p["state"]), Path(p["projectsRoot"])),
                        unavailable=(CodexRpcError,))


def _opencode() -> PluginServer:
    from .opencode_host_adapter import OpenCodeHostAdapter
    holder: dict = {}

    def build(p: dict):
        adapter = OpenCodeHostAdapter(Path(p["state"]), Path(p["projectsRoot"]),
                                      binary=_setting(p, "binary"))
        holder["adapter"] = adapter

        def supervise():
            # The owned engine and this plugin are one supervised unit: if
            # opencode serve dies, exit so the Node restarts both.
            adapter.native.process.wait()
            if not adapter._closed:
                os._exit(1)
        threading.Thread(target=supervise, daemon=True, name="opencode-native-supervisor").start()
        return adapter

    return PluginServer(build, result_filter=lambda value: holder["adapter"]._scrub(value))


BUILTINS = {"claude": _claude, "codex": _codex, "opencode": _opencode}


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1 or argv[0] not in BUILTINS:
        raise SystemExit("usage: python -m workspace_bridge.plugin_host claude|codex|opencode")
    # Keep the protocol stream private: the real stdout moves to a private
    # descriptor and fd 1 becomes stderr for everything else in the process.
    out = os.fdopen(os.dup(1), "wb", buffering=0)
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    level = os.environ.get("WB_LOG_LEVEL", "INFO").strip().upper() or "INFO"
    logging.basicConfig(stream=sys.stderr, level=getattr(logging, level, logging.INFO),
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    server = BUILTINS[argv[0]]()
    server.serve(sys.stdin.buffer, out)


if __name__ == "__main__":
    main()
