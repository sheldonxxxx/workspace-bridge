"""Durable webhook lifecycle, delivery isolation and safe canonical events."""
import base64
import json
import sqlite3
import time

import pytest

from workspace_bridge.event_broker import EventBroker, FINISHED, NEEDS_ATTENTION
from workspace_bridge.notifications import NotificationEvent
from workspace_bridge.security import BridgeError
from workspace_bridge.service import uid
from workspace_bridge.webhook_transport import CallbackError
from event_fakes import CallbackReceiver, TEST_SECRET, TEST_URL


@pytest.fixture
def broker_env(env):
    service = env['service']
    service.event_broker.close()
    receiver = CallbackReceiver()
    service.event_broker = EventBroker(service, transport=receiver)
    return {**env, 'broker': service.event_broker, 'receiver': receiver}


def publish(env, *, kind='run_completed', workspace_id=None, run_id=None, subject_id='',
            runtime_type='codex', stamp='2026-10-04T00:00:00+00:00'):
    event = NotificationEvent.build(event_type=kind, workspace_id=workspace_id or env['id'],
        run_id=run_id or uid('run_'), adapter_id='adapter_000000000000000000000001', runtime_type=runtime_type,
        workspace_name='private project name', handoff_title='private prompt/result', adapter_name='private host',
        subject_id=subject_id, request_kind='approval' if subject_id else '', action='private_action', occurred_at=stamp)
    return env['service'].notification_manager.publish(event)


def subscribe(env, *, name=FINISHED, arguments=None, secret=TEST_SECRET, url=TEST_URL, **kwargs):
    return env['broker'].subscribe(env['token'], name, arguments or {'workspace_id': env['id']},
                                  {'mode': 'webhook', 'url': url, 'secret': secret}, **kwargs)


def test_subscribe_verify_idempotent_refresh_and_unsubscribe(broker_env):
    env = broker_env
    first = subscribe(env)
    second = subscribe(env)
    assert first['id'] == second['id'] and first['cursor'] is None and first['truncated'] is False
    assert second['refreshBefore'] >= first['refreshBefore'] and len(env['receiver'].verifications) == 1
    assert env['service'].db.execute('SELECT COUNT(*) FROM mcp_event_subscriptions').fetchone()[0] == 1
    publish(env)
    env['broker'].drain_once()
    assert len(env['receiver'].deliveries) == 1
    assert env['broker'].unsubscribe(env['token'], FINISHED, {'workspace_id': env['id']},
        {'mode': 'webhook', 'url': TEST_URL}) == {}
    assert env['broker'].unsubscribe(env['token'], FINISHED, {'workspace_id': env['id']},
        {'mode': 'webhook', 'url': TEST_URL}) == {}
    publish(env)
    env['broker'].drain_once()
    assert len(env['receiver'].deliveries) == 1


def test_callback_failure_does_not_activate_subscription(broker_env):
    env = broker_env
    env['receiver'].verification_error = 'challenge_failed'
    with pytest.raises(CallbackError):
        subscribe(env)
    assert env['service'].db.execute('SELECT COUNT(*) FROM mcp_event_subscriptions').fetchone()[0] == 0
    publish(env)
    env['broker'].drain_once()
    assert env['receiver'].deliveries == []


def test_tail_start_ordering_and_dedupe(broker_env):
    env = broker_env
    publish(env)
    subscribe(env)
    run = uid('run_')
    first = publish(env, run_id=run, stamp='2026-10-04T01:00:00Z')
    assert publish(env, run_id=run) == first
    second = publish(env, kind='run_failed', stamp='2026-10-03T00:00:00Z')
    env['broker'].drain_once()
    assert [item['event']['eventId'] for item in env['receiver'].deliveries] == [first, second]
    env['broker'].drain_once()
    assert len(env['receiver'].deliveries) == 2


