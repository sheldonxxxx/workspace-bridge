from __future__ import annotations

import hashlib
import inspect
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys

import pytest

from workspace_bridge.node_cli import initialize_node, select_service_backend
from workspace_bridge.node_systemd import (
    MANIFEST_NAME,
    UNIT_NAME,
    UNIT_PATH,
    SystemdManager,
    _encode_systemd_arg,
    _validate_root_executable_trust,
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
    """Fake privileged runner: no real /etc, sudo, or systemctl.

    Accepts fixed sudo-prefixed argv (non-root) and fixed direct helper
    argv (root, EUID 0). Status ``show`` is always unprivileged.
    """

    def __init__(self, unit_dest: Path | None = None):
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
            # Unprivileged status show (both modes).
            assert argv[2] == UNIT_NAME
            assert "--no-pager" in argv
            if not self.systemd_available:
                return subprocess.CompletedProcess(
                    argv, 1, "", "System has not been booted with systemd"
                )
            if not self.show_available:
                return subprocess.CompletedProcess(argv, 1, "LoadState=not-found\n", "")
            out = (
                f"LoadState={self.load_state}\n"
                f"ActiveState={self.active_state}\n"
                f"SubState={self.sub_state}\n"
                f"UnitFileState={self.unit_file_state}\n"
            )
            return subprocess.CompletedProcess(argv, 0, out, "")
        if argv[0] == FAKE_SUDO:
            if not self.sudo_available or self.privilege_fail:
                return subprocess.CompletedProcess(
                    argv, 1, "", "Sorry, user is not allowed to execute '/bin/systemctl' as root"
                )
            helper, args = argv[1], argv[2:]
        else:
            # Root direct execution: fixed helper argv without sudo.
            assert argv[0] in (FAKE_SYSTEMCTL, FAKE_INSTALL, FAKE_RM), argv
            helper, args = argv[0], argv[1:]
        if "install" in helper:
            # install -o root -g root -m 0644 -- <temp> <dest>
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
                argv, 1, "", "System has not been booted with systemd"
            )
        action = args[0]
        if action == "daemon-reload":
            if self.fail_next_reload:
                self.fail_next_reload = False
                return subprocess.CompletedProcess(argv, 1, "", "daemon reload boom")
            return subprocess.CompletedProcess(argv, 0, "", "")
        if action == "enable":
            assert args[1] == "--now"
            assert args[2] == UNIT_NAME
            self.unit_file_state = "enabled"
            self.load_state = "loaded"
            self.active_state = "active"
            self.sub_state = "running"
            return subprocess.CompletedProcess(argv, 0, "", "")
        if action == "start":
            self.active_state = "active"
            self.sub_state = "running"
            self.load_state = "loaded"
            return subprocess.CompletedProcess(argv, 0, "", "")
        if action == "stop":
            self.active_state = "inactive"
            self.sub_state = "dead"
            return subprocess.CompletedProcess(argv, 0, "", "")
        if action == "restart":
            self.active_state = "active"
            self.sub_state = "running"
            self.load_state = "loaded"
            return subprocess.CompletedProcess(argv, 0, "", "")
        if action == "disable":
            assert args[1] == "--now"
            self.unit_file_state = "disabled"
            self.active_state = "inactive"
            self.sub_state = "dead"
            return subprocess.CompletedProcess(argv, 0, "", "")
        raise AssertionError(argv)


def _initialized_node(tmp_path: Path):
    root = tmp_path / "projects" / "alpha"
    root.mkdir(parents=True)
    state = tmp_path / "node-state"
    initialize_node(state, [str(root)], "127.0.0.1", 8770)
    token = (state / "node-token").read_text().strip()
    return state, root, token


def _manager(fake: FakeSystem, unit_path: Path, *, euid: int | None = None) -> SystemdManager:
    uid = os.getuid() if euid is None else euid
    return SystemdManager(
        runner=fake,
        platform_name="Linux",
        sudo_bin=FAKE_SUDO,
        systemctl_bin=FAKE_SYSTEMCTL,
        install_bin=FAKE_INSTALL,
        rm_bin=FAKE_RM,
        unit_path=unit_path,
        ids_provider=_ids,
        euid=uid,
        unit_owner_uid=os.getuid(),
    )


def _strict_manager(fake: FakeSystem, unit_path: Path, *, euid: int | None = None) -> SystemdManager:
    # Production-strict unit ownership (requires root uid 0).
    uid = os.getuid() if euid is None else euid
    return SystemdManager(
        runner=fake,
        platform_name="Linux",
        sudo_bin=FAKE_SUDO,
        systemctl_bin=FAKE_SYSTEMCTL,
        install_bin=FAKE_INSTALL,
        rm_bin=FAKE_RM,
        unit_path=unit_path,
        ids_provider=_ids,
        euid=uid,
    )


