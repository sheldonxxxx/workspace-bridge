"""Runtime-neutral notification outbox and safe Discord channel behavior."""
import io
import json
import threading
import urllib.error
import urllib.request
from email.message import Message

import pytest

from workspace_bridge.cli import initialize
from workspace_bridge.notifications import (
    BRIDGE_USER_AGENT, DiscordChannel, NotificationEvent, NotificationManager,
    NotificationResult, notification_channels_from_environment,
)
from workspace_bridge.security import BridgeError
from workspace_bridge.service import Service

from notification_fakes import RecordingChannel


class FakeResponse:
    def __init__(self, status):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def make_event(*, event_type="run_completed", subject_id="", run_id="run_abc"):
    return NotificationEvent.build(
        event_type=event_type, run_id=run_id, workspace_id="ws_alpha",
        workspace_name="Alpha", handoff_title="Improve add",
        adapter_id="adapter_000000000000000000000001", adapter_name="Local Codex",
        runtime_type="codex",
        occurred_at="2026-09-19T00:00:00+00:00", subject_id=subject_id,
        request_kind="permission" if subject_id else "", action="external_directory" if subject_id else "")


def attach_channels(service, *channels):
    service.notification_manager.close()
    service.notification_manager = NotificationManager(service, list(channels))
    service.notification_manager.start()
    return service.notification_manager


def wait_for_drain(manager):
    assert manager._idle.wait(2), "notification worker did not drain pending rows"


def test_discord_formats_only_safe_metadata_and_suppresses_mentions(monkeypatch):
    records = []

    def urlopen(request, timeout=None):
        records.append({"url": request.full_url, "timeout": timeout,
                        "headers": {k.lower(): v for k, v in request.header_items()},
                        "body": json.loads(request.data.decode())})
        return FakeResponse(204)

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    channel = DiscordChannel("https://discord.example/api/webhooks/secret-token", sleep=lambda _: None)
    attention = make_event(event_type="run_needs_attention", subject_id="req_123")
    assert channel.deliver(attention).status == "sent"
    assert channel.deliver(make_event()).status == "sent"
    body = json.dumps(records[0]["body"])
    assert "Alpha" in body and "run_abc" in body and "external_directory" in body
    assert '"allowed_mentions": {"parse": []' in body
    assert "secret-token" not in body
    assert "permission.updated" not in body
    assert records[0]["body"]["embeds"][0]["description"].startswith("Review the pending request")
    assert records[1]["body"]["embeds"][0]["description"].startswith("Read the final run result")
    assert records[0]["headers"].get("user-agent") == BRIDGE_USER_AGENT
    assert "secret-token" not in BRIDGE_USER_AGENT


def test_discord_retries_are_bounded_and_diagnostics_are_safe(monkeypatch):
    sleeps = []

    def unavailable(request, timeout=None):
        raise urllib.error.URLError("down")

    monkeypatch.setattr(urllib.request, "urlopen", unavailable)
    channel = DiscordChannel("https://discord.example/hook", attempts=3, sleep=sleeps.append)
    result = channel.deliver(make_event())
    assert result.status == "failed" and result.attempts == 3 and len(sleeps) == 2
    assert result.code == "unreachable"

    url = "https://discord.example/api/webhooks/secret-token"
    body = json.dumps({"message": "Unknown Webhook", "code": 10015}).encode()
    headers = Message()
    headers["Content-Type"] = "application/json"

    def forbidden(request, timeout=None):
        raise urllib.error.HTTPError(url, 403, "Forbidden", headers, io.BytesIO(body))

    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    result = DiscordChannel(url, attempts=3, sleep=lambda _: None).deliver(make_event())
    assert result.status == "failed" and result.attempts == 1
    assert result.code == "http_403" and result.detail == "discord_10015: Unknown Webhook"
    assert "secret-token" not in json.dumps(result.public())


