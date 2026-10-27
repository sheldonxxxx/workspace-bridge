"""One service module runs the Bridge and the Node under launchd or systemd."""
from __future__ import annotations

import json
import logging
import os
import plistlib
import subprocess
import sys

import pytest

from workspace_bridge import oplog
from workspace_bridge.cli import initialize
from workspace_bridge.node_cli import initialize_node
from workspace_bridge.os_service import (BRIDGE, ENV_FILE, MARKER, NODE, Launchd, Systemd,
                                         load_env_file, run_action)
from workspace_bridge.security import BridgeError


class FakeLaunchctl:
    """Tracks one job's load state from the launchctl argv it receives."""

    def __init__(self):
        self.calls: list[list[str]] = []
        self.loaded = False

    def __call__(self, argv):
        self.calls.append(argv)
        verb = argv[1]
        if verb == "print":
            out = "state = running\n\tpid = 4242\n" if self.loaded else ""
            return subprocess.CompletedProcess(argv, 0 if self.loaded else 113, out, "")
        if verb == "bootstrap":
            self.loaded = True
        elif verb == "bootout":
            self.loaded = False
        return subprocess.CompletedProcess(argv, 0, "", "")


class FakeSystemctl:
    def __init__(self):
        self.calls: list[list[str]] = []
        self.active = False

    def __call__(self, argv):
        self.calls.append(argv)
        if argv[0] == "loginctl":
            return subprocess.CompletedProcess(argv, 0, "no\n", "")
        verb = next(part for part in argv[1:] if not part.startswith("--"))
        if verb == "show":
            out = f"LoadState=loaded\nActiveState={'active' if self.active else 'inactive'}\nMainPID=77\n"
            return subprocess.CompletedProcess(argv, 0, out, "")
        if verb in ("start", "restart"):
            self.active = True
        elif verb in ("stop", "disable"):
            self.active = False
        return subprocess.CompletedProcess(argv, 0, "", "")


@pytest.fixture
def bridge_state(tmp_path):
    state = tmp_path / "bridge-state"
    initialize(state, 18765, 18766, 18767)
    return state


@pytest.fixture
def node_state(tmp_path):
    projects = tmp_path / "projects"
    projects.mkdir()
    state = tmp_path / "node-state"
    initialize_node(state, [str(projects)])
    return state


def healthy(component, state):
    return {"status": "healthy", "component": component.name}


def test_launchd_user_agent_lifecycle(bridge_state, tmp_path):
    fake = FakeLaunchctl()
    agent = Launchd(BRIDGE, False, run=fake, directory=tmp_path / "LaunchAgents", uid=501)
    installed = run_action(BRIDGE, "install", bridge_state, manager=agent, euid=os.getuid(), probe=healthy)
    assert installed["scope"] == "user" and installed["running"] and installed["pid"] == 4242
    assert installed["health"] == {"status": "healthy", "component": "bridge"}
    raw = agent.path.read_bytes()
    assert MARKER.encode() in raw
    plist = plistlib.loads(raw)
    assert plist["Label"] == "com.workspace-bridge.bridge"
    assert plist["ProgramArguments"] == [sys.executable, "-m", "workspace_bridge.cli",
                                         "--state", str(bridge_state), "serve"]
    assert plist["EnvironmentVariables"] == {"WB_SERVICE": "1"}
    assert "UserName" not in plist
    assert plist["StandardErrorPath"] == str(bridge_state / "logs" / "bridge.stderr.log")
    assert (bridge_state / "logs").stat().st_mode & 0o777 == 0o700
    assert ["launchctl", "bootstrap", "gui/501", str(agent.path)] in fake.calls

    run_action(BRIDGE, "restart", bridge_state, manager=agent, euid=os.getuid(), probe=healthy)
    assert fake.calls[-2] == ["launchctl", "kickstart", "-k", "gui/501/com.workspace-bridge.bridge"]
    stopped = run_action(BRIDGE, "stop", bridge_state, manager=agent, euid=os.getuid(), probe=healthy)
    assert (stopped["loaded"], stopped["running"]) == (False, False) and "health" not in stopped
    run_action(BRIDGE, "start", bridge_state, manager=agent, euid=os.getuid(), probe=healthy)
    assert fake.loaded
    # Reinstalling replaces the managed definition in place.
    run_action(BRIDGE, "install", bridge_state, manager=agent, euid=os.getuid(), probe=healthy)
    removed = run_action(BRIDGE, "uninstall", bridge_state, manager=agent, euid=os.getuid())
    assert removed["installed"] is False and not agent.path.exists() and not fake.loaded
    assert (bridge_state / "config.json").exists(), "uninstall keeps state"


