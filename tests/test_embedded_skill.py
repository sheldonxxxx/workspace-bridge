"""Skill delivery and trust boundary, not a behavioral evaluation of an LLM."""
from hashlib import sha256
from pathlib import Path
import json

import httpx
import pytest

from workspace_bridge import __version__
from workspace_bridge.api import Handoff, INSTRUCTIONS, TOOLS, make_mcp
from workspace_bridge.embedded_skill import SKILL_URI, SKILLS_EXTENSION, read_project_lead_skill, skill_hint
from workspace_bridge.protocol import LEGACY, MODERN, PREFIX
from workspace_bridge.security import BridgeError, MAX_OUTPUT
from workspace_bridge.service import Service


async def rpc(env, method="skills/get", params=None, *, version=LEGACY[-1], token=None):
    params = dict(params) if params is not None else {"uri": SKILL_URI}
    headers = {"Accept": "application/json", "X-Bridge-Token": token or env["token"],
               "MCP-Protocol-Version": version}
    if version == MODERN:
        params["_meta"] = {PREFIX + "protocolVersion": MODERN, PREFIX + "clientCapabilities": {}}
        headers["Mcp-Method"] = method
        if method == "resources/read":
            headers["Mcp-Name"] = params["uri"]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=make_mcp(env["service"])),
                                 base_url="http://127.0.0.1:8765") as client:
        return await client.post("/mcp", headers=headers,
                                 json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})


def test_skill_is_complete_versioned_and_bounded():
    skill = read_project_lead_skill()
    assert skill["name"] == "project-lead" and skill["version"] == "3.3.0"
    assert skill["sha256"] == sha256(skill["content"].encode()).hexdigest()
    assert skill["content"].startswith("---\nname: project-lead\ndescription:")
    assert skill["content"].endswith("independently ran its tests.\n")
    assert 200 < len(skill["content"].split()) < 1400
    assert len(json.dumps(skill)) < MAX_OUTPUT


@pytest.mark.parametrize("text", [
    "You own understanding", "less-capable coding model", "smallest coherent, testable milestone",
    "context_hashes", "copy_prompt", "user will paste", "corrective handoff",
    "reported tests and commands", "higher-priority instructions", "untrusted",
    "list_agent_adapters", "adapter_id", "runtime_type",
    "Review the execution log of every automated run", "list_agent_executions",
    "execution-quality", "propose a bounded fix", "cancel_agent_run", "list_agent_runs", "list_handoffs",
])
def test_skill_covers_project_lead_contract(text):
    assert text in read_project_lead_skill()["content"]


