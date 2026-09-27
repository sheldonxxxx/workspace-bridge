"""Docker-facing app contracts without pretending to execute Docker namespaces."""
from __future__ import annotations
import importlib.util
import json
import os
from pathlib import Path
from unittest.mock import Mock
from urllib.error import HTTPError, URLError

import httpx
import pytest
import yaml

from workspace_bridge.api import Boundary, make_admin, make_mcp
from workspace_bridge.cli import initialize, main as cli_main
from workspace_bridge.docker_entrypoint import bootstrap, main as entry_main, port_value
from workspace_bridge.security import BridgeError
from workspace_bridge.service import Service
from workspace_bridge import container_health

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('configure_docker', ROOT/'scripts/configure_docker.py')
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)


@pytest.mark.parametrize('port',[None, 8875, 55555])
def test_boundary_exact_local_names(port):
    boundary = Boundary(None, 8765, public_port=port)
    expected = {f'{host}:{p}' for host in ('localhost','127.0.0.1') for p in ({8765,port} if port else {8765})}
    assert boundary.hosts == expected
    assert boundary.origins == {'http://'+h for h in expected}


@pytest.mark.parametrize('port',[0,80,65536,True,'8875',-1])
def test_boundary_bad_public_port(port):
    with pytest.raises(ValueError):
        Boundary(None,8765,public_port=port)


@pytest.mark.parametrize('host',['localhost:8875','127.0.0.1:8875','localhost:8765','127.0.0.1:8765'])
async def test_published_mcp_hosts_work(env,host):
    app=make_mcp(env['service'],8765,public_port=8875)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://'+host) as client:
        response=await client.post('/mcp',headers={'X-Bridge-Token':env['token'],'Origin':'http://'+host},
                                  json={'jsonrpc':'2.0','id':1,'method':'ping'})
        assert response.status_code==200
        assert response.json()['result']=={}


@pytest.mark.parametrize('headers',[
    {'Host':'0.0.0.0:8765'}, {'Host':'evil.example:8875'},
    {'Origin':'http://evil.example'}, {'Origin':'http://127.0.0.1:8876'},
    {'Host':'127.0.0.1:9999'}, {'Origin':'null'},
])
async def test_published_mcp_still_rejects_untrusted(env,headers):
    app=make_mcp(env['service'],8765,public_port=8875)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1:8875') as client:
        response=await client.post('/mcp',headers={**headers,'X-Bridge-Token':env['token']},
                                  json={'jsonrpc':'2.0','id':1,'method':'ping'})
        assert response.status_code==403


@pytest.mark.parametrize('host',['bridge:8765','workspace-bridge:8765'])
async def test_native_mcp_still_rejects_sidecar_names(env,host):
    # Without container mode the Compose DNS names stay untrusted.
    app=make_mcp(env['service'],8765,public_port=8875)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://'+host) as client:
        response=await client.post('/mcp',headers={'X-Bridge-Token':env['token']},
                                  json={'jsonrpc':'2.0','id':1,'method':'ping'})
        assert response.status_code==403


@pytest.mark.parametrize('host',['bridge:8765','workspace-bridge:8765'])
async def test_container_mcp_allows_sidecar_names_on_internal_port(env,host):
    app=make_mcp(env['service'],8765,public_port=8875,container_mode=True)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://'+host) as client:
        response=await client.post('/mcp',headers={'X-Bridge-Token':env['token'],'Origin':'http://'+host},
                                  json={'jsonrpc':'2.0','id':1,'method':'ping'})
        assert response.status_code==200
        assert response.json()['result']=={}


@pytest.mark.parametrize('headers',[
    {'Host':'bridge:8875'}, {'Host':'workspace-bridge:8875'},
    {'Host':'bridge:8766'}, {'Host':'0.0.0.0:8765'}, {'Host':'evil.example:8765'},
])
async def test_container_mcp_rejects_sidecar_on_public_port_and_spoof(env,headers):
    app=make_mcp(env['service'],8765,public_port=8875,container_mode=True)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1:8875') as client:
        response=await client.post('/mcp',headers={**headers,'X-Bridge-Token':env['token']},
                                  json={'jsonrpc':'2.0','id':1,'method':'ping'})
        assert response.status_code==403


