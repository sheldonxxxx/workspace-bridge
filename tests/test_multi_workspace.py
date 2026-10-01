from admin_helpers import admin_cookie
import asyncio
import json
from pathlib import Path

import httpx
import pytest

from workspace_bridge.api import make_admin, make_mcp, TOOLS, Handoff
from workspace_bridge.security import BridgeError
from workspace_bridge.service import Service


def add(env, name="Beta", enabled=True):
    root = env["parent"] / name.lower()
    root.mkdir()
    (root / "README.md").write_text("# " + name + "\n")
    result = env["service"].add_workspace(name, str(root), [])
    assert "token" not in result
    ident = result["workspace"]["id"]
    if enabled:
        env["service"].manage_workspace(ident, "enable")
    return ident, root


async def request(env, name, args=None, *, token=None, route="/mcp"):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=make_mcp(env["service"])),
                                 base_url="http://127.0.0.1:8765") as c:
        return await c.post(route, headers={"X-Bridge-Token": token or env["token"], "Accept": "application/json"},
                            json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                  "params": {"name": name, "arguments": args or {}}})


def value(response):
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert not result["isError"], result
    return json.loads(result["content"][0]["text"])


async def test_single_route_many_workspaces(env):
    beta, _ = add(env)
    hidden, _ = add(env, "Hidden", enabled=False)
    listed = value(await request(env, "list_workspaces"))
    assert {x["workspace_id"] for x in listed["workspaces"]} == {env["id"], beta}
    assert "Hidden" not in json.dumps(listed) and hidden not in json.dumps(listed)
    assert all("root" not in x and "token_hash" not in x for x in listed["workspaces"])
    for ident, name in [(env["id"], "Alpha"), (beta, "Beta")]:
        content = value(await request(env, "read_file", {"workspace_id": ident, "path": "README.md"}))
        assert content["workspace_id"] == ident and content["lines"][0]["text"] == "# " + name
    assert (await request(env, "workspace_info", route="/mcp/" + beta)).status_code == 404


async def test_workspace_id_is_required_even_with_one_mapping(env):
    r = await request(env, "read_file", {"path": "README.md"})
    assert r.json()["error"]["code"] == -32602
    for name, (model, _, _, _) in TOOLS.items():
        assert ("workspace_id" in model.model_json_schema().get("required", [])) == (name not in {"list_workspaces", "read_project_lead_skill"})


async def test_new_mapping_needs_no_reconnection_or_new_token(env):
    before = value(await request(env, "list_workspaces"))["total"]
    new, _ = add(env)
    after = value(await request(env, "list_workspaces"))["total"]
    assert after == before + 1
    assert value(await request(env, "workspace_info", {"workspace_id": new}))["name"] == "Beta"


async def test_interleaved_calls_never_switch_active_workspace(env):
    beta, _ = add(env)
    tasks = [request(env, "read_file", {"workspace_id": ident, "path": "README.md"})
             for ident in [env["id"], beta] * 12]
    results = await asyncio.gather(*tasks)
    assert [value(r)["lines"][0]["text"] for r in results] == ["# Alpha", "# Beta"] * 12


async def test_disabled_workspace_denied_without_breaking_other_workspaces(env):
    beta, _ = add(env)
    env["service"].manage_workspace(env["id"], "disable")
    r = await request(env, "workspace_info", {"workspace_id": env["id"]})
    assert r.json()["result"]["isError"]
    assert value(await request(env, "workspace_info", {"workspace_id": beta}))["name"] == "Beta"


async def test_bridge_pause_resume_rotation_and_old_header_rejection(env):
    s = env["service"]
    s.manage_bridge("disable")
    assert (await request(env, "list_workspaces")).status_code == 401
    s.manage_bridge("enable")
    assert value(await request(env, "list_workspaces"))["total"] == 1
    rotated = s.manage_bridge("rotate_token")["token"]
    assert (await request(env, "list_workspaces")).status_code == 401
    assert value(await request(env, "list_workspaces", token=rotated))["total"] == 1
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=make_mcp(s)), base_url="http://127.0.0.1:8765") as c:
        r = await c.post("/mcp", headers={"X-Workspace-Token": rotated, "Accept": "application/json"},
                         json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
        assert r.status_code == 401


async def test_admin_and_bridge_credentials_are_not_interchangeable(env):
    app = make_admin(env["service"])
    admin = admin_cookie(app)
    assert (await request(env, "list_workspaces", token=admin)).status_code == 401
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://127.0.0.1:8766") as c:
        assert (await c.get("/api/bridge", headers={"Authorization": "Bearer " + env["token"]})).status_code == 401
        c.headers["Cookie"] = admin
        result = (await c.post("/api/bridge", json={"operation": "rotate_token"})).json()
        assert result["bridge"]["endpoint"] == "/mcp" and result["token"]
        assert "token" not in (await c.get("/api/bridge")).json()
        assert (await c.post("/api/workspaces/" + env["id"], json={"operation": "rotate_token"})).status_code == 400


async def test_duplicate_bridge_header_rejected(env):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=make_mcp(env["service"])), base_url="http://127.0.0.1:8765") as c:
        r = await c.post("/mcp", headers=[("X-Bridge-Token", env["token"]), ("X-Bridge-Token", "wrong")], json={})
        assert r.status_code == 403


def test_cross_project_handoff_id_rejected(env, payload):
    s = env["service"]
    job = s.call(env["id"], env["token"], "prepare_handoff", Handoff.model_validate(payload).model_dump())
    beta, _ = add(env)
    with pytest.raises(BridgeError, match="not found"):
        s.call(beta, env["token"], "read_handoff", {"job_id": job["id"], "document": "TASK.md", "start_line": 1, "max_lines": 40})



async def test_read_offsets_hashes_and_metadata(env):
    args = {"workspace_id": env["id"], "path": "src/main.py", "offset": 2, "limit": 1}
    read = value(await request(env, "read_file", args))
    assert read["lines"] == [{"line": 2, "text": "    return a + b"}]
    assert read["next_offset"] is None and "next_line" not in read
    (env["root"] / "src/main.py").write_text("changed\n")
    r = await request(env, "read_file", {**args, "expected_sha256": read["sha256"]})
    assert r.json()["result"]["isError"]


@pytest.mark.parametrize("args", [{"offset": 0}, {"offset": True}, {"limit": 401}, {"path": "../beta/README.md"},
                                  {"path": "/etc/passwd"}, {"path": ".workspace-handoff/jobs/task.md"}])
async def test_read_boundaries(env, args):
    r = await request(env, "read_file", {"workspace_id": env["id"], "path": "README.md", **args})
    body = r.json()
    assert "error" in body or body["result"]["isError"]


async def test_discovery_pagination(env):
    add(env)
    first = value(await request(env, "list_workspaces", {"limit": 1}))
    second = value(await request(env, "list_workspaces", {"offset": first["next_offset"], "limit": 1}))
    assert first["workspaces"][0]["workspace_id"] != second["workspaces"][0]["workspace_id"]
    assert second["next_offset"] is None
