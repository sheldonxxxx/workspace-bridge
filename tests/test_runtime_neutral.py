"""Runtime-neutral core: AgentRuntime seam, runtime identity, capabilities,
normalized interactions/events.

All runtime interaction goes through the scripted fake: no Node, network,
provider credentials, Discord or real model is required.
"""
import sqlite3
from datetime import datetime, timezone

import pytest

from workspace_bridge.api import Handoff
from workspace_bridge.orchestration import AgentOrchestrator, OpenCodeOrchestrator
from workspace_bridge.runtime import (OPENCODE_RUNTIME_ID, AgentRuntime, HttpOpenCodeRuntime,
                                      OpenCodeRuntime, PendingPermission, PendingQuestion,
                                      RuntimeCapabilities, RuntimeEvent, RuntimeInteraction,
                                      coerce_runtime_event)
from workspace_bridge.security import BridgeError
from workspace_bridge.service import Service

from runtime_fakes import FakeRuntime, permission_event, permission_event_v2


def publish(agent_env, payload, **overrides):
    body = {**payload, **overrides}
    return agent_env["service"].call(agent_env["id"], agent_env["token"], "prepare_handoff",
                                     Handoff.model_validate(body).model_dump())


def start(agent_env, job_id, request_id="run-request-1", **kwargs):
    return agent_env["service"].call(agent_env["id"], agent_env["token"], "start_opencode_run",
                                     {"job_id": job_id, "request_id": request_id, **kwargs})


def call(agent_env, tool, **args):
    return agent_env["service"].call(agent_env["id"], agent_env["token"], tool, args)


# ------------------------------------------------------- internal architecture
def test_neutral_contract_and_installed_backend(agent_env):
    assert OPENCODE_RUNTIME_ID == "opencode"
    assert issubclass(HttpOpenCodeRuntime, AgentRuntime)
    assert issubclass(OpenCodeRuntime, AgentRuntime)
    assert isinstance(agent_env["runtime"], AgentRuntime)
    assert agent_env["runtime"].runtime_id == "opencode"
    assert isinstance(agent_env["service"].orchestrator, AgentOrchestrator)
    # Compatibility alias only; implementation uses the neutral name.
    assert OpenCodeOrchestrator is AgentOrchestrator
    # OpenCode remains the sole backend: no Pi/ACP/other runtime exists.
    import workspace_bridge.runtime as runtime_module
    assert not hasattr(runtime_module, "PiRuntime")
    assert not hasattr(runtime_module, "ACPRuntime")
    assert not hasattr(runtime_module, "ClineRuntime")


def test_installed_backend_capabilities_are_explicit():
    caps = HttpOpenCodeRuntime("http://adapter:8770").capabilities
    assert isinstance(caps, RuntimeCapabilities)
    assert caps.model_discovery and caps.session_reuse and caps.event_polling
    assert caps.session_status and caps.pending_snapshot and caps.permission_response
    assert caps.question_detection
    # Deliberately unsupported: never claimed, fail-closed downstream.
    assert caps.question_response is False
    assert caps.session_branching is False


def test_capability_gates_fail_closed(agent_env, payload):
    job = publish(agent_env, payload)
    run = start(agent_env, job["id"])
    agent_env["runtime"].messages_script = []
    event = permission_event(run["session_id"], "per_cap", pattern=["/x/**"])
    agent_env["service"].orchestrator.handle_event(event)
    assert call(agent_env, "read_opencode_run", run_id=run["run_id"])["state"] == "waiting_permission"
    agent_env["runtime"]._capabilities = RuntimeCapabilities(permission_response=False)
    with pytest.raises(BridgeError) as exc:
        call(agent_env, "respond_opencode_permission", run_id=run["run_id"],
             request_id="per_cap", decision="once")
    assert exc.value.code == "runtime_unsupported"


