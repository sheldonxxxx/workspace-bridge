"""Agent Protocol v2: Node-supervised stdio plugins."""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

from conftest import attach_test_node
from workspace_bridge.adapter_events import validate_event_page
from workspace_bridge.api import Handoff
from workspace_bridge.cli import initialize
from workspace_bridge.node_service import NodeService
from workspace_bridge.plugin_protocol import (builtin_manifests, to_rpc,
                                              validate_manifest)
from workspace_bridge.runtime import RuntimeRejected, RuntimeUnavailable
from workspace_bridge.security import BridgeError, digest
from workspace_bridge.service import Service

PLUGINS = Path(__file__).parent / "plugins"


def manifest_file(tmp_path: Path, ident: str, script: str, settings=("mode", "big")) -> Path:
    path = tmp_path / f"{ident}.toml"
    keys = ", ".join(f'"{key}"' for key in settings)
    path.write_text(f'id = "{ident}"\nversion = "0.1"\nprotocol = 2\nruntime_type = "pi"\n'
                    f'command = ["{sys.executable}", "{PLUGINS / script}"]\nsettings = [{keys}]\n')
    return path


@pytest.fixture
def node(tmp_path):
    projects = tmp_path / "projects"
    (projects / "alpha").mkdir(parents=True)
    service = NodeService(tmp_path / "node", {
        "allowed_roots": [str(projects)], "node_token_hash": digest(b"t"), "host": "127.0.0.1"})
    service.install_manifest(manifest_file(tmp_path, "fake", "fake_agent.py"))
    yield service, projects
    service.close()


def add(node_service, projects, **settings):
    return node_service.save_adapter({"name": "Fake " + "".join(settings), "plugin": "fake",
                                      "projects_root": str(projects), "settings": settings})


def client(node_service, adapter_id, timeout=10):
    return node_service.plugin_client(node_service.adapter(adapter_id), timeout=timeout)


def test_builtin_manifests_cover_the_four_agents():
    manifests = builtin_manifests()
    assert set(manifests) == {"claude", "codex", "opencode", "pi"}
    assert manifests["claude"]["command"][1:] == ["-m", "workspace_bridge.plugin_host", "claude"]
    assert manifests["pi"]["command"] == ["workspace-bridge-pi-adapter", "--stdio"]


@pytest.mark.parametrize("change, message", [
    ({"protocol": 1}, "protocol"),
    ({"runtime_type": "shell"}, "runtime_type"),
    ({"surprise": 1}, "unknown keys"),
    ({"python_module": "claude"}, "exactly one"),
    ({"command": "sh -c x"}, "command"),
    ({"settings": ["Bad-Key"]}, "settings"),
])
def test_manifest_validation(change, message):
    raw = {"id": "x", "version": "1", "protocol": 2, "runtime_type": "pi", "command": ["x"]}
    with pytest.raises(BridgeError, match=message):
        validate_manifest({**raw, **change})


def test_v1_paths_map_to_named_methods():
    assert to_rpc("GET", "/v1/conversations/c1/runs/r1", None, None) == (
        "runs.find", {"conversationId": "c1", "clientRunId": "r1"})
    assert to_rpc("GET", "/v1/profiles", {"workspaceId": "w", "directory": None, "fresh": "1"},
                  None) == ("profiles.list", {"workspaceId": "w", "fresh": True})
    assert to_rpc("POST", "/v1/runs/r/steer", None, {"a": 1}) == (
        "runs.steer", {"runId": "r", "body": {"a": 1}})
    with pytest.raises(BridgeError):
        to_rpc("POST", "/v1/shell", None, {})


def test_plugin_adapter_fields_are_validated(node, tmp_path):
    service, projects = node
    with pytest.raises(BridgeError, match="no URL or token"):
        service.save_adapter({"name": "x", "plugin": "fake", "token": "t"})
    with pytest.raises(BridgeError, match="outside"):
        service.save_adapter({"name": "x", "plugin": "fake", "projects_root": str(tmp_path)})
    with pytest.raises(BridgeError, match="settings accept only"):
        service.save_adapter({"name": "x", "plugin": "fake", "settings": {"binary": "/bin/sh"}})
    with pytest.raises(BridgeError) as unknown:
        service.save_adapter({"name": "x", "plugin": "nope"})
    assert unknown.value.code == "unknown_plugin"
    row = add(service, projects)
    assert row["kind"] == "plugin" and row["base_url"] == "" and not row["has_token"]
    assert row["projects_root"] == str(projects)
    # Changing settings rotates the revision, so runs bound to the old
    # configuration are recognised as changed.
    updated = service.save_adapter({"settings": {"mode": "busy"}}, row["id"])
    assert updated["revision"] != row["revision"]
    assert service.save_adapter({"name": "Renamed"}, row["id"])["revision"] == updated["revision"]


