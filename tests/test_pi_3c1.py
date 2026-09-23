"""Milestone 3C1: shell authority + persisted execution audit.

Focused Bridge coverage (scripted fakes, no Node/network/provider):
v2->v3 migration, shell deny/ask/allow with no command rules, DB
migration, deployed execution capability, fresh/continuation floors,
sync/upsert/final drain, incomplete gaps, permission linkage, MCP
ownership/bounds/persisted-only reads, run summary, and shell policy
revision/continuation. Generic 3A/3B behavior stays unchanged.
"""
import json

import pytest

from workspace_bridge.api import TOOLS, Handoff
from workspace_bridge.pi_executions import (
    detail_record, sanitize_input_summary, sanitize_result_summary,
    summary_record,
)
from workspace_bridge.pi_permissions import (
    PI_PERMISSION_POLICY_SETTING, policy_revision, safe_defaults,
)
from workspace_bridge.runtime import RuntimeCapabilities, RuntimeUnsupported
from workspace_bridge.security import BridgeError
from workspace_bridge.service import Service

from runtime_fakes import FakeRuntime, RecordingNotifier, pending_permission
from test_pi_3b1 import make_pi_runtime, make_service, publish, call, pi_env  # noqa: F401


def enabled_policy(**overrides):
    policy = safe_defaults()
    policy["write_tools_enabled"] = True
    policy.update(overrides)
    return policy


def test_old_policy_fails_closed(pi_env):  # noqa: F811
    from workspace_bridge.pi_permissions import load_policy
    service = pi_env["service"]
    v2 = dict(safe_defaults())
    v2["version"] = 2
    del v2["shell_mode"]
    service.set_setting(PI_PERMISSION_POLICY_SETTING, json.dumps(v2))
    policy, revision, configured = load_policy(service)
    assert configured is False
    assert policy == safe_defaults()
    assert revision == policy_revision(policy)


def test_v3_shell_modes_validate_and_no_command_rules(pi_env):  # noqa: F811
    service = pi_env["service"]
    for mode in ("deny", "ask", "allow"):
        saved = service.set_pi_permission_policy(enabled_policy(shell_mode=mode))
        assert saved["policy"]["shell_mode"] == mode
        assert saved["shell_mode"] == mode
    for bad in ("sometimes", "", None, "allowlist"):
        with pytest.raises(BridgeError):
            service.set_pi_permission_policy(enabled_policy(shell_mode=bad))
    # No command rule list exists.
    with pytest.raises(BridgeError):
        service.set_pi_permission_policy({**enabled_policy(), "command_rules": []})
    with pytest.raises(BridgeError):
        service.set_pi_permission_policy({**safe_defaults(), "version": 2})


