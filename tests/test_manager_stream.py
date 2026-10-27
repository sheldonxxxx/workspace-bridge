"""Manager SSE boundaries, revocation, recovery and bounded delivery."""
from __future__ import annotations

import asyncio
import threading
from contextlib import asynccontextmanager

import httpx
import pytest

from admin_helpers import admin_cookie
from starlette.requests import ClientDisconnect
from workspace_bridge.api import AdminSessionStore, make_admin
from workspace_bridge.manager_events import RunEventHub
from test_run_coordinator import ADAPTER_ID, modern_env


RUN_A = "run_" + "a" * 24
RUN_B = "run_" + "b" * 24


@asynccontextmanager
async def connection(app, cookie="", query=b"", extra_headers=()):
    """Read streaming ASGI messages without buffering an infinite response."""
    incoming, outgoing = asyncio.Queue(), asyncio.Queue()
    await incoming.put({"type": "http.request", "body": b"", "more_body": False})
    scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
             "method": "GET", "scheme": "http", "path": "/api/run-events",
             "raw_path": b"/api/run-events", "query_string": query,
             "root_path": "", "http_version": "1.1",
             "server": ("127.0.0.1", 8766), "client": ("127.0.0.1", 12345),
             "headers": [(b"host", b"127.0.0.1:8766"),
                         (b"cookie", cookie.encode()), *extra_headers]}
    task = asyncio.create_task(app(scope, incoming.get, outgoing.put))
    try:
        start = await asyncio.wait_for(outgoing.get(), 2)
        assert start["type"] == "http.response.start"
        yield start, outgoing, task
    finally:
        await incoming.put({"type": "http.disconnect"})
        try:
            await asyncio.wait_for(task, 2)
        except TimeoutError:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task


async def body(queue):
    message = await asyncio.wait_for(queue.get(), 2)
    assert message["type"] == "http.response.body"
    return message


async def test_hub_thread_delivery_filters_and_resets_on_every_connect():
    hub = RunEventHub()
    first = hub.subscribe(RUN_A)
    assert await first.queue.get() == ("reset", None)
    thread = threading.Thread(target=lambda: (hub.publish(RUN_B), hub.publish(RUN_A)))
    thread.start()
    thread.join()
    assert await asyncio.wait_for(first.queue.get(), 1) == ("run.changed", RUN_A)
    assert first.queue.empty()
    hub.unsubscribe(first)
    again = hub.subscribe(RUN_A)
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


async def test_manager_stream_requires_login_password_and_boundary(env):
    app = make_admin(env["service"])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                base_url="http://127.0.0.1:8766") as client:
        assert (await client.get("/api/run-events")).status_code == 401
        await client.post("/api/login", json={"username": "admin", "password": "admin"})
        assert (await client.get("/api/run-events")).status_code == 403
        assert (await client.get("/api/run-events", headers={"Origin": "https://evil.example"})).status_code == 403
        assert (await client.get("/api/run-events", headers={"Host": "evil.example"})).status_code == 403
        assert (await client.post("/api/run-events")).status_code == 405


async def test_manager_stream_scoping_headers_reconnect_and_disconnect(env):
    service = env["service"]
    app = make_admin(service)
    cookie = admin_cookie(app)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                base_url="http://127.0.0.1:8766", headers={"Cookie": cookie}) as client:
        assert (await client.get("/api/run-events?run_id=other")).status_code == 400
        assert (await client.get(f"/api/run-events?run_id={RUN_A}")).status_code == 404
    async with connection(app, cookie, extra_headers=((b"last-event-id", b"obsolete:999999"),)) as (start, queue, _):
        assert start["status"] == 200
        headers = dict(start["headers"])
        assert headers[b"content-type"].startswith(b"text/event-stream")
        assert headers[b"cache-control"] == b"no-store"
        assert headers[b"x-accel-buffering"] == b"no"
        assert (await body(queue))["body"] == b"event: reset\ndata: {}\n\n"
        service.run_coordinator.run_events.publish(RUN_A)
        payload = (await body(queue))["body"]
        assert payload == f'event: run.changed\ndata: {{"run_id": "{RUN_A}"}}\n\n'.encode()
    assert not service.run_coordinator.run_events._subscribers
    async with connection(app, cookie) as (_, queue, _):
        assert (await body(queue))["body"].startswith(b"event: reset\n")


