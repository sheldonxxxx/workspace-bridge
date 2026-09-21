"""MCP/admin surfaces for OpenCode orchestration: schemas, annotations, routes."""
import json

import httpx
import pytest

from workspace_bridge.api import Handoff, make_admin, make_mcp, TOOLS
from workspace_bridge.security import digest

from runtime_fakes import permission_event

OPENCODE_TOOLS = {"list_opencode_models", "start_opencode_run", "list_opencode_runs",
                  "read_opencode_run", "read_opencode_request", "respond_opencode_permission",
                  "cancel_opencode_run"}


def publish(agent_env, payload):
    return agent_env["service"].call(agent_env["id"], agent_env["token"], "prepare_handoff",
                                     Handoff.model_validate(payload).model_dump())


async def mcp_call(agent_env, name, arguments=None, call_id=1):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=make_mcp(agent_env["service"])),
                                 base_url="http://127.0.0.1:8765") as client:
        return await client.post("/mcp", headers={"X-Bridge-Token": agent_env["token"],
                                                  "Accept": "application/json"},
            json={"jsonrpc": "2.0", "id": call_id, "method": "tools/call",
                  "params": {"name": name, "arguments": {"workspace_id": agent_env["id"], **(arguments or {})}}})


def mcp_value(response):
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert not result["isError"], result
    return json.loads(result["content"][0]["text"])


def test_new_tool_schemas_and_annotations(agent_env):
    assert OPENCODE_TOOLS <= set(TOOLS)
    assert len(TOOLS) == 28
    for name in OPENCODE_TOOLS:
        schema = TOOLS[name][0].model_json_schema()
        assert schema["additionalProperties"] is False
        assert "workspace_id" in schema["required"]
    assert TOOLS["start_opencode_run"][2] is False and TOOLS["start_opencode_run"][3] is True
    assert TOOLS["respond_opencode_permission"][2] is False
    assert TOOLS["list_opencode_models"][2] is True


