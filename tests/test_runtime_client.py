"""The runtime client validates what a plugin returns, whatever the transport."""
import pytest

from workspace_bridge import security_profiles as sp
from workspace_bridge.codex_host_adapter import AdapterFailure, CodexHostAdapter
from workspace_bridge.node_client import NodeRuntimeAdapterProxy
from workspace_bridge.plugin_host import LocalPluginTransport
from workspace_bridge.runtime import RuntimeUnavailable
from workspace_bridge.security import BridgeError
from workspace_bridge.wbrp import Descriptor, RuntimeClient
from test_codex_host_adapter import FakeCodexRpc

SECURITY = sp.wire("trusted", sp.BUILTINS["trusted"])
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
        conversation = client.create_conversation({
            "workspaceId": "ws-one", "directory": str(workspace),
            "security": SECURITY,
        })
        assert conversation["securityBinding"] == {"source": "profile", "profile": {
            "id": "trusted", "revision": SECURITY["profile"]["revision"]}}
        run = client.start_run(conversation["id"], {
            "security": SECURITY, "input": [{"type": "text", "text": "Check"}], "model": "gpt-test"})
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
        native_conversation = client.create_conversation({
            "workspaceId": "ws-one", "directory": str(workspace),
            "security": {"source": "native"}})
        assert native_conversation["securityBinding"] == {"source": "native"}
        native_run = client.start_run(native_conversation["id"], {
            "security": {"source": "native"},
            "input": [{"type": "text", "text": "Follow Codex config"}]})
        assert native_run["securityBinding"] == {"source": "native"}
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