def _root_stat_fakes(*owned: Path | str, force_ancestors: bool = True, real_paths: tuple = ()):
    """Claim a root-owned world for designated subtrees (test-only).

    Paths under ``owned`` keep their real modes but report uid/gid 0. When
    ``force_ancestors`` is true, directories outside ``owned`` report
    root-owned 0755 so parent-chain validation can pass without real root
    files; when false they report real stats, letting parent-chain tests
    prove user-owned parents are rejected. ``real_paths`` subtrees always
    report real stats, so one unsafe hop can be simulated inside an
    otherwise root-controlled chain.
    """
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
                 st.st_size, st.st_atime, st.st_mtime, st.st_ctime)
            )
        if force_ancestors and stat.S_ISDIR(st.st_mode):
            return os.stat_result(
                (stat.S_IFDIR | 0o755, st.st_ino, st.st_dev, st.st_nlink, 0, 0,
                 st.st_size, st.st_atime, st.st_mtime, st.st_ctime)
            )
        return st

    return (lambda p: _fake(p, _lstat=False)), (lambda p: _fake(p, _lstat=True))


def _root_manager(
    fake: FakeSystem,
    unit_path: Path,
    *,
    stat_fn=None,
    lstat_fn=None,
) -> SystemdManager:
    return SystemdManager(
        runner=fake,
        platform_name="Linux",
        sudo_bin=FAKE_SUDO,
        systemctl_bin=FAKE_SYSTEMCTL,
        install_bin=FAKE_INSTALL,
        rm_bin=FAKE_RM,
        unit_path=unit_path,
        ids_provider=_root_ids,
        euid=0,
        stat_fn=stat_fn,
        lstat_fn=lstat_fn,
    )


def _fake_executable(tmp_path: Path, name: str = "workspace-bridge") -> Path:
    exe = tmp_path / "bin" / name
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_text("#!/bin/sh\nexit 0\n")
    exe.chmod(0o700)
    return exe


def _root_executable(tmp_path: Path, name: str = "workspace-bridge") -> Path:
    exe = tmp_path / "rootbin" / name
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_text("#!/bin/sh\nexit 0\n")
    exe.chmod(0o755)
    return exe


# --- fixed path / manifest identity ---------------------------------------

def test_fixed_system_path_and_manifest_name():
    assert UNIT_NAME == "workspace-bridge-node.service"
    assert UNIT_PATH == Path("/etc/systemd/system/workspace-bridge-node.service")
    assert MANIFEST_NAME == "systemd-system-manifest.json"
    paths = service_paths("/tmp/some-state")
    assert paths.unit == UNIT_PATH
    assert paths.manifest.name == MANIFEST_NAME
    sig = inspect.signature(service_paths)
    assert "unit_path" not in sig.parameters
    assert "xdg" not in str(sig).lower()
    assert "--user" not in str(sig)


def test_no_user_manager_xdg_linger_terminology():
    src = Path("workspace_bridge/node_systemd.py").read_text()
    assert "--user" not in src
    assert "XDG" not in src
    assert "xdg_config" not in src.lower()
    assert "loginctl" not in src
    assert "linger" not in src.lower()
    assert "systemd-user" not in src
    assert "SystemdUserManager" not in src
    assert "default.target" not in src
    assert "WantedBy=multi-user.target" in src
    assert "shell=True" not in src


def test_no_sudo_user_or_identity_overrides():
    src = Path("workspace_bridge/node_systemd.py").read_text()
    assert 'SUDO_USER"' not in src
    assert "SUDO_USER']" not in src
    assert 'SUDO_USER")' not in src
    assert "getenv" not in src or "SUDO_USER" not in src.split("getenv")[1][:80] if "getenv" in src else True
    assert "require_not_root" not in src
    assert "root_refused" not in src
    cli = Path("workspace_bridge/node_cli.py").read_text()
    assert "--user " not in cli
    assert "--group" not in cli


# --- deterministic unit / secrets ------------------------------------------

def test_unit_bytes_deterministic_with_user_group(tmp_path):
    state = tmp_path / "node-state"
    exe = _fake_executable(tmp_path)
    first = build_systemd_unit(state, [str(exe)], user=TEST_USER, group=TEST_GROUP)
    second = build_systemd_unit(state, [str(exe)], user=TEST_USER, group=TEST_GROUP)
    assert first == second
    text = first.decode()
    assert "Description=Workspace Bridge Node" in text
    assert f"User={TEST_USER}" in text
    assert f"Group={TEST_GROUP}" in text
    assert "Type=simple" in text
    assert "Restart=on-failure" in text
    assert "RestartSec=10" in text
    assert "UMask=0077" in text
    assert "WantedBy=multi-user.target" in text
    assert "ProtectHome" not in text and "ProtectSystem" not in text
    assert "node-token" not in text
    assert "stdout.log" not in text and "stderr.log" not in text
    assert "sh -c" not in text


def test_unit_has_no_secrets_or_roots(tmp_path):
    state, root, token = _initialized_node(tmp_path)
    exe = _fake_executable(tmp_path)
    unit = build_systemd_unit(state, [str(exe)], user=TEST_USER, group=TEST_GROUP)
    assert token.encode() not in unit
    assert str(root).encode() not in unit