def test_requests_round_trip_and_errors_keep_their_v1_meaning(node):
    service, projects = node
    plugin = client(service, add(service, projects, mode="busy")["id"])
    descriptor = plugin.descriptor()
    assert descriptor.runtime_id == "pi" and descriptor.supports("events")
    assert plugin.models("ws")[0]["selector"] == "fake"
    with pytest.raises(RuntimeRejected) as busy:
        plugin.start_run("conv1", {"clientRunId": "run_1", "input": []})
    assert busy.value.code == "conversation_busy" and busy.value.status == 409
    with pytest.raises(RuntimeRejected) as missing:
        plugin.run("nope")
    assert missing.value.code == "not_found"
    # Unexpected plugin exceptions surface as unavailability, without detail.
    with pytest.raises(RuntimeUnavailable) as hidden:
        plugin.process.call("usageLimits", {})
    assert "secret" not in str(hidden.value)


def test_stray_stdout_and_node_environment_never_reach_the_protocol(node, monkeypatch):
    service, projects = node
    monkeypatch.setenv("WB_NODE_SECRET", "do-not-pass")
    plugin = client(service, add(service, projects)["id"])
    assert plugin.descriptor().runtime_id == "pi"
    state = Path(service.state / "plugins" / plugin.adapter_id / "state")
    log = (service.state / "plugins" / plugin.adapter_id / "plugin.log").read_text()
    assert "noise on stdout" in log and "raw noise on fd 1" in log
    pid = int((state / "started").read_text())
    environ = Path(f"/proc/{pid}/environ")
    if environ.exists():
        assert b"WB_NODE_SECRET" not in environ.read_bytes()


def test_events_are_pushed_and_served_as_v1_pages(node):
    service, projects = node
    plugin = client(service, add(service, projects)["id"])
    instance = plugin.descriptor().instance_id
    run = plugin.start_run("conv1", {"clientRunId": "run_1", "input": []})
    page = plugin.events(after=0, wait_ms=2000)
    checked = validate_event_page(page, 0)
    assert checked["instanceId"] == instance and not checked["gap"]
    assert [(e["type"], e["runId"]) for e in checked["events"]] == [("run.started", run["id"])]
    started = time.monotonic()
    assert plugin.events(after=page["cursor"], wait_ms=300)["events"] == []
    assert time.monotonic() - started >= 0.25


def test_a_plugin_whose_native_runtime_is_down_stays_up(node):
    service, projects = node
    plugin = client(service, add(service, projects, mode="native-down")["id"])
    with pytest.raises(RuntimeUnavailable, match="Native runtime is unavailable"):
        plugin.descriptor()
    pid = plugin.process._process.pid
    assert plugin.models("ws")[0]["selector"] == "fake"
    assert plugin.process._process.pid == pid and plugin.process.instance_id.startswith("fake_instance_")


def test_a_crashed_plugin_fails_pending_calls_and_restarts_with_backoff(node):
    service, projects = node
    plugin = client(service, add(service, projects, mode="crash")["id"])
    state = service.state / "plugins" / plugin.adapter_id / "state"
    first_pid = (state / "started").read_text() if plugin.descriptor() else None
    with pytest.raises(RuntimeUnavailable, match="exited"):
        plugin.cancel("native1")
    with pytest.raises(RuntimeUnavailable, match="restarting"):
        plugin.descriptor()
    plugin.process._retry_at = 0
    assert plugin.descriptor().runtime_id == "pi"
    assert (state / "started").read_text() != first_pid


