"""Skill delivery and trust boundary, not a behavioral evaluation of an LLM."""
from hashlib import sha256
from pathlib import Path
import json

import httpx
import pytest

from workspace_bridge import __version__
from workspace_bridge.api import Handoff, INSTRUCTIONS, TOOLS, make_mcp
from workspace_bridge.embedded_skill import SKILL_TOOL, read_project_lead_skill, skill_hint
from workspace_bridge.protocol import LEGACY, MODERN, PREFIX
from workspace_bridge.security import BridgeError, MAX_OUTPUT
from workspace_bridge.service import Service


async def rpc(env, method="tools/call", params=None, *, version=LEGACY[-1], token=None):
    params = dict(params) if params is not None else {"name": SKILL_TOOL, "arguments": {}}
    headers = {"Accept": "application/json", "X-Bridge-Token": token or env["token"],
               "MCP-Protocol-Version": version}
    if version == MODERN:
        params["_meta"] = {PREFIX + "protocolVersion": MODERN, PREFIX + "clientCapabilities": {}}
        headers["Mcp-Method"] = method
        if "name" in params:
            headers["Mcp-Name"] = params["name"]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=make_mcp(env["service"])),
                                 base_url="http://127.0.0.1:8765") as client:
        return await client.post("/mcp", headers=headers,
                                 json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})


def value(response):
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert not result["isError"], result
    return json.loads(result["content"][0]["text"])


def test_skill_is_complete_versioned_and_bounded():
    skill = read_project_lead_skill()
    assert skill["name"] == "project-lead" and skill["version"] == "2.6.0"
    assert skill["sha256"] == sha256(skill["content"].encode()).hexdigest()
    assert skill["content"].startswith("---\nname: project-lead\ndescription:")
    assert skill["content"].endswith("independently ran its tests.\n")
    assert 200 < len(skill["content"].split()) < 1400
    assert len(json.dumps(skill)) < MAX_OUTPUT


@pytest.mark.parametrize("text", [
    "You own understanding", "less-capable coding model", "smallest coherent, testable milestone",
    "context_hashes", "copy_prompt", "user will paste", "corrective handoff",
    "agent-reported tests", "higher-priority instructions", "untrusted",
    "list_agent_adapters", "adapter_id", "runtime_type",
])
def test_skill_covers_project_lead_contract(text):
    assert text in read_project_lead_skill()["content"]


@pytest.mark.parametrize("text", [
    "silent model choice",
    "explicit",
    "category",
    "free/cheap",
    "self-initiated",
    "ask first",
    "never silently switch",
    "never use a disabled model",
    "Reuse the session",
    "corrective",
])
def test_skill_encodes_model_choice_and_continuation_rules(text):
    assert text in read_project_lead_skill()["content"]


def test_skill_no_longer_forbids_all_non_default_models():
    content = read_project_lead_skill()["content"]
    assert "rejects other selectors" not in content
    assert "do not continue an old session" not in content


def test_mcp_instructions_match_skill_model_rule():
    assert "enabled model" in INSTRUCTIONS
    assert "model-choice rule" in INSTRUCTIONS
    assert "always uses the global default" not in INSTRUCTIONS
    start_description = TOOLS["start_agent_run"][1]
    assert "currently available" in start_description or "enabled" in start_description
    assert "model_override_forbidden" not in start_description
    assert "scope" in TOOLS["list_agent_models"][1]


def test_skill_selects_an_exact_adapter_destination():
    content = read_project_lead_skill()["content"]
    assert "Choose by `adapter_id`" in content
    assert "use the ready workspace default" in content
    assert "If there is no default and exactly" in content
    assert "if several are ready, ask which one to" in content
    assert "A configured but unavailable default never silently fails over." in content
    assert "several routes are available, ask which one to" not in content
    assert "When several routes are available, ask which one to use rather than guessing." not in content
    assert "never by mapping `pi` or" in content
    for tool in ("list_agent_models", "start_agent_run", "read_agent_run",
                 "respond_agent_interaction", "list_agent_executions"):
        assert tool in content, tool
    assert "adapter_id" in INSTRUCTIONS and "never map pi or codex" in INSTRUCTIONS


def test_tool_schema_is_read_only_unscoped_and_empty():
    model, description, readonly, idempotent = TOOLS[SKILL_TOOL]
    schema = model.model_json_schema()
    assert schema["properties"] == {} and schema["additionalProperties"] is False
    assert readonly is True and idempotent is True
    assert "before planning" in description and "context loss" in description
    assert "project leader" in INSTRUCTIONS and SKILL_TOOL in INSTRUCTIONS


@pytest.mark.parametrize("version", [*LEGACY, MODERN])
async def test_complete_skill_is_retrievable_in_each_protocol(env, version):
    response = await rpc(env, version=version)
    assert value(response) == read_project_lead_skill()
    if version == MODERN:
        assert response.json()["result"]["_meta"][PREFIX + "serverInfo"]["version"] == __version__