@pytest.mark.parametrize('runtime_type', ['pi', 'codex', 'claude'])
def test_safe_envelope_all_outcomes_and_attention(broker_env, runtime_type):
    env = broker_env
    subscribe(env)
    subscribe(env, name=NEEDS_ATTENTION)
    expected = [publish(env, kind=kind, runtime_type=runtime_type) for kind in (
        'run_completed', 'run_failed', 'run_cancelled', 'run_interrupted', 'run_orphaned', 'run_blocked')]
    expected.append(publish(env, kind='run_needs_attention', subject_id='int_' + '1' * 24, runtime_type=runtime_type))
    env['broker'].drain_once()
    events = [item['event'] for item in env['receiver'].deliveries]
    assert [event['eventId'] for event in events] == expected
    assert [event['data']['outcome'] for event in events[:-1]] == ['succeeded', 'failed', 'cancelled', 'interrupted', 'orphaned', 'blocked']
    assert events[-1]['name'] == NEEDS_ATTENTION and events[-1]['data']['interaction_id'] == 'int_' + '1' * 24
    assert all(event['cursor'] is None and event['data']['runtime_type'] == runtime_type for event in events)
    assert 'private' not in json.dumps(events) and 'secret' not in json.dumps(env['broker'].status())
    assert env['service'].db.execute('SELECT COUNT(*) FROM notification_events').fetchone()[0] == 7
    assert env['service'].db.execute('SELECT COUNT(*) FROM notification_deliveries').fetchone()[0] == 0


def test_workspace_and_event_filters(broker_env):
    env = broker_env
    other_root = env['parent'] / 'beta'
    other_root.mkdir()
    service = env['service']
    other = service.add_workspace('Beta', str(other_root), [], env['node_id'])['workspace']['id']
    service.manage_workspace(other, 'enable')
    subscribe(env)
    publish(env, workspace_id=other)
    publish(env, kind='run_needs_attention', subject_id='int_' + '2' * 24)
    expected = publish(env)
    env['broker'].drain_once()
    assert [item['event']['eventId'] for item in env['receiver'].deliveries] == [expected]
    with pytest.raises(BridgeError):
        subscribe(env, arguments={'workspace_id': env['id'], 'run_id': 'run_' + '0' * 24})


def test_retry_keeps_event_id_body_and_signs_each_attempt(broker_env):
    env = broker_env
    instant = [time.time()]
    env['broker']._clock = lambda: instant[0]
    subscribe(env)
    env['receiver'].responses = [503, CallbackError('timeout'), 204]
    event_id = publish(env)
    env['broker'].drain_once()
    assert env['broker'].status()['deliveries'] == {'pending': 1}
    instant[0] += 2
    env['broker'].drain_once()
    instant[0] += 4
    env['broker'].drain_once()
    deliveries = env['receiver'].deliveries
    assert len(deliveries) == 3 and len({item['body'] for item in deliveries}) == 1
    assert all(item['headers']['webhook-id'] == event_id for item in deliveries)
    assert len({item['headers']['webhook-signature'] for item in deliveries}) == 3
    assert env['broker'].status()['deliveries'] == {'sent': 1}


@pytest.mark.parametrize('status', [410, 413, 302, 400])
def test_permanent_failures_are_not_retried(broker_env, status):
    env = broker_env
    subscribe(env)
    env['receiver'].responses = [status]
    publish(env)
    env['broker'].drain_once()
    env['broker'].drain_once()
    assert len(env['receiver'].deliveries) == 1
    assert env['broker'].status()['deliveries'] == {'failed': 1}
    if status == 410:
        publish(env)
        env['broker'].drain_once()
        assert len(env['receiver'].deliveries) == 1
        assert env['broker'].status()['subscriptions'] == {'terminated': 1}


def test_retry_attempts_are_bounded(broker_env):
    env = broker_env
    instant = [time.time()]
    env['broker']._clock = lambda: instant[0]
    subscribe(env)
    env['receiver'].responses = [503] * 10
    publish(env)
    for _ in range(10):
        env['broker'].drain_once()
        instant[0] += 100
    assert len(env['receiver'].deliveries) == 6
    assert env['broker'].status()['deliveries'] == {'failed': 1}


@pytest.mark.parametrize('revocation', ['workspace', 'gateway', 'rotation'])
def test_access_revocation_stops_delivery(broker_env, revocation):
    env = broker_env
    subscribe(env)
    publish(env)
    if revocation == 'workspace':
        env['service'].manage_workspace(env['id'], 'disable')
    elif revocation == 'gateway':
        env['service'].manage_bridge('disable')
    else:
        env['service'].manage_bridge('rotate_token')
    env['broker'].drain_once()
    assert env['receiver'].deliveries == []
    assert env['broker'].status()['subscriptions'] == {'revoked': 1}


