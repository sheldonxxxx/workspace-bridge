"""3A3 audit corrections: real Pi completion evidence, idle gating, cursor
isolation, neutral admin routes, and persisted-history readability.

Scripted fakes only, except one real HttpPiRuntime transport test served by
a fake HTTP layer (no Node, network, provider credentials, or real model).
"""
import json
import urllib.request

import httpx
import pytest

from workspace_bridge.api import Handoff, make_admin
from workspace_bridge.runtime import HttpPiRuntime, MessageInfo, ModelInfo, RuntimeCapabilities
from workspace_bridge.security import BridgeError
from workspace_bridge.service import Service

from runtime_fakes import FakeRuntime, RecordingNotifier

PI_MODELS = [
    ModelInfo(selector="pi/default", provider="pi", model="default", name="Pi Default"),
]


class FakePiRuntime(FakeRuntime):
    """Scripted Pi backend: workspace-scoped discovery, no events/snapshots."""

    def __init__(self, directory: str):
        super().__init__(directory, models=list(PI_MODELS))
        self._runtime_id = "pi"
        self._capabilities = RuntimeCapabilities(
            model_discovery=True, session_reuse=True, event_polling=False,
            session_status=True, pending_snapshot=False, permission_response=False,
            question_detection=False, question_response=False, session_branching=False)
        self.poll_calls: list = []

    def list_models(self, directory=None):
        if not isinstance(directory, str) or not directory:
            raise BridgeError("Pi model discovery requires a workspace directory",
                              "invalid_arguments")
        self.model_calls.append(directory)
        return list(self.models)

    def poll_events(self, cursor, timeout=25.0):
        self.poll_calls.append((cursor, timeout))
        from workspace_bridge.runtime import RuntimeUnsupported
        raise RuntimeUnsupported("Pi runtime does not support event polling")


@pytest.fixture
def dual_env(tmp_path):
    from workspace_bridge.cli import initialize
    parent = tmp_path / "projects"
    parent.mkdir()
    root = parent / "alpha"
    root.mkdir()
    state = tmp_path / "private-state"
    cfg = initialize(state, [str(parent)], 8765, 8766)
    aux = FakeRuntime(str(root))
    aux._runtime_id = "aux"
    pi = FakePiRuntime(str(root))
    service = Service(state, cfg, runtimes={"aux": aux, "pi": pi},
                      notifier=RecordingNotifier(), orchestrator_background=False)
    ws_id = service.add_workspace("Alpha", str(root), [])["workspace"]["id"]
    token = service.manage_bridge("rotate_token")["token"]
    service.manage_workspace(ws_id, "enable")
    service.manage_workspace(ws_id, "set_agent_enabled", agent_enabled=True)
    ws = service.workspace(ws_id)
    service.orchestrators["aux"].set_model_policy(
        ["anthropic/claude-sonnet", "glm/zai-glm-5.2"], "anthropic/claude-sonnet", ws)
    yield {"service": service, "aux": aux, "pi": pi, "root": root,
           "state": state, "config": cfg, "id": ws_id, "token": token, "tmp": tmp_path}
    service.close()


def publish(dual_env, request_id="handoff-1", title="Work"):
    return dual_env["service"].call(
        dual_env["id"], dual_env["token"], "prepare_handoff",
        Handoff.model_validate({"request_id": request_id, "title": title, "goal": "g",
                                "plan": "p", "acceptance": "a", "constraints": "c",
                                "context": "x", "context_hashes": {}}).model_dump())


def call(dual_env, tool, **args):
    return dual_env["service"].call(dual_env["id"], dual_env["token"], tool, args)


def set_pi_policy(dual_env):
    ws = dual_env["service"].workspace(dual_env["id"])
    return dual_env["service"].orchestrators["pi"].set_model_policy(
        ["pi/default"], "pi/default", ws)


