"""RunCoordinator completion and interaction snapshots reach signed webhooks."""
from workspace_bridge.event_broker import EventBroker, FINISHED, NEEDS_ATTENTION
from test_run_coordinator import ADAPTER_ID, modern_env, _finish_test_run, _persist_pending_interaction
from event_fakes import CallbackReceiver, TEST_SECRET, TEST_URL


def test_coordinator_completion_and_attention_reach_the_shared_outbox_once(modern_env):
    service, ws_id, token, job, native, _ = modern_env
    service.event_broker.close()
    receiver = CallbackReceiver()
    service.event_broker = EventBroker(service, transport=receiver)
    delivery = {'mode': 'webhook', 'url': TEST_URL, 'secret': TEST_SECRET}
    for name in (FINISHED, NEEDS_ATTENTION):
        service.event_broker.subscribe(token, name, {'workspace_id': ws_id}, delivery)
    started = service.call(ws_id, token, 'start_agent_run', {
        'adapter_id': ADAPTER_ID, 'job_id': job['id'], 'request_id': 'webhook-integration'})
    run_id = started['run_id']
    interaction_id = _persist_pending_interaction(service, ws_id, token, native, run_id)
    service.event_broker.drain_once()
    assert len(receiver.deliveries) == 1
    event = receiver.deliveries[0]['event']
    assert event['name'] == NEEDS_ATTENTION and event['data']['interaction_id'] == interaction_id
    view = service.call(ws_id, token, 'read_agent_run', {'run_id': run_id})
    interaction = view['interactions'][0]
    choice = next(item for item in interaction['details']['choices'] if item['semantic'] == 'deny')
    service.call(ws_id, token, 'respond_agent_interaction', {
        'run_id': run_id, 'interaction_id': interaction_id, 'response': {'choiceId': choice['id']}})
    _finish_test_run(service, native, ws_id, run_id)
    service.call(ws_id, token, 'read_agent_run', {'run_id': run_id})
    service.event_broker.drain_once()
    assert len(receiver.deliveries) == 2
    event = receiver.deliveries[-1]['event']
    assert event['name'] == FINISHED and event['data']['run_id'] == run_id
    assert event['data']['outcome'] == 'succeeded' and event['data']['adapter_id'] == ADAPTER_ID
    assert receiver.deliveries[-1]['headers']['webhook-id'] == event['eventId']