@pytest.mark.parametrize('host',['bridge:8766','workspace-bridge:8766'])
async def test_container_admin_still_rejects_sidecar_names(env,host):
    # Management stays loopback-only; the tunnel sidecar must never reach /api/.
    app=make_admin(env['service'],env['config']['admin_token_hash'],8766,public_port=8876,
                   public_mcp_port=8875,container_mode=True)
    token=(env['state']/'admin-token').read_text().strip()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://'+host) as client:
        response=await client.get('/api/status',headers={'Authorization':'Bearer '+token})
        assert response.status_code==403


async def test_admin_status_uses_published_port_and_auth(env):
    app=make_admin(env['service'],env['config']['admin_token_hash'],8766,public_port=8876,
                   public_mcp_port=8875,container_mode=True)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1:8876') as client:
        assert (await client.get('/api/status')).status_code==401
        token=(env['state']/'admin-token').read_text().strip()
        response=await client.get('/api/status',headers={'Authorization':'Bearer '+token,'Origin':'http://127.0.0.1:8876'})
        data=response.json()
        assert response.status_code==200 and data['mcp_port']==8875 and data['admin_port']==8876
        assert data['listen_mode']=='docker-published-loopback'
        assert (await client.get('/mcp')).status_code==404
        assert (await client.get('/api/status',headers={'Authorization':'Bearer '+env['token']})).status_code==401


async def test_native_admin_advertises_native_port(env):
    app=make_admin(env['service'],env['config']['admin_token_hash'])
    token=(env['state']/'admin-token').read_text().strip()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1:8766') as client:
        data=(await client.get('/api/status',headers={'Authorization':'Bearer '+token})).json()
        assert data['mcp_port']==8765 and data['listen_mode']=='loopback'


@pytest.mark.parametrize('value',['abc','80','65536','-1','12.3',' 8765','８７６５'])
def test_env_bad_ports(monkeypatch,value):
    monkeypatch.setenv('WB_MCP_PORT',value)
    with pytest.raises(BridgeError): port_value('WB_MCP_PORT',8765)


def test_env_port_default_and_override(monkeypatch):
    monkeypatch.delenv('WB_MCP_PORT',raising=False)
    assert port_value('WB_MCP_PORT',8765)==8765
    monkeypatch.setenv('WB_MCP_PORT','8875')
    assert port_value('WB_MCP_PORT',8765)==8875


def test_fresh_bootstrap_preserves_credentials_and_policy(tmp_path,capsys):
    state=tmp_path/'state';state.mkdir(mode=0o700)
    cfg=bootstrap(state)
    token=(state/'admin-token').read_bytes()
    assert token.decode().strip() not in capsys.readouterr().out
    assert bootstrap(state)==cfg
    assert (state/'admin-token').read_bytes()==token


@pytest.mark.parametrize('file',['bridge.sqlite3','admin-token','unrelated.txt'])
def test_bootstrap_nonempty_without_config_denied(tmp_path,file):
    state=tmp_path/'state';state.mkdir(mode=0o700)
    (state/file).write_text('preserve me')
    with pytest.raises(BridgeError,match='nonempty'):bootstrap(state)
    assert not (state/'config.json').exists()
    assert (state/file).read_text()=='preserve me'


def test_bootstrap_existing_listener_config_denied_without_rewrite(tmp_path):
    state=tmp_path/'state';state.mkdir(mode=0o700)
    initialize(state,8875,8876)
    before=(state/'config.json').read_bytes()
    with pytest.raises(BridgeError,match='does not match'):bootstrap(state)
    assert (state/'config.json').read_bytes()==before


def test_bootstrap_native_ports_not_rewritten(tmp_path):
    state=tmp_path/'state';state.mkdir(mode=0o700)
    initialize(state,8875,8876)
    with pytest.raises(BridgeError,match='does not match'):bootstrap(state)


@pytest.mark.parametrize('mode',[0o755,0o750,0o777])
def test_bootstrap_insecure_state_denied(tmp_path,mode):
    state=tmp_path/'state';state.mkdir();state.chmod(mode)
    with pytest.raises(BridgeError,match='0700'):bootstrap(state)
    assert (state.stat().st_mode&0o777)==mode


def test_bootstrap_symlink_denied(tmp_path):
    state=tmp_path/'state';state.mkdir(mode=0o700)
    link=tmp_path/'link';link.symlink_to(state)
    with pytest.raises(BridgeError):bootstrap(link)
    (state/'bootstrap.lock').symlink_to(tmp_path/'outside')
    with pytest.raises(OSError):bootstrap(state)
    assert not (tmp_path/'outside').exists()


