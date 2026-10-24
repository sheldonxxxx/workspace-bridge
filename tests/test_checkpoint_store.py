"""Node shadow checkpoint store: capture, comparison, retention and isolation."""
import hashlib
import os
from pathlib import Path
import sqlite3
import subprocess
import threading

import pytest

from workspace_bridge import checkpoint_store as module
from workspace_bridge.checkpoint_store import CheckpointStore
from workspace_bridge.security import BridgeError, SafeRoot

WS = "ws_" + "a" * 24


def _git(root: Path, *args: str) -> str:
    env = {"PATH": "/usr/bin:/bin:/usr/local/bin", "LC_ALL": "C", "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0"}
    return subprocess.run(["git", *args], cwd=root, env=env, text=True, check=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout


def _repo(root: Path) -> None:
    _git(root, "init", "-q", "--initial-branch=main")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "user.email", "test@example.invalid")
    (root / "tracked.txt").write_text("base\n")
    _git(root, "add", "tracked.txt")
    _git(root, "commit", "-qm", "base")


def _tree_digest(path: Path) -> dict:
    result = {}
    for directory, _, files in os.walk(path):
        for name in files:
            full = Path(directory) / name
            st = full.lstat()
            result[str(full.relative_to(path))] = (hashlib.sha256(full.read_bytes()).hexdigest(),
                                                   st.st_mtime_ns, st.st_size)
    return result


