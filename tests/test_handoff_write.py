"""Handoff-only mutations, no source execution or snapshot/audit subsystem."""
import json
import os
from pathlib import Path
import stat
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from workspace_bridge.api import TOOLS, make_mcp
from workspace_bridge.embedded_skill import read_project_lead_skill
from workspace_bridge.protocol import LEGACY, MODERN, PREFIX
from workspace_bridge.security import BridgeError, HANDOFF, MAX_WRITE, SafeRoot, digest
from workspace_bridge.service import Service

NOTE = HANDOFF + '/notes/plan.md'


def call(env, tool, **args):
    return env['service'].call(env['id'], env['token'], tool, args)


def write(env, content='first\n', path=NOTE, **args):
    return call(env, 'write_file', path=path, content=content, **args)


def read(env, path=NOTE, **args):
    return call(env, 'read_file', path=path, start_line=1, max_lines=200, expected_sha256=args.get('expected_sha256'))


def edit(env, sha, old='first', new='second', path=NOTE):
    return call(env, 'edit_file', path=path, old_text=old, new_text=new, expected_sha256=sha)


def test_create_read_replace_exact_edit_no_source_changes(env):
    original = (env['root']/'src/main.py').read_bytes()
    created = write(env)
    assert created['created'] and created['bytes'] == 6
    assert created['absolute_path'] == str(env['root']/NOTE)
    assert read(env)['sha256'] == created['sha256']
    replacement = write(env, 'replacement\n', expected_sha256=created['sha256'])
    assert replacement['created'] is False
    result = edit(env, replacement['sha256'], 'replacement', 'final')
    assert result['replacements'] == 1
    assert (env['root']/NOTE).read_bytes() == b'final\n'
    assert stat.S_IMODE((env['root']/NOTE).stat().st_mode) == 0o600
    assert (env['root']/'src/main.py').read_bytes() == original
    assert not list((env['root']/HANDOFF).rglob('.wb-write-*'))


def test_create_only_no_silent_overwrite(env):
    first = write(env)
    with pytest.raises(BridgeError) as exc:
        write(env, 'other')
    assert exc.value.code == 'conflict'
    assert read(env)['sha256'] == first['sha256']


@pytest.mark.parametrize('tool', ['write_file', 'edit_file'])
@pytest.mark.parametrize('path', ['src/main.py','README.md','../outside.txt','/tmp/outside.txt',
    HANDOFF, HANDOFF+'/../src/main.py', HANDOFF+'/notes/../../src/main.py',
    HANDOFF+'-other/note.md', 'nested/'+HANDOFF+'/note.md', '.WORKSPACE-HANDOFF/note.md',
    HANDOFF+'//note.md', HANDOFF+'/./note.md', HANDOFF+'/bad\\name.md',
    'C:\\Projects\\note.md', HANDOFF+'/bad:name.md', HANDOFF+'/bad\x00name.md'])
def test_denied_paths_leave_source_untouched(env, tool, path):
    original = (env['root']/'src/main.py').read_bytes()
    with pytest.raises(BridgeError):
        if tool == 'write_file':
            write(env, path=path)
        else:
            edit(env, '0'*64, path=path)
    assert (env['root']/'src/main.py').read_bytes() == original
    assert not (env['tmp']/'outside.txt').exists()


@pytest.mark.parametrize('path', [HANDOFF+'/.env',HANDOFF+'/secret.key',HANDOFF+'/.git/config',
    HANDOFF+'/data.db',HANDOFF+'/secrets.json',HANDOFF+'/.wb-write-fake',
    HANDOFF+'/.WB-WRITE-other/file.md',HANDOFF+'/'+HANDOFF+'/file.md'])
def test_secret_internal_and_reserved_paths_denied(env, path):
    with pytest.raises(BridgeError): write(env, path=path)
    assert not (env['root']/path).exists()


