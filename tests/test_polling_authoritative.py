"""Polling-authoritative behavior: correct without functional SSE events.

Direct live validation showed the native OpenCode event stream delivering
only server.connected/heartbeat while a real session ran. The bridge must
therefore converge permission/completion/question state from bounded
authoritative polling alone, independent of the 25s event long poll.

All runtime interaction goes through a scripted fake: no Node, network,
provider credentials, Discord or real model is required.
"""
import json
import threading
import time

from workspace_bridge import orchestration as orch_module
from workspace_bridge.api import Handoff
from workspace_bridge.runtime import (HttpOpenCodeRuntime, MessageInfo, PendingQuestion,
                                      RuntimeUnavailable)
from workspace_bridge.service import Service

from runtime_fakes import pending_permission, pending_question


def publish(agent_env, payload, **overrides):
    body = {**payload, **overrides}
    return agent_env["service"].call(agent_env["id"], agent_env["token"], "prepare_handoff",
                                     Handoff.model_validate(body).model_dump())


def start(agent_env, job_id, request_id="run-request-1", **kwargs):
    return agent_env["service"].call(agent_env["id"], agent_env["token"], "start_opencode_run",
                                     {"job_id": job_id, "request_id": request_id,
                                      "model": kwargs.get("model"),
                                      "parent_run_id": kwargs.get("parent_run_id"),
                                      "continue_from_run_id": kwargs.get("continue_from_run_id")})


def call(agent_env, tool, **args):
    return agent_env["service"].call(agent_env["id"], agent_env["token"], tool, args)


def hold_open(agent_env):
    """Script a transcript with no durable completion evidence."""
    agent_env["runtime"].messages_script = [MessageInfo(id="u", role="user", created=1)]


def completed_messages(text="Implemented the change and ran the tests."):
    return [MessageInfo(id="m1", role="user", created=1),
            MessageInfo(id="m2", role="assistant", created=2, completed=3,
                        text=text, tools=("edit", "bash"))]


# ------------------------------------------------- cadence/cap contract
def test_polling_cadences_and_caps_match_plan_bounds():
    assert 3.0 <= orch_module.PERMISSION_RESYNC_INTERVAL <= 5.0
    assert 5.0 <= orch_module.COMPLETION_RECONCILE_INTERVAL <= 10.0
    assert 3.0 <= orch_module.QUESTION_RESYNC_INTERVAL <= 5.0
    assert orch_module.PERMISSION_RESYNC_SESSION_LIMIT <= 50
    assert orch_module.COMPLETION_RECONCILE_SESSION_LIMIT <= 50
    assert orch_module.QUESTION_RESYNC_SESSION_LIMIT <= 50
    assert 30.0 <= orch_module.FUNCTIONAL_DEGRADED_AFTER <= 60.0


# ------------------------------------------------- event-dead permission recovery
def test_event_dead_permission_converges_via_poll_sweep(agent_env, payload):
    """Heartbeat-only stream: a snapshot ask reaches waiting_permission via polling."""
    job = publish(agent_env, payload)
    run = start(agent_env, job["id"], "poll-permission")
    hold_open(agent_env)
    # No event is ever delivered; the ask appears only in the snapshot.
    agent_env["runtime"].add_pending_permission(
        run["session_id"],
        pending_permission(run["session_id"], "per_poll",
                           pattern=["/data/always/**"],
                           requested_patterns=["/data/requested/**"]))
    outcome = agent_env["service"].orchestrator._poll_sweep()
    assert outcome["checked"] == 1
    detail = call(agent_env, "read_opencode_run", run_id=run["run_id"])
    assert detail["state"] == "waiting_permission"
    assert detail["pending_requests"][0]["request_id"] == "per_poll"
    assert detail["permission_sync"]["status"] == "ok"


