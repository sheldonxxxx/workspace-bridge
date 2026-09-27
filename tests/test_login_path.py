"""Focused login-shell PATH resolution and Codex child-env injection."""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from workspace_bridge import login_path
from workspace_bridge.codex_host_adapter import CodexHostAdapter, _codex_cli_version
from workspace_bridge.codex_rpc import CodexRpc


def _fake_run(stdout, returncode=0):
    def _run(cmd, *, timeout):
        # Interactive-login invocation: `-l -i -c <probe>` for full shells,
        # conservative `-i -c <probe>` for the minimal sh family.
        assert "-i" in cmd and "-c" in cmd
        assert "printf" in cmd[-1]
        assert "__WB_LOGIN_PATH_BEGIN__" in cmd[-1]
        name = (cmd[0].rsplit("/", 1)[-1] or "").lower()
        if name in ("sh", "dash", "ash"):
            assert cmd[1:3] == ["-i", "-c"]
        else:
            assert cmd[1:4] == ["-l", "-i", "-c"]
        m = type("R", (), {})()
        m.stdout = stdout
        m.returncode = returncode
        return m
    return _run


def test_interactive_login_invocation_and_regression():
    seen = {}

    def _run(cmd, *, timeout):
        seen["cmd"] = list(cmd)
        # Regression: this fake shell only contributes the interactive PATH
        # entry when interactive mode (`-i`) is present, mimicking `.zshrc`
        # (interactive-only) versus login-only startup files.
        if "-i" in cmd:
            path = "/usr/bin:/bin:/interactive/tools"
        else:
            path = "/usr/bin:/bin"
        m = type("R", (), {})()
        m.stdout = f"__WB_LOGIN_PATH_BEGIN__{path}__WB_LOGIN_PATH_END__"
        m.returncode = 0
        return m

    result = login_path.resolve_login_path(_run=_run, _shell="/bin/zsh")
    assert result["resolved"] is True
    assert result["path"] == "/usr/bin:/bin:/interactive/tools"
    assert result["entry_count"] == 3
    assert seen["cmd"][1:4] == ["-l", "-i", "-c"]
    # The same fake invoked login-only would miss the interactive entry.
    login_only_path = "/usr/bin:/bin"
    assert login_only_path != result["path"]


def test_unsupported_shell_fails_safely_to_inherited_path():
    result = login_path.resolve_login_path(_shell="/bin/elvish")
    assert result["resolved"] is False
    assert result["code"] == "unsupported"
    base = {"PATH": "/inherited"}
    env, out = login_path.runtime_env_with_login_path(
        base, _resolve=lambda: result)
    assert env["PATH"] == "/inherited"
    assert out["code"] == "unsupported"


def test_fish_probe_uses_documented_join_form():
    assert "string join" in login_path.probe_command_for_shell("/usr/local/bin/fish")
    assert "$PATH" in login_path.probe_command_for_shell("/bin/zsh")
    argv = login_path.probe_argv("/usr/local/bin/fish")
    assert argv is not None and argv[1:4] == ["-l", "-i", "-c"]
    assert login_path.probe_argv("/bin/elvish") is None
    assert login_path.probe_argv("/bin/sh")[1:3] == ["-i", "-c"]


def test_resolve_success_validates_and_exposes_safe_summary_only():
    run = _fake_run("__WB_LOGIN_PATH_BEGIN__/usr/bin:/bin:/opt/tools/bin__WB_LOGIN_PATH_END__")
    result = login_path.resolve_login_path(_run=run, _shell="/bin/zsh")
    assert result["resolved"] is True
    assert result["path"] == "/usr/bin:/bin:/opt/tools/bin"
    assert result["shell"] == "/bin/zsh"
    assert result["shell_basename"] == "zsh"
    assert result["entry_count"] == 3
    assert result["code"] == "ok"
    summary = login_path.safe_summary(result)
    assert summary == {"resolved": True, "shell_basename": "zsh",
                       "entry_count": 3, "code": "ok"}
    assert "/usr/bin" not in json.dumps(summary)
    ok, entries, code = login_path.validate_search_path(result["path"])
    assert ok and code == "ok" and entries == ["/usr/bin", "/bin", "/opt/tools/bin"]


