"""Input admission, receipts, and Manager/MCP transport boundaries."""
import json

import httpx
import pytest

from admin_helpers import admin_cookie
from mcp_helpers import McpTransport
from workspace_bridge.api import TOOLS, make_admin, make_mcp
from workspace_bridge.runtime import RuntimeUnavailable, RuntimeUnsupported
from workspace_bridge.security import BridgeError
from workspace_bridge.wbrp import RuntimeClient, validate_run_state
from test_run_coordinator import ADAPTER_ID, modern_env  # noqa: F401


def start(env):
    service, ws_id, token, job, _, _ = env
    run = service.call(ws_id, token, "start_run", {
        "adapter_id": ADAPTER_ID, "job_id": job["id"], "request_id": "input-run"})
    return run


@pytest.mark.parametrize("mode", ["steer", "queue"])
def test_input_tools_keep_run_identity_and_receipts(modern_env, mode):
    service, ws_id, token, _, _, rpc = modern_env
    run = start(modern_env)
    body = {"run_id": run["run_id"], "instruction": "Review the edge case.",
            "request_id": "message-1"}
    first = service.call(ws_id, token, "send_run_input", {**body, "mode": mode})
    retry = service.call(ws_id, token, "send_run_input", {**body, "mode": mode})
    assert first["run_id"] == retry["run_id"] == run["run_id"]
    assert first["conversation_id"] == run["conversation_id"]
    assert first["input_message"] == retry["input_message"]
    assert first["input_message"]["mode"] == mode
    assert first["input_messages"] == [first["input_message"]]
    assert "Review the edge case" not in json.dumps(first["input_messages"])
    if mode == "steer":
        assert sum(method == "turn/steer" for method, _ in rpc.calls) == 1
    snapshot = service.call(ws_id, token, "read_run", {"run_id": run["run_id"]})
    assert snapshot["input_capabilities"] == {"steer": True, "queue": True}
    assert snapshot["input_messages"] == first["input_messages"]


@pytest.mark.parametrize("mode", ["steer", "queue"])
def test_disabled_route_cannot_send_input(modern_env, mode):
    service, ws_id, token, _, _, rpc = modern_env
    run = start(modern_env)
    ws = service.workspace(ws_id)
    service.set_workspace_route(ws, ADAPTER_ID, False, "trusted")
    before = len(rpc.calls)
    with pytest.raises(BridgeError) as raised:
        service.call(ws_id, token, "send_run_input", {
            "run_id": run["run_id"], "instruction": "More work", "request_id": "blocked", "mode": mode})
    assert raised.value.code == "route_disabled"
    assert len(rpc.calls) == before


def test_input_rejects_changed_security_before_delivery(modern_env, monkeypatch):
    service, ws_id, token, _, _, rpc = modern_env
    run = start(modern_env)
    adapter = service.run_coordinator.adapter(ADAPTER_ID)
    original = adapter.run

    def changed(ident):
        return {**original(ident), "securityBinding": {
            "source": "profile", "profile": {"id": "read-only", "revision": "changed"}}}

    monkeypatch.setattr(adapter, "run", changed)
    with pytest.raises(BridgeError) as raised:
        service.call(ws_id, token, "send_run_input", {
            "run_id": run["run_id"], "instruction": "Correction", "request_id": "blocked", "mode": "steer"})
    assert raised.value.code == "binding_mismatch"
    assert not any(method == "turn/steer" for method, _ in rpc.calls)