@pytest.mark.parametrize('argv',[
    ['serve','--mcp-public-port','8875'],
    ['serve','--container'],
    ['serve','--container','--mcp-public-port','8875','--admin-public-port','8875'],
    ['serve','--container','--mcp-public-port','80','--admin-public-port','8876'],
])
def test_cli_rejects_incomplete_container_options(argv):
    with pytest.raises(SystemExit) as exc:cli_main(argv)
    assert exc.value.code==2


def test_entrypoint_refuses_root(monkeypatch):
    monkeypatch.setattr(os,'getuid',lambda:0)
    with pytest.raises(SystemExit) as exc:entry_main(['serve'])
    assert exc.value.code==1


def test_entrypoint_invokes_cli_with_explicit_container_flags(tmp_path,monkeypatch):
    import workspace_bridge.docker_entrypoint as entry
    monkeypatch.setattr(os,'getuid',lambda:1001)
    monkeypatch.setenv('WB_STATE_DIR',str(tmp_path/'state'))
    monkeypatch.setenv('WB_MCP_PORT','8875');monkeypatch.setenv('WB_ADMIN_PORT','8876')
    boot=Mock(); cli=Mock()
    monkeypatch.setattr(entry,'bootstrap',boot);monkeypatch.setattr(entry,'cli_main',cli)
    entry.main(['serve'])
    boot.assert_called_once()
    assert cli.call_args.args[0]==['--state',str(tmp_path/'state'),'serve','--container',
                                  '--mcp-public-port','8875','--admin-public-port','8876']


def test_compose_security_and_same_host_path():
    cfg=yaml.safe_load((ROOT/'compose.yaml').read_text())
    assert set(cfg['services'])=={'bridge','mcp-tunnel'}
    svc=cfg['services']['bridge']
    assert svc['read_only'] and svc['init']
    assert svc['cap_drop']==['ALL'] and 'no-new-privileges:true' in svc['security_opt']
    assert all(p.startswith('127.0.0.1:') for p in svc['ports'])
    assert len(svc['volumes']) == 1
    assert svc['volumes'][0]['target'] == '/state'
    assert all(mount['bind']['create_host_path'] is False for mount in svc['volumes'])
    assert not any('docker.sock' in str(m) for m in svc['volumes'])
    assert 'privileged' not in svc and 'network_mode' not in svc
    assert svc['restart']=='unless-stopped'
    assert svc['mem_limit']=='1536m' and svc['pids_limit']==64
    assert svc['healthcheck']['test']==['CMD','python','-m','workspace_bridge.container_health']
    # Adapter URLs and Bridge-side tokens live in private SQLite state;
    # native daemon bootstrap remains configured on the adapter host.
    assert 'WB_RUNTIME_ADAPTERS' not in svc['environment']
    assert 'WB_RUNTIME_TOKEN' not in svc['environment']
    assert set(cfg['services']) == {'bridge', 'mcp-tunnel'}
    # Tunnel sidecar: internal-only client, no published ports, no project/state mounts.
    tunnel=cfg['services']['mcp-tunnel']
    assert 'ports' not in tunnel and 'network_mode' not in tunnel and 'privileged' not in tunnel
    assert not any('docker.sock' in str(m) for m in tunnel.get('volumes', []))
    assert all('WB_STATE_DIR' not in str(m) and 'WB_PROJECTS_DIR' not in str(m)
               for m in tunnel.get('volumes', []))
    assert tunnel['depends_on']['bridge']['condition']=='service_healthy'
    # The profile is ignored local configuration and cannot be required by CI.
    assert tunnel['volumes']==[
        './tunnel-client.yaml:/etc/tunnel-client/tunnel-client.yaml:ro']
    assert tunnel['command']==[
        '--profile-file', '/etc/tunnel-client/tunnel-client.yaml']


