"""M4.3B native log policy, guard rotation, plist marker, spawn and journal units."""
from __future__ import annotations

import json
import os
from pathlib import Path
import plistlib
import stat
import subprocess
import sys

import pytest

from workspace_bridge import native_logs
from workspace_bridge.security import BridgeError


def _make_state(tmp_path: Path, *, with_logs: bool = True) -> Path:
    state = tmp_path / "svc-state"
    state.mkdir(mode=0o700)
    os.chmod(state, 0o700)
    if with_logs:
        log_dir = state / "logs"
        log_dir.mkdir(mode=0o700)
        os.chmod(log_dir, 0o700)
        for name in ("stdout.log", "stderr.log"):
            p = log_dir / name
            fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            os.close(fd)
            os.chmod(p, 0o600)
    return state


def test_policy_constants_are_bounded():
    assert native_logs.ACTIVE_LIMIT == 10 * 1024 * 1024
    assert native_logs.RETAIN_COUNT == 2
    assert 5.0 <= native_logs.POLL_INTERVAL_SECONDS <= 10.0
    assert native_logs.STATE_MODE == 0o700
    assert native_logs.FILE_MODE == 0o600
    assert native_logs.GUARD_ENV == "WB_NATIVE_LOG_GUARD"
    assert native_logs.GUARD_VALUE == "1"
    assert native_logs.LOG_DIRNAME == "logs"


def test_rotate_noop_under_threshold(tmp_path):
    state = _make_state(tmp_path)
    log_dir, stdout_log, _ = native_logs.log_paths(state)
    stdout_log.write_bytes(b"hello\n")
    os.chmod(stdout_log, 0o600)
    before_inode = stdout_log.stat().st_ino
    assert native_logs.rotate_stream(stdout_log, log_dir) == "no-op"
    assert stdout_log.stat().st_ino == before_inode
    assert stdout_log.read_bytes() == b"hello\n"
    assert not (log_dir / "stdout.log.1").exists()
    assert not (log_dir / "stdout.log.2").exists()


def test_rotate_threshold_and_archive_shift_cap(tmp_path):
    state = _make_state(tmp_path)
    log_dir, stdout_log, _ = native_logs.log_paths(state)
    # Active just over 10 MiB: 10 MiB + 100 bytes, with distinct head/tail.
    head = b"A" * 100
    tail = b"B" * native_logs.ACTIVE_LIMIT
    stdout_log.write_bytes(head + tail)
    os.chmod(stdout_log, 0o600)
    before_inode = stdout_log.stat().st_ino
    # Prior .1 exists with known content.
    first = log_dir / "stdout.log.1"
    first.write_bytes(b"OLD-ONE")
    os.chmod(first, 0o600)
    result = native_logs.rotate_stream(stdout_log, log_dir)
    assert result == "rotated"
    # Same inode truncated.
    assert stdout_log.stat().st_ino == before_inode
    assert stdout_log.stat().st_size == 0
    assert stat.S_IMODE(stdout_log.stat().st_mode) == 0o600
    # .1 holds at most last 10 MiB (the tail), .2 holds prior .1.
    data1 = first.read_bytes()
    assert len(data1) == native_logs.ACTIVE_LIMIT
    assert data1 == tail
    second = log_dir / "stdout.log.2"
    assert second.read_bytes() == b"OLD-ONE"
    assert stat.S_IMODE(first.stat().st_mode) == 0o600
    assert stat.S_IMODE(second.stat().st_mode) == 0o600
    # Second rotation with small file is a no-op and preserves archives.
    assert native_logs.rotate_stream(stdout_log, log_dir) == "no-op"


