"""HTTP runtime boundary: request shape, error mapping, sanitization, secrets."""
import json
import urllib.error
import urllib.request

import pytest

from workspace_bridge.runtime import (HttpOpenCodeRuntime, RuntimeRejected, RuntimeUnavailable,
                                      RuntimeUnsupported, basic_auth_header,
                                      runtime_from_environment, sanitize_metadata)
from workspace_bridge.security import BridgeError


class FakeResponse:
    def __init__(self, payload, status=200):
        self._body = json.dumps(payload).encode()
        self.status = status

    def read(self, _=None):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def patch(monkeypatch, handler):
    calls = []
    def urlopen(request, timeout=None):
        record = {"url": request.full_url, "method": request.get_method(), "timeout": timeout,
                  "headers": {k.lower(): v for k, v in request.header_items()}}
        record["body"] = json.loads(request.data.decode()) if request.data else None
        calls.append(record)
        result = handler(record)
        if isinstance(result, Exception):
            raise result
        return result
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    return calls


def http_error(url, code, message="error"):
    return urllib.error.HTTPError(url, code, message, {}, None)


def test_health_models_and_session_calls(monkeypatch):
    def handler(record):
        if record["url"].endswith("/health"):
            return FakeResponse({"ok": True, "version": "1.18.31", "adapter_version": "0.1.0",
                                 "server_configured": True, "instance": "abc", "cursor": 7})
        if record["url"].endswith("/models"):
            return FakeResponse({"models": [
                {"provider": "anthropic", "model": "claude", "selector": "anthropic/claude",
                 "name": "Claude", "default": True, "variants": []},
                {"provider": "glm", "model": "zai-glm-5.2", "selector": "glm/zai-glm-5.2",
                 "name": "GLM", "default": False},
            ]})
        if record["method"] == "POST" and record["url"].endswith("/sessions"):
            return FakeResponse({"session": {"id": "ses_1", "directory": record["body"]["directory"],
                                             "title": record["body"]["title"]}})
        if "/sessions/ses_1?" in record["url"] or record["url"].endswith("/sessions/ses_1"):
            return FakeResponse({"session": {"id": "ses_1", "directory": "/projects/alpha", "title": "t"}})
        raise AssertionError(record["url"])
    calls = patch(monkeypatch, handler)
    runtime = HttpOpenCodeRuntime("http://opencode-adapter:8770", "shared-token")
    health = runtime.health()
    assert health["ok"] and health["version"] == "1.18.31" and health["instance"] == "abc" and health["cursor"] == 7
    models = runtime.list_models()
    assert [m.selector for m in models] == ["anthropic/claude", "glm/zai-glm-5.2"]
    assert models[0].default is True
    assert all("directory" not in call["url"] for call in calls
               if call["url"].endswith("/models")), "Model discovery is global; no workspace directory may be sent"
    session = runtime.create_session("/projects/alpha", "Handoff")
    assert session.id == "ses_1" and session.directory == "/projects/alpha"
    assert runtime.get_session("/projects/alpha", "ses_1").id == "ses_1"
    assert all(call["headers"].get("x-runtime-token") == "shared-token" for call in calls)
    assert "secret" not in json.dumps(calls).lower()