def test_session_reuse_capability_gates_continuation(agent_env, payload):
    job = publish(agent_env, payload)
    first = start(agent_env, job["id"], "cap-first")
    assert call(agent_env, "read_opencode_run", run_id=first["run_id"])["state"] == "completed"
    job2 = publish(agent_env, payload, request_id="example-2", title="Follow-up")
    agent_env["runtime"]._capabilities = RuntimeCapabilities(session_reuse=False)
    with pytest.raises(BridgeError) as exc:
        start(agent_env, job2["id"], "cap-second", continue_from_run_id=first["run_id"])
    assert exc.value.code == "runtime_unsupported"


# ------------------------------------------------- runtime identity / migration
def test_new_runs_persist_opencode_identity(agent_env, payload):
    job = publish(agent_env, payload)
    run = start(agent_env, job["id"])
    assert run["runtime"] == "opencode"
    row = agent_env["service"].db.execute(
        "SELECT runtime FROM agent_runs WHERE id=?", (run["run_id"],)).fetchone()
    assert row["runtime"] == "opencode"


def test_legacy_database_backfills_runtime_without_loss(tmp_path):
    from workspace_bridge.cli import initialize
    from runtime_fakes import RecordingNotifier
    parent = tmp_path / "projects"
    parent.mkdir()
    root = parent / "alpha"
    root.mkdir()
    state = tmp_path / "private-state"
    cfg = initialize(state, [str(parent)], 8765, 8766)
    runtime = FakeRuntime(str(root))
    service = Service(state, cfg, runtime=runtime, notifier=RecordingNotifier(),
                      orchestrator_background=False)
    ws_id = service.add_workspace("Alpha", str(root), [])["workspace"]["id"]
    token = service.manage_bridge("rotate_token")["token"]
    service.manage_workspace(ws_id, "enable")
    service.manage_workspace(ws_id, "set_agent_enabled", agent_enabled=True)
    service.orchestrator.set_model_policy(
        ["anthropic/claude-sonnet", "glm/zai-glm-5.2"], "anthropic/claude-sonnet")
    ws = service.workspace(ws_id)
    job = service.call(ws_id, token, "prepare_handoff",
                       Handoff.model_validate({"request_id": "legacy-1", "title": "Legacy",
                                               "goal": "g", "plan": "p", "acceptance": "a",
                                               "constraints": "c", "context": "x",
                                               "context_hashes": {}}).model_dump())
    run = service.call(ws_id, token, "start_opencode_run",
                       {"job_id": job["id"], "request_id": "legacy-run-1"})
    # Downgrade to the pre-neutral shape: no runtime column, session-only index.
    service.db.execute("DROP INDEX IF EXISTS ux_agent_runs_session_active")
    service.db.execute("ALTER TABLE agent_runs DROP COLUMN runtime")
    service.db.execute(
        "CREATE UNIQUE INDEX ux_agent_runs_session_active ON agent_runs(session) "
        "WHERE state IN ('starting','running','waiting_permission','waiting_question') "
        "AND session IS NOT NULL AND session <> ''")
    service.db.commit()
    assert "runtime" not in {row[1] for row in service.db.execute("PRAGMA table_info(agent_runs)")}
    service.close()
    # Reopen: non-destructive migration backfills identity, keeps rows.
    reopened = Service(state, cfg, runtime=FakeRuntime(str(root)),
                       notifier=RecordingNotifier(), orchestrator_background=False)
    try:
        assert "runtime" in {row[1] for row in reopened.db.execute("PRAGMA table_info(agent_runs)")}
        row = reopened.db.execute(
            "SELECT runtime, session, model, state FROM agent_runs WHERE id=?",
            (run["run_id"],)).fetchone()
        assert row["runtime"] == "opencode"
        assert row["session"] == run["session_id"] and row["state"] in ("starting", "running")
        index_sql = reopened.db.execute(
            "SELECT sql FROM sqlite_master WHERE name='ux_agent_runs_session_active'").fetchone()[0]
        assert "(runtime, session)" in index_sql
        detail = reopened.call(ws_id, token, "read_opencode_run", {"run_id": run["run_id"]})
        assert detail["runtime"] == "opencode"
        assert detail["session_id"] == run["session_id"]
    finally:
        reopened.close()