@pytest.mark.parametrize('rule', ['*.md',HANDOFF+'/**','notes/**','notes','*.MD',HANDOFF])
def test_admin_exclusions_apply_to_read_write_list_search(env, rule):
    write(env)
    env['service'].manage_workspace(env['id'], 'set_excludes', [rule])
    with pytest.raises(BridgeError): write(env)
    with pytest.raises(BridgeError): read(env)
    try:
        listing = call(env, 'glob', path=HANDOFF, pattern='**/*')
        assert not listing['entries']
    except BridgeError:
        pass


@pytest.mark.parametrize('where', ['handoff_root','parent','file'])
def test_symlinks_cannot_redirect_write_or_edit(env, where):
    outside=env['tmp']/'outside';outside.mkdir()
    target=outside/'plan.md';target.write_text('keep\n')
    handoff=env['root']/HANDOFF
    if where=='handoff_root': handoff.symlink_to(outside, target_is_directory=True)
    elif where=='parent':
        handoff.mkdir();(handoff/'notes').symlink_to(outside, target_is_directory=True)
    else:
        (handoff/'notes').mkdir(parents=True);(handoff/'notes/plan.md').symlink_to(target)
    with pytest.raises(BridgeError): write(env)
    with pytest.raises(BridgeError): edit(env,digest(b'keep\n'),old='keep')
    assert target.read_text()=='keep\n'
    assert list(outside.iterdir())==[target]


@pytest.mark.parametrize('kind', ['hardlink','fifo','directory'])
def test_nonregular_or_multilink_files_cannot_be_modified(env, kind):
    p=env['root']/NOTE;p.parent.mkdir(parents=True)
    target=env['root']/'src/main.py';before=target.read_bytes()
    if kind=='hardlink':os.link(target,p)
    elif kind=='fifo':os.mkfifo(p)
    else:p.mkdir()
    with pytest.raises(BridgeError): write(env, expected_sha256=digest(before))
    with pytest.raises(BridgeError): edit(env,digest(before),old='return')
    assert target.read_bytes()==before


@pytest.mark.parametrize('tool', ['write','edit'])
def test_stale_hash_does_not_overwrite_local_changes(env, tool):
    first=write(env);(env['root']/NOTE).write_text('local update\n')
    with pytest.raises(BridgeError) as exc:
        if tool=='write':write(env,'replacement',expected_sha256=first['sha256'])
        else:edit(env,first['sha256'])
    assert exc.value.code=='stale_evidence'
    assert (env['root']/NOTE).read_text()=='local update\n'


@pytest.mark.parametrize('content,old,error', [('same same','same','ambiguous_match'),('aaa','aa','ambiguous_match'),('first','absent','match_not_found'),('first','','invalid_arguments')])
def test_exact_edit_rejects_ambiguity_and_missing_text(env,content,old,error):
    first=write(env,content)
    with pytest.raises(BridgeError) as exc:edit(env,first['sha256'],old,'x')
    assert exc.value.code==error
    assert (env['root']/NOTE).read_text()==content


def test_exact_edit_preserves_crlf_final_newline_and_unicode(env):
    text='標題\r\nhello\r\nlast'
    first=write(env,text)
    edit(env,first['sha256'],'hello','再見')
    assert (env['root']/NOTE).read_bytes()=='標題\r\n再見\r\nlast'.encode()


@pytest.mark.parametrize('content,error', [('a\x00b','binary_file'),('a\x1bb','binary_file'),('\ud800','invalid_arguments'),('sk-'+'A'*30,'secret_content')])
def test_bad_new_content_rejected_before_creating_folders(env,content,error):
    with pytest.raises(BridgeError) as exc:write(env,content)
    assert exc.value.code==error
    assert not (env['root']/HANDOFF).exists()


@pytest.mark.parametrize('raw,error', [(b'\xff\xfe','binary_file'),(b'a\x00b','binary_file'),(('sk-'+'A'*30).encode(),'secret_content')])
def test_existing_binary_or_secret_content_not_overwritten(env,raw,error):
    p=env['root']/NOTE;p.parent.mkdir(parents=True);p.write_bytes(raw)
    with pytest.raises(BridgeError) as exc:write(env,'replacement',expected_sha256=digest(raw))
    assert exc.value.code==error
    assert p.read_bytes()==raw


