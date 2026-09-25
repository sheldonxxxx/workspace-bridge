"""Security-simplification milestone regressions.

Covers: terminal failed/cancelled/interrupted continuation when the native
conversation is still owned and idle; model change on continuation; benign
Node/adapter revision drift with an owned idle conversation; missing/unowned
conversations failing before any prompt; optional model governance (native
default, explicit live model, configured allowlist); route readiness and
default selection without a policy; the retired workspace-wide agent_enabled
gate; and direct-instruction auto-handoff idempotency/mutual exclusion.
"""
from __future__ import annotations

import json

import pytest

from workspace_bridge.api import AgentStartRun, Handoff
from workspace_bridge.cli import initialize
from workspace_bridge.codex_host_adapter import CodexHostAdapter
from workspace_bridge.security import BridgeError
from workspace_bridge.service import Service
from test_codex_host_adapter import FakeCodexRpc
from conftest import attach_test_node, create_test_adapter
from test_run_coordinator import ADAPTER_ID, DirectAdapter, _finish_test_run, modern_env  # noqa: F401


@pytest.fixture
def simplified_env(tmp_path, payload):
    """modern_env without a Bridge model policy and without agent_enabled."""
    parent = tmp_path / "projects"
    parent.mkdir()
    root = parent / "alpha"
    root.mkdir()
    state = tmp_path / "bridge-state"
    cfg = initialize(state, 8765, 8766)
    native_rpc = FakeCodexRpc()
    native = CodexHostAdapter(tmp_path / "codex-state", parent, rpc=native_rpc)
    service = Service(state, cfg, run_coordinator_background=False)
    node, node_record = attach_test_node(service, tmp_path / "node-state", parent)
    create_test_adapter(service, node_record["id"], {
        "name": "Local Codex", "runtime_type": "codex",
        "base_url": "http://127.0.0.1:8767", "token": "codex-test-secret"}, ADAPTER_ID)
    native_client = DirectAdapter(native)
    client_factory = service.adapter_registry.client
    service.adapter_registry.client = lambda adapter_id, **kwargs: (
        native_client if adapter_id == ADAPTER_ID
        else client_factory(adapter_id, **kwargs))
    ws_id = service.add_workspace("Alpha", str(root), [])["workspace"]["id"]
    token = service.manage_bridge("rotate_token")["token"]
    service.manage_workspace(ws_id, "enable")
    # No set_agent_enabled call: the workspace-wide legacy switch stays OFF
    # (column default 0) and must not gate runs.
    service.set_workspace_route(service.workspace(ws_id), ADAPTER_ID, True,
                                "workspace-write-reviewed")
    job = service.call(ws_id, token, "prepare_handoff",
                       Handoff.model_validate(payload).model_dump())
    yield service, ws_id, token, job, native, native_rpc
    service.close()
    native.close()
    node["stop"]()


@pytest.mark.parametrize("outcome", ["failed", "cancelled", "interrupted"])
def test_terminal_failed_cancelled_interrupted_continuation_with_owned_idle_conversation(
        modern_env, outcome):
    service, ws_id, token, job, native, _ = modern_env
    ws = service.workspace(ws_id)
    first = service.call(ws_id, token, "start_agent_run", {
        "adapter_id": ADAPTER_ID, "job_id": job["id"], "request_id": "term-" + outcome})
    _finish_test_run(service, native, ws_id, first["run_id"])
    with service.lock, service.db:
        service.db.execute(
            "UPDATE agent_runs SET outcome=?,result='',error='simulated',updated=? WHERE id=?",
            (outcome, service.run_coordinator._run_row(ws, first["run_id"])["updated"],
             first["run_id"]))
    second_job = service.call(ws_id, token, "prepare_handoff", Handoff.model_validate({
        "request_id": "term-second-job-" + outcome, "title": "Second", "goal": "g",
        "plan": "p", "acceptance": "a", "constraints": "c", "context": "c",
        "context_hashes": {}}).model_dump())
    second = service.call(ws_id, token, "start_agent_run", {
        "adapter_id": ADAPTER_ID, "job_id": second_job["id"],
        "request_id": "cont-" + outcome, "continue_from_run_id": first["run_id"]})
    assert second["conversation_id"] == first["conversation_id"]
    assert second["parent_run_id"] == first["run_id"]
    _finish_test_run(service, native, ws_id, second["run_id"])


