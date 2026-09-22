"""Runtime-neutral agent boundary with the OpenCode backend installed.

Workspace Bridge never starts or supervises an agent server. In the Docker
deployment a small package-owned Node SDK adapter (``runtime/opencode-adapter``)
is the only process that holds provider/network credentials and talks to the
external host server. This module defines the generic internal contract
(``AgentRuntime`` / ``RuntimeCapabilities`` / ``RuntimeInteraction`` /
``RuntimeEvent``) and the currently installed OpenCode backend
(``HttpOpenCodeRuntime``, identity ``"opencode"``) that implements it.

The orchestrator programs against the neutral contract only. OpenCode-specific
transport details (endpoint names, V1/V2 permission generations, raw event
shapes) are normalized here. Sanitization happens here and again in the
orchestrator: no credentials, webhook URLs, external paths beyond the mapped
workspace root, or hidden reasoning may cross this boundary into persisted
state or MCP output.
"""
from __future__ import annotations

import base64
import json
import os
import re
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
    # Pi 3C1 enforcement/audit metadata (empty for OpenCode/legacy).
    # Persisted with the run: fingerprint(s), adapter/Pi versions and
    # permission revision identify the enforcement build without paths.
    enforcement_fingerprint: str = ""
    adapter_version: str = ""
    pi_version: str = ""
    policy_revision: str = ""
    # Pi 3C2 extension snapshot metadata (empty for OpenCode/legacy and
    # for Pi sessions created without enabled extensions). The revision
    # binds continuation; the snapshot rows carry id/name/version/
    # fingerprint with no host paths.
    extension_revision: str = ""
    extensions: list = field(default_factory=list)


@dataclass
class MessageInfo:
    id: str
    role: str
    created: int | None = None
    completed: int | None = None
    text: str = ""
    tools: tuple[str, ...] = ()
    error: str | None = None


#: Stable identity of the currently installed backend. Persisted per agent
#: run so a future multi-runtime bridge can refuse cross-runtime session
#: reuse fail-closed instead of attaching one backend's session to another.
OPENCODE_RUNTIME_ID = "opencode"


#: Canonical package-owned runtime identity grammar, shared by the MCP
#: schema and the registry boundary (defined here to avoid circular
#: imports). Configured ids must match exactly: lowercase alphanumeric
#: start, then up to 31 lowercase alphanumerics, dashes, or underscores.
RUNTIME_ID_PATTERN = r"^[a-z0-9][a-z0-9_-]{0,31}$"
_RUNTIME_ID_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")


def is_valid_runtime_id(value: object) -> bool:
    """Whether a value is a canonical package-known runtime id shape."""
    return isinstance(value, str) and _RUNTIME_ID_RE.fullmatch(value) is not None


@dataclass(frozen=True)
class RuntimeCapabilities:
    """Explicit capability advertisement for one AgentRuntime backend.

    The orchestrator must consult these flags instead of assuming every
    future backend supports the current OpenCode flows. All ``True`` defaults
    below describe the installed OpenCode adapter; unsupported futures stay
    ``False`` and fail closed (``runtime_unsupported``), never silently fall
    back. Deliberately unsupported today: ``question_response`` (the
    installed API exposes no question reply) and ``session_branching``.
    ``execution_history`` is the clean execution-history capability:
    Pi implements it, OpenCode remains unsupported and unchanged.
    """

    model_discovery: bool = True
    session_reuse: bool = True
    event_polling: bool = True
    session_status: bool = True
    pending_snapshot: bool = True
    permission_response: bool = True
    question_detection: bool = True
    question_response: bool = False
    session_branching: bool = False
    execution_history: bool = False
    # 3C2: native extension inventory (GET /extensions). Pi implements
    # it; OpenCode remains unsupported and unchanged.
    extension_inventory: bool = False


@dataclass
class RuntimeInteraction:
    """Generic normalized pending-interaction representation.

    Covers the current permission and question cases behind one type so the
    orchestrator never depends on backend-specific classes. ``kind`` is
    ``"permission"`` or ``"question"``.

    Permission semantics: ``pattern`` holds exactly the backend's proposed
    always scope (never synthesized; empty means ``always`` must fail
    closed), ``requested_patterns`` holds what is being requested,
    ``generation`` is opaque backend reply-routing metadata used verbatim.
    Question semantics: only ``question_count``/``call_id`` cross the
    boundary; question bodies, options and answers are never carried.
    """

    id: str
    session_id: str
    kind: str = "permission"
    action: str = ""
    title: str = ""
    pattern: tuple[str, ...] = ()
    requested_patterns: tuple[str, ...] = ()
    tool: Any = None
    call_id: str | None = None
    metadata: dict = field(default_factory=dict)
    redacted: bool = False
    created: str = ""
    generation: str = "v1"
    question_count: int = 0

    def scope_text(self) -> str:
        return ", ".join(self.pattern)

    # Read-only dict-style access for legacy call sites/tests that index
    # normalized interactions (``item["id"]``). New code uses attributes.
    def __getitem__(self, key: str) -> Any:
        values = {
            "id": self.id, "session_id": self.session_id, "kind": self.kind,
            "action": self.action, "title": self.title,
            "pattern": list(self.pattern),
            "requested_patterns": list(self.requested_patterns),
            "tool": self.tool, "call_id": self.call_id,
            "metadata": self.metadata, "redacted": self.redacted,
            "created": self.created, "generation": self.generation,
            "question_count": self.question_count,
        }
        if key in values:
            return values[key]
        raise KeyError(key)

    def get(self, key: str, default: Any = None) -> Any:
        try:
            return self[key]
        except KeyError:
            return default


@dataclass
class PendingPermission(RuntimeInteraction):
    """Thin compatibility subclass for the permission case (kind preset)."""

    kind: str = "permission"


@dataclass
class PendingQuestion(RuntimeInteraction):
    """One official pending question request owned by exactly one session.

    Verified against installed @opencode-ai/sdk 1.18.31: the V2
    session-scoped snapshot (GET /api/session/{sessionID}/question)
    returns Array<QuestionV2Request> = {id, sessionID,
    questions: [{question, header, options, multiple, custom}],
    tool?: {messageID, callID}}. Only the request id, owning session,
    question count and tool call reference cross this boundary: question
    bodies, headers, options and answers are never carried, persisted or
    logged by the bridge.
    """

    kind: str = "question"