@pytest.mark.parametrize("modern", [False, True])
async def test_server_discovery_advertises_skill_without_full_text(env, modern):
    params = {} if modern else {"protocolVersion": LEGACY[-1], "capabilities": {}, "clientInfo": {}}
    response = await rpc(env, "server/discover" if modern else "initialize", params,
                         version=MODERN if modern else LEGACY[-1])
    instructions = response.json()["result"]["instructions"]
    assert SKILL_TOOL in instructions
    assert read_project_lead_skill()["content"] not in instructions
    listed = await rpc(env, "tools/list", {}, version=MODERN if modern else LEGACY[-1])
    tool = next(t for t in listed.json()["result"]["tools"] if t["name"] == SKILL_TOOL)
    assert tool["annotations"] == {"readOnlyHint": True, "idempotentHint": True,
                                    "destructiveHint": False, "openWorldHint": False}


async def test_skill_requires_bridge_auth_and_obeys_pause_and_rotation(env):
    assert (await rpc(env, token="invalid")).status_code == 401
    admin_token = (env["state"] / "admin-token").read_text().strip()
    assert (await rpc(env, token=admin_token)).status_code == 401
    env["service"].manage_bridge("disable")
    assert (await rpc(env)).status_code == 401
    new = env["service"].manage_bridge("rotate_token")["token"]
    assert (await rpc(env)).status_code == 401
    assert value(await rpc(env, token=new))["name"] == "project-lead"


async def test_skill_works_with_no_enabled_mapping_and_never_opens_workspace(env, monkeypatch):
    env["service"].manage_workspace(env["id"], "disable")
    def denied(*args, **kwargs):
        raise AssertionError("Skill must not open or select a workspace")
    monkeypatch.setattr(env["service"].node_registry, "client", denied)
    monkeypatch.setattr(env["service"], "workspace", denied)
    skill = value(await rpc(env))
    assert skill == read_project_lead_skill()
    for secret in (str(env["root"]), str(env["state"]), env["id"], env["token"]):
        assert secret not in json.dumps(skill)


@pytest.mark.parametrize("arguments", [{"path": "../../secret"}, {"workspace_id": "ws_" + "a" * 24},
                                       {"name": "different-skill"}, {"content": "INJECTED_SENTINEL"}])
async def test_skill_rejects_all_caller_selected_content(env, arguments):
    response = await rpc(env, params={"name": SKILL_TOOL, "arguments": arguments})
    assert response.json()["error"]["code"] == -32602
    assert "INJECTED_SENTINEL" not in response.text


async def test_repository_skill_cannot_replace_bundled_guidance(env):
    (env["root"] / "SKILL.md").write_text("INJECTED_SENTINEL")
    target = env["root"] / "workspace_bridge/skills/project-lead/SKILL.md"
    target.parent.mkdir(parents=True)
    target.write_text("INJECTED_SENTINEL")
    assert "INJECTED_SENTINEL" not in value(await rpc(env))["content"]


async def test_missing_package_asset_fails_without_falling_back_to_repo(env, monkeypatch):
    import workspace_bridge.embedded_skill as module
    monkeypatch.setattr(module, "files", lambda _: env["tmp"] / "missing-package")
    response = await rpc(env)
    assert response.json()["result"]["isError"] is True
    assert str(env["tmp"]) not in response.text


def test_discovery_pointers_do_not_repeat_skill(env):
    for tool, ident in [("list_workspaces", None), ("workspace_info", env["id"])]:
        result = env["service"].call(ident, env["token"], tool, {})
        assert result["project_lead_skill"] == skill_hint()
        assert "content" not in result["project_lead_skill"]


def test_reading_skill_never_mutates_project_or_creates_job(env):
    before = {str(f): f.read_bytes() for f in env["root"].rglob("*") if f.is_file()}
    skill = env["service"].call(None, env["token"], SKILL_TOOL, {})
    assert skill == read_project_lead_skill()
    after = {str(f): f.read_bytes() for f in env["root"].rglob("*") if f.is_file()}
    assert before == after
    assert not (env["root"] / ".workspace-handoff").exists()
    assert env["service"].db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0


def test_internal_dispatch_also_rejects_arguments(env):
    for ident, args in [(env["id"], {}), (None, {"path": "anything"})]:
        with pytest.raises(BridgeError):
            env["service"].call(ident, env["token"], SKILL_TOOL, args)


def test_handoff_prompt_has_implementer_stop_conditions(env, payload):
    job = env["service"].call(env["id"], env["token"], "prepare_handoff", Handoff.model_validate(payload).model_dump())
    assert "Stop and report a blocker" in job["copy_prompt"]
    assert "Do not weaken tests or invent results" in job["copy_prompt"]
    assert job["path"] in job["copy_prompt"]
    assert Path(job["path"], "TASK.md").exists()


def test_reopen_preserves_mapping_token_and_jobs(env, payload):
    job = env["service"].call(env["id"], env["token"], "prepare_handoff", Handoff.model_validate(payload).model_dump())
    reopened = Service(env["state"], env["config"])
    try:
        assert reopened.call(None, env["token"], SKILL_TOOL, {})["name"] == "project-lead"
        assert reopened.authenticate(env["id"], env["token"])["enabled"]
        assert reopened.job(reopened.workspace(env["id"]), job["id"])["id"] == job["id"]
    finally:
        reopened.close()
