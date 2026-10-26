"""The runtime client validates what a plugin returns, whatever the transport."""
import pytest

from workspace_bridge.codex_host_adapter import AdapterFailure, CodexHostAdapter
from workspace_bridge.node_client import NodeRuntimeAdapterProxy
from workspace_bridge.plugin_host import LocalPluginTransport
from workspace_bridge.runtime import RuntimeUnavailable
from workspace_bridge.security import BridgeError
from workspace_bridge.wbrp import Descriptor, RuntimeClient
from test_codex_host_adapter import FakeCodexRpc

ADAPTER_ID = "adapter_000000000000000000000001"


def test_descriptor_ignores_future_optional_capabilities():
    value = {"protocol": {"major": 1, "minor": 7}, "runtime": {
        "id": "codex", "instanceId": "test-instance"}, "features": {
        "models": 1, "conversations": 1, "runs": 1,
        "activities": 1, "interactions": 1, "futureFeature": 3,
        "steering": 2}}
    descriptor = Descriptor.parse(value, expected_runtime="codex")
    assert not descriptor.supports("steering")
    assert "futureFeature" not in descriptor.features


def test_client_redacts_credentials_in_plugin_payloads():
    secret = "sk-proj-" + "x" * 32
    response = RuntimeClient._scrub_secret({"message": f"rejected with {secret}",
                                            "nested": [f"echo:{secret}"]})
    assert secret not in str(response)
    assert "[REDACTED_SECRET]" in response["message"]


def test_codex_plugin_contract(tmp_path):
    projects = tmp_path / "projects"
    workspace = projects / "work"
    workspace.mkdir(parents=True)
    native = CodexHostAdapter(tmp_path / "adapter", projects, rpc=FakeCodexRpc())
    try:
        client = RuntimeClient(ADAPTER_ID, "codex", LocalPluginTransport(native))
        assert client.adapter_id == "adapter_000000000000000000000001"
        assert client.runtime_type == "codex"
        assert client.descriptor().supports("interactions")
        assert client.models("ws-one")[0]["selector"] == "gpt-test"
        profile_catalog = client.profile_catalog("ws-one", str(workspace), fresh=True)
        profile = next(row for row in profile_catalog["profiles"]
                       if row["id"] == "read-only")
        assert profile_catalog["runtimeConfig"]["supported"] is True
        assert profile_catalog["runtimeConfig"]["available"] is True
        custom = client.save_profile("reviewed-workspace", {
            "permissions": ":workspace", "approvalPolicy": "on-request",
            "approvalsReviewer": "user"}, None)
        assert any(row["id"] == custom["id"] for row in client.profiles())
        catalog = client.profile_catalog("ws-one", str(workspace), fresh=True)
        assert any(row["id"] == "workspace-write-reviewed"
                   and row["available"] is True for row in catalog["profiles"])
        assert any(row["id"] == ":workspace"
                   for row in catalog["permissionProfiles"])
        assert ("permissionProfile/list", {"cwd": str(workspace)}) in native.rpc.calls
        assert client.delete_profile(custom["id"]) == {"deleted": custom["id"]}
        conversation = client.create_conversation({
            "workspaceId": "ws-one", "directory": str(workspace),
            "securityProfile": {"id": profile["id"], "revision": profile["revision"]},
        })
        run = client.start_run(conversation["id"], {
            "input": [{"type": "text", "text": "Check"}], "model": "gpt-test"})
        assert run["phase"] == "active"
        assert client.run(run["id"])["conversationId"] == conversation["id"]
        assert client.interactions(run["id"]) == []
        assert client.activities(run["id"]) == []
        queued = client.queue(run["id"], [{"type": "text", "text": "Review next"}], "http-queue")
        assert queued["inputMessages"] == [{"requestId": "http-queue", "mode": "queue", "status": "queued"}]
        steered = client.steer(run["id"], [{"type": "text", "text": "Review now"}], "http-steer")
        assert steered["inputMessages"][-1] == {"requestId": "http-steer", "mode": "steer", "status": "delivered"}
        assert client.steer(run["id"], [{"type": "text", "text": "Review now"}], "http-steer")["inputMessages"] == steered["inputMessages"]
        native.rpc.status = "idle"
        runtime_config = catalog["runtimeConfig"]
        dynamic = client.create_conversation({
            "workspaceId": "ws-one", "directory": str(workspace),
            "securityBinding": {"source": "runtime-config",
                                "revision": runtime_config["revision"]},
        })
        assert dynamic["securityBinding"]["source"] == "runtime-config"
        dynamic_run = client.start_run(dynamic["id"], {
            "input": [{"type": "text", "text": "Follow Codex config"}]})
        assert dynamic_run["securityBinding"]["source"] == "runtime-config"
        assert dynamic_run["securityBinding"]["resolvedSummary"][
            "activePermissionProfile"] == ":workspace"
        # Optional securityRebind: same conversation across a named-profile change.
        # Use an idle conversation (the earlier runs remain active; the fake
        # RPC uses a global status, so reset it for this idle-only check).
        assert client.descriptor().supports("securityRebind")
        native.rpc.status = "idle"
        idle_conv = client.create_conversation({
            "workspaceId": "ws-one", "directory": str(workspace),
            "securityProfile": {"id": profile["id"], "revision": profile["revision"]},
        })
        target = next(row for row in client.profile_catalog(
            "ws-one", str(workspace), fresh=True)["profiles"]
                      if row["id"] == "workspace-write-reviewed")
        rebound = client.rebind_conversation(idle_conv["id"], {
            "source": "profile", "profile": {
                "id": target["id"], "revision": target["revision"]}})
        assert rebound["id"] == idle_conv["id"]
        assert rebound["status"] == "idle"
        assert rebound["securityBinding"] == {"source": "profile", "profile": {
            "id": target["id"], "revision": target["revision"]}}
        # Strict client validation: no arbitrary payload.
        import pytest as _pytest
        from workspace_bridge.security import BridgeError as _BridgeError
        with _pytest.raises(_BridgeError):
            client.rebind_conversation(idle_conv["id"], {
                "source": "profile", "profile": {"id": "x"}})
        with _pytest.raises(_BridgeError):
            client.rebind_conversation("", {
                "source": "profile", "profile": {"id": "x", "revision": "y"}})
        native.rpc.security_config = {"config": {
            "sandbox_mode": "workspace-write",
            "sandbox_workspace_write": {"network_access": True}}, "origins": {}}
        blocked = client.profile_catalog("ws-one", str(workspace), fresh=True)
        assert all(row["unavailableReason"] == "legacy-sandbox-conflict"
                   for row in blocked["profiles"])
    finally:
        native.close()


