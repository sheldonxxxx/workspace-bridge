"""Live completion recovery: missed session.idle self-heals without restart.

A run whose agent job actually finished must become completed during
normal operation from durable completed-assistant-message evidence, even
when session.idle was never observed. Live events (session.idle,
session.status idle) are latency hints only; status alone never
completes and a lagging busy status never blocks durable evidence.

All runtime interaction goes through a scripted fake: no Node, network,
provider credentials, Discord or real model is required.
"""
import logging

from workspace_bridge.api import Handoff
from workspace_bridge.runtime import MessageInfo, RuntimeUnavailable
from workspace_bridge.service import Service

from runtime_fakes import permission_event


def publish(agent_env, payload, **overrides):
    body = {**payload, **overrides}
    return agent_env["service"].call(agent_env["id"], agent_env["token"], "prepare_handoff",
                                     Handoff.model_validate(body).model_dump())


def start(agent_env, job_id, request_id="run-request-1", **kwargs):
    return agent_env["service"].call(agent_env["id"], agent_env["token"], "start_agent_run",
                                     {"runtime": "pi", "job_id": job_id, "request_id": request_id,
                                      "model": kwargs.get("model"),
                                      "parent_run_id": kwargs.get("parent_run_id"),
                                      "continue_from_run_id": kwargs.get("continue_from_run_id")})


def call(agent_env, tool, **args):
    return agent_env["service"].call(agent_env["id"], agent_env["token"], tool, args)


def completed_messages(text="Implemented the change and ran the tests."):
    return [MessageInfo(id="m1", role="user", created=1),
            MessageInfo(id="m2", role="assistant", created=2, completed=3,
                        text=text, tools=("edit", "bash"))]


def completed_notifications(agent_env, run_id):
    return [c for c in agent_env["notifier"].calls
            if c.get("state") == "completed" and c.get("run_id") == run_id]


# ------------------------------------------------- read self-heals a missed idle
def test_read_heals_missed_idle_without_restart(agent_env, payload):
    """The exact live bug: no session.idle ever delivered, durable final exists."""
    job = publish(agent_env, payload)
    run = start(agent_env, job["id"], "missed-idle-read")
    agent_env["runtime"].messages_script = completed_messages()
    # No event is delivered at all: the run is still active.
    detail = call(agent_env, "read_agent_run", run_id=run["run_id"])
    assert detail["state"] == "completed"
    assert detail["result"]["has_final_response"] is True
    assert detail["result"]["reason"] == "read_reconcile"
    assert detail["result"]["message_count"] == 2
    assert detail["active"] is False


def test_background_sweep_heals_without_read_or_restart(agent_env, payload):
    job = publish(agent_env, payload)
    run = start(agent_env, job["id"], "missed-idle-sweep")
    agent_env["runtime"].messages_script = completed_messages()
    agent_env["service"].orchestrator._background_completion_reconcile()
    with agent_env["service"].lock:
        state = agent_env["service"].db.execute(
            "SELECT state FROM agent_runs WHERE id=?", (run["run_id"],)).fetchone()["state"]
    assert state == "completed"
    detail = call(agent_env, "read_agent_run", run_id=run["run_id"])
    assert detail["state"] == "completed"
    assert detail["result"]["reason"] == "background_reconcile"


# ------------------------------------------------- status hints, never verdicts
def test_status_idle_hint_completes_with_evidence(agent_env, payload):
    job = publish(agent_env, payload)
    run = start(agent_env, job["id"], "status-idle-evidence")
    agent_env["runtime"].messages_script = completed_messages()
    agent_env["service"].orchestrator.handle_event(
        {"type": "session.status", "session_id": run["session_id"],
         "data": {"status": "idle"}})
    detail = call(agent_env, "read_agent_run", run_id=run["run_id"])
    assert detail["state"] == "completed"
    assert detail["result"]["reason"] == "status_idle"


def test_status_idle_without_evidence_stays_active(agent_env, payload):
    job = publish(agent_env, payload)
    run = start(agent_env, job["id"], "status-idle-no-evidence")
    agent_env["runtime"].messages_script = [MessageInfo(id="u", role="user", created=1)]
    agent_env["service"].orchestrator.handle_event(
        {"type": "session.status", "session_id": run["session_id"],
         "data": {"status": "idle"}})
    detail = call(agent_env, "read_agent_run", run_id=run["run_id"])
    assert detail["state"] in ("running", "starting")
    assert detail["result"]["has_final_response"] is False


