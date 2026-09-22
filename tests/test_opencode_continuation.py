"""Session continuation: reuse a completed run's OpenCode session for a new handoff.

All runtime interaction goes through the scripted fake: no Node, network,
provider credentials, Discord or real model is required.
"""
import json

import pytest

from workspace_bridge.api import Handoff, TOOLS
from workspace_bridge.runtime import MessageInfo, RuntimeUnavailable
from workspace_bridge.security import BridgeError
from workspace_bridge.service import Service

from runtime_fakes import permission_event


def publish(agent_env, payload, **overrides):
    body = {**payload, **overrides}
    return agent_env["service"].call(agent_env["id"], agent_env["token"], "prepare_handoff",
                                     Handoff.model_validate(body).model_dump())


def start(agent_env, job_id, request_id="run-request-1", model=None, parent_run_id=None,
          continue_from_run_id=None):
    return agent_env["service"].call(agent_env["id"], agent_env["token"], "start_opencode_run",
                                     {"job_id": job_id, "request_id": request_id,
                                      "model": model, "parent_run_id": parent_run_id,
                                      "continue_from_run_id": continue_from_run_id})


def call(agent_env, tool, **args):
    return agent_env["service"].call(agent_env["id"], agent_env["token"], tool, args)


def complete(agent_env, run):
    agent_env["service"].orchestrator.handle_event(
        {"type": "session.idle", "session_id": run["session_id"]})
    return call(agent_env, "read_opencode_run", run_id=run["run_id"])


def publish_and_complete(agent_env, payload, job_overrides=None, request_id="first-run"):
    job = publish(agent_env, payload, **(job_overrides or {}))
    run = start(agent_env, job["id"], request_id)
    detail = complete(agent_env, run)
    assert detail["state"] == "completed"
    return job, run


# ------------------------------------------------------------- tool surface
def test_tool_count_unchanged_and_continue_exposed(agent_env):
    assert len(TOOLS) == 21
    schema = TOOLS["start_agent_run"][0].model_json_schema()
    assert "continue_from_run_id" in schema["properties"]
    assert "SAME runtime" in TOOLS["start_agent_run"][1]


def test_fresh_start_creates_session_and_reports_not_reused(agent_env, payload):
    job = publish(agent_env, payload)
    run = start(agent_env, job["id"])
    assert run["session_reused"] is False
    assert run["continue_from_run_id"] is None
    assert run["parent_run_id"] is None
    assert len(agent_env["runtime"].sessions) == 1


# ------------------------------------------------------- happy-path reuse
def test_continuation_reuses_session_without_create(agent_env, payload):
    job, first = publish_and_complete(agent_env, payload)
    job2 = publish(agent_env, payload, request_id="example-2", title="Fix follow-up")
    sessions_before = len(agent_env["runtime"].sessions)
    prompts_before = len(agent_env["runtime"].prompts)
    second = start(agent_env, job2["id"], "second-run", continue_from_run_id=first["run_id"])
    assert second["session_id"] == first["session_id"]
    assert second["session_reused"] is True
    assert second["parent_run_id"] == first["run_id"]
    assert second["continue_from_run_id"] == first["run_id"]
    assert second["run_id"] != first["run_id"]
    assert second["job_id"] == job2["id"]
    assert second["model"] == first["model"]
    assert len(agent_env["runtime"].sessions) == sessions_before
    assert len(agent_env["runtime"].prompts) == prompts_before + 1
    latest = agent_env["runtime"].prompts[-1]
    assert latest["session"] == first["session_id"]
    assert "follow-up" in latest["text"]
    assert latest["model"] == {"providerID": "anthropic", "modelID": "claude-sonnet"}


def test_continuation_parent_lineage_and_conflict(agent_env, payload):
    _, first = publish_and_complete(agent_env, payload)
    job2 = publish(agent_env, payload, request_id="example-2", title="Follow-up")
    with pytest.raises(BridgeError) as exc:
        start(agent_env, job2["id"], "conflict-parent", parent_run_id="run_" + "0" * 24,
              continue_from_run_id=first["run_id"])
    assert exc.value.code == "invalid_arguments"
    ok = start(agent_env, job2["id"], "equal-parent", parent_run_id=first["run_id"],
               continue_from_run_id=first["run_id"])
    assert ok["parent_run_id"] == first["run_id"]


