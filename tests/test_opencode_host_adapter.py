"""OpenCode native ownership, closed permissions and private WBRP contract."""
import json
from pathlib import Path
import threading
import time

import pytest

from workspace_bridge import security_profiles as sp
from workspace_bridge.opencode_host_adapter import OpenCodeHostAdapter, tool_decision
from workspace_bridge.plugin_host import invoke
from workspace_bridge.claude_host_adapter import AdapterFailure
from workspace_bridge.wbrp import validate_run_state, Descriptor


class FakeNative:
    version = '2.0.27'
    def __init__(self):
        self.sessions = {}
        self.messages = {}
        self.logs = {}
        self.inbox = {}
        self.calls = []
        self.queries = []
        self.active = {}
        self.permissions = []
        self.forms = []
        self.fail_prompt = False
        self.adapter = None
        # Report ready as the real plugin does: from its own thread, through
        # the adapter's policy server.
        self.ready_via_policy = False
    def start(self):
        pass
    def close(self):
        pass
    def request(self, method, path, body=None, *, query=None, sse=False):
        self.calls.append((method, path, body))
        self.queries.append((path, query))
        if path == '/api/location' or path == '/api/agent':
            if self.adapter and self.ready_via_policy:
                threading.Thread(target=self.adapter._policy, args=(
                    '/ready', {'directory': query['location[directory]']}), daemon=True).start()
            elif self.adapter:
                self.adapter.ready.add(query['location[directory]'])
            return {}
        if path == '/api/plugin':
            return {'data': [{'id':'workspace-bridge.permissions.v1','state':{'status':'active'}}]}
        if path == '/api/model':
            return {'data':[{'id':'fake', 'providerID':'local', 'name':'Fake',
                'capabilities':{'input':['text','image']}, 'variants':[{'id':'high'}]}]}
        if path == '/api/model/default':
            return {'data':{'id':'fake','providerID':'local'}}
        if path == '/api/session/active':
            return {'data': self.active}
        if path == '/api/session' and method == 'POST':
            sid=body['id']; self.sessions[sid]={**body};self.messages[sid]=[];self.logs[sid]=[];self.inbox[sid]=[]
            return {'data': self.sessions[sid]}
        if '/log' in path:
            sid=path.split('/')[-2]
            return [r for r in self.logs[sid] if r['seq'] > query['after']]
        parts=path.split('/')
        sid=parts[3]
        if sid not in self.sessions:
            raise AdapterFailure('Not found',404,'not_found')
        if len(parts)==4:
            return {'data': self.sessions[sid]}
        tail=parts[4]
        if tail=='model':
            return {}
        if tail=='prompt':
            self.active[sid]={'type':'running'}
            self.messages[sid].append({'id':body['id'],'type':'user','text':body['text']})
            self.logs[sid].append({'seq':len(self.logs[sid])+1,'type':'session.inbox.delivered','data':{'inboxID':body['id']}})
            if self.fail_prompt:
                raise AdapterFailure('Ambiguous',503,'runtime_unavailable')
            return {'data': {'id':body['id']}}
        if tail=='message':
            if len(parts)>5:
                found=next((r for r in self.messages[sid] if r['id']==parts[5]),None)
                if found is None:raise AdapterFailure('Not found',404,'not_found')
                return {'data': found}
            return {'data':list(reversed(self.messages[sid]))}
        if tail=='inbox':
            if method=='DELETE':
                self.inbox[sid]=[r for r in self.inbox[sid] if r['id']!=parts[5]]
                return {}
            return {'data': self.inbox[sid]}
        if tail=='interrupt':
            self.active.pop(sid,None)
            return {'interrupted':True}
        if tail=='permission':
            if method=='POST':self.permissions=[p for p in self.permissions if p['id']!=parts[5]]
            return {'data': self.permissions} if method=='GET' else {}
        if tail=='form':
            if method=='POST':self.forms=[f for f in self.forms if f['id']!=parts[5]]
            return {'data': self.forms} if method=='GET' else {}
        raise AssertionError((method,path))
    def complete(self,sid):
        self.messages[sid].append({'id':'msg_answer','type':'assistant','content':[
            {'type':'text','text':'Completed safely'}, {'type':'tool','id':'tool1','name':'read',
             'state':{'status':'completed'}}], 'tokens':{'input':5,'output':7,'reasoning':2,'cache':{'read':3,'write':1}}})
        self.logs[sid].append({'seq':len(self.logs[sid])+1,'type':'session.execution.succeeded','data':{'sessionID':sid}})
        self.messages[sid].append({'id':'msg_idle','type':'idle','outcome':'succeeded'})
        self.active.pop(sid,None)