# --------------------------------------------- idle-gated no-event completion
def test_pi_busy_with_terminal_message_stays_running(dual_env):
    set_pi_policy(dual_env)
    job = publish(dual_env, "idle-1")
    run = call(dual_env, "start_agent_run", runtime="pi",
               job_id=job["id"], request_id="idle-run")
    pi = dual_env["pi"]
    session = run["session_id"]
    terminal = [
        MessageInfo(id="m1", role="user", created=10, text="do it"),
        MessageInfo(id="m2", role="assistant", created=11, completed=12,
                    text="Done.", tools=("read",)),
    ]
    pi.messages_script = list(terminal)
    pi.set_session_status("busy", session)
    sweep = dual_env["service"].orchestrators["pi"]._poll_sweep(
        due_permission=True, due_completion=True, due_question=True)
    assert sweep["checked"] == 1 and sweep["recovered"] == 0
    assert pi.poll_calls == []
    detail = call(dual_env, "read_agent_run", run_id=run["run_id"])
    assert detail["state"] in ("starting", "running")
    # Idle with the same durable evidence completes without any event.
    pi.set_session_status("idle", session)
    sweep = dual_env["service"].orchestrators["pi"]._poll_sweep(
        due_permission=True, due_completion=True, due_question=True)
    assert sweep["recovered"] >= 1
    assert pi.poll_calls == []
    detail = call(dual_env, "read_agent_run", run_id=run["run_id"])
    assert detail["state"] == "completed"
    assert detail["result"]["summary"] == "Done."


def test_aux_busy_still_completes_from_durable_evidence(dual_env):
    job = publish(dual_env, "aux-busy-1")
    run = call(dual_env, "start_agent_run", runtime="aux",
               job_id=job["id"], request_id="aux-busy-run")
    dual_env["aux"].set_session_status("busy", run["session_id"])
    # Generic behavior is unchanged: durable evidence completes even busy.
    detail = call(dual_env, "read_agent_run", run_id=run["run_id"])
    assert detail["state"] == "completed"


def test_restart_reconcile_defers_busy_no_event_sessions(dual_env):
    set_pi_policy(dual_env)
    job = publish(dual_env, "recon-1")
    run = call(dual_env, "start_agent_run", runtime="pi",
               job_id=job["id"], request_id="recon-run")
    pi = dual_env["pi"]
    pi.messages_script = [
        MessageInfo(id="m1", role="user", created=10, text="do it"),
        MessageInfo(id="m2", role="assistant", created=11, completed=12,
                    text="Done.", tools=("read",)),
    ]
    pi.set_session_status("busy", run["session_id"])
    service = dual_env["service"]
    service.orchestrators["pi"].reconcile_startup()
    state = service.db.execute("SELECT state FROM agent_runs WHERE id=?",
                               (run["run_id"],)).fetchone()["state"]
    assert state in ("starting", "running")
    pi.set_session_status("idle", run["session_id"])
    service.orchestrators["pi"].retry_reconcile()
    sweep = service.orchestrators["pi"]._poll_sweep(
        due_permission=False, due_completion=True, due_question=False)
    assert sweep["recovered"] >= 1
    assert call(dual_env, "read_agent_run", run_id=run["run_id"])["state"] == "completed"


