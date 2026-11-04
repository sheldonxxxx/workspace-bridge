import json
import os
import pytest
from workspace_bridge.security import BridgeError, HANDOFF, digest
from workspace_bridge.api import Handoff


def call(env, name, **args):
    return env["service"].call(env["id"], env["token"], name, args)

def prepare(env, payload):
    return call(env, "save_handoff", **Handoff.model_validate(payload).model_dump())



def test_idempotent_handoff_and_conflict(env, payload):
    first = prepare(env, payload); second = prepare(env, payload)
    assert first["id"] == second["id"]
    payload["plan"] = "Changed plan"
    with pytest.raises(BridgeError, match="different content"): prepare(env, payload)

def test_stale_plan_context(env, payload):
    payload["context_hashes"] = {"src/main.py": "a" * 64}
    with pytest.raises(BridgeError, match="context file"): prepare(env, payload)
    assert not (env["root"] / HANDOFF).exists()









def test_revoke_and_disable(env):
    env["service"].manage_bridge("rotate_token")
    with pytest.raises(BridgeError): call(env, "workspace_info")
    env["service"].manage_workspace(env["id"], "disable")
    with pytest.raises(BridgeError): call(env, "workspace_info")

def test_paginated_reads(env):
    (env["root"] / "numbered.txt").write_text("\n".join(str(i) for i in range(20)))
    r = call(env, "read_file", path="numbered.txt", start_line=5, max_lines=3, expected_sha256=None)
    assert r["lines"][0] == {"line": 5, "text": "4"} and r["next_line"] == 8
    with pytest.raises(BridgeError): call(env, "read_file", path="numbered.txt", start_line=1, max_lines=3, expected_sha256="f"*64)




def test_publish_never_writes_through_a_handoff_symlink(env, payload):
    target = env["tmp"] / "elsewhere"
    target.mkdir()
    (env["root"] / HANDOFF).symlink_to(target, target_is_directory=True)
    before = (env["root"] / "src/main.py").read_bytes()
    job = prepare(env, payload)
    assert job["stored"] == "bridge"
    assert (env["root"] / "src/main.py").read_bytes() == before
    assert list(target.iterdir()) == []


def test_diagnostic_service_does_not_recover_active_publication(env, payload):
    from workspace_bridge.service import Service
    svc = env['service']; ws = svc.workspace(env['id'])
    result = svc.prepare_handoff(ws, payload)
    with svc.db:
        svc.db.execute("UPDATE jobs SET state='publishing' WHERE id=?", (result['id'],))
    diagnostic = Service(env['state'], env['config'])
    diagnostic.close()
    assert svc.job(ws, result['id'])['state'] == 'publishing'
    recovery = Service(env['state'], env['config'], recover_incomplete=True)
    recovery.close()
    assert svc.job(ws, result['id'])['state'] == 'failed'




def test_edit_delete_handoff_updates_node_and_checks_stale(env, payload):
    job = prepare(env, payload)
    service = env['service']; ws = service.workspace(env['id'])
    original = service.job(ws, job['id'])
    docs = json.loads(original['content'])
    hashes = json.loads(original['documents'])
    docs['TASK.md'] = 'Updated task\n'
    updated = call(env, 'edit_handoff', job_id=job['id'], documents=docs, expected_documents=hashes, title='Updated title', expected_title=original['title'])
    assert 'Updated task' in updated['copy_prompt']
    assert (env['node']['service'].state / 'handoffs' / env['id'] / job['id'] / 'TASK.md').read_text() == docs['TASK.md']
    with pytest.raises(BridgeError, match='changed'):
        call(env, 'delete_handoff', job_id=job['id'], expected_documents=hashes, expected_title=original['title'])
    new_hashes = json.loads(service.job(ws, job['id'])['documents'])
    assert call(env, 'delete_handoff', job_id=job['id'], expected_documents=new_hashes, expected_title='Updated title')['deleted']
    assert not (env['node']['service'].state / 'handoffs' / env['id'] / job['id']).exists()
    with pytest.raises(BridgeError, match='not found'):
        service.job(ws, job['id'])


def test_used_handoff_cannot_change(env, payload):
    job = prepare(env, payload)
    service = env['service']; ws = service.workspace(env['id'])
    stored = service.job(ws, job['id'])
    # Model an existing run without requiring an adapter process.
    with service.lock:
        service.db.execute('PRAGMA foreign_keys=OFF')
        service.db.execute("INSERT INTO agent_runs (id, conversation, workspace, node_id, node_revision, adapter_id, runtime_type, adapter_revision, handoff, request_id, request_hash, model, phase, effective_security, created, updated) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", ('run_test', 'conv', ws['id'], ws['node_id'], 'rev', 'adapter', 'pi', 'rev', job['id'], 'req', 'hash', 'model', 'completed', '{}', 'now', 'now'))
        service.db.commit()
        service.db.execute('PRAGMA foreign_keys=ON')
    hashes = json.loads(stored['documents'])
    for name, args in [('delete_handoff', {}), ('edit_handoff', {'documents': json.loads(stored['content']), 'title': stored['title']})]:
        with pytest.raises(BridgeError, match='used by a run'):
            call(env, name, job_id=job['id'], expected_documents=hashes, expected_title=stored['title'], **args)
    assert service.handoff_summary(ws, stored)['used'] is True


def test_node_failure_preserves_bridge_handoff(env, payload, monkeypatch):
    job = prepare(env, payload)
    service = env['service']; ws = service.workspace(env['id'])
    stored = service.job(ws, job['id'])
    client = service.node_registry.client(ws['node_id'])
    def unavailable(*args, **kwargs):
        raise BridgeError('Node unavailable', 'plan_unavailable')
    monkeypatch.setattr(client, 'workspace', unavailable)
    monkeypatch.setattr(service.node_registry, 'client', lambda *args, **kwargs: client)
    hashes = json.loads(stored['documents'])
    with pytest.raises(BridgeError, match='Node unavailable'):
        call(env, 'delete_handoff', job_id=job['id'], expected_title=stored['title'], expected_documents=hashes)
    assert service.job(ws, job['id'])['content'] == stored['content']
