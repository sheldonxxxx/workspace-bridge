"""One coordinator drives a conforming adapter without native runtime types."""
from __future__ import annotations

import json
import threading
from pathlib import Path

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
from conftest import attach_test_node, create_test_adapter, start_test_node

ADAPTER_ID = "adapter_000000000000000000000001"


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

    def profile_catalog(self, workspace_id=None, directory=None, *, fresh=False):
        if workspace_id is None:
            return self.native.profiles()
        return self.native.profiles(workspace_id, directory, fresh=fresh)

    def profiles(self):
        return self.profile_catalog()["profiles"]

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


class PiDirectAdapter(DirectAdapter):
    """Use the scripted native host contract to exercise a Pi family identity."""
    def descriptor(self):
        value = self.native.descriptor()
        value["runtime"]["id"] = "pi"
        return Descriptor.parse(value, expected_runtime="pi")


def test_custom_profile_assignment_tracks_revision_and_guards_delete(modern_env):
    service, ws_id, _, _, _, _ = modern_env
    coordinator = service.run_coordinator
    ws = service.workspace(ws_id)
    config = {"permissions": ":workspace", "approvalPolicy": "on-request",
              "approvalsReviewer": "user"}
    saved = coordinator.save_profile(ADAPTER_ID, "team-reviewed", config, None)
    service.set_workspace_route(ws, ADAPTER_ID, False, saved["id"])
    route = service.workspace_route_policy(ws)["routes"][ADAPTER_ID]
    assert route["enabled"] is False and route["profile"]["id"] == saved["id"]
    service.set_workspace_route(ws, ADAPTER_ID, True, saved["id"])
    changed = coordinator.save_profile(ADAPTER_ID, saved["id"],
        {**config, "permissions": ":read-only"}, saved["revision"])
    current = next(row for row in coordinator.profiles(ADAPTER_ID, ws, fresh=True)
                   if row["id"] == changed["id"])
    assert coordinator.profile(ws, ADAPTER_ID)["revision"] == current["revision"]
    with pytest.raises(BridgeError, match="Assign another profile"):
        coordinator.delete_profile(ADAPTER_ID, saved["id"])
    service.set_workspace_route(ws, ADAPTER_ID, True, "read-only")
    assert coordinator.delete_profile(ADAPTER_ID, saved["id"]) == {"deleted": saved["id"]}


def test_runtime_config_binding_persists_source_and_tracks_live_observation(modern_env):
    service, ws_id, _, _, _, rpc = modern_env
    coordinator = service.run_coordinator
    ws = service.workspace(ws_id)
    service.set_workspace_route(ws, ADAPTER_ID, True,
                                security_source="runtime-config")
    with service.lock:
        stored = service.db.execute(
            "SELECT security_source,profile_id,profile_revision FROM workspace_routes "
            "WHERE workspace=? AND adapter_id=?", (ws_id, ADAPTER_ID)).fetchone()
    assert stored["security_source"] == "runtime-config"
    assert stored["profile_id"] == ""
    first = coordinator.security_binding(ws, ADAPTER_ID)
    rpc.security_config["config"]["approval_policy"] = "never"
    second = coordinator.security_binding(ws, ADAPTER_ID)
    assert first["source"] == second["source"] == "runtime-config"
    assert first["revision"] != second["revision"]
    assert second["resolvedSummary"]["approvalPolicy"] == "never"
    with service.lock:
        persisted = service.db.execute(
            "SELECT profile_revision FROM workspace_routes WHERE workspace=? AND adapter_id=?",
            (ws_id, ADAPTER_ID)).fetchone()["profile_revision"]
    assert persisted == stored["profile_revision"]


def test_workspace_default_is_explicit_and_reported_per_adapter(modern_env):
    service, ws_id, _, _, _, _ = modern_env
    ws = service.workspace(ws_id)
    unbound_id = "adapter_000000000000000000000003"
    create_test_adapter(service, ws["node_id"], {
        "name": "Unbound Codex", "runtime_type": "codex",
        "base_url": "http://127.0.0.1:8768", "token": "unbound-secret",
    }, unbound_id)

    selected = service.set_workspace_default(ws, ADAPTER_ID)
    route = selected["routes"][ADAPTER_ID]
    assert route["is_default"] is True
    listed = {item["adapter_id"]: item
              for item in service.list_agent_adapters(ws)["adapters"]}
    assert listed[ADAPTER_ID]["default"] is True
    assert listed[unbound_id]["bound"] is False
    assert all(item["node_id"] == ws["node_id"] and
               item["node_name"] == "Local Node" for item in listed.values())

    cleared = service.set_workspace_default(ws, None)
    assert cleared["routes"][ADAPTER_ID]["is_default"] is False