@dataclass
class RuntimeEvent:
    """Small normalized event representation at the runtime boundary.

    The orchestrator consumes these, never raw backend event dicts.
    ``type`` is the canonical kind (``permission.asked``,
    ``permission.replied``, ``session.idle``, ``session.status``,
    ``session.error``, or any ``*question*`` hint). ``permission`` carries
    the normalized ask; ``permission_id``/``response`` carry the reply;
    ``status`` carries the bounded native idle/busy/retry hint; ``error``
    and ``data`` carry bounded sanitized payloads only. ``cursor`` preserves
    stream order. No raw provider payload, credentials, hidden reasoning,
    question content or unsafe paths are carried.
    """

    type: str
    session_id: str | None = None
    cursor: int | None = None
    status: str = ""
    data: dict = field(default_factory=dict)
    error: dict = field(default_factory=dict)
    permission: RuntimeInteraction | None = None
    permission_id: str | None = None
    response: str | None = None
    event_id: str | None = None

    # Read-only dict-style access for legacy call sites/tests that index
    # normalized events (``event["permission"]["id"]``). New code uses
    # attributes.
    def __getitem__(self, key: str) -> Any:
        values = {
            "type": self.type, "session_id": self.session_id,
            "cursor": self.cursor, "status": self.status, "data": self.data,
            "error": self.error, "permission": self.permission,
            "permission_id": self.permission_id, "response": self.response,
            "id": self.event_id,
        }
        if key in values:
            return values[key]
        raise KeyError(key)

    def get(self, key: str, default: Any = None) -> Any:
        try:
            return self[key]
        except KeyError:
            return default

    def public_dict(self) -> dict:
        """JSON-serializable sanitized view (diagnostics/tests only)."""
        permission = self.permission
        return {
            "type": self.type, "session_id": self.session_id,
            "cursor": self.cursor, "status": self.status, "data": self.data,
            "error": self.error,
            "permission": None if permission is None else {
                "id": permission.id, "session_id": permission.session_id,
                "kind": permission.kind, "action": permission.action,
                "title": permission.title, "pattern": list(permission.pattern),
                "requested_patterns": list(permission.requested_patterns),
                "tool": permission.tool, "call_id": permission.call_id,
                "metadata": permission.metadata, "redacted": permission.redacted,
                "created": permission.created, "generation": permission.generation,
                "question_count": permission.question_count},
            "permission_id": self.permission_id, "response": self.response,
            "id": self.event_id,
        }


class AgentRuntime:
    """Generic runtime-neutral transport contract; tests substitute a scripted fake.

    Backends expose their stable ``runtime_id``, explicit
    ``capabilities``, and normalized ``RuntimeInteraction``/``RuntimeEvent``
    objects. The orchestrator programs against this contract only and must
    never need backend endpoint names, raw event dicts, or
    backend-specific pending classes.
    """

    name = "runtime"

    @property
    def runtime_id(self) -> str:
        """Stable backend identity persisted per agent run (e.g. ``"opencode"``)."""
        raise NotImplementedError

    @property
    def capabilities(self) -> RuntimeCapabilities:
        """Explicit capability advertisement; unsupported paths fail closed."""
        raise NotImplementedError

    def health(self) -> dict:
        raise NotImplementedError

    def list_models(self, directory: str | None = None) -> list[ModelInfo]:
        """List models known to the backend.

        ``directory`` is workspace-scoped discovery context. The installed
        OpenCode backend ignores it (global discovery); workspace-scoped
        backends such as Pi require a non-empty directory and fail closed
        without one.
        """
        raise NotImplementedError

    def create_session(self, directory: str, title: str,
                       options: dict | None = None) -> SessionInfo:
        raise NotImplementedError

    def get_session(self, directory: str, session_id: str) -> SessionInfo | None:
        raise NotImplementedError

    def prompt_async(self, directory: str, session_id: str, text: str,
                     model: dict | None = None) -> None:
        raise NotImplementedError

    def messages(self, directory: str, session_id: str, limit: int = 40) -> list[MessageInfo]:
        raise NotImplementedError

    def respond_permission(self, directory: str, session_id: str, permission_id: str,
                           response: str, generation: str = "v1") -> bool:
        raise NotImplementedError

    def abort_session(self, directory: str, session_id: str) -> bool:
        raise NotImplementedError

    def session_status(self, directory: str, session_id: str) -> str:
        """Return the native session status: idle, busy or retry.

        A missing map entry is idle under the installed OpenCode v1.18.31
        semantics; malformed/unknown statuses raise so callers fail closed.
        """
        raise NotImplementedError

    def list_pending_permissions(self, directory: str, session_id: str) -> list[RuntimeInteraction]:
        """Return the backend's currently pending permissions for exactly one session.

        Used only to recover a permission.asked event that was never
        observed. Transport/API failures raise (fail closed) and must never
        be treated as an empty list; absence from a successful list never
        resolves an already persisted request.
        """
        raise NotImplementedError

    def list_pending_questions(self, directory: str, session_id: str) -> list[RuntimeInteraction]:
        """Return the backend's currently pending questions for exactly one session.

        Official V2 session-scoped snapshot only
        (GET /api/session/{sessionID}/question). Used only to recover a
        question.asked event that was never observed. Transport/API
        failures raise (fail closed) and must never be treated as an empty
        list; absence from a successful list never resolves an already
        persisted request.
        """
        raise NotImplementedError

    def poll_events(self, cursor: int, timeout: float = 25.0) -> tuple[list[RuntimeEvent], int]:
        raise NotImplementedError

    def read_executions(self, directory: str, session_id: str, *,
                        after: int = 0, limit: int = 50) -> dict:
        """Clean execution-history journal read (3C1).

        Pi implements it via the token-authenticated exact-session
        endpoint; OpenCode remains unsupported and unchanged
        (RuntimeUnsupported, no HTTP call).
        """
        raise NotImplementedError

    def close(self) -> None:
        return None


