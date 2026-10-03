from __future__ import annotations

import json
import os
from pathlib import Path
import plistlib
import stat
import subprocess

import pytest

from workspace_bridge.node_cli import initialize_node
from workspace_bridge.node_launchd import (
    LAUNCH_AGENT_LABEL,
    LaunchctlResult,
    LaunchdManager,
    build_launch_agent_plist,
    install_service,
    launch_agent_program,
    probe_node_status,
    restart_service,
    service_paths,
    service_status,
    start_service,
    stop_service,
    uninstall_service,
)
from workspace_bridge.security import BridgeError


class FakeLaunchctl:
    def __init__(self):
        self.calls: list[list[str]] = []
        self.loaded = False
        self.running = False

    def __call__(self, command, **_kwargs):
        self.calls.append(list(command))
        action = command[1]
        if action == "print":
            if not self.loaded:
                return subprocess.CompletedProcess(command, 3, "", "service not found")
            state = "running" if self.running else "waiting"
            return subprocess.CompletedProcess(
                command, 0, f"state = {state}\npid = 4321\n", "")
        if action == "bootstrap":
            self.loaded = True
            self.running = True
            return subprocess.CompletedProcess(command, 0, "", "")
        if action == "bootout":
            self.loaded = False
            self.running = False
            return subprocess.CompletedProcess(command, 0, "", "")
        if action == "kickstart":
            self.loaded = True
            self.running = True
            return subprocess.CompletedProcess(command, 0, "", "")
        raise AssertionError(command)


def _initialized_node(tmp_path: Path, *, host: str = "127.0.0.1"):
    root = tmp_path / "projects" / "alpha"
    root.mkdir(parents=True)
    state = tmp_path / "node-state"
    initialize_node(state, [str(root)], host, 8770)
    token = (state / "node-token").read_text().strip()
    home = tmp_path / "home"
    home.mkdir()
    return state, root, token, home


def _manager(fake: FakeLaunchctl, home: Path) -> LaunchdManager:
    return LaunchdManager(runner=fake, platform_name="Darwin", home=home, uid=501)


def _fake_executable(tmp_path: Path, name: str = "workspace-bridge-node") -> Path:
    executable = tmp_path / "bin" / name
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o700)
    return executable


class _StatusResponse:
    def __init__(self, payload: dict):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self, _limit):
        return json.dumps(self.payload).encode()


def test_launchagent_plist_is_deterministic_and_does_not_contain_secrets(tmp_path):
    state = tmp_path / "state with spaces"
    executable = tmp_path / "venv with spaces" / "bin" / "workspace-bridge-node"
    home = tmp_path / "home"
    first = build_launch_agent_plist(state, executable, home=home)
    second = build_launch_agent_plist(state, executable, home=home)

    assert first == second
    payload = plistlib.loads(first)
    assert payload["Label"] == LAUNCH_AGENT_LABEL
    assert payload["ProgramArguments"] == [
        str(executable), "--state", str(state), "serve"]
    assert payload["RunAtLoad"] is True
    assert payload["KeepAlive"] == {"SuccessfulExit": False}
    assert payload["ThrottleInterval"] == 10
    assert payload["StandardOutPath"].endswith("/logs/stdout.log")
    assert payload["StandardErrorPath"].endswith("/logs/stderr.log")
    assert b"node-token" not in first
    assert b"workspace-bridge-node-token" not in first


