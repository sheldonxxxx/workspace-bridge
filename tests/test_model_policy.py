"""Global model policy (fail-closed) and global Bridge-owned sessions.

Uses scripted fakes only: no Node, network, provider credentials or real model.
"""
import json

import httpx
import pytest

from workspace_bridge.api import Handoff, make_admin
from workspace_bridge.security import BridgeError


def publish(agent_env, payload, **overrides):
    body = {**payload, **overrides}
    return agent_env["service"].call(agent_env["id"], agent_env["token"], "prepare_handoff",
                                     Handoff.model_validate(body).model_dump())


def start(agent_env, job_id, request_id="run-request-1", model=None, parent_run_id=None):
    return agent_env["service"].call(agent_env["id"], agent_env["token"], "start_opencode_run",
                                     {"job_id": job_id, "request_id": request_id,
                                      "model": model, "parent_run_id": parent_run_id})


def call(agent_env, tool, **args):
    return agent_env["service"].call(agent_env["id"], agent_env["token"], tool, args)


# ------------------------------------------------------- fail-closed default
def test_unconfigured_policy_rejects_new_runs_without_a_session(env, payload):
    service = env["service"]
    service.manage_workspace(env["id"], "set_agent_enabled", agent_enabled=True)
    job = service.call(env["id"], env["token"], "prepare_handoff",
                       Handoff.model_validate(payload).model_dump())
    assert service.orchestrator.model_policy_status() == {
        "configured": False, "enabled": [], "default": None, "enabled_count": 0}
    with pytest.raises(BridgeError) as exc:
        service.call(env["id"], env["token"], "start_opencode_run",
                     {"job_id": job["id"], "request_id": "r1", "model": None, "parent_run_id": None})
    assert exc.value.code == "model_policy_unconfigured"
    assert service.db.execute("SELECT count(*) FROM agent_runs").fetchone()[0] == 0


def test_legacy_default_model_alone_never_configures_policy(env, payload):
    service = env["service"]
    service.manage_workspace(env["id"], "set_agent_enabled", agent_enabled=True)
    service.set_setting("default_model", "anthropic/claude-sonnet")
    service.set_setting("default_model_name", "Claude Sonnet")
    job = service.call(env["id"], env["token"], "prepare_handoff",
                       Handoff.model_validate(payload).model_dump())
    assert service.orchestrator.model_policy_status()["configured"] is False
    with pytest.raises(BridgeError) as exc:
        service.call(env["id"], env["token"], "start_opencode_run",
                     {"job_id": job["id"], "request_id": "r1", "model": None, "parent_run_id": None})
    assert exc.value.code == "model_policy_unconfigured"


def test_corrupt_policy_setting_fails_closed(agent_env, payload):
    service = agent_env["service"]
    service.set_setting("model_policy", "not-json{{")
    job = publish(agent_env, payload)
    with pytest.raises(BridgeError) as exc:
        start(agent_env, job["id"], "corrupt")
    assert exc.value.code == "model_policy_unconfigured"


# ------------------------------------------------------------- save validation
def test_set_policy_validation(agent_env):
    orchestrator = agent_env["service"].orchestrator
    with pytest.raises(BridgeError) as exc:
        orchestrator.set_model_policy([], "anthropic/claude-sonnet")
    assert exc.value.code == "invalid_arguments"
    with pytest.raises(BridgeError) as exc:
        orchestrator.set_model_policy(["anthropic/claude-sonnet",
                                       "anthropic/claude-sonnet"], "anthropic/claude-sonnet")
    assert exc.value.code == "invalid_arguments"
    with pytest.raises(BridgeError) as exc:
        orchestrator.set_model_policy(["anthropic/claude-sonnet"], "glm/zai-glm-5.2")
    assert exc.value.code == "invalid_arguments"
    with pytest.raises(BridgeError) as exc:
        orchestrator.set_model_policy(["bad selector!"], "bad selector!")
    assert exc.value.code == "invalid_arguments"
    with pytest.raises(BridgeError) as exc:
        orchestrator.set_model_policy(["anthropic/no-such-model"], "anthropic/no-such-model")
    assert exc.value.code == "model_unavailable"
    status = orchestrator.set_model_policy(["glm/zai-glm-5.2"], "glm/zai-glm-5.2")
    assert status == {"configured": True, "enabled": ["glm/zai-glm-5.2"],
                      "default": "glm/zai-glm-5.2", "enabled_count": 1}
    # No secret material is exposed through the status surface.
    assert "token" not in json.dumps(status).lower()


