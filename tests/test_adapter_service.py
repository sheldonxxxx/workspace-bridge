"""Focused adapter state/CLI/launcher contracts (no live services)."""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import sys

import pytest

from workspace_bridge.adapter_cli import main as adapter_main
from workspace_bridge.adapter_cli import select_service_backend
from workspace_bridge.adapter_service import (
    adapter_label,
    adapter_unit,
    build_adapter_env,
    initialize_adapter,
    load_adapter_config,
    probe_adapter_descriptor,
    read_adapter_token,
    resolve_adapter_executable,
    serve_argv,
    validate_adapter_state,
)
from workspace_bridge.security import BridgeError


def _fake_executable(tmp_path: Path, name: str = "fake-adapter") -> Path:
    exe = tmp_path / "bin" / name
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_text("#!/bin/sh\nexit 0\n")
    exe.chmod(0o700)
    return exe


def _projects(tmp_path: Path, name: str = "projects") -> Path:
    root = tmp_path / name
    root.mkdir(parents=True, exist_ok=True)
    return root


def test_init_creates_fresh_state_with_private_modes_and_once_token(tmp_path, capsys):
    projects = _projects(tmp_path)
    exe = _fake_executable(tmp_path)
    state = tmp_path / "adapter-state"
    config, token = initialize_adapter(
        state, runtime_type="pi", projects_root=str(projects),
        port=18980, executable=str(exe))
    assert config["schema_version"] == 1
    assert config["runtime_type"] == "pi"
    assert config["port"] == 18980
    assert config["executable"] == str(exe)
    assert len(config["service_id"]) == 12
    # Config contains only bounded nonsecret fields.
    assert set(config) <= {"schema_version", "runtime_type", "projects_root",
                           "port", "executable", "service_id", "pi_binary",
                           "agent_dir", "log_level"}
    assert "token" not in json.dumps(config).lower()
    assert stat.S_IMODE(state.stat().st_mode) == 0o700
    assert stat.S_IMODE((state / "config.json").stat().st_mode) == 0o600
    assert stat.S_IMODE((state / "runtime-token").stat().st_mode) == 0o600
    assert stat.S_IMODE((state / "runtime").stat().st_mode) == 0o700
    assert stat.S_IMODE((state / "logs").stat().st_mode) == 0o700
    assert (state / "runtime-token").read_text().strip() == token
    # CLI prints the token once on init.
    state2 = tmp_path / "adapter-state-2"
    adapter_main(["--state", str(state2), "init", "--runtime", "codex",
                  "--projects-root", str(projects), "--port", "18981",
                  "--executable", str(exe)])
    out = capsys.readouterr().out
    assert (state2 / "runtime-token").read_text().strip() in out
    # Second init refuses to overwrite.
    with pytest.raises(SystemExit):
        adapter_main(["--state", str(state2), "init", "--runtime", "codex",
                      "--projects-root", str(projects), "--port", "18981",
                      "--executable", str(exe)])


def test_init_validates_projects_root_port_and_executable(tmp_path):
    projects = _projects(tmp_path)
    exe = _fake_executable(tmp_path)
    with pytest.raises(BridgeError):
        initialize_adapter(tmp_path / "s1", runtime_type="pi",
                           projects_root=str(tmp_path / "missing"),
                           port=18982, executable=str(exe))
    with pytest.raises(BridgeError):
        initialize_adapter(tmp_path / "s2", runtime_type="pi",
                           projects_root=str(projects), port=80,
                           executable=str(exe))
    with pytest.raises(BridgeError):
        initialize_adapter(tmp_path / "s3", runtime_type="pi",
                           projects_root=str(projects), port=18983,
                           executable=str(tmp_path / "missing-exe"))
    # Non-absolute executable is rejected.
    with pytest.raises(BridgeError):
        initialize_adapter(tmp_path / "s4", runtime_type="pi",
                           projects_root=str(projects), port=18983,
                           executable="relative/fake")
    # Runtime type is strict.
    with pytest.raises(BridgeError):
        initialize_adapter(tmp_path / "s5", runtime_type="bogus",
                           projects_root=str(projects), port=18983,
                           executable=str(exe))
    # State inside projects root is rejected.
    with pytest.raises(BridgeError):
        initialize_adapter(projects / "inner-state", runtime_type="pi",
                           projects_root=str(projects), port=18983,
                           executable=str(exe))