def test_noisy_shell_output_is_isolated_to_marked_value():
    stdout = "Welcome to zsh\nmotd line 2\n__WB_LOGIN_PATH_BEGIN__/usr/bin:/bin__WB_LOGIN_PATH_END__\nbye\n"
    result = login_path.resolve_login_path(_run=_fake_run(stdout), _shell="/bin/zsh")
    assert result["resolved"] is True
    assert result["path"] == "/usr/bin:/bin"
    assert result["entry_count"] == 2


def test_oversize_output_falls_back_without_exposing_content():
    oversize = "x" * (login_path.MAX_LOGIN_SHELL_OUTPUT + 1024)
    result = login_path.resolve_login_path(
        _run=_fake_run(oversize), _shell="/bin/zsh")
    assert result["resolved"] is False
    assert result["code"] == "output_too_large"
    assert result["path"] is None
    summary = login_path.safe_summary(result)
    assert summary["code"] == "output_too_large"
    dumped = json.dumps(summary)
    assert oversize[:32] not in dumped
    assert "x" * 64 not in dumped
    base = {"PATH": "/inherited"}
    env, out = login_path.runtime_env_with_login_path(
        base, _resolve=lambda: result)
    assert env["PATH"] == "/inherited"


def test_bounded_fd3_run_discards_stdout_noise_and_caps_probe():
    # Huge ordinary stdout must be discarded at the boundary (DEVNULL) while
    # the small fd-3 probe still resolves: 200k to fd 1 is dropped, only the
    # marked fd-3 value is captured.
    noisy = ["/bin/sh", "-c",
             "yes noisy-startup-line | head -c 200000; "
             f"printf '{login_path._MARK_BEGIN}%s{login_path._MARK_END}' \"/usr/bin:/bin\" >&3"]
    box = login_path._bounded_fd3_run(noisy, timeout=5)
    assert isinstance(box.stdout, str)
    assert len(box.stdout) <= login_path.MAX_LOGIN_SHELL_OUTPUT
    assert login_path._MARK_BEGIN in box.stdout
    assert "noisy-startup-line" not in box.stdout
    # Flooding fd 3 itself must cap during capture, not buffer unbounded.
    flood = ["/bin/sh", "-c", "yes flood-line | head -c 200000 >&3"]
    box2 = login_path._bounded_fd3_run(flood, timeout=5)
    assert isinstance(box2.stdout, str)
    assert len(box2.stdout) <= login_path.MAX_LOGIN_SHELL_OUTPUT + 1
    assert len(box2.stdout) > login_path.MAX_LOGIN_SHELL_OUTPUT
    result = login_path.resolve_login_path(_shell="/bin/sh")
    # Real shell resolution stays safe (resolved or safe fallback) and never
    # exposes content via the summary.
    assert result["code"] in login_path._SAFE_CODES
    assert result["path"] is None or isinstance(result["path"], str)


def test_bounded_fd3_run_captures_probe_with_high_fd():
    # A busy process may allocate the capture pipe above fd 9. This must
    # still work with /bin/sh, including dash on Linux.
    holders = [open(os.devnull, "rb") for _ in range(16)]
    try:
        assert holders[-1].fileno() > 9
        noisy = ["/bin/sh", "-c",
                 "printf ignored; "
                 f"printf '{login_path._MARK_BEGIN}%s{login_path._MARK_END}' "
                 '"/usr/bin:/bin" >&3']
        box = login_path._bounded_fd3_run(noisy, timeout=5)
        assert box.returncode == 0
        assert box.stdout == f"{login_path._MARK_BEGIN}/usr/bin:/bin{login_path._MARK_END}"
    finally:
        for handle in holders:
            handle.close()


def test_timeout_falls_back_to_inherited_path():
    def _run(cmd, *, timeout):
        raise subprocess.TimeoutExpired(cmd, timeout)
    result = login_path.resolve_login_path(_run=_run, _shell="/bin/zsh")
    assert result["resolved"] is False
    assert result["code"] == "timeout"
    base = {"PATH": "/usr/bin:/bin", "OTHER": "kept"}
    env, out = login_path.runtime_env_with_login_path(
        base, _resolve=lambda: result)
    assert env["PATH"] == "/usr/bin:/bin"
    assert env["OTHER"] == "kept"
    assert out["resolved"] is False