def test_runtime_config_binding_remains_unsupported_for_pi(modern_env):
    service, ws_id, _, _, _, _ = modern_env
    ws = service.workspace(ws_id)

    class PiProfileCatalog:
        def profile_catalog(self, _workspace, *, fresh=False):
            return {"profiles": [], "permissionProfiles": []}

    pi_id = "adapter_000000000000000000000002"
    create_test_adapter(service, ws["node_id"], {"name": "Local Pi", "runtime_type": "pi",
        "base_url": "http://127.0.0.1:8767", "token": "pi-secret"}, pi_id)
    existing_client = service.adapter_registry.client
    service.adapter_registry.client = lambda adapter_id, **kwargs: (
        PiProfileCatalog() if adapter_id == pi_id else existing_client(adapter_id, **kwargs))
    with pytest.raises(BridgeError) as exc:
        service.set_workspace_route(ws, pi_id, True,
                                    security_source="runtime-config")
    assert exc.value.code == "runtime_config_unavailable"
    with service.lock:
        assert service.db.execute(
            "SELECT 1 FROM workspace_routes WHERE workspace=? AND adapter_id=?",
            (ws_id, pi_id)).fetchone() is None


def test_runtime_config_continuation_refreshes_same_conversation_before_next_turn(modern_env):
    service, ws_id, _, job, _, rpc = modern_env
    coordinator = service.run_coordinator
    ws = service.workspace(ws_id)
    service.set_workspace_route(ws, ADAPTER_ID, True,
                                security_source="runtime-config")
    first = coordinator.start(ws, ADAPTER_ID, job["id"], "runtime-config-first")
    assert first["phase"] == "active"
    assert first["effective_security"]["source"] == "runtime-config"
    assert first["effective_security"]["resolved_summary"]["approvalPolicy"] == "on-request"
    first_row = coordinator._run_row(ws, first["run_id"])
    first_conversation = coordinator._conversation(first_row)
    assert first_conversation["source"] == "runtime-config"
    native = coordinator.adapter(ADAPTER_ID).native
    native_turn_id = native.run(first_row["native_id"])["nativeId"]
    native_thread_id = native.conversation(first_conversation["native_id"])["nativeId"]
    native._notification("turn/completed", {
        "threadId": native_thread_id,
        "turn": {"id": native_turn_id, "status": "completed"}})
    assert native.run(first_row["native_id"])["phase"] == "terminal"
    rpc.status = "idle"
    coordinator.read(ws, first["run_id"])
    old_revision = first_conversation["revision"]
    rpc.security_config["config"]["approval_policy"] = "never"
    second = coordinator.start(
        ws, ADAPTER_ID, job["id"], "runtime-config-second",
        continue_from_run_id=first["run_id"])
    assert second["phase"] == "active"
    assert second["conversation_id"] == first["conversation_id"]
    second_row = coordinator._run_row(ws, second["run_id"])
    second_conversation = coordinator._conversation(second_row)
    assert second_conversation["revision"] != old_revision
    assert json.loads(second_conversation["security_snapshot"])["approvalPolicy"] == "never"
    assert second["effective_security"]["effective_revision"] == second_conversation["revision"]
    update = next(params for method, params in rpc.calls
                  if method == "thread/settings/update")
    assert update["threadId"] == native_thread_id
    assert "permissions" not in update


def test_diagnostics_marks_binding_stale_after_native_requirements_change(modern_env):
    service, ws_id, _, _, _, rpc = modern_env
    rpc.requirements = {
        "allowedApprovalPolicies": ["on-request", "untrusted"],
        "allowedApprovalsReviewers": ["user"],
        "allowedPermissionProfiles": {":workspace": True},
    }
    report = service.diagnostic_report()
    route = next(item for item in report["runnable_routes"]
                 if item["workspace_id"] == ws_id and item["adapter_id"] == ADAPTER_ID)
    assert route["ready"] is False
    assert "route.security_freshness" in route["blockers"]
    assert "allowedApprovalPolicies" not in json.dumps(report)


@pytest.mark.asyncio
async def test_admin_prepared_handoff_start_is_strict_and_idempotent(modern_env):
    service, ws_id, _, job, _, _ = modern_env
    app = make_admin(service, service.config["admin_token_hash"])
    token = (service.state / "admin-token").read_text().strip()
    headers = {"Authorization": f"Bearer {token}"}
    path = f"/api/workspaces/{ws_id}/jobs/{job['id']}/runs"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://127.0.0.1:8766") as client:
        assert (await client.post(path, json={
            "adapter_id": ADAPTER_ID, "request_id": "manager-start-1"
        })).status_code == 401

        extra = await client.post(path, headers=headers, json={
            "adapter_id": ADAPTER_ID, "request_id": "manager-start-1",
            "prompt": "run arbitrary instructions", "model": "gpt-test",
            "path": "/tmp/unrelated",
        })
        assert extra.status_code == 400

        malformed_path = await client.post(
            f"/api/workspaces/{ws_id}/jobs/not-a-job/runs", headers=headers,
            json={"adapter_id": ADAPTER_ID, "request_id": "manager-start-1"})
        assert malformed_path.status_code == 400

        body = {"adapter_id": ADAPTER_ID, "request_id": "manager-start-1"}
        first = await client.post(path, headers=headers, json=body)
        assert first.status_code == 200, first.text
        repeated = await client.post(path, headers=headers, json=body)
        assert repeated.status_code == 200, repeated.text
        assert first.json()["run_id"] == repeated.json()["run_id"]
        assert repeated.json()["idempotent"] is True
        with service.lock:
            count = service.db.execute(
                "SELECT count(*) FROM agent_runs WHERE workspace=? AND request_id=?",
                (ws_id, body["request_id"]),
            ).fetchone()[0]
        assert count == 1


