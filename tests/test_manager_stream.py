"""Manager WebSocket boundaries, revocation, recovery and bounded delivery."""
from __future__ import annotations

import asyncio
import json
import threading
from contextlib import asynccontextmanager

import httpx
import pytest

from admin_helpers import admin_cookie
from workspace_bridge import api as api_module
from workspace_bridge.api import AdminSessionStore, make_admin
from workspace_bridge.manager_events import RunEventHub
from test_run_coordinator import ADAPTER_ID, modern_env


RUN_A = "run_" + "a" * 24
RUN_B = "run_" + "b" * 24


class Socket:
    """Drive the ASGI WebSocket app without a network."""

    def __init__(self, app, cookie, headers):
        self.incoming, self.outgoing = asyncio.Queue(), asyncio.Queue()
        scope = {"type": "websocket", "asgi": {"version": "3.0", "spec_version": "2.3"},
                 "scheme": "ws", "path": "/api/ws", "raw_path": b"/api/ws",
                 "query_string": b"", "root_path": "", "subprotocols": [],
                 "server": ("127.0.0.1", 8766), "client": ("127.0.0.1", 12345),
                 "headers": [(b"host", b"127.0.0.1:8766"),
                             (b"cookie", cookie.encode()), *headers]}
        self.task = asyncio.create_task(app(scope, self.incoming.get, self.outgoing.put))

    async def first(self):
        await self.incoming.put({"type": "websocket.connect"})
        return await asyncio.wait_for(self.outgoing.get(), 2)

    async def message(self):
        frame = await asyncio.wait_for(self.outgoing.get(), 2)
        assert frame["type"] == "websocket.send", frame
        return json.loads(frame["text"])

    async def ended(self):
        """The server closed the socket; return its close code."""
        frame = await asyncio.wait_for(self.outgoing.get(), 2)
        assert frame["type"] == "websocket.close", frame
        await asyncio.wait_for(self.task, 2)
        return frame["code"]


@asynccontextmanager
async def socket(app, cookie="", headers=()):
    client = Socket(app, cookie, headers)
    try:
        yield client
    finally:
        if not client.task.done():
            await client.incoming.put({"type": "websocket.disconnect", "code": 1001})
            try:
                await asyncio.wait_for(client.task, 2)
            except TimeoutError:
                client.task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await client.task


async def opened(client):
    assert (await client.first())["type"] == "websocket.accept"
    assert await client.message() == {"type": "reset"}


async def test_hub_thread_delivery_and_resets_on_every_connect():
    hub = RunEventHub()
    first = hub.subscribe()
    assert await first.queue.get() == ("reset", None)
    thread = threading.Thread(target=lambda: (hub.publish(RUN_B), hub.publish(RUN_A),
                                              hub.publish_state(), hub.publish_state()))
    thread.start()
    thread.join()
    assert await asyncio.wait_for(first.queue.get(), 1) == ("run.changed", RUN_B)
    assert await asyncio.wait_for(first.queue.get(), 1) == ("run.changed", RUN_A)
    # A burst of state hints collapses into one reread.
    assert await asyncio.wait_for(first.queue.get(), 1) == ("state.changed", None)
    assert first.queue.empty()
    hub.unsubscribe(first)
    again = hub.subscribe()
    assert await again.queue.get() == ("reset", None)
    hub.publish("run_a\nevent: secret")
    await asyncio.sleep(0)
    assert again.queue.empty()
    hub.close()
    assert await asyncio.wait_for(again.queue.get(), 1) == ("closed", None)


async def test_hub_bounds_pending_callbacks_queues_and_client_count():
    hub = RunEventHub()
    clients = [hub.subscribe() for _ in range(hub.MAX_SUBSCRIBERS)]
    with pytest.raises(RuntimeError):
        hub.subscribe()
    # Publish on another thread while this loop is stalled: pending callbacks
    # and delivered queue entries are both bounded and overflow requests reset.
    thread = threading.Thread(target=lambda: [hub.publish(RUN_A) for _ in range(10000)])
    thread.start()
    thread.join()
    assert all(len(client._pending) <= 64 for client in clients)
    await asyncio.sleep(0)
    assert all(client.queue.qsize() <= 64 for client in clients)
    assert all(any(event[0] == "reset" for event in client.queue._queue) for client in clients)
    hub.unsubscribe(clients[0])
    replacement = hub.subscribe()
    hub.close()
    await asyncio.sleep(0)
    assert await asyncio.wait_for(replacement.queue.get(), 1) == ("closed", None)
    with pytest.raises(RuntimeError):
        hub.subscribe()


