"""The Bridge client speaks the private protocol over a real HTTP listener."""
import socket
import threading
import time

import uvicorn

from workspace_bridge.codex_host_adapter import CodexHostAdapter, make_app
from workspace_bridge.wbrp import Descriptor, HttpRuntimeAdapter
from test_codex_host_adapter import FakeCodexRpc


def test_descriptor_ignores_future_optional_capabilities():
    value = {"protocol": {"major": 1, "minor": 7}, "runtime": {
        "id": "codex", "instanceId": "test-instance"}, "features": {
        "models": 1, "conversations": 1, "runs": 1,
        "activities": 1, "interactions": 1, "futureFeature": 3,
        "steering": 2}}
    descriptor = Descriptor.parse(value, expected_runtime="codex")
    assert not descriptor.supports("steering")
    assert "futureFeature" not in descriptor.features


def test_http_adapter_scrubs_its_token_from_remote_payloads():
    token = "adapter-token-should-not-escape"
    client = HttpRuntimeAdapter("adapter_000000000000000000000001", "pi",
                                "http://127.0.0.1:8780", token)
    response = client._scrub_secret({"message": f"rejected with {token}",
                                     "nested": [f"echo:{token}"]})
    assert token not in str(response)
    assert response["message"] == "rejected with [REDACTED_SECRET]"


def test_codex_http_adapter_contract(tmp_path):
    projects = tmp_path / "projects"
    workspace = projects / "work"
    workspace.mkdir(parents=True)
    native = CodexHostAdapter(tmp_path / "adapter", projects, rpc=FakeCodexRpc())
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(make_app(native, "secret"),
        host="127.0.0.1", port=port, log_level="error", access_log=False))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        for _ in range(100):
            if server.started:
                break
            time.sleep(0.01)
        assert server.started
        client = HttpRuntimeAdapter("adapter_000000000000000000000001", "codex",
                                    f"http://127.0.0.1:{port}", "secret")
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
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        native.close()
