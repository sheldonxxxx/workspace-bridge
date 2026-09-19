"""Narrow client boundary to the separately managed native OpenCode runtime.

Workspace Bridge never starts or supervises an OpenCode server. In the Docker
deployment a small package-owned Node SDK adapter (``runtime/opencode-adapter``)
is the only process that holds provider/network credentials and talks to the
external host server. This module speaks a minimal, bounded HTTP protocol to
that adapter and never exposes arbitrary command execution.

Sanitization happens here and again in the orchestrator: no credentials,
webhook URLs, external paths beyond the mapped workspace root, or hidden
reasoning may cross this boundary into persisted state or MCP output.
"""
from __future__ import annotations

import base64
import json
import os
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from .security import BridgeError, redact

MAX_RUNTIME_TIMEOUT = 60.0
DEFAULT_CONNECT_TIMEOUT = 4.0
DEFAULT_READ_TIMEOUT = 30.0
MAX_EVENT_TEXT = 2000
MAX_MESSAGE_TEXT = 8000
MAX_TRANSCRIPT_CHARS = 60000


class RuntimeUnavailable(BridgeError):
    """The adapter or the upstream OpenCode server could not be reached."""

    def __init__(self, message: str = "OpenCode runtime is unavailable"):
        super().__init__(message, "runtime_unavailable")


class RuntimeRejected(BridgeError):
    """The runtime answered but rejected the bounded request."""

    def __init__(self, message: str, code: str = "runtime_rejected", status: int | None = None):
        super().__init__(message, code)
        self.status = status


class RuntimeUnsupported(BridgeError):
    """The installed runtime does not expose a requested capability."""

    def __init__(self, message: str):
        super().__init__(message, "runtime_unsupported")


def _bounded(value: Any, limit: int) -> str:
    text = value if isinstance(value, str) else "" if value is None else str(value)
    return text[:limit]


def sanitize_metadata(value: Any, *, depth: int = 0, limit: int = 60,
                      budget: int = 4000, _redacted: list | None = None) -> tuple[Any, bool]:
    """Keep a small, flat, printable subset of runtime-supplied metadata.

    Strings have secret-like patterns redacted. Returns ``(value, redacted)`` so
    callers can indicate when review metadata had to be sanitized. This is a
    display aid, not a trusted record.
    """
    redacted = _redacted if _redacted is not None else []
    if depth > 2:
        return None, bool(redacted)
    if value is None or isinstance(value, bool):
        return value, bool(redacted)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value, bool(redacted)
    if isinstance(value, str):
        text = "".join(c for c in value if c == "\n" or ord(c) >= 32)[:400]
        cleaned, changed = redact(text)
        if changed:
            redacted.append(True)
        return cleaned, bool(redacted)
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key in list(value.keys())[:limit]:
            name = _bounded(key, 80)
            if not name:
                continue
            item, _ = sanitize_metadata(value[key], depth=depth + 1, limit=limit, budget=budget,
                                        _redacted=redacted)
            result[name] = item
            if len(json.dumps(result, default=str)) > budget:
                result.pop(name, None)
                break
        return result, bool(redacted)
    if isinstance(value, (list, tuple)):
        items = []
        for item in value[:limit]:
            mapped, _ = sanitize_metadata(item, depth=depth + 1, limit=limit, budget=budget,
                                          _redacted=redacted)
            items.append(mapped)
        return items, bool(redacted)
    return None, bool(redacted)


@dataclass
class ModelInfo:
    selector: str
    provider: str
    model: str
    name: str = ""
    default: bool = False
    variants: tuple[str, ...] = ()

    def public(self) -> dict:
        return {"selector": self.selector, "provider": self.provider, "model": self.model,
                "name": self.name or self.model, "default": self.default,
                "variants": list(self.variants)}


@dataclass
class SessionInfo:
    id: str
    directory: str
    title: str = ""


