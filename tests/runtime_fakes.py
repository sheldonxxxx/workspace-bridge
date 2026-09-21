"""Scripted runtime/notifier fakes for agent orchestration tests.

These never touch the network, Node, a real OpenCode server, Discord or provider
credentials. They implement the same neutral AgentRuntime boundary the HTTP
adapter exposes.
"""
from __future__ import annotations

from workspace_bridge.notifications import NotificationResult, Notifier
from workspace_bridge.runtime import (OPENCODE_RUNTIME_ID, AgentRuntime, MessageInfo, ModelInfo,
                                      PendingPermission, PendingQuestion, RuntimeCapabilities,
                                      RuntimeEvent, RuntimeInteraction, RuntimeRejected,
                                      RuntimeUnavailable, SessionInfo, coerce_runtime_event)


class FakeRuntime(AgentRuntime):
    name = "fake"

    @property
    def runtime_id(self) -> str:
        return self._runtime_id

    @property
    def capabilities(self) -> RuntimeCapabilities:
        return self._capabilities

    def __init__(self, directory: str, *, models=None, messages=None,
                 session_directory=None, abort_result=True, respond_result=True,
                 health_ok=True, health_error=None, prompt_error=None,
                 instance="adapter-1", cursor=0, session_error=None, locked=False):
        self.directory = directory
        self.models = models if models is not None else [
            ModelInfo(selector="anthropic/claude-sonnet", provider="anthropic",
                      model="claude-sonnet", name="Claude Sonnet", default=True),
            ModelInfo(selector="glm/zai-glm-5.2", provider="glm", model="zai-glm-5.2",
                      name="GLM 5.2"),
        ]
        self.messages_script = messages if messages is not None else [
            MessageInfo(id="m1", role="user", created=1),
            MessageInfo(id="m2", role="assistant", created=2, completed=3,
                        text="Implemented the change and ran the tests.", tools=("edit", "bash")),
        ]
        self.session_directory = session_directory
        self.abort_result = abort_result
        self.respond_result = respond_result
        self.health_ok = health_ok
        self.health_error = health_error
        self.prompt_error = prompt_error
        self.session_error = session_error
        self.locked = locked
        self.instance = instance
        self.head_cursor = cursor
        # Neutral identity/capabilities: the fake models the installed
        # OpenCode backend by default. Tests override these attributes to
        # exercise runtime-mismatch and capability fail-closed paths.
        self._runtime_id = OPENCODE_RUNTIME_ID
        self._capabilities = RuntimeCapabilities()
        self.sessions: list[dict] = []
        self.prompts: list[dict] = []
        self.get_session_calls: list[tuple] = []
        self.messages_calls: list[tuple] = []
        self.respond_calls: list[tuple] = []
        self.abort_calls: list[tuple] = []
        self.model_calls: list[str] = []
        self.session_missing = False
        self.prompt_hook = None
        self.respond_hook = None
        self.statuses: dict[str, str] = {}
        self.status_error = None
        self.status_calls: list[tuple] = []
        self.pending_by_session: dict[str, list[RuntimeInteraction]] = {}
        self.list_pending_error = None
        self.list_pending_calls: list[tuple] = []
        self.questions_by_session: dict[str, list[RuntimeInteraction]] = {}
        self.list_questions_error = None
        self.list_questions_calls: list[tuple] = []
        # Which snapshot the fake models the adapter as having used ("v2"
        # primary or "v1" compatibility fallback). Tests set "v1" to
        # exercise the fallback diagnostics path end to end.
        self.question_source: str = "v2"
        self.last_question_source: str | None = None
        # Adapter EventHub-style functional counters (heartbeat-only vs
        # useful traffic). health() exposes them when not None.
        self.raw_event_count: int | None = None
        self.control_event_count: int | None = None
        self.functional_event_count: int | None = None
        self.last_raw_event_at: str | None = None
        self.last_functional_event_at: str | None = None
        self.last_permission_source: str | None = None
        self.respond_generations: list[str] = []
        self._events: list[dict] = []
        self._counter = 0
        self.event_stream_status: str | None = "subscribed"
        self.event_stream_transitions = 0
        self.event_stream_failures = 0

    def health(self):
        if self.health_error:
            raise RuntimeUnavailable(self.health_error)
        payload: dict = {"ok": self.health_ok, "version": "1.18.31", "adapter_version": "0.1.7",
                         "server_configured": True, "locked": self.locked,
                         "instance": self.instance, "cursor": self.head_cursor}
        if self.event_stream_status is not None:
            stream: dict = {"status": self.event_stream_status,
                            "transitions": self.event_stream_transitions,
                            "last_transition": None,
                            "consecutive_failures": self.event_stream_failures}
            if self.raw_event_count is not None:
                stream["raw_event_count"] = self.raw_event_count
            if self.control_event_count is not None:
                stream["control_event_count"] = self.control_event_count
            if self.functional_event_count is not None:
                stream["functional_event_count"] = self.functional_event_count
            if self.last_raw_event_at is not None:
                stream["last_raw_event_at"] = self.last_raw_event_at
            if self.last_functional_event_at is not None:
                stream["last_functional_event_at"] = self.last_functional_event_at
            payload["event_stream"] = stream
        return payload

    def list_models(self, directory=None):
        # Global discovery: the workspace directory is never required or used.
        self.model_calls.append(directory)
        return list(self.models)

    def create_session(self, directory, title, options=None):
        self._counter += 1
        # 3B1: record the optional runtime-neutral session options (Pi
        # permission policy snapshot) for assertions; OpenCode passes None.
        self.session_options = getattr(self, "session_options", [])
        self.session_options.append(options)
        # None means "adapter did not override" (normal); "" explicitly simulates an
        # observed-missing directory and must not fall back to the requested path.
        observed = self.session_directory if self.session_directory is not None else directory
        session = SessionInfo(id=f"ses_{self._counter}", directory=observed, title=title)
        self.sessions.append({"directory": directory, "title": title, "session": session})
        return session

    def get_session(self, directory, session_id):
        self.get_session_calls.append((directory, session_id))
        if self.session_error:
            raise RuntimeUnavailable(self.session_error)
        if self.session_missing:
            return None
        for item in self.sessions:
            if item["session"].id == session_id:
                return SessionInfo(id=session_id, directory=item["session"].directory, title=item["session"].title)
        return None

    def set_session_directory(self, value, session_id=None):
        """Simulate a recorded session that now resolves to a different directory."""
        for item in self.sessions:
            if session_id is None or item["session"].id == session_id:
                item["session"] = SessionInfo(id=item["session"].id, directory=value,
                                              title=item["session"].title)

    def session_status(self, directory, session_id):
        self.status_calls.append((directory, session_id))
        if self.status_error:
            raise self.status_error
        status = self.statuses.get(session_id, "idle")
        if status not in ("idle", "busy", "retry"):
            raise RuntimeUnavailable("OpenCode session status was invalid")
        return status

    def set_session_status(self, status, session_id=None):
        """Simulate a native busy/retry/idle session at dispatch time."""
        targets = [item["session"].id for item in self.sessions
                   if session_id is None or item["session"].id == session_id]
        for target in targets:
            self.statuses[target] = status

    def add_pending_permission(self, session_id: str, permission: RuntimeInteraction) -> RuntimeInteraction:
        """Stage an OpenCode-side pending permission without emitting any event."""
        self.pending_by_session.setdefault(session_id, []).append(permission)
        return permission

    def set_list_pending_error(self, error) -> None:
        """Make the next permission-list calls fail closed with this error."""
        self.list_pending_error = error

    def list_pending_permissions(self, directory, session_id):
        self.list_pending_calls.append((directory, session_id))
        if self.list_pending_error is not None:
            raise self.list_pending_error
        found = [p for p in self.pending_by_session.get(session_id, [])
                 if p.session_id == session_id and p.id]
        if any(getattr(p, "generation", "v1") == "v2" for p in found):
            self.last_permission_source = "v2"
        elif found:
            self.last_permission_source = "v1"
        else:
            # A successful empty snapshot is authoritative for the primary
            # (V2) source; absence never fabricates a request.
            self.last_permission_source = "v2"
        return found

    def add_pending_question(self, session_id: str, question: RuntimeInteraction) -> RuntimeInteraction:
        """Stage an OpenCode-side pending question without emitting any event."""
        self.questions_by_session.setdefault(session_id, []).append(question)
        return question

    def set_list_questions_error(self, error) -> None:
        """Make the next question-list calls fail closed with this error."""
        self.list_questions_error = error

    def list_pending_questions(self, directory, session_id):
        self.list_questions_calls.append((directory, session_id))
        if self.list_questions_error is not None:
            raise self.list_questions_error
        self.last_question_source = self.question_source if self.question_source in ("v1", "v2") else None
        return [q for q in self.questions_by_session.get(session_id, [])
                if q.session_id == session_id and q.id]

    def prompt_async(self, directory, session_id, text, model=None):
        if self.prompt_error:
            raise self.prompt_error
        self.prompts.append({"directory": directory, "session": session_id, "text": text, "model": model})
        if self.prompt_hook is not None:
            self.prompt_hook(session_id)

    def messages(self, directory, session_id, limit=40):
        self.messages_calls.append((directory, session_id, limit))
        return list(self.messages_script)[:limit]

    def respond_permission(self, directory, session_id, permission_id, response,
                           generation="v1"):
        self.respond_calls.append((session_id, permission_id, response))
        self.respond_generations.append(generation)
        if self.respond_hook is not None:
            self.respond_hook(session_id, permission_id, response)
        return self.respond_result

    def abort_session(self, directory, session_id):
        self.abort_calls.append((directory, session_id))
        return self.abort_result

    def poll_events(self, cursor, timeout=25.0):
        # The runtime boundary yields normalized events, never raw dicts.
        stored = [e for e in self._events if (e.get("cursor") or 0) > cursor]
        normalized = []
        for raw in stored:
            event = raw if isinstance(raw, RuntimeEvent) else coerce_runtime_event(raw)
            if event is not None:
                normalized.append(event)
        return normalized, (stored[-1]["cursor"] if stored else cursor)

    def push(self, event: dict):
        self.head_cursor += 1
        self._events.append({**event, "cursor": self.head_cursor})

    def close(self):
        return None