def test_event_dead_completion_converges_without_read_or_restart(agent_env, payload):
    """Heartbeat-only stream: a durable final message completes via polling."""
    job = publish(agent_env, payload)
    run = start(agent_env, job["id"], "poll-completion")
    agent_env["runtime"].messages_script = completed_messages()
    outcome = agent_env["service"].orchestrator._poll_sweep()
    assert outcome["checked"] == 1
    with agent_env["service"].lock:
        row = agent_env["service"].db.execute(
            "SELECT state, result FROM agent_runs WHERE id=?", (run["run_id"],)).fetchone()
    assert row["state"] == "completed"
    assert json.loads(row["result"])["reason"] == "background_reconcile"


def test_blocked_event_poll_cannot_delay_reconciliation(agent_env, payload):
    """A 25s-blocked event long poll runs on its own thread; the sweep converges."""
    job = publish(agent_env, payload)
    run = start(agent_env, job["id"], "blocked-pump")
    agent_env["runtime"].messages_script = completed_messages()
    release = threading.Event()
    pump_entered = threading.Event()

    def blocking_poll(cursor, timeout=25.0):
        pump_entered.set()
        release.wait(timeout=25.0)
        return [], cursor

    runtime = agent_env["runtime"]
    original_poll = runtime.poll_events
    runtime.poll_events = blocking_poll
    orch = agent_env["service"].orchestrator
    pump = threading.Thread(target=orch._pump_loop, name="test-pump", daemon=True)
    try:
        pump.start()
        assert pump_entered.wait(timeout=5.0), "pump thread never entered poll_events"
        # The pump is still blocked inside its 25s long poll: reconcile anyway.
        outcome = orch._poll_sweep()
        assert outcome["checked"] == 1
        assert pump.is_alive(), "pump must still be blocked; the sweep must not depend on it"
        detail = call(agent_env, "read_opencode_run", run_id=run["run_id"])
        assert detail["state"] == "completed"
    finally:
        runtime.poll_events = original_poll
        orch._stop.set()
        release.set()
        pump.join(timeout=5.0)
        orch._stop.clear()


# ------------------------------------------------- boundedness
def test_no_active_runs_produces_no_runtime_polling(agent_env, payload):
    orch = agent_env["service"].orchestrator
    outcome = orch._poll_sweep()
    assert outcome == {"checked": 0, "recovered": 0}
    assert agent_env["runtime"].list_pending_calls == []
    assert agent_env["runtime"].list_questions_calls == []
    # A terminal run is equally quiet: it disappears from future sweeps.
    job = publish(agent_env, payload)
    run = start(agent_env, job["id"], "terminal-quiet")
    agent_env["runtime"].messages_script = completed_messages()
    assert call(agent_env, "read_opencode_run", run_id=run["run_id"])["state"] == "completed"
    agent_env["runtime"].list_pending_calls.clear()
    agent_env["runtime"].list_questions_calls.clear()
    calls_before = len(agent_env["runtime"].list_pending_calls)
    outcome = orch._poll_sweep()
    assert outcome == {"checked": 0, "recovered": 0}
    assert len(agent_env["runtime"].list_pending_calls) == calls_before
    assert agent_env["runtime"].list_questions_calls == []


def test_terminal_run_absent_from_sweep_enumeration(agent_env, payload):
    job = publish(agent_env, payload)
    run = start(agent_env, job["id"], "terminal-gone")
    hold_open(agent_env)
    orch = agent_env["service"].orchestrator
    assert orch._poll_sweep()["checked"] == 1
    agent_env["runtime"].messages_script = completed_messages()
    assert orch._poll_sweep()["checked"] == 1  # still active: completion probe runs
    detail = call(agent_env, "read_opencode_run", run_id=run["run_id"])
    assert detail["state"] == "completed"
    assert orch._poll_sweep()["checked"] == 0  # terminal: gone from sweeps


