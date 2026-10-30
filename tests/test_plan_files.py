"""Read-only plan files in Node state that a run can re-read after losing context."""
import json
from pathlib import Path

import pytest

from workspace_bridge import security_profiles as sp
from workspace_bridge.plan_files import PlanStore
from workspace_bridge.security import BridgeError
from test_run_coordinator import ADAPTER_ID, _finish_test_run, _second_job, modern_env  # noqa: F401

DOCS = {"TASK.md": "# Task\nDo it.\n", "CONTEXT.md": "Context.\n", "ACCEPTANCE.md": "- Done.\n"}


def call(e, name, **args):
    return e["service"].call(e["id"], e["token"], name, args)


def test_store_writes_read_only_files_and_restores_changed_ones(tmp_path):
    store = PlanStore(tmp_path)
    result = store.write("ws_1", "job_1", DOCS)
    folder = Path(result["folder"])
    assert folder == tmp_path / "handoffs" / "ws_1" / "job_1"
    assert sorted(result["written"]) == sorted(DOCS)
    for name, text in DOCS.items():
        assert (folder / name).read_text() == text
        assert oct((folder / name).stat().st_mode & 0o777) == "0o400"
    assert store.write("ws_1", "job_1", DOCS)["written"] == []
    # A changed, replaced or removed copy is restored from the published text.
    (folder / "TASK.md").chmod(0o600)
    (folder / "TASK.md").write_text("tampered\n")
    (folder / "CONTEXT.md").unlink()
    (folder / "CONTEXT.md").symlink_to(tmp_path / "elsewhere")
    (folder / "ACCEPTANCE.md").unlink()
    (folder / "ACCEPTANCE.md").mkdir()
    assert sorted(store.write("ws_1", "job_1", DOCS)["written"]) == sorted(DOCS)
    assert {name: (folder / name).read_text() for name in DOCS} == DOCS
    assert not (folder / "CONTEXT.md").is_symlink()


@pytest.mark.parametrize("workspace_id,job_id,documents", [
    ("../ws", "job_1", DOCS),
    ("ws_1", "job/1", DOCS),
    ("ws_1", "job_1", {"TASK.md": "x"}),
    ("ws_1", "job_1", {**DOCS, "TASK.md": 3}),
])
def test_store_rejects_bad_identities_and_documents(tmp_path, workspace_id, job_id, documents):
    with pytest.raises(BridgeError) as caught:
        PlanStore(tmp_path).write(workspace_id, job_id, documents)
    assert caught.value.code == "invalid_arguments"
    assert not (tmp_path / "handoffs").exists()


def test_published_handoff_gets_a_read_only_copy_named_in_the_prompt(env, payload):
    job = call(env, "save_handoff", **payload)
    folder = Path(job["plan_folder"])
    node_state = env["node"]["service"].state
    assert folder == node_state / "handoffs" / env["id"] / job["id"]
    assert (folder / "TASK.md").read_text() == call(
        env, "list_handoffs", job_id=job["id"], document="TASK.md", start_line=1, max_lines=200)["content"] + "\n"
    assert json.dumps(str(folder)) in job["copy_prompt"]
    assert "Re-read TASK.md" in job["copy_prompt"]
    assert not (env["root"] / ".workspace-handoff").exists()


def test_publishing_works_when_the_node_cannot_save_plan_files(env, payload, monkeypatch):
    def fail(self, *args):
        raise BridgeError("Unknown Node workspace operation", "not_found")
    monkeypatch.setattr(PlanStore, "write", fail)
    job = call(env, "save_handoff", **payload)
    assert "plan_folder" not in job
    assert "There are no handoff files to open." in job["copy_prompt"]


def test_run_reads_its_plan_folder_and_a_changed_copy_is_restored(modern_env, payload, monkeypatch):
    service, ws_id, token, job, native, _ = modern_env
    first = service.call(ws_id, token, "start_run", {
        "adapter_id": ADAPTER_ID, "job_id": job["id"], "request_id": "plan-first"})
    folder = Path(first["effective_security"]["plan_folder"])
    assert folder.name == job["id"]
    task = (folder / "TASK.md").read_text()
    _finish_test_run(service, native, ws_id, first["run_id"])
    (folder / "TASK.md").chmod(0o600)
    (folder / "TASK.md").write_text("tampered\n")
    captured = []
    client = service.run_coordinator.adapter(ADAPTER_ID)
    original = client.start_run
    monkeypatch.setattr(client, "start_run", lambda ident, body: captured.append(body) or original(ident, body))
    second = service.call(ws_id, token, "start_run", {
        "adapter_id": ADAPTER_ID, "job_id": job["id"], "request_id": "plan-second"})
    assert (folder / "TASK.md").read_text() == task
    body = captured[0]
    assert json.dumps(str(folder)) in body["input"][0]["text"]
    assert {"path": str(folder), "read": "allow"} in body["security"]["profile"]["rules"]["external_roots"]
    assert body["security"]["profile"]["revision"] == second["effective_security"]["revision"]


def test_a_run_needs_the_node_to_save_its_plan_unless_the_node_predates_plan_files(
        modern_env, payload, monkeypatch):
    service, ws_id, token, job, native, _ = modern_env
    monkeypatch.setattr(PlanStore, "write", lambda self, *a: (_ for _ in ()).throw(
        BridgeError("The Node could not save the plan files", "plan_unavailable")))
    with pytest.raises(BridgeError) as caught:
        service.call(ws_id, token, "start_run", {
            "adapter_id": ADAPTER_ID, "job_id": job["id"], "request_id": "plan-fails"})
    assert caught.value.code == "plan_unavailable"
    monkeypatch.setattr(PlanStore, "write", lambda self, *a: (_ for _ in ()).throw(
        BridgeError("Unknown Node workspace operation", "not_found")))
    run = service.call(ws_id, token, "start_run", {
        "adapter_id": ADAPTER_ID, "job_id": job["id"], "request_id": "plan-old-node"})
    assert "plan_folder" not in run["effective_security"]


def test_grant_read_denies_writes_only_where_the_plugin_enforces_roots():
    binding = sp.wire("trusted", sp.BUILTINS["trusted"])
    enforced = sp.grant_read(binding, "/state/handoffs/ws/job", ["external_roots"])
    assert enforced["profile"]["rules"]["external_roots"] == [
        {"path": "/state/handoffs/ws/job", "read": "allow", "write": "deny"}]
    assert sp.from_wire(enforced)[1] == enforced["profile"]["revision"] != binding["profile"]["revision"]
    loose = sp.grant_read(binding, "/state/handoffs/ws/job", [])
    assert loose["profile"]["rules"]["external_roots"] == [{"path": "/state/handoffs/ws/job", "read": "allow"}]
    assert sp.unenforced(loose["profile"]["rules"], []) == sp.unenforced(binding["profile"]["rules"], [])
    assert sp.grant_read({"source": "native"}, "/x", ["external_roots"]) == {"source": "native"}
    full = sp.wire("full", {**sp.BUILTINS["trusted"], "external_roots": [
        {"path": f"/r{index}", "read": "allow"} for index in range(sp.MAX_ROOTS)]})
    assert sp.grant_read(full, "/x", ["external_roots"]) == full
