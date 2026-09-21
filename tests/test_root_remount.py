"""Same configured path must stay usable when filesystem identity changes.

Simulates reboot/remount/volume-remount by tampering the persisted
dev/ino fingerprint rather than performing a real remount.
"""
from pathlib import Path

import pytest

from workspace_bridge.api import Handoff
from workspace_bridge.security import BridgeError, SafeRoot
from workspace_bridge.service import Service


def simulate_remount(env, dev=999999999, ino=888888888):
    s = env["service"]
    with s.lock, s.db:
        s.db.execute("UPDATE workspaces SET dev=?, ino=? WHERE id=?", (dev, ino, env["id"]))
    row = s.workspace(env["id"], False)
    assert (row["dev"], row["ino"]) == (dev, ino)
    return row


def call(env, tool, **args):
    return env["service"].call(env["id"], env["token"], tool, args)


def test_stored_identity_mismatch_does_not_block_lookup_info_and_reads(env):
    ws_before = env["service"].workspace(env["id"])
    simulate_remount(env)
    # Lookup still succeeds.
    ws = env["service"].workspace(env["id"])
    assert ws["id"] == env["id"]
    assert ws["root"] == ws_before["root"]
    # workspace_info still succeeds.
    info = call(env, "workspace_info")
    assert info["workspace_id"] == env["id"]
    # Normal allowed reads still succeed.
    read = call(env, "read_file", path="src/main.py", start_line=1, max_lines=200,
                expected_sha256=None)
    assert read["path"] == "src/main.py"
    assert any("return a + b" in line["text"] for line in read["lines"])
    listed = call(env, "list_dir", path="", depth=2, offset=0, limit=60,
                  expected_listing_sha256=None)
    assert any(e["path"] == "src/main.py" for e in listed["entries"])
    globbed = call(env, "glob", pattern="**/*.py", path="", offset=0, limit=60,
                   expected_listing_sha256=None)
    assert any(e["path"] == "src/main.py" for e in globbed["entries"])
    grepped = call(env, "grep_files", pattern="return", path="", include="**/*.py")
    assert grepped["matches"]


def test_safe_root_ignores_historical_identity(env):
    # Direct SafeRoot construction with a bogus historical fingerprint
    # must not raise root_changed.
    with SafeRoot(str(env["root"]), (12345, 67890)) as safe:
        data, _ = safe.read("README.md")
    assert b"Alpha" in data


def test_same_path_after_restart_stays_usable(env):
    simulate_remount(env, dev=111111, ino=222222)
    env["service"].close()
    reopened = Service(env["state"], env["config"])
    try:
        ws = reopened.workspace(env["id"])
        info = reopened.info(ws)
        assert info["id"] == env["id"]
        # Reads through the reopened service also succeed despite stale dev/ino.
        data = reopened.read_file(ws, "src/main.py", 1, 200, None)
        assert data["path"] == "src/main.py"
    finally:
        reopened.close()
    # Reopen the original fixture service connection for teardown safety.
    # The fixture's service was closed; create a fresh one in its place so
    # fixture teardown (service.close) does not fail on a closed db.
    from workspace_bridge.service import Service as _Service
    env["service"] = _Service(env["state"], env["config"])


def test_opencode_execution_remains_available_after_remount(agent_env, payload):
    s = agent_env["service"]
    with s.lock, s.db:
        s.db.execute("UPDATE workspaces SET dev=?, ino=? WHERE id=?",
                     (42424242, 43434343, agent_env["id"]))
    job = s.call(agent_env["id"], agent_env["token"], "prepare_handoff",
                 Handoff.model_validate(payload).model_dump())
    assert job["id"].startswith("job_")
    run = s.call(agent_env["id"], agent_env["token"], "start_opencode_run",
                 {"job_id": job["id"], "request_id": "remount-run-1",
                  "model": None, "parent_run_id": None})
    assert run["run_id"].startswith("run_")
    models = s.call(agent_env["id"], agent_env["token"], "list_opencode_models",
                    {"query": "", "limit": 25})
    assert models["count"] >= 1