def test_waiting_runs_skip_permission_listing_but_keep_question_state(agent_env, payload):
    """waiting_permission persists without repeated listing merely to re-prove it."""
    from runtime_fakes import permission_event
    job = publish(agent_env, payload)
    run = start(agent_env, job["id"], "waiting-quiet")
    hold_open(agent_env)
    orch = agent_env["service"].orchestrator
    orch.handle_event(permission_event(run["session_id"], "per_w", pattern=["/x/**"]))
    assert call(agent_env, "read_opencode_run", run_id=run["run_id"])["state"] == "waiting_permission"
    agent_env["runtime"].list_pending_calls.clear()
    outcome = orch._poll_sweep()
    assert outcome["checked"] == 0  # starting/running only
    assert agent_env["runtime"].list_pending_calls == []


# ------------------------------------------------- failure semantics
def test_runtime_unavailable_retries_without_false_transitions(agent_env, payload):
    job = publish(agent_env, payload)
    run = start(agent_env, job["id"], "unavailable-sweep")
    hold_open(agent_env)
    runtime = agent_env["runtime"]
    runtime.set_list_pending_error(RuntimeUnavailable("adapter is down"))
    runtime.set_list_questions_error(RuntimeUnavailable("adapter is down"))
    original_messages = runtime.messages

    def boom(directory, session_id, limit=40):
        raise RuntimeUnavailable("adapter is down")

    runtime.messages = boom
    try:
        outcome = runtime and agent_env["service"].orchestrator._poll_sweep()
        assert outcome["checked"] == 1
        detail = call(agent_env, "read_opencode_run", run_id=run["run_id"])
        assert detail["state"] in ("running", "starting")
        assert detail["active"] is True
        assert detail["error"] is None
        assert detail["permission_sync"]["status"] == "degraded"
        assert detail["question_sync"]["status"] == "degraded"
    finally:
        runtime.set_list_pending_error(None)
        runtime.set_list_questions_error(None)
        runtime.messages = original_messages
    # Recovery is automatic once the adapter answers again.
    outcome = agent_env["service"].orchestrator._poll_sweep()
    assert outcome["checked"] == 1
    detail = call(agent_env, "read_opencode_run", run_id=run["run_id"])
    assert detail["permission_sync"]["status"] == "ok"
    assert detail["question_sync"]["status"] == "ok"


# ------------------------------------------------- official question polling
def test_event_dead_question_recovers_via_official_snapshot(agent_env, payload):
    """No question event delivered: the V2 session snapshot still waits the run."""
    job = publish(agent_env, payload)
    run = start(agent_env, job["id"], "poll-question")
    hold_open(agent_env)
    agent_env["runtime"].add_pending_question(
        run["session_id"], pending_question(run["session_id"], "q_poll", question_count=2))
    outcome = agent_env["service"].orchestrator._poll_sweep()
    assert outcome["checked"] == 1
    detail = call(agent_env, "read_opencode_run", run_id=run["run_id"])
    assert detail["state"] == "waiting_question"
    assert detail["pending_request_count"] == 1
    pending = detail["pending_requests"][0]
    assert pending["request_id"] == "q_poll"
    assert pending["kind"] == "question"
    assert detail["question_sync"]["status"] == "ok"
    assert detail["question_sync"]["matched"] == 1
    # Only references persist: no question bodies, headers, options or answers.
    with agent_env["service"].lock:
        row = agent_env["service"].db.execute(
            "SELECT metadata, explanation FROM agent_requests WHERE run=?", (run["run_id"],)).fetchone()
    metadata = json.loads(row["metadata"])
    assert set(metadata.keys()) <= {"question_count", "call_id"}
    assert metadata["question_count"] == 2
    blob = json.dumps(detail)
    assert "question_count" in blob  # the count itself is reviewable metadata