def test_continuation_refuses_foreign_runtime(agent_env, payload):
    job = publish(agent_env, payload)
    first = start(agent_env, job["id"], "rt-first")
    assert call(agent_env, "read_opencode_run", run_id=first["run_id"])["state"] == "completed"
    with agent_env["service"].lock, agent_env["service"].db:
        agent_env["service"].db.execute("UPDATE agent_runs SET runtime='other' WHERE id=?",
                                        (first["run_id"],))
    job2 = publish(agent_env, payload, request_id="example-2", title="Follow-up")
    with pytest.raises(BridgeError) as exc:
        start(agent_env, job2["id"], "rt-second", continue_from_run_id=first["run_id"])
    assert exc.value.code == "continuation_unavailable"


def test_active_session_uniqueness_is_runtime_scoped(agent_env, payload):
    job = publish(agent_env, payload)
    run = start(agent_env, job["id"], "uniq-first")
    agent_env["runtime"].messages_script = []
    session = run["session_id"]
    service = agent_env["service"]
    # Same runtime + session while active collides at the DB constraint.
    with pytest.raises(sqlite3.IntegrityError):
        with service.lock, service.db:
            service.db.execute(
                "INSERT INTO agent_runs (id,workspace,runtime,job,request_id,request_hash,"
                "parent_run,session,model,state,error_code,error_message,result,notification,"
                "created,started,updated,finished,message_floor_ms,session_reused,transcript) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("run_sameruntime", agent_env["id"], "opencode", job["id"], "uniq-other",
                 "hash", None, session, "anthropic/claude-sonnet", "running", None, None,
                 "{}", "{}", "2026-09-21T00:00:00+00:00", None, "2026-09-21T00:00:00+00:00",
                 None, 0, 0, "[]"))
    service.db.rollback()
    # A different runtime may reference the same native session id.
    with service.lock, service.db:
        service.db.execute(
            "INSERT INTO agent_runs (id,workspace,runtime,job,request_id,request_hash,"
            "parent_run,session,model,state,error_code,error_message,result,notification,"
            "created,started,updated,finished,message_floor_ms,session_reused,transcript) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("run_otherruntime", agent_env["id"], "future", job["id"], "uniq-future",
             "hash", None, session, "anthropic/claude-sonnet", "running", None, None,
             "{}", "{}", "2026-09-21T00:00:00+00:00", None, "2026-09-21T00:00:00+00:00",
             None, 0, 0, "[]"))
    rows = service.db.execute(
        "SELECT id FROM agent_runs WHERE session=? AND state='running'", (session,)).fetchall()
    assert {r["id"] for r in rows} == {run["run_id"], "run_otherruntime"}


# ------------------------------------------------------ normalized interactions
def test_permission_interaction_preserves_scope_and_routing(agent_env, payload):
    from runtime_fakes import pending_permission_v2
    job = publish(agent_env, payload)
    run = start(agent_env, job["id"])
    agent_env["runtime"].messages_script = []
    ws = agent_env["service"].workspace(agent_env["id"])
    session = run["session_id"]
    staged = pending_permission_v2(session, "per_scope", resources=["/data/requested/**"],
                                   save=["/data/always/**"])
    assert isinstance(staged, RuntimeInteraction)
    assert staged.kind == "permission" and staged.generation == "v2"
    agent_env["runtime"].add_pending_permission(session, staged)
    detail = call(agent_env, "read_opencode_run", run_id=run["run_id"])
    assert detail["state"] == "waiting_permission"
    pending = detail["pending_requests"][0]
    assert pending["pattern"] == ["/data/always/**"]
    assert pending["requested_patterns"] == ["/data/requested/**"]
    assert pending["generation"] == "v2"
    assert detail["requests"][0]["generation"] == "v2"
    # Orchestrator consumes the normalized object, not the legacy class.
    assert isinstance(agent_env["runtime"].list_pending_permissions(str(agent_env["root"]),
                                                                   session)[0],
                      RuntimeInteraction)


