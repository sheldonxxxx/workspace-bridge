"""Codex adapter contract checks with a scripted app-server, no model calls."""
from __future__ import annotations

import json

import pytest
from httpx import ASGITransport, AsyncClient

from workspace_bridge.codex_host_adapter import (AdapterFailure, CodexHostAdapter,
                                                  make_app)
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
        self.active_permission_profile_override = None
        self.settings_update_error = None
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
            return {"thread": {"id": thread_id}, "cwd": params["cwd"], **settings}
        if method == "thread/read":
            return {"thread": {"id": params["threadId"],
                               "status": {"type": self.status}}}
        if method == "thread/settings/update":
            if self.settings_update_error:
                raise CodexRpcError(self.settings_update_error)
            thread_id = params["threadId"]
            settings = self.thread_settings[thread_id]
            if "permissions" in params:
                settings["activePermissionProfile"] = {"id": params["permissions"]}
            if "approvalPolicy" in params:
                settings["approvalPolicy"] = params["approvalPolicy"]
            if "approvalsReviewer" in params:
                settings["approvalsReviewer"] = params["approvalsReviewer"]
            if self.settings_update_notify and self.on_notification:
                self.on_notification("thread/settings/updated", {
                    "threadId": thread_id, "threadSettings": dict(settings)})
            return {}
        if method == "turn/start":
            self.status = "active"
            self.next_turn += 1
            return {"turn": {"id": f"native-turn-{self.next_turn}"}}
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
    profile = next(row for row in adapter.profiles(
        "ws_test", str(workspace))["profiles"]
                   if row["id"] == "workspace-write-reviewed")
    return adapter.create_conversation({
        "workspaceId": "ws_test", "directory": str(workspace),
        "securityProfile": {"id": "workspace-write-reviewed",
                            "revision": profile["revision"]},
    })


def new_runtime_config_conversation(adapter, workspace, rpc, workspace_id="ws_test"):
    runtime_config = adapter.profiles(
        workspace_id, str(workspace), fresh=True)["runtimeConfig"]
    return adapter.create_conversation({
        "workspaceId": workspace_id, "directory": str(workspace),
        "securityBinding": {"source": "runtime-config",
                            "revision": runtime_config["revision"]},
    })


