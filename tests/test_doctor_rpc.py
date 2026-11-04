"""CLI diagnostics observe the owning service, never a second adapter owner."""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace

import pytest

from workspace_bridge import cli
from workspace_bridge.diagnostics import failure_report
from workspace_bridge.doctor_rpc import LocalDiagnostics, REQUEST, SOCKET_NAME, read_report


@pytest.fixture
def private_state():
    # AF_UNIX paths on macOS are limited to 104 bytes; pytest's default
    # temporary paths can exceed that before the socket name is appended.
    with tempfile.TemporaryDirectory(prefix="wb-doctor-", dir="/tmp") as folder:
        yield Path(folder).resolve()


@contextmanager
def endpoint(state, callback):
    service = SimpleNamespace(state=state, diagnostic_report=callback)
    server = LocalDiagnostics(service, {"admin_port": 8766})
    assert server.start()
    try:
        yield server
    finally:
        server.close()


@pytest.mark.parametrize("status", ["pass", "warning", "unknown", "action_required", "failed"])
def test_cli_completed_live_report_exits_zero_for_all_findings(private_state, monkeypatch, capsys, status):
    cli.initialize(private_state / "state", 8765, 8766)
    state = private_state / "state"
    report = failure_report(mode="live", summary="An observed adapter failed.")
    report["overall"]["status"] = status
    calls = []

    def observe(**kwargs):
        calls.append(kwargs)
        return report

    def no_standalone_service(*args, **kwargs):
        pytest.fail("CLI must reuse the live service instead of creating another Service")

    monkeypatch.setattr(cli, "Service", no_standalone_service)
    with endpoint(state, observe):
        assert cli._doctor(state, offline=False, as_json=True) == 0
    assert json.loads(capsys.readouterr().out) == report
    assert calls == [{"listener": {"admin_port": 8766}}]
    assert not (state / SOCKET_NAME).exists()


def test_completed_standalone_report_with_findings_exits_zero(private_state, capsys):
    state = private_state / "state"
    config = cli.initialize(state, 8765, 8766)
    service = cli.Service(state, config, run_coordinator_background=False)
    service.close()
    # The gateway is unconfigured: a real finding, not a failed invocation.
    assert cli._doctor(state, offline=True, as_json=True) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["overall"]["status"] == "action_required"


def test_doctor_initialization_and_evaluation_errors_remain_nonzero(private_state, monkeypatch, capsys):
    state = private_state / "missing"
    assert cli._doctor(state, offline=True, as_json=True) == 1
    assert json.loads(capsys.readouterr().out)["overall"]["status"] == "failed"
    config = cli.initialize(state, 8765, 8766)
    service = cli.Service(state, config, run_coordinator_background=False)
    service.close()

    def broken(*args, **kwargs):
        raise RuntimeError("private provider payload /secret/path")

    monkeypatch.setattr(cli.Service, "diagnostic_report", broken)
    assert cli._doctor(state, offline=True, as_json=True) == 1
    text = capsys.readouterr().out
    report = json.loads(text)
    assert report["checks"][0]["code"] == "doctor.evaluation_failed"
    assert "private provider" not in text and "/secret/path" not in text


@pytest.mark.parametrize("offline", [False, True])
def test_standalone_fallback_is_valid_json_and_offline_never_connects(
        private_state, monkeypatch, capsys, offline):
    state = private_state / "state"
    config = cli.initialize(state, 8765, 8766)
    service = cli.Service(state, config, run_coordinator_background=False)
    service.manage_bridge("rotate_token")
    service.close()
    if offline:
        def forbidden(*args, **kwargs):
            pytest.fail("Offline Doctor must not contact the local service")
        monkeypatch.setattr("workspace_bridge.doctor_rpc.read_report", forbidden)
    result = cli._doctor(state, offline=offline, as_json=True)
    report = json.loads(capsys.readouterr().out)
    assert result == 0
    assert report["mode"] == ("offline" if offline else "live")
    if not offline:
        check = next(row for row in report["checks"] if row["code"] == "core.live_service")
        assert check["status"] == "unknown"
    assert not (state / SOCKET_NAME).exists()