def test_policy_save_is_atomic_on_rejection(agent_env):
    orchestrator = agent_env["service"].orchestrator
    before = orchestrator.model_policy_status()
    with pytest.raises(BridgeError):
        orchestrator.set_model_policy(["anthropic/missing"], "anthropic/missing")
    assert orchestrator.model_policy_status() == before


# ---------------------------------------------------------------- enforcement
def test_enabled_allowlist_and_default_resolution(agent_env, payload):
    job = publish(agent_env, payload)
    # Omitting the model uses the configured default.
    defaulted = start(agent_env, job["id"], "omitted-model")
    assert defaulted["model"] == "anthropic/claude-sonnet"
    assert agent_env["runtime"].prompts[-1]["model"] == {"providerID": "anthropic", "modelID": "claude-sonnet"}
    # An explicit enabled NON-default selector is allowed and persisted exactly.
    explicit = start(agent_env, job["id"], "enabled-non-default", model="glm/zai-glm-5.2")
    assert explicit["model"] == "glm/zai-glm-5.2"
    assert agent_env["runtime"].prompts[-1]["model"] == {"providerID": "glm", "modelID": "zai-glm-5.2"}
    # Passing the exact default explicitly is tolerated for compatibility.
    again = start(agent_env, job["id"], "explicit-default", model="anthropic/claude-sonnet")
    assert again["model"] == "anthropic/claude-sonnet"
    # Restrict the policy to the default only; the other model stays
    # discoverable but cannot start new runs.
    agent_env["service"].orchestrator.set_model_policy(
        ["anthropic/claude-sonnet"], "anthropic/claude-sonnet")
    listed = call(agent_env, "list_opencode_models")
    assert listed["scope"] == "global"
    by_selector = {m["selector"]: m for m in listed["models"]}
    assert by_selector["glm/zai-glm-5.2"]["enabled"] is False
    assert by_selector["anthropic/claude-sonnet"]["policy_default"] is True
    before = len(agent_env["runtime"].sessions)
    with pytest.raises(BridgeError) as exc:
        start(agent_env, job["id"], "disabled-model", model="glm/zai-glm-5.2")
    assert exc.value.code == "model_not_enabled"
    assert len(agent_env["runtime"].sessions) == before
    # Unknown selectors fail before session creation too.
    with pytest.raises(BridgeError) as exc:
        start(agent_env, job["id"], "unknown-model", model="glm/does-not-exist")
    assert exc.value.code == "model_not_enabled"
    assert len(agent_env["runtime"].sessions) == before
    # Existing runs keep their exact persisted selector.
    run = start(agent_env, job["id"], "after-restriction")
    assert run["model"] == "anthropic/claude-sonnet"


def test_enabled_but_unavailable_model_fails_before_session(agent_env, payload):
    job = publish(agent_env, payload)
    agent_env["runtime"].models = [m for m in agent_env["runtime"].models
                                   if m.selector != "glm/zai-glm-5.2"]
    before = len(agent_env["runtime"].sessions)
    with pytest.raises(BridgeError) as exc:
        start(agent_env, job["id"], "enabled-gone", model="glm/zai-glm-5.2")
    assert exc.value.code == "model_unavailable"
    assert len(agent_env["runtime"].sessions) == before


def test_default_must_remain_available_at_start(agent_env, payload):
    agent_env["runtime"].models = [m for m in agent_env["runtime"].models
                                   if m.selector != "anthropic/claude-sonnet"]
    job = publish(agent_env, payload)
    with pytest.raises(BridgeError) as exc:
        start(agent_env, job["id"], "default-gone")
    assert exc.value.code == "model_unavailable"
    assert agent_env["service"].db.execute("SELECT count(*) FROM agent_runs").fetchone()[0] == 0


def test_conflicting_request_id_still_rejected(agent_env, payload):
    job = publish(agent_env, payload)
    first = start(agent_env, job["id"], "dup-request")
    other = publish(agent_env, payload, request_id="example-2")
    with pytest.raises(BridgeError) as exc:
        start(agent_env, other["id"], "dup-request")
    assert exc.value.code == "conflict"
    assert agent_env["runtime"].sessions and first["run_id"]