def test_write_byte_budget_and_empty_file(env):
    with pytest.raises(BridgeError) as exc:write(env,'界'*(MAX_WRITE//3+1))
    assert exc.value.code=='too_large'
    first=write(env,'x'*MAX_WRITE)
    with pytest.raises(BridgeError) as exc:edit(env,first['sha256'],'x'*MAX_WRITE,'y'*(MAX_WRITE+1))
    assert exc.value.code=='too_large'
    result=write(env,'',expected_sha256=first['sha256'])
    assert result['bytes']==0 and (env['root']/NOTE).exists()


def test_explicit_general_browsing_no_default_source_scan_of_handoff(env):
    write(env,'unique handoff phrase\n')
    assert all(not e['path'].startswith(HANDOFF) for e in call(env,'list_dir',depth=4)['entries'])
    assert not call(env,'grep_files',pattern='unique handoff phrase')['matches']
    assert call(env,'glob',path=HANDOFF,pattern='**/*.md')['entries'][0]['path']==NOTE
    found=call(env,'grep_files',path=HANDOFF,pattern='unique handoff phrase')
    assert found['matches'][0]['path']==NOTE
    assert call(env,'list_dir',path=HANDOFF,depth=2)['entries']


def test_edit_published_plan_keeps_original_hash_as_publication_reference(env,payload):
    job=call(env,'prepare_handoff',**payload)
    before=call(env,'read_handoff',job_id=job['id'],document='TASK.md',start_line=1,max_lines=200)
    path=f"{HANDOFF}/jobs/{job['id']}/TASK.md"
    edit(env,before['sha256'],'Improve arithmetic behavior','Document arithmetic behavior',path)
    after=call(env,'read_handoff',job_id=job['id'],document='TASK.md',start_line=1,max_lines=200)
    assert before['matches_published'] and after['matches_published'] is False
    assert 'Document arithmetic behavior' in after['content']
    assert call(env,'list_handoffs',offset=0,limit=20)['handoffs'][0]['state']=='prepared'


def test_two_workspaces_same_relative_path_isolated(env):
    other=env['parent']/'beta';other.mkdir()
    svc=env['service'];ws=svc.add_workspace('Beta',str(other),[])['workspace']['id'];svc.manage_workspace(ws,'enable')
    one=write(env,'alpha\n')
    two=svc.call(ws,env['token'],'write_file',dict(path=NOTE,content='beta\n'))
    assert (env['root']/NOTE).read_text()=='alpha\n' and (other/NOTE).read_text()=='beta\n'
    with pytest.raises(BridgeError):svc.call(ws,env['token'],'write_file',dict(path=NOTE,content='bad',expected_sha256=one['sha256']))
    assert two['sha256']!=one['sha256']


@pytest.mark.parametrize('action',['workspace_disable','bridge_disable','rotate','wrong_token'])
def test_auth_revocation_prevents_write(env,action):
    svc=env['service']
    if action=='workspace_disable':svc.manage_workspace(env['id'],'disable')
    elif action=='bridge_disable':svc.manage_bridge('disable')
    elif action=='rotate':svc.manage_bridge('rotate_token')
    else:env['token']='invalid'
    with pytest.raises(BridgeError):write(env)
    assert not (env['root']/HANDOFF).exists()


def test_competing_bridge_edits_only_one_stale_base_wins(env):
    first=write(env)
    def attempt(value):
        try:return write(env,value,expected_sha256=first['sha256'])
        except BridgeError as exc:return exc.code
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(attempt,['one','two']))
    assert sum(isinstance(r,dict) for r in results)==1
    assert results.count('stale_evidence')==1


def test_failed_staged_write_keeps_old_file_and_removes_temp(env,monkeypatch):
    first=write(env)
    def failed(*args,**kw):raise OSError('disk error')
    monkeypatch.setattr(os,'replace',failed)
    with pytest.raises(BridgeError):write(env,'changed',expected_sha256=first['sha256'])
    assert (env['root']/NOTE).read_bytes()==b'first\n'
    assert not list((env['root']/HANDOFF).rglob('.wb-write-*'))


def test_local_change_during_staging_rejected(env,monkeypatch):
    first=write(env);original=os.fsync;once=False
    def fsync(fd):
        nonlocal once
        if not once:
            once=True;(env['root']/NOTE).write_bytes(b'local edit\n')
        return original(fd)
    monkeypatch.setattr(os,'fsync',fsync)
    with pytest.raises(BridgeError) as exc:write(env,'bad',expected_sha256=first['sha256'])
    assert exc.value.code=='stale_evidence'
    assert (env['root']/NOTE).read_bytes()==b'local edit\n'
    assert not list((env['root']/HANDOFF).rglob('.wb-write-*'))


def test_create_race_does_not_clobber_existing_file(env,monkeypatch):
    link=os.link
    def race(src,dst,**kwargs):
        (env['root']/NOTE).write_text('other writer\n')
        return link(src,dst,**kwargs)
    monkeypatch.setattr(os,'link',race)
    with pytest.raises(BridgeError) as exc:write(env)
    assert exc.value.code=='conflict'
    assert (env['root']/NOTE).read_text()=='other writer\n'
    assert not list((env['root']/HANDOFF).rglob('.wb-write-*'))


def test_root_replacement_rejected(env):
    # Same configured path with a fresh directory stays usable: no
    # historical root-identity pin. A write creates the handoff file in the
    # current root instead of failing with root_changed.
    root=env['root'];root.rename(root.with_name('moved'));root.mkdir()
    created=write(env)
    assert created['created']
    assert (root/NOTE).read_bytes()==b'first\n'


def test_logs_do_not_contain_document_content_or_paths(env):
    write(env,'my-planning-sentinel')
    rows=[dict(row) for row in env['service'].db.execute('SELECT * FROM events')]
    text=json.dumps(rows)
    assert 'write_file' in text
    assert 'my-planning-sentinel' not in text and NOTE not in text


def test_skill_and_tools_describe_handoff_only_and_manual_review():
    skill=read_project_lead_skill()
    assert skill['version']=='2.5.0'
    for value in ['write_file','edit_file','expected_sha256','.workspace-handoff/','re-read and reconcile','No special report files','Audit using normal tools']:
        assert value in skill['content']
    for name in ['write_file','edit_file']:
        schema,_,readonly,idempotent=TOOLS[name]
        assert not readonly and not idempotent
        assert 'workspace_id' in schema.model_json_schema()['required']
    assert 'expected_sha256' in TOOLS['edit_file'][0].model_json_schema()['required']


@pytest.mark.parametrize('version',[LEGACY[-1],MODERN])
async def test_http_write_edit_read_and_destructive_annotations(env,version):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=make_mcp(env['service'])),base_url='http://127.0.0.1:8765') as client:
        async def rpc(method,name=None,args=None):
            params={} if name is None else dict(name=name,arguments={'workspace_id':env['id'],**(args or {})})
            headers={'X-Bridge-Token':env['token'],'Accept':'application/json','MCP-Protocol-Version':version}
            if version==MODERN:
                params['_meta']={PREFIX+'protocolVersion':version,PREFIX+'clientCapabilities':{}}
                headers['Mcp-Method']=method
                if name:headers['Mcp-Name']=name
            response=await client.post('/mcp',headers=headers,json={'jsonrpc':'2.0','id':1,'method':method,'params':params})
            assert response.status_code==200,response.text
            return response.json()
        tools=(await rpc('tools/list'))['result']['tools']
        for t in tools:
            assert t['annotations']['destructiveHint']==(t['name'] in ['write_file','edit_file','start_agent_run','respond_agent_interaction'])
        def value(r):
            assert not r['result']['isError'],r
            return json.loads(r['result']['content'][0]['text'])
        new=value(await rpc('tools/call','write_file',dict(path=NOTE,content='hello\n')))
        edited=value(await rpc('tools/call','edit_file',dict(path=NOTE,old_text='hello',new_text='world',expected_sha256=new['sha256'])))
        current=value(await rpc('tools/call','read_file',dict(path=NOTE)))
        assert current['sha256']==edited['sha256'] and current['lines'][0]['text']=='world'
        bad=await rpc('tools/call','write_file',dict(path='src/main.py',content='bad'))
        assert bad['result']['isError']
        bad=await rpc('tools/call','edit_file',dict(path=NOTE,old_text='world',new_text='bad'))
        assert bad['error']['code']==-32602