def test_question_interaction_carries_no_content(agent_env, payload):
    from runtime_fakes import pending_question
    job = publish(agent_env, payload)
    run = start(agent_env, job["id"])
    agent_env["runtime"].messages_script = []
    session = run["session_id"]
    staged = pending_question(session, "q_quiet", question_count=3, call_id="call_q")
    assert isinstance(staged, RuntimeInteraction) and staged.kind == "question"
    agent_env["runtime"].add_pending_question(session, staged)
    detail = call(agent_env, "read_opencode_run", run_id=run["run_id"])
    assert detail["state"] == "waiting_question"
    stored = agent_env["service"].db.execute(
        "SELECT metadata, explanation FROM agent_requests WHERE runtime_request='q_quiet'").fetchone()
    assert "3" not in stored["explanation"]
    assert "question_count" in stored["metadata"] and "call_q" in stored["metadata"]
    blob = stored["metadata"] + stored["explanation"]
    assert "body" not in blob and "option" not in blob


def test_legacy_permission_classes_are_neutral_aliases():
    assert issubclass(PendingPermission, RuntimeInteraction)
    assert issubclass(PendingQuestion, RuntimeInteraction)
    permission = PendingPermission(id="per_1", session_id="ses_1", action="edit",
                                   pattern=("/x/**",), requested_patterns=("/y/**",))
    assert permission.kind == "permission" and permission.scope_text() == "/x/**"
    question = PendingQuestion(id="q_1", session_id="ses_1", question_count=2)
    assert question.kind == "question"


# ------------------------------------------------------------ normalized events
def test_runtime_boundary_yields_typed_events(agent_env, payload):
    job = publish(agent_env, payload)
    run = start(agent_env, job["id"])
    agent_env["runtime"].messages_script = []
    raw = permission_event(run["session_id"], "per_typed", pattern=["/x/**"])
    agent_env["runtime"].push(raw)
    events, _ = agent_env["runtime"].poll_events(0)
    assert events and all(isinstance(e, RuntimeEvent) for e in events)
    ask = next(e for e in events if e.type == "permission.asked")
    assert isinstance(ask.permission, RuntimeInteraction)
    assert ask.permission.pattern == ("/x/**",)
    # The orchestrator consumes the typed event directly.
    agent_env["service"].orchestrator.handle_event(ask)
    assert call(agent_env, "read_opencode_run", run_id=run["run_id"])["state"] == "waiting_permission"


def test_wire_event_coercion_preserves_evidence_bounds():
    event = coerce_runtime_event(
        {"type": "permission.asked", "session_id": "ses_1", "cursor": 7,
         "data": {"id": "per_1", "session_id": "ses_1", "action": "edit",
                  "title": "t", "pattern": ["/data/always/**"],
                  "requested_patterns": ["/data/requested/**"],
                  "tool": {"name": "edit"},
                  "metadata": {"token": "sk-" + "a" * 30}, "generation": "v1"}})
    assert isinstance(event, RuntimeEvent)
    assert event.cursor == 7 and event.permission is not None
    assert event.permission.pattern == ("/data/always/**",)
    assert "sk-" not in str(event.permission.metadata)
    assert event.permission.redacted is True
    assert coerce_runtime_event({"type": "", "session_id": "s"}) is None
    assert coerce_runtime_event("not-a-dict") is None


# ------------------------------------------------- audit: transcript isolation
def _insert_run_row(service, ws_id, job_id, run_id, session, runtime, created,
                    state="running"):
    with service.lock, service.db:
        service.db.execute(
            "INSERT INTO agent_runs (id,workspace,runtime,job,request_id,request_hash,"
            "parent_run,session,model,state,error_code,error_message,result,notification,"
            "created,started,updated,finished,message_floor_ms,session_reused,transcript) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, ws_id, runtime, job_id, f"req-{run_id}", "hash", None, session,
             "anthropic/claude-sonnet", state, None, None, "{}", "{}",
             created, created, created, None, 0, 0, "[]"))