# ------------------------------------------------------- model semantics
def test_continuation_inherits_source_model_despite_default_change(agent_env, payload):
    _, first = publish_and_complete(agent_env, payload)
    assert first["model"] == "anthropic/claude-sonnet"
    agent_env["service"].orchestrator.set_model_policy(
        ["anthropic/claude-sonnet", "glm/zai-glm-5.2"], "glm/zai-glm-5.2")
    job2 = publish(agent_env, payload, request_id="example-2", title="Follow-up")
    second = start(agent_env, job2["id"], "inherit-model", continue_from_run_id=first["run_id"])
    assert second["model"] == "anthropic/claude-sonnet"
    assert agent_env["runtime"].prompts[-1]["model"] == {
        "providerID": "anthropic", "modelID": "claude-sonnet"}


def test_continuation_explicit_model_must_match_source(agent_env, payload):
    _, first = publish_and_complete(agent_env, payload)
    job2 = publish(agent_env, payload, request_id="example-2", title="Follow-up")
    same = start(agent_env, job2["id"], "same-model", model="anthropic/claude-sonnet",
                 continue_from_run_id=first["run_id"])
    assert same["model"] == "anthropic/claude-sonnet"
    job3 = publish(agent_env, payload, request_id="example-3", title="Follow-up 2")
    with pytest.raises(BridgeError) as exc:
        start(agent_env, job3["id"], "other-model", model="glm/zai-glm-5.2",
              continue_from_run_id=first["run_id"])
    assert exc.value.code == "continuation_model_mismatch"
    assert agent_env["service"].db.execute("SELECT count(*) FROM agent_runs").fetchone()[0] == 2


def test_continuation_blocked_when_source_model_disabled_or_gone(agent_env, payload):
    _, first = publish_and_complete(agent_env, payload)
    agent_env["service"].orchestrator.set_model_policy(["glm/zai-glm-5.2"], "glm/zai-glm-5.2")
    job2 = publish(agent_env, payload, request_id="example-2", title="Follow-up")
    with pytest.raises(BridgeError) as exc:
        start(agent_env, job2["id"], "disabled-model", continue_from_run_id=first["run_id"])
    assert exc.value.code == "model_not_enabled"
    agent_env["service"].orchestrator.set_model_policy(
        ["anthropic/claude-sonnet", "glm/zai-glm-5.2"], "anthropic/claude-sonnet")
    agent_env["runtime"].models = [m for m in agent_env["runtime"].models
                                   if m.selector != "anthropic/claude-sonnet"]
    job3 = publish(agent_env, payload, request_id="example-3", title="Follow-up 2")
    with pytest.raises(BridgeError) as exc:
        start(agent_env, job3["id"], "gone-model", continue_from_run_id=first["run_id"])
    assert exc.value.code == "model_unavailable"


# ------------------------------------------------------- source eligibility
def test_non_completed_sources_cannot_continue(agent_env, payload):
    job = publish(agent_env, payload)
    running = start(agent_env, job["id"], "live-source")
    job2 = publish(agent_env, payload, request_id="example-2", title="Follow-up")
    with pytest.raises(BridgeError) as exc:
        start(agent_env, job2["id"], "continue-running", continue_from_run_id=running["run_id"])
    assert exc.value.code == "continuation_unavailable"
    agent_env["service"].orchestrator.handle_event(
        permission_event(running["session_id"], "per_1"))
    with pytest.raises(BridgeError) as exc:
        start(agent_env, job2["id"], "continue-waiting", continue_from_run_id=running["run_id"])
    assert exc.value.code == "continuation_unavailable"
    call(agent_env, "respond_opencode_permission", run_id=running["run_id"],
         request_id="per_1", decision="once")
    agent_env["service"].orchestrator.handle_event(
        {"type": "session.error", "session_id": running["session_id"],
         "error": {"name": "ApiError", "message": "boom"}})
    with pytest.raises(BridgeError) as exc:
        start(agent_env, job2["id"], "continue-failed", continue_from_run_id=running["run_id"])
    assert exc.value.code == "continuation_unavailable"
    job3 = publish(agent_env, payload, request_id="example-3", title="Cancelled")
    cancelled = start(agent_env, job3["id"], "to-cancel")
    call(agent_env, "cancel_opencode_run", run_id=cancelled["run_id"])
    with pytest.raises(BridgeError) as exc:
        start(agent_env, job2["id"], "continue-cancelled",
              continue_from_run_id=cancelled["run_id"])
    assert exc.value.code == "continuation_unavailable"


