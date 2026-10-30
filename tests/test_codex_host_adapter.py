"""Codex adapter contract checks with a scripted app-server, no model calls."""
from __future__ import annotations

import json
import threading
import time

import pytest

from workspace_bridge import security_profiles as sp
from workspace_bridge.codex_host_adapter import AdapterFailure, CodexHostAdapter
from workspace_bridge.codex_rpc import CodexRpcError
from workspace_bridge.plugin_host import invoke


class FakeCodexRpc:
    def __init__(self):
        self.calls = []
        self.responses = []
        self.status = "idle"
        self.on_notification = None
        self.on_request = None
        self.alive = True
        self.thread_start_error = None
        self.active_permission_profile_override = None
        self.settings_update_error = None
        self.settings_update_error_after_apply = False
        self.resume_error_after_apply = False
        self.settings_update_notify = True
        self.next_thread = 0
        self.next_turn = 0
        self.thread_settings = {}
        self.catalog = [
            {"id": ":read-only", "allowed": True, "description": "Read only"},
            {"id": ":workspace", "allowed": True, "description": "Workspace"},
            {"id": ":danger-full-access", "allowed": True, "description": "Full access"},
        ]
        self.catalog_by_cwd = {}
        self.security_config = {"config": {"permissions": {}, "sandbox_mode": None,
                                           "sandbox_workspace_write": None},
                                "origins": {}}
        self.requirements = None
        self.stderr = ""
        # Optional account quota read (account/rateLimits/read).
        self.rate_limits = None
        self.rate_limits_error = None
        # What thread/start reports as the thread's model and effort, if anything.
        self.thread_model = None
        self.turns = []

    def permission_profiles(self, cwd):
        self.calls.append(("permissionProfile/list", {"cwd": cwd}))
        return self.catalog_by_cwd.get(cwd, self.catalog)

    def read_security_config(self, cwd):
        self.calls.append(("config/read", {"cwd": cwd, "includeLayers": False}))
        return self.security_config

    def read_config_requirements(self):
        self.calls.append(("configRequirements/read", {}))
        return self.requirements

    def call(self, method, params, timeout=30):
        self.calls.append((method, params))
        if method == "model/list":
            return {"data": [{"id": "gpt-test", "displayName": "Test model",
                              "isDefault": True}]}
        if method == "thread/start":
            if self.thread_start_error:
                raise CodexRpcError(self.thread_start_error)
            self.next_thread += 1
            thread_id = f"native-thread-{self.next_thread}"
            config = self.security_config.get("config", {})
            selected = (params.get("permissions") or config.get("default_permissions")
                        or ((self.requirements or {}).get("defaultPermissions"))
                        or ":workspace")
            active = (self.active_permission_profile_override
                      if self.active_permission_profile_override is not None
                      else {"id": selected})
            settings = {"activePermissionProfile": active,
                        "approvalPolicy": params.get("approvalPolicy",
                            config.get("approval_policy") or "on-request"),
                        "approvalsReviewer": params.get("approvalsReviewer",
                            config.get("approvals_reviewer") or "user"),
                        "sandboxPolicy": {"type": "workspaceWrite"}}
            self.thread_settings[thread_id] = settings
            return {"thread": {"id": thread_id}, "cwd": params["cwd"], **settings,
                    **(self.thread_model or {})}
        if method == "thread/read":
            return {"thread": {"id": params["threadId"],
                               "status": {"type": self.status}}}
        if method == "thread/settings/update":
            if self.settings_update_error:
                raise CodexRpcError(self.settings_update_error)
            thread_id = params["threadId"]
            if self.settings_update_error_after_apply:
                settings = self.thread_settings[thread_id]
                if self.active_permission_profile_override is not None:
                    settings["activePermissionProfile"] = self.active_permission_profile_override
                elif "permissions" in params:
                    settings["activePermissionProfile"] = {"id": params["permissions"]}
                if "approvalPolicy" in params:
                    settings["approvalPolicy"] = params["approvalPolicy"]
                if "approvalsReviewer" in params:
                    settings["approvalsReviewer"] = params["approvalsReviewer"]
                raise CodexRpcError("injected timeout after apply")
            settings = self.thread_settings[thread_id]
            if self.active_permission_profile_override is not None:
                settings["activePermissionProfile"] = self.active_permission_profile_override
            elif "permissions" in params:
                settings["activePermissionProfile"] = {"id": params["permissions"]}
            if "approvalPolicy" in params:
                settings["approvalPolicy"] = params["approvalPolicy"]
            if "approvalsReviewer" in params:
                settings["approvalsReviewer"] = params["approvalsReviewer"]
            if self.settings_update_notify and self.on_notification:
                self.on_notification("thread/settings/updated", {
                    "threadId": thread_id, "threadSettings": dict(settings)})
            return {}
        if method == "thread/resume":
            thread_id = params["threadId"]
            if self.resume_error_after_apply:
                settings = self.thread_settings.get(thread_id, {})
                if "permissions" in params:
                    settings = {**settings, "activePermissionProfile": {"id": params["permissions"]}}
                if "approvalPolicy" in params:
                    settings = {**settings, "approvalPolicy": params["approvalPolicy"]}
                if "approvalsReviewer" in params:
                    settings = {**settings, "approvalsReviewer": params["approvalsReviewer"]}
                if self.active_permission_profile_override is not None:
                    settings = {**settings, "activePermissionProfile": self.active_permission_profile_override}
                self.thread_settings[thread_id] = settings
                raise CodexRpcError("injected resume timeout after apply")
            # Same-thread resume with target settings; preserves history.
            settings = self.thread_settings.get(thread_id, {})
            if "permissions" in params:
                settings = {**settings, "activePermissionProfile": {"id": params["permissions"]}}
            if "approvalPolicy" in params:
                settings = {**settings, "approvalPolicy": params["approvalPolicy"]}
            if "approvalsReviewer" in params:
                settings = {**settings, "approvalsReviewer": params["approvalsReviewer"]}
            # Honor an explicit mismatch override for negative tests.
            if self.active_permission_profile_override is not None:
                settings = {**settings, "activePermissionProfile": self.active_permission_profile_override}
            self.thread_settings[thread_id] = settings
            return {"thread": {"id": thread_id, "status": {"type": "idle"}},
                    "cwd": params.get("cwd"), **settings}
        if method == "turn/start":
            self.status = "active"
            self.next_turn += 1
            return {"turn": {"id": f"native-turn-{self.next_turn}"}}
        if method == "turn/steer":
            return {}
        if method == "turn/interrupt":
            return {}
        if method == "thread/turns/list":
            return {"data": self.turns}
        if method == "account/rateLimits/read":
            if self.rate_limits_error:
                raise CodexRpcError(self.rate_limits_error)
            return self.rate_limits if isinstance(self.rate_limits, dict) else {}
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