def test_invalid_paths_rejected_with_safe_codes():
    cases = [
        ("", "empty"),
        ("/usr/bin:relative/bin", "relative"),
        ("/usr/bin::/bin", "empty_entry"),
        ("/usr/bin:.:/bin", "relative"),
        ("/usr/bin:/a/../b", "dot_entry"),
        ("/usr/bin:/bin\n/evil", "control"),
        ("/usr/bin:\x00/bin", "control"),
        ("x" * 9000, "too_long"),
    ]
    for candidate, code in cases:
        ok, _, got = login_path.validate_search_path(candidate)
        assert not ok, candidate
        assert got == code, candidate
        # Resolver maps validation failure to fallback without applying.
        marked = f"__WB_LOGIN_PATH_BEGIN__{candidate}__WB_LOGIN_PATH_END__"
        result = login_path.resolve_login_path(_run=_fake_run(marked), _shell="/bin/sh")
        assert result["resolved"] is False
        assert result["path"] is None
        # Safe summary never contains the candidate value.
        assert candidate[:20] not in json.dumps(login_path.safe_summary(result)) or not candidate


def test_only_path_is_imported_other_env_untouched():
    run = _fake_run("__WB_LOGIN_PATH_BEGIN__/usr/bin:/bin__WB_LOGIN_PATH_END__")
    base = {"PATH": "/inherited", "WB_RUNTIME_TOKEN": "tok", "HOME": "/home/u"}
    env, result = login_path.runtime_env_with_login_path(
        base, _resolve=lambda: login_path.resolve_login_path(_run=run, _shell="/bin/zsh"))
    assert result["resolved"] is True
    assert env["PATH"] == "/usr/bin:/bin"
    assert env["WB_RUNTIME_TOKEN"] == "tok"
    assert env["HOME"] == "/home/u"
    assert base["PATH"] == "/inherited"


def test_codex_app_server_child_receives_resolved_env():
    marker = "codex-probe-path-xyz"
    child = (
        "import json, os, sys\n"
        "for raw in sys.stdin:\n"
        "  req=json.loads(raw)\n"
        "  if 'id' not in req: continue\n"
        "  if req['method']=='initialize':\n"
        "    print(json.dumps({'id':req['id'],'result':{'serverInfo':{'version':'9.9.9'},\n"
        "      'childPath':os.environ.get('PATH','')}}),flush=True)\n"
    )
    custom = {"PATH": f"/tmp/{marker}:/usr/bin:/bin", "OTHER": "kept"}
    rpc = CodexRpc(command=(sys.executable, "-u", "-c", child), env=custom)
    try:
        assert rpc.initialize_result["childPath"] == custom["PATH"]
        assert rpc._runtime_env == custom
        # The stored env is a copy; mutating the caller dict is safe.
        custom["PATH"] = "/mutated"
        assert rpc._runtime_env["PATH"].startswith("/tmp/")
    finally:
        rpc.close()


def test_codex_version_probe_shares_app_server_env(tmp_path, monkeypatch):
    from workspace_bridge import codex_host_adapter as mod

    seen = {}

    def _fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["env"] = kwargs.get("env")
        m = type("R", (), {})()
        m.returncode = 0
        m.stdout = "codex-cli 1.2.3\n"
        m.stderr = ""
        return m

    monkeypatch.setattr(mod.subprocess, "run", _fake_run)
    env = {"PATH": "/tmp/shared-probe:/usr/bin:/bin"}
    assert _codex_cli_version("codex", env) == "1.2.3"
    assert seen["env"] == env

    # Adapter construction with an injected fake RPC keeps the explicit env
    # for the version probe without spawning a login shell.
    class _FakeRpc:
        alive = True
        initialize_result = {}
        _process = type("P", (), {"args": ("codex", "app-server", "--stdio")})()
        _runtime_env = None
        on_notification = None
        on_request = None
        on_unexpected_exit = None

        def close(self):
            pass

    parent = tmp_path / "projects"
    parent.mkdir()
    (parent / "app").mkdir()
    state = tmp_path / "codex-state"
    adapter = CodexHostAdapter(state, parent, rpc=_FakeRpc(),
                               _exit_process=lambda code: None,
                               _runtime_env=env)
    try:
        assert adapter._runtime_env == env
        seen.clear()
        assert adapter.descriptor()["runtime"]["nativeVersion"] == "1.2.3"
        assert seen["env"] == env
    finally:
        adapter.close()