def test_tunnel_sidecar_logging_level_format_rotation():
    """The tracked tunnel sidecar logs JSON at the mapped level with 10m x3 rotation.

    Tracked compose.yaml is the canonical Compose contract. A local
    deployment-specific compose-prod.yaml (ignored, host-specific paths)
    may mirror it and is validated separately, never here."""
    cfg = yaml.safe_load((ROOT/'compose.yaml').read_text())
    tunnel = cfg['services']['mcp-tunnel']
    env = tunnel.get('environment', {}) or {}
    if isinstance(env, list):
        env = dict(item.split('=', 1) for item in env)
    assert env.get('LOG_FORMAT') == 'json'
    level = str(env.get('LOG_LEVEL', ''))
    assert 'WB_TUNNEL_LOG_LEVEL' in level and ':-info' in level
    # Raw HTTP tunnel logging must never be enabled: it may expose
    # sensitive headers/bodies. Do not inspect tunnel.env secret values.
    assert 'LOG_HTTP_RAW_UNSAFE' not in env
    assert 'LOG_HTTP_RAW_UNSAFE' not in json.dumps(tunnel)
    logcfg = tunnel.get('logging', {}) or {}
    assert logcfg.get('driver') == 'json-file'
    assert logcfg.get('options', {}).get('max-size') == '10m'
    assert logcfg.get('options', {}).get('max-file') == '3'


def test_env_example_documents_tunnel_log_level():
    text = (ROOT/'.env.example').read_text()
    assert 'WB_TUNNEL_LOG_LEVEL' in text and 'info' in text


def test_dockerfile_dependency_layer_before_source():
    """Third-party wheel work must not be invalidated by source-only changes."""
    text = (ROOT/'Dockerfile').read_text()
    assert 'COPY . ' not in text
    assert 'pip wheel' in text and '--no-index' in text
    assert 'ENTRYPOINT ["python", "-m", "workspace_bridge.docker_entrypoint"]' in text
    builder = text.split('FROM ${PYTHON_IMAGE} AS builder')[1].split('FROM ${PYTHON_IMAGE} AS runtime')[0]
    assert 'PIP_NO_CACHE_DIR' not in builder
    assert '--mount=type=cache,target=/root/.cache/pip' in builder
    dep_wheel = builder.index('pip wheel --require-hashes --only-binary=:all: --wheel-dir /wheels -r runtime-requirements.txt')
    toml_copy = builder.index('pyproject.toml')
    source_copy = builder.index('workspace_bridge/ ./workspace_bridge/')
    local_wheel = builder.index('pip wheel --no-deps --no-build-isolation')
    assert toml_copy < dep_wheel < source_copy < local_wheel
    assert 'docker/runtime-requirements.txt' in builder
    # A source/static/test-only change touches none of the dependency-layer inputs.
    assert builder.count('COPY') == 4  # source wheel also receives the built web assets
    runtime = text.split('FROM ${PYTHON_IMAGE} AS runtime')[1]
    assert 'apt-get update' in runtime
    assert 'apt-get install -y --no-install-recommends git' in runtime
    assert 'rm -rf /var/lib/apt/lists/*' in runtime
    assert runtime.index('apt-get update') < runtime.index('apt-get install') < runtime.index('rm -rf /var/lib/apt/lists/*')
    assert '--no-cache-dir --no-index --find-links=/wheels --require-hashes' in runtime
    assert '--no-deps /wheels/workspace_bridge-*.whl' in runtime
    assert 'workspace-bridge==0.1.0' not in runtime
    assert 'USER 10001:10001' in runtime
    ignore = (ROOT/'.dockerignore').read_text()
    assert '\n**\n' in ignore and '!workspace_bridge/' in ignore and '!README.md' in ignore
    assert '!docker/runtime-requirements.txt' in ignore
    assert '!scripts/' not in ignore and '!.env' not in ignore


def test_pi_adapter_is_native_without_container_image():
    """The Pi host adapter runs natively on the host: no Dockerfile, no
    Compose service, no published port. The bridge only holds the adapter
    URL plus the shared runtime token."""
    import re
    adapter_dir = ROOT/'runtime/pi-host-adapter'
    assert not list(adapter_dir.glob('Dockerfile*'))
    assert (adapter_dir/'package.json').exists()
    # Every relative .mjs import of the shipped entry modules must exist,
    # otherwise the native adapter crashes with ERR_MODULE_NOT_FOUND.
    entry_modules = ['adapter.mjs', 'server.mjs', 'main.mjs']
    for module in entry_modules:
        assert (adapter_dir/module).exists(), module
        source = (adapter_dir/module).read_text()
        for imported in re.findall(r'''from\s+["']\./([^"']+)["']''', source):
            assert (adapter_dir/imported).exists(), f"{module} imports ./{imported} which is missing"
    cfg = yaml.safe_load((ROOT/'compose.yaml').read_text())
    assert set(cfg['services']) == {'bridge', 'mcp-tunnel'}
    bridge_text = (ROOT/'Dockerfile').read_text()
    assert 'pi-host-adapter' not in bridge_text