def test_continuation_allows_model_change(modern_env, monkeypatch):
    service, ws_id, token, job, native, _ = modern_env
    ws = service.workspace(ws_id)
    client = service.run_coordinator.adapter(ADAPTER_ID)
    original_models = client.models
    extra = {"selector": "gpt-second", "displayName": "Second",
             "reasoningOptions": ["low"], "defaultReasoningEffort": None}

    def models_with_extra(workspace_id=None):
        return [*original_models(workspace_id), extra]

    monkeypatch.setattr(client, "models", models_with_extra)
    service.run_coordinator.set_model_policy(ADAPTER_ID, ["gpt-test", "gpt-second"],
                                             "gpt-test", ws)
    captured = []
    original_start = client.start_run

    def recording_start(conversation_id, body):
        captured.append(body)
        return original_start(conversation_id, body)

    monkeypatch.setattr(client, "start_run", recording_start)
    first = service.call(ws_id, token, "start_agent_run", {
        "adapter_id": ADAPTER_ID, "job_id": job["id"], "request_id": "model-first"})
    assert captured[-1]["model"] == "gpt-test"
    _finish_test_run(service, native, ws_id, first["run_id"])
    second = service.call(ws_id, token, "start_agent_run", {
        "adapter_id": ADAPTER_ID, "job_id": job["id"], "request_id": "model-second",
        "continue_from_run_id": first["run_id"], "model": "gpt-second"})
    assert second["conversation_id"] == first["conversation_id"]
    assert second["model"] == "gpt-second"
    assert captured[-1]["model"] == "gpt-second"


def test_missing_native_conversation_fails_closed_before_prompt(modern_env, monkeypatch):
    service, ws_id, token, job, native, _ = modern_env
    ws = service.workspace(ws_id)
    first = service.call(ws_id, token, "start_agent_run", {
        "adapter_id": ADAPTER_ID, "job_id": job["id"], "request_id": "missing-first"})
    _finish_test_run(service, native, ws_id, first["run_id"])
    conversation = service.run_coordinator._conversation(
        service.run_coordinator._run_row(ws, first["run_id"]))
    with native.lock, native.db:
        native.db.execute("DELETE FROM runs WHERE conversation=?",
                          (conversation["native_id"],))
        native.db.execute("DELETE FROM conversations WHERE id=?",
                          (conversation["native_id"],))
    second_job = service.call(ws_id, token, "prepare_handoff", Handoff.model_validate({
        "request_id": "missing-second-job", "title": "Second", "goal": "g",
        "plan": "p", "acceptance": "a", "constraints": "c", "context": "c",
        "context_hashes": {}}).model_dump())
    client = service.run_coordinator.adapter(ADAPTER_ID)
    calls = []
    monkeypatch.setattr(client, "start_run",
                        lambda *a, **k: calls.append("start") or (_ for _ in ()).throw(
                            AssertionError("native start must not run")))
    before = service.db.execute(
        "SELECT count(*) FROM agent_runs WHERE workspace=?", (ws_id,)).fetchone()[0]
    with pytest.raises(BridgeError):
        service.call(ws_id, token, "start_agent_run", {
            "adapter_id": ADAPTER_ID, "job_id": second_job["id"],
            "request_id": "missing-cont", "continue_from_run_id": first["run_id"]})
    assert calls == []
    after = service.db.execute(
        "SELECT count(*) FROM agent_runs WHERE workspace=?", (ws_id,)).fetchone()[0]
    assert after == before