@pytest.mark.parametrize("change", ["logout", "password"])
async def test_manager_stream_revokes_live_cookie_before_next_event(env, change):
    service = env["service"]
    app = make_admin(service)
    cookie = admin_cookie(app)
    async with connection(app, cookie) as (_, queue, task):
        await body(queue)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                    base_url="http://127.0.0.1:8766", headers={"Cookie": cookie}) as client:
            if change == "logout":
                result = await client.post("/api/logout")
            else:
                result = await client.post("/api/account/password", json={
                    "current_password": "test-admin-password", "new_password": "a-new-test-password"})
            assert result.status_code == 200
        service.run_coordinator.run_events.publish(RUN_A)
        assert not (await body(queue)).get("body")
        await asyncio.wait_for(task, 2)
    assert not service.run_coordinator.run_events._subscribers


async def test_manager_stream_expires_while_idle(env, monkeypatch):
    clock = [0.0]
    original_init = AdminSessionStore.__init__
    def clocked_init(self, _clock=None):
        original_init(self, _clock=lambda: clock[0])
    monkeypatch.setattr(AdminSessionStore, "__init__", clocked_init)
    service = env["service"]
    app = make_admin(service)
    cookie = admin_cookie(app)
    async with connection(app, cookie) as (_, queue, task):
        await body(queue)
        clock[0] = AdminSessionStore.SESSION_TTL_SECONDS + 1
        assert not (await body(queue)).get("body")
        await asyncio.wait_for(task, 2)
    assert not service.run_coordinator.run_events._subscribers


async def test_manager_stream_limit_and_shutdown(env):
    service = env["service"]
    app = make_admin(service)
    cookie = admin_cookie(app)
    hub = service.run_coordinator.run_events
    clients = [hub.subscribe() for _ in range(hub.MAX_SUBSCRIBERS)]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                base_url="http://127.0.0.1:8766", headers={"Cookie": cookie}) as client:
        assert (await client.get("/api/run-events")).status_code == 503
    for subscriber in clients:
        hub.unsubscribe(subscriber)
    async with connection(app, cookie) as (_, queue, task):
        await body(queue)
        hub.close()
        assert not (await body(queue)).get("body")
        await asyncio.wait_for(task, 2)


async def test_manager_stream_cancellation_during_headers_releases_slot(env):
    service = env["service"]
    app = make_admin(service)
    cookie = admin_cookie(app)
    scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
             "method": "GET", "scheme": "http", "path": "/api/run-events",
             "raw_path": b"/api/run-events", "query_string": b"", "root_path": "",
             "http_version": "1.1", "server": ("127.0.0.1", 8766),
             "client": ("127.0.0.1", 12345),
             "headers": [(b"host", b"127.0.0.1:8766"), (b"cookie", cookie.encode())]}
    async def receive():
        await asyncio.Future()
    async def cancel_send(message):
        raise OSError("Client disconnected during headers")
    with pytest.raises(ClientDisconnect):
        await app(scope, receive, cancel_send)
    assert not service.run_coordinator.run_events._subscribers


async def test_manager_stream_bound_snapshot_change_and_unchanged_read(modern_env):
    service, ws_id, token, job, native, _ = modern_env
    started = service.call(ws_id, token, "start_agent_run", {
        "adapter_id": ADAPTER_ID, "job_id": job["id"], "request_id": "stream-projection"})
    run_id = started["run_id"]
    app = make_admin(service)
    cookie = admin_cookie(app)
    async with connection(app, cookie, query=f"run_id={run_id}".encode()) as (_, queue, _):
        await body(queue)
        service.run_coordinator.run_events.publish(RUN_B)
        native._request(99, "item/fileChange/requestApproval", {
            "threadId": "native-thread-1", "turnId": "native-turn-1",
            "reason": "Private approval body", "availableDecisions": ["accept", "decline"]})
        service.run_coordinator.reconcile(service.workspace(ws_id), run_id)
        event = (await body(queue))["body"]
        assert event == f'event: run.changed\ndata: {{"run_id": "{run_id}"}}\n\n'.encode()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                    base_url="http://127.0.0.1:8766", headers={"Cookie": cookie}) as client:
            snapshot = (await client.get(f"/api/runs/{run_id}")).json()
            assert snapshot["active_state"] == "waiting_interaction"
            assert snapshot["interactions"][0]["state"] == "pending"
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(queue.get(), 0.05)