def test_question_snapshot_is_idempotent_and_strictly_bound(agent_env, payload):
    job = publish(agent_env, payload)
    run = start(agent_env, job["id"], "question-dedupe")
    hold_open(agent_env)
    orch = agent_env["service"].orchestrator
    staged = pending_question(run["session_id"], "q_dup", question_count=1)
    agent_env["runtime"].add_pending_question(run["session_id"], staged)
    # Another session's question and malformed rows must never attach here.
    agent_env["runtime"].add_pending_question(
        run["session_id"], PendingQuestion(id="q_foreign", session_id="ses_other"))
    agent_env["runtime"].add_pending_question(
        run["session_id"], PendingQuestion(id="", session_id=run["session_id"]))
    orch._poll_sweep()
    orch._poll_sweep()
    detail = call(agent_env, "read_opencode_run", run_id=run["run_id"])
    assert detail["state"] == "waiting_question"
    assert detail["pending_request_count"] == 1
    assert detail["pending_requests"][0]["request_id"] == "q_dup"


def test_question_list_failure_is_degraded_never_empty(agent_env, payload):
    job = publish(agent_env, payload)
    run = start(agent_env, job["id"], "question-degraded")
    hold_open(agent_env)
    agent_env["runtime"].set_list_questions_error(RuntimeUnavailable("adapter is down"))
    try:
        detail = call(agent_env, "read_opencode_run", run_id=run["run_id"])
    finally:
        agent_env["runtime"].set_list_questions_error(None)
    assert detail["state"] in ("running", "starting")
    assert detail["pending_request_count"] == 0
    assert detail["question_sync"]["status"] == "degraded"
    assert detail["question_sync"]["reason"] == "question_list_failed"
    assert detail["error"] is None


def test_question_transport_uses_session_scoped_snapshot(monkeypatch):
    """The wire read is GET /sessions/{id}/questions: exact session only."""
    import json as _json
    import urllib.request

    seen = []

    class FakeResponse:
        def __init__(self, payload):
            self._body = _json.dumps(payload).encode()

        def read(self, _=None):
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def urlopen(request, timeout=None):
        seen.append(request.full_url)
        assert request.get_method() == "GET"
        return FakeResponse({"questions": [
            {"id": "q_1", "session_id": "ses_1", "question_count": 2, "call_id": "call_q"},
            {"id": "q_x", "session_id": "ses_other", "question_count": 1},
            {"id": "", "session_id": "ses_1"},
            "not-a-dict",
        ]})

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    runtime = HttpOpenCodeRuntime("http://adapter:8770")
    found = runtime.list_pending_questions("/d", "ses_1")
    assert len(found) == 1
    assert found[0].id == "q_1" and found[0].session_id == "ses_1"
    assert found[0].question_count == 2 and found[0].call_id == "call_q"
    assert seen and "/sessions/ses_1/questions" in seen[0]
    assert "directory=%2Fd" in seen[0] or "directory=/d" in seen[0]


def test_question_transport_reports_snapshot_source(monkeypatch):
    """The adapter reports which snapshot served the list (v2 primary, v1 fallback)."""
    import json as _json
    import urllib.request

    class FakeResponse:
        def __init__(self, payload):
            self._body = _json.dumps(payload).encode()

        def read(self, _=None):
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    bodies = [
        {"source": "v2", "questions": []},
        {"source": "v1", "questions": []},
        {"questions": []},
        {"source": "v9", "questions": []},
    ]

    def urlopen(request, timeout=None):
        return FakeResponse(bodies.pop(0))

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    runtime = HttpOpenCodeRuntime("http://adapter:8770")
    assert runtime.list_pending_questions("/d", "ses_1") == []
    assert runtime.last_question_source == "v2"
    assert runtime.list_pending_questions("/d", "ses_1") == []
    assert runtime.last_question_source == "v1"
    assert runtime.list_pending_questions("/d", "ses_1") == []
    assert runtime.last_question_source is None
    assert runtime.list_pending_questions("/d", "ses_1") == []
    assert runtime.last_question_source is None