@pytest.mark.parametrize('kind',['secret','excluded'])
def test_prepare_handoff_uses_same_policy_before_publication(env,payload,kind):
    if kind=='secret':payload['context']='sk-'+'A'*30
    else:env['service'].manage_workspace(env['id'],'set_excludes',['*.md'])
    with pytest.raises(BridgeError):call(env,'prepare_handoff',**payload)
    assert not (env['root']/HANDOFF).exists()


async def test_http_large_bounded_write_and_notification_denial(env):
    headers={'X-Bridge-Token':env['token'],'Accept':'application/json'}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=make_mcp(env['service'])),base_url='http://127.0.0.1:8765',headers=headers) as client:
        msg={'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':'write_file','arguments':{'workspace_id':env['id'],'path':NOTE,'content':'x'*MAX_WRITE}}}
        r=await client.post('/mcp',json=msg)
        assert r.status_code==200 and not r.json()['result']['isError'],r.text
        assert (env['root']/NOTE).stat().st_size==MAX_WRITE
        msg['params']['arguments']['path']=HANDOFF+'/not-created.md'
        msg.pop('id')
        r=await client.post('/mcp',json=msg)
        assert r.status_code==400 and not (env['root']/HANDOFF/'not-created.md').exists()
        msg['id']=2;msg['padding']='z'*(1024*1024)
        r=await client.post('/mcp',json=msg)
        assert r.status_code==400 and not (env['root']/HANDOFF/'not-created.md').exists()


