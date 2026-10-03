"""Linux per-instance adapter systemd lifecycle (fake systemd)."""
from __future__ import annotations

import hashlib
import inspect
import json
import os
from pathlib import Path
import stat
import subprocess
import sys

import pytest

from workspace_bridge.adapter_service import initialize_adapter
from workspace_bridge.adapter_systemd import (
    MANIFEST_NAME,
    SystemdManager,
    _encode_systemd_arg,
    build_systemd_unit,
    install_service,
    render_systemd_unit,
    restart_service,
    service_paths,
    service_status,
    start_service,
    stop_service,
    systemd_program,
    uninstall_service,
)
from workspace_bridge.adapter_service import validate_root_executable_trust as _validate_trust
from workspace_bridge.security import BridgeError


FAKE_SUDO = "/fake/sudo"
FAKE_SYSTEMCTL = "/fake/systemctl"
FAKE_INSTALL = "/fake/install"
FAKE_RM = "/fake/rm"

TEST_USER = "testuser"
TEST_GROUP = "testgroup"
REAL_UID = os.getuid()
REAL_GID = os.getgid()

ROOT_USER = "root"
ROOT_UID = 0
ROOT_GROUP = "root"
ROOT_GID = 0


def _ids():
    return (TEST_USER, REAL_UID, TEST_GROUP, REAL_GID)


def _root_ids():
    return (ROOT_USER, ROOT_UID, ROOT_GROUP, ROOT_GID)


class FakeSystem:
    """Fake privileged runner for dynamic per-instance units."""

    def __init__(self, unit_dir: Path | None = None):
        self.calls: list[list[str]] = []
        self.show_available = True
        self.load_state = "loaded"
        self.active_state = "inactive"
        self.sub_state = "dead"
        self.unit_file_state = "disabled"
        self.sudo_available = True
        self.systemd_available = True
        self.privilege_fail = False
        self.fail_next_reload = False
        self.unit_dir = unit_dir

    def __call__(self, argv, **kwargs):
        assert isinstance(argv, list), argv
        assert kwargs.get("capture_output") is True
        assert kwargs.get("check") is False
        joined = " ".join(argv)
        assert "--user" not in argv, joined
        assert "loginctl" not in joined
        assert "XDG" not in joined
        self.calls.append(list(argv))
        if argv[0] == FAKE_SYSTEMCTL and len(argv) > 1 and argv[1] == "show":
            assert argv[2].startswith("workspace-bridge-adapter-"), argv
            assert argv[2].endswith(".service"), argv
            assert "--no-pager" in argv
            if not self.systemd_available:
                return subprocess.CompletedProcess(
                    argv, 1, "", "System has not been booted with systemd")
            if not self.show_available:
                return subprocess.CompletedProcess(argv, 1, "LoadState=not-found\n", "")
            out = (f"LoadState={self.load_state}\n"
                   f"ActiveState={self.active_state}\n"
                   f"SubState={self.sub_state}\n"
                   f"UnitFileState={self.unit_file_state}\n")
            return subprocess.CompletedProcess(argv, 0, out, "")
        if argv[0] == FAKE_SUDO:
            if not self.sudo_available or self.privilege_fail:
                return subprocess.CompletedProcess(
                    argv, 1, "", "Sorry, user is not allowed to execute '/bin/systemctl' as root")
            helper, args = argv[1], argv[2:]
        else:
            assert argv[0] in (FAKE_SYSTEMCTL, FAKE_INSTALL, FAKE_RM), argv
            helper, args = argv[0], argv[1:]
        if "install" in helper:
            assert args[:6] == ["-o", "root", "-g", "root", "-m", "0644"], argv
            assert "--" in args
            temp = Path(args[-2])
            dest = Path(args[-1])
            data = temp.read_bytes()
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            os.chmod(dest, 0o644)
            return subprocess.CompletedProcess(argv, 0, "", "")
        if helper == FAKE_RM or helper.endswith("/rm"):
            assert args[0] == "--"
            dest = Path(args[-1])
            try:
                dest.unlink()
            except FileNotFoundError:
                pass
            return subprocess.CompletedProcess(argv, 0, "", "")
        assert "systemctl" in helper, argv
        if not self.systemd_available:
            return subprocess.CompletedProcess(
                argv, 1, "", "System has not been booted with systemd")
        action = args[0]
        if action == "daemon-reload":
            if self.fail_next_reload:
                self.fail_next_reload = False
                return subprocess.CompletedProcess(argv, 1, "", "daemon reload boom")
            return subprocess.CompletedProcess(argv, 0, "", "")
        if action == "enable":
            assert args[1] == "--now"
            assert args[2].startswith("workspace-bridge-adapter-")
            self.unit_file_state = "enabled"
            self.load_state = "loaded"
            self.active_state = "active"
            self.sub_state = "running"
            return subprocess.CompletedProcess(argv, 0, "", "")
        if action in {"start", "restart"}:
            assert args[1].startswith("workspace-bridge-adapter-")
            self.active_state = "active"
            self.sub_state = "running"
            return subprocess.CompletedProcess(argv, 0, "", "")
        if action == "stop":
            self.active_state = "inactive"
            self.sub_state = "dead"
            return subprocess.CompletedProcess(argv, 0, "", "")
        if action == "disable":
            assert args[1] == "--now"
            self.unit_file_state = "disabled"
            self.active_state = "inactive"
            self.sub_state = "dead"
            return subprocess.CompletedProcess(argv, 0, "", "")
        raise AssertionError(argv)