def test_status_busy_event_never_completes_but_read_does(agent_env, payload):
    """Known busy-status lag: the busy hint changes nothing, durable evidence still wins."""
    job = publish(agent_env, payload)
    run = start(agent_env, job["id"], "busy-lag")
    agent_env["runtime"].messages_script = completed_messages()
    agent_env["runtime"].set_session_status("busy", run["session_id"])
    try:
        agent_env["service"].orchestrator.handle_event(
            {"type": "session.status", "session_id": run["session_id"],
             "data": {"status": "busy"}})
        # The busy hint alone changes nothing: verify without a read probe.
        with agent_env["service"].lock:
            mid_state = agent_env["service"].db.execute(
                "SELECT state FROM agent_runs WHERE id=?", (run["run_id"],)).fetchone()["state"]
        assert mid_state in ("running", "starting")
        # The read itself probes durable messages and completes despite busy.
        mid = call(agent_env, "read_agent_run", run_id=run["run_id"])
        assert mid["state"] == "completed"
        assert mid["result"]["reason"] == "read_reconcile"
    finally:
        agent_env["runtime"].set_session_status("idle", run["session_id"])


def test_busy_status_alone_with_evidence_completes_on_background(agent_env, payload):
    job = publish(agent_env, payload)
    run = start(agent_env, job["id"], "busy-background")
    agent_env["runtime"].messages_script = completed_messages()
    agent_env["runtime"].set_session_status("busy", run["session_id"])
    try:
        agent_env["service"].orchestrator._background_completion_reconcile()
        detail = call(agent_env, "read_agent_run", run_id=run["run_id"])
        assert detail["state"] == "completed"
    finally:
        agent_env["runtime"].set_session_status("idle", run["session_id"])


# ------------------------------------------------- terminal-message selection
def test_later_incomplete_assistant_blocks_earlier_completed(agent_env, payload):
    """An earlier completed turn must not complete a still-active later turn."""
    job = publish(agent_env, payload)
    run = start(agent_env, job["id"], "later-incomplete")
    agent_env["runtime"].messages_script = [
        MessageInfo(id="u", role="user", created=1),
        MessageInfo(id="a1", role="assistant", created=2, completed=3, text="first done"),
        MessageInfo(id="a2", role="assistant", created=4, completed=None, text="still working"),
    ]
    agent_env["service"].orchestrator.handle_event(
        {"type": "session.idle", "session_id": run["session_id"]})
    detail = call(agent_env, "read_agent_run", run_id=run["run_id"])
    assert detail["state"] in ("running", "starting")
    assert detail["result"]["has_final_response"] is False


def test_error_assistant_never_completes(agent_env, payload):
    job = publish(agent_env, payload)
    run = start(agent_env, job["id"], "error-assistant")
    agent_env["runtime"].messages_script = [
        MessageInfo(id="u", role="user", created=1),
        MessageInfo(id="a", role="assistant", created=2, completed=3,
                    text="boom", error="ProviderAuthError"),
    ]
    agent_env["service"].orchestrator.handle_event(
        {"type": "session.idle", "session_id": run["session_id"]})
    detail = call(agent_env, "read_agent_run", run_id=run["run_id"])
    assert detail["state"] in ("running", "starting")
    assert detail["result"]["has_final_response"] is False


# ------------------------------------------------- waiting and floor isolation
def test_waiting_runs_never_complete_from_durable_evidence(agent_env, payload):
    for kind, request_id in (("permission", "per_w"), ("question", "q_w")):
        job = publish(agent_env, payload, request_id=f"wait-{kind}-{request_id}",
                      title=f"Wait {kind} {request_id}")
        run = start(agent_env, job["id"], f"wait-{kind}")
        if kind == "permission":
            event = permission_event(run["session_id"], request_id, pattern=["/x/**"])
            agent_env["service"].orchestrator.handle_event(event)
        else:
            agent_env["service"].orchestrator.handle_event(
                {"type": "question.asked", "session_id": run["session_id"],
                 "data": {"id": request_id, "action": "choose"}})
        agent_env["runtime"].messages_script = completed_messages()
        agent_env["service"].orchestrator._background_completion_reconcile()
        detail = call(agent_env, "read_agent_run", run_id=run["run_id"])
        assert detail["state"] == ("waiting_permission" if kind == "permission"
                                   else "waiting_question"), kind
        assert detail["result"]["has_final_response"] is False


def test_stale_precontinuation_output_never_completes_continuation(agent_env, payload):
    job = publish(agent_env, payload)
    first = start(agent_env, job["id"], "floor-first")
    agent_env["runtime"].messages_script = completed_messages("first iteration done")
    assert call(agent_env, "read_agent_run", run_id=first["run_id"])["state"] == "completed"
    job2 = publish(agent_env, payload, request_id="example-2", title="Follow-up")
    second = start(agent_env, job2["id"], "floor-second",
                   continue_from_run_id=first["run_id"])
    # Only pre-continuation history exists: the continuation must stay active.
    agent_env["service"].orchestrator._background_completion_reconcile()
    detail = call(agent_env, "read_agent_run", run_id=second["run_id"])
    assert detail["state"] in ("running", "starting")
    assert detail["result"]["has_final_response"] is False
    # A genuinely new completed turn after the floor completes it.
    with agent_env["service"].lock:
        floor = agent_env["service"].db.execute(
            "SELECT message_floor_ms FROM agent_runs WHERE id=?",
            (second["run_id"],)).fetchone()["message_floor_ms"]
    agent_env["runtime"].messages_script = completed_messages("first iteration done") + [
        MessageInfo(id="m3", role="assistant", created=int(floor) + 10,
                    completed=int(floor) + 11, text="second iteration done")]
    detail = call(agent_env, "read_agent_run", run_id=second["run_id"])
    assert detail["state"] == "completed"
    assert detail["result"]["summary"] == "second iteration done"