def test_expiry_refresh_no_replay_and_verification_cache_expiry(broker_env):
    env = broker_env
    instant = [time.time()]
    env['broker']._clock = lambda: instant[0]
    first = subscribe(env, ttl_ms=1)
    row = env['service'].db.execute('SELECT expires_at FROM mcp_event_subscriptions').fetchone()
    assert row[0] == instant[0] + 60
    instant[0] += 301
    publish(env)
    env['broker'].drain_once()
    assert env['receiver'].deliveries == []
    second = subscribe(env, ttl_ms=None)
    assert second['id'] == first['id'] and second['refreshBefore'] is not None
    assert len(env['receiver'].verifications) == 2
    env['broker'].drain_once()
    assert env['receiver'].deliveries == []
    publish(env)
    env['broker'].drain_once()
    assert len(env['receiver'].deliveries) == 1


def test_secret_rotation_uses_dual_signatures_for_bounded_window(broker_env):
    env = broker_env
    instant = [time.time()]
    env['broker']._clock = lambda: instant[0]
    first = subscribe(env)
    replacement = 'whsec_' + base64.b64encode(b'new-event-key-0000000000000000000').decode()
    assert subscribe(env, secret=replacement)['id'] == first['id']
    publish(env)
    env['broker'].drain_once()
    assert len(env['receiver'].deliveries[-1]['headers']['webhook-signature'].split()) == 2
    instant[0] += 301
    publish(env)
    env['broker'].drain_once()
    assert len(env['receiver'].deliveries[-1]['headers']['webhook-signature'].split()) == 1


def test_pending_intent_survives_reopen_with_stable_event_id(broker_env):
    env = broker_env
    subscribe(env)
    expected = publish(env)
    env['receiver'].responses = [503]
    env['broker'].drain_once()
    service = env['service']
    env['broker'].close()
    service.notification_manager.close()
    service.run_coordinator.close()
    with service.lock:
        with service.db:
            service.db.execute("UPDATE mcp_event_deliveries SET status='sending',next_attempt=0")
        service.db.close()
        service.db = sqlite3.connect(service.state / 'bridge.sqlite3', check_same_thread=False)
        service.db.row_factory = sqlite3.Row
        service.db.execute('PRAGMA foreign_keys=ON')
        service.event_broker = EventBroker(service, transport=env['receiver'])
    service.event_broker.drain_once()
    assert env['receiver'].deliveries[-1]['event']['eventId'] == expected
    assert service.event_broker.status()['deliveries'] == {'sent': 1}


def test_notification_rollback_does_not_queue_delivery(broker_env):
    env = broker_env
    subscribe(env)
    with pytest.raises(RuntimeError):
        with env['service'].lock, env['service'].db:
            event = NotificationEvent.build(event_type='run_failed', run_id=uid('run_'), workspace_id=env['id'],
                adapter_id='adapter_' + '1' * 24, runtime_type='codex', workspace_name='Alpha',
                handoff_title='private', adapter_name='Local')
            env['service'].notification_manager._record_event_locked(event)
            raise RuntimeError('rollback')
    env['broker'].drain_once()
    assert env['receiver'].deliveries == []


def test_capacity_and_invalid_filters(broker_env, monkeypatch):
    env = broker_env
    monkeypatch.setattr('workspace_bridge.event_broker.MAX_SUBSCRIPTIONS', 1)
    subscribe(env)
    with pytest.raises(BridgeError) as error:
        subscribe(env, name=NEEDS_ATTENTION)
    assert error.value.code == 'subscription_limit'
    assert subscribe(env)['id'].startswith('sub_')
    for arguments in ({}, {'workspace_id': 'secret'}, {'workspace_id': env['id'], 'callback_url': 'secret'}):
        with pytest.raises(BridgeError):
            env['broker'].subscribe(env['token'], FINISHED, arguments,
                {'mode': 'webhook', 'url': TEST_URL, 'secret': TEST_SECRET})


def test_background_delivery_does_not_hold_service_lock(broker_env):
    import threading
    env = broker_env
    subscribe(env)
    entered, release = threading.Event(), threading.Event()
    original = env['receiver'].post
    def blocked(*args):
        entered.set()
        assert release.wait(2)
        return original(*args)
    env['receiver'].post = blocked
    env['broker'].start()
    publish(env)
    assert entered.wait(2)
    try:
        assert env['service'].call(env['id'], env['token'], 'workspace_info', {})['workspace_id'] == env['id']
    finally:
        release.set()


def test_idle_sweeps_do_not_rewrite_subscription_state(broker_env):
    env = broker_env
    subscribe(env)
    env['broker'].drain_once()
    before = env['service'].db.total_changes
    for _ in range(5):
        env['broker'].drain_once()
    assert env['service'].db.total_changes == before