@pytest.mark.parametrize("text", [
    "Silent choice",
    "explicit request",
    "category",
    "free/cheap",
    "A non-default you chose yourself: ask first",
    "Never switch models silently",
    "never use a",
    "disabled model",
    "continue_from_run_id",
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
    assert "identified by `adapter_id`" in content
    assert "the ready workspace default" in content
    assert "the only ready adapter" in content
    assert "if several are ready, ask which to use" in content
    assert "If nothing is\nready" in content
    assert "A configured but unavailable default never silently fails over." in content
    assert "several routes are available, ask which one to" not in content
    assert "When several routes are available, ask which one to use rather than guessing." not in content
    assert "never map `pi` or `codex`" in content
    for tool in ("list_agent_models", "start_agent_run", "read_agent_run",
                 "respond_agent_interaction", "list_agent_executions"):
        assert tool in content, tool
    assert "adapter_id" in INSTRUCTIONS and "never map pi or codex" in INSTRUCTIONS


def test_skill_is_not_a_tool():
    assert not any("skill" in name for name in TOOLS)
    assert SKILL_URI in INSTRUCTIONS and "project leader" in INSTRUCTIONS


def digest(text):
    return "sha256:" + sha256(text.encode()).hexdigest()


@pytest.mark.parametrize("version", [*LEGACY, MODERN])
async def test_skills_list_and_get_return_catalog_entry(env, version):
    skill = read_project_lead_skill()
    entry = {"uri": SKILL_URI,
             "frontmatter": {"name": "project-lead", "version": "3.3.0",
                             "description": skill["content"].split("\n")[2].removeprefix("description: ")},
             "resources": [{"uri": SKILL_URI, "digest": digest(skill["content"])}]}
    listed = await rpc(env, "skills/list", {}, version=version)
    assert listed.json()["result"]["skills"] == [entry] and "nextCursor" not in listed.json()["result"]
    got = await rpc(env, "skills/get", {"uri": SKILL_URI}, version=version)
    result = got.json()["result"]
    assert result["skill"] == entry
    assert {k: result[k] for k in entry} == entry
    if version == MODERN:
        assert result["_meta"][PREFIX + "serverInfo"]["version"] == __version__


@pytest.mark.parametrize("version", [*LEGACY, MODERN])
async def test_resources_read_returns_exact_skill_text(env, version):
    response = await rpc(env, "resources/read", {"uri": SKILL_URI}, version=version)
    content = response.json()["result"]["contents"]
    assert content == [{"uri": SKILL_URI, "mimeType": "text/markdown",
                        "text": read_project_lead_skill()["content"]}]


@pytest.mark.parametrize("method,params", [
    ("skills/get", {"uri": "skill://workspace-bridge/other/SKILL.md"}),
    ("skills/get", {}), ("skills/list", {"cursor": "x"}),
    ("resources/read", {"uri": "skill://workspace-bridge/project-lead/../../secret"}),
    ("resources/read", {"uri": "file:///etc/passwd"}), ("resources/read", {"uri": 1})])
async def test_skill_methods_reject_caller_selected_content(env, method, params):
    response = await rpc(env, method, params)
    assert response.json()["error"]["code"] == -32602


@pytest.mark.parametrize("modern", [False, True])
async def test_discovery_declares_skills_extension_without_full_text(env, modern):
    params = {} if modern else {"protocolVersion": LEGACY[-1], "capabilities": {}, "clientInfo": {}}
    response = await rpc(env, "server/discover" if modern else "initialize", params,
                         version=MODERN if modern else LEGACY[-1])
    result = response.json()["result"]
    assert result["capabilities"]["extensions"] == {SKILLS_EXTENSION: {}}
    assert result["capabilities"]["resources"] == {}
    assert read_project_lead_skill()["content"] not in result["instructions"]


async def test_skill_requires_bridge_auth_and_obeys_pause_and_rotation(env):
    assert (await rpc(env, token="invalid")).status_code == 401
    assert (await rpc(env, token="admin")).status_code == 401
    env["service"].manage_bridge("disable")
    assert (await rpc(env)).status_code == 401
    new = env["service"].manage_bridge("rotate_token")["token"]
    assert (await rpc(env)).status_code == 401
    assert (await rpc(env, token=new)).json()["result"]["uri"] == SKILL_URI


async def test_skill_works_with_no_enabled_mapping_and_never_opens_workspace(env, monkeypatch):
    env["service"].manage_workspace(env["id"], "disable")
    def denied(*args, **kwargs):
        raise AssertionError("Skill must not open or select a workspace")
    monkeypatch.setattr(env["service"].node_registry, "client", denied)
    monkeypatch.setattr(env["service"], "workspace", denied)
    response = await rpc(env, "resources/read", {"uri": SKILL_URI})
    assert response.json()["result"]["contents"][0]["text"] == read_project_lead_skill()["content"]
    for secret in (str(env["root"]), str(env["state"]), env["id"], env["token"]):
        assert secret not in response.text


async def test_repository_skill_cannot_replace_bundled_guidance(env):
    (env["root"] / "SKILL.md").write_text("INJECTED_SENTINEL")
    target = env["root"] / "workspace_bridge/skills/project-lead/SKILL.md"
    target.parent.mkdir(parents=True)
    target.write_text("INJECTED_SENTINEL")
    assert "INJECTED_SENTINEL" not in (await rpc(env, "resources/read", {"uri": SKILL_URI})).text


def test_discovery_pointers_do_not_repeat_skill(env):
    for tool, ident in [("list_workspaces", None), ("workspace_info", env["id"])]:
        result = env["service"].call(ident, env["token"], tool, {})
        assert result["project_lead_skill"] == skill_hint()
        assert "content" not in result["project_lead_skill"]