@dataclass
class MessageInfo:
    id: str
    role: str
    created: int | None = None
    completed: int | None = None
    text: str = ""
    tools: tuple[str, ...] = ()
    error: str | None = None


@dataclass
class PendingPermission:
    id: str
    session_id: str
    action: str
    title: str
    pattern: tuple[str, ...] = ()
    requested_patterns: tuple[str, ...] = ()
    tool: Any = None
    call_id: str | None = None
    metadata: dict = field(default_factory=dict)
    redacted: bool = False
    created: str = ""

    def scope_text(self) -> str:
        return ", ".join(self.pattern)


class OpenCodeRuntime:
    """Transport abstraction; tests substitute a scripted fake."""

    name = "runtime"

    def health(self) -> dict:
        raise NotImplementedError

    def list_models(self) -> list[ModelInfo]:
        raise NotImplementedError

    def create_session(self, directory: str, title: str) -> SessionInfo:
        raise NotImplementedError

    def get_session(self, directory: str, session_id: str) -> SessionInfo | None:
        raise NotImplementedError

    def prompt_async(self, directory: str, session_id: str, text: str,
                     model: dict | None = None) -> None:
        raise NotImplementedError

    def messages(self, directory: str, session_id: str, limit: int = 40) -> list[MessageInfo]:
        raise NotImplementedError

    def respond_permission(self, directory: str, session_id: str, permission_id: str,
                           response: str) -> bool:
        raise NotImplementedError

    def abort_session(self, directory: str, session_id: str) -> bool:
        raise NotImplementedError

    def session_status(self, directory: str, session_id: str) -> str:
        """Return the native session status: idle, busy or retry.

        A missing map entry is idle under the installed OpenCode v1.18.31
        semantics; malformed/unknown statuses raise so callers fail closed.
        """
        raise NotImplementedError

    def poll_events(self, cursor: int, timeout: float = 25.0) -> tuple[list[dict], int]:
        raise NotImplementedError

    def close(self) -> None:
        return None


