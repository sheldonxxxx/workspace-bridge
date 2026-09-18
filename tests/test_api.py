import json
import httpx
import pytest
from workspace_bridge.api import make_admin, make_mcp, TOOLS

@pytest.fixture
def mcp(env):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=make_mcp(env["service"])), base_url="http://127.0.0.1:8765", headers={"X-Bridge-Token": env["token"], "Accept": "application/json, text/event-stream"})

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
        assert len(tools) == 12 and set(t["name"] for t in tools) == set(TOOLS)
        assert all(("workspace_id" in t["inputSchema"].get("required", [])) == (t["name"] not in {"list_workspaces", "read_project_lead_skill"}) for t in tools)
        assert not any("shell" == t["name"] for t in tools)

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
        assert r.json()["error"]["code"] == -32602
        r = await rpc(mcp, env, "tools/call", {"name":"read_file", "arguments":{"path":"../secret"}})
        assert r.json()["result"]["isError"] is True

async def test_notifications_no_write_and_invalid_batch(env, mcp):
    async with mcp:
        path = "/mcp"
        r = await mcp.post(path, json={"jsonrpc":"2.0", "method":"notifications/initialized"})
        assert r.status_code == 202 and not r.content
        r = await mcp.post(path, json={"jsonrpc":"2.0", "method":"tools/call", "params":{"name":"prepare_handoff"}})
        assert r.status_code == 400
        r = await mcp.post(path, json=[{"jsonrpc":"2.0", "id":1, "method":"ping"}])
        assert r.status_code == 400
        r = await mcp.post(path, content="{broken", headers={"Content-Type":"application/json"})
        assert r.json()["error"]["code"] == -32700

async def test_admin_auth_policy_and_mapping(env):
    app = make_admin(env["service"], env["config"]["admin_token_hash"])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8766") as c:
        r = await c.get("/"); assert r.status_code == 200 and "frame-ancestors 'none'" in r.headers["content-security-policy"]
        assert (await c.get("/api/workspaces")).status_code == 401
        token = (env["state"] / "admin-token").read_text().strip()
        c.headers["Authorization"] = "Bearer " + token
        assert (await c.get("/api/workspaces")).status_code == 200
        assert (await c.get("/api/status", headers={"Origin":"http://evil.local"})).status_code == 403
        other = env["parent"] / "beta"; other.mkdir()
        r = await c.post("/api/workspaces", json={"name":"Beta", "root":str(other)})
        assert r.status_code == 201 and not r.json()["workspace"]["enabled"]
        assert (await c.get("/mcp")).status_code == 404

async def test_workspace_token_is_not_admin_token(env):
    app = make_admin(env["service"], env["config"]["admin_token_hash"])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8766") as c:
        assert (await c.get("/api/workspaces", headers={"Authorization":"Bearer "+env["token"]})).status_code == 401

async def test_body_limits(env, mcp):
    async with mcp:
        r = await mcp.post("/mcp", content=b"a"*100000, headers={"Content-Type":"application/json"})
        assert r.status_code == 400

async def test_http_handoff_roundtrip(env, mcp, payload):
    async with mcp:
        r = await rpc(mcp, env, "tools/call", {"name":"prepare_handoff", "arguments":payload})
        assert not r.json()["result"]["isError"]
        job = json.loads(r.json()["result"]["content"][0]["text"])
        (env["root"] / "README.md").write_text("# Updated\n")
        r = await rpc(mcp, env, "tools/call", {"name":"read_file", "arguments":{"path":"README.md"}})
        value = json.loads(r.json()["result"]["content"][0]["text"])
        assert value["lines"][0]["text"] == "# Updated"
        r = await rpc(mcp, env, "tools/call", {"name":"read_handoff", "arguments":{"job_id":job["id"],"document":"TASK.md"}})
        task = json.loads(r.json()["result"]["content"][0]["text"])
        assert "paste your reply" in task["content"]
        assert job["completion_tracking"] == "not_tracked"


@pytest.mark.parametrize('name', [[], {}, None, 8])
async def test_non_string_tool_name_is_protocol_error(env, mcp, name):
    async with mcp:
        r = await rpc(mcp, env, 'tools/call', {'name': name})
        assert r.status_code == 200 and r.json()['error']['code'] == -32602