# --------------------------------------------- cursor/instance key isolation
def test_cursor_keys_are_runtime_scoped_and_pi_starts_no_pump(tmp_path):
    from workspace_bridge.cli import initialize
    parent = tmp_path / "projects"
    parent.mkdir()
    root = parent / "alpha"
    root.mkdir()
    state = tmp_path / "private-state"
    cfg = initialize(state, [str(parent)], 8765, 8766)
    aux = FakeRuntime(str(root))
    aux._runtime_id = "aux"
    pi = FakePiRuntime(str(root))
    other = FakeRuntime(str(root))
    other._runtime_id = "b2"
    service = Service(state, cfg,
                      runtimes={"aux": aux, "pi": pi, "b2": other},
                      notifier=RecordingNotifier(), orchestrator_background=True)
    try:
        aux_orch, pi_orch, b2 = (service.orchestrators["aux"], service.orchestrators["pi"],
                                 service.orchestrators["b2"])
        assert aux_orch._cursor_setting_keys() == ("runtime_instance:aux", "runtime_cursor:aux")
        assert pi_orch._cursor_setting_keys() == ("runtime_instance:pi", "runtime_cursor:pi")
        assert b2._cursor_setting_keys() == ("runtime_instance:b2", "runtime_cursor:b2")
        assert pi_orch._event_polling_supported() is False
        assert aux_orch._event_polling_supported() is True
        aux_orch.start()
        pi_orch.start()
        try:
            # Every runtime uses its exact id as the cursor-key suffix;
            # Pi writes nothing event-related.
            assert service.setting("runtime_instance:aux") == "adapter-1"
            assert service.setting("runtime_instance") is None
            assert service.setting("runtime_instance:pi") is None
            assert service.setting("runtime_cursor:pi") is None
            assert aux_orch._pump is not None and pi_orch._pump is None
            assert pi_orch._poll is not None
            assert pi.poll_calls == []
        finally:
            aux_orch.stop()
            pi_orch.stop()
    finally:
        service.close()


# --------------------------------------------- neutral admin routes by owner
@pytest.mark.asyncio
async def test_admin_run_routes_serve_pi_through_owner(dual_env):
    set_pi_policy(dual_env)
    service = dual_env["service"]
    token = (dual_env["state"] / "admin-token").read_text().strip()
    job = publish(dual_env, "admin-pi-1")
    aux_run = call(dual_env, "start_agent_run", runtime="aux",
                  job_id=job["id"], request_id="admin-pi-aux")
    pi_run = call(dual_env, "start_agent_run", runtime="pi",
                  job_id=job["id"], request_id="admin-pi-run")
    dual_env["pi"].messages_script = []
    dual_env["aux"].messages_script = []
    dual_env["pi"].messages_script = []
    async with httpx.AsyncClient(
            transport=httpx.ASGITransport(
                app=make_admin(service, service.config["admin_token_hash"])),
            base_url="http://127.0.0.1:8766",
            headers={"Authorization": "Bearer " + token}) as client:
        detail = (await client.get(f"/api/runs/{pi_run['run_id']}")).json()
        assert detail["runtime"] == "pi" and detail["run_id"] == pi_run["run_id"]
        session = (await client.get(f"/api/runs/{pi_run['run_id']}/session")).json()
        assert session["runtime"] == "pi"
        assert session["transcript"] == []
        stopped = (await client.post(f"/api/runs/{pi_run['run_id']}/stop")).json()
        assert stopped["state"] == "cancelled"
        assert dual_env["pi"].abort_calls
        # Pi has no permission surface: reply fails closed, never remote.
        denied = await client.post(f"/api/runs/{pi_run['run_id']}/requests/anything",
                                   json={"decision": "once"})
        assert denied.status_code == 400
        assert dual_env["pi"].respond_calls == []
        # Aux through the same generic routes is unchanged.
        aux_detail = (await client.get(f"/api/runs/{aux_run['run_id']}")).json()
        assert aux_detail["runtime"] == "aux"
        aux_stopped = (await client.post(f"/api/runs/{aux_run['run_id']}/stop")).json()
        assert aux_stopped["state"] == "cancelled"
        # Project run listing is intentionally cross-runtime.
        ws_runs = (await client.get(f"/api/workspaces/{dual_env['id']}/runs")).json()
        assert {r["run_id"] for r in ws_runs["runs"]} >= {aux_run["run_id"], pi_run["run_id"]}
        assert {r["runtime"] for r in ws_runs["runs"]} >= {"aux", "pi"}
        # Runtime filter stays scoped.
        scoped = (await client.get("/api/sessions", params={"runtime": "aux"})).json()
        assert all(r["runtime"] == "aux" for r in scoped["runs"])