def test_foreign_conversation_fails_closed_before_prompt(modern_env, monkeypatch):
    service, ws_id, token, job, native, _ = modern_env
    ws = service.workspace(ws_id)
    first = service.call(ws_id, token, "start_agent_run", {
        "adapter_id": ADAPTER_ID, "job_id": job["id"], "request_id": "foreign-first"})
    _finish_test_run(service, native, ws_id, first["run_id"])
    conversation = service.run_coordinator._conversation(
        service.run_coordinator._run_row(ws, first["run_id"]))
    with native.lock, native.db:
        native.db.execute("UPDATE conversations SET workspace='ws_foreign' WHERE id=?",
                          (conversation["native_id"],))
    client = service.run_coordinator.adapter(ADAPTER_ID)
    calls = []
    monkeypatch.setattr(client, "start_run",
                        lambda *a, **k: calls.append("start") or (_ for _ in ()).throw(
                            AssertionError("native start must not run")))
    with pytest.raises(BridgeError) as exc:
        service.call(ws_id, token, "start_agent_run", {
            "adapter_id": ADAPTER_ID, "job_id": job["id"],
            "request_id": "foreign-cont", "continue_from_run_id": first["run_id"]})
    assert calls == []
    assert exc.value.code in {"conversation_unavailable", "binding_mismatch"}


def test_no_policy_native_default_run_is_ready_and_uses_no_selector(simplified_env,
                                                                    monkeypatch):
    service, ws_id, token, job, native, _ = simplified_env
    ws = service.workspace(ws_id)
    policy = service.workspace_route_policy(ws)["routes"][ADAPTER_ID]
    assert policy["ready"] is True and policy["blockers"] == []
    assert policy["default_model"] is None
    # Without a policy the adapter can still be the workspace default.
    service.set_workspace_default(ws, ADAPTER_ID)
    assert service.workspace_route_policy(ws)["routes"][ADAPTER_ID]["is_default"] is True
    client = service.run_coordinator.adapter(ADAPTER_ID)
    captured = []
    original_start = client.start_run
    monkeypatch.setattr(client, "start_run",
                        lambda conversation_id, body: captured.append(body)
                        or original_start(conversation_id, body))
    started = service.call(ws_id, token, "start_agent_run", {
        "adapter_id": ADAPTER_ID, "job_id": job["id"], "request_id": "native-default"})
    assert captured[-1].get("model") is None
    assert started["model"] is None
    with service.lock:
        stored = service.db.execute("SELECT model FROM agent_runs WHERE id=?",
                                    (started["run_id"],)).fetchone()["model"]
    assert stored == ""  # native default, not a fake selector
    _finish_test_run(service, native, ws_id, started["run_id"])


def test_no_policy_explicit_live_model_and_unknown_rejected(simplified_env):
    service, ws_id, token, job, native, _ = simplified_env
    started = service.call(ws_id, token, "start_agent_run", {
        "adapter_id": ADAPTER_ID, "job_id": job["id"], "request_id": "explicit-model",
        "model": "gpt-test"})
    assert started["model"] == "gpt-test"
    with pytest.raises(BridgeError) as exc:
        service.call(ws_id, token, "start_agent_run", {
            "adapter_id": ADAPTER_ID, "job_id": job["id"], "request_id": "bad-model",
            "model": "not-in-catalog"})
    assert exc.value.code == "model_unavailable"


def test_models_presentation_marks_unrestricted_without_policy(simplified_env):
    service, ws_id, token, job, native, _ = simplified_env
    ws = service.workspace(ws_id)
    listing = service.list_agent_models(ws, ADAPTER_ID)
    assert listing["policy"]["configured"] is False
    assert listing["policy_restricted"] is False
    assert all(row["enabled"] is True and row["policy_enabled"] is None
               for row in listing["models"])
    # A configured policy keeps its exact allowlist/default semantics.
    service.run_coordinator.set_model_policy(ADAPTER_ID, ["gpt-test"], "gpt-test", ws)
    restricted = service.list_agent_models(ws, ADAPTER_ID)
    assert restricted["policy_restricted"] is True
    assert restricted["models"][0]["enabled"] is True
    assert restricted["models"][0]["policy_default"] is True


def test_configured_policy_still_enforces_allowlist(modern_env):
    service, ws_id, token, job, native, _ = modern_env
    with pytest.raises(BridgeError) as exc:
        service.call(ws_id, token, "start_agent_run", {
            "adapter_id": ADAPTER_ID, "job_id": job["id"], "request_id": "allowlist",
            "model": "gpt-not-enabled"})
    assert exc.value.code == "model_not_enabled"