class OpenCodeRuntime(AgentRuntime):
    """Compatibility base for the OpenCode backend; new code uses AgentRuntime."""

    name = "runtime"

    @property
    def runtime_id(self) -> str:
        return OPENCODE_RUNTIME_ID

    @property
    def capabilities(self) -> RuntimeCapabilities:
        return RuntimeCapabilities()

    def read_executions(self, directory: str, session_id: str, *,
                        after: int = 0, limit: int = 50) -> dict:
        """OpenCode has no execution history (unchanged)."""
        raise RuntimeUnsupported("OpenCode runtime does not support execution history")


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
            if exc.code in (400, 409, 412, 422):
                # 400 covers an upstream GET /permission encoding failure:
                # a rejection, never an empty permission list.
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
                "cursor": value.get("cursor") if isinstance(value.get("cursor"), int) else 0,
                "event_stream": self._normalize_event_stream(value.get("event_stream"))}

    @staticmethod
    def _normalize_event_stream(raw: Any) -> dict | None:
        # Sanitized adapter EventHub health only: status/transitions/timing.
        # Never event contents or secrets. Unknown shapes -> None (unknown),
        # never a fabricated "subscribed".
        if not isinstance(raw, dict):
            return None
        status = raw.get("status")
        if not isinstance(status, str):
            return None
        status = _bounded(status, 40)
        if status not in ("starting", "subscribed", "reconnecting", "degraded"):
            status = "degraded" if status else "starting"
            if not status:
                return None
        transitions = raw.get("transitions")
        failures = raw.get("consecutiveFailures", raw.get("consecutive_failures"))

        def _count(*names: str) -> int | None:
            for name in names:
                value = raw.get(name)
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                    return value
            return None

        def _moment(*names: str) -> str | None:
            for name in names:
                value = raw.get(name)
                if isinstance(value, str) and value:
                    return _bounded(value, 60)
            return None

        return {"status": status,
                "transitions": transitions if isinstance(transitions, int) and transitions >= 0 else 0,
                "last_transition": _bounded(raw.get("lastTransition", raw.get("last_transition")), 60)
                if isinstance(raw.get("lastTransition", raw.get("last_transition")), str) else None,
                "consecutive_failures": failures if isinstance(failures, int) and failures >= 0 else 0,
                # Functional event-stream health: bounded counters/timestamps
                # only, never event contents. Absent on older adapters
                # (unknown, never fabricated).
                "raw_event_count": _count("rawEventCount", "raw_event_count"),
                "control_event_count": _count("controlEventCount", "control_event_count"),
                "functional_event_count": _count("functionalEventCount", "functional_event_count"),
                "last_raw_event_at": _moment("lastRawEventAt", "last_raw_event_at"),
                "last_functional_event_at": _moment("lastFunctionalEventAt",
                                                   "last_functional_event_at")}

    def list_models(self, directory: str | None = None) -> list[ModelInfo]:
        # Global discovery: the adapter /models endpoint takes no workspace
        # directory. Model availability never depends on a workspace. The
        # optional directory is accepted for the directory-aware contract
        # and deliberately ignored here.
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

    def create_session(self, directory: str, title: str,
                       options: dict | None = None) -> SessionInfo:
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
                           response: str, generation: str = "v1") -> bool:
        if response not in ("once", "always", "reject"):
            raise BridgeError("Permission response must be once, always or reject", "invalid_arguments")
        if generation not in ("v1", "v2"):
            raise BridgeError("Unknown permission generation for reply routing", "invalid_arguments")
        value = self._request(
            "POST",
            f"/sessions/{urllib.parse.quote(session_id)}/permissions/{urllib.parse.quote(permission_id)}",
            body={"directory": directory, "response": response, "generation": generation})
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

    # Last pending-list source reported by the adapter ("v2" primary or
    # "v1" compatibility fallback). Set by list_pending_permissions; read by
    # the orchestrator for generation-aware diagnostics. None when unknown.
    last_permission_source: str | None = None

    def list_pending_permissions(self, directory: str, session_id: str) -> list[RuntimeInteraction]:
        if not session_id:
            raise RuntimeUnavailable("OpenCode permission list required a session id")
        value = self._request("GET", f"/sessions/{urllib.parse.quote(session_id)}/permissions",
                              query={"directory": directory})
        rows = value.get("permissions") if isinstance(value, dict) else None
        if not isinstance(rows, list):
            raise RuntimeUnavailable("OpenCode permission list was invalid")
        result: list[PendingPermission] = []
        for row in rows[:100]:
            permission = self._normalize_pending(row, session_id)
            if permission is not None:
                result.append(permission)
        reported = value.get("source") if isinstance(value, dict) else None
        if reported in ("v1", "v2"):
            self.last_permission_source = reported
        elif any(p.generation == "v2" for p in result):
            self.last_permission_source = "v2"
        elif result:
            self.last_permission_source = "v1"
        else:
            self.last_permission_source = None
        return result

    # Last pending-list source reported by the adapter ("v2" primary or
    # "v1" compatibility fallback). Set by list_pending_questions; read by
    # the orchestrator for source-aware diagnostics. None when unknown.
    last_question_source: str | None = None

    def list_pending_questions(self, directory: str, session_id: str) -> list[RuntimeInteraction]:
        if not session_id:
            raise RuntimeUnavailable("OpenCode question list required a session id")
        value = self._request("GET", f"/sessions/{urllib.parse.quote(session_id)}/questions",
                              query={"directory": directory})
        rows = value.get("questions") if isinstance(value, dict) else None
        if not isinstance(rows, list):
            raise RuntimeUnavailable("OpenCode question list was invalid")
        result: list[PendingQuestion] = []
        for row in rows[:100]:
            question = self._normalize_pending_question(row, session_id)
            if question is not None:
                result.append(question)
        reported = value.get("source") if isinstance(value, dict) else None
        self.last_question_source = reported if reported in ("v1", "v2") else None
        return result

    @staticmethod
    def _normalize_pending_question(raw: Any, session_id: str) -> PendingQuestion | None:
        # Canonical pending-question item from the adapter (same shape as
        # the official V2 snapshot row): strict session scoping, bounded
        # counts/references only. Question bodies, headers, options and
        # answers never cross this boundary.
        if not isinstance(raw, dict):
            return None
        question_id = _bounded(raw.get("id"), 200)
        owner = _bounded(raw.get("session_id"), 200)
        if not question_id or owner != session_id:
            return None
        try:
            count = int(raw.get("question_count") or 0)
        except (TypeError, ValueError):
            return None
        if count < 0:
            return None
        call_id = _bounded(raw.get("call_id"), 200) or None
        return PendingQuestion(id=question_id, session_id=owner,
                               question_count=min(count, 100), call_id=call_id)

    @staticmethod
    def _normalize_pending(raw: Any, session_id: str) -> PendingPermission | None:
        # Canonical pending-permission item from the adapter (same shape as
        # the normalized ask event): strict session scoping, bounded and
        # sanitized fields, exact always scope preserved in `pattern`.
        if not isinstance(raw, dict):
            return None
        permission_id = _bounded(raw.get("id"), 200)
        owner = _bounded(raw.get("session_id"), 200)
        if not permission_id or owner != session_id:
            return None

        def _str_list(value: Any, limit: int = 32) -> list[str]:
            if isinstance(value, str):
                return [value[:400]]
            if isinstance(value, list):
                return [_bounded(p, 400) for p in value if isinstance(p, str)][:limit]
            return []

        if "requested_patterns" in raw:
            requested = _str_list(raw.get("requested_patterns"))
        elif "patterns" in raw:
            requested = _str_list(raw.get("patterns"))
        elif "resources" in raw:
            # Defensive V2 raw shape: resources are the requested targets.
            requested = _str_list(raw.get("resources"))
        else:
            requested = _str_list(raw.get("pattern"))
        if "pattern" in raw:
            scope = _str_list(raw.get("pattern"))
        elif "always" in raw:
            scope = _str_list(raw.get("always"))
        elif "save" in raw:
            # Defensive V2 raw shape: save is the exact proposed always scope.
            scope = _str_list(raw.get("save"))
        else:
            scope = []
        raw_tool = raw.get("tool")
        if isinstance(raw_tool, str):
            tool: Any = _bounded(raw_tool, 200)
        elif isinstance(raw_tool, (dict, list)):
            tool, _ = sanitize_metadata(raw_tool)
        else:
            tool = None
        raw_source = raw.get("source")
        if tool is None and isinstance(raw_source, dict):
            tool, _ = sanitize_metadata(raw_source)
        call_id = (_bounded(raw.get("call_id"), 200) or _bounded(raw.get("callID"), 200)
                   or None)
        if call_id is None and isinstance(raw_source, dict):
            call_id = (_bounded(raw_source.get("callID"), 200)
                       or _bounded(raw_source.get("call_id"), 200) or None)
        metadata, metadata_redacted = sanitize_metadata(raw.get("metadata"))
        if not isinstance(metadata, dict):
            metadata = {}
        generation = raw.get("generation")
        if generation not in ("v1", "v2"):
            # Backward compatibility: rows persisted or served without the
            # generation marker are V1. Anything else fails closed upstream
            # (reply routing rejects unknown generations explicitly).
            generation = "v1"
        return PendingPermission(
            id=permission_id, session_id=owner,
            action=(_bounded(raw.get("action"), 120) or _bounded(raw.get("permission"), 120)
                    or _bounded(raw.get("type"), 120)),
            title=_bounded(raw.get("title"), 300),
            pattern=tuple(scope), requested_patterns=tuple(requested), tool=tool,
            call_id=call_id, metadata=metadata,
            redacted=bool(raw.get("redacted")) or metadata_redacted,
            created=_bounded(raw.get("created"), 60),
            generation=generation)

    def poll_events(self, cursor: int, timeout: float = 25.0) -> tuple[list[RuntimeEvent], int]:
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
    def _normalize_event(raw: Any) -> RuntimeEvent | None:
        """Normalize one raw adapter event; compatibility wrapper around coerce_runtime_event."""
        return coerce_runtime_event(raw)

    @staticmethod
    def _coerce_event(raw: Any) -> RuntimeEvent | None:
        return coerce_runtime_event(raw)


