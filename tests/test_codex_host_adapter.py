"""Codex adapter contract checks with a scripted app-server, no model calls."""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from workspace_bridge.codex_host_adapter import (AdapterFailure, CodexHostAdapter,
                                                  _profile_revision, make_app)
from workspace_bridge.codex_rpc import CodexRpcError


class FakeCodexRpc:
    def __init__(self):
        self.calls = []
        self.responses = []
        self.status = "idle"
        self.on_notification = None
        self.on_request = None
        self.alive = True
        self.thread_start_error = None
        self.stderr = ""

    def call(self, method, params, timeout=30):
        self.calls.append((method, params))
        if method == "model/list":
            return {"data": [{"id": "gpt-test", "displayName": "Test model",
                              "isDefault": True}]}
        if method == "thread/start":
            if self.thread_start_error:
                raise CodexRpcError(self.thread_start_error)
            return {"thread": {"id": "native-thread-1"}, "cwd": params["cwd"]}
        if method == "thread/read":
            return {"thread": {"id": params["threadId"],
                               "status": {"type": self.status}}}
        if method == "turn/start":
            self.status = "active"
            return {"turn": {"id": "native-turn-1"}}
        if method == "turn/steer":
            return {}
        if method == "turn/interrupt":
            return {}
        if method == "thread/turns/list":
            return {"data": []}
        raise AssertionError(method)

    def respond(self, request_id, result=None, error=None):
        self.responses.append((request_id, result, error))

    def stderr_summary(self):
        return self.stderr

    def close(self):
        pass


@pytest.fixture
def codex_adapter(tmp_path):
    projects = tmp_path / "projects"
    projects.mkdir()
    workspace = projects / "workspace"
    workspace.mkdir()
    rpc = FakeCodexRpc()
    adapter = CodexHostAdapter(tmp_path / "adapter-state", projects, rpc=rpc)
    yield adapter, workspace, rpc
    adapter.close()


def new_conversation(adapter, workspace):
    return adapter.create_conversation({
        "workspaceId": "ws_test", "directory": str(workspace),
        "securityProfile": {"id": "workspace-write-reviewed",
                            "revision": _profile_revision("workspace-write-reviewed")},
    })


def test_owned_conversation_idle_only_runs_and_opaque_model(codex_adapter):
    adapter, workspace, rpc = codex_adapter
    descriptor = adapter.descriptor()
    assert descriptor["protocol"]["major"] == 1
    assert adapter.models()["models"][0]["selector"] == "gpt-test"
    conversation = new_conversation(adapter, workspace)
    run = adapter.start_run(conversation["id"], {
        "input": [{"type": "text", "text": "Check the project"}], "model": "gpt-test"})
    assert run["phase"] == "active" and run["nativeId"] == "native-turn-1"
    assert ("turn/start", {"input": [{"type": "text", "text": "Check the project"}],
                            "model": "gpt-test", "threadId": "native-thread-1"}) in rpc.calls
    with pytest.raises(AdapterFailure) as exc:
        adapter.start_run(conversation["id"], {"input": [{"type": "text", "text": "Second"}]})
    assert exc.value.code == "conversation_busy"
    adapter._notification("item/completed", {
        "threadId": "native-thread-1", "turnId": "native-turn-1",
        "item": {"id": "item-1", "type": "agentMessage", "text": "Done"}})
    adapter._notification("turn/completed", {
        "threadId": "native-thread-1", "turn": {"id": "native-turn-1",
                                                  "status": "completed"}})
    assert adapter.run(run["id"])["outcome"] == "succeeded"
    assert adapter.run(run["id"])["result"] == "Done"
    assert adapter.activities(run["id"])["activities"][0]["kind"] == "other"


def test_interaction_resolves_exact_live_native_request(codex_adapter):
    adapter, workspace, rpc = codex_adapter
    conversation = new_conversation(adapter, workspace)
    run = adapter.start_run(conversation["id"], {
        "input": [{"type": "text", "text": "Run tests"}]})
    adapter._request(17, "item/commandExecution/requestApproval", {
        "threadId": "native-thread-1", "turnId": "native-turn-1",
        "reason": "Allow tests", "availableDecisions": ["accept", "decline"]})
    interaction = adapter.interactions(run["id"])["interactions"][0]
    assert adapter.run(run["id"])["activeState"] == "waiting_interaction"
    selected = next(choice for choice in interaction["choices"]
                    if choice["semantic"] == "approve")
    adapter.resolve(interaction["id"], {"choiceId": selected["id"]})
    assert rpc.responses == [(17, {"decision": "accept"}, None)]
    assert adapter.run(run["id"])["activeState"] == "running"
    with pytest.raises(AdapterFailure) as exc:
        adapter.resolve(interaction["id"], {"choiceId": selected["id"]})
    assert exc.value.code == "interaction_stale"


def test_persistent_native_policy_amendments_are_not_offered(codex_adapter):
    adapter, workspace, rpc = codex_adapter
    conversation = new_conversation(adapter, workspace)
    run = adapter.start_run(conversation["id"], {
        "input": [{"type": "text", "text": "Run checks"}]})
    adapter._request(18, "item/commandExecution/requestApproval", {
        "threadId": "native-thread-1", "turnId": "native-turn-1",
        "availableDecisions": ["acceptForSession",
            {"acceptWithExecpolicyAmendment": {"execpolicy_amendment": ["*"]}},
            "accept", "decline"]})
    choices = adapter.interactions(run["id"])["interactions"][0]["choices"]
    assert [choice["label"] for choice in choices] == ["accept", "decline"]