TRUSTED = sp.BUILTINS["trusted"]
# Asks before test commands, so their approvals reach the Bridge.
ASK_TESTS = sp.validate({**TRUSTED, "commands": [{"prefix": "pytest", "mode": "ask"}]})
# The security block each conversation was created with; its runs carry it.
_SECURITY: dict[str, dict] = {}


def new_conversation(adapter, workspace, profile="trusted", rules=None):
    if profile == "native":
        block = {"source": "native"}
    else:
        block = sp.wire(profile, rules if rules is not None else sp.BUILTINS[profile])
    created = adapter.create_conversation({
        "workspaceId": "ws_test", "directory": str(workspace), "security": block})
    _SECURITY[created["id"]] = block
    return created


def finish_fake_run(adapter, rpc, conversation, run):
    adapter._notification("turn/completed", {
        "threadId": conversation["nativeId"],
        "turn": {"id": run["nativeId"], "status": "completed"}})
    rpc.status = "idle"


def wait_for_queued_turn(adapter, run_id, expected_turn):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        row = adapter._run(run_id)
        if row["turn"] == expected_turn or row["phase"] == "terminal":
            return row
        time.sleep(0.01)
    pytest.fail("queued turn did not advance without a read")


def input_message(run_id, request_id, text, mode="queue"):
    return {"expectedRunId": run_id, "requestId": request_id,
            "input": [{"type": "text", "text": text}]}


def test_steer_is_idempotent_and_bound_to_current_turn(codex_adapter):
    adapter, workspace, rpc = codex_adapter
    conversation = new_conversation(adapter, workspace)
    run = adapter.start_run(conversation["id"], {"security": _SECURITY[conversation["id"]], "input": [{"type": "text", "text": "Start"}]})
    body = input_message(run["id"], "steer.1", "Change course")
    result = adapter.steer(run["id"], body)
    assert result["inputMessages"] == [
        {"requestId": "steer.1", "mode": "steer", "status": "delivered"}]
    assert rpc.calls[-1] == ("turn/steer", {"threadId": conversation["nativeId"],
        "expectedTurnId": run["nativeId"], "input": body["input"]})
    adapter.steer(run["id"], body)
    assert len([call for call in rpc.calls if call[0] == "turn/steer"]) == 1
    with pytest.raises(AdapterFailure) as conflict:
        adapter.steer(run["id"], input_message(run["id"], "steer.1", "Other"))
    assert conflict.value.code == "idempotency_conflict"
    finish_fake_run(adapter, rpc, conversation, run)
    assert adapter.steer(run["id"], body)["phase"] == "terminal"
    assert "Change course" not in json.dumps(adapter.run(run["id"]))