def test_discord_rejects_non_http_urls_and_environment_never_exposes_secret():
    assert notification_channels_from_environment({}) == []
    assert notification_channels_from_environment({"WB_DISCORD_WEBHOOK_URL": "file:///etc/passwd"}) == []
    with pytest.raises(BridgeError):
        DiscordChannel("file:///etc/passwd")
    configured = notification_channels_from_environment(
        {"WB_DISCORD_WEBHOOK_URL": "https://discord.example/hook"})
    assert [channel.channel_id for channel in configured] == ["discord"]


def test_fanout_is_independent_and_dedupe_persists_across_manager_restart(env):
    service = env["service"]

    class FailingChannel:
        channel_id = "broken"
        name = "Broken"
        enabled = True

        def __init__(self):
            self.calls = 0

        def deliver(self, event):
            self.calls += 1
            return NotificationResult("failed", 2, "unreachable")

    failing = FailingChannel()
    good = RecordingChannel()
    manager = attach_channels(service, failing, good)
    event = make_event()
    first_id = manager.publish(event)
    wait_for_drain(manager)
    assert failing.calls == 1 and len(good.calls) == 1
    assert manager.summary(event.run_id)["overall"] == "partial"

    manager.close()
    restarted_channel = RecordingChannel()
    restarted = NotificationManager(service, [restarted_channel])
    service.notification_manager = restarted
    restarted.start()
    restarted.publish(event)
    wait_for_drain(restarted)
    assert restarted_channel.calls == []  # old channel rows remain authoritative
    assert restarted.summary(event.run_id)["events"][0]["id"] == first_id
    assert restarted.summary(event.run_id)["overall"] == "partial"


def test_pending_delivery_survives_restart_and_sent_rows_stay_sent(env):
    service = env["service"]
    first = RecordingChannel()
    manager = attach_channels(service, first)
    sent = make_event(run_id="run_sent")
    manager.publish(sent)
    wait_for_drain(manager)
    assert len(first.calls) == 1

    pending = make_event(run_id="run_pending")
    with service.lock, service.db:
        event_id, _ = manager._record_event_locked(pending)
        service.db.execute(
            "UPDATE notification_deliveries SET status='sending' WHERE event_id=?",
            (event_id,))
    manager.close()
    recovered = RecordingChannel()
    restarted = NotificationManager(service, [recovered])
    service.notification_manager = restarted
    restarted.start()
    wait_for_drain(restarted)
    assert [item.run_id for item in recovered.calls] == ["run_pending"]
    assert restarted.summary("run_pending")["overall"] == "sent"
    assert restarted.summary("run_sent")["overall"] == "sent"


def test_no_channels_is_disabled_and_untrusted_delivery_result_is_bounded(env):
    service = env["service"]
    manager = attach_channels(service)
    event = make_event(run_id="run_disabled")
    manager.publish(event)
    wait_for_drain(manager)
    disabled = manager.summary(event.run_id)
    assert disabled["overall"] == "disabled" and disabled["channels"] == {}
    assert len(disabled["events"]) == 1
    assert disabled["events"][0]["event_type"] == "run_completed"
    assert disabled["events"][0]["channels"] == {}
    manager.close()

    class UnsafeChannel:
        channel_id = "unsafe"
        name = "Unsafe"
        enabled = True

        def deliver(self, event):
            return NotificationResult("failed", 999, "https://secret.invalid/key",
                                      "/Users/sheldon/private token=sk-" + "A" * 30)

    unsafe = NotificationManager(service, [UnsafeChannel()])
    service.notification_manager = unsafe
    unsafe.start()
    unsafe.publish(make_event(run_id="run_unsafe"))
    wait_for_drain(unsafe)
    summary = unsafe.summary("run_unsafe")
    assert summary["overall"] == "failed"
    assert summary["channels"]["unsafe"]["attempts"] == 20
    assert summary["channels"]["unsafe"]["code"] == "channel_error"
    assert summary["channels"]["unsafe"]["detail"] == ""


