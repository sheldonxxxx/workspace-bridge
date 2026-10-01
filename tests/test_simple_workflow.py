from admin_helpers import admin_cookie
"""The manual-return contract and non-destructive removal of snapshot review."""
from pathlib import Path
import json
import sqlite3

import httpx
import pytest

from workspace_bridge.api import Handoff, INSTRUCTIONS, TOOLS, make_admin, make_mcp
from workspace_bridge.embedded_skill import read_project_lead_skill
from workspace_bridge.security import BridgeError, HANDOFF, SafeRoot, digest
from workspace_bridge.service import Service

RETIRED = ('review_changes', 'read_change', 'record_audit')
EXPECTED = {'read_project_lead_skill', 'list_workspaces', 'workspace_info',
            'list_dir', 'read_file', 'glob', 'grep_files',
            'prepare_handoff', 'list_handoffs', 'read_handoff', 'write_file', 'edit_file',
            'list_agent_models', 'start_agent_run', 'list_agent_runs',
            'list_agent_adapters',
            'read_agent_run',
            'cancel_agent_run', 'list_agent_executions', 'read_agent_execution',
            'read_agent_interaction', 'respond_agent_interaction',
            'list_agent_activities', 'read_agent_activity', 'git_status', 'git_diff'}
MUTATING = {'prepare_handoff', 'write_file', 'edit_file',
            'start_agent_run', 'cancel_agent_run',
            'respond_agent_interaction'}


def publish(env, payload):
    return env['service'].call(env['id'], env['token'], 'prepare_handoff',
                               Handoff.model_validate(payload).model_dump())


def read(env, path, expected=None):
    return env['service'].call(env['id'], env['token'], 'read_file',
        dict(path=path, start_line=1, max_lines=100, expected_sha256=expected))


def test_tool_surface_and_mutations_are_deliberate():
    assert set(TOOLS) == EXPECTED
    assert {n for n, (_, _, ro, _) in TOOLS.items() if not ro} == MUTATING
    assert not any(hasattr(Service, n) for n in (*RETIRED, 'snapshot', 'snapshot_once', 'artifact_state'))


def test_workspace_info_describes_exact_adapter_workflow(env):
    info = env['service'].call(env['id'], env['token'], 'workspace_info', {})
    workflow = info['workflow']
    assert 'explicit runtime' not in workflow
    assert workflow.index('list_agent_adapters') < workflow.index('adapter_id')
    assert workflow.index('adapter_id') < workflow.index('list_agent_models')
    assert workflow.index('list_agent_models') < workflow.index('start_agent_run')