def test_setup_generates_private_config_no_source_changes(tmp_path,monkeypatch):
    # Simulated uid only for host setup logic; actual OS ownership remains unchanged.
    uid=os.getuid() or 1001
    monkeypatch.setattr(os,'getuid',lambda:uid)
    parent=tmp_path/'projects # dollar$';parent.mkdir()
    state=tmp_path/'private';out=tmp_path/'.env'
    setup.configure(state,out,8875,8876)
    content=out.read_text()
    assert 'WB_MCP_PORT=8875' in content and 'WB_ADMIN_PORT=8876' in content
    assert 'TOKEN' not in content and 'API_KEY' not in content
    assert state.stat().st_mode&0o777==0o700 and out.stat().st_mode&0o777==0o600
    with pytest.raises(ValueError,match='already exists'):setup.configure(state,out)


def test_setup_rejects_root(tmp_path,monkeypatch):
    monkeypatch.setattr(os,'getuid',lambda:0)
    with pytest.raises(ValueError,match='root'):setup.configure(tmp_path/'state',tmp_path/'.env')


@pytest.mark.parametrize('bad', ["/home/a/quo'te", '/home/a/path\\bad', '/home/a/a:b', '/home/a/new\nline'])
def test_setup_env_rejects_unsafe_characters(bad):
    with pytest.raises(ValueError):setup.env_path(Path(bad))


def test_setup_state_overlap_rejected(tmp_path,monkeypatch):
    monkeypatch.setattr(os,'getuid',lambda:1001)
    state=tmp_path/'state'; state.mkdir()
    with pytest.raises(ValueError,match='outside'):setup.configure(state,state/'nested.env')
    assert not (state/'nested.env').exists()


def test_health_exact_urls_and_no_proxy(monkeypatch):
    calls=[]
    class Response:
        status=200
        def __enter__(self):return self
        def __exit__(self,*args):pass
    class Opener:
        def open(self,url,timeout):
            calls.append(url)
            if url.endswith('/mcp'):raise HTTPError(url,401,'Unauthenticated',{},None)
            return Response()
    monkeypatch.setattr(container_health,'build_opener',lambda proxy:Opener())
    assert container_health.check()
    assert calls==['http://127.0.0.1:8766/','http://127.0.0.1:8765/mcp']


def test_health_unavailable(monkeypatch):
    opener=Mock();opener.open.side_effect=URLError('unavailable')
    monkeypatch.setattr(container_health,'build_opener',lambda _:opener)
    assert not container_health.check()


def test_no_skill_or_remote_schema_expansion():
    from workspace_bridge.api import TOOLS
    from workspace_bridge.embedded_skill import SKILL_VERSION
    assert len(TOOLS)==26 and SKILL_VERSION=='3.1.0'
    assert not any('docker' in name or 'container' in name for name in TOOLS)


def test_admin_allowed_hosts_parse_empty(monkeypatch):
    from workspace_bridge.cli import admin_allowed_hosts_from_env, parse_admin_allowed_hosts
    assert parse_admin_allowed_hosts('') == ()
    assert parse_admin_allowed_hosts('   ') == ()
    monkeypatch.delenv('WB_ADMIN_ALLOWED_HOSTS', raising=False)
    assert admin_allowed_hosts_from_env() == ()


def test_admin_allowed_hosts_parse_valid(monkeypatch):
    from workspace_bridge.cli import admin_allowed_hosts_from_env
    monkeypatch.setenv('WB_ADMIN_ALLOWED_HOSTS', 'Admin.lan, 192.168.1.10,ADMIN.LAN')
    assert admin_allowed_hosts_from_env() == ('admin.lan', '192.168.1.10')


@pytest.mark.parametrize('raw', [
    'evil.example:8766', 'http://evil.example', 'https://evil.example:8766/x',
    '*.example.com', 'a/b', 'a@b', 'a b', 'a[b]', 'ex_ample!.com',
    '.leading-dot', 'trailing-dot.', '-leading', 'double..dot', 'with:colon',
])
def test_admin_allowed_hosts_reject_invalid(raw):
    from workspace_bridge.cli import parse_admin_allowed_hosts
    with pytest.raises(BridgeError):
        parse_admin_allowed_hosts(raw)