def test_channel_delivery_never_runs_under_the_service_lock(env):
    service = env["service"]

    class LockCheckingChannel:
        channel_id = "lock-check"
        name = "Lock check"
        enabled = True

        def __init__(self):
            self.calls = 0
            self.entered = threading.Event()
            self.release = threading.Event()
            self.released = None

        def deliver(self, event):
            self.calls += 1
            self.entered.set()
            self.released = self.release.wait(5)
            return NotificationResult("sent", 1)

    channel = LockCheckingChannel()
    manager = attach_channels(service, channel)
    try:
        with service.lock:
            manager.publish(make_event(run_id="run_lock"))
            assert channel.calls == 0
        assert channel.entered.wait(2), "worker did not enter the channel"
        # Probe from a different thread while delivery is still in progress:
        # the worker could reacquire its own RLock, and unrelated service
        # threads may hold it briefly, so a nonblocking probe is misleading.
        acquired = service.lock.acquire(timeout=2)
        if acquired:
            service.lock.release()
        assert acquired, "channel delivery held the service lock"
    finally:
        channel.release.set()
    wait_for_drain(manager)
    assert channel.calls == 1 and channel.released is True


def test_removed_channel_row_is_disabled_and_does_not_block_configured_delivery(env):
    service = env["service"]
    channel = RecordingChannel()
    manager = attach_channels(service, channel)
    event = make_event(run_id="run_removed_channel")
    with service.lock, service.db:
        event_id, _ = manager._record_event_locked(event)
        service.db.execute(
            "INSERT INTO notification_deliveries(event_id,channel_id,status,updated) "
            "VALUES(?,?,'pending',?)", (event_id, "aaa_removed", manager._now()))

    manager.wake()
    wait_for_drain(manager)
    with service.lock:
        removed = service.db.execute(
            "SELECT status,code FROM notification_deliveries "
            "WHERE event_id=? AND channel_id='aaa_removed'", (event_id,)).fetchone()
    assert dict(removed) == {"status": "disabled", "code": "channel_unconfigured"}
    assert [item.run_id for item in channel.calls] == [event.run_id]
    assert manager.summary(event.run_id)["overall"] == "partial"


def test_persistent_worker_drains_multiple_published_events_without_manual_dispatch(env):
    service = env["service"]
    channel = RecordingChannel()
    manager = attach_channels(service, channel)
    manager.publish(make_event(run_id="run_batch_a"))
    manager.publish(make_event(run_id="run_batch_b"))

    wait_for_drain(manager)
    assert {item.run_id for item in channel.calls} == {"run_batch_a", "run_batch_b"}


def test_slow_channel_does_not_block_publish_return_path(env):
    service = env["service"]

    class BlockingChannel:
        channel_id = "blocking"
        name = "Blocking"
        enabled = True

        def __init__(self):
            self.entered = threading.Event()
            self.release = threading.Event()
            self.calls = []

        def deliver(self, event):
            self.entered.set()
            self.release.wait()
            self.calls.append(event)
            return NotificationResult("sent", 1)

    channel = BlockingChannel()
    manager = attach_channels(service, channel)
    manager.publish(make_event(run_id="run_slow_first"))
    assert channel.entered.wait(2), "worker did not enter the blocking channel"

    published = threading.Event()

    def publish_second():
        manager.publish(make_event(run_id="run_slow_second"))
        published.set()

    publisher = threading.Thread(target=publish_second)
    publisher.start()
    try:
        assert published.wait(2), "publish waited for channel delivery to finish"
    finally:
        channel.release.set()
        publisher.join(timeout=2)
    wait_for_drain(manager)
    assert [item.run_id for item in channel.calls] == [
        "run_slow_first", "run_slow_second"]


def test_service_starts_and_stops_notification_dispatcher(tmp_path):
    parent = tmp_path / "projects"
    parent.mkdir()
    state = tmp_path / "bridge-state"
    config = initialize(state, 8765, 8766)
    service = Service(state, config, run_coordinator_background=False)
    thread = service.notification_manager._thread
    assert thread is not None and thread.daemon and thread.ident is not None
    service.close()
    assert not thread.is_alive()
