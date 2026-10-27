"""Worktree isolation: the Node's worktree store and isolated runs through the Bridge."""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

from workspace_bridge.api import Handoff
from workspace_bridge.cli import initialize
from workspace_bridge.codex_host_adapter import CodexHostAdapter
from workspace_bridge.run_roots import run_directory
from workspace_bridge.run_worktrees import RETENTION_SECONDS, RunWorktrees
from workspace_bridge.security import BridgeError
from workspace_bridge.service import Service

from conftest import attach_test_node, create_test_adapter
from test_codex_host_adapter import FakeCodexRpc
from test_run_coordinator import ADAPTER_ID, DirectAdapter, _finish_test_run, _second_job


def _git(root: Path, *args: str) -> str:
    env = {"PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin", "LC_ALL": "C",
           "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0"}
    return subprocess.run(["git", *args], cwd=root, env=env, text=True, check=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout


def _repo(root: Path) -> None:
    _git(root, "init", "-q", "--initial-branch=main")
    _git(root, "config", "user.name", "Workspace Bridge Test")
    _git(root, "config", "user.email", "bridge-test@example.invalid")
    (root / "app.py").write_text("print('one')\n")
    (root / ".gitignore").write_text("build/\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "initial")


@pytest.fixture
def iso_env(tmp_path, payload):
    parent = tmp_path / "projects"
    root = parent / "alpha"
    root.mkdir(parents=True)
    _repo(root)
    state = tmp_path / "bridge-state"
    service = Service(state, initialize(state, 8765, 8766), run_coordinator_background=False)
    node, node_record = attach_test_node(service, tmp_path / "node-state", parent)
    create_test_adapter(service, node_record["id"], {"name": "Local Codex", "plugin": "codex"}, ADAPTER_ID)
    native = CodexHostAdapter(tmp_path / "codex-state", parent.resolve(), rpc=FakeCodexRpc(),
                              worktrees_root=(tmp_path / "node-state" / "worktrees").resolve())
    client = DirectAdapter(native)
    factory = service.adapter_registry.client

    def adapter_client(adapter_id, **kwargs):
        if adapter_id == ADAPTER_ID:
            service.adapter_registry.get(adapter_id, require_enabled=kwargs.get("require_enabled", False))
            return client
        return factory(adapter_id, **kwargs)

    service.adapter_registry.client = adapter_client
    ws_id = service.add_workspace("Alpha", str(root), [])["workspace"]["id"]
    token = service.manage_bridge("rotate_token")["token"]
    service.manage_workspace(ws_id, "enable")
    service.manage_workspace(ws_id, "set_agent_enabled", agent_enabled=True)
    service.manage_workspace(ws_id, "set_write_scope", write_scope="workspace")
    service.set_workspace_route(service.workspace(ws_id), ADAPTER_ID, True, "trusted")
    service.run_coordinator.set_model_policy(ADAPTER_ID, ["gpt-test"], "gpt-test")
    job = service.call(ws_id, token, "prepare_handoff", Handoff.model_validate(payload).model_dump())
    env = {"service": service, "ws_id": ws_id, "token": token, "job": job, "native": native,
           "root": root, "payload": payload, "node": node["service"]}
    yield env
    service.close()
    native.close()
    node["stop"]()


def _call(env, name, **arguments):
    return env["service"].call(env["ws_id"], env["token"], name, arguments)


def _start(env, request_id="iso-1", **extra):
    return _call(env, "start_agent_run", adapter_id=ADAPTER_ID, job_id=env["job"]["id"],
                 request_id=request_id, **extra)


def _finish(env, run_id):
    _finish_test_run(env["service"], env["native"], env["ws_id"], run_id)
    env["service"].run_coordinator.checkpoints.process_pending()


# -- Node store ------------------------------------------------------------------


def _store(env):
    service = env["service"]
    ws = service.workspace(env["ws_id"])
    return env["node"], ws, env["node"].worktrees


def test_snapshot_holds_uncommitted_work_but_not_secrets_or_ignored_files(iso_env):
    root = iso_env["root"]
    (root / "app.py").write_text("print('uncommitted')\n")
    (root / "notes.md").write_text("untracked\n")
    (root / ".env").write_text("TOKEN=secret\n")
    (root / "build").mkdir()
    (root / "build" / "out.bin").write_text("ignored\n")
    status = _git(root, "status", "--porcelain")
    run = _start(iso_env, isolation="worktree")
    path = Path(run["isolation"]["path"])
    assert run["isolation"]["state"] == "active" and run["isolation"]["branch"].startswith("wb/wt_")
    assert (path / "app.py").read_text() == "print('uncommitted')\n"
    assert (path / "notes.md").read_text() == "untracked\n"
    assert not (path / ".env").exists() and not (path / "build").exists()
    # The user's checkout, index and branch are untouched.
    assert _git(root, "status", "--porcelain") == status
    assert _git(root, "branch", "--show-current").strip() == "main"
    assert ".env" not in _git(root, "show", "--name-only", run["isolation"]["base"])


def test_non_git_workspace_cannot_isolate(iso_env, tmp_path):
    plain = iso_env["root"].parent / "plain"
    plain.mkdir()
    (plain / "a.txt").write_text("a\n")
    service = iso_env["service"]
    ws = service.add_workspace("Plain", str(plain), [])["workspace"]
    service.manage_workspace(ws["id"], "enable")
    with pytest.raises(BridgeError) as caught:
        service.node_registry.client(ws["node_id"]).workspace(
            "worktree_create", service.workspace(ws["id"]), {"worktree_id": "wt_" + "0" * 24})
    assert caught.value.code == "isolation_unavailable"


def test_node_lets_conversations_start_only_in_its_own_live_worktrees(iso_env):
    run = _start(iso_env, isolation="worktree")
    store, ws_id, path = iso_env["node"].worktrees, iso_env["ws_id"], run["isolation"]["path"]
    assert store.owns(ws_id, path)
    assert not store.owns(ws_id, str(Path(path).parent)) and not store.owns("ws_other", path)
    assert not store.owns(ws_id, str(iso_env["root"])) and not store.owns(ws_id, None)
    _finish(iso_env, run["run_id"])
    _call(iso_env, "discard_run", run_id=run["run_id"])
    assert not store.owns(ws_id, path)


def test_plugins_accept_only_node_worktrees(tmp_path):
    projects, worktrees = tmp_path / "projects", tmp_path / "worktrees"
    (projects / "alpha").mkdir(parents=True)
    (worktrees / "ws_1" / "wt_1").mkdir(parents=True)
    root, trees = projects.resolve(), worktrees.resolve()
    assert run_directory(str(worktrees / "ws_1" / "wt_1"), root, trees).endswith("wt_1")
    for bad in (worktrees, worktrees / "ws_1"):
        with pytest.raises(ValueError):
            run_directory(str(bad), root, trees)
    with pytest.raises(ValueError):
        run_directory(str(worktrees / "ws_1" / "wt_1"), root, None)


# -- isolated runs ----------------------------------------------------------------


def test_isolated_run_works_apart_and_applies_as_one_undoable_edit(iso_env, monkeypatch):
    service, root = iso_env["service"], iso_env["root"]
    captured = []
    client = service.run_coordinator.adapter(ADAPTER_ID)
    original_create, original_start = client.create_conversation, client.start_run
    monkeypatch.setattr(client, "create_conversation",
                        lambda body: captured.append(("conversation", body)) or original_create(body))
    monkeypatch.setattr(client, "start_run",
                        lambda ident, body: captured.append(("run", body)) or original_start(ident, body))
    run = _start(iso_env, isolation="worktree")
    path = Path(run["isolation"]["path"])
    assert captured[0][1]["directory"] == str(path)
    prompt = captured[1][1]["input"][0]["text"]
    assert str(path) in prompt and run["isolation"]["branch"] in prompt and str(root) + '"' not in prompt
    (path / "app.py").write_text("print('two')\n")
    (path / "new.py").write_text("print('new')\n")
    _finish(iso_env, run["run_id"])
    assert (root / "app.py").read_text() == "print('one')\n" and not (root / "new.py").exists()
    diff = _call(iso_env, "git_diff", mode="run", run_id=run["run_id"])
    assert "print('two')" in diff["patch"] and "print('new')" in diff["patch"]
    assert diff["chatgpt_edits"] == {"status": "not_applicable", "reason": "isolated_run"}
    # The branch holds the run's end state for anyone reading it with Git.
    branch = run["isolation"]["branch"]
    assert "new.py" in _git(root, "show", "--name-only", "--format=", branch)
    applied = _call(iso_env, "apply_run", run_id=run["run_id"])
    assert applied["isolation"]["state"] == "applied" and applied["edit_id"]
    assert {item["path"] for item in applied["files"]} == {"app.py", "new.py"}
    assert (root / "app.py").read_text() == "print('two')\n" and (root / "new.py").exists()
    assert not path.exists() and branch not in _git(root, "branch", "--list", "wb/*")
    again = _call(iso_env, "apply_run", run_id=run["run_id"])
    assert again["repeated"] is True
    read = _call(iso_env, "read_agent_run", run_id=run["run_id"])
    assert read["isolation"]["state"] == "applied" and read["isolation"]["edit_id"] == applied["edit_id"]
    _call(iso_env, "undo_edit", edit_id=applied["edit_id"])
    assert (root / "app.py").read_text() == "print('one')\n" and not (root / "new.py").exists()
    with pytest.raises(BridgeError) as caught:
        _call(iso_env, "discard_run", run_id=run["run_id"])
    assert caught.value.code == "worktree_unavailable"


def test_apply_refuses_when_the_workspace_moved_on(iso_env):
    root = iso_env["root"]
    run = _start(iso_env, isolation="worktree")
    path = Path(run["isolation"]["path"])
    (path / "app.py").write_text("print('agent')\n")
    (path / "other.py").write_text("print('other')\n")
    _finish(iso_env, run["run_id"])
    (root / "app.py").write_text("print('user')\n")
    with pytest.raises(BridgeError) as caught:
        _call(iso_env, "apply_run", run_id=run["run_id"])
    assert caught.value.code == "apply_conflict" and "app.py" in str(caught.value)
    # All or nothing: the file without a conflict did not land either.
    assert not (root / "other.py").exists() and path.exists()
    discarded = _call(iso_env, "discard_run", run_id=run["run_id"])
    assert discarded["removed"] is True and discarded["isolation"]["state"] == "discarded"
    assert not path.exists() and (root / "app.py").read_text() == "print('user')\n"
    assert _call(iso_env, "discard_run", run_id=run["run_id"])["repeated"] is True


def test_continuation_keeps_the_worktree(iso_env, monkeypatch):
    first = _start(iso_env, isolation="worktree")
    _finish(iso_env, first["run_id"])
    path = Path(first["isolation"]["path"])
    (path / "first.txt").write_text("1\n")
    second_job = _second_job(iso_env["service"], iso_env["ws_id"], iso_env["token"], iso_env["payload"], "job-2")
    with pytest.raises(BridgeError) as caught:
        _call(iso_env, "start_agent_run", adapter_id=ADAPTER_ID, job_id=second_job["id"], request_id="iso-2",
              continue_from_run_id=first["run_id"], isolation="none")
    assert caught.value.code == "isolation_mismatch"
    second = _call(iso_env, "start_agent_run", adapter_id=ADAPTER_ID, job_id=second_job["id"],
                   request_id="iso-2", continue_from_run_id=first["run_id"])
    assert second["isolation"]["worktree_id"] == first["isolation"]["worktree_id"]
    with pytest.raises(BridgeError) as caught:
        _call(iso_env, "apply_run", run_id=first["run_id"])
    assert caught.value.code == "run_active"
    (path / "second.txt").write_text("2\n")
    _finish(iso_env, second["run_id"])
    applied = _call(iso_env, "apply_run", run_id=first["run_id"])
    assert {item["path"] for item in applied["files"]} == {"first.txt", "second.txt"}
    third_job = _second_job(iso_env["service"], iso_env["ws_id"], iso_env["token"], iso_env["payload"], "job-3")
    with pytest.raises(BridgeError) as caught:
        _call(iso_env, "start_agent_run", adapter_id=ADAPTER_ID, job_id=third_job["id"], request_id="iso-3",
              continue_from_run_id=second["run_id"])
    assert caught.value.code == "worktree_unavailable"


def test_ordinary_runs_report_no_isolation(iso_env):
    run = _start(iso_env)
    assert run["isolation"] == {"mode": "none"}
    _finish(iso_env, run["run_id"])
    with pytest.raises(BridgeError) as caught:
        _call(iso_env, "apply_run", run_id=run["run_id"])
    assert caught.value.code == "not_isolated"
    second_job = _second_job(iso_env["service"], iso_env["ws_id"], iso_env["token"], iso_env["payload"], "job-2")
    with pytest.raises(BridgeError) as caught:
        _call(iso_env, "start_agent_run", adapter_id=ADAPTER_ID, job_id=second_job["id"], request_id="iso-2",
              continue_from_run_id=run["run_id"], isolation="worktree")
    assert caught.value.code == "isolation_mismatch"


def test_idempotent_retry_must_match_isolation(iso_env):
    run = _start(iso_env, isolation="worktree")
    assert _start(iso_env, isolation="worktree")["idempotent"] is True
    assert _start(iso_env)["run_id"] == run["run_id"]
    with pytest.raises(BridgeError) as caught:
        _start(iso_env, isolation="none")
    assert caught.value.code == "conflict"


def test_isolation_needs_plugin_support_and_edit_scope_to_apply(iso_env, monkeypatch):
    listed = _call(iso_env, "list_agent_adapters")["adapters"]
    assert [item["isolation"] for item in listed] == [["none", "worktree"]]
    monkeypatch.setattr(RunWorktrees, "supported", staticmethod(lambda adapter: False))
    with pytest.raises(BridgeError) as caught:
        _start(iso_env, isolation="worktree")
    assert caught.value.code == "isolation_unavailable"
    monkeypatch.undo()
    run = _start(iso_env, "iso-2", isolation="worktree")
    _finish(iso_env, run["run_id"])
    iso_env["service"].manage_workspace(iso_env["ws_id"], "set_write_scope", write_scope="none")
    with pytest.raises(BridgeError) as caught:
        _call(iso_env, "apply_run", run_id=run["run_id"])
    assert caught.value.code == "policy_denied"
    # Discarding changes no workspace file, so a read-only workspace may still do it.
    assert _call(iso_env, "discard_run", run_id=run["run_id"])["removed"] is True


def test_failed_conversation_start_removes_the_new_worktree(iso_env, monkeypatch):
    client = iso_env["service"].run_coordinator.adapter(ADAPTER_ID)

    def refuse(body):
        raise BridgeError("No conversation", "runtime_unavailable")

    monkeypatch.setattr(client, "create_conversation", refuse)
    with pytest.raises(BridgeError):
        _start(iso_env, isolation="worktree")
    service = iso_env["service"]
    with service.lock:
        rows = [dict(row) for row in service.db.execute("SELECT state,error,path FROM agent_worktrees")]
    assert len(rows) == 1 and rows[0]["state"] == "failed" and rows[0]["error"] == "run_not_started"
    assert not Path(rows[0]["path"]).exists()


def test_retention_removes_idle_worktrees_after_fourteen_days(iso_env):
    run = _start(iso_env, isolation="worktree")
    worktrees = iso_env["service"].run_coordinator.worktrees
    assert worktrees.apply_retention(time.time() + RETENTION_SECONDS + 60) == 0  # still running
    _finish(iso_env, run["run_id"])
    assert worktrees.apply_retention() == 0
    assert worktrees.apply_retention(time.time() + RETENTION_SECONDS + 60) == 1
    path = Path(run["isolation"]["path"])
    assert not path.exists()
    assert _call(iso_env, "read_agent_run", run_id=run["run_id"])["isolation"]["state"] == "expired"
    with pytest.raises(BridgeError) as caught:
        _call(iso_env, "apply_run", run_id=run["run_id"])
    assert caught.value.code == "worktree_unavailable"