#: Stable identity of the Pi native host-adapter backend (milestone 3A).
#: Persisted per agent run so orchestrators refuse cross-runtime session
#: reuse fail-closed instead of attaching one backend's session to another.
PI_RUNTIME_ID = "pi"


class HttpPiRuntime(AgentRuntime):
    """Bounded HTTP client for the native macOS Pi host adapter (3A1 contract).

    Audited 3A1 surface only: GET /health, GET /models?directory=...,
    POST /sessions, GET /sessions/:id, GET /sessions/:id/status,
    POST /sessions/:id/prompt-async, GET /sessions/:id/messages,
    POST /sessions/:id/abort. The adapter exposes no events, permission /
    question snapshots, or reply endpoints in this milestone, so those
    capability-gated methods raise ``RuntimeUnsupported`` without any HTTP
    call. Model discovery is workspace-scoped: a non-empty directory is
    required and passed as the ``directory`` query parameter.
    """

    name = "http-pi-adapter"

    # Static client contract (network-free): the Bridge client implements
    # the permission snapshot/reply + execution-history calls. Whether the
    # CONNECTED adapter actually serves them is a separate deployed
    # capability, probed via /health (see health()/permissions_supported
    # and execution_supported): static True here never proves an old
    # adapter supports the surface.
    PI_CAPABILITIES = RuntimeCapabilities(
        model_discovery=True,
        session_reuse=True,
        event_polling=False,
        session_status=True,
        pending_snapshot=True,
        permission_response=True,
        question_detection=False,
        question_response=False,
        session_branching=False,
        execution_history=True,
        extension_inventory=True,
    )

    def __init__(self, base_url: str, token: str = "", *,
                 connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
                 read_timeout: float = DEFAULT_READ_TIMEOUT):
        parsed = urllib.parse.urlparse(base_url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise BridgeError("WB_PI_RUNTIME_URL must be an http(s) URL")
        self.base_url = base_url.rstrip("/")
        self.token = token or ""
        self.connect_timeout = min(max(float(connect_timeout), 0.5), MAX_RUNTIME_TIMEOUT)
        self.read_timeout = min(max(float(read_timeout), 0.5), MAX_RUNTIME_TIMEOUT)

    @property
    def runtime_id(self) -> str:
        return PI_RUNTIME_ID

    @property
    def capabilities(self) -> RuntimeCapabilities:
        return self.PI_CAPABILITIES

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
            if exc.code in (400, 409, 412, 422):
                raise RuntimeRejected(_bounded(detail, 300) or "Runtime rejected the request",
                                      status=exc.code) from None
            if exc.code == 501:
                raise RuntimeUnsupported(_bounded(detail, 300) or "Runtime capability unsupported") from None
            if exc.code in (404,):
                raise RuntimeRejected("Runtime resource not found", "not_found", status=404) from None
            raise RuntimeUnavailable("Pi runtime returned an error") from None
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError):
            raise RuntimeUnavailable("Pi runtime is unavailable") from None
        except (ValueError, UnicodeError):
            raise RuntimeUnavailable("Pi runtime returned an invalid response") from None

    def health(self) -> dict:
        value = self._request("GET", "/health", timeout=self.connect_timeout)
        if not isinstance(value, dict):
            raise RuntimeUnavailable("Pi runtime health response was invalid")
        # Generic bounded fields only: never paths, tokens, or adapter internals.
        # deployed_capabilities mirrors the adapter-advertised permission +
        # execution-history surface (all default False for old adapters that
        # omit the block); permissions_supported and execution_supported
        # derive from it. No version-string comparison is used anywhere.
        raw_caps = value.get("capabilities")
        caps = raw_caps if isinstance(raw_caps, dict) else {}
        # Strict booleans only: truthy non-booleans (e.g. "yes") never
        # count as deployed support.
        deployed = {"pending_snapshot": caps.get("pending_snapshot") is True,
                    "permission_response": caps.get("permission_response") is True,
                    "execution_history": caps.get("execution_history") is True,
                    "extension_inventory": caps.get("extension_inventory") is True}
        fingerprint = value.get("enforcement_fingerprint")
        out: dict[str, Any] = {"ok": bool(value.get("ok")),
                "version": _bounded(value.get("pi_version"), 80),
                "adapter_version": _bounded(value.get("adapter_version"), 40),
                "locked": bool(value.get("locked")),
                "instance": _bounded(value.get("instance"), 80),
                "status": _bounded(value.get("status"), 40),
                "deployed_capabilities": deployed,
                "permissions_supported": bool(deployed["pending_snapshot"]
                                              and deployed["permission_response"]),
                "execution_supported": bool(deployed["execution_history"]),
                "extension_inventory_supported": bool(deployed["extension_inventory"])}
        if isinstance(fingerprint, str) and len(fingerprint) == 64:
            out["enforcement_fingerprint"] = fingerprint
        return out

    def _deployed_permissions_supported(self) -> bool:
        """Whether the CONNECTED adapter serves the permission surface.

        Probes current adapter /health; any probe failure or missing
        capability fails closed to False. Old adapters (no capabilities
        block) are never treated as permission-capable.
        """
        try:
            return bool(self.health().get("permissions_supported"))
        except (RuntimeUnavailable, RuntimeRejected, RuntimeUnsupported):
            return False

    def _require_permissions_supported(self) -> None:
        if not self._deployed_permissions_supported():
            raise RuntimeUnsupported("Pi adapter does not support permission endpoints")

    def _deployed_execution_supported(self) -> bool:
        """Whether the CONNECTED adapter serves execution history.

        Distinct from the static client capability: probed via /health.
        """
        try:
            return bool(self.health().get("execution_supported"))
        except (RuntimeUnavailable, RuntimeRejected, RuntimeUnsupported):
            return False

    def _require_execution_supported(self) -> None:
        if not self._deployed_execution_supported():
            raise RuntimeUnsupported("Pi adapter does not support execution history")

    def _deployed_extension_inventory_supported(self) -> bool:
        """Whether the CONNECTED adapter serves extension inventory.

        Probed via /health; any probe failure or missing capability fails
        closed to False. Old adapters (no capability block) are never
        treated as inventory-capable.
        """
        try:
            return bool(self.health().get("extension_inventory_supported"))
        except (RuntimeUnavailable, RuntimeRejected, RuntimeUnsupported):
            return False

    def _require_extension_inventory_supported(self) -> None:
        if not self._deployed_extension_inventory_supported():
            raise RuntimeUnsupported("Pi adapter does not support extension inventory")

    def list_extensions(self) -> list[dict]:
        """Bounded native extension inventory (3C2).

        Token-authenticated GET /extensions (global, no workspace path).
        Requires deployed adapter support first; old adapters fail cleanly
        with RuntimeUnsupported. Rows are strictly normalized here: only
        bounded id/name/version/extension declarations/fingerprints and
        supported/reason flags cross this boundary. No host paths,
        agentDir, tokens, settings fields, file contents, or dependency
        lists are ever carried.
        """
        from .pi_extensions import normalize_inventory_row
        self._require_extension_inventory_supported()
        value = self._request("GET", "/extensions", timeout=self.connect_timeout)
        rows = value.get("packages") if isinstance(value, dict) else None
        if not isinstance(rows, list):
            raise RuntimeUnavailable("Pi extension list was invalid")
        result: list[dict] = []
        for row in rows[:200]:
            normalized = normalize_inventory_row(row)
            if normalized is not None:
                result.append(normalized)
        return result

    def list_models(self, directory: str | None = None) -> list[ModelInfo]:
        # Workspace-scoped discovery: the adapter resolves Pi configuration
        # per workspace directory. Fail closed without one.
        if not isinstance(directory, str) or not directory:
            raise BridgeError("Pi model discovery requires a workspace directory",
                              "invalid_arguments")
        value = self._request("GET", "/models", query={"directory": directory})
        rows = value.get("models") if isinstance(value, dict) else None
        if not isinstance(rows, list):
            raise RuntimeUnavailable("Pi model list was invalid")
        result: list[ModelInfo] = []
        for row in rows[:2000]:
            if not isinstance(row, dict):
                continue
            provider = _bounded(row.get("provider"), 120)
            model = _bounded(row.get("id", row.get("model")), 200)
            if not provider or not model:
                continue
            result.append(ModelInfo(selector=f"{provider}/{model}",
                                    provider=provider, model=model,
                                    name=_bounded(row.get("name"), 200) or model,
                                    default=False, variants=()))
        return result

    def create_session(self, directory: str, title: str,
                       options: dict | None = None) -> SessionInfo:
        # 3C1: Bridge is the source of truth for the configured Pi
        # permission policy (v3). The exact policy snapshot + revision
        # travel in the internal session-creation body (never MCP). options
        # carries {"permission_policy": {...}, "policy_revision": "..."}.
        # 3C2: the extension policy snapshot + revision travel alongside
        # ({"extension_policy": {...}, "extension_revision": "..."}).
        body: dict[str, Any] = {"directory": directory, "title": title}
        if options:
            snapshot = options.get("permission_policy")
            revision = options.get("policy_revision")
            if isinstance(snapshot, dict) and isinstance(revision, str) and revision:
                body["permission_policy"] = snapshot
                body["policy_revision"] = revision
            ext_snapshot = options.get("extension_policy")
            ext_revision = options.get("extension_revision")
            if isinstance(ext_snapshot, dict) and isinstance(ext_revision, str) and ext_revision:
                body["extension_policy"] = ext_snapshot
                body["extension_revision"] = ext_revision
        # Every managed v3 session loads the trusted permission extension
        # and requires BOTH deployed permission and execution-history
        # support BEFORE any session exists, so normal sessions cannot
        # silently run unaudited. Old adapters get RuntimeUnsupported and
        # no session is created (never a silent unmanaged fallback). Only
        # legacy no-policy creation stays compatible and reports
        # not_recorded downstream.
        # 3C2: a managed session with a non-empty enabled extension set
        # additionally requires deployed extension-inventory support, so
        # configured extensions are never silently omitted. An empty
        # extension set keeps the old 3C1 compatibility behavior.
        if isinstance(body.get("permission_policy"), dict):
            self._require_permissions_supported()
            self._require_execution_supported()
            ext_body = body.get("extension_policy")
            if isinstance(ext_body, dict) and len(ext_body.get("enabled") or []) > 0:
                self._require_extension_inventory_supported()
        value = self._request("POST", "/sessions", body=body)
        session = value.get("session") if isinstance(value, dict) else None
        if not isinstance(session, dict) or not session.get("id"):
            raise RuntimeUnavailable("Pi session creation returned no session id")
        fingerprint = session.get("enforcement_fingerprint")
        if not isinstance(fingerprint, str):
            fingerprint = value.get("enforcement_fingerprint") if isinstance(value, dict) else ""
        if not isinstance(fingerprint, str):
            fingerprint = ""
        ext_revision_out = session.get("extension_revision")
        if not isinstance(ext_revision_out, str):
            ext_revision_out = ""
        ext_snapshot_out = session.get("extensions")
        if not isinstance(ext_snapshot_out, list):
            ext_snapshot_out = []
        bounded_snapshot = []
        for row in ext_snapshot_out[:64]:
            if not isinstance(row, dict):
                continue
            ident = row.get("id") if isinstance(row.get("id"), str) else ""
            if not ident:
                continue
            bounded_snapshot.append({
                "id": ident[:218],
                "name": row.get("name")[:214] if isinstance(row.get("name"), str) else "",
                "version": row.get("version")[:80] if isinstance(row.get("version"), str) else "",
                "fingerprint": row.get("fingerprint")[:64]
                if isinstance(row.get("fingerprint"), str) else "",
            })
        return SessionInfo(id=_bounded(session["id"], 200),
                           directory=_bounded(session.get("directory"), 1024),
                           title=_bounded(session.get("title"), 200),
                           enforcement_fingerprint=fingerprint[:64] if len(fingerprint) == 64 else "",
                           adapter_version=_bounded(session.get("adapter_version")
                                                    or (value.get("adapter_version") if isinstance(value, dict) else ""), 40),
                           pi_version=_bounded(session.get("pi_version")
                                               or (value.get("pi_version") if isinstance(value, dict) else ""), 80),
                           policy_revision=_bounded(session.get("policy_revision") or "", 64),
                           extension_revision=_bounded(ext_revision_out, 64),
                           extensions=bounded_snapshot)

    # Deployed capability negotiation (see _require_permissions_supported):
    # permission list/respond require the connected adapter to advertise
    # both permission capabilities. Anything older fails cleanly with
    # RuntimeUnsupported before touching the permission endpoints, so an
    # old adapter is never treated as permission-capable and absence from
    # a snapshot never resolves a persisted request.

    def get_session(self, directory: str, session_id: str) -> SessionInfo | None:
        try:
            value = self._request("GET", f"/sessions/{urllib.parse.quote(session_id)}",
                                  query={"directory": directory})
        except RuntimeRejected as exc:
            if exc.code == "not_found":
                return None
            raise
        # None ONLY for a genuine 404 above. A successful 2xx payload must
        # carry a valid session object; a malformed shape is a protocol
        # violation (never absence: absence must never silently
        # orphan/reconcile a run).
        if not isinstance(value, dict):
            raise RuntimeUnavailable("Pi session response was invalid")
        session = value.get("session")
        if not isinstance(session, dict) or not session.get("id"):
            raise RuntimeUnavailable("Pi session response was invalid")
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
            raise RuntimeUnavailable("Pi runtime did not accept the prompt")

    def messages(self, directory: str, session_id: str, limit: int = 40) -> list[MessageInfo]:
        value = self._request("GET", f"/sessions/{urllib.parse.quote(session_id)}/messages",
                              query={"directory": directory, "limit": max(1, min(int(limit), 100))})
        rows = value.get("messages") if isinstance(value, dict) else None
        if not isinstance(rows, list):
            raise RuntimeUnavailable("Pi message list was invalid")
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

    def session_status(self, directory: str, session_id: str) -> str:
        value = self._request("GET", f"/sessions/{urllib.parse.quote(session_id)}/status",
                              query={"directory": directory})
        status = value.get("status") if isinstance(value, dict) else None
        if status in ("idle", "busy"):
            return status
        raise RuntimeUnavailable("Pi session status was invalid")

    def abort_session(self, directory: str, session_id: str) -> bool:
        value = self._request("POST", f"/sessions/{urllib.parse.quote(session_id)}/abort",
                              body={"directory": directory})
        return bool(value.get("ok")) if isinstance(value, dict) else False

    # 3B1 exposes exact-session permission snapshots and replies on the
    # deployed adapter. Event polling and questions stay unsupported and
    # fail closed without any HTTP call.

    def poll_events(self, cursor: int, timeout: float = 25.0) -> tuple[list[RuntimeEvent], int]:
        raise RuntimeUnsupported("Pi runtime does not support event polling")

    def list_pending_questions(self, directory: str, session_id: str) -> list[RuntimeInteraction]:
        raise RuntimeUnsupported("Pi runtime does not expose pending questions")

    def list_pending_permissions(self, directory: str, session_id: str) -> list[RuntimeInteraction]:
        """Exact-session pending permission snapshot from the Pi adapter.

        Requires deployed adapter support first (old adapters fail
        cleanly with RuntimeUnsupported, never an empty claim). Fail
        closed (raise) on transport/API failures; absence from a
        successful list never resolves an already persisted request.
        """
        self._require_permissions_supported()
        if not session_id:
            raise RuntimeUnavailable("Pi permission list required a session id")
        value = self._request("GET", f"/sessions/{urllib.parse.quote(session_id)}/permissions",
                              query={"directory": directory})
        rows = value.get("permissions") if isinstance(value, dict) else None
        if not isinstance(rows, list):
            raise RuntimeUnavailable("Pi permission list was invalid")
        result: list[PendingPermission] = []
        for row in rows[:100]:
            permission = self._normalize_pi_pending(row, session_id)
            if permission is not None:
                result.append(permission)
        return result

    @staticmethod
    def _normalize_pi_pending(raw: Any, session_id: str) -> PendingPermission | None:
        # Canonical Pi pending-permission item from the adapter: strict
        # session scoping, bounded sanitized fields only. No raw args, file
        # contents, reasoning, tokens, or outside paths cross this boundary.
        if not isinstance(raw, dict):
            return None
        permission_id = _bounded(raw.get("id"), 200)
        owner = _bounded(raw.get("session_id"), 200)
        if not permission_id or owner != session_id:
            return None

        def _str_list(value: Any, limit: int = 32) -> list[str]:
            if isinstance(value, str):
                return [value[:400]]
            if isinstance(value, list):
                return [_bounded(p, 400) for p in value if isinstance(p, str)][:limit]
            return []

        requested = _str_list(raw.get("requested"))
        if not requested:
            requested = _str_list(raw.get("requested_patterns"))
        scope = _str_list(raw.get("always_pattern"))
        if not scope:
            scope = _str_list(raw.get("pattern"))
        raw_tool = raw.get("tool")
        tool: Any = _bounded(raw_tool, 200) if isinstance(raw_tool, str) else None
        call_id = _bounded(raw.get("tool_call_id"), 200) or None
        metadata, metadata_redacted = sanitize_metadata(raw.get("metadata"))
        if not isinstance(metadata, dict):
            metadata = {}
        return PendingPermission(
            id=permission_id, session_id=owner,
            action=_bounded(raw.get("action"), 120),
            title=_bounded(raw.get("title"), 300),
            pattern=tuple(scope), requested_patterns=tuple(requested), tool=tool,
            call_id=call_id, metadata=metadata,
            redacted=bool(raw.get("redacted")) or metadata_redacted,
            created=_bounded(raw.get("created"), 60),
            generation="v1")

    def respond_permission(self, directory: str, session_id: str, permission_id: str,
                           response: str, generation: str = "v1") -> bool:
        if response not in ("once", "always", "reject"):
            raise BridgeError("Permission response must be once, always or reject", "invalid_arguments")
        self._require_permissions_supported()
        value = self._request(
            "POST",
            f"/sessions/{urllib.parse.quote(session_id)}/permissions/{urllib.parse.quote(permission_id)}/respond",
            body={"directory": directory, "response": response})
        return bool(value.get("ok")) if isinstance(value, dict) else False

    def read_executions(self, directory: str, session_id: str, *,
                        after: int = 0, limit: int = 50) -> dict:
        """Exact-session execution journal read (3C1).

        Token-authenticated GET /sessions/:id/executions. Returns bounded
        normalized updates plus next/head/oldest cursor evidence. Poll
        failures raise (never imply no executions); evicted history
        reports audit_gap/cursor_too_old, never silent completeness.
        """
        self._require_execution_supported()
        if not session_id:
            raise RuntimeUnavailable("Pi execution read required a session id")
        value = self._request(
            "GET", f"/sessions/{urllib.parse.quote(session_id)}/executions",
            query={"directory": directory, "after": max(0, int(after)),
                   "limit": max(1, min(int(limit), 100))})
        if not isinstance(value, dict) or not isinstance(value.get("updates"), list):
            raise RuntimeUnavailable("Pi execution list was invalid")
        return value