def test_question_sync_exposes_snapshot_source(agent_env, payload):
    """question_sync carries source v2 normally and v1 on the fallback path."""
    job = publish(agent_env, payload)
    run = start(agent_env, job["id"], "question-source")
    hold_open(agent_env)
    agent_env["runtime"].add_pending_question(
        run["session_id"], pending_question(run["session_id"], "q_src", question_count=1))
    detail = call(agent_env, "read_opencode_run", run_id=run["run_id"])
    assert detail["state"] == "waiting_question"
    assert detail["question_sync"]["status"] == "ok"
    assert detail["question_sync"]["source"] == "v2"
    # The adapter fallback path reports v1 through the same diagnostics.
    job2 = publish(agent_env, payload, request_id="example-2", title="Fallback source")
    run2 = start(agent_env, job2["id"], "question-source-v1")
    hold_open(agent_env)
    agent_env["runtime"].question_source = "v1"
    try:
        agent_env["runtime"].add_pending_question(
            run2["session_id"], pending_question(run2["session_id"], "q_src_v1"))
        agent_env["service"].orchestrator._poll_sweep()
        sync_view = agent_env["service"].orchestrator._question_view(run2["run_id"])
    finally:
        agent_env["runtime"].question_source = "v2"
    assert sync_view["status"] == "ok"
    assert sync_view["matched"] == 1
    assert sync_view["source"] == "v1"
    detail2 = call(agent_env, "read_opencode_run", run_id=run2["run_id"])
    assert detail2["state"] == "waiting_question"
    assert detail2["pending_requests"][0]["request_id"] == "q_src_v1"


def test_event_dead_question_discovery_through_fallback(agent_env, payload):
    """No SSE delivered and V2 unavailable: the V1 fallback path still waits the run."""
    job = publish(agent_env, payload)
    run = start(agent_env, job["id"], "poll-question-v1")
    hold_open(agent_env)
    agent_env["runtime"].question_source = "v1"
    try:
        agent_env["runtime"].add_pending_question(
            run["session_id"], pending_question(run["session_id"], "q_fb", question_count=1))
        outcome = agent_env["service"].orchestrator._poll_sweep()
        sync_view = agent_env["service"].orchestrator._question_view(run["run_id"])
    finally:
        agent_env["runtime"].question_source = "v2"
    assert outcome["checked"] == 1
    assert sync_view["status"] == "ok"
    assert sync_view["matched"] == 1
    assert sync_view["source"] == "v1"
    detail = call(agent_env, "read_opencode_run", run_id=run["run_id"])
    assert detail["state"] == "waiting_question"
    assert detail["pending_requests"][0]["request_id"] == "q_fb"


def test_question_transport_errors_are_fail_closed(monkeypatch):
    import urllib.error
    import urllib.request

    def boom(request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, 500, "down", {}, None)

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    runtime = HttpOpenCodeRuntime("http://adapter:8770")
    try:
        runtime.list_pending_questions("/d", "ses_1")
    except RuntimeUnavailable:
        pass
    else:
        raise AssertionError("question list failure must raise, never return []")
    try:
        runtime.list_pending_questions("/d", "")
    except RuntimeUnavailable:
        pass
    else:
        raise AssertionError("missing session id must raise")


# ------------------------------------------------- functional health
def test_heartbeat_only_stream_reports_degraded_without_blocking(agent_env, payload, monkeypatch):
    """Transport subscribed + only control traffic + active run = degraded, still correct."""
    monkeypatch.setattr(orch_module, "FUNCTIONAL_DEGRADED_AFTER", 0.0)
    job = publish(agent_env, payload)
    run = start(agent_env, job["id"], "health-degraded")
    hold_open(agent_env)
    runtime = agent_env["runtime"]
    runtime.raw_event_count = 120
    runtime.control_event_count = 120
    runtime.functional_event_count = 0
    status = agent_env["service"].orchestrator.runtime_status()
    stream = status["event_stream"]
    assert stream["status"] == "subscribed"
    assert stream["functional_event_count"] == 0
    assert stream["functional_status"] == "degraded"
    assert stream["functional_reason"] == "no_functional_events"
    # Degraded health never blocks polling-based correctness.
    runtime.add_pending_permission(
        run["session_id"],
        pending_permission(run["session_id"], "per_deg", pattern=["/d/**"]))
    agent_env["service"].orchestrator._poll_sweep()
    detail = call(agent_env, "read_opencode_run", run_id=run["run_id"])
    assert detail["state"] == "waiting_permission"