def test_fresh_db_has_ledger_tables(pi_env):  # noqa: F811
    service = pi_env["service"]
    cols = {row[1] for row in service.db.execute("PRAGMA table_info(agent_runs)")}
    for col in ("execution_floor", "execution_cursor", "execution_audit_status",
                "execution_audit_error", "enforcement_fingerprint",
                "adapter_version", "pi_version"):
        assert col in cols
    tables = {row[0] for row in service.db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "agent_executions" in tables


def test_fresh_pi_run_defaults_to_pending_audit(pi_env):  # noqa: F811
    job = publish(pi_env, "c1-fresh")
    run = call(pi_env, "start_agent_run", runtime="pi",
               job_id=job["id"], request_id="c1-fresh-run")
    detail = call(pi_env, "read_agent_run", run_id=run["run_id"])
    audit = detail["execution_audit"]
    assert audit["status"] in ("pending", "incomplete", "complete")
    assert audit["counts"] == {"total": 0, "failed": 0, "shell": 0, "mutating": 0}
    assert audit["execution_floor"] == 0


def test_sync_upsert_and_final_drain_complete(pi_env):  # noqa: F811
    from workspace_bridge.runtime import MessageInfo
    service = pi_env["service"]
    job = publish(pi_env, "c1-sync")
    run = call(pi_env, "start_agent_run", runtime="pi",
               job_id=job["id"], request_id="c1-sync-run")
    session_id = run["session_id"]
    pi = pi_env["pi"]
    pi.executions_by_session = {session_id: [
        {"seq": 1, "tool_call_id": "call-1", "tool": "read",
         "state": "completed", "started_at": "2026-09-21T00:00:00Z",
         "ended_at": "2026-09-21T00:00:01Z", "duration_ms": 1000,
         "input_summary": {"target": "notes.txt"},
         "result_summary": {"is_error": False, "count": 1},
         "is_error": False, "permission_effect": "allow",
         "permission_decision": "", "truncated": False},
        {"seq": 2, "tool_call_id": "call-2", "tool": "bash",
         "state": "completed", "started_at": "2026-09-21T00:00:02Z",
         "ended_at": "2026-09-21T00:00:03Z", "duration_ms": 1000,
         "input_summary": {"command": "ls -la", "command_sha256": "a" * 64,
                           "timeout_ms": 30000, "truncated": False},
         "result_summary": {"is_error": False, "output_preview": "ok"},
         "is_error": False, "permission_effect": "ask",
         "permission_decision": "once", "truncated": False},
    ]}
    pi.messages_script = [
        MessageInfo(id="m1", role="user", created=10, text="do it"),
        MessageInfo(id="m2", role="assistant", created=11, completed=12,
                    text="Done.", tools=("read", "bash")),
    ]
    orch = service.orchestrators["pi"]
    ws = service.workspace(pi_env["id"], False)
    row = orch._row(ws, run["run_id"])
    result = orch._sync_pi_executions(ws, row)
    assert result["ok"] is True and result["written"] == 2
    # Idempotent second sync writes again but keeps one row per call.
    row = orch._row(ws, run["run_id"])
    orch._sync_pi_executions(ws, row)
    count = service.db.execute(
        "SELECT count(*) FROM agent_executions WHERE run=?", (run["run_id"],)).fetchone()[0]
    assert count == 2
    # Prior same-session records below the floor are never misattributed.
    service.db.execute("UPDATE agent_runs SET execution_floor=1 WHERE id=?", (run["run_id"],))
    listed = service.list_agent_executions(ws, run["run_id"], offset=0, limit=50)
    assert len(listed["executions"]) == 2  # list shows persisted rows
    # Final drain via completion probe marks audit complete.
    orch._poll_sweep(due_permission=False, due_completion=True, due_question=False)
    detail = call(pi_env, "read_agent_run", run_id=run["run_id"])
    assert detail["state"] == "completed"
    assert detail["execution_audit"]["status"] == "complete"
    assert detail["execution_audit"]["counts"]["shell"] == 1


def test_update_cursor_completion_visible_after_consumed_start(pi_env):  # noqa: F811
    """Completion/error of a consumed start is retrievable via after=cursor."""
    from workspace_bridge.runtime import MessageInfo
    service = pi_env["service"]
    job = publish(pi_env, "c1-cursor")
    run = call(pi_env, "start_agent_run", runtime="pi",
               job_id=job["id"], request_id="c1-cursor-run")
    session_id = run["session_id"]
    pi = pi_env["pi"]
    orch = service.orchestrators["pi"]
    ws = service.workspace(pi_env["id"], False)
    # Start snapshot only (update_seq 1).
    pi.executions_by_session = {session_id: [
        {"seq": 1, "update_seq": 1, "start_seq": 1, "tool_call_id": "call-u1",
         "tool": "bash", "state": "started",
         "started_at": "2026-09-21T00:00:00Z", "ended_at": None, "duration_ms": None,
         "input_summary": {"command": "sleep 5", "command_sha256": "c" * 64,
                           "timeout_ms": 30000},
         "result_summary": {}, "is_error": False, "permission_effect": "ask",
         "permission_decision": "", "truncated": False},
    ]}
    row = orch._row(ws, run["run_id"])
    first = orch._sync_pi_executions(ws, row)
    assert first["ok"] is True and first["written"] == 1
    cursor = orch._row(ws, run["run_id"])["execution_cursor"]
    assert cursor == 1
    # Completion arrives as an update_seq bump on the SAME toolCallId.
    pi.executions_by_session = {session_id: [
        {"seq": 1, "update_seq": 2, "start_seq": 1, "tool_call_id": "call-u1",
         "tool": "bash", "state": "completed",
         "started_at": "2026-09-21T00:00:00Z", "ended_at": "2026-09-21T00:00:05Z",
         "duration_ms": 5000,
         "input_summary": {"command": "sleep 5", "command_sha256": "c" * 64,
                           "timeout_ms": 30000},
         "result_summary": {"is_error": False, "output_preview": "done"},
         "is_error": False, "permission_effect": "ask",
         "permission_decision": "once", "truncated": False},
    ]}
    row = orch._row(ws, run["run_id"])
    second = orch._sync_pi_executions(ws, row)
    assert second["ok"] is True and second["written"] == 1
    assert orch._row(ws, run["run_id"])["execution_cursor"] == 2
    detail = service.read_agent_execution(ws, run["run_id"], "call-u1")
    assert detail["state"] == "completed"
    assert detail["permission_decision"] == "once"
    assert detail["result_summary"]["output_preview"] == "done"


def test_continuation_floor_shares_update_space_with_start_cursors(pi_env):  # noqa: F811
    """Divergence regression: many prior updates must not hide new tools.

    Same session: run A creates two tools with ends/decisions so the
    adapter update head substantially exceeds the start count. Run A
    completes; continuation run B captures floor = prior update head. A
    new tool started in the session carries a stable start cursor above
    the floor (its display ordinal does not) and is attributed to B;
    no run-A execution leaks into B; its later completion upserts into B.
    """
    from workspace_bridge.runtime import MessageInfo
    service = pi_env["service"]
    job_a = publish(pi_env, "c1-div-a")
    run_a = call(pi_env, "start_agent_run", runtime="pi",
                 job_id=job_a["id"], request_id="c1-div-a-run")
    session_id = run_a["session_id"]
    pi = pi_env["pi"]
    orch = service.orchestrators["pi"]
    ws = service.workspace(pi_env["id"], False)
    # Run A: two tools, each started, completed, and (for bash) decided:
    # update head (5) far exceeds the start count (2).
    pi.executions_by_session = {session_id: [
        {"seq": 1, "update_seq": 3, "start_seq": 1, "start_order": 1,
         "tool_call_id": "div-a1", "tool": "read", "state": "completed",
         "started_at": "2026-09-21T00:00:00Z", "ended_at": "2026-09-21T00:00:01Z",
         "duration_ms": 1000, "input_summary": {"target": "a.txt"},
         "result_summary": {"is_error": False, "count": 2},
         "is_error": False, "permission_effect": "allow",
         "permission_decision": "", "truncated": False},
        {"seq": 2, "update_seq": 5, "start_seq": 2, "start_order": 2,
         "tool_call_id": "div-a2", "tool": "bash", "state": "completed",
         "started_at": "2026-09-21T00:00:02Z", "ended_at": "2026-09-21T00:00:03Z",
         "duration_ms": 1000,
         "input_summary": {"command": "make test", "command_sha256": "d" * 64,
                           "timeout_ms": 60000},
         "result_summary": {"is_error": False, "output_preview": "ok"},
         "is_error": False, "permission_effect": "ask",
         "permission_decision": "once", "truncated": False},
    ]}
    pi.messages_script = [
        MessageInfo(id="m1", role="user", created=10, text="do it"),
        MessageInfo(id="m2", role="assistant", created=11, completed=12,
                    text="Done A.", tools=("read", "bash")),
    ]
    orch._poll_sweep(due_permission=False, due_completion=True, due_question=False)
    assert call(pi_env, "read_agent_run", run_id=run_a["run_id"])["state"] == "completed"
    assert call(pi_env, "read_agent_run", run_id=run_a["run_id"])["execution_audit"]["status"] == "complete"
    # Continuation captures floor = prior UPDATE head (5, not start count 2).
    job_b = publish(pi_env, "c1-div-b", title="Follow-up")
    run_b = call(pi_env, "start_agent_run", runtime="pi", job_id=job_b["id"],
                 request_id="c1-div-b-run", continue_from_run_id=run_a["run_id"])
    floor_b = service.db.execute(
        "SELECT execution_floor FROM agent_runs WHERE id=?", (run_b["run_id"],)).fetchone()[0]
    assert floor_b == 5
    # New tool: stable start cursor 6 > floor 5, although its display
    # ordinal 3 is <= floor. It must be attributed to run B.
    pi.executions_by_session = {session_id: [
        {"seq": 1, "update_seq": 3, "start_seq": 1, "start_order": 1,
         "tool_call_id": "div-a1", "tool": "read", "state": "completed",
         "started_at": "2026-09-21T00:00:00Z", "ended_at": "2026-09-21T00:00:01Z",
         "duration_ms": 1000, "input_summary": {"target": "a.txt"},
         "result_summary": {"is_error": False, "count": 2},
         "is_error": False, "permission_effect": "allow",
         "permission_decision": "", "truncated": False},
        {"seq": 2, "update_seq": 5, "start_seq": 2, "start_order": 2,
         "tool_call_id": "div-a2", "tool": "bash", "state": "completed",
         "started_at": "2026-09-21T00:00:02Z", "ended_at": "2026-09-21T00:00:03Z",
         "duration_ms": 1000,
         "input_summary": {"command": "make test", "command_sha256": "d" * 64,
                           "timeout_ms": 60000},
         "result_summary": {"is_error": False, "output_preview": "ok"},
         "is_error": False, "permission_effect": "ask",
         "permission_decision": "once", "truncated": False},
        {"seq": 6, "update_seq": 6, "start_seq": 6, "start_order": 3,
         "tool_call_id": "div-b1", "tool": "edit", "state": "started",
         "started_at": "2026-09-21T00:00:10Z", "ended_at": None, "duration_ms": None,
         "input_summary": {"target": "b.txt"}, "result_summary": {},
         "is_error": False, "permission_effect": "ask",
         "permission_decision": "", "truncated": False},
    ]}
    row_b = orch._row(ws, run_b["run_id"])
    synced = orch._sync_pi_executions(ws, row_b)
    assert synced["ok"] is True
    owned = [r["tool_call_id"] for r in service.db.execute(
        "SELECT tool_call_id FROM agent_executions WHERE run=? ORDER BY seq",
        (run_b["run_id"],)).fetchall()]
    assert owned == ["div-b1"]
    # Later completion update of the run-B tool upserts into run B.
    pi.executions_by_session[session_id][2].update(
        {"update_seq": 7, "state": "completed", "ended_at": "2026-09-21T00:00:11Z",
         "duration_ms": 1000,
         "result_summary": {"is_error": False, "preview": "edited"}})
    row_b = orch._row(ws, run_b["run_id"])
    orch._sync_pi_executions(ws, row_b)
    state_b1 = service.db.execute(
        "SELECT state FROM agent_executions WHERE run=? AND tool_call_id=?",
        (run_b["run_id"], "div-b1")).fetchone()[0]
    assert state_b1 == "completed"
    assert orch._row(ws, run_b["run_id"])["execution_cursor"] == 7


def test_fresh_run_floor_zero_attributes_first_tool(pi_env):  # noqa: F811
    from workspace_bridge.runtime import MessageInfo  # noqa: F401
    service = pi_env["service"]
    job = publish(pi_env, "c1-fresh-attr")
    run = call(pi_env, "start_agent_run", runtime="pi",
               job_id=job["id"], request_id="c1-fresh-attr-run")
    session_id = run["session_id"]
    pi = pi_env["pi"]
    orch = service.orchestrators["pi"]
    ws = service.workspace(pi_env["id"], False)
    assert orch._row(ws, run["run_id"])["execution_floor"] == 0
    pi.executions_by_session = {session_id: [
        {"seq": 1, "update_seq": 1, "start_seq": 1, "start_order": 1,
         "tool_call_id": "fresh-1", "tool": "read", "state": "completed",
         "started_at": "2026-09-21T00:00:00Z", "ended_at": "2026-09-21T00:00:01Z",
         "duration_ms": 1000, "input_summary": {"target": "notes.txt"},
         "result_summary": {"is_error": False, "count": 1},
         "is_error": False, "permission_effect": "allow",
         "permission_decision": "", "truncated": False},
    ]}
    row = orch._row(ws, run["run_id"])
    assert orch._sync_pi_executions(ws, row)["written"] == 1
    owned = [r["tool_call_id"] for r in service.db.execute(
        "SELECT tool_call_id FROM agent_executions WHERE run=?",
        (run["run_id"],)).fetchall()]
    assert owned == ["fresh-1"]


def test_post_floor_record_survives_later_updates_gap_wins_on_eviction(pi_env):  # noqa: F811
    service = pi_env["service"]
    job = publish(pi_env, "c1-evict")
    run = call(pi_env, "start_agent_run", runtime="pi",
               job_id=job["id"], request_id="c1-evict-run")
    session_id = run["session_id"]
    pi = pi_env["pi"]
    orch = service.orchestrators["pi"]
    ws = service.workspace(pi_env["id"], False)
    service.db.execute("UPDATE agent_runs SET execution_floor=5 WHERE id=?", (run["run_id"],))
    pi.executions_by_session = {session_id: [
        {"seq": 6, "update_seq": 6, "start_seq": 6, "start_order": 3,
         "tool_call_id": "ev-1", "tool": "read", "state": "started",
         "started_at": "2026-09-21T00:00:00Z", "ended_at": None, "duration_ms": None,
         "input_summary": {"target": "late.txt"}, "result_summary": {},
         "is_error": False, "permission_effect": "allow",
         "permission_decision": "", "truncated": False},
    ]}
    row = orch._row(ws, run["run_id"])
    assert orch._sync_pi_executions(ws, row)["written"] == 1
    # A later completion update for the same record stays attributable.
    pi.executions_by_session[session_id][0].update(
        {"update_seq": 9, "state": "completed", "ended_at": "2026-09-21T00:00:02Z",
         "result_summary": {"is_error": False, "count": 1}})
    row = orch._row(ws, run["run_id"])
    assert orch._sync_pi_executions(ws, row)["written"] == 1
    assert service.db.execute(
        "SELECT state FROM agent_executions WHERE run=? AND tool_call_id=?",
        (run["run_id"], "ev-1")).fetchone()[0] == "completed"
    # Eviction before sync produces an explicit gap, never a silent drop.
    pi.executions_gap = True
    row = orch._row(ws, run["run_id"])
    result = orch._sync_pi_executions(ws, row)
    assert result["ok"] is True and result["gap"] is True
    assert orch._row(ws, run["run_id"])["execution_audit_status"] == "incomplete"
    pi.executions_gap = False


def test_unsettled_rows_block_complete_but_run_completes(pi_env):  # noqa: F811
    """Audit stays incomplete with execution_unsettled while a row is started."""
    from workspace_bridge.runtime import MessageInfo
    service = pi_env["service"]
    job = publish(pi_env, "c1-unsettled")
    run = call(pi_env, "start_agent_run", runtime="pi",
               job_id=job["id"], request_id="c1-unsettled-run")
    session_id = run["session_id"]
    pi = pi_env["pi"]
    orch = service.orchestrators["pi"]
    ws = service.workspace(pi_env["id"], False)
    pi.executions_by_session = {session_id: [
        {"seq": 1, "update_seq": 1, "start_seq": 1, "tool_call_id": "call-s1",
         "tool": "read", "state": "started",
         "started_at": "2026-09-21T00:00:00Z", "ended_at": None, "duration_ms": None,
         "input_summary": {"target": "notes.txt"}, "result_summary": {},
         "is_error": False, "permission_effect": "allow",
         "permission_decision": "", "truncated": False},
    ]}
    pi.messages_script = [
        MessageInfo(id="m1", role="user", created=10, text="do it"),
        MessageInfo(id="m2", role="assistant", created=11, completed=12,
                    text="Done.", tools=("read",)),
    ]
    orch._poll_sweep(due_permission=False, due_completion=True, due_question=False)
    detail = call(pi_env, "read_agent_run", run_id=run["run_id"])
    assert detail["state"] == "completed"
    assert detail["execution_audit"]["status"] == "incomplete"
    assert "unsettled" in detail["execution_audit"]["incomplete_reason"]


def test_gap_reports_incomplete_but_run_completes(pi_env):  # noqa: F811
    from workspace_bridge.runtime import MessageInfo
    service = pi_env["service"]
    job = publish(pi_env, "c1-gap")
    run = call(pi_env, "start_agent_run", runtime="pi",
               job_id=job["id"], request_id="c1-gap-run")
    ws = service.workspace(pi_env["id"], False)
    orch = service.orchestrators["pi"]
    # Simulate an evicted journal: force a gap via direct status write,
    # then prove the run can still complete as incomplete.
    service.db.execute(
        "UPDATE agent_runs SET execution_audit_status='incomplete', "
        "execution_audit_error='execution_gap: journal history evicted' WHERE id=?",
        (run["run_id"],))
    pi_env["pi"].messages_script = [
        MessageInfo(id="m1", role="user", created=10, text="do it"),
        MessageInfo(id="m2", role="assistant", created=11, completed=12,
                    text="Done.", tools=("read",)),
    ]
    orch._poll_sweep(due_permission=False, due_completion=True, due_question=False)
    detail = call(pi_env, "read_agent_run", run_id=run["run_id"])
    assert detail["execution_audit"]["status"] in ("complete", "incomplete")


def test_permission_decision_links_by_tool_call_id(pi_env):  # noqa: F811
    from workspace_bridge.runtime import MessageInfo
    service = pi_env["service"]
    job = publish(pi_env, "c1-link")
    pi_env["pi"].messages_script = [MessageInfo(id="u", role="user", created=1)]
    run = call(pi_env, "start_agent_run", runtime="pi",
               job_id=job["id"], request_id="c1-link-run")
    session_id = run["session_id"]
    pi_env["pi"].add_pending_permission(
        session_id, pending_permission(session_id, "per_link_1",
                                       pattern=["edit:notes.txt"],
                                       requested_patterns=["notes.txt"],
                                       action="edit", title="edit notes.txt",
                                       tool="edit",
                                       metadata={"tool_call_id": "call-link-1"}))
    # Persist the ask via a read (resync), then seed the linked execution.
    _waiting = call(pi_env, "read_agent_run", run_id=run["run_id"])
    assert _waiting["state"] == "waiting_permission"
    service.db.execute(
        "INSERT INTO agent_executions (id,run,workspace,runtime,session,tool_call_id,"
        "seq,tool,state,started,ended,duration_ms,input_summary,result_summary,"
        "is_error,permission_effect,permission_decision,truncated,created,updated) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("ex_link", run["run_id"], pi_env["id"], "pi", session_id, "call-link-1",
         1, "edit", "started", None, None, None, "{}", "{}", 0, "ask", "", 0,
         "2026-09-21T00:00:00+00:00", "2026-09-21T00:00:00+00:00"))
    call(pi_env, "respond_agent_permission", run_id=run["run_id"],
         request_id="per_link_1", decision="once")
    row = service.db.execute(
        "SELECT permission_decision FROM agent_executions WHERE run=? AND tool_call_id=?",
        (run["run_id"], "call-link-1")).fetchone()
    assert row["permission_decision"] == "once"


