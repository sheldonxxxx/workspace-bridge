"""Read-only Git evidence policy, dispatch, and bounded subprocess regression tests."""
import json
import os
from pathlib import Path
import subprocess
import sys

import httpx
import pytest

from workspace_bridge.api import TOOLS, make_mcp
from workspace_bridge.security import BridgeError


def _git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    env = {
        "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
        "LC_ALL": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
    }
    return subprocess.run(["git", *args], cwd=root, env=env, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=check)


def _repo(root: Path) -> str:
    _git(root, "init", "-q", "--initial-branch=main")
    _git(root, "config", "user.name", "Workspace Bridge Test")
    _git(root, "config", "user.email", "bridge-test@example.invalid")
    (root / "tracked.txt").write_text("base\n")
    _git(root, "add", "tracked.txt")
    _git(root, "commit", "-qm", "base")
    return "main"


def _call(env, name: str, **arguments):
    return env["service"].call(env["id"], env["token"], name, arguments)


def test_non_git_workspace_is_available_false_and_diff_fails_cleanly(env):
    status = _call(env, "git_status")
    assert status["available"] is False
    assert status["reason"] == "not_a_repository"
    assert status["entries"] == [] and status["hidden_count"] == 0
    with pytest.raises(BridgeError) as exc:
        _call(env, "git_diff", mode="head")
    assert exc.value.code == "repository_unavailable"
    with pytest.raises(BridgeError):
        env["service"].call(env["id"], env["token"], "read_file",
                            {"path": ".git/config", "start_line": 1,
                             "max_lines": 10, "expected_sha256": None})


def test_git_metadata_stays_denied_to_normal_file_tools(env):
    _repo(env["root"])
    with pytest.raises(BridgeError) as exc:
        env["service"].call(env["id"], env["token"], "read_file",
                            {"path": ".git/config", "start_line": 1,
                             "max_lines": 10, "expected_sha256": None})
    assert exc.value.code == "policy_denied"


def test_metadata_validation_does_not_depend_on_object_database_entry_count(env, monkeypatch):
    _repo(env["root"])
    # These are the former recursive walk limits. Setting them below zero
    # deterministically models a repository whose object/ref inventory would
    # exceed the old bound without creating tens of thousands of test files.
    import workspace_bridge.git_evidence as git_evidence

    monkeypatch.setattr(git_evidence, "MAX_METADATA_ENTRIES", -1, raising=False)
    monkeypatch.setattr(git_evidence, "MAX_METADATA_SECONDS", -1, raising=False)
    status = _call(env, "git_status")
    assert status["available"] is True
    assert status["status_sha256"]


def test_status_represents_staged_unstaged_untracked_and_conflicts(env):
    _repo(env["root"])
    (env["root"] / "tracked.txt").write_text("staged\n")
    _git(env["root"], "add", "tracked.txt")
    (env["root"] / "tracked.txt").write_text("unstaged\n")
    (env["root"] / "new.txt").write_text("new\n")
    status = _call(env, "git_status")
    entries = {item["path"]: item for item in status["entries"]}
    assert entries["tracked.txt"]["staged"] is True
    assert entries["tracked.txt"]["unstaged"] is True
    assert entries["new.txt"]["untracked"] is True
    assert entries["new.txt"]["staged"] is False
    assert entries["new.txt"]["unstaged"] is False

    branch = _git(env["root"], "branch", "--show-current").stdout.strip()
    _git(env["root"], "switch", "-qc", "topic")
    (env["root"] / "tracked.txt").write_text("topic\n")
    _git(env["root"], "commit", "-qam", "topic change")
    _git(env["root"], "switch", "-q", branch)
    (env["root"] / "tracked.txt").write_text("main change\n")
    _git(env["root"], "commit", "-qam", "main change")
    _git(env["root"], "merge", "--no-edit", "topic", check=False)
    conflicted = _call(env, "git_status")
    conflict = next(item for item in conflicted["entries"] if item["path"] == "tracked.txt")
    assert conflict["conflicted"] is True
    assert conflict["conflict_stages"] == [1, 2, 3]