def test_rotate_refuses_symlink_wrong_mode_and_only_log_files(tmp_path):
    state = _make_state(tmp_path)
    log_dir, stdout_log, _ = native_logs.log_paths(state)
    # Symlink active is refused and archives untouched.
    real = log_dir / "real.log"
    real.write_bytes(b"x" * (native_logs.ACTIVE_LIMIT + 10))
    os.chmod(real, 0o600)
    link = log_dir / "stdout.log"
    link.unlink()
    link.symlink_to(real)
    assert native_logs.rotate_stream(link, log_dir).startswith("refused")
    assert not (log_dir / "stdout.log.1").exists()
    link.unlink()
    fd = os.open(link, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    os.close(fd)
    # Wrong mode is refused.
    os.chmod(link, 0o644)
    link.write_bytes(b"y" * 100)
    os.chmod(link, 0o644)
    assert native_logs.rotate_stream(link, log_dir).startswith("refused")
    os.chmod(link, 0o600)
    # Arbitrary paths are never touched.
    config = state / "node-config.json"
    config.write_bytes(b"z" * (native_logs.ACTIVE_LIMIT + 10))
    os.chmod(config, 0o600)
    before = config.read_bytes()
    assert native_logs.rotate_stream(config, log_dir).startswith("refused")
    assert config.read_bytes() == before
    # Wrong-owner archive blocks rotation (simulated by bad mode).
    stdout_log = log_dir / "stdout.log"
    stdout_log.write_bytes(b"Q" * (native_logs.ACTIVE_LIMIT + 5))
    os.chmod(stdout_log, 0o600)
    bad_archive = log_dir / "stdout.log.1"
    bad_archive.write_bytes(b"bad")
    os.chmod(bad_archive, 0o644)
    assert native_logs.rotate_stream(stdout_log, log_dir).startswith("refused")
    # Active was not truncated on refusal.
    assert stdout_log.stat().st_size == native_logs.ACTIVE_LIMIT + 5


def test_guard_once_validates_state_explicitly(tmp_path):
    state = _make_state(tmp_path)
    result = native_logs.guard_once(state)
    assert set(result) == {"stdout", "stderr"}
    # Unsafe state mode fails closed and explicitly.
    os.chmod(state, 0o755)
    with pytest.raises(BridgeError):
        native_logs.guard_once(state)
    os.chmod(state, 0o700)
    # Symlinked log dir fails explicitly.
    log_dir = state / "logs"
    # Replace with symlink to another dir.
    import shutil
    other = tmp_path / "other-logs"
    other.mkdir(mode=0o700)
    os.chmod(other, 0o700)
    shutil.rmtree(log_dir)
    log_dir.symlink_to(other)
    with pytest.raises(BridgeError):
        native_logs.guard_once(state)


def test_parent_exit_helper(monkeypatch):
    monkeypatch.setattr(native_logs.os, "getppid", lambda: 1234)
    assert native_logs._parent_exited(1234) is False
    assert native_logs._parent_exited(9999) is True


def test_guard_loop_exits_when_parent_reparented(tmp_path, monkeypatch):
    state = _make_state(tmp_path)
    # Simulate immediate reparenting.
    result = native_logs.guard_loop(state, interval=0.01, max_iterations=10,
                                    initial_ppid=999999)
    assert result == "parent-exited"


def test_node_plist_carries_fixed_nonsecret_marker(tmp_path):
    from workspace_bridge.node_launchd import build_launch_agent_plist
    state = tmp_path / "node-state"
    exe = tmp_path / "wb-node"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o700)
    home = tmp_path / "home"
    home.mkdir()
    raw = build_launch_agent_plist(state, exe, home=home)
    payload = plistlib.loads(raw)
    assert payload["EnvironmentVariables"] == {"WB_NATIVE_LOG_GUARD": "1"}
    blob = raw.decode() if isinstance(raw, bytes) else raw
    assert "WB_NATIVE_LOG_GUARD" in blob
    # No token or projects root is persisted.
    assert b"runtime-token" not in raw
    assert b"node-token" not in raw


def test_adapter_plist_carries_fixed_nonsecret_marker(tmp_path, monkeypatch):
    from workspace_bridge.adapter_service import initialize_adapter, load_adapter_config
    from workspace_bridge.adapter_launchd import build_launch_agent_plist
    projects = tmp_path / "projects"
    projects.mkdir(parents=True)
    exe = tmp_path / "bin" / "workspace-bridge-pi-adapter"
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o700)
    state = tmp_path / "adapter-state"
    initialize_adapter(state, runtime_type="pi", projects_root=str(projects),
                       port=18999, executable=str(exe))
    config = load_adapter_config(state, require_roots=False)
    label = f"com.workspace-bridge.adapter.pi.{config['service_id']}"
    launcher = tmp_path / "workspace-bridge"
    launcher.write_text("#!/bin/sh\n")
    launcher.chmod(0o700)
    raw = build_launch_agent_plist(state, [str(launcher)], label, home=tmp_path / "home")
    payload = plistlib.loads(raw)
    assert payload["EnvironmentVariables"] == {"WB_NATIVE_LOG_GUARD": "1"}
    assert payload["ProgramArguments"] == [str(launcher), "adapter", "--state", str(state), "serve"]
    assert config["projects_root"].encode() not in raw
    token = (state / "runtime-token").read_text().strip()
    assert token.encode() not in raw


def test_should_spawn_guard_requires_exact_marker_on_darwin():
    assert native_logs.should_spawn_guard(platform_name="Darwin",
                                          environ={"WB_NATIVE_LOG_GUARD": "1"}) is True
    assert native_logs.should_spawn_guard(platform_name="Darwin", environ={}) is False
    assert native_logs.should_spawn_guard(platform_name="Darwin",
                                          environ={"WB_NATIVE_LOG_GUARD": "true"}) is False
    assert native_logs.should_spawn_guard(platform_name="Darwin",
                                          environ={"WB_NATIVE_LOG_GUARD": "0"}) is False
    assert native_logs.should_spawn_guard(platform_name="Darwin",
                                          environ={"WB_NATIVE_LOG_GUARD": ""}) is False
    assert native_logs.should_spawn_guard(platform_name="Linux",
                                          environ={"WB_NATIVE_LOG_GUARD": "1"}) is False
    assert native_logs.should_spawn_guard(platform_name="Windows",
                                          environ={"WB_NATIVE_LOG_GUARD": "1"}) is False


