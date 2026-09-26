"""macOS per-instance adapter LaunchAgent lifecycle (fake launchctl)."""
from __future__ import annotations

import json
from pathlib import Path
import plistlib
import stat
import subprocess

import pytest

from workspace_bridge.adapter_service import initialize_adapter
from workspace_bridge.adapter_launchd import (
    LaunchdManager,
    build_launch_agent_plist,
    install_service,
    launch_agent_program,
    render_launch_agent_plist,
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
        self.loaded: dict[str, bool] = {}
        self.running: dict[str, bool] = {}

    def __call__(self, command, **_kwargs):
        self.calls.append(list(command))
        action = command[1]
        target = command[-1] if len(command) > 2 else ""
        if action == "print":
            if not self.loaded.get(target, False):
                return subprocess.CompletedProcess(command, 3, "", "service not found")
            state = "running" if self.running.get(target, False) else "waiting"
            return subprocess.CompletedProcess(
                command, 0, f"state = {state}\npid = 4321\n", "")
        if action == "bootstrap":
            plist = command[-1]
            # Find the label by reading the plist file.
            try:
                label = plistlib.loads(Path(plist).read_bytes())["Label"]
            except Exception:
                label = target
            domain = command[2]
            full = f"{domain}/{label}"
            self.loaded[full] = True
            self.running[full] = True
            return subprocess.CompletedProcess(command, 0, "", "")
        if action == "bootout":
            self.loaded[target] = False
            self.running[target] = False
            return subprocess.CompletedProcess(command, 0, "", "")
        if action == "kickstart":
            tgt = command[-1]
            self.loaded[tgt] = True
            self.running[tgt] = True
            return subprocess.CompletedProcess(command, 0, "", "")
        raise AssertionError(command)


def _initialized_adapter(tmp_path: Path, *, runtime: str = "pi", port: int = 18980):
    projects = tmp_path / "projects" / "alpha"
    projects.mkdir(parents=True, exist_ok=True)
    exe = tmp_path / "bin" / "workspace-bridge-pi-adapter"
    if runtime == "codex":
        exe = tmp_path / "bin" / "workspace-bridge-codex-adapter"
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_text("#!/bin/sh\nexit 0\n")
    exe.chmod(0o700)
    state = tmp_path / f"adapter-{runtime}-{port}"
    initialize_adapter(state, runtime_type=runtime,
                       projects_root=str(projects), port=port,
                       executable=str(exe))
    token = (state / "runtime-token").read_text().strip()
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    return state, projects, token, home, exe


def _manager(fake: FakeLaunchctl, home: Path) -> LaunchdManager:
    return LaunchdManager(runner=fake, platform_name="Darwin", home=home, uid=501)


def _fake_launcher(tmp_path: Path, name: str = "workspace-bridge") -> Path:
    exe = tmp_path / "bin" / name
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_text("#!/bin/sh\nexit 0\n")
    exe.chmod(0o700)
    return exe


class _StatusResponse:
    def __init__(self, payload: dict):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self, _limit):
        return json.dumps(self.payload).encode()


def test_plist_is_deterministic_and_secret_free(tmp_path, monkeypatch):
    state, _projects, _token, home, _exe = _initialized_adapter(tmp_path, port=18981)
    from workspace_bridge.adapter_service import load_adapter_config

    config = load_adapter_config(state, require_roots=False)
    label = f"com.workspace-bridge.adapter.pi.{config['service_id']}"
    launcher = _fake_launcher(tmp_path)
    monkeypatch.setattr("workspace_bridge.adapter_launchd.launch_agent_program",
                        lambda executable=None: [str(launcher)])
    first = build_launch_agent_plist(state, [str(launcher)], label, home=home)
    second = render_launch_agent_plist(state, [str(launcher)], label, home=home)
    assert first == second
    payload = plistlib.loads(first)
    assert payload["Label"] == label
    assert payload["ProgramArguments"] == [
        str(launcher), "adapter", "--state", str(state), "serve"]
    assert payload["RunAtLoad"] is True
    assert payload["KeepAlive"] == {"SuccessfulExit": False}
    assert payload["ThrottleInterval"] == 10
    assert payload["ProcessType"] == "Background"
    assert payload["StandardOutPath"].endswith("/logs/stdout.log")
    assert payload["StandardErrorPath"].endswith("/logs/stderr.log")
    assert b"runtime-token" not in first
    assert _token.encode() not in first
    assert str(_projects).encode() not in first