def test_diff_modes_pagination_and_stale_status_hash(env):
    _repo(env["root"])
    staged_content = "staged-change\n" + "".join(f"staged-line-{i}-{'s' * 30}\n" for i in range(40))
    worktree_content = "worktree-change\n" + "".join(f"worktree-line-{i}-{'w' * 30}\n" for i in range(40))
    (env["root"] / "tracked.txt").write_text(staged_content)
    _git(env["root"], "add", "tracked.txt")
    (env["root"] / "tracked.txt").write_text(worktree_content)
    head = _call(env, "git_diff", mode="head")["patch"]
    staged = _call(env, "git_diff", mode="staged")["patch"]
    worktree = _call(env, "git_diff", mode="worktree")["patch"]
    assert "+worktree-change" in head and "+staged-change" not in head
    assert "+staged-change" in staged and "+worktree-change" not in staged
    assert "+worktree-change" in worktree and "-staged-change" in worktree

    first = _call(env, "git_diff", mode="head", max_bytes=256)
    assert first["next_offset"] is not None
    assert len(first["patch"].encode("utf-8")) <= 256
    second = _call(env, "git_diff", mode="head", max_bytes=256, offset=first["next_offset"])
    assert second["offset"] == first["next_offset"]
    assert second["total_bytes"] == first["total_bytes"]

    status = _call(env, "git_status", limit=1)
    (env["root"] / "tracked.txt").write_text("same status, new content\n")
    with pytest.raises(BridgeError) as exc:
        _call(env, "git_status", offset=1, expected_status_sha256=status["status_sha256"])
    assert exc.value.code == "stale_evidence"
    with pytest.raises(BridgeError) as exc:
        _call(env, "git_diff", mode="head", expected_status_sha256=status["status_sha256"])
    assert exc.value.code == "stale_evidence"


def test_status_paginates_and_uses_stable_hash_when_unchanged(env):
    _repo(env["root"])
    (env["root"] / "a.txt").write_text("a\n")
    (env["root"] / "b.txt").write_text("b\n")
    status = _call(env, "git_status", limit=1)
    assert len(status["entries"]) == 1
    assert status["next_offset"] == 1
    next_page = _call(env, "git_status", offset=1, limit=1,
                      expected_status_sha256=status["status_sha256"])
    assert len(next_page["entries"]) == 1
    assert next_page["status_sha256"] == status["status_sha256"]


def test_staged_diff_supports_an_unborn_repository(env):
    _git(env["root"], "init", "-q", "--initial-branch=main")
    _git(env["root"], "config", "user.name", "Workspace Bridge Test")
    _git(env["root"], "config", "user.email", "bridge-test@example.invalid")
    (env["root"] / "first.txt").write_text("initial staged content\n")
    _git(env["root"], "add", "first.txt")
    status = _call(env, "git_status")
    assert status["available"] is True and status["head"] is None
    assert "+initial staged content" in _call(env, "git_diff", mode="staged")["patch"]


def test_excluded_names_and_redacted_content_do_not_leak(env):
    _repo(env["root"])
    (env["root"] / ".env").write_text("TOKEN=do-not-show-this-value\n")
    (env["root"] / "src").mkdir(exist_ok=True)
    (env["root"] / "src" / "app.py").write_text("api_key = abcdefghijklmnop\n")
    _git(env["root"], "add", ".env", "src/app.py")
    _git(env["root"], "commit", "-qm", "sensitive baseline")
    (env["root"] / ".env").write_text("TOKEN=sk-" + "A" * 24 + "\n")
    (env["root"] / "src" / "app.py").write_text("api_key = ghp_" + "A" * 24 + "\n")
    status = _call(env, "git_status")
    serialized = json.dumps(status)
    assert status["hidden_count"] == 1
    assert ".env" not in serialized and "do-not-show-this-value" not in serialized
    patch = _call(env, "git_diff", mode="head")["patch"]
    assert ".env" not in patch and "sk-" + "A" * 24 not in patch
    assert "ghp_" + "A" * 24 not in patch
    assert "REDACTED_SECRET" in patch
    with pytest.raises(BridgeError) as exc:
        _call(env, "git_diff", mode="head", path=".env")
    assert ".env" not in str(exc.value)
    (env["root"] / ".env").write_text("TOKEN=sk-" + "B" * 24 + "\n")
    with pytest.raises(BridgeError) as exc:
        _call(env, "git_status", expected_status_sha256=status["status_sha256"])
    assert exc.value.code == "stale_evidence"


