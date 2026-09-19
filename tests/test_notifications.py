"""Discord notification behavior: safe metadata, bounded retries, no secret leakage."""
import io
import json
import urllib.error
import urllib.request
from email.message import Message

import pytest

from workspace_bridge.api import Handoff
from workspace_bridge.notifications import (BRIDGE_USER_AGENT, DiscordNotifier, NullNotifier,
                                            NotificationResult, notifier_from_environment)
from workspace_bridge.security import BridgeError

from runtime_fakes import permission_event


class FakeResponse:
    def __init__(self, status):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def capture(monkeypatch, status=204, error=None, recorder=None):
    def urlopen(request, timeout=None):
        if recorder is not None:
            recorder.append({"url": request.full_url, "timeout": timeout,
                             "body": json.loads(request.data.decode())})
        if error is not None:
            raise error
        if status >= 400:
            raise urllib.error.HTTPError(request.full_url, status, "error", {}, None)
        return FakeResponse(status)
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)


def test_waiting_and_completed_payloads_are_safe(monkeypatch):
    records = []
    capture(monkeypatch, recorder=records)
    notifier = DiscordNotifier("https://discord.example/api/webhooks/secret-token", sleep=lambda _: None)
    waiting = notifier.notify(state="waiting_permission", workspace_name="Alpha",
                              handoff_title="Improve add", run_id="run_abc",
                              request_kind="permission", request_action="external_directory",
                              at="2026-09-19T00:00:00+00:00")
    completed = notifier.notify(state="completed", workspace_name="Alpha",
                                handoff_title="Improve add", run_id="run_abc")
    assert waiting.status == "sent" and completed.status == "sent"
    body = json.dumps(records[0]["body"])
    assert records[0]["url"].endswith("secret-token")
    assert "Alpha" in body and "run_abc" in body and "external_directory" in body
    assert "permission.updated" not in body
    for forbidden in ("/Users/", "sk-", "BEGIN PRIVATE KEY", "rm -rf", ".workspace-handoff"):
        assert forbidden not in body
    assert records[0]["body"]["embeds"][0]["description"].startswith("Review the pending request")
    assert records[1]["body"]["embeds"][0]["description"].startswith("Read the final run result")


def test_bounded_retries_and_permanent_failure(monkeypatch):
    sleeps = []
    capture(monkeypatch, error=urllib.error.URLError("down"))
    notifier = DiscordNotifier("https://discord.example/hook", attempts=3, sleep=sleeps.append)
    result = notifier.notify(state="completed", workspace_name="A", handoff_title="T", run_id="run_1")
    assert result.status == "failed" and result.attempts == 3 and len(sleeps) == 2

    records = []
    capture(monkeypatch, status=400, recorder=records)
    notifier = DiscordNotifier("https://discord.example/hook", attempts=3, sleep=lambda _: None)
    result = notifier.notify(state="completed", workspace_name="A", handoff_title="T", run_id="run_1")
    assert result.status == "failed" and result.attempts == 1 and len(records) == 1


def test_unknown_state_is_skipped_and_public_never_leaks(monkeypatch):
    capture(monkeypatch, recorder=[])
    notifier = DiscordNotifier("https://discord.example/hook", sleep=lambda _: None)
    assert notifier.notify(state="running", workspace_name="A", handoff_title="T",
                           run_id="run_1").status == "skipped"
    public = notifier.notify(state="completed", workspace_name="A", handoff_title="T",
                             run_id="run_1").public()
    assert "url" not in json.dumps(public).lower() and "hook" not in json.dumps(public).lower()


def http_error_with_body(url, code, body: bytes, content_type="application/json"):
    headers = Message()
    headers["Content-Type"] = content_type
    return urllib.error.HTTPError(url, code, "Forbidden", headers, io.BytesIO(body))


def capture_request(monkeypatch, error=None, status=204):
    seen = {}

    def urlopen(request, timeout=None):
        seen["headers"] = {k.lower(): v for k, v in request.header_items()}
        seen["url"] = request.full_url
        if error is not None:
            raise error
        if status >= 400:
            raise urllib.error.HTTPError(request.full_url, status, "error", {}, None)
        return FakeResponse(status)

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    return seen


def test_webhook_request_sends_user_agent_and_accept(monkeypatch):
    seen = capture_request(monkeypatch)
    notifier = DiscordNotifier("https://discord.example/api/webhooks/secret-token", sleep=lambda _: None)
    result = notifier.notify(state="completed", workspace_name="A", handoff_title="T", run_id="run_1")
    assert result.status == "sent"
    assert seen["headers"].get("user-agent") == BRIDGE_USER_AGENT
    assert seen["headers"].get("accept") == "application/json"
    assert "secret-token" not in BRIDGE_USER_AGENT
    assert "secret-token" not in json.dumps(result.public())