def test_execstart_quoting_and_escaping(tmp_path):
    exe = _fake_executable(tmp_path, 'a"b\\c')
    unit = build_systemd_unit(tmp_path / "s", [str(exe)], user=TEST_USER, group=TEST_GROUP).decode()
    assert '\\"' in unit and "\\\\" in unit
    pct = build_systemd_unit(tmp_path / "s", [str(_fake_executable(tmp_path, "a%b"))], user=TEST_USER, group=TEST_GROUP).decode()
    assert "%%" in pct
    dollar = build_systemd_unit(tmp_path / "s", [str(_fake_executable(tmp_path, "a$b"))], user=TEST_USER, group=TEST_GROUP).decode()
    assert "$$" in dollar
    spaced_exe = tmp_path / "dir with spaces" / "workspace-bridge"
    spaced_exe.parent.mkdir(parents=True)
    spaced_exe.write_text("#!/bin/sh\n")
    spaced_exe.chmod(0o700)
    spaced = build_systemd_unit(tmp_path / "state with spaces", [str(spaced_exe)], user=TEST_USER, group=TEST_GROUP).decode()
    assert '"' in spaced


def test_execstart_rejects_control():
    for bad in ("a\nb", "a\x00b", "a\x01b", "a\rb", ""):
        with pytest.raises(BridgeError):
            _encode_systemd_arg(bad)


def test_user_group_validation():
    with pytest.raises(BridgeError):
        build_systemd_unit("/tmp/s", ["/bin/true"], user="bad user", group=TEST_GROUP)
    with pytest.raises(BridgeError):
        build_systemd_unit("/tmp/s", ["/bin/true"], user=TEST_USER, group="bad/group")
    with pytest.raises(BridgeError):
        build_systemd_unit("/tmp/s", ["/bin/true"], user="a\nb", group=TEST_GROUP)


def test_stable_lexical_symlink_preserved_across_upgrade(tmp_path):
    shim_dir = tmp_path / ".local" / "bin"
    shim_dir.mkdir(parents=True)
    target_v1 = tmp_path / "tool" / "v1" / "workspace-bridge"
    target_v1.parent.mkdir(parents=True)
    target_v1.write_text("#!/bin/sh\nexit 0\n")
    target_v1.chmod(0o700)
    shim = shim_dir / "workspace-bridge"
    shim.symlink_to(target_v1)
    state = tmp_path / "node-state"
    unit_v1 = build_systemd_unit(state, [str(shim)], user=TEST_USER, group=TEST_GROUP).decode()
    assert str(shim) in unit_v1
    assert str(target_v1) not in unit_v1
    target_v2 = tmp_path / "tool" / "v2" / "workspace-bridge"
    target_v2.parent.mkdir(parents=True)
    target_v2.write_text("#!/bin/sh\nexit 0\n")
    target_v2.chmod(0o700)
    shim.unlink()
    shim.symlink_to(target_v2)
    unit_v2 = build_systemd_unit(state, [str(shim)], user=TEST_USER, group=TEST_GROUP).decode()
    assert str(shim) in unit_v2
    assert str(target_v2) not in unit_v2
    assert "uv" not in unit_v2


# --- executable validation --------------------------------------------------

def test_install_rejects_bad_executable_before_mutation(tmp_path):
    state, _r, _t = _initialized_node(tmp_path)
    unit_path = tmp_path / "system" / UNIT_NAME
    fake = FakeSystem()
    manager = _manager(fake, unit_path)
    with pytest.raises(BridgeError) as exc:
        install_service(state, manager=manager, executable=tmp_path / "missing")
    assert exc.value.code == "node_service_invalid_executable"
    assert not unit_path.exists()
    assert not (state / MANIFEST_NAME).exists()
    assert fake.calls == []
    bad = _fake_executable(tmp_path, "non-exec")
    bad.chmod(0o600)
    with pytest.raises(BridgeError):
        install_service(state, manager=manager, executable=bad)
    assert fake.calls == []
    with pytest.raises(BridgeError):
        install_service(state, manager=manager, executable="relative/path")
    assert fake.calls == []


# --- root mode: intentional identity -----------------------------------------

def test_root_install_succeeds_without_sudo(tmp_path):
    state, root, token = _initialized_node(tmp_path)
    unit_path = tmp_path / "system" / UNIT_NAME
    exe = _root_executable(tmp_path)
    stat_fn, lstat_fn = _root_stat_fakes(state, exe.parent, unit_path.parent)
    fake = FakeSystem()
    manager = _root_manager(fake, unit_path, stat_fn=stat_fn, lstat_fn=lstat_fn)
    result = install_service(state, manager=manager, executable=exe)
    assert result["installed"] is True
    assert result["service_manager"] == "systemd"
    text = unit_path.read_bytes().decode()
    assert "\nUser=root\n" in text
    assert "\nGroup=root\n" in text
    manifest = json.loads((state / MANIFEST_NAME).read_text())
    assert (manifest["user"], manifest["uid"], manifest["group"], manifest["gid"]) == ("root", 0, "root", 0)
    assert token.encode() not in unit_path.read_bytes()
    # No sudo prefix anywhere in root mode.
    assert all(call[0] != FAKE_SUDO for call in fake.calls)
    assert any(call[0] == FAKE_INSTALL for call in fake.calls)
    assert any(call[0] == FAKE_SYSTEMCTL and call[1] == "daemon-reload" for call in fake.calls)
    assert any(call[0] == FAKE_SYSTEMCTL and call[1] == "enable" for call in fake.calls)