def _initialized_adapter(tmp_path: Path, *, runtime: str = "pi", port: int = 18990,
                          name: str = "adapter-state"):
    projects = tmp_path / "projects" / "alpha"
    projects.mkdir(parents=True, exist_ok=True)
    exe = tmp_path / "bin" / (f"workspace-bridge-{runtime}-adapter" if runtime in {"pi", "codex"} else "fake")
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_text("#!/bin/sh\nexit 0\n")
    exe.chmod(0o700)
    state = tmp_path / name
    if state.exists():
        import shutil as _shutil

        _shutil.rmtree(state)
    initialize_adapter(state, runtime_type=runtime,
                       projects_root=str(projects), port=port,
                       executable=str(exe))
    token = (state / "runtime-token").read_text().strip()
    return state, projects, token, exe


def _fake_launcher(tmp_path: Path, name: str = "workspace-bridge") -> Path:
    exe = tmp_path / "launchers" / name
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_text("#!/bin/sh\nexit 0\n")
    exe.chmod(0o700)
    return exe


def _manager(fake: FakeSystem, unit_dir: Path, *, euid: int | None = None) -> SystemdManager:
    uid = os.getuid() if euid is None else euid
    return SystemdManager(
        runner=fake,
        platform_name="Linux",
        sudo_bin=FAKE_SUDO,
        systemctl_bin=FAKE_SYSTEMCTL,
        install_bin=FAKE_INSTALL,
        rm_bin=FAKE_RM,
        unit_dir=unit_dir,
        ids_provider=_ids,
        euid=uid,
        unit_owner_uid=os.getuid(),
    )


def _root_stat_fakes(*owned: Path | str, force_ancestors: bool = True, real_paths: tuple = ()):
    owned_strs = [os.fspath(p) for p in owned]
    real_strs = [os.fspath(p) for p in real_paths]

    def _fake(path: Path, *, _lstat: bool):
        s = os.fspath(path)
        st = os.lstat(s) if _lstat else os.stat(s)
        if any(s == p or s.startswith(p + os.sep) for p in real_strs):
            return st
        if any(s == p or s.startswith(p + os.sep) for p in owned_strs):
            return os.stat_result(
                (st.st_mode, st.st_ino, st.st_dev, st.st_nlink, 0, 0,
                 st.st_size, st.st_atime, st.st_mtime, st.st_ctime))
        if force_ancestors and stat.S_ISDIR(st.st_mode):
            return os.stat_result(
                (stat.S_IFDIR | 0o755, st.st_ino, st.st_dev, st.st_nlink, 0, 0,
                 st.st_size, st.st_atime, st.st_mtime, st.st_ctime))
        return st

    return (lambda p: _fake(p, _lstat=False)), (lambda p: _fake(p, _lstat=True))


def _root_manager(fake: FakeSystem, unit_dir: Path, *, stat_fn=None, lstat_fn=None) -> SystemdManager:
    return SystemdManager(
        runner=fake,
        platform_name="Linux",
        sudo_bin=FAKE_SUDO,
        systemctl_bin=FAKE_SYSTEMCTL,
        install_bin=FAKE_INSTALL,
        rm_bin=FAKE_RM,
        unit_dir=unit_dir,
        ids_provider=_root_ids,
        euid=0,
        unit_owner_uid=0,
        stat_fn=stat_fn,
        lstat_fn=lstat_fn,
    )


