#!/usr/bin/env python3
"""Local-process test only, NOT Docker build/runtime validation.
Requires an installed package and free local 8765/8766 ports. Creates only temporary
fixtures; when run as root on Linux it drops the child to nobody (UID/GID 65534).
The entrypoint binds both ports inside the caller environment; use a test environment.
"""
import base64, importlib.util, json, os, pathlib, re, socket, subprocess, sys, tempfile, time
PROJECT=pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0,str(PROJECT));sys.path.insert(0,str(PROJECT/'scripts'))
from test_docker import verify_service, http
from workspace_bridge.container_health import check

# The actual Docker entrypoint has intentionally fixed internal ports.
for port in (8765,8766):
 with socket.socket() as s:s.bind(('127.0.0.1',port))
with tempfile.TemporaryDirectory(prefix='wb-container-entry-') as tmp:
 temp=pathlib.Path(tmp).resolve(); temp.chmod(0o755)
 parent=temp/'projects';parent.mkdir(mode=0o755)
 state=temp/'state';state.mkdir(mode=0o700)
 uid=65534 if os.getuid()==0 else os.getuid();gid=65534 if os.getuid()==0 else os.getgid()
 encoded=re.search("png=base64.b64decode\\('([^']+)'\\)",(PROJECT/'scripts/test_docker.py').read_text()).group(1)
 for name in ('alpha','beta'):
  root=parent/name;root.mkdir(mode=0o755);(root/'README.md').write_text('# '+name+'\n')
  (root/'pixel.png').write_bytes(base64.b64decode(encoded))
 if os.getuid()==0:
  for path in [parent,state,*parent.rglob('*')]:os.chown(path,uid,gid)
 env={**os.environ,'PYTHONPATH':str(PROJECT),'HOME':'/tmp','WB_STATE_DIR':str(state),'WB_PROJECTS_DIR':str(parent),'WB_MCP_PORT':'8765','WB_ADMIN_PORT':'8766'}
 kwargs={'user':uid,'group':gid,'extra_groups':[]} if os.getuid()==0 else {}
 with (temp/'server.log').open('w') as logs:
  def start():
   return subprocess.Popen([sys.executable,'-m','workspace_bridge.docker_entrypoint','serve'],env=env,cwd=PROJECT,stdout=logs,stderr=logs,**kwargs)
  proc=start()
  try:
   for _ in range(150):
    if proc.poll() is not None:raise RuntimeError((temp/'server.log').read_text())
    if check():break
    time.sleep(.1)
   else:raise RuntimeError('entrypoint did not start')
   ids,token=verify_service(8765,8766,parent)
   account_before=(state/'admin-account.json').read_bytes()
   proc.terminate();proc.wait(timeout=15)
   proc=start()
   for _ in range(150):
    if check():break
    time.sleep(.1)
   assert (state/'admin-account.json').read_bytes()==account_before
   status,body=http(8765,'/mcp',{'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':'list_workspaces','arguments':{}}},{'X-Bridge-Token':token})
   data=json.loads(json.loads(body)['result']['content'][0]['text'])
   assert status==200 and {i['workspace_id'] for i in data['workspaces']}==set(ids)
   assert next(i for i in data['workspaces'] if i['workspace_id']==ids[0])['write_scope']=='none'
   assert (parent/'alpha'/'.workspace-handoff/notes/compose-check.md').read_text()=='two\n'
   print('PASS: actual non-root Docker entrypoint process over local TCP; both listeners, health, fresh initialization, two mappings, native PNG, '
    'same-path handoffs, protected writes, stale hash, bad Origin, process restart, credential/mapping/policy persistence.')
   print('Boundary: NOT Docker: no image build, namespaces, Docker NAT, bind mounts or Docker recreation were exercised.')
  finally:
   if proc.poll() is None:proc.terminate();proc.wait(timeout=15)
