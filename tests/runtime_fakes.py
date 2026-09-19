"""Scripted runtime/notifier fakes for OpenCode orchestration tests.

These never touch the network, Node, a real OpenCode server, Discord or provider
credentials. They implement the same narrow boundary the HTTP adapter exposes.
"""
from __future__ import annotations

from workspace_bridge.notifications import NotificationResult, Notifier
from workspace_bridge.runtime import (MessageInfo, ModelInfo, OpenCodeRuntime,
                                      RuntimeRejected, RuntimeUnavailable, SessionInfo)


class FakeRuntime(OpenCodeRuntime):
    name = "fake"

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
        self.sessions: list[dict] = []
        self.prompts: list[dict] = []
        self.respond_calls: list[tuple] = []
        self.abort_calls: list[tuple] = []
        self.model_calls: list[str] = []
        self.session_missing = False
        self.prompt_hook = None
        self.respond_hook = None
        self.statuses: dict[str, str] = {}
        self.status_error = None
        self.status_calls: list[tuple] = []
        self._events: list[dict] = []
        self._counter = 0

    def health(self):
        if self.health_error:
            raise RuntimeUnavailable(self.health_error)
        return {"ok": self.health_ok, "version": "1.18.31", "adapter_version": "0.1.4",
                "server_configured": True, "locked": self.locked,
                "instance": self.instance, "cursor": self.head_cursor}

    def list_models(self, directory=None):
        # Global discovery: the workspace directory is never required or used.
        self.model_calls.append(directory)
        return list(self.models)

    def create_session(self, directory, title):
        self._counter += 1
        # None means "adapter did not override" (normal); "" explicitly simulates an
        # observed-missing directory and must not fall back to the requested path.
        observed = self.session_directory if self.session_directory is not None else directory
        session = SessionInfo(id=f"ses_{self._counter}", directory=observed, title=title)
        self.sessions.append({"directory": directory, "title": title, "session": session})
        return session

    def get_session(self, directory, session_id):
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

    def prompt_async(self, directory, session_id, text, model=None):
        if self.prompt_error:
            raise self.prompt_error
        self.prompts.append({"directory": directory, "session": session_id, "text": text, "model": model})
        if self.prompt_hook is not None:
            self.prompt_hook(session_id)

    def messages(self, directory, session_id, limit=40):
        return list(self.messages_script)[:limit]

    def respond_permission(self, directory, session_id, permission_id, response):
        self.respond_calls.append((session_id, permission_id, response))
        if self.respond_hook is not None:
            self.respond_hook(session_id, permission_id, response)
        return self.respond_result

    def abort_session(self, directory, session_id):
        self.abort_calls.append((directory, session_id))
        return self.abort_result

    def poll_events(self, cursor, timeout=25.0):
        events = [e for e in self._events if (e.get("cursor") or 0) > cursor]
        return events, (events[-1]["cursor"] if events else cursor)

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
                     event_type="permission.asked") -> dict:
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
        },
    }


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
