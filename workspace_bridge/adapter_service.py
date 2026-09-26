"""Runtime-neutral AdapterInstance state schema v1 and trusted launcher helpers.

One state directory owns exactly one Pi or Codex AdapterInstance. This module
owns only the private state contract (permissions, bounded nonsecret config,
token handling, executable resolution, allowlisted serve environment, and
authenticated descriptor health). It never installs/upgrades packages, never
writes Bridge/Node registry state, never calls npm/uv registries, and never
uses a shell.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import sys

from .security import BridgeError, open_absolute_dir


STATE_MODE = 0o700
PRIVATE_FILE_MODE = 0o600
CONFIG_NAME = "config.json"
TOKEN_NAME = "runtime-token"
RUNTIME_DIR_NAME = "runtime"
LOG_DIR_NAME = "logs"

RUNTIME_TYPES = frozenset({"pi", "codex"})
LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR"})
DEFAULT_PI_PORT = 8780
DEFAULT_CODEX_PORT = 8772

_SERVICE_ID_RE = re.compile(r"^[0-9a-f]{12}$")
_LABEL_RE = re.compile(r"^com\.workspace-bridge\.adapter\.(pi|codex)\.[0-9a-f]{12}$")
_UNIT_RE = re.compile(r"^workspace-bridge-adapter-(pi|codex)-[0-9a-f]{12}\.service$")

_HEALTH_TIMEOUT = 3
_MAX_HEALTH_RESPONSE = 64 * 1024


def _state_error(message: str, code: str = "adapter_state_unsafe") -> BridgeError:
    return BridgeError(message, code)


def _absolute_path(value: str | os.PathLike[str]) -> Path:
    """Return an absolute lexical path without following symlinks."""
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return Path(os.path.normpath(str(path)))


def validate_runtime_type(value: object) -> str:
    if not isinstance(value, str) or value not in RUNTIME_TYPES:
        raise _state_error("Adapter runtime type must be 'pi' or 'codex'",
                           "adapter_config_invalid")
    return value


def validate_service_id(value: object) -> str:
    if not isinstance(value, str) or not _SERVICE_ID_RE.fullmatch(value):
        raise _state_error("Adapter service ID is invalid",
                           "adapter_config_invalid")
    return value


def validate_port(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise _state_error("Adapter port must be an unprivileged TCP port",
                           "adapter_config_invalid")
    if not 1024 <= value <= 65535:
        raise _state_error("Choose an unprivileged TCP port",
                           "adapter_config_invalid")
    return int(value)


def validate_log_level(value: object) -> str:
    if not isinstance(value, str):
        raise _state_error("Log level must be DEBUG, INFO, WARNING or ERROR",
                           "adapter_config_invalid")
    text = value.strip().upper()
    if text not in LOG_LEVELS:
        raise _state_error("Log level must be DEBUG, INFO, WARNING or ERROR",
                           "adapter_config_invalid")
    return text


def adapter_label(runtime_type: str, service_id: str) -> str:
    runtime_type = validate_runtime_type(runtime_type)
    service_id = validate_service_id(service_id)
    return f"com.workspace-bridge.adapter.{runtime_type}.{service_id}"


def adapter_unit(runtime_type: str, service_id: str) -> str:
    runtime_type = validate_runtime_type(runtime_type)
    service_id = validate_service_id(service_id)
    return f"workspace-bridge-adapter-{runtime_type}-{service_id}.service"


def validate_label(value: object) -> str:
    if not isinstance(value, str) or not _LABEL_RE.fullmatch(value):
        raise _state_error("Adapter service label is invalid",
                           "adapter_config_invalid")
    return value


def validate_unit(value: object) -> str:
    if not isinstance(value, str) or not _UNIT_RE.fullmatch(value):
        raise _state_error("Adapter systemd unit is invalid",
                           "adapter_config_invalid")
    return value


def _validate_projects_root_value(raw: object) -> Path:
    if not isinstance(raw, str) or not raw or len(raw) > 1024:
        raise _state_error("Adapter projects root must be a canonical directory",
                           "adapter_config_invalid")
    if "\x00" in raw or "\n" in raw or "\r" in raw:
        raise _state_error("Adapter projects root must be a canonical directory",
                           "adapter_config_invalid")
    path = _absolute_path(raw)
    if str(path) != raw:
        raise _state_error("Adapter projects root must be canonical",
                           "adapter_config_invalid")
    return path


def validate_projects_root(raw: object, *, state: Path | None = None,
                            require_exists: bool = True) -> str:
    """Validate a canonical projects parent directory.

    When ``require_exists`` is true the directory must already exist; the
    input is canonically resolved (like Node ``--allow-root``) and the
    resolved absolute path is stored. Freshness checks for status paths may
    pass ``require_exists=False`` so an unavailable root is reported as
    degraded health rather than a config error.
    """
    if require_exists:
        if not isinstance(raw, str) or not raw or len(raw) > 1024:
            raise _state_error("Adapter projects root must be a canonical directory",
                               "adapter_config_invalid")
        if "\x00" in raw or "\n" in raw or "\r" in raw:
            raise _state_error("Adapter projects root must be a canonical directory",
                               "adapter_config_invalid")
        try:
            resolved = Path(raw).expanduser().resolve(strict=True)
        except (OSError, RuntimeError):
            raise _state_error("Adapter projects root is unavailable",
                               "adapter_config_invalid") from None
        if not resolved.is_absolute() or resolved.is_symlink() or not resolved.is_dir():
            raise _state_error("Adapter projects root must be a canonical directory",
                               "adapter_config_invalid")
        path = resolved
    else:
        path = _validate_projects_root_value(raw)
    if state is not None:
        try:
            canon_state = Path(state).expanduser().resolve(strict=False)
        except (OSError, RuntimeError):
            canon_state = _absolute_path(state)
        canon_state = Path(os.path.normpath(str(canon_state)))
        if path == canon_state or canon_state in path.parents or path in canon_state.parents:
            raise _state_error("Keep adapter state outside the projects root",
                               "adapter_config_invalid")
    return str(path)


def _validate_lexical_executable(raw: object) -> Path:
    if not isinstance(raw, str) or not raw or len(raw) > 1024:
        raise BridgeError("Adapter executable must be an absolute path",
                          "adapter_service_invalid_executable")
    if "\x00" in raw or "\n" in raw or "\r" in raw:
        raise BridgeError("Adapter executable must be an absolute path",
                          "adapter_service_invalid_executable")
    path = _absolute_path(raw)
    if str(path) != raw or not path.is_absolute():
        raise BridgeError("Adapter executable must be an absolute path",
                          "adapter_service_invalid_executable")
    try:
        is_regular = path.is_file() and stat.S_ISREG(path.stat().st_mode)
        executable = os.access(path, os.X_OK)
    except OSError:
        is_regular = False
        executable = False
    if not is_regular or not executable:
        raise BridgeError(
            "Adapter executable must be an existing regular executable file",
            "adapter_service_invalid_executable",
        )
    return path


def default_executable_name(runtime_type: str) -> str:
    runtime_type = validate_runtime_type(runtime_type)
    if runtime_type == "pi":
        return "workspace-bridge-pi-adapter"
    return "workspace-bridge-codex-adapter"


def resolve_adapter_executable(runtime_type: str,
                               explicit: str | os.PathLike[str] | None = None) -> str:
    """Resolve the stable lexical adapter executable.

    The default is the package-manager-provided stable command found on PATH
    (``workspace-bridge-pi-adapter`` or ``workspace-bridge-codex-adapter``);
    the lexical symlink path is preserved across package upgrades. An
    explicit absolute path is a local-admin development/test override only;
    it is validated as a regular executable and preserved lexically. No
    package registries are consulted.
    """
    runtime_type = validate_runtime_type(runtime_type)
    if explicit is not None:
        path = Path(explicit).expanduser()
        if not path.is_absolute():
            raise BridgeError("Adapter executable must be an absolute path",
                              "adapter_service_invalid_executable")
        return str(_validate_lexical_executable(str(_absolute_path(path))))
    name = default_executable_name(runtime_type)
    found = shutil.which(name)
    if not found:
        raise BridgeError(
            f"Adapter executable {name!r} was not found on PATH; install the "
            "runtime package or pass an explicit absolute executable for "
            "development use",
            "adapter_service_invalid_executable",
        )
    return str(_validate_lexical_executable(str(_absolute_path(found))))


def launcher_program(executable: str | os.PathLike[str] | None = None) -> list[str]:
    """Resolve the stable lexical top-level ``workspace-bridge`` launcher.

    The lexical symlink path is preserved so a stable uv-tool shim stays
    stable across upgrades. Development fallback uses the absolute
    ``sys.executable -m workspace_bridge.cli``. No registry, shell, or
    environment-setup command is persisted.
    """
    if executable is not None:
        path = Path(executable).expanduser()
        if not path.is_absolute():
            raise BridgeError("Adapter launcher must be an absolute path",
                              "adapter_service_invalid_executable")
        return [str(_validate_lexical_executable(str(_absolute_path(path))))]
    argv0 = Path(sys.argv[0])
    if argv0.name == "workspace-bridge" and argv0.is_absolute():
        try:
            st = argv0.stat()
            if argv0.is_file() and stat.S_ISREG(st.st_mode) and os.access(argv0, os.X_OK):
                return [str(_absolute_path(argv0))]
        except OSError:
            pass
    found = shutil.which("workspace-bridge")
    if found:
        return [str(_validate_lexical_executable(str(_absolute_path(found))))]
    python = _absolute_path(sys.executable)
    validated = _validate_lexical_executable(str(python))
    return [str(validated), "-m", "workspace_bridge.cli"]


def _validate_pi_binary(raw: object) -> str:
    if not isinstance(raw, str):
        raise _state_error("Pi binary must be a bounded executable name or path",
                           "adapter_config_invalid")
    text = raw.strip()
    if not 1 <= len(text) <= 256 or "\x00" in text or "\n" in text or "\r" in text:
        raise _state_error("Pi binary must be a bounded executable name or path",
                           "adapter_config_invalid")
    if any(ord(c) < 32 or ord(c) == 127 for c in text):
        raise _state_error("Pi binary must be a bounded executable name or path",
                           "adapter_config_invalid")
    if "/" in text and not text.startswith("/"):
        raise _state_error("Pi binary path must be absolute",
                           "adapter_config_invalid")
    return text


def _validate_agent_dir(raw: object) -> str:
    if not isinstance(raw, str) or not raw.strip() or len(raw) > 1024:
        raise _state_error("Agent directory must be an absolute path",
                           "adapter_config_invalid")
    text = raw.strip()
    if "\x00" in text or "\n" in text or "\r" in text:
        raise _state_error("Agent directory must be an absolute path",
                           "adapter_config_invalid")
    path = _absolute_path(text)
    if str(path) != text or not path.is_absolute():
        raise _state_error("Agent directory must be a canonical absolute path",
                           "adapter_config_invalid")
    return str(path)


def _validate_config_dict(config: object, state: Path) -> dict:
    if not isinstance(config, dict) or config.get("schema_version") != 1:
        raise _state_error("Unsupported adapter configuration",
                           "adapter_config_invalid")
    # Exact allowlist: no secrets, no arbitrary env, no Bridge/Node state.
    allowed = {"schema_version", "runtime_type", "projects_root", "port",
               "executable", "service_id", "pi_binary", "agent_dir",
               "log_level"}
    if set(config) - allowed:
        raise _state_error("Adapter configuration has unexpected fields",
                           "adapter_config_invalid")
    runtime_type = validate_runtime_type(config.get("runtime_type"))
    service_id = validate_service_id(config.get("service_id"))
    validate_projects_root(config.get("projects_root"), state=state,
                            require_exists=False)
    validate_port(config.get("port"))
    _validate_lexical_executable(config.get("executable"))
    if "pi_binary" in config:
        if runtime_type != "pi":
            raise _state_error("pi_binary applies only to the Pi runtime",
                               "adapter_config_invalid")
        _validate_pi_binary(config["pi_binary"])
    if "agent_dir" in config:
        if runtime_type != "pi":
            raise _state_error("agent_dir applies only to the Pi runtime",
                               "adapter_config_invalid")
        _validate_agent_dir(config["agent_dir"])
    if "log_level" in config:
        validate_log_level(config["log_level"])
    # Cross-check derived identity so a hand-edited config cannot claim a
    # foreign label/unit.
    _ = adapter_label(runtime_type, service_id)
    _ = adapter_unit(runtime_type, service_id)
    return config


def _check_directory(path: Path, *, name: str, mode: int | None = None,
                     create: bool = False) -> None:
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
    if not stat.S_ISDIR(st.st_mode):
        raise _state_error(f"{name} must be a directory owned by the service user")
    actual_mode = stat.S_IMODE(st.st_mode)
    if mode is not None and actual_mode != mode:
        raise _state_error(f"{name} must have mode {mode:04o}")
    if actual_mode & 0o077:
        raise _state_error(f"{name} must be private (mode 0700)")


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
    if not stat.S_ISREG(st.st_mode):
        raise _state_error(f"{name} must be a regular private file")
    if stat.S_IMODE(st.st_mode) != PRIVATE_FILE_MODE:
        raise _state_error(f"{name} must have mode 0600")
    return True


def _cleanup_fresh_state(state_path: Path) -> None:
    """Remove only artifacts created by a failed fresh init attempt.

    The state directory itself was just created by this call (pre-existing
    paths are rejected before creation), so its known children (config,
    token, runtime/, logs/) cannot be pre-existing content. Explicit safe
    removal is used instead of broad ``rmtree``: known files are unlinked,
    known empty subdirs are removed, then the state directory itself is
    removed. Failures during cleanup are swallowed so the original init
    error propagates; a successful cleanup leaves the path retryable.
    """
    try:
        for name in (CONFIG_NAME, TOKEN_NAME):
            try:
                (state_path / name).unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass
        for sub in (RUNTIME_DIR_NAME, LOG_DIR_NAME):
            try:
                (state_path / sub).rmdir()
            except FileNotFoundError:
                pass
            except OSError:
                pass
        try:
            state_path.rmdir()
        except FileNotFoundError:
            pass
        except OSError:
            pass
    except Exception:
        pass


def _check_owned(path: Path, st: os.stat_result, owner_uid: int) -> None:
    if int(st.st_uid) != int(owner_uid):
        if int(owner_uid) == 0:
            raise _state_error(
                "Adapter state is not root-owned; a root service requires an "
                "intentionally root-owned adapter state initialized and managed "
                "as root (running with sudo against a user-owned state is "
                "rejected, never converted)")
        raise _state_error("Adapter state is not owned by the current user")


def initialize_adapter(state: str | os.PathLike[str], *,
                       runtime_type: str,
                       projects_root: str,
                       port: int | None = None,
                       executable: str | os.PathLike[str] | None = None,
                       pi_binary: str | None = None,
                       agent_dir: str | None = None,
                       log_level: str | None = None) -> tuple[dict, str]:
    """Create fresh AdapterInstance state only.

    Generates the opaque service ID and runtime token. Validates the
    canonical projects root, loopback port, and stable lexical executable.
    Never installs/upgrades packages and never touches Bridge/Node state.
    Returns ``(config, token)``; the caller prints the token exactly once.
    """
    runtime_type = validate_runtime_type(runtime_type)
    if port is None:
        port = DEFAULT_PI_PORT if runtime_type == "pi" else DEFAULT_CODEX_PORT
    port = validate_port(port)
    state_path = _absolute_path(state)
    projects_canonical = validate_projects_root(projects_root, state=state_path,
                                                 require_exists=True)
    exe_lexical = resolve_adapter_executable(runtime_type, explicit=executable)
    config: dict = {
        "schema_version": 1,
        "runtime_type": runtime_type,
        "projects_root": projects_canonical,
        "port": port,
        "executable": exe_lexical,
        "service_id": secrets.token_hex(6),
    }
    if pi_binary is not None:
        if runtime_type != "pi":
            raise _state_error("pi_binary applies only to the Pi runtime",
                               "adapter_config_invalid")
        config["pi_binary"] = _validate_pi_binary(pi_binary)
    if agent_dir is not None:
        if runtime_type != "pi":
            raise _state_error("agent_dir applies only to the Pi runtime",
                               "adapter_config_invalid")
        config["agent_dir"] = _validate_agent_dir(agent_dir)
    if log_level is not None:
        config["log_level"] = validate_log_level(log_level)
    # Validate the assembled config before touching the filesystem.
    _validate_config_dict(config, state_path)
    # Fresh-state-only: reject any pre-existing path (directory, file, or
    # symlink) before any chmod/write. The parent must already exist as a
    # safe directory and is never created or mutated; only the final new
    # state directory is created by this call.
    if os.path.lexists(state_path):
        raise _state_error("Adapter already initialized; existing state was not overwritten",
                           "adapter_service_conflict")
    parent = state_path.parent
    try:
        if parent.is_symlink():
            raise _state_error("Adapter parent state directory is unavailable")
        pst = parent.stat()
        if not stat.S_ISDIR(pst.st_mode):
            raise _state_error("Adapter parent state directory is unavailable")
        fd = open_absolute_dir(str(parent))
        os.close(fd)
    except BridgeError:
        raise _state_error("Adapter parent state directory is unavailable") from None
    except FileNotFoundError:
        raise _state_error("Adapter parent state directory is unavailable") from None
    except OSError:
        raise _state_error("Adapter parent state directory is unavailable") from None
    try:
        state_path.mkdir(mode=STATE_MODE, parents=False, exist_ok=False)
    except FileExistsError:
        raise _state_error("Adapter already initialized; existing state was not overwritten",
                           "adapter_service_conflict") from None
    except OSError:
        raise _state_error("Unable to create adapter state") from None
    try:
        os.chmod(state_path, STATE_MODE)
    except OSError:
        _cleanup_fresh_state(state_path)
        raise _state_error("Unable to secure adapter state") from None
    token = secrets.token_urlsafe(32)
    try:
        for name, content in ((CONFIG_NAME, json.dumps(config, indent=2, sort_keys=True) + "\n"),
                              (TOKEN_NAME, token + "\n")):
            fd = os.open(state_path / name,
                         os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         PRIVATE_FILE_MODE)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        for sub in (RUNTIME_DIR_NAME, LOG_DIR_NAME):
            subdir = state_path / sub
            subdir.mkdir(mode=STATE_MODE, exist_ok=False)
            try:
                os.chmod(subdir, STATE_MODE)
            except OSError:
                raise _state_error(f"Unable to secure adapter {sub}") from None
    except Exception:
        _cleanup_fresh_state(state_path)
        raise
    return config, token


def load_adapter_config(state: str | os.PathLike[str], *,
                        require_roots: bool = True,
                        expected_uid: int | None = None) -> dict:
    """Load and validate adapter config without following state symlinks."""
    state_path = _absolute_path(state)
    _check_directory(state_path, name="Adapter state", mode=STATE_MODE)
    if expected_uid is not None:
        _check_owned(state_path, state_path.stat(), expected_uid)
    else:
        st = state_path.stat()
        uid = os.getuid() if hasattr(os, "getuid") else None
        if uid is not None and int(st.st_uid) != int(uid):
            raise _state_error("Adapter state is not owned by the current user")
    try:
        fd = open_absolute_dir(str(state_path))
        os.close(fd)
    except BridgeError:
        raise _state_error("Adapter state path is not a safe directory") from None
    config_path = state_path / CONFIG_NAME
    _check_private_file(config_path, name=CONFIG_NAME)
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        raise _state_error("Adapter configuration is not valid JSON",
                           "adapter_config_invalid") from None
    validated = _validate_config_dict(config, state_path)
    if require_roots:
        validate_projects_root(validated["projects_root"], state=state_path,
                                require_exists=True)
    for sub in (RUNTIME_DIR_NAME, LOG_DIR_NAME):
        subdir = state_path / sub
        if subdir.exists():
            _check_directory(subdir, name=f"Adapter {sub}", mode=STATE_MODE)
    return validated


def read_adapter_token(state: str | os.PathLike[str]) -> str:
    """Read the private runtime token (internal use only; never log it)."""
    state_path = _absolute_path(state)
    token_path = state_path / TOKEN_NAME
    _check_private_file(token_path, name=TOKEN_NAME)
    try:
        token = token_path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        raise _state_error("Adapter token cannot be read") from None
    if not token or len(token) > 512 or len(token) < 16:
        raise _state_error("Adapter token is invalid",
                           "adapter_config_invalid")
    if any(ord(c) < 33 or ord(c) == 127 or c.isspace() for c in token):
        raise _state_error("Adapter token is invalid",
                           "adapter_config_invalid")
    return token


def validate_adapter_state(state: str | os.PathLike[str], *,
                           expected_uid: int | None = None) -> tuple[dict, str]:
    """Validate state/config/token permissions; return config plus token."""
    state_path = _absolute_path(state)
    if expected_uid is not None:
        config = load_adapter_config(state_path, expected_uid=expected_uid)
    else:
        config = load_adapter_config(state_path)
    token = read_adapter_token(state_path)
    return config, token


def build_adapter_env(config: dict, token: str, state: str | os.PathLike[str]) -> dict:
    """Build the allowlisted runtime environment for ``execve``.

    Starts from the current process environment (preserving HOME/PATH and
    native login/auth context) and overrides only the fixed allowlisted
    names for the configured runtime. Config never injects arbitrary names.
    """
    runtime_type = validate_runtime_type(config.get("runtime_type"))
    port = validate_port(config.get("port"))
    projects_root = str(config.get("projects_root") or "")
    if not projects_root:
        raise _state_error("Adapter projects root is missing",
                           "adapter_config_invalid")
    if not token or len(token) > 512:
        raise _state_error("Adapter token is invalid",
                           "adapter_config_invalid")
    env = dict(os.environ)
    if runtime_type == "pi":
        env["WB_RUNTIME_TOKEN"] = token
        env["WB_PI_PROJECTS_DIR"] = projects_root
        env["WB_PI_ADAPTER_PORT"] = str(port)
        if "pi_binary" in config:
            env["WB_PI_BINARY"] = str(config["pi_binary"])
        else:
            env.pop("WB_PI_BINARY", None)
        if "agent_dir" in config:
            env["PI_CODING_AGENT_DIR"] = str(config["agent_dir"])
        else:
            env.pop("PI_CODING_AGENT_DIR", None)
        if "log_level" in config:
            env["WB_LOG_LEVEL"] = str(config["log_level"])
        # Never leak the other runtime's names into a Pi child.
        for stale in ("WB_CODEX_PROJECTS_ROOT", "WB_CODEX_ADAPTER_PORT",
                      "WB_CODEX_ADAPTER_STATE"):
            env.pop(stale, None)
    else:
        state_path = _absolute_path(state)
        env["WB_RUNTIME_TOKEN"] = token
        env["WB_CODEX_PROJECTS_ROOT"] = projects_root
        env["WB_CODEX_ADAPTER_PORT"] = str(port)
        env["WB_CODEX_ADAPTER_STATE"] = str(state_path / RUNTIME_DIR_NAME)
        if "log_level" in config:
            env["WB_LOG_LEVEL"] = str(config["log_level"])
        for stale in ("WB_PI_PROJECTS_DIR", "WB_PI_ADAPTER_PORT",
                      "WB_PI_BINARY", "PI_CODING_AGENT_DIR"):
            env.pop(stale, None)
    return env


def serve_argv(config: dict) -> list[str]:
    """Return the direct ``execve`` argv for the stored adapter executable."""
    exe = _validate_lexical_executable(config.get("executable"))
    return [str(exe)]


def probe_adapter_descriptor(config: dict, token: str) -> dict:
    """Probe the authenticated ``/v1/descriptor`` without leaking secrets."""
    import urllib.error
    import urllib.request

    runtime_type = validate_runtime_type(config.get("runtime_type"))
    port = validate_port(config.get("port"))
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/descriptor",
        headers={"Accept": "application/json", "X-Runtime-Token": token},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=_HEALTH_TIMEOUT) as response:
            raw = response.read(_MAX_HEALTH_RESPONSE + 1)
            if len(raw) > _MAX_HEALTH_RESPONSE:
                return {"status": "invalid_response", "code": "response_too_large"}
            value = json.loads(raw)
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            return {"status": "auth_failed", "code": "adapter_auth_failed"}
        if exc.code in (502, 503):
            return {"status": "degraded", "code": "runtime_degraded"}
        return {"status": "unavailable", "code": "http_error"}
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, UnicodeError):
        return {"status": "unavailable", "code": "adapter_unavailable"}
    if not isinstance(value, dict):
        return {"status": "invalid_response", "code": "adapter_protocol_error"}
    protocol = value.get("protocol")
    if not isinstance(protocol, dict) or protocol.get("major") != 1:
        return {"status": "invalid_response", "code": "adapter_protocol_error"}
    runtime = value.get("runtime")
    if not isinstance(runtime, dict) or runtime.get("id") != runtime_type:
        return {"status": "invalid_response", "code": "adapter_identity_mismatch"}
    instance = runtime.get("instanceId")
    if not isinstance(instance, str) or not instance or len(instance) > 200:
        return {"status": "invalid_response", "code": "adapter_protocol_error"}
    result: dict = {"status": "healthy", "code": "ok", "protocol": 1}
    adapter_version = runtime.get("adapterVersion")
    if isinstance(adapter_version, str) and adapter_version:
        result["adapter_version"] = adapter_version[:80]
    native_version = runtime.get("nativeVersion")
    if isinstance(native_version, str) and native_version:
        result["native_version"] = native_version[:80]
    return result


_MAX_EXE_LINK_DEPTH = 16
_MAX_PARENT_DEPTH = 128


def validate_root_executable_trust(
    lexical: str | os.PathLike[str],
    *,
    stat_fn=None,
    lstat_fn=None,
) -> Path:
    """Validate a root-service executable chain as root-controlled.

    The lexical entry, every followed symlink target, and the final
    executable must be root-owned and not group/world-writable; every parent
    directory that could replace the executable must likewise be
    root-owned and not group/world-writable. Returns the lexical path
    unchanged for ``ExecStart`` after validation.
    """
    lexical_path = _absolute_path(lexical)
    do_lstat = lstat_fn if lstat_fn is not None else os.lstat
    do_stat = stat_fn if stat_fn is not None else (lambda p: Path(p).stat())
    try:
        lexical_lstat = do_lstat(lexical_path)
    except OSError:
        raise BridgeError(
            "Adapter executable must be an existing regular executable file",
            "adapter_service_invalid_executable",
        ) from None
    if int(lexical_lstat.st_uid) != 0:
        raise BridgeError(
            "Root adapter service executable must be root-owned",
            "adapter_service_invalid_executable",
        )
    current = lexical_path
    traversed: list[Path] = [lexical_path]
    seen: set[str] = set()
    for _ in range(_MAX_EXE_LINK_DEPTH + 1):
        try:
            current_lstat = do_lstat(current)
        except OSError:
            raise BridgeError(
                "Adapter executable must be an existing regular executable file",
                "adapter_service_invalid_executable",
            ) from None
        if int(current_lstat.st_uid) != 0:
            raise BridgeError(
                "Root adapter service executable must be root-owned",
                "adapter_service_invalid_executable",
            )
        if not stat.S_ISLNK(current_lstat.st_mode):
            break
        key = str(current)
        if key in seen:
            raise BridgeError("Adapter executable symlink loop is unsafe",
                              "adapter_service_invalid_executable")
        seen.add(key)
        try:
            link_target = os.readlink(current)
        except OSError:
            raise BridgeError(
                "Adapter executable must be an existing regular executable file",
                "adapter_service_invalid_executable",
            ) from None
        next_path = Path(link_target)
        if not next_path.is_absolute():
            next_path = current.parent / next_path
        current = Path(os.path.normpath(str(next_path)))
        traversed.append(current)
    else:
        raise BridgeError("Adapter executable symlink chain is too deep",
                          "adapter_service_invalid_executable")
    try:
        final_stat = do_stat(current)
    except OSError:
        raise BridgeError(
            "Adapter executable must be an existing regular executable file",
            "adapter_service_invalid_executable",
        ) from None
    if not stat.S_ISREG(final_stat.st_mode):
        raise BridgeError(
            "Adapter executable must be an existing regular executable file",
            "adapter_service_invalid_executable",
        )
    if int(final_stat.st_uid) != 0:
        raise BridgeError(
            "Root adapter service executable must be root-owned",
            "adapter_service_invalid_executable",
        )
    if stat.S_IMODE(final_stat.st_mode) & 0o022:
        raise BridgeError(
            "Root adapter service executable must not be group/world-writable",
            "adapter_service_invalid_executable",
        )
    if final_stat.st_mode & 0o111 == 0:
        raise BridgeError(
            "Adapter executable must be an existing regular executable file",
            "adapter_service_invalid_executable",
        )
    anchors: list[Path] = []
    seen_anchors: set[str] = set()
    for hop in traversed:
        key = str(hop.parent)
        if key not in seen_anchors:
            seen_anchors.add(key)
            anchors.append(hop.parent)
    for anchor in anchors:
        directory = Path(anchor)
        for _ in range(_MAX_PARENT_DEPTH):
            try:
                directory_lstat = do_lstat(directory)
            except OSError:
                raise BridgeError("Adapter executable location is unsafe",
                                  "adapter_service_invalid_executable") from None
            if not stat.S_ISDIR(directory_lstat.st_mode):
                raise BridgeError("Adapter executable location is unsafe",
                                  "adapter_service_invalid_executable")
            if int(directory_lstat.st_uid) != 0:
                raise BridgeError(
                    "Root adapter service executable must live on a root-controlled path",
                    "adapter_service_invalid_executable",
                )
            if stat.S_IMODE(directory_lstat.st_mode) & 0o022:
                raise BridgeError(
                    "Root adapter service executable must not live on a writable path",
                    "adapter_service_invalid_executable",
                )
            parent = directory.parent
            if parent == directory:
                break
            directory = parent
        else:
            raise BridgeError("Adapter executable location is unsafe",
                              "adapter_service_invalid_executable")
    return lexical_path
