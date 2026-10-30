"""Native event reads preserve Node admission and asynchronous responsiveness."""
from __future__ import annotations

import asyncio
import json
import threading

import pytest

from conftest import patch_node_runtime

from workspace_bridge import node_api, node_link
from workspace_bridge.node_service import NodeService


@pytest.fixture
def event_node(tmp_path):
    parent = tmp_path / "projects"
    parent.mkdir()
    node = NodeService(tmp_path / "node-state", {
        "allowed_roots": [str(parent)], "cursor_secret": "c" * 64,
    })
    adapter = node.save_adapter({
        "name": "Event adapter", "plugin": "codex",
    })
    try:
        yield node, adapter
    finally:
        node.close()


class FakeLink:
    """The Bridge side of a paired link: frames in, responses out."""

    def __init__(self):
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.responses: dict[int, dict] = {}
        self.arrived = asyncio.Event()

    def request(self, ident, method, path, body=None):
        self.inbox.put_nowait(json.dumps({"type": "request", "id": ident, "method": method,
                                          "path": path, "body": body}))

    def __aiter__(self):
        return self

    async def __anext__(self):
        message = await self.inbox.get()
        if message is None:
            raise StopAsyncIteration
        return message

    async def send(self, text):
        frame = json.loads(text)
        self.responses[frame["id"]] = frame
        self.arrived.set()

    async def response(self, ident, timeout):
        async def wait():
            while ident not in self.responses:
                self.arrived.clear()
                await self.arrived.wait()
            return self.responses[ident]
        return await asyncio.wait_for(wait(), timeout)


async def test_pending_event_read_does_not_block_node_status(event_node, monkeypatch):
    node, adapter = event_node
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
    link = FakeLink()
    serving = asyncio.create_task(node_link._serve(link, node))
    try:
        link.request(1, "POST", f"/v1/runtime/{adapter['id']}/events", {
            "arguments": {"after": 17, "wait_ms": 25000},
            "expected_adapter_revision": adapter["revision"],
        })
        assert await asyncio.wait_for(asyncio.to_thread(entered.wait, 1), 2)
        link.request(2, "GET", "/v1/status")
        status = await link.response(2, 1)
        assert status["status"] == 200
        assert status["body"]["status"] == "ok"
        assert 1 not in link.responses, "status waited for the event read to finish"
    finally:
        release.set()
    response = await link.response(1, 2)
    link.inbox.put_nowait(None)
    await asyncio.wait_for(serving, 2)
    assert response["status"] == 200, response
    assert response["body"] == {"instanceId": "native-instance", "cursor": 17, "events": []}
    assert forwarded == [(17, 25000)]


def test_event_read_keeps_revision_validation(event_node, monkeypatch):
    node, adapter = event_node
    constructed = []

    def unexpected_native(*args, **kwargs):
        constructed.append((args, kwargs))
        raise AssertionError("rejected event request constructed a native client")

    patch_node_runtime(monkeypatch, unexpected_native)
    path = f"/v1/runtime/{adapter['id']}/events"
    body = {"arguments": {"after": 0, "wait_ms": 25000},
            "expected_adapter_revision": adapter["revision"]}

    def post(payload):
        status, raw = node_api.handle(node, "POST", path, json.dumps(payload).encode())
        return status, json.loads(raw)

    status, missing = post({"arguments": body["arguments"]})
    assert status == 400 and missing["code"] == "invalid_arguments"
    node.save_adapter({"settings": {"log_level": "DEBUG"}}, adapter["id"])
    status, stale = post(body)
    assert status == 400 and stale["code"] == "adapter_changed"
    assert constructed == []
