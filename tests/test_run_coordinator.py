"""One coordinator drives a conforming adapter without native runtime types."""
from __future__ import annotations
from admin_helpers import admin_cookie

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
from workspace_bridge import security_profiles as sp
from workspace_bridge.security import BridgeError
from workspace_bridge.service import Service
from workspace_bridge.wbrp import Descriptor

from test_codex_host_adapter import FakeCodexRpc
from notification_fakes import RecordingChannel
from conftest import attach_test_node, create_test_adapter, register_test_node, start_test_node, patch_node_runtime

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

    def steer(self, ident, input_items, request_id):
        return self.native.steer(ident, {"expectedRunId": ident, "input": input_items,
                                         "requestId": request_id})

    def queue(self, ident, input_items, request_id):
        return self.native.queue(ident, {"expectedRunId": ident, "input": input_items,
                                         "requestId": request_id})


class PiDirectAdapter(DirectAdapter):
    """Use the scripted native host contract to exercise a Pi family identity."""
    def descriptor(self):
        value = self.native.descriptor()
        value["runtime"]["id"] = "pi"
        return Descriptor.parse(value, expected_runtime="pi")


def test_profile_rules_travel_with_every_run_and_reach_continuations(modern_env, payload):
    service, ws_id, token, first_job, native, rpc = modern_env
    ws = service.workspace(ws_id)
    first = service.call(ws_id, token, "start_run", {
        "adapter_id": ADAPTER_ID, "job_id": first_job["id"], "request_id": "rules-first"})
    trusted = sp.BUILTINS["trusted"]
    assert first["effective_security"]["source"] == "profile"
    assert first["effective_security"]["profile_id"] == "trusted"
    assert first["effective_security"]["profile_revision"] == sp.revision(trusted)
    # The run's rules add read access to its handoff's plan files.
    folder = first["effective_security"]["plan_folder"]
    assert first["effective_security"]["rules"]["external_roots"] == [{"path": folder, "read": "allow"}]
    assert first["effective_security"]["revision"] == sp.revision(
        {**trusted, "external_roots": [{"path": folder, "read": "allow"}]})
    start = next(params for method, params in rpc.calls if method == "thread/start")
    assert start["permissions"] == ":read-only"
    _finish_test_run(service, native, ws_id, first["run_id"])
    # Editing the profile reaches the next continuation on the same thread.
    rules = {**trusted, "commands": [{"prefix": "git push", "mode": "deny"}]}
    saved = service.profiles.save("team", rules)
    service.set_workspace_route(ws, ADAPTER_ID, True, "team")
    second_job = _second_job(service, ws_id, token, payload, "rules-second-job")
    threads = sum(method == "thread/start" for method, _ in rpc.calls)
    second = service.call(ws_id, token, "start_run", {
        "adapter_id": ADAPTER_ID, "job_id": second_job["id"], "request_id": "rules-second",
        "continue_from_run_id": first["run_id"]})
    assert second["conversation_id"] == first["conversation_id"]
    assert second["effective_security"]["profile_id"] == "team"
    assert second["effective_security"]["profile_revision"] == saved["revision"]
    assert sum(method == "thread/start" for method, _ in rpc.calls) == threads
    # The first run keeps the security it started with.
    assert service.run_coordinator.read(ws, first["run_id"])["effective_security"][
        "profile_id"] == "trusted"


def test_native_route_runs_without_rules_and_cannot_continue_a_profile_thread(
        modern_env, payload, monkeypatch):
    service, ws_id, token, first_job, native, rpc = modern_env
    ws = service.workspace(ws_id)
    first = service.call(ws_id, token, "start_run", {
        "adapter_id": ADAPTER_ID, "job_id": first_job["id"], "request_id": "native-first"})
    _finish_test_run(service, native, ws_id, first["run_id"])
    service.set_workspace_route(ws, ADAPTER_ID, True, security_source="native")
    second_job = _second_job(service, ws_id, token, payload, "native-second-job")
    client = service.run_coordinator.adapter(ADAPTER_ID)
    monkeypatch.setattr(client, "start_run", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("native start must not run")))
    before = _run_counts(service, ws_id)
    with pytest.raises(BridgeError) as exc:
        service.call(ws_id, token, "start_run", {
            "adapter_id": ADAPTER_ID, "job_id": second_job["id"],
            "request_id": "native-continue", "continue_from_run_id": first["run_id"]})
    assert exc.value.code == "security_source_changed"
    assert _run_counts(service, ws_id) == before
    monkeypatch.undo()
    fresh = service.call(ws_id, token, "start_run", {
        "adapter_id": ADAPTER_ID, "job_id": second_job["id"], "request_id": "native-fresh"})
    assert fresh["conversation_id"] != first["conversation_id"]
    assert fresh["effective_security"]["source"] == "native"
    assert fresh["effective_security"]["revision"] is None
    start = [params for method, params in rpc.calls if method == "thread/start"][-1]
    assert "permissions" not in start