@pytest.mark.parametrize("layout", ["git_file", "git_symlink", "config_symlink", "config_include"])
def test_unsupported_metadata_layouts_fail_closed(env, layout):
    if layout == "git_file":
        (env["root"] / ".git").write_text("gitdir: /outside/worktree\n")
    elif layout == "git_symlink":
        outside = env["tmp"] / "outside-git"
        outside.mkdir()
        (env["root"] / ".git").symlink_to(outside, target_is_directory=True)
    elif layout == "config_include":
        _repo(env["root"])
        with (env["root"] / ".git" / "config").open("a") as config:
            config.write("\n[include]\n path = /outside/config\n")
    else:
        _repo(env["root"])
        config = env["root"] / ".git" / "config"
        outside = env["tmp"] / "external-config"
        outside.write_text("[core]\nbare = false\n")
        config.unlink()
        config.symlink_to(outside)
    with pytest.raises(BridgeError) as exc:
        _call(env, "git_status")
    assert exc.value.code == "unsupported_repository_layout"
    assert "outside" not in str(exc.value)


def test_real_linked_worktree_git_file_is_rejected(env):
    _repo(env["root"])
    linked = env["parent"] / "linked"
    _git(env["root"], "worktree", "add", "--detach", str(linked))
    result = env["service"].add_workspace("Linked", str(linked), [])
    ws_id = result["workspace"]["id"]
    env["service"].manage_workspace(ws_id, "enable")
    with pytest.raises(BridgeError) as exc:
        env["service"].call(ws_id, env["token"], "git_status", {})
    assert exc.value.code == "unsupported_repository_layout"


@pytest.mark.parametrize("marker", ["gitdir", "commondir", "config.worktree"])
def test_shared_worktree_markers_are_rejected(env, marker):
    _repo(env["root"])
    (env["root"] / ".git" / marker).write_text("marker\n")
    with pytest.raises(BridgeError) as exc:
        _call(env, "git_status")
    assert exc.value.code == "unsupported_repository_layout"


def test_bare_repository_is_rejected(env):
    _git(env["parent"], "init", "-q", "--bare", str(env["root"] / ".git"))
    with pytest.raises(BridgeError) as exc:
        _call(env, "git_status")
    assert exc.value.code == "unsupported_repository_layout"


@pytest.mark.parametrize("alternate", ["alternates", "http-alternates"])
def test_external_object_storage_markers_are_rejected(env, alternate):
    _repo(env["root"])
    marker = env["root"] / ".git" / "objects" / "info" / alternate
    marker.write_text(str(env["tmp"] / "outside-objects") + "\n")
    with pytest.raises(BridgeError) as exc:
        _call(env, "git_status")
    assert exc.value.code == "unsupported_repository_layout"
    assert "outside-objects" not in str(exc.value)


@pytest.mark.parametrize("metadata_path", ["HEAD", "index", "packed-refs"])
def test_symlinked_critical_metadata_files_are_rejected(env, metadata_path):
    _repo(env["root"])
    metadata = env["root"] / ".git" / metadata_path
    if not metadata.exists():
        metadata.write_text("# empty metadata\n")
    target = env["tmp"] / f"external-{metadata_path}"
    target.write_bytes(metadata.read_bytes())
    metadata.unlink()
    metadata.symlink_to(target)
    with pytest.raises(BridgeError) as exc:
        _call(env, "git_status")
    assert exc.value.code == "unsupported_repository_layout"
    assert "external-" not in str(exc.value)


@pytest.mark.parametrize("metadata_path", ["refs", "objects", "objects/info", "objects/pack"])
def test_symlinked_critical_metadata_directories_are_rejected(env, metadata_path):
    import shutil

    _repo(env["root"])
    metadata = env["root"] / ".git" / metadata_path
    if metadata.exists():
        shutil.rmtree(metadata)
    target = env["tmp"] / ("external-dir-" + metadata_path.replace("/", "-"))
    target.mkdir()
    metadata.symlink_to(target, target_is_directory=True)
    with pytest.raises(BridgeError) as exc:
        _call(env, "git_status")
    assert exc.value.code == "unsupported_repository_layout"
    assert "external-dir" not in str(exc.value)


def test_submodule_status_and_diff_do_not_enter_submodule_metadata(env):
    _repo(env["root"])
    _git(env["root"], "add", "-A")
    _git(env["root"], "commit", "-qm", "track fixture files")
    nested_source = env["tmp"] / "nested-source"
    nested_source.mkdir()
    _repo(nested_source)
    commit = _git(nested_source, "rev-parse", "HEAD").stdout.strip()
    _git(env["root"], "update-index", "--add", "--cacheinfo", "160000", commit, "nested")
    _git(env["root"], "commit", "-qm", "add submodule gitlink")

    nested = env["root"] / "nested"
    nested.mkdir()
    (nested / ".git").write_text("gitdir: " + str(env["tmp"] / "outside-submodule-metadata") + "\n")
    status = _call(env, "git_status")
    assert status["available"] is True
    assert status["entries"] == []
    assert _call(env, "git_diff", mode="head")["patch"] == ""


