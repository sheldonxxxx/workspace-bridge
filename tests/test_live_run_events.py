"""Adapter hints accelerate authoritative snapshots without becoming state."""
from __future__ import annotations

import copy
import threading
import time
from types import SimpleNamespace

import pytest

from workspace_bridge.adapter_events import AdapterEventReader, validate_event_page
from workspace_bridge.runtime import RuntimeRejected, RuntimeUnavailable
from workspace_bridge.security import BridgeError
from workspace_bridge.wbrp import Descriptor
from test_run_coordinator import ADAPTER_ID, modern_env, _finish_test_run


def hint(cursor=1, *, instance="instance-1", conversation="conversation-1", run="native-1"):
    return {"instanceId": instance, "cursor": cursor, "type": "run.completed",
            "conversationId": conversation, "runId": run}


def page(events=None, *, cursor=1, instance="instance-1"):
    return {"instanceId": instance, "cursor": cursor,
            "events": [hint(instance=instance)] if events is None else events}


class ScriptedAdapter:
    def __init__(self, pages):
        self.pages = list(pages)
        self.instance = "instance-1"
        self.supported = True
        self.afters = []

    def descriptor(self):
        return SimpleNamespace(instance_id=self.instance,
                               supports=lambda feature: self.supported and feature == "events")

    def events(self, *, after, wait_ms):
        assert wait_ms == 1000
        self.afters.append(after)
        value = self.pages.pop(0)
        if isinstance(value, Exception):
            raise value
        if callable(value):
            value = value()
        return value


def reader_for(adapter, *, rows=None):
    refreshed = []
    rows = rows or [{"id": "bridge-1", "native_id": "native-1",
                     "native_conversation": "conversation-1"}]
    return AdapterEventReader(lambda: adapter, lambda: rows, refreshed.append), refreshed


@pytest.mark.parametrize("cursor", [True, False, -1, 1.5, "1", 9007199254740992])
def test_invalid_envelope_cursors_are_rejected(cursor):
    with pytest.raises(RuntimeUnavailable):
        validate_event_page(page(cursor=cursor), 0)


@pytest.mark.parametrize("update", [
    {"cursor": True}, {"cursor": 0}, {"cursor": 2}, {"instanceId": "another-instance"},
    {"conversationId": ""}, {"conversationId": "x" * 201},
    {"runId": False}, {"type": "run\nsecret"}, {"type": "x" * 81},
])
def test_invalid_event_hints_are_rejected_without_partial_consumption(update):
    with pytest.raises(RuntimeUnavailable):
        validate_event_page(page([hint() | update]), 0)


def test_ordering_bounds_and_unknown_payload_are_checked():
    for events in ([hint(), hint()], [hint(2), hint(1)], [hint()] * 101):
        with pytest.raises(RuntimeUnavailable):
            validate_event_page(page(events, cursor=2), 0)
    result = validate_event_page(page([hint() | {"prompt": "private content"}]), 0)
    assert "private content" not in str(result)


def test_head_cursor_does_not_skip_later_pages():
    adapter = ScriptedAdapter([
        page([hint(cursor) for cursor in range(1, 101)], cursor=250),
        page([hint(cursor) for cursor in range(101, 201)], cursor=250),
        page([hint(cursor) for cursor in range(201, 251)], cursor=250),
    ])
    reader, refreshed = reader_for(adapter)
    for cursor in (100, 200, 250):
        assert reader.poll_once()
        assert reader.after == cursor
    assert adapter.afters == [0, 100, 200]
    assert refreshed == ["bridge-1"] * 3


def test_repeated_and_regressing_pages_keep_last_cursor_and_snapshot():
    adapter = ScriptedAdapter([page(), page(), page([], cursor=0)])
    reader, refreshed = reader_for(adapter)
    reader.poll_once()
    for _ in range(2):
        with pytest.raises(RuntimeUnavailable):
            reader.poll_once()
    assert reader.after == 1
    assert refreshed == ["bridge-1"]


