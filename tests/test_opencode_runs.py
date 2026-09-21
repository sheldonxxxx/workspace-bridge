"""OpenCode run lifecycle, permission decisions and restart reconciliation.

All runtime interaction goes through a scripted fake: no Node, network, provider
credentials, Discord or real model is required.
"""
import json
from pathlib import Path

import pytest

from workspace_bridge.api import Handoff
from workspace_bridge.orchestration import MAX_RESULT_CHARS
from workspace_bridge.security import BridgeError
from workspace_bridge.service import Service

from runtime_fakes import permission_event


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


def permission_wait(agent_env, run, pattern=("/Users/me/projects/**",), permission_id="per_1"):
    event = permission_event(run["session_id"], permission_id, pattern=list(pattern))
    agent_env["runtime"].push(event)
    agent_env["service"].orchestrator.handle_event(event)
    return call(agent_env, "read_opencode_run", run_id=run["run_id"])


def hold_open(agent_env):
    """Script a transcript with no durable completion evidence.

    Permission/cancel/pre-start tests isolate non-completion behavior, so
    the fake session must not contain a completed assistant message that
    the live completion probe would (correctly) complete from. Assertions
    on the tested behavior are unchanged.
    """
    from workspace_bridge.runtime import MessageInfo
    agent_env["runtime"].messages_script = [MessageInfo(id="u", role="user", created=1)]


# --------------------------------------------------------------- policy/migration
def test_agent_execution_is_separate_and_fails_closed(agent_env, payload):
    assert call(agent_env, "workspace_info")["agent_execution"] == "enabled"
    job = publish(agent_env, payload)
    agent_env["service"].manage_workspace(agent_env["id"], "set_write_scope", write_scope="workspace")
    agent_env["service"].manage_workspace(agent_env["id"], "set_agent_enabled", agent_enabled=False)
    assert call(agent_env, "workspace_info")["write_scope"] == "workspace"
    assert call(agent_env, "workspace_info")["agent_execution"] == "disabled"
    with pytest.raises(BridgeError) as exc:
        start(agent_env, job["id"])
    assert exc.value.code == "agent_disabled"


def test_mcp_cannot_enable_agent_execution():
    from workspace_bridge.api import TOOLS
    schemas = json.dumps({name: model.model_json_schema() for name, (model, *_) in TOOLS.items()})
    assert "agent_enabled" not in schemas
    assert "manage_workspace" not in TOOLS and "set_agent_enabled" not in TOOLS


def test_fresh_workspace_defaults_disabled_and_migration_fails_closed(env, payload):
    service = env["service"]
    assert service.workspace(env["id"], False)["agent_enabled"] == 0
    job = service.call(env["id"], env["token"], "prepare_handoff", Handoff.model_validate(payload).model_dump())
    before = dict(service.workspace(env["id"], False))
    service.db.execute("ALTER TABLE workspaces DROP COLUMN agent_enabled")
    service.db.commit()
    service.close()
    reopened = Service(env["state"], env["config"])
    try:
        current = dict(reopened.workspace(env["id"], False))
        assert current["agent_enabled"] == 0 and current["enabled"] == before["enabled"]
        assert current["write_scope"] == before["write_scope"]
        assert reopened.job(reopened.workspace(env["id"]), job["id"])["state"] == "prepared"
    finally:
        reopened.close()


def test_runs_table_is_isolated_from_job_publication_state(agent_env, payload):
    job = publish(agent_env, payload)
    run = start(agent_env, job["id"])
    job_row = agent_env["service"].job(agent_env["service"].workspace(agent_env["id"]), job["id"])
    assert job_row["state"] == "prepared" and run["state"] in ("running", "starting")
    assert run["state"] != job_row["state"]


# ------------------------------------------------------------------- validation
def test_start_requires_prepared_handoff_in_the_same_workspace(agent_env, payload):
    with pytest.raises(BridgeError) as exc:
        start(agent_env, "job_" + "0" * 24)
    assert exc.value.code == "not_found"
    other = agent_env["parent"] / "beta"; other.mkdir()
    beta = agent_env["service"].add_workspace("Beta", str(other), [])["workspace"]["id"]
    agent_env["service"].manage_workspace(beta, "enable")
    agent_env["service"].manage_workspace(beta, "set_agent_enabled", agent_enabled=True)
    foreign = agent_env["service"].call(beta, agent_env["token"], "prepare_handoff",
                                        Handoff.model_validate(payload).model_dump())
    with pytest.raises(BridgeError) as exc:
        start(agent_env, foreign["id"])
    assert exc.value.code == "not_found"
    job = publish(agent_env, payload)
    agent_env["service"].db.execute("UPDATE jobs SET state='publishing' WHERE id=?", (job["id"],))
    agent_env["service"].db.commit()
    with pytest.raises(BridgeError) as exc:
        start(agent_env, job["id"])
    assert exc.value.code == "conflict"


def test_start_is_idempotent_and_conflicts_on_changed_request(agent_env, payload):
    job = publish(agent_env, payload)
    first = start(agent_env, job["id"], "same-request")
    second = start(agent_env, job["id"], "same-request")
    assert first["run_id"] == second["run_id"] and second["idempotent_replay"] is True
    assert len(agent_env["runtime"].sessions) == 1
    with pytest.raises(BridgeError) as exc:
        start(agent_env, job["id"], "same-request", model="glm/zai-glm-5.2")
    assert exc.value.code == "conflict"
    assert len(agent_env["runtime"].sessions) == 1


def test_model_discovery_is_global_and_allowlist_is_enforced(agent_env, payload):
    job = publish(agent_env, payload)
    models = call(agent_env, "list_opencode_models", query="glm", limit=10)
    assert models["scope"] == "global"
    assert [m["selector"] for m in models["models"]] == ["glm/zai-glm-5.2"]
    assert models["models"][0]["enabled"] is True
    assert models["policy"]["default"] == "anthropic/claude-sonnet"
    # An unknown selector is not enabled and fails before session creation.
    with pytest.raises(BridgeError) as exc:
        start(agent_env, job["id"], "bad-model", model="glm/does-not-exist")
    assert exc.value.code == "model_not_enabled"
    assert len(agent_env["runtime"].sessions) == 0
    # An enabled non-default selector succeeds and is used exactly.
    other = start(agent_env, job["id"], "override", model="glm/zai-glm-5.2")
    assert other["model"] == "glm/zai-glm-5.2"
    assert agent_env["runtime"].prompts[-1]["model"] == {"providerID": "glm", "modelID": "zai-glm-5.2"}
    # Omitting the model uses the configured global default; passing the
    # exact default is tolerated for compatibility.
    defaulted = start(agent_env, job["id"], "defaulted")
    assert defaulted["model"] == "anthropic/claude-sonnet"
    assert agent_env["runtime"].prompts[-1]["model"] == {"providerID": "anthropic", "modelID": "claude-sonnet"}
    explicit = start(agent_env, job["id"], "explicit-default", model="anthropic/claude-sonnet")
    assert explicit["model"] == "anthropic/claude-sonnet"