@pytest.fixture
def setup(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    root = tmp_path / "work"
    root.mkdir()
    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.row_factory = sqlite3.Row
    store = CheckpointStore(state, db, threading.RLock())
    ws = {"id": WS, "root": str(root), "excludes": []}
    counter = iter(range(1, 10000))

    def capture(**kwargs):
        checkpoint_id = "ckp_" + format(next(counter), "024x")
        with SafeRoot(str(root), None, ws["excludes"]) as safe:
            return store.capture(safe, ws, checkpoint_id, **kwargs)

    return {"store": store, "root": root, "ws": ws, "state": state, "capture": capture}


def _diff(setup, before, after, **kwargs):
    return setup["store"].diff(setup["ws"], from_id=before["checkpoint_id"],
                               to_id=after["checkpoint_id"], **kwargs)


def _changes(setup, before, after):
    return setup["store"].changes(setup["ws"], from_id=before["checkpoint_id"],
                                  to_id=after["checkpoint_id"])["changes"]


def test_run_window_diff_excludes_preexisting_dirty_changes(setup):
    root = setup["root"]
    _repo(root)
    (root / "tracked.txt").write_text("dirty before the run\n")
    before = setup["capture"]()
    (root / "new.py").write_text("print('agent')\n")
    after = setup["capture"]()
    assert before["status"] == after["status"] == "complete"
    assert _changes(setup, before, after) == [{"path": "new.py", "status": "added"}]
    patch = _diff(setup, before, after)["patch"]
    assert "print('agent')" in patch and "dirty before the run" not in patch


def test_user_git_metadata_is_never_written(setup):
    root = setup["root"]
    _repo(root)
    (root / "untracked.txt").write_text("x\n")
    snapshot = _tree_digest(root / ".git")
    first = setup["capture"]()
    (root / "untracked.txt").write_text("y\n")
    second = setup["capture"]()
    _diff(setup, first, second)
    assert _tree_digest(root / ".git") == snapshot


def test_non_git_workspace_is_captured(setup):
    root = setup["root"]
    (root / "notes.md").write_text("one\n")
    before = setup["capture"]()
    (root / "notes.md").write_text("two\n")
    after = setup["capture"]()
    assert after["skipped"]["listing"] == "ok"
    assert _changes(setup, before, after) == [{"path": "notes.md", "status": "modified"}]


def test_policy_ignored_and_unsafe_entries_never_enter_the_store(setup):
    root = setup["root"]
    _repo(root)
    (root / ".gitignore").write_text("generated/\n")
    (root / "generated").mkdir()
    (root / "generated" / "big.txt").write_text("ignored\n")
    (root / ".env").write_text("TOKEN=supersecretvalue\n")
    (root / "node_modules").mkdir()
    (root / "node_modules" / "dep.js").write_text("dep\n")
    (root / "real.txt").write_text("real\n")
    os.symlink(root / "real.txt", root / "link.txt")
    os.link(root / "real.txt", root / "hard.txt")
    result = setup["capture"]()
    listed = _git(setup["state"] / "checkpoints" / f"{WS}.git", "ls-tree", "-r", "--name-only",
                  f"refs/wb/checkpoints/{result['checkpoint_id']}").split()
    assert sorted(listed) == [".gitignore", "tracked.txt"]
    assert result["skipped"]["symlink_or_mount"] == 1
    assert result["skipped"]["special_or_hardlinked"] == 2


def test_new_exclusion_hides_old_checkpoint_content(setup):
    root = setup["root"]
    (root / "keep.txt").write_text("a\n")
    (root / "private.txt").write_text("a\n")
    before = setup["capture"]()
    (root / "keep.txt").write_text("b\n")
    (root / "private.txt").write_text("b\n")
    after = setup["capture"]()
    setup["ws"]["excludes"] = ["private.txt"]
    assert [item["path"] for item in _changes(setup, before, after)] == ["keep.txt"]
    with pytest.raises(BridgeError) as exc:
        _diff(setup, before, after, path="private.txt")
    assert exc.value.code == "not_found"


def test_oversized_file_is_a_placeholder_not_a_deletion(setup):
    root = setup["root"]
    (root / "large.bin").write_bytes(b"a" * (module.MAX_FILE + 1))
    before = setup["capture"]()
    (root / "large.bin").write_bytes(b"b" * (module.MAX_FILE + 2))
    after = setup["capture"]()
    assert after["status"] == "complete" and after["skipped"]["too_large"] == 1
    assert _changes(setup, before, after) == [
        {"path": "large.bin", "status": "modified", "not_captured": "too_large"}]
    assert "content not captured: too_large" in _diff(setup, before, after)["patch"]


def test_over_cap_is_partial_and_tracked_files_come_first(setup):
    root = setup["root"]
    _repo(root)
    for name in ("a.txt", "b.txt"):
        (root / name).write_text(name + "\n")
    result = setup["capture"](max_files=1)
    assert result["status"] == "partial" and result["skipped"]["over_cap"] == 2
    listed = _git(setup["state"] / "checkpoints" / f"{WS}.git", "show",
                  f"refs/wb/checkpoints/{result['checkpoint_id']}:tracked.txt")
    assert listed == "base\n"


def test_incremental_capture_reuses_unchanged_blobs(setup, monkeypatch):
    root = setup["root"]
    (root / "same.txt").write_text("same\n")
    old = os.stat(root / "same.txt")
    os.utime(root / "same.txt", ns=(old.st_atime_ns, old.st_mtime_ns - 10_000_000_000))
    first = setup["capture"]()
    reads = []
    original = SafeRoot.read

    def counting(self, path, **kwargs):
        reads.append(path)
        return original(self, path, **kwargs)

    monkeypatch.setattr(SafeRoot, "read", counting)
    (root / "changed.txt").write_text("new\n")
    second = setup["capture"]()
    assert reads == ["changed.txt"]
    assert _changes(setup, first, second) == [{"path": "changed.txt", "status": "added"}]


def test_capture_is_idempotent_for_one_checkpoint_id(setup):
    (setup["root"] / "a.txt").write_text("a\n")
    ws = setup["ws"]
    with SafeRoot(str(setup["root"]), None, []) as safe:
        first = setup["store"].capture(safe, ws, "ckp_" + "f" * 24)
        (setup["root"] / "a.txt").write_text("changed\n")
        again = setup["store"].capture(safe, ws, "ckp_" + "f" * 24)
    assert again["checkpoint_id"] == first["checkpoint_id"]
    assert again["created"] == first["created"]


def test_timeout_leaves_no_checkpoint(setup, monkeypatch):
    (setup["root"] / "a.txt").write_text("a\n")
    clock = iter([0.0] + [1000.0] * 100)
    monkeypatch.setattr(module.time, "monotonic", lambda: next(clock))
    with pytest.raises(BridgeError) as exc:
        setup["capture"]()
    assert exc.value.code == "checkpoint_timeout"
    assert setup["store"].stats(setup["ws"])["checkpoints"] == 0


def test_failed_import_writes_no_ref(setup, monkeypatch):
    (setup["root"] / "a.txt").write_text("a\n")
    original = CheckpointStore._git

    def failing(self, repo, args, **kwargs):
        if args and args[0] == "fast-import":
            raise BridgeError("boom", "checkpoint_storage")
        return original(self, repo, args, **kwargs)

    monkeypatch.setattr(CheckpointStore, "_git", failing)
    with pytest.raises(BridgeError):
        setup["capture"]()
    monkeypatch.setattr(CheckpointStore, "_git", original)
    repo = setup["state"] / "checkpoints" / f"{WS}.git"
    assert _git(repo, "for-each-ref").strip() == ""
    assert setup["store"].stats(setup["ws"])["checkpoints"] == 0


def test_purge_removes_refs_and_resets_the_stat_cache(setup):
    root = setup["root"]
    (root / "a.txt").write_text("a\n")
    first = setup["capture"]()
    (root / "a.txt").write_text("b\n")
    second = setup["capture"]()
    result = setup["store"].purge(setup["ws"], checkpoint_ids=[first["checkpoint_id"]])
    assert result["removed"] == 1
    with pytest.raises(BridgeError) as exc:
        _diff(setup, first, second)
    assert exc.value.code == "checkpoint_expired"
    third = setup["capture"]()
    assert _changes(setup, second, third) == []
    assert setup["store"].purge(setup["ws"], everything=True)["store_bytes"] == 0
    assert not (setup["state"] / "checkpoints" / f"{WS}.git").exists()


def test_diff_output_is_redacted(setup):
    root = setup["root"]
    (root / "config.py").write_text("x = 1\n")
    before = setup["capture"]()
    (root / "config.py").write_text("api_key = 'abcdefghijklmnopqrstuvwxyz'\n")
    after = setup["capture"]()
    page = _diff(setup, before, after)
    assert page["redacted"] is True and "abcdefghijklmnop" not in page["patch"]


def test_unusual_file_names_round_trip(setup):
    root = setup["root"]
    names = ['with space.txt', '"quoted".txt', '中文 檔案.md']
    # Backslashes are outside the normal read policy, so they stay out too.
    for name in [*names, 'back\\slash.txt']:
        (root / name).write_text("before\n")
    before = setup["capture"]()
    for name in [*names, 'back\\slash.txt']:
        (root / name).write_text("after\n")
    after = setup["capture"]()
    assert sorted(item["path"] for item in _changes(setup, before, after)) == sorted(names)
    assert _diff(setup, before, after, path='"quoted".txt')["patch"].count("+after") == 1


def test_invalid_identifiers_fail_closed(setup):
    with SafeRoot(str(setup["root"]), None, []) as safe:
        with pytest.raises(BridgeError):
            setup["store"].capture(safe, setup["ws"], "../escape")
        with pytest.raises(BridgeError):
            setup["store"].capture(safe, {**setup["ws"], "id": "../ws"}, "ckp_" + "1" * 24)
