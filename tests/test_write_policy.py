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
from workspace_bridge.protocol import LEGACY, MODERN, PREFIX


def call(e, name, **args):
    return e['service'].call(e['id'], e['token'], name, args)


def scope(e, value):
    return e['service'].manage_workspace(e['id'], 'set_write_scope', write_scope=value)


def read(e, path):
    return call(e, 'read_file', path=path, start_line=1, max_lines=200, expected_sha256=None)


def test_generic_tools_replace_handoff_names_without_aliases(env):
    assert len(TOOLS) == 19
    assert {'read_file','write_file','edit_file'} <= TOOLS.keys()
    for name in ['write_handoff_file','edit_handoff_file','review_changes','read_change','record_audit']:
        assert name not in TOOLS
        with pytest.raises(BridgeError) as exc: call(env, name)
        assert exc.value.code == 'unknown_tool'
    for name in ['write_file','edit_file']:
        schema = TOOLS[name][0].model_json_schema()
        assert schema['additionalProperties'] is False
        assert 'workspace_id' in schema['required']
        assert 'write_scope' not in schema['properties'] and 'force' not in schema['properties']
        assert 'pattern' not in schema['properties']['path']  # Paths aren't tied to a handoff prefix.


@pytest.mark.parametrize('value', ['none','handoff','workspace'])
def test_scope_is_exposed_in_discovery_and_info_and_does_not_limit_reads(env, value):
    scope(env, value)
    info = call(env, 'workspace_info')
    assert info['write_scope'] == value and info['read_scope'] == 'workspace'
    assert info['writable_path_prefix'] == {'none':None,'handoff':HANDOFF+'/','workspace':''}[value]
    assert info['source_access'] == ('read_write' if value=='workspace' else 'read_only')
    listed = env['service'].call(None, env['token'], 'list_workspaces', {})
    assert listed['workspaces'][0]['write_scope'] == value
    assert read(env, 'src/main.py')['lines'][0]['text'].startswith('def add')


def test_default_stays_handoff_and_read_only_mode_has_no_prepare_bypass(env, payload):
    assert call(env, 'workspace_info')['write_scope'] == 'handoff'
    note = HANDOFF + '/plan.md'
    created = call(env,'write_file',path=note,content='keep\n')
    scope(env,'none')
    for name,args in [
        ('write_file',dict(path=note,content='replace\n',expected_sha256=created['sha256'])),
        ('edit_file',dict(path=note,old_text='keep',new_text='changed',expected_sha256=created['sha256'])),
        ('write_file',dict(path='new/plan.md',content='no')),
        ('prepare_handoff',payload),
    ]:
        with pytest.raises(BridgeError) as exc: call(env,name,**args)
        assert exc.value.code=='policy_denied'
    assert read(env,note)['sha256']==created['sha256']
    assert not (env['root']/'new').exists()
    assert not (env['root']/HANDOFF/'jobs').exists()
    assert not env['service'].db.execute('SELECT * FROM jobs').fetchall()


def test_workspace_enable_and_revoke_without_changing_tool_schema(env):
    schemas = {name: model.model_json_schema() for name,(model,*_) in TOOLS.items()}
    original = read(env,'src/main.py')
    with pytest.raises(BridgeError):
        call(env,'edit_file',path='src/main.py',old_text='return a + b',new_text='return a - b',expected_sha256=original['sha256'])
    scope(env,'workspace')
    result=call(env,'edit_file',path='src/main.py',old_text='return a + b',new_text='return a - b',expected_sha256=original['sha256'])
    assert result['write_scope']=='workspace'
    assert (env['root']/'src/main.py').read_text().endswith('return a - b\n')
    call(env,'write_file',path='docs/design.md',content='# Design\n')
    scope(env,'handoff')
    with pytest.raises(BridgeError):
        call(env,'write_file',path='src/main.py',content='no',expected_sha256=result['sha256'])
    call(env,'write_file',path=HANDOFF+'/allowed.md',content='allowed')
    assert schemas == {name: model.model_json_schema() for name,(model,*_) in TOOLS.items()}


