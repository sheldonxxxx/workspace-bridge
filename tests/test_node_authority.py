from __future__ import annotations

import sqlite3

import pytest

from workspace_bridge.node_service import NodeService
from workspace_bridge.security import BridgeError, digest

from conftest import create_test_adapter, start_test_node


def test_read_only_node_service_does_not_create_or_mutate_state(tmp_path):
    parent = tmp_path / "projects"
    parent.mkdir()
    state = tmp_path / "node-state"
    config = {"allowed_roots": [str(parent)],
              "node_token_hash": digest(b"node-token"), "host": "127.0.0.1"}

    writable = NodeService(state, config)
    writable.save_adapter({
        "name": "Local adapter", "runtime_type": "pi",
        "base_url": "http://127.0.0.1:8780", "token": "adapter-secret",
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
                "name": "Should fail", "runtime_type": "pi",
                "base_url": "http://127.0.0.1:8781", "token": "secret",
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
        "name": "Node A adapter", "runtime_type": "pi",
        "base_url": "http://127.0.0.1:8780", "token": "node-a-secret",
    }, collision_id)

    node_b = start_test_node(env["tmp"] / "node-b-state", env["parent"])
    try:
        record_b = service.node_registry.create({
            "name": "Node B", "base_url": node_b["url"],
            "token": node_b["token"], "enabled": False,
        })
        service._node_transport[record_b["id"]] = node_b["transport"]
        service._node_transport_services[record_b["id"]] = node_b["service"]
        created = node_b["service"].save_adapter({
            "name": "Node B conflicting adapter", "runtime_type": "pi",
            "base_url": "http://127.0.0.1:8782", "token": "node-b-secret",
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
        public = next(item for item in service.node_registry.list_public(probe=True)
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
              "node_token_hash": digest(b"node-token"), "host": "127.0.0.1"}
    node = NodeService(state, config)
    try:
        created = node.save_adapter({
            "name": "Revision test", "runtime_type": "pi",
            "base_url": "http://127.0.0.1:8780", "token": "old-secret",
        })
        old_revision = created["revision"]
        updated = node.save_adapter({
            "name": "Revision test", "runtime_type": "pi",
            "base_url": "http://127.0.0.1:8781", "token": "new-secret",
        }, created["id"])
        assert updated["revision"] != old_revision

        constructed = []

        def unexpected_native(*args, **kwargs):
            constructed.append((args, kwargs))
            raise AssertionError("stale runtime call constructed a native client")

        monkeypatch.setattr("workspace_bridge.node_service.HttpRuntimeAdapter",
                            unexpected_native)
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


def test_runtime_rebind_conversation_validates_and_forwards(tmp_path, monkeypatch):
    from workspace_bridge.node_service import NodeService
    from workspace_bridge.security import BridgeError, digest
    parent = tmp_path / "projects"
    parent.mkdir()
    state = tmp_path / "node-state"
    config = {"allowed_roots": [str(parent)],
              "node_token_hash": digest(b"node-token"), "host": "127.0.0.1"}
    node = NodeService(state, config)
    try:
        created = node.save_adapter({
            "name": "Rebind test", "runtime_type": "codex",
            "base_url": "http://127.0.0.1:8780", "token": "secret",
        })
        # Invalid bindings fail before any native rebind call (no arbitrary payload).
        rebound_calls = []
        class _GuardedNative:
            def __init__(self, *args, **kwargs):
                pass
            def rebind_conversation(self, conversation_id, binding):
                rebound_calls.append((conversation_id, binding))
                raise AssertionError("invalid rebind reached the native client")
        monkeypatch.setattr("workspace_bridge.node_service.HttpRuntimeAdapter", _GuardedNative)
        with __import__("pytest").raises(BridgeError) as exc:
            node.runtime_call(created["id"], "rebind_conversation", {
                "arguments": {"conversation_id": "conv_1",
                              "security_binding": {"source": "profile",
                                                   "profile": {"id": "a"}}},
                "expected_adapter_revision": created["revision"]})
        assert exc.value.code == "invalid_arguments"
        assert rebound_calls == []
        with __import__("pytest").raises(BridgeError) as exc2:
            node.runtime_call(created["id"], "rebind_conversation", {
                "arguments": {"conversation_id": "",
                              "security_binding": {"source": "profile",
                                                   "profile": {"id": "a", "revision": "r"}}},
                "expected_adapter_revision": created["revision"]})
        assert exc2.value.code == "invalid_arguments"
        assert rebound_calls == []
        # Unknown operation remains unknown.
        with __import__("pytest").raises(BridgeError) as exc3:
            node.runtime_call(created["id"], "unknown_op", {
                "arguments": {}, "expected_adapter_revision": created["revision"]})
        assert exc3.value.code == "not_found"
        # Valid binding forwards to the native client with no extra payload.
        forwarded = {}
        class _FakeNative:
            def __init__(self, *args, **kwargs):
                pass
            def rebind_conversation(self, conversation_id, binding):
                forwarded["conversation_id"] = conversation_id
                forwarded["binding"] = binding
                return {"id": conversation_id, "status": "idle",
                        "securityBinding": binding}
        monkeypatch.setattr("workspace_bridge.node_service.HttpRuntimeAdapter", _FakeNative)
        result = node.runtime_call(created["id"], "rebind_conversation", {
            "arguments": {"conversation_id": "conv_123",
                          "security_binding": {"source": "profile",
                                               "profile": {"id": "read-only",
                                                           "revision": "rev-1"}}},
            "expected_adapter_revision": created["revision"]})
        assert result["id"] == "conv_123"
        assert forwarded["binding"] == {"source": "profile",
                                        "profile": {"id": "read-only",
                                                    "revision": "rev-1"}}
    finally:
        node.close()