@pytest.mark.asyncio
async def test_tools_list_annotations_are_open_world(agent_env):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=make_mcp(agent_env["service"])),
                                 base_url="http://127.0.0.1:8765") as client:
        response = await client.post("/mcp", headers={"X-Bridge-Token": agent_env["token"],
                                                      "Accept": "application/json"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
    tools = {t["name"]: t for t in response.json()["result"]["tools"]}
    for name in OPENCODE_TOOLS:
        assert tools[name]["annotations"]["openWorldHint"] is True
    assert tools["start_opencode_run"]["annotations"]["destructiveHint"] is True
    assert tools["start_opencode_run"]["annotations"]["readOnlyHint"] is False
    assert tools["respond_opencode_permission"]["annotations"]["destructiveHint"] is True
    assert tools["cancel_opencode_run"]["annotations"]["readOnlyHint"] is False
    assert tools["cancel_opencode_run"]["annotations"]["destructiveHint"] is False
    assert tools["read_opencode_run"]["annotations"]["readOnlyHint"] is True
    assert "shell" not in tools and "execute" not in tools


@pytest.mark.asyncio
async def test_mcp_start_read_and_arbitrary_fields_rejected(agent_env, payload):
    job = publish(agent_env, payload)
    response = await mcp_call(agent_env, "start_opencode_run",
                              {"job_id": job["id"], "request_id": "mcp-run", "model": None})
    run = mcp_value(response)
    assert run["workspace_id"] == agent_env["id"] and run["session_id"]
    detail = mcp_value(await mcp_call(agent_env, "read_opencode_run", {"run_id": run["run_id"]}))
    assert detail["agent_evidence"] == "unverified"
    for extra in ({"prompt": "do something else"}, {"path": "/etc/passwd"}, {"command": "rm -rf /"}):
        bad = await mcp_call(agent_env, "start_opencode_run",
                             {"job_id": job["id"], "request_id": "x", **extra})
        assert bad.json()["error"]["code"] == -32602


@pytest.mark.asyncio
async def test_mcp_permission_roundtrip_on_same_session(agent_env, payload):
    job = publish(agent_env, payload)
    run = mcp_value(await mcp_call(agent_env, "start_opencode_run",
                                   {"job_id": job["id"], "request_id": "perm-run", "model": None}))
    agent_env["service"].orchestrator.handle_event(permission_event(run["session_id"], "per_mcp",
                                                                   pattern=["/Users/me/projects/**"]))
    detail = mcp_value(await mcp_call(agent_env, "read_opencode_run", {"run_id": run["run_id"]}))
    assert detail["state"] == "waiting_permission"
    assert detail["pending_requests"][0]["request_id"] == "per_mcp"
    request = mcp_value(await mcp_call(agent_env, "read_opencode_request",
                                       {"run_id": run["run_id"], "request_id": "per_mcp"}))
    assert request["pattern"] == ["/Users/me/projects/**"] and request["always_allowed"] is True
    replied = mcp_value(await mcp_call(agent_env, "respond_opencode_permission",
                                       {"run_id": run["run_id"], "request_id": "per_mcp", "decision": "always"}))
    assert replied["run_state"] == "running" and replied["decision"] == "always"
    assert agent_env["runtime"].respond_calls[-1][2] == "always"


@pytest.mark.asyncio
async def test_admin_routes_expose_safe_run_data(agent_env, payload):
    job = publish(agent_env, payload)
    run = agent_env["service"].call(agent_env["id"], agent_env["token"], "start_opencode_run",
                                    {"job_id": job["id"], "request_id": "admin-run", "model": None,
                                     "parent_run_id": None})
    token = (agent_env["state"] / "admin-token").read_text().strip()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(
            app=make_admin(agent_env["service"], agent_env["config"]["admin_token_hash"])),
            base_url="http://127.0.0.1:8766", headers={"Authorization": "Bearer " + token}) as client:
        status = (await client.get("/api/status")).json()
        assert status["open_code_integration"] is True
        assert status["opencode"]["configured"] is True and status["opencode"]["healthy"] is True
        assert "discord_configured" in status["opencode"]
        assert status["agent_execution"]["default"] == "disabled"
        serialized = json.dumps(status)
        assert "WB_DISCORD_WEBHOOK_URL" not in serialized and "WB_RUNTIME_TOKEN" not in serialized
        assert (await client.get("/api/opencode/models")).json()["scope"] == "global"
        listed = (await client.get("/api/opencode/models", params={"query": "glm"})).json()
        assert [m["selector"] for m in listed["models"]] == ["glm/zai-glm-5.2"]
        assert listed["models"][0]["enabled"] is True
        sessions = (await client.get("/api/opencode/sessions")).json()
        assert sessions["scope"] == "global"
        assert sessions["runs"][0]["run_id"] == run["run_id"]
        assert sessions["runs"][0]["workspace_name"] == "Alpha"
        runs = (await client.get(f"/api/workspaces/{agent_env['id']}/runs")).json()
        assert runs["runs"][0]["run_id"] == run["run_id"]
        detail = (await client.get(f"/api/runs/{run['run_id']}")).json()
        assert detail["session_id"] == run["session_id"]
        session = (await client.get(f"/api/runs/{run['run_id']}/session")).json()
        assert "transcript" in session and session["transcript"][0]["role"] in ("user", "assistant")
        assert (await client.get("/api/runs/run_" + "0" * 24)).status_code == 400
        assert (await client.get("/api/runs/missing")).status_code == 400


@pytest.mark.asyncio
async def test_admin_agent_toggle_settings_and_stop(agent_env, payload):
    token = (agent_env["state"] / "admin-token").read_text().strip()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(
            app=make_admin(agent_env["service"], agent_env["config"]["admin_token_hash"])),
            base_url="http://127.0.0.1:8766", headers={"Authorization": "Bearer " + token}) as client:
        disabled = await client.post(f"/api/workspaces/{agent_env['id']}",
                                     json={"operation": "set_agent_enabled", "agent_enabled": False})
        assert disabled.json()["workspace"]["agent_enabled"] == 0
        enabled = await client.post(f"/api/workspaces/{agent_env['id']}",
                                    json={"operation": "set_agent_enabled", "agent_enabled": True})
        assert enabled.json()["workspace"]["agent_enabled"] == 1
        saved = await client.post("/api/settings", json={"enabled": ["anthropic/claude-sonnet", "glm/zai-glm-5.2"],
                                                              "default": "glm/zai-glm-5.2"})
        assert saved.json()["default"] == "glm/zai-glm-5.2"
        assert saved.json()["configured"] is True
        assert (await client.get("/api/settings")).json()["model_policy"]["default"] == "glm/zai-glm-5.2"
        status_policy = (await client.get("/api/status")).json()["model_policy"]
        assert status_policy["default"] == "glm/zai-glm-5.2" and status_policy["enabled_count"] == 2
        assert (await client.post("/api/settings", json={"enabled": [], "default": "x"})).status_code == 400
        assert (await client.post("/api/settings", json={"enabled": ["anthropic/claude-sonnet"],
                                                          "default": "glm/zai-glm-5.2"})).status_code == 400
        assert (await client.post("/api/settings", json={"enabled": ["nope/missing"],
                                                          "default": "nope/missing"})).status_code == 400
    job = publish(agent_env, payload)
    run = agent_env["service"].call(agent_env["id"], agent_env["token"], "start_opencode_run",
                                    {"job_id": job["id"], "request_id": "stop-run", "model": None,
                                     "parent_run_id": None})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(
            app=make_admin(agent_env["service"], agent_env["config"]["admin_token_hash"])),
            base_url="http://127.0.0.1:8766", headers={"Authorization": "Bearer " + token}) as client:
        stopped = await client.post(f"/api/runs/{run['run_id']}/stop")
        assert stopped.json()["state"] == "cancelled"