def test_root_does_not_need_sudo_binary(tmp_path):
    state, _r, _t = _initialized_node(tmp_path)
    unit_path = tmp_path / "system" / UNIT_NAME
    exe = _root_executable(tmp_path)
    stat_fn, lstat_fn = _root_stat_fakes(state, exe.parent, unit_path.parent)
    fake = FakeSystem()
    manager = SystemdManager(
        runner=fake,
        platform_name="Linux",
        sudo_bin="/definitely/not/sudo",
        systemctl_bin=FAKE_SYSTEMCTL,
        install_bin=FAKE_INSTALL,
        rm_bin=FAKE_RM,
        unit_path=unit_path,
        ids_provider=_root_ids,
        euid=0,
        stat_fn=stat_fn,
        lstat_fn=lstat_fn,
    )
    result = install_service(state, manager=manager, executable=exe)
    assert result["installed"] is True


def test_root_on_user_owned_state_fails_closed(tmp_path):
    state, _r, _t = _initialized_node(tmp_path)  # real user-owned state
    unit_path = tmp_path / "system" / UNIT_NAME
    fake = FakeSystem()
    manager = _root_manager(fake, unit_path)  # real stat: state is user-owned
    with pytest.raises(BridgeError):
        install_service(state, manager=manager, executable=_root_executable(tmp_path))
    assert not unit_path.exists()
    assert not (state / MANIFEST_NAME).exists()
    assert fake.calls == []


def test_nonroot_on_foreign_state_fails_closed(tmp_path):
    state, _r, _t = _initialized_node(tmp_path)
    unit_path = tmp_path / "system" / UNIT_NAME
    fake = FakeSystem()
    foreign = SystemdManager(
        runner=fake,
        platform_name="Linux",
        sudo_bin=FAKE_SUDO,
        systemctl_bin=FAKE_SYSTEMCTL,
        install_bin=FAKE_INSTALL,
        rm_bin=FAKE_RM,
        unit_path=unit_path,
        ids_provider=lambda: ("otheruser", 60001, "othergroup", 60001),
        euid=60001,
        unit_owner_uid=os.getuid(),
    )
    with pytest.raises(BridgeError):
        install_service(state, manager=foreign, executable=_fake_executable(tmp_path))
    assert fake.calls == []


def test_root_rejects_user_owned_executable(tmp_path):
    state, _r, _t = _initialized_node(tmp_path)
    unit_path = tmp_path / "system" / UNIT_NAME
    exe = _fake_executable(tmp_path)  # user-owned in a user-owned dir
    stat_fn, lstat_fn = _root_stat_fakes(state)  # state passes, exe does not
    fake = FakeSystem()
    manager = _root_manager(fake, unit_path, stat_fn=stat_fn, lstat_fn=lstat_fn)
    with pytest.raises(BridgeError) as exc:
        install_service(state, manager=manager, executable=exe)
    assert exc.value.code == "node_service_invalid_executable"
    assert not unit_path.exists()
    assert fake.calls == []


def test_root_rejects_user_symlink_shim(tmp_path):
    state, _r, _t = _initialized_node(tmp_path)
    unit_path = tmp_path / "system" / UNIT_NAME
    target = _fake_executable(tmp_path, "real-target")
    shim = tmp_path / "shim" / "workspace-bridge"
    shim.parent.mkdir(parents=True)
    shim.symlink_to(target)
    stat_fn, lstat_fn = _root_stat_fakes(state)
    fake = FakeSystem()
    manager = _root_manager(fake, unit_path, stat_fn=stat_fn, lstat_fn=lstat_fn)
    with pytest.raises(BridgeError) as exc:
        install_service(state, manager=manager, executable=shim)
    assert exc.value.code == "node_service_invalid_executable"
    assert fake.calls == []


def test_root_rejects_writable_parent_chain(tmp_path):
    exe = _root_executable(tmp_path)
    # Only the executable itself is claimed root-owned; its user-owned
    # parent directory must still fail the chain check.
    stat_fn, lstat_fn = _root_stat_fakes(exe, force_ancestors=False)
    with pytest.raises(BridgeError) as exc:
        _validate_root_executable_trust(exe, stat_fn=stat_fn, lstat_fn=lstat_fn)
    assert exc.value.code == "node_service_invalid_executable"


def test_root_trust_accepts_controlled_tree_and_preserves_lexical(tmp_path):
    target = tmp_path / "rootbin" / "wb-real"
    target.parent.mkdir(parents=True)
    target.write_text("#!/bin/sh\nexit 0\n")
    target.chmod(0o755)
    link = tmp_path / "rootbin" / "workspace-bridge"
    link.symlink_to(target)
    stat_fn, lstat_fn = _root_stat_fakes(link, target, link.parent)
    out = _validate_root_executable_trust(link, stat_fn=stat_fn, lstat_fn=lstat_fn)
    assert out == Path(os.path.normpath(str(link)))
    unit = build_systemd_unit(tmp_path / "s", [str(link)], user="root", group="root").decode()
    assert str(link) in unit
    assert str(target) not in unit


def test_root_trust_rejects_user_paths_without_fakes(tmp_path):
    exe = _fake_executable(tmp_path)
    with pytest.raises(BridgeError):
        _validate_root_executable_trust(exe)