def test_containment_still_enforced_after_remount(env):
    simulate_remount(env)
    # ../ traversal outside the workspace.
    with pytest.raises(BridgeError):
        call(env, "read_file", path="../secret", start_line=1, max_lines=200,
             expected_sha256=None)
    with pytest.raises(BridgeError):
        call(env, "read_file", path="src/../../x", start_line=1, max_lines=200,
             expected_sha256=None)
    # Symlink escape outside the workspace.
    outside = env["tmp"] / "outside-secret"
    outside.mkdir(exist_ok=True)
    (outside / "data.txt").write_text("secret")
    (env["root"] / "leak").symlink_to(outside / "data.txt")
    try:
        with pytest.raises(BridgeError):
            call(env, "read_file", path="leak", start_line=1, max_lines=200,
                 expected_sha256=None)
    finally:
        (env["root"] / "leak").unlink()
    # Excluded paths.
    env["service"].manage_workspace(env["id"], "set_excludes", excludes=["*.md"])
    try:
        with pytest.raises(BridgeError):
            call(env, "read_file", path="README.md", start_line=1, max_lines=200,
                 expected_sha256=None)
    finally:
        env["service"].manage_workspace(env["id"], "set_excludes", excludes=[])
    # Writes beyond write_scope (default handoff-only denies source writes).
    with pytest.raises(BridgeError):
        call(env, "write_file", path="src/main.py", content="x = 1\n")
    # Accessing another registered workspace stays isolated.
    beta_root = env["parent"] / "beta"
    beta_root.mkdir(exist_ok=True)
    (beta_root / "README.md").write_text("# Beta\n")
    beta = env["service"].add_workspace("Beta", str(beta_root), [])
    beta_id = beta["workspace"]["id"]
    env["service"].manage_workspace(beta_id, "enable")
    try:
        beta_read = env["service"].call(beta_id, env["token"], "read_file",
                                        {"path": "README.md", "start_line": 1,
                                         "max_lines": 200, "expected_sha256": None})
        assert beta_read["lines"][0]["text"] == "# Beta"
        # A handoff owned by Alpha is not readable through Beta.
        job = env["service"].call(env["id"], env["token"], "prepare_handoff",
                                 Handoff.model_validate({
                                     "request_id": "remount-isolation-1",
                                     "title": "Isolation check",
                                     "goal": "Check isolation",
                                     "plan": "Read the file.",
                                     "acceptance": "No cross access.",
                                     "constraints": "Do not commit.",
                                     "context": "Isolation.",
                                     "context_hashes": {},
                                 }).model_dump())
        with pytest.raises(BridgeError):
            env["service"].call(beta_id, env["token"], "read_handoff",
                                {"job_id": job["id"], "document": "TASK.md",
                                 "start_line": 1, "max_lines": 40})
    finally:
        env["service"].manage_workspace(beta_id, "disable")


def test_missing_root_fails_without_root_changed(env):
    # Genuinely missing root must fail clearly, never with root_changed.
    env["root"].rename(env["parent"] / "gone-away")
    ws = env["service"].workspace(env["id"])
    with pytest.raises(BridgeError) as exc:
        env["service"].info(ws)
    assert exc.value.code != "root_changed"
    with pytest.raises(BridgeError) as exc:
        call(env, "read_file", path="src/main.py", start_line=1, max_lines=200,
             expected_sha256=None)
    assert exc.value.code != "root_changed"


def test_invalid_root_fails_safely_without_root_changed(env, tmp_path):
    # Configured root that resolves to a regular file (or symlink) is invalid
    # as a workspace directory and must fail closed, not with root_changed.
    ws = env["service"].workspace(env["id"])
    env["root"].rename(env["parent"] / "real-dir")
    (env["root"]).write_text("not a directory")
    try:
        with pytest.raises(BridgeError) as exc:
            env["service"].info(ws)
        assert exc.value.code != "root_changed"
    finally:
        env["root"].unlink()
        (env["parent"] / "real-dir").rename(env["root"])
    # Symlink at the configured root must also fail closed.
    env["root"].rename(env["parent"] / "real-dir-2")
    env["root"].symlink_to(env["parent"] / "real-dir-2", target_is_directory=True)
    try:
        with pytest.raises(BridgeError) as exc:
            env["service"].info(ws)
        assert exc.value.code != "root_changed"
    finally:
        env["root"].unlink()
        (env["parent"] / "real-dir-2").rename(env["root"])
    # After restoring the real directory the same path is usable again.
    info = env["service"].info(env["service"].workspace(env["id"]))
    assert info["id"] == env["id"]