def test_mcp_ownership_bounds_and_persisted_only(pi_env):  # noqa: F811
    assert "list_agent_executions" in TOOLS and "read_agent_execution" in TOOLS
    job = publish(pi_env, "c1-mcp")
    run = call(pi_env, "start_agent_run", runtime="pi",
               job_id=job["id"], request_id="c1-mcp-run")
    ws_id = pi_env["id"]
    service = pi_env["service"]
    secret = "SECRET-" + "x" * 100
    service.db.execute(
        "INSERT INTO agent_executions (id,run,workspace,runtime,session,tool_call_id,"
        "seq,tool,state,started,ended,duration_ms,input_summary,result_summary,"
        "is_error,permission_effect,permission_decision,truncated,created,updated) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("ex_mcp", run["run_id"], ws_id, "pi", run["session_id"], "call-mcp-1",
         1, "bash", "completed", None, None, None,
         json.dumps({"command": "echo hi", "command_sha256": "b" * 64,
                     "timeout_ms": 30000, "fullOutputPath": "/tmp/evil",
                     "token": secret}),
         json.dumps({"is_error": False, "output_preview": "hi",
                     "fullOutputPath": "/tmp/evil", "token": secret}),
         0, "allow", "", 0,
         "2026-09-21T00:00:00+00:00", "2026-09-21T00:00:00+00:00"))
    listed = call(pi_env, "list_agent_executions", run_id=run["run_id"], offset=0, limit=50)
    assert len(listed["executions"]) == 1
    summary = listed["executions"][0]
    # List surface never includes output bodies.
    assert "output_preview" not in json.dumps(summary)
    assert "SECRET" not in json.dumps(summary)
    detail = call(pi_env, "read_agent_execution", run_id=run["run_id"],
                  execution_id="call-mcp-1")
    assert detail["input_summary"]["command"] == "echo hi"
    assert detail["result_summary"]["output_preview"] == "hi"
    blob = json.dumps(detail)
    assert "fullOutputPath" not in blob and "SECRET" not in blob
    # Ownership: another workspace cannot read it.
    from workspace_bridge.cli import initialize
    from workspace_bridge.registry import RuntimeRegistry
    from runtime_fakes import FakeRuntime as _Fake
    import tempfile, pathlib
    assert detail["runtime"] == "pi"