def test_session_directory_mismatch_is_rejected(agent_env, payload):
    agent_env["runtime"].session_directory = "/somewhere/else"
    job = publish(agent_env, payload)
    with pytest.raises(BridgeError) as exc:
        start(agent_env, job["id"])
    assert exc.value.code == "session_mismatch"
    assert agent_env["runtime"].abort_calls
    assert agent_env["service"].db.execute("SELECT count(*) FROM agent_runs").fetchone()[0] == 0


# --------------------------------------------------------------- permission flow
def test_permission_asked_creates_wait_and_external_reply_resolves(agent_env, payload):
    """Realistic v1 ask -> wait -> manual approval in an attached client."""
    job = publish(agent_env, payload)
    run = start(agent_env, job["id"], "asked-flow")
    hold_open(agent_env)
    asked = permission_event(run["session_id"], "per_asked", permission="edit",
                             patterns=["/data/requested/**"], always=["/data/always/**"])
    assert asked["type"] == "permission.asked"
    agent_env["service"].orchestrator.handle_event(asked)
    detail = call(agent_env, "read_opencode_run", run_id=run["run_id"])
    assert detail["state"] == "waiting_permission" and detail["active"] is True
    assert detail["pending_request_count"] == 1
    assert detail["pending_requests"][0]["request_id"] == "per_asked"
    assert detail["pending_requests"][0]["action"] == "edit"
    # The exact OpenCode-proposed always scope stays in `pattern`;
    # the requested target stays separately reviewable.
    assert detail["pending_requests"][0]["pattern"] == ["/data/always/**"]
    assert detail["pending_requests"][0]["requested_patterns"] == ["/data/requested/**"]
    request = call(agent_env, "read_opencode_request", run_id=run["run_id"],
                   request_id="per_asked")
    assert request["action"] == "edit"
    assert request["pattern"] == ["/data/always/**"]
    assert request["requested_patterns"] == ["/data/requested/**"]
    # Manual approval in an attached OpenCode client arrives as permission.replied
    # (V1 requestID/reply already mapped to permission_id/response by transport).
    agent_env["service"].orchestrator.handle_event(
        {"type": "permission.replied", "session_id": run["session_id"],
         "permission_id": "per_asked", "response": "once"})
    after = call(agent_env, "read_opencode_run", run_id=run["run_id"])
    assert after["pending_request_count"] == 0
    assert after["state"] == "running"
    assert after["requests"][0]["decision"] == "once"
    assert after["requests"][0]["state"] == "approved"
    # A duplicate external reply for the same V1 request stays idempotent.
    agent_env["service"].orchestrator.handle_event(
        {"type": "permission.replied", "session_id": run["session_id"],
         "permission_id": "per_asked", "response": "once"})
    duplicate = call(agent_env, "read_opencode_run", run_id=run["run_id"])
    assert duplicate["pending_request_count"] == 0
    assert duplicate["state"] == "running"
    assert duplicate["requests"][0]["decision"] == "once"
    assert duplicate["requests"][0]["state"] == "approved"


def test_permission_updated_alias_still_creates_wait(agent_env, payload):
    job = publish(agent_env, payload)
    run = start(agent_env, job["id"], "alias-flow")
    event = permission_event(run["session_id"], "per_alias", pattern=["/x/**"],
                             event_type="permission.updated")
    agent_env["service"].orchestrator.handle_event(event)
    detail = call(agent_env, "read_opencode_run", run_id=run["run_id"])
    assert detail["state"] == "waiting_permission"
    assert detail["pending_request_count"] == 1
    assert detail["pending_requests"][0]["request_id"] == "per_alias"


def test_legacy_single_scope_ask_remains_reviewable(agent_env, payload):
    from runtime_fakes import permission_event_legacy
    job = publish(agent_env, payload)
    run = start(agent_env, job["id"], "legacy-flow")
    agent_env["service"].orchestrator.handle_event(
        permission_event_legacy(run["session_id"], "per_legacy", pattern=["/legacy/**"]))
    detail = call(agent_env, "read_opencode_run", run_id=run["run_id"])
    assert detail["state"] == "waiting_permission"
    assert detail["pending_requests"][0]["request_id"] == "per_legacy"
    assert detail["pending_requests"][0]["pattern"] == ["/legacy/**"]
    assert detail["pending_requests"][0]["requested_patterns"] == ["/legacy/**"]


def test_waiting_permission_persists_and_once_resumes_same_session(agent_env, payload):
    job = publish(agent_env, payload)
    run = start(agent_env, job["id"])
    hold_open(agent_env)
    detail = permission_wait(agent_env, run)
    assert detail["state"] == "waiting_permission" and detail["active"] is True
    pending = detail["pending_requests"]
    assert len(pending) == 1 and pending[0]["kind"] == "permission"
    assert pending[0]["pattern"] == ["/Users/me/projects/**"]
    assert detail["pending_request_count"] == 1
    response = call(agent_env, "respond_opencode_permission", run_id=run["run_id"],
                    request_id="per_1", decision="once")
    assert response["resumed_same_session"] is True and response["run_state"] == "running"
    assert agent_env["runtime"].respond_calls == [(run["session_id"], "per_1", "once")]
    after = call(agent_env, "read_opencode_run", run_id=run["run_id"])
    assert after["state"] == "running" and after["pending_requests"] == []
    assert after["requests"][0]["decision"] == "once" and after["requests"][0]["state"] == "approved"


def test_always_uses_exact_opencode_pattern_and_fails_closed_without_scope(agent_env, payload):
    job = publish(agent_env, payload)
    scoped = start(agent_env, job["id"], "with-scope")
    permission_wait(agent_env, scoped, pattern=("/Users/me/projects/**", "/tmp/work/**"))
    request = call(agent_env, "read_opencode_request", run_id=scoped["run_id"], request_id="per_1")
    assert request["pattern"] == ["/Users/me/projects/**", "/tmp/work/**"]
    assert request["always_allowed"] is True
    response = call(agent_env, "respond_opencode_permission", run_id=scoped["run_id"],
                    request_id="per_1", decision="always")
    assert response["scope"] == ["/Users/me/projects/**", "/tmp/work/**"]
    assert agent_env["runtime"].respond_calls[-1] == (scoped["session_id"], "per_1", "always")

    job2 = publish(agent_env, payload, request_id="example-2", title="No scope")
    unscoped = start(agent_env, job2["id"], "no-scope")
    permission_wait(agent_env, unscoped, pattern=(), permission_id="per_2")
    request = call(agent_env, "read_opencode_request", run_id=unscoped["run_id"], request_id="per_2")
    assert request["always_allowed"] is False
    with pytest.raises(BridgeError) as exc:
        call(agent_env, "respond_opencode_permission", run_id=unscoped["run_id"],
             request_id="per_2", decision="always")
    assert exc.value.code == "always_scope_unknown"
    assert (unscoped["session_id"], "per_2", "always") not in agent_env["runtime"].respond_calls
    assert call(agent_env, "respond_opencode_permission", run_id=unscoped["run_id"],
                request_id="per_2", decision="once")["request_state"] == "approved"