@pytest.mark.asyncio
async def test_manager_ui_exposes_runs_and_permission_controls(agent_env):
    token = (agent_env["state"] / "admin-token").read_text().strip()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(
            app=make_admin(agent_env["service"], agent_env["config"]["admin_token_hash"])),
            base_url="http://127.0.0.1:8766", headers={"Authorization": "Bearer " + token}) as client:
        js = (await client.get("/static/app.js")).text
        html = (await client.get("/")).text
    for fragment in ("OpenCode runtime", "Pi runtime", "Manage models", "Manage Pi models",
                       "models-dialog", "policy-default", "discovery-workspace",
                       "Default model", "Save model policy",
                       "View details", "Stop", "Approve once", "Approve always", "Reject", "Run details",
                       "Agent sessions", "Checking local session", "Refresh global models"):
        assert fragment in js or fragment in html, fragment
    # Pi discovery uses an explicit workspace picker; OpenCode stays global.
    assert 'id="discovery-workspace"' in html
    assert "renderDiscoveryWorkspaces" in js
    assert "/api/runtimes/" in js
    # Compact summary on the main page; full management lives in the modal.
    assert "Policy:" in js
    assert 'id="policy-status"' in html
    assert "<dialog" in html and 'id="models-dialog"' in html
    assert "showModal" in js
    # Exactly one default <select> populated from enabled selections; no radios.
    assert 'type="radio"' not in js and "type: 'radio'" not in js
    assert "<select" in html
    assert "model-filter" in html or "model-filter" in js
    # Cancel discards drafts: the cancel handler closes without posting.
    cancel_segment = js.split("cancel-policy", 1)[1][:400]
    assert "close(" in cancel_segment and "api(" not in cancel_segment
    # Save posts enabled[] + default atomically to the runtime policy endpoint.
    assert "`/api/runtimes/${modelRuntime}/model-policy`, \"POST\", body" in js
    assert "openModels(\"opencode\")" in js or 'openModels("opencode")' in js
    assert "Enable agent" in js or "Disable agent" in js, "Agent toggle button"
    assert "model-workspace" not in js and "model-workspace" not in html, "No workspace model picker"
    assert "List models for a workspace" not in js, "No per-workspace model UX"
    assert "default-model" not in js and "save-model" not in js, "Old default-model select removed"
    assert "Save agent policy" not in js, "Old save-agent-policy control must be absent"
    assert "innerHTML" not in js
    code = "\n".join(line for line in js.splitlines() if not line.lstrip().startswith("//"))
    assert "sessionStorage." not in code and "document.cookie" not in code
    for segment in code.split("localStorage.")[1:]:
        assert segment.startswith(("setItem(", "removeItem(", "getItem(")), segment[:40]
        assert "wb-theme" in segment[:60], segment[:60]
    assert "set_agent_enabled" in js and "set_write_scope" in js
    # Each workspace action row orders Enable/Disable access, immediately
    # followed by Enable/Disable agent, before Handoffs & runs / Copy ID.
    access_at = js.index("Disable access")
    agent_at = js.index("Disable agent")
    handoffs_at = js.index("Handoffs & runs", access_at)
    assert access_at < agent_at < handoffs_at
    assert "Handoffs" not in js[access_at:agent_at]
    # Refresh bootstrap: login starts hidden behind a neutral loading state.
    assert 'id="boot"' in html and 'id="login" class="panel narrow" hidden' in html
    assert 'id="dashboard" hidden' in html
    assert "Save agent policy" not in js, "Old checkbox UI removed"
    assert "Agent execution (OpenCode)" not in js or "Agent execution (OpenCode)" not in html, "Old checkbox label removed"
    # Global agent sessions overview is a real table, not stacked cards.
    assert "<table" in html and 'aria-label="Agent sessions"' in html
    assert "<thead>" in html and '<tbody id="sessions">' in html
    for column in ("State", "Runtime", "Workspace", "Handoff", "Model", "Session", "Timing",
                   "Pending/Attention", "Notification", "Actions"):
        assert f"<th>{column}</th>" in html, column
    assert '<div id="sessions">' not in html
    session_fn = js.split("function sessionRow", 1)[1].split("async function loadSessions", 1)[0]
    assert 'node("tr")' in session_fn
    assert session_fn.count('node("td"') >= 10
    assert 'node("div", undefined, "job")' not in session_fn
    assert '"job"' not in session_fn, "Global session rows must not use .job cards"
    assert "View details" in session_fn
    assert "Stop" in session_fn and "run.active" in session_fn
    assert "pending_request_count" in session_fn or "pending" in session_fn
    assert "needs attention" in session_fn
    assert "run.runtime" in session_fn, "Global session rows must display the run runtime"
    assert 'node("td", undefined, "actions")' not in session_fn, "Actions td must remain a normal table cell"
    assert 'node("div", undefined, "actions")' in session_fn, "Actions buttons must use a nested .actions wrapper"
    assert "actionsCell.append(actions)" in session_fn, "Actions wrapper must be nested inside the td"
    assert "row.append(actionsCell)" in session_fn, "Actions td must be appended to the row"
    assert "colSpan" in js and "No Bridge-owned agent sessions yet." in js
    loader_fn = js.split("async function loadSessions", 1)[1].split("async function requestView", 1)[0]
    assert "$(\"sessions\").replaceChildren()" in loader_fn
    assert "$(\"sessions\").append(sessionRow(run))" in loader_fn
    assert '"/api/sessions?' in loader_fn or "`/api/sessions?" in loader_fn, "Sessions table uses the neutral endpoint"
    # Continued/reused runs are visibly distinguished from the summary fields,
    # using text nodes only and without adding table columns.
    assert "session_reused" in session_fn
    assert "continue_from_run_id" in session_fn
    assert "Continued from" in session_fn
    assert "reused" in session_fn
    assert session_fn.count('node("td"') >= 10, "Sessions table keeps its ten columns"
    run_fn = js.split("function runRow", 1)[1].split("async function stopRun", 1)[0]
    assert "session_reused" in run_fn
    assert "continue_from_run_id" in run_fn
    assert "Reused " in run_fn and " session" in run_fn
    assert "Reused OpenCode session" not in run_fn, "Run rows use runtime-aware wording"
    assert "OpenCode default model" not in run_fn, "Run rows use runtime-aware model wording"
    assert "run.runtime" in run_fn, "Run rows must display the run runtime"
    assert "Continued from" in run_fn


