"""The Bridge's own OS service reuses the Node lifecycle with its own spec."""
from __future__ import annotations

import json
import os
import plistlib
import subprocess
from pathlib import Path

import pytest

from workspace_bridge.bridge_service import (ENV_FILE, launchd_spec, load_env_file,
                                             run_action, systemd_spec)
from workspace_bridge.cli import initialize
from workspace_bridge.node_launchd import LaunchdManager
from workspace_bridge.node_systemd import SystemdManager
from workspace_bridge.security import BridgeError

BRIDGE_UNIT = "workspace-bridge.service"


class FakeSystemctl:
    """Records fixed helper argv; never touches real /etc, sudo or systemd."""

    def __init__(self):
        self.calls: list[list[str]] = []
        self.active = "inactive"

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        helper, args = (argv[1], argv[2:]) if argv[0] == "/fake/sudo" else (argv[0], argv[1:])
        if argv[0] == "/fake/systemctl" and argv[1] == "show":
            assert argv[2] == BRIDGE_UNIT
            return subprocess.CompletedProcess(
                argv, 0, f"LoadState=loaded\nActiveState={self.active}\nSubState=running\n"
                         "UnitFileState=enabled\n", "")
        if helper == "/fake/install":
            Path(args[-1]).write_bytes(Path(args[-2]).read_bytes())
        elif helper == "/fake/rm":
            Path(args[-1]).unlink()
        elif args[0] in {"enable", "start", "restart"}:
            assert BRIDGE_UNIT in args
            self.active = "active"
        elif args[0] in {"stop", "disable"}:
            self.active = "inactive"
        return subprocess.CompletedProcess(argv, 0, "", "")


@pytest.fixture
def bridge_state(tmp_path):
    state = tmp_path / "bridge-state"
    initialize(state, 18765, 18766, 18767)
    return state


def _systemd_manager(fake, unit: Path) -> SystemdManager:
    return SystemdManager(
        runner=fake, platform_name="Linux", sudo_bin="/fake/sudo",
        systemctl_bin="/fake/systemctl", install_bin="/fake/install", rm_bin="/fake/rm",
        unit_path=unit, ids_provider=lambda: ("tester", os.getuid(), "testers", os.getgid()),
        euid=os.getuid(), unit_owner_uid=os.getuid(), spec=systemd_spec())


def test_systemd_lifecycle_runs_the_bridge_launcher(bridge_state, tmp_path):
    fake, unit = FakeSystemctl(), tmp_path / "etc" / BRIDGE_UNIT
    unit.parent.mkdir()
    manager = _systemd_manager(fake, unit)
    installed = run_action("install", bridge_state, platform_name="Linux", manager=manager)
    text = unit.read_text()
    assert "Description=Workspace Bridge\n" in text
    exec_start = next(line for line in text.splitlines() if line.startswith("ExecStart="))
    assert exec_start.endswith(f"--state {bridge_state} serve")
    assert " node " not in exec_start
    assert installed["unit"] == BRIDGE_UNIT and installed["installed"] is True
    assert installed["listen"] == {"mcp_port": 18765, "admin_port": 18766, "gateway_port": 18767}
    manifest = json.loads((bridge_state / "systemd-system-manifest.json").read_text())
    assert manifest["unit"] == BRIDGE_UNIT
    status = run_action("status", bridge_state, platform_name="Linux", manager=manager)
    assert status["health"]["status"] in {"unavailable", "healthy"}
    assert run_action("restart", bridge_state, platform_name="Linux", manager=manager)["action"] == "restart"
    run_action("uninstall", bridge_state, platform_name="Linux", manager=manager)
    assert not unit.exists()
    assert (bridge_state / "config.json").exists()


def test_systemd_errors_are_reported_as_bridge_errors(bridge_state, tmp_path):
    fake, unit = FakeSystemctl(), tmp_path / "etc" / BRIDGE_UNIT
    unit.parent.mkdir()
    with pytest.raises(BridgeError) as missing:
        run_action("start", bridge_state, platform_name="Linux",
                   manager=_systemd_manager(fake, unit))
    assert missing.value.code == "bridge_service_not_installed"
    assert "Node" not in str(missing.value)


def test_unsafe_bridge_state_is_refused(bridge_state, tmp_path):
    os.chmod(bridge_state / "config.json", 0o644)
    unit = tmp_path / "etc" / BRIDGE_UNIT
    unit.parent.mkdir()
    with pytest.raises(BridgeError):
        run_action("install", bridge_state, platform_name="Linux",
                   manager=_systemd_manager(FakeSystemctl(), unit))
    assert not unit.exists()


class FakeLaunchctl:
    def __init__(self):
        self.calls: list[list[str]] = []
        self.loaded = False

    def __call__(self, command, **_kwargs):
        self.calls.append(list(command))
        action = command[1]
        if action == "print":
            assert command[2].endswith("/com.workspace-bridge.bridge")
            if not self.loaded:
                return subprocess.CompletedProcess(command, 3, "", "service not found")
            return subprocess.CompletedProcess(command, 0, "state = running\npid = 1\n", "")
        self.loaded = action in {"bootstrap", "kickstart"}
        return subprocess.CompletedProcess(command, 0, "", "")


def test_launchd_lifecycle_uses_the_bridge_label_and_entrypoint(bridge_state, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    executable = tmp_path / "bin" / "workspace-bridge"
    executable.parent.mkdir()
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o700)
    fake = FakeLaunchctl()
    manager = LaunchdManager(runner=fake, platform_name="Darwin", home=home, uid=501,
                             spec=launchd_spec())
    from workspace_bridge.node_launchd import install_service
    result = install_service(bridge_state, manager=manager, executable=executable)
    plist_path = home / "Library" / "LaunchAgents" / "com.workspace-bridge.bridge.plist"
    plist = plistlib.loads(plist_path.read_bytes())
    assert plist["Label"] == "com.workspace-bridge.bridge"
    assert plist["ProgramArguments"] == [str(executable), "--state", str(bridge_state), "serve"]
    assert result["label"] == "com.workspace-bridge.bridge" and result["installed"] is True
    run_action("uninstall", bridge_state, platform_name="Darwin", manager=manager)
    assert not plist_path.exists()


def test_env_file_applies_allowlisted_settings_and_real_env_wins(bridge_state):
    path = bridge_state / ENV_FILE
    path.write_text("# gateway on the VPN\nWB_NODE_GATEWAY_HOST=100.64.0.7\n"
                    "WB_LOG_LEVEL='DEBUG'\n")
    os.chmod(path, 0o600)
    env = {"WB_LOG_LEVEL": "INFO"}
    assert load_env_file(bridge_state, env) == ["WB_NODE_GATEWAY_HOST"]
    assert env == {"WB_LOG_LEVEL": "INFO", "WB_NODE_GATEWAY_HOST": "100.64.0.7"}


@pytest.mark.parametrize("content", ["PATH=/tmp/evil\n", "not a setting\n"])
def test_env_file_refuses_unknown_or_malformed_lines(bridge_state, content):
    path = bridge_state / ENV_FILE
    path.write_text(content)
    os.chmod(path, 0o600)
    with pytest.raises(BridgeError):
        load_env_file(bridge_state, {})


def test_env_file_must_be_private(bridge_state):
    path = bridge_state / ENV_FILE
    path.write_text("WB_LOG_LEVEL=INFO\n")
    os.chmod(path, 0o644)
    with pytest.raises(BridgeError):
        load_env_file(bridge_state, {})
