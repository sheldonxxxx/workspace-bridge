from __future__ import annotations

import json
import sqlite3

import httpx
import pytest

from admin_helpers import admin_cookie
from workspace_bridge.api import make_admin
from workspace_bridge.node_service import NodeService
from workspace_bridge.security import BridgeError, digest

from conftest import create_test_adapter, register_test_node, start_test_node, patch_node_runtime



def test_read_only_node_service_does_not_create_or_mutate_state(tmp_path):
    parent = tmp_path / "projects"
    parent.mkdir()
    state = tmp_path / "node-state"
    config = {"allowed_roots": [str(parent)],
              "cursor_secret": "c" * 64}

    writable = NodeService(state, config)
    writable.save_adapter({
        "name": "Local adapter", "plugin": "pi",
    })
    live_readonly = NodeService(state, config, read_only=True)
    try:
        assert live_readonly.status()["runtime_adapters"] == 1
    finally:
        live_readonly.close()
    writable.close()
    db_path = state / "node.sqlite3"
    before_bytes = db_path.read_bytes()
    before_db_stat = db_path.stat()
    before_state_stat = state.stat()

    readonly = NodeService(state, config, read_only=True)
    try:
        assert readonly.status()["runtime_adapters"] == 1
        with pytest.raises(sqlite3.OperationalError):
            readonly.save_adapter({
                "name": "Should fail", "plugin": "pi",
            })
        with pytest.raises(sqlite3.OperationalError):
            readonly.db.execute("CREATE TABLE should_not_exist(value TEXT)")
    finally:
        readonly.close()

    after_db_stat = db_path.stat()
    after_state_stat = state.stat()
    assert db_path.read_bytes() == before_bytes
    assert (after_db_stat.st_mode & 0o777) == (before_db_stat.st_mode & 0o777) == 0o600
    assert after_db_stat.st_mtime_ns == before_db_stat.st_mtime_ns
    assert (after_state_stat.st_mode & 0o777) == (before_state_stat.st_mode & 0o777) == 0o700
    assert after_state_stat.st_mtime_ns == before_state_stat.st_mtime_ns


def test_refresh_adapters_rejects_cross_node_identity_and_surfaces_failure(env):
    service = env["service"]
    node_a_id = env["node_id"]
    collision_id = "adapter_444444444444444444444444"
    create_test_adapter(service, node_a_id, {
        "name": "Node A adapter", "plugin": "pi",
    }, collision_id)

    node_b = start_test_node(env["tmp"] / "node-b-state", env["parent"])
    try:
        record_b = register_test_node(service, "Node B", enabled=False)
        service._node_transport[record_b["id"]] = node_b["transport"]
        service._node_transport_services[record_b["id"]] = node_b["service"]
        created = node_b["service"].save_adapter({
            "name": "Node B conflicting adapter", "plugin": "pi",
        })
        with node_b["service"].lock, node_b["service"].db:
            node_b["service"].db.execute(
                "UPDATE runtime_adapters SET id=? WHERE id=?",
                (collision_id, created["id"]))

        with pytest.raises(BridgeError) as exc:
            service.node_registry.update(record_b["id"], {"enabled": True})
        assert exc.value.code == "adapter_identity_conflict"

        cached = service.adapter_registry.get(collision_id)
        assert cached["node_id"] == node_a_id
        public = next(item for item in service.node_registry.list_public()
                      if item["id"] == record_b["id"])
        assert public["health"] == "failed"
        assert public["error_code"] == "adapter_identity_conflict"
    finally:
        node_b["stop"]()


def test_runtime_call_rejects_stale_adapter_revision_before_native_construction(
        tmp_path, monkeypatch):
    parent = tmp_path / "projects"
    parent.mkdir()
    state = tmp_path / "node-state"
    config = {"allowed_roots": [str(parent)],
              "cursor_secret": "c" * 64}
    node = NodeService(state, config)
    try:
        created = node.save_adapter({
            "name": "Revision test", "plugin": "pi",
        })
        old_revision = created["revision"]
        updated = node.save_adapter({"settings": {"log_level": "DEBUG"}}, created["id"])
        assert updated["revision"] != old_revision

        constructed = []

        def unexpected_native(*args, **kwargs):
            constructed.append((args, kwargs))
            raise AssertionError("stale runtime call constructed a native client")

        patch_node_runtime(monkeypatch, unexpected_native)
        with pytest.raises(BridgeError) as exc:
            node.runtime_call(created["id"], "descriptor", {
                "arguments": {}, "expected_adapter_revision": old_revision})
        assert exc.value.code == "adapter_changed"
        assert constructed == []

        with pytest.raises(BridgeError) as missing:
            node.runtime_call(created["id"], "descriptor", {"arguments": {}})
        assert missing.value.code == "invalid_arguments"
    finally:
        node.close()


def test_status_carries_no_update_or_installer_surface(tmp_path):
    """Node status is health/identity only: no updater affordance."""
    parent = tmp_path / "projects"
    parent.mkdir()
    node = start_test_node(tmp_path / "node-state", parent)
    try:
        status = node["service"].status()
        assert status["status"] == "ok"
        assert status["protocol"] == 1
        assert "installers" not in status
        assert "deployment-v1" not in status["capabilities"]
        assert set(status["host"]) == {"platform", "arch"}
        assert "node_major" not in status["host"]
        live = json.loads(node["transport"].request("GET", "/v1/status").content)
        assert live["status"] == "ok"
        assert "installers" not in live
        assert "deployment-v1" not in live["capabilities"]
        text = json.dumps(live)
        assert "token" not in text.lower()
    finally:
        node["stop"]()


def test_runtime_call_refuses_unknown_operations(tmp_path):
    from workspace_bridge.node_service import NodeService
    from workspace_bridge.security import BridgeError, digest
    parent = tmp_path / "projects"
    parent.mkdir()
    config = {"allowed_roots": [str(parent)],
              "cursor_secret": "c" * 64}
    node = NodeService(tmp_path / "node-state", config)
    try:
        created = node.save_adapter({"name": "Codex", "plugin": "codex"})
        # Security travels with each run, so no rebind operation exists.
        for operation in ("rebind_conversation", "profile_catalog", "unknown_op"):
            with pytest.raises(BridgeError) as exc:
                node.runtime_call(created["id"], operation, {
                    "arguments": {}, "expected_adapter_revision": created["revision"]})
            assert exc.value.code == "not_found"
    finally:
        node.close()