def test_root_launch_daemon_runs_as_the_state_owner(node_state, tmp_path):
    import pwd
    config = json.loads((node_state / "node-config.json").read_text())
    (node_state / "node-config.json").write_text(json.dumps({**config, "bridge": {"node_id": "x"}}))
    fake = FakeLaunchctl()
    daemon = Launchd(NODE, True, run=fake, directory=tmp_path / "LaunchDaemons")
    run_action(NODE, "install", node_state, root=True, manager=daemon, euid=0, probe=healthy)
    plist = plistlib.loads(daemon.path.read_bytes())
    assert plist["UserName"] == pwd.getpwuid(os.getuid()).pw_name
    assert plist["ProgramArguments"][1:3] == ["-m", "workspace_bridge.node_cli"]
    assert ["launchctl", "bootstrap", "system", str(daemon.path)] in fake.calls


def test_systemd_user_and_system_units(bridge_state, tmp_path):
    fake = FakeSystemctl()
    unit = Systemd(BRIDGE, False, run=fake, directory=tmp_path / "user")
    status = run_action(BRIDGE, "install", bridge_state, manager=unit, euid=os.getuid(), probe=healthy)
    text = unit.path.read_text()
    assert text.startswith(f"# {MARKER}\n")
    assert "Environment=WB_SERVICE=1" in text and "WantedBy=default.target" in text
    assert "User=" not in text
    assert f"ExecStart={sys.executable} -m workspace_bridge.cli --state {bridge_state} serve" in text
    assert [call[:2] for call in fake.calls if call[0] == "systemctl"][:4] == [
        ["systemctl", "--user"]] * 4
    assert status["starts_at_boot"] is False, "user units need lingering to start at boot"
    assert status["running"] and status["pid"] == 77

    system = Systemd(BRIDGE, True, run=FakeSystemctl(), directory=tmp_path / "system")
    run_action(BRIDGE, "install", bridge_state, root=True, manager=system, euid=0, probe=healthy)
    text = system.path.read_text()
    assert "WantedBy=multi-user.target" in text and "\nUser=" in text and "\nGroup=" in text
    assert system.run.calls[0] == ["systemctl", "daemon-reload"]


def test_systemd_quotes_paths_with_spaces(tmp_path):
    state = tmp_path / "state with space"
    unit = Systemd(BRIDGE, False, directory=tmp_path).render(state, "u", "g").decode()
    assert f'"{state}"' in unit


def test_scope_must_be_explicit(bridge_state, tmp_path):
    agent = Launchd(BRIDGE, False, run=FakeLaunchctl(), directory=tmp_path)
    with pytest.raises(BridgeError) as exc:
        run_action(BRIDGE, "install", bridge_state, root=True, manager=agent, euid=os.getuid() or 501)
    assert exc.value.code == "service_needs_root"
    with pytest.raises(BridgeError) as exc:
        run_action(BRIDGE, "install", bridge_state, manager=agent, euid=0)
    assert exc.value.code == "service_scope_ambiguous"
    assert not agent.path.exists()


def test_unsafe_or_unready_state_is_refused(bridge_state, node_state, tmp_path):
    agent = Launchd(NODE, False, run=FakeLaunchctl(), directory=tmp_path / "agents")
    with pytest.raises(BridgeError) as exc:
        run_action(NODE, "install", node_state, manager=agent, euid=os.getuid())
    assert exc.value.code == "node_not_paired"
    with pytest.raises(BridgeError) as exc:
        run_action(NODE, "install", tmp_path / "missing", manager=agent, euid=os.getuid())
    assert exc.value.code == "service_state_missing"
    os.chmod(bridge_state, 0o755)
    try:
        with pytest.raises(BridgeError) as exc:
            run_action(BRIDGE, "install", bridge_state, manager=agent, euid=os.getuid())
        assert exc.value.code == "service_state_unsafe"
    finally:
        os.chmod(bridge_state, 0o700)
    with pytest.raises(BridgeError) as exc:
        run_action(BRIDGE, "install", bridge_state, manager=agent, euid=os.getuid() + 1)
    assert exc.value.code == "service_state_unsafe"
    assert not agent.path.exists()