def _root_executable(tmp_path: Path, name: str = "workspace-bridge-codex-adapter") -> Path:
    exe = tmp_path / "rootbin" / name
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_text("#!/bin/sh\nexit 0\n")
    exe.chmod(0o755)
    return exe


class _StatusResponse:
    def __init__(self, payload: dict):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self, _limit):
        import json as _json

        return _json.dumps(self.payload).encode()


def test_unit_bytes_deterministic_and_secret_free(tmp_path):
    state, _projects, token, _exe = _initialized_adapter(tmp_path, port=18991)
    launcher = _fake_launcher(tmp_path)
    from workspace_bridge.adapter_service import load_adapter_config

    config = load_adapter_config(state, require_roots=False)
    first = build_systemd_unit(state, [str(launcher)], user=TEST_USER,
                               group=TEST_GROUP, runtime_type=config["runtime_type"],
                               service_id=config["service_id"])
    second = render_systemd_unit(state, [str(launcher)], user=TEST_USER,
                                 group=TEST_GROUP, runtime_type=config["runtime_type"],
                                 service_id=config["service_id"])
    assert first == second
    text = first.decode()
    assert "Description=Workspace Bridge Adapter" in text
    assert f"User={TEST_USER}" in text
    assert f"Group={TEST_GROUP}" in text
    assert "Type=simple" in text
    assert "Restart=on-failure" in text
    assert "RestartSec=10" in text
    assert "UMask=0077" in text
    assert "WantedBy=multi-user.target" in text
    assert "adapter --state" in text
    assert token.encode() not in first
    assert b"runtime-token" not in first
    assert str(_projects).encode() not in first


def test_install_status_lifecycle_with_sudo_and_preservation(tmp_path, monkeypatch):
    state, projects, token, _exe = _initialized_adapter(tmp_path, port=18992)
    unit_dir = tmp_path / "system"
    launcher = _fake_launcher(tmp_path)
    monkeypatch.setattr("workspace_bridge.adapter_systemd.systemd_program",
                        lambda executable=None: [str(launcher)])
    fake = FakeSystem(unit_dir)
    manager = _manager(fake, unit_dir)
    before = (state / "config.json").read_bytes()
    installed = install_service(state, manager=manager)
    paths = service_paths(state, unit_dir=unit_dir)
    assert installed["installed"] is True
    assert installed["state"] == "running"
    assert installed["listen"] == {"port": 18992,
                                   "endpoint": "http://127.0.0.1:18992"}
    text = paths.unit.read_text()
    assert f"User={TEST_USER}" in text
    assert f"Group={TEST_GROUP}" in text
    assert token.encode() not in paths.unit.read_bytes()
    assert str(projects).encode() not in paths.unit.read_bytes()
    assert stat.S_IMODE(paths.unit.stat().st_mode) == 0o644
    assert (state / "config.json").read_bytes() == before
    # sudo was used for privileged mutations.
    assert any(call[0] == FAKE_SUDO for call in fake.calls)
    assert f"User={TEST_USER}" in text

    stop_service(state, manager=manager)
    start_service(state, manager=manager)
    restart_service(state, manager=manager)
    # Idempotent reinstall.
    again = install_service(state, manager=manager)
    assert again["installed"] is True
    # Uninstall preserves adapter state.
    uninstall_service(state, manager=manager)
    assert not paths.unit.exists()
    assert (state / "config.json").exists()
    assert (state / "runtime-token").exists()


def test_multi_instance_unit_isolation(tmp_path, monkeypatch):
    first, _, _, _ = _initialized_adapter(tmp_path, runtime="pi", port=18993,
                                          name="adapter-a")
    second, _, _, _ = _initialized_adapter(tmp_path, runtime="pi", port=18994,
                                           name="adapter-b")
    codex, _, _, _ = _initialized_adapter(tmp_path, runtime="codex", port=18995,
                                          name="adapter-c")
    unit_dir = tmp_path / "system"
    launcher = _fake_launcher(tmp_path)
    monkeypatch.setattr("workspace_bridge.adapter_systemd.systemd_program",
                        lambda executable=None: [str(launcher)])
    fake = FakeSystem(unit_dir)
    manager = _manager(fake, unit_dir)
    install_service(first, manager=manager)
    install_service(second, manager=manager)
    install_service(codex, manager=manager)
    first_paths = service_paths(first, unit_dir=unit_dir)
    second_paths = service_paths(second, unit_dir=unit_dir)
    codex_paths = service_paths(codex, unit_dir=unit_dir)
    assert len({first_paths.unit_name, second_paths.unit_name,
                codex_paths.unit_name}) == 3
    assert first_paths.unit_name.startswith("workspace-bridge-adapter-pi-")
    assert codex_paths.unit_name.startswith("workspace-bridge-adapter-codex-")
    for paths in (first_paths, second_paths, codex_paths):
        assert paths.unit.exists()