def test_root_trust_rejects_unsafe_intermediate_hop(tmp_path):
    # Multi-hop chain: lexical and final parents are root-controlled, but
    # the intermediate symlink lives under a group/world-writable,
    # non-root-owned parent and must fail.
    safe = tmp_path / "safe"
    shared = tmp_path / "shared"
    safe.mkdir()
    shared.mkdir()
    final = safe / "final"
    final.write_text("#!/bin/sh\nexit 0\n")
    final.chmod(0o755)
    link = shared / "link"
    link.symlink_to(final)
    lexical = safe / "wb"
    lexical.symlink_to(link)
    os.chmod(shared, 0o775)
    stat_fn, lstat_fn = _root_stat_fakes(
        lexical, final, safe, link, real_paths=(shared,)
    )
    with pytest.raises(BridgeError) as exc:
        _validate_root_executable_trust(lexical, stat_fn=stat_fn, lstat_fn=lstat_fn)
    assert exc.value.code == "node_service_invalid_executable"
    # Install through the same chain fails before any mutation.
    chain_root = tmp_path / "chainstate"
    chain_root.mkdir()
    state, _r, _t = _initialized_node(chain_root)
    unit_path = tmp_path / "system" / UNIT_NAME
    fake = FakeSystem()
    install_stat, install_lstat = _root_stat_fakes(
        state, lexical, final, safe, link, real_paths=(shared,)
    )
    manager = _root_manager(
        fake, unit_path, stat_fn=install_stat, lstat_fn=install_lstat
    )
    with pytest.raises(BridgeError) as exc2:
        install_service(state, manager=manager, executable=lexical)
    assert exc2.value.code == "node_service_invalid_executable"
    assert not unit_path.exists()
    assert fake.calls == []


def test_root_trust_accepts_all_controlled_multihop(tmp_path):
    safe = tmp_path / "safe"
    safe2 = tmp_path / "safe2"
    safe.mkdir()
    safe2.mkdir()
    final = safe / "final"
    final.write_text("#!/bin/sh\nexit 0\n")
    final.chmod(0o755)
    link = safe2 / "link"
    link.symlink_to(final)
    lexical = safe / "wb"
    lexical.symlink_to(link)
    stat_fn, lstat_fn = _root_stat_fakes(lexical, link, final, safe, safe2)
    out = _validate_root_executable_trust(lexical, stat_fn=stat_fn, lstat_fn=lstat_fn)
    assert out == Path(os.path.normpath(str(lexical)))


def test_identity_switch_requires_uninstall(tmp_path):
    state, _r, _t = _initialized_node(tmp_path)
    unit_path = tmp_path / "system" / UNIT_NAME
    exe = _fake_executable(tmp_path)
    fake = FakeSystem()
    install_service(state, manager=_manager(fake, unit_path), executable=exe)
    # Same state managed as root must conflict (state ownership gate).
    with pytest.raises(BridgeError):
        install_service(state, manager=_root_manager(FakeSystem(), unit_path), executable=exe)
    # Root-installed service managed as non-root must conflict (manifest identity).
    state2 = tmp_path / "s2"
    root2 = tmp_path / "p2" / "alpha"
    root2.mkdir(parents=True)
    initialize_node(state2, [str(root2)], "127.0.0.1", 8771)
    unit2 = tmp_path / "sys2" / UNIT_NAME
    exe2 = _root_executable(tmp_path, "wb2")
    stat_fn, lstat_fn = _root_stat_fakes(state2, exe2.parent, unit2.parent)
    fake2 = FakeSystem()
    install_service(state2, manager=_root_manager(fake2, unit2, stat_fn=stat_fn, lstat_fn=lstat_fn), executable=exe2)
    with pytest.raises(BridgeError) as exc:
        start_service(state2, manager=_manager(FakeSystem(), unit2))
    assert exc.value.code == "node_service_conflict"


def test_root_lifecycle_uses_direct_calls(tmp_path):
    state, _r, token = _initialized_node(tmp_path)
    unit_path = tmp_path / "system" / UNIT_NAME
    exe = _root_executable(tmp_path)
    stat_fn, lstat_fn = _root_stat_fakes(state, exe.parent, unit_path.parent)
    fake = FakeSystem()
    manager = _root_manager(fake, unit_path, stat_fn=stat_fn, lstat_fn=lstat_fn)
    install_service(state, manager=manager, executable=exe)
    assert all(call[0] != FAKE_SUDO for call in fake.calls)
    fake.calls.clear()
    stop_service(state, manager=manager)
    assert any(call[0] == FAKE_SYSTEMCTL and call[1] == "stop" for call in fake.calls)
    assert all(call[0] != FAKE_SUDO for call in fake.calls)
    fake.calls.clear()
    start_service(state, manager=manager)
    assert any(call[0] == FAKE_SYSTEMCTL and call[1] == "start" for call in fake.calls)
    fake.calls.clear()
    restart_service(state, manager=manager)
    assert any(call[0] == FAKE_SYSTEMCTL and call[1] == "restart" for call in fake.calls)
    before = (state / "node-config.json").read_bytes()
    uninstall_service(state, manager=manager)
    assert not unit_path.exists()
    assert not (state / MANIFEST_NAME).exists()
    assert (state / "node-config.json").read_bytes() == before
    assert (state / "node-token").read_text().strip() == token