def test_external_diff_and_textconv_are_never_executed(env):
    _repo(env["root"])
    sentinel = env["tmp"] / "diff-was-executed"
    script = env["tmp"] / "malicious-diff"
    script.write_text("#!/bin/sh\ntouch '" + str(sentinel) + "'\n")
    script.chmod(0o700)
    (env["root"] / ".gitattributes").write_text("tracked.txt diff=evil\n")
    _git(env["root"], "add", ".gitattributes")
    _git(env["root"], "commit", "-qm", "add attributes")
    hook = env["root"] / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\ntouch '" + str(sentinel) + "'\n")
    hook.chmod(0o700)
    _git(env["root"], "config", "diff.external", str(script))
    _git(env["root"], "config", "diff.evil.textconv", str(script))
    _git(env["root"], "config", "core.fsmonitor", str(script))
    index_path = env["root"] / ".git" / "index"
    index_before = (index_path.read_bytes(), index_path.stat().st_mtime_ns)
    (env["root"] / "tracked.txt").write_text("changed\n")
    _call(env, "git_status")
    patch = _call(env, "git_diff", mode="head")["patch"]
    assert "changed" in patch
    assert not sentinel.exists()
    assert (index_path.read_bytes(), index_path.stat().st_mtime_ns) == index_before


def test_diff_output_time_bounds_and_git_errors_are_sanitized(env, tmp_path):
    from workspace_bridge.git_evidence import _run

    scripts = tmp_path / "git-shims"
    scripts.mkdir()
    safe = env["service"].safe_root(env["service"].workspace(env["id"]))
    try:
        noisy = scripts / "noisy"
        noisy.write_text("#!" + sys.executable + "\nimport sys\nsys.stdout.write('x' * 10000)\n")
        noisy.chmod(0o700)
        with pytest.raises(BridgeError) as exc:
            _run(str(noisy), safe, ".git", ["status"], timeout=1.0, output_limit=100)
        assert exc.value.code == "output_limit"

        slow = scripts / "slow"
        slow.write_text("#!" + sys.executable + "\nimport time\ntime.sleep(2)\n")
        slow.chmod(0o700)
        with pytest.raises(BridgeError) as exc:
            _run(str(slow), safe, ".git", ["status"], timeout=0.05, output_limit=100)
        assert exc.value.code == "git_timeout"

        failing = scripts / "failing"
        failing.write_text("#!" + sys.executable + "\nimport sys\nsys.stderr.write('secret /outside/path')\nsys.exit(2)\n")
        failing.chmod(0o700)
        with pytest.raises(BridgeError) as exc:
            _run(str(failing), safe, ".git", ["status"], timeout=1.0, output_limit=100)
        assert exc.value.code == "git_evidence_failed"
        assert "secret" not in str(exc.value) and "/outside/path" not in str(exc.value)
    finally:
        safe.close()


async def test_mcp_discovers_strict_read_only_workspace_scoped_git_tools(env):
    _repo(env["root"])
    app = make_mcp(env["service"])
    headers = {"X-Bridge-Token": env["token"], "Accept": "application/json, text/event-stream"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://127.0.0.1:8765", headers=headers) as client:
        listed = await client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        tools = {item["name"]: item for item in listed.json()["result"]["tools"]}
        for name in ("git_status", "git_diff"):
            item = tools[name]
            assert "workspace_id" in item["inputSchema"]["required"]
            assert item["inputSchema"]["additionalProperties"] is False
            assert item["annotations"]["readOnlyHint"] is True
            assert item["annotations"]["destructiveHint"] is False
        call = await client.post("/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "git_status", "arguments": {"workspace_id": env["id"]}}})
        assert call.json()["result"]["isError"] is False
        invalid = await client.post("/mcp", json={"jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "git_diff", "arguments": {"workspace_id": env["id"],
                                                                  "mode": "head", "ref": "HEAD~1"}}})
        assert invalid.json()["error"]["code"] == -32602
        assert len(TOOLS) == 25