# --------------------------------------------- persisted history readability
def _insert_hist_pi(service, ws_id, job_id, run_id="run_hist_pi2", state="completed"):
    with service.lock, service.db:
        service.db.execute(
            "INSERT INTO agent_runs (id,workspace,runtime,job,request_id,request_hash,"
            "parent_run,session,model,state,error_code,error_message,result,notification,"
            "created,started,updated,finished,message_floor_ms,session_reused,transcript) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, ws_id, "pi", job_id, f"req-{run_id}", "hash", None,
             "ses_hist", "pi/default", state, None, None,
             '{"summary": "old work", "reason": "background_reconcile", '
             '"message_count": 2, "has_final_response": true}',
             "{}", "2026-09-21T00:00:00+00:00", "2026-09-21T00:00:01+00:00",
             "2026-09-21T00:00:02+00:00", "2026-09-21T00:00:03+00:00", 0, 0,
             '[{"id": "m2", "role": "assistant", "text": "old work", "tools": ["read"], '
             '"error": null, "created": 11, "completed": 12}]'))
        service.db.execute(
            "INSERT INTO agent_requests (id,run,workspace,session,"
            "runtime_request,kind,action,resource,pattern,metadata,explanation,redacted,"
            "state,decision,created,updated,resolved,generation) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"req_{run_id}", run_id, ws_id, "ses_hist",
             "per_hist", "permission", "edit", "notes", '["/x/**"]',
             '{"requested_patterns": ["/x/**"]}', "notes", 0,
             "approved", "once", "2026-09-21T00:00:01+00:00", "2026-09-21T00:00:02+00:00",
             "2026-09-21T00:00:02+00:00", "v1"))


def _hist_service(tmp_path):
    from workspace_bridge.cli import initialize
    parent = tmp_path / "projects"
    parent.mkdir()
    root = parent / "alpha"
    root.mkdir()
    state = tmp_path / "private-state"
    cfg = initialize(state, [str(parent)], 8765, 8766)
    runtime = FakeRuntime(str(root))
    # Registered under a foreign id so the inserted Pi rows exercise the
    # unconfigured-runtime persisted-history path with zero backend contact.
    runtime._runtime_id = "aux"
    service = Service(state, cfg, runtimes={"aux": runtime},
                      notifier=RecordingNotifier(), orchestrator_background=False)
    ws_id = service.add_workspace("Alpha", str(root), [])["workspace"]["id"]
    token = service.manage_bridge("rotate_token")["token"]
    service.manage_workspace(ws_id, "enable")
    job = service.call(ws_id, token, "prepare_handoff",
                       Handoff.model_validate(
                           {"request_id": "hist-2", "title": "Hist", "goal": "g",
                            "plan": "p", "acceptance": "a", "constraints": "c",
                            "context": "x", "context_hashes": {}}).model_dump())
    _insert_hist_pi(service, ws_id, job["id"])
    return service, runtime, ws_id, token