def test_fresh_state_has_no_review_or_audit_tables(env):
    names = {r[0] for r in env['service'].db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert 'reviews' not in names and 'audits' not in names


def test_handoff_does_not_read_or_walk_project(env, payload, monkeypatch):
    before = (env['root'] / 'src/main.py').read_bytes()
    def fail(*a, **kw):
        raise AssertionError('No project scan or source read is needed without context hashes')
    monkeypatch.setattr(SafeRoot, 'walk', fail)
    monkeypatch.setattr(SafeRoot, 'read', fail)
    job = publish(env, payload)
    folder = Path(job['path'])
    assert {p.name for p in folder.iterdir()} == {'TASK.md', 'CONTEXT.md', 'ACCEPTANCE.md'}
    assert (env['root'] / 'src/main.py').read_bytes() == before
    assert not any(k in job for k in ['baseline_files', 'baseline_sha256', 'review_id', 'verdict'])
    assert job['completion_tracking'] == 'not_tracked'
    assert 'paste your reply' in job['copy_prompt']
    cols = {row[1] for row in env['service'].db.execute('PRAGMA table_info(jobs)')}
    assert 'baseline' not in cols


def test_optional_context_checks_only_named_files(env, payload, monkeypatch):
    payload['context_hashes'] = {'src/main.py': digest((env['root']/'src/main.py').read_bytes())}
    original = SafeRoot.read
    seen = []
    def checked(self, path, *a, **kw):
        seen.append(path)
        return original(self, path, *a, **kw)
    monkeypatch.setattr(SafeRoot, 'read', checked)
    monkeypatch.setattr(SafeRoot, 'walk', lambda *a, **kw: pytest.fail('No workspace scan'))
    publish(env, payload)
    assert seen == ['src/main.py']


@pytest.mark.parametrize('path', ['missing.py', '.env', '../outside', '/etc/passwd'])
def test_bad_context_fails_without_publishing(env, payload, path):
    payload['context_hashes'] = {path: 'a' * 64}
    with pytest.raises(BridgeError) as e:
        publish(env, payload)
    assert e.value.code == 'stale_context'
    assert not (env['root']/HANDOFF).exists()


def test_unrelated_large_or_binary_files_do_not_block_handoff(env, payload):
    with (env['root']/'huge.bin').open('wb') as f:
        f.truncate(8 * 1024 * 1024)
    (env['root']/'other.bin').write_bytes(b'\x00\xff')
    assert publish(env, payload)['state'] == 'prepared'


def test_general_reads_observe_current_source_without_result_files(env, payload):
    job = publish(env, payload)
    first = read(env, 'src/main.py')
    (env['root']/'src/main.py').write_text('def add(a, b):\n    return int(a) + int(b)\n')
    current = read(env, 'src/main.py')
    assert current['sha256'] != first['sha256']
    assert 'int(a)' in current['lines'][1]['text']
    assert not (Path(job['path'])/'result').exists()
    with pytest.raises(BridgeError) as e:
        read(env, 'src/main.py', first['sha256'])
    assert e.value.code == 'stale_evidence'
    svc = env['service']
    assert svc.job(svc.workspace(env['id']), job['id'])['state'] == 'prepared'


def test_exclusion_change_does_not_invalidate_handoff(env, payload):
    job = publish(env, payload)
    env['service'].manage_workspace(env['id'], 'set_excludes', ['README.md'])
    task = env['service'].call(env['id'], env['token'], 'read_handoff',
        dict(job_id=job['id'], document='TASK.md', start_line=1, max_lines=100))
    assert task['matches_published']
    with pytest.raises(BridgeError):
        read(env, 'README.md')
    assert read(env, 'src/main.py')['lines']


def test_handoff_integrity_is_visible_but_not_an_audit_gate(env, payload):
    job = publish(env, payload)
    (Path(job['path'])/'TASK.md').write_text('Modified locally')
    task = env['service'].call(env['id'], env['token'], 'read_handoff',
        dict(job_id=job['id'], document='TASK.md', start_line=1, max_lines=100))
    assert task['matches_published'] is False
    assert task['trust'] == 'untrusted_handoff_content'


@pytest.mark.parametrize('document', ['BASELINE.json', 'result/RESULT.md', 'result/TESTS.json', '../../README.md'])
def test_retired_and_arbitrary_handoff_documents_rejected(env, payload, document):
    job = publish(env, payload)
    with pytest.raises(BridgeError) as e:
        env['service'].call(env['id'], env['token'], 'read_handoff',
            dict(job_id=job['id'], document=document, start_line=1, max_lines=100))
    assert e.value.code == 'invalid_arguments'


@pytest.mark.parametrize('name', RETIRED)
async def test_retired_tools_are_rejected_at_http_and_service(env, name):
    with pytest.raises(BridgeError) as e:
        env['service'].call(env['id'], env['token'], name, {})
    assert e.value.code == 'unknown_tool'
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=make_mcp(env['service'])),
                                base_url='http://127.0.0.1:8765') as c:
        r = await c.post('/mcp', headers={'X-Bridge-Token': env['token'], 'Accept':'application/json'},
            json={'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':name,'arguments':{'workspace_id':env['id']}}})
        assert r.json()['error']['code'] == -32602


def test_skill_and_discovery_describe_manual_return_not_retired_tools():
    skill = read_project_lead_skill()['content']
    for name in RETIRED:
        assert name not in skill and name not in INSTRUCTIONS
    assert 'user will paste' in skill
    assert 'No special report files' in skill
    assert 'live reads' in skill.lower()
    assert 'current code' in INSTRUCTIONS
    assert 'actual check commands/outcomes' in skill


async def test_manager_only_offers_planning_documents(env, payload):
    job = publish(env, payload)
    app = make_admin(env['service'])
    token = admin_cookie(app)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                base_url='http://127.0.0.1:8766', headers={"Cookie": token}) as c:
        js = (__import__('pathlib').Path(__file__).resolve().parents[1] / 'web' / 'src' / 'App.tsx').read_text()
        html = (await c.get('/')).text
        assert 'Agent result' not in js and 'Reported tests' not in js
        assert 'saved baseline' not in html
        assert '"CONTEXT.md"' in js
        assert 'h.state === "prepared"' in js
        for doc in ['TASK.md','CONTEXT.md','ACCEPTANCE.md']:
            r = await c.get('/api/workspaces/'+env['id']+'/document',params={'job_id':job['id'],'document':doc})
            assert r.status_code == 200
        for doc in ['BASELINE.json','result/RESULT.md','result/TESTS.json']:
            r = await c.get('/api/workspaces/'+env['id']+'/document',params={'job_id':job['id'],'document':doc})
            assert r.status_code == 400
