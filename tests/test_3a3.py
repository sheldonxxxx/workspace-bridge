"""Milestone 3A3: runtime-neutral MCP/admin workflow with Pi model policy.

Focused coverage: neutral tool schemas/routing/annotations, legacy OpenCode
scoping, runtime-safe idempotency, global-per-runtime model policy, Pi
completion without events, and capability-safe Pi continuation. Scripted
fakes only: no Node, network, provider credentials, or real model.
"""
import json

import httpx
import pytest

from workspace_bridge.api import TOOLS, make_admin
from workspace_bridge.api import Handoff
from workspace_bridge.orchestration import neutral_run_summary
from workspace_bridge.runtime import MessageInfo, ModelInfo, RuntimeCapabilities
from workspace_bridge.security import BridgeError
from workspace_bridge.service import NEUTRAL_AGENT_TOOLS, Service

from runtime_fakes import FakeRuntime, RecordingNotifier

PI_MODELS = [
    ModelInfo(selector="pi/default", provider="pi", model="default", name="Pi Default"),
    ModelInfo(selector="anthropic/claude-opus", provider="anthropic", model="claude-opus",
              name="Claude Opus"),
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
    opencode = FakeRuntime(str(root))
    pi = FakePiRuntime(str(root))
    service = Service(state, cfg, runtimes={"opencode": opencode, "pi": pi},
                      notifier=RecordingNotifier(), orchestrator_background=False)
    ws_id = service.add_workspace("Alpha", str(root), [])["workspace"]["id"]
    token = service.manage_bridge("rotate_token")["token"]
    service.manage_workspace(ws_id, "enable")
    service.manage_workspace(ws_id, "set_agent_enabled", agent_enabled=True)
    service.orchestrators["opencode"].set_model_policy(
        ["anthropic/claude-sonnet", "glm/zai-glm-5.2"], "anthropic/claude-sonnet")
    yield {"service": service, "opencode": opencode, "pi": pi, "root": root,
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


# ------------------------------------------------- neutral schema/annotations
def test_neutral_tools_present_with_workspace_scope():
    assert NEUTRAL_AGENT_TOOLS == {"list_agent_models", "start_agent_run", "list_agent_runs",
                                   "read_agent_run", "read_agent_request",
                                   "respond_agent_permission", "cancel_agent_run",
                                   "list_agent_executions", "read_agent_execution"}
    assert NEUTRAL_AGENT_TOOLS <= set(TOOLS)
    for name in NEUTRAL_AGENT_TOOLS:
        schema = TOOLS[name][0].model_json_schema()
        assert schema["additionalProperties"] is False
        assert "workspace_id" in schema["required"]
    for name in ("list_agent_models", "start_agent_run"):
        assert "runtime" in TOOLS[name][0].model_json_schema()["required"]
    for name in ("read_agent_run", "read_agent_request", "respond_agent_permission",
                 "cancel_agent_run"):
        props = TOOLS[name][0].model_json_schema()["properties"]
        assert "runtime" not in props, name
    # list_agent_runs carries the optional cross-runtime filter.
    assert "runtime" in TOOLS["list_agent_runs"][0].model_json_schema()["properties"]
    assert TOOLS["start_agent_run"][2] is False and TOOLS["start_agent_run"][3] is True
    assert TOOLS["respond_agent_permission"][2] is False
    assert TOOLS["cancel_agent_run"][2] is False and TOOLS["cancel_agent_run"][3] is True
    assert TOOLS["list_agent_models"][2] is True and TOOLS["list_agent_runs"][2] is True
    assert TOOLS["read_agent_run"][2] is True and TOOLS["read_agent_request"][2] is True
    from workspace_bridge.api import DESTRUCTIVE_TOOLS, OPEN_WORLD_TOOLS
    assert {"start_agent_run", "respond_agent_permission"} <= DESTRUCTIVE_TOOLS
    assert NEUTRAL_AGENT_TOOLS <= OPEN_WORLD_TOOLS
    assert "cancel_agent_run" not in DESTRUCTIVE_TOOLS


def test_neutral_summary_helper_leaks_no_bodies():
    run = {"id": "run_x", "workspace": "ws_1", "runtime": "pi", "job": "job_1",
           "parent_run": None, "request_id": "r", "session_reused": 0, "state": "running",
           "model": "pi/default", "session": "ses_1", "created": "2026-09-21T00:00:00+00:00",
           "started": None, "updated": "2026-09-21T00:00:00+00:00", "finished": None,
           "notification": "{}"}
    view = neutral_run_summary(run)
    assert view["runtime"] == "pi" and view["run_id"] == "run_x"
    blob = json.dumps(view)
    assert "transcript" not in view and "result" not in blob


# ------------------------------------------------------- start persists owner
def test_neutral_starts_persist_runtime(dual_env):
    job = publish(dual_env, "persist-1")
    started_oc = call(dual_env, "start_agent_run", runtime="opencode",
                      job_id=job["id"], request_id="persist-oc")
    assert started_oc["runtime"] == "opencode"
    ws = dual_env["service"].workspace(dual_env["id"])
    dual_env["service"].orchestrators["pi"].set_model_policy(
        ["pi/default"], "pi/default", ws)
    started_pi = call(dual_env, "start_agent_run", runtime="pi",
                      job_id=job["id"], request_id="persist-pi")
    assert started_pi["runtime"] == "pi"
    assert started_pi["session_id"].startswith("ses_")
    db = dual_env["service"].db
    assert db.execute("SELECT runtime FROM agent_runs WHERE id=?",
                      (started_oc["run_id"],)).fetchone()["runtime"] == "opencode"
    assert db.execute("SELECT runtime FROM agent_runs WHERE id=?",
                      (started_pi["run_id"],)).fetchone()["runtime"] == "pi"


def test_unknown_runtime_fails_before_backend(dual_env):
    job = publish(dual_env, "unknown-1")
    oc_calls = len(dual_env["opencode"].model_calls)
    pi_calls = len(dual_env["pi"].model_calls)
    for tool, args in (
            ("list_agent_models", {"runtime": "nope"}),
            ("start_agent_run", {"runtime": "nope", "job_id": job["id"],
                                 "request_id": "unknown-run"}),
            ("list_agent_runs", {"runtime": "nope"})):
        with pytest.raises(BridgeError) as exc:
            call(dual_env, tool, **args)
        assert exc.value.code == "unknown_runtime"
    assert len(dual_env["opencode"].model_calls) == oc_calls
    assert len(dual_env["pi"].model_calls) == pi_calls


# ------------------------------------------------------- legacy stays scoped
def test_legacy_paths_reject_and_filter_pi_rows(dual_env):
    job = publish(dual_env, "legacy-1")
    ws = dual_env["service"].workspace(dual_env["id"])
    dual_env["service"].orchestrators["pi"].set_model_policy(
        ["pi/default"], "pi/default", ws)
    pi_run = call(dual_env, "start_agent_run", runtime="pi",
                  job_id=job["id"], request_id="legacy-pi")
    service = dual_env["service"]
    # Legacy lists never surface Pi rows.
    assert call(dual_env, "list_opencode_runs")["runs"] == []
    assert service.orchestrator.list_all_runs()["runs"] == []
    # Legacy per-run paths fail closed on Pi rows.
    for fn in (lambda: service.orchestrator.read_run(ws, pi_run["run_id"]),
               lambda: service.orchestrator.read_request(ws, pi_run["run_id"], "x"),
               lambda: service.orchestrator.respond_permission(
                   ws, pi_run["run_id"], "x", "once"),
               lambda: service.orchestrator.cancel_run(ws, pi_run["run_id"])):
        with pytest.raises(BridgeError) as exc:
            fn()
        assert exc.value.code == "runtime_mismatch"
    with pytest.raises(BridgeError) as exc:
        call(dual_env, "read_opencode_run", run_id=pi_run["run_id"])
    assert exc.value.code == "runtime_mismatch"


def test_neutral_list_crosses_runtimes_with_filter(dual_env):
    job = publish(dual_env, "cross-1")
    ws = dual_env["service"].workspace(dual_env["id"])
    dual_env["service"].orchestrators["pi"].set_model_policy(
        ["pi/default"], "pi/default", ws)
    oc_run = call(dual_env, "start_agent_run", runtime="opencode",
                  job_id=job["id"], request_id="cross-oc")
    pi_run = call(dual_env, "start_agent_run", runtime="pi",
                  job_id=job["id"], request_id="cross-pi")
    # Keep the Pi run active: the scripted default history would otherwise
    # complete it on the next read_reconcile.
    dual_env["pi"].messages_script = []
    full = call(dual_env, "list_agent_runs")
    assert {r["run_id"] for r in full["runs"]} == {oc_run["run_id"], pi_run["run_id"]}
    assert {r["runtime"] for r in full["runs"]} == {"opencode", "pi"}
    assert "transcript" not in json.dumps(full)
    only_oc = call(dual_env, "list_agent_runs", runtime="opencode")
    assert [r["run_id"] for r in only_oc["runs"]] == [oc_run["run_id"]]
    assert only_oc["runtime"] == "opencode"
    only_pi = call(dual_env, "list_agent_runs", runtime="pi")
    assert [r["run_id"] for r in only_pi["runs"]] == [pi_run["run_id"]]
    # Neutral per-run routing serves both owners.
    assert call(dual_env, "read_agent_run", run_id=oc_run["run_id"])["runtime"] == "opencode"
    assert call(dual_env, "read_agent_run", run_id=pi_run["run_id"])["runtime"] == "pi"
    # Neutral cancel reaches the owning backend.
    stopped = call(dual_env, "cancel_agent_run", run_id=pi_run["run_id"])
    assert stopped["state"] == "cancelled"
    assert dual_env["pi"].abort_calls


# ------------------------------------------------------- runtime-safe replay
def test_idempotency_never_replays_across_runtimes(dual_env):
    job = publish(dual_env, "idem-1")
    ws = dual_env["service"].workspace(dual_env["id"])
    dual_env["service"].orchestrators["pi"].set_model_policy(
        ["pi/default"], "pi/default", ws)
    first = call(dual_env, "start_agent_run", runtime="opencode",
                 job_id=job["id"], request_id="shared-req")
    # Same workspace request_id under the other runtime fails closed even
    # though job/model/hash would otherwise match.
    with pytest.raises(BridgeError) as exc:
        call(dual_env, "start_agent_run", runtime="pi",
             job_id=job["id"], request_id="shared-req")
    assert exc.value.code == "runtime_mismatch"
    assert len(dual_env["pi"].sessions) == 0
    # And in the other direction.
    second = call(dual_env, "start_agent_run", runtime="pi",
                  job_id=job["id"], request_id="shared-pi")
    with pytest.raises(BridgeError) as exc:
        call(dual_env, "start_agent_run", runtime="opencode",
             job_id=job["id"], request_id="shared-pi")
    assert exc.value.code == "runtime_mismatch"
    # Same-runtime exact retry still replays.
    replay = call(dual_env, "start_agent_run", runtime="opencode",
                  job_id=job["id"], request_id="shared-req")
    assert replay["run_id"] == first["run_id"] and replay["idempotent_replay"] is True
    assert second["idempotent_replay"] is False


# ------------------------------------------------------- Pi policy is global
def test_pi_policy_isolated_workspace_validated_and_global(dual_env):
    service = dual_env["service"]
    ws = service.workspace(dual_env["id"])
    pi_orch = service.orchestrators["pi"]
    assert pi_orch.model_policy_status()["configured"] is False
    # Saving without a workspace context fails closed.
    with pytest.raises(BridgeError) as exc:
        pi_orch.set_model_policy(["pi/default"], "pi/default")
    assert exc.value.code == "invalid_arguments"
    # Unknown selectors fail against the workspace discovery.
    with pytest.raises(BridgeError) as exc:
        pi_orch.set_model_policy(["nope/missing"], "nope/missing", ws)
    assert exc.value.code == "model_unavailable"
    status = pi_orch.set_model_policy(["pi/default", "anthropic/claude-opus"],
                                      "pi/default", ws)
    assert status["configured"] is True and status["default"] == "pi/default"
    assert service.db.execute("SELECT value FROM settings WHERE key='model_policy:pi'").fetchone()
    # OpenCode policy is untouched.
    assert service.orchestrator.model_policy_status()["default"] == "anthropic/claude-sonnet"
    # Discovery metadata exposes runtime scopes.
    models = call(dual_env, "list_agent_models", runtime="pi")
    assert models["runtime"] == "pi"
    assert models["discovery_scope"] == "workspace"
    assert models["policy_scope"] == "runtime_global"
    assert models["policy"]["default"] == "pi/default"
    assert models["workspace_id"] == dual_env["id"]
    oc_models = call(dual_env, "list_agent_models", runtime="opencode")
    assert oc_models["runtime"] == "opencode"
    assert oc_models["discovery_scope"] == "global"
    assert oc_models["policy_scope"] == "runtime_global"


def test_pi_run_revalidates_policy_per_workspace(dual_env):
    service = dual_env["service"]
    ws = service.workspace(dual_env["id"])
    service.orchestrators["pi"].set_model_policy(["pi/default"], "pi/default", ws)
    job = publish(dual_env, "reval-1")
    # The saved default vanishes from this workspace's discovery: no silent
    # substitution, an explicit model_unavailable/model_disabled failure.
    dual_env["pi"].models = []
    with pytest.raises(BridgeError) as exc:
        call(dual_env, "start_agent_run", runtime="pi",
             job_id=job["id"], request_id="reval-run")
    assert exc.value.code in ("model_unavailable", "model_disabled")
    assert dual_env["pi"].sessions == []


# ------------------------------------------------------- Pi completion w/o events
def test_pi_completion_via_polling_with_no_events(dual_env):
    service = dual_env["service"]
    ws = service.workspace(dual_env["id"])
    service.orchestrators["pi"].set_model_policy(["pi/default"], "pi/default", ws)
    job = publish(dual_env, "complete-1")
    run = call(dual_env, "start_agent_run", runtime="pi",
               job_id=job["id"], request_id="complete-run")
    pi = dual_env["pi"]
    session = run["session_id"]
    # Busy session with no finished work yet: the background sweep recovers nothing.
    pi.set_session_status("busy", session)
    pi.messages_script = [MessageInfo(id="m1", role="user", created=10, text="do it")]
    sweep = service.orchestrators["pi"]._poll_sweep(
        due_permission=True, due_completion=True, due_question=True)
    assert sweep["checked"] == 1 and sweep["recovered"] == 0
    assert service.db.execute("SELECT state FROM agent_runs WHERE id=?",
                              (run["run_id"],)).fetchone()["state"] in ("starting", "running")
    # Status moves busy -> idle and a completed assistant response lands.
    # No RuntimeEvent is ever emitted; the event stream is never polled.
    pi.set_session_status("idle", session)
    pi.messages_script = [
        MessageInfo(id="m1", role="user", created=10, text="do it"),
        MessageInfo(id="m2", role="assistant", created=11, completed=12,
                    text="Done: edited one file.", tools=("read",)),
    ]
    sweep = service.orchestrators["pi"]._poll_sweep(
        due_permission=True, due_completion=True, due_question=True)
    assert sweep["recovered"] >= 1
    assert pi.poll_calls == []
    detail = call(dual_env, "read_agent_run", run_id=run["run_id"])
    assert detail["state"] == "completed"
    assert detail["result"]["has_final_response"] is True
    assert "Done: edited one file." in detail["result"]["summary"]
    assert detail["result"]["message_count"] == 2


def test_pi_resync_is_capability_gated_without_noise(dual_env):
    service = dual_env["service"]
    ws = service.workspace(dual_env["id"])
    service.orchestrators["pi"].set_model_policy(["pi/default"], "pi/default", ws)
    job = publish(dual_env, "quiet-1")
    run = call(dual_env, "start_agent_run", runtime="pi",
               job_id=job["id"], request_id="quiet-run")
    pi = dual_env["pi"]
    pi.messages_script = []
    detail = call(dual_env, "read_agent_run", run_id=run["run_id"])
    assert detail["state"] in ("starting", "running")
    assert pi.list_pending_calls == [] and pi.list_questions_calls == []
    assert detail["permission_sync"]["reason"] == "capability_unsupported"
    # Pi has no permission interactions: answering fails closed, never remote.
    with pytest.raises(BridgeError) as exc:
        call(dual_env, "respond_agent_permission", run_id=run["run_id"],
             request_id="anything", decision="once")
    assert exc.value.code in ("runtime_unsupported", "not_found")
    assert pi.respond_calls == []


# ------------------------------------------------------- Pi continuation
def test_pi_continuation_same_runtime_and_cross_runtime_refused(dual_env):
    service = dual_env["service"]
    ws = service.workspace(dual_env["id"])
    service.orchestrators["pi"].set_model_policy(["pi/default"], "pi/default", ws)
    pi = dual_env["pi"]
    first = call(dual_env, "start_agent_run", runtime="pi",
                 job_id=publish(dual_env, "cont-1")["id"], request_id="cont-first")
    pi.messages_script = [
        MessageInfo(id="m1", role="user", created=10, text="do it"),
        MessageInfo(id="m2", role="assistant", created=11, completed=12,
                    text="Done.", tools=("read",)),
    ]
    service.orchestrators["pi"]._poll_sweep(
        due_permission=False, due_completion=True, due_question=False)
    assert call(dual_env, "read_agent_run", run_id=first["run_id"])["state"] == "completed"
    follow = publish(dual_env, "cont-2", title="Follow-up")
    second = call(dual_env, "start_agent_run", runtime="pi", job_id=follow["id"],
                  request_id="cont-second", continue_from_run_id=first["run_id"])
    assert second["session_reused"] is True
    assert second["session_id"] == first["session_id"]
    assert second["model"] == first["model"]
    # Cross-runtime continuation fails closed in both directions.
    other = publish(dual_env, "cont-3", title="Other")
    with pytest.raises(BridgeError) as exc:
        call(dual_env, "start_agent_run", runtime="opencode", job_id=other["id"],
             request_id="cont-x1", continue_from_run_id=first["run_id"])
    assert exc.value.code == "continuation_unavailable"
    oc_first = call(dual_env, "start_agent_run", runtime="opencode",
                    job_id=publish(dual_env, "cont-4")["id"], request_id="cont-oc")
    assert call(dual_env, "read_agent_run", run_id=oc_first["run_id"])["state"] == "completed"
    with pytest.raises(BridgeError) as exc:
        call(dual_env, "start_agent_run", runtime="pi", job_id=other["id"],
             request_id="cont-x2", continue_from_run_id=oc_first["run_id"])
    assert exc.value.code == "continuation_unavailable"


def test_unconfigured_runtime_rows_stay_listable_but_inoperable(tmp_path):
    from workspace_bridge.cli import initialize
    parent = tmp_path / "projects"
    parent.mkdir()
    root = parent / "alpha"
    root.mkdir()
    state = tmp_path / "private-state"
    cfg = initialize(state, [str(parent)], 8765, 8766)
    service = Service(state, cfg, runtimes={"opencode": FakeRuntime(str(root))},
                      notifier=RecordingNotifier(), orchestrator_background=False)
    try:
        ws_id = service.add_workspace("Alpha", str(root), [])["workspace"]["id"]
        token = service.manage_bridge("rotate_token")["token"]
        service.manage_workspace(ws_id, "enable")
        job = service.call(ws_id, token, "prepare_handoff",
                           Handoff.model_validate(
                               {"request_id": "hist-1", "title": "Hist", "goal": "g",
                                "plan": "p", "acceptance": "a", "constraints": "c",
                                "context": "x", "context_hashes": {}}).model_dump())
        with service.lock, service.db:
            service.db.execute(
                "INSERT INTO agent_runs (id,workspace,runtime,job,request_id,request_hash,"
                "parent_run,session,model,state,error_code,error_message,result,notification,"
                "created,started,updated,finished,message_floor_ms,session_reused,transcript) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("run_hist_pi", ws_id, "pi", job["id"], "hist-req", "hash", None,
                 "ses_hist", "pi/default", "completed", None, None, "{}", "{}",
                 "2026-09-21T00:00:00+00:00", None, "2026-09-21T00:00:00+00:00",
                 None, 0, 0, "[]"))
        ws = service.workspace(ws_id)
        listed = service.list_agent_runs(ws, runtime="pi")
        assert [r["run_id"] for r in listed["runs"]] == ["run_hist_pi"]
        with pytest.raises(BridgeError) as exc:
            service.list_agent_runs(ws, runtime="ghost")
        assert exc.value.code == "unknown_runtime"
        # Reads stay available from persisted state only, with no backend.
        runtime = service.orchestrators["opencode"].runtime
        calls_before = (len(runtime.get_session_calls), len(runtime.messages_calls),
                        len(runtime.model_calls))
        view = service.read_agent_run(ws, "run_hist_pi")
        assert view["run_id"] == "run_hist_pi" and view["runtime"] == "pi"
        assert view["state"] == "completed"
        assert view["runtime_available"] is False
        assert view["agent_evidence"] == "unverified"
        assert (len(runtime.get_session_calls), len(runtime.messages_calls),
                len(runtime.model_calls)) == calls_before
        # Operations needing the missing backend fail explicitly, no reroute.
        with pytest.raises(BridgeError) as exc:
            service.cancel_agent_run(ws, "run_hist_pi")
        assert exc.value.code == "unknown_runtime"
        with pytest.raises(BridgeError) as exc:
            service.respond_agent_permission(ws, "run_hist_pi", "x", "once")
        assert exc.value.code == "unknown_runtime"
    finally:
        service.close()


# ------------------------------------------------------- admin + UI surface
@pytest.mark.asyncio
async def test_admin_runtime_routes_and_compat(dual_env):
    service = dual_env["service"]
    token = (dual_env["state"] / "admin-token").read_text().strip()
    ws = service.workspace(dual_env["id"])
    service.orchestrators["pi"].set_model_policy(["pi/default"], "pi/default", ws)
    async with httpx.AsyncClient(
            transport=httpx.ASGITransport(
                app=make_admin(service, service.config["admin_token_hash"])),
            base_url="http://127.0.0.1:8766",
            headers={"Authorization": "Bearer " + token}) as client:
        # Legacy OpenCode routes are unchanged.
        assert (await client.get("/api/opencode/models")).json()["scope"] == "global"
        assert (await client.get("/api/settings")).json()["model_policy"]["configured"] is True
        saved = (await client.post("/api/settings",
                                   json={"enabled": ["glm/zai-glm-5.2"],
                                         "default": "glm/zai-glm-5.2"})).json()
        assert saved["default"] == "glm/zai-glm-5.2"
        # Restore the dual fixture default for later tests (function scope).
        await client.post("/api/settings",
                          json={"enabled": ["anthropic/claude-sonnet", "glm/zai-glm-5.2"],
                                "default": "anthropic/claude-sonnet"})
        # Neutral runtime discovery: Pi requires an enabled workspace.
        assert (await client.get("/api/runtimes/pi/models")).status_code == 400
        models = (await client.get("/api/runtimes/pi/models",
                                   params={"workspace_id": dual_env["id"]})).json()
        assert models["runtime"] == "pi" and models["discovery_scope"] == "workspace"
        assert models["policy_scope"] == "runtime_global"
        assert models["workspace_id"] == dual_env["id"]
        oc_models = (await client.get("/api/runtimes/opencode/models")).json()
        assert oc_models["runtime"] == "opencode" and oc_models["discovery_scope"] == "global"
        assert (await client.get("/api/runtimes/ghost/models")).status_code == 400
        # Neutral runtime policy: Pi requires workspace_id on POST.
        assert (await client.post("/api/runtimes/pi/model-policy",
                                  json={"enabled": ["pi/default"],
                                        "default": "pi/default"})).status_code == 400
        policy = (await client.get("/api/runtimes/pi/model-policy")).json()
        assert policy["runtime"] == "pi" and policy["policy_scope"] == "runtime_global"
        assert policy["configured"] is True
        posted = (await client.post("/api/runtimes/pi/model-policy",
                                    json={"enabled": ["pi/default"],
                                          "default": "pi/default",
                                          "workspace_id": dual_env["id"]})).json()
        assert posted["default"] == "pi/default"
        assert (await client.get("/api/status")).json()["runtime_policies"]["pi"]["configured"] is True
        # Sessions: neutral crosses runtimes, opencode route stays scoped.
        job = publish(dual_env, "admin-sess")
        oc_run = service.call(dual_env["id"], dual_env["token"], "start_agent_run",
                              {"runtime": "opencode", "job_id": job["id"],
                               "request_id": "admin-oc"})
        pi_run = service.call(dual_env["id"], dual_env["token"], "start_agent_run",
                              {"runtime": "pi", "job_id": job["id"],
                               "request_id": "admin-pi"})
        neutral = (await client.get("/api/sessions")).json()
        assert {r["run_id"] for r in neutral["runs"]} >= {oc_run["run_id"], pi_run["run_id"]}
        assert {r["runtime"] for r in neutral["runs"]} >= {"opencode", "pi"}
        scoped = (await client.get("/api/opencode/sessions")).json()
        assert {r["run_id"] for r in scoped["runs"]} >= {oc_run["run_id"]}
        assert all(r["runtime"] == "opencode" for r in scoped["runs"])
        filtered = (await client.get("/api/sessions", params={"runtime": "pi"})).json()
        assert {r["run_id"] for r in filtered["runs"]} >= {pi_run["run_id"]}
        assert all(r["runtime"] == "pi" for r in filtered["runs"])
        assert (await client.get("/api/sessions", params={"runtime": "ghost"})).status_code == 400


@pytest.mark.asyncio
async def test_mcp_neutral_roundtrip_and_unknown_runtime(dual_env):
    from workspace_bridge.api import make_mcp

    async def mcp_call(name, arguments, call_id=1):
        async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=make_mcp(dual_env["service"])),
                base_url="http://127.0.0.1:8765") as client:
            return await client.post(
                "/mcp", headers={"X-Bridge-Token": dual_env["token"],
                                 "Accept": "application/json"},
                json={"jsonrpc": "2.0", "id": call_id, "method": "tools/call",
                      "params": {"name": name,
                                 "arguments": {"workspace_id": dual_env["id"], **arguments}}})

    def value(response):
        assert response.status_code == 200, response.text
        result = response.json()["result"]
        assert not result["isError"], result
        return json.loads(result["content"][0]["text"])

    job = publish(dual_env, "mcp-3a3")
    models = value(await mcp_call("list_agent_models", {"runtime": "opencode"}))
    assert models["workspace_id"] == dual_env["id"] and models["runtime"] == "opencode"
    run = value(await mcp_call("start_agent_run",
                               {"runtime": "opencode", "job_id": job["id"],
                                "request_id": "mcp-3a3-run"}))
    assert run["runtime"] == "opencode" and run["session_id"]
    detail = value(await mcp_call("read_agent_run", {"run_id": run["run_id"]}))
    assert detail["run_id"] == run["run_id"]
    listed = value(await mcp_call("list_agent_runs", {}))
    assert run["run_id"] in {r["run_id"] for r in listed["runs"]}
    bad = await mcp_call("list_agent_models", {"runtime": "ghost"})
    assert bad.json()["result"]["isError"] is True
    assert "unknown_runtime" in bad.json()["result"]["content"][0]["text"]
    bad_start = await mcp_call("start_agent_run",
                               {"runtime": "ghost", "job_id": job["id"],
                                "request_id": "mcp-ghost"})
    assert bad_start.json()["result"]["isError"] is True
    # Per-run tools accept no caller runtime override.
    bad_schema = await mcp_call("read_agent_run",
                                {"run_id": run["run_id"], "runtime": "pi"}, call_id=2)
    assert bad_schema.status_code == 200
    assert bad_schema.json().get("error", {}).get("code") == -32602