@pytest.mark.asyncio
async def test_admin_status_reports_discord_configured_without_secret(agent_env, monkeypatch):
    from workspace_bridge.notifications import DiscordNotifier
    agent_env["service"].orchestrator.notifier = DiscordNotifier(
        "https://discord.example/api/webhooks/SUPERSECRET", sleep=lambda _: None)
    token = (agent_env["state"] / "admin-token").read_text().strip()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(
            app=make_admin(agent_env["service"], agent_env["config"]["admin_token_hash"])),
            base_url="http://127.0.0.1:8766", headers={"Authorization": "Bearer " + token}) as client:
        body = (await client.get("/api/status")).text
    assert json.loads(body)["opencode"]["discord_configured"] is True
    assert "SUPERSECRET" not in body and "discord.example" not in body


@pytest.mark.asyncio
async def test_admin_status_reports_locked_adapter_without_secret(agent_env):
    agent_env["runtime"].locked = True
    token = (agent_env["state"] / "admin-token").read_text().strip()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(
            app=make_admin(agent_env["service"], agent_env["config"]["admin_token_hash"])),
            base_url="http://127.0.0.1:8766", headers={"Authorization": "Bearer " + token}) as client:
        body = (await client.get("/api/status")).text
    opencode = json.loads(body)["opencode"]
    assert opencode["locked"] is True and opencode["healthy"] is False
    assert "shared-private-token" not in body