def test_functional_event_recovers_health(agent_env, payload):
    job = publish(agent_env, payload)
    run = start(agent_env, job["id"], "health-recover")
    hold_open(agent_env)
    orch = agent_env["service"].orchestrator
    # Age the bridge start past the grace window: heartbeat-only reads degraded.
    orch._started_at = time.monotonic() - (orch_module.FUNCTIONAL_DEGRADED_AFTER + 5.0)
    before = orch.runtime_status()["event_stream"]["functional_status"]
    assert before == "degraded"
    # Any functional event (here a normalized idle hint) clears the diagnostic.
    orch.handle_event({"type": "session.idle", "session_id": run["session_id"]})
    after = orch.runtime_status()["event_stream"]["functional_status"]
    assert after == "healthy"


def test_no_active_runs_leaves_functional_status_unknown(agent_env, payload):
    status = agent_env["service"].orchestrator.runtime_status()
    assert status["event_stream"]["functional_status"] == "unknown"
    assert status["event_stream"]["functional_reason"] == "no_active_runs"


def test_unsubscribed_transport_is_not_mislabeled_healthy(agent_env, payload):
    job = publish(agent_env, payload)
    start(agent_env, job["id"], "health-transport")
    agent_env["runtime"].event_stream_status = "reconnecting"
    try:
        stream = agent_env["service"].orchestrator.runtime_status()["event_stream"]
    finally:
        agent_env["runtime"].event_stream_status = "subscribed"
    assert stream["status"] == "reconnecting"
    assert stream["functional_status"] == "unknown"
    assert stream["functional_reason"] == "transport_not_subscribed"


def test_functional_transitions_log_once(agent_env, payload):
    """Degraded/recovered emit exactly one WARNING/INFO each, not per tick."""
    import logging
    job = publish(agent_env, payload)
    run = start(agent_env, job["id"], "health-log-once")
    hold_open(agent_env)
    orch = agent_env["service"].orchestrator
    orch._started_at = time.monotonic() - (orch_module.FUNCTIONAL_DEGRADED_AFTER + 5.0)
    lines: list[str] = []

    class Capture(logging.Handler):
        def emit(self, record):
            lines.append(record.getMessage())

    logger = logging.getLogger("workspace_bridge.ops")
    handler = Capture()
    previous_level = logger.level
    logger.setLevel(logging.INFO)
    orch._functional_status = "unknown"
    logger.addHandler(handler)
    try:
        orch._update_functional_health()
        orch._update_functional_health()
        degraded = [l for l in lines if '"event_stream_health"' in l and "degraded" in l]
        assert len(degraded) == 1, lines
        orch.handle_event({"type": "session.idle", "session_id": run["session_id"]})
        orch._update_functional_health()
        orch._update_functional_health()
        recovered = [l for l in lines if '"event_stream_health"' in l and "healthy" in l]
        assert len(recovered) == 1, lines
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)