class RecordingNotifier(Notifier):
    enabled = True

    def __init__(self, result_factory=None):
        self.calls: list[dict] = []
        self.result_factory = result_factory or (lambda **kw: NotificationResult(status="sent", attempts=1))

    def notify(self, **kwargs):
        self.calls.append(kwargs)
        return self.result_factory(**kwargs)


def permission_event(session_id: str, permission_id: str = "per_1", *, pattern=None,
                     patterns=None, always=None, requested_patterns=None,
                     action="external_directory", permission=None, title="access outside workspace",
                     tool=None, metadata=None, redacted=False, created="2026-09-19T00:00:00+00:00",
                     event_type="permission.asked", generation="v1") -> dict:
    """Realistic V1 ask at the orchestrator boundary.

    `pattern` is the legacy alias for the exact OpenCode-proposed always
    scope; `patterns`/`requested_patterns` is what is being requested.
    When only `pattern` is given it is mirrored into `requested_patterns`
    so older single-scope call sites stay reviewable.
    """
    action_name = permission or action
    if always is not None:
        scope = list(always)
    else:
        scope = list(pattern or [])
    if requested_patterns is not None:
        requested = list(requested_patterns)
    elif patterns is not None:
        requested = list(patterns)
    else:
        requested = list(pattern or [])
    return {
        "type": event_type,
        "session_id": session_id,
        "permission": {
            "id": permission_id,
            "session_id": session_id,
            "action": action_name,
            "title": title,
            "pattern": list(scope),
            "requested_patterns": list(requested),
            "tool": tool if tool is not None else {"name": "edit"},
            "call_id": None,
            "metadata": metadata or {},
            "redacted": redacted,
            "created": created,
            "generation": generation,
        },
    }