@pytest.mark.asyncio
async def test_custom_profile_manager_routes_require_admin(modern_env):
    service, _, _, _, _, _ = modern_env
    app = make_admin(service, service.config["admin_token_hash"])
    config = {"permissions": ":workspace", "approvalPolicy": "on-request",
              "approvalsReviewer": "user"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://127.0.0.1:8766") as client:
        assert (await client.post(f"/api/adapters/{ADAPTER_ID}/profiles", json={
            "id": "custom", "config": config,
            "expected_revision": None})).status_code == 401
        token = (service.state / "admin-token").read_text().strip()
        headers = {"Authorization": "Bearer " + token}
        created = await client.post(f"/api/adapters/{ADAPTER_ID}/profiles", json={
            "id": "custom", "config": config,
            "expected_revision": None}, headers=headers)
        assert created.status_code == 200
        assert created.json()["config"] == config
        listed = await client.get(f"/api/adapters/{ADAPTER_ID}/profiles", headers=headers)
        assert any(row["id"] == "custom" for row in listed.json()["profiles"])
        status = await client.get("/api/status", headers=headers)
        assert status.status_code == 200
        assert status.json()["notifications"]["channels"] == {}
        assert "runtimes" not in status.json()
        deleted = await client.delete(f"/api/adapters/{ADAPTER_ID}/profiles/custom", headers=headers)
        assert deleted.status_code == 200


@pytest.mark.asyncio
async def test_codex_profile_catalog_api_uses_exact_workspace_context(modern_env):
    service, ws_id, _, _, _, rpc = modern_env
    first = service.workspace(ws_id)
    second_root = Path(service.workspace(ws_id)["root"]).parent / "beta"
    second_root.mkdir()
    second_id = service.add_workspace("Beta", str(second_root), [])["workspace"]["id"]
    first_root = str(Path(first["root"]).resolve())
    rpc.catalog_by_cwd[first_root] = [*rpc.catalog,
        {"id": "project-only", "allowed": True, "description": "Project access"}]
    profile = service.run_coordinator.save_profile(ADAPTER_ID, "project-access", {
        "permissions": "project-only", "approvalPolicy": "on-request",
        "approvalsReviewer": "user"}, None)
    app = make_admin(service, service.config["admin_token_hash"])
    token = (service.state / "admin-token").read_text().strip()
    headers = {"Authorization": "Bearer " + token}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://127.0.0.1:8766") as client:
        first_response = await client.get(
            f"/api/adapters/{ADAPTER_ID}/profiles?workspace_id={ws_id}&fresh=1", headers=headers)
        second_response = await client.get(
            f"/api/adapters/{ADAPTER_ID}/profiles?workspace_id={second_id}&fresh=1", headers=headers)
        invalid = await client.get(
            f"/api/adapters/{ADAPTER_ID}/profiles?workspace_id={ws_id}&fresh=yes",
            headers=headers)
    assert first_response.status_code == second_response.status_code == 200, (
        first_response.text, second_response.text)
    assert invalid.status_code == 400
    first_row = next(row for row in first_response.json()["profiles"]
                     if row["id"] == profile["id"])
    second_row = next(row for row in second_response.json()["profiles"]
                      if row["id"] == profile["id"])
    assert first_row["available"] is True
    assert second_row["available"] is False
    assert any(row["id"] == "project-only"
               for row in first_response.json()["permissionProfiles"])
    profile_cwds = [call[1]["cwd"] for call in rpc.calls
                    if call[0] == "permissionProfile/list"]
    assert first_root in profile_cwds
    assert str(second_root.resolve()) in profile_cwds


@pytest.fixture
def modern_env(tmp_path, payload):
    parent = tmp_path / "projects"
    parent.mkdir()
    root = parent / "alpha"
    root.mkdir()
    state = tmp_path / "bridge-state"
    cfg = initialize(state, 8765, 8766)
    native_rpc = FakeCodexRpc()
    native = CodexHostAdapter(tmp_path / "codex-state", parent, rpc=native_rpc)
    service = Service(state, cfg, run_coordinator_background=False)
    node, node_record = attach_test_node(service, tmp_path / "node-state", parent)
    create_test_adapter(service, node_record["id"], {
        "name": "Local Codex", "runtime_type": "codex",
        "base_url": "http://127.0.0.1:8767", "token": "codex-test-secret"}, ADAPTER_ID)
    native_client = DirectAdapter(native)
    client_factory = service.adapter_registry.client
    def adapter_client(adapter_id, **kwargs):
        if adapter_id == ADAPTER_ID:
            service.adapter_registry.get(adapter_id,
                                         require_enabled=kwargs.get("require_enabled", False))
            return native_client
        return client_factory(adapter_id, **kwargs)
    service.adapter_registry.client = adapter_client
    ws_id = service.add_workspace("Alpha", str(root), [])["workspace"]["id"]
    token = service.manage_bridge("rotate_token")["token"]
    service.manage_workspace(ws_id, "enable")
    service.manage_workspace(ws_id, "set_agent_enabled", agent_enabled=True)
    service.set_workspace_route(service.workspace(ws_id), ADAPTER_ID, True,
                                "workspace-write-reviewed")
    service.run_coordinator.set_model_policy(ADAPTER_ID, ["gpt-test"], "gpt-test")
    job = service.call(ws_id, token, "prepare_handoff",
                       Handoff.model_validate(payload).model_dump())
    yield service, ws_id, token, job, native, native_rpc
    service.close()
    native.close()
    node["stop"]()


def test_two_pi_instances_start_and_persist_as_distinct_destinations(modern_env, tmp_path):
    service, ws_id, token, job, _, _ = modern_env
    parent = Path(service.workspace(ws_id)["root"]).parent
    local_native = CodexHostAdapter(tmp_path / "pi-local", parent, rpc=FakeCodexRpc())
    gpu_native = CodexHostAdapter(tmp_path / "pi-gpu", parent, rpc=FakeCodexRpc())
    clients = {}
    ids = []
    for name, url, secret, native in (
            ("Local Pi", "http://127.0.0.1:8780", "local-pi-secret", local_native),
            ("GPU Pi", "https://gpu.example:8780", "gpu-pi-secret", gpu_native)):
        row = service.adapter_registry.create({"name": name, "runtime_type": "pi",
                                               "base_url": url, "token": secret})
        ids.append(row["id"])
        clients[row["id"]] = PiDirectAdapter(native)
    old_client = service.adapter_registry.client

    def client_factory(adapter_id, **kwargs):
        if adapter_id in clients:
            service.adapter_registry.get(
                adapter_id, require_enabled=kwargs.get("require_enabled", False))
            return clients[adapter_id]
        return old_client(adapter_id, **kwargs)

    service.adapter_registry.client = client_factory
    ws = service.workspace(ws_id)
    for adapter_id in ids:
        service.run_coordinator.set_model_policy(adapter_id, ["gpt-test"], "gpt-test", ws)
        service.set_workspace_route(ws, adapter_id, True, "read-only")

    request_id = "shared-pi-request"
    local = service.call(ws_id, token, "start_agent_run", {
        "adapter_id": ids[0], "job_id": job["id"], "request_id": request_id})
    gpu = service.call(ws_id, token, "start_agent_run", {
        "adapter_id": ids[1], "job_id": job["id"], "request_id": request_id})
    assert local["adapter_id"] == ids[0] and gpu["adapter_id"] == ids[1]
    assert local["runtime_type"] == gpu["runtime_type"] == "pi"
    assert local["conversation_id"] != gpu["conversation_id"]
    local_retry = service.call(ws_id, token, "start_agent_run", {
        "adapter_id": ids[0], "job_id": job["id"], "request_id": request_id})
    gpu_retry = service.call(ws_id, token, "start_agent_run", {
        "adapter_id": ids[1], "job_id": job["id"], "request_id": request_id})
    assert local_retry["run_id"] == local["run_id"]
    assert local_retry["conversation_id"] == local["conversation_id"]
    assert local_retry["idempotent"] is True
    assert gpu_retry["run_id"] == gpu["run_id"]
    assert gpu_retry["conversation_id"] == gpu["conversation_id"]
    assert gpu_retry["idempotent"] is True
    with service.lock:
        persisted = service.db.execute(
            "SELECT adapter_id,runtime_type FROM agent_runs WHERE workspace=? AND request_id=?",
            (ws_id, request_id)).fetchall()
    assert {(row["adapter_id"], row["runtime_type"]) for row in persisted} == {
        (ids[0], "pi"), (ids[1], "pi")}
    node_service = service._node_transport_services[ws["node_id"]]
    assert node_service.db.execute(
        "SELECT token FROM runtime_adapters WHERE id=?", (ids[0],)).fetchone()["token"] == "local-pi-secret"
    assert node_service.db.execute(
        "SELECT token FROM runtime_adapters WHERE id=?", (ids[1],)).fetchone()["token"] == "gpu-pi-secret"
    assert "token" not in service.adapter_registry.get(ids[0])
    assert "token" not in service.adapter_registry.get(ids[1])
    local_native.close()
    gpu_native.close()


def _finish_test_run(service, native, workspace_id, run_id):
    coordinator = service.run_coordinator
    ws = service.workspace(workspace_id)
    run = coordinator._run_row(ws, run_id)
    conversation = coordinator._conversation(run)
    native_run_id = native.run(run["native_id"])["nativeId"]
    native_thread_id = native.conversation(conversation["native_id"])["nativeId"]
    native._notification("turn/completed", {
        "threadId": native_thread_id,
        "turn": {"id": native_run_id, "status": "completed"}})
    native.rpc.status = "idle"
    return coordinator.read(ws, run_id)


def _record_native_calls(native, monkeypatch):
    calls = []
    for name in ("run", "interactions", "activities", "cancel", "resolve"):
        original = getattr(native, name)

        def wrapped(*args, _name=name, _original=original, **kwargs):
            calls.append(_name)
            return _original(*args, **kwargs)

        monkeypatch.setattr(native, name, wrapped)
    return calls


def _persist_pending_interaction(service, ws_id, token, native, run_id):
    native._request(99, "item/commandExecution/requestApproval", {
        "threadId": "native-thread-1", "turnId": "native-turn-1",
        "reason": "Run tests", "availableDecisions": ["accept", "decline"]})
    viewed = service.call(ws_id, token, "read_agent_run", {"run_id": run_id})
    assert viewed["interactions"]
    return viewed["interactions"][0]["id"]


def test_existing_run_node_revision_drift_blocks_live_control_and_keeps_snapshot(
        modern_env, monkeypatch):
    service, ws_id, token, job, native, _ = modern_env
    ws = service.workspace(ws_id)
    started = service.call(ws_id, token, "start_agent_run", {
        "adapter_id": ADAPTER_ID, "job_id": job["id"], "request_id": "node-drift"})
    interaction_id = _persist_pending_interaction(service, ws_id, token, native,
                                                   started["run_id"])
    calls = _record_native_calls(native, monkeypatch)
    original_node = service.node_registry.get(ws["node_id"])
    service.node_registry.update(ws["node_id"], {
        "base_url": "http://127.0.0.1:9876", "token": "rotated-node-secret"})

    with pytest.raises(BridgeError) as reconcile_error:
        service.run_coordinator.reconcile(ws, started["run_id"])
    assert reconcile_error.value.code == "node_changed"
    snapshot = service.call(ws_id, token, "read_agent_run", {"run_id": started["run_id"]})
    assert snapshot["sync_warning"]["code"] == "node_changed"
    assert snapshot["effective_security"] == started["effective_security"]
    with pytest.raises(BridgeError) as resolve_error:
        service.call(ws_id, token, "respond_agent_interaction", {
            "run_id": started["run_id"], "interaction_id": interaction_id,
            "response": {"choiceId": "accept"}})
    assert resolve_error.value.code == "node_changed"
    with pytest.raises(BridgeError) as cancel_error:
        service.call(ws_id, token, "cancel_agent_run", {"run_id": started["run_id"]})
    assert cancel_error.value.code == "node_changed"
    assert calls == []
    assert service.node_registry.get(ws["node_id"])["revision"] != original_node["revision"]


def test_existing_run_adapter_revision_drift_blocks_live_control_and_keeps_snapshot(
        modern_env, monkeypatch):
    service, ws_id, token, job, native, _ = modern_env
    ws = service.workspace(ws_id)
    started = service.call(ws_id, token, "start_agent_run", {
        "adapter_id": ADAPTER_ID, "job_id": job["id"], "request_id": "adapter-drift"})
    interaction_id = _persist_pending_interaction(service, ws_id, token, native,
                                                   started["run_id"])
    calls = _record_native_calls(native, monkeypatch)
    original_adapter = service.adapter_registry.get(ADAPTER_ID)
    service.adapter_registry.update(ADAPTER_ID, {
        "base_url": "http://127.0.0.1:9877", "token": "rotated-adapter-secret"})

    with pytest.raises(BridgeError) as reconcile_error:
        service.run_coordinator.reconcile(ws, started["run_id"])
    assert reconcile_error.value.code == "adapter_changed"
    snapshot = service.call(ws_id, token, "read_agent_run", {"run_id": started["run_id"]})
    assert snapshot["sync_warning"]["code"] == "adapter_changed"
    assert snapshot["effective_security"] == started["effective_security"]
    with pytest.raises(BridgeError) as resolve_error:
        service.call(ws_id, token, "respond_agent_interaction", {
            "run_id": started["run_id"], "interaction_id": interaction_id,
            "response": {"choiceId": "accept"}})
    assert resolve_error.value.code == "adapter_changed"
    with pytest.raises(BridgeError) as cancel_error:
        service.call(ws_id, token, "cancel_agent_run", {"run_id": started["run_id"]})
    assert cancel_error.value.code == "adapter_changed"
    assert calls == []
    assert service.adapter_registry.get(ADAPTER_ID)["revision"] != original_adapter["revision"]


def test_node_adapter_revision_race_blocks_existing_run_before_any_runtime_call(
        tmp_path, payload, monkeypatch):
    parent = tmp_path / "projects"
    parent.mkdir()
    root = parent / "alpha"
    root.mkdir()
    state = tmp_path / "bridge-state"
    cfg = initialize(state, 8765, 8766)
    old_native = CodexHostAdapter(tmp_path / "old-codex-state", parent,
                                  rpc=FakeCodexRpc())
    replacement_native = CodexHostAdapter(tmp_path / "replacement-codex-state", parent,
                                          rpc=FakeCodexRpc())
    old_url = "http://old-runtime.example:8780"
    replacement_url = "http://replacement-runtime.example:8780"
    old_calls, replacement_calls, constructed = [], [], []
    old_adapter = DirectAdapter(old_native)
    replacement_adapter = DirectAdapter(replacement_native)

    class RoutedRuntime:
        def __init__(self, adapter_id, runtime_type, base_url, token, *, timeout=30):
            constructed.append((base_url, token))
            targets = {
                old_url: (old_adapter, old_calls),
                replacement_url: (replacement_adapter, replacement_calls),
            }
            try:
                self._target, self._calls = targets[base_url]
            except KeyError:
                raise AssertionError(f"unexpected runtime target: {base_url}") from None

        def __getattr__(self, name):
            value = getattr(self._target, name)
            if not callable(value):
                return value

            def call(*args, **kwargs):
                self._calls.append(name)
                return value(*args, **kwargs)

            return call

    monkeypatch.setattr("workspace_bridge.node_service.HttpRuntimeAdapter", RoutedRuntime)
    service = Service(state, cfg, run_coordinator_background=False)
    node, node_record = attach_test_node(service, tmp_path / "node-state", parent)
    create_test_adapter(service, node_record["id"], {
        "name": "Authoritative Codex", "runtime_type": "codex",
        "base_url": old_url, "token": "old-adapter-secret"}, ADAPTER_ID)
    try:
        ws_id = service.add_workspace("Alpha", str(root), [], node_record["id"])["workspace"]["id"]
        token = service.manage_bridge("rotate_token")["token"]
        service.manage_workspace(ws_id, "enable")
        service.manage_workspace(ws_id, "set_agent_enabled", agent_enabled=True)
        ws = service.workspace(ws_id)
        service.set_workspace_route(ws, ADAPTER_ID, True, "workspace-write-reviewed")
        service.run_coordinator.set_model_policy(ADAPTER_ID, ["gpt-test"], "gpt-test", ws)
        job = service.call(ws_id, token, "prepare_handoff",
                           Handoff.model_validate(payload).model_dump())
        started = service.call(ws_id, token, "start_agent_run", {
            "adapter_id": ADAPTER_ID, "job_id": job["id"],
            "request_id": "node-adapter-race"})
        interaction_id = _persist_pending_interaction(
            service, ws_id, token, old_native, started["run_id"])
        old_calls.clear()
        replacement_calls.clear()
        constructed.clear()

        run = service.run_coordinator._run_row(ws, started["run_id"])
        old_revision = run["adapter_revision"]
        updated = service._node_transport_services[node_record["id"]].save_adapter({
            "name": "Authoritative Codex", "runtime_type": "codex",
            "base_url": replacement_url, "token": "replacement-adapter-secret",
        }, ADAPTER_ID)
        assert updated["revision"] != old_revision
        assert service.adapter_registry.get(ADAPTER_ID)["revision"] == old_revision

        with pytest.raises(BridgeError) as reconcile_error:
            service.run_coordinator.reconcile(ws, started["run_id"])
        assert reconcile_error.value.code == "adapter_changed"
        snapshot = service.call(ws_id, token, "read_agent_run",
                                {"run_id": started["run_id"]})
        assert snapshot["sync_warning"]["code"] == "adapter_changed"
        assert snapshot["effective_security"] == started["effective_security"]
        with pytest.raises(BridgeError) as resolve_error:
            service.call(ws_id, token, "respond_agent_interaction", {
                "run_id": started["run_id"], "interaction_id": interaction_id,
                "response": {"choiceId": "accept"}})
        assert resolve_error.value.code == "adapter_changed"
        with pytest.raises(BridgeError) as cancel_error:
            service.call(ws_id, token, "cancel_agent_run",
                         {"run_id": started["run_id"]})
        assert cancel_error.value.code == "adapter_changed"
        assert old_calls == replacement_calls == constructed == []

        # Finish the old target directly so the fresh-run assertion can use a
        # separate conversation; this is test setup after the guarded calls.
        old_run_id = old_native.run(run["native_id"])["nativeId"]
        old_thread_id = old_native.conversation(
            service.run_coordinator._conversation(run)["native_id"])["nativeId"]
        old_native._notification("turn/completed", {
            "threadId": old_thread_id,
            "turn": {"id": old_run_id, "status": "completed"}})
        old_native.rpc.status = "idle"
        service.run_coordinator._persist_snapshot(
            run, old_native.run(run["native_id"]), [], [])

        # Refreshing the Bridge inventory permits a new run on the new
        # revision, while the old conversation remains permanently pinned.
        service.node_registry.refresh_adapters(node_record["id"])
        fresh = service.call(ws_id, token, "start_agent_run", {
            "adapter_id": ADAPTER_ID, "job_id": job["id"],
            "request_id": "node-adapter-race-fresh"})
        assert fresh["effective_security"]["adapter_revision"] == updated["revision"]
        assert "start_run" in replacement_calls
        with pytest.raises(BridgeError) as continuation_error:
            service.call(ws_id, token, "start_agent_run", {
                "adapter_id": ADAPTER_ID, "job_id": job["id"],
                "request_id": "node-adapter-race-continuation",
                "continue_from_run_id": started["run_id"]})
        assert continuation_error.value.code == "adapter_changed"
    finally:
        service.close()
        node["stop"]()
        old_native.close()
        replacement_native.close()


def test_adapter_revision_blocks_explicit_continuation_but_rename_does_not(modern_env):
    service, ws_id, token, job, native, _ = modern_env
    first = service.call(ws_id, token, "start_agent_run", {
        "adapter_id": ADAPTER_ID, "job_id": job["id"], "request_id": "revision-first"})
    _finish_test_run(service, native, ws_id, first["run_id"])
    original = service.adapter_registry.get(ADAPTER_ID)
    renamed = service.adapter_registry.update(ADAPTER_ID, {"name": "Renamed Codex"})
    assert renamed["revision"] == original["revision"]

    second = service.call(ws_id, token, "start_agent_run", {
        "adapter_id": ADAPTER_ID, "job_id": job["id"], "request_id": "revision-rename-continuation",
        "continue_from_run_id": first["run_id"]})
    assert second["conversation_id"] == first["conversation_id"]
    _finish_test_run(service, native, ws_id, second["run_id"])
    changed = service.adapter_registry.update(ADAPTER_ID, {
        "base_url": "http://127.0.0.1:9877", "token": "replacement-codex-secret"})
    assert changed["revision"] != original["revision"]
    with pytest.raises(BridgeError) as exc:
        service.call(ws_id, token, "start_agent_run", {
            "adapter_id": ADAPTER_ID, "job_id": job["id"], "request_id": "revision-reject",
            "continue_from_run_id": second["run_id"]})
    assert exc.value.code == "adapter_changed"


def test_wrong_node_route_and_start_fail_before_native_call(modern_env, tmp_path):
    service, ws_id, token, job, native, _ = modern_env
    ws = service.workspace(ws_id)
    node_b = start_test_node(tmp_path / "other-node-state", Path(ws["root"]).parent)
    try:
        node_b_record = service.node_registry.create({
            "name": "Other Node", "base_url": node_b["url"],
            "token": node_b["token"], "enabled": False,
        })
        service._node_transport[node_b_record["id"]] = node_b["transport"]
        service._node_transport_services[node_b_record["id"]] = node_b["service"]
        service.node_registry.update(node_b_record["id"], {"enabled": True})
        wrong_adapter = create_test_adapter(service, node_b_record["id"], {
            "name": "Other Node Pi", "runtime_type": "pi",
            "base_url": "http://127.0.0.1:8789", "token": "other-secret",
        }, "adapter_555555555555555555555555")

        native_calls = []
        original_start = native.start_run
        native.start_run = lambda *args, **kwargs: (
            native_calls.append((args, kwargs)), original_start(*args, **kwargs))[1]
        with pytest.raises(BridgeError) as route_error:
            service.set_workspace_route(ws, wrong_adapter["id"], True, "read-only")
        assert route_error.value.code == "adapter_node_mismatch"
        with pytest.raises(BridgeError) as start_error:
            service.call(ws_id, token, "start_agent_run", {
                "adapter_id": wrong_adapter["id"], "job_id": job["id"],
                "request_id": "wrong-node-start"})
        assert start_error.value.code == "adapter_node_mismatch"
        assert native_calls == []
    finally:
        node_b["stop"]()


def test_bridge_start_reconcile_interaction_and_activity(modern_env):
    service, ws_id, token, job, native, rpc = modern_env
    recorder = RecordingChannel()
    manager = attach_notification_channel(service, recorder)
    started = service.call(ws_id, token, "start_agent_run", {
        "adapter_id": ADAPTER_ID, "job_id": job["id"], "request_id": "modern-1"})
    bridge_id = started["run_id"]
    assert started["phase"] == "active" and started["runtime_type"] == "codex"
    assert started["adapter_id"] == ADAPTER_ID
    repeated = service.call(ws_id, token, "start_agent_run", {
        "adapter_id": ADAPTER_ID, "job_id": job["id"], "request_id": "modern-1"})
    assert repeated["run_id"] == bridge_id and repeated["idempotent"] is True
    with pytest.raises(BridgeError) as conflict:
        service.call(ws_id, token, "start_agent_run", {
            "adapter_id": ADAPTER_ID, "job_id": job["id"], "request_id": "modern-1",
            "model": "different-model"})
    assert conflict.value.code == "conflict"
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
        "adapter_id": ADAPTER_ID, "job_id": job["id"], "request_id": "outcome-" + outcome})
    coordinator = service.run_coordinator
    ws = service.workspace(ws_id)
    run = coordinator._run_row(ws, started["run_id"])
    snapshot = coordinator.adapter(ADAPTER_ID).run(run["native_id"])
    terminal = {**snapshot, "phase": "terminal", "activeState": None,
                "outcome": outcome, "result": "bounded result"}
    coordinator._persist_snapshot(run, terminal, [], [])
    coordinator._persist_snapshot(run, terminal, [], [])
    wait_for_notifications(manager)
    assert [event.event_type for event in recorder.calls] == [event_type]
    assert recorder.calls[0].node_id.startswith("node_")
    assert recorder.calls[0].node_name == "Local Node"


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
        "adapter_id": ADAPTER_ID, "job_id": job["id"], "request_id": "slow-notification"})
    coordinator = service.run_coordinator
    ws = service.workspace(ws_id)
    run = coordinator._run_row(ws, started["run_id"])
    snapshot = coordinator.adapter(ADAPTER_ID).run(run["native_id"])
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
        "adapter_id": ADAPTER_ID, "job_id": job["id"], "request_id": "interaction-dedupe"})
    coordinator = service.run_coordinator
    ws = service.workspace(ws_id)
    run = coordinator._run_row(ws, started["run_id"])
    snapshot = coordinator.adapter(ADAPTER_ID).run(run["native_id"])
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
            "UPDATE agent_interactions SET state='resolved' WHERE run=? AND native_id='native-a'",
            (run["id"],))
    coordinator._persist_snapshot(run, snapshot, interactions, [])
    assert len(recorder.calls) == 2
    with service.lock:
        raw = json.dumps([dict(row) for row in service.db.execute(
            "SELECT * FROM notification_events WHERE run_id=?", (run["id"],))])
        state = service.db.execute(
            "SELECT state FROM agent_interactions WHERE run=? AND native_id='native-a'",
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
            "adapter_id": ADAPTER_ID, "job_id": job["id"], "request_id": "reject-notification"})
    wait_for_notifications(manager)
    with service.lock:
        row = service.db.execute(
            "SELECT id,outcome FROM agent_runs WHERE request_id='reject-notification'").fetchone()
    assert row["outcome"] == "failed"
    assert [event.event_type for event in recorder.calls] == ["run_failed"]