@pytest.fixture
def adapter(tmp_path):
    root=tmp_path/'work';root.mkdir()
    native=FakeNative()
    a=OpenCodeHostAdapter(tmp_path/'state',root,transport=native)
    native.adapter=a
    yield a,native,root
    a.close()


READ_ONLY=sp.wire('read-only',sp.BUILTINS['read-only'])
REVIEWED=sp.wire('reviewed',sp.BUILTINS['reviewed'])


def conversation(a,root,security=READ_ONLY):
    return a.create_conversation({'workspaceId':'workspace','directory':str(root),'security':security})


def start(a,conv,security=READ_ONLY,client_run='request'):
    return a.start_run(conv['id'],{'clientRunId':client_run,'security':security,
        'input':[{'type':'text','text':'Run this'}]})


def wait_terminal(a,ident):
    for _ in range(100):
        r=a.run(ident)
        if r['phase']=='terminal':return r
        time.sleep(.02)
    raise AssertionError('Run did not finish')


def test_native_completion_activities_and_usage(adapter):
    a,native,root=adapter
    conv=conversation(a,root)
    run=start(a,conv)
    native.complete(conv['nativeId'])
    result=wait_terminal(a,run['id'])
    assert validate_run_state(result)['outcome']=='succeeded'
    assert result['result']=='Completed safely'
    assert result['usage']['totalTokens']==18
    assert len(a.activities(run['id'])['activities'])==1
    assert Descriptor.parse(a.descriptor(),expected_runtime='opencode').supports('imageInput')


def test_lost_submission_is_not_repeated_and_changed_request_fails(adapter):
    a,native,root=adapter
    conv=conversation(a,root);native.fail_prompt=True
    run=start(a,conv)
    assert start(a,conv)['id']==run['id']
    assert a.find_run(conv['id'],'request')['id']==run['id']
    assert sum(p.endswith('/prompt') for _,p,_ in native.calls)==1
    with pytest.raises(AdapterFailure,match='different input'):
        a.start_run(conv['id'],{'clientRunId':'request','security':READ_ONLY,'input':[{'type':'text','text':'Different'}]})


def test_private_credentials_are_removed_before_result_storage(adapter):
    a,native,root=adapter
    native.password='private-native-fixture-credential'
    conv=conversation(a,root);run=start(a,conv)
    native.complete(conv['nativeId'])
    next(m for m in native.messages[conv['nativeId']] if m['type']=='assistant')['content'][0]['text']=native.password+' '+a._policy_token
    result=wait_terminal(a,run['id'])
    stored=a._run(run['id'])['result']
    assert native.password not in stored and a._policy_token not in stored
    assert result['result']=='[REDACTED] [REDACTED]'


def test_missing_binary_releases_adapter_state_lock(tmp_path):
    root=tmp_path/'work';root.mkdir()
    state=tmp_path/'adapter'
    with pytest.raises(AdapterFailure,match='could not start'):
        OpenCodeHostAdapter(state,root,binary=str(tmp_path/'missing-opencode'))
    adapter=OpenCodeHostAdapter(state,root,transport=FakeNative())
    adapter.close()


def test_ownership_change_blocks_cancel_and_handoff_requires_idle(adapter):
    a,native,root=adapter
    conv=conversation(a,root);run=start(a,conv)
    with pytest.raises(AdapterFailure,match='idle'):
        a.handoff(conv['id'])
    native.sessions[conv['nativeId']]['metadata']={}
    with pytest.raises(AdapterFailure,match='ownership'):
        a.cancel(run['id'])
    assert not any(p.endswith('/interrupt') for _,p,_ in native.calls)


