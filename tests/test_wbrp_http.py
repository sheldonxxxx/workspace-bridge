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
        client = HttpRuntimeAdapter("codex", f"http://127.0.0.1:{port}", "secret")
        assert client.descriptor().supports("interactions")
        assert client.models("ws-one")[0]["selector"] == "gpt-test"
        profile = client.profiles()[0]
        custom = client.save_profile("reviewed-workspace", {
            "sandbox": "workspace-write", "approvalPolicy": "on-request",
            "approvalsReviewer": "user"}, None)
        assert any(row["id"] == custom["id"] for row in client.profiles())
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
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        native.close()