def test_foreign_source_run_is_rejected(agent_env, payload):
    other = agent_env["parent"] / "beta"
    other.mkdir()
    beta = agent_env["service"].add_workspace("Beta", str(other), [])["workspace"]["id"]
    agent_env["service"].manage_workspace(beta, "enable")
    agent_env["service"].manage_workspace(beta, "set_agent_enabled", agent_enabled=True)
    foreign_job = agent_env["service"].call(
        beta, agent_env["token"], "prepare_handoff", Handoff.model_validate(payload).model_dump())
    foreign_run = agent_env["service"].call(
        beta, agent_env["token"], "start_opencode_run",
        {"job_id": foreign_job["id"], "request_id": "foreign-1", "model": None,
         "parent_run_id": None, "continue_from_run_id": None})
    job = publish(agent_env, payload)
    with pytest.raises(BridgeError) as exc:
        start(agent_env, job["id"], "foreign-source", continue_from_run_id=foreign_run["run_id"])
    assert exc.value.code == "continuation_unavailable"


@pytest.mark.parametrize("failure", ["missing", "mismatch", "busy", "retry"])
def test_binding_and_status_failures_create_nothing(agent_env, payload, failure):
    _, first = publish_and_complete(agent_env, payload)
    runs_before = agent_env["service"].db.execute("SELECT count(*) FROM agent_runs").fetchone()[0]
    sessions_before = len(agent_env["runtime"].sessions)
    prompts_before = len(agent_env["runtime"].prompts)
    if failure == "missing":
        agent_env["runtime"].session_missing = True
    elif failure == "mismatch":
        agent_env["runtime"].set_session_directory("/somewhere/else")
    else:
        agent_env["runtime"].set_session_status(failure)
    try:
        job2 = publish(agent_env, payload, request_id="example-2", title="Follow-up")
        with pytest.raises(BridgeError) as exc:
            start(agent_env, job2["id"], f"blocked-{failure}",
                  continue_from_run_id=first["run_id"])
        assert exc.value.code in ("session_missing", "session_mismatch", "session_busy",
                                  "continuation_unavailable")
        assert agent_env["service"].db.execute(
            "SELECT count(*) FROM agent_runs").fetchone()[0] == runs_before
        assert len(agent_env["runtime"].sessions) == sessions_before
        assert len(agent_env["runtime"].prompts) == prompts_before
    finally:
        agent_env["runtime"].session_missing = False
        agent_env["runtime"].set_session_directory(str(agent_env["root"]))
        agent_env["runtime"].statuses.clear()


def test_malformed_status_blocks_continuation(agent_env, payload):
    _, first = publish_and_complete(agent_env, payload)
    agent_env["runtime"].statuses[first["session_id"]] = "weird"
    job2 = publish(agent_env, payload, request_id="example-2", title="Follow-up")
    with pytest.raises(BridgeError) as exc:
        start(agent_env, job2["id"], "malformed-status", continue_from_run_id=first["run_id"])
    assert exc.value.code == "continuation_unavailable"
    agent_env["runtime"].statuses.clear()


def test_unreliable_boundary_blocks_continuation(agent_env, payload):
    _, first = publish_and_complete(agent_env, payload)
    agent_env["runtime"].messages_script = [
        MessageInfo(id="u", role="user", text="no timestamps"),
        MessageInfo(id="a", role="assistant", text="no timestamps either"),
    ]
    job2 = publish(agent_env, payload, request_id="example-2", title="Follow-up")
    with pytest.raises(BridgeError) as exc:
        start(agent_env, job2["id"], "no-boundary", continue_from_run_id=first["run_id"])
    assert exc.value.code == "continuation_unavailable"


# ------------------------------------------------------- active-session invariant
def test_second_continuation_while_active_is_rejected(agent_env, payload):
    _, first = publish_and_complete(agent_env, payload)
    job2 = publish(agent_env, payload, request_id="example-2", title="Follow-up")
    second = start(agent_env, job2["id"], "second", continue_from_run_id=first["run_id"])
    assert second["state"] in ("starting", "running")
    job3 = publish(agent_env, payload, request_id="example-3", title="Follow-up 2")
    with pytest.raises(BridgeError) as exc:
        start(agent_env, job3["id"], "third", continue_from_run_id=first["run_id"])
    assert exc.value.code == "continuation_unavailable"
    with pytest.raises(BridgeError) as exc:
        start(agent_env, job3["id"], "third-fresh-source",
              continue_from_run_id=second["run_id"])
    assert exc.value.code == "continuation_unavailable"


