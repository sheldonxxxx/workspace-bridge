import os
from pathlib import Path
import pytest
from workspace_bridge.security import SafeRoot, BridgeError, allowed, parts, redact, MAX_FILE
from workspace_bridge.cli import initialize

@pytest.mark.parametrize("path", ["../secret", "/etc/passwd", "src/../../x", "src//main.py", "./src/main.py", "src/./main.py", "src/", "C:\\secret", "src/\x00", "src\nname", "a:b"])
def test_paths_reject(path):
    with pytest.raises(BridgeError): parts(path)

@pytest.mark.parametrize("path", [".env", ".env.local", "src/.env.test", ".git/config", ".SSH/key", "auth.pem", "a/credentials.json", "node_modules/a.js", ".workspace-handoff/jobs/a/TASK.md", "a/secret.key", ".npmrc", "cache.sqlite3"])
def test_default_denies(path):
    assert not allowed(path)

@pytest.mark.parametrize("path", ["src/main.py", "README.md", "package.json", ".github/workflows/test.yml", "src/東京.py"])
def test_normal_paths_allowed(path):
    assert allowed(path)

def test_custom_policy():
    assert not allowed("private/notes.md", ["private/**"])
    assert not allowed("foo/bar.txt", ["*.txt"])

def test_symlink_final_and_parent(env):
    outside = env["tmp"] / "outside"; outside.mkdir(); (outside / "data").write_text("secret")
    (env["root"] / "leak").symlink_to(outside / "data")
    (env["root"] / "dirlink").symlink_to(outside, target_is_directory=True)
    with SafeRoot(str(env["root"])) as safe:
        for p in ["leak", "dirlink/data"]:
            with pytest.raises(BridgeError): safe.read(p)
        files, skipped = safe.walk()
        assert len(skipped) == 2
        assert "leak" not in [f["path"] for f in files]

def test_hardlinks(env):
    external = env["tmp"] / "external"; external.write_text("sensitive")
    os.link(external, env["root"] / "hard")
    with SafeRoot(str(env["root"])) as safe:
        with pytest.raises(BridgeError): safe.read("hard")

def test_fifo_nonblocking(env):
    os.mkfifo(env["root"] / "pipe")
    with SafeRoot(str(env["root"])) as safe:
        with pytest.raises(BridgeError): safe.read("pipe")

def test_oversize(env):
    (env["root"] / "large.txt").write_bytes(b"a" * (MAX_FILE + 1))
    with SafeRoot(str(env["root"])) as safe:
        with pytest.raises(BridgeError): safe.read("large.txt")

def test_root_replaced(env):
    # Same configured path stays usable after a remount-like replacement
    # with a fresh directory: historical device/inode must not gate access.
    s = env["service"]; ws = s.workspace(env["id"])
    env["root"].rename(env["parent"] / "old")
    env["root"].mkdir()
    info = s.info(ws)
    assert info["id"] == env["id"]

def test_root_symlink_replaced(env):
    s = env["service"]; ws = s.workspace(env["id"])
    env["root"].rename(env["parent"] / "old")
    env["root"].symlink_to(env["parent"] / "old", target_is_directory=True)
    with pytest.raises(BridgeError) as exc: s.info(ws)
    assert exc.value.code != "root_changed"

def test_handoff_symlink_never_writes_outside(env):
    external = env["tmp"] / "outside"; external.mkdir()
    (env["root"] / ".workspace-handoff").symlink_to(external, target_is_directory=True)
    with SafeRoot(str(env["root"])) as safe:
        with pytest.raises(BridgeError): safe.create_artifact(".workspace-handoff/jobs/a/TASK.md", b"x")
    assert list(external.iterdir()) == []

def test_artifacts_are_create_only(env):
    with SafeRoot(str(env["root"])) as safe:
        p = ".workspace-handoff/jobs/a/TASK.md"
        safe.create_artifact(p, b"original")
        with pytest.raises(BridgeError): safe.create_artifact(p, b"replacement")
        assert safe.read(p, artifact=True)[0] == b"original"
        with pytest.raises(BridgeError): safe.create_artifact("src/main.py", b"bad")

def test_redaction():
    text = 'key=sk-proj-' + 'a' * 40 + '\npassword = "verysecret123"\nAKIAABCDEFGHIJKLMNOP'
    safe, changed = redact(text)
    assert changed and "verysecret123" not in safe and "sk-proj-" not in safe

def test_mapping_parent_and_overlap(env):
    s = env["service"]
    with pytest.raises(BridgeError): s.add_workspace("Elsewhere", str(env["tmp"]), [])
    with pytest.raises(BridgeError): s.add_workspace("Parent", str(env["parent"]), [])
    with pytest.raises(BridgeError): s.add_workspace("Nested", str(env["root"] / "src"), [])
    s.manage_workspace(env["id"], "disable")
    with pytest.raises(BridgeError): s.add_workspace("Duplicate", str(env["root"]), [])

def test_init_preserves_state(env):
    before = (env["state"] / "config.json").read_bytes()
    with pytest.raises(BridgeError): initialize(env["state"], [str(env["parent"])], 8765, 8766)
    assert (env["state"] / "config.json").read_bytes() == before

def test_no_symlink_ancestor(env):
    alias = env["tmp"] / "alias"; alias.symlink_to(env["parent"], target_is_directory=True)
    with pytest.raises(BridgeError): SafeRoot(str(alias / "alpha"))

@pytest.mark.parametrize('path', ['.WORKSPACE-HANDOFF/jobs/a/TASK.md', 'src/.workspace-handoff/file'])
def test_casefold_handoff_is_reserved(path):
    assert not allowed(path)

def test_casefold_extra_policy():
    assert not allowed('PRIVATE/key.txt', ['private/*'])

def test_removed_parent_revokes_access(env):
    from workspace_bridge.service import Service
    other = env['tmp'] / 'other-parent'; other.mkdir()
    second = Service(env['state'], {**env['config'], 'allowed_parents': [str(other)]})
    try:
        with pytest.raises(BridgeError, match='approved parent'):
            second.info(second.workspace(env['id']))
    finally:
        second.close()