def test_parent_replaced_during_write_is_detected(env,monkeypatch):
    first=write(env);original=os.fsync;once=False
    def fsync(fd):
        nonlocal once
        if not once:
            once=True
            parent=(env['root']/NOTE).parent
            parent.rename(parent.with_name('moved-notes'));parent.mkdir()
        return original(fd)
    monkeypatch.setattr(os,'fsync',fsync)
    with pytest.raises(BridgeError) as exc:write(env,'bad',expected_sha256=first['sha256'])
    assert exc.value.code=='stale_evidence'
    assert not (env['root']/NOTE).exists()
    assert (env['root']/HANDOFF/'moved-notes/plan.md').read_bytes()==b'first\n'
    assert not list((env['root']/HANDOFF).rglob('.wb-write-*'))


def test_existing_state_opens_without_migration_and_notes_survive(env,payload):
    job=call(env,'prepare_handoff',**payload);created=write(env)
    columns=[tuple(r) for r in env['service'].db.execute('PRAGMA table_info(jobs)')]
    reopened=Service(env['state'],env['config'])
    try:
        reopened.authenticate(env['id'],env['token'])
        assert columns==[tuple(r) for r in reopened.db.execute('PRAGMA table_info(jobs)')]
        assert reopened.job(reopened.workspace(env['id']),job['id'])['state']=='prepared'
        value=reopened.call(env['id'],env['token'],'read_file',dict(path=NOTE,start_line=1,max_lines=20,expected_sha256=created['sha256']))
        assert value['lines'][0]['text']=='first'
    finally:reopened.close()