def test_reject_returns_running_and_stale_or_cross_workspace_requests_fail(agent_env, payload):
    job = publish(agent_env, payload)
    run = start(agent_env, job["id"])
    permission_wait(agent_env, run)
    rejected = call(agent_env, "respond_opencode_permission", run_id=run["run_id"],
                    request_id="per_1", decision="reject")
    assert rejected["request_state"] == "rejected" and rejected["run_state"] == "running"
    with pytest.raises(BridgeError) as exc:
        call(agent_env, "respond_opencode_permission", run_id=run["run_id"],
             request_id="per_1", decision="once")
    assert exc.value.code == "conflict"

    other = agent_env["parent"] / "beta"; other.mkdir()
    beta = agent_env["service"].add_workspace("Beta", str(other), [])["workspace"]["id"]
    agent_env["service"].manage_workspace(beta, "enable")
    with pytest.raises(BridgeError) as exc:
        agent_env["service"].call(beta, agent_env["token"], "read_opencode_run", {"run_id": run["run_id"]})
    assert exc.value.code == "not_found"
    with pytest.raises(BridgeError) as exc:
        agent_env["service"].call(beta, agent_env["token"], "respond_opencode_permission",
                                  {"run_id": run["run_id"], "request_id": "per_1", "decision": "once"})
    assert exc.value.code == "not_found"


def test_question_wait_is_visible_and_not_remotely_answerable(agent_env, payload):
    job = publish(agent_env, payload)
    run = start(agent_env, job["id"])
    agent_env["service"].orchestrator.handle_event(
        {"type": "question.asked", "session_id": run["session_id"], "data": {"id": "q_1", "action": "choose"}})
    detail = call(agent_env, "read_opencode_run", run_id=run["run_id"])
    assert detail["state"] == "waiting_question"
    assert detail["pending_requests"][0]["kind"] == "question"
    with pytest.raises(BridgeError) as exc:
        call(agent_env, "respond_opencode_permission", run_id=run["run_id"],
             request_id="q_1", decision="once")
    assert exc.value.code == "runtime_unsupported"


# --------------------------------------------------------------- completion/cancel
def test_idle_completion_is_bounded_and_unverified(agent_env, payload):
    job = publish(agent_env, payload)
    run = start(agent_env, job["id"])
    from workspace_bridge.runtime import MessageInfo
    agent_env["runtime"].messages_script = [
        MessageInfo(id="m", role="assistant", created=1, completed=2, text="z" * (MAX_RESULT_CHARS + 5000))]
    agent_env["service"].orchestrator.handle_event({"type": "session.idle", "session_id": run["session_id"]})
    detail = call(agent_env, "read_opencode_run", run_id=run["run_id"])
    assert detail["state"] == "completed" and detail["result"]["has_final_response"] is True
    assert len(detail["result"]["summary"]) <= MAX_RESULT_CHARS
    assert detail["agent_evidence"] == "unverified"


def test_session_error_fails_with_sanitized_code(agent_env, payload):
    job = publish(agent_env, payload)
    run = start(agent_env, job["id"])
    agent_env["service"].orchestrator.handle_event(
        {"type": "session.error", "session_id": run["session_id"],
         "error": {"name": "ProviderAuthError", "message": "bad key"}})
    detail = call(agent_env, "read_opencode_run", run_id=run["run_id"])
    assert detail["state"] == "failed" and detail["error"]["code"] == "ProviderAuthError"


def test_cancel_positive_and_uncertain(agent_env, payload):
    job = publish(agent_env, payload)
    run = start(agent_env, job["id"])
    hold_open(agent_env)
    assert call(agent_env, "cancel_opencode_run", run_id=run["run_id"])["state"] == "cancelled"
    assert agent_env["runtime"].abort_calls[-1] == (str(agent_env["root"]), run["session_id"])

    job2 = publish(agent_env, payload, request_id="example-2", title="Uncertain")
    run2 = start(agent_env, job2["id"], "uncertain")
    agent_env["runtime"].abort_result = False
    with pytest.raises(BridgeError) as exc:
        call(agent_env, "cancel_opencode_run", run_id=run2["run_id"])
    assert exc.value.code == "abort_uncertain"
    detail = call(agent_env, "read_opencode_run", run_id=run2["run_id"])
    assert detail["state"] == "running" and detail["error"]["code"] == "abort_uncertain"


# --------------------------------------------------------------- reconcile/restart
def test_restart_keeps_pending_wait_answerable(agent_env, payload):
    job = publish(agent_env, payload)
    run = start(agent_env, job["id"])
    permission_wait(agent_env, run)
    reopened = Service(agent_env["state"], agent_env["config"], runtime=agent_env["runtime"],
                       notifier=agent_env["notifier"], orchestrator_background=False)
    try:
        reopened.orchestrator.reconcile_startup()
        detail = reopened.call(agent_env["id"], agent_env["token"], "read_opencode_run",
                               {"run_id": run["run_id"]})
        assert detail["state"] == "waiting_permission"
        assert detail["pending_requests"][0]["request_id"] == "per_1"
        answered = reopened.call(agent_env["id"], agent_env["token"], "respond_opencode_permission",
                                 {"run_id": run["run_id"], "request_id": "per_1", "decision": "once"})
        assert answered["run_state"] == "running"
    finally:
        reopened.close()


def test_restart_orphans_missing_sessions_without_pending_and_completes_finished(agent_env, payload):
    job = publish(agent_env, payload)
    orphan = start(agent_env, job["id"], "orphan-run")
    agent_env["runtime"].session_missing = True
    reopened = Service(agent_env["state"], agent_env["config"], runtime=agent_env["runtime"],
                       notifier=agent_env["notifier"], orchestrator_background=False)
    try:
        reopened.orchestrator.reconcile_startup()
        detail = reopened.call(agent_env["id"], agent_env["token"], "read_opencode_run",
                               {"run_id": orphan["run_id"]})
        assert detail["state"] == "orphaned" and detail["error"]["code"] == "session_missing"
    finally:
        reopened.close()
    agent_env["runtime"].session_missing = False


def test_no_runtime_configured_fails_closed(env, payload):
    service = env["service"]
    job = service.call(env["id"], env["token"], "prepare_handoff", Handoff.model_validate(payload).model_dump())
    service.manage_workspace(env["id"], "set_agent_enabled", agent_enabled=True)
    # A policy alone cannot conjure a runtime: availability is still fail-closed.
    service.set_setting("model_policy", json.dumps(
        {"enabled": ["anthropic/claude-sonnet"], "default": "anthropic/claude-sonnet"}))
    with pytest.raises(BridgeError) as exc:
        service.call(env["id"], env["token"], "start_opencode_run",
                     {"job_id": job["id"], "request_id": "r", "model": None, "parent_run_id": None})
    assert exc.value.code == "runtime_unavailable"