async def test_only_mcp_sends_run_input(modern_env):
    service, ws_id, token, _, _, rpc = modern_env
    run = start(modern_env)
    app = make_admin(service)
    cookie = admin_cookie(app)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                base_url="http://127.0.0.1:8766",
                                headers={"Cookie": cookie}) as client:
        for mode in ("queue", "steer"):
            sent = await client.post(f"/api/runs/{run['run_id']}/{mode}", json={
                "instruction": "Then review tests.", "request_id": f"manager-{mode}"})
            assert sent.status_code in {404, 405}
    assert not any(method == "turn/steer" for method, _ in rpc.calls)
    async with httpx.AsyncClient(transport=McpTransport(make_mcp(service)),
                                base_url="http://127.0.0.1:8765",
                                headers={"X-Bridge-Token": token,
                                         "Accept": "application/json, text/event-stream"}) as client:
        sent = await client.post("/mcp", json={"jsonrpc": "2.0", "id": 1,
            "method": "tools/call", "params": {"name": "send_run_input", "arguments": {
                "workspace_id": ws_id, "run_id": run["run_id"], "instruction": "Review now.",
                "request_id": "mcp-steer", "mode": "steer"}}})
        assert sent.status_code == 200
        result = sent.json()["result"]
        assert not result["isError"]
        assert json.loads(result["content"][0]["text"])["input_message"]["status"] == "delivered"


@pytest.mark.parametrize("value", [None, {}, [None], [{"requestId": "x", "mode": "queue", "status": "done"}],
    [{"requestId": "x", "mode": [], "status": "queued"}],
    [{"requestId": "x", "mode": "queue", "status": {}}],
    [{"requestId": "x", "mode": "steer", "status": "queued"}] * 2])
def test_runtime_receipts_fail_closed(value):
    with pytest.raises(RuntimeUnavailable):
        validate_run_state({"phase": "active", "activeState": "running", "outcome": None,
                            "inputMessages": value})


def test_runtime_receipts_drop_prompt_and_internal_fields():
    result = validate_run_state({"phase": "active", "activeState": "running", "outcome": None,
        "inputMessages": [{"requestId": "one", "mode": "queue", "status": "queued",
                           "text": "private prompt", "hash": "internal"}]})
    assert result["inputMessages"] == [{"requestId": "one", "mode": "queue", "status": "queued"}]


def test_stale_receipt_snapshot_does_not_undo_delivery(modern_env):
    service, ws_id, token, _, native, _ = modern_env
    run = start(modern_env)
    service.call(ws_id, token, "send_run_input", {
        "run_id": run["run_id"], "instruction": "Correction", "request_id": "one", "mode": "steer"})
    ws = service.workspace(ws_id)
    stored = service.run_coordinator._run_row(ws, run["run_id"])
    snapshot = native.run(stored["native_id"])
    snapshot["inputMessages"][0]["status"] = "queued"
    service.run_coordinator._persist_snapshot(stored, snapshot, [], [{
        "id": "bridge-input:forged", "runId": stored["native_id"],
        "kind": "run_input", "status": "queued"}])
    receipts = service.run_coordinator.read(ws, run["run_id"], sync=False)["input_messages"]
    assert receipts == [{"requestId": "one", "mode": "steer", "status": "delivered"}]


def test_input_tools_are_scoped_mutating_open_world():
    from workspace_bridge.api import DESTRUCTIVE_TOOLS, OPEN_WORLD_TOOLS
    model, _, readonly, idempotent = TOOLS["send_run_input"]
    assert not readonly and idempotent
    assert "send_run_input" in DESTRUCTIVE_TOOLS and "send_run_input" in OPEN_WORLD_TOOLS
    assert set(model.model_json_schema()["required"]) == {
        "workspace_id", "run_id", "instruction", "request_id", "mode"}


@pytest.mark.parametrize("mode", ["queue", "steer"])
def test_legacy_adapter_without_receipts_never_receives_input(monkeypatch, mode):
    from workspace_bridge.wbrp import Descriptor
    client = RuntimeClient(ADAPTER_ID, "codex", None)
    descriptor = Descriptor.parse({"protocol": {"major": 1}, "runtime": {
        "id": "codex", "instanceId": "test"}, "features": {
        "models": 1, "conversations": 1, "runs": 1, "activities": 1, "interactions": 1,
        "steering": 1}},
        expected_runtime="codex")
    monkeypatch.setattr(client, "descriptor", lambda: descriptor)
    monkeypatch.setattr(client, "_request", lambda *a, **k: pytest.fail("unexpected POST"))
    with pytest.raises(RuntimeUnsupported):
        getattr(client, mode)("run_x", [{"type": "text", "text": "Later"}], "one")