def coerce_runtime_event(raw: Any) -> RuntimeEvent | None:
    """Normalize one raw adapter event dict into a RuntimeEvent.

    Neutral boundary helper shared by ``HttpOpenCodeRuntime`` (whose
    ``_normalize_event`` delegates here) and the orchestrator's
    compatibility path for scripted/test events. Returns ``None`` for
    malformed input. Permission asks become a normalized
    ``RuntimeInteraction``; replies keep id/response only; session
    status/error keep bounded hints; everything else keeps kind, session
    and cursor so question hints still surface without leaking payloads.
    """
    if not isinstance(raw, dict):
        return None
    kind = _bounded(raw.get("type"), 80)
    if not kind:
        return None
    data = raw.get("data") if isinstance(raw.get("data"), dict) else {}
    event = RuntimeEvent(
    type=kind,
    session_id=_bounded(raw.get("session_id"), 200) or None,
    cursor=raw.get("cursor") if isinstance(raw.get("cursor"), int) else None)
    if kind in ("permission.asked", "permission.updated", "permission.v2.asked"):
        # Canonical internal ask event is "permission.asked".
        # "permission.updated" is a V1 compatibility alias and
        # "permission.v2.asked" (verified V2 shape: action, resources,
        # save, source) is normalized through the same branch so callers
        # have one ask branch. The adapter normally performs this V2
        # normalization; the mapping here is a defensive second boundary.
        #
        # Real V1 ask fields: id, permission, patterns (requested),
        # always (exact proposed always scope), metadata, tool.
        # Real V2 ask fields: id, action, resources (requested),
        # save (exact proposed always scope), metadata, source.
        # `pattern` holds exactly OpenCode's proposed always scope and is
        # never synthesized; `requested_patterns` stays separately
        # reviewable.
        event.type = "permission.asked"

        def _str_list(value: Any, limit: int = 32) -> list[str]:
            if isinstance(value, str):
                return [value[:400]]
            if isinstance(value, list):
                return [_bounded(p, 400) for p in value if isinstance(p, str)][:limit]
            return []

        # Two equivalent input shapes reach this boundary: the adapter wire
        # shape (fields under ``data``) and the orchestrator-boundary shape
        # (an already-normalized ``permission`` mapping at top level, as
        # produced by fixtures/scripted callers). Both normalize identically;
        # the wire shape wins when it carries an id.
        top_permission = raw.get("permission")
        if ((not isinstance(data.get("id"), str) or not data.get("id"))
                and isinstance(top_permission, dict) and top_permission.get("id")):
            data = top_permission
        if "requested_patterns" in data:
            requested = _str_list(data.get("requested_patterns"))
        elif "patterns" in data:
            requested = _str_list(data.get("patterns"))
        elif "resources" in data:
            requested = _str_list(data.get("resources"))
        elif "pattern" in data:
            requested = _str_list(data.get("pattern"))
        else:
            requested = []
        if "pattern" in data:
            scope = _str_list(data.get("pattern"))
        elif "always" in data:
            scope = _str_list(data.get("always"))
        elif "save" in data:
            scope = _str_list(data.get("save"))
        elif "patterns" in data:
            scope = _str_list(data.get("patterns"))
        elif "resources" in data and kind == "permission.v2.asked":
            # V2 without an exact proposed scope: never synthesize one.
            scope = []
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
        raw_source = data.get("source")
        if tool is None and isinstance(raw_source, dict):
            tool, _ = sanitize_metadata(raw_source)
        permission_id = (_bounded(data.get("id"), 200) or _bounded(data.get("requestID"), 200)
                         or _bounded(data.get("requestId"), 200))
        action = (_bounded(data.get("action"), 120) or _bounded(data.get("permission"), 120)
                  or _bounded(data.get("type"), 120))
        call_id = (_bounded(data.get("call_id"), 200) or _bounded(data.get("callID"), 200)
                   or None)
        if call_id is None and isinstance(raw_source, dict):
            call_id = (_bounded(raw_source.get("callID"), 200)
                       or _bounded(raw_source.get("call_id"), 200) or None)
        metadata, metadata_redacted = sanitize_metadata(data.get("metadata"))
        if not isinstance(metadata, dict):
            metadata = {}
        generation = data.get("generation")
        if generation not in ("v1", "v2"):
            generation = "v2" if kind == "permission.v2.asked" else "v1"
        event.permission = PendingPermission(
            id=permission_id,
            session_id=_bounded(data.get("session_id"), 200) or event.session_id or "",
            action=action,
            title=_bounded(data.get("title"), 300),
            pattern=tuple(scope),
            requested_patterns=tuple(requested),
            tool=tool,
            call_id=call_id,
            metadata=metadata,
            redacted=bool(data.get("redacted")) or metadata_redacted,
            created=_bounded(data.get("created"), 60),
            generation=generation)
    elif kind in ("permission.replied", "permission.v2.replied"):
        # Real reply fields: requestID + reply (V1 and verified V2 share
        # this shape). permissionID/response remain only as compatibility
        # fallbacks at this boundary.
        event.type = "permission.replied"
        event.permission_id = (_bounded(data.get("permission_id"), 200)
                               or _bounded(data.get("requestID"), 200)
                               or _bounded(data.get("requestId"), 200)
                               or _bounded(data.get("permissionID"), 200)
                               or _bounded(data.get("permissionId"), 200)
                               or _bounded(data.get("id"), 200)
                               or _bounded(raw.get("permission_id"), 200)
                               or _bounded(raw.get("id"), 200))
        event.response = _bounded(data.get("response") or data.get("reply")
                                  or raw.get("response") or raw.get("reply"), 40)
        if not event.session_id:
            event.session_id = (_bounded(data.get("session_id"), 200)
                                or _bounded(data.get("sessionID"), 200) or None)
    elif kind == "session.error":
        # Two equivalent input shapes: the adapter wire shape (fields under
        # ``data``) and the orchestrator-boundary shape (an ``error``
        # mapping at top level). Both normalize to the same bounded hint.
        err_source = data
        top_error = raw.get("error")
        if ((not isinstance(data.get("name"), str) or not data.get("name"))
                and isinstance(top_error, dict) and top_error.get("name")):
            err_source = top_error
        event.error = {"name": _bounded(err_source.get("name"), 80),
                       "message": _bounded(err_source.get("message"), 300)}
    elif kind == "session.status":
        # Preserve the native idle/busy/retry hint (bounded) so the
        # orchestrator can treat idle as a durable-probe hint. Status
        # alone never completes a run. Both the adapter shape
        # {status: "idle"} and the native shape {status: {type: ...}}
        # are accepted; anything else stays an empty (non-idle) hint.
        raw_status = data.get("status")
        if isinstance(raw_status, dict):
            raw_status = raw_status.get("type")
        status = _bounded(raw_status, 40) if isinstance(raw_status, str) else ""
        event.status = status
        event.data = {"status": status}
    elif kind == "session.idle":
        event.data = {}
    else:
        # Any other kind (including question hints): keep kind, session
        # and cursor plus a bounded action/id reference only. Payloads,
        # question bodies and raw contents never cross this boundary.
        action_hint = data.get("action")
        if isinstance(action_hint, str) and action_hint:
            event.data = {"action": _bounded(action_hint, 120)}
        request_hint = (data.get("id") if isinstance(data.get("id"), str) else None)
        if request_hint:
            event.event_id = _bounded(request_hint, 200)
    return event


def runtime_from_environment(environ: dict | None = None) -> AgentRuntime | None:
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
