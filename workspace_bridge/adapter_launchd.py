"""Per-instance macOS LaunchAgent lifecycle for ``workspace-bridge adapter``.

One adapter state owns exactly one Pi, Codex or Claude AdapterInstance. The service
label derives only from the validated config-generated ``runtime_type`` plus
the opaque ``service_id`` (``com.workspace-bridge.adapter.<runtime>.<id>``).
Service artifacts never contain the runtime token or the projects root.
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
import stat
import subprocess
import sys
import tempfile
import time
from typing import Callable, Sequence

from .adapter_service import (
    _absolute_path,
    adapter_label,
    load_adapter_config,
    read_adapter_token,
    validate_adapter_state,
    validate_label,
    validate_runtime_type,
    validate_service_id,
    launcher_program,
    probe_adapter_descriptor,
)
from .security import BridgeError, open_absolute_dir


MANIFEST_NAME = "launchagent-manifest.json"
LOG_DIRECTORY_NAME = "logs"
STDOUT_LOG_NAME = "stdout.log"
STDERR_LOG_NAME = "stderr.log"
STATE_MODE = 0o700
PRIVATE_FILE_MODE = 0o600
LAUNCHCTL_TIMEOUT = 10
MAX_LAUNCHCTL_DETAIL = 160


@dataclass(frozen=True)
class AdapterServicePaths:
    """Canonical paths owned by one adapter LaunchAgent lifecycle."""

    state: Path
    label: str
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
        return f"{self.domain}/{self.label}"


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


def _state_error(message: str, code: str = "adapter_state_unsafe") -> BridgeError:
    return BridgeError(message, code)


def _current_uid() -> int | None:
    getuid = getattr(os, "getuid", None)
    return getuid() if getuid is not None else None


def _owned_by_current_user(path: Path, st: os.stat_result) -> bool:
    uid = _current_uid()
    return uid is None or st.st_uid == uid


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
        raise _state_error(f"{name} is not initialized",
                           "adapter_uninitialized") from None
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
            raise _state_error(f"{name} is missing", "adapter_uninitialized")
        return False
    try:
        st = path.stat()
    except FileNotFoundError:
        if required:
            raise _state_error(f"{name} is missing", "adapter_uninitialized") from None
        return False
    except OSError:
        raise _state_error(f"Unable to inspect {name}") from None
    if not stat.S_ISREG(st.st_mode) or not _owned_by_current_user(path, st):
        raise _state_error(f"{name} must be a regular file owned by the service user")
    if stat.S_IMODE(st.st_mode) != PRIVATE_FILE_MODE:
        raise _state_error(f"{name} must have mode 0600")
    return True


def _label_for_config(config: dict) -> str:
    return adapter_label(validate_runtime_type(config.get("runtime_type")),
                         validate_service_id(config.get("service_id")))


def service_paths(state: str | os.PathLike[str], *,
                  home: str | os.PathLike[str] | None = None,
                  label: str | None = None) -> AdapterServicePaths:
    """Return canonical LaunchAgent paths for an adapter state.

    The label is derived from validated config when omitted; an explicit
    label is an internal/test override only and is always re-validated.
    Public lifecycle entrypoints never accept a caller-supplied label.
    """
    state_path = _absolute_path(state)
    home_path = _absolute_path(home if home is not None else Path.home())
    if label is None:
        config = load_adapter_config(state_path, require_roots=False)
        label = _label_for_config(config)
    else:
        label = validate_label(label)
    launch_agents = home_path / "Library" / "LaunchAgents"
    log_dir = state_path / LOG_DIRECTORY_NAME
    return AdapterServicePaths(
        state=state_path,
        label=label,
        plist=launch_agents / f"{label}.plist",
        manifest=state_path / MANIFEST_NAME,
        log_dir=log_dir,
        stdout_log=log_dir / STDOUT_LOG_NAME,
        stderr_log=log_dir / STDERR_LOG_NAME,
        uid=None,
    )


def _launch_agents_dir(home: Path) -> Path:
    _check_directory(home, name="home directory", reject_group_write=True)
    library = home / "Library"
    _check_directory(library, name="Library directory", create=True,
                     reject_group_write=True)
    launch_agents = library / "LaunchAgents"
    _check_directory(launch_agents, name="LaunchAgents directory", create=True,
                     reject_group_write=True)
    return launch_agents


def _ensure_private_log_files(paths: AdapterServicePaths) -> None:
    _check_directory(paths.log_dir, name="Adapter log directory",
                     mode=STATE_MODE, create=True, reject_group_write=False)
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
            raise _state_error("Unable to create private adapter log files") from None


def _validate_program(program: Sequence[str]) -> list[str]:
    """Validate the workspace-bridge launcher persisted in the plist."""
    if not program:
        raise BridgeError("Adapter launcher is missing",
                          "adapter_service_invalid_executable")
    selected = Path(program[0])
    try:
        is_regular = selected.is_file() and stat.S_ISREG(selected.stat().st_mode)
        executable = os.access(selected, os.X_OK)
    except OSError:
        is_regular = False
        executable = False
    if not selected.is_absolute() or not is_regular or not executable:
        raise BridgeError(
            "Adapter launcher must be an existing regular executable file",
            "adapter_service_invalid_executable",
        )
    for extra in list(program[1:]):
        if not isinstance(extra, str) or not extra or "\x00" in extra:
            raise BridgeError("Adapter launcher arguments are invalid",
                              "adapter_service_invalid_executable")
        if any(ord(c) < 32 or ord(c) == 127 for c in extra):
            raise BridgeError("Adapter launcher arguments are invalid",
                              "adapter_service_invalid_executable")
    return list(program)


def launch_agent_program(executable: str | os.PathLike[str] | None = None) -> list[str]:
    """Resolve the stable lexical ``workspace-bridge`` launcher."""
    if executable is not None:
        path = Path(executable).expanduser()
        if not path.is_absolute():
            raise BridgeError("Adapter launcher must be an absolute path",
                              "adapter_service_invalid_executable")
        return _validate_program([str(_absolute_path(path))])
    return _validate_program(launcher_program())


def build_launch_agent_plist(
    state: str | os.PathLike[str],
    launcher: Sequence[str] | str | os.PathLike[str],
    label: str,
    *,
    home: str | os.PathLike[str] | None = None,
) -> bytes:
    """Build deterministic XML plist bytes for one adapter LaunchAgent.

    ``ProgramArguments`` contain only the stable lexical top-level
    ``workspace-bridge``, ``adapter``, ``--state``, ``<state>``, ``serve``;
    no runtime token or projects root is ever persisted.
    """
    label = validate_label(label)
    if isinstance(launcher, (str, os.PathLike)):
        program = _validate_program([str(_absolute_path(launcher))])
    else:
        program = _validate_program(list(launcher))
    state_path = _absolute_path(state)
    home_path = _absolute_path(home if home is not None else Path.home())
    log_dir = state_path / LOG_DIRECTORY_NAME
    payload = {
        "Label": label,
        "ProgramArguments": [*program, "adapter", "--state", str(state_path), "serve"],
        "EnvironmentVariables": {"WB_NATIVE_LOG_GUARD": "1"},
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 10,
        "ProcessType": "Background",
        "StandardOutPath": str(log_dir / STDOUT_LOG_NAME),
        "StandardErrorPath": str(log_dir / STDERR_LOG_NAME),
    }
    _ = home_path
    return plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=True)


def render_launch_agent_plist(
    state: str | os.PathLike[str],
    launcher: Sequence[str] | str | os.PathLike[str],
    label: str,
    *,
    home: str | os.PathLike[str] | None = None,
) -> bytes:
    return build_launch_agent_plist(state, launcher, label, home=home)


def _write_private_file(path: Path, content: bytes, *, overwrite: bool = False) -> None:
    if path.exists():
        _check_private_file(path, name=str(path))
        if not overwrite:
            raise _state_error(f"Refusing to overwrite existing {path}",
                               "adapter_service_conflict")
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


def _read_manifest(paths: AdapterServicePaths) -> dict | None:
    if paths.manifest.is_symlink():
        raise _state_error("LaunchAgent manifest must not be a symlink",
                           "adapter_service_conflict")
    if not paths.manifest.exists():
        return None
    _check_private_file(paths.manifest, name="LaunchAgent manifest")
    try:
        value = json.loads(paths.manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        raise _state_error("LaunchAgent manifest is invalid",
                           "adapter_service_conflict") from None
    if (not isinstance(value, dict) or value.get("schema_version") != 1
            or not isinstance(value.get("label"), str)
            or not isinstance(value.get("state_path"), str)
            or value.get("state_path") != str(paths.state)
            or value.get("label") != paths.label
            or not isinstance(value.get("plist_sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", value["plist_sha256"])
            or not isinstance(value.get("runtime_type"), str)
            or value.get("runtime_type") not in {"pi", "codex", "claude"}
            or not isinstance(value.get("service_id"), str)
            or not re.fullmatch(r"[0-9a-f]{12}", value["service_id"])):
        raise _state_error("LaunchAgent manifest is invalid",
                           "adapter_service_conflict")
    expected = adapter_label(value["runtime_type"], value["service_id"])
    if expected != value["label"] or expected != paths.label:
        raise _state_error("LaunchAgent manifest is invalid",
                           "adapter_service_conflict")
    return value


def _plist_bytes(paths: AdapterServicePaths) -> bytes | None:
    if paths.plist.is_symlink():
        raise _state_error("managed LaunchAgent plist must not be a symlink",
                           "adapter_service_conflict")
    if not paths.plist.exists():
        return None
    _check_private_file(paths.plist, name="managed LaunchAgent plist")
    try:
        return paths.plist.read_bytes()
    except OSError:
        raise _state_error("Unable to read managed LaunchAgent plist") from None


def _managed_plist(paths: AdapterServicePaths) -> bool:
    raw = _plist_bytes(paths)
    if raw is None:
        return False
    manifest = _read_manifest(paths)
    return bool(manifest and manifest["plist_sha256"] == hashlib.sha256(raw).hexdigest())


def _manifest_for(paths: AdapterServicePaths, plist: bytes, config: dict) -> bytes:
    value = {
        "schema_version": 1,
        "label": paths.label,
        "runtime_type": config["runtime_type"],
        "service_id": config["service_id"],
        "state_path": str(paths.state),
        "plist_sha256": hashlib.sha256(plist).hexdigest(),
    }
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _safe_launchctl_detail(result: LaunchctlResult) -> str:
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
                "workspace-bridge adapter service commands require macOS launchd",
                "adapter_service_unsupported_platform",
            )

    def paths(self, state: str | os.PathLike[str]) -> AdapterServicePaths:
        result = service_paths(state, home=self.home)
        return AdapterServicePaths(
            state=result.state,
            label=result.label,
            plist=result.plist,
            manifest=result.manifest,
            log_dir=result.log_dir,
            stdout_log=result.stdout_log,
            stderr_log=result.stderr_log,
            uid=self.uid,
        )

    def _target(self, state: str | os.PathLike[str]) -> tuple[AdapterServicePaths, str, str]:
        paths = self.paths(state)
        domain = f"gui/{self.uid}"
        return paths, domain, f"{domain}/{paths.label}"

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
                          "adapter_service_launchctl_failed")

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
    one exists; otherwise a bounded ``adapter_service_launchctl_failed``
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
        "adapter_service_launchctl_failed",
    )


def _config_status(state: Path) -> tuple[dict | None, str | None, str | None]:
    try:
        config = load_adapter_config(state, require_roots=False)
        token = read_adapter_token(state)
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
    try:
        paths = manager.paths(state)
    except BridgeError as exc:
        return {
            "service_manager": "launchd",
            "label": None,
            "installed": False,
            "plist": "unsafe",
            "launchd": {"available": False, "loaded": False,
                          "running": False, "pid": None,
                          "state": "unavailable", "error": "manager_unavailable"},
            "state": "unsafe",
            "health": {"status": "unavailable", "code": exc.code},
            "listen": None,
        }
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
        health = {"status": "unavailable", "code": config_error or "adapter_uninitialized"}
    elif not probe:
        health = {"status": "not_probed", "code": "not_probed"}
    else:
        try:
            health = probe_adapter_descriptor(config, token or "")
        except BridgeError:
            health = {"status": "unavailable", "code": "adapter_unavailable"}
    # Combine process state with descriptor health without claiming a
    # healthy runtime from process state alone.
    if health.get("status") == "healthy" and launchd_state not in {"running", "loaded"}:
        # A healthy descriptor proves the runtime even if launchd print lags;
        # keep the descriptor verdict (it is the stronger signal).
        state_value = launchd_state if launchd_state in {"running", "loaded"} else launchd_state
        _ = state_value
    elif health.get("status") in {"unavailable", "invalid_response"} and launchd_state == "running":
        health = {"status": "degraded", "code": health.get("code", "adapter_unavailable")}
        combined = "degraded"
        result = {
            "service_manager": "launchd",
            "label": paths.label,
            "domain": paths.domain,
            "target": paths.target,
            "installed": installed,
            "plist": plist_state,
            "launchd": launchd,
            "state": combined,
            "health": health,
        }
        if config is not None:
            result["runtime_type"] = config["runtime_type"]
            result["service_id"] = config["service_id"]
            result["listen"] = {"port": config["port"],
                                 "endpoint": f"http://127.0.0.1:{config['port']}"}
            if isinstance(health.get("adapter_version"), str):
                result["adapter_version"] = health["adapter_version"][:80]
            if isinstance(health.get("native_version"), str):
                result["native_version"] = health["native_version"][:80]
        else:
            result["listen"] = None
        return result
    combined = launchd_state
    if launchd_state == "running" and health.get("status") not in {"healthy", "not_probed"}:
        combined = "degraded"
    result = {
        "service_manager": "launchd",
        "label": paths.label,
        "domain": paths.domain,
        "target": paths.target,
        "installed": installed,
        "plist": plist_state,
        "launchd": launchd,
        "state": combined,
        "health": health,
    }
    if config is not None:
        result["runtime_type"] = config["runtime_type"]
        result["service_id"] = config["service_id"]
        result["listen"] = {"port": config["port"],
                             "endpoint": f"http://127.0.0.1:{config['port']}"}
        if isinstance(health.get("adapter_version"), str):
            result["adapter_version"] = health["adapter_version"][:80]
        if isinstance(health.get("native_version"), str):
            result["native_version"] = health["native_version"][:80]
    else:
        result["listen"] = None
    return result


def _require_managed(paths: AdapterServicePaths) -> None:
    if _managed_plist(paths):
        return
    if paths.plist.exists():
        raise _state_error("Refusing to operate on an unmanaged LaunchAgent plist",
                           "adapter_service_conflict")
    raise _state_error("Adapter LaunchAgent is not installed",
                       "adapter_service_not_installed")


def _record_action(action: str, state: Path, manager: LaunchdManager) -> dict:
    value = service_status(state, manager=manager, probe=False)
    value["action"] = action
    return value


def install_service(
    state: str | os.PathLike[str],
    *,
    manager: LaunchdManager | None = None,
    launcher: str | os.PathLike[str] | Sequence[str] | None = None,
) -> dict:
    manager = manager or LaunchdManager()
    manager.require_macos()
    # Validate full state (projects root must exist for install).
    config, _ = validate_adapter_state(manager.paths(state).state)
    # Re-resolve paths from the validated config label.
    paths = manager.paths(state)
    expected_label = _label_for_config(config)
    if paths.label != expected_label:
        raise _state_error("Adapter service label mismatch",
                           "adapter_service_conflict")
    if isinstance(launcher, (str, os.PathLike)):
        program = launch_agent_program(launcher)
    elif launcher is not None:
        program = _validate_program(list(launcher))
    else:
        program = launch_agent_program()
    _check_directory(paths.state, name="Adapter state", mode=STATE_MODE,
                     reject_group_write=False)
    try:
        fd = open_absolute_dir(str(paths.state))
        os.close(fd)
    except BridgeError:
        raise _state_error("Adapter state path is not a safe directory") from None
    _ensure_private_log_files(paths)
    _launch_agents_dir(manager.home)
    if len(program) == 1:
        plist = build_launch_agent_plist(paths.state, program, paths.label,
                                         home=manager.home)
    else:
        # Multi-element launcher (development python -m fallback) renders as
        # a complete argv array with the same adapter subcommand suffix.
        validated = _validate_program(program)
        state_str = str(paths.state)
        payload = {
            "Label": paths.label,
            "ProgramArguments": [*validated, "adapter", "--state", state_str, "serve"],
            "EnvironmentVariables": {"WB_NATIVE_LOG_GUARD": "1"},
            "RunAtLoad": True,
            "KeepAlive": {"SuccessfulExit": False},
            "ThrottleInterval": 10,
            "ProcessType": "Background",
            "StandardOutPath": str(paths.stdout_log),
            "StandardErrorPath": str(paths.stderr_log),
        }
        plist = plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=True)
    existing = _plist_bytes(paths)
    manifest = _read_manifest(paths)
    if (existing is not None and manifest and
            manifest["plist_sha256"] != hashlib.sha256(existing).hexdigest()):
        raise _state_error("LaunchAgent manifest does not match its plist",
                           "adapter_service_conflict")
    if existing is not None and existing != plist:
        if manifest and manifest["plist_sha256"] == hashlib.sha256(existing).hexdigest():
            raise _state_error("Managed LaunchAgent differs from the requested configuration; uninstall first",
                               "adapter_service_conflict")
        raise _state_error("Refusing to overwrite an unrelated or modified LaunchAgent plist",
                           "adapter_service_conflict")
    if existing is None:
        _write_private_file(paths.plist, plist)
    _write_private_file(paths.manifest, _manifest_for(paths, plist, config),
                        overwrite=True)
    launchd = _print_state(manager.print_service(paths.state))
    if not launchd["loaded"]:
        # RunAtLoad agent: one verified bootstrap only, no kickstart.
        _launchctl_transition(manager, paths.state, "bootstrap", "running")
    return _record_action("install", paths.state, manager)


def start_service(state: str | os.PathLike[str], *,
                  manager: LaunchdManager | None = None) -> dict:
    manager = manager or LaunchdManager()
    manager.require_macos()
    paths = manager.paths(state)
    validate_adapter_state(paths.state)
    _require_managed(paths)
    launchd = _print_state(manager.print_service(paths.state))
    if not launchd["loaded"]:
        # Plist already has RunAtLoad=True: one bootstrap + bounded
        # verification only; no redundant kickstart after bootstrap.
        _launchctl_transition(manager, paths.state, "bootstrap", "running")
    elif not launchd["running"]:
        _launchctl_transition(manager, paths.state, "kickstart", "running")
    return _record_action("start", paths.state, manager)


def stop_service(state: str | os.PathLike[str], *,
                 manager: LaunchdManager | None = None) -> dict:
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


def restart_service(state: str | os.PathLike[str], *,
                    manager: LaunchdManager | None = None) -> dict:
    manager = manager or LaunchdManager()
    manager.require_macos()
    paths = manager.paths(state)
    validate_adapter_state(paths.state)
    _require_managed(paths)
    launchd = _print_state(manager.print_service(paths.state))
    if launchd["loaded"]:
        _launchctl_transition(manager, paths.state, "bootout", "not_loaded")
    # One bootstrap + bounded verification only; no kickstart after bootstrap.
    _launchctl_transition(manager, paths.state, "bootstrap", "running")
    return _record_action("restart", paths.state, manager)


def uninstall_service(state: str | os.PathLike[str], *,
                      manager: LaunchdManager | None = None) -> dict:
    manager = manager or LaunchdManager()
    manager.require_macos()
    paths = manager.paths(state)
    plist_exists = paths.plist.exists() or paths.plist.is_symlink()
    manifest_exists = paths.manifest.exists() or paths.manifest.is_symlink()
    if not plist_exists and not manifest_exists:
        return _record_action("uninstall", paths.state, manager)
    if not plist_exists:
        # Manifest-only partial cleanup: the plist is already gone but a
        # valid manifest for this exact state/label/runtime/service ID
        # remains (e.g. a prior uninstall removed the plist then failed at
        # manifest removal). Never infer this from an invalid/foreign
        # manifest: identity and hash shape must validate first.
        manifest = _read_manifest(paths)
        if manifest is None:
            return _record_action("uninstall", paths.state, manager)
        launchd = _print_state(manager.print_service(paths.state))
        if not launchd["available"]:
            raise _state_error("Cannot verify launchd before removing the managed manifest",
                               "adapter_service_launchctl_unavailable")
        if launchd["loaded"]:
            # Only the exact managed target proven by the valid manifest
            # is ever booted out here.
            manager.bootout(paths.state)
        try:
            paths.manifest.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            raise _state_error("Unable to remove the managed LaunchAgent manifest") from None
        return _record_action("uninstall", paths.state, manager)
    _require_managed(paths)
    launchd = _print_state(manager.print_service(paths.state))
    if not launchd["available"]:
        raise _state_error("Cannot verify launchd before removing the managed plist",
                           "adapter_service_launchctl_unavailable")
    if launchd["loaded"]:
        manager.bootout(paths.state)
    try:
        paths.plist.unlink()
    except OSError:
        raise _state_error("Unable to remove the managed LaunchAgent plist") from None
    # Remove the service manifest second so a failure here is retryable via
    # the manifest-only path above. Config, token, runtime/, logs/ and log
    # files are always preserved.
    try:
        paths.manifest.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        raise _state_error("Unable to remove the managed LaunchAgent manifest") from None
    return _record_action("uninstall", paths.state, manager)
