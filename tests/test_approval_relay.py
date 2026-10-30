"""The approval relay: one answer per interaction, recorded with who decided."""
from __future__ import annotations

import sqlite3
import threading

import httpx
import pytest

from admin_helpers import admin_cookie
from workspace_bridge.api import make_admin
from workspace_bridge.security import BridgeError

from test_run_coordinator import ADAPTER_ID, modern_env, _finish_test_run, _persist_pending_interaction  # noqa: F401


def _start(service, ws_id, token, job, request_id):
    return service.call(ws_id, token, "start_run", {
        "adapter_id": ADAPTER_ID, "job_id": job["id"], "request_id": request_id})["run_id"]


def _interaction(service, ws_id, token, ref):
    run = service.call(ws_id, token, "read_run", {"run_id": ref["run_id"]})
    return next(item for item in run["interactions"] if item["id"] == ref["interaction_id"])


def _waiting(service, ws_id, token, job, native, request_id):
    run_id = _start(service, ws_id, token, job, request_id)
    interaction_id = _persist_pending_interaction(service, ws_id, token, native, run_id)
    item = _interaction(service, ws_id, token, {
        "run_id": run_id, "interaction_id": interaction_id})
    choices = {choice["semantic"]: choice["id"] for choice in item["details"]["choices"]}
    return run_id, interaction_id, choices


def test_an_answer_is_recorded_with_who_decided_and_cannot_be_repeated(modern_env):
    service, ws_id, token, job, native, rpc = modern_env
    run_id, interaction_id, choices = _waiting(service, ws_id, token, job, native, "relay-1")
    answer = {"run_id": run_id, "interaction_id": interaction_id,
              "choice_id": choices["approve"], "decided_by": "chatgpt",
              "reason": "Runs   the tests\nthe handoff names"}
    viewed = service.call(ws_id, token, "answer_interaction", answer)
    decision = viewed["interactions"][0]["decision"]
    assert decision["decided_by"] == "chatgpt"
    assert decision["reason"] == "Runs the tests the handoff names"
    assert decision["choice_id"] == choices["approve"] and decision["decided"]
    assert rpc.responses == [(99, {"decision": "accept"}, None)]

    with pytest.raises(BridgeError) as again:
        service.call(ws_id, token, "answer_interaction", {**answer, "decided_by": "owner"})
    assert again.value.code == "already_answered"
    assert rpc.responses == [(99, {"decision": "accept"}, None)]
    assert _interaction(service, ws_id, token, {
        "run_id": run_id, "interaction_id": interaction_id})["decision"] == decision

    log = service.interaction_decisions("chatgpt")["decisions"]
    assert [(item["run_id"], item["interaction_id"], item["decided_by"]) for item in log] == [
        (run_id, interaction_id, "chatgpt")]
    assert log[0]["workspace_id"] == ws_id and log[0]["adapter_id"] == ADAPTER_ID
    assert isinstance(log[0]["choice_label"], str) and log[0]["choice_label"]
    assert service.interaction_decisions("owner")["decisions"] == []
    _finish_test_run(service, native, ws_id, run_id)


@pytest.mark.parametrize("change", [
    {"decided_by": "manager"}, {"reason": ""}, {"reason": "   "}, {"reason": "x" * 501}])
def test_an_answer_needs_a_known_decider_and_a_bounded_reason(modern_env, change):
    service, ws_id, token, job, native, rpc = modern_env
    run_id, interaction_id, choices = _waiting(service, ws_id, token, job, native, "relay-2")
    with pytest.raises(BridgeError) as rejected:
        service.call(ws_id, token, "answer_interaction", {
            "run_id": run_id, "interaction_id": interaction_id,
            "choice_id": choices["deny"], "decided_by": "owner",
            "reason": "The user said no", **change})
    assert rejected.value.code == "invalid_arguments"
    assert rpc.responses == []
    assert _interaction(service, ws_id, token, {
        "run_id": run_id, "interaction_id": interaction_id})["state"] == "pending"
    _finish_test_run(service, native, ws_id, run_id)