def test_prompt_permission_abort_and_events(monkeypatch):
    def handler(record):
        if record["url"].endswith("/prompt-async"):
            return FakeResponse({"accepted": True})
        if "/permissions/" in record["url"]:
            return FakeResponse({"ok": True})
        if record["url"].endswith("/abort"):
            return FakeResponse({"ok": True})
        if "/messages?" in record["url"]:
            return FakeResponse({"messages": [
                {"id": "m1", "role": "assistant", "created": 1, "completed": 2,
                 "text": "done", "tools": ["bash"]}]})
        if "/events?" in record["url"]:
            return FakeResponse({"cursor": 3, "events": [
                {"cursor": 1, "type": "session.idle", "session_id": "ses_1", "data": {}},
                {"cursor": 2, "type": "permission.asked", "session_id": "ses_1",
                 "data": {"id": "per_1", "session_id": "ses_1", "action": "edit",
                          "title": "t", "pattern": ["/data/always/**"],
                          "requested_patterns": ["/data/requested/**"],
                          "tool": {"name": "edit"},
                          "metadata": {"token": "sk-" + "a" * 30},
                          "redacted": False, "created": "now"}},
            ]})
        raise AssertionError(record["url"])
    calls = patch(monkeypatch, handler)
    runtime = HttpOpenCodeRuntime("http://adapter:8770")
    runtime.prompt_async("/d", "ses_1", "hello", {"providerID": "anthropic", "modelID": "claude"})
    assert runtime.respond_permission("/d", "ses_1", "per_1", "always") is True
    assert runtime.abort_session("/d", "ses_1") is True
    messages = runtime.messages("/d", "ses_1", 10)
    assert messages[0].text == "done" and messages[0].tools == ("bash",)
    events, cursor = runtime.poll_events(0, timeout=1)
    assert cursor == 3
    assert events[1]["type"] == "permission.asked"
    assert events[1]["permission"]["action"] == "edit"
    assert events[1]["permission"]["pattern"] == ["/data/always/**"]
    assert events[1]["permission"]["requested_patterns"] == ["/data/requested/**"]
    assert events[1]["permission"]["tool"] == {"name": "edit"}
    assert events[1]["permission"]["redacted"] is True
    assert "sk-" not in json.dumps(events)
    prompt = next(c for c in calls if c["url"].endswith("/prompt-async"))
    assert prompt["body"]["model"] == {"providerID": "anthropic", "modelID": "claude"}
    reply = next(c for c in calls if "/permissions/" in c["url"])
    assert reply["body"]["response"] == "always"


@pytest.mark.parametrize("status,expected", [(404, RuntimeRejected), (409, RuntimeRejected), (501, RuntimeUnsupported), (500, RuntimeUnavailable)])
def test_error_status_mapping(monkeypatch, status, expected):
    patch(monkeypatch, lambda record: http_error(record["url"], status))
    runtime = HttpOpenCodeRuntime("http://adapter:8770")
    with pytest.raises(expected):
        runtime.abort_session("/d", "ses_1")


def test_non_json_and_connection_errors_are_unavailable(monkeypatch):
    class Bad:
        status = 200
        def read(self, _=None):
            return b"not json"
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
    patch(monkeypatch, lambda record: Bad())
    runtime = HttpOpenCodeRuntime("http://adapter:8770")
    with pytest.raises(RuntimeUnavailable):
        runtime.health()
    patch(monkeypatch, lambda record: urllib.error.URLError("down"))
    with pytest.raises(RuntimeUnavailable):
        runtime.health()


def test_permission_updated_alias_normalizes_to_asked():
    from workspace_bridge.runtime import HttpOpenCodeRuntime
    base = {"session_id": "ses_1",
            "data": {"id": "per_9", "session_id": "ses_1", "action": "edit",
                     "title": "t", "pattern": ["/data/always/**"],
                     "requested_patterns": ["/data/requested/**"],
                     "tool": {"name": "edit"}, "metadata": {},
                     "redacted": False, "created": "now"}}
    asked = HttpOpenCodeRuntime._normalize_event({"type": "permission.asked", **base})
    assert asked is not None and asked["type"] == "permission.asked"
    assert asked["permission"]["id"] == "per_9"
    assert asked["permission"]["pattern"] == ["/data/always/**"]
    assert asked["permission"]["requested_patterns"] == ["/data/requested/**"]
    alias = HttpOpenCodeRuntime._normalize_event({"type": "permission.updated", **base})
    assert alias is not None and alias["type"] == "permission.asked"
    assert alias["permission"] == asked["permission"]


def test_permission_replied_maps_v1_request_id_and_reply():
    from workspace_bridge.runtime import HttpOpenCodeRuntime
    event = HttpOpenCodeRuntime._normalize_event({
        "type": "permission.replied", "session_id": "ses_1",
        "data": {"sessionID": "ses_1", "requestID": "per_v1", "reply": "always"}})
    assert event is not None
    assert event["permission_id"] == "per_v1"
    assert event["response"] == "always"
    assert event["session_id"] == "ses_1"
    legacy = HttpOpenCodeRuntime._normalize_event({
        "type": "permission.replied", "session_id": "ses_1",
        "data": {"permissionID": "per_old", "response": "once"}})
    assert legacy is not None
    assert legacy["permission_id"] == "per_old"
    assert legacy["response"] == "once"


def test_permission_asked_legacy_pattern_stays_reviewable():
    from workspace_bridge.runtime import HttpOpenCodeRuntime
    event = HttpOpenCodeRuntime._normalize_event({
        "type": "permission.asked", "session_id": "ses_1",
        "data": {"id": "per_old", "session_id": "ses_1", "action": "edit",
                 "title": "t", "pattern": ["/legacy/**"], "metadata": {}}})
    assert event is not None
    assert event["permission"]["pattern"] == ["/legacy/**"]
    assert event["permission"]["requested_patterns"] == ["/legacy/**"]