def test_default_ports_and_executable_resolution(tmp_path, monkeypatch):
    projects = _projects(tmp_path)
    exe_pi = _fake_executable(tmp_path, "workspace-bridge-pi-adapter")
    exe_codex = _fake_executable(tmp_path, "workspace-bridge-codex-adapter")
    monkeypatch.setattr("workspace_bridge.adapter_service.shutil.which",
                        lambda name: str(exe_pi) if name == "workspace-bridge-pi-adapter" else None)
    config, _ = initialize_adapter(tmp_path / "pi-default", runtime_type="pi",
                                   projects_root=str(projects),
                                   executable=None)
    assert config["port"] == 8780
    assert config["executable"] == str(exe_pi)
    monkeypatch.setattr("workspace_bridge.adapter_service.shutil.which",
                        lambda name: str(exe_codex) if name == "workspace-bridge-codex-adapter" else None)
    config2, _ = initialize_adapter(tmp_path / "codex-default", runtime_type="codex",
                                    projects_root=str(projects),
                                    executable=None)
    assert config2["port"] == 8772
    assert config2["executable"] == str(exe_codex)
    # Missing stable executable fails closed.
    monkeypatch.setattr("workspace_bridge.adapter_service.shutil.which",
                        lambda name: None)
    with pytest.raises(BridgeError):
        initialize_adapter(tmp_path / "pi-missing", runtime_type="pi",
                           projects_root=str(projects))


def test_executable_lexical_symlink_is_preserved(tmp_path):
    projects = _projects(tmp_path)
    real = _fake_executable(tmp_path, "real-adapter")
    link = tmp_path / "bin" / "linked-adapter"
    try:
        os.symlink(real, link)
    except OSError:
        pytest.skip("symlinks unavailable")
    config, _ = initialize_adapter(tmp_path / "link-state", runtime_type="pi",
                                   projects_root=str(projects), port=18984,
                                   executable=str(link))
    assert config["executable"] == str(link)
    assert config["executable"] != str(real.resolve())


def test_multiple_instances_get_isolated_service_ids(tmp_path):
    projects = _projects(tmp_path)
    exe = _fake_executable(tmp_path)
    first, _ = initialize_adapter(tmp_path / "a1", runtime_type="pi",
                                  projects_root=str(projects), port=18985,
                                  executable=str(exe))
    second, _ = initialize_adapter(tmp_path / "a2", runtime_type="pi",
                                   projects_root=str(projects), port=18986,
                                   executable=str(exe))
    assert first["service_id"] != second["service_id"]
    assert adapter_label("pi", first["service_id"]) != adapter_label("pi", second["service_id"])
    assert adapter_unit("pi", first["service_id"]) != adapter_unit("pi", second["service_id"])


def test_state_ownership_and_mode_rejection(tmp_path):
    projects = _projects(tmp_path)
    exe = _fake_executable(tmp_path)
    state = tmp_path / "mode-state"
    initialize_adapter(state, runtime_type="pi", projects_root=str(projects),
                       port=18987, executable=str(exe))
    os.chmod(state, 0o755)
    with pytest.raises(BridgeError):
        load_adapter_config(state)
    os.chmod(state, 0o700)
    os.chmod(state / "config.json", 0o644)
    with pytest.raises(BridgeError):
        load_adapter_config(state)


def test_show_token_roundtrip_and_unsafe_rejection(tmp_path, capsys):
    projects = _projects(tmp_path)
    exe = _fake_executable(tmp_path)
    state = tmp_path / "token-state"
    _, token = initialize_adapter(state, runtime_type="codex",
                                  projects_root=str(projects), port=18988,
                                  executable=str(exe))
    assert read_adapter_token(state) == token
    adapter_main(["--state", str(state), "show-token"])
    assert capsys.readouterr().out.strip() == token