class HttpOpenCodeRuntime(OpenCodeRuntime):
    """Bounded HTTP client for the private adapter sidecar (no host-published port)."""

    name = "http-adapter"

    def __init__(self, base_url: str, token: str = "", *,
                 connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
                 read_timeout: float = DEFAULT_READ_TIMEOUT):
        parsed = urllib.parse.urlparse(base_url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise BridgeError("WB_OPENCODE_RUNTIME_URL must be an http(s) URL")
        self.base_url = base_url.rstrip("/")
        self.token = token or ""
        self.connect_timeout = min(max(float(connect_timeout), 0.5), MAX_RUNTIME_TIMEOUT)
        self.read_timeout = min(max(float(read_timeout), 0.5), MAX_RUNTIME_TIMEOUT)

    def _request(self, method: str, path: str, *, query: dict | None = None,
                 body: dict | None = None, timeout: float | None = None) -> Any:
        url = self.base_url + path
        if query:
            clean = {k: str(v) for k, v in query.items() if v is not None}
            if clean:
                url += "?" + urllib.parse.urlencode(clean)
        data = None
        headers = {"Accept": "application/json"}
        if self.token:
            headers["X-Runtime-Token"] = self.token
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        request.timeout = timeout if timeout is not None else self.read_timeout
        try:
            with urllib.request.urlopen(request, timeout=request.timeout) as response:
                raw = response.read(2 * 1024 * 1024)
                if not raw:
                    return {}
                return json.loads(raw)
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = json.loads(exc.read(8192)).get("error", "") if exc.fp else ""
            except Exception:  # noqa: BLE001 - never surface raw bodies
                detail = ""
            if exc.code in (409, 412, 422):
                raise RuntimeRejected(_bounded(detail, 300) or "Runtime rejected the request",
                                      status=exc.code) from None
            if exc.code == 501:
                raise RuntimeUnsupported(_bounded(detail, 300) or "Runtime capability unsupported") from None
            if exc.code in (404,):
                raise RuntimeRejected("Runtime resource not found", "not_found", status=404) from None
            raise RuntimeUnavailable("OpenCode runtime returned an error") from None
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError):
            raise RuntimeUnavailable("OpenCode runtime is unavailable") from None
        except (ValueError, UnicodeError):
            raise RuntimeUnavailable("OpenCode runtime returned an invalid response") from None

    def health(self) -> dict:
        value = self._request("GET", "/health", timeout=self.connect_timeout)
        if not isinstance(value, dict):
            raise RuntimeUnavailable("OpenCode runtime health response was invalid")
        return {"ok": bool(value.get("ok")), "version": _bounded(value.get("version"), 80),
                "adapter_version": _bounded(value.get("adapter_version"), 40),
                "server_configured": bool(value.get("server_configured")),
                "locked": bool(value.get("locked")),
                "instance": _bounded(value.get("instance"), 80),
                "cursor": value.get("cursor") if isinstance(value.get("cursor"), int) else 0}

    def list_models(self) -> list[ModelInfo]:
        # Global discovery: the adapter /models endpoint takes no workspace
        # directory. Model availability never depends on a workspace.
        value = self._request("GET", "/models")
        rows = value.get("models") if isinstance(value, dict) else None
        if not isinstance(rows, list):
            raise RuntimeUnavailable("OpenCode model list was invalid")
        result: list[ModelInfo] = []
        for row in rows[:2000]:
            if not isinstance(row, dict):
                continue
            provider = _bounded(row.get("provider"), 120)
            model = _bounded(row.get("model"), 200)
            if not provider or not model:
                continue
            variants = tuple(_bounded(v, 80) for v in (row.get("variants") or []) if isinstance(v, str))[:16]
            result.append(ModelInfo(selector=_bounded(row.get("selector"), 260) or f"{provider}/{model}",
                                    provider=provider, model=model,
                                    name=_bounded(row.get("name"), 200),
                                    default=bool(row.get("default")), variants=variants))
        return result

    def create_session(self, directory: str, title: str) -> SessionInfo:
        value = self._request("POST", "/sessions", body={"directory": directory, "title": title})
        session = value.get("session") if isinstance(value, dict) else None
        if not isinstance(session, dict) or not session.get("id"):
            raise RuntimeUnavailable("OpenCode session creation returned no session id")
        # Never substitute the requested directory for a missing observed one:
        # callers must fail closed on an unknown/mismatched session directory.
        return SessionInfo(id=_bounded(session["id"], 200),
                           directory=_bounded(session.get("directory"), 1024),
                           title=_bounded(session.get("title"), 200))

    def get_session(self, directory: str, session_id: str) -> SessionInfo | None:
        try:
            value = self._request("GET", f"/sessions/{urllib.parse.quote(session_id)}",
                                  query={"directory": directory})
        except RuntimeRejected as exc:
            if exc.code == "not_found":
                return None
            raise
        session = value.get("session") if isinstance(value, dict) else None
        if not isinstance(session, dict) or not session.get("id"):
            return None
        return SessionInfo(id=_bounded(session["id"], 200),
                           directory=_bounded(session.get("directory"), 1024),
                           title=_bounded(session.get("title"), 200))

    def prompt_async(self, directory: str, session_id: str, text: str,
                     model: dict | None = None) -> None:
        body: dict[str, Any] = {"directory": directory, "text": _bounded(text, 60000)}
        if model:
            body["model"] = {"providerID": _bounded(model.get("providerID"), 120),
                             "modelID": _bounded(model.get("modelID"), 200)}
        value = self._request("POST", f"/sessions/{urllib.parse.quote(session_id)}/prompt-async",
                              body=body, timeout=self.read_timeout)
        if not isinstance(value, dict) or not value.get("accepted"):
            raise RuntimeUnavailable("OpenCode runtime did not accept the prompt")

    def messages(self, directory: str, session_id: str, limit: int = 40) -> list[MessageInfo]:
        value = self._request("GET", f"/sessions/{urllib.parse.quote(session_id)}/messages",
                              query={"directory": directory, "limit": max(1, min(int(limit), 100))})
        rows = value.get("messages") if isinstance(value, dict) else None
        if not isinstance(rows, list):
            raise RuntimeUnavailable("OpenCode message list was invalid")
        result: list[MessageInfo] = []
        for row in rows[-100:]:
            if not isinstance(row, dict):
                continue
            tools = tuple(_bounded(t, 120) for t in (row.get("tools") or []) if isinstance(t, str))[:24]
            error = row.get("error")
            result.append(MessageInfo(id=_bounded(row.get("id"), 200),
                                      role=_bounded(row.get("role"), 40),
                                      created=row.get("created") if isinstance(row.get("created"), int) else None,
                                      completed=row.get("completed") if isinstance(row.get("completed"), int) else None,
                                      text=_bounded(row.get("text"), MAX_MESSAGE_TEXT),
                                      tools=tools,
                                      error=_bounded(error, 300) if error else None))
        return result

    def respond_permission(self, directory: str, session_id: str, permission_id: str,
                           response: str) -> bool:
        if response not in ("once", "always", "reject"):
            raise BridgeError("Permission response must be once, always or reject", "invalid_arguments")
        value = self._request(
            "POST",
            f"/sessions/{urllib.parse.quote(session_id)}/permissions/{urllib.parse.quote(permission_id)}",
            body={"directory": directory, "response": response})
        return bool(value.get("ok")) if isinstance(value, dict) else False

    def abort_session(self, directory: str, session_id: str) -> bool:
        value = self._request("POST", f"/sessions/{urllib.parse.quote(session_id)}/abort",
                              body={"directory": directory})
        return bool(value.get("ok")) if isinstance(value, dict) else False

    def session_status(self, directory: str, session_id: str) -> str:
        value = self._request("GET", f"/sessions/{urllib.parse.quote(session_id)}/status",
                              query={"directory": directory})
        status = value.get("status") if isinstance(value, dict) else None
        # The adapter already normalizes the native status map: a session id
        # absent from the map is reported as idle under the installed
        # v1.18.31 semantics. Anything else unknown here is malformed and
        # must fail validation rather than guess.
        if status in ("idle", "busy", "retry"):
            return status
        raise RuntimeUnavailable("OpenCode session status was invalid")

    def poll_events(self, cursor: int, timeout: float = 25.0) -> tuple[list[dict], int]:
        value = self._request("GET", "/events",
                              query={"cursor": max(0, int(cursor)),
                                     "timeout": min(max(float(timeout), 1.0), 30.0)},
                              timeout=min(max(float(timeout), 1.0), 30.0) + 8.0)
        events = value.get("events") if isinstance(value, dict) else None
        next_cursor = value.get("cursor") if isinstance(value, dict) else None
        if not isinstance(events, list):
            raise RuntimeUnavailable("OpenCode event poll was invalid")
        normalized = [event for event in (self._normalize_event(item) for item in events) if event is not None]
        return normalized, int(next_cursor) if isinstance(next_cursor, int) else cursor

    @staticmethod
    def _normalize_event(raw: Any) -> dict | None:
        if not isinstance(raw, dict):
            return None
        kind = _bounded(raw.get("type"), 80)
        if not kind:
            return None
        data = raw.get("data") if isinstance(raw.get("data"), dict) else {}
        event = {"type": kind, "session_id": _bounded(raw.get("session_id"), 200) or None,
                 "cursor": raw.get("cursor") if isinstance(raw.get("cursor"), int) else None}
        if kind in ("permission.asked", "permission.updated"):
            # Canonical internal ask event is "permission.asked" (the real
            # OpenCode v1 ask contract). "permission.updated" is accepted only
            # as a compatibility alias at this boundary and normalized to the
            # same shape so callers have one ask branch.
            #
            # Real V1 ask fields: id, permission, patterns (requested),
            # always (exact proposed always scope), metadata, tool.
            # `pattern` (the Approve-always display) reflects `always`
            # exactly; `requested_patterns` stays separately reviewable.
            event["type"] = "permission.asked"

            def _str_list(value: Any, limit: int = 32) -> list[str]:
                if isinstance(value, str):
                    return [value[:400]]
                if isinstance(value, list):
                    return [_bounded(p, 400) for p in value if isinstance(p, str)][:limit]
                return []

            if "requested_patterns" in data:
                requested = _str_list(data.get("requested_patterns"))
            elif "patterns" in data:
                requested = _str_list(data.get("patterns"))
            elif "pattern" in data:
                requested = _str_list(data.get("pattern"))
            else:
                requested = []
            if "pattern" in data:
                scope = _str_list(data.get("pattern"))
            elif "always" in data:
                scope = _str_list(data.get("always"))
            elif "patterns" in data:
                scope = _str_list(data.get("patterns"))
            else:
                scope = ()
                scope = list(scope)
            raw_tool = data.get("tool")
            if isinstance(raw_tool, str):
                tool: Any = _bounded(raw_tool, 200)
            elif isinstance(raw_tool, (dict, list)):
                tool, _ = sanitize_metadata(raw_tool)
            else:
                tool = None
            permission_id = (_bounded(data.get("id"), 200) or _bounded(data.get("requestID"), 200)
                             or _bounded(data.get("requestId"), 200))
            action = (_bounded(data.get("action"), 120) or _bounded(data.get("permission"), 120)
                      or _bounded(data.get("type"), 120))
            call_id = (_bounded(data.get("call_id"), 200) or _bounded(data.get("callID"), 200)
                       or None)
            metadata, metadata_redacted = sanitize_metadata(data.get("metadata"))
            event["permission"] = {
                "id": permission_id,
                "session_id": _bounded(data.get("session_id"), 200) or event["session_id"],
                "action": action,
                "title": _bounded(data.get("title"), 300),
                "pattern": list(scope),
                "requested_patterns": list(requested),
                "tool": tool,
                "call_id": call_id,
                "metadata": metadata,
                "redacted": bool(data.get("redacted")) or metadata_redacted,
                "created": _bounded(data.get("created"), 60),
            }
        elif kind == "permission.replied":
            # Real V1 reply fields: requestID + reply. permissionID/response
            # remain only as compatibility fallbacks at this boundary.
            event["permission_id"] = (_bounded(data.get("permission_id"), 200)
                                      or _bounded(data.get("requestID"), 200)
                                      or _bounded(data.get("requestId"), 200)
                                      or _bounded(data.get("permissionID"), 200)
                                      or _bounded(data.get("permissionId"), 200)
                                      or _bounded(data.get("id"), 200))
            event["response"] = _bounded(data.get("response") or data.get("reply"), 40)
            if not event["session_id"]:
                event["session_id"] = (_bounded(data.get("session_id"), 200)
                                       or _bounded(data.get("sessionID"), 200) or None)
        elif kind == "session.error":
            event["error"] = {"name": _bounded(data.get("name"), 80),
                              "message": _bounded(data.get("message"), 300)}
        return event


def runtime_from_environment(environ: dict | None = None) -> OpenCodeRuntime | None:
    """Build the runtime client from local runtime configuration only.

    No credentials are read from MCP, project files, manager JSON or handoffs.
    """
    env = environ if environ is not None else os.environ
    url = (env.get("WB_OPENCODE_RUNTIME_URL") or "").strip()
    if not url:
        return None
    token = (env.get("WB_RUNTIME_TOKEN") or "").strip()
    return HttpOpenCodeRuntime(url, token)


def basic_auth_header(username: str, password: str) -> str:
    """Small helper for adapter configuration/tests; never logged or persisted."""
    raw = f"{username}:{password}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")
