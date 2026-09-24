"""One coordinator drives a conforming adapter without native runtime types."""
from __future__ import annotations

import json
import threading

import pytest
import httpx

from workspace_bridge.api import Handoff, make_admin
from workspace_bridge.cli import initialize
from workspace_bridge.codex_host_adapter import CodexHostAdapter
from workspace_bridge.notifications import NotificationManager
from workspace_bridge.runtime import RuntimeRejected
from workspace_bridge.security import BridgeError
from workspace_bridge.service import Service
from workspace_bridge.wbrp import Descriptor

from test_codex_host_adapter import FakeCodexRpc
from notification_fakes import RecordingChannel


def attach_notification_channel(service, channel):
    service.notification_manager.close()
    manager = NotificationManager(service, [channel])
    service.notification_manager = manager
    manager.start()
    return manager


def wait_for_notifications(manager):
    assert manager._idle.wait(2), "notification worker did not drain pending rows"


class DirectAdapter:
    """In-process WBRP transport for tests; production uses HttpRuntimeAdapter."""
    def __init__(self, native):
        self.native = native

    def descriptor(self):
        return Descriptor.parse(self.native.descriptor(), expected_runtime="codex")

    def models(self, workspace_id):
        return self.native.models()["models"]

    def profiles(self):
        return self.native.profiles()["profiles"]

    def save_profile(self, profile_id, config, expected_revision):
        return self.native.save_profile({"id": profile_id, "config": config,
                                         "expectedRevision": expected_revision})

    def delete_profile(self, profile_id):
        return self.native.delete_profile(profile_id)

    def create_conversation(self, payload):
        return self.native.create_conversation(payload)

    def conversation(self, ident):
        return self.native.conversation(ident)

    def start_run(self, ident, payload):
        return self.native.start_run(ident, payload)

    def run(self, ident):
        return self.native.run(ident)

    def find_run(self, conversation_id, client_run_id):
        return self.native.find_run(conversation_id, client_run_id)

    def interactions(self, ident):
        return self.native.interactions(ident)["interactions"]

    def resolve(self, ident, payload):
        return self.native.resolve(ident, payload)

    def activities(self, ident):
        return self.native.activities(ident)["activities"]

    def cancel(self, ident):
        return self.native.cancel(ident)


def test_custom_profile_assignment_tracks_revision_and_guards_delete(modern_env):
    service, ws_id, _, _, _, _ = modern_env
    coordinator = service.run_coordinator
    ws = service.workspace(ws_id)
    config = {"sandbox": "workspace-write", "approvalPolicy": "on-request",
              "approvalsReviewer": "user"}
    saved = coordinator.save_profile("codex", "team-reviewed", config, None)
    service.set_workspace_runtime(ws, "codex", False, saved["id"])
    grant = service.workspace_runtime_policy(ws)["runtimes"]["codex"]
    assert grant["enabled"] is False and grant["profile"]["id"] == saved["id"]
    service.set_workspace_runtime(ws, "codex", True, saved["id"])
    changed = coordinator.save_profile("codex", saved["id"],
        {**config, "sandbox": "read-only"}, saved["revision"])
    assert coordinator.profile(ws, "codex")["revision"] == changed["revision"]
    with pytest.raises(BridgeError, match="Assign another profile"):
        coordinator.delete_profile("codex", saved["id"])
    service.set_workspace_runtime(ws, "codex", True, "read-only")
    assert coordinator.delete_profile("codex", saved["id"]) == {"deleted": saved["id"]}