def test_two_workspaces_share_one_global_policy(agent_env, payload):
    other_root = agent_env["parent"] / "beta"
    other_root.mkdir()
    beta = agent_env["service"].add_workspace("Beta", str(other_root), [])["workspace"]["id"]
    agent_env["service"].manage_workspace(beta, "enable")
    agent_env["service"].manage_workspace(beta, "set_agent_enabled", agent_enabled=True)
    alpha_models = call(agent_env, "list_opencode_models")
    beta_models = agent_env["service"].call(
        beta, agent_env["token"], "list_opencode_models", {"query": "", "limit": 25})
    assert alpha_models["scope"] == "global" == beta_models["scope"]
    assert [m["selector"] for m in alpha_models["models"]] == \
        [m["selector"] for m in beta_models["models"]]
    assert [m["enabled"] for m in alpha_models["models"]] == \
        [m["enabled"] for m in beta_models["models"]]
    assert agent_env["runtime"].model_calls and all(
        call_directory is None for call_directory in agent_env["runtime"].model_calls)


def test_mcp_surface_has_no_policy_mutation_or_bypass():
    from workspace_bridge.api import TOOLS
    assert "set_model_policy" not in TOOLS
    start_schema = TOOLS["start_opencode_run"][0].model_json_schema()
    assert set(start_schema["properties"]) <= {"workspace_id", "job_id", "request_id", "model",
                                               "parent_run_id", "continue_from_run_id"}
    assert "force" not in json.dumps(start_schema).lower()
    assert "project-specific" not in TOOLS["list_opencode_models"][1]
    assert "scope=global" in TOOLS["list_opencode_models"][1]
    assert len(TOOLS) == 19


# ------------------------------------------------------- global sessions table
@pytest.mark.asyncio
async def test_global_sessions_endpoint_covers_all_workspaces(agent_env, payload):
    job = publish(agent_env, payload)
    run = start(agent_env, job["id"], "global-sessions")
    other_root = agent_env["parent"] / "beta"
    other_root.mkdir()
    beta = agent_env["service"].add_workspace("Beta", str(other_root), [])["workspace"]["id"]
    agent_env["service"].manage_workspace(beta, "enable")
    agent_env["service"].manage_workspace(beta, "set_agent_enabled", agent_enabled=True)
    other_job = agent_env["service"].call(beta, agent_env["token"], "prepare_handoff",
                                          Handoff.model_validate(payload).model_dump())
    other_run = agent_env["service"].call(beta, agent_env["token"], "start_opencode_run",
                                          {"job_id": other_job["id"], "request_id": "beta-run",
                                           "model": None, "parent_run_id": None})
    token = (agent_env["state"] / "admin-token").read_text().strip()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(
            app=make_admin(agent_env["service"], agent_env["config"]["admin_token_hash"])),
            base_url="http://127.0.0.1:8766") as anonymous:
        assert (await anonymous.get("/api/opencode/sessions")).status_code == 401
    async with httpx.AsyncClient(transport=httpx.ASGITransport(
            app=make_admin(agent_env["service"], agent_env["config"]["admin_token_hash"])),
            base_url="http://127.0.0.1:8766", headers={"Authorization": "Bearer " + token}) as client:
        body = (await client.get("/api/opencode/sessions")).json()
        assert body["scope"] == "global"
        ids = [row["run_id"] for row in body["runs"]]
        assert run["run_id"] in ids and other_run["run_id"] in ids
        # Newest first.
        created = [row["created"] for row in body["runs"]]
        assert created == sorted(created, reverse=True)
        first = body["runs"][0]
        for key in ("run_id", "state", "workspace_id", "workspace_name", "job_id",
                    "handoff_title", "model", "session_id", "created", "started",
                    "updated", "finished", "duration_seconds", "pending_request_count",
                    "notification", "active"):
            assert key in first, key
        assert first["model"] == "anthropic/claude-sonnet"
        assert first["workspace_name"] in ("Alpha", "Beta")
        assert first["handoff_title"] == payload["title"]
        bounded = await client.get("/api/opencode/sessions", params={"limit": 1})
        assert len(bounded.json()["runs"]) == 1
        assert bounded.json()["next_offset"] == 1
        stopped = await client.post(f"/api/runs/{other_run['run_id']}/stop")
        assert stopped.json()["state"] == "cancelled"