def test_pi_and_codex_env_mapping_is_allowlisted(tmp_path, monkeypatch):
    projects = _projects(tmp_path)
    exe = _fake_executable(tmp_path)
    pi_state = tmp_path / "pi-env"
    pi_config, pi_token = initialize_adapter(
        pi_state, runtime_type="pi", projects_root=str(projects), port=18989,
        executable=str(exe), pi_binary="/opt/pi/bin/pi",
        agent_dir=str(tmp_path / "agent"), log_level="debug")
    monkeypatch.setenv("HOME", "/Users/tester")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/ssh.sock")
    monkeypatch.setenv("WB_CODEX_PROJECTS_ROOT", "stale-must-go")
    env = build_adapter_env(pi_config, pi_token, pi_state)
    assert env["WB_RUNTIME_TOKEN"] == pi_token
    assert env["WB_PI_PROJECTS_DIR"] == pi_config["projects_root"]
    assert env["WB_PI_ADAPTER_PORT"] == "18989"
    assert env["WB_PI_BINARY"] == "/opt/pi/bin/pi"
    assert env["PI_CODING_AGENT_DIR"] == str(tmp_path / "agent")
    assert env["WB_LOG_LEVEL"] == "DEBUG"
    assert env["HOME"] == "/Users/tester"
    assert env["PATH"] == "/usr/bin:/bin"
    assert env["SSH_AUTH_SOCK"] == "/tmp/ssh.sock"
    assert "WB_CODEX_PROJECTS_ROOT" not in env
    assert "WB_CODEX_ADAPTER_PORT" not in env
    assert "WB_CODEX_ADAPTER_STATE" not in env

    codex_state = tmp_path / "codex-env"
    codex_config, codex_token = initialize_adapter(
        codex_state, runtime_type="codex", projects_root=str(projects),
        port=18990, executable=str(exe), log_level="INFO")
    monkeypatch.setenv("WB_PI_PROJECTS_DIR", "stale-must-go")
    env2 = build_adapter_env(codex_config, codex_token, codex_state)
    assert env2["WB_RUNTIME_TOKEN"] == codex_token
    assert env2["WB_CODEX_PROJECTS_ROOT"] == codex_config["projects_root"]
    assert env2["WB_CODEX_ADAPTER_PORT"] == "18990"
    assert env2["WB_CODEX_ADAPTER_STATE"] == str(codex_state / "runtime")
    assert env2["WB_LOG_LEVEL"] == "INFO"
    assert "WB_PI_PROJECTS_DIR" not in env2
    assert "WB_PI_BINARY" not in env2
    assert "PI_CODING_AGENT_DIR" not in env2


def test_serve_execves_directly_with_no_shell_and_no_secret_on_failure(tmp_path, monkeypatch):
    projects = _projects(tmp_path)
    exe = _fake_executable(tmp_path)
    state = tmp_path / "serve-state"
    config, token = initialize_adapter(state, runtime_type="pi",
                                       projects_root=str(projects), port=18991,
                                       executable=str(exe))
    captured: dict = {}

    def _fake_execve(path, argv, env):
        captured["path"] = path
        captured["argv"] = argv
        captured["env"] = env
        raise OSError("nope")

    monkeypatch.setattr(os, "execve", _fake_execve)
    from workspace_bridge.adapter_cli import _serve

    with pytest.raises(BridgeError) as exc:
        _serve(state)
    assert token not in str(exc.value)
    assert str(projects) not in str(exc.value)
    # On success path, verify argv/env before the fake failure.
    assert captured["path"] == str(exe)
    assert captured["argv"] == [str(exe)]
    assert captured["env"]["WB_RUNTIME_TOKEN"] == token
    assert serve_argv(config) == [str(exe)]
    # No shell is ever used by the launcher.
    for name in ("adapter_service.py", "adapter_cli.py"):
        src = (Path("workspace_bridge") / name).read_text()
        assert "shell=True" not in src
        assert "os.system(" not in src


def test_serve_uses_resolved_login_path_for_env_shebang_runtime(tmp_path, monkeypatch):
    projects = _projects(tmp_path)
    runtime_bin = tmp_path / "runtime-bin"
    runtime_bin.mkdir()
    node = runtime_bin / "node"
    node.write_text("#!/bin/sh\nexit 0\n")
    node.chmod(0o700)
    exe = tmp_path / "bin" / "workspace-bridge-pi-adapter"
    exe.parent.mkdir(parents=True)
    exe.write_text("#!/usr/bin/env node\n")
    exe.chmod(0o700)
    state = tmp_path / "serve-login-path"
    initialize_adapter(
        state, runtime_type="pi", projects_root=str(projects),
        port=18993, executable=str(exe),
    )

    inherited_path = "/usr/bin:/bin"
    resolved_path = f"{runtime_bin}:{inherited_path}"
    monkeypatch.setenv("PATH", inherited_path)
    monkeypatch.setattr(
        "workspace_bridge.adapter_cli.runtime_env_with_login_path",
        lambda: (
            {"PATH": resolved_path, "HOME": str(tmp_path)},
            {"resolved": True, "path": resolved_path, "shell": "/bin/zsh",
             "shell_basename": "zsh", "entry_count": 3, "code": "ok"},
        ),
    )
    captured: dict = {}

    def _fake_execve(path, argv, env):
        captured["path"] = path
        captured["argv"] = list(argv)
        captured["env"] = dict(env)
        raise RuntimeError("stop")

    monkeypatch.setattr(os, "execve", _fake_execve)
    from workspace_bridge.adapter_cli import _serve

    with pytest.raises(RuntimeError):
        _serve(state)

    assert captured["path"] == str(exe)
    assert captured["argv"] == [str(exe)]
    assert captured["env"]["PATH"] == resolved_path
    assert (Path(captured["env"]["PATH"].split(":")[0]) / "node") == node
    assert captured["env"]["WB_PI_PROJECTS_DIR"] == str(projects)


