"""Owner-only local access to the running Bridge's canonical diagnostics.

The socket's private parent directory and 0600 mode authenticate the local
service account. There is no TCP listener, credential, or mutation method.
Unavailable, older, or stopped services fall back to standalone observations.
"""
from __future__ import annotations

import errno
import json
import os
from pathlib import Path
import socket
import socketserver
import stat
import threading

from .diagnostics import STATUSES

SOCKET_NAME = "doctor.sock"
REQUEST = b"workspace-bridge-doctor-v1\n"
MAX_RESPONSE = 4 * 1024 * 1024


def _private_state(state: Path) -> bool:
    info = state.lstat()
    return (stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()
            and not info.st_mode & 0o077)


def _owned_socket(path: Path) -> os.stat_result:
    info = path.lstat()
    if (not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o077):
        raise OSError("Unsafe local diagnostics socket")
    return info


def read_report(state: Path, *, timeout: float = 60) -> dict | None:
    """Request a fresh report without creating state or starting an adapter."""
    try:
        if not _private_state(state):
            return None
        path = state / SOCKET_NAME
        _owned_socket(path)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(timeout)
            client.connect(str(path))
            client.sendall(REQUEST)
            with client.makefile("rb") as stream:
                line = stream.readline(MAX_RESPONSE + 1)
        if len(line) > MAX_RESPONSE or not line.endswith(b"\n"):
            return None
        result = json.loads(line)
        report = result.get("report") if isinstance(result, dict) else None
        if (not isinstance(report, dict) or report.get("mode") != "live"
                or not isinstance(report.get("overall"), dict)
                or not isinstance(report["overall"].get("status"), str)
                or report["overall"]["status"] not in STATUSES
                or not isinstance(report.get("checks"), list)
                or not isinstance(report.get("runnable_routes"), list)
                or not isinstance(report.get("generated_at"), str)):
            return None
        return report
    except (OSError, ValueError):
        return None


class _Server(socketserver.ThreadingUnixStreamServer):
    # Finish an in-flight observation before serve() closes its Service/DB.
    daemon_threads = False
    block_on_close = True

    def __init__(self, path: Path, service, listener: dict):
        self.service = service
        self.listener = listener
        self.probe_lock = threading.Lock()
        super().__init__(str(path), _Handler)


class _Handler(socketserver.StreamRequestHandler):
    timeout = 2

    def handle(self) -> None:
        server = self.server
        try:
            if self.rfile.readline(len(REQUEST) + 1) != REQUEST:
                self.wfile.write(b'{"error":"invalid_request"}\n')
                return
            if not server.probe_lock.acquire(blocking=False):
                self.wfile.write(b'{"error":"busy"}\n')
                return
            try:
                report = server.service.diagnostic_report(listener=server.listener)
                response = json.dumps({"report": report}, ensure_ascii=True).encode() + b"\n"
                if len(response) > MAX_RESPONSE:
                    response = b'{"error":"report_limit"}\n'
            except Exception:  # Native/provider details never leave this endpoint.
                response = b'{"error":"diagnostics_unavailable"}\n'
            finally:
                server.probe_lock.release()
            self.wfile.write(response)
        except OSError:
            pass


class LocalDiagnostics:
    """Lifecycle owned by serve(), under the existing exclusive service lock."""

    def __init__(self, service, listener: dict):
        self.service = service
        self.listener = listener
        self.path = service.state / SOCKET_NAME
        self.server: _Server | None = None
        self.thread: threading.Thread | None = None
        self.identity: tuple[int, int] | None = None

    def start(self) -> bool:
        try:
            if not _private_state(self.service.state):
                return False
            try:
                previous = _owned_socket(self.path)
            except FileNotFoundError:
                previous = None
            if previous is not None:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                    probe.settimeout(0.2)
                    try:
                        probe.connect(str(self.path))
                    except OSError as exc:
                        if exc.errno != errno.ECONNREFUSED:
                            return False
                    else:
                        return False  # Never replace an active endpoint.
                current = _owned_socket(self.path)
                if (current.st_dev, current.st_ino) != (previous.st_dev, previous.st_ino):
                    return False
                self.path.unlink()
            self.server = _Server(self.path, self.service, self.listener)
            info = self.path.lstat()
            self.identity = (info.st_dev, info.st_ino)
            os.chmod(self.path, 0o600)
            self.thread = threading.Thread(target=self.server.serve_forever,
                                           name="bridge-local-diagnostics", daemon=True)
            self.thread.start()
            return True
        except OSError:
            self.close()
            return False

    def close(self) -> None:
        # Withdraw the endpoint before waiting for in-flight reports or the
        # listener's polling interval. No new clients should arrive at shutdown.
        if self.identity is not None:
            try:
                info = self.path.lstat()
                if stat.S_ISSOCK(info.st_mode) and (info.st_dev, info.st_ino) == self.identity:
                    self.path.unlink()
            except OSError:
                pass
            self.identity = None
        if self.server is not None:
            if self.thread is not None:
                self.server.shutdown()
                self.thread.join(timeout=2)
            self.server.server_close()
            self.server = None
