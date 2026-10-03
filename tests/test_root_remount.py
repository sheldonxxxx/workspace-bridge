"""The Node remains the filesystem authority across Bridge restarts/remounts."""
from pathlib import Path

import pytest

from workspace_bridge.security import BridgeError, SafeRoot
from workspace_bridge.service import Service


def call(env, tool, **args):
    return env["service"].call(env["id"], env["token"], tool, args)


def test_authoritative_node_serves_info_reads_and_search(env):
    info = call(env, "workspace_info")
    assert info["node_id"] == env["node_id"]
    assert info["node_name"] == "Local Node"
    assert info["root"] == str(env["root"])
    result = call(env, "read_file", path="src/main.py", start_line=1, max_lines=200,
                  expected_sha256=None)
    assert any("return a + b" in line["text"] for line in result["lines"])
    assert call(env, "grep_files", pattern="return", include="**/*.py")["matches"]


def test_same_path_after_replacement_stays_node_authoritative(env):
    root = env["root"]
    root.rename(env["parent"] / "old")
    root.mkdir()
    (root / "README.md").write_text("# Remounted project\n")
    result = call(env, "read_file", path="README.md", start_line=1, max_lines=20,
                  expected_sha256=None)
    assert result["lines"][0]["text"] == "# Remounted project"
    registered = env["node"]["service"].authoritative_workspace(
        {"id": env["id"], "root": str(root), "excludes": [], "write_scope": "workspace"})
    assert registered["root"] == str(root)


def test_node_workspace_identity_cannot_be_rebound(env):
    other = env["parent"] / "other"
    other.mkdir()
    with pytest.raises(BridgeError) as exc:
        env["node"]["service"].register_workspace({
            "id": env["id"], "root": str(other), "excludes": [],
            "write_scope": "handoff"})
    assert exc.value.code == "workspace_authority_mismatch"


def test_unavailable_node_never_falls_back_to_bridge_local_files(env):
    bridge = env["service"]
    bridge._node_transport = {}
    with pytest.raises(BridgeError) as exc:
        call(env, "read_file", path="src/main.py", start_line=1, max_lines=20,
             expected_sha256=None)
    assert exc.value.code == "node_unavailable"


def test_safe_root_ignores_historical_identity(env):
    with SafeRoot(str(env["root"]), (12345, 67890)) as safe:
        data, _ = safe.read("README.md")
    assert b"Alpha" in data or b"Remounted" in data


def test_containment_and_symlink_escape_stay_enforced(env):
    for path in ("../secret", "src/../../x"):
        with pytest.raises(BridgeError):
            call(env, "read_file", path=path, start_line=1, max_lines=20,
                 expected_sha256=None)
    outside = env["tmp"] / "outside-secret"
    outside.mkdir(exist_ok=True)
    (outside / "data.txt").write_text("secret")
    (env["root"] / "leak").symlink_to(outside / "data.txt")
    try:
        with pytest.raises(BridgeError):
            call(env, "read_file", path="leak", start_line=1, max_lines=20,
                 expected_sha256=None)
    finally:
        (env["root"] / "leak").unlink()
