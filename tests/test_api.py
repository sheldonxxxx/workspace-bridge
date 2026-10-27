from admin_helpers import admin_cookie
import json
import time
import httpx
import pytest
from mcp_helpers import argument_error, McpTransport
from workspace_bridge.api import make_admin, make_mcp, TOOLS, AdminSessionStore

@pytest.fixture
def mcp(env):
    return httpx.AsyncClient(transport=McpTransport(make_mcp(env["service"])), base_url="http://127.0.0.1:8765", headers={"X-Bridge-Token": env["token"], "Accept": "application/json, text/event-stream"})

async def rpc(client, env, method, params=None, call_id=1):
    params = dict(params or {})
    if method == "tools/call" and isinstance(params.get("name"), str) and params["name"] != "list_workspaces":
        params["arguments"] = {"workspace_id": env["id"], **params.get("arguments", {})}
    return await client.post("/mcp", json={"jsonrpc": "2.0", "id": call_id, "method": method, "params": params or {}})

async def test_initialize_and_discovery(env, mcp):
    async with mcp:
        r = await rpc(mcp, env, "initialize", {"protocolVersion":"2025-11-25", "capabilities":{}, "clientInfo":{"name":"test", "version":"1"}})
        assert r.status_code == 200 and r.json()["result"]["protocolVersion"] == "2025-11-25"
        assert "mcp-session-id" not in r.headers
        r = await rpc(mcp, env, "tools/list")
        tools = r.json()["result"]["tools"]
        assert len(tools) == len(TOOLS) == 24
        assert set(t["name"] for t in tools) == set(TOOLS)
        assert all(("workspace_id" in t["inputSchema"].get("required", [])) == (t["name"] not in {"list_workspaces"}) for t in tools)
        assert not any("shell" == t["name"] for t in tools)
        schemas = {tool["name"]: tool["inputSchema"] for tool in tools}
        for name in ("list_targets", "start_run", "list_runs"):
            assert "adapter_id" in schemas[name]["properties"]
            assert "runtime" not in schemas[name]["properties"]

async def test_each_client_and_protocol_version_is_recorded_once(env, mcp):
    def recorded():
        with env["service"].lock:
            return [row["outcome"] for row in env["service"].db.execute(
                "SELECT outcome FROM events WHERE action='mcp_client' ORDER BY id")]
    async with mcp:
        for _ in range(2):
            await rpc(mcp, env, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                               "clientInfo": {"name": "Chat\nGPT <x>", "version": "9"}})
        await rpc(mcp, env, "tools/list")
    assert recorded() == ["2025-06-18 Chat?GPT ?x?", "2025-03-26 unknown"]
    assert env["token"] not in " ".join(recorded())


async def test_http_tool_read(env, mcp):
    async with mcp:
        r = await rpc(mcp, env, "tools/call", {"name":"read_file", "arguments":{"path":"src/main.py"}})
        value = json.loads(r.json()["result"]["content"][0]["text"])
        assert value["lines"][0]["line"] == 1
        assert "return a + b" in value["lines"][1]["text"]

@pytest.mark.parametrize("headers,status", [({"Origin":"https://evil.example"},403), ({"Host":"evil.example"},403), ({"X-Bridge-Token":"wrong"},401), ({"MCP-Protocol-Version":"2099-01-01"},400)])
async def test_http_boundary(env, mcp, headers, status):
    async with mcp:
        r = await mcp.post("/mcp", headers=headers, json={"jsonrpc":"2.0", "id":1, "method":"ping"})
        assert r.status_code == status

async def test_no_management_on_mcp_port(env, mcp):
    async with mcp:
        assert (await mcp.get("/api/workspaces")).status_code == 404
        assert (await mcp.get("/")).status_code == 404
        assert (await mcp.get("/mcp")).status_code == 405