def test_reasoning_default_only_for_configured_policy(simplified_env, monkeypatch):
    service, ws_id, token, job, native, _ = simplified_env
    ws = service.workspace(ws_id)
    client = service.run_coordinator.adapter(ADAPTER_ID)
    original_models = client.models
    monkeypatch.setattr(client, "models", lambda workspace_id=None: [
        {**row, "reasoningOptions": ["low"]} for row in original_models(workspace_id)])
    native_rpc_call = native.rpc.call

    def rpc_call_with_options(method, params, timeout=30):
        if method == "model/list":
            return {"data": [{"id": "gpt-test", "displayName": "Test model",
                              "isDefault": True,
                              "supportedReasoningEfforts": ["low"]}]}
        return native_rpc_call(method, params, timeout=timeout)

    monkeypatch.setattr(native.rpc, "call", rpc_call_with_options)
    captured = []
    original_start = client.start_run
    monkeypatch.setattr(client, "start_run",
                        lambda conversation_id, body: captured.append(body)
                        or original_start(conversation_id, body))
    started = service.call(ws_id, token, "start_agent_run", {
        "adapter_id": ADAPTER_ID, "job_id": job["id"], "request_id": "no-reasoning"})
    assert "reasoning" not in captured[-1]
    assert started["reasoning"] is None
    _finish_test_run(service, native, ws_id, started["run_id"])
    service.run_coordinator.set_model_policy(
        ADAPTER_ID, ["gpt-test"], "gpt-test", ws,
        reasoning_defaults={"gpt-test": "low"})
    second_job = service.call(ws_id, token, "prepare_handoff", Handoff.model_validate({
        "request_id": "reasoning-second-job", "title": "Second", "goal": "g",
        "plan": "p", "acceptance": "a", "constraints": "c", "context": "c",
        "context_hashes": {}}).model_dump())
    second = service.call(ws_id, token, "start_agent_run", {
        "adapter_id": ADAPTER_ID, "job_id": second_job["id"], "request_id": "with-reasoning"})
    assert captured[-1]["reasoning"] == "low"
    assert second["reasoning"] == "low"


def test_agent_enabled_false_does_not_block_runs(simplified_env):
    service, ws_id, token, job, native, _ = simplified_env
    service.manage_workspace(ws_id, "set_agent_enabled", agent_enabled=False)
    with service.lock:
        assert service.db.execute("SELECT agent_enabled FROM workspaces WHERE id=?",
                                  (ws_id,)).fetchone()["agent_enabled"] == 0
    started = service.call(ws_id, token, "start_agent_run", {
        "adapter_id": ADAPTER_ID, "job_id": job["id"], "request_id": "switch-off"})
    assert started["phase"] == "active"


def _direct_instruction(service, ws_id, token, request_id, instruction, **extra):
    return service.call(ws_id, token, "start_agent_run", {
        "adapter_id": ADAPTER_ID, "request_id": request_id,
        "instruction": instruction, **extra})


def test_direct_instruction_auto_handoff_is_auditable_and_idempotent(simplified_env):
    service, ws_id, token, job, native, _ = simplified_env
    with service.lock:
        jobs_before = service.db.execute("SELECT count(*) FROM jobs WHERE workspace=?",
                                         (ws_id,)).fetchone()[0]
    instruction = "Add a README section explaining the module layout."
    first = _direct_instruction(service, ws_id, token, "direct-1", instruction)
    assert first["phase"] == "active"
    with service.lock:
        jobs = service.db.execute("SELECT id,title FROM jobs WHERE workspace=?",
                                  (ws_id,)).fetchall()
        runs = service.db.execute("SELECT count(*) FROM agent_runs WHERE workspace=?",
                                  (ws_id,)).fetchone()[0]
    assert len(jobs) == jobs_before + 1 and runs == 1
    # The generated audit handoff carries the instruction as its goal.
    task = service.call(ws_id, token, "read_handoff", {
        "job_id": first["job_id"], "document": "TASK.md", "start_line": 1,
        "max_lines": 100})
    assert instruction in task["content"]
    assert "Do not commit" in task["content"]
    # Exact retry reuses the same audit handoff and the same run.
    retry = _direct_instruction(service, ws_id, token, "direct-1", instruction)
    assert retry["run_id"] == first["run_id"]
    assert retry["job_id"] == first["job_id"]
    assert retry["idempotent"] is True
    with service.lock:
        assert service.db.execute("SELECT count(*) FROM jobs WHERE workspace=?",
                                  (ws_id,)).fetchone()[0] == jobs_before + 1
    # A different instruction under the same run request_id is rejected by
    # the handoff content conflict before a duplicate row/artifact is
    # created: the derived audit identity is keyed by adapter_id +
    # request_id, not instruction content.
    with pytest.raises(BridgeError, match="different content"):
        _direct_instruction(service, ws_id, token, "direct-1", "Different instruction.")
    with service.lock:
        assert service.db.execute("SELECT count(*) FROM jobs WHERE workspace=?",
                                  (ws_id,)).fetchone()[0] == jobs_before + 1
    _finish_test_run(service, native, ws_id, first["run_id"])