def test_workspace_route_defaults_off_for_new_workspace(tmp_path):
    parent = tmp_path / "projects"
    parent.mkdir()
    root = parent / "alpha"
    root.mkdir()
    state = tmp_path / "bridge-state"
    cfg = initialize(state, 8765, 8766)
    native = CodexHostAdapter(tmp_path / "codex-state", parent, rpc=FakeCodexRpc())
    service = Service(state, cfg, run_coordinator_background=False)
    node, node_record = attach_test_node(service, tmp_path / "node-state", parent)
    create_test_adapter(service, node_record["id"], {
        "name": "Local Codex", "runtime_type": "codex",
        "base_url": "http://127.0.0.1:8767", "token": "codex-test-secret"}, ADAPTER_ID)
    direct = DirectAdapter(native)
    factory = service.adapter_registry.client
    service.adapter_registry.client = lambda adapter_id, **kwargs: (
        direct if adapter_id == ADAPTER_ID else factory(adapter_id, **kwargs))
    try:
        ws_id = service.add_workspace("Alpha", str(root), [])["workspace"]["id"]
        service.manage_workspace(ws_id, "enable")
        service.manage_workspace(ws_id, "set_agent_enabled", agent_enabled=True)
        service.adapter_registry.get(ADAPTER_ID)
        assert service.workspace_route_policy(service.workspace(ws_id))["routes"] == {}
        with pytest.raises(BridgeError) as exc:
            service.require_workspace_route(service.workspace(ws_id), ADAPTER_ID)
        assert exc.value.code == "route_disabled"
    finally:
        service.close()
        native.close()
        node["stop"]()


def test_unconfirmed_bridge_run_rebinds_by_client_run_id(modern_env):
    service, ws_id, token, job, native, _ = modern_env
    started = service.call(ws_id, token, "start_agent_run", {
        "adapter_id": ADAPTER_ID, "job_id": job["id"], "request_id": "rebind-1"})
    bridge_id = started["run_id"]
    with service.lock, service.db:
        service.db.execute(
            "UPDATE agent_runs SET native_id=NULL,phase='starting',active_state=NULL "
            "WHERE id=?", (bridge_id,))
    recovered = service.call(ws_id, token, "read_agent_run", {"run_id": bridge_id})
    assert recovered["phase"] == "active"
    assert recovered["run_id"] == bridge_id
