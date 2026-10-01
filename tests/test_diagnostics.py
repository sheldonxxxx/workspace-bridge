"""Diagnostics and schema checks for exact workspace/adapter routes."""
from __future__ import annotations
from admin_helpers import admin_cookie

import json
import sqlite3

import pytest
import httpx

from workspace_bridge.api import make_admin
from workspace_bridge.cli import initialize
from workspace_bridge.diagnostics import MAX_ROUTES
from workspace_bridge.security import BridgeError
from workspace_bridge.service import Service
from workspace_bridge.wbrp import CORE_FEATURES, Descriptor
from conftest import attach_test_node


class DiagnosticAdapter:
    def __init__(self, runtime_type: str, models: list[str]):
        self.runtime_type = runtime_type
        self.model_rows = [{"selector": name, "reasoningOptions": ["low", "high"]}
                           for name in models]
        self.profile_rows = [{"id": "reviewed", "revision": "rev-1",
                              "available": True}]
        self.features = dict.fromkeys(CORE_FEATURES, 1)
        self.descriptor_calls = 0
        self.model_calls: list[str] = []

    def descriptor(self):
        self.descriptor_calls += 1
        return Descriptor(self.runtime_type, self.runtime_type, "1", "test",
                          f"native-{self.runtime_type}", self.features)

    def profile_catalog(self, _workspace_id=None, _directory=None, *, fresh=False):
        return {"profiles": list(self.profile_rows)}

    def profiles(self):
        return list(self.profile_rows)

    def models(self, workspace_id: str):
        self.model_calls.append(workspace_id)
        return list(self.model_rows)


@pytest.fixture
def diagnostic_env(tmp_path):
    parent = tmp_path / "projects"
    parent.mkdir()
    state = tmp_path / "state"
    config = initialize(state, 8765, 8766)
    service = Service(state, config, run_coordinator_background=False)
    node, node_record = attach_test_node(service, tmp_path / "node-state", parent)
    adapters = {}
    for name, model in (("Local Pi", "local-model"), ("GPU Pi", "gpu-model")):
        row = service.adapter_registry.create({
            "node_id": node_record["id"],
            "name": name, "runtime_type": "pi",
            "base_url": f"http://127.0.0.1:{8760 + len(adapters)}",
            "token": f"private-{name.lower().replace(' ', '-')}",
        })
        adapters[row["id"]] = DiagnosticAdapter("pi", [model])
    client_factory = service.adapter_registry.client
    service.adapter_registry.client = lambda adapter_id, **kwargs: (
        adapters[adapter_id] if adapter_id in adapters else client_factory(adapter_id, **kwargs))
    yield {"service": service, "state": state, "parent": parent,
           "node": node, "node_id": node_record["id"],
           "adapters": adapters, "client_factory": client_factory, "tmp": tmp_path}
    service.close()
    node["stop"]()


def ready_workspace(env, name="Alpha"):
    root = env["parent"] / name.lower()
    root.mkdir()
    service = env["service"]
    ws_id = service.add_workspace(name, str(root), []) ["workspace"]["id"]
    service.manage_workspace(ws_id, "enable")
    service.manage_workspace(ws_id, "set_agent_enabled", agent_enabled=True)
    if not service.bridge_status()["configured"]:
        service.manage_bridge("rotate_token")
    ws = service.workspace(ws_id)
    for adapter_id, client in env["adapters"].items():
        profile = client.profile_rows[0]
        service.run_coordinator.set_model_policy(
            adapter_id, [client.model_rows[0]["selector"]],
            client.model_rows[0]["selector"], ws)
        service.set_workspace_route(ws, adapter_id, True, profile["id"])
    return service.workspace(ws_id)