def test_spawn_uses_fixed_argv_no_shell_and_devnull_stdio(tmp_path):
    state = _make_state(tmp_path)
    calls: list[dict] = []

    class FakePopen:
        def __init__(self, argv, **kwargs):
            calls.append({"argv": list(argv), "kwargs": dict(kwargs)})

    ok = native_logs.spawn_log_guard(state, platform_name="Darwin",
                                     environ={"WB_NATIVE_LOG_GUARD": "1"},
                                     popen=FakePopen)
    assert ok is True
    assert len(calls) == 1
    argv = calls[0]["argv"]
    kwargs = calls[0]["kwargs"]
    # Fixed module argv: <python> -m workspace_bridge.native_logs guard --state <state>
    assert argv[1:4] == ["-m", "workspace_bridge.native_logs", "guard"]
    assert argv[4] == "--state"
    assert argv[5] == str(native_logs._absolute_state(state))
    assert kwargs.get("shell") is False
    assert kwargs.get("stdin") is subprocess.DEVNULL
    assert kwargs.get("stdout") is subprocess.DEVNULL
    assert kwargs.get("stderr") is subprocess.DEVNULL
    assert kwargs.get("close_fds") is True
    # No-spawn without the marker.
    calls.clear()
    assert native_logs.spawn_log_guard(state, platform_name="Darwin",
                                       environ={}, popen=FakePopen) is False
    assert calls == []
    # No-spawn off Darwin even with the marker.
    assert native_logs.spawn_log_guard(state, platform_name="Linux",
                                       environ={"WB_NATIVE_LOG_GUARD": "1"},
                                       popen=FakePopen) is False
    assert calls == []


def test_adapter_serve_preserves_exec_argv_env(tmp_path, monkeypatch):
    from workspace_bridge.adapter_service import initialize_adapter, serve_argv, build_adapter_env, validate_adapter_state
    from workspace_bridge import adapter_cli
    projects = tmp_path / "projects"
    projects.mkdir(parents=True)
    exe = tmp_path / "bin" / "workspace-bridge-pi-adapter"
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_text("#!/bin/sh\nexit 0\n")
    exe.chmod(0o700)
    state = tmp_path / "adapter-pi"
    initialize_adapter(state, runtime_type="pi", projects_root=str(projects),
                       port=18881, executable=str(exe))
    # Capture execve argv/env while disabling the guard spawn.
    monkeypatch.setattr(adapter_cli, "_serve", adapter_cli._serve)  # keep reference
    import workspace_bridge.native_logs as _nl
    spawned: list[bool] = []
    real_spawn = _nl.spawn_log_guard
    monkeypatch.setattr(_nl, "spawn_log_guard",
                        lambda *a, **k: spawned.append(True) or False)
    # Also patch the already-imported reference inside adapter_cli if any.
    captured: dict = {}
    def fake_execve(path, argv, env):
        captured["path"] = path
        captured["argv"] = list(argv)
        captured["env"] = dict(env)
        raise RuntimeError("stop")
    monkeypatch.setattr(os, "execve", fake_execve)
    # Foreground without marker: no guard, exec still exact.
    monkeypatch.delenv("WB_NATIVE_LOG_GUARD", raising=False)
    with pytest.raises(RuntimeError):
        adapter_cli._serve(native_logs._absolute_state(state))
    config, token = validate_adapter_state(state)
    assert captured["argv"] == serve_argv(config)
    assert captured["env"] == build_adapter_env(config, token, state)
    # With the marker on Darwin, spawn is attempted but exec argv/env unchanged.
    monkeypatch.setenv("WB_NATIVE_LOG_GUARD", "1")
    monkeypatch.setattr("workspace_bridge.native_logs.platform", "platform")
    import platform as _plat
    monkeypatch.setattr(_plat, "system", lambda: "Darwin")
    captured.clear()
    spawned.clear()
    with pytest.raises(RuntimeError):
        adapter_cli._serve(native_logs._absolute_state(state))
    assert captured["argv"] == serve_argv(config)
    assert captured["env"] == build_adapter_env(config, token, state)


def test_node_and_adapter_systemd_units_declare_journal(tmp_path):
    from workspace_bridge.node_systemd import build_systemd_unit as build_node
    from workspace_bridge.adapter_systemd import build_systemd_unit as build_adapter
    exe = tmp_path / "workspace-bridge"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o700)
    node_unit = build_node(tmp_path / "node-state", [str(exe)],
                           user="testuser", group="testgroup").decode()
    assert "StandardOutput=journal" in node_unit
    assert "StandardError=journal" in node_unit
    # Deterministic.
    again = build_node(tmp_path / "node-state", [str(exe)],
                       user="testuser", group="testgroup").decode()
    assert node_unit == again
    adapter_unit = build_adapter(tmp_path / "adapter-state", [str(exe)],
                                 user="testuser", group="testgroup",
                                 runtime_type="pi", service_id="0123456789ab").decode()
    assert "StandardOutput=journal" in adapter_unit
    assert "StandardError=journal" in adapter_unit