def test_native_handoff_requires_an_idle_reclaim(adapter):
    a,native,root=adapter
    conv=conversation(a,root)
    a.handoff(conv['id'])
    with pytest.raises(AdapterFailure,match='reclaim'):
        start(a,conv)
    assert a.reclaim(conv['id'])['ownership']=='bridge'
    assert start(a,conv)['phase']=='active'


def test_each_run_carries_its_profile_and_preserves_run_evidence(adapter):
    a,native,root=adapter
    first=sp.wire('custom',{**sp.BUILTINS['read-only'],'protected':['secrets/']})
    conv=conversation(a,root,first)
    run=start(a,conv,first);native.complete(conv['nativeId']);wait_terminal(a,run['id'])
    a.handoff(conv['id']);a.reclaim(conv['id'])
    second=sp.wire('custom',{**sp.BUILTINS['reviewed'],'protected':['secrets/']})
    later=start(a,conv,second,'second')
    assert later['securityBinding']['profile']['revision']==second['profile']['revision']
    assert a.conversation(conv['id'])['securityBinding']['profile']['revision']==second['profile']['revision']
    assert a.run(run['id'])['securityBinding']['profile']['revision']==first['profile']['revision']
    with pytest.raises(AdapterFailure):
        a.start_run(conv['id'],{'clientRunId':'third','security':{'source':'native'},
            'input':[{'type':'text','text':'x'}]})


@pytest.mark.parametrize('pending', [False,True])
def test_shared_database_unfinished_work_blocks_admission_and_handoff(adapter,pending):
    a,native,root=adapter;conv=conversation(a,root)
    if pending:native.inbox[conv['nativeId']]=[{'id':'msg_external'}]
    else:native.messages[conv['nativeId']]=[{'id':'msg_external','type':'user'}]
    assert not native.active
    assert a.conversation(conv['id'])['status']=='active'
    with pytest.raises(AdapterFailure,match='busy'):start(a,conv)
    with pytest.raises(AdapterFailure,match='busy'):a.reclaim(conv['id'])
    with pytest.raises(AdapterFailure,match='idle'):a.handoff(conv['id'])
    assert not any(p.endswith('/prompt') for _,p,_ in native.calls)


def test_input_receipts_do_not_contain_text(adapter):
    a,native,root=adapter
    conv=conversation(a,root);run=start(a,conv)
    body={'expectedRunId':run['id'],'requestId':'steer-1','input':[{'type':'text','text':'private instruction'}]}
    result=a.steer(run['id'],body)
    assert a.steer(run['id'],body)['inputMessages'][0]['requestId']==result['inputMessages'][0]['requestId']
    assert sum(p.endswith('/prompt') for _,p,_ in native.calls)==2
    assert 'private instruction' not in json.dumps(result)
    native.complete(conv['nativeId'])
    assert wait_terminal(a,run['id'])['inputMessages'][0]['status']=='delivered'


def test_native_forms_convert_wbrp_answers_and_remain_pending_on_invalid_value(adapter):
    a,native,root=adapter;conv=conversation(a,root);run=start(a,conv)
    native.forms=[{'id':'frm_fixture','title':'Parameters','fields':[
        {'key':'name','title':'Display name','type':'string','required':True},
        {'key':'count','type':'integer'}, {'key':'size','type':'number'},
        {'key':'enabled','type':'boolean'}, {'key':'tags','type':'multiselect','options':[]},
        {'key':'optional','type':'string'}]}]
    a._native_interactions(a._run(run['id']),a._conversation(conv['id']))
    item=a.interactions(run['id'])['interactions'][0]
    assert item['fields'][0]['question']=='Display name'
    answers={k:{'answers':[v]} for k,v in {'name':'Example','count':'3','size':'NaN',
        'enabled':'true','tags':'["a", "b"]'}.items()}
    with pytest.raises(AdapterFailure,match='Invalid form value'):
        a.resolve(item['id'],{'answers':answers})
    assert a.interactions(run['id'])['interactions'][0]['state']=='pending'
    answers['size']={'answers':['1.5']}
    a.resolve(item['id'],{'answers':answers})
    reply=next(body for method,path,body in native.calls if method=='POST' and path.endswith('/frm_fixture/reply'))
    assert reply=={'answer':{'name':'Example','count':3,'size':1.5,'enabled':True,'tags':['a','b']}}
    with pytest.raises(AdapterFailure,match='stale'):a.resolve(item['id'],{'answers':answers})