def test_historical_pi_row_readable_without_backend(tmp_path):
    service, runtime, ws_id, _ = _hist_service(tmp_path)
    try:
        ws = service.workspace(ws_id)
        before = (len(runtime.get_session_calls), len(runtime.messages_calls),
                  len(runtime.model_calls), len(runtime.respond_calls),
                  len(runtime.abort_calls))
        listed = service.list_agent_runs(ws, runtime="pi")
        assert [r["run_id"] for r in listed["runs"]] == ["run_hist_pi2"]
        view = service.read_agent_run(ws, "run_hist_pi2")
        assert view["runtime"] == "pi" and view["state"] == "completed"
        assert view["runtime_available"] is False
        assert view["agent_evidence"] == "unverified"
        assert view["result"]["summary"] == "old work"
        assert view["result"]["has_final_response"] is True
        assert len(view["requests"]) == 1
        assert view["requests"][0]["request_id"] == "per_hist"
        assert view["requests"][0]["pattern"] == ["/x/**"]
        assert view["pending_request_count"] == 0
        req = service.read_agent_request(ws, "run_hist_pi2", "per_hist")
        assert req["request_id"] == "per_hist" and req["run_state"] == "completed"
        assert req["runtime_available"] is False
        snap = service.admin_read_agent_run("run_hist_pi2",
                                            include_transcript=True, limit=40)
        assert snap["transcript"] and snap["transcript"][0]["text"] == "old work"
        with pytest.raises(BridgeError) as exc:
            service.cancel_agent_run(ws, "run_hist_pi2")
        assert exc.value.code == "unknown_runtime"
        with pytest.raises(BridgeError) as exc:
            service.respond_agent_permission(ws, "run_hist_pi2", "per_hist", "once")
        assert exc.value.code == "unknown_runtime"
        after = (len(runtime.get_session_calls), len(runtime.messages_calls),
                 len(runtime.model_calls), len(runtime.respond_calls),
                 len(runtime.abort_calls))
        assert after == before
    finally:
        service.close()


def test_historical_active_pi_row_has_no_live_transcript(tmp_path):
    service, runtime, ws_id, _ = _hist_service(tmp_path)
    try:
        ws = service.workspace(ws_id)
        with service.lock, service.db:
            service.db.execute("UPDATE agent_runs SET state='running', transcript='[]' "
                               "WHERE id='run_hist_pi2'")
        view = service.admin_read_agent_run("run_hist_pi2",
                                            include_transcript=True, limit=40)
        assert view["state"] == "running"
        assert view["transcript"] == [{"error": "transcript unavailable"}]
    finally:
        service.close()


@pytest.mark.asyncio
async def test_mcp_neutral_read_serves_persisted_history(tmp_path):
    service, _, ws_id, token = _hist_service(tmp_path)
    try:
        detail = service.call(ws_id, token, "read_agent_run", {"run_id": "run_hist_pi2"})
        assert detail["workspace_id"] == ws_id and detail["runtime"] == "pi"
        assert detail["runtime_available"] is False
        req = service.call(ws_id, token, "read_agent_request",
                           {"run_id": "run_hist_pi2", "request_id": "per_hist"})
        assert req["request_id"] == "per_hist"
    finally:
        service.close()


# --------------------------------------------- canonical runtime-id grammar
def test_registry_rejects_noncanonical_ids():
    from workspace_bridge.registry import RuntimeRegistry
    from workspace_bridge.runtime import RUNTIME_ID_PATTERN, is_valid_runtime_id
    assert RUNTIME_ID_PATTERN == r"^[a-z0-9][a-z0-9_-]{0,31}$"
    bad_ids = ["a:b", "A", "has space", "", " leading", "trailing ", "UPPER",
               "a/b", "a.b", "pi!", "é", "x" * 33, "-abc", "_abc", "a\nb"]
    for bad in bad_ids:
        candidate = FakeRuntime("/tmp")
        candidate._runtime_id = bad
        with pytest.raises(BridgeError) as exc:
            RuntimeRegistry().register(candidate)
        assert exc.value.code == "invalid_arguments", bad
        # A matching explicit key does not rescue a noncanonical identity.
        candidate2 = FakeRuntime("/tmp")
        candidate2._runtime_id = bad
        with pytest.raises(BridgeError) as exc:
            RuntimeRegistry().register(candidate2, runtime_id=bad)
        assert exc.value.code == "invalid_arguments", bad
        assert not is_valid_runtime_id(bad), bad
    good_ids = ["a", "0", "ab", "a-b", "a_b", "a-b_c9", "x" * 32,
                "aux", "pi"]
    for good in good_ids:
        candidate = FakeRuntime("/tmp")
        candidate._runtime_id = good
        assert RuntimeRegistry().register(candidate) is candidate
        assert is_valid_runtime_id(good), good