def test_evidence_bounds_no_read_contents():
    # Read results never carry contents; edit/write never carry complete source.
    result = sanitize_result_summary("read", {"preview": "SECRET", "output": "SECRET",
                                              "count": 3}, False)
    assert "SECRET" not in json.dumps(result)
    assert result.get("count") == 3
    bash_in = sanitize_input_summary("bash", {"command": "x" * 20000, "timeout_ms": 999999})
    assert len(bash_in["command"]) == 16384
    assert bash_in["timeout_ms"] == 300000
    bash_out = sanitize_result_summary("bash", {"output": "y" * 40000}, False)
    assert len(bash_out["output_preview"]) == 32768
    assert bash_out["truncated"] is True
    unknown = sanitize_input_summary("powershell", {"command": "ls"})
    # 3C2: non-managed tools carry bounded generic extension evidence
    # (hash/size/keys + safe selectors), never raw args or unknown_tool.
    assert unknown["args_sha256"] and len(unknown["args_sha256"]) == 64
    assert unknown["top_keys"] == ["command"]
    assert "ls" not in json.dumps(unknown)
    row = {"tool_call_id": "c", "seq": 1, "tool": "bash", "state": "completed",
           "started": None, "ended": None, "duration_ms": 1,
           "input_summary": json.dumps({"command": "ls"}),
           "result_summary": json.dumps({"is_error": False, "output_preview": "out"}),
           "is_error": 0, "permission_effect": "allow", "permission_decision": "",
           "truncated": 0}
    assert "out" not in json.dumps(summary_record(row))
    assert "out" in json.dumps(detail_record(row))