def test_session_status_normalizes_idle_busy_retry(monkeypatch):
    states = {"ses_idle": "idle", "ses_busy": "busy", "ses_retry": "retry"}
    def handler(record):
        if "/sessions/" in record["url"] and "/status" in record["url"]:
            session = record["url"].split("/sessions/")[1].split("/")[0]
            return FakeResponse({"status": states[session]})
        raise AssertionError(record["url"])
    patch(monkeypatch, handler)
    runtime = HttpOpenCodeRuntime("http://adapter:8770")
    assert runtime.session_status("/d", "ses_idle") == "idle"
    assert runtime.session_status("/d", "ses_busy") == "busy"
    assert runtime.session_status("/d", "ses_retry") == "retry"
    status_call = None
    def missing_handler(record):
        nonlocal status_call
        status_call = record
        return FakeResponse({})
    patch(monkeypatch, missing_handler)
    with pytest.raises(RuntimeUnavailable):
        runtime.session_status("/d", "ses_idle")
    assert "/sessions/ses_idle/status" in status_call["url"]
    assert status_call["body"] is None
    assert "directory=/d" in status_call["url"] or "directory=%2Fd" in status_call["url"]


def test_session_status_rejects_unknown_values(monkeypatch):
    patch(monkeypatch, lambda record: FakeResponse({"status": "weird"}))
    runtime = HttpOpenCodeRuntime("http://adapter:8770")
    with pytest.raises(RuntimeUnavailable):
        runtime.session_status("/d", "ses_1")


def test_get_session_missing_returns_none(monkeypatch):
    patch(monkeypatch, lambda record: http_error(record["url"], 404))
    runtime = HttpOpenCodeRuntime("http://adapter:8770")
    assert runtime.get_session("/d", "ses_x") is None
    with pytest.raises(RuntimeRejected):
        runtime.respond_permission("/d", "ses_1", "per_1", "once")


def test_sanitize_metadata_redacts_secrets_and_bounds_depth():
    value, redacted = sanitize_metadata({"note": "password = hunter2hunter2",
                                         "nested": {"a": {"b": {"c": 1}}}, "path": "/tmp/x"})
    assert redacted is True
    assert "hunter2" not in json.dumps(value)
    assert value["path"] == "/tmp/x"
    assert value["nested"] == {"a": {"b": None}}


def test_runtime_from_environment_and_url_validation(monkeypatch):
    assert runtime_from_environment({}) is None
    runtime = runtime_from_environment({"WB_OPENCODE_RUNTIME_URL": "http://opencode-adapter:8770",
                                        "WB_RUNTIME_TOKEN": "t"})
    assert isinstance(runtime, HttpOpenCodeRuntime)
    with pytest.raises(BridgeError):
        HttpOpenCodeRuntime("file:///etc/passwd")
    assert basic_auth_header("u", "p") == "Basic " + __import__("base64").b64encode(b"u:p").decode()


def test_get_session_does_not_substitute_missing_directory(monkeypatch):
    patch(monkeypatch, lambda record: FakeResponse({"session": {"id": "ses_x", "title": "t"}}))
    runtime = HttpOpenCodeRuntime("http://adapter:8770")
    session = runtime.get_session("/projects/alpha", "ses_x")
    assert session is not None and session.directory == ""


def test_locked_adapter_and_health_flag(monkeypatch):
    patch(monkeypatch, lambda record: FakeResponse(
        {"ok": False, "error": "locked", "locked": True, "token_configured": False,
         "server_configured": True, "adapter_version": "0.1.2", "instance": "i", "cursor": 0}))
    runtime = HttpOpenCodeRuntime("http://adapter:8770", "")
    assert runtime.health()["locked"] is True
    patch(monkeypatch, lambda record: http_error(record["url"], 401))
    with pytest.raises(RuntimeUnavailable):
        runtime.list_models()


def test_create_session_missing_directory_is_empty(monkeypatch):
    patch(monkeypatch, lambda record: FakeResponse({"session": {"id": "ses_x", "title": "t"}}))
    runtime = HttpOpenCodeRuntime("http://adapter:8770")
    session = runtime.create_session("/projects/alpha", "t")
    assert session.id == "ses_x" and session.directory == ""