def test_cursor_keys_lossless_for_distinct_valid_ids(tmp_path):
    from workspace_bridge.cli import initialize
    from workspace_bridge.registry import RuntimeRegistry
    parent = tmp_path / "projects"
    parent.mkdir()
    root = parent / "alpha"
    root.mkdir()
    state = tmp_path / "private-state"
    cfg = initialize(state, [str(parent)], 8765, 8766)
    first, second = FakeRuntime(str(root)), FakeRuntime(str(root))
    first._runtime_id, second._runtime_id = "a-b", "ab"
    long1, long2 = FakeRuntime(str(root)), FakeRuntime(str(root))
    long1._runtime_id, long2._runtime_id = "m" + "b" * 31, "m" + "c" * 31
    service = Service(state, cfg,
                      runtimes={"a-b": first, "ab": second,
                                long1._runtime_id: long1, long2._runtime_id: long2},
                      notifier=RecordingNotifier(), orchestrator_background=False)
    try:
        keys = {rid: orch._cursor_setting_keys()
                for rid, orch in service.orchestrators.items()}
        assert keys["a-b"] == ("runtime_instance:a-b", "runtime_cursor:a-b")
        assert keys["ab"] == ("runtime_instance:ab", "runtime_cursor:ab")
        assert keys[long1._runtime_id] == (f"runtime_instance:{long1._runtime_id}",
                                           f"runtime_cursor:{long1._runtime_id}")
        assert keys[long2._runtime_id] == (f"runtime_instance:{long2._runtime_id}",
                                           f"runtime_cursor:{long2._runtime_id}")
        assert len(set(keys.values())) == 4
        # A >32-char id can never be registered, so prefix truncation
        # collisions are unreachable through configured paths.
        toolong = FakeRuntime(str(root))
        toolong._runtime_id = "m" + "b" * 32
        with pytest.raises(BridgeError):
            RuntimeRegistry().register(toolong)
    finally:
        service.close()


def test_cursor_keys_fail_closed_for_invalid_identity(dual_env):
    from workspace_bridge.orchestration import AgentOrchestrator
    bad = FakeRuntime(str(dual_env["root"]))
    bad._runtime_id = "a:b"
    orch = AgentOrchestrator(dual_env["service"], bad, background=False)
    with pytest.raises(BridgeError) as exc:
        orch._cursor_setting_keys()
    assert exc.value.code == "invalid_arguments"


def test_invalid_ids_cannot_reach_service_construction(tmp_path):
    from workspace_bridge.cli import initialize
    parent = tmp_path / "projects"
    parent.mkdir()
    root = parent / "alpha"
    root.mkdir()
    state = tmp_path / "private-state"
    cfg = initialize(state, [str(parent)], 8765, 8766)
    bad = FakeRuntime(str(root))
    bad._runtime_id = "a:b"
    with pytest.raises(BridgeError) as exc:
        Service(state, cfg, runtimes={"a:b": bad},
                notifier=RecordingNotifier(), orchestrator_background=False)
    assert exc.value.code == "invalid_arguments"
    with pytest.raises(BridgeError):
        Service(state, cfg, runtime=bad,
                notifier=RecordingNotifier(), orchestrator_background=False)


def test_registry_mcp_grammar_stays_aligned():
    from pydantic import ValidationError
    from workspace_bridge.api import TOOLS
    from workspace_bridge.runtime import RUNTIME_ID_PATTERN, is_valid_runtime_id
    schema = TOOLS["list_agent_models"][0].model_json_schema()
    assert schema["properties"]["runtime"]["pattern"] == RUNTIME_ID_PATTERN
    TOOLS["list_agent_models"][0].model_validate(
        {"workspace_id": "ws_" + "0" * 24, "runtime": "a-b"})
    with pytest.raises(ValidationError):
        TOOLS["list_agent_models"][0].model_validate(
            {"workspace_id": "ws_" + "0" * 24, "runtime": "a:b"})
    assert is_valid_runtime_id("a-b") and not is_valid_runtime_id("a:b")