def test_a_slow_call_times_out_without_blocking_others(node):
    service, projects = node
    plugin = client(service, add(service, projects, mode="slow")["id"], timeout=0.5)
    plugin.descriptor()
    started = time.monotonic()
    with pytest.raises(RuntimeUnavailable, match="in time"):
        plugin.cancel("native1")
    assert time.monotonic() - started < 2
    assert plugin.models("ws")[0]["selector"] == "fake"


def test_removing_or_closing_stops_the_plugin_gracefully(node):
    service, projects = node
    row = add(service, projects)
    plugin = client(service, row["id"])
    plugin.descriptor()
    state = service.state / "plugins" / row["id"] / "state"
    service.delete_adapter(row["id"])
    deadline = time.monotonic() + 5
    while not (state / "closed").exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert (state / "closed").exists()
    assert plugin.process._process is None


def test_a_real_codex_adapter_runs_as_a_plugin_through_the_bridge(tmp_path, payload):
    parent = tmp_path / "projects"
    root = parent / "alpha"
    root.mkdir(parents=True)
    state = tmp_path / "bridge"
    service = Service(state, initialize(state, 8765, 8766), run_coordinator_background=False)
    node, record = attach_test_node(service, tmp_path / "node-state", parent)
    node_service = node["service"]
    try:
        node_service.install_manifest(manifest_file(tmp_path, "fakecodex", "fake_codex.py", ()))
        manifest = node_service.manifests()["fakecodex"]
        manifest["runtime_type"] = "codex"
        node_service.manifests = lambda: {"fakecodex": manifest}
        adapter = node_service.save_adapter({"name": "Codex plugin", "plugin": "fakecodex",
                                             "projects_root": str(parent)})
        service.node_registry.refresh_adapters(record["id"])
        ws_id = service.add_workspace("Alpha", str(root), [])["workspace"]["id"]
        token = service.manage_bridge("rotate_token")["token"]
        service.manage_workspace(ws_id, "enable")
        ws = service.workspace(ws_id)
        listed = service.list_agent_adapters(ws)["adapters"]
        assert [a["descriptor"]["status"] for a in listed] == ["ok"]
        service.set_workspace_route(ws, adapter["id"], True, "workspace-write-reviewed")
        service.run_coordinator.set_model_policy(adapter["id"], ["gpt-test"], "gpt-test")
        job = service.call(ws_id, token, "prepare_handoff",
                           Handoff.model_validate(payload).model_dump())
        started = service.call(ws_id, token, "start_agent_run", {
            "adapter_id": adapter["id"], "job_id": job["id"], "request_id": "plugin-1"})
        assert started["phase"] in {"starting", "active"}
        read = service.call(ws_id, token, "read_agent_run", {"run_id": started["run_id"]})
        assert read["run_id"] == started["run_id"] and "sync_warning" not in read
        # The run went through a supervised child process, not an HTTP URL.
        process = node_service.plugins._processes[adapter["id"]][1]
        assert process._process is not None and process._process.poll() is None
        assert (node_service.state / "plugins" / adapter["id"] / "state").is_dir()
    finally:
        service.close()
        node["stop"]()


async def test_manager_api_lists_plugins_and_creates_a_plugin_adapter(env):
    import httpx
    from admin_helpers import admin_cookie
    from workspace_bridge.api import make_admin
    service = env["service"]
    app = make_admin(service)
    cookie = admin_cookie(app)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://127.0.0.1:8766") as http:
        listed = await http.get(f"/api/nodes/{env['node_id']}/plugins", headers={"Cookie": cookie})
        assert listed.status_code == 200
        assert {"claude", "codex", "opencode", "pi"} <= {p["id"] for p in listed.json()["plugins"]}
        created = await http.post("/api/adapters", headers={"Cookie": cookie}, json={
            "node_id": env["node_id"], "name": "Claude here", "plugin": "claude",
            "settings": {"setting_sources": "user"}})
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["runtime_type"] == "claude" and body["base_url"] == ""
        bad = await http.post("/api/adapters", headers={"Cookie": cookie}, json={
            "node_id": env["node_id"], "name": "x", "plugin": "claude", "token": "t"})
        assert bad.status_code == 400
    row = env["node"]["service"].adapter(body["id"])
    assert row["kind"] == "plugin" and row["projects_root"] == str(env["parent"])