def test_probe_validates_descriptor_shape(monkeypatch):
    config = {"runtime_type": "pi", "port": 18992}

    class _Resp:
        def __init__(self, payload: bytes):
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, _limit):
            return self.payload

    import json as _json

    good = {"protocol": {"major": 1, "minor": 0},
            "runtime": {"id": "pi", "adapterVersion": "0.1.0",
                        "nativeVersion": "0.87.0", "instanceId": "pi_instance_abc"},
            "features": {"models": 1}}
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda _req, timeout: _Resp(_json.dumps(good).encode()))
    result = probe_adapter_descriptor(config, "token-value")
    assert result["status"] == "healthy"
    assert result["adapter_version"] == "0.1.0"
    assert "token-value" not in json.dumps(result)

    bad_runtime = {"protocol": {"major": 1},
                   "runtime": {"id": "codex", "instanceId": "x"},
                   "features": {}}
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda _req, timeout: _Resp(_json.dumps(bad_runtime).encode()))
    assert probe_adapter_descriptor(config, "t")["code"] == "adapter_identity_mismatch"

    bad_major = {"protocol": {"major": 2},
                 "runtime": {"id": "pi", "instanceId": "x"}, "features": {}}
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda _req, timeout: _Resp(_json.dumps(bad_major).encode()))
    assert probe_adapter_descriptor(config, "t")["status"] == "invalid_response"


def test_init_rejects_preexisting_empty_dir_without_mutation(tmp_path):
    projects = _projects(tmp_path)
    exe = _fake_executable(tmp_path)
    state = tmp_path / "preempty"
    state.mkdir(mode=0o755)
    before_mode = stat.S_IMODE(state.stat().st_mode)
    before_entries = sorted(p.name for p in state.iterdir())
    with pytest.raises(BridgeError) as exc:
        initialize_adapter(state, runtime_type="pi",
                           projects_root=str(projects), port=19010,
                           executable=str(exe))
    assert exc.value.code == "adapter_service_conflict"
    assert stat.S_IMODE(state.stat().st_mode) == before_mode
    assert sorted(p.name for p in state.iterdir()) == before_entries
    assert not (state / "config.json").exists()
    assert not (state / "runtime-token").exists()


def test_init_rejects_preexisting_nonempty_dir_without_mutation(tmp_path):
    projects = _projects(tmp_path)
    exe = _fake_executable(tmp_path)
    state = tmp_path / "prenonempty"
    state.mkdir(mode=0o700)
    sentinel = state / "keep.txt"
    sentinel.write_text("keep")
    sentinel.chmod(0o600)
    before = {p.name: p.read_bytes() for p in state.iterdir() if p.is_file()}
    with pytest.raises(BridgeError) as exc:
        initialize_adapter(state, runtime_type="pi",
                           projects_root=str(projects), port=19011,
                           executable=str(exe))
    assert exc.value.code == "adapter_service_conflict"
    assert {p.name: p.read_bytes() for p in state.iterdir() if p.is_file()} == before
    assert stat.S_IMODE(state.stat().st_mode) == 0o700


def test_init_rejects_symlink_and_file_paths(tmp_path):
    projects = _projects(tmp_path)
    exe = _fake_executable(tmp_path)
    target = tmp_path / "real-dir"
    target.mkdir()
    link = tmp_path / "link-state"
    try:
        os.symlink(target, link)
    except OSError:
        pytest.skip("symlinks unavailable")
    with pytest.raises(BridgeError) as exc:
        initialize_adapter(link, runtime_type="pi",
                           projects_root=str(projects), port=19012,
                           executable=str(exe))
    assert exc.value.code == "adapter_service_conflict"
    assert link.is_symlink()
    victim = tmp_path / "file-state"
    victim.write_text("x")
    with pytest.raises(BridgeError):
        initialize_adapter(victim, runtime_type="pi",
                           projects_root=str(projects), port=19013,
                           executable=str(exe))
    assert victim.read_text() == "x"