def test_historical_noncanonical_id_rows_unaffected(tmp_path):
    from workspace_bridge.cli import initialize
    parent = tmp_path / "projects"
    parent.mkdir()
    root = parent / "alpha"
    root.mkdir()
    state = tmp_path / "private-state"
    cfg = initialize(state, [str(parent)], 8765, 8766)
    aux = FakeRuntime(str(root))
    aux._runtime_id = "aux"
    service = Service(state, cfg, runtimes={"aux": aux},
                      notifier=RecordingNotifier(), orchestrator_background=False)
    try:
        ws_id = service.add_workspace("Alpha", str(root), [])["workspace"]["id"]
        token = service.manage_bridge("rotate_token")["token"]
        service.manage_workspace(ws_id, "enable")
        job = service.call(ws_id, token, "prepare_handoff",
                           Handoff.model_validate(
                               {"request_id": "hist-nc", "title": "Hist", "goal": "g",
                                "plan": "p", "acceptance": "a", "constraints": "c",
                                "context": "x", "context_hashes": {}}).model_dump())
        with service.lock, service.db:
            service.db.execute(
                "INSERT INTO agent_runs (id,workspace,runtime,job,request_id,request_hash,"
                "parent_run,session,model,state,error_code,error_message,result,notification,"
                "created,started,updated,finished,message_floor_ms,session_reused,transcript) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("run_hist_nc", ws_id, "a:b", job["id"], "hist-nc-req", "hash", None,
                 "ses_nc", "x/y", "completed", None, None, "{}", "{}",
                 "2026-09-21T00:00:00+00:00", None, "2026-09-21T00:00:00+00:00",
                 None, 0, 0, "[]"))
        ws = service.workspace(ws_id)
        # Persisted ids are never re-validated: list + persisted read work.
        assert [r["run_id"] for r in
                service.list_agent_runs(ws, runtime="a:b")["runs"]] == ["run_hist_nc"]
        view = service.read_agent_run(ws, "run_hist_nc")
        assert view["runtime"] == "a:b" and view["runtime_available"] is False
        with pytest.raises(BridgeError) as exc:
            service.cancel_agent_run(ws, "run_hist_nc")
        assert exc.value.code == "unknown_runtime"
    finally:
        service.close()