# ------------------------------------------------- prompt/event submission races
def test_permission_during_prompt_preserves_waiting_and_resumes(agent_env, payload):
    job = publish(agent_env, payload)
    def hook(session_id):
        agent_env["service"].orchestrator.handle_event(
            permission_event(session_id, "per_race", pattern=["/Users/me/projects/**"]))
    agent_env["runtime"].prompt_hook = hook
    run = start(agent_env, job["id"], "race-permission")
    detail = call(agent_env, "read_opencode_run", run_id=run["run_id"])
    assert detail["state"] == "waiting_permission" and detail["active"] is True
    assert detail["started"] is not None
    assert detail["pending_requests"][0]["request_id"] == "per_race"
    answered = call(agent_env, "respond_opencode_permission", run_id=run["run_id"],
                    request_id="per_race", decision="once")
    assert answered["run_state"] == "running" and answered["resumed_same_session"] is True
    assert agent_env["runtime"].respond_calls == [(run["session_id"], "per_race", "once")]


def test_session_error_during_prompt_is_not_overwritten(agent_env, payload):
    job = publish(agent_env, payload)
    def hook(session_id):
        agent_env["service"].orchestrator.handle_event(
            {"type": "session.error", "session_id": session_id,
             "error": {"name": "ApiError", "message": "boom"}})
    agent_env["runtime"].prompt_hook = hook
    run = start(agent_env, job["id"], "race-error")
    detail = call(agent_env, "read_opencode_run", run_id=run["run_id"])
    assert detail["state"] == "failed" and detail["error"]["code"] == "ApiError"


def test_stale_idle_before_prompt_acceptance_cannot_complete(agent_env, payload):
    job = publish(agent_env, payload)
    def hook(session_id):
        agent_env["service"].orchestrator.handle_event({"type": "session.idle", "session_id": session_id})
    agent_env["runtime"].prompt_hook = hook
    run = start(agent_env, job["id"], "stale-idle")
    hold_open(agent_env)
    detail = call(agent_env, "read_opencode_run", run_id=run["run_id"])
    assert detail["state"] == "running" and detail["active"] is True
    assert detail["result"]["summary"] == "" and detail["result"]["has_final_response"] is False


def test_idle_without_completed_assistant_does_not_complete(agent_env, payload):
    from workspace_bridge.runtime import MessageInfo
    job = publish(agent_env, payload)
    run = start(agent_env, job["id"], "empty-idle")
    agent_env["runtime"].messages_script = [
        MessageInfo(id="u", role="user", created=1),
        MessageInfo(id="a", role="assistant", created=2, completed=None, text="partial"),
    ]
    agent_env["service"].orchestrator.handle_event({"type": "session.idle", "session_id": run["session_id"]})
    detail = call(agent_env, "read_opencode_run", run_id=run["run_id"])
    assert detail["state"] == "running" and detail["result"]["has_final_response"] is False


def test_permission_replied_event_wins_the_respond_race(agent_env, payload):
    job = publish(agent_env, payload)
    run = start(agent_env, job["id"], "reply-race")
    hold_open(agent_env)
    permission_wait(agent_env, run, pattern=("/x/**",), permission_id="per_race")
    def hook(session_id, permission_id, response):
        agent_env["service"].orchestrator.handle_event(
            {"type": "permission.replied", "session_id": session_id,
             "permission_id": permission_id, "response": "always"})
    agent_env["runtime"].respond_hook = hook
    result = call(agent_env, "respond_opencode_permission", run_id=run["run_id"],
                  request_id="per_race", decision="once")
    assert agent_env["runtime"].respond_calls == [(run["session_id"], "per_race", "once")]
    assert result["decision"] == "always" and result["request_state"] == "approved"
    detail = call(agent_env, "read_opencode_run", run_id=run["run_id"])
    assert detail["state"] == "running"
    assert detail["requests"][0]["state"] == "approved" and detail["requests"][0]["decision"] == "always"
    with pytest.raises(BridgeError) as exc:
        call(agent_env, "respond_opencode_permission", run_id=run["run_id"],
             request_id="per_race", decision="once")
    assert exc.value.code == "conflict"


def test_permission_replied_without_response_keeps_local_decision(agent_env, payload):
    job = publish(agent_env, payload)
    run = start(agent_env, job["id"], "reply-no-response")
    permission_wait(agent_env, run, pattern=("/y/**",), permission_id="per_nr")
    def hook(session_id, permission_id, response):
        agent_env["service"].orchestrator.handle_event(
            {"type": "permission.replied", "session_id": session_id, "permission_id": permission_id})
    agent_env["runtime"].respond_hook = hook
    result = call(agent_env, "respond_opencode_permission", run_id=run["run_id"],
                  request_id="per_nr", decision="once")
    assert result["decision"] == "once" and result["request_state"] == "resolved"


# --------------------------------------------------------- binding revalidation
def test_respond_rejects_mismatched_session_directory(agent_env, payload):
    job = publish(agent_env, payload)
    run = start(agent_env, job["id"], "binding-respond")
    permission_wait(agent_env, run)
    agent_env["runtime"].set_session_directory("/somewhere/else")
    with pytest.raises(BridgeError) as exc:
        call(agent_env, "respond_opencode_permission", run_id=run["run_id"],
             request_id="per_1", decision="once")
    assert exc.value.code == "session_mismatch"
    assert agent_env["runtime"].respond_calls == []


def test_cancel_does_not_abort_a_mismatched_session(agent_env, payload):
    job = publish(agent_env, payload)
    run = start(agent_env, job["id"], "binding-cancel")
    agent_env["runtime"].set_session_directory("/somewhere/else")
    result = call(agent_env, "cancel_opencode_run", run_id=run["run_id"])
    assert result["state"] == "orphaned" and result["cancelled"] is False
    assert agent_env["runtime"].abort_calls == []


# ----------------------------------------------------------- reconcile retries
def _reopen(agent_env):
    return Service(agent_env["state"], agent_env["config"], runtime=agent_env["runtime"],
                   notifier=agent_env["notifier"], orchestrator_background=False)


def test_transient_runtime_unavailable_does_not_orphan_then_recovers(agent_env, payload):
    from workspace_bridge.runtime import MessageInfo
    job = publish(agent_env, payload)
    run = start(agent_env, job["id"], "transient")
    agent_env["runtime"].messages_script = [MessageInfo(id="u", role="user", created=1)]
    agent_env["runtime"].session_error = "adapter is starting"
    reopened = _reopen(agent_env)
    try:
        reopened.orchestrator.reconcile_startup()
        detail = reopened.call(agent_env["id"], agent_env["token"], "read_opencode_run",
                               {"run_id": run["run_id"]})
        assert detail["state"] == "running" and detail["error"] is None
        assert reopened.orchestrator.retry_reconcile() == [run["run_id"]]
        agent_env["runtime"].session_error = None
        assert reopened.orchestrator.retry_reconcile() == []
        detail = reopened.call(agent_env["id"], agent_env["token"], "read_opencode_run",
                               {"run_id": run["run_id"]})
        assert detail["state"] == "running"
    finally:
        reopened.close()