def test_install_bootstrap_and_service_actions_use_gui_domain_and_preserve_state(tmp_path):
    state, root, token, home = _initialized_node(tmp_path, host="0.0.0.0")
    fake = FakeLaunchctl()
    manager = _manager(fake, home)
    executable = _fake_executable(tmp_path, "workspace-bridge-node with spaces")
    before_config = (state / "node-config.json").read_bytes()
    before_roots = json.loads(before_config)["allowed_roots"]

    installed = install_service(state, manager=manager, executable=executable)
    paths = service_paths(state, home=home)
    payload = plistlib.loads(paths.plist.read_bytes())
    assert installed["installed"] is True
    assert installed["state"] == "running"
    assert installed["listen"] == {
        "host": "0.0.0.0", "port": 8770, "endpoint": "http://0.0.0.0:8770"}
    assert payload["ProgramArguments"] == [
        str(executable), "--state", str(state), "serve"]
    assert token.encode() not in paths.plist.read_bytes()
    assert str(root).encode() not in paths.plist.read_bytes()
    assert stat.S_IMODE(paths.plist.stat().st_mode) == 0o600
    assert stat.S_IMODE(paths.log_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(paths.stdout_log.stat().st_mode) == 0o600
    assert stat.S_IMODE(paths.stderr_log.stat().st_mode) == 0o600
    assert json.loads((state / "node-config.json").read_bytes())["allowed_roots"] == before_roots

    stop_service(state, manager=manager)
    start_service(state, manager=manager)
    restart_service(state, manager=manager)
    assert any(call[1:3] == ["bootstrap", "gui/501"] for call in fake.calls)
    assert any(call[1:2] == ["bootout"] and call[2] == "gui/501/com.workspace-bridge.node"
               for call in fake.calls)
    # Deterministic lifecycle: bootstrap already runs the RunAtLoad agent,
    # so start-from-unloaded and restart must not issue a redundant kickstart.
    assert not any(call[1:4] == ["kickstart", "-k", "gui/501/com.workspace-bridge.node"]
                   for call in fake.calls)

    uninstall_service(state, manager=manager)
    assert not paths.plist.exists()
    assert (state / "node-config.json").read_bytes() == before_config
    assert (state / "node-token").read_text().strip() == token
    assert paths.stdout_log.exists()
    assert paths.stderr_log.exists()


def test_install_is_idempotent_for_the_same_managed_plist(tmp_path):
    state, _root, _token, home = _initialized_node(tmp_path)
    fake = FakeLaunchctl()
    manager = _manager(fake, home)
    executable = _fake_executable(tmp_path)

    install_service(state, manager=manager, executable=executable)
    call_count = len(fake.calls)
    second = install_service(state, manager=manager, executable=executable)

    assert second["installed"] is True
    assert len(fake.calls) == call_count + 2  # print before and after the no-op install
    assert not any(call[1] == "bootstrap" for call in fake.calls[call_count:])


def test_install_rejects_uninitialized_or_unsafe_state(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    fake = FakeLaunchctl()
    manager = _manager(fake, home)
    executable = _fake_executable(tmp_path)
    with pytest.raises(BridgeError) as missing:
        install_service(tmp_path / "missing", manager=manager,
                        executable=executable)
    assert missing.value.code == "node_uninitialized"

    state, _root, _token, _home = _initialized_node(tmp_path / "unsafe")
    os.chmod(state / "node-config.json", 0o644)
    with pytest.raises(BridgeError) as unsafe:
        install_service(state, manager=manager, executable=executable)
    assert unsafe.value.code == "node_state_unsafe"


def test_install_fails_closed_for_unmanaged_or_modified_plist(tmp_path):
    state, _root, _token, home = _initialized_node(tmp_path)
    fake = FakeLaunchctl()
    manager = _manager(fake, home)
    executable = _fake_executable(tmp_path)
    paths = service_paths(state, home=home)
    paths.plist.parent.mkdir(parents=True)
    paths.plist.write_bytes(b"unmanaged plist")
    os.chmod(paths.plist, 0o600)

    with pytest.raises(BridgeError) as conflict:
        install_service(state, manager=manager, executable=executable)
    assert conflict.value.code == "node_service_conflict"
    assert paths.plist.read_bytes() == b"unmanaged plist"


def test_modified_managed_plist_cannot_be_replaced_or_uninstalled(tmp_path):
    state, _root, _token, home = _initialized_node(tmp_path)
    fake = FakeLaunchctl()
    manager = _manager(fake, home)
    executable = _fake_executable(tmp_path)
    install_service(state, manager=manager, executable=executable)
    paths = service_paths(state, home=home)
    paths.plist.write_bytes(paths.plist.read_bytes() + b"\nmodified")
    os.chmod(paths.plist, 0o600)
    modified = paths.plist.read_bytes()

    with pytest.raises(BridgeError) as install_error:
        install_service(state, manager=manager, executable=executable)
    assert install_error.value.code == "node_service_conflict"
    with pytest.raises(BridgeError) as uninstall_error:
        uninstall_service(state, manager=manager)
    assert uninstall_error.value.code == "node_service_conflict"
    assert paths.plist.read_bytes() == modified


def test_install_validates_executable_before_creating_service_files(tmp_path):
    state, _root, _token, home = _initialized_node(tmp_path)
    fake = FakeLaunchctl()
    manager = _manager(fake, home)
    executable = tmp_path / "bin" / "missing-workspace-bridge-node"

    with pytest.raises(BridgeError) as exc:
        install_service(state, manager=manager, executable=executable)

    assert exc.value.code == "node_service_invalid_executable"
    paths = service_paths(state, home=home)
    assert not paths.log_dir.exists()
    assert not paths.plist.exists()
    assert not paths.manifest.exists()
    assert fake.calls == []

    non_executable = _fake_executable(tmp_path, "non-executable")
    non_executable.chmod(0o600)
    with pytest.raises(BridgeError) as non_executable_error:
        install_service(state, manager=manager, executable=non_executable)
    assert non_executable_error.value.code == "node_service_invalid_executable"
    assert fake.calls == []


def test_launch_agent_preserves_lexical_executable_symlink_path(tmp_path):
    target = _fake_executable(tmp_path, "target")
    link = tmp_path / "bin" / "workspace-bridge-node"
    link.symlink_to(target)

    assert launch_agent_program(link) == [str(link)]


def test_probe_uses_loopback_for_wildcard_listen_hosts(monkeypatch):
    calls = []

    def fake_urlopen(request, timeout):
        calls.append((request.full_url, timeout))
        return _StatusResponse({
            "status": "ok", "protocol": 1,
            "allowed_roots": [{"available": True, "root_label": "alpha"}],
        })

    monkeypatch.setattr("workspace_bridge.node_launchd.urllib.request.urlopen", fake_urlopen)
    for host, expected in (
            ("0.0.0.0", "http://127.0.0.1:8770/v1/status"),
            ("::", "http://[::1]:8770/v1/status"),
            ("[::]", "http://[::1]:8770/v1/status"),
            ("192.0.2.10", "http://192.0.2.10:8770/v1/status")):
        assert probe_node_status({"host": host, "port": 8770}, "node-token")["status"] == "healthy"
        assert calls[-1][0] == expected


def test_probe_summarizes_allowed_root_availability_without_paths(monkeypatch):
    payload = {
        "status": "ok", "protocol": 1,
        "allowed_roots": [
            {"available": True, "root_label": "alpha"},
            {"available": False, "root_label": "beta"},
        ],
    }
    monkeypatch.setattr(
        "workspace_bridge.node_launchd.urllib.request.urlopen",
        lambda _request, timeout: _StatusResponse(payload),
    )

    result = probe_node_status({"host": "127.0.0.1", "port": 8770}, "node-token")

    assert result["status"] == "healthy"
    assert result["root_status"] == {
        "status": "degraded",
        "total": 2,
        "available": 1,
        "unavailable": 1,
        "unavailable_labels": ["beta"],
    }
    assert "/" not in json.dumps(result["root_status"])


@pytest.mark.parametrize(
    "allowed_roots, expected_code",
    [
        ([{"available": "yes", "root_label": "alpha"}], "root_metadata_invalid"),
        ([{"available": False, "root_label": "/Volumes/data2"}], "root_metadata_invalid"),
        (None, "root_metadata_missing"),
    ],
)
def test_probe_distinguishes_invalid_or_missing_root_metadata(
        monkeypatch, allowed_roots, expected_code):
    payload = {"status": "ok", "protocol": 1}
    if allowed_roots is not None:
        payload["allowed_roots"] = allowed_roots
    monkeypatch.setattr(
        "workspace_bridge.node_launchd.urllib.request.urlopen",
        lambda _request, timeout: _StatusResponse(payload),
    )

    result = probe_node_status({"host": "127.0.0.1", "port": 8770}, "node-token")

    assert result["status"] == "healthy"
    assert result["root_status"] == {"status": "unknown", "code": expected_code}


def test_status_reports_not_installed_without_mutating_and_unsupported_os_is_stable(tmp_path):
    state, _root, _token, home = _initialized_node(tmp_path)
    fake = FakeLaunchctl()
    manager = _manager(fake, home)
    status = service_status(state, manager=manager, probe=False)
    assert status["installed"] is False
    assert status["state"] == "not_installed"
    assert status["health"]["status"] == "not_probed"
    assert fake.calls == [["launchctl", "print", "gui/501/com.workspace-bridge.node"]]

    unsupported = LaunchdManager(platform_name="Linux", home=home, uid=501)
    with pytest.raises(BridgeError) as exc:
        service_status(state, manager=unsupported, probe=False)
    assert exc.value.code == "node_service_unsupported_platform"
    assert "macOS launchd" in str(exc.value)


def test_launchctl_failure_detail_is_bounded_and_redacted():
    manager = LaunchdManager(platform_name="Darwin")
    with pytest.raises(BridgeError) as exc:
        manager._expect_success(
            "bootstrap",
            LaunchctlResult(1, stderr="token=super-secret-value " + "x" * 1000),
        )
    assert "super-secret-value" not in str(exc.value)
    assert len(str(exc.value)) < 300


def test_transition_budget_is_bounded():
    from workspace_bridge import node_launchd as _mod
    assert _mod._LAUNCHD_TRANSITION_ATTEMPTS == 50
    assert abs(_mod._LAUNCHD_TRANSITION_INTERVAL - 0.1) < 1e-9
    total = _mod._LAUNCHD_TRANSITION_ATTEMPTS * _mod._LAUNCHD_TRANSITION_INTERVAL
    assert 4.0 <= total <= 6.0
    src = Path("workspace_bridge/node_launchd.py").read_text()
    # Polling must be read-only launchctl print; no retry loops that re-issue mutations.
    assert src.count('_launchctl_transition(manager, paths.state, "bootout"') >= 2
    assert src.count('_launchctl_transition(manager, paths.state, "bootstrap"') >= 2
    # Lifecycle entrypoints must verify via the transition helper: no direct
    # un-verified bootstrap/kickstart on the managed path. Direct bootout
    # remains only in uninstall (out of scope for this correction).
    assert "manager.bootstrap(paths.state)" not in src
    assert "manager.kickstart(paths.state)" not in src
    assert src.count("manager.bootout(paths.state)") == 1


def test_stop_waits_for_delayed_bootout_visibility(tmp_path, monkeypatch):
    state, _root, token, home = _initialized_node(tmp_path)
    executable = _fake_executable(tmp_path)

    class _DelayedBootoutFake(FakeLaunchctl):
        def __init__(self):
            super().__init__()
            self.delay = 0
            self._pending = 0
            self._unloading = False

        def __call__(self, command, **kwargs):
            action = command[1]
            if action == "bootout":
                self.calls.append(list(command))
                if self.delay > 0:
                    self._pending = self.delay
                    self._unloading = True
                    self.loaded = True
                    self.running = True
                    return subprocess.CompletedProcess(command, 0, "", "")
                self.loaded = False
                self.running = False
                return subprocess.CompletedProcess(command, 0, "", "")
            if action == "print":
                self.calls.append(list(command))
                if self._pending > 0:
                    self._pending -= 1
                    return subprocess.CompletedProcess(
                        command, 0, "state = running\npid = 4321\n", "")
                if self._unloading:
                    self._unloading = False
                    self._pending = 0
                    self.loaded = False
                    self.running = False
                    return subprocess.CompletedProcess(command, 3, "", "service not found")
                if not self.loaded:
                    return subprocess.CompletedProcess(command, 3, "", "service not found")
                st = "running" if self.running else "waiting"
                return subprocess.CompletedProcess(
                    command, 0, f"state = {st}\npid = 4321\n", "")
            return super().__call__(command, **kwargs)

    fake = _DelayedBootoutFake()
    manager = _manager(fake, home)
    install_service(state, manager=manager, executable=executable)
    # Require unload to be visible: keep exact target reporting loaded twice after bootout.
    fake.delay = 2
    sleeps: list[float] = []
    monkeypatch.setattr("workspace_bridge.node_launchd.time.sleep",
                        lambda s: sleeps.append(s))
    result = stop_service(state, manager=manager)
    assert result["action"] == "stop"
    assert result["state"] == "not_loaded"
    bootouts = [c for c in fake.calls if c[1] == "bootout"]
    assert len(bootouts) == 1, "stop must issue exactly one bootout"
    assert len(sleeps) >= 2, "stop must poll until delayed unload becomes visible"
    assert len(sleeps) <= 50
    assert all(s == 0.1 for s in sleeps)
    payload = json.dumps(result, sort_keys=True)
    assert token not in payload
    assert "node-token" not in payload


def test_start_bootstrap_exit5_recovers_when_running(tmp_path, monkeypatch):
    state, _root, token, home = _initialized_node(tmp_path)
    executable = _fake_executable(tmp_path)

    class _FlakyBootstrapFake(FakeLaunchctl):
        def __init__(self):
            super().__init__()
            self.mode = "normal"

        def __call__(self, command, **kwargs):
            if command[1] == "bootstrap" and self.mode != "normal":
                self.calls.append(list(command))
                if self.mode == "recover":
                    self.loaded = True
                    self.running = True
                    return subprocess.CompletedProcess(
                        command, 5, "", "Bootstrap failed: 5: Input/output error")
                return subprocess.CompletedProcess(
                    command, 5, "", "Bootstrap failed: 5: Input/output error")
            return super().__call__(command, **kwargs)

    fake = _FlakyBootstrapFake()
    manager = _manager(fake, home)
    install_service(state, manager=manager, executable=executable)
    stop_service(state, manager=manager)
    fake.calls.clear()
    fake.mode = "recover"
    sleeps: list[float] = []
    monkeypatch.setattr("workspace_bridge.node_launchd.time.sleep",
                        lambda s: sleeps.append(s))
    result = start_service(state, manager=manager)
    assert result["state"] in {"running", "loaded"}
    bootstraps = [c for c in fake.calls if c[1] == "bootstrap"]
    kickstarts = [c for c in fake.calls if c[1] == "kickstart"]
    assert len(bootstraps) == 1, "start-from-unloaded must bootstrap exactly once"
    assert kickstarts == [], "no redundant kickstart after verified bootstrap"
    assert len(sleeps) <= 50
    payload = json.dumps(result, sort_keys=True)
    assert token not in payload
    assert "node-token" not in payload


def test_start_kickstart_return_failure_recovers_when_running(tmp_path, monkeypatch):
    """Live false-negative: kickstart fails but the exact target is running."""
    state, _root, token, home = _initialized_node(tmp_path)
    executable = _fake_executable(tmp_path)

    class _FlakyKickstartFake(FakeLaunchctl):
        def __init__(self):
            super().__init__()
            self.flaky = False

        def __call__(self, command, **kwargs):
            if command[1] == "kickstart" and self.flaky:
                self.calls.append(list(command))
                self.loaded = True
                self.running = True
                return subprocess.CompletedProcess(
                    command, 1, "", "kickstart failed: unavailable")
            return super().__call__(command, **kwargs)

    fake = _FlakyKickstartFake()
    manager = _manager(fake, home)
    install_service(state, manager=manager, executable=executable)
    # Simulate already-loaded but not running (waiting, not running).
    fake.loaded = True
    fake.running = False
    fake.calls.clear()
    fake.flaky = True
    sleeps: list[float] = []
    monkeypatch.setattr("workspace_bridge.node_launchd.time.sleep",
                        lambda s: sleeps.append(s))
    result = start_service(state, manager=manager)
    assert result["state"] in {"running", "loaded"}
    bootstraps = [c for c in fake.calls if c[1] == "bootstrap"]
    kickstarts = [c for c in fake.calls if c[1] == "kickstart"]
    assert bootstraps == [], "already-loaded start must not bootstrap"
    assert len(kickstarts) == 1, "already-loaded/non-running must kickstart exactly once"
    assert len(sleeps) <= 50
    payload = json.dumps(result, sort_keys=True)
    assert token not in payload
    assert "node-token" not in payload


def test_start_kickstart_oserror_recovers_when_running(tmp_path, monkeypatch):
    state, _root, token, home = _initialized_node(tmp_path)
    executable = _fake_executable(tmp_path)

    class _OSErrorKickstartFake(FakeLaunchctl):
        def __init__(self):
            super().__init__()
            self.flaky = False

        def __call__(self, command, **kwargs):
            if command[1] == "kickstart" and self.flaky:
                self.calls.append(list(command))
                self.loaded = True
                self.running = True
                raise OSError("injected transient kickstart unavailable")
            return super().__call__(command, **kwargs)

    fake = _OSErrorKickstartFake()
    manager = _manager(fake, home)
    install_service(state, manager=manager, executable=executable)
    fake.loaded = True
    fake.running = False
    fake.calls.clear()
    fake.flaky = True
    sleeps: list[float] = []
    monkeypatch.setattr("workspace_bridge.node_launchd.time.sleep",
                        lambda s: sleeps.append(s))
    result = start_service(state, manager=manager)
    assert result["state"] in {"running", "loaded"}
    assert len([c for c in fake.calls if c[1] == "kickstart"]) == 1
    assert [c for c in fake.calls if c[1] == "bootstrap"] == []
    payload = json.dumps(result, sort_keys=True)
    assert token not in payload
    assert "node-token" not in payload


def test_bootstrap_genuine_failure_raises_original_error(tmp_path, monkeypatch):
    state, _root, token, home = _initialized_node(tmp_path)
    executable = _fake_executable(tmp_path)

    class _GenuineFailFake(FakeLaunchctl):
        def __init__(self):
            super().__init__()
            self.fail = False

        def __call__(self, command, **kwargs):
            if command[1] == "bootstrap" and self.fail:
                self.calls.append(list(command))
                return subprocess.CompletedProcess(
                    command, 5, "", "Bootstrap failed: 5: Input/output error")
            return super().__call__(command, **kwargs)

    fake = _GenuineFailFake()
    manager = _manager(fake, home)
    install_service(state, manager=manager, executable=executable)
    stop_service(state, manager=manager)
    fake.calls.clear()
    fake.fail = True
    sleeps: list[float] = []
    monkeypatch.setattr("workspace_bridge.node_launchd.time.sleep",
                        lambda s: sleeps.append(s))
    with pytest.raises(BridgeError) as exc:
        start_service(state, manager=manager)
    assert exc.value.code == "node_service_launchctl_failed"
    # Original launchctl detail must propagate (exit 5), not be swallowed.
    assert "5" in str(exc.value)
    assert token not in str(exc.value)
    assert str(state) not in str(exc.value)
    bootstraps = [c for c in fake.calls if c[1] == "bootstrap"]
    kickstarts = [c for c in fake.calls if c[1] == "kickstart"]
    assert len(bootstraps) == 1
    assert kickstarts == []
    # Bounded wait: polling is finite and read-only.
    assert 1 <= len(sleeps) <= 50
    assert all(s == 0.1 for s in sleeps)


def test_start_and_restart_have_no_redundant_kickstart(tmp_path, monkeypatch):
    state, _root, token, home = _initialized_node(tmp_path)
    executable = _fake_executable(tmp_path)
    fake = FakeLaunchctl()
    manager = _manager(fake, home)
    install_service(state, manager=manager, executable=executable)
    stop_service(state, manager=manager)
    fake.calls.clear()
    sleeps: list[float] = []
    monkeypatch.setattr("workspace_bridge.node_launchd.time.sleep",
                        lambda s: sleeps.append(s))
    started = start_service(state, manager=manager)
    assert started["state"] in {"running", "loaded"}
    assert len([c for c in fake.calls if c[1] == "bootstrap"]) == 1
    assert [c for c in fake.calls if c[1] == "kickstart"] == []
    fake.calls.clear()
    restarted = restart_service(state, manager=manager)
    assert restarted["state"] in {"running", "loaded"}
    assert len([c for c in fake.calls if c[1] == "bootout"]) == 1
    assert len([c for c in fake.calls if c[1] == "bootstrap"]) == 1
    assert [c for c in fake.calls if c[1] == "kickstart"] == []
    for result in (started, restarted):
        payload = json.dumps(result, sort_keys=True)
        assert token not in payload
        assert "node-token" not in payload


def test_install_bootstrap_exit5_recovers_when_running(tmp_path, monkeypatch):
    state, _root, token, home = _initialized_node(tmp_path)
    executable = _fake_executable(tmp_path)

    class _InstallFlakyFake(FakeLaunchctl):
        def __call__(self, command, **kwargs):
            if command[1] == "bootstrap":
                self.calls.append(list(command))
                self.loaded = True
                self.running = True
                return subprocess.CompletedProcess(
                    command, 5, "", "Bootstrap failed: 5: Input/output error")
            return super().__call__(command, **kwargs)

    fake = _InstallFlakyFake()
    manager = _manager(fake, home)
    sleeps: list[float] = []
    monkeypatch.setattr("workspace_bridge.node_launchd.time.sleep",
                        lambda s: sleeps.append(s))
    result = install_service(state, manager=manager, executable=executable)
    assert result["installed"] is True
    assert result["state"] in {"running", "loaded"}
    bootstraps = [c for c in fake.calls if c[1] == "bootstrap"]
    assert len(bootstraps) == 1, "fresh install must bootstrap exactly once"
    assert [c for c in fake.calls if c[1] == "kickstart"] == []
    assert len(sleeps) <= 50
    payload = json.dumps(result, sort_keys=True)
    assert token not in payload
    assert "node-token" not in payload


def test_install_bootstrap_genuine_failure_raises_original(tmp_path, monkeypatch):
    state, _root, token, home = _initialized_node(tmp_path)
    executable = _fake_executable(tmp_path)

    class _InstallGenuineFailFake(FakeLaunchctl):
        def __call__(self, command, **kwargs):
            if command[1] == "bootstrap":
                self.calls.append(list(command))
                return subprocess.CompletedProcess(
                    command, 5, "", "Bootstrap failed: 5: Input/output error")
            return super().__call__(command, **kwargs)

    fake = _InstallGenuineFailFake()
    manager = _manager(fake, home)
    sleeps: list[float] = []
    monkeypatch.setattr("workspace_bridge.node_launchd.time.sleep",
                        lambda s: sleeps.append(s))
    with pytest.raises(BridgeError) as exc:
        install_service(state, manager=manager, executable=executable)
    assert exc.value.code == "node_service_launchctl_failed"
    assert "5" in str(exc.value)
    assert token not in str(exc.value)
    assert str(state) not in str(exc.value)
    bootstraps = [c for c in fake.calls if c[1] == "bootstrap"]
    assert len(bootstraps) == 1
    assert [c for c in fake.calls if c[1] == "kickstart"] == []
    assert 1 <= len(sleeps) <= 50
    assert all(s == 0.1 for s in sleeps)
    # Polling is read-only: only print calls besides the single bootstrap.
    others = [c for c in fake.calls if c[1] not in ("bootstrap", "print")]
    assert others == []


def test_install_idempotent_when_loaded_non_running_performs_no_mutation(tmp_path, monkeypatch):
    state, _root, token, home = _initialized_node(tmp_path)
    executable = _fake_executable(tmp_path)
    fake = FakeLaunchctl()
    manager = _manager(fake, home)
    first = install_service(state, manager=manager, executable=executable)
    assert first["installed"] is True
    # Simulate already-loaded but non-running; install must not mutate.
    fake.loaded = True
    fake.running = False
    fake.calls.clear()
    sleeps: list[float] = []
    monkeypatch.setattr("workspace_bridge.node_launchd.time.sleep",
                        lambda s: sleeps.append(s))
    again = install_service(state, manager=manager, executable=executable)
    assert again["installed"] is True
    assert again["action"] == "install"
    assert fake.calls and all(c[1] == "print" for c in fake.calls), fake.calls
    assert [c for c in fake.calls if c[1] in ("bootstrap", "kickstart", "bootout")] == []
    assert sleeps == []
    payload = json.dumps(again, sort_keys=True)
    assert token not in payload
    assert "node-token" not in payload