@pytest.mark.parametrize(("code", "state"), [
    ("runtime_unavailable", "pending"), ("interaction_stale", "stale")])
def test_an_answer_the_plugin_refuses_is_not_recorded(modern_env, monkeypatch, code, state):
    service, ws_id, token, job, native, rpc = modern_env
    run_id, interaction_id, choices = _waiting(service, ws_id, token, job, native, "relay-3")
    answer = {"run_id": run_id, "interaction_id": interaction_id,
              "choice_id": choices["approve"], "decided_by": "chatgpt",
              "reason": "Runs the tests"}
    original = native.resolve

    def refuse(*_args, **_kwargs):
        raise BridgeError("Plugin refused", code)

    monkeypatch.setattr(native, "resolve", refuse)
    with pytest.raises(BridgeError) as refused:
        service.call(ws_id, token, "answer_interaction", answer)
    assert refused.value.code == code
    item = _interaction(service, ws_id, token, {
        "run_id": run_id, "interaction_id": interaction_id})
    assert item["state"] == state and "decision" not in item
    assert service.interaction_decisions()["decisions"] == []
    monkeypatch.setattr(native, "resolve", original)
    if state == "pending":
        service.call(ws_id, token, "answer_interaction", answer)
        assert rpc.responses == [(99, {"decision": "accept"}, None)]
    _finish_test_run(service, native, ws_id, run_id)


def test_runs_waiting_on_an_answer_are_listed_first(modern_env):
    service, ws_id, token, job, native, _ = modern_env
    waiting, _, _ = _waiting(service, ws_id, token, job, native, "relay-waiting")
    # A newer finished run, copied from the waiting one.
    with service.lock, service.db:
        columns = [row["name"] for row in service.db.execute("PRAGMA table_info(agent_runs)")]
        copied = ",".join({"id": "'run_newer'", "request_id": "'relay-newer'",
                           "phase": "'terminal'", "active_state": "NULL",
                           "outcome": "'succeeded'",
                           "created": "'2999-01-01T00:00:00+00:00'"}.get(name, name)
                          for name in columns)
        service.db.execute(f"INSERT INTO agent_runs({','.join(columns)}) "
                           f"SELECT {copied} FROM agent_runs WHERE id=?", (waiting,))
    listed = service.call(ws_id, token, "list_runs", {})["runs"]
    assert [item["run_id"] for item in listed] == [waiting, "run_newer"]
    _finish_test_run(service, native, ws_id, waiting)
    listed = service.call(ws_id, token, "list_runs", {})["runs"]
    assert [item["run_id"] for item in listed] == ["run_newer", waiting]


def _manager(service):
    app = make_admin(service)
    return app, {"Cookie": admin_cookie(app)}


def _notices(service, run_id, event_type="run_action"):
    with service.lock:
        return [dict(row) for row in service.db.execute(
            "SELECT action,subject_id FROM notification_events WHERE run_id=? AND event_type=? ORDER BY rowid",
            (run_id, event_type))]


@pytest.mark.asyncio
async def test_the_manager_reads_the_approval_log_and_answers_as_manager(modern_env):
    service, ws_id, token, job, native, rpc = modern_env
    run_id, interaction_id, choices = _waiting(service, ws_id, token, job, native, "relay-4")
    service.call(ws_id, token, "answer_interaction", {
        "run_id": run_id, "interaction_id": interaction_id,
        "choice_id": choices["deny"], "decided_by": "owner",
        "reason": "The user declined in the chat"})
    app, headers = _manager(service)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://127.0.0.1:8766") as client:
        assert (await client.get("/api/approvals")).status_code == 401
        owner = (await client.get("/api/approvals?decided_by=owner", headers=headers)).json()
        assert [item["reason"] for item in owner["decisions"]] == ["The user declined in the chat"]
        chatgpt = (await client.get("/api/approvals?decided_by=chatgpt", headers=headers)).json()
        assert chatgpt["decisions"] == []
        assert (await client.get("/api/approvals?decided_by=manager",
                                 headers=headers)).json()["decisions"] == []
        assert (await client.get("/api/approvals?decided_by=bot",
                                 headers=headers)).status_code == 400
    _finish_test_run(service, native, ws_id, run_id)