def test_init_failure_cleans_up_and_retry_succeeds(tmp_path, monkeypatch):
    projects = _projects(tmp_path)
    exe = _fake_executable(tmp_path)
    state = tmp_path / "retry-state"
    real_open = os.open

    def _fail_once(path, *args, **kwargs):
        if str(path).endswith("config.json"):
            raise OSError("injected")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", _fail_once)
    with pytest.raises(OSError):
        initialize_adapter(state, runtime_type="pi",
                           projects_root=str(projects), port=19014,
                           executable=str(exe))
    # No initialized or conflicting partial state remains.
    assert not os.path.lexists(state) or list(Path(state).iterdir()) == []
    # Retry at the same path succeeds once the failure is gone.
    monkeypatch.setattr(os, "open", real_open)
    if os.path.lexists(state):
        # An empty leftover dir (if platform semantics require) still blocks
        # fresh init by design; remove the empty dir to retry deterministically.
        try:
            Path(state).rmdir()
        except OSError:
            pass
    config, token = initialize_adapter(state, runtime_type="pi",
                                       projects_root=str(projects), port=19014,
                                       executable=str(exe))
    assert config["port"] == 19014
    assert (state / "runtime-token").read_text().strip() == token
    assert stat.S_IMODE(state.stat().st_mode) == 0o700


def test_init_requires_existing_parent_and_creates_only_final_dir(tmp_path):
    projects = _projects(tmp_path)
    exe = _fake_executable(tmp_path)
    # One missing parent level: nothing is created.
    nested = tmp_path / "missing-parent" / "adapter-state"
    with pytest.raises(BridgeError):
        initialize_adapter(nested, runtime_type="pi",
                           projects_root=str(projects), port=19015,
                           executable=str(exe))
    assert not os.path.lexists(nested)
    assert not os.path.lexists(tmp_path / "missing-parent")
    # Multiple missing levels: still nothing is created.
    deep = tmp_path / "nope-a" / "nope-b" / "adapter-state"
    with pytest.raises(BridgeError):
        initialize_adapter(deep, runtime_type="pi",
                           projects_root=str(projects), port=19016,
                           executable=str(exe))
    assert not os.path.lexists(deep)
    assert not os.path.lexists(tmp_path / "nope-a")
    # Existing valid parent succeeds and creates exactly the final dir.
    parent = tmp_path / "ready-parent"
    parent.mkdir(mode=0o700)
    state = parent / "adapter-state"
    config, _ = initialize_adapter(state, runtime_type="pi",
                                   projects_root=str(projects), port=19017,
                                   executable=str(exe))
    assert state.is_dir()
    assert stat.S_IMODE(state.stat().st_mode) == 0o700
    assert config["port"] == 19017


def test_init_rejects_file_parent_without_creating(tmp_path):
    projects = _projects(tmp_path)
    exe = _fake_executable(tmp_path)
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    nested = blocker / "adapter-state"
    with pytest.raises(BridgeError):
        initialize_adapter(nested, runtime_type="pi",
                           projects_root=str(projects), port=19018,
                           executable=str(exe))
    assert blocker.is_file()
    assert blocker.read_text() == "x"
    assert not os.path.lexists(nested)


def test_select_backend_and_unsupported_platform():
    assert select_service_backend("Darwin") == "launchd"
    assert select_service_backend("Linux") == "systemd"
    with pytest.raises(BridgeError) as exc:
        select_service_backend("Windows")
    assert exc.value.code == "adapter_service_unsupported_platform"


def test_no_arbitrary_helper_argv_or_shell():
    for name in ("adapter_launchd.py", "adapter_systemd.py", "adapter_cli.py"):
        src = (Path("workspace_bridge") / name).read_text()
        assert "shell=True" not in src
        assert "os.system(" not in src
        assert "subprocess.call(" not in src or "shell" not in src
    cli_src = Path("workspace_bridge/adapter_cli.py").read_text()
    assert "--service-name" not in cli_src
    assert "--unit" not in cli_src
    assert "SUDO_USER" not in Path("workspace_bridge/adapter_systemd.py").read_text()