def test_fifo_queue_advances_same_run_without_polling(codex_adapter):
    adapter, workspace, rpc = codex_adapter
    conversation = new_conversation(adapter, workspace)
    run = adapter.start_run(conversation["id"], {"security": _SECURITY[conversation["id"]], 
        "input": [{"type": "text", "text": "Start"}], "model": "gpt-test"})
    first = input_message(run["id"], "q1", "One")
    second = input_message(run["id"], "q2", "Two")
    assert adapter.queue(run["id"], first)["inputMessages"][0]["status"] == "queued"
    adapter.queue(run["id"], second)
    finish_fake_run(adapter, rpc, conversation, run)
    assert wait_for_queued_turn(adapter, run["id"], "native-turn-2")["phase"] == "active"
    assert adapter._run(run["id"])["outcome"] is None
    adapter._notification("turn/completed", {"threadId": conversation["nativeId"],
        "turn": {"id": "native-turn-2", "status": "completed"}})
    assert wait_for_queued_turn(adapter, run["id"], "native-turn-3")["phase"] == "active"
    adapter._notification("turn/completed", {"threadId": conversation["nativeId"],
        "turn": {"id": "native-turn-3", "status": "completed"}})
    result = adapter.run(run["id"])
    assert result["outcome"] == "succeeded"
    assert [item["status"] for item in result["inputMessages"]] == ["delivered", "delivered"]
    assert [item["input"]["summary"] for item in adapter.activities(run["id"])["activities"]
            if item["nativeId"].startswith("input:")] == [
                "queue input delivered", "queue input delivered"]
    assert [params["input"] for method, params in rpc.calls if method == "turn/start"] == [
        [{"type": "text", "text": "Start"}], first["input"], second["input"]]
    assert all(params["threadId"] == conversation["nativeId"]
        for method, params in rpc.calls if method == "turn/start")
    assert len([event for event in adapter.events_buffer if event["type"] == "run.completed"]) == 1


def test_queue_bounds_cancel_and_restart_drop_prompts(codex_adapter):
    adapter, workspace, rpc = codex_adapter
    conversation = new_conversation(adapter, workspace)
    run = adapter.start_run(conversation["id"], {"security": _SECURITY[conversation["id"]], "input": [{"type": "text", "text": "Start"}]})
    for index in range(20):
        adapter.queue(run["id"], input_message(run["id"], f"q{index}", f"private-{index}"))
    with pytest.raises(AdapterFailure) as full:
        adapter.queue(run["id"], input_message(run["id"], "q20", "overflow"))
    assert full.value.code == "queue_full"
    adapter.cancel(run["id"])
    assert all(item["status"] == "discarded"
        for item in adapter._run_public(adapter._run(run["id"]))["inputMessages"])
    adapter._notification("turn/completed", {"threadId": conversation["nativeId"],
        "turn": {"id": run["nativeId"], "status": "interrupted"}})
    assert len([call for call in rpc.calls if call[0] == "turn/start"]) == 1
    assert "private-" not in (adapter.state / "codex-adapter.sqlite3").read_bytes().decode(
        "utf-8", errors="ignore")
    adapter.close()
    restarted = CodexHostAdapter(adapter.state, workspace.parent, rpc=FakeCodexRpc())
    try:
        assert all(item["status"] == "discarded"
            for item in restarted.run(run["id"])["inputMessages"])
    finally:
        restarted.close()


def test_queue_start_failure_fails_closed_and_discards_remaining(codex_adapter):
    adapter, workspace, rpc = codex_adapter
    conversation = new_conversation(adapter, workspace)
    run = adapter.start_run(conversation["id"], {"security": _SECURITY[conversation["id"]], "input": [{"type": "text", "text": "Start"}]})
    adapter.queue(run["id"], input_message(run["id"], "q1", "One"))
    adapter.queue(run["id"], input_message(run["id"], "q2", "Two"))
    original_call = rpc.call

    def fail_queued_start(method, params, timeout=30):
        if method == "turn/start":
            raise CodexRpcError("could contain private prompt: One")
        return original_call(method, params, timeout)

    rpc.call = fail_queued_start
    finish_fake_run(adapter, rpc, conversation, run)
    assert wait_for_queued_turn(adapter, run["id"], "native-turn-2")["outcome"] == "failed"
    result = adapter.run(run["id"])
    assert [item["status"] for item in result["inputMessages"]] == ["unknown", "discarded"]
    assert "One" not in result["error"]
    assert len([event for event in adapter.events_buffer if event["type"] == "run.completed"]) == 1


def test_queue_restart_never_replays_pending_input(codex_adapter):
    adapter, workspace, rpc = codex_adapter
    conversation = new_conversation(adapter, workspace)
    run = adapter.start_run(conversation["id"], {"security": _SECURITY[conversation["id"]], "input": [{"type": "text", "text": "Start"}]})
    body = input_message(run["id"], "q1", "secret queued prompt")
    adapter.queue(run["id"], body)
    adapter.close()
    new_rpc = FakeCodexRpc()
    restarted = CodexHostAdapter(adapter.state, workspace.parent, rpc=new_rpc)
    try:
        result = restarted.run(run["id"])
        assert result["outcome"] == "interrupted"
        assert result["inputMessages"] == [
            {"requestId": "q1", "mode": "queue", "status": "discarded"}]
        assert restarted.queue(run["id"], body)["inputMessages"] == result["inputMessages"]
        assert not [call for call in new_rpc.calls if call[0] == "turn/start"]
    finally:
        restarted.close()