@pytest.mark.asyncio
async def test_an_answer_in_the_manager_is_recorded_as_manager_and_announced(modern_env):
    service, ws_id, token, job, native, rpc = modern_env
    run_id, interaction_id, choices = _waiting(service, ws_id, token, job, native, "relay-m1")
    app, headers = _manager(service)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://127.0.0.1:8766") as client:
        path = f"/api/runs/{run_id}/answer"
        assert (await client.post(path, json={})).status_code == 401
        both = await client.post(path, headers=headers, json={
            "interaction_id": interaction_id, "choice_id": choices["approve"], "answers": {}})
        assert both.status_code == 400
        answered = await client.post(path, headers=headers, json={
            "interaction_id": interaction_id, "choice_id": choices["approve"]})
        assert answered.status_code == 200
        again = await client.post(path, headers=headers, json={
            "interaction_id": interaction_id, "choice_id": choices["deny"]})
        assert again.status_code == 400 and "already" in again.json()["error"].lower()
        log = (await client.get("/api/approvals?decided_by=manager", headers=headers)).json()
    assert [(item["run_id"], item["decided_by"], item["reason"]) for item in log["decisions"]] == [
        (run_id, "manager", "Answered in the Manager")]
    assert rpc.responses == [(99, {"decision": "accept"}, None)]
    seen = _interaction(service, ws_id, token, {"run_id": run_id, "interaction_id": interaction_id})
    assert seen["decision"]["decided_by"] == "manager"
    assert _notices(service, run_id) == [
        {"action": "started", "subject_id": "start"},
        {"action": "answered", "subject_id": interaction_id}]
    with pytest.raises(BridgeError) as claimed:
        service.call(ws_id, token, "answer_interaction", {
            "run_id": run_id, "interaction_id": interaction_id,
            "choice_id": choices["approve"], "decided_by": "manager", "reason": "Claimed"})
    assert claimed.value.code == "invalid_arguments"
    _finish_test_run(service, native, ws_id, run_id)


def test_chatgpt_and_the_manager_answering_together_forward_one_answer(modern_env):
    service, ws_id, token, job, native, rpc = modern_env
    run_id, interaction_id, choices = _waiting(service, ws_id, token, job, native, "relay-race")
    barrier = threading.Barrier(2)
    outcomes: list[str] = []

    def chatgpt():
        barrier.wait()
        try:
            service.call(ws_id, token, "answer_interaction", {
                "run_id": run_id, "interaction_id": interaction_id,
                "choice_id": choices["approve"], "decided_by": "chatgpt", "reason": "Fine"})
            outcomes.append("chatgpt")
        except BridgeError as exc:
            outcomes.append(exc.code)

    def manager():
        barrier.wait()
        try:
            service.admin_answer_interaction(run_id, interaction_id, choice_id=choices["deny"])
            outcomes.append("manager")
        except BridgeError as exc:
            outcomes.append(exc.code)

    threads = [threading.Thread(target=chatgpt), threading.Thread(target=manager)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    winners = [item for item in outcomes if item in {"chatgpt", "manager"}]
    assert len(winners) == 1 and len(rpc.responses) == 1
    assert [item for item in outcomes if item not in winners] == ["already_answered"]
    decided = _interaction(service, ws_id, token, {
        "run_id": run_id, "interaction_id": interaction_id})["decision"]["decided_by"]
    assert decided == winners[0]
    _finish_test_run(service, native, ws_id, run_id)