def test_pending_permission_survives_transient_then_orphans_when_session_missing(agent_env, payload):
    job = publish(agent_env, payload)
    run = start(agent_env, job["id"], "pending-transient")
    permission_wait(agent_env, run)
    agent_env["runtime"].session_error = "adapter is starting"
    reopened = _reopen(agent_env)
    try:
        reopened.orchestrator.reconcile_startup()
        detail = reopened.call(agent_env["id"], agent_env["token"], "read_opencode_run",
                               {"run_id": run["run_id"]})
        assert detail["state"] == "waiting_permission" and len(detail["pending_requests"]) == 1
        agent_env["runtime"].session_error = None
        assert reopened.orchestrator.retry_reconcile() == [run["run_id"]]
        agent_env["runtime"].session_missing = True
        assert reopened.orchestrator.retry_reconcile() == []
        detail = reopened.call(agent_env["id"], agent_env["token"], "read_opencode_run",
                               {"run_id": run["run_id"]})
        assert detail["state"] == "orphaned" and detail["error"]["code"] == "session_missing"
        assert detail["pending_requests"] == []
        assert detail["requests"][0]["state"] == "orphaned"
    finally:
        reopened.close()
        agent_env["runtime"].session_missing = False


def test_reconcile_completes_only_with_completed_assistant(agent_env, payload):
    job = publish(agent_env, payload)
    run = start(agent_env, job["id"], "reconcile-complete")
    reopened = _reopen(agent_env)
    try:
        reopened.orchestrator.reconcile_startup()
        detail = reopened.call(agent_env["id"], agent_env["token"], "read_opencode_run",
                               {"run_id": run["run_id"]})
        assert detail["state"] == "completed" and detail["result"]["has_final_response"] is True
    finally:
        reopened.close()


def test_reconcile_keeps_existing_running_session_active(agent_env, payload):
    from workspace_bridge.runtime import MessageInfo
    job = publish(agent_env, payload)
    run = start(agent_env, job["id"], "reconcile-active")
    agent_env["runtime"].messages_script = [MessageInfo(id="u", role="user", created=1)]
    reopened = _reopen(agent_env)
    try:
        reopened.orchestrator.reconcile_startup()
        detail = reopened.call(agent_env["id"], agent_env["token"], "read_opencode_run",
                               {"run_id": run["run_id"]})
        assert detail["state"] == "running" and detail["error"] is None
        assert reopened.orchestrator.retry_reconcile() == []
    finally:
        reopened.close()


# --------------------------------------------------------------- model policy
def test_start_is_fail_closed_without_model_policy(env, payload):
    service = env["service"]
    service.manage_workspace(env["id"], "set_agent_enabled", agent_enabled=True)
    job = service.call(env["id"], env["token"], "prepare_handoff",
                       Handoff.model_validate(payload).model_dump())
    assert service.orchestrator.model_policy_status()["configured"] is False
    with pytest.raises(BridgeError) as exc:
        service.call(env["id"], env["token"], "start_opencode_run",
                     {"job_id": job["id"], "request_id": "no-policy", "model": None,
                      "parent_run_id": None})
    assert exc.value.code == "model_policy_unconfigured"
    assert service.db.execute("SELECT count(*) FROM agent_runs").fetchone()[0] == 0


# ------------------------------------------- missing observed (empty) directory
def test_missing_observed_directory_rejects_start(agent_env, payload):
    agent_env["runtime"].session_directory = ""
    job = publish(agent_env, payload)
    with pytest.raises(BridgeError) as exc:
        start(agent_env, job["id"], "missing-dir-start")
    assert exc.value.code == "session_mismatch"
    assert agent_env["runtime"].abort_calls
    assert agent_env["service"].db.execute("SELECT count(*) FROM agent_runs").fetchone()[0] == 0


def test_missing_observed_directory_fails_closed_on_followups(agent_env, payload):
    job = publish(agent_env, payload)
    run = start(agent_env, job["id"], "missing-dir-followup")
    permission_wait(agent_env, run)
    agent_env["runtime"].set_session_directory("")
    with pytest.raises(BridgeError) as exc:
        call(agent_env, "respond_opencode_permission", run_id=run["run_id"],
             request_id="per_1", decision="once")
    assert exc.value.code == "session_mismatch"
    assert agent_env["runtime"].respond_calls == []
    transcript = agent_env["service"].orchestrator.admin_read_run(run["run_id"], include_transcript=True)
    assert transcript["transcript"] == [{"error": "transcript unavailable"}]
    cancelled = call(agent_env, "cancel_opencode_run", run_id=run["run_id"])
    assert cancelled["state"] == "orphaned" and cancelled["cancelled"] is False
    assert agent_env["runtime"].abort_calls == []
    detail = call(agent_env, "read_opencode_run", run_id=run["run_id"])
    assert detail["state"] == "orphaned" and detail["error"]["code"] == "session_mismatch"


def test_missing_observed_directory_orphans_on_reconcile(agent_env, payload):
    job = publish(agent_env, payload)
    run = start(agent_env, job["id"], "missing-dir-reconcile")
    permission_wait(agent_env, run)
    agent_env["runtime"].set_session_directory("")
    reopened = _reopen(agent_env)
    try:
        reopened.orchestrator.reconcile_startup()
        detail = reopened.call(agent_env["id"], agent_env["token"], "read_opencode_run",
                               {"run_id": run["run_id"]})
        assert detail["state"] == "orphaned" and detail["error"]["code"] == "session_mismatch"
        assert detail["pending_requests"] == []
        assert detail["requests"][0]["state"] == "orphaned"
    finally:
        reopened.close()


# --------------------------------------- missed permission.asked recovery
def test_missed_ask_recovered_on_read_with_exact_scopes(agent_env, payload):
    """CRITICAL regression: a live event never observed is repaired by a status read."""
    from runtime_fakes import pending_permission
    job = publish(agent_env, payload)
    run = start(agent_env, job["id"], "missed-ask")
    hold_open(agent_env)
    assert call(agent_env, "read_opencode_run", run_id=run["run_id"])["state"] in ("running", "starting")
    agent_env["runtime"].add_pending_permission(
        run["session_id"],
        pending_permission(run["session_id"], "per_missed",
                           pattern=["/data/always/**"],
                           requested_patterns=["/data/requested/**"]))
    detail = call(agent_env, "read_opencode_run", run_id=run["run_id"])
    assert detail["state"] == "waiting_permission" and detail["active"] is True
    assert detail["pending_request_count"] == 1
    assert detail["pending_requests"][0]["request_id"] == "per_missed"
    # The exact OpenCode-proposed always scope stays in `pattern`;
    # the requested target stays separately reviewable.
    assert detail["pending_requests"][0]["pattern"] == ["/data/always/**"]
    assert detail["pending_requests"][0]["requested_patterns"] == ["/data/requested/**"]
    assert agent_env["runtime"].list_pending_calls[-1] == (str(agent_env["root"]), run["session_id"])


