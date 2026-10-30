"""One slow Node call or native start only delays work on its own key."""
from __future__ import annotations

import threading

import pytest

from test_multi_workspace import add
from test_run_coordinator import ADAPTER_ID, modern_env, _finish_test_run  # noqa: F401
from workspace_bridge.keyed_locks import KeyedLocks, LockOrderError


def test_keyed_locks_are_reentrant_and_dropped_when_unused():
    locks = KeyedLocks()
    with locks.hold("a"):
        with locks.hold("a"):
            assert len(locks) == 1
        with locks.hold("b"):
            assert len(locks) == 2
    assert len(locks) == 0


def test_keyed_lock_refuses_to_wait_while_holding_the_database_lock():
    inner = threading.RLock()
    locks = KeyedLocks(inner=inner)
    with inner:
        with pytest.raises(LockOrderError):
            with locks.hold("a"):
                pass
    with locks.hold("a"), inner:
        pass


def _block_node(env, workspace_id, operations):
    """Hold the Node inside one workspace's operation until released."""
    node_service = env["node"]["service"]
    original = node_service._workspace_call
    entered, release = threading.Event(), threading.Event()

    def slow(operation, body):
        if operation in operations and body["workspace"]["id"] == workspace_id:
            entered.set()
            assert release.wait(10)
        return original(operation, body)

    node_service._workspace_call = slow
    return entered, release


def _background(target):
    outcome = {}

    def run():
        try:
            outcome["value"] = target()
        except Exception as exc:  # noqa: BLE001 - surfaced by the assertion below
            outcome["error"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, outcome


def test_slow_node_read_blocks_neither_other_workspaces_nor_admin(env):
    service, token, alpha = env["service"], env["token"], env["id"]
    beta, _ = add(env)
    entered, release = _block_node(env, alpha, {"read_file"})
    slow, outcome = _background(lambda: service.call(
        alpha, token, "read_file", {"path": "README.md", "start_line": 1, "max_lines": 5,
                                    "expected_sha256": None}))
    try:
        assert entered.wait(5)
        # The database lock is free while the Node works.
        assert service.lock.acquire(timeout=0.5)
        service.lock.release()
        other = service.call(beta, token, "read_file", {
            "path": "README.md", "start_line": 1, "max_lines": 5, "expected_sha256": None})
        assert other["lines"][0]["text"] == "# Beta"
        assert {row["id"] for row in service.list_workspaces()} == {alpha, beta}
        service.manage_workspace(beta, "set_write_scope", write_scope="workspace")
        written = service.call(beta, token, "apply_patch", {"changes": [{"op": "create", "path": "notes.md", "content": "hi\n"}], "message": "Add notes.md."})
        assert written["workspace_id"] == beta
        assert slow.is_alive()
    finally:
        release.set()
        slow.join(5)
    assert outcome["value"]["lines"][0]["text"] == "# Alpha"


def test_writes_to_one_workspace_queue_while_its_reads_continue(env):
    service, token, alpha = env["service"], env["token"], env["id"]
    service.manage_workspace(alpha, "set_write_scope", write_scope="workspace")
    entered, release = _block_node(env, alpha, {"apply_patch"})
    first, first_outcome = _background(lambda: service.call(
        alpha, token, "apply_patch", {"changes": [{"op": "create", "path": "a.md", "content": "a\n"}], "message": "Add a.md."}))
    second = None
    try:
        assert entered.wait(5)
        second, second_outcome = _background(lambda: service.call(
            alpha, token, "apply_patch", {"changes": [{"op": "create", "path": "b.md", "content": "b\n"}], "message": "Add b.md."}))
        second.join(0.3)
        assert second.is_alive(), "a second write to the same workspace did not wait"
        assert not (env["root"] / "b.md").exists()
        read = service.call(alpha, token, "read_file", {
            "path": "README.md", "start_line": 1, "max_lines": 5, "expected_sha256": None})
        assert read["lines"][0]["text"] == "# Alpha"
    finally:
        release.set()
        first.join(5)
        if second is not None:
            second.join(5)
    assert "error" not in first_outcome and "error" not in second_outcome
    assert (env["root"] / "a.md").read_text() == "a\n"
    assert (env["root"] / "b.md").read_text() == "b\n"


def test_slow_native_start_does_not_block_cancel_or_reads_of_other_runs(modern_env):
    service, ws_id, token, job, native, _ = modern_env
    first = service.call(ws_id, token, "start_run", {
        "adapter_id": ADAPTER_ID, "job_id": job["id"], "request_id": "first"})
    direct = service.run_coordinator.adapter(ADAPTER_ID)
    original = direct.start_run
    entered, release = threading.Event(), threading.Event()

    def slow_start(*args):
        entered.set()
        assert release.wait(10)
        return original(*args)

    direct.start_run = slow_start
    slow, outcome = _background(lambda: service.call(ws_id, token, "start_run", {
        "adapter_id": ADAPTER_ID, "job_id": job["id"], "request_id": "second"}))
    try:
        assert entered.wait(5)
        read = service.call(ws_id, token, "read_run", {"run_id": first["run_id"]})
        assert read["run_id"] == first["run_id"]
        listed = service.call(ws_id, token, "list_runs", {})
        assert first["run_id"] in {run["run_id"] for run in listed["runs"]}
        cancelled = service.call(ws_id, token, "cancel_run", {"run_id": first["run_id"]})
        assert cancelled["run_id"] == first["run_id"]
        assert slow.is_alive()
        native.rpc.status = "idle"  # the fake runtime tracks a single turn
    finally:
        direct.start_run = original
        release.set()
        slow.join(10)
    assert "error" not in outcome, outcome.get("error")
    _finish_test_run(service, native, ws_id, outcome["value"]["run_id"])


@pytest.mark.parametrize("name", ["send_run_input", "answer_interaction"])
def test_run_input_queues_on_its_own_run(modern_env, name):
    service, ws_id, *_ = modern_env
    assert service._tool_queue(ws_id, name, {"run_id": "run_x"}) is not None
    with service.run_coordinator.input_lock("run_x"):
        held = threading.Event()

        def other_run():
            with service.run_coordinator.input_lock("run_y"):
                held.set()

        thread = threading.Thread(target=other_run, daemon=True)
        thread.start()
        assert held.wait(2), "input on another run waited behind this run"
        thread.join(2)