async def test_manager_socket_requires_login_password_and_boundary(env):
    app = make_admin(env["service"])
    async with socket(app) as client:
        assert (await client.first())["type"] == "websocket.close"
        await client.task
    cookie = admin_cookie(app)
    for headers in ([(b"origin", b"https://evil.example")],
                    [(b"host", b"evil.example")]):
        async with socket(app, cookie, headers) as client:
            frame = await client.first()
            assert frame == {"type": "websocket.close", "code": 1008}
    # Plain HTTP on the same path is no longer a route.
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://127.0.0.1:8766",
                                 headers={"Cookie": cookie}) as http:
        assert (await http.get("/api/run-events")).status_code == 404
        assert not env["service"].run_coordinator.run_events._subscribers


async def test_manager_socket_pushes_run_and_state_hints_then_resets_on_reconnect(env):
    service = env["service"]
    app = make_admin(service)
    cookie = admin_cookie(app)
    hub = service.run_coordinator.run_events
    async with socket(app, cookie) as client:
        await opened(client)
        hub.publish(RUN_A)
        assert await client.message() == {"type": "run.changed", "run_id": RUN_A}
        service.event(None, "test_event")
        assert await client.message() == {"type": "state.changed"}
    assert not hub._subscribers
    async with socket(app, cookie) as client:
        await opened(client)


async def test_manager_socket_closes_on_any_client_message(env):
    app = make_admin(env["service"])
    cookie = admin_cookie(app)
    async with socket(app, cookie) as client:
        await opened(client)
        await client.incoming.put({"type": "websocket.receive", "text": "hello"})
        assert await client.ended() == 1008
    assert not env["service"].run_coordinator.run_events._subscribers


@pytest.mark.parametrize("change", ["logout", "password"])
async def test_manager_socket_closes_when_the_session_is_revoked(env, monkeypatch, change):
    monkeypatch.setattr(api_module, "SOCKET_RECHECK_SECONDS", 0.02)
    service = env["service"]
    app = make_admin(service)
    cookie = admin_cookie(app)
    async with socket(app, cookie) as client:
        await opened(client)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://127.0.0.1:8766",
                                     headers={"Cookie": cookie}) as http:
            if change == "logout":
                result = await http.post("/api/logout")
            else:
                result = await http.post("/api/account/password", json={
                    "current_password": "test-admin-password", "new_password": "a-new-test-password"})
            assert result.status_code == 200
        assert await client.ended() == 1008
    assert not service.run_coordinator.run_events._subscribers


async def test_manager_socket_expires_while_idle(env, monkeypatch):
    clock = [0.0]
    original_init = AdminSessionStore.__init__
    def clocked_init(self, _clock=None):
        original_init(self, _clock=lambda: clock[0])
    monkeypatch.setattr(AdminSessionStore, "__init__", clocked_init)
    monkeypatch.setattr(api_module, "SOCKET_RECHECK_SECONDS", 0.02)
    service = env["service"]
    app = make_admin(service)
    cookie = admin_cookie(app)
    async with socket(app, cookie) as client:
        await opened(client)
        clock[0] = AdminSessionStore.SESSION_TTL_SECONDS + 1
        assert await client.ended() == 1008
    assert not service.run_coordinator.run_events._subscribers


async def test_manager_socket_limit_and_shutdown(env):
    service = env["service"]
    app = make_admin(service)
    cookie = admin_cookie(app)
    hub = service.run_coordinator.run_events
    clients = [hub.subscribe() for _ in range(hub.MAX_SUBSCRIBERS)]
    async with socket(app, cookie) as client:
        frame = await client.first()
        assert (frame["type"], frame["code"]) == ("websocket.close", 1013)
    for subscriber in clients:
        hub.unsubscribe(subscriber)
    async with socket(app, cookie) as client:
        await opened(client)
        hub.close()
        assert await client.ended() == 1008


async def test_manager_socket_hint_carries_no_run_content(modern_env):
    service, ws_id, token, job, native, _ = modern_env
    started = service.call(ws_id, token, "start_run", {
        "adapter_id": ADAPTER_ID, "job_id": job["id"], "request_id": "stream-projection"})
    run_id = started["run_id"]
    app = make_admin(service)
    cookie = admin_cookie(app)
    async with socket(app, cookie) as client:
        await opened(client)
        while not client.outgoing.empty():
            client.outgoing.get_nowait()
        native._request(99, "item/fileChange/requestApproval", {
            "threadId": "native-thread-1", "turnId": "native-turn-1",
            "reason": "Private approval body", "availableDecisions": ["accept", "decline"]})
        service.run_coordinator.reconcile(service.workspace(ws_id), run_id)
        seen = []
        while True:
            seen.append(await client.message())
            if seen[-1] == {"type": "run.changed", "run_id": run_id}:
                break
        assert all(set(item) <= {"type", "run_id"} for item in seen)
        assert "Private approval body" not in json.dumps(seen)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://127.0.0.1:8766",
                                     headers={"Cookie": cookie}) as http:
            snapshot = (await http.get(f"/api/runs/{run_id}")).json()
            assert snapshot["active_state"] == "waiting_interaction"
            assert snapshot["interactions"][0]["state"] == "pending"
