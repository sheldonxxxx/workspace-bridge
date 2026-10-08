"""MCP 2.0 discovery and the documented ChatGPT webhook subscription shape."""
import httpx
import pytest
from workspace_bridge.api import make_mcp, TOOLS
from workspace_bridge.event_broker import FINISHED
from workspace_bridge.protocol import LEGACY, MODERN, PREFIX
from workspace_bridge.webhook_transport import CallbackError
from test_event_broker import broker_env, publish
from event_fakes import TEST_SECRET, TEST_URL


def message(method, params=None):
    body = {'jsonrpc': '2.0', 'id': 1, 'method': method,
            'params': {**(params or {}), '_meta': {PREFIX + 'protocolVersion': MODERN,
                                                 PREFIX + 'clientCapabilities': {}}}}
    return body


async def rpc(client, env, method, params=None, *, token=None):
    return await client.post('/mcp', json=message(method, params), headers={
        'Accept': 'application/json', 'X-Bridge-Token': token if token is not None else env['token'],
        'MCP-Protocol-Version': MODERN, 'Mcp-Method': method,
        **({'Mcp-Name': params['name']} if params and 'name' in params else {})})


@pytest.fixture
async def event_client(broker_env):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=make_mcp(broker_env['service'])),
                                base_url='http://127.0.0.1:8765') as client:
        yield client


def subscription_params(env):
    return {'name': FINISHED, 'arguments': {'workspace_id': env['id']},
            'delivery': {'mode': 'webhook', 'url': TEST_URL, 'secret': TEST_SECRET}, 'cursor': None}


async def test_documented_discovery_and_complete_webhook_lifecycle(broker_env, event_client):
    env = broker_env
    result = (await rpc(event_client, env, 'server/discover')).json()['result']
    assert result['capabilities']['events'] == {} and result['resultType'] == 'complete'
    assert 'io.workspacebridge/experimental-events' not in result['capabilities']['extensions']
    catalog = (await rpc(event_client, env, 'events/list')).json()['result']
    assert len(catalog['events']) == 2 and catalog['nextCursor'] is None
    assert catalog['events'][0]['delivery'] == ['webhook']
    assert 'workspace_id' in catalog['events'][0]['inputSchema']['required']
    assert 'outcome' in catalog['events'][0]['payloadSchema']['properties']
    assert len((await rpc(event_client, env, 'tools/list')).json()['result']['tools']) == len(TOOLS)
    params = subscription_params(env)
    first = (await rpc(event_client, env, 'events/subscribe', params)).json()['result']
    second = (await rpc(event_client, env, 'events/subscribe', params)).json()['result']
    assert first['id'] == second['id'] and first['refreshBefore'] and first['cursor'] is None
    assert 'secret' not in str(first) and TEST_URL not in str(first)
    expected = publish(env)
    env['broker'].drain_once()
    assert env['receiver'].deliveries[0]['event']['eventId'] == expected
    params['delivery'].pop('secret')
    result = (await rpc(event_client, env, 'events/unsubscribe', params | {'cursor': None})).json()
    assert result['error']['code'] == -32602
    params.pop('cursor')
    result = (await rpc(event_client, env, 'events/unsubscribe', params)).json()['result']
    assert result['resultType'] == 'complete' and set(result) == {'resultType', '_meta'}
    publish(env)
    env['broker'].drain_once()
    assert len(env['receiver'].deliveries) == 1


async def test_legacy_does_not_advertise_or_execute_mcp2_events(broker_env, event_client):
    env = broker_env
    headers = {'Accept': 'application/json', 'X-Bridge-Token': env['token']}
    result = await event_client.post('/mcp', json={'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
        'params': {'protocolVersion': LEGACY[-1], 'capabilities': {}, 'clientInfo': {}}}, headers=headers)
    assert 'events' not in result.json()['result']['capabilities']
    result = await event_client.post('/mcp', json={'jsonrpc': '2.0', 'id': 1, 'method': 'events/list', 'params': {}}, headers=headers)
    assert result.json()['error']['code'] == -32601
    result = await rpc(event_client, env, 'events/poll')
    assert result.status_code == 404 and result.json()['error']['code'] == -32601


@pytest.mark.parametrize('update', [
    {'name': 'secret'}, {'arguments': {'workspace_id': 'secret'}}, {'arguments': {'workspace_id': 'ws_' + '1' * 24, 'unknown': 'secret'}},
    {'ttlMs': True}, {'ttlMs': 0}, {'cursor': 'secret-old-poll-cursor'},
    {'delivery': {'mode': 'poll', 'url': TEST_URL, 'secret': TEST_SECRET}},
    {'delivery': {'mode': 'webhook', 'url': 'http://private.example/secret', 'secret': TEST_SECRET}},
    {'delivery': {'mode': 'webhook', 'url': TEST_URL, 'secret': 'whsec_bad'}},
])
async def test_invalid_arguments_are_rejected_without_echo(broker_env, event_client, update):
    env = broker_env
    result = await rpc(event_client, env, 'events/subscribe', subscription_params(env) | update)
    assert result.json()['error']['code'] == -32602
    assert TEST_SECRET not in result.text and 'private.example' not in result.text and 'secret-old' not in result.text


async def test_callback_endpoint_error_has_documented_code_and_safe_reason(broker_env, event_client):
    env = broker_env
    env['receiver'].verification_error = 'timeout'
    result = await rpc(event_client, env, 'events/subscribe', subscription_params(env))
    assert result.json()['error']['code'] == -32015
    assert result.json()['error']['data'] == {'reason': 'timeout'}
    assert TEST_SECRET not in result.text and TEST_URL not in result.text
    assert env['broker'].status()['subscriptions'] == {}
    audit = env['service'].db.execute("SELECT action,outcome FROM events WHERE action='events/subscribe'").fetchall()
    assert [row['outcome'] for row in audit] == ['received_mcp2', 'callback_timeout']


async def test_auth_workspace_admission_and_protocol_checks(broker_env, event_client):
    env = broker_env
    assert (await rpc(event_client, env, 'events/list', token='wrong-secret')).status_code == 401
    params = subscription_params(env)
    params['arguments']['workspace_id'] = 'ws_' + '0' * 24
    result = await rpc(event_client, env, 'events/subscribe', params)
    assert result.json()['error']['data']['code'] == 'unavailable' and env['receiver'].verifications == []
    result = await event_client.post('/mcp', json=message('events/list'), headers={
        'Accept': 'application/json', 'X-Bridge-Token': env['token'], 'MCP-Protocol-Version': MODERN, 'Mcp-Method': 'tools/call'})
    assert result.json()['error']['code'] == -32020
    body = message('events/subscribe', subscription_params(env))
    body.pop('id')
    result = await event_client.post('/mcp', json=body, headers={
        'Accept': 'application/json', 'X-Bridge-Token': env['token'], 'MCP-Protocol-Version': MODERN, 'Mcp-Method': 'events/subscribe'})
    assert result.json()['error']['code'] == -32600
    assert env['receiver'].verifications == []