def test_workspace_default_is_explicit_and_reported_per_adapter(modern_env):
    service, ws_id, _, _, _, _ = modern_env
    ws = service.workspace(ws_id)
    unbound_id = "adapter_000000000000000000000003"
    create_test_adapter(service, ws["node_id"], {
        "name": "Unbound Codex", "plugin": "codex",
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


def test_diagnostics_ignores_profile_revision_drift(modern_env):
    service, ws_id, _, _, _, rpc = modern_env
    rpc.requirements = {
        "allowedApprovalPolicies": ["on-request", "untrusted"],
        "allowedApprovalsReviewers": ["user"],
        "allowedPermissionProfiles": {":workspace": True},
    }
    report = service.diagnostic_report()
    route = next(item for item in report["runnable_routes"]
                 if item["workspace_id"] == ws_id and item["adapter_id"] == ADAPTER_ID)
    assert route["ready"] is True
    assert route["blockers"] == []
    assert "allowedApprovalPolicies" not in json.dumps(report)


@pytest.mark.asyncio
async def test_admin_prepared_handoff_start_is_strict_and_idempotent(modern_env):
    service, ws_id, _, job, _, _ = modern_env
    app = make_admin(service)
    token = admin_cookie(app)
    headers = {"Cookie": token}
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
async def test_profile_manager_routes_require_admin(modern_env):
    service, _, _, _, _, _ = modern_env
    app = make_admin(service)
    rules = {**sp.BUILTINS["trusted"], "protected": ["secrets/"]}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://127.0.0.1:8766") as client:
        assert (await client.put("/api/profiles/custom", json={
            "rules": rules})).status_code == 401
        headers = {"Cookie": admin_cookie(app)}
        created = await client.put("/api/profiles/custom", json={"rules": rules},
                                   headers=headers)
        assert created.status_code == 200, created.text
        assert created.json()["rules"]["protected"] == ["secrets/"]
        listed = await client.get("/api/profiles", headers=headers)
        assert [row["id"] for row in listed.json()["profiles"]] == [
            "read-only", "reviewed", "trusted", "custom"]
        status = await client.get("/api/status", headers=headers)
        assert status.status_code == 200
        assert status.json()["notifications"]["channels"] == {}
        assert "runtimes" not in status.json()
        deleted = await client.delete("/api/profiles/custom", headers=headers)
        assert deleted.status_code == 200


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
        "name": "Local Codex", "plugin": "codex"}, ADAPTER_ID)
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
    service.set_workspace_route(service.workspace(ws_id), ADAPTER_ID, True, "trusted")
    service.run_coordinator.set_model_policy(ADAPTER_ID, ["gpt-test"], "gpt-test")
    job = service.call(ws_id, token, "save_handoff",
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
        row = service.adapter_registry.create({"name": name, "plugin": "pi"})
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
    local = service.call(ws_id, token, "start_run", {
        "adapter_id": ids[0], "job_id": job["id"], "request_id": request_id})
    gpu = service.call(ws_id, token, "start_run", {
        "adapter_id": ids[1], "job_id": job["id"], "request_id": request_id})
    assert local["adapter_id"] == ids[0] and gpu["adapter_id"] == ids[1]
    assert local["runtime_type"] == gpu["runtime_type"] == "pi"
    assert local["conversation_id"] != gpu["conversation_id"]
    local_retry = service.call(ws_id, token, "start_run", {
        "adapter_id": ids[0], "job_id": job["id"], "request_id": request_id})
    gpu_retry = service.call(ws_id, token, "start_run", {
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
    for adapter_id in ids:
        stored = node_service.db.execute(
            "SELECT kind,token FROM runtime_adapters WHERE id=?", (adapter_id,)).fetchone()
        assert (stored["kind"], stored["token"]) == ("plugin", "")
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
    native._request(99, "item/fileChange/requestApproval", {
        "threadId": "native-thread-1", "turnId": "native-turn-1",
        "reason": "Run tests", "availableDecisions": ["accept", "decline"]})
    viewed = service.call(ws_id, token, "read_run", {"run_id": run_id})
    assert viewed["interactions"]
    return viewed["interactions"][0]["id"]


def test_existing_run_node_revision_drift_interrupts_run_without_runtime_calls(
        modern_env, monkeypatch):
    service, ws_id, token, job, native, _ = modern_env
    ws = service.workspace(ws_id)
    started = service.call(ws_id, token, "start_run", {
        "adapter_id": ADAPTER_ID, "job_id": job["id"], "request_id": "node-drift"})
    interaction_id = _persist_pending_interaction(service, ws_id, token, native,
                                                   started["run_id"])
    calls = _record_native_calls(native, monkeypatch)
    original_node = service.node_registry.get(ws["node_id"])
    # Re-pairing gives the Node a new connection revision.
    with service.lock, service.db:
        service.db.execute("UPDATE nodes SET revision=? WHERE id=?",
                           ("rev_" + "9" * 32, ws["node_id"]))

    with pytest.raises(BridgeError) as reconcile_error:
        service.run_coordinator.reconcile(ws, started["run_id"])
    assert reconcile_error.value.code == "node_changed"
    snapshot = service.call(ws_id, token, "read_run", {"run_id": started["run_id"]})
    assert snapshot["sync_warning"]["code"] == "node_changed"
    assert "interrupted" in snapshot["sync_warning"]["message"]
    assert snapshot["effective_security"] == started["effective_security"]
    # The run can never be controlled again, so it stops occupying its
    # conversation; its pending interaction is stale rather than answerable.
    assert (snapshot["phase"], snapshot["outcome"]) == ("terminal", "interrupted")
    assert snapshot["error"] == "runtime_authority_changed"
    assert [item["state"] for item in snapshot["interactions"]] == ["stale"]
    with pytest.raises(BridgeError) as resolve_error:
        service.call(ws_id, token, "answer_interaction", {
            "run_id": started["run_id"], "interaction_id": interaction_id,
            "choice_id": "accept",
            "decided_by": "chatgpt", "reason": "Runs the tests"})
    assert resolve_error.value.code == "interaction_stale"
    cancelled = service.call(ws_id, token, "cancel_run", {"run_id": started["run_id"]})
    assert cancelled["outcome"] == "interrupted"
    assert calls == []
    assert service.node_registry.get(ws["node_id"])["revision"] != original_node["revision"]


def test_existing_run_adapter_revision_drift_interrupts_run_without_runtime_calls(
        modern_env, monkeypatch):
    service, ws_id, token, job, native, _ = modern_env
    ws = service.workspace(ws_id)
    started = service.call(ws_id, token, "start_run", {
        "adapter_id": ADAPTER_ID, "job_id": job["id"], "request_id": "adapter-drift"})
    interaction_id = _persist_pending_interaction(service, ws_id, token, native,
                                                   started["run_id"])
    calls = _record_native_calls(native, monkeypatch)
    original_adapter = service.adapter_registry.get(ADAPTER_ID)
    service.adapter_registry.update(ADAPTER_ID, {"settings": {"log_level": "DEBUG"}})

    with pytest.raises(BridgeError) as reconcile_error:
        service.run_coordinator.reconcile(ws, started["run_id"])
    assert reconcile_error.value.code == "adapter_changed"
    snapshot = service.call(ws_id, token, "read_run", {"run_id": started["run_id"]})
    assert snapshot["sync_warning"]["code"] == "adapter_changed"
    assert "interrupted" in snapshot["sync_warning"]["message"]
    assert snapshot["effective_security"] == started["effective_security"]
    # The run can never be controlled again, so it stops occupying its
    # conversation; its pending interaction is stale rather than answerable.
    assert (snapshot["phase"], snapshot["outcome"]) == ("terminal", "interrupted")
    assert snapshot["error"] == "runtime_authority_changed"
    assert [item["state"] for item in snapshot["interactions"]] == ["stale"]
    with pytest.raises(BridgeError) as resolve_error:
        service.call(ws_id, token, "answer_interaction", {
            "run_id": started["run_id"], "interaction_id": interaction_id,
            "choice_id": "accept",
            "decided_by": "chatgpt", "reason": "Runs the tests"})
    assert resolve_error.value.code == "interaction_stale"
    cancelled = service.call(ws_id, token, "cancel_run", {"run_id": started["run_id"]})
    assert cancelled["outcome"] == "interrupted"
    assert calls == []
    assert service.adapter_registry.get(ADAPTER_ID)["revision"] != original_adapter["revision"]


def _one_sweep(coordinator, monkeypatch):
    """Run exactly one background sweep iteration in the test thread."""
    class OneShot:
        def clear(self):
            pass

        def set(self):
            pass

        def wait(self, _timeout):
            coordinator._stop.set()

    monkeypatch.setattr(coordinator, "_wake", OneShot())
    coordinator._stop.clear()
    coordinator._sweep()
    coordinator._stop.clear()


def test_sweep_interrupts_runs_stranded_by_an_adapter_connection_change(
        modern_env, monkeypatch):
    service, ws_id, token, job, native, _ = modern_env
    started = service.call(ws_id, token, "start_run", {
        "adapter_id": ADAPTER_ID, "job_id": job["id"], "request_id": "stranded"})
    calls = _record_native_calls(native, monkeypatch)
    service.adapter_registry.update(ADAPTER_ID, {"settings": {"log_level": "DEBUG"}})

    _one_sweep(service.run_coordinator, monkeypatch)

    run = service.run_coordinator._run_row(service.workspace(ws_id), started["run_id"])
    assert (run["phase"], run["outcome"], run["error"]) == (
        "terminal", "interrupted", "runtime_authority_changed")
    with service.lock:
        events = [row["event_type"] for row in service.db.execute(
            "SELECT event_type FROM notification_events WHERE run_id=?", (run["id"],))]
    assert events == ["run_interrupted"]
    assert service.run_coordinator.interrupt_stranded_runs() == []
    assert calls == []


def test_disabled_adapter_keeps_runs_recoverable_and_out_of_the_sweep(
        modern_env, monkeypatch):
    service, ws_id, token, job, _, _ = modern_env
    started = service.call(ws_id, token, "start_run", {
        "adapter_id": ADAPTER_ID, "job_id": job["id"], "request_id": "disabled-adapter"})
    coordinator = service.run_coordinator
    reconciled = []
    monkeypatch.setattr(coordinator, "reconcile",
                        lambda ws, run_id: reconciled.append(run_id))

    service.adapter_registry.update(ADAPTER_ID, {"enabled": False})
    _one_sweep(coordinator, monkeypatch)
    run = coordinator._run_row(service.workspace(ws_id), started["run_id"])
    assert run["phase"] == "active"
    assert reconciled == []

    service.adapter_registry.update(ADAPTER_ID, {"enabled": True})
    _one_sweep(coordinator, monkeypatch)
    assert reconciled == [started["run_id"]]


@pytest.mark.asyncio
async def test_admin_abandon_marks_run_orphaned_without_runtime_calls(
        modern_env, monkeypatch):
    service, ws_id, token, job, native, _ = modern_env
    started = service.call(ws_id, token, "start_run", {
        "adapter_id": ADAPTER_ID, "job_id": job["id"], "request_id": "abandon-me"})
    interaction_id = _persist_pending_interaction(service, ws_id, token, native,
                                                   started["run_id"])
    calls = _record_native_calls(native, monkeypatch)
    app = make_admin(service)
    headers = {"Cookie": admin_cookie(app)}
    path = f"/api/runs/{started['run_id']}/abandon"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://127.0.0.1:8766") as client:
        assert (await client.post(path)).status_code == 401
        first = await client.post(path, headers=headers)
        assert first.status_code == 200, first.text
        repeated = await client.post(path, headers=headers)
        assert repeated.status_code == 200, repeated.text
    body = first.json()
    assert (body["phase"], body["outcome"], body["error"]) == (
        "terminal", "orphaned", "abandoned_by_admin")
    assert [item["state"] for item in body["interactions"]] == ["stale"]
    assert repeated.json()["outcome"] == "orphaned"
    assert calls == []
    with pytest.raises(BridgeError) as resolve_error:
        service.call(ws_id, token, "answer_interaction", {
            "run_id": started["run_id"], "interaction_id": interaction_id,
            "choice_id": "accept",
            "decided_by": "chatgpt", "reason": "Runs the tests"})
    assert resolve_error.value.code == "interaction_stale"


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
    # The adapter's configuration selects which runtime answers: the
    # default (old) or the one saved after a settings change (replacement).
    old_url = "INFO"
    replacement_url = "DEBUG"
    old_calls, replacement_calls, constructed = [], [], []
    old_adapter = DirectAdapter(old_native)
    replacement_adapter = DirectAdapter(replacement_native)

    class RoutedRuntime:
        def __init__(self, adapter_id, runtime_type, *, timeout=30, row=None):
            base_url = json.loads(row["settings"]).get("log_level", "INFO")
            constructed.append(base_url)
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

    patch_node_runtime(monkeypatch, RoutedRuntime)
    service = Service(state, cfg, run_coordinator_background=False)
    node, node_record = attach_test_node(service, tmp_path / "node-state", parent)
    create_test_adapter(service, node_record["id"], {
        "name": "Authoritative Codex", "plugin": "codex"}, ADAPTER_ID)
    try:
        ws_id = service.add_workspace("Alpha", str(root), [], node_record["id"])["workspace"]["id"]
        token = service.manage_bridge("rotate_token")["token"]
        service.manage_workspace(ws_id, "enable")
        service.manage_workspace(ws_id, "set_agent_enabled", agent_enabled=True)
        ws = service.workspace(ws_id)
        service.set_workspace_route(ws, ADAPTER_ID, True, "trusted")
        service.run_coordinator.set_model_policy(ADAPTER_ID, ["gpt-test"], "gpt-test", ws)
        job = service.call(ws_id, token, "save_handoff",
                           Handoff.model_validate(payload).model_dump())
        started = service.call(ws_id, token, "start_run", {
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
            "settings": {"log_level": "DEBUG"}}, ADAPTER_ID)
        assert updated["revision"] != old_revision
        assert service.adapter_registry.get(ADAPTER_ID)["revision"] == old_revision

        with pytest.raises(BridgeError) as reconcile_error:
            service.run_coordinator.reconcile(ws, started["run_id"])
        assert reconcile_error.value.code == "adapter_changed"
        snapshot = service.call(ws_id, token, "read_run",
                                {"run_id": started["run_id"]})
        assert snapshot["sync_warning"]["code"] == "adapter_changed"
        assert snapshot["effective_security"] == started["effective_security"]
        with pytest.raises(BridgeError) as resolve_error:
            service.call(ws_id, token, "answer_interaction", {
                "run_id": started["run_id"], "interaction_id": interaction_id,
                "choice_id": "accept",
            "decided_by": "chatgpt", "reason": "Runs the tests"})
        assert resolve_error.value.code == "adapter_changed"
        with pytest.raises(BridgeError) as cancel_error:
            service.call(ws_id, token, "cancel_run",
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
        fresh = service.call(ws_id, token, "start_run", {
            "adapter_id": ADAPTER_ID, "job_id": job["id"],
            "request_id": "node-adapter-race-fresh"})
        assert fresh["effective_security"]["adapter_revision"] == updated["revision"]
        assert "start_run" in replacement_calls
        replacement_calls.clear()
        before = _run_counts(service, ws_id)
        # Continuation is proven live now: the stored native conversation is
        # missing on the replacement adapter, so the ownership proof fails
        # closed before any prompt and no native start runs.
        with pytest.raises(BridgeError) as continuation_error:
            service.call(ws_id, token, "start_run", {
                "adapter_id": ADAPTER_ID, "job_id": job["id"],
                "request_id": "node-adapter-race-continuation",
                "continue_from_run_id": started["run_id"]})
        assert "start_run" not in replacement_calls
        assert _run_counts(service, ws_id) == before
    finally:
        service.close()
        node["stop"]()
        old_native.close()
        replacement_native.close()


def test_continuation_ignores_benign_revision_churn_and_rename_keeps_conversation(modern_env):
    service, ws_id, token, job, native, _ = modern_env
    ws = service.workspace(ws_id)
    first = service.call(ws_id, token, "start_run", {
        "adapter_id": ADAPTER_ID, "job_id": job["id"], "request_id": "revision-first"})
    _finish_test_run(service, native, ws_id, first["run_id"])
    original = service.adapter_registry.get(ADAPTER_ID)
    first_run = service.run_coordinator._run_row(ws, first["run_id"])
    first_effective = first_run["effective_security"]
    # A display rename rotates no revision and never invalidates anything.
    renamed = service.adapter_registry.update(ADAPTER_ID, {"name": "Renamed Codex"})
    assert renamed["revision"] == original["revision"]

    second = service.call(ws_id, token, "start_run", {
        "adapter_id": ADAPTER_ID, "job_id": job["id"], "request_id": "revision-rename-continuation",
        "continue_from_run_id": first["run_id"]})
    assert second["conversation_id"] == first["conversation_id"]
    _finish_test_run(service, native, ws_id, second["run_id"])
    # Benign revision churn alone (Node and adapter revisions rotate while
    # the same live adapter still owns the idle conversation) no longer
    # invalidates the conversation. The conversation metadata is refreshed;
    # historical run evidence stays immutable.
    node_row = service.node_registry.get(ws["node_id"])
    service._node_transport_services[ws["node_id"]].save_adapter({
        "settings": {"log_level": "DEBUG"}}, ADAPTER_ID)
    with service.lock, service.db:
        service.db.execute("UPDATE nodes SET revision=? WHERE id=?",
                           (node_row["revision"] + "-drifted", ws["node_id"]))
    service.node_registry.refresh_adapters(ws["node_id"])
    node_info = service.node_registry.get(ws["node_id"])
    assert node_info["revision"] != first_run["node_revision"]
    drifted_adapter = service.adapter_registry.get(ADAPTER_ID)
    assert drifted_adapter["revision"] != original["revision"]
    third = service.call(ws_id, token, "start_run", {
        "adapter_id": ADAPTER_ID, "job_id": job["id"], "request_id": "revision-drift-continuation",
        "continue_from_run_id": second["run_id"]})
    assert third["conversation_id"] == first["conversation_id"]
    drifted_row = service.run_coordinator._run_row(service.workspace(ws_id), second["run_id"])
    assert drifted_row["effective_security"] == first_run["effective_security"]
    conv = service.run_coordinator._conversation(drifted_row)
    assert conv["node_revision"] == node_info["revision"]
    assert conv["adapter_revision"] == drifted_adapter["revision"]
    assert json.loads(first_effective)["adapter_revision"] == original["revision"]


def test_wrong_node_route_and_start_fail_before_native_call(modern_env, tmp_path):
    service, ws_id, token, job, native, _ = modern_env
    ws = service.workspace(ws_id)
    node_b = start_test_node(tmp_path / "other-node-state", Path(ws["root"]).parent)
    try:
        node_b_record = register_test_node(service, "Other Node", enabled=False)
        service._node_transport[node_b_record["id"]] = node_b["transport"]
        service._node_transport_services[node_b_record["id"]] = node_b["service"]
        service.node_registry.update(node_b_record["id"], {"enabled": True})
        wrong_adapter = create_test_adapter(service, node_b_record["id"], {
            "name": "Other Node Pi", "plugin": "pi",
        }, "adapter_555555555555555555555555")

        native_calls = []
        original_start = native.start_run
        native.start_run = lambda *args, **kwargs: (
            native_calls.append((args, kwargs)), original_start(*args, **kwargs))[1]
        with pytest.raises(BridgeError) as route_error:
            service.set_workspace_route(ws, wrong_adapter["id"], True, "read-only")
        assert route_error.value.code == "adapter_node_mismatch"
        with pytest.raises(BridgeError) as start_error:
            service.call(ws_id, token, "start_run", {
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
    started = service.call(ws_id, token, "start_run", {
        "adapter_id": ADAPTER_ID, "job_id": job["id"], "request_id": "modern-1"})
    bridge_id = started["run_id"]
    assert started["phase"] == "active" and started["runtime_type"] == "codex"
    assert started["adapter_id"] == ADAPTER_ID
    repeated = service.call(ws_id, token, "start_run", {
        "adapter_id": ADAPTER_ID, "job_id": job["id"], "request_id": "modern-1"})
    assert repeated["run_id"] == bridge_id and repeated["idempotent"] is True
    with pytest.raises(BridgeError) as conflict:
        service.call(ws_id, token, "start_run", {
            "adapter_id": ADAPTER_ID, "job_id": job["id"], "request_id": "modern-1",
            "model": "different-model"})
    assert conflict.value.code == "conflict"
    native._request(99, "item/fileChange/requestApproval", {
        "threadId": "native-thread-1", "turnId": "native-turn-1",
        "reason": "Run tests", "availableDecisions": ["accept", "decline"]})
    viewed = service.call(ws_id, token, "read_run", {"run_id": bridge_id})
    assert viewed["active_state"] == "waiting_interaction"
    wait_for_notifications(manager)
    assert [event.event_type for event in recorder.calls] == ["run_needs_attention"]
    assert recorder.calls[0].subject_id == viewed["interactions"][0]["id"]
    service.call(ws_id, token, "read_run", {"run_id": bridge_id})
    assert len(recorder.calls) == 1
    interaction = viewed["interactions"][0]
    choice = next(item for item in interaction["details"]["choices"]
                  if item["semantic"] == "approve")
    service.call(ws_id, token, "answer_interaction", {
        "run_id": bridge_id, "interaction_id": interaction["id"],
        "choice_id": choice["id"],
        "decided_by": "chatgpt", "reason": "Runs the tests the handoff names"})
    assert rpc.responses == [(99, {"decision": "accept"}, None)]
    native._notification("item/completed", {
        "threadId": "native-thread-1", "turnId": "native-turn-1",
        "item": {"id": "command-1", "type": "commandExecution", "command": "pytest"}})
    native._notification("turn/completed", {
        "threadId": "native-thread-1", "turn": {"id": "native-turn-1",
                                                  "status": "completed"}})
    finished = service.call(ws_id, token, "read_run", {"run_id": bridge_id})
    assert finished["outcome"] == "succeeded"
    wait_for_notifications(manager)
    service.call(ws_id, token, "read_run", {"run_id": bridge_id})
    assert [event.event_type for event in recorder.calls] == [
        "run_needs_attention", "run_completed"]
    assert manager.summary(bridge_id)["overall"] == "sent"
    activities = service.call(ws_id, token, "run_log",
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
    started = service.call(ws_id, token, "start_run", {
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
    started = service.call(ws_id, token, "start_run", {
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
    started = service.call(ws_id, token, "start_run", {
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
        service.call(ws_id, token, "start_run", {
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
        "name": "Local Codex", "plugin": "codex"}, ADAPTER_ID)
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
    started = service.call(ws_id, token, "start_run", {
        "adapter_id": ADAPTER_ID, "job_id": job["id"], "request_id": "rebind-1"})
    bridge_id = started["run_id"]
    with service.lock, service.db:
        service.db.execute(
            "UPDATE agent_runs SET native_id=NULL,phase='starting',active_state=NULL "
            "WHERE id=?", (bridge_id,))
    recovered = service.call(ws_id, token, "read_run", {"run_id": bridge_id})
    assert recovered["phase"] == "active"
    assert recovered["run_id"] == bridge_id


def _second_job(service, ws_id, token, payload, request_id):
    body = dict(payload)
    body["request_id"] = request_id
    return service.call(ws_id, token, "save_handoff",
                          Handoff.model_validate(body).model_dump())


def _run_counts(service, ws_id):
    with service.lock:
        runs = service.db.execute(
            "SELECT count(*) FROM agent_runs WHERE workspace=?", (ws_id,)).fetchone()[0]
        convs = service.db.execute(
            "SELECT count(*) FROM agent_conversations WHERE workspace=?", (ws_id,)).fetchone()[0]
    return runs, convs


def test_cross_handoff_continuation_reuses_conversation_with_new_prompt(modern_env, payload, monkeypatch):
    service, ws_id, token, first_job, native, _ = modern_env
    first = service.call(ws_id, token, "start_run", {
        "adapter_id": ADAPTER_ID, "job_id": first_job["id"], "request_id": "cross-first"})
    _finish_test_run(service, native, ws_id, first["run_id"])
    second_job = _second_job(service, ws_id, token, payload, "cross-second-job")
    assert second_job["id"] != first_job["id"]
    captured = []
    client = service.run_coordinator.adapter(ADAPTER_ID)
    original_start = client.start_run

    def recording_start(conversation_id, body):
        captured.append((conversation_id, body))
        return original_start(conversation_id, body)

    monkeypatch.setattr(client, "start_run", recording_start)
    second = service.call(ws_id, token, "start_run", {
        "adapter_id": ADAPTER_ID, "job_id": second_job["id"],
        "request_id": "cross-second", "continue_from_run_id": first["run_id"]})
    assert second["conversation_id"] == first["conversation_id"]
    assert second["job_id"] == second_job["id"]
    assert second["continue_from_run_id"] == first["run_id"]
    assert second["parent_run_id"] == first["run_id"]
    assert captured, "native start_run was not called"
    prompt = captured[0][1]["input"][0]["text"]
    assert second_job["id"] in prompt
    # The prompt carries the handoff text itself; nothing is read from the repo.
    assert f"Handoff {second_job['id']}" in prompt and "===== TASK.md =====" in prompt
    assert ".workspace-handoff" not in prompt
    ws = service.workspace(ws_id)
    stored = service.run_coordinator._run_row(ws, second["run_id"])
    assert stored["handoff"] == second_job["id"]
    assert stored["continue_from"] == first["run_id"]
    assert stored["parent_run"] == first["run_id"]


def test_cross_handoff_continuation_idempotent_with_canonical_parent(modern_env, payload):
    service, ws_id, token, first_job, native, _ = modern_env
    first = service.call(ws_id, token, "start_run", {
        "adapter_id": ADAPTER_ID, "job_id": first_job["id"], "request_id": "idem-first"})
    _finish_test_run(service, native, ws_id, first["run_id"])
    second_job = _second_job(service, ws_id, token, payload, "idem-second-job")
    second = service.call(ws_id, token, "start_run", {
        "adapter_id": ADAPTER_ID, "job_id": second_job["id"],
        "request_id": "idem-second", "continue_from_run_id": first["run_id"]})
    # Exact retry with omitted parent returns the same run.
    retry = service.call(ws_id, token, "start_run", {
        "adapter_id": ADAPTER_ID, "job_id": second_job["id"],
        "request_id": "idem-second", "continue_from_run_id": first["run_id"]})
    assert retry["run_id"] == second["run_id"]
    assert retry["idempotent"] is True
    # Retry that spells the canonical parent explicitly is the same content.
    explicit = service.call(ws_id, token, "start_run", {
        "adapter_id": ADAPTER_ID, "job_id": second_job["id"],
        "request_id": "idem-second", "continue_from_run_id": first["run_id"],
        "parent_run_id": first["run_id"]})
    assert explicit["run_id"] == second["run_id"]
    assert explicit["idempotent"] is True


def test_continuation_parent_mismatch_fails_without_native_start(modern_env, payload, monkeypatch):
    service, ws_id, token, first_job, native, _ = modern_env
    first = service.call(ws_id, token, "start_run", {
        "adapter_id": ADAPTER_ID, "job_id": first_job["id"], "request_id": "mismatch-first"})
    _finish_test_run(service, native, ws_id, first["run_id"])
    other = service.call(ws_id, token, "start_run", {
        "adapter_id": ADAPTER_ID, "job_id": first_job["id"], "request_id": "mismatch-other"})
    _finish_test_run(service, native, ws_id, other["run_id"])
    second_job = _second_job(service, ws_id, token, payload, "mismatch-second-job")
    client = service.run_coordinator.adapter(ADAPTER_ID)
    calls = []
    monkeypatch.setattr(client, "start_run",
                          lambda *a, **k: (calls.append((a, k)), (_ for _ in ()).throw(AssertionError("native start must not run"))))
    monkeypatch.setattr(client, "create_conversation",
                          lambda *a, **k: (calls.append((a, k)), (_ for _ in ()).throw(AssertionError("native create must not run"))))
    before = _run_counts(service, ws_id)
    with pytest.raises(BridgeError) as exc:
        service.call(ws_id, token, "start_run", {
            "adapter_id": ADAPTER_ID, "job_id": second_job["id"],
            "request_id": "mismatch-continuation",
            "continue_from_run_id": first["run_id"], "parent_run_id": other["run_id"]})
    assert exc.value.code == "continuation_parent_mismatch"
    assert calls == []
    assert _run_counts(service, ws_id) == before


def test_continuation_source_failure_modes_stay_unavailable(modern_env, payload, monkeypatch):
    service, ws_id, token, first_job, native, _ = modern_env
    ws = service.workspace(ws_id)
    # Prepare sources without leaving concurrent native actives: finish each
    # run (global native idle), then tamper the Bridge phase/outcome to
    # simulate nonterminal/failed while the native conversation stays idle.
    # The Bridge must reject nonterminal sources before any native
    # start/create, while a failed terminal source may continue when the
    # live adapter still owns and proves the idle conversation.
    active = service.call(ws_id, token, "start_run", {
        "adapter_id": ADAPTER_ID, "job_id": first_job["id"], "request_id": "fail-active"})
    _finish_test_run(service, native, ws_id, active["run_id"])
    with service.lock, service.db:
        service.db.execute(
            "UPDATE agent_runs SET phase='active',active_state='thinking',outcome=NULL WHERE id=?",
            (active["run_id"],))
    finished = service.call(ws_id, token, "start_run", {
        "adapter_id": ADAPTER_ID, "job_id": first_job["id"], "request_id": "fail-terminal"})
    _finish_test_run(service, native, ws_id, finished["run_id"])
    with service.lock, service.db:
        service.db.execute(
            "UPDATE agent_runs SET outcome='failed',result='',error='boom',updated=? WHERE id=?",
            (service.run_coordinator.read(ws, finished["run_id"], sync=False)["updated"], finished["run_id"]))
    good = service.call(ws_id, token, "start_run", {
        "adapter_id": ADAPTER_ID, "job_id": first_job["id"], "request_id": "fail-model-good"})
    _finish_test_run(service, native, ws_id, good["run_id"])
    second_job = _second_job(service, ws_id, token, payload, "fail-second-job")
    client = service.run_coordinator.adapter(ADAPTER_ID)
    calls = []

    def _forbidden_start(*a, **k):
        calls.append("start")
        raise AssertionError("native start must not run")

    def _forbidden_create(*a, **k):
        calls.append("create")
        raise AssertionError("native create must not run")

    monkeypatch.setattr(client, "start_run", _forbidden_start)
    monkeypatch.setattr(client, "create_conversation", _forbidden_create)
    before = _run_counts(service, ws_id)
    # Nonterminal source is ineligible.
    with pytest.raises(BridgeError) as exc:
        service.call(ws_id, token, "start_run", {
            "adapter_id": ADAPTER_ID, "job_id": second_job["id"],
            "request_id": "fail-active-continuation",
            "continue_from_run_id": active["run_id"]})
    assert exc.value.code == "continuation_unavailable"
    assert calls == []
    assert _run_counts(service, ws_id) == before
    # A failed terminal source with an owned idle conversation may now
    # continue: the historical outcome does not gate a proven conversation.
    monkeypatch.undo()
    resumed = service.call(ws_id, token, "start_run", {
        "adapter_id": ADAPTER_ID, "job_id": second_job["id"],
        "request_id": "fail-failed-continuation",
        "continue_from_run_id": finished["run_id"]})
    assert resumed["conversation_id"] == service.run_coordinator._conversation(
        service.run_coordinator._run_row(ws, finished["run_id"]))["id"]
    _finish_test_run(service, native, ws_id, resumed["run_id"])
    # Explicit model that cannot resolve stays a safe model error with no run.
    with pytest.raises(BridgeError) as model_exc:
        service.call(ws_id, token, "start_run", {
            "adapter_id": ADAPTER_ID, "job_id": second_job["id"],
            "request_id": "fail-model-continuation",
            "continue_from_run_id": good["run_id"], "model": "missing-model"})
    assert model_exc.value.code in {"model_not_enabled", "model_unavailable"}


def test_cross_handoff_continuation_busy_and_owned_drift(modern_env, payload, monkeypatch):
    service, ws_id, token, first_job, native, rpc = modern_env
    ws = service.workspace(ws_id)
    first = service.call(ws_id, token, "start_run", {
        "adapter_id": ADAPTER_ID, "job_id": first_job["id"], "request_id": "drift-first"})
    _finish_test_run(service, native, ws_id, first["run_id"])
    second_job = _second_job(service, ws_id, token, payload, "drift-second-job")
    client = service.run_coordinator.adapter(ADAPTER_ID)
    calls = []

    def _forbidden_start(*a, **k):
        calls.append("start")
        raise AssertionError("native start must not run")

    def _forbidden_create(*a, **k):
        calls.append("create")
        raise AssertionError("native create must not run")

    monkeypatch.setattr(client, "start_run", _forbidden_start)
    monkeypatch.setattr(client, "create_conversation", _forbidden_create)
    # Busy native conversation stays conversation_busy with no new run.
    rpc.status = "active"
    before = _run_counts(service, ws_id)
    with pytest.raises(BridgeError) as busy_exc:
        service.call(ws_id, token, "start_run", {
            "adapter_id": ADAPTER_ID, "job_id": second_job["id"],
            "request_id": "drift-busy", "continue_from_run_id": first["run_id"]})
    assert busy_exc.value.code == "conversation_busy"
    assert calls == []
    assert _run_counts(service, ws_id) == before
    monkeypatch.undo()
    rpc.status = "idle"
    # Revision churn alone is proven benign: the same live adapter still
    # owns the idle conversation, so continuation proceeds, refreshes the
    # conversation's current Node metadata, and leaves run evidence intact.
    node_row = service.node_registry.get(ws["node_id"])
    first_run = service.run_coordinator._run_row(ws, first["run_id"])
    with service.lock, service.db:
        service.db.execute("UPDATE nodes SET revision=? WHERE id=?",
                           (node_row["revision"] + "-drifted", ws["node_id"]))
    second = service.call(ws_id, token, "start_run", {
        "adapter_id": ADAPTER_ID, "job_id": second_job["id"],
        "request_id": "drift-node", "continue_from_run_id": first["run_id"]})
    assert second["conversation_id"] == first["conversation_id"]
    drifted = service.run_coordinator._run_row(service.workspace(ws_id), second["run_id"])
    assert drifted["node_revision"] == node_row["revision"] + "-drifted"
    # The historical run keeps its original revision evidence.
    assert service.run_coordinator._run_row(
        service.workspace(ws_id), first["run_id"])["effective_security"] \
        == first_run["effective_security"]
    conv = service.run_coordinator._conversation(drifted)
    assert conv["node_revision"] == node_row["revision"] + "-drifted"


def test_list_agent_adapters_reports_bounded_descriptor_ok(modern_env):
    service, ws_id, _, _, _, _ = modern_env
    ws = service.workspace(ws_id)
    policy = service.workspace_route_policy(ws)["routes"][ADAPTER_ID]
    listed = {item["adapter_id"]: item for item in service.list_agent_adapters(ws)["adapters"]}
    entry = listed[ADAPTER_ID]
    desc = entry["descriptor"]
    assert desc["status"] == "ok"
    assert desc["features"].get("steering") == 1
    assert isinstance(desc["instance_id"], str) and desc["instance_id"]
    assert isinstance(desc["adapter_version"], str)
    assert isinstance(desc["native_version"], str)
    assert set(desc) <= {"status", "features", "instance_id", "adapter_version", "native_version"}
    blob = json.dumps(entry)
    assert "codex-test-secret" not in blob
    assert "unbound-secret" not in blob
    assert ".workspace-handoff" not in blob
    assert entry["available"] == bool(policy["ready"])
    assert entry["readiness"] == policy["readiness"]


def test_list_agent_adapters_descriptor_failure_safe_and_readiness_unchanged(modern_env, monkeypatch):
    from workspace_bridge.runtime import RuntimeUnavailable
    service, ws_id, _, _, _, _ = modern_env
    ws = service.workspace(ws_id)
    policy = service.workspace_route_policy(ws)["routes"][ADAPTER_ID]
    client = service.run_coordinator.adapter(ADAPTER_ID)
    secret = "sk-proj-list-secret-marker-4242"
    raw_url = "http://evil-list.example/secret-path"
    raw_path = "/tmp/evil-secret-path"

    def _fail():
        raise RuntimeUnavailable(f"transport boom {secret} {raw_url} {raw_path}")
    monkeypatch.setattr(client, "descriptor", _fail)
    listed = {item["adapter_id"]: item for item in service.list_agent_adapters(ws)["adapters"]}
    entry = listed[ADAPTER_ID]
    desc = entry["descriptor"]
    assert desc["status"] == "unavailable"
    assert desc["code"] == "runtime_unavailable"
    blob = json.dumps(listed)
    assert secret not in blob
    assert raw_url not in blob
    assert raw_path not in blob
    assert "transport boom" not in blob
    assert entry["available"] == bool(policy["ready"])
    assert entry["readiness"] == policy["readiness"]
    assert entry["adapter_enabled"] is True


def test_list_agent_adapters_disabled_not_probed(modern_env, monkeypatch):
    service, ws_id, _, _, _, _ = modern_env
    ws = service.workspace(ws_id)
    disabled_id = "adapter_000000000000000000000003"
    create_test_adapter(service, ws["node_id"], {
        "name": "Disabled Codex", "plugin": "codex",
    }, disabled_id)
    node_svc = service._node_transport_services[ws["node_id"]]
    node_svc.save_adapter({"enabled": False}, disabled_id)
    service.node_registry.refresh_adapters(ws["node_id"])
    assert service.adapter_registry.get(disabled_id)["enabled"] == 0
    orig_client = service.adapter_registry.client

    def guarded_client(adapter_id, **kwargs):
        if adapter_id == disabled_id:
            raise AssertionError("disabled adapter must not be probed")
        return orig_client(adapter_id, **kwargs)
    monkeypatch.setattr(service.adapter_registry, "client", guarded_client)
    listed = {item["adapter_id"]: item for item in service.list_agent_adapters(ws)["adapters"]}
    assert listed[disabled_id]["descriptor"]["status"] == "disabled"
    assert listed[disabled_id]["descriptor"]["code"] == "adapter_disabled"
    assert listed[disabled_id]["adapter_enabled"] is False
    assert listed[ADAPTER_ID]["descriptor"]["status"] == "ok"
    assert listed[ADAPTER_ID]["descriptor"]["features"].get("steering") == 1