def test_native_permission_is_answered_once_without_saving_a_grant(adapter):
    a,native,root=adapter;conv=conversation(a,root);run=start(a,conv)
    native.permissions=[{'id':'per_fixture','action':'read','resources':['safe.txt']}]
    a._native_interactions(a._run(run['id']),a._conversation(conv['id']))
    item=a.interactions(run['id'])['interactions'][0]
    a.resolve(item['id'],{'choiceId':'approve'})
    assert next(body for method,path,body in native.calls if method=='POST' and path.endswith('/per_fixture/reply'))=={'decision':'once'}


def test_plugin_descriptor_and_permission_plugin_readiness(adapter):
    a,native,root=adapter
    assert invoke(a,'descriptor',{})[1]['runtime']['id']=='opencode'
    a.ready.clear();native.adapter=None
    with pytest.raises(AdapterFailure,match='plugin'):
        a._activate(str(root))
    assert not native.sessions


@pytest.mark.parametrize('tool,args',[
    ('read',{'filePath':'../secret'}),('read',{'filePath':'.git/config'}),
    ('write',{'filePath':'opencode.json'}),('write',{'filePath':'.opencode/plugin.mjs'}),
    ('read',{'filePath':'.env'}),('unknown',{}),('bash',{'command':'pwd'}),
    ('grep',{'pattern':'.*'}),('grep',{'path':'.','pattern':'.*'}),
    ('grep',{'path':'.env','pattern':'.*'}),('execute',{'code':'fetch("https://example.com")'}),
    ('mcp__unregistered',{}),
])
def test_read_only_closed_guard(adapter,tool,args):
    _,_,root=adapter
    assert tool_decision(sp.BUILTINS['read-only'],str(root),tool,args)=='deny'


def test_symlink_and_patch_configuration_guard(adapter):
    _,_,root=adapter
    (root/'outside').symlink_to(root.parent)
    config={**sp.BUILTINS['reviewed'],'read_outside':'deny'}
    assert tool_decision(config,str(root),'read',{'filePath':'outside/secret'})=='deny'
    assert tool_decision(config,str(root),'apply_patch',{'patchText':'*** Begin Patch\n*** Add File: .opencode/evil.mjs\n+x\n*** End Patch'})=='deny'
    assert tool_decision(config,str(root),'write',{'filePath':'safe.txt'})=='allow'
    (root/'safe.txt').write_text('searchable')
    assert tool_decision(config,str(root),'grep',{'path':'safe.txt','pattern':'searchable'})=='allow'


def test_descriptor_fails_closed_when_owned_native_server_is_lost(adapter):
    a,native,_=adapter
    native.is_alive=lambda:False
    with pytest.raises(AdapterFailure,match='unavailable'):a.descriptor()


def test_restart_reconciles_completion_without_resubmitting(tmp_path):
    root=tmp_path/'work';root.mkdir();native=FakeNative()
    state=tmp_path/'state'
    a=OpenCodeHostAdapter(state,root,transport=native);native.adapter=a
    conv=conversation(a,root);run=start(a,conv)
    native.complete(conv['nativeId']);a.close()
    b=OpenCodeHostAdapter(state,root,transport=native);native.adapter=b
    try:
        assert b.run(run['id'])['outcome']=='succeeded'
        assert sum(p.endswith('/prompt') for _,p,_ in native.calls)==1
    finally:b.close()