def test_respond_once_resumes_recovered_session(agent_env, payload):
    from runtime_fakes import pending_permission
    job = publish(agent_env, payload)
    run = start(agent_env, job["id"], "recover-respond")
    hold_open(agent_env)
    agent_env["runtime"].add_pending_permission(
        run["session_id"], pending_permission(run["session_id"], "per_rec"))
    assert call(agent_env, "read_opencode_run", run_id=run["run_id"])["state"] == "waiting_permission"
    response = call(agent_env, "respond_opencode_permission", run_id=run["run_id"],
                    request_id="per_rec", decision="once")
    assert response["resumed_same_session"] is True and response["run_state"] == "running"
    assert agent_env["runtime"].respond_calls == [(run["session_id"], "per_rec", "once")]
    after = call(agent_env, "read_opencode_run", run_id=run["run_id"])
    assert after["state"] == "running" and after["pending_requests"] == []
    assert after["requests"][0]["decision"] == "once" and after["requests"][0]["state"] == "approved"


def test_recovery_is_idempotent_across_read_and_restart(agent_env, payload):
    from runtime_fakes import pending_permission
    job = publish(agent_env, payload)
    run = start(agent_env, job["id"], "idempotent-recovery")
    agent_env["runtime"].add_pending_permission(
        run["session_id"],
        pending_permission(run["session_id"], "per_dup",
                           pattern=["/a/**"], requested_patterns=["/r/**"]))
    first = call(agent_env, "read_opencode_run", run_id=run["run_id"])
    assert first["pending_request_count"] == 1
    second = call(agent_env, "read_opencode_run", run_id=run["run_id"])
    assert second["pending_request_count"] == 1
    agent_env["service"].orchestrator.reconcile_startup()
    third = call(agent_env, "read_opencode_run", run_id=run["run_id"])
    assert third["pending_request_count"] == 1
    with agent_env["service"].lock:
        count = agent_env["service"].db.execute(
            "SELECT count(*) FROM agent_requests WHERE run=?", (run["run_id"],)).fetchone()[0]
    assert count == 1
    waits = [c for c in agent_env["notifier"].calls if c.get("state") == "waiting_permission"]
    assert len(waits) == 1


def test_restart_reconcile_recovers_unpersisted_ask(agent_env, payload):
    from runtime_fakes import pending_permission
    job = publish(agent_env, payload)
    run = start(agent_env, job["id"], "restart-missed")
    agent_env["runtime"].add_pending_permission(
        run["session_id"],
        pending_permission(run["session_id"], "per_restart",
                           pattern=["/data/always/**"],
                           requested_patterns=["/data/requested/**"]))
    reopened = _reopen(agent_env)
    try:
        reopened.orchestrator.reconcile_startup()
        detail = reopened.call(agent_env["id"], agent_env["token"], "read_opencode_run",
                               {"run_id": run["run_id"]})
        assert detail["state"] == "waiting_permission"
        assert detail["pending_request_count"] == 1
        assert detail["pending_requests"][0]["request_id"] == "per_restart"
        answered = reopened.call(agent_env["id"], agent_env["token"], "respond_opencode_permission",
                                 {"run_id": run["run_id"], "request_id": "per_restart",
                                  "decision": "once"})
        assert answered["run_state"] == "running"
    finally:
        reopened.close()


def test_permission_list_error_leaves_run_active(agent_env, payload):
    from workspace_bridge.runtime import RuntimeUnavailable
    job = publish(agent_env, payload)
    run = start(agent_env, job["id"], "list-error")
    hold_open(agent_env)
    agent_env["runtime"].set_list_pending_error(RuntimeUnavailable("adapter is down"))
    try:
        detail = call(agent_env, "read_opencode_run", run_id=run["run_id"])
    finally:
        agent_env["runtime"].set_list_pending_error(None)
    assert detail["state"] in ("running", "starting")
    assert detail["pending_request_count"] == 0
    assert detail["pending_requests"] == []


def test_cross_session_permission_never_attached(agent_env, payload):
    job = publish(agent_env, payload)
    run = start(agent_env, job["id"], "cross-session")
    hold_open(agent_env)
    ws = agent_env["service"].workspace(agent_env["id"], False)
    row = agent_env["service"].orchestrator._row(ws, run["run_id"])
    agent_env["service"].orchestrator._on_permission(
        dict(row), {"id": "per_x", "session_id": "ses_other", "action": "edit",
                    "title": "t", "pattern": ["/other/**"],
                    "requested_patterns": ["/other/**"]})
    detail = call(agent_env, "read_opencode_run", run_id=run["run_id"])
    assert detail["pending_request_count"] == 0
    assert detail["state"] in ("running", "starting")


def test_empty_list_does_not_resolve_persisted_wait(agent_env, payload):
    job = publish(agent_env, payload)
    run = start(agent_env, job["id"], "persisted-wait")
    permission_wait(agent_env, run)
    # OpenCode reports nothing pending (e.g. it restarted and lost the ask);
    # the Bridge-persisted request must remain waiting, never auto-resolve.
    assert agent_env["runtime"].list_pending_permissions(
        str(agent_env["root"]), run["session_id"]) == []
    detail = call(agent_env, "read_opencode_run", run_id=run["run_id"])
    assert detail["state"] == "waiting_permission"
    assert detail["pending_request_count"] == 1
    assert detail["pending_requests"][0]["request_id"] == "per_1"


# --------------------------------- permission-list failure observability (0.1.6)
def test_list_failure_is_visible_degraded_without_fabrication(agent_env, payload):
    """Regression: a failed listing is a visible degraded diagnostic, not []."""
    from workspace_bridge.runtime import RuntimeRejected
    job = publish(agent_env, payload)
    run = start(agent_env, job["id"], "list-degraded")
    hold_open(agent_env)
    agent_env["runtime"].set_list_pending_error(
        RuntimeRejected("upstream encoding failure", "runtime_rejected"))
    try:
        detail = call(agent_env, "read_opencode_run", run_id=run["run_id"])
    finally:
        agent_env["runtime"].set_list_pending_error(None)
    assert detail["state"] in ("running", "starting")
    assert detail["active"] is True
    assert detail["pending_request_count"] == 0
    assert detail["pending_requests"] == []
    sync = detail["permission_sync"]
    assert sync["status"] == "degraded"
    assert sync["reason"] == "permission_list_failed"
    assert sync["matched"] is None
    # No approval fabricated, no request persisted, no terminal failure.
    assert detail["requests"] == []
    assert detail["error"] is None