def test_later_run_check_ignores_other_runtime_sessions(agent_env, payload):
    job = publish(agent_env, payload)
    first = start(agent_env, job["id"], "later-first")
    assert call(agent_env, "read_opencode_run", run_id=first["run_id"])["state"] == "completed"
    service = agent_env["service"]
    # Legacy shape: completed without a persisted transcript snapshot.
    with service.lock, service.db:
        service.db.execute("UPDATE agent_runs SET transcript='[]' WHERE id=?",
                           (first["run_id"],))
    run_row = service.db.execute(
        "SELECT * FROM agent_runs WHERE id=?", (first["run_id"],)).fetchone()
    assert service.orchestrator._session_has_later_run(dict(run_row)) is False
    later = datetime.now(timezone.utc).isoformat()
    # Another backend reusing the same native session id is not a later run.
    _insert_run_row(service, agent_env["id"], job["id"], "run_other_rt",
                    first["session_id"], "future", later)
    assert service.orchestrator._session_has_later_run(dict(run_row)) is False
    transcript_view = service.orchestrator.read_run(
        service.workspace(agent_env["id"]), first["run_id"],
        include_transcript=True, limit=40)
    assert transcript_view["transcript"] != [{"error": "transcript unavailable"}]
    # A same-runtime later run still counts for transcript safety.
    _insert_run_row(service, agent_env["id"], job["id"], "run_same_rt",
                    first["session_id"], "opencode", later)
    assert service.orchestrator._session_has_later_run(dict(run_row)) is True


def test_sweep_ignores_foreign_runtime_rows(agent_env, payload):
    job = publish(agent_env, payload)
    run = start(agent_env, job["id"], "sweep-first")
    agent_env["runtime"].messages_script = []
    service = agent_env["service"]
    runtime = agent_env["runtime"]
    runtime.list_pending_calls.clear()
    runtime.get_session_calls.clear()
    runtime.messages_calls.clear()
    # A foreign active row sharing the same native session id must not be
    # enumerated, queried, or reconciled by this orchestrator.
    _insert_run_row(service, agent_env["id"], job["id"], "run_sweep_other",
                    run["session_id"], "future",
                    datetime.now(timezone.utc).isoformat())
    outcome = service.orchestrator._poll_sweep(due_permission=True,
                                               due_completion=False,
                                               due_question=False)
    assert outcome["checked"] == 1
    assert runtime.list_pending_calls == [(str(agent_env["root"]), run["session_id"])]
    assert runtime.get_session_calls == [(str(agent_env["root"]), run["session_id"])]
    assert runtime.messages_calls == []
    foreign = service.db.execute(
        "SELECT state FROM agent_runs WHERE id='run_sweep_other'").fetchone()
    assert foreign["state"] == "running"


def test_active_run_count_ignores_foreign_runtime_rows(agent_env, payload):
    job = publish(agent_env, payload)
    run = start(agent_env, job["id"], "count-first")
    agent_env["runtime"].messages_script = []
    service = agent_env["service"]
    assert service.orchestrator._active_run_count() == 1
    _insert_run_row(service, agent_env["id"], job["id"], "run_count_other",
                    run["session_id"], "future",
                    datetime.now(timezone.utc).isoformat())
    assert service.orchestrator._active_run_count() == 1
    outcome = service.orchestrator._poll_sweep(due_permission=False,
                                               due_completion=False,
                                               due_question=False)
    assert outcome["checked"] == 1