def test_queue_rejects_wrong_run_and_ignores_late_turn_items(codex_adapter):
    adapter, workspace, rpc = codex_adapter
    conversation = new_conversation(adapter, workspace)
    run = adapter.start_run(conversation["id"], {"security": _SECURITY[conversation["id"]], "input": [{"type": "text", "text": "Start"}]})
    wrong = input_message("run_wrong", "q1", "One")
    with pytest.raises(AdapterFailure) as mismatch:
        adapter.queue(run["id"], wrong)
    assert mismatch.value.code == "run_mismatch"
    adapter.queue(run["id"], input_message(run["id"], "q1", "One"))
    finish_fake_run(adapter, rpc, conversation, run)
    assert wait_for_queued_turn(adapter, run["id"], "native-turn-2")["phase"] == "active"
    adapter._notification("item/completed", {"threadId": conversation["nativeId"],
        "turnId": "native-turn-1", "item": {"id": "late", "type": "agentMessage",
                                          "text": "wrong previous answer"}})
    assert "wrong previous answer" not in adapter.run(run["id"])["result"]
    adapter._notification("turn/completed", {"threadId": "wrong-thread",
        "turn": {"id": "native-turn-2", "status": "failed"}})
    assert adapter._run(run["id"])["phase"] == "active"


def test_queue_usage_accumulates_across_owned_turns(codex_adapter):
    adapter, workspace, rpc = codex_adapter
    conversation = new_conversation(adapter, workspace)
    run = adapter.start_run(conversation["id"], {"security": _SECURITY[conversation["id"]], "input": [{"type": "text", "text": "Start"}]})
    adapter.queue(run["id"], input_message(run["id"], "q1", "Continue"))
    adapter._notification("thread/tokenUsage/updated", {
        "threadId": conversation["nativeId"], "turnId": "native-turn-1",
        "tokenUsage": {"total": {"inputTokens": 5, "outputTokens": 3}}})
    finish_fake_run(adapter, rpc, conversation, run)
    assert wait_for_queued_turn(adapter, run["id"], "native-turn-2")["phase"] == "active"
    adapter._notification("thread/tokenUsage/updated", {
        "threadId": conversation["nativeId"], "turnId": "native-turn-2",
        "tokenUsage": {"total": {"inputTokens": 11, "outputTokens": 7}}})
    adapter._notification("turn/completed", {"threadId": conversation["nativeId"],
        "turn": {"id": "native-turn-2", "status": "completed"}})
    assert adapter.run(run["id"])["usage"] == {"inputTokens": 11, "outputTokens": 7}


def test_cancel_wins_before_steer_submission(codex_adapter):
    adapter, workspace, rpc = codex_adapter
    conversation = new_conversation(adapter, workspace)
    run = adapter.start_run(conversation["id"], {"security": _SECURITY[conversation["id"]], "input": [{"type": "text", "text": "Start"}]})
    entered = threading.Event()
    release = threading.Event()
    original_body = adapter._message_body
    errors = []

    def delayed_body(run_id, body):
        parsed = original_body(run_id, body)
        entered.set()
        assert release.wait(2)
        return parsed

    adapter._message_body = delayed_body

    def submit():
        try:
            adapter.steer(run["id"], input_message(run["id"], "s1", "late steer"))
        except AdapterFailure as exc:
            errors.append(exc.code)

    worker = threading.Thread(target=submit)
    worker.start()
    assert entered.wait(2)
    adapter.cancel(run["id"])
    release.set()
    worker.join(2)
    assert not worker.is_alive()
    assert errors == ["run_mismatch"]
    assert not [call for call in rpc.calls if call[0] == "turn/steer"]


def test_cancel_wins_before_queued_start_submission(codex_adapter):
    adapter, workspace, rpc = codex_adapter
    conversation = new_conversation(adapter, workspace)
    run = adapter.start_run(conversation["id"], {"security": _SECURITY[conversation["id"]], "input": [{"type": "text", "text": "Start"}]})
    adapter.queue(run["id"], input_message(run["id"], "q1", "late queue"))
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    original_start = adapter._start_queued_turn

    def delayed_start(run_id, prior_turn):
        entered.set()
        try:
            assert release.wait(2)
            original_start(run_id, prior_turn)
        finally:
            finished.set()

    adapter._start_queued_turn = delayed_start
    finish_fake_run(adapter, rpc, conversation, run)
    assert entered.wait(2)
    assert adapter.cancel(run["id"])["outcome"] == "interrupted"
    release.set()
    assert finished.wait(2)
    assert len([call for call in rpc.calls if call[0] == "turn/start"]) == 1
    assert adapter.run(run["id"])["inputMessages"][0]["status"] == "discarded"


def test_unconfirmed_queued_start_clears_buffered_callbacks(codex_adapter):
    adapter, workspace, rpc = codex_adapter
    conversation = new_conversation(adapter, workspace)
    run = adapter.start_run(conversation["id"], {"security": _SECURITY[conversation["id"]], "input": [{"type": "text", "text": "Start"}]})
    adapter.queue(run["id"], input_message(run["id"], "q1", "Continue"))
    original_call = rpc.call

    def fail_after_callbacks(method, params, timeout=30):
        if method == "turn/start":
            adapter._notification("item/started", {
                "threadId": conversation["nativeId"], "turnId": "unconfirmed",
                "item": {"id": "early", "type": "agentMessage", "text": "private"}})
            adapter._request(777, "item/tool/requestUserInput", {
                "threadId": conversation["nativeId"], "turnId": "unconfirmed"})
            raise RuntimeError("unexpected failure")
        return original_call(method, params, timeout)

    rpc.call = fail_after_callbacks
    finish_fake_run(adapter, rpc, conversation, run)
    assert wait_for_queued_turn(adapter, run["id"], "native-turn-2")["outcome"] == "failed"
    assert adapter._early_notifications == []
    assert adapter._early_requests == []
    assert rpc.responses[-1] == (777, None, {"code": -32602,
        "message": "Native run start was not confirmed"})