async def test_unknown_and_invalid_calls(env, mcp):
    async with mcp:
        r = await rpc(mcp, env, "tools/call", {"name":"execute_shell", "arguments":{"command":"rm -rf /"}})
        assert r.json()["error"]["code"] == -32602
        r = await rpc(mcp, env, "tools/call", {"name":"read_file", "arguments":{"path":"README.md", "workspace":"other"}})
        assert "workspace: Extra inputs are not permitted" in argument_error(r.json())
        r = await rpc(mcp, env, "tools/call", {"name":"read_file", "arguments":{"path":"../secret"}})
        assert r.json()["result"]["isError"] is True

async def test_invalid_arguments_name_the_problem_without_echoing_values(env, mcp):
    secret = "sk-" + "A" * 40
    async with mcp:
        r = await rpc(mcp, env, "tools/call", {"name": "apply_patch", "arguments": {
            "workspace_id": env["id"], "message": "Note.", "changes": [{"op": secret, "path": "a.md"}]}})
        tag = argument_error(r.json())
        assert "changes.0: expected one of 'edit', 'create', 'move', 'delete'" in tag
        r = await rpc(mcp, env, "tools/call", {"name": "save_handoff", "arguments": {
            "workspace_id": env["id"], "request_id": secret + "!", "title": "t", "goal": "g",
            "plan": "p", "acceptance": "a"}})
        message = argument_error(r.json())
        assert message.startswith("Invalid tool arguments: request_id: String should match pattern")
        assert secret not in tag and secret not in message

async def test_notifications_no_write_and_invalid_batch(env, mcp):
    async with mcp:
        path = "/mcp"
        r = await mcp.post(path, json={"jsonrpc":"2.0", "method":"notifications/initialized"})
        assert r.status_code == 202 and not r.content
        # A tool call sent as a notification is acknowledged and never run.
        r = await mcp.post(path, json={"jsonrpc":"2.0", "method":"tools/call", "params":{"name":"save_handoff",
            "arguments":{"workspace_id": env["id"], "request_id":"note", "title":"t", "goal":"g", "plan":"p", "acceptance":"a"}}})
        assert r.status_code == 202 and not r.content
        r = await rpc(mcp, env, "tools/call", {"name":"list_handoffs", "arguments":{}})
        assert json.loads(r.json()["result"]["content"][0]["text"])["handoffs"] == []
        r = await mcp.post(path, json=[{"jsonrpc":"2.0", "id":1, "method":"ping"}])
        assert r.status_code == 400
        r = await mcp.post(path, content="{broken", headers={"Content-Type":"application/json"})
        assert r.json()["error"]["code"] == -32700

async def test_admin_auth_policy_and_mapping(env):
    app = make_admin(env["service"])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8766") as c:
        r = await c.get("/"); assert r.status_code == 200 and "frame-ancestors 'none'" in r.headers["content-security-policy"]
        csp = r.headers["content-security-policy"]
        assert "script-src 'self'" in csp
        assert "style-src-elem 'self' 'unsafe-inline'" in csp
        assert "style-src-attr 'unsafe-inline'" in csp
        assert '<div id="root"></div>' in r.text
        assert '/static/dist/assets/' in r.text
        asset_path = r.text.split('src="/static/dist/')[1].split('"')[0]
        assert (await c.get('/static/dist/' + asset_path)).status_code == 200
        assert (await c.get('/static/dist/assets/not-present.js')).status_code == 404
        assert (await c.get('/legacy')).status_code == 404
        assert (await c.get('/static/app.js')).status_code == 404
        assert (await c.get("/api/workspaces")).status_code == 401
        assert (await c.get("/api/diagnostics")).status_code == 401
        token = "admin"
        c.headers["Cookie"] = admin_cookie(app)
        assert (await c.get("/api/workspaces")).status_code == 200
        diagnostics = await c.get("/api/diagnostics")
        assert diagnostics.status_code == 200
        assert set(diagnostics.json()) == {
            "generated_at", "mode", "overall", "checks", "runnable_routes",
            "versions"
        }
        assert (await c.post("/api/diagnostics")).status_code == 405
        assert (await c.get("/api/status", headers={"Origin":"http://evil.local"})).status_code == 403
        other = env["parent"] / "beta"; other.mkdir()
        r = await c.post("/api/workspaces", json={"name":"Beta", "root":str(other),
                                                 "node_id": env["node_id"]})
        assert r.status_code == 201 and not r.json()["workspace"]["enabled"]
        assert (await c.get("/mcp")).status_code == 404