def test_startup_reconcile_ignores_foreign_runtime_rows(agent_env, payload):
    job = publish(agent_env, payload)
    run = start(agent_env, job["id"], "reconcile-first")
    # No durable completion evidence: startup reconcile must leave the owned
    # run explicitly active rather than orphan it.
    agent_env["runtime"].messages_script = []
    service = agent_env["service"]
    foreign_created = datetime.now(timezone.utc).isoformat()
    _insert_run_row(service, agent_env["id"], job["id"], "run_reconcile_other",
                    run["session_id"], "future", foreign_created,
                    state="waiting_permission")
    service.orchestrator.reconcile_startup()
    foreign = service.db.execute(
        "SELECT state, error_code FROM agent_runs WHERE id='run_reconcile_other'").fetchone()
    assert foreign["state"] == "waiting_permission" and foreign["error_code"] is None
    assert "run_reconcile_other" not in service.orchestrator._reconcile_pending
    owned = service.db.execute(
        "SELECT state FROM agent_runs WHERE id=?", (run["run_id"],)).fetchone()
    assert owned["state"] in ("starting", "running")
    # A stale foreign id in the retry set is dropped, never reconciled.
    with service.orchestrator._reconcile_guard:
        service.orchestrator._reconcile_pending.add("run_reconcile_other")
    assert service.orchestrator.retry_reconcile() == []
    foreign_again = service.db.execute(
        "SELECT state, error_code FROM agent_runs WHERE id='run_reconcile_other'").fetchone()
    assert foreign_again["state"] == "waiting_permission"
    assert foreign_again["error_code"] is None


def _insert_request_row(service, ws_id, run_id, session, native_id, kind="permission"):
    with service.lock, service.db:
        service.db.execute(
            "INSERT INTO agent_requests (id,run,workspace,session,opencode_request,"
            "runtime_request,kind,action,resource,pattern,metadata,explanation,redacted,"
            "state,decision,created,updated,resolved,generation) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"req_{native_id}", run_id, ws_id, session, f"{run_id}:{native_id}",
             native_id, kind, "edit", "t", '["/x/**"]', "{}", "t", 0,
             "pending", None, "2026-09-21T00:00:00+00:00", "2026-09-21T00:00:00+00:00",
             None, "v1"))


def test_direct_paths_never_call_backend_for_foreign_runs(agent_env, payload):
    job = publish(agent_env, payload)
    run = start(agent_env, job["id"], "owned-first")
    agent_env["runtime"].messages_script = []
    service = agent_env["service"]
    runtime = agent_env["runtime"]
    _insert_run_row(service, agent_env["id"], job["id"], "run_foreign_direct",
                    run["session_id"], "future",
                    datetime.now(timezone.utc).isoformat(), state="waiting_permission")
    _insert_request_row(service, agent_env["id"], "run_foreign_direct",
                        run["session_id"], "per_foreign")
    ws = service.workspace(agent_env["id"])
    foreign = dict(service.db.execute(
        "SELECT * FROM agent_runs WHERE id='run_foreign_direct'").fetchone())
    # Ownership gate fails before get_session with zero backend contact.
    with pytest.raises(BridgeError) as exc:
        service.orchestrator._revalidate_binding(ws, foreign)
    assert exc.value.code == "runtime_mismatch"
    runtime.get_session_calls.clear()
    runtime.respond_calls.clear()
    runtime.abort_calls.clear()
    runtime.messages_calls.clear()
    # Legacy read, cancel, respond, and request reads all refuse foreign
    # rows without backend calls.
    with pytest.raises(BridgeError) as exc:
        service.orchestrator.cancel_run(ws, "run_foreign_direct")
    assert exc.value.code == "runtime_mismatch"
    with pytest.raises(BridgeError) as exc:
        service.orchestrator.respond_permission(ws, "run_foreign_direct",
                                                "per_foreign", "once")
    assert exc.value.code == "runtime_mismatch"
    with pytest.raises(BridgeError) as exc:
        service.orchestrator.read_run(ws, "run_foreign_direct")
    assert exc.value.code == "runtime_mismatch"
    with pytest.raises(BridgeError) as exc:
        service.orchestrator.read_request(ws, "run_foreign_direct", "per_foreign")
    assert exc.value.code == "runtime_mismatch"
    assert runtime.get_session_calls == []
    assert runtime.respond_calls == []
    assert runtime.abort_calls == []
    assert runtime.messages_calls == []
    untouched = service.db.execute(
        "SELECT state, error_code, error_message FROM agent_runs "
        "WHERE id='run_foreign_direct'").fetchone()
    assert untouched["state"] == "waiting_permission"
    assert untouched["error_code"] is None and untouched["error_message"] is None
    pending = service.db.execute(
        "SELECT state FROM agent_requests WHERE runtime_request='per_foreign'").fetchone()
    assert pending["state"] == "pending"