def test_events_route_only_to_active_run(agent_env, payload):
    _, first = publish_and_complete(agent_env, payload)
    job2 = publish(agent_env, payload, request_id="example-2", title="Follow-up")
    second = start(agent_env, job2["id"], "second", continue_from_run_id=first["run_id"])
    agent_env["service"].orchestrator.handle_event(
        permission_event(second["session_id"], "per_child"))
    parent = call(agent_env, "read_opencode_run", run_id=first["run_id"])
    child = call(agent_env, "read_opencode_run", run_id=second["run_id"])
    assert parent["state"] == "completed" and parent["pending_request_count"] == 0
    assert child["state"] == "waiting_permission"
    assert child["pending_requests"][0]["request_id"] == "per_child"
    agent_env["service"].orchestrator.handle_event(
        {"type": "session.idle", "session_id": second["session_id"]})
    parent_after = call(agent_env, "read_opencode_run", run_id=first["run_id"])
    assert parent_after["state"] == "completed"


def test_permission_during_continuation_attaches_to_continuation(agent_env, payload):
    _, first = publish_and_complete(agent_env, payload)
    job2 = publish(agent_env, payload, request_id="example-2", title="Follow-up")
    second = start(agent_env, job2["id"], "second", continue_from_run_id=first["run_id"])
    agent_env["service"].orchestrator.handle_event(
        permission_event(second["session_id"], "per_c", pattern=["/new/**"]))
    response = call(agent_env, "respond_opencode_permission", run_id=second["run_id"],
                    request_id="per_c", decision="once")
    assert response["run_state"] == "running"
    assert response["resumed_same_session"] is True
    parent = call(agent_env, "read_opencode_run", run_id=first["run_id"])
    assert parent["requests"] == [] or all(
        r["request_id"] != "per_c" for r in parent["requests"])


# ------------------------------------------------------- iteration scoping
def test_stale_idle_cannot_complete_continuation(agent_env, payload):
    _, first = publish_and_complete(agent_env, payload)
    job2 = publish(agent_env, payload, request_id="example-2", title="Follow-up")
    second = start(agent_env, job2["id"], "second", continue_from_run_id=first["run_id"])
    agent_env["service"].orchestrator.handle_event(
        {"type": "session.idle", "session_id": second["session_id"]})
    detail = call(agent_env, "read_opencode_run", run_id=second["run_id"])
    assert detail["state"] in ("starting", "running")
    assert detail["result"]["has_final_response"] is False


def test_completion_result_and_count_are_iteration_scoped(agent_env, payload):
    _, first = publish_and_complete(agent_env, payload)
    job2 = publish(agent_env, payload, request_id="example-2", title="Follow-up")
    second = start(agent_env, job2["id"], "second", continue_from_run_id=first["run_id"])
    agent_env["runtime"].messages_script = [
        MessageInfo(id="m1", role="user", created=1),
        MessageInfo(id="m2", role="assistant", created=2, completed=3,
                    text="Implemented the change and ran the tests."),
        MessageInfo(id="m3", role="user", created=10, text="follow-up"),
        MessageInfo(id="m4", role="assistant", created=11, completed=12,
                    text="Fixed the follow-up."),
    ]
    agent_env["service"].orchestrator.handle_event(
        {"type": "session.idle", "session_id": second["session_id"]})
    detail = call(agent_env, "read_opencode_run", run_id=second["run_id"])
    assert detail["state"] == "completed"
    assert detail["result"]["summary"] == "Fixed the follow-up."
    assert detail["result"]["message_count"] == 2
    transcript = call(agent_env, "read_opencode_run", run_id=second["run_id"],
                      include_transcript=True)["transcript"]
    assert [entry["id"] for entry in transcript] == ["m3", "m4"]