async def test_workspace_token_is_not_admin_token(env):
    app = make_admin(env["service"])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8766") as c:
        assert (await c.get("/api/workspaces", headers={"Authorization":"Bearer "+env["token"]})).status_code == 401

async def test_body_limits(env, mcp):
    async with mcp:
        r = await mcp.post("/mcp", content=b"a"*100000, headers={"Content-Type":"application/json"})
        assert r.status_code == 400

async def test_http_handoff_roundtrip(env, mcp, payload):
    async with mcp:
        r = await rpc(mcp, env, "tools/call", {"name":"save_handoff", "arguments":payload})
        assert not r.json()["result"]["isError"]
        job = json.loads(r.json()["result"]["content"][0]["text"])
        (env["root"] / "README.md").write_text("# Updated\n")
        r = await rpc(mcp, env, "tools/call", {"name":"read_file", "arguments":{"path":"README.md"}})
        value = json.loads(r.json()["result"]["content"][0]["text"])
        assert value["lines"][0]["text"] == "# Updated"
        r = await rpc(mcp, env, "tools/call", {"name":"list_handoffs", "arguments":{"job_id":job["id"],"document":"PROMPT.md"}})
        prompt = json.loads(r.json()["result"]["content"][0]["text"])
        assert "paste your reply" in prompt["content"]
        assert "===== TASK.md =====" in prompt["content"]
        assert job["completion_tracking"] == "not_tracked"


@pytest.mark.parametrize('name', [[], {}, None, 8])
async def test_non_string_tool_name_is_protocol_error(env, mcp, name):
    async with mcp:
        r = await rpc(mcp, env, 'tools/call', {'name': name})
        assert r.status_code == 200 and r.json()['error']['code'] == -32602


async def test_admin_login_and_session_cookie(env):
    app = make_admin(env["service"])
    token = "admin"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8766") as c:
        r = await c.post("/api/login", json={"username": "admin", "password": token})
        assert r.status_code == 200
        assert r.json()["status"] == "ok"
        cookies = r.cookies
        assert "wb-session" in cookies
        cookie_header = r.headers.get("set-cookie", "").lower()
        assert "httponly" in cookie_header
        assert "samesite=strict" in cookie_header
        assert "path=/api" in cookie_header
        assert "token" not in r.json()
        assert "secret" not in r.json()
        assert r.json()["must_change_password"] is True
        assert (await c.get("/api/status")).status_code == 403
        assert (await c.get("/api/workspaces")).status_code == 403
        changed = await c.post("/api/account/password", json={"current_password": "admin", "new_password": "new-admin-password"})
        assert changed.status_code == 200
        assert changed.json()["must_change_password"] is False
        assert (await c.get("/api/status", headers={"Cookie": f"wb-session={cookies['wb-session']}"})).status_code == 401
        r2 = await c.get("/api/status")
        assert r2.status_code == 200
        assert r2.json()["version"]
        r3 = await c.get("/api/workspaces")
        assert r3.status_code == 200


async def test_admin_login_rejects_bridge_token(env):
    app = make_admin(env["service"])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8766") as c:
        r = await c.post("/api/login", headers={"Authorization": "Bearer " + env["token"]})
        assert r.status_code == 401


async def test_admin_login_rejects_no_auth(env):
    app = make_admin(env["service"])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8766") as c:
        r = await c.post("/api/login")
        assert r.status_code == 401


async def test_admin_login_rejects_session_cookie_only(env):
    app = make_admin(env["service"])
    token = "admin"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8766") as c:
        r = await c.post("/api/login", json={"username": "admin", "password": token})
        cookies = r.cookies
        r2 = await c.post("/api/login", headers={"Cookie": f"wb-session={cookies['wb-session']}"})
        assert r2.status_code == 401