# ------------------------------------------- audit: neutral request identity
def test_request_migration_backfills_runtime_request(tmp_path):
    from workspace_bridge.cli import initialize
    from runtime_fakes import RecordingNotifier
    parent = tmp_path / "projects"
    parent.mkdir()
    root = parent / "alpha"
    root.mkdir()
    state = tmp_path / "private-state"
    cfg = initialize(state, [str(parent)], 8765, 8766)
    runtime = FakeRuntime(str(root))
    service = Service(state, cfg, runtime=runtime, notifier=RecordingNotifier(),
                      orchestrator_background=False)
    ws_id = service.add_workspace("Alpha", str(root), [])["workspace"]["id"]
    token = service.manage_bridge("rotate_token")["token"]
    service.manage_workspace(ws_id, "enable")
    service.manage_workspace(ws_id, "set_agent_enabled", agent_enabled=True)
    service.orchestrator.set_model_policy(
        ["anthropic/claude-sonnet", "glm/zai-glm-5.2"], "anthropic/claude-sonnet")
    job = service.call(ws_id, token, "prepare_handoff",
                       Handoff.model_validate({"request_id": "reqmig-1", "title": "ReqMig",
                                               "goal": "g", "plan": "p", "acceptance": "a",
                                               "constraints": "c", "context": "x",
                                               "context_hashes": {}}).model_dump())
    run = service.call(ws_id, token, "start_opencode_run",
                       {"job_id": job["id"], "request_id": "reqmig-run-1"})
    runtime.messages_script = []
    ws = service.workspace(ws_id)
    service.orchestrator.handle_event(permission_event(run["session_id"], "per_legacy"))
    current = service.db.execute(
        "SELECT opencode_request, runtime_request FROM agent_requests").fetchone()
    assert current["runtime_request"] == "per_legacy"
    assert current["opencode_request"] == f"{run['run_id']}:per_legacy"
    # Rewrite the row to the genuine pre-neutral shape (native id stored
    # directly in opencode_request), then downgrade and reopen (migration).
    with service.lock, service.db:
        service.db.execute("UPDATE agent_requests SET opencode_request='per_legacy'")
    # Downgrade to the pre-neutral request shape, then reopen (migration).
    service.db.execute("DROP INDEX IF EXISTS ux_agent_requests_run_native")
    service.db.execute("ALTER TABLE agent_requests DROP COLUMN runtime_request")
    service.db.commit()
    assert "runtime_request" not in {
        row[1] for row in service.db.execute("PRAGMA table_info(agent_requests)")}
    service.close()
    reopened = Service(state, cfg, runtime=FakeRuntime(str(root)),
                       notifier=RecordingNotifier(), orchestrator_background=False)
    try:
        assert "runtime_request" in {
            row[1] for row in reopened.db.execute("PRAGMA table_info(agent_requests)")}
        row = reopened.db.execute(
            "SELECT opencode_request, runtime_request, kind, state FROM agent_requests").fetchone()
        assert row["runtime_request"] == "per_legacy" == row["opencode_request"]
        assert row["kind"] == "permission" and row["state"] == "pending"
        index_sql = reopened.db.execute(
            "SELECT sql FROM sqlite_master WHERE name='ux_agent_requests_run_native'").fetchone()[0]
        assert "(run, runtime_request)" in index_sql
        # The historical row resolves by its native id within its run.
        view = reopened.call(ws_id, token, "read_opencode_request",
                             {"run_id": run["run_id"], "request_id": "per_legacy"})
        assert view["request_id"] == "per_legacy"
    finally:
        reopened.close()