def finish_fake_run(adapter, rpc, conversation, run):
    adapter._notification("turn/completed", {
        "threadId": conversation["nativeId"],
        "turn": {"id": run["nativeId"], "status": "completed"}})
    rpc.status = "idle"


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
    profile_row = next(row for row in adapter.profiles(
        "ws-test", str(workspace))["profiles"] if row["id"] == "read-only")
    profile = {"id": "read-only", "revision": profile_row["revision"]}
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
    config = {"permissions": ":workspace", "approvalPolicy": "on-request",
              "approvalsReviewer": "user"}
    saved = adapter.save_profile({"id": "team-reviewed", "config": config})
    assert saved["config"] == config
    assert next(row for row in adapter.profiles()["profiles"]
                if row["id"] == "team-reviewed")["mutable"] is True
    workspace_profile = next(row for row in adapter.profiles(
        "ws-test", str(workspace))["profiles"] if row["id"] == "team-reviewed")
    conversation = adapter.create_conversation({
        "workspaceId": "ws-test", "directory": str(workspace),
        "securityProfile": {"id": saved["id"], "revision": workspace_profile["revision"]}})
    start = next(params for method, params in rpc.calls if method == "thread/start")
    assert start["permissions"] == ":workspace"
    assert "sandbox" not in start and start["approvalsReviewer"] == "user"
    run = adapter.start_run(conversation["id"], {
        "input": [{"type": "text", "text": "Review this project"}]})
    with pytest.raises(AdapterFailure) as active:
        adapter.save_profile({"id": "team-reviewed",
            "config": {**config, "permissions": ":read-only"},
            "expectedRevision": saved["revision"]})
    assert active.value.code == "conflict"
    adapter._notification("turn/completed", {
        "threadId": "native-thread-1", "turn": {"id": run["nativeId"],
                                              "status": "completed"}})
    changed = adapter.save_profile({"id": "team-reviewed",
        "config": {**config, "permissions": ":read-only"},
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


def test_permission_catalog_is_scoped_to_exact_workspace(codex_adapter, tmp_path):
    adapter, workspace, rpc = codex_adapter
    other = workspace.parent / "other"
    other.mkdir()
    rpc.catalog_by_cwd[str(workspace)] = [*rpc.catalog,
        {"id": "project-only", "allowed": True, "description": None}]
    saved = adapter.save_profile({"id": "project-profile", "config": {
        "permissions": "project-only", "approvalPolicy": "on-request",
        "approvalsReviewer": "user"}})
    first = adapter.profiles("ws-a", str(workspace))
    second = adapter.profiles("ws-b", str(other))
    first_row = next(row for row in first["profiles"] if row["id"] == saved["id"])
    second_row = next(row for row in second["profiles"] if row["id"] == saved["id"])
    assert first_row["available"] is True
    assert second_row["available"] is False
    with pytest.raises(AdapterFailure) as unavailable:
        adapter.create_conversation({"workspaceId": "ws-b", "directory": str(other),
            "securityProfile": {"id": saved["id"], "revision": first_row["revision"]}})
    assert unavailable.value.code == "profile_unavailable"
    assert not any(method == "thread/start" for method, _ in rpc.calls)


def test_profile_effective_revision_tracks_native_security_changes(codex_adapter):
    adapter, workspace, rpc = codex_adapter
    before = next(row for row in adapter.profiles(
        "ws-test", str(workspace), fresh=True)["profiles"]
                  if row["id"] == "workspace-write-reviewed")
    rpc.security_config = {"config": {
        "permissions": {"workspace": {"network": {"enabled": True}}},
        "sandbox_mode": "workspace-write", "sandbox_workspace_write": None},
        "origins": {}}
    after = next(row for row in adapter.profiles(
        "ws-test", str(workspace), fresh=True)["profiles"]
                 if row["id"] == "workspace-write-reviewed")
    assert before["revision"] != after["revision"]
    assert "network" not in str(after)
    assert str(workspace) not in after["revision"]


def test_effective_legacy_workspace_write_config_fails_closed(codex_adapter):
    adapter, workspace, rpc = codex_adapter
    rpc.security_config = {"config": {
        "sandbox_mode": "workspace-write",
        "sandbox_workspace_write": {"network_access": True}},
        "origins": {"sandbox_workspace_write": {"type": "user"}}}
    with pytest.raises(AdapterFailure) as conflict:
        new_conversation(adapter, workspace)
    assert conflict.value.code == "profile_unavailable"
    assert "sandbox_workspace_write" in str(conflict.value)
    assert str(workspace) not in str(conflict.value)
    assert not any(method == "thread/start" for method, _ in rpc.calls)


def test_runtime_config_start_omits_security_overrides_and_persists_snapshot(codex_adapter):
    adapter, workspace, rpc = codex_adapter
    conversation = new_runtime_config_conversation(adapter, workspace, rpc)
    start = next(params for method, params in rpc.calls if method == "thread/start")
    assert set(start) == {"cwd", "ephemeral"}
    assert conversation["securityBinding"]["source"] == "runtime-config"
    assert conversation["securityBinding"]["resolvedSummary"] == {
        "activePermissionProfile": ":workspace", "approvalPolicy": "on-request",
        "approvalsReviewer": "user", "provenance": "implicit/default"}
    with adapter.lock:
        row = adapter.db.execute(
            "SELECT source,applied_revision,security_snapshot FROM conversations WHERE id=?",
            (conversation["id"],)).fetchone()
    assert row["source"] == "runtime-config"
    assert row["applied_revision"] == conversation["securityBinding"]["revision"]
    assert json.loads(row["security_snapshot"]) == conversation["securityBinding"]["resolvedSummary"]


def test_runtime_config_unchanged_reuses_thread_without_settings_update(codex_adapter):
    adapter, workspace, rpc = codex_adapter
    conversation = new_runtime_config_conversation(adapter, workspace, rpc)
    first = adapter.start_run(conversation["id"], {
        "input": [{"type": "text", "text": "first"}]})
    finish_fake_run(adapter, rpc, conversation, first)
    second = adapter.start_run(conversation["id"], {
        "input": [{"type": "text", "text": "second"}]})
    assert second["conversationId"] == conversation["id"]
    assert not any(method == "thread/settings/update" for method, _ in rpc.calls)


def test_runtime_config_changed_profile_id_updates_same_thread_before_turn(codex_adapter):
    adapter, workspace, rpc = codex_adapter
    rpc.catalog.extend([
        {"id": "profile-p", "allowed": True, "description": "P"},
        {"id": "profile-q", "allowed": True, "description": "Q"},
    ])
    rpc.security_config = {"config": {"default_permissions": "profile-p",
        "permissions": {}, "approval_policy": "on-request",
        "approvals_reviewer": "user"}, "origins": {}}
    conversation = new_runtime_config_conversation(adapter, workspace, rpc)
    first = adapter.start_run(conversation["id"], {
        "input": [{"type": "text", "text": "first"}]})
    finish_fake_run(adapter, rpc, conversation, first)
    rpc.security_config["config"]["default_permissions"] = "profile-q"
    second = adapter.start_run(conversation["id"], {
        "input": [{"type": "text", "text": "next"}]})
    update = next(params for method, params in rpc.calls
                  if method == "thread/settings/update")
    assert update == {"threadId": "native-thread-1", "permissions": "profile-q"}
    assert second["conversationId"] == conversation["id"]
    assert second["securityBinding"]["revision"] != first["securityBinding"]["revision"]
    assert second["securityBinding"]["resolvedSummary"]["activePermissionProfile"] == "profile-q"


def test_runtime_config_same_profile_definition_change_reselects_same_id(codex_adapter):
    adapter, workspace, rpc = codex_adapter
    rpc.catalog.append({"id": "profile-p", "allowed": True, "description": "P"})
    rpc.security_config = {"config": {"default_permissions": "profile-p",
        "permissions": {"profile-p": {"extends": ":workspace", "network": {"enabled": False}}},
        "approval_policy": "on-request", "approvals_reviewer": "user"}, "origins": {}}
    conversation = new_runtime_config_conversation(adapter, workspace, rpc)
    first = adapter.start_run(conversation["id"], {
        "input": [{"type": "text", "text": "first"}]})
    finish_fake_run(adapter, rpc, conversation, first)
    rpc.security_config["config"]["permissions"]["profile-p"]["network"]["enabled"] = True
    second = adapter.start_run(conversation["id"], {
        "input": [{"type": "text", "text": "next"}]})
    update = next(params for method, params in rpc.calls
                  if method == "thread/settings/update")
    assert update["permissions"] == "profile-p"
    assert second["conversationId"] == conversation["id"]
    assert second["securityBinding"]["revision"] != first["securityBinding"]["revision"]


def test_runtime_config_approval_and_reviewer_change_does_not_reselect_profile(codex_adapter):
    adapter, workspace, rpc = codex_adapter
    rpc.catalog.append({"id": "profile-p", "allowed": True, "description": "P"})
    rpc.security_config = {"config": {"default_permissions": "profile-p",
        "permissions": {}, "approval_policy": "on-request",
        "approvals_reviewer": "user"}, "origins": {}}
    conversation = new_runtime_config_conversation(adapter, workspace, rpc)
    first = adapter.start_run(conversation["id"], {
        "input": [{"type": "text", "text": "first"}]})
    finish_fake_run(adapter, rpc, conversation, first)
    rpc.security_config["config"]["approval_policy"] = "never"
    second = adapter.start_run(conversation["id"], {
        "input": [{"type": "text", "text": "next"}]})
    update = next(params for method, params in rpc.calls
                  if method == "thread/settings/update")
    assert update == {"threadId": "native-thread-1", "approvalPolicy": "never",
                      "approvalsReviewer": "user"}
    assert second["conversationId"] == conversation["id"]
    assert second["securityBinding"]["resolvedSummary"]["approvalPolicy"] == "never"


def test_runtime_config_never_updates_security_while_turn_is_active(codex_adapter):
    adapter, workspace, rpc = codex_adapter
    conversation = new_runtime_config_conversation(adapter, workspace, rpc)
    adapter.start_run(conversation["id"], {
        "input": [{"type": "text", "text": "first"}]})
    rpc.security_config["config"]["approval_policy"] = "never"
    with pytest.raises(AdapterFailure) as busy:
        adapter.start_run(conversation["id"], {
            "input": [{"type": "text", "text": "next"}]})
    assert busy.value.code == "conversation_busy"
    assert not any(method == "thread/settings/update" for method, _ in rpc.calls)


def test_unconfirmed_runtime_settings_update_replaces_conversation(codex_adapter):
    adapter, workspace, rpc = codex_adapter
    rpc.catalog.append({"id": "profile-p", "allowed": True, "description": "P"})
    rpc.security_config = {"config": {"default_permissions": "profile-p",
        "permissions": {"profile-p": {"extends": ":workspace"}},
        "approval_policy": "on-request", "approvals_reviewer": "user"}, "origins": {}}
    conversation = new_runtime_config_conversation(adapter, workspace, rpc)
    first = adapter.start_run(conversation["id"], {
        "input": [{"type": "text", "text": "first"}]})
    finish_fake_run(adapter, rpc, conversation, first)
    rpc.security_config["config"]["permissions"]["profile-p"]["extends"] = ":read-only"
    rpc.settings_update_notify = False
    second = adapter.start_run(conversation["id"], {
        "input": [{"type": "text", "text": "next"}]})
    assert second["conversationId"] != conversation["id"]
    assert second["securityBinding"]["replacementReason"] == "settings-update-unconfirmed"
    assert second["securityBinding"]["replacedConversationId"] == conversation["id"]


def test_legacy_sandbox_drift_falls_back_to_native_thread(codex_adapter):
    adapter, workspace, rpc = codex_adapter
    rpc.catalog.append({"id": "profile-p", "allowed": True, "description": "P"})
    rpc.security_config = {"config": {"default_permissions": "profile-p",
        "permissions": {}, "approval_policy": "on-request",
        "approvals_reviewer": "user"}, "origins": {}}
    conversation = new_runtime_config_conversation(adapter, workspace, rpc)
    first = adapter.start_run(conversation["id"], {
        "input": [{"type": "text", "text": "first"}]})
    finish_fake_run(adapter, rpc, conversation, first)
    rpc.security_config = {"config": {"sandbox_mode": "workspace-write",
        "sandbox_workspace_write": {"writable_roots": ["/tmp"]}}, "origins": {}}
    second = adapter.start_run(conversation["id"], {
        "input": [{"type": "text", "text": "next"}]})
    assert second["conversationId"] != conversation["id"]
    assert second["securityBinding"]["replacementReason"] == "legacy-sandbox-transition"
    assert not any(method == "thread/settings/update" for method, _ in rpc.calls)


def test_managed_untrusted_requirement_is_preserved_but_not_selectable(codex_adapter):
    adapter, workspace, rpc = codex_adapter
    config = {"permissions": ":workspace", "approvalPolicy": "on-request",
              "approvalsReviewer": "user"}
    saved = adapter.save_profile({"id": "managed-choice", "config": config})
    rpc.requirements = {"allowedApprovalPolicies": ["untrusted", "on-request"],
                        "allowedApprovalsReviewers": ["user"],
                        "allowedPermissionProfiles": {":workspace": True}}
    selected = next(row for row in adapter.profiles(
        "ws-test", str(workspace), fresh=True)["profiles"]
                    if row["id"] == saved["id"])
    assert selected["available"] is True
    conversation = adapter.create_conversation({"workspaceId": "ws-test", "directory": str(workspace),
        "securityProfile": {"id": saved["id"], "revision": selected["revision"]}})
    start = next(params for method, params in rpc.calls if method == "thread/start")
    assert start["approvalPolicy"] == "on-request"
    assert start["approvalsReviewer"] == "user"

    rpc.requirements = {"allowedApprovalPolicies": ["on-request"],
                        "allowedApprovalsReviewers": ["user"],
                        "allowedPermissionProfiles": {":workspace": True}}
    revised = next(row for row in adapter.profiles(
        "ws-test", str(workspace), fresh=True)["profiles"]
                   if row["id"] == saved["id"])
    assert revised["available"] is True
    assert revised["revision"] != selected["revision"]
    with pytest.raises(AdapterFailure) as stale:
        adapter.conversation(conversation["id"])
    assert stale.value.code == "profile_mismatch"

    rpc.requirements = {"allowedApprovalPolicies": ["untrusted"],
                        "allowedApprovalsReviewers": ["user"],
                        "allowedPermissionProfiles": {":workspace": True}}
    denied = next(row for row in adapter.profiles(
        "ws-test", str(workspace), fresh=True)["profiles"]
                  if row["id"] == saved["id"])
    assert denied["available"] is False
    assert ":workspace" in {
        row["id"] for row in adapter.profiles(
            "ws-test", str(workspace), fresh=True)["permissionProfiles"]}

    with pytest.raises(AdapterFailure):
        adapter.save_profile({"id": "unsafe-never", "config": {
            "permissions": ":workspace", "approvalPolicy": "never",
            "approvalsReviewer": "auto_review"}})


def test_stored_legacy_profile_is_normalized_to_v2(codex_adapter):
    adapter, _, _ = codex_adapter
    old = {"sandbox": "danger-full-access", "approvalPolicy": "on-request",
           "approvalsReviewer": "user"}
    with adapter.lock, adapter.db:
        adapter.db.execute("INSERT INTO security_profiles VALUES(?,?,?)",
                           ("legacy-full", json.dumps(old), "legacy-revision"))
    adapter._migrate_legacy_profile_rows()
    row = adapter.db.execute(
        "SELECT config,revision FROM security_profiles WHERE id='legacy-full'").fetchone()
    migrated = json.loads(row["config"])
    assert migrated == {"permissions": ":danger-full-access",
                        "approvalPolicy": "on-request", "approvalsReviewer": "user"}
    assert "sandbox" not in migrated and row["revision"] != "legacy-revision"


def test_active_native_permission_profile_mismatch_fails_binding(codex_adapter):
    adapter, workspace, rpc = codex_adapter
    rpc.active_permission_profile_override = {"id": ":read-only"}
    with pytest.raises(AdapterFailure) as mismatch:
        new_conversation(adapter, workspace)
    assert mismatch.value.code == "binding_mismatch"
    assert not adapter.db.execute("SELECT 1 FROM conversations").fetchone()


def test_new_profile_saves_reject_legacy_schema(codex_adapter):
    adapter, _, _ = codex_adapter
    with pytest.raises(AdapterFailure, match="permissions"):
        adapter.save_profile({"id": "legacy-new", "config": {
            "sandbox": "workspace-write", "approvalPolicy": "on-request",
            "approvalsReviewer": "user"}})


def test_existing_untrusted_profile_remains_unchanged_and_invalid(codex_adapter):
    adapter, _, _ = codex_adapter
    config = {"permissions": ":workspace", "approvalPolicy": "untrusted",
              "approvalsReviewer": "user"}
    with adapter.lock, adapter.db:
        adapter.db.execute("INSERT INTO security_profiles VALUES(?,?,?)",
                           ("old-untrusted", json.dumps(config), "old-revision"))

    adapter._migrate_legacy_profile_rows()

    row = adapter.db.execute(
        "SELECT config,revision FROM security_profiles WHERE id='old-untrusted'").fetchone()
    assert json.loads(row["config"]) == config
    assert row["revision"] == "old-revision"
    assert "old-untrusted" not in {
        profile["id"] for profile in adapter.profiles()["profiles"]}
    with pytest.raises(AdapterFailure) as invalid:
        adapter._profile_definition("old-untrusted")
    assert invalid.value.code == "profile_mismatch"


@pytest.mark.parametrize("config", [
    {"permissions": ":workspace", "approvalPolicy": "untrusted",
     "approvalsReviewer": "user"},
    {"permissions": ":workspace", "approvalPolicy": [],
     "approvalsReviewer": "user"},
    {"permissions": ":workspace", "approvalPolicy": "on-request",
     "approvalsReviewer": {}},
    {"permissions": "/private/project", "approvalPolicy": "on-request",
     "approvalsReviewer": "user"},
    {"sandbox": [], "approvalPolicy": "on-request",
     "approvalsReviewer": "user"},
])
def test_malformed_profile_values_are_rejected_cleanly(codex_adapter, config):
    adapter, _, _ = codex_adapter
    with pytest.raises(AdapterFailure):
        adapter.save_profile({"id": "malformed", "config": config})


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
        with caplog.at_level("CRITICAL", logger="uvicorn.error"):
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