@pytest.mark.asyncio
async def test_admin_routes_are_not_on_the_mcp_listener(agent_env):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=make_mcp(agent_env["service"])),
                                 base_url="http://127.0.0.1:8765") as client:
        for path in ("/api/status", "/api/settings", "/api/opencode/models",
                      "/api/opencode/sessions",
                      "/api/runs/run_" + "0" * 24, f"/api/workspaces/{agent_env['id']}/runs"):
            assert (await client.get(path)).status_code == 404


@pytest.mark.asyncio
async def test_manager_list_views_carry_continuation_lineage(agent_env, payload):
    """The views behind the manager sessions table and run cards expose the
    summary fields the UI renders lineage from."""
    service = agent_env["service"]
    job = publish(agent_env, payload)
    first = service.call(agent_env["id"], agent_env["token"], "start_opencode_run",
                         {"job_id": job["id"], "request_id": "lineage-first", "model": None,
                          "parent_run_id": None})
    service.orchestrator.handle_event(
        {"type": "session.idle", "session_id": first["session_id"]})
    job2 = service.call(agent_env["id"], agent_env["token"], "prepare_handoff",
                        Handoff.model_validate({**payload, "request_id": "lineage-2",
                                                "title": "Follow-up"}).model_dump())
    second = service.call(agent_env["id"], agent_env["token"], "start_opencode_run",
                          {"job_id": job2["id"], "request_id": "lineage-second", "model": None,
                           "parent_run_id": None, "continue_from_run_id": first["run_id"]})
    assert second["session_reused"] is True
    assert second["continue_from_run_id"] == first["run_id"]
    listed = service.call(agent_env["id"], agent_env["token"], "list_opencode_runs",
                          {"offset": 0, "limit": 20})["runs"]
    child = next(r for r in listed if r["run_id"] == second["run_id"])
    parent = next(r for r in listed if r["run_id"] == first["run_id"])
    assert child["session_reused"] is True
    assert child["continue_from_run_id"] == first["run_id"]
    assert child["parent_run_id"] == first["run_id"]
    assert parent["session_reused"] is False and parent["continue_from_run_id"] is None
    token = (agent_env["state"] / "admin-token").read_text().strip()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(
            app=make_admin(agent_env["service"], agent_env["config"]["admin_token_hash"])),
            base_url="http://127.0.0.1:8766", headers={"Authorization": "Bearer " + token}) as client:
        overview = (await client.get("/api/opencode/sessions?offset=0&limit=25")).json()
        rows = {r["run_id"]: r for r in overview["runs"]}
        assert rows[second["run_id"]]["session_reused"] is True
        assert rows[second["run_id"]]["continue_from_run_id"] == first["run_id"]
        assert rows[first["run_id"]]["session_reused"] is False
        detail = (await client.get(f"/api/runs/{second['run_id']}")).json()
        assert detail["session_reused"] is True
        assert detail["continue_from_run_id"] == first["run_id"]