def test_successful_resync_recovers_and_clears_diagnostic(agent_env, payload):
    """A later successful list recovers the exact ask and clears degraded state."""
    from runtime_fakes import pending_permission
    from workspace_bridge.runtime import RuntimeUnavailable
    job = publish(agent_env, payload)
    run = start(agent_env, job["id"], "recover-clears")
    hold_open(agent_env)
    agent_env["runtime"].set_list_pending_error(RuntimeUnavailable("adapter is down"))
    try:
        degraded = call(agent_env, "read_opencode_run", run_id=run["run_id"])
    finally:
        agent_env["runtime"].set_list_pending_error(None)
    assert degraded["permission_sync"]["status"] == "degraded"
    assert degraded["pending_request_count"] == 0
    agent_env["runtime"].add_pending_permission(
        run["session_id"],
        pending_permission(run["session_id"], "per_late",
                           pattern=["/data/always/**"],
                           requested_patterns=["/data/requested/**"]))
    recovered = call(agent_env, "read_opencode_run", run_id=run["run_id"])
    assert recovered["state"] == "waiting_permission"
    assert recovered["pending_request_count"] == 1
    assert recovered["pending_requests"][0]["request_id"] == "per_late"
    assert recovered["pending_requests"][0]["pattern"] == ["/data/always/**"]
    assert recovered["permission_sync"]["status"] == "ok"
    assert recovered["permission_sync"]["matched"] == 1


def test_successful_empty_list_is_ok_and_distinct_from_failure(agent_env, payload):
    """Successful [] reports ok/empty; it never looks like a failed listing."""
    from workspace_bridge.runtime import RuntimeUnavailable
    job = publish(agent_env, payload)
    run = start(agent_env, job["id"], "empty-ok")
    hold_open(agent_env)
    detail = call(agent_env, "read_opencode_run", run_id=run["run_id"])
    assert detail["pending_request_count"] == 0
    assert detail["permission_sync"]["status"] == "ok"
    assert detail["permission_sync"]["matched"] == 0
    agent_env["runtime"].set_list_pending_error(RuntimeUnavailable("down"))
    try:
        failed = call(agent_env, "read_opencode_run", run_id=run["run_id"])
    finally:
        agent_env["runtime"].set_list_pending_error(None)
    assert failed["permission_sync"]["status"] == "degraded"
    assert failed["permission_sync"]["reason"] == "permission_list_failed"


def test_binding_failure_is_visible_and_fail_closed(agent_env, payload):
    job = publish(agent_env, payload)
    run = start(agent_env, job["id"], "binding-diagnostic")
    hold_open(agent_env)
    agent_env["runtime"].set_session_directory("/somewhere/else")
    try:
        detail = call(agent_env, "read_opencode_run", run_id=run["run_id"])
    finally:
        agent_env["runtime"].set_session_directory(str(agent_env["root"]))
    assert detail["state"] in ("running", "starting")
    assert detail["pending_request_count"] == 0
    assert detail["permission_sync"]["status"] == "degraded"
    assert detail["permission_sync"]["reason"] == "session_binding"
    assert detail["requests"] == []


def test_start_marks_run_degraded_when_event_stream_not_subscribed(agent_env, payload):
    job = publish(agent_env, payload)
    agent_env["runtime"].event_stream_status = "reconnecting"
    try:
        run = start(agent_env, job["id"], "event-degraded-start")
    finally:
        agent_env["runtime"].event_stream_status = "subscribed"
    assert run["permission_sync"] is not None
    assert run["permission_sync"]["status"] == "degraded"
    assert run["permission_sync"]["reason"] == "event_stream_not_subscribed"


def test_runtime_status_exposes_event_stream_health(agent_env, payload):
    status = agent_env["service"].orchestrator.runtime_status()
    assert status["configured"] is True
    assert isinstance(status.get("event_stream"), dict)
    assert status["event_stream"]["status"] == "subscribed"


# ------------------------------------------------- V2 permission generation (0.1.8)
def test_v2_pending_recovers_wait_while_legacy_surface_is_empty(agent_env, payload):
    """Exact live-bug shape: a real V2 pending ask exists for the run session
    while the legacy V1 listing surface successfully returns []."""
    from runtime_fakes import pending_permission_v2
    job = publish(agent_env, payload)
    run = start(agent_env, job["id"], "v2-missed-ask")
    hold_open(agent_env)
    assert call(agent_env, "read_opencode_run", run_id=run["run_id"])["state"] in ("running", "starting")
    agent_env["runtime"].add_pending_permission(
        run["session_id"],
        pending_permission_v2(run["session_id"], "per_v2_live",
                              action="external_directory",
                              resources=["/data/requested/**"],
                              save=["/data/always/**"]))
    detail = call(agent_env, "read_opencode_run", run_id=run["run_id"])
    assert detail["state"] == "waiting_permission" and detail["active"] is True
    assert detail["pending_request_count"] == 1
    pending = detail["pending_requests"][0]
    assert pending["request_id"] == "per_v2_live"
    assert pending["action"] == "external_directory"
    assert pending["pattern"] == ["/data/always/**"]
    assert pending["requested_patterns"] == ["/data/requested/**"]
    assert pending["generation"] == "v2"
    assert agent_env["runtime"].last_permission_source == "v2"
    assert detail["permission_sync"]["status"] == "ok"
    assert detail["permission_sync"]["matched"] == 1
    assert detail["permission_sync"]["source"] == "v2"


def test_v2_asked_event_transitions_without_resync(agent_env, payload):
    from runtime_fakes import permission_event_v2
    job = publish(agent_env, payload)
    run = start(agent_env, job["id"], "v2-live-event")
    before = len(agent_env["runtime"].list_pending_calls)
    agent_env["service"].orchestrator.handle_event(
        permission_event_v2(run["session_id"], "per_v2_evt",
                            resources=["/data/requested/**"],
                            save=["/data/always/**"]))
    assert len(agent_env["runtime"].list_pending_calls) == before
    detail = call(agent_env, "read_opencode_run", run_id=run["run_id"])
    assert detail["state"] == "waiting_permission"
    assert detail["pending_requests"][0]["request_id"] == "per_v2_evt"
    assert detail["pending_requests"][0]["generation"] == "v2"


def test_v2_event_and_snapshot_dedupe_by_request_id(agent_env, payload):
    from runtime_fakes import pending_permission_v2, permission_event_v2
    job = publish(agent_env, payload)
    run = start(agent_env, job["id"], "v2-dedupe")
    agent_env["service"].orchestrator.handle_event(
        permission_event_v2(run["session_id"], "per_v2_dup",
                            resources=["/data/requested/**"],
                            save=["/data/always/**"]))
    agent_env["runtime"].add_pending_permission(
        run["session_id"],
        pending_permission_v2(run["session_id"], "per_v2_dup",
                              resources=["/data/requested/**"],
                              save=["/data/always/**"]))
    detail = call(agent_env, "read_opencode_run", run_id=run["run_id"])
    assert detail["pending_request_count"] == 1
    with agent_env["service"].lock:
        count = agent_env["service"].db.execute(
            "SELECT count(*) FROM agent_requests WHERE run=?", (run["run_id"],)).fetchone()[0]
    assert count == 1