# ------------------------------------------------- idempotency and retryability
def test_repeated_probes_notify_exactly_once(agent_env, payload):
    job = publish(agent_env, payload)
    run = start(agent_env, job["id"], "idempotent-completion")
    agent_env["runtime"].messages_script = completed_messages()
    orch = agent_env["service"].orchestrator
    first = call(agent_env, "read_agent_run", run_id=run["run_id"])
    assert first["state"] == "completed"
    orch._background_completion_reconcile()
    orch.handle_event({"type": "session.idle", "session_id": run["session_id"]})
    orch.handle_event({"type": "session.status", "session_id": run["session_id"],
                       "data": {"status": "idle"}})
    second = call(agent_env, "read_agent_run", run_id=run["run_id"])
    assert second["state"] == "completed"
    assert completed_notifications(agent_env, run["run_id"]) and \
        len(completed_notifications(agent_env, run["run_id"])) == 1


def test_runtime_unavailable_leaves_run_active_and_retryable(agent_env, payload):
    job = publish(agent_env, payload)
    run = start(agent_env, job["id"], "probe-unavailable")
    original = agent_env["runtime"].messages

    def boom(directory, session_id, limit=40):
        raise RuntimeUnavailable("adapter is down")

    agent_env["runtime"].messages = boom
    try:
        detail = call(agent_env, "read_agent_run", run_id=run["run_id"])
        assert detail["state"] in ("running", "starting")
        assert detail["error"] is None
        agent_env["service"].orchestrator._background_completion_reconcile()
        detail = call(agent_env, "read_agent_run", run_id=run["run_id"])
        assert detail["state"] in ("running", "starting")
    finally:
        agent_env["runtime"].messages = original
    agent_env["runtime"].messages_script = completed_messages()
    detail = call(agent_env, "read_agent_run", run_id=run["run_id"])
    assert detail["state"] == "completed"


def test_restarted_inflight_run_joins_background_sweep(agent_env, payload):
    """Startup reconciliation without a final stops retrying, yet the run
    still completes later via the normal background sweep (no 2nd restart)."""
    job = publish(agent_env, payload)
    run = start(agent_env, job["id"], "restart-sweep")
    agent_env["runtime"].messages_script = [MessageInfo(id="u", role="user", created=1)]
    reopened = Service(agent_env["state"], agent_env["config"], runtime=agent_env["runtime"],
                       notifier=agent_env["notifier"], orchestrator_background=False)
    try:
        reopened.orchestrator.reconcile_startup()
        assert reopened.orchestrator.retry_reconcile() == []
        detail = reopened.call(agent_env["id"], agent_env["token"], "read_agent_run",
                               {"run_id": run["run_id"]})
        assert detail["state"] in ("running", "starting")
        agent_env["runtime"].messages_script = completed_messages()
        reopened.orchestrator._background_completion_reconcile()
        detail = reopened.call(agent_env["id"], agent_env["token"], "read_agent_run",
                               {"run_id": run["run_id"]})
        assert detail["state"] == "completed"
        assert detail["result"]["reason"] == "background_reconcile"
    finally:
        reopened.close()


# ------------------------------------------------- operational log hygiene
def test_probe_logs_reason_and_count_without_response_text(agent_env, payload):
    secret = "probe secret response " + "x" * 40
    job = publish(agent_env, payload)
    run = start(agent_env, job["id"], "probe-log-hygiene")
    agent_env["runtime"].messages_script = completed_messages(secret)
    lines: list[str] = []

    class Capture(logging.Handler):
        def emit(self, record):
            lines.append(record.getMessage())

    logger = logging.getLogger("workspace_bridge.ops")
    handler = Capture()
    previous_level = logger.level
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    try:
        detail = call(agent_env, "read_agent_run", run_id=run["run_id"])
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)
    assert detail["state"] == "completed"
    assert lines, "expected operational log records"
    blob = "\n".join(lines)
    assert secret not in blob
    assert "read_reconcile" in blob
    assert "message_count" in blob or "count" in blob


# ------------------------------------------------- transport preserves the hint
def test_transport_preserves_session_status_hint():
    from workspace_bridge.runtime import coerce_runtime_event as _coerce
    idle = _coerce(
        {"type": "session.status", "session_id": "ses_1",
         "data": {"status": "idle"}})
    assert idle is not None and idle["type"] == "session.status"
    assert idle["data"] == {"status": "idle"}
    busy = _coerce(
        {"type": "session.status", "session_id": "ses_1",
         "data": {"status": {"type": "busy"}}})
    assert busy is not None and busy["data"] == {"status": "busy"}
    assert _coerce(
        {"type": "session.idle", "session_id": "ses_1", "data": {}}) is not None