def test_same_native_request_id_coexists_across_runs(agent_env, payload):
    job1 = publish(agent_env, payload)
    run1 = start(agent_env, job1["id"], "dup-first")
    job2 = publish(agent_env, payload, request_id="example-2", title="Second")
    run2 = start(agent_env, job2["id"], "dup-second")
    agent_env["runtime"].messages_script = []
    orch = agent_env["service"].orchestrator
    orch.handle_event(permission_event(run1["session_id"], "per_dup", pattern=["/one/**"]))
    orch.handle_event(permission_event_v2(run2["session_id"], "per_dup",
                                           resources=["/data/requested/**"],
                                           save=["/data/always/**"]))
    assert call(agent_env, "read_opencode_run", run_id=run1["run_id"])["state"] == "waiting_permission"
    assert call(agent_env, "read_opencode_run", run_id=run2["run_id"])["state"] == "waiting_permission"
    rows = agent_env["service"].db.execute(
        "SELECT opencode_request, runtime_request, run FROM agent_requests "
        "WHERE runtime_request='per_dup' ORDER BY run").fetchall()
    assert len(rows) == 2
    # Native identity is identical; legacy storage keys are run-scoped.
    assert {r["runtime_request"] for r in rows} == {"per_dup"}
    assert len({r["opencode_request"] for r in rows}) == 2
    assert call(agent_env, "read_opencode_request", run_id=run1["run_id"],
                request_id="per_dup")["request_id"] == "per_dup"
    # Wrong-run lookups and responses stay rejected.
    with pytest.raises(BridgeError) as exc:
        call(agent_env, "read_opencode_request", run_id=run1["run_id"],
             request_id="nope")
    assert exc.value.code == "not_found"
    with pytest.raises(BridgeError) as exc:
        call(agent_env, "respond_opencode_permission", run_id=run1["run_id"],
             request_id="nope", decision="once")
    assert exc.value.code == "not_found"
    # Resolving run1's ask leaves run2's identical native id untouched.
    answered = call(agent_env, "respond_opencode_permission", run_id=run1["run_id"],
                    request_id="per_dup", decision="once")
    assert answered["request_state"] == "approved" and answered["run_state"] == "running"
    detail2 = call(agent_env, "read_opencode_run", run_id=run2["run_id"])
    assert detail2["state"] == "waiting_permission"
    assert detail2["pending_requests"][0]["request_id"] == "per_dup"
    answered2 = call(agent_env, "respond_opencode_permission", run_id=run2["run_id"],
                     request_id="per_dup", decision="reject")
    assert answered2["request_state"] == "rejected"


def test_reply_routing_uses_native_id_with_generation(agent_env, payload):
    job1 = publish(agent_env, payload)
    run1 = start(agent_env, job1["id"], "route-first")
    job2 = publish(agent_env, payload, request_id="example-2", title="Second")
    run2 = start(agent_env, job2["id"], "route-second")
    agent_env["runtime"].messages_script = []
    orch = agent_env["service"].orchestrator
    orch.handle_event(permission_event(run1["session_id"], "per_v1", pattern=["/x/**"]))
    orch.handle_event(permission_event_v2(run2["session_id"], "per_v2",
                                           resources=["/data/requested/**"],
                                           save=["/data/always/**"]))
    call(agent_env, "respond_opencode_permission", run_id=run1["run_id"],
         request_id="per_v1", decision="once")
    call(agent_env, "respond_opencode_permission", run_id=run2["run_id"],
         request_id="per_v2", decision="always")
    # The backend receives the exact native ids with verbatim generation.
    assert agent_env["runtime"].respond_calls == [
        (run1["session_id"], "per_v1", "once"),
        (run2["session_id"], "per_v2", "always"),
    ]
    assert agent_env["runtime"].respond_generations == ["v1", "v2"]