def test_owned_conversation_idle_only_runs_and_opaque_model(codex_adapter):
    adapter, workspace, rpc = codex_adapter
    descriptor = adapter.descriptor()
    assert descriptor["protocol"]["major"] == 1
    assert adapter.models()["models"][0]["selector"] == "gpt-test"
    conversation = new_conversation(adapter, workspace)
    run = adapter.start_run(conversation["id"], {"security": _SECURITY[conversation["id"]], 
        "input": [{"type": "text", "text": "Check the project"}], "model": "gpt-test"})
    assert run["phase"] == "active" and run["nativeId"] == "native-turn-1"
    assert ("turn/start", {"input": [{"type": "text", "text": "Check the project"}],
                            "model": "gpt-test", "threadId": "native-thread-1"}) in rpc.calls
    with pytest.raises(AdapterFailure) as exc:
        adapter.start_run(conversation["id"], {"security": _SECURITY[conversation["id"]], "input": [{"type": "text", "text": "Second"}]})
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
    conversation = new_conversation(adapter, workspace, "ask-tests", ASK_TESTS)
    run = adapter.start_run(conversation["id"], {"security": _SECURITY[conversation["id"]], 
        "input": [{"type": "text", "text": "Run tests"}]})
    adapter._request(17, "item/commandExecution/requestApproval", {
        "threadId": "native-thread-1", "turnId": "native-turn-1",
        "reason": "Allow tests", "command": "pytest -q",
        "availableDecisions": ["accept", "decline"]})
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
    conversation = new_conversation(adapter, workspace, "ask-tests", ASK_TESTS)
    run = adapter.start_run(conversation["id"], {"security": _SECURITY[conversation["id"]], 
        "input": [{"type": "text", "text": "Run checks"}]})
    adapter._request(18, "item/commandExecution/requestApproval", {
        "threadId": "native-thread-1", "turnId": "native-turn-1", "command": ["pytest"],
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
    import logging as _logging

    from workspace_bridge import codex_host_adapter as _host_module

    adapter, workspace, rpc = codex_adapter
    rpc.thread_start_error = (
        "denied at /Users/private/project; WB_RUNTIME_TOKEN=super-secret-token")
    rpc.stderr = "launch failed for /Volumes/private/data; api_key=sk-proj-" + "x" * 32

    with caplog.at_level("WARNING", logger="workspace_bridge.codex_host_adapter"):
        with pytest.raises(AdapterFailure) as exc:
            new_conversation(adapter, workspace)

    assert exc.value.code == "runtime_unavailable"
    assert exc.value.status == 502
    visible = str(exc.value)
    assert visible.startswith("Codex thread/start failed: ")
    assert "denied" in visible
    assert "[PATH]" in visible and "[REDACTED_SECRET]" in visible
    assert "/Users/" not in visible and "/Volumes/" not in visible
    assert "super-secret-token" not in visible and "sk-proj-" not in visible
    # Fuller stderr tail stays operational-log-only, not HTTP-visible.
    assert "launch failed" not in visible
    assert len(visible) <= 400
    message = caplog.records[-1].getMessage()
    assert caplog.records[-1].name == "workspace_bridge.codex_host_adapter"
    assert _host_module._LOG.name == "workspace_bridge.codex_host_adapter"
    assert "thread/start" in message and "denied" in message
    assert "launch failed" in message
    assert "[PATH]" in message and "[REDACTED_SECRET]" in message
    assert "/Users/" not in message and "/Volumes/" not in message
    assert "super-secret-token" not in message and "sk-proj-" not in message
    assert len(message) <= 2600
    assert any(isinstance(h, _logging.StreamHandler)
               for h in _host_module._LOG.handlers)


def test_profile_run_denies_native_escalation(codex_adapter):
    adapter, workspace, rpc = codex_adapter
    conversation = new_conversation(adapter, workspace)
    run = adapter.start_run(conversation["id"], {"security": _SECURITY[conversation["id"]], 
        "input": [{"type": "text", "text": "Read files"}]})
    adapter._request(25, "item/permissions/requestApproval", {
        "threadId": "native-thread-1", "turnId": "native-turn-1",
        "permissions": {"network": True}})
    assert rpc.responses == [(25, {"permissions": {}, "scope": "turn"}, None)]
    assert adapter.interactions(run["id"]) == {"interactions": []}


def test_profile_thread_stays_read_only_and_approvals_follow_the_rules(codex_adapter):
    adapter, workspace, rpc = codex_adapter
    rules = sp.validate({**TRUSTED, "protected": ["secrets/"],
                         "commands": [{"prefix": "git push", "mode": "deny"}]})
    conversation = new_conversation(adapter, workspace, "team", rules)
    start = next(params for method, params in rpc.calls if method == "thread/start")
    assert {key: start[key] for key in ("permissions", "approvalPolicy",
                                        "approvalsReviewer")} == {
        "permissions": ":read-only", "approvalPolicy": "on-request",
        "approvalsReviewer": "user"}
    assert conversation["securityBinding"] == {"source": "profile", "profile": {
        "id": "team", "revision": sp.revision(rules)}}
    run = adapter.start_run(conversation["id"], {
        "security": _SECURITY[conversation["id"]],
        "input": [{"type": "text", "text": "Work"}]})
    assert run["securityBinding"] == conversation["securityBinding"]
    ids = {"threadId": "native-thread-1", "turnId": "native-turn-1",
           "availableDecisions": ["accept", "decline", "cancel"]}
    adapter._request(1, "item/commandExecution/requestApproval", {**ids, "command": "make"})
    adapter._request(2, "item/commandExecution/requestApproval",
                     {**ids, "command": ["git", "push", "origin"]})
    for item_id, path in (("edit-in", "src/a.py"), ("edit-secret", "secrets/key"),
                          ("edit-out", str(workspace.parent / "other.txt"))):
        adapter._notification("item/started", {
            "threadId": "native-thread-1", "turnId": "native-turn-1",
            "item": {"id": item_id, "type": "fileChange", "changes": [{"path": path}]}})
    adapter._request(3, "item/fileChange/requestApproval", {**ids, "itemId": "edit-in"})
    adapter._request(4, "item/fileChange/requestApproval", {**ids, "itemId": "edit-secret"})
    adapter._request(5, "item/fileChange/requestApproval", {**ids, "itemId": "edit-out"})
    adapter._request(6, "item/fileChange/requestApproval", {**ids, "itemId": "unknown"})
    adapter._request(7, "item/commandExecution/requestApproval",
                     {**ids, "command": "make", "availableDecisions": ["acceptForSession"]})
    assert rpc.responses[:4] == [(1, {"decision": "accept"}, None),
                                 (2, {"decision": "decline"}, None),
                                 (3, {"decision": "accept"}, None),
                                 (4, {"decision": "decline"}, None)]
    assert rpc.responses[4][0] == 7 and rpc.responses[4][1] is None
    # Writes outside the workspace and unknown targets ask the Bridge.
    asked = adapter.interactions(run["id"])["interactions"]
    assert len(asked) == 2


def test_native_conversation_keeps_codex_configuration(codex_adapter):
    adapter, workspace, rpc = codex_adapter
    conversation = new_conversation(adapter, workspace, "native")
    start = next(params for method, params in rpc.calls if method == "thread/start")
    assert "permissions" not in start and "approvalPolicy" not in start
    assert conversation["securityBinding"] == {"source": "native"}
    run = adapter.start_run(conversation["id"], {
        "security": {"source": "native"}, "input": [{"type": "text", "text": "Work"}]})
    assert run["securityBinding"] == {"source": "native"}
    adapter._request(9, "item/commandExecution/requestApproval", {
        "threadId": "native-thread-1", "turnId": "native-turn-1", "command": "make",
        "availableDecisions": ["accept", "decline"]})
    assert rpc.responses == []
    assert len(adapter.interactions(run["id"])["interactions"]) == 1
    finish_fake_run(adapter, rpc, conversation, run)
    with pytest.raises(AdapterFailure) as changed:
        adapter.start_run(conversation["id"], {
            "security": sp.wire("trusted", TRUSTED),
            "input": [{"type": "text", "text": "Again"}]})
    assert changed.value.code == "security_source_changed"


def test_profile_edits_reach_the_next_run_on_the_same_thread(codex_adapter):
    adapter, workspace, rpc = codex_adapter
    conversation = new_conversation(adapter, workspace)
    first = adapter.start_run(conversation["id"], {
        "security": _SECURITY[conversation["id"]], "input": [{"type": "text", "text": "One"}]})
    finish_fake_run(adapter, rpc, conversation, first)
    edited = sp.wire("trusted", ASK_TESTS)
    second = adapter.start_run(conversation["id"], {
        "security": edited, "input": [{"type": "text", "text": "Two"}]})
    assert second["securityBinding"]["profile"]["revision"] == sp.revision(ASK_TESTS)
    assert adapter.run(first["id"])["securityBinding"]["profile"]["revision"] == sp.revision(TRUSTED)
    assert sum(method == "thread/start" for method, _ in rpc.calls) == 1
    adapter._request(11, "item/commandExecution/requestApproval", {
        "threadId": "native-thread-1", "turnId": "native-turn-2", "command": "pytest",
        "availableDecisions": ["accept", "decline"]})
    assert len(adapter.interactions(second["id"])["interactions"]) == 1


def test_pre_profile_thread_is_refused(codex_adapter):
    adapter, workspace, rpc = codex_adapter
    conversation = new_conversation(adapter, workspace)
    with adapter.lock, adapter.db:
        adapter.db.execute("UPDATE conversations SET permission_revision=':workspace'")
    with pytest.raises(AdapterFailure) as old:
        adapter.start_run(conversation["id"], {
            "security": _SECURITY[conversation["id"]], "input": [{"type": "text", "text": "x"}]})
    assert old.value.code == "security_source_changed"
    for bad in ({"source": "native", "x": 1}, {"source": "profile"}, None):
        with pytest.raises(AdapterFailure) as invalid:
            adapter.create_conversation({"workspaceId": "ws_test",
                                         "directory": str(workspace), "security": bad})
        assert invalid.value.code == "invalid_profile"


def test_active_native_permission_profile_mismatch_fails_binding(codex_adapter):
    adapter, workspace, rpc = codex_adapter
    rpc.active_permission_profile_override = {"id": ":workspace"}
    with pytest.raises(AdapterFailure) as mismatch:
        new_conversation(adapter, workspace)
    assert mismatch.value.code == "binding_mismatch"
    assert not adapter.db.execute("SELECT 1 FROM conversations").fetchone()


def test_named_profile_null_active_profile_still_fails(codex_adapter):
    adapter, workspace, rpc = codex_adapter
    real_call = rpc.call

    def _null_active(method, params, timeout=30):
        if method == "thread/start":
            return {"thread": {"id": "native-thread-null"},
                    "cwd": params["cwd"],
                    "activePermissionProfile": None,
                    "approvalPolicy": params.get("approvalPolicy", "on-request"),
                    "approvalsReviewer": params.get("approvalsReviewer", "user"),
                    "sandbox": {"type": "workspaceWrite"}}
        return real_call(method, params, timeout=timeout)

    rpc.call = _null_active
    try:
        with pytest.raises(AdapterFailure) as mismatch:
            new_conversation(adapter, workspace)
    finally:
        rpc.call = real_call
    assert mismatch.value.code == "binding_mismatch"
    assert not adapter.db.execute("SELECT 1 FROM conversations").fetchone()


def test_restart_marks_unrecoverable_run_interrupted(codex_adapter):
    adapter, workspace, rpc = codex_adapter
    conversation = new_conversation(adapter, workspace)
    run = adapter.start_run(conversation["id"], {"security": _SECURITY[conversation["id"]], 
        "input": [{"type": "text", "text": "Long task"}]})
    adapter.close()
    restarted = CodexHostAdapter(adapter.state, workspace.parent, rpc=FakeCodexRpc())
    try:
        assert restarted.run(run["id"])["outcome"] == "interrupted"
        assert restarted.conversation(conversation["id"])["nativeId"] == "native-thread-1"
        assert restarted.interactions(run["id"]) == {"interactions": []}
    finally:
        restarted.close()


def test_native_engine_loss_terminates_once_with_static_diagnostic(tmp_path, caplog):
    projects = tmp_path / "projects"
    projects.mkdir()
    workspace = projects / "workspace"
    workspace.mkdir()
    exits: list[int] = []
    rpc = FakeCodexRpc()
    adapter = CodexHostAdapter(tmp_path / "adapter-state", projects, rpc=rpc,
                               _exit_process=lambda code: exits.append(code))
    try:
        assert callable(rpc.on_unexpected_exit)
        assert getattr(rpc.on_unexpected_exit, "__self__", None) is adapter
        with caplog.at_level("CRITICAL", logger="workspace_bridge.codex_host_adapter"):
            rpc.on_unexpected_exit()
            rpc.on_unexpected_exit()
        assert exits == [1]
        assert caplog.records, "fatal native loss must be logged"
        message = caplog.records[-1].getMessage()
        assert "unexpected" in message.lower()
        assert "supervisor" in message.lower()
        assert len(message) <= 500
        assert str(workspace) not in message
        assert "token" not in message.lower()
    finally:
        adapter.close()
    assert exits == [1]


def test_adapter_close_does_not_terminate(tmp_path):
    projects = tmp_path / "projects"
    projects.mkdir()
    workspace = projects / "workspace"
    workspace.mkdir()
    exits: list[int] = []
    rpc = FakeCodexRpc()
    adapter = CodexHostAdapter(tmp_path / "adapter-state", projects, rpc=rpc,
                               _exit_process=lambda code: exits.append(code))
    adapter.close()
    assert exits == []


def test_plugin_dispatch_serves_the_codex_descriptor(codex_adapter):
    adapter, _, _ = codex_adapter
    kind, value = invoke(adapter, "descriptor", {})
    assert kind == "result" and value["runtime"]["id"] == "codex"
    assert invoke(adapter, "shell.exec", {})[1]["data"]["code"] == "not_found"


def test_conversation_5xx_boundary_logs_sanitized(codex_adapter, caplog):
    class _FailingConversations:
        def create_conversation(self, body):
            raise AdapterFailure(
                "boom at /Users/private/project for /Volumes/private/data; "
                "WB_RUNTIME_TOKEN=super-secret-token", 502, "runtime_unavailable")

    with caplog.at_level("WARNING", logger="workspace_bridge.plugin"):
        caplog.clear()
        kind, error = invoke(_FailingConversations(), "conversations.create",
                             {"body": {"workspaceId": "ws-one"}})
    assert kind == "error"
    assert error["data"] == {"status": 502, "code": "runtime_unavailable"}
    # The Node redacts secrets from the message; the log line is sanitized here.
    boundary = [record for record in caplog.records
                if "plugin request failed" in record.getMessage()]
    assert len(boundary) == 1
    message = boundary[0].getMessage()
    assert "status=502" in message and "code=runtime_unavailable" in message
    assert "[PATH]" in message and "[REDACTED_SECRET]" in message
    assert "/Users/" not in message and "/Volumes/" not in message
    assert "super-secret-token" not in message and "ws-one" not in message
    assert len(message) <= 800


def test_conversation_4xx_not_logged_as_5xx(codex_adapter, caplog):
    class _RejectingConversations:
        def create_conversation(self, body):
            raise AdapterFailure("Unknown or changed security profile", 409,
                                 "profile_mismatch")

    with caplog.at_level("WARNING", logger="workspace_bridge.plugin"):
        caplog.clear()
        kind, error = invoke(_RejectingConversations(), "conversations.create",
                             {"body": {"workspaceId": "ws-one"}})
    assert kind == "error" and error["data"] == {"status": 409, "code": "profile_mismatch"}
    assert not [record for record in caplog.records
                if "plugin request failed" in record.getMessage()]


def test_failed_turn_leaves_the_conversation_ready_for_the_next_run(codex_adapter):
    adapter, workspace, rpc = codex_adapter
    conversation = new_conversation(adapter, workspace)
    rpc.status = "systemError"
    assert adapter.conversation(conversation["id"])["status"] == "idle"
    run = adapter.start_run(conversation["id"], {
        "security": _SECURITY[conversation["id"]], "input": [{"type": "text", "text": "Again"}]})
    assert run["phase"] == "active"


def test_runs_without_a_model_use_the_threads_own_model(codex_adapter):
    adapter, workspace, rpc = codex_adapter
    rpc.thread_model = {"model": "gpt-test", "reasoningEffort": "medium"}
    conversation = new_conversation(adapter, workspace)
    security = _SECURITY[conversation["id"]]
    first = adapter.start_run(conversation["id"], {
        "security": security, "model": "gpt-other", "input": [{"type": "text", "text": "One"}]})
    finish_fake_run(adapter, rpc, conversation, first)
    adapter.start_run(conversation["id"], {
        "security": security, "input": [{"type": "text", "text": "Two"}]})
    starts = [params for method, params in rpc.calls if method == "turn/start"]
    # Codex keeps a turn's model for later turns, so the default is sent explicitly.
    assert (starts[0]["model"], "effort" in starts[0]) == ("gpt-other", False)
    assert (starts[1]["model"], starts[1]["effort"]) == ("gpt-test", "medium")


def test_a_just_started_turn_listed_as_interrupted_stays_active(codex_adapter):
    adapter, workspace, rpc = codex_adapter
    conversation = new_conversation(adapter, workspace)
    run = adapter.start_run(conversation["id"], {
        "security": _SECURITY[conversation["id"]], "input": [{"type": "text", "text": "Go"}]})
    rpc.turns = [{"id": run["nativeId"], "status": "interrupted", "completedAt": None,
                  "items": []}]
    assert adapter.run(run["id"])["phase"] == "active"
    rpc.turns[0]["completedAt"] = 1793060979
    assert adapter.run(run["id"])["outcome"] == "interrupted"


def test_a_run_after_a_restart_resumes_the_unloaded_thread(codex_adapter):
    adapter, workspace, rpc = codex_adapter
    conversation = new_conversation(adapter, workspace)
    rpc.status = "notLoaded"
    run = adapter.start_run(conversation["id"], {
        "security": _SECURITY[conversation["id"]], "input": [{"type": "text", "text": "Go on"}]})
    assert run["phase"] == "active"
    methods = [method for method, _ in rpc.calls]
    assert methods.index("thread/resume") < methods.index("turn/start")


def test_rules_see_through_the_shell_codex_wraps_commands_in(codex_adapter):
    adapter, workspace, rpc = codex_adapter
    rules = sp.validate({**TRUSTED, "commands": [{"prefix": "git push", "mode": "deny"}]})
    conversation = new_conversation(adapter, workspace, "team", rules)
    adapter.start_run(conversation["id"], {
        "security": _SECURITY[conversation["id"]],
        "input": [{"type": "text", "text": "Work"}]})
    ids = {"threadId": "native-thread-1", "turnId": "native-turn-1",
           "availableDecisions": ["accept", "decline", "cancel"]}
    adapter._request(1, "item/commandExecution/requestApproval",
                     {**ids, "command": "/bin/zsh -lc 'git push origin main'"})
    adapter._request(2, "item/commandExecution/requestApproval",
                     {**ids, "command": ["/bin/zsh", "-lc", "cd app && git push"]})
    adapter._request(3, "item/commandExecution/requestApproval",
                     {**ids, "command": "/bin/zsh -lc 'git status'"})
    assert rpc.responses == [(1, {"decision": "decline"}, None),
                             (2, {"decision": "decline"}, None),
                             (3, {"decision": "accept"}, None)]