def test_a_continuation_after_restart_waits_for_the_plugin_without_holding_the_lock(tmp_path):
    root=tmp_path/'work';root.mkdir();native=FakeNative()
    state=tmp_path/'state'
    a=OpenCodeHostAdapter(state,root,transport=native);native.adapter=a
    conv=conversation(a,root);run=start(a,conv)
    native.complete(conv['nativeId']);wait_terminal(a,run['id']);a.close()
    b=OpenCodeHostAdapter(state,root,transport=native);native.adapter=b
    native.ready_via_policy=True
    try:
        began=time.monotonic()
        second=start(b,conv,client_run='after-restart')
        assert time.monotonic()-began<5 and second['phase']!='terminal'
        catalog=[q for p,q in native.queries if p=='/api/model']
        assert catalog[-1]=={'location[directory]':str(root)}
    finally:b.close()


def test_a_failed_run_carries_opencodes_own_message(adapter):
    a,native,root=adapter
    conv=conversation(a,root);run=start(a,conv);sid=conv['nativeId']
    native.messages[sid].append({'id':'msg_idle','type':'idle','outcome':'failed','error':{
        'type':'provider.free-limit','message':'Free usage limit reached.',
        'response':{'body':'provider detail that stays out'}}})
    native.logs[sid].append({'seq':len(native.logs[sid])+1,'type':'session.execution.failed','data':{}})
    native.active.pop(sid,None)
    result=wait_terminal(a,run['id'])
    assert result['outcome']=='failed' and result['error']=='Free usage limit reached.'


def test_restart_discards_pending_owned_input_and_stales_interactions(tmp_path):
    root=tmp_path/'work';root.mkdir();native=FakeNative();state=tmp_path/'state'
    a=OpenCodeHostAdapter(state,root,transport=native);native.adapter=a
    conv=conversation(a,root);run=start(a,conv)
    a.queue(run['id'],{'expectedRunId':run['id'],'requestId':'q1','input':[{'type':'text','text':'later'}]})
    message=a.db.execute('SELECT message FROM input_messages').fetchone()['message']
    native.inbox[conv['nativeId']]=[{'id':message}]
    # Remove receipt's projected delivery to simulate an undelivered native item.
    native.messages[conv['nativeId']]=native.messages[conv['nativeId']][:1]
    native.logs[conv['nativeId']]=native.logs[conv['nativeId']][:1]
    a.close()
    b=OpenCodeHostAdapter(state,root,transport=native);native.adapter=b
    try:
        assert b.run(run['id'])['outcome']=='interrupted'
        assert native.inbox[conv['nativeId']]==[]
        assert b.interactions(run['id'])['interactions']==[]
        assert sum(p.endswith('/prompt') for _,p,_ in native.calls)==2
    finally:b.close()


def test_external_native_input_relinquishes_without_interrupt(adapter):
    a,native,root=adapter;conv=conversation(a,root);run=start(a,conv)
    native.logs[conv['nativeId']].append({'seq':2,'type':'session.inbox.enqueued','data':{'inboxID':'msg_human'}})
    assert wait_terminal(a,run['id'])['outcome']=='orphaned'
    assert a._conversation(conv['id'])['owner']=='human'
    assert not any(p.endswith('/interrupt') for _,p,_ in native.calls)


def test_private_policy_approval_is_live_once_and_stales_on_cancel(adapter):
    a,native,root=adapter
    conv=conversation(a,root,REVIEWED)
    run=start(a,conv,REVIEWED);answers=[]
    def check():
        answers.append(a._policy('/tool',{'directory':str(root),'sessions':[conv['nativeId']],
            'tool':'bash','input':{'command':'make'},'nativeId':'tool'}))
    thread=threading.Thread(target=check);thread.start()
    for _ in range(100):
        pending=a.interactions(run['id'])['interactions']
        if pending:break
        time.sleep(.01)
    assert pending
    a.resolve(pending[0]['id'],{'choiceId':'approve'})
    thread.join(timeout=2)
    assert answers==[{'effect':'allow'}]
    with pytest.raises(AdapterFailure,match='stale'):a.resolve(pending[0]['id'],{'choiceId':'approve'})
    a.cancel(run['id'])