# ------------------------------------------------- sweep log volume
def test_sweep_failures_avoid_warning_spam(agent_env, payload):
    """Startup grace keeps sweep failures at DEBUG; afterwards one WARNING, repeats quiet."""
    import logging
    job = publish(agent_env, payload)
    run = start(agent_env, job["id"], "sweep-log-throttle")
    hold_open(agent_env)
    agent_env["runtime"].set_list_pending_error(RuntimeUnavailable("adapter is down"))
    orch = agent_env["service"].orchestrator
    lines: list[str] = []

    class Capture(logging.Handler):
        def emit(self, record):
            lines.append(record.getMessage())

    logger = logging.getLogger("workspace_bridge.ops")
    previous_level = logger.level
    logger.setLevel(logging.DEBUG)
    handler = Capture()
    logger.addHandler(handler)
    try:
        # Inside the startup grace window: no WARNING spam while the
        # adapter may simply not be up yet.
        orch._started_at = time.monotonic()
        orch._poll_sweep()
        orch._poll_sweep()
        assert [l for l in lines
                if '"permission_resync"' in l and '"WARNING"' in l] == []
        assert [l for l in lines if '"permission_resync"' in l], "failures stay visible at DEBUG"
        # Past the grace window: the first failure warns once, repeats
        # stay DEBUG, and no false terminal state ever appears.
        orch._started_at = time.monotonic() - (orch_module.STARTUP_UNAVAILABLE_GRACE + 5.0)
        lines.clear()
        orch._poll_sweep()
        orch._poll_sweep()
        warns = [l for l in lines if '"permission_resync"' in l and '"WARNING"' in l]
        assert len(warns) == 1, lines
        detail = call(agent_env, "read_opencode_run", run_id=run["run_id"])
        assert detail["state"] in ("running", "starting")
        assert detail["error"] is None
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)
        agent_env["runtime"].set_list_pending_error(None)


# ------------------------------------------------- lifecycle
def test_start_stop_owns_both_event_and_reconcile_loops(agent_env, payload):
    import time as _time
    orch = agent_env["service"].orchestrator
    runtime = agent_env["runtime"]
    original_poll = runtime.poll_events
    runtime.poll_events = lambda cursor, timeout=25.0: (_time.sleep(0.02), ([], cursor))[1]
    orch.background = True
    try:
        orch.start()
        deadline = _time.monotonic() + 5.0
        while _time.monotonic() < deadline:
            names = {t.name for t in threading.enumerate()}
            if {"opencode-events", "opencode-reconcile-poll"} <= names:
                break
            _time.sleep(0.02)
        names = {t.name for t in threading.enumerate()}
        assert "opencode-events" in names
        assert "opencode-reconcile-poll" in names
    finally:
        runtime.poll_events = original_poll
        orch.stop()
        orch.background = False
    assert orch._pump is None
    assert orch._poll is None
    names = {t.name for t in threading.enumerate()}
    assert "opencode-events" not in names
    assert "opencode-reconcile-poll" not in names


def test_reconcile_poll_loop_meets_each_cadence_independently(agent_env, payload, monkeypatch):
    """The poll loop (not the event pump) drives permission/question/completion."""
    monkeypatch.setattr(orch_module, "PERMISSION_RESYNC_INTERVAL", 0.05)
    monkeypatch.setattr(orch_module, "QUESTION_RESYNC_INTERVAL", 0.05)
    monkeypatch.setattr(orch_module, "COMPLETION_RECONCILE_INTERVAL", 0.05)
    monkeypatch.setattr(orch_module, "RECONCILE_POLL_TICK", 0.02)
    job = publish(agent_env, payload)
    run = start(agent_env, job["id"], "loop-cadence")
    hold_open(agent_env)
    agent_env["runtime"].add_pending_permission(
        run["session_id"], pending_permission(run["session_id"], "per_loop", pattern=["/l/**"]))
    orch = agent_env["service"].orchestrator
    stopper = threading.Event()
    orch._stop = stopper  # fresh stop flag for a manual loop thread
    loop = threading.Thread(target=orch._poll_loop, daemon=True)
    try:
        loop.start()
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            with agent_env["service"].lock:
                state = agent_env["service"].db.execute(
                    "SELECT state FROM agent_runs WHERE id=?", (run["run_id"],)).fetchone()["state"]
            if state == "waiting_permission":
                break
            time.sleep(0.02)
        assert state == "waiting_permission"
        assert len(agent_env["runtime"].list_pending_calls) >= 1
        assert len(agent_env["runtime"].list_questions_calls) >= 1
    finally:
        stopper.set()
        loop.join(timeout=5.0)
        orch._stop = threading.Event()