@pytest.mark.parametrize("events", [[hint(3)], [hint(1), hint(3)], []])
def test_evicted_interior_or_empty_gap_refreshes_all_owned_active_runs(events):
    adapter = ScriptedAdapter([page(events, cursor=3)])
    rows = [{"id": "bridge-1", "native_id": "native-1", "native_conversation": "conversation-1"},
            {"id": "bridge-2", "native_id": "native-2", "native_conversation": "conversation-2"}]
    reader, refreshed = reader_for(adapter, rows=rows)
    reader.instance_id = adapter.instance
    reader.resync = False
    reader.poll_once()
    assert set(refreshed) == {"bridge-1", "bridge-2"}
    assert reader.after == 3


def test_hints_cannot_select_other_runs_or_conversations():
    adapter = ScriptedAdapter([page([hint(run="foreign"),
        hint(2, conversation="foreign"), hint(3)], cursor=3)])
    reader, refreshed = reader_for(adapter)
    reader.instance_id = adapter.instance
    reader.resync = False
    reader.poll_once()
    assert refreshed == ["bridge-1"]


def test_changed_authority_after_an_inflight_read_discards_hints():
    rows = [{"id": "bridge-1", "native_id": "native-1", "native_conversation": "conversation-1"}]

    def replacement():
        rows.clear()
        return page()

    reader, refreshed = reader_for(ScriptedAdapter([replacement]), rows=rows)
    assert reader.poll_once() is False
    assert reader.after == 0 and refreshed == []


def test_adapter_restart_resets_only_cursor_and_refreshes_snapshots():
    adapter = ScriptedAdapter([page(instance="instance-2")])
    adapter.instance = "instance-2"
    reader, refreshed = reader_for(adapter)
    reader.instance_id = "instance-1"
    reader.after = 50
    reader.resync = False
    assert reader.poll_once()
    assert reader.instance_id == "instance-2" and reader.after == 1
    assert refreshed == ["bridge-1"]


def test_unconfirmed_adapter_restart_is_rejected():
    reader, refreshed = reader_for(ScriptedAdapter([page(instance="instance-2")]))
    with pytest.raises(RuntimeUnavailable):
        reader.poll_once()
    assert reader.after == 0 and refreshed == []


def test_unsupported_feature_is_cached_and_readers_stop_without_replay():
    adapter = ScriptedAdapter([])
    adapter.supported = False
    reader, refreshed = reader_for(adapter)
    reader.start()
    reader.thread.join(1)
    assert not reader.thread.is_alive()
    assert reader.retry_at > time.monotonic()
    assert not adapter.afters and not refreshed
    reader.close()
    assert reader.poll_once() is False