class _FakeHTTP:
    def __init__(self, payload, status=200):
        self._body = json.dumps(payload).encode()
        self.status = status

    def read(self, _=None):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_real_pi_transport_completion_uses_adapter_evidence(monkeypatch, tmp_path):
    """End-to-end over a real HttpPiRuntime: adapter-shaped completed ints
    drive completion; toolUse-shaped (completed null) messages do not."""
    from workspace_bridge.cli import initialize
    state_holder = {"status": "busy", "messages": [
        {"id": "m1", "role": "user", "created": 1758398400000,
         "completed": None, "text": "do it", "tools": [], "error": None},
    ]}

    def handler(record):
        url, method = record["url"], record["method"]
        base = url.split("?")[0]
        if method == "GET" and base.endswith("/health"):
            return _FakeHTTP({"ok": True, "pi_version": "0.86.1", "adapter_version": "0.2.0",
                              "locked": False, "instance": "pi-e2e", "status": "ok",
                              "capabilities": {"pending_snapshot": True,
                                               "permission_response": True,
                                               "execution_history": True}})
        if method == "GET" and "/executions" in base:
            return _FakeHTTP({"updates": [], "next": 0, "head": 0, "oldest": 1,
                              "audit_gap": False, "cursor_too_old": False})
        if method == "GET" and base.endswith("/models"):
            assert "directory=" in url
            return _FakeHTTP({"models": [{"provider": "pi", "id": "default",
                                           "name": "Pi Default"}]})
        if method == "POST" and base.endswith("/sessions"):
            return _FakeHTTP({"session": {"id": "pi_e2e_1",
                                          "directory": record["body"]["directory"],
                                          "title": record["body"]["title"]}})
        if method == "GET" and base.endswith("/sessions/pi_e2e_1/status"):
            return _FakeHTTP({"status": state_holder["status"]})
        if method == "POST" and base.endswith("/prompt-async"):
            return _FakeHTTP({"accepted": True})
        if method == "GET" and base.endswith("/sessions/pi_e2e_1/messages"):
            return _FakeHTTP({"messages": state_holder["messages"]})
        if method == "GET" and base.endswith("/sessions/pi_e2e_1"):
            import urllib.parse as _up
            query = _up.parse_qs(_up.urlsplit(url).query)
            return _FakeHTTP({"session": {"id": "pi_e2e_1",
                                          "directory": query.get("directory", [""])[0],
                                          "title": "t"}})
        if method == "POST" and base.endswith("/abort"):
            return _FakeHTTP({"ok": True})
        raise AssertionError(url)

    def urlopen(request, timeout=None):
        record = {"url": request.full_url, "method": request.get_method()}
        record["body"] = json.loads(request.data.decode()) if request.data else None
        return handler(record)

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    parent = tmp_path / "projects"
    parent.mkdir()
    root = parent / "alpha"
    root.mkdir()
    state = tmp_path / "private-state"
    cfg = initialize(state, [str(parent)], 8765, 8766)
    runtime = HttpPiRuntime("http://127.0.0.1:8780", "tok")
    service = Service(state, cfg, runtimes={"pi": runtime},
                      notifier=RecordingNotifier(), orchestrator_background=False)
    try:
        ws_id = service.add_workspace("Alpha", str(root), [])["workspace"]["id"]
        token = service.manage_bridge("rotate_token")["token"]
        service.manage_workspace(ws_id, "enable")
        service.manage_workspace(ws_id, "set_agent_enabled", agent_enabled=True)
        ws = service.workspace(ws_id)
        service.orchestrators["pi"].set_model_policy(["pi/default"], "pi/default", ws)
        job = service.call(ws_id, token, "prepare_handoff",
                           Handoff.model_validate(
                               {"request_id": "e2e-1", "title": "E2E", "goal": "g",
                                "plan": "p", "acceptance": "a", "constraints": "c",
                                "context": "x", "context_hashes": {}}).model_dump())
        run = service.call(ws_id, token, "start_agent_run",
                           {"runtime": "pi", "job_id": job["id"], "request_id": "e2e-run"})
        assert run["runtime"] == "pi"
        orch = service.orchestrators["pi"]
        # Busy with only user history: stays running.
        assert orch._poll_sweep(due_permission=True, due_completion=True,
                                due_question=True)["checked"] == 1
        # Intermediate toolUse-shaped turn (completed null) while idle:
        # still no completion evidence.
        state_holder["status"] = "idle"
        state_holder["messages"] = [
            {"id": "m1", "role": "user", "created": 1758398400000,
             "completed": None, "text": "do it", "tools": [], "error": None},
            {"id": "m2", "role": "assistant", "created": 1758398400001,
             "completed": None, "text": "", "tools": ["read"], "error": None},
        ]
        assert orch._poll_sweep(due_permission=True, due_completion=True,
                                due_question=True)["recovered"] == 0
        # Terminal stop-shaped turn (numeric completed, as the fixed native
        # adapter emits) completes the run with no events involved.
        state_holder["messages"].append(
            {"id": "m3", "role": "assistant", "created": 1758398400002,
             "completed": 1758398400002, "text": "Edited one file.", "tools": [],
             "error": None})
        assert orch._poll_sweep(due_permission=True, due_completion=True,
                                due_question=True)["recovered"] >= 1
        detail = service.call(ws_id, token, "read_agent_run", {"run_id": run["run_id"]})
        assert detail["state"] == "completed"
        assert "Edited one file." in detail["result"]["summary"]
    finally:
        service.close()