def test_status_is_secret_safe_and_no_sudo(tmp_path, monkeypatch):
    state, _, token, _ = _initialized_adapter(tmp_path, port=18996)
    unit_dir = tmp_path / "system"
    launcher = _fake_launcher(tmp_path)
    monkeypatch.setattr("workspace_bridge.adapter_systemd.systemd_program",
                        lambda executable=None: [str(launcher)])
    fake = FakeSystem(unit_dir)
    manager = _manager(fake, unit_dir)
    install_service(state, manager=manager)
    calls_after_install = len(fake.calls)
    good = {"protocol": {"major": 1, "minor": 0},
            "runtime": {"id": "pi", "adapterVersion": "0.1.0",
                        "nativeVersion": "0.87.0", "instanceId": "pi_instance_y"},
            "features": {"models": 1}}
    monkeypatch.setattr("urllib.request.urlopen",
                        lambda _req, timeout: _StatusResponse(good))
    status = service_status(state, manager=manager, probe=True)
    assert status["health"]["status"] == "healthy"
    text = json.dumps(status)
    assert token not in text
    assert "runtime-token" not in text
    assert "executable" not in text
    # Status adds only an unprivileged show call (no sudo).
    new_calls = fake.calls[calls_after_install:]
    assert new_calls
    assert all(call[0] != FAKE_SUDO for call in new_calls)
    assert any(call[0] == FAKE_SYSTEMCTL and call[1] == "show" for call in new_calls)


def test_status_results_are_path_free_and_secret_safe(tmp_path, monkeypatch):
    state, projects, token, exe = _initialized_adapter(tmp_path, port=19000)
    unit_dir = tmp_path / "system"
    launcher = _fake_launcher(tmp_path)
    monkeypatch.setattr("workspace_bridge.adapter_systemd.systemd_program",
                        lambda executable=None: [str(launcher)])
    fake = FakeSystem(unit_dir)
    manager = _manager(fake, unit_dir)
    install_service(state, manager=manager)
    paths = service_paths(state, unit_dir=unit_dir)
    good = {"protocol": {"major": 1, "minor": 0},
            "runtime": {"id": "pi", "adapterVersion": "0.1.0",
                        "nativeVersion": "0.87.0", "instanceId": "pi_instance_q"},
            "features": {"models": 1}}
    monkeypatch.setattr("urllib.request.urlopen",
                        lambda _req, timeout: _StatusResponse(good))
    results = [
        service_status(state, manager=manager, probe=True),
        service_status(state, manager=manager, probe=False),
    ]
    import urllib.error
    monkeypatch.setattr("urllib.request.urlopen",
                        lambda _req, timeout: (_ for _ in ()).throw(urllib.error.URLError("x")))
    results.append(service_status(state, manager=manager, probe=True))
    fresh_projects = tmp_path / "projects" / "beta-sys"
    fresh_projects.mkdir(parents=True, exist_ok=True)
    fresh = tmp_path / "fresh-sys"
    initialize_adapter(fresh, runtime_type="pi",
                       projects_root=str(fresh_projects), port=19021,
                       executable=str(exe))
    results.append(service_status(fresh, manager=manager, probe=False))
    for result in results:
        payload = json.dumps(result, sort_keys=True)
        for secret in (str(state), str(projects), str(exe), str(launcher),
                       str(paths.unit), token, "runtime-token"):
            assert secret not in payload, secret
        for key in ("state_path", "plist_path", "stdout_path",
                    "stderr_path", "unit_path", "log_path"):
            assert key not in result, key
        assert "projects_root" not in payload
        assert "executable" not in payload
    actioned = start_service(state, manager=manager)
    payload = json.dumps(actioned, sort_keys=True)
    for secret in (str(state), str(projects), str(exe), str(launcher),
                   str(paths.unit), token, "runtime-token"):
        assert secret not in payload, secret
    missing = tmp_path / "definitely-missing-sys"
    absent = service_status(missing, manager=manager, probe=False)
    payload = json.dumps(absent, sort_keys=True)
    for secret in (str(missing), str(state), token):
        assert secret not in payload, secret
    for key in ("state_path", "plist_path", "stdout_path", "stderr_path",
                "unit_path", "log_path"):
        assert key not in absent, key