def test_only_fixed_read_request_is_supported(private_state):
    calls = []
    with endpoint(private_state, lambda **kwargs: calls.append(kwargs)):
        assert (private_state / SOCKET_NAME).stat().st_mode & 0o777 == 0o600
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(2)
            client.connect(str(private_state / SOCKET_NAME))
            client.sendall(b'{"method":"start_run"}\n')
            assert json.loads(client.recv(1024)) == {"error": "invalid_request"}
    assert calls == []


def test_endpoint_errors_are_bounded_and_do_not_leak_details(private_state):
    def broken(**kwargs):
        raise RuntimeError("private provider error /secret/path")
    with endpoint(private_state, broken):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(2)
            client.connect(str(private_state / SOCKET_NAME))
            client.sendall(REQUEST)
            assert json.loads(client.recv(1024)) == {"error": "diagnostics_unavailable"}
        assert read_report(private_state) is None


@pytest.mark.parametrize("kind", ["file", "symlink", "public_directory"])
def test_unsafe_socket_locations_are_preserved(private_state, kind):
    path = private_state / SOCKET_NAME
    if kind == "file":
        path.write_text("preserve")
    elif kind == "symlink":
        target = private_state / "target"
        target.write_text("preserve")
        path.symlink_to(target)
    else:
        private_state.chmod(0o755)
    server = LocalDiagnostics(SimpleNamespace(state=private_state), {})
    assert not server.start()
    assert read_report(private_state, timeout=0.1) is None
    if kind != "public_directory":
        assert path.read_text() == "preserve"


def test_active_socket_is_never_replaced_and_stale_socket_is_recovered(private_state):
    report = failure_report(mode="live")
    with endpoint(private_state, lambda **kwargs: report):
        second = LocalDiagnostics(SimpleNamespace(state=private_state), {})
        assert not second.start()
        second.close()
        assert read_report(private_state) == report
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(private_state / SOCKET_NAME))
    stale.close()
    (private_state / SOCKET_NAME).chmod(0o600)
    with endpoint(private_state, lambda **kwargs: report):
        assert read_report(private_state) == report


def test_endpoint_cleanup_does_not_remove_a_replacement(private_state):
    with endpoint(private_state, lambda **kwargs: None):
        path = private_state / SOCKET_NAME
        path.unlink()
        path.write_text("replacement")
    assert path.read_text() == "replacement"


def test_partial_startup_cleans_its_socket(private_state, monkeypatch):
    def denied(*args, **kwargs):
        raise PermissionError("cannot set socket permissions")
    monkeypatch.setattr("workspace_bridge.doctor_rpc.os.chmod", denied)
    server = LocalDiagnostics(SimpleNamespace(state=private_state), {})
    assert not server.start()
    assert not (private_state / SOCKET_NAME).exists()


def test_running_cli_service_serves_doctor_and_removes_socket_on_shutdown(private_state):
    sockets = [socket.socket() for _ in range(3)]
    try:
        for sock in sockets:
            sock.bind(("127.0.0.1", 0))
        ports = [sock.getsockname()[1] for sock in sockets]
    finally:
        for sock in sockets:
            sock.close()
    state = private_state / "state"
    config = cli.initialize(state, *ports)
    service = cli.Service(state, config, run_coordinator_background=False)
    service.manage_bridge("rotate_token")
    service.close()
    command = [sys.executable, "-m", "workspace_bridge.cli", "--state", str(state)]
    environment = {key: value for key, value in os.environ.items() if not key.startswith("WB_")}
    process = subprocess.Popen([*command, "serve"], stdout=subprocess.DEVNULL,
                               stderr=subprocess.PIPE, env=environment, text=True)
    try:
        deadline = time.monotonic() + 10
        while not (state / SOCKET_NAME).exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert process.poll() is None
        assert (state / SOCKET_NAME).exists()
        result = subprocess.run([*command, "doctor", "--json"], env=environment,
                                capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, result.stderr
        report = json.loads(result.stdout)
        assert report["mode"] == "live" and report["overall"]["status"] == "pass"
        assert report["generated_at"]
        assert all(row["code"] != "core.live_service" for row in report["checks"])
    finally:
        process.terminate()
        process.communicate(timeout=10)
    assert not (state / SOCKET_NAME).exists()