def permission_event_v2(session_id: str, permission_id: str = "per_v2", *,
                        action="external_directory", resources=None, save=None,
                        source=None, metadata=None, created="2026-09-19T00:00:00+00:00") -> dict:
    """Canonical V2 ask at the orchestrator boundary (adapter-normalized).

    `resources` are the requested targets; `save` is OpenCode's exact
    proposed always scope (never synthesized: omitted `save` means an empty
    pattern so "always" fails closed). `source` carries the V2 tool
    reference {type, messageID, callID}; its callID is preserved verbatim.
    """
    requested = list(resources if resources is not None else ["/data/requested/**"])
    scope = list(save if save is not None else [])
    resolved_source = dict(source) if source is not None else {"type": "tool", "messageID": "msg_1",
                                                               "callID": "call_v2"}
    return {
        "type": "permission.asked",
        "session_id": session_id,
        "permission": {
            "id": permission_id,
            "session_id": session_id,
            "action": action,
            "title": "",
            "pattern": scope,
            "requested_patterns": requested,
            "tool": dict(resolved_source),
            "call_id": resolved_source.get("callID"),
            "metadata": dict(metadata or {}),
            "redacted": False,
            "created": created,
            "generation": "v2",
        },
    }


def pending_permission(session_id: str, permission_id: str = "per_1", *,
                       pattern=None, requested_patterns=None,
                       action="external_directory", title="access outside workspace",
                       tool=None, metadata=None, generation="v1") -> PendingPermission:
    """Canonical OpenCode-side pending permission with no live event attached.

    `pattern` is the exact proposed always scope; `requested_patterns` is
    what is being requested. Mirrors permission_event's split.
    """
    scope = list(pattern if pattern is not None else ["/Users/me/projects/**"])
    requested = list(requested_patterns if requested_patterns is not None else scope)
    return PendingPermission(
        id=permission_id, session_id=session_id, action=action, title=title,
        pattern=tuple(scope), requested_patterns=tuple(requested),
        tool=tool if tool is not None else {"name": "edit"},
        call_id=None, metadata=dict(metadata or {}), redacted=False,
        created="2026-09-19T00:00:00+00:00", generation=generation)