def test_403_json_error_yields_bounded_diagnostic(monkeypatch):
    url = "https://discord.example/api/webhooks/secret-token"
    body = json.dumps({"message": "Unknown Webhook", "code": 10015}).encode()
    capture_request(monkeypatch, error=http_error_with_body(url, 403, body))
    notifier = DiscordNotifier(url, sleep=lambda _: None)
    result = notifier.notify(state="completed", workspace_name="A", handoff_title="T", run_id="run_1")
    assert result.status == "failed" and result.attempts == 1 and result.code == "http_403"
    public = result.public()
    assert public["detail"] == "discord_10015: Unknown Webhook"
    assert len(public["detail"]) <= 160
    assert "secret-token" not in json.dumps(public)


def test_403_json_error_with_secret_is_redacted_and_bounded(monkeypatch):
    url = "https://discord.example/api/webhooks/secret-token"
    body = json.dumps({"message": "api_key=sk-" + "B" * 30, "code": 50027}).encode()
    capture_request(monkeypatch, error=http_error_with_body(url, 403, body))
    notifier = DiscordNotifier(url, sleep=lambda _: None)
    result = notifier.notify(state="completed", workspace_name="A", handoff_title="T", run_id="run_1")
    assert result.code == "http_403"
    assert "sk-" not in result.public().get("detail", "")
    assert len(result.public().get("detail", "")) <= 160
    assert "secret-token" not in json.dumps(result.public())


def test_403_html_body_persists_no_detail(monkeypatch):
    url = "https://discord.example/api/webhooks/secret-token"
    html = b"<html><head><title>403</title></head><body>Cloudflare challenge</body></html>"
    capture_request(monkeypatch, error=http_error_with_body(url, 403, html, "text/html"))
    notifier = DiscordNotifier(url, sleep=lambda _: None)
    result = notifier.notify(state="completed", workspace_name="A", handoff_title="T", run_id="run_1")
    assert result.status == "failed" and result.code == "http_403"
    assert result.public().get("detail", "") == ""
    assert "Cloudflare" not in json.dumps(result.public())


def test_environment_configuration_never_returns_or_logs_the_secret(monkeypatch):
    assert isinstance(notifier_from_environment({}), NullNotifier)
    captured = {}
    def factory(url):
        captured["url"] = url
        return DiscordNotifier(url, sleep=lambda _: None)
    notifier = notifier_from_environment({"WB_DISCORD_WEBHOOK_URL": "https://discord.example/hook"},
                                         factory=factory)
    assert captured["url"] == "https://discord.example/hook"
    assert isinstance(notifier, DiscordNotifier)
    with pytest.raises(BridgeError) as exc:
        DiscordNotifier("file:///etc/passwd")
    assert "passwd" not in str(exc.value)


def test_orchestrator_notifies_on_wait_and_completion(agent_env, payload):
    job = agent_env["service"].call(agent_env["id"], agent_env["token"], "prepare_handoff",
                                    Handoff.model_validate(payload).model_dump())
    run = agent_env["service"].call(agent_env["id"], agent_env["token"], "start_opencode_run",
                                    {"job_id": job["id"], "request_id": "n1", "model": None,
                                     "parent_run_id": None})
    agent_env["service"].orchestrator.handle_event(permission_event(run["session_id"], "per_n", pattern=["/x/**"]))
    states = [c["state"] for c in agent_env["notifier"].calls]
    assert states == ["waiting_permission"]
    assert agent_env["notifier"].calls[0]["workspace_name"] == "Alpha"
    assert agent_env["notifier"].calls[0]["handoff_title"] == "Improve add"
    agent_env["service"].orchestrator.respond_permission(agent_env["service"].workspace(agent_env["id"]),
                                                         run["run_id"], "per_n", "once")
    agent_env["service"].orchestrator.handle_event({"type": "session.idle", "session_id": run["session_id"]})
    assert [c["state"] for c in agent_env["notifier"].calls] == ["waiting_permission", "completed"]
    persisted = agent_env["service"].call(agent_env["id"], agent_env["token"], "read_opencode_run",
                                          {"run_id": run["run_id"]})
    assert persisted["notification"]["status"] == "sent"
    assert "discord" not in json.dumps(persisted).lower()


def test_notification_failure_does_not_change_run_state(agent_env, payload):
    from runtime_fakes import RecordingNotifier
    agent_env["service"].orchestrator.notifier = RecordingNotifier(
        result_factory=lambda **kw: NotificationResult(status="failed", attempts=3, code="unreachable"))
    job = agent_env["service"].call(agent_env["id"], agent_env["token"], "prepare_handoff",
                                    Handoff.model_validate(payload).model_dump())
    run = agent_env["service"].call(agent_env["id"], agent_env["token"], "start_opencode_run",
                                    {"job_id": job["id"], "request_id": "nf", "model": None,
                                     "parent_run_id": None})
    agent_env["service"].orchestrator.handle_event({"type": "session.idle", "session_id": run["session_id"]})
    detail = agent_env["service"].call(agent_env["id"], agent_env["token"], "read_opencode_run",
                                       {"run_id": run["run_id"]})
    assert detail["state"] == "completed"
    assert detail["notification"]["status"] == "failed"