def test_unmanaged_definitions_are_left_alone(bridge_state, tmp_path):
    fake = FakeLaunchctl()
    agent = Launchd(BRIDGE, False, run=fake, directory=tmp_path)
    agent.path.write_text("<plist>hand written</plist>")
    for action in ("install", "uninstall", "start"):
        with pytest.raises(BridgeError) as exc:
            run_action(BRIDGE, action, bridge_state, manager=agent, euid=os.getuid())
        assert exc.value.code == "service_file_unmanaged"
    assert agent.path.read_text() == "<plist>hand written</plist>"
    assert fake.calls == []


def test_status_names_a_definition_for_another_state_folder(bridge_state, tmp_path):
    fake = FakeLaunchctl()
    agent = Launchd(BRIDGE, False, run=fake, directory=tmp_path)
    run_action(BRIDGE, "install", bridge_state, manager=agent, euid=os.getuid(), probe=healthy)
    other = tmp_path / "other-state"
    other.mkdir(mode=0o700)
    status = run_action(BRIDGE, "status", other, manager=agent, euid=os.getuid(), probe=healthy)
    assert status["serves_state"] == str(bridge_state) and "health" not in status
    unit = Systemd(BRIDGE, False, run=FakeSystemctl(), directory=tmp_path / "units")
    run_action(BRIDGE, "install", bridge_state, manager=unit, euid=os.getuid(), probe=healthy)
    assert unit.defined_state() == str(bridge_state)


def test_status_and_control_before_install(bridge_state, tmp_path):
    fake = FakeLaunchctl()
    agent = Launchd(BRIDGE, False, run=fake, directory=tmp_path)
    status = run_action(BRIDGE, "status", bridge_state, manager=agent, euid=os.getuid())
    assert (status["installed"], status["loaded"], status["running"]) == (False, False, False)
    with pytest.raises(BridgeError) as exc:
        run_action(BRIDGE, "start", bridge_state, manager=agent, euid=os.getuid())
    assert exc.value.code == "service_not_installed"
    assert run_action(BRIDGE, "uninstall", bridge_state, manager=agent, euid=os.getuid())["installed"] is False
    assert all(call[1] == "print" for call in fake.calls)


def test_service_log_is_capped_in_process(tmp_path, monkeypatch):
    log_file = tmp_path / "logs" / "node.log"
    log_file.parent.mkdir()
    crash = tmp_path / "logs" / "node.stderr.log"
    crash.write_bytes(b"x" * (oplog.CRASH_LOG_BYTES + 1))
    monkeypatch.setattr(oplog, "LOG_FILE_BYTES", 2000)
    logger = logging.getLogger("workspace_bridge.ops")
    saved = list(logger.handlers)
    try:
        oplog.configure_operational_logging("INFO", {"WB_SERVICE": "1"}, log_file=log_file)
        assert crash.stat().st_size == 0
        for _ in range(200):
            oplog.emit(logger, "INFO", "node", "node_link_retry", code="ConnectionRefusedError")
        files = sorted(path.name for path in log_file.parent.iterdir())
        assert files == ["node.log", "node.log.1", "node.log.2", "node.stderr.log"]
        assert all((log_file.parent / name).stat().st_size <= 2000 for name in files)
        assert json.loads(log_file.read_text().splitlines()[-1])["event"] == "node_link_retry"
    finally:
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            if handler not in saved:
                handler.close()
        for handler in saved:
            logger.addHandler(handler)


def test_foreground_logs_stay_on_stderr(tmp_path):
    oplog.configure_operational_logging("INFO", {}, log_file=tmp_path / "logs" / "bridge.log")
    assert not (tmp_path / "logs").exists()


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


def test_cli_exposes_root_as_an_explicit_flag(capsys):
    from workspace_bridge.cli import main
    for argv in (["service", "install", "--help"], ["node", "service", "install", "--help"]):
        with pytest.raises(SystemExit):
            main(argv)
        assert "--root" in capsys.readouterr().out