def pending_permission_v2(session_id: str, permission_id: str = "per_v2", *,
                          action="external_directory", resources=None, save=None,
                          source=None, metadata=None) -> PendingPermission:
    """V2 OpenCode-side pending permission (adapter-normalized V2 snapshot row).

    `resources` are the requested targets; `save` is the exact proposed
    always scope and is never synthesized when absent.
    """
    requested = list(resources if resources is not None else ["/data/requested/**"])
    scope = list(save if save is not None else [])
    resolved_source = dict(source) if source is not None else {"type": "tool", "messageID": "msg_1",
                                                               "callID": "call_v2"}
    return PendingPermission(
        id=permission_id, session_id=session_id, action=action, title="",
        pattern=tuple(scope), requested_patterns=tuple(requested),
        tool=dict(resolved_source), call_id=resolved_source.get("callID"),
        metadata=dict(metadata or {}), redacted=False,
        created="2026-09-19T00:00:00+00:00", generation="v2")


def pending_question(session_id: str, question_id: str = "q_1", *,
                     question_count: int = 1, call_id: str | None = "call_q1") -> PendingQuestion:
    """Canonical official V2 snapshot row with no live event attached.

    Only the request id, owning session, question count and tool call
    reference are carried: question bodies/options/answers never cross
    this boundary.
    """
    return PendingQuestion(id=question_id, session_id=session_id,
                           question_count=question_count, call_id=call_id)


def permission_replied_event(session_id: str, permission_id: str = "per_1",
                             reply: str = "once") -> dict:
    """Real V1-shaped external/manual reply: requestID + reply."""
    return {
        "type": "permission.replied",
        "session_id": session_id,
        "permission_id": permission_id,
        "response": reply,
    }


def permission_event_legacy(session_id: str, permission_id: str = "per_legacy", *,
                            pattern=None, action="external_directory",
                            title="access outside workspace") -> dict:
    """Legacy single-pattern ask without the V1 requested/always split."""
    return {
        "type": "permission.asked",
        "session_id": session_id,
        "permission": {
            "id": permission_id,
            "session_id": session_id,
            "action": action,
            "title": title,
            "pattern": list(pattern or []),
            "call_id": "call_1",
            "metadata": {},
            "redacted": False,
            "created": "2026-09-19T00:00:00+00:00",
        },
    }
