#!/usr/bin/env python3
"""Creates two temporary synthetic projects, starts a local server and cleans up.
Optional UI component checks use real assets and a clearly labeled fetch relay.
No tunnel, model or coding agent is invoked.
"""
from __future__ import annotations
import argparse, json, os, pathlib, subprocess, sys, tempfile, time, socket
import httpx

parser = argparse.ArgumentParser(description='Synthetic loopback integration validation; never reads your projects.')
parser.add_argument('--ui-components', action='store_true', help='Also test browser components via an explicit HTTP relay (not normal-browser E2E). Requires playwright.')
parser.add_argument('--chromium', help='Optional browser executable path for the component check.')
args = parser.parse_args()
PROJECT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0,str(PROJECT))
from workspace_bridge.cli import initialize
from workspace_bridge.service import Service
from workspace_bridge import __version__
if args.ui_components:
    from playwright.sync_api import sync_playwright

logs=[]
def passed(s):
    logs.append('PASS: '+s)
    print(logs[-1],flush=True)

def port():
    with socket.socket() as s:
        s.bind(('127.0.0.1',0)); return s.getsockname()[1]

with tempfile.TemporaryDirectory(prefix='workspace-bridge-v05-') as tmp:
    tmp=pathlib.Path(tmp); parent=tmp/'projects';parent.mkdir()
    state=tmp/'private-state'; mp, ap=port(),port()
    cfg=initialize(state,[str(parent)],mp,ap)
    service=Service(state,cfg)
    roots={}; ids={}
    for name in ['Alpha','Beta']:
        root=parent/name.lower();root.mkdir();(root/'src').mkdir();(root/'empty').mkdir()
        (root/'README.md').write_text('# '+name+'\nSynthetic validation project\n')
        (root/'src/main.py').write_text('def add(a, b):\n    return a + b\n')
        roots[name]=root
        ids[name]=service.add_workspace(name,str(root),[])['workspace']['id']
        service.manage_workspace(ids[name],'enable')
    token=service.manage_bridge('rotate_token')['token']
    admin_password='synthetic-admin-password'
    service.close()
    serverlog=tmp/'server.log'
    with serverlog.open('w') as output:
        proc=subprocess.Popen([sys.executable,'-m','workspace_bridge.cli','--state',str(state),'serve'],cwd=PROJECT,stdout=output,stderr=output,env={**os.environ,'PYTHONPATH':str(PROJECT)})
        try:
            client=httpx.Client(base_url=f'http://127.0.0.1:{mp}',trust_env=False,timeout=20)
            admin=httpx.Client(base_url=f'http://127.0.0.1:{ap}',trust_env=False,timeout=20)
            for _ in range(100):
                try:
                    if admin.get('/').status_code==200: break
                except httpx.TransportError: pass
                time.sleep(.05)
            else: raise RuntimeError('local server did not become ready')
            assert admin.post('/api/login',json={'username':'admin','password':'admin'}).status_code==200
            assert admin.post('/api/account/password',json={'current_password':'admin','new_password':admin_password}).status_code==200
            seq=0
            def raw(name,args={},key=None):
                global seq
                seq+=1
                return client.post('/mcp',headers={'X-Bridge-Token':key or token,'Accept':'application/json'},json={'jsonrpc':'2.0','id':seq,'method':'tools/call','params':{'name':name,'arguments':args}})
            def call(name,args={}):
                r=raw(name,args);assert r.status_code==200,r.text
                res=r.json()['result'];assert not res['isError'],res
                return json.loads(res['content'][0]['text'])
            smoke=subprocess.run([sys.executable,'scripts/smoke_mcp.py','--url',f'http://127.0.0.1:{mp}/mcp'],cwd=PROJECT,env={**os.environ,'WORKSPACE_BRIDGE_TOKEN':token},text=True,capture_output=True,timeout=30)
            assert smoke.returncode==0,smoke.stdout+smoke.stderr
            logs.extend(smoke.stdout.strip().splitlines());print(smoke.stdout,flush=True)
            assert call('list_workspaces')['total']==2
            for name,ident in ids.items():
                assert call('read_file',{'workspace_id':ident,'path':'README.md','offset':1,'limit':1})['lines'][0]['text']=='# '+name
                listing=call('list_dir',{'workspace_id':ident,'depth':2})
                assert any(x['path']=='empty' for x in listing['entries'])
                assert call('glob',{'workspace_id':ident,'pattern':'**/*.py'})['entries'][0]['path']=='src/main.py'
                assert call('grep_files',{'workspace_id':ident,'pattern':'def add','include':'**/*.py'})['matches'][0]['line']==1
            passed('real TCP shared endpoint: two distinct workspace reads, directories, globs and grep')
            # Native image results over real loopback TCP, not just ASGI fixtures.
            from PIL import Image
            from io import BytesIO
            import base64, hashlib
            for label, ident in ids.items():
                with Image.new('RGB', (120 if label == 'Alpha' else 90, 80), 'navy') as image:
                    image.save(roots[label] / 'probe.png')
                response = raw('read_file', {'workspace_id': ident, 'path': 'probe.png'})
                result = response.json()['result']; assert not result['isError'], result
                assert [c['type'] for c in result['content']] == ['text', 'image']
                meta = json.loads(result['content'][0]['text'])
                data = base64.b64decode(result['content'][1]['data'], validate=True)
                with Image.open(BytesIO(data)) as image:
                    image.load(); assert image.width == (120 if label == 'Alpha' else 90)
                assert hashlib.sha256(data).hexdigest() == meta['preview_sha256']
            images = subprocess.run([sys.executable, 'scripts/check_image_mcp.py',
                '--url', f'http://127.0.0.1:{mp}/mcp', '--workspace-id', ids['Alpha'], '--path', 'probe.png'],
                cwd=PROJECT, env={**os.environ, 'WORKSPACE_BRIDGE_TOKEN': token}, text=True,
                capture_output=True, timeout=40)
            assert images.returncode == 0, images.stdout + images.stderr
            logs.extend(images.stdout.strip().splitlines()); print(images.stdout, flush=True)
            passed('real TCP native image blocks, decoded pixels and workspace attribution; no live model claim')

            a={'workspace_id':ids['Alpha']};b={'workspace_id':ids['Beta']}
            file=call('read_file',{**a,'path':'src/main.py'})
            job=call('prepare_handoff',{**a,'request_id':'http-smoke-v05','title':'Review arithmetic helper','goal':'Add a concise docstring without changing behavior.','plan':'Add a one-line docstring in src/main.py. Preserve existing arithmetic.','acceptance':'add still returns a + b; document what was and was not tested.','context_hashes':{'src/main.py':file['sha256']}})
            assert str(roots['Alpha']) in job['copy_prompt']
            (roots['Alpha']/'src/main.py').write_text('def add(a, b):\n    """Return the sum of two operands."""\n    return a + b\n')
            folder = pathlib.Path(job['path'])
            assert {x.name for x in folder.iterdir()} == {'TASK.md','CONTEXT.md','ACCEPTANCE.md'}
            assert job['completion_tracking'] == 'not_tracked'
            observed = call('read_file',{**a,'path':'src/main.py'})
            assert any('Return the sum of two operands' in x['text'] for x in observed['lines'])
            task = call('read_handoff',{**a,'job_id':job['id'],'document':'TASK.md'})
            assert 'paste your reply' in task['content']
            assert raw('read_handoff',{**b,'job_id':job['id'],'document':'TASK.md'}).json()['result']['isError']
            for removed in ['review_changes','read_change','record_audit']:
                assert raw(removed,a).json()['error']['code'] == -32602
            passed('real TCP handoff -> synthetic local edit -> general source read; no result files, snapshots or verdict; cross-project denial and removed-tool rejection')
            note='.workspace-handoff/notes/review.md'
            created=call('write_file',{**a,'path':note,'content':'# Review notes\nStatus: pending\n'})
            edited=call('edit_file',{**a,'path':note,'old_text':'Status: pending','new_text':'Status: inspected', 'expected_sha256':created['sha256']})
            observed_note=call('read_file',{**a,'path':note})
            assert observed_note['sha256']==edited['sha256']
            assert 'Status: inspected' in [x['text'] for x in observed_note['lines']]
            assert raw('write_file',{**a,'path':note,'content':'stale','expected_sha256':created['sha256']}).json()['result']['isError']
            source_before=(roots['Alpha']/'src/main.py').read_bytes()
            assert raw('write_file',{**a,'path':'src/main.py','content':'forbidden'}).json()['result']['isError']
            assert (roots['Alpha']/'src/main.py').read_bytes()==source_before
            assert call('glob',{**a,'path':'.workspace-handoff','pattern':'notes/*.md'})['entries'][0]['path']==note
            assert not call('grep_files',{**a,'pattern':'Status: inspected'})['matches']
            assert call('grep_files',{**a,'path':'.workspace-handoff','pattern':'Status: inspected'})['matches'][0]['path']==note
            call('write_file',{**b,'path':note,'content':'Beta only\n'})
            assert (roots['Beta']/note).read_text()=='Beta only\n'
            assert (roots['Alpha']/note).read_text()=='# Review notes\nStatus: inspected\n'
            passed('real TCP handoff-only create/edit/read/search, stale overwrite and source-write rejection, two-workspace isolation')
            # Exercise policy changes over the separate HTTP admin listener.
            for old_name in ['write_handoff_file', 'edit_handoff_file']:
                assert raw(old_name,{**a,'path':note,'content':'no'}).json()['error']['code']==-32602
            assert call('workspace_info',a)['write_scope']=='handoff'
            assert admin.post('/api/workspaces/'+ids['Alpha'],json={'operation':'set_write_scope','write_scope':'workspace'}).status_code==200
            source_new=call('write_file',{**a,'path':'docs/source-note.md','content':'Initial text\n'})
            call('edit_file',{**a,'path':'docs/source-note.md','old_text':'Initial','new_text':'Updated','expected_sha256':source_new['sha256']})
            assert (roots['Alpha']/'docs/source-note.md').read_text()=='Updated text\n'
            assert raw('write_file',{**b,'path':'docs/source-note.md','content':'not allowed'}).json()['result']['isError']
            assert raw('write_file',{**a,'path':'.env','content':'still forbidden'}).json()['result']['isError']
            assert admin.post('/api/workspaces/'+ids['Alpha'],json={'operation':'set_write_scope','write_scope':'none'}).status_code==200
            assert raw('write_file',{**a,'path':'.workspace-handoff/blocked.md','content':'no'}).json()['result']['isError']
            assert call('read_file',{**a,'path':'docs/source-note.md'})['lines'][0]['text']=='Updated text'
            assert admin.post('/api/workspaces/'+ids['Alpha'],json={'operation':'set_write_scope','write_scope':'handoff'}).status_code==200
            assert raw('write_file',{**a,'path':'no-more-source.md','content':'no'}).json()['result']['isError']
            passed('real TCP generic tools: local-admin scope changes, allowed source edits, per-workspace isolation, exclusions, read-only mode, revocation and old-name rejection')
            assert admin.post('/api/workspaces/'+ids['Beta'],json={'operation':'disable'}).status_code==200
            assert call('list_workspaces')['total']==1
            assert raw('workspace_info',b).json()['result']['isError']
            assert call('workspace_info',a)['name']=='Alpha'
            admin.post('/api/bridge',json={'operation':'disable'}).raise_for_status()
            assert raw('list_workspaces').status_code==401
            admin.post('/api/bridge',json={'operation':'enable'}).raise_for_status()
            old=token; token=admin.post('/api/bridge',json={'operation':'rotate_token'}).json()['token']
            assert raw('list_workspaces',key=old).status_code==401
            assert call('list_workspaces')['total']==1
            assert client.post('/mcp/'+ids['Alpha']).status_code==404
            passed('real TCP mapping disable, global pause/resume, shared-token rotation and retired-route rejection')

            if args.ui_components:
                with sync_playwright() as p:
                    browser=p.chromium.launch(headless=True, **({'executable_path':args.chromium} if args.chromium else {}))
                    page=browser.new_page(viewport={'width':1440,'height':1200})
                    errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
                    # This intentionally tests components through a labeled relay.
                    # Component test: actual assets; fetch is explicitly relayed to the
                    # real loopback API. This is not normal-browser end-to-end coverage.
                    html=admin.get('/').text
                    html=html.replace('<link rel="stylesheet" href="/static/app.css">','').replace('<script src="/static/app.js" defer></script>','')
                    page.set_content(html)
                    page.add_style_tag(content=admin.get('/static/app.css').text)
                    def relay(path,options):
                        assert path.startswith('/api/')
                        response=admin.request(options.get('method','GET'),path,headers=options.get('headers',{}),content=options.get('body'))
                        return {'status':response.status_code,'ok':response.is_success,'body':response.json()}
                    page.expose_function('relayAPI',relay)
                    page.evaluate('''() => {
                        window.fetch = async (path, options) => {
                            const r = await window.relayAPI(path, options);
                            return {status:r.status, ok:r.ok, json:async()=>r.body};
                        };
                        for (const key of ['localStorage','sessionStorage']) {
                            Object.defineProperty(window,key,{get(){throw new Error('Persistent storage is forbidden in component check');}});
                        }
                    }''')
                    page.add_script_tag(content=admin.get('/static/app.js').text)
                    page.locator('#admin-password').fill(admin_password)
                    page.locator('#login-form button').click()
                    page.locator('#dashboard').wait_for(state='visible')
                    assert page.locator('#workspace-count').inner_text()=='2'
                    assert page.locator('#bridge-status').inner_text()=='Enabled'
                    page.locator('#bridge-profile').click()
                    profile=page.locator('#output').inner_text()
                    assert profile.count('channel: main')==1 and 'X-Bridge-Token' in profile and '/mcp/ws_' not in profile
                    assert token not in profile and admin_password not in profile
                    page.locator('#close-output').click()
                    page.locator('#bridge-pause').click()
                    page.wait_for_function("document.querySelector('#bridge-status').textContent === 'Paused'")
                    assert raw('list_workspaces').status_code==401
                    page.locator('#bridge-pause').click()
                    page.wait_for_function("document.querySelector('#bridge-status').textContent === 'Enabled'")
                    assert call('list_workspaces')['total']==1
                    alpha=page.locator('.workspace').filter(has=page.get_by_role('heading',name='Alpha',exact=False))
                    alpha.get_by_role('button',name='Handoffs',exact=True).click()
                    page.locator('#jobs-panel').wait_for(state='visible')
                    page.get_by_role('button',name='Context',exact=True).wait_for()
                    assert page.get_by_role('button',name='Agent result',exact=True).count()==0
                    assert page.get_by_role('button',name='Reported tests',exact=True).count()==0
                    page.get_by_role('button',name='Plan',exact=True).click()
                    page.wait_for_function("document.querySelector('#output').textContent.includes('paste your reply')")
                    page.locator('#close-output').click()
                    decision = {'accept': False}
                    page.on('dialog',lambda dialog: dialog.accept() if decision['accept'] else dialog.dismiss())
                    alpha.locator('details > summary').click()
                    alpha.get_by_label('Write permission for Alpha').select_option('workspace')
                    alpha.get_by_role('button',name='Save write permission',exact=True).click()
                    page.wait_for_function("document.querySelector('select[aria-label=\"Write permission for Alpha\"]').value === 'handoff'")
                    assert call('workspace_info',a)['write_scope']=='handoff'
                    decision['accept'] = True
                    alpha.get_by_label('Write permission for Alpha').select_option('workspace')
                    alpha.get_by_role('button',name='Save write permission',exact=True).click()
                    page.wait_for_function("document.querySelector('select[aria-label=\"Write permission for Alpha\"]').value === 'workspace'")
                    assert call('workspace_info',a)['write_scope']=='workspace'
                    assert call('workspace_info',a)['source_access']=='read_write'
                    alpha.locator('details > summary').click()
                    alpha.get_by_label('Write permission for Alpha').select_option('none')
                    alpha.get_by_role('button',name='Save write permission',exact=True).click()
                    page.wait_for_function("document.querySelector('select[aria-label=\"Write permission for Alpha\"]').value === 'none'")
                    assert call('workspace_info',a)['write_scope']=='none'
                    alpha.locator('details > summary').click()
                    alpha.get_by_label('Write permission for Alpha').select_option('handoff')
                    alpha.get_by_role('button',name='Save write permission',exact=True).click()
                    page.wait_for_function("document.querySelector('select[aria-label=\"Write permission for Alpha\"]').value === 'handoff'")
                    assert call('workspace_info',a)['write_scope']=='handoff'
                    passed('manager component write policy: cancellation preserves default, confirmed workspace/none/handoff changes reach the real local API')
                    beta=page.locator('.workspace').filter(has=page.get_by_role('heading',name='Beta',exact=False))
                    beta.get_by_role('button',name='Enable access',exact=True).click()
                    page.wait_for_function("document.querySelectorAll('.workspace .enabled').length===2")
                    assert call('list_workspaces')['total']==2
                    page.locator('#bridge-rotate').click()
                    page.locator('#output-panel').wait_for(state='visible')
                    secret_output=page.locator('#output').inner_text()
                    assert 'ALL enabled workspace mappings' in secret_output
                    assert raw('list_workspaces').status_code==401
                    page.locator('#close-output').click()
                    assert page.locator('#output').inner_text()==''
                    alpha.locator('details > summary').click()
                    page.evaluate('window.scrollTo(0,0)');page.wait_for_timeout(250)
                    page.screenshot(path=str(PROJECT/'docs/assets/workspace-manager.png'),full_page=True)
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                    page.set_viewport_size({'width':390,'height':844})
                    page.wait_for_timeout(150)
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), 'mobile overflow'
                    page.locator('#logout').click()
                    assert page.locator('#login').is_visible()
                    assert page.locator('#output').inner_text()==''
                    assert not errors,errors
                    browser.close()
                    passed('browser components with HTTP relay: login, planning-only handoff panel, single profile, shared pause/rotation, mapping enable, clear/logout and no JS errors')
                    passed('component layouts: desktop 1440px/mobile 390px no horizontal overflow; storage access trap not triggered')
                    logs.append('NOT RUN: normal browser navigation/E2E; this explicitly relayed component check is not equivalent.')
            passed('separate MCP and loopback management listeners; no external tunnel/account/agent invoked')
        finally:
            proc.terminate()
            try: proc.wait(timeout=10)
            except subprocess.TimeoutExpired:proc.kill();proc.wait()
            if proc.returncode not in [0,-15]: print(serverlog.read_text())
            client.close();admin.close()
PROJECT.joinpath('SMOKE_RESULTS.txt').write_text(f'Workspace Bridge {__version__} — Linux local validation\nSynthetic projects only; no real OpenAI tunnel or Pi agent execution.\n\n'+'\n'.join(logs)+'\n')