def test_root_status_has_no_sudo(tmp_path):
    state, _r, _t = _initialized_node(tmp_path)
    unit_path = tmp_path / "system" / UNIT_NAME
    exe = _root_executable(tmp_path)
    stat_fn, lstat_fn = _root_stat_fakes(state, exe.parent, unit_path.parent)
    fake = FakeSystem()
    manager = _root_manager(fake, unit_path, stat_fn=stat_fn, lstat_fn=lstat_fn)
    install_service(state, manager=manager, executable=exe)
    fake.calls.clear()
    status = service_status(state, manager=manager, probe=False)
    assert status["installed"] is True
    assert all(call[0] != FAKE_SUDO for call in fake.calls)


# --- install privileged sequence (non-root) ----------------------------------

def test_install_privileged_copy_reload_enable(tmp_path):
    state, root, token = _initialized_node(tmp_path)
    unit_path = tmp_path / "system" / UNIT_NAME
    fake = FakeSystem()
    manager = _manager(fake, unit_path)
    exe = _fake_executable(tmp_path)
    result = install_service(state, manager=manager, executable=exe)
    assert result["service_manager"] == "systemd"
    assert result["installed"] is True
    assert result["action"] == "install"
    assert unit_path.exists()
    assert stat.S_IMODE(unit_path.stat().st_mode) == 0o644
    assert stat.S_IMODE((state / MANIFEST_NAME).stat().st_mode) == 0o600
    manifest = json.loads((state / MANIFEST_NAME).read_text())
    assert manifest["unit"] == UNIT_NAME
    assert manifest["unit_path"] == str(unit_path)
    assert manifest["user"] == TEST_USER and manifest["group"] == TEST_GROUP
    assert manifest["unit_sha256"] == hashlib.sha256(unit_path.read_bytes()).hexdigest()
    assert token.encode() not in unit_path.read_bytes()
    assert str(root).encode() not in unit_path.read_bytes()
    assert any(call[0] == FAKE_SUDO and "install" in call[1] for call in fake.calls)
    assert any(call[0] == FAKE_SUDO and call[2] == "daemon-reload" for call in fake.calls)
    assert any(call[0] == FAKE_SUDO and call[2] == "enable" for call in fake.calls)
    for call in fake.calls:
        assert "--user" not in call
        assert call[0] in (FAKE_SUDO, FAKE_SYSTEMCTL)
    install_calls = [call for call in fake.calls if "install" in call[1]]
    assert install_calls and install_calls[0][2:8] == ["-o", "root", "-g", "root", "-m", "0644"]


def test_status_uses_no_sudo(tmp_path):
    state, _r, _t = _initialized_node(tmp_path)
    unit_path = tmp_path / "system" / UNIT_NAME
    fake = FakeSystem()
    manager = _manager(fake, unit_path)
    service_status(state, manager=manager, probe=False)
    assert fake.calls
    assert all(call[0] == FAKE_SYSTEMCTL for call in fake.calls)


def test_install_idempotent_no_reinstall(tmp_path):
    state, _r, _t = _initialized_node(tmp_path)
    unit_path = tmp_path / "system" / UNIT_NAME
    fake = FakeSystem()
    manager = _manager(fake, unit_path)
    install_service(state, manager=manager, executable=_fake_executable(tmp_path))
    before = unit_path.read_bytes()
    manifest_before = (state / MANIFEST_NAME).read_bytes()
    fake.calls.clear()
    second = install_service(state, manager=manager, executable=_fake_executable(tmp_path))
    assert second["installed"] is True
    assert unit_path.read_bytes() == before
    assert (state / MANIFEST_NAME).read_bytes() == manifest_before
    assert any(call[2] == "daemon-reload" for call in fake.calls if len(call) > 2)
    assert any(call[2] == "enable" for call in fake.calls if len(call) > 2)
    assert not any("install" in call[1] and "-o" in call for call in fake.calls)


def test_partial_manifest_only_retry(tmp_path):
    state, _r, _t = _initialized_node(tmp_path)
    unit_path = tmp_path / "system" / UNIT_NAME
    fake = FakeSystem()
    manager = _manager(fake, unit_path)
    install_service(state, manager=manager, executable=_fake_executable(tmp_path))
    unit_path.unlink()
    assert (state / MANIFEST_NAME).exists()
    fake.calls.clear()
    result = install_service(state, manager=manager, executable=_fake_executable(tmp_path))
    assert result["installed"] is True
    assert unit_path.exists()
    assert any("install" in call[1] for call in fake.calls)


# --- conflicts -----------------------------------------------------------------