def test_v2_once_reply_routes_v2_and_resumes_same_session(agent_env, payload):
    from runtime_fakes import pending_permission_v2
    job = publish(agent_env, payload)
    run = start(agent_env, job["id"], "v2-respond")
    hold_open(agent_env)
    agent_env["runtime"].add_pending_permission(
        run["session_id"],
        pending_permission_v2(run["session_id"], "per_v2_once",
                              resources=["/data/requested/**"],
                              save=["/data/always/**"]))
    assert call(agent_env, "read_opencode_run", run_id=run["run_id"])["state"] == "waiting_permission"
    response = call(agent_env, "respond_opencode_permission", run_id=run["run_id"],
                    request_id="per_v2_once", decision="once")
    assert response["resumed_same_session"] is True and response["run_state"] == "running"
    assert response["generation"] == "v2"
    assert agent_env["runtime"].respond_calls == [(run["session_id"], "per_v2_once", "once")]
    assert agent_env["runtime"].respond_generations == ["v2"]
    after = call(agent_env, "read_opencode_run", run_id=run["run_id"])
    assert after["state"] == "running" and after["pending_requests"] == []
    assert after["requests"][0]["decision"] == "once" and after["requests"][0]["state"] == "approved"


def test_v1_reply_still_routes_v1(agent_env, payload):
    job = publish(agent_env, payload)
    run = start(agent_env, job["id"], "v1-still-v1")
    permission_wait(agent_env, run)
    response = call(agent_env, "respond_opencode_permission", run_id=run["run_id"],
                    request_id="per_1", decision="once")
    assert response["generation"] == "v1"
    assert agent_env["runtime"].respond_generations == ["v1"]


def test_v2_always_requires_exact_save_scope(agent_env, payload):
    from runtime_fakes import pending_permission_v2
    job = publish(agent_env, payload)
    run = start(agent_env, job["id"], "v2-no-save")
    agent_env["runtime"].add_pending_permission(
        run["session_id"],
        pending_permission_v2(run["session_id"], "per_v2_nosave",
                              resources=["/data/requested/**"]))
    detail = call(agent_env, "read_opencode_run", run_id=run["run_id"])
    assert detail["pending_requests"][0]["pattern"] == []
    assert detail["pending_requests"][0]["requested_patterns"] == ["/data/requested/**"]
    with pytest.raises(BridgeError) as exc:
        call(agent_env, "respond_opencode_permission", run_id=run["run_id"],
             request_id="per_v2_nosave", decision="always")
    assert exc.value.code == "always_scope_unknown"
    assert agent_env["runtime"].respond_calls == []
    assert call(agent_env, "respond_opencode_permission", run_id=run["run_id"],
                request_id="per_v2_nosave", decision="once")["request_state"] == "approved"


def test_wrong_session_v2_request_is_discarded(agent_env, payload):
    from runtime_fakes import pending_permission_v2, permission_event_v2
    job = publish(agent_env, payload)
    run = start(agent_env, job["id"], "v2-cross-session")
    hold_open(agent_env)
    ws = agent_env["service"].workspace(agent_env["id"], False)
    row = agent_env["service"].orchestrator._row(ws, run["run_id"])
    other = pending_permission_v2("ses_other", "per_v2_x",
                                  resources=["/other/**"], save=["/other/**"])
    agent_env["service"].orchestrator._on_permission(
        dict(row), {"id": other.id, "session_id": other.session_id, "action": other.action,
                    "title": "", "pattern": list(other.pattern),
                    "requested_patterns": list(other.requested_patterns),
                    "generation": "v2"})
    agent_env["service"].orchestrator.handle_event(
        permission_event_v2("ses_child_unowned", "per_v2_child",
                            resources=["/child/**"], save=["/child/**"]))
    detail = call(agent_env, "read_opencode_run", run_id=run["run_id"])
    assert detail["pending_request_count"] == 0
    assert detail["state"] in ("running", "starting")


def test_v2_empty_snapshot_never_invents_a_request(agent_env, payload):
    job = publish(agent_env, payload)
    run = start(agent_env, job["id"], "v2-empty")
    detail = call(agent_env, "read_opencode_run", run_id=run["run_id"])
    assert detail["pending_request_count"] == 0
    assert detail["requests"] == []
    assert detail["permission_sync"]["status"] == "ok"
    assert detail["permission_sync"]["matched"] == 0
    assert detail["permission_sync"]["source"] == "v2"


def test_legacy_v1_rows_without_generation_keep_working_as_v1(agent_env, payload):
    job = publish(agent_env, payload)
    run = start(agent_env, job["id"], "legacy-row")
    permission_wait(agent_env, run)
    with agent_env["service"].lock:
        agent_env["service"].db.execute(
            "UPDATE agent_requests SET generation='v1' WHERE run=?", (run["run_id"],))
        agent_env["service"].db.commit()
    response = call(agent_env, "respond_opencode_permission", run_id=run["run_id"],
                    request_id="per_1", decision="reject")
    assert response["request_state"] == "rejected" and response["generation"] == "v1"
    assert agent_env["runtime"].respond_generations == ["v1"]


def test_unknown_stored_generation_fails_closed(agent_env, payload):
    import sqlite3
    job = publish(agent_env, payload)
    run = start(agent_env, job["id"], "bad-generation")
    permission_wait(agent_env, run)
    with agent_env["service"].lock:
        try:
            agent_env["service"].db.execute(
                "UPDATE agent_requests SET generation='v9' WHERE run=?", (run["run_id"],))
            agent_env["service"].db.commit()
        except sqlite3.IntegrityError:
            pass
        else:
            with pytest.raises(BridgeError) as exc:
                call(agent_env, "respond_opencode_permission", run_id=run["run_id"],
                     request_id="per_1", decision="once")
            assert exc.value.code == "incompatible_request"
            assert agent_env["runtime"].respond_calls == []
            return
    detail = call(agent_env, "read_opencode_run", run_id=run["run_id"])
    assert detail["pending_requests"][0]["generation"] == "v1"


def test_permission_logs_carry_generation_without_scopes(agent_env, payload):
    import json
    import logging
    from runtime_fakes import pending_permission_v2, permission_event_v2
    job = publish(agent_env, payload)
    run = start(agent_env, job["id"], "log-hygiene")
    lines: list[str] = []

    class Capture(logging.Handler):
        def emit(self, record):
            lines.append(record.getMessage())

    logger = logging.getLogger("workspace_bridge.ops")
    handler = Capture()
    previous_level = logger.level
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    try:
        agent_env["service"].orchestrator.handle_event(
            permission_event_v2(run["session_id"], "per_v2_log",
                                resources=["/secret/requested/**"],
                                save=["/secret/always/**"]))
        agent_env["runtime"].add_pending_permission(
            run["session_id"],
            pending_permission_v2(run["session_id"], "per_v2_log",
                                  resources=["/secret/requested/**"],
                                  save=["/secret/always/**"]))
        call(agent_env, "read_opencode_run", run_id=run["run_id"])
        call(agent_env, "respond_opencode_permission", run_id=run["run_id"],
             request_id="per_v2_log", decision="once")
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)
    assert lines, "expected operational log records"
    blob = "\n".join(lines)
    assert "/secret/requested" not in blob and "/secret/always" not in blob
    assert "resources" not in blob and "metadata" not in blob
    assert '"generation":"v2"' in blob.replace(" ", "")