def test_direct_instruction_mutual_exclusion_validation(simplified_env):
    service, ws_id, token, job, native, _ = simplified_env
    with pytest.raises(BridgeError) as both:
        _direct_instruction(service, ws_id, token, "both-1", "instruction",
                            job_id=job["id"])
    assert both.value.code == "invalid_arguments"
    with pytest.raises(BridgeError) as neither:
        service.call(ws_id, token, "start_agent_run", {
            "adapter_id": ADAPTER_ID, "request_id": "neither-1"})
    assert neither.value.code == "invalid_arguments"
    with pytest.raises(ValueError):
        AgentStartRun.model_validate({"adapter_id": ADAPTER_ID, "request_id": "x"})
    with pytest.raises(ValueError):
        AgentStartRun.model_validate({"adapter_id": ADAPTER_ID, "request_id": "x",
                                      "job_id": "job_" + "0" * 24,
                                      "instruction": "text"})


@pytest.mark.asyncio
async def test_direct_instruction_works_over_mcp_tool_surface(simplified_env):
    import httpx
    from workspace_bridge.api import make_mcp

    service, ws_id, token, job, native, _ = simplified_env
    app = make_mcp(service)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://127.0.0.1:8765",
                                 headers={"X-Bridge-Token": token,
                                          "Accept": "application/json"}) as client:
        response = await client.post("/mcp", json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "start_agent_run", "arguments": {
                "workspace_id": ws_id, "adapter_id": ADAPTER_ID,
                "request_id": "mcp-direct-1",
                "instruction": "Summarize the module layout."}}})
        assert response.status_code == 200
        result = response.json()["result"]
        assert not result["isError"], result
        value = json.loads(result["content"][0]["text"])
        assert value["phase"] == "active" and value["job_id"]
        # Both/neither is rejected at schema validation.
        invalid = await client.post("/mcp", json={
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "start_agent_run", "arguments": {
                "workspace_id": ws_id, "adapter_id": ADAPTER_ID,
                "request_id": "mcp-direct-2"}}})
        assert invalid.status_code == 200
        assert invalid.json()["error"]["code"] == -32602


def test_direct_instruction_identity_is_keyed_by_adapter_and_request():
    """The audit-handoff identity covers the run idempotency domain only."""
    adapter_a = "adapter_" + "0" * 24
    adapter_b = "adapter_" + "1" * 24
    same = Service.direct_instruction_request_id(adapter_a, "run-request-1")
    assert same == Service.direct_instruction_request_id(adapter_a, "run-request-1")
    # Different adapters in one workspace reuse the same run request_id
    # without colliding in handoff identity.
    assert Service.direct_instruction_request_id(adapter_b, "run-request-1") != same
    # The identity never depends on instruction content, and stays within
    # the handoff request_id pattern bounds.
    assert (Service.direct_instruction_request_id(adapter_a, "run-request-1")
            != Service.direct_instruction_request_id(adapter_a, "run-request-2"))
    for derived in (same, Service.direct_instruction_request_id(adapter_b, "run-request-1")):
        assert derived.startswith("direct-")
        assert 1 <= len(derived) <= 64
        assert all(c.isalnum() or c in "-_" for c in derived)
