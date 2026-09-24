"""Private stdio JSON-RPC transport owned by the Codex host adapter process.

The Bridge control plane never imports this module. One dedicated app-server
process owns only Bridge-created threads; Desktop/TUI control sockets are not
used. Server requests remain pending until the exact live callback is answered.
"""
from __future__ import annotations

import json
import re
import subprocess
import threading
from collections import deque
from typing import Any, Callable

from .security import redact


_MAX_STDERR_LINE = 4096
_MAX_STDERR_TAIL = 8192
_MAX_DIAGNOSTIC = 2048
_ABSOLUTE_PATH = re.compile(r"(?<![A-Za-z0-9:+])/(?!/)[^\s\"'<>|,;)]*")
_FILE_URL = re.compile(r"file://\S+")
_WINDOWS_PATH = re.compile(r"\b[A-Za-z]:\\[^\s\"'<>|,;)]*")
_ENV_SECRET = re.compile(
    r"(?i)\b[\w-]*(?:token|secret|password|api[_-]?key|credential)[\w-]*\s*[=:]\s*[^\s,;]+")
_BEARER = re.compile(r"(?i)\bBearer\s+[^\s,;]+")
_PRIVATE_KEY_BEGIN = re.compile(r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----")
_PRIVATE_KEY_END = re.compile(r"-----END (?:[A-Z ]+ )?PRIVATE KEY-----")


def sanitize_diagnostic(value: Any, *, limit: int = 300) -> str:
    """Bound native diagnostics and remove secret-like values and absolute paths."""
    text = value if isinstance(value, str) else "" if value is None else str(value)
    text, _ = redact(text)
    text = _ENV_SECRET.sub("[REDACTED_SECRET]", text)
    text = _BEARER.sub("Bearer [REDACTED_SECRET]", text)
    text = _FILE_URL.sub("[PATH]", text)
    text = _ABSOLUTE_PATH.sub("[PATH]", text)
    text = _WINDOWS_PATH.sub("[PATH]", text)
    text = "".join(char for char in text if char in "\t\n" or ord(char) >= 32)
    return " ".join(text.split())[:max(0, limit)]


class CodexRpcError(Exception):
    pass


class CodexRpc:
    def __init__(self, *, command: tuple[str, ...] = ("codex", "app-server", "--stdio"),
                 on_notification: Callable[[str, dict], None] | None = None,
                 on_request: Callable[[int | str, str, dict], None] | None = None):
        self._process = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
            bufsize=1)
        self._write_lock = threading.Lock()
        self._pending_lock = threading.Lock()
        self._stderr_lock = threading.Lock()
        self._shutdown_lock = threading.Lock()
        self._pending: dict[int, tuple[threading.Event, list[Any]]] = {}
        self._stderr_tail: deque[str] = deque()
        self._stderr_size = 0
        self._discard_private_key = False
        self._next_id = 0
        self._closed = False
        self._shutdown_started = False
        self.on_notification = on_notification
        self.on_request = on_request
        self._stderr_reader = threading.Thread(target=self._stderr_loop,
                                               name="codex-app-server-stderr",
                                               daemon=True)
        self._stderr_reader.start()
        self._reader = threading.Thread(target=self._read_loop, name="codex-app-server-rpc",
                                        daemon=True)
        self._reader.start()
        self.initialize_result = self.call("initialize", {
            "clientInfo": {"name": "workspace-bridge-codex-adapter", "version": "1.0.0"},
            "capabilities": {"experimentalApi": True},
        }, timeout=15)
        self.notify("initialized")

    @property
    def alive(self) -> bool:
        return not self._closed and self._process.poll() is None

    def _send(self, value: dict) -> None:
        if not self.alive or self._process.stdin is None:
            raise CodexRpcError("Codex app-server is unavailable")
        line = json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n"
        with self._write_lock:
            try:
                self._process.stdin.write(line)
                self._process.stdin.flush()
            except (OSError, ValueError, BrokenPipeError) as exc:
                raise CodexRpcError("Codex app-server transport failed") from exc

    def call(self, method: str, params: dict, *, timeout: float = 30) -> dict:
        event = threading.Event()
        box: list[Any] = []
        with self._pending_lock:
            self._next_id += 1
            request_id = self._next_id
            self._pending[request_id] = (event, box)
        try:
            self._send({"id": request_id, "method": method, "params": params})
            if not event.wait(timeout):
                raise CodexRpcError("Codex app-server request timed out")
            response = box[0] if box else None
            if not isinstance(response, dict):
                raise CodexRpcError("Codex app-server response is invalid")
            if "error" in response:
                error = response["error"]
                code = error.get("code") if isinstance(error, dict) else None
                message = error.get("message") if isinstance(error, dict) else "request rejected"
                summary = f"{method} rejected"
                if isinstance(code, int) and not isinstance(code, bool):
                    summary += f" (code {code})"
                detail = sanitize_diagnostic(message)
                if detail:
                    summary += f": {detail}"
                raise CodexRpcError(summary[:300])
            result = response.get("result")
            if not isinstance(result, dict):
                raise CodexRpcError("Codex app-server result is invalid")
            return result
        finally:
            with self._pending_lock:
                self._pending.pop(request_id, None)

    def notify(self, method: str, params: dict | None = None) -> None:
        message: dict = {"method": method}
        if params is not None:
            message["params"] = params
        self._send(message)

    def respond(self, request_id: int | str, result: dict | None = None,
                error: dict | None = None) -> None:
        if error is not None:
            self._send({"id": request_id, "error": error})
        else:
            self._send({"id": request_id, "result": result or {}})

    def _stderr_loop(self) -> None:
        stream = self._process.stderr
        if stream is None:
            return
        while True:
            try:
                line = stream.readline(_MAX_STDERR_LINE + 1)
            except (OSError, ValueError):
                return
            if not line:
                return
            if len(line.rstrip("\r\n")) > _MAX_STDERR_LINE:
                while line and not line.endswith(("\n", "\r")):
                    try:
                        line = stream.readline(_MAX_STDERR_LINE + 1)
                    except (OSError, ValueError):
                        line = ""
                self._append_stderr("[oversized app-server stderr line omitted]")
                continue
            if self._discard_private_key:
                if _PRIVATE_KEY_END.search(line):
                    self._discard_private_key = False
                continue
            if _PRIVATE_KEY_BEGIN.search(line):
                if not _PRIVATE_KEY_END.search(line):
                    self._discard_private_key = True
                self._append_stderr("[private key material omitted]")
                continue
            self._append_stderr(line)

    def _append_stderr(self, line: str) -> None:
        summary = sanitize_diagnostic(line, limit=_MAX_STDERR_LINE)
        if not summary:
            return
        summary += "\n"
        with self._stderr_lock:
            self._stderr_tail.append(summary)
            self._stderr_size += len(summary)
            while self._stderr_tail and self._stderr_size > _MAX_STDERR_TAIL:
                self._stderr_size -= len(self._stderr_tail.popleft())

    def stderr_summary(self) -> str:
        """Return only a small in-memory tail of sanitized app-server stderr."""
        with self._stderr_lock:
            summary = "".join(self._stderr_tail)
        return summary[-_MAX_DIAGNOSTIC:].strip()

    def _read_loop(self) -> None:
        assert self._process.stdout is not None
        try:
            for line in self._process.stdout:
                if len(line) > 8 * 1024 * 1024:
                    continue
                try:
                    value = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(value, dict):
                    continue
                request_id = value.get("id")
                method = value.get("method")
                if request_id is not None and isinstance(method, str):
                    if self.on_request is not None:
                        try:
                            self.on_request(request_id, method, value.get("params") or {})
                        except Exception:
                            try:
                                self.respond(request_id, error={"code": -32603,
                                                                "message": "Adapter request handler failed"})
                            except CodexRpcError:
                                pass
                    else:
                        try:
                            self.respond(request_id, error={"code": -32601,
                                                            "message": "Unsupported server request"})
                        except CodexRpcError:
                            pass
                elif request_id is not None:
                    with self._pending_lock:
                        target = self._pending.get(request_id)
                    if target is not None:
                        event, box = target
                        box.append(value)
                        event.set()
                elif isinstance(method, str) and self.on_notification is not None:
                    try:
                        self.on_notification(method, value.get("params") or {})
                    except Exception:
                        pass
        finally:
            self._closed = True
            with self._pending_lock:
                for event, _ in self._pending.values():
                    event.set()

    def close(self) -> None:
        with self._shutdown_lock:
            if self._shutdown_started:
                return
            self._shutdown_started = True
            self._closed = True
        if self._process.stdin is not None:
            try:
                self._process.stdin.close()
            except OSError:
                pass
        try:
            self._process.terminate()
            self._process.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            self._process.kill()
            try:
                self._process.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                pass
        self._stderr_reader.join(timeout=1)