def test_install_bootstrap_and_lifecycle_preserve_state(tmp_path, monkeypatch):
    state, projects, token, home, _exe = _initialized_adapter(tmp_path, port=18982)
    fake = FakeLaunchctl()
    manager = _manager(fake, home)
    launcher = _fake_launcher(tmp_path)
    monkeypatch.setattr("workspace_bridge.adapter_launchd.launch_agent_program",
                        lambda executable=None: [str(launcher)])
    before = (state / "config.json").read_bytes()
    installed = install_service(state, manager=manager)
    paths = service_paths(state, home=home)
    payload = plistlib.loads(paths.plist.read_bytes())
    assert installed["installed"] is True
    assert installed["state"] in {"running", "loaded"}
    assert installed["listen"] == {"port": 18982,
                                   "endpoint": "http://127.0.0.1:18982"}
    assert installed["runtime_type"] == "pi"
    assert payload["ProgramArguments"] == [
        str(launcher), "adapter", "--state", str(state), "serve"]
    assert token.encode() not in paths.plist.read_bytes()
    assert str(projects).encode() not in paths.plist.read_bytes()
    assert stat.S_IMODE(paths.plist.stat().st_mode) == 0o600
    assert stat.S_IMODE(paths.log_dir.stat().st_mode) == 0o700
    assert (state / "config.json").read_bytes() == before
    assert (state / "runtime-token").read_text().strip() == token

    stop_service(state, manager=manager)
    start_service(state, manager=manager)
    restart_service(state, manager=manager)
    assert any(call[1:3] == ["bootstrap", "gui/501"] for call in fake.calls)
    assert any(call[1] == "bootout" for call in fake.calls)
    assert any(call[1:3] == ["kickstart", "-k"] for call in fake.calls)

    # Install is idempotent for exact managed content.
    again = install_service(state, manager=manager)
    assert again["installed"] is True

    # Uninstall preserves adapter state.
    uninstall_service(state, manager=manager)
    assert not paths.plist.exists()
    assert (state / "config.json").exists()
    assert (state / "runtime-token").exists()
    assert (state / "runtime").exists()


def test_multi_instance_label_isolation(tmp_path, monkeypatch):
    first, _, _, home, _ = _initialized_adapter(tmp_path, runtime="pi", port=18983)
    # Second instance needs a distinct state dir (avoid collision on port/state).
    projects2 = tmp_path / "projects" / "beta"
    projects2.mkdir(parents=True, exist_ok=True)
    exe = tmp_path / "bin" / "workspace-bridge-pi-adapter"
    from workspace_bridge.adapter_service import initialize_adapter as _init

    second = tmp_path / "adapter-pi-second"
    _init(second, runtime_type="pi", projects_root=str(projects2),
          port=18984, executable=str(exe))
    launcher = _fake_launcher(tmp_path)
    monkeypatch.setattr("workspace_bridge.adapter_launchd.launch_agent_program",
                        lambda executable=None: [str(launcher)])
    fake = FakeLaunchctl()
    manager = _manager(fake, home)
    install_service(first, manager=manager)
    install_service(second, manager=manager)
    first_paths = service_paths(first, home=home)
    second_paths = service_paths(second, home=home)
    assert first_paths.label != second_paths.label
    assert first_paths.plist != second_paths.plist
    assert first_paths.label.startswith("com.workspace-bridge.adapter.pi.")
    assert second_paths.label.startswith("com.workspace-bridge.adapter.pi.")
    # Codex uses a distinct namespace.
    codex_state, _, _, _, _ = _initialized_adapter(tmp_path, runtime="codex", port=18985)
    install_service(codex_state, manager=manager)
    codex_paths = service_paths(codex_state, home=home)
    assert codex_paths.label.startswith("com.workspace-bridge.adapter.codex.")


def test_status_is_secret_safe_and_combines_health(tmp_path, monkeypatch):
    state, _, token, home, _ = _initialized_adapter(tmp_path, port=18986)
    fake = FakeLaunchctl()
    manager = _manager(fake, home)
    launcher = _fake_launcher(tmp_path)
    monkeypatch.setattr("workspace_bridge.adapter_launchd.launch_agent_program",
                        lambda executable=None: [str(launcher)])
    install_service(state, manager=manager)
    good = {"protocol": {"major": 1, "minor": 0},
            "runtime": {"id": "pi", "adapterVersion": "0.1.0",
                        "nativeVersion": "0.87.0", "instanceId": "pi_instance_x"},
            "features": {"models": 1}}
    monkeypatch.setattr("urllib.request.urlopen",
                        lambda _req, timeout: _StatusResponse(good))
    status = service_status(state, manager=manager, probe=True)
    assert status["health"]["status"] == "healthy"
    assert status["health"]["adapter_version"] == "0.1.0"
    text = json.dumps(status)
    assert token not in text
    assert "runtime-token" not in text
    assert "projects" not in text.lower() or "projects_root" not in text
    assert "executable" not in text
    assert "HOME" not in text
    # Degraded when the process runs but the descriptor is unavailable.
    import urllib.error

    def _fail(_req, timeout):
        raise urllib.error.URLError("refused")

    monkeypatch.setattr("urllib.request.urlopen", _fail)
    degraded = service_status(state, manager=manager, probe=True)
    assert degraded["state"] == "degraded"
    assert degraded["health"]["status"] in {"degraded", "unavailable"}