def wait_until(predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        threading.Event().wait(0.01)
    assert predicate()


def start_run(env, request_id):
    service, ws_id, token, job, _, _ = env
    run = service.call(ws_id, token, "start_run", {
        "adapter_id": ADAPTER_ID, "job_id": job["id"], "request_id": request_id})
    return service.run_coordinator, service.workspace(ws_id), run["run_id"]


class RecordedHub:
    def __init__(self, service):
        self.service = service
        self.published = []
        self.completed = threading.Event()

    def publish(self, run_id):
        with self.service.lock:
            row = self.service.db.execute("SELECT phase FROM agent_runs WHERE id=?", (run_id,)).fetchone()
            self.published.append(run_id)
            if row and row["phase"] == "terminal":
                self.completed.set()

    def close(self):
        pass


def test_meaningful_committed_snapshots_publish_once_and_terminal_cannot_regress(modern_env):
    coordinator, ws, run_id = start_run(modern_env, "meaningful-events")
    coordinator.run_events.close()
    hub = coordinator.run_events = RecordedHub(coordinator.service)
    run = coordinator._run_row(ws, run_id)
    native = coordinator.adapter(ADAPTER_ID).run(run["native_id"])
    coordinator._persist_snapshot(run, native, [], [])
    assert hub.published == []
    interaction = {"id": "request-1", "runId": run["native_id"], "kind": "choice", "prompt": "review"}
    activity = {"id": "activity-1", "runId": run["native_id"], "kind": "command", "status": "running"}
    coordinator._persist_snapshot(run, native, [interaction], [activity])
    coordinator._persist_snapshot(run, copy.deepcopy(native), [interaction], [activity])
    assert hub.published == [run_id]
    terminal = native | {"phase": "terminal", "activeState": None, "outcome": "succeeded"}
    coordinator._persist_snapshot(run, terminal, [], [activity | {"status": "completed"}])
    coordinator._persist_snapshot(run, native, [interaction], [activity])
    assert hub.published == [run_id, run_id]
    assert coordinator._run_row(ws, run_id)["phase"] == "terminal"
    assert coordinator.read(ws, run_id, sync=False)["interactions"][0]["state"] == "stale"


def test_background_native_completion_reaches_manager_before_fallback_sweep(modern_env):
    service, _, _, _, native, _ = modern_env
    coordinator, ws, run_id = start_run(modern_env, "background-event")
    direct = coordinator.adapter(ADAPTER_ID)
    direct.events = lambda *, after, wait_ms: native.events(after, wait_ms)
    coordinator.run_events.close()
    hub = coordinator.run_events = RecordedHub(service)
    coordinator.background = True
    coordinator.start_background()
    wait_until(lambda: bool(coordinator._event_readers))
    reader = next(iter(coordinator._event_readers.values()))
    wait_until(lambda: reader.after == native.cursor)
    run = coordinator._run_row(ws, run_id)
    conversation = coordinator._conversation(run)
    native_run_id = native.run(run["native_id"])["nativeId"]
    native_thread_id = native.conversation(conversation["native_id"])["nativeId"]
    native._notification("turn/completed", {"threadId": native_thread_id,
        "turn": {"id": native_run_id, "status": "completed"}})
    assert hub.completed.wait(2), "event hint did not accelerate authoritative completion"
    assert coordinator.read(ws, run_id, sync=False)["outcome"] == "succeeded"


@pytest.mark.parametrize("failure", ["unsupported", "unavailable"])
def test_five_second_snapshot_fallback_survives_missing_event_transport(modern_env, failure):
    service, _, _, _, native, _ = modern_env
    coordinator, ws, run_id = start_run(modern_env, "fallback-" + failure)
    direct = coordinator.adapter(ADAPTER_ID)
    if failure == "unsupported":
        descriptor = direct.descriptor()
        direct.descriptor = lambda: Descriptor(descriptor.runtime_id, descriptor.display_name,
            descriptor.adapter_version, descriptor.native_version, descriptor.instance_id,
            {key: value for key, value in descriptor.features.items() if key != "events"})
    else:
        def unavailable(**kwargs):
            raise RuntimeUnavailable("event endpoint unavailable")
        direct.events = unavailable
    coordinator.run_events.close()
    hub = coordinator.run_events = RecordedHub(service)
    coordinator.background = True
    coordinator.start_background()
    wait_until(lambda: bool(coordinator._event_readers))
    reader = next(iter(coordinator._event_readers.values()))
    if failure == "unsupported":
        wait_until(lambda: reader.retry_at > 0)
    else:
        wait_until(lambda: reader.instance_id is not None)
    run = coordinator._run_row(ws, run_id)
    conversation = coordinator._conversation(run)
    native._notification("turn/completed", {
        "threadId": native.conversation(conversation["native_id"])["nativeId"],
        "turn": {"id": native.run(run["native_id"])["nativeId"], "status": "completed"}})
    assert hub.completed.wait(6)
    assert coordinator.read(ws, run_id, sync=False)["outcome"] == "succeeded"


def test_shutdown_during_native_snapshot_cannot_access_closed_bridge_state(modern_env):
    service, _, _, _, _, _ = modern_env
    coordinator, ws, run_id = start_run(modern_env, "shutdown-event")
    direct = coordinator.adapter(ADAPTER_ID)
    original = direct.run
    entered, release = threading.Event(), threading.Event()
    errors = []

    def blocked(ident):
        snapshot = original(ident)
        entered.set()
        release.wait(2)
        return snapshot

    direct.run = blocked

    def reconcile():
        try:
            coordinator.reconcile(ws, run_id)
        except Exception as exc:
            errors.append(exc)

    thread = threading.Thread(target=reconcile)
    thread.start()
    assert entered.wait(1)
    service.close()
    release.set()
    thread.join(2)
    assert not thread.is_alive()
    assert len(errors) == 1 and isinstance(errors[0], RuntimeUnavailable)


@pytest.mark.parametrize("lookup", ["run", "find_run"])
def test_stale_not_found_cannot_orphan_a_run_after_authority_changed(modern_env, lookup):
    service, _, _, _, _, _ = modern_env
    coordinator, ws, run_id = start_run(modern_env, "stale-missing-" + lookup)
    direct = coordinator.adapter(ADAPTER_ID)
    if lookup == "find_run":
        with service.lock, service.db:
            service.db.execute("UPDATE agent_runs SET native_id=NULL WHERE id=?", (run_id,))

    def replacement(*args):
        with service.lock, service.db:
            service.db.execute("UPDATE node_adapters SET revision=? WHERE adapter_id=?",
                               ("rev_" + "f" * 32, ADAPTER_ID))
        raise RuntimeRejected("Run not found", code="not_found")

    setattr(direct, lookup, replacement)
    with pytest.raises(BridgeError) as error:
        coordinator.reconcile(ws, run_id)
    assert error.value.code == "adapter_changed"
    stored = coordinator._run_row(ws, run_id)
    assert stored["phase"] == "active" and stored["outcome"] is None


def test_recovered_native_identity_is_not_bound_after_authority_changed(modern_env):
    service, _, _, _, _, _ = modern_env
    coordinator, ws, run_id = start_run(modern_env, "stale-recovery")
    direct = coordinator.adapter(ADAPTER_ID)
    original = direct.find_run
    with service.lock, service.db:
        service.db.execute("UPDATE agent_runs SET native_id=NULL WHERE id=?", (run_id,))

    def replacement(*args):
        snapshot = original(*args)
        with service.lock, service.db:
            service.db.execute("UPDATE node_adapters SET revision=? WHERE adapter_id=?",
                               ("rev_" + "f" * 32, ADAPTER_ID))
        return snapshot

    direct.find_run = replacement
    with pytest.raises(BridgeError) as error:
        coordinator.reconcile(ws, run_id)
    assert error.value.code == "adapter_changed"
    assert coordinator._run_row(ws, run_id)["native_id"] is None


def test_starting_run_reconciliation_waits_for_admitted_native_submission(modern_env):
    service, ws_id, _, job, native, _ = modern_env
    coordinator = service.run_coordinator
    ws = service.workspace(ws_id)
    direct = coordinator.adapter(ADAPTER_ID)
    original = direct.start_run
    entered, release, attempted, reconciled = (threading.Event() for _ in range(4))
    captured, errors = [], []

    def blocked(*args):
        snapshot = original(*args)
        captured.append(copy.deepcopy(snapshot))
        entered.set()
        assert release.wait(2)
        return snapshot

    direct.start_run = blocked

    def start():
        try:
            coordinator.start(ws, ADAPTER_ID, job["id"], "admitted-start-race")
        except Exception as exc:
            errors.append(exc)

    start_thread = threading.Thread(target=start)
    start_thread.start()
    assert entered.wait(1)
    with service.lock:
        row = service.db.execute("SELECT id,phase,native_id FROM agent_runs").fetchone()
    assert row["phase"] == "starting" and row["native_id"] is None

    def reconcile():
        attempted.set()
        try:
            coordinator.reconcile(ws, row["id"])
        except Exception as exc:
            errors.append(exc)
        finally:
            reconciled.set()

    reconcile_thread = threading.Thread(target=reconcile)
    reconcile_thread.start()
    assert attempted.wait(1)
    try:
        assert not reconciled.wait(0.05), "recovery raced an admitted native submission"
        snapshot = captured[0]
        native._notification("turn/completed", {
            "threadId": native.conversation(snapshot["conversationId"])["nativeId"],
            "turn": {"id": native.run(snapshot["id"])["nativeId"], "status": "completed"}})
    finally:
        release.set()
        start_thread.join(2)
        reconcile_thread.join(2)
    assert not start_thread.is_alive() and not reconcile_thread.is_alive()
    assert errors == []
    stored = coordinator._run_row(ws, row["id"])
    assert stored["phase"] == "terminal" and stored["outcome"] == "succeeded"
    with service.lock:
        outcomes = [item["event_type"] for item in service.db.execute(
            "SELECT event_type FROM notification_events WHERE run_id=?", (row["id"],))]
    assert outcomes == ["run_completed"]


@pytest.mark.parametrize("operation", ["cancel_run", "send_run_input"])
def test_mcp_mutation_does_not_invert_locks_against_background_reconciliation(modern_env, operation):
    service, _, token, _, _, _ = modern_env
    coordinator, ws, run_id = start_run(modern_env, "mutation-lock-" + operation)
    direct = coordinator.adapter(ADAPTER_ID)
    original_run = direct.run
    original_mutation = direct.cancel if operation == "cancel_run" else direct.steer
    entered, release, mutated = (threading.Event() for _ in range(3))
    errors, results = [], []

    def blocked(ident):
        snapshot = original_run(ident)
        if threading.current_thread().name == "test-background-reconcile":
            entered.set()
            assert release.wait(2)
        return snapshot

    def native_mutation(*args):
        result = original_mutation(*args)
        mutated.set()
        return result

    direct.run = blocked
    setattr(direct, "cancel" if operation == "cancel_run" else "steer", native_mutation)

    def reconcile():
        try:
            coordinator.reconcile(ws, run_id)
        except Exception as exc:
            errors.append(exc)

    def mutation():
        try:
            arguments = {"run_id": run_id}
            if operation == "send_run_input":
                arguments.update(instruction="Review this now", request_id="mutation-lock-input", mode="steer")
            results.append(service.call(ws["id"], token, operation, arguments))
        except Exception as exc:
            errors.append(exc)

    background = threading.Thread(target=reconcile, name="test-background-reconcile")
    mutation_thread = threading.Thread(target=mutation)
    background.start()
    assert entered.wait(1)
    mutation_thread.start()
    try:
        assert mutated.wait(1)
        acquired = service.lock.acquire(timeout=0.2)
        assert acquired, "a native mutation retained the shared database lock"
        service.lock.release()
    finally:
        release.set()
        background.join(2)
        mutation_thread.join(2)
    assert not background.is_alive() and not mutation_thread.is_alive()
    assert errors == [] and len(results) == 1
    if operation == "send_run_input":
        assert results[0]["input_message"]["status"] == "delivered"
    else:
        # Cancellation admission is asynchronous; native completion decides
        # the terminal outcome later.
        assert results[0]["run_id"] == run_id
        assert any(method == "turn/interrupt" for method, _ in modern_env[-1].calls)


def test_concurrent_manager_and_mcp_start_with_same_request_submit_native_once(modern_env):
    service, ws_id, token, job, _, _ = modern_env
    coordinator = service.run_coordinator
    ws = service.workspace(ws_id)
    direct = coordinator.adapter(ADAPTER_ID)
    original = direct.start_run
    entered, release, attempted = (threading.Event() for _ in range(3))
    calls, results, errors = [], [], []

    def blocked(*args):
        calls.append(args)
        snapshot = original(*args)
        entered.set()
        assert release.wait(2)
        return snapshot

    direct.start_run = blocked

    def manager_start():
        try:
            results.append(coordinator.start(ws, ADAPTER_ID, job["id"], "shared-admission"))
        except Exception as exc:
            errors.append(exc)

    def mcp_start():
        attempted.set()
        try:
            results.append(service.call(ws_id, token, "start_run", {
                "adapter_id": ADAPTER_ID, "job_id": job["id"], "request_id": "shared-admission"}))
        except Exception as exc:
            errors.append(exc)

    manager_thread = threading.Thread(target=manager_start)
    mcp_thread = threading.Thread(target=mcp_start)
    manager_thread.start()
    assert entered.wait(1)
    mcp_thread.start()
    try:
        assert attempted.wait(1)
        acquired = service.lock.acquire(timeout=0.2)
        assert acquired, "a native start retained the shared database lock"
        service.lock.release()
    finally:
        release.set()
        manager_thread.join(2)
        mcp_thread.join(2)
    assert not manager_thread.is_alive() and not mcp_thread.is_alive()
    assert errors == [] and len(calls) == 1 and len(results) == 2
    assert results[0]["run_id"] == results[1]["run_id"]
    assert sum(bool(result.get("idempotent")) for result in results) == 1


def test_queued_mcp_mutation_reauthenticates_after_gateway_token_rotation(modern_env):
    service, ws_id, token, job, _, _ = modern_env
    direct = service.run_coordinator.adapter(ADAPTER_ID)
    calls, errors = [], []
    attempted = threading.Event()
    direct.start_run = lambda *args: calls.append(args)

    def mutation():
        attempted.set()
        try:
            service.call(ws_id, token, "start_run", {
                "adapter_id": ADAPTER_ID, "job_id": job["id"], "request_id": "queued-old-token"})
        except Exception as exc:
            errors.append(exc)

    queue = service.run_coordinator.admission_lock(ws_id, ADAPTER_ID)
    queue.__enter__()
    thread = threading.Thread(target=mutation)
    thread.start()
    try:
        assert attempted.wait(1)
        service.manage_bridge("rotate_token")
    finally:
        queue.__exit__(None, None, None)
        thread.join(2)
    assert not thread.is_alive() and calls == []
    assert len(errors) == 1 and isinstance(errors[0], BridgeError)
    assert errors[0].code == "unauthorized"


def change_admission(service, ws, change):
    with service.lock, service.db:
        if change == "node":
            service.db.execute("UPDATE nodes SET revision=? WHERE id=?",
                               ("rev_" + "e" * 32, ws["node_id"]))
        elif change == "route":
            service.db.execute("UPDATE workspace_routes SET enabled=0 WHERE workspace=? AND adapter_id=?",
                               (ws["id"], ADAPTER_ID))
        elif change == "profile":
            service.db.execute("UPDATE workspace_routes SET profile_id='reviewed' "
                               "WHERE workspace=? AND adapter_id=?", (ws["id"], ADAPTER_ID))
        else:
            service.db.execute("UPDATE workspace_routes SET security_source='native',profile_id='' "
                               "WHERE workspace=? AND adapter_id=?", (ws["id"], ADAPTER_ID))


@pytest.mark.parametrize("phase", ["conversation", "start"])
@pytest.mark.parametrize("change", ["node", "route", "profile", "source"])
def test_start_rechecks_admission_after_native_discovery_before_submission(modern_env, phase, change):
    service, ws_id, _, job, _, _ = modern_env
    coordinator = service.run_coordinator
    ws = service.workspace(ws_id)
    direct = coordinator.adapter(ADAPTER_ID)
    original_descriptor = direct.descriptor
    original_conversation = direct.create_conversation
    conversation_calls, run_calls = [], []

    def descriptor():
        value = original_descriptor()
        if phase == "conversation":
            change_admission(service, ws, change)
        return value

    def conversation(payload):
        conversation_calls.append(payload)
        value = original_conversation(payload)
        if phase == "start":
            change_admission(service, ws, change)
        return value

    direct.descriptor = descriptor
    direct.create_conversation = conversation
    direct.start_run = lambda *args: run_calls.append(args)
    with pytest.raises(BridgeError) as error:
        coordinator.start(ws, ADAPTER_ID, job["id"], "changed-admission-" + phase + change)
    assert error.value.code == {"node": "node_changed", "route": "route_disabled"}.get(change, "binding_mismatch")
    assert run_calls == []
    assert len(conversation_calls) == (0 if phase == "conversation" else 1)


@pytest.mark.parametrize("change", ["node", "route", "profile", "source"])
def test_run_input_rechecks_admission_after_live_binding_read(modern_env, change):
    service, _, _, _, _, _ = modern_env
    coordinator, ws, run_id = start_run(modern_env, "changed-input-" + change)
    direct = coordinator.adapter(ADAPTER_ID)
    original = direct.run
    calls = []

    def changed(ident):
        snapshot = original(ident)
        change_admission(service, ws, change)
        return snapshot

    direct.run = changed
    direct.steer = lambda *args: calls.append(args)
    with pytest.raises(BridgeError) as error:
        coordinator.input(ws, run_id, "Review now", "changed-input", "steer")
    assert error.value.code == {"node": "node_changed", "route": "route_disabled"}.get(change, "binding_mismatch")
    assert calls == []