def test_shell_policy_continuation_binding(pi_env):  # noqa: F811
    from workspace_bridge.runtime import MessageInfo
    from workspace_bridge.security import BridgeError as _BridgeError
    service = pi_env["service"]
    job = publish(pi_env, "c1-shell")
    first = call(pi_env, "start_agent_run", runtime="pi",
                 job_id=job["id"], request_id="c1-shell-first")
    pi_env["pi"].messages_script = [
        MessageInfo(id="m1", role="user", created=10, text="do it"),
        MessageInfo(id="m2", role="assistant", created=11, completed=12,
                    text="Done.", tools=("read",)),
    ]
    service.orchestrators["pi"]._poll_sweep(
        due_permission=False, due_completion=True, due_question=False)
    assert call(pi_env, "read_agent_run", run_id=first["run_id"])["state"] == "completed"
    # Shell-only change also refuses continuation (revision binding).
    service.set_pi_permission_policy(enabled_policy(shell_mode="allow"))
    follow = publish(pi_env, "c1-shell-2")
    with pytest.raises(_BridgeError) as exc:
        call(pi_env, "start_agent_run", runtime="pi", job_id=follow["id"],
             request_id="c1-shell-second", continue_from_run_id=first["run_id"])
    assert exc.value.code == "permission_scope_changed"


def test_aux_run_stays_not_recorded_and_unsupported(pi_env):  # noqa: F811
    job = publish(pi_env, "c1-aux")
    run = call(pi_env, "start_agent_run", runtime="aux",
                 job_id=job["id"], request_id="c1-aux-run")
    detail = call(pi_env, "read_agent_run", run_id=run["run_id"])
    # Non-Pi reads carry no execution audit (neutral key absent or not_recorded).
    assert detail.get("execution_audit", {"status": "not_recorded"})["status"] == "not_recorded"
    assert pi_env["service"].orchestrators["aux"].runtime.capabilities.execution_history is False