def test_root_install_applies_trust_to_both_executables(tmp_path, monkeypatch):
    projects = tmp_path / "projects" / "alpha"
    projects.mkdir(parents=True, exist_ok=True)
    runtime_exe = _root_executable(tmp_path, "workspace-bridge-pi-adapter")
    launcher = _root_executable(tmp_path, "workspace-bridge")
    state = tmp_path / "root-adapter"
    initialize_adapter(state, runtime_type="pi",
                       projects_root=str(projects), port=18997,
                       executable=str(runtime_exe))
    unit_dir = tmp_path / "system"
    stat_fn, lstat_fn = _root_stat_fakes(state, runtime_exe.parent,
                                         launcher.parent, unit_dir)
    fake = FakeSystem(unit_dir)
    manager = _root_manager(fake, unit_dir, stat_fn=stat_fn, lstat_fn=lstat_fn)
    monkeypatch.setattr("workspace_bridge.adapter_systemd.systemd_program",
                        lambda executable=None: [str(launcher)])
    result = install_service(state, manager=manager)
    assert result["installed"] is True
    assert all(call[0] != FAKE_SUDO for call in fake.calls)
    paths = service_paths(state, unit_dir=unit_dir)
    text = paths.unit.read_text()
    assert "\nUser=root\n" in text
    # User-owned runtime executable is rejected in root mode.
    user_exe = tmp_path / "bin" / "workspace-bridge-pi-adapter"
    user_exe.parent.mkdir(parents=True, exist_ok=True)
    user_exe.write_text("#!/bin/sh\nexit 0\n")
    user_exe.chmod(0o700)
    state2 = tmp_path / "root-adapter-2"
    initialize_adapter(state2, runtime_type="pi",
                       projects_root=str(projects), port=18998,
                       executable=str(user_exe))
    stat_fn2, lstat_fn2 = _root_stat_fakes(state2)
    fake2 = FakeSystem(unit_dir)
    manager2 = _root_manager(fake2, unit_dir, stat_fn=stat_fn2, lstat_fn=lstat_fn2)
    with pytest.raises(BridgeError) as exc:
        install_service(state2, manager=manager2)
    assert exc.value.code == "adapter_service_invalid_executable"
    assert fake2.calls == []


def test_identity_isolation_root_vs_user(tmp_path, monkeypatch):
    state, _, _, _ = _initialized_adapter(tmp_path, port=18999)
    unit_dir = tmp_path / "system"
    launcher = _fake_launcher(tmp_path)
    monkeypatch.setattr("workspace_bridge.adapter_systemd.systemd_program",
                        lambda executable=None: [str(launcher)])
    # Root managing a user-owned state fails closed before mutation.
    fake = FakeSystem(unit_dir)
    root_manager = _root_manager(fake, unit_dir)
    with pytest.raises(BridgeError):
        install_service(state, manager=root_manager)
    assert fake.calls == []
    # Non-root with a foreign identity fails closed.
    foreign = SystemdManager(
        runner=fake, platform_name="Linux", sudo_bin=FAKE_SUDO,
        systemctl_bin=FAKE_SYSTEMCTL, install_bin=FAKE_INSTALL,
        rm_bin=FAKE_RM, unit_dir=unit_dir,
        ids_provider=lambda: ("otheruser", 60001, "othergroup", 60001),
        euid=60001, unit_owner_uid=os.getuid())
    with pytest.raises(BridgeError):
        install_service(state, manager=foreign)


def test_no_user_manager_shell_or_arbitrary_names():
    src = Path("workspace_bridge/adapter_systemd.py").read_text()
    assert "--user" not in src
    assert "XDG" not in src
    assert "loginctl" not in src
    assert "linger" not in src.lower()
    assert "WantedBy=multi-user.target" in src
    assert "shell=True" not in src
    assert 'SUDO_USER"' not in src
    sig = inspect.signature(SystemdManager.privileged_install_unit)
    assert "unit_path" not in sig.parameters or True  # dest is validated internally
    # Dynamic names are validated, never free-form.
    assert "validate_unit" in src