def test_two_same_runtime_adapters_have_independent_ready_routes(diagnostic_env):
    env = diagnostic_env
    ws = ready_workspace(env)
    report = env["service"].diagnostic_report()
    routes = [row for row in report["runnable_routes"]
              if row["workspace_id"] == ws["id"]]

    assert len(routes) == 2
    assert {row["runtime_type"] for row in routes} == {"pi"}
    assert {row["adapter_id"] for row in routes} == set(env["adapters"])
    assert all(row["ready"] and row["blockers"] == [] for row in routes)
    assert {row["adapter_name"] for row in routes} == {"Local Pi", "GPU Pi"}
    assert {row["default_model_selector"] for row in routes} == {
        "local-model", "gpu-model"}
    assert all(client.descriptor_calls == 1 for client in env["adapters"].values())
    assert str(env["parent"]) not in json.dumps(report)
    for secret in ("private-local-pi", "private-gpu-pi"):
        assert secret not in json.dumps(report)


def test_write_scope_none_does_not_block_route_readiness(diagnostic_env):
    """Run admission never checks write_scope, so neither do route blockers.

    The separate workspace.write_scope diagnostic still reports the
    handoff-publication limitation as action_required.
    """
    env = diagnostic_env
    ws = ready_workspace(env)
    local_id, gpu_id = env["adapters"]
    env["service"].manage_workspace(ws["id"], "set_write_scope", write_scope="none")
    ws = env["service"].workspace(ws["id"])
    assert ws["write_scope"] == "none"

    report = env["service"].diagnostic_report()
    routes = [row for row in report["runnable_routes"]
              if row["workspace_id"] == ws["id"]]
    assert len(routes) == 2
    for route in routes:
        assert route["ready"] is True
        assert "workspace.write_scope" not in route["blockers"]
        assert route["blockers"] == []
    write_checks = [row for row in report["checks"]
                    if row["code"] == "workspace.write_scope"
                    and row.get("workspace_id") == ws["id"]]
    assert write_checks and all(row["status"] == "action_required"
                                for row in write_checks)
    assert any("handoff" in row["summary"] or "prepared" in row["summary"]
               for row in write_checks)


def test_route_disable_and_offline_mode_are_exact_to_adapter(diagnostic_env):
    env = diagnostic_env
    ws = ready_workspace(env)
    local_id, gpu_id = env["adapters"]
    env["service"].set_workspace_route(ws, local_id, False)

    live = env["service"].diagnostic_report()
    local = next(row for row in live["runnable_routes"]
                 if row["adapter_id"] == local_id)
    gpu = next(row for row in live["runnable_routes"]
               if row["adapter_id"] == gpu_id)
    assert local["ready"] is False and "workspace_route.enabled" in local["blockers"]
    assert gpu["ready"] is True

    calls_before = [client.descriptor_calls for client in env["adapters"].values()]
    offline = env["service"].diagnostic_report(offline=True)
    assert all(row["status"] == "blocked" for row in offline["runnable_routes"])
    assert [client.descriptor_calls for client in env["adapters"].values()] == calls_before


def test_adapter_tokens_are_not_returned_and_deletion_is_blocked(diagnostic_env):
    env = diagnostic_env
    ws = ready_workspace(env)
    service = env["service"]
    adapter_id = next(iter(env["adapters"]))
    public = service.list_adapters()
    assert all("token" not in item for item in public["adapters"])
    assert all(item["has_token"] for item in public["adapters"])
    serialized = json.dumps({"adapters": public, "diagnostics": service.diagnostic_report()})
    assert "private-local-pi" not in serialized
    assert "private-gpu-pi" not in serialized
    with pytest.raises(BridgeError, match="workspace routes"):
        service.adapter_registry.delete(adapter_id)
    assert service.workspace_route_policy(ws)["routes"][adapter_id]["enabled"]


