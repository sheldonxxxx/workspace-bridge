"""Native event reads preserve Node admission and asynchronous responsiveness."""
from __future__ import annotations

import asyncio
import threading

import httpx
import pytest

from conftest import patch_node_runtime

from workspace_bridge.node_api import make_node_api
from workspace_bridge.node_service import NodeService
from workspace_bridge.security import digest


@pytest.fixture
def event_node(tmp_path):
    parent = tmp_path / "projects"
    parent.mkdir()
    token = "event-node-token"
    node = NodeService(tmp_path / "node-state", {
        "allowed_roots": [str(parent)], "node_token_hash": digest(token.encode()),
        "host": "127.0.0.1",
    })
    adapter = node.save_adapter({
        "name": "Event adapter", "plugin": "codex",
    })
    try:
        yield node, adapter, token
    finally:
        node.close()


async def test_pending_event_read_does_not_block_node_status(event_node, monkeypatch):
    node, adapter, token = event_node
    entered = threading.Event()
    release = threading.Event()
    forwarded = []

    class BlockingRuntime:
        def __init__(self, *args, **kwargs):
            pass

        def events(self, *, after, wait_ms):
            forwarded.append((after, wait_ms))
            entered.set()
            assert release.wait(5), "event read was not released"
            return {"instanceId": "native-instance", "cursor": after, "events": []}

    patch_node_runtime(monkeypatch, BlockingRuntime)
    app = make_node_api(node, digest(token.encode()))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://node-test.invalid",
                                 headers={"X-Node-Token": token}) as client:
        pending = asyncio.create_task(client.post(
            f"/v1/runtime/{adapter['id']}/events", json={
                "arguments": {"after": 17, "wait_ms": 25000},
                "expected_adapter_revision": adapter["revision"],
            }))
        try:
            assert await asyncio.wait_for(asyncio.to_thread(entered.wait, 1), 2)
            assert not pending.done(), "event read completed before the status probe"
            status = await asyncio.wait_for(client.get("/v1/status"), 1)
            assert status.status_code == 200
            assert status.json()["status"] == "ok"
            assert not pending.done(), "status waited for the event read to finish"
        finally:
            release.set()
            response = await asyncio.wait_for(pending, 2)
        assert response.status_code == 200, response.text
        assert response.json() == {"instanceId": "native-instance", "cursor": 17, "events": []}
        assert forwarded == [(17, 25000)]


async def test_event_read_keeps_node_token_and_revision_validation(event_node, monkeypatch):
    node, adapter, token = event_node
    constructed = []

    def unexpected_native(*args, **kwargs):
        constructed.append((args, kwargs))
        raise AssertionError("rejected event request constructed a native client")

    patch_node_runtime(monkeypatch, unexpected_native)
    app = make_node_api(node, digest(token.encode()))
    path = f"/v1/runtime/{adapter['id']}/events"
    body = {"arguments": {"after": 0, "wait_ms": 25000},
            "expected_adapter_revision": adapter["revision"]}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://node-test.invalid") as client:
        denied = await client.post(path, json=body,
                                   headers={"X-Node-Token": "wrong-token"})
        assert denied.status_code == 401
        missing = await client.post(path, json={"arguments": body["arguments"]},
                                    headers={"X-Node-Token": token})
        assert missing.status_code == 400
        assert missing.json()["code"] == "invalid_arguments"
        node.save_adapter({"settings": {"log_level": "DEBUG"}}, adapter["id"])
        stale = await client.post(path, json=body, headers={"X-Node-Token": token})
        assert stale.status_code == 400
        assert stale.json()["code"] == "adapter_changed"
        assert constructed == []
