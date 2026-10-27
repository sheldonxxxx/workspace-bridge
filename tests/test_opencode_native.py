"""Opt-in installed OpenCode v2 acceptance using only a local fake provider."""
import json
import os
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import time

import pytest
from workspace_bridge import security_profiles as sp
from workspace_bridge.opencode_host_adapter import OpenCodeHostAdapter

BINARY = os.environ.get('WB_OPENCODE_TEST_BINARY')
pytestmark = pytest.mark.skipif(not BINARY, reason='WB_OPENCODE_TEST_BINARY is required for installed v2 acceptance')


@pytest.mark.parametrize('mode', ['text', 'image', 'images4', 'deny', 'ask', 'queue', 'grep'])
def test_installed_v2_shared_database_plugin_and_local_provider(tmp_path, mode):
    requests = []
    provider_entered = threading.Event()
    provider_release = threading.Event()
    class Provider(BaseHTTPRequestHandler):
        def log_message(self,*_):pass
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            requests.append(payload)
            if mode=='queue' and len(requests)==1:
                provider_entered.set()
                provider_release.wait(5)
            self.send_response(200)
            self.send_header('Content-Type','text/event-stream')
            self.end_headers()
            chunks=[{'id':'fake-call','object':'chat.completion.chunk','created':1,'model':'fake',
                'choices':[{'index':0,'delta':{'role':'assistant','content':'Native v2 completed'},'finish_reason':None}]},
                {'id':'fake-call','object':'chat.completion.chunk','created':1,'model':'fake',
                 'choices':[{'index':0,'delta':{},'finish_reason':'stop'}],
                 'usage':{'prompt_tokens':5,'completion_tokens':7,'total_tokens':12}}]
            if mode in ('deny','ask','grep') and len(requests) == 1:
                chunks = [{'id':'fake-call','object':'chat.completion.chunk','created':1,'model':'fake',
                    'choices':[{'index':0,'delta':{'role':'assistant','tool_calls':[{'index':0, 'id':'write-call','type':'function',
                      'function':{'name':'grep' if mode=='grep' else 'write','arguments':json.dumps(
                          {'pattern':'.*'} if mode=='grep' else {'path':str(root/'native.txt'),'content':'Approved native edit'})}}]},'finish_reason':None}]},
                    {'id':'fake-call','object':'chat.completion.chunk','created':1,'model':'fake',
                     'choices':[{'index':0,'delta':{},'finish_reason':'tool_calls'}],
                     'usage':{'prompt_tokens':5,'completion_tokens':7,'total_tokens':12}}]
            for chunk in chunks:self.wfile.write(('data: '+json.dumps(chunk)+'\n\n').encode())
            self.wfile.write(b'data: [DONE]\n\n')
    server=ThreadingHTTPServer(('127.0.0.1',0),Provider)
    threading.Thread(target=server.serve_forever,daemon=True).start()
    root=tmp_path/'project';root.mkdir()
    if mode=='grep':(root/'.env').write_text('PRIVATE_GREP_FIXTURE=must-stay-private')
    overlay={'model':'fixture/fake','providers':{'fixture':{
        'name':'Fixture','package':'@ai-sdk/openai-compatible',
        'settings':{'baseURL':f'http://127.0.0.1:{server.server_port}/v1','apiKey':'test-only'},
        'models':{'fake':{'name':'Fake','capabilities':{'tools':True,'input':['text','image'],'output':['text']},
                         'limit':{'context':100000,'output':1000}}}}}}
    environment={'PATH':os.environ['PATH'],'HOME':str(tmp_path/'home'),
        'OPENCODE_DB':str(tmp_path/'native.db'),'OPENCODE_CONFIG_DIR':str(tmp_path/'config'),
        'OPENCODE_CONFIG_PROJECT_DISABLE':'1','OPENCODE_CONFIG_CONTENT':json.dumps(overlay),
        'XDG_DATA_HOME':str(tmp_path/'data'),'XDG_CONFIG_HOME':str(tmp_path/'config'),
        'XDG_CACHE_HOME':str(tmp_path/'cache'),'XDG_STATE_HOME':str(tmp_path/'state')}
    state=tmp_path/'adapter'
    adapter=OpenCodeHostAdapter(state,root,binary=BINARY,environment=environment)
    try:
        profile_id='reviewed' if mode=='ask' else 'read-only'
        security=sp.wire(profile_id,sp.BUILTINS[profile_id])
        conv=adapter.create_conversation({'workspaceId':'fixture','directory':str(root),
            'security':security})
        assert adapter.native.version.startswith('2.')
        assert str(root) in adapter.ready
        models=adapter.models('fixture')['models']
        assert any(m['selector']=='fixture/fake' and 'image' in m['inputModalities'] for m in models)
        items=[{'type':'text','text':'Reply with a short acknowledgement.'}]
        if mode in ('image','images4'):
            import base64
            from io import BytesIO
            from PIL import Image
            with BytesIO() as out:
                image=(Image.frombytes('RGB',(724,724),os.urandom(724*724*3)) if mode=='images4'
                       else Image.new('RGB',(16,16),'red'))
                image.save(out,'PNG')
                items += [{'type':'image','mimeType':'image/png','data':base64.b64encode(out.getvalue()).decode()} for _ in range(4 if mode=='images4' else 1)]
        approvals=[]
        run=adapter.start_run(conv['id'],{'clientRunId':'native-acceptance','model':'fixture/fake',
            'security':security,'input':items})
        if mode=='queue':
            assert provider_entered.wait(5)
            adapter.queue(run['id'],{'expectedRunId':run['id'],'requestId':'followup', 'input':[{'type':'text','text':'Follow up after this turn.'}]})
            provider_release.set()
        deadline=time.monotonic()+25
        while time.monotonic()<deadline:
            result=adapter.run(run['id'])
            for item in adapter.interactions(run['id'])['interactions']:
                if item['id'] not in approvals:
                    adapter.resolve(item['id'],{'choiceId':'approve'})
                    approvals.append(item['id'])
            if result['phase']=='terminal':break
            time.sleep(.1)
        assert result['outcome']=='succeeded', result
        assert 'Native v2 completed' in result['result']
        assert result['usage']['totalTokens']==(24 if mode in ('deny','ask','queue','grep') else 12)
        assert requests
        if mode in ('image','images4'):
            assert sum(part.get('type')=='image_url' for m in requests[0]['messages'] if isinstance(m.get('content'), list) for part in m['content'])==(4 if mode=='images4' else 1)
        if mode=='deny':
            assert not (root/'native.txt').exists()
            assert any(a['status']=='failed' for a in adapter.activities(run['id'])['activities'])
        if mode=='grep':
            assert 'must-stay-private' not in json.dumps(requests)
            assert any(a['status']=='failed' for a in adapter.activities(run['id'])['activities'])
        if mode=='queue':
            assert result['inputMessages']==[{'requestId':'followup','mode':'queue','status':'delivered'}]
        if mode=='ask':
            assert approvals
            assert (root/'native.txt').read_text()=='Approved native edit'
        assert adapter.native.env['OPENCODE_DB']==environment['OPENCODE_DB']
        # A native transcript survives an adapter process boundary in the SAME DB.
        adapter.close()
        adapter=OpenCodeHostAdapter(state,root,binary=BINARY,environment=environment)
        assert adapter.conversation(conv['id'])['nativeId']==conv['nativeId']
        assert adapter.run(run['id'])['outcome']=='succeeded'
        assert len(requests)==(2 if mode in ('deny','ask','queue','grep') else 1)
    finally:
        adapter.close();server.shutdown();server.server_close()