def test_admin_allowed_hosts_max_entries():
    from workspace_bridge.cli import parse_admin_allowed_hosts
    with pytest.raises(BridgeError):
        parse_admin_allowed_hosts(','.join(f'h{i}.lan' for i in range(21)))


def test_boundary_extra_hosts_admin_only():
    boundary = Boundary(None, 8766, public_port=8876, extra_hosts=('admin.lan',))
    assert 'admin.lan:8766' in boundary.hosts and 'admin.lan:8876' in boundary.hosts
    assert 'http://admin.lan:8766' in boundary.origins
    assert 'https://admin.lan:8766' in boundary.origins
    # Loopback stays http-only; MCP default boundary has no extra hosts.
    assert 'https://127.0.0.1:8766' not in boundary.origins
    mcp = Boundary(None, 8765, public_port=8875)
    assert 'admin.lan:8765' not in mcp.hosts


@pytest.mark.parametrize('host', ['admin.lan:8766', 'admin.lan:8876'])
async def test_admin_allows_configured_host(env, host):
    from workspace_bridge.cli import parse_admin_allowed_hosts
    extra = parse_admin_allowed_hosts('admin.lan')
    app = make_admin(env['service'], env['config']['admin_token_hash'], 8766,
                     public_port=8876, container_mode=False, extra_hosts=extra)
    token = (env['state'] / 'admin-token').read_text().strip()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url='http://' + host) as client:
        r = await client.get('/api/status', headers={'Authorization': 'Bearer ' + token})
        assert r.status_code == 200
        assert r.json()['admin_allowed_hosts'] == ['admin.lan']
        assert 'remote-admin' in r.json()['listen_mode']


async def test_admin_https_origin_allowed_for_extra_host(env):
    from workspace_bridge.cli import parse_admin_allowed_hosts
    extra = parse_admin_allowed_hosts('admin.lan')
    app = make_admin(env['service'], env['config']['admin_token_hash'], 8766,
                     extra_hosts=extra)
    token = (env['state'] / 'admin-token').read_text().strip()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url='http://admin.lan:8766') as client:
        r = await client.get('/api/status', headers={
            'Authorization': 'Bearer ' + token, 'Origin': 'https://admin.lan:8766'})
        assert r.status_code == 200


@pytest.mark.parametrize('headers', [
    {'Host': 'evil.example:8766'}, {'Host': 'admin.lan:9999'},
    {'Origin': 'http://evil.example'}, {'Origin': 'https://127.0.0.1:8766'},
])
async def test_admin_still_rejects_untrusted_with_allowlist(env, headers):
    from workspace_bridge.cli import parse_admin_allowed_hosts
    extra = parse_admin_allowed_hosts('admin.lan')
    app = make_admin(env['service'], env['config']['admin_token_hash'], 8766,
                     extra_hosts=extra)
    token = (env['state'] / 'admin-token').read_text().strip()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url='http://admin.lan:8766') as client:
        r = await client.get('/api/status', headers={
            **headers, 'Authorization': 'Bearer ' + token})
        assert r.status_code == 403


def test_resolve_bind_hosts():
    from workspace_bridge.cli import resolve_bind_hosts
    assert resolve_bind_hosts(False, ()) == ('127.0.0.1', '127.0.0.1')
    assert resolve_bind_hosts(False, ('admin.lan',)) == ('127.0.0.1', '0.0.0.0')
    assert resolve_bind_hosts(True, ()) == ('0.0.0.0', '0.0.0.0')
    assert resolve_bind_hosts(True, ('admin.lan',)) == ('0.0.0.0', '0.0.0.0')


def test_entrypoint_rejects_bad_allowed_hosts(tmp_path, monkeypatch):
    import workspace_bridge.docker_entrypoint as entry
    monkeypatch.setattr(os, 'getuid', lambda: 1001)
    monkeypatch.setenv('WB_STATE_DIR', str(tmp_path / 'state'))
    monkeypatch.setenv('WB_ADMIN_ALLOWED_HOSTS', 'bad:host')
    with pytest.raises(SystemExit) as exc:
        entry.main(['serve'])
    assert exc.value.code == 1


def test_compose_admin_loopback_and_allowlist_passthrough():
    cfg = yaml.safe_load((ROOT / 'compose.yaml').read_text())
    svc = cfg['services']['bridge']
    assert all(p.startswith('127.0.0.1:') for p in svc['ports'])
    assert 'WB_ADMIN_ALLOWED_HOSTS' in svc['environment']