def test_adapter_connection_revision_and_blank_token_update(diagnostic_env):
    env = diagnostic_env
    service = env["service"]
    adapter_id = next(iter(env["adapters"]))
    original = service.adapter_registry.get(adapter_id)
    renamed = service.adapter_registry.update(adapter_id, {
        "name": "Local Pi Renamed", "token": ""})
    assert renamed["revision"] == original["revision"]
    with pytest.raises(BridgeError) as exc:
        service.adapter_registry.update(adapter_id, {"runtime_type": "codex"})
    assert exc.value.code == "invalid_arguments"
    replaced = service.adapter_registry.update(adapter_id, {
        "base_url": "https://pi-gpu.example:9443", "token": "replacement-secret"})
    assert replaced["revision"] != original["revision"]
    service.adapter_registry.client = env["client_factory"]
    client = service.adapter_registry.client(adapter_id)
    assert not hasattr(client, "token")
    assert "token" not in service.adapter_registry.get(adapter_id)
    saved = env["node"]["service"].adapter(adapter_id)
    assert saved["base_url"] == "https://pi-gpu.example:9443"
    assert saved["token"] == "replacement-secret"


@pytest.mark.asyncio
async def test_adapter_admin_crud_is_strict_and_never_reads_back_token(diagnostic_env):
    service = diagnostic_env["service"]
    app = make_admin(service)
    token = admin_cookie(app)
    headers = {"Cookie": token}
    payload = {"node_id": diagnostic_env["node_id"],
               "name": "Local Codex", "runtime_type": "codex",
               "base_url": "http://127.0.0.1:9876", "token": "secret-for-test"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://127.0.0.1:8766") as client:
        assert (await client.post("/api/adapters", json=payload)).status_code == 401
        created = await client.post("/api/adapters", headers=headers,
                                    json={**payload, "unexpected": True})
        assert created.status_code == 400
        created = await client.post("/api/adapters", headers=headers, json=payload)
        assert created.status_code == 201, created.text
        adapter_id = created.json()["id"]
        assert "secret-for-test" not in created.text
        saved = await client.get(f"/api/adapters/{adapter_id}", headers=headers)
        assert saved.status_code == 200 and saved.json()["has_token"] is True
        assert "secret-for-test" not in saved.text
        updated = await client.patch(f"/api/adapters/{adapter_id}", headers=headers,
                                     json={"name": "Renamed Codex"})
        assert updated.status_code == 200 and "secret-for-test" not in updated.text
        with diagnostic_env["node"]["service"].lock:
            row = diagnostic_env["node"]["service"].db.execute(
                "SELECT token FROM runtime_adapters WHERE id=?", (adapter_id,)).fetchone()
        assert row["token"] == "secret-for-test"


@pytest.mark.parametrize("read_only", [False, True])
def test_legacy_state_is_rejected_without_schema_writes(tmp_path, read_only):
    parent = tmp_path / "projects"
    parent.mkdir()
    state = tmp_path / "state"
    config = initialize(state, 8765, 8766)
    database = state / "bridge.sqlite3"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE workspace_runtimes(workspace TEXT,runtime TEXT)")
    connection.commit()
    connection.close()

    connection = sqlite3.connect(database)
    before = list(connection.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_schema "
        "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"))
    connection.close()
    with pytest.raises(BridgeError) as exc:
        Service(state, config, read_only=read_only,
                run_coordinator_background=False)
    assert exc.value.code == "state_schema_incompatible"
    assert "fresh state path" in str(exc.value).lower()

    connection = sqlite3.connect(database)
    after = list(connection.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_schema "
        "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"))
    connection.close()
    assert after == before


def test_fresh_state_schema_and_private_mode(tmp_path):
    parent = tmp_path / "projects"
    parent.mkdir()
    state = tmp_path / "state"
    config = initialize(state, 8765, 8766)
    service = Service(state, config, run_coordinator_background=False)
    try:
        tables = {row["name"] for row in service.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"nodes", "node_adapters", "workspace_routes", "adapter_model_policies",
                "agent_conversations", "agent_runs", "agent_interactions",
                "agent_activities"} <= tables
        assert service.db.execute(
            "SELECT value FROM bridge_meta WHERE key='schema_version'").fetchone()[0] == "4"
        assert (state / "bridge.sqlite3").stat().st_mode & 0o777 == 0o600
    finally:
        service.close()


def test_diagnostics_route_output_is_bounded(diagnostic_env):
    report = diagnostic_env["service"].diagnostic_report(offline=True)
    assert len(report["runnable_routes"]) <= MAX_ROUTES