def test_ui_has_pi_card_workspace_picker_and_neutral_labels():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    js = (root / "workspace_bridge" / "static" / "app.js").read_text()
    html = (root / "workspace_bridge" / "static" / "index.html").read_text()
    assert "Pi runtime" in html and 'id="pi-policy-status"' in html
    assert 'id="open-pi-models"' in html and 'id="discovery-workspace"' in html
    assert "Reused OpenCode session" not in js
    assert "Interrupt only this OpenCode session" not in js
    assert "OpenCode default model" not in js
    assert "innerHTML" not in js


def test_skill_prefers_neutral_workflow_and_pins_version():
    from workspace_bridge.embedded_skill import SKILL_VERSION, read_project_lead_skill
    assert SKILL_VERSION == "2.1.0"
    content = read_project_lead_skill()["content"]
    for fragment in ("list_agent_models", "start_agent_run", "read_agent_run",
                     "respond_agent_permission", "silent", "OpenCode",
                     "ask first", "fresh session", "web-admin configured",
                     "immutable policy snapshot", "bash",
                     "execution_audit", "list_agent_executions"):
        assert fragment in content, fragment


def test_compose_docs_state_orbstack_guidance_without_absolute_claim():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    compose = (root / "compose.yaml").read_text()
    assert "host.docker.internal:8780" in compose
    assert "OrbStack 29.4.0" in compose
    assert "Docker Desktop behavior may" in compose
    assert "host.docker.internal cannot reach it" not in compose
    env_example = (root / ".env.example").read_text()
    assert "OrbStack 29.4.0" in env_example
    assert "0.0.0.0/LAN" in env_example