@pytest.mark.parametrize('host', ['admin.lan', 'admin.lan:443', 'ADMIN.LAN'])
async def test_admin_allows_proxy_host_forms(env, host):
    """TLS-terminating nginx sends bare Host or :443, not the internal port."""
    from workspace_bridge.cli import parse_admin_allowed_hosts
    extra = parse_admin_allowed_hosts('admin.lan')
    app = make_admin(env['service'], env['config']['admin_token_hash'], 8766,
                     extra_hosts=extra)
    token = (env['state'] / 'admin-token').read_text().strip()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url='http://127.0.0.1:8766') as client:
        r = await client.get('/api/status', headers={
            'Host': host, 'Authorization': 'Bearer ' + token})
        assert r.status_code == 200


@pytest.mark.parametrize('origin', [
    'https://admin.lan', 'https://admin.lan:443', 'http://admin.lan',
])
async def test_admin_allows_proxy_origins(env, origin):
    """Browsers behind https://<name> send Origin without the internal port."""
    from workspace_bridge.cli import parse_admin_allowed_hosts
    extra = parse_admin_allowed_hosts('admin.lan')
    app = make_admin(env['service'], env['config']['admin_token_hash'], 8766,
                     extra_hosts=extra)
    token = (env['state'] / 'admin-token').read_text().strip()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url='http://127.0.0.1:8766') as client:
        r = await client.get('/api/status', headers={
            'Host': 'admin.lan', 'Origin': origin,
            'Authorization': 'Bearer ' + token})
        assert r.status_code == 200


async def test_admin_proxy_host_and_origin_together(env):
    from workspace_bridge.cli import parse_admin_allowed_hosts
    extra = parse_admin_allowed_hosts('admin.example.com')
    app = make_admin(env['service'], env['config']['admin_token_hash'], 8766,
                     extra_hosts=extra)
    token = (env['state'] / 'admin-token').read_text().strip()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url='http://127.0.0.1:8766') as client:
        r = await client.get('/api/status', headers={
            'Host': 'admin.example.com', 'Origin': 'https://admin.example.com',
            'Authorization': 'Bearer ' + token})
        assert r.status_code == 200


@pytest.mark.parametrize('headers', [
    {'Host': 'admin.lan:9999'},
    {'Host': 'admin.lan:8766', 'Origin': 'https://evil.example'},
    {'Host': 'evil.example', 'Origin': 'https://evil.example'},
])
async def test_admin_proxy_still_rejects_wrong_port_and_evil(env, headers):
    from workspace_bridge.cli import parse_admin_allowed_hosts
    extra = parse_admin_allowed_hosts('admin.lan')
    app = make_admin(env['service'], env['config']['admin_token_hash'], 8766,
                     extra_hosts=extra)
    token = (env['state'] / 'admin-token').read_text().strip()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url='http://127.0.0.1:8766') as client:
        r = await client.get('/api/status', headers={
            **headers, 'Authorization': 'Bearer ' + token})
        assert r.status_code == 403


async def test_boundary_reject_is_structured_without_host_origin_path(env, caplog):
    import json
    import logging
    from workspace_bridge.cli import parse_admin_allowed_hosts
    extra = parse_admin_allowed_hosts('admin.lan')
    app = make_admin(env['service'], env['config']['admin_token_hash'], 8766,
                     extra_hosts=extra)
    token = (env['state'] / 'admin-token').read_text().strip()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url='http://127.0.0.1:8766') as client:
        with caplog.at_level(logging.WARNING, logger="workspace_bridge.ops"):
            r = await client.get('/api/status', headers={
                'Host': 'someone-else.example', 'Authorization': 'Bearer ' + token})
        assert r.status_code == 403
        lines = [rec.message for rec in caplog.records
                 if rec.name == "workspace_bridge.ops"]
        assert lines, "expected a structured boundary_reject record"
        dumped = "\n".join(lines)
        assert "someone-else.example" not in dumped
        assert "/api/status" not in dumped
        parsed = [json.loads(line) for line in lines]
        rejects = [p for p in parsed if p.get("event") == "boundary_reject"]
        assert rejects and rejects[0]["level"] == "WARNING"
        assert rejects[0]["reason"] == "untrusted-host"
        assert set(rejects[0]) <= {"timestamp", "level", "component", "event",
                                    "reason", "code", "source", "action",
                                    "workspace_id"}
