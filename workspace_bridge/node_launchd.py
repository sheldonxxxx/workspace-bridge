"""Safe per-user macOS LaunchAgent lifecycle for ``workspace-bridge-node``.

The Node is deliberately a host-native process.  This module owns only the
LaunchAgent configuration and launchd control plane; it never changes the
Node's allowed roots or deletes Node state.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import platform
import plistlib
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from typing import Callable, Sequence
import urllib.error
import urllib.request

from .security import BridgeError, digest, open_absolute_dir


LAUNCH_AGENT_LABEL = "com.workspace-bridge.node"
MANIFEST_NAME = "launchagent-manifest.json"
LOG_DIRECTORY_NAME = "logs"
STDOUT_LOG_NAME = "stdout.log"
STDERR_LOG_NAME = "stderr.log"
STATE_MODE = 0o700
PRIVATE_FILE_MODE = 0o600
LAUNCHCTL_TIMEOUT = 10
HEALTH_TIMEOUT = 3
MAX_LAUNCHCTL_DETAIL = 160
MAX_HEALTH_RESPONSE = 64 * 1024
MAX_ROOT_LABELS = 8
MAX_ROOT_ENTRIES = 64


@dataclass(frozen=True)
class NodeServicePaths:
    """Canonical paths owned by the Node LaunchAgent lifecycle."""

    state: Path
    plist: Path
    manifest: Path
    log_dir: Path
    stdout_log: Path
    stderr_log: Path
    uid: int | None = None

    @property
    def domain(self) -> str:
        uid = os.getuid() if self.uid is None else self.uid
        return f"gui/{uid}"

    @property
    def target(self) -> str:
        return f"{self.domain}/{LAUNCH_AGENT_LABEL}"


@dataclass(frozen=True)
class LaunchctlResult:
    """Bounded result from one launchctl invocation."""

    returncode: int | None
    stdout: str = ""
    stderr: str = ""
    error: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.error is None and self.returncode == 0


def _absolute_path(value: str | os.PathLike[str]) -> Path:
    """Return an absolute lexical path without following symlinks."""
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return Path(os.path.normpath(str(path)))


def default_node_state() -> Path:
    return _absolute_path(Path.home() / ".local/state/workspace-bridge-node")


def service_paths(state: str | os.PathLike[str], *, home: str | os.PathLike[str] | None = None) -> NodeServicePaths:
    state_path = _absolute_path(state)
    home_path = _absolute_path(home if home is not None else Path.home())
    launch_agents = home_path / "Library" / "LaunchAgents"
    log_dir = state_path / LOG_DIRECTORY_NAME
    return NodeServicePaths(
        state=state_path,
        plist=launch_agents / f"{LAUNCH_AGENT_LABEL}.plist",
        manifest=state_path / MANIFEST_NAME,
        log_dir=log_dir,
        stdout_log=log_dir / STDOUT_LOG_NAME,
        stderr_log=log_dir / STDERR_LOG_NAME,
        uid=None,
    )


def _current_uid() -> int | None:
    getuid = getattr(os, "getuid", None)
    return getuid() if getuid is not None else None


def _owned_by_current_user(path: Path, st: os.stat_result) -> bool:
    uid = _current_uid()
    return uid is None or st.st_uid == uid


def _state_error(message: str, code: str = "node_state_unsafe") -> BridgeError:
    return BridgeError(message, code)


def _check_directory(path: Path, *, name: str, mode: int | None = None,
                     create: bool = False, reject_group_write: bool = True) -> None:
    if create and not path.exists():
        try:
            path.mkdir(mode=mode or STATE_MODE, parents=True, exist_ok=True)
        except OSError:
            raise _state_error(f"Unable to create private {name}") from None
    try:
        if path.is_symlink():
            raise _state_error(f"{name} must not be a symlink")
        st = path.stat()
    except FileNotFoundError:
        raise _state_error(f"{name} is not initialized", "node_uninitialized") from None
    except OSError:
        raise _state_error(f"Unable to inspect {name}") from None
    if not stat.S_ISDIR(st.st_mode) or not _owned_by_current_user(path, st):
        raise _state_error(f"{name} must be a directory owned by the service user")
    actual_mode = stat.S_IMODE(st.st_mode)
    if mode is not None and actual_mode != mode:
        raise _state_error(f"{name} must have mode {mode:04o}")
    if reject_group_write and actual_mode & 0o022:
        raise _state_error(f"{name} must not be group- or world-writable")


def _check_private_file(path: Path, *, name: str, required: bool = True) -> bool:
    if path.is_symlink():
        raise _state_error(f"{name} must not be a symlink")
    if not path.exists():
        if required:
            raise _state_error(f"{name} is missing", "node_uninitialized")
        return False
    try:
        st = path.stat()
    except FileNotFoundError:
        if required:
            raise _state_error(f"{name} is missing", "node_uninitialized") from None
        return False
    except OSError:
        raise _state_error(f"Unable to inspect {name}") from None
    if not stat.S_ISREG(st.st_mode) or not _owned_by_current_user(path, st):
        raise _state_error(f"{name} must be a regular file owned by the service user")
    if stat.S_IMODE(st.st_mode) != PRIVATE_FILE_MODE:
        raise _state_error(f"{name} must have mode 0600")
    return True


def _validate_config(config: object, state: Path, *, require_roots: bool = True) -> dict:
    if not isinstance(config, dict) or config.get("schema_version") != 1:
        raise _state_error("Unsupported Node configuration", "node_config_invalid")
    roots = config.get("allowed_roots")
    if not isinstance(roots, list) or not roots or any(not isinstance(root, str) for root in roots):
        raise _state_error("Node configuration has invalid allowed roots", "node_config_invalid")
    for raw_root in roots:
        root = _absolute_path(raw_root)
        if (str(root) != raw_root or root == state or state in root.parents or
                root in state.parents):
            raise _state_error("Node configuration has non-canonical allowed roots",
                               "node_config_invalid")
        if require_roots:
            try:
                if root.is_symlink() or not root.is_dir():
                    raise _state_error("An allowed root is unavailable", "node_config_invalid")
            except OSError:
                raise _state_error("An allowed root is unavailable", "node_config_invalid") from None
    host = config.get("host")
    port = config.get("port")
    token_hash = config.get("node_token_hash")
    if (not isinstance(host, str) or not host or len(host) > 255 or
            any(ord(char) < 32 or ord(char) == 127 or char.isspace() for char in host)):
        raise _state_error("Node configuration has an invalid listen host", "node_config_invalid")
    if (not isinstance(port, int) or isinstance(port, bool) or not 1024 <= port <= 65535):
        raise _state_error("Node configuration has an invalid listen port", "node_config_invalid")
    if not isinstance(token_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", token_hash):
        raise _state_error("Node configuration has an invalid token hash", "node_config_invalid")
    return config


def load_node_config(state: str | os.PathLike[str], *, require_roots: bool = True) -> dict:
    """Load and validate initialized Node configuration without following state symlinks."""
    paths = service_paths(state)
    _check_directory(paths.state, name="Node state", mode=STATE_MODE,
                     reject_group_write=False)
    try:
        fd = open_absolute_dir(str(paths.state))
        os.close(fd)
    except BridgeError:
        raise _state_error("Node state path is not a safe directory") from None
    config_path = paths.state / "node-config.json"
    _check_private_file(config_path, name="node-config.json")
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        raise _state_error("Node configuration is not valid JSON", "node_config_invalid") from None
    return _validate_config(config, paths.state, require_roots=require_roots)


def _read_node_token(state: Path, config: dict) -> str:
    token_path = state / "node-token"
    _check_private_file(token_path, name="node-token")
    try:
        token = token_path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        raise _state_error("Node token cannot be read") from None
    if not token or len(token) > 512 or digest(token.encode()) != config["node_token_hash"]:
        raise _state_error("Node token does not match Node configuration", "node_config_invalid")
    return token


def validate_node_state(state: str | os.PathLike[str]) -> tuple[dict, str]:
    """Validate state/config/token permissions and return config plus token internally."""
    paths = service_paths(state)
    config = load_node_config(paths.state)
    for name in ("node.sqlite3", "node.sqlite3-wal", "node.sqlite3-shm"):
        _check_private_file(paths.state / name, name=name, required=False)
    return config, _read_node_token(paths.state, config)


def _launch_agents_dir(home: Path) -> Path:
    _check_directory(home, name="home directory", reject_group_write=True)
    library = home / "Library"
    _check_directory(library, name="Library directory", create=True,
                     reject_group_write=True)
    launch_agents = library / "LaunchAgents"
    _check_directory(launch_agents, name="LaunchAgents directory", create=True,
                     reject_group_write=True)
    return launch_agents


def _ensure_private_log_files(paths: NodeServicePaths) -> None:
    _check_directory(paths.log_dir, name="Node log directory", mode=STATE_MODE,
                     create=True, reject_group_write=False)
    for path in (paths.stdout_log, paths.stderr_log):
        if path.exists():
            _check_private_file(path, name=str(path))
            continue
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         PRIVATE_FILE_MODE)
            os.close(fd)
        except FileExistsError:
            _check_private_file(path, name=str(path))
        except OSError:
            raise _state_error("Unable to create private Node log files") from None


def _validate_program(program: Sequence[str]) -> list[str]:
    """Validate the executable persisted as ProgramArguments[0]."""
    if not program:
        raise BridgeError("Node executable is missing", "node_service_invalid_executable")
    selected = Path(program[0])
    try:
        is_regular = selected.is_file() and stat.S_ISREG(selected.stat().st_mode)
        executable = os.access(selected, os.X_OK)
    except OSError:
        is_regular = False
        executable = False
    if (not selected.is_absolute() or not is_regular or not executable):
        raise BridgeError(
            "Node executable must be an existing regular executable file",
            "node_service_invalid_executable",
        )
    return list(program)


def launch_agent_program(executable: str | os.PathLike[str] | None = None) -> list[str]:
    """Resolve an absolute installed entrypoint without relying on shell activation."""
    if executable is not None:
        path = Path(executable).expanduser()
        if not path.is_absolute():
            raise BridgeError(
                "Node executable must be an absolute path",
                "node_service_invalid_executable",
            )
        return _validate_program([str(_absolute_path(path))])

    argv0 = Path(sys.argv[0])
    if (argv0.name == "workspace-bridge-node" and argv0.is_absolute() and
            argv0.is_file() and os.access(argv0, os.X_OK)):
        return _validate_program([str(argv0)])
    found = shutil.which("workspace-bridge-node")
    if found:
        return _validate_program([str(_absolute_path(found))])
    python = _absolute_path(sys.executable)
    return _validate_program([str(python), "-m", "workspace_bridge.node_cli"])


def build_launch_agent_plist(
    state: str | os.PathLike[str],
    executable: str | os.PathLike[str],
    *,
    home: str | os.PathLike[str] | None = None,
) -> bytes:
    """Build deterministic XML plist bytes for the managed LaunchAgent."""
    paths = service_paths(state, home=home)
    program = _absolute_path(executable)
    if not program.is_absolute():
        raise BridgeError("Node executable must be an absolute path", "node_service_invalid")
    payload = {
        "Label": LAUNCH_AGENT_LABEL,
        "ProgramArguments": [str(program), "--state", str(paths.state), "serve"],
        "EnvironmentVariables": {"WB_NATIVE_LOG_GUARD": "1"},
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 10,
        "ProcessType": "Background",
        "StandardOutPath": str(paths.stdout_log),
        "StandardErrorPath": str(paths.stderr_log),
    }
    return plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=True)


def render_launch_agent_plist(
    state: str | os.PathLike[str],
    executable: str | os.PathLike[str],
    *,
    home: str | os.PathLike[str] | None = None,
) -> bytes:
    """Compatibility-named wrapper useful to callers/tests that render plists."""
    return build_launch_agent_plist(state, executable, home=home)


def _write_private_file(path: Path, content: bytes, *, overwrite: bool = False) -> None:
    if path.exists():
        _check_private_file(path, name=str(path))
        if not overwrite:
            raise _state_error(f"Refusing to overwrite existing {path}", "node_service_conflict")
    path.parent.mkdir(mode=STATE_MODE, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        os.chmod(temporary_path, PRIVATE_FILE_MODE)
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
        os.chmod(path, PRIVATE_FILE_MODE)
    except OSError:
        raise _state_error(f"Unable to write private {path}") from None
    finally:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass


def _read_manifest(paths: NodeServicePaths) -> dict | None:
    if paths.manifest.is_symlink():
        raise _state_error("LaunchAgent manifest must not be a symlink", "node_service_conflict")
    if not paths.manifest.exists():
        return None
    _check_private_file(paths.manifest, name="LaunchAgent manifest")
    try:
        value = json.loads(paths.manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        raise _state_error("LaunchAgent manifest is invalid", "node_service_conflict") from None
    if (not isinstance(value, dict) or value.get("schema_version") != 1 or
            value.get("label") != LAUNCH_AGENT_LABEL or
            value.get("state_path") != str(paths.state) or
            not isinstance(value.get("plist_sha256"), str) or
            not re.fullmatch(r"[0-9a-f]{64}", value["plist_sha256"])):
        raise _state_error("LaunchAgent manifest is invalid", "node_service_conflict")
    return value


def _plist_bytes(paths: NodeServicePaths) -> bytes | None:
    if paths.plist.is_symlink():
        raise _state_error("managed LaunchAgent plist must not be a symlink",
                           "node_service_conflict")
    if not paths.plist.exists():
        return None
    _check_private_file(paths.plist, name="managed LaunchAgent plist")
    try:
        return paths.plist.read_bytes()
    except OSError:
        raise _state_error("Unable to read managed LaunchAgent plist") from None


def _managed_plist(paths: NodeServicePaths) -> bool:
    raw = _plist_bytes(paths)
    if raw is None:
        return False
    manifest = _read_manifest(paths)
    return bool(manifest and manifest["plist_sha256"] == hashlib.sha256(raw).hexdigest())


def _manifest_for(paths: NodeServicePaths, plist: bytes) -> bytes:
    value = {
        "schema_version": 1,
        "label": LAUNCH_AGENT_LABEL,
        "state_path": str(paths.state),
        "plist_sha256": hashlib.sha256(plist).hexdigest(),
    }
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _safe_launchctl_detail(result: LaunchctlResult) -> str:
    """Return only a bounded, secret-redacted diagnostic fragment."""
    raw = (result.stderr or result.stdout or "").strip().splitlines()
    if not raw:
        return ""
    detail = raw[0][:MAX_LAUNCHCTL_DETAIL]
    for pattern in (
        r"(?i)(?:token|secret|password|authorization)[^\s:=]*\s*[:=]\s*[^\s]+",
        r"(?i)(?:environment|env)\s*[:=].*",
    ):
        detail = re.sub(pattern, "[REDACTED]", detail)
    return detail


class LaunchdManager:
    """Small injectable wrapper around modern user-domain launchctl commands."""

    def __init__(self, *, runner: Callable[..., object] | None = None,
                 platform_name: str | None = None,
                 home: str | os.PathLike[str] | None = None,
                 uid: int | None = None):
        self.runner = runner or subprocess.run
        self.platform_name = platform_name or platform.system()
        self.home = _absolute_path(home if home is not None else Path.home())
        self.uid = os.getuid() if uid is None else uid

    def require_macos(self) -> None:
        if self.platform_name != "Darwin":
            raise BridgeError(
                "workspace-bridge-node service commands require macOS launchd",
                "node_service_unsupported_platform",
            )

    def paths(self, state: str | os.PathLike[str]) -> NodeServicePaths:
        result = service_paths(state, home=self.home)
        return NodeServicePaths(
            state=result.state,
            plist=result.plist,
            manifest=result.manifest,
            log_dir=result.log_dir,
            stdout_log=result.stdout_log,
            stderr_log=result.stderr_log,
            uid=self.uid,
        )

    def _target(self, state: str | os.PathLike[str]) -> tuple[NodeServicePaths, str, str]:
        paths = self.paths(state)
        domain = f"gui/{self.uid}"
        return paths, domain, f"{domain}/{LAUNCH_AGENT_LABEL}"

    def run(self, arguments: Sequence[str]) -> LaunchctlResult:
        command = ["launchctl", *arguments]
        try:
            completed = self.runner(
                command,
                capture_output=True,
                text=True,
                check=False,
                timeout=LAUNCHCTL_TIMEOUT,
            )
        except FileNotFoundError:
            return LaunchctlResult(None, error="launchctl_not_found")
        except subprocess.TimeoutExpired:
            return LaunchctlResult(None, error="launchctl_timeout")
        except OSError:
            return LaunchctlResult(None, error="launchctl_unavailable")
        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
        if isinstance(stdout, bytes):
            stdout = stdout.decode("utf-8", "replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", "replace")
        return LaunchctlResult(int(completed.returncode), stdout[:4096], stderr[:4096])

    def print_service(self, state: str | os.PathLike[str]) -> LaunchctlResult:
        self.require_macos()
        _, _, target = self._target(state)
        return self.run(["print", target])

    def _expect_success(self, action: str, result: LaunchctlResult) -> None:
        if result.succeeded:
            return
        detail = _safe_launchctl_detail(result)
        suffix = f": {detail}" if detail else ""
        code = result.returncode if result.returncode is not None else "unavailable"
        raise BridgeError(f"launchctl {action} failed (exit {code}){suffix}",
                          "node_service_launchctl_failed")

    def bootstrap(self, state: str | os.PathLike[str]) -> None:
        self.require_macos()
        paths, domain, _ = self._target(state)
        self._expect_success("bootstrap", self.run(["bootstrap", domain, str(paths.plist)]))

    def bootout(self, state: str | os.PathLike[str]) -> None:
        self.require_macos()
        _, _, target = self._target(state)
        self._expect_success("bootout", self.run(["bootout", target]))

    def kickstart(self, state: str | os.PathLike[str]) -> None:
        self.require_macos()
        _, _, target = self._target(state)
        self._expect_success("kickstart", self.run(["kickstart", "-k", target]))


def _print_state(result: LaunchctlResult) -> dict:
    if result.error is not None:
        return {"available": False, "loaded": False, "running": False,
                "pid": None, "state": "unavailable", "error": result.error}
    if result.returncode != 0:
        return {"available": True, "loaded": False, "running": False,
                "pid": None, "state": "not_loaded"}
    state_match = re.search(r"(?im)^\s*state\s*=\s*([^\s]+)", result.stdout)
    pid_match = re.search(r"(?im)^\s*pid\s*=\s*(\d+)", result.stdout)
    state = state_match.group(1).lower() if state_match else "loaded"
    pid = int(pid_match.group(1)) if pid_match and int(pid_match.group(1)) <= 2**31 - 1 else None
    running = state == "running"
    if running and pid is None:
        running = True
    lifecycle_state = "failed" if state in {"exited", "crashed", "failed", "throttled"} else state
    return {"available": True, "loaded": True, "running": running,
            "pid": pid, "state": lifecycle_state}


_LAUNCHD_TRANSITION_ATTEMPTS = 50
_LAUNCHD_TRANSITION_INTERVAL = 0.1


def _is_running_state(info: dict) -> bool:
    """Exact-target proof that the service is loaded and running."""
    return bool(info.get("available") and info.get("loaded") and info.get("running"))


def _is_unloaded_state(info: dict) -> bool:
    """Exact-target proof that the service is not loaded.

    ``available`` must be true so a ``launchctl`` outage (``unavailable``)
    is never mistaken for a successful unload.
    """
    return bool(info.get("available") and not info.get("loaded"))


def _await_launchd_state(manager, state, predicate, *,
                         attempts: int = _LAUNCHD_TRANSITION_ATTEMPTS,
                         interval: float = _LAUNCHD_TRANSITION_INTERVAL):
    """Poll exact-target ``launchctl print`` until ``predicate`` holds.

    Returns the satisfying state dict, or ``None`` when the bounded budget
    expires. Uses module ``time.sleep`` so tests can monkeypatch it.
    """
    for index in range(attempts):
        current = _print_state(manager.print_service(state))
        if predicate(current):
            return current
        if index + 1 < attempts:
            time.sleep(interval)
    return None


def _launchctl_transition(manager, state, action: str, desired: str, *,
                          attempts: int = _LAUNCHD_TRANSITION_ATTEMPTS,
                          interval: float = _LAUNCHD_TRANSITION_INTERVAL):
    """Execute one launchctl mutation then verify the exact target state.

    A transient/nonzero/OSError ``BridgeError`` from the single mutation is
    accepted only when bounded ``launchctl print`` polling proves the exact
    target reached ``desired`` (``"running"`` or ``"not_loaded"``). When the
    desired state is not reached, the original command error is re-raised if
    one exists; otherwise a bounded ``node_service_launchctl_failed``
    error is raised. Success is never inferred from plist existence alone.
    """
    if desired == "running":
        predicate = _is_running_state
    elif desired == "not_loaded":
        predicate = _is_unloaded_state
    else:
        raise ValueError(f"Unknown launchd desired state: {desired}")
    command_error = None
    try:
        if action == "bootout":
            manager.bootout(state)
        elif action == "bootstrap":
            manager.bootstrap(state)
        elif action == "kickstart":
            manager.kickstart(state)
        else:
            raise ValueError(f"Unknown launchctl action: {action}")
    except BridgeError as exc:
        command_error = exc
    observed = _await_launchd_state(manager, state, predicate,
                                    attempts=attempts, interval=interval)
    if observed is not None:
        return observed
    if command_error is not None:
        raise command_error
    raise BridgeError(
        f"launchctl {action} did not reach {desired} within the bounded wait",
        "node_service_launchctl_failed",
    )


def _health_host(host: str) -> str:
    if host == "0.0.0.0":
        return "127.0.0.1"
    if host in {"::", "[::]"}:
        return "::1"
    return host


def _format_endpoint(host: str, port: int) -> str:
    display_host = host
    if ":" in display_host and not display_host.startswith("["):
        display_host = f"[{display_host}]"
    return f"http://{display_host}:{port}"


def _health_url(host: str, port: int) -> str:
    return _format_endpoint(_health_host(host), port) + "/v1/status"


def _root_status(value: dict) -> dict:
    """Return a bounded, path-free summary of Node allowed-root metadata."""
    if "allowed_roots" not in value:
        return {"status": "unknown", "code": "root_metadata_missing"}
    entries = value["allowed_roots"]
    if not isinstance(entries, list) or not entries or len(entries) > MAX_ROOT_ENTRIES:
        return {"status": "unknown", "code": "root_metadata_invalid"}

    available = 0
    unavailable_labels: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            return {"status": "unknown", "code": "root_metadata_invalid"}
        is_available = entry.get("available")
        label = entry.get("root_label")
        if (not isinstance(is_available, bool) or not isinstance(label, str) or
                not 1 <= len(label) <= 80 or "/" in label or "\\" in label or
                any(ord(char) < 32 or ord(char) == 127 for char in label)):
            return {"status": "unknown", "code": "root_metadata_invalid"}
        if is_available:
            available += 1
        elif len(unavailable_labels) < MAX_ROOT_LABELS:
            unavailable_labels.append(label)

    unavailable = len(entries) - available
    summary = {
        "status": "ready" if unavailable == 0 else "degraded",
        "total": len(entries),
        "available": available,
        "unavailable": unavailable,
        "unavailable_labels": unavailable_labels,
    }
    if unavailable > MAX_ROOT_LABELS:
        summary["unavailable_labels_truncated"] = True
    return summary


def probe_node_status(config: dict, token: str) -> dict:
    """Probe the authenticated local Node endpoint without returning credentials."""
    request = urllib.request.Request(
        _health_url(config["host"], config["port"]),
        headers={"Accept": "application/json", "X-Node-Token": token},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=HEALTH_TIMEOUT) as response:
            raw = response.read(MAX_HEALTH_RESPONSE + 1)
            if len(raw) > MAX_HEALTH_RESPONSE:
                return {"status": "invalid_response", "code": "response_too_large"}
            value = json.loads(raw)
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            return {"status": "auth_failed", "code": "node_auth_failed"}
        return {"status": "unavailable", "code": "http_error"}
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, UnicodeError):
        return {"status": "unavailable", "code": "node_unavailable"}
    if not isinstance(value, dict) or value.get("status") != "ok":
        return {"status": "invalid_response", "code": "node_protocol_error"}
    protocol = value.get("protocol")
    result = {"status": "healthy", "code": "ok"}
    if isinstance(protocol, int):
        result["protocol"] = protocol
    if isinstance(value.get("node_version"), str) and len(value["node_version"]) <= 64:
        result["node_version"] = value["node_version"]
    result["root_status"] = _root_status(value)
    return result


def _config_status(state: Path) -> tuple[dict | None, str | None, str | None]:
    try:
        config = load_node_config(state, require_roots=False)
        for name in ("node.sqlite3", "node.sqlite3-wal", "node.sqlite3-shm"):
            _check_private_file(state / name, name=name, required=False)
        token = _read_node_token(state, config)
    except BridgeError as exc:
        return None, None, exc.code
    return config, token, None


def service_status(
    state: str | os.PathLike[str],
    *,
    manager: LaunchdManager | None = None,
    probe: bool = True,
) -> dict:
    manager = manager or LaunchdManager()
    manager.require_macos()
    paths = manager.paths(state)
    config, token, config_error = _config_status(paths.state)
    plist_exists = paths.plist.exists() or paths.plist.is_symlink()
    installed = False
    plist_state = "missing"
    if plist_exists:
        try:
            raw_plist = _plist_bytes(paths)
            manifest = _read_manifest(paths)
            if manifest and raw_plist is not None:
                installed = manifest["plist_sha256"] == hashlib.sha256(raw_plist).hexdigest()
                plist_state = "managed" if installed else "modified"
            else:
                plist_state = "unmanaged"
        except BridgeError:
            plist_state = "unsafe"
    launchd = _print_state(manager.print_service(paths.state))
    if launchd["loaded"] and launchd["state"] == "failed":
        launchd_state = "failed"
    elif launchd["running"]:
        launchd_state = "running"
    elif launchd["loaded"]:
        launchd_state = "loaded"
    elif installed:
        launchd_state = "not_loaded"
    elif plist_state == "unmanaged":
        launchd_state = "unmanaged"
    elif plist_state == "modified":
        launchd_state = "modified"
    else:
        launchd_state = "not_installed"
    if config is None:
        health = {"status": "unavailable", "code": config_error or "node_uninitialized"}
    elif not probe:
        health = {"status": "not_probed", "code": "not_probed"}
    else:
        health = probe_node_status(config, token or "")
    result = {
        "label": LAUNCH_AGENT_LABEL,
        "domain": paths.domain,
        "target": paths.target,
        "plist_path": str(paths.plist),
        "state_path": str(paths.state),
        "stdout_path": str(paths.stdout_log),
        "stderr_path": str(paths.stderr_log),
        "installed": installed,
        "plist": plist_state,
        "launchd": launchd,
        "state": launchd_state,
        "health": health,
    }
    if config is not None:
        result["listen"] = {"host": config["host"], "port": config["port"],
                             "endpoint": _format_endpoint(config["host"], config["port"])}
    else:
        result["listen"] = None
    return result


def _require_managed(paths: NodeServicePaths) -> None:
    if _managed_plist(paths):
        return
    if paths.plist.exists():
        raise _state_error("Refusing to operate on an unmanaged LaunchAgent plist",
                           "node_service_conflict")
    raise _state_error("Node LaunchAgent is not installed", "node_service_not_installed")


def _record_action(action: str, state: Path, manager: LaunchdManager) -> dict:
    value = service_status(state, manager=manager, probe=False)
    value["action"] = action
    return value


def install_service(
    state: str | os.PathLike[str],
    *,
    manager: LaunchdManager | None = None,
    executable: str | os.PathLike[str] | None = None,
) -> dict:
    manager = manager or LaunchdManager()
    manager.require_macos()
    paths = manager.paths(state)
    validate_node_state(paths.state)
    program = launch_agent_program(executable)
    _check_directory(paths.state, name="Node state", mode=STATE_MODE,
                     reject_group_write=False)
    _ensure_private_log_files(paths)
    _launch_agents_dir(manager.home)
    if len(program) != 1:
        # ``build_launch_agent_plist`` persists the Python executable plus the
        # module invocation when no console script is available.  That form is
        # intentionally rendered here as a complete argv array as well.
        executable_path = _absolute_path(program[0])
        plist = plistlib.dumps({
            "Label": LAUNCH_AGENT_LABEL,
            "ProgramArguments": [str(executable_path), *program[1:], "--state",
                                  str(paths.state), "serve"],
            "EnvironmentVariables": {"WB_NATIVE_LOG_GUARD": "1"},
            "RunAtLoad": True,
            "KeepAlive": {"SuccessfulExit": False},
            "ThrottleInterval": 10,
            "ProcessType": "Background",
            "StandardOutPath": str(paths.stdout_log),
            "StandardErrorPath": str(paths.stderr_log),
        }, fmt=plistlib.FMT_XML, sort_keys=True)
    else:
        plist = build_launch_agent_plist(paths.state, program[0], home=manager.home)
    existing = _plist_bytes(paths)
    manifest = _read_manifest(paths)
    if (existing is not None and manifest and
            manifest["plist_sha256"] != hashlib.sha256(existing).hexdigest()):
        raise _state_error("LaunchAgent manifest does not match its plist",
                           "node_service_conflict")
    if existing is not None and existing != plist:
        if manifest and manifest["plist_sha256"] == hashlib.sha256(existing).hexdigest():
            raise _state_error("Managed LaunchAgent differs from the requested configuration; uninstall first",
                               "node_service_conflict")
        raise _state_error("Refusing to overwrite an unrelated or modified LaunchAgent plist",
                           "node_service_conflict")
    if existing is None:
        _write_private_file(paths.plist, plist)
    _write_private_file(paths.manifest, _manifest_for(paths, plist), overwrite=True)
    launchd = _print_state(manager.print_service(paths.state))
    if not launchd["loaded"]:
        # RunAtLoad agent: one verified bootstrap only, no kickstart.
        _launchctl_transition(manager, paths.state, "bootstrap", "running")
    return _record_action("install", paths.state, manager)


def start_service(state: str | os.PathLike[str], *, manager: LaunchdManager | None = None) -> dict:
    manager = manager or LaunchdManager()
    manager.require_macos()
    paths = manager.paths(state)
    validate_node_state(paths.state)
    _require_managed(paths)
    launchd = _print_state(manager.print_service(paths.state))
    if not launchd["loaded"]:
        # Plist already has RunAtLoad=True: one bootstrap + bounded
        # verification only; no redundant kickstart after bootstrap.
        _launchctl_transition(manager, paths.state, "bootstrap", "running")
    elif not launchd["running"]:
        _launchctl_transition(manager, paths.state, "kickstart", "running")
    return _record_action("start", paths.state, manager)


def stop_service(state: str | os.PathLike[str], *, manager: LaunchdManager | None = None) -> dict:
    manager = manager or LaunchdManager()
    manager.require_macos()
    paths = manager.paths(state)
    if not paths.plist.exists() and not paths.plist.is_symlink():
        return _record_action("stop", paths.state, manager)
    _require_managed(paths)
    launchd = _print_state(manager.print_service(paths.state))
    if launchd["loaded"]:
        # Wait until the exact target is truly not_loaded before returning
        # so a subsequent start cannot race the unload.
        _launchctl_transition(manager, paths.state, "bootout", "not_loaded")
    return _record_action("stop", paths.state, manager)


def restart_service(state: str | os.PathLike[str], *, manager: LaunchdManager | None = None) -> dict:
    manager = manager or LaunchdManager()
    manager.require_macos()
    paths = manager.paths(state)
    validate_node_state(paths.state)
    _require_managed(paths)
    launchd = _print_state(manager.print_service(paths.state))
    if launchd["loaded"]:
        _launchctl_transition(manager, paths.state, "bootout", "not_loaded")
    # One bootstrap + bounded verification only; no kickstart after bootstrap.
    _launchctl_transition(manager, paths.state, "bootstrap", "running")
    return _record_action("restart", paths.state, manager)


def uninstall_service(state: str | os.PathLike[str], *, manager: LaunchdManager | None = None) -> dict:
    manager = manager or LaunchdManager()
    manager.require_macos()
    paths = manager.paths(state)
    if not paths.plist.exists() and not paths.plist.is_symlink():
        return _record_action("uninstall", paths.state, manager)
    _require_managed(paths)
    launchd = _print_state(manager.print_service(paths.state))
    if not launchd["available"]:
        raise _state_error("Cannot verify launchd before removing the managed plist",
                           "node_service_launchctl_unavailable")
    if launchd["loaded"]:
        manager.bootout(paths.state)
    try:
        paths.plist.unlink()
    except OSError:
        raise _state_error("Unable to remove the managed LaunchAgent plist") from None
    return _record_action("uninstall", paths.state, manager)