@pytest.mark.parametrize('path', ['../other.md','/tmp/no.md','src/../../other.md','src//a.py','src/./a.py',
    'C:\\project\\a.py','.WORKSPACE-HANDOFF/plan.md', '.env','.git/config','secrets.json',
    'nested/.ssh/key','node_modules/test.js','.wb-write-fake','src/.WB-WRITE-fake','src/.wb-write-dir/a.md'])
def test_workspace_scope_still_denies_unsafe_or_reserved_paths(env,path):
    scope(env,'workspace')
    with pytest.raises(BridgeError):call(env,'write_file',path=path,content='no')


@pytest.mark.parametrize('rule',['src','src/**','*.py'])
def test_workspace_scope_respects_admin_exclusions(env,rule):
    scope(env,'workspace');before=(env['root']/'src/main.py').read_bytes()
    env['service'].manage_workspace(env['id'],'set_excludes',[rule])
    with pytest.raises(BridgeError):call(env,'write_file',path='src/main.py',content='no',expected_sha256=digest(before))
    assert (env['root']/'src/main.py').read_bytes()==before


@pytest.mark.parametrize('value',[None,'','all','read_write','WORKSPACE',False])
def test_invalid_policy_cannot_broaden_access(env,value):
    with pytest.raises(BridgeError):scope(env,value)
    assert call(env,'workspace_info')['write_scope']=='handoff'
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
    with pytest.raises(BridgeError):call(env,'write_file',path=rel,content='no',expected_sha256=digest(b'keep'))
    assert target.read_text()=='keep'


@pytest.mark.parametrize('mode',[0o644,0o755,0o640,0o4755])
def test_source_replacement_preserves_ordinary_permissions_not_special_bits(env,mode):
    scope(env,'workspace');p=env['root']/'src/main.py';p.chmod(mode)
    before=read(env,'src/main.py')
    call(env,'write_file',path='src/main.py',content='replaced\n',expected_sha256=before['sha256'])
    assert stat.S_IMODE(p.stat().st_mode)==mode & 0o777
    new=env['root']/'new.txt';call(env,'write_file',path='new.txt',content='private')
    assert stat.S_IMODE(new.stat().st_mode)==0o600


@pytest.mark.parametrize('content',['\x00no','sk-'+'A'*32,'x'*(256*1024+1)])
def test_workspace_new_content_is_still_validated_before_parents(env,content):
    scope(env,'workspace')
    with pytest.raises(BridgeError):call(env,'write_file',path='fresh/note.md',content=content)
    assert not (env['root']/'fresh').exists()


def test_workspace_stale_hash_and_unique_edit_are_enforced(env):
    scope(env,'workspace');p=env['root']/'src/main.py'
    old=read(env,'src/main.py');p.write_text('local local\n')
    with pytest.raises(BridgeError) as exc:
        call(env,'write_file',path='src/main.py',content='bad',expected_sha256=old['sha256'])
    assert exc.value.code=='stale_evidence'
    with pytest.raises(BridgeError) as exc:
        call(env,'edit_file',path='src/main.py',old_text='local',new_text='bad',expected_sha256=digest(p.read_bytes()))
    assert exc.value.code=='ambiguous_match' and p.read_text()=='local local\n'


def test_permissions_are_per_workspace_and_new_mappings_never_inherit(env):
    scope(env,'workspace');svc=env['service'];root=env['parent']/'beta';root.mkdir()
    ident=svc.add_workspace('Beta',str(root),[])['workspace']['id'];svc.manage_workspace(ident,'enable')
    assert svc.workspace(ident)['write_scope']=='handoff'
    with pytest.raises(BridgeError):svc.call(ident,env['token'],'write_file',dict(path='note.md',content='no'))
    call(env,'write_file',path='note.md',content='alpha')
    assert not (root/'note.md').exists()


def test_queued_call_uses_current_policy_not_old_discovery(env):
    scope(env,'workspace');svc=env['service'];entered=Event()
    def queued():
        entered.set()
        return call(env,'write_file',path='queued.md',content='no')
    with ThreadPoolExecutor(max_workers=1) as pool:
        with svc.lock:
            future=pool.submit(queued);assert entered.wait(2)
            scope(env,'handoff')
        with pytest.raises(BridgeError):future.result(timeout=3)
    assert not (env['root']/'queued.md').exists()