def test_parent_transcript_never_shows_child_messages(agent_env, payload):
    _, first = publish_and_complete(agent_env, payload)
    job2 = publish(agent_env, payload, request_id="example-2", title="Follow-up")
    second = start(agent_env, job2["id"], "second", continue_from_run_id=first["run_id"])
    agent_env["runtime"].messages_script = [
        MessageInfo(id="m1", role="user", created=1),
        MessageInfo(id="m2", role="assistant", created=2, completed=3, text="first done"),
        MessageInfo(id="m3", role="user", created=10, text="follow-up"),
        MessageInfo(id="m4", role="assistant", created=11, completed=12, text="child done"),
    ]
    agent_env["service"].orchestrator.handle_event(
        {"type": "session.idle", "session_id": second["session_id"]})
    parent = call(agent_env, "read_opencode_run", run_id=first["run_id"],
                  include_transcript=True)
    assert [entry["id"] for entry in parent["transcript"]] == ["m1", "m2"]
    child = call(agent_env, "read_opencode_run", run_id=second["run_id"],
                 include_transcript=True)
    assert [entry["id"] for entry in child["transcript"]] == ["m3", "m4"]


def test_active_continuation_transcript_is_live_and_scoped(agent_env, payload):
    _, first = publish_and_complete(agent_env, payload)
    job2 = publish(agent_env, payload, request_id="example-2", title="Follow-up")
    second = start(agent_env, job2["id"], "second", continue_from_run_id=first["run_id"])
    assert second["state"] in ("starting", "running")
    agent_env["runtime"].messages_script = [
        MessageInfo(id="m1", role="user", created=1),
        MessageInfo(id="m2", role="assistant", created=2, completed=3, text="first done"),
        MessageInfo(id="m3", role="user", created=10, text="follow-up so far"),
    ]
    transcript = call(agent_env, "read_opencode_run", run_id=second["run_id"],
                      include_transcript=True)["transcript"]
    assert [entry["id"] for entry in transcript] == ["m3"]


# ------------------------------------------------------- idempotency
def test_continuation_request_id_replay_is_idempotent(agent_env, payload):
    _, first = publish_and_complete(agent_env, payload)
    job2 = publish(agent_env, payload, request_id="example-2", title="Follow-up")
    one = start(agent_env, job2["id"], "replay-me", continue_from_run_id=first["run_id"])
    two = start(agent_env, job2["id"], "replay-me", continue_from_run_id=first["run_id"])
    assert one["run_id"] == two["run_id"] and two["idempotent_replay"] is True
    assert len(agent_env["runtime"].prompts) == 2  # first run + one continuation
    job3 = publish(agent_env, payload, request_id="example-3", title="Other")
    with pytest.raises(BridgeError) as exc:
        start(agent_env, job3["id"], "replay-me", continue_from_run_id=first["run_id"])
    assert exc.value.code == "conflict"


def test_restart_reconcile_treats_active_continuation_like_any_run(agent_env, payload):
    _, first = publish_and_complete(agent_env, payload)
    job2 = publish(agent_env, payload, request_id="example-2", title="Follow-up")
    second = start(agent_env, job2["id"], "second", continue_from_run_id=first["run_id"])
    reopened = Service(agent_env["state"], agent_env["config"], runtime=agent_env["runtime"],
                       notifier=agent_env["notifier"], orchestrator_background=False)
    try:
        reopened.orchestrator.reconcile_startup()
        detail = reopened.call(agent_env["id"], agent_env["token"], "read_opencode_run",
                               {"run_id": second["run_id"]})
        # Only pre-continuation history exists: stays active, never completes stale.
        assert detail["state"] in ("starting", "running")
        assert detail["result"]["has_final_response"] is False
    finally:
        reopened.close()


def test_continuation_result_payload_persists_scoped_transcript(agent_env, payload):
    _, first = publish_and_complete(agent_env, payload)
    job2 = publish(agent_env, payload, request_id="example-2", title="Follow-up")
    second = start(agent_env, job2["id"], "second", continue_from_run_id=first["run_id"])
    agent_env["runtime"].messages_script = [
        MessageInfo(id="m1", role="user", created=1),
        MessageInfo(id="m2", role="assistant", created=2, completed=3, text="first done"),
        MessageInfo(id="m3", role="assistant", created=11, completed=12, text="child done"),
    ]
    agent_env["service"].orchestrator.handle_event(
        {"type": "session.idle", "session_id": second["session_id"]})
    row = agent_env["service"].db.execute(
        "SELECT transcript FROM agent_runs WHERE id=?", (second["run_id"],)).fetchone()
    assert [entry["id"] for entry in json.loads(row["transcript"])] == ["m3"]
