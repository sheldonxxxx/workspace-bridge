import base64
import json
import httpx
import pytest
from workspace_bridge.api import make_mcp, TOOLS
from workspace_bridge.protocol import PREFIX, MODERN

async def modern_call(env, method='server/discover', params=None, *, header_changes=None, body_changes=None):
    params = dict(params or {})
    if method == 'tools/call' and params.get('name') not in ('list_workspaces', 'read_project_lead_skill'):
        params['arguments'] = {'workspace_id': env['id'], **params.get('arguments', {})}
    params = {**params, '_meta': {PREFIX+'protocolVersion': MODERN, PREFIX+'clientCapabilities': {}}}
    message = {'jsonrpc':'2.0', 'id':1, 'method': method, 'params':params}
    headers = {'X-Bridge-Token':env['token'], 'Accept':'application/json, text/event-stream',
               'MCP-Protocol-Version':MODERN, 'Mcp-Method': method}
    if 'name' in params:
        headers['Mcp-Name'] = params['name']
    for k, v in (header_changes or {}).items():
        if v is None: headers.pop(k, None)
        else: headers[k] = v
    if body_changes: body_changes(message)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=make_mcp(env['service'])), base_url='http://127.0.0.1:8765') as client:
        return await client.post('/mcp', json=message, headers=headers)

async def test_modern_discovery_no_initialize(env):
    response = await modern_call(env)
    result = response.json()['result']
    assert response.status_code == 200
    assert result['resultType'] == 'complete' and MODERN in result['supportedVersions']
    assert result['_meta'][PREFIX+'serverInfo']['name'] == 'workspace-bridge'

async def test_modern_tools_list_and_read(env):
    response = await modern_call(env, 'tools/list')
    assert len(response.json()['result']['tools']) == len(TOOLS)
    response = await modern_call(env, 'tools/call', {'name':'read_file','arguments':{'path':'README.md'}})
    result = response.json()['result']
    assert result['resultType'] == 'complete' and not result['isError']
    assert 'Alpha' in result['content'][0]['text']

@pytest.mark.parametrize('change', [{'Mcp-Method':None}, {'Mcp-Method':'tools/call'}, {'MCP-Protocol-Version':None}, {'MCP-Protocol-Version':'2025-11-25'}])
async def test_metadata_headers_required_and_match(env, change):
    r = await modern_call(env, header_changes=change)
    assert r.status_code == 400 and r.json()['error']['code'] == -32020

async def test_client_capabilities_required(env):
    r = await modern_call(env, body_changes=lambda m:m['params']['_meta'].pop(PREFIX+'clientCapabilities'))
    assert r.status_code == 400 and r.json()['error']['code'] == -32602

async def test_unsupported_modern_version_advertises_supported(env):
    r = await modern_call(env, header_changes={'MCP-Protocol-Version':'2099-01-01'}, body_changes=lambda m:m['params']['_meta'].update({PREFIX+'protocolVersion':'2099-01-01'}))
    assert r.status_code == 400 and r.json()['error']['code'] == -32022
    assert MODERN in r.json()['error']['data']['supported']

@pytest.mark.parametrize('header', [None, 'read_handoff', '=?base64?invalid?='])
async def test_tool_header_mismatch(env, header):
    r = await modern_call(env, 'tools/call', {'name':'read_file', 'arguments':{'path':'README.md'}}, header_changes={'Mcp-Name':header})
    assert r.status_code == 400 and r.json()['error']['code'] == -32020

async def test_base64_tool_header(env):
    value = '=?base64?' + base64.b64encode(b'read_file').decode() + '?='
    r = await modern_call(env, 'tools/call', {'name':'read_file', 'arguments':{'path':'README.md'}}, header_changes={'Mcp-Name':value})
    assert r.status_code == 200 and not r.json()['result']['isError']

async def test_modern_unknown_method(env):
    r = await modern_call(env, 'execute/shell')
    assert r.status_code == 404 and r.json()['error']['code'] == -32601

async def test_modern_handoff_workflow(env, payload):
    r = await modern_call(env, 'tools/call', {'name':'prepare_handoff', 'arguments':payload})
    result = r.json()['result']
    assert not result['isError']
    handoff = json.loads(result['content'][0]['text'])
    assert (env['root']/'.workspace-handoff'/'jobs'/handoff['id']/'TASK.md').is_file()