def test_old_database_migrates_to_handoff_and_preserves_jobs_credentials(env,payload):
    svc=env['service'];job=call(env,'prepare_handoff',**payload)
    before=svc.workspace(env['id']);svc.db.execute('ALTER TABLE workspaces DROP COLUMN write_scope');svc.db.commit()
    svc.close()
    restored=Service(env['state'],env['config'])
    try:
        current=restored.workspace(env['id']);assert current['write_scope']=='handoff'
        assert {k:v for k,v in current.items() if k!='write_scope'} == {k:v for k,v in before.items() if k!='write_scope'}
        restored.authenticate_bridge(env['token'])
        assert restored.job(current,job['id'])['state']=='prepared'
        assert {p.name for p in Path(job['path']).iterdir()}=={'TASK.md','CONTEXT.md','ACCEPTANCE.md'}
        assert restored.db.execute("SELECT name FROM sqlite_master WHERE name IN ('reviews','audits')").fetchall()==[]
        restored.manage_workspace(env['id'],'set_write_scope',write_scope='workspace')
    finally:restored.close()
    again=Service(env['state'],env['config'])
    try:assert again.workspace(env['id'])['write_scope']=='workspace'
    finally:again.close()


async def test_only_admin_listener_and_admin_credential_can_set_policy(env):
    svc=env['service'];key=(env['state']/'admin-token').read_text().strip()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=make_admin(svc,digest(key.encode()))),base_url='http://127.0.0.1:8766') as c:
        route='/api/workspaces/'+env['id'];body=dict(operation='set_write_scope',write_scope='workspace')
        assert (await c.post(route,json=body,headers={'Authorization':'Bearer '+env['token']})).status_code==401
        r=await c.post(route,json=body,headers={'Authorization':'Bearer '+key})
        assert r.status_code==200 and r.json()['workspace']['write_scope']=='workspace'
        r=await c.post(route,json=dict(operation='set_write_scope',write_scope='all'),headers={'Authorization':'Bearer '+key})
        assert r.status_code==400
    assert 'set_write_scope' not in TOOLS and 'manage_workspace' not in TOOLS
    with pytest.raises(BridgeError):call(env,'set_write_scope',write_scope='workspace')


@pytest.mark.parametrize('version',[LEGACY[-1],MODERN])
async def test_generic_tool_http_contract_and_no_permission_override(env,version):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=make_mcp(env['service'])),base_url='http://127.0.0.1:8765') as c:
        async def rpc(name,args):
            params={'name':name,'arguments':{'workspace_id':env['id'],**args}}
            headers={'X-Bridge-Token':env['token'],'Accept':'application/json','Mcp-Protocol-Version':version}
            if version==MODERN:
                params['_meta']={PREFIX+'protocolVersion':version,PREFIX+'clientCapabilities':{}}
                headers.update({'Mcp-Method':'tools/call','Mcp-Name':name})
            return (await c.post('/mcp',headers=headers,json={'jsonrpc':'2.0','id':1,'method':'tools/call','params':params})).json()
        for field,value in [('write_scope','workspace'),('force',True),('allow_source',True)]:
            r=await rpc('write_file',dict(path='source.md',content='bad',**{field:value}))
            assert r['error']['code']==-32602
        assert (await rpc('write_file',dict(path='source.md',content='bad')))['result']['isError']
        scope(env,'workspace')
        result=(await rpc('write_file',dict(path='source.md',content='hello')))['result']
        assert result['isError'] is False
        sha=json.loads(result['content'][0]['text'])['sha256']
        assert not (await rpc('edit_file',dict(path='source.md',old_text='hello',new_text='hi',expected_sha256=sha)))['result']['isError']
        for old in ['write_handoff_file','edit_handoff_file']:
            assert (await rpc(old,dict(path='source.md',content='bad')))['error']['code']==-32602
        assert (await c.post('/api/workspaces/'+env['id'],json={'operation':'set_write_scope','write_scope':'workspace'})).status_code==404