def test_conflicts_unmanaged_modified_symlink_writable_ownership(tmp_path):
    state, _r, _t = _initialized_node(tmp_path)
    unit_path = tmp_path / "system" / UNIT_NAME
    exe = _fake_executable(tmp_path)
    unit_path.parent.mkdir(parents=True)
    unit_path.write_bytes(b"foreign")
    os.chmod(unit_path, 0o644)
    fake = FakeSystem()
    with pytest.raises(BridgeError) as exc:
        install_service(state, manager=_manager(fake, unit_path), executable=exe)
    assert exc.value.code == "node_service_conflict"
    assert unit_path.read_bytes() == b"foreign"
    state2 = tmp_path / "s2"
    root2 = tmp_path / "p2" / "alpha"
    root2.mkdir(parents=True)
    initialize_node(state2, [str(root2)], "127.0.0.1", 8771)
    unit2 = tmp_path / "sys2" / UNIT_NAME
    fake2 = FakeSystem()
    install_service(state2, manager=_manager(fake2, unit2), executable=exe)
    unit2.write_bytes(unit2.read_bytes() + b"\n# tampered")
    os.chmod(unit2, 0o644)
    with pytest.raises(BridgeError) as exc2:
        install_service(state2, manager=_manager(FakeSystem(), unit2), executable=exe)
    assert exc2.value.code == "node_service_conflict"
    with pytest.raises(BridgeError) as exc3:
        uninstall_service(state2, manager=_manager(FakeSystem(), unit2))
    assert exc3.value.code == "node_service_conflict"
    unit3 = tmp_path / "sys3" / UNIT_NAME
    unit3.parent.mkdir(parents=True)
    real = tmp_path / "real.service"
    real.write_bytes(b"real")
    unit3.symlink_to(real)
    with pytest.raises(BridgeError) as exc4:
        install_service(state, manager=_manager(FakeSystem(), unit3), executable=exe)
    assert exc4.value.code == "node_service_conflict"
    unit4 = tmp_path / "sys4" / UNIT_NAME
    unit4.parent.mkdir(parents=True)
    unit4.write_bytes(b"x")
    os.chmod(unit4, 0o666)
    with pytest.raises(BridgeError) as exc5:
        install_service(state, manager=_manager(FakeSystem(), unit4), executable=exe)
    assert exc5.value.code == "node_service_conflict"
    unit5 = tmp_path / "sys5" / UNIT_NAME
    unit5.parent.mkdir(parents=True)
    unit5.write_bytes(b"user-owned")
    os.chmod(unit5, 0o644)
    assert os.getuid() != 0
    with pytest.raises(BridgeError) as exc6:
        install_service(state, manager=_strict_manager(FakeSystem(), unit5), executable=exe)
    assert exc6.value.code == "node_service_conflict"


# --- privilege / systemd availability -------------------------------------------

def test_privilege_denied_fails_closed(tmp_path):
    state, _r, _t = _initialized_node(tmp_path)
    unit_path = tmp_path / "system" / UNIT_NAME
    fake = FakeSystem()
    fake.privilege_fail = True
    with pytest.raises(BridgeError) as exc:
        install_service(state, manager=_manager(fake, unit_path), executable=_fake_executable(tmp_path))
    assert exc.value.code == "node_service_privilege_unavailable"
    assert (state / MANIFEST_NAME).exists()


def test_systemd_unavailable_status_and_mutation(tmp_path):
    state, _r, _t = _initialized_node(tmp_path)
    unit_path = tmp_path / "system" / UNIT_NAME
    fake = FakeSystem()
    manager = _manager(fake, unit_path)
    install_service(state, manager=manager, executable=_fake_executable(tmp_path))
    fake.systemd_available = False
    status = service_status(state, manager=manager, probe=False)
    assert status["systemd"]["available"] is False
    assert status["state"] == "unavailable"
    with pytest.raises(BridgeError) as exc:
        start_service(state, manager=manager)
    assert exc.value.code in {"node_service_systemd_unavailable", "node_service_privilege_unavailable"}


# --- lifecycle --------------------------------------------------------------------

def test_start_stop_restart_use_sudo_systemctl(tmp_path):
    state, _r, _t = _initialized_node(tmp_path)
    unit_path = tmp_path / "system" / UNIT_NAME
    fake = FakeSystem()
    manager = _manager(fake, unit_path)
    install_service(state, manager=manager, executable=_fake_executable(tmp_path))
    fake.calls.clear()
    stop_service(state, manager=manager)
    assert any(call[0] == FAKE_SUDO and call[2] == "stop" for call in fake.calls)
    fake.calls.clear()
    start_service(state, manager=manager)
    assert any(call[0] == FAKE_SUDO and call[2] == "start" for call in fake.calls)
    fake.calls.clear()
    restart_service(state, manager=manager)
    assert any(call[0] == FAKE_SUDO and call[2] == "restart" for call in fake.calls)
    for call in fake.calls:
        assert "--user" not in call


def test_status_mapping_and_secrets(tmp_path, monkeypatch):
    state, _r, token = _initialized_node(tmp_path)
    unit_path = tmp_path / "system" / UNIT_NAME
    fake = FakeSystem()
    manager = _manager(fake, unit_path)
    install_service(state, manager=manager, executable=_fake_executable(tmp_path))
    fake.active_state = "active"
    fake.sub_state = "running"
    fake.unit_file_state = "enabled"
    payload = {"status": "ok", "protocol": 1, "allowed_roots": [{"available": True, "root_label": "alpha"}]}

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return None

        def read(self, _l):
            return json.dumps(payload).encode()

    monkeypatch.setattr(
        "workspace_bridge.node_launchd.urllib.request.urlopen", lambda _r, timeout: Resp()
    )
    status = service_status(state, manager=manager, probe=True)
    assert status["service_manager"] == "systemd"
    assert status["running"] is True and status["state"] == "running"
    blob = json.dumps(status)
    assert token not in blob
    assert "ExecStart" not in blob
    assert "journal" not in blob.lower()
    assert ".config/systemd" not in blob
    fake.active_state = "failed"
    fake.sub_state = "failed"
    assert service_status(state, manager=manager, probe=False)["state"] == "failed"


