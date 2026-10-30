from admin_helpers import admin_cookie
"""Generic file tools: permissions are local policy, not tool-name prefixes."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import stat
from threading import Event

import httpx
import pytest

from workspace_bridge.api import TOOLS, make_admin, make_mcp
from workspace_bridge.security import BridgeError, HANDOFF, SafeRoot, digest, write_allowed
from workspace_bridge.service import Service
from mcp_helpers import argument_error, McpTransport, LEGACY, MODERN, PREFIX


def call(e, name, **args):
    return e['service'].call(e['id'], e['token'], name, args)


def scope(e, value):
    return e['service'].manage_workspace(e['id'], 'set_write_scope', write_scope=value)


def read(e, path):
    return call(e, 'read_file', path=path, start_line=1, max_lines=200, expected_sha256=None)


def create(e, path, content, ws=None):
    return e['service'].call(ws or e['id'], e['token'], 'apply_patch', {
        'changes': [{'op': 'create', 'path': path, 'content': content}], 'message': 'Create a file.'})


def edit(e, path, old, new, sha):
    return call(e, 'apply_patch', changes=[{'op': 'edit', 'path': path, 'sha256': sha, 'old': old, 'new': new}],
                message='Edit a file.')


def test_generic_tools_replace_handoff_names_without_aliases(env):
    assert len(TOOLS) == 24
    assert {'read_file','apply_patch','undo_edit'} <= TOOLS.keys()
    for name in ['write_file','edit_file','write_handoff_file','edit_handoff_file','review_changes','read_change','record_audit']:
        assert name not in TOOLS
        with pytest.raises(BridgeError) as exc: call(env, name)
        assert exc.value.code == 'unknown_tool'
    for name in ['apply_patch','undo_edit']:
        schema = TOOLS[name][0].model_json_schema()
        assert schema['additionalProperties'] is False
        assert 'workspace_id' in schema['required']
        assert 'write_scope' not in schema['properties'] and 'force' not in schema['properties']


@pytest.mark.parametrize('value', ['none','workspace'])
def test_scope_is_exposed_in_discovery_and_info_and_does_not_limit_reads(env, value):
    scope(env, value)
    info = call(env, 'workspace_info')
    assert info['write_scope'] == value and info['read_scope'] == 'workspace'
    assert info['writable_path_prefix'] == {'none':None,'workspace':''}[value]
    assert info['source_access'] == ('read_write' if value=='workspace' else 'read_only')
    listed = env['service'].call(None, env['token'], 'list_workspaces', {})
    assert listed['workspaces'][0]['write_scope'] == value
    assert read(env, 'src/main.py')['lines'][0]['text'].startswith('def add')


def test_default_is_read_only_and_handoffs_still_work(env, payload):
    assert call(env, 'workspace_info')['write_scope'] == 'none'
    note = HANDOFF + '/plan.md'
    for path in [note, 'new/plan.md']:
        with pytest.raises(BridgeError) as exc: create(env,path,'no')
        assert exc.value.code=='policy_denied'
    assert not (env['root']/'new').exists() and not (env['root']/HANDOFF).exists()
    job = call(env,'save_handoff',**payload)
    assert job['state']=='prepared' and not (env['root']/HANDOFF).exists()


def test_handoff_is_not_a_write_scope(env):
    with pytest.raises(BridgeError) as exc: scope(env,'handoff')
    assert exc.value.code=='invalid_arguments'


def test_workspace_enable_and_revoke_without_changing_tool_schema(env):
    schemas = {name: model.model_json_schema() for name,(model,*_) in TOOLS.items()}
    original = read(env,'src/main.py')
    with pytest.raises(BridgeError):
        edit(env,'src/main.py','return a + b','return a - b',original['sha256'])
    scope(env,'workspace')
    result=edit(env,'src/main.py','return a + b','return a - b',original['sha256'])
    assert (env['root']/'src/main.py').read_text().endswith('return a - b\n')
    create(env,'docs/design.md','# Design\n')
    scope(env,'none')
    with pytest.raises(BridgeError):
        edit(env,'src/main.py','return a - b','no',result['files'][0]['sha256'])
    with pytest.raises(BridgeError):
        create(env,HANDOFF+'/denied.md','denied')
    assert schemas == {name: model.model_json_schema() for name,(model,*_) in TOOLS.items()}


@pytest.mark.parametrize('path', ['../other.md','/tmp/no.md','src/../../other.md','src//a.py','src/./a.py',
    'C:\\project\\a.py','.WORKSPACE-HANDOFF/plan.md', '.env','.git/config','secrets.json',
    'nested/.ssh/key','node_modules/test.js','.wb-write-fake','src/.WB-WRITE-fake','src/.wb-write-dir/a.md'])
def test_workspace_scope_still_denies_unsafe_or_reserved_paths(env,path):
    scope(env,'workspace')
    with pytest.raises(BridgeError):create(env,path,'no')


@pytest.mark.parametrize('rule',['src','src/**','*.py'])
def test_workspace_scope_respects_admin_exclusions(env,rule):
    scope(env,'workspace');before=(env['root']/'src/main.py').read_bytes()
    env['service'].manage_workspace(env['id'],'set_excludes',[rule])
    with pytest.raises(BridgeError):edit(env,'src/main.py','return a + b','no',digest(before))
    assert (env['root']/'src/main.py').read_bytes()==before


@pytest.mark.parametrize('value',[None,'','all','read_write','WORKSPACE',False])
def test_invalid_policy_cannot_broaden_access(env,value):
    with pytest.raises(BridgeError):scope(env,value)
    assert call(env,'workspace_info')['write_scope']=='none'
    assert not write_allowed('src/main.py',scope=value)


def test_missing_stored_policy_fails_closed():
    assert Service.access_policy({})['write_scope']=='none'
    with pytest.raises(BridgeError):Service.access_policy({'write_scope':'bogus'})


@pytest.mark.parametrize('kind',['symlink_parent','symlink_target','hardlink','fifo'])
def test_workspace_writes_still_reject_links_and_special_files(env,kind):
    scope(env,'workspace')
    outside=env['tmp']/'outside';outside.mkdir();target=outside/'test.txt';target.write_text('keep')
    path=env['root']/'unsafe.txt'
    if kind=='symlink_parent':
        (env['root']/'unsafe').symlink_to(outside,target_is_directory=True);path=env['root']/'unsafe/test.txt'
    elif kind=='symlink_target':path.symlink_to(target)
    elif kind=='hardlink':os.link(target,path)
    else:os.mkfifo(path)
    rel=path.relative_to(env['root']).as_posix()
    with pytest.raises(BridgeError):edit(env,rel,'keep','no',digest(b'keep'))
    with pytest.raises(BridgeError):create(env,rel,'no')
    assert target.read_text()=='keep'


@pytest.mark.parametrize('mode',[0o644,0o755,0o640,0o4755])
def test_source_replacement_preserves_ordinary_permissions_not_special_bits(env,mode):
    scope(env,'workspace');p=env['root']/'src/main.py';p.chmod(mode)
    before=read(env,'src/main.py')
    edit(env,'src/main.py','return a + b','return a - b',before['sha256'])
    assert stat.S_IMODE(p.stat().st_mode)==mode & 0o777
    new=env['root']/'new.txt';create(env,'new.txt','private')
    assert stat.S_IMODE(new.stat().st_mode)==0o600


@pytest.mark.parametrize('content',['\x00no','sk-'+'A'*32,'x'*(256*1024+1)])
def test_workspace_new_content_is_still_validated_before_parents(env,content):
    scope(env,'workspace')
    with pytest.raises(BridgeError):create(env,'fresh/note.md',content)
    assert not (env['root']/'fresh').exists()


def test_workspace_stale_hash_and_unique_edit_are_enforced(env):
    scope(env,'workspace');p=env['root']/'src/main.py'
    old=read(env,'src/main.py');p.write_text('local local\n')
    with pytest.raises(BridgeError) as exc:
        edit(env,'src/main.py','local local','bad',old['sha256'])
    assert exc.value.code=='stale_evidence'
    with pytest.raises(BridgeError) as exc:
        edit(env,'src/main.py','local','bad',digest(p.read_bytes()))
    assert exc.value.code=='ambiguous_match' and p.read_text()=='local local\n'


def test_permissions_are_per_workspace_and_new_mappings_never_inherit(env):
    scope(env,'workspace');svc=env['service'];root=env['parent']/'beta';root.mkdir()
    ident=svc.add_workspace('Beta',str(root),[])['workspace']['id'];svc.manage_workspace(ident,'enable')
    assert svc.workspace(ident)['write_scope']=='none'
    with pytest.raises(BridgeError):create(env,'note.md','no',ws=ident)
    create(env,'note.md','alpha')
    assert not (root/'note.md').exists()


def test_queued_call_uses_current_policy_not_old_discovery(env):
    scope(env,'workspace');svc=env['service'];entered=Event()
    def queued():
        entered.set()
        return create(env,'queued.md','no')
    with ThreadPoolExecutor(max_workers=1) as pool:
        # The write waits in its workspace queue while the policy changes.
        with svc.workspace_lock(env['id']):
            future=pool.submit(queued);assert entered.wait(2)
            scope(env,'none')
        with pytest.raises(BridgeError):future.result(timeout=3)
    assert not (env['root']/'queued.md').exists()


async def test_only_admin_listener_and_admin_credential_can_set_policy(env):
    svc=env['service'];app=make_admin(svc);key=admin_cookie(app)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1:8766') as c:
        route='/api/workspaces/'+env['id'];body=dict(operation='set_write_scope',write_scope='workspace')
        assert (await c.post(route,json=body,headers={'Authorization':'Bearer '+env['token']})).status_code==401
        r=await c.post(route,json=body,headers={"Cookie": key})
        assert r.status_code==200 and r.json()['workspace']['write_scope']=='workspace'
        r=await c.post(route,json=dict(operation='set_write_scope',write_scope='all'),headers={"Cookie": key})
        assert r.status_code==400
    assert 'set_write_scope' not in TOOLS and 'manage_workspace' not in TOOLS
    with pytest.raises(BridgeError):call(env,'set_write_scope',write_scope='workspace')


def test_workspace_settings_save_is_atomic(env):
    service = env['service']
    ident = env['id']
    updated = service.manage_workspace(ident, 'set_settings', excludes=['private/**'], write_scope='none')['workspace']
    assert updated['write_scope'] == 'none'
    assert updated['excludes'] == '["private/**"]'
    with pytest.raises(BridgeError):
        service.manage_workspace(ident, 'set_settings', excludes=['x' * 121], write_scope='workspace')
    persisted = service.workspace(ident, False)
    assert persisted['write_scope'] == 'none'
    assert persisted['excludes'] == '["private/**"]'


@pytest.mark.parametrize('version',[LEGACY[-1],MODERN])
async def test_generic_tool_http_contract_and_no_permission_override(env,version):
    async with httpx.AsyncClient(transport=McpTransport(make_mcp(env['service'])),base_url='http://127.0.0.1:8765') as c:
        async def rpc(name,args):
            params={'name':name,'arguments':{'workspace_id':env['id'],**args}}
            headers={'X-Bridge-Token':env['token'],'Accept':'application/json','Mcp-Protocol-Version':version}
            if version==MODERN:
                params['_meta']={PREFIX+'protocolVersion':version,PREFIX+'clientCapabilities':{}}
                headers.update({'Mcp-Method':'tools/call','Mcp-Name':name})
            return (await c.post('/mcp',headers=headers,json={'jsonrpc':'2.0','id':1,'method':'tools/call','params':params})).json()
        def patch(*changes, **extra):
            return rpc('apply_patch', dict(changes=list(changes), message='Write the source note.', **extra))
        new = {'op': 'create', 'path': 'source.md', 'content': 'hello'}
        for field,value in [('write_scope','workspace'),('force',True),('allow_source',True)]:
            r=await patch(new, **{field:value})
            argument_error(r)
        assert (await patch(new))['result']['isError']
        scope(env,'workspace')
        result=(await patch(new))['result']
        assert result['isError'] is False
        sha=json.loads(result['content'][0]['text'])['files'][0]['sha256']
        assert not (await patch({'op':'edit','path':'source.md','sha256':sha,'old':'hello','new':'hi'}))['result']['isError']
        for old in ['write_file','edit_file','write_handoff_file','edit_handoff_file']:
            assert (await rpc(old,dict(path='source.md',content='bad')))['error']['code']==-32602
        assert (await c.post('/api/workspaces/'+env['id'],headers={'X-Bridge-Token':env['token']},json={'operation':'set_write_scope','write_scope':'workspace'})).status_code==404
