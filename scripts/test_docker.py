#!/usr/bin/env python3
"""Real Docker smoke test using disposable projects and private state only.

Requires local Docker Engine/Desktop, Compose v2 and a non-root POSIX host user.
Never uses the deployment's .env, mounts real projects, or contacts the Pi agent/tunnel.
An absent Docker engine is an error, not a passing/skipped runtime validation.
"""
from __future__ import annotations
import argparse
import base64
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError
from urllib.request import Request, ProxyHandler, build_opener

from configure_docker import configure

ROOT=Path(__file__).resolve().parents[1]
OPENER=build_opener(ProxyHandler({}))


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0));return sock.getsockname()[1]


def http(port: int, path: str, data=None, headers=None):
    request=Request(f'http://127.0.0.1:{port}{path}',
                    data=None if data is None else json.dumps(data).encode(),
                    headers={'Content-Type':'application/json','Accept':'application/json',**(headers or {})})
    try:
        with OPENER.open(request,timeout=25) as response:return response.status,response.read()
    except HTTPError as exc:
        with exc:return exc.code,exc.read()


def verify_service(mcp: int, admin: int, parent: Path, admin_token: str):
    admin_headers={'Authorization':'Bearer '+admin_token}
    def manage(path,data=None):
        status,body=http(admin,path,data,admin_headers)
        assert status in (200,201),(status,body.decode())
        return json.loads(body)
    status,body=http(admin,'/api/status',headers=admin_headers)
    assert status==200 and json.loads(body)['mcp_port']==mcp
    assert json.loads(body)['listen_mode']=='docker-published-loopback'
    ids=[]
    for name in ('alpha','beta'):
        value=manage('/api/workspaces',{'name':name,'root':str(parent/name)})['workspace']
        assert not value['enabled'] and value['write_scope']=='handoff'
        ident=value['id'];ids.append(ident)
        manage('/api/workspaces/'+ident,{'operation':'enable'})
    token=manage('/api/bridge',{'operation':'rotate_token'})['token']
    def call(name,args=None):
        status,body=http(mcp,'/mcp',{'jsonrpc':'2.0','id':1,'method':'tools/call',
                        'params':{'name':name,'arguments':args or {}}},{'X-Bridge-Token':token})
        assert status==200,(status,body.decode())
        return json.loads(body)['result']
    def value(name,args=None):
        response=call(name,args)
        assert not response['isError'],response
        return json.loads(response['content'][0]['text'])
    assert value('list_workspaces')['total']==2
    for name,ident in zip(('alpha','beta'),ids):
        assert value('read_file',{'workspace_id':ident,'path':'README.md'})['lines'][0]['text']=='# '+name
        image=call('read_file',{'workspace_id':ident,'path':'pixel.png'})
        assert not image['isError'] and [c['type'] for c in image['content']]==['text','image']
        assert base64.b64decode(image['content'][1]['data'],validate=True).startswith(b'\x89PNG\r\n\x1a\n')
        assert call('write_file',{'workspace_id':ident,'path':'blocked.py','content':'denied'})['isError']
        assert not (parent/name/'blocked.py').exists()
    note='.workspace-handoff/notes/compose-check.md'
    write=value('write_file',{'workspace_id':ids[0],'path':note,'content':'one\n'})
    assert (parent/'alpha'/note).read_text()=='one\n'
    assert not (parent/'beta'/note).exists()
    value('edit_file',{'workspace_id':ids[0],'path':note,'old_text':'one','new_text':'two','expected_sha256':write['sha256']})
    assert (parent/'alpha'/note).read_text()=='two\n'
    assert call('write_file',{'workspace_id':ids[0],'path':note,'content':'stale','expected_sha256':write['sha256']})['isError']
    job=value('prepare_handoff',{'workspace_id':ids[0],'request_id':'docker-test', 'title':'Docker check',
         'goal':'Inspect fixture','plan':'Read README.md','acceptance':'Preserve the fixture'})
    assert str(parent/'alpha' / '.workspace-handoff') in job['copy_prompt']
    assert Path(job['path']).is_dir(), 'Copied handoff path must resolve on the host'
    assert call('read_handoff',{'workspace_id':ids[1],'job_id':job['id'],'document':'TASK.md'})['isError']
    assert http(mcp,'/api/status')[0]==404
    assert http(admin,'/api/status')[0]==401
    assert http(admin,'/api/status',headers={**admin_headers,'Origin':'http://evil.invalid'})[0]==403
    manage('/api/workspaces/'+ids[0],{'operation':'set_write_scope','write_scope':'none'})
    assert call('write_file',{'workspace_id':ids[0],'path':'.workspace-handoff/no.md','content':'no'})['isError']
    assert value('read_file',{'workspace_id':ids[0],'path':'README.md'})['lines']
    return ids,token


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--no-build',action='store_true',help='Use an already locally built v0.8.3 image')
    args=parser.parse_args()
    if os.name!='posix' or os.getuid()==0 or not shutil.which('docker'):
        raise SystemExit('Requires a non-root POSIX host user, Docker CLI/engine and Compose v2. Runtime was NOT tested.')
    subprocess.run(['docker','info'],check=True,stdout=subprocess.DEVNULL,timeout=20)
    with tempfile.TemporaryDirectory(prefix='wb-compose-smoke-') as temporary:
        temp=Path(temporary).resolve();parent=temp/'projects';parent.mkdir()
        # A small, valid PNG fixture; no image model/OCR or sensitive pixels.
        png=base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAMAAAACCAIAAAASFvFNAAAAFUlEQVR4nGPkC2plYGBgYGBgYoABAA/fAOlC/O8IAAAAAElFTkSuQmCC')
        for name in ('alpha','beta'):
            root=parent/name;root.mkdir();(root/'README.md').write_text('# '+name+'\n')
            (root/'pixel.png').write_bytes(png)
        mp,ap=free_port(),free_port()
        while ap==mp:ap=free_port()
        env_file=temp/'compose.env';configure(parent,temp/'state',env_file,mp,ap)
        command=['docker','compose','--project-name','wb-smoke-'+secrets.token_hex(5),
                 '--env-file',str(env_file),'-f',str(ROOT/'compose.yaml')]
        env={k:v for k,v in os.environ.items() if not k.startswith('WB_') and k!='COMPOSE_FILE'}
        def compose(*parts, capture=False, timeout=180):
            return subprocess.run([*command,*parts],cwd=ROOT,env=env,check=True,timeout=timeout,
                                  text=True,stdout=subprocess.PIPE if capture else None)
        try:
            compose('config','--quiet')
            build=[] if args.no_build else ['--build']
            compose('up','-d',*build,'--wait','--wait-timeout','180',timeout=900)
            admin_token=compose('exec','-T','bridge','workspace-bridge','--state','/state','show-admin-token',capture=True).stdout.strip()
            ids,token=verify_service(mp,ap,parent,admin_token)
            cid=compose('ps','-q','bridge',capture=True).stdout.strip()
            inspection=json.loads(subprocess.check_output(['docker','inspect',cid],text=True))[0]
            assert inspection['Config']['User'].split(':')[0]!='0'
            assert inspection['HostConfig']['ReadonlyRootfs']
            bindings=inspection['HostConfig']['PortBindings']
            assert all(row['HostIp']=='127.0.0.1' for rows in bindings.values() for row in rows)
            assert inspection['State']['Health']['Status']=='healthy'
            compose('up','-d','--force-recreate','--wait','--wait-timeout','180')
            new_admin=compose('exec','-T','bridge','workspace-bridge','--state','/state','show-admin-token',capture=True).stdout.strip()
            assert new_admin==admin_token
            status,body=http(mp,'/mcp',{'jsonrpc':'2.0','id':2,'method':'tools/call',
                               'params':{'name':'list_workspaces','arguments':{}}},{'X-Bridge-Token':token})
            data=json.loads(json.loads(body)['result']['content'][0]['text'])
            assert status==200 and {x['workspace_id'] for x in data['workspaces']}==set(ids)
            assert next(x for x in data['workspaces'] if x['workspace_id']==ids[0])['write_scope']=='none'
            assert (parent/'alpha'/'.workspace-handoff/notes/compose-check.md').read_text()=='two\n'
            print('PASS: real Compose build/start/health, non-root runtime, loopback port publishing, two workspaces, native PNG, '
                  'handoff host paths/writes, default source denial, stale hashes, hostile Origin, separation and recreate persistence.')
            print('No real tunnel, ChatGPT recognition or Pi agent execution was tested.')
        finally:
            compose('down','--remove-orphans')


if __name__=='__main__':main()