async def test_admin_logout_invalidates_session(env):
    app = make_admin(env["service"])
    token = "admin"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8766") as c:
        r = await c.post("/api/login", json={"username": "admin", "password": token})
        cookies = r.cookies
        r2 = await c.post("/api/logout", headers={"Cookie": f"wb-session={cookies['wb-session']}"})
        assert r2.status_code == 200
        logout_cookie = r2.headers.get("set-cookie", "").lower()
        assert "path=/api" in logout_cookie
        assert "wb-session" in logout_cookie
        r3 = await c.get("/api/status", headers={"Cookie": f"wb-session={cookies['wb-session']}"})
        assert r3.status_code == 401


async def test_admin_expired_session_is_401(env):
    app = make_admin(env["service"])
    token = "admin"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8766") as c:
        r = await c.post("/api/login", json={"username": "admin", "password": token})
        cookies = r.cookies
        await c.post("/api/logout", headers={"Cookie": f"wb-session={cookies['wb-session']}"})
        r2 = await c.get("/api/status", headers={"Cookie": f"wb-session={cookies['wb-session']}"})
        assert r2.status_code == 401


async def test_admin_session_ttl_expiry_deterministic():
    clock_value = 1000.0
    def fake_clock():
        return clock_value
    store = AdminSessionStore(_clock=fake_clock)
    secret = store.create()
    assert store.validate(secret) is True
    assert store.count == 1
    clock_value += store.SESSION_TTL_SECONDS + 1
    assert store.validate(secret) is False
    assert store.count == 0


async def test_admin_session_cleanup_removes_expired_entries():
    clock_value = 500.0
    def fake_clock():
        return clock_value
    store = AdminSessionStore(_clock=fake_clock)
    s1 = store.create()
    s2 = store.create()
    assert store.count == 2
    clock_value += store.SESSION_TTL_SECONDS + 1
    store._cleanup()
    assert store.count == 0
    assert store.validate(s1) is False
    assert store.validate(s2) is False


async def test_admin_unknown_session_cookie_is_401(env):
    app = make_admin(env["service"])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8766") as c:
        r = await c.get("/api/status", headers={"Cookie": "wb-session=invalid-session-value"})
        assert r.status_code == 401


async def test_admin_bearer_is_rejected(env):
    app = make_admin(env["service"])
    token = "admin"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8766") as c:
        r = await c.get("/api/status", headers={"Authorization": "Bearer " + token})
        assert r.status_code == 401


async def test_admin_session_bounded_count(env):
    from workspace_bridge.api import AdminSessionStore
    store = AdminSessionStore()
    for i in range(store.MAX_SESSIONS + 10):
        store.create()
    assert store.count <= store.MAX_SESSIONS


async def test_admin_node_reads_run_off_the_event_loop(env, mcp, payload, monkeypatch):
    """Route and handoff-document reads contact the Node; they must not block the loop."""
    import threading
    service = env["service"]
    async with mcp:
        r = await rpc(mcp, env, "tools/call", {"name": "save_handoff", "arguments": payload})
        job = json.loads(r.json()["result"]["content"][0]["text"])
    loop_thread = threading.current_thread()
    seen = {}
    for name in ("workspace_route_policy", "read_handoff"):
        original = getattr(service, name)

        def recorded(*args, _name=name, _original=original, **kwargs):
            seen[_name] = threading.current_thread()
            return _original(*args, **kwargs)

        monkeypatch.setattr(service, name, recorded)
    app = make_admin(service)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://127.0.0.1:8766") as c:
        c.headers["Cookie"] = admin_cookie(app)
        assert (await c.get(f"/api/workspaces/{env['id']}/routes")).status_code == 200
        document = await c.get(f"/api/workspaces/{env['id']}/document",
                               params={"job_id": job["id"], "document": "TASK.md"})
        assert document.status_code == 200, document.text
    assert set(seen) == {"workspace_route_policy", "read_handoff"}
    assert all(thread is not loop_thread for thread in seen.values())
