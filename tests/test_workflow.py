import json
import os
import pytest
from workspace_bridge.security import BridgeError, HANDOFF, digest
from workspace_bridge.api import Handoff


def call(env, name, **args):
    return env["service"].call(env["id"], env["token"], name, args)

def prepare(env, payload):
    return call(env, "prepare_handoff", **Handoff.model_validate(payload).model_dump())



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




def test_failed_publish_preserves_source(env, payload):
    (env["root"] / HANDOFF).symlink_to(env["tmp"], target_is_directory=True)
    before = (env["root"] / "src/main.py").read_bytes()
    with pytest.raises(BridgeError): prepare(env, payload)
    assert (env["root"] / "src/main.py").read_bytes() == before
    with pytest.raises(BridgeError, match="Previous publication failed"): prepare(env, payload)


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