def test_uninstall_preserves_state(tmp_path):
    state, _r, token = _initialized_node(tmp_path)
    unit_path = tmp_path / "system" / UNIT_NAME
    fake = FakeSystem()
    manager = _manager(fake, unit_path)
    before = (state / "node-config.json").read_bytes()
    install_service(state, manager=manager, executable=_fake_executable(tmp_path))
    sibling = unit_path.parent / "other.service"
    sibling.write_text("other")
    uninstall_service(state, manager=manager)
    assert not unit_path.exists()
    assert not (state / MANIFEST_NAME).exists()
    assert sibling.exists()
    assert (state / "node-config.json").read_bytes() == before
    assert (state / "node-token").read_text().strip() == token
    assert any(call[2] == "disable" for call in fake.calls if len(call) > 2)
    assert any(call[2] == "daemon-reload" for call in fake.calls if len(call) > 2)


def test_uninstall_retry_after_reload_failure(tmp_path):
    state, _r, token = _initialized_node(tmp_path)
    unit_path = tmp_path / "system" / UNIT_NAME
    fake = FakeSystem()
    manager = _manager(fake, unit_path)
    install_service(state, manager=manager, executable=_fake_executable(tmp_path))
    # Simulate remove-unit success followed by daemon-reload failure.
    fake.fail_next_reload = True
    with pytest.raises(BridgeError) as exc:
        uninstall_service(state, manager=manager)
    assert exc.value.code == "node_service_systemctl_failed"
    assert not unit_path.exists()
    assert (state / MANIFEST_NAME).exists()
    # Retry completes cleanup without touching remove again.
    fake.calls.clear()
    result = uninstall_service(state, manager=manager)
    assert result["installed"] is False
    assert not (state / MANIFEST_NAME).exists()
    assert not any(
        (call[0] == FAKE_SUDO and "rm" in call[1]) or call[0] == FAKE_RM
        for call in fake.calls
    )
    assert any(call[2] == "daemon-reload" for call in fake.calls if len(call) > 2)
    assert (state / "node-token").read_text().strip() == token


def test_uninstall_manifest_only_foreign_or_invalid_fails_closed(tmp_path):
    state, _r, _t = _initialized_node(tmp_path)
    unit_path = tmp_path / "system" / UNIT_NAME
    unit_path.parent.mkdir(parents=True)
    # Invalid manifest bytes with no unit present.
    (state / MANIFEST_NAME).write_text("not-json")
    os.chmod(state / MANIFEST_NAME, 0o600)
    with pytest.raises(BridgeError) as exc:
        uninstall_service(state, manager=_manager(FakeSystem(), unit_path))
    assert exc.value.code == "node_service_conflict"
    assert (state / MANIFEST_NAME).exists()
    # Foreign identity manifest with no unit present.
    (state / MANIFEST_NAME).unlink()
    fake = FakeSystem()
    manager = _manager(fake, unit_path)
    install_service(state, manager=manager, executable=_fake_executable(tmp_path))
    unit_path.unlink()  # simulate prior removal
    manifest_path = state / MANIFEST_NAME
    value = json.loads(manifest_path.read_text())
    value["uid"] = 60001
    value["user"] = "otheruser"
    manifest_path.write_text(json.dumps(value, sort_keys=True) + "\n")
    os.chmod(manifest_path, 0o600)
    with pytest.raises(BridgeError) as exc2:
        uninstall_service(state, manager=_manager(FakeSystem(), unit_path))
    assert exc2.value.code == "node_service_conflict"
    assert manifest_path.exists()


def test_platform_dispatch_and_help():
    assert select_service_backend("Darwin") == "launchd"
    assert select_service_backend("Linux") == "systemd"
    with pytest.raises(BridgeError) as exc:
        select_service_backend("Windows")
    assert exc.value.code == "node_service_unsupported_platform"
    with pytest.raises(BridgeError):
        SystemdManager(platform_name="Darwin").require_linux()
    import subprocess as _sp

    out = _sp.run([sys.executable, "-m", "workspace_bridge.node_cli", "--help"], capture_output=True, text=True)
    combined = (out.stdout or "") + (out.stderr or "")
    assert "systemd" in combined.lower()
    assert "launchd" in combined.lower()
    assert "--user" not in combined


def test_systemd_analyze_verify_when_available(tmp_path):
    analyzer = shutil.which("systemd-analyze")
    if analyzer is None:
        pytest.skip("systemd-analyze not present")
    exe = _fake_executable(tmp_path)
    state = tmp_path / "node-state"
    state.mkdir()
    unit_bytes = render_systemd_unit(state, [str(exe)], user=TEST_USER, group=TEST_GROUP)
    unit_file = tmp_path / "verify.service"
    unit_file.write_bytes(unit_bytes)
    result = subprocess.run([analyzer, "verify", str(unit_file)], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr[:500]


def test_systemd_analyze_verify_root_unit_when_available(tmp_path):
    analyzer = shutil.which("systemd-analyze")
    if analyzer is None:
        pytest.skip("systemd-analyze not present")
    exe = _root_executable(tmp_path)
    state = tmp_path / "node-state"
    state.mkdir()
    unit_bytes = render_systemd_unit(state, [str(exe)], user="root", group="root")
    unit_file = tmp_path / "verify-root.service"
    unit_file.write_bytes(unit_bytes)
    result = subprocess.run([analyzer, "verify", str(unit_file)], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr[:500]