def test_non_owned_and_outside_workspace_refused(codex_adapter, tmp_path):
    adapter, workspace, rpc = codex_adapter
    with pytest.raises(AdapterFailure):
        new_conversation(adapter, tmp_path)
    assert not rpc.calls
    with pytest.raises(AdapterFailure) as exc:
        adapter.conversation("some-other-client-thread")
    assert exc.value.code == "not_found"


def test_thread_start_failure_has_safe_host_diagnostics(codex_adapter, caplog):
    adapter, workspace, rpc = codex_adapter
    rpc.thread_start_error = (
        "denied at /Users/private/project; WB_RUNTIME_TOKEN=super-secret-token")
    rpc.stderr = "launch failed for /Volumes/private/data; api_key=sk-proj-" + "x" * 32

    with caplog.at_level("WARNING", logger="workspace_bridge.codex_host_adapter"):
        with pytest.raises(AdapterFailure) as exc:
            new_conversation(adapter, workspace)

    assert exc.value.code == "runtime_unavailable"
    assert str(exc.value) == "Codex thread/start failed"
    message = caplog.records[-1].getMessage()
    assert "thread/start" in message and "denied" in message
    assert "[PATH]" in message and "[REDACTED_SECRET]" in message
    assert "/Users/" not in message and "/Volumes/" not in message
    assert "super-secret-token" not in message and "sk-proj-" not in message
    assert len(message) <= 2600


def test_read_only_profile_denies_native_escalation(codex_adapter):
    adapter, workspace, rpc = codex_adapter
    profile = {"id": "read-only", "revision": _profile_revision("read-only")}
    conversation = adapter.create_conversation({
        "workspaceId": "ws-test", "directory": str(workspace),
        "securityProfile": profile})
    run = adapter.start_run(conversation["id"], {
        "input": [{"type": "text", "text": "Read files"}]})
    adapter._request(25, "item/permissions/requestApproval", {
        "threadId": "native-thread-1", "turnId": "native-turn-1",
        "permissions": {"network": True}})
    assert rpc.responses == [(25, {"permissions": {}, "scope": "turn"}, None)]
    assert adapter.interactions(run["id"]) == {"interactions": []}


def test_custom_profile_controls_revision_and_delete(codex_adapter):
    adapter, workspace, rpc = codex_adapter
    config = {"sandbox": "workspace-write", "approvalPolicy": "on-request",
              "approvalsReviewer": "user"}
    saved = adapter.save_profile({"id": "team-reviewed", "config": config})
    assert saved["config"] == config
    assert next(row for row in adapter.profiles()["profiles"]
                if row["id"] == "team-reviewed")["mutable"] is True
    conversation = adapter.create_conversation({
        "workspaceId": "ws-test", "directory": str(workspace),
        "securityProfile": {"id": saved["id"], "revision": saved["revision"]}})
    assert rpc.calls[-1][1]["approvalsReviewer"] == "user"
    run = adapter.start_run(conversation["id"], {
        "input": [{"type": "text", "text": "Review this project"}]})
    with pytest.raises(AdapterFailure) as active:
        adapter.save_profile({"id": "team-reviewed",
            "config": {**config, "sandbox": "read-only"},
            "expectedRevision": saved["revision"]})
    assert active.value.code == "conflict"
    adapter._notification("turn/completed", {
        "threadId": "native-thread-1", "turn": {"id": run["nativeId"],
                                              "status": "completed"}})
    changed = adapter.save_profile({"id": "team-reviewed",
        "config": {**config, "sandbox": "read-only"},
        "expectedRevision": saved["revision"]})
    assert changed["revision"] != saved["revision"]
    with pytest.raises(AdapterFailure) as stale:
        adapter.conversation(conversation["id"])
    assert stale.value.code == "profile_mismatch"
    with pytest.raises(AdapterFailure) as conflict:
        adapter.save_profile({"id": "team-reviewed", "config": config,
                              "expectedRevision": saved["revision"]})
    assert conflict.value.code == "profile_mismatch"
    assert adapter.delete_profile("team-reviewed") == {"deleted": "team-reviewed"}
    assert all(row["id"] != "team-reviewed" for row in adapter.profiles()["profiles"])


def test_restart_marks_unrecoverable_run_interrupted(codex_adapter):
    adapter, workspace, rpc = codex_adapter
    conversation = new_conversation(adapter, workspace)
    run = adapter.start_run(conversation["id"], {
        "input": [{"type": "text", "text": "Long task"}]})
    adapter.close()
    restarted = CodexHostAdapter(adapter.state, workspace.parent, rpc=FakeCodexRpc())
    try:
        assert restarted.run(run["id"])["outcome"] == "interrupted"
        assert restarted.conversation(conversation["id"])["nativeId"] == "native-thread-1"
        assert restarted.interactions(run["id"]) == {"interactions": []}
    finally:
        restarted.close()


@pytest.mark.asyncio
async def test_private_http_surface_requires_token(codex_adapter):
    adapter, workspace, _ = codex_adapter
    app = make_app(adapter, "private-token")
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://127.0.0.1:8772") as client:
        denied = await client.get("/v1/descriptor")
        assert denied.status_code == 401
        allowed = await client.get("/v1/descriptor",
                                   headers={"X-Runtime-Token": "private-token"})
        assert allowed.status_code == 200
        assert allowed.json()["runtime"]["id"] == "codex"