@pytest.mark.asyncio
async def test_custom_profile_manager_routes_require_admin(modern_env):
    service, _, _, _, _, _ = modern_env
    app = make_admin(service, service.config["admin_token_hash"])
    config = {"sandbox": "workspace-write", "approvalPolicy": "on-request",
              "approvalsReviewer": "user"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://127.0.0.1:8766") as client:
        assert (await client.post("/api/runtimes/codex/profiles", json={
            "id": "custom", "config": config,
            "expected_revision": None})).status_code == 401
        token = (service.state / "admin-token").read_text().strip()
        headers = {"Authorization": "Bearer " + token}
        created = await client.post("/api/runtimes/codex/profiles", json={
            "id": "custom", "config": config,
            "expected_revision": None}, headers=headers)
        assert created.status_code == 200
        assert created.json()["config"] == config
        listed = await client.get("/api/runtimes/codex/profiles", headers=headers)
        assert any(row["id"] == "custom" for row in listed.json()["profiles"])
        status = await client.get("/api/status", headers=headers)
        assert status.status_code == 200
        assert status.json()["notifications"]["channels"] == {}
        assert "pi" not in status.json()
        deleted = await client.delete("/api/runtimes/codex/profiles/custom", headers=headers)
        assert deleted.status_code == 200


@pytest.fixture
def modern_env(tmp_path, payload):
    parent = tmp_path / "projects"
    parent.mkdir()
    root = parent / "alpha"
    root.mkdir()
    state = tmp_path / "bridge-state"
    cfg = initialize(state, [str(parent)], 8765, 8766)
    native_rpc = FakeCodexRpc()
    native = CodexHostAdapter(tmp_path / "codex-state", parent, rpc=native_rpc)
    service = Service(state, cfg, adapters={"codex": DirectAdapter(native)},
                      run_coordinator_background=False)
    ws_id = service.add_workspace("Alpha", str(root), [])["workspace"]["id"]
    token = service.manage_bridge("rotate_token")["token"]
    service.manage_workspace(ws_id, "enable")
    service.manage_workspace(ws_id, "set_agent_enabled", agent_enabled=True)
    service.set_workspace_runtime(service.workspace(ws_id), "codex", True,
                                  "workspace-write-reviewed")
    service.run_coordinator.set_model_policy("codex", ["gpt-test"], "gpt-test")
    job = service.call(ws_id, token, "prepare_handoff",
                       Handoff.model_validate(payload).model_dump())
    yield service, ws_id, token, job, native, native_rpc
    service.close()
    native.close()


def test_bridge_start_reconcile_interaction_and_activity(modern_env):
    service, ws_id, token, job, native, rpc = modern_env
    recorder = RecordingChannel()
    manager = attach_notification_channel(service, recorder)
    started = service.call(ws_id, token, "start_agent_run", {
        "runtime": "codex", "job_id": job["id"], "request_id": "modern-1"})
    bridge_id = started["run_id"]
    assert started["phase"] == "active" and started["runtime"] == "codex"
    repeated = service.call(ws_id, token, "start_agent_run", {
        "runtime": "codex", "job_id": job["id"], "request_id": "modern-1"})
    assert repeated["run_id"] == bridge_id and repeated["idempotent"] is True
    native._request(99, "item/commandExecution/requestApproval", {
        "threadId": "native-thread-1", "turnId": "native-turn-1",
        "reason": "Run tests", "availableDecisions": ["accept", "decline"]})
    viewed = service.call(ws_id, token, "read_agent_run", {"run_id": bridge_id})
    assert viewed["active_state"] == "waiting_interaction"
    wait_for_notifications(manager)
    assert [event.event_type for event in recorder.calls] == ["run_needs_attention"]
    assert recorder.calls[0].subject_id == viewed["interactions"][0]["id"]
    service.call(ws_id, token, "read_agent_run", {"run_id": bridge_id})
    assert len(recorder.calls) == 1
    interaction = viewed["interactions"][0]
    choice = next(item for item in interaction["details"]["choices"]
                  if item["semantic"] == "approve")
    service.call(ws_id, token, "respond_agent_interaction", {
        "run_id": bridge_id, "interaction_id": interaction["id"],
        "response": {"choiceId": choice["id"]}})
    assert rpc.responses == [(99, {"decision": "accept"}, None)]
    native._notification("item/completed", {
        "threadId": "native-thread-1", "turnId": "native-turn-1",
        "item": {"id": "command-1", "type": "commandExecution", "command": "pytest"}})
    native._notification("turn/completed", {
        "threadId": "native-thread-1", "turn": {"id": "native-turn-1",
                                                  "status": "completed"}})
    finished = service.call(ws_id, token, "read_agent_run", {"run_id": bridge_id})
    assert finished["outcome"] == "succeeded"
    wait_for_notifications(manager)
    service.call(ws_id, token, "read_agent_run", {"run_id": bridge_id})
    assert [event.event_type for event in recorder.calls] == [
        "run_needs_attention", "run_completed"]
    assert manager.summary(bridge_id)["overall"] == "sent"
    activities = service.call(ws_id, token, "list_agent_activities",
                              {"run_id": bridge_id})["activities"]
    assert activities[0]["kind"] == "command"


def test_runtime_outcome_mapping_covers_all_terminal_events():
    from workspace_bridge.run_coordinator import RunCoordinator
    assert {outcome: RunCoordinator._outcome_event_type(outcome) for outcome in (
        "succeeded", "failed", "cancelled", "interrupted", "orphaned")} == {
            "succeeded": "run_completed", "failed": "run_failed",
            "cancelled": "run_cancelled", "interrupted": "run_interrupted",
            "orphaned": "run_orphaned"}


@pytest.mark.parametrize(("outcome", "event_type"), [
    ("succeeded", "run_completed"), ("failed", "run_failed"),
    ("cancelled", "run_cancelled"), ("interrupted", "run_interrupted"),
    ("orphaned", "run_orphaned"),
])
def test_snapshot_terminal_outcomes_create_one_event(modern_env, outcome, event_type):
    service, ws_id, token, job, native, _ = modern_env
    recorder = RecordingChannel()
    manager = attach_notification_channel(service, recorder)
    started = service.call(ws_id, token, "start_agent_run", {
        "runtime": "codex", "job_id": job["id"], "request_id": "outcome-" + outcome})
    coordinator = service.run_coordinator
    ws = service.workspace(ws_id)
    run = coordinator._run_row(ws, started["run_id"])
    snapshot = coordinator.adapter("codex").run(run["native_id"])
    terminal = {**snapshot, "phase": "terminal", "activeState": None,
                "outcome": outcome, "result": "bounded result"}
    coordinator._persist_snapshot(run, terminal, [], [])
    coordinator._persist_snapshot(run, terminal, [], [])
    wait_for_notifications(manager)
    assert [event.event_type for event in recorder.calls] == [event_type]


def test_slow_notification_does_not_block_run_state_transition(modern_env):
    service, ws_id, token, job, _, _ = modern_env

    class BlockingChannel:
        channel_id = "blocking"
        name = "Blocking"
        enabled = True

        def __init__(self):
            self.entered = threading.Event()
            self.release = threading.Event()

        def deliver(self, event):
            self.entered.set()
            self.release.wait()
            from workspace_bridge.notifications import NotificationResult
            return NotificationResult("sent", 1)

    channel = BlockingChannel()
    manager = attach_notification_channel(service, channel)
    started = service.call(ws_id, token, "start_agent_run", {
        "runtime": "codex", "job_id": job["id"], "request_id": "slow-notification"})
    coordinator = service.run_coordinator
    ws = service.workspace(ws_id)
    run = coordinator._run_row(ws, started["run_id"])
    snapshot = coordinator.adapter("codex").run(run["native_id"])
    terminal = {**snapshot, "phase": "terminal", "activeState": None,
                "outcome": "succeeded", "result": "completed"}
    transition_done = threading.Event()
    transition_errors = []

    def transition():
        try:
            coordinator._persist_snapshot(run, terminal, [], [])
        except Exception as exc:  # capture worker-thread assertion context
            transition_errors.append(exc)
        finally:
            transition_done.set()

    transition_thread = threading.Thread(target=transition)
    transition_thread.start()
    try:
        assert channel.entered.wait(2), "notification worker did not enter the channel"
        assert transition_done.wait(2), "run transition waited for channel delivery"
    finally:
        channel.release.set()
        transition_thread.join(timeout=2)
    assert not transition_thread.is_alive()
    assert transition_errors == []
    wait_for_notifications(manager)
    assert manager.summary(started["run_id"])["overall"] == "sent"


def test_distinct_runtime_interactions_notify_once_without_contents(modern_env):
    service, ws_id, token, job, native, _ = modern_env
    recorder = RecordingChannel()
    manager = attach_notification_channel(service, recorder)
    started = service.call(ws_id, token, "start_agent_run", {
        "runtime": "codex", "job_id": job["id"], "request_id": "interaction-dedupe"})
    coordinator = service.run_coordinator
    ws = service.workspace(ws_id)
    run = coordinator._run_row(ws, started["run_id"])
    snapshot = coordinator.adapter("codex").run(run["native_id"])
    secret_text = "private interaction body must not enter notifications"
    interactions = [{"runId": run["native_id"], "id": "native-a", "kind": "choice",
                     "prompt": secret_text},
                    {"runId": run["native_id"], "id": "native-b", "kind": "form",
                     "prompt": secret_text}]
    coordinator._persist_snapshot(run, snapshot, interactions, [])
    coordinator._persist_snapshot(run, snapshot, interactions, [])
    wait_for_notifications(manager)
    assert [event.event_type for event in recorder.calls] == [
        "run_needs_attention", "run_needs_attention"]
    assert len({event.subject_id for event in recorder.calls}) == 2
    with service.lock, service.db:
        service.db.execute(
            "UPDATE runtime_interactions SET state='resolved' WHERE run=? AND native_id='native-a'",
            (run["id"],))
    coordinator._persist_snapshot(run, snapshot, interactions, [])
    assert len(recorder.calls) == 2
    with service.lock:
        raw = json.dumps([dict(row) for row in service.db.execute(
            "SELECT * FROM notification_events WHERE run_id=?", (run["id"],))])
        state = service.db.execute(
            "SELECT state FROM runtime_interactions WHERE run=? AND native_id='native-a'",
            (run["id"],)).fetchone()["state"]
    assert secret_text not in raw
    assert state == "resolved"


def test_runtime_rejected_after_bridge_run_creation_records_failure(modern_env, monkeypatch):
    service, ws_id, token, job, native, _ = modern_env
    recorder = RecordingChannel()
    manager = attach_notification_channel(service, recorder)

    def reject(*args, **kwargs):
        raise RuntimeRejected("Runtime rejected request", "request_rejected")

    monkeypatch.setattr(native, "start_run", reject)
    with pytest.raises(RuntimeRejected):
        service.call(ws_id, token, "start_agent_run", {
            "runtime": "codex", "job_id": job["id"], "request_id": "reject-notification"})
    wait_for_notifications(manager)
    with service.lock:
        row = service.db.execute(
            "SELECT id,outcome FROM runtime_runs WHERE request_id='reject-notification'").fetchone()
    assert row["outcome"] == "failed"
    assert [event.event_type for event in recorder.calls] == ["run_failed"]


def test_runtime_grant_defaults_off_for_new_workspace(tmp_path):
    parent = tmp_path / "projects"
    parent.mkdir()
    root = parent / "alpha"
    root.mkdir()
    state = tmp_path / "bridge-state"
    cfg = initialize(state, [str(parent)], 8765, 8766)
    native = CodexHostAdapter(tmp_path / "codex-state", parent, rpc=FakeCodexRpc())
    service = Service(state, cfg, adapters={"codex": DirectAdapter(native)},
                      run_coordinator_background=False)
    try:
        ws_id = service.add_workspace("Alpha", str(root), [])["workspace"]["id"]
        service.manage_workspace(ws_id, "enable")
        service.manage_workspace(ws_id, "set_agent_enabled", agent_enabled=True)
        assert service.workspace_runtime_policy(service.workspace(ws_id))["runtimes"]["codex"] == {
            "enabled": False, "profile": None}
        with pytest.raises(BridgeError) as exc:
            service.require_workspace_runtime(service.workspace(ws_id), "codex")
        assert exc.value.code == "runtime_disabled"
    finally:
        service.close()
        native.close()


def test_unconfirmed_bridge_run_rebinds_by_client_run_id(modern_env):
    service, ws_id, token, job, native, _ = modern_env
    started = service.call(ws_id, token, "start_agent_run", {
        "runtime": "codex", "job_id": job["id"], "request_id": "rebind-1"})
    bridge_id = started["run_id"]
    with service.lock, service.db:
        service.db.execute(
            "UPDATE runtime_runs SET native_id=NULL,phase='starting',active_state=NULL "
            "WHERE id=?", (bridge_id,))
    recovered = service.call(ws_id, token, "read_agent_run", {"run_id": bridge_id})
    assert recovered["phase"] == "active"
    assert recovered["run_id"] == bridge_id