def test_status_results_are_path_free_and_secret_safe(tmp_path, monkeypatch):
    state, projects, token, home, exe = _initialized_adapter(tmp_path, port=18989)
    fake = FakeLaunchctl()
    manager = _manager(fake, home)
    launcher = _fake_launcher(tmp_path)
    monkeypatch.setattr("workspace_bridge.adapter_launchd.launch_agent_program",
                        lambda executable=None: [str(launcher)])
    paths = service_paths(state, home=home)
    install_service(state, manager=manager)
    good = {"protocol": {"major": 1, "minor": 0},
            "runtime": {"id": "pi", "adapterVersion": "0.1.0",
                        "nativeVersion": "0.87.0", "instanceId": "pi_instance_z"},
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
    # Missing/uninitialized state derives no label; use a probe=False action
    # result plus an uninstalled-state result for the same contract.
    fresh = tmp_path / "fresh-missing-probe"
    from workspace_bridge.adapter_service import initialize_adapter as _init2
    _projects2 = tmp_path / "projects" / "beta2"
    _projects2.mkdir(parents=True, exist_ok=True)
    _init2(fresh, runtime_type="pi", projects_root=str(_projects2),
           port=19020, executable=str(exe))
    results.append(service_status(fresh, manager=manager, probe=False))
    for result in results:
        payload = json.dumps(result, sort_keys=True)
        for secret in (str(state), str(projects), str(exe), str(launcher),
                       str(home), str(paths.plist), str(paths.stdout_log),
                       str(paths.stderr_log), str(paths.log_dir),
                       token, "runtime-token"):
            assert secret not in payload, secret
        for key in ("state_path", "plist_path", "stdout_path",
                    "stderr_path", "unit_path", "log_path"):
            assert key not in result, key
        assert "projects_root" not in payload
        assert "executable" not in payload
    # Action results inherit the same contract.
    actioned = start_service(state, manager=manager)
    payload = json.dumps(actioned, sort_keys=True)
    for secret in (str(state), str(projects), str(exe), str(launcher),
                   str(home), token, "runtime-token"):
        assert secret not in payload, secret
    for key in ("state_path", "plist_path", "stdout_path", "stderr_path"):
        assert key not in actioned, key
    # Truly missing state is also path-free.
    missing = tmp_path / "definitely-missing"
    absent = service_status(missing, manager=manager, probe=False)
    payload = json.dumps(absent, sort_keys=True)
    for secret in (str(missing), str(state), token):
        assert secret not in payload, secret
    for key in ("state_path", "plist_path", "stdout_path", "stderr_path",
                "unit_path", "log_path"):
        assert key not in absent, key


def test_uninstall_removes_plist_and_manifest_and_preserves_state(tmp_path, monkeypatch):
    state, _, token, home, _ = _initialized_adapter(tmp_path, port=18990)
    fake = FakeLaunchctl()
    manager = _manager(fake, home)
    launcher = _fake_launcher(tmp_path)
    monkeypatch.setattr("workspace_bridge.adapter_launchd.launch_agent_program",
                        lambda executable=None: [str(launcher)])
    install_service(state, manager=manager)
    paths = service_paths(state, home=home)
    assert paths.plist.exists()
    assert paths.manifest.exists()
    config_before = (state / "config.json").read_bytes()
    token_before = (state / "runtime-token").read_bytes()
    uninstall_service(state, manager=manager)
    assert not paths.plist.exists()
    assert not paths.manifest.exists()
    assert (state / "config.json").read_bytes() == config_before
    assert (state / "runtime-token").read_bytes() == token_before
    assert (state / "runtime").is_dir()
    assert (state / "logs").is_dir()
    assert paths.stdout_log.exists()
    assert paths.stderr_log.exists()
    # Both absent is a no-op.
    again = uninstall_service(state, manager=manager)
    assert again["installed"] is False


def test_uninstall_manifest_only_retry_completes(tmp_path, monkeypatch):
    state, _, _, home, _ = _initialized_adapter(tmp_path, port=18991)
    fake = FakeLaunchctl()
    manager = _manager(fake, home)
    launcher = _fake_launcher(tmp_path)
    monkeypatch.setattr("workspace_bridge.adapter_launchd.launch_agent_program",
                        lambda executable=None: [str(launcher)])
    install_service(state, manager=manager)
    paths = service_paths(state, home=home)
    # Simulate a prior uninstall that removed the plist but failed before
    # manifest removal.
    paths.plist.unlink()
    assert paths.manifest.exists()
    result = uninstall_service(state, manager=manager)
    assert not paths.manifest.exists()
    assert result["installed"] is False
    assert (state / "config.json").exists()


def test_uninstall_manifest_only_foreign_fails_closed(tmp_path, monkeypatch):
    state, _, _, home, _ = _initialized_adapter(tmp_path, port=18992)
    fake = FakeLaunchctl()
    manager = _manager(fake, home)
    launcher = _fake_launcher(tmp_path)
    monkeypatch.setattr("workspace_bridge.adapter_launchd.launch_agent_program",
                        lambda executable=None: [str(launcher)])
    install_service(state, manager=manager)
    paths = service_paths(state, home=home)
    paths.plist.unlink()
    # Corrupt the manifest so it no longer matches this exact identity.
    raw = json.loads(paths.manifest.read_text())
    raw["service_id"] = "ffffffffffff"
    paths.manifest.write_text(json.dumps(raw))
    import os as _os
    _os.chmod(paths.manifest, 0o600)
    with pytest.raises(BridgeError) as exc:
        uninstall_service(state, manager=manager)
    assert exc.value.code == "adapter_service_conflict"
    assert paths.manifest.exists()


def test_uninstall_manifest_removal_failure_is_retryable(tmp_path, monkeypatch):
    state, _, _, home, _ = _initialized_adapter(tmp_path, port=18993)
    fake = FakeLaunchctl()
    manager = _manager(fake, home)
    launcher = _fake_launcher(tmp_path)
    monkeypatch.setattr("workspace_bridge.adapter_launchd.launch_agent_program",
                        lambda executable=None: [str(launcher)])
    install_service(state, manager=manager)
    paths = service_paths(state, home=home)
    real_unlink = Path.unlink

    def _fail_manifest(self, *args, **kwargs):
        if self == paths.manifest:
            raise OSError("injected manifest failure")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", _fail_manifest)
    with pytest.raises(BridgeError):
        uninstall_service(state, manager=manager)
    assert not paths.plist.exists()
    assert paths.manifest.exists()
    monkeypatch.setattr(Path, "unlink", real_unlink)
    retry = uninstall_service(state, manager=manager)
    assert not paths.manifest.exists()
    assert retry["installed"] is False


def test_status_not_installed_and_unsupported_platform(tmp_path):
    state, _, _, home, _ = _initialized_adapter(tmp_path, port=18987)
    fake = FakeLaunchctl()
    manager = _manager(fake, home)
    status = service_status(state, manager=manager, probe=False)
    assert status["installed"] is False
    assert status["state"] == "not_installed"
    unsupported = LaunchdManager(platform_name="Linux", home=home, uid=501)
    with pytest.raises(BridgeError) as exc:
        service_status(state, manager=unsupported, probe=False)
    assert exc.value.code == "adapter_service_unsupported_platform"


def test_conflict_closed_and_unmanaged_rejected(tmp_path, monkeypatch):
    state, _, _, home, _ = _initialized_adapter(tmp_path, port=18988)
    fake = FakeLaunchctl()
    manager = _manager(fake, home)
    launcher = _fake_launcher(tmp_path)
    monkeypatch.setattr("workspace_bridge.adapter_launchd.launch_agent_program",
                        lambda executable=None: [str(launcher)])
    install_service(state, manager=manager)
    paths = service_paths(state, home=home)
    # Corrupt the plist: start must refuse an unmanaged artifact.
    paths.plist.write_bytes(b"corrupted")
    import os as _os

    _os.chmod(paths.plist, 0o600)
    with pytest.raises(BridgeError) as exc:
        start_service(state, manager=manager)
    assert exc.value.code == "adapter_service_conflict"


def test_no_shell_or_caller_labels():
    src = Path("workspace_bridge/adapter_launchd.py").read_text()
    assert "shell=True" not in src
    assert "os.system(" not in src
    cli = Path("workspace_bridge/adapter_cli.py").read_text()
    assert "--label" not in cli
    assert "--service-name" not in cli