class _Failing:
    def __init__(self, message):
        self.message = message

    def create_conversation(self, body):
        raise AdapterFailure(self.message, 502, "runtime_unavailable")


def test_plugin_502_preserves_sanitized_native_detail():
    secret = "sk-proj-" + "y" * 32
    # Adapters already apply sanitize_diagnostic (paths -> [PATH]); the
    # client keeps that bounded detail and redacts credentials.
    client = RuntimeClient(ADAPTER_ID, "codex", LocalPluginTransport(
        _Failing(f"Codex thread/start failed: denied at [PATH]; echo:{secret}")))
    with pytest.raises(RuntimeUnavailable) as exc:
        client.create_conversation({"workspaceId": "ws-one"})
    assert exc.value.code == "runtime_unavailable"
    message = str(exc.value)
    assert "denied" in message and "[PATH]" in message and "[REDACTED_SECRET]" in message
    assert secret not in message and len(message) <= 500


def test_plugin_502_empty_detail_stays_generic():
    client = RuntimeClient(ADAPTER_ID, "codex", LocalPluginTransport(_Failing("")))
    with pytest.raises(RuntimeUnavailable) as exc:
        client.create_conversation({"workspaceId": "ws-one"})
    assert str(exc.value) == "Runtime adapter returned an error"


def test_node_proxy_preserves_safe_runtime_unavailable():
    safe_detail = "Codex thread/start failed: denied at [PATH]"

    class _Node:
        def runtime(self, *args, **kwargs):
            raise BridgeError(safe_detail, "runtime_unavailable")

    proxy = NodeRuntimeAdapterProxy(
        _Node(),  # type: ignore[arg-type]
        {"id": "adapter_1", "runtime_type": "codex", "revision": "rev_" + "0" * 32})
    with pytest.raises(RuntimeUnavailable) as exc:
        proxy.create_conversation({"workspaceId": "ws-one"})
    assert exc.value.code == "runtime_unavailable"
    assert "denied" in str(exc.value)
    assert "[PATH]" in str(exc.value)


def test_node_proxy_redacts_unsafe_runtime_unavailable():
    secret = "sk-proj-" + "x" * 32

    class _Node:
        def runtime(self, *args, **kwargs):
            raise BridgeError(
                f"denied at [PATH]; api_key={secret}",
                "runtime_unavailable")

    proxy = NodeRuntimeAdapterProxy(
        _Node(),  # type: ignore[arg-type]
        {"id": "adapter_1", "runtime_type": "codex", "revision": "rev_" + "0" * 32})
    with pytest.raises(RuntimeUnavailable) as exc:
        proxy.create_conversation({"workspaceId": "ws-one"})
    assert exc.value.code == "runtime_unavailable"
    message = str(exc.value)
    assert "[PATH]" in message
    assert "[REDACTED_SECRET]" in message
    assert secret not in message


def test_node_proxy_keeps_node_failures_generic():
    class _Node:
        def runtime(self, *args, **kwargs):
            raise BridgeError("node down", "node_unavailable")

    proxy = NodeRuntimeAdapterProxy(
        _Node(),  # type: ignore[arg-type]
        {"id": "adapter_1", "runtime_type": "codex", "revision": "rev_" + "0" * 32})
    with pytest.raises(RuntimeUnavailable) as exc:
        proxy.create_conversation({"workspaceId": "ws-one"})
    assert exc.value.code == "runtime_unavailable"
    assert str(exc.value) == "Runtime Node is unavailable"
