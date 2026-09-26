"""Linux system-level systemd lifecycle for ``workspace-bridge node``.

The Node is a host-native process running as the account that owns the
initialized Node state. By default that is the normal non-root installing
user; an intentional advanced mode supports a root-owned state managed as
root, in which case the unit runs as ``User=root``/``Group=root``. This
module owns only the system unit configuration and control plane; it never
changes allowed roots, deletes Node state, accepts caller unit paths or
caller user/group overrides, uses per-user units and boot persistence
helpers, or infers identity from SUDO_USER.

System manager only: the fixed unit
``/etc/systemd/system/workspace-bridge-node.service`` starts at boot via
``enable --now`` while ``User=``/``Group=`` reflect the state owner. A
non-root caller runs the plain
``workspace-bridge node --state <state> service ...`` command and the backend
invokes only fixed bounded ``sudo`` operations for privileged file and
``systemctl`` mutations; a root caller (EUID 0) executes the same fixed
helper argv directly without ``sudo``. No shell is ever invoked.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from typing import Callable, Sequence

from .security import BridgeError, open_absolute_dir


UNIT_NAME = "workspace-bridge-node.service"
UNIT_PATH = Path("/etc/systemd/system/workspace-bridge-node.service")
MANIFEST_NAME = "systemd-system-manifest.json"
STATE_MODE = 0o700
PRIVATE_FILE_MODE = 0o600
SYSTEMCTL_TIMEOUT = 10
SUDO_TIMEOUT = 60
MAX_COMMAND_OUTPUT = 4096
MAX_COMMAND_DETAIL = 160

_SUDO_CANDIDATES = ("/usr/bin/sudo", "/bin/sudo")
_SYSTEMCTL_CANDIDATES = ("/bin/systemctl", "/usr/bin/systemctl")
_INSTALL_CANDIDATES = ("/usr/bin/install", "/bin/install")
_RM_CANDIDATES = ("/bin/rm", "/usr/bin/rm")

_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\-]*\$?$")


@dataclass(frozen=True)
class SystemdServicePaths:
    """Canonical paths for the Linux system lifecycle (fixed unit path)."""

    state: Path
    unit: Path
    manifest: Path


@dataclass(frozen=True)
class CommandResult:
    """Bounded result from one fixed subprocess invocation."""

    returncode: int | None
    stdout: str = ""
    stderr: str = ""
    error: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.error is None and self.returncode == 0


def _absolute_path(value: str | os.PathLike[str]) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return Path(os.path.normpath(str(path)))


def _state_error(message: str, code: str = "node_state_unsafe") -> BridgeError:
    return BridgeError(message, code)


def service_paths(state: str | os.PathLike[str]) -> SystemdServicePaths:
    """Return fixed system paths for a Node state (no caller unit path)."""
    state_path = _absolute_path(state)
    return SystemdServicePaths(
        state=state_path, unit=UNIT_PATH, manifest=state_path / MANIFEST_NAME
    )


def _validate_systemd_name(value: str, *, kind: str) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 64:
        raise BridgeError(f"Systemd {kind} is invalid", "node_service_invalid_identity")
    if any(ord(c) < 33 or ord(c) == 127 or c.isspace() for c in value):
        raise BridgeError(f"Systemd {kind} is invalid", "node_service_invalid_identity")
    if any(c in (":", "/", "\\", "\x00", "\n", "\r") for c in value):
        raise BridgeError(f"Systemd {kind} is invalid", "node_service_invalid_identity")
    if not _NAME_RE.fullmatch(value):
        raise BridgeError(f"Systemd {kind} is invalid", "node_service_invalid_identity")
    return value


def _effective_uid() -> int:
    geteuid = getattr(os, "geteuid", None)
    if geteuid is not None:
        try:
            return int(geteuid())
        except OSError:
            pass
    return int(os.getuid())


def _default_current_ids() -> tuple[str, int, str, int]:
    import grp
    import pwd

    # Run-as identity derives strictly from the effective account, never from
    # SUDO_USER or caller overrides. Non-root keeps User/Group=current user;
    # EUID 0 resolves the passwd/group identity for uid/gid 0.
    uid = _effective_uid()
    try:
        entry = pwd.getpwuid(uid)
    except (KeyError, OSError):
        raise BridgeError("Current user is unavailable", "node_service_privilege_unavailable") from None
    username = _validate_systemd_name(entry.pw_name, kind="User")
    gid = int(entry.pw_gid)
    try:
        group_name = grp.getgrgid(gid).gr_name
    except (KeyError, OSError):
        raise BridgeError("Current primary group is unavailable", "node_service_privilege_unavailable") from None
    group = _validate_systemd_name(group_name, kind="Group")
    if uid < 0 or gid < 0 or uid > 2**32 - 1 or gid > 2**32 - 1:
        raise BridgeError("Current user identity is invalid", "node_service_invalid_identity")
    return username, uid, group, gid


# ---------------------------------------------------------------------------
# Executable resolution (stable single public CLI path)
# ---------------------------------------------------------------------------

def _validate_systemd_prefix(program: Sequence[str]) -> list[str]:
    if not program or not isinstance(program[0], str):
        raise BridgeError("Node executable is missing", "node_service_invalid_executable")
    selected = Path(program[0])
    try:
        st = selected.stat()
        is_regular = selected.is_file() and stat.S_ISREG(st.st_mode)
        executable = os.access(selected, os.X_OK)
    except OSError:
        is_regular = False
        executable = False
    if not selected.is_absolute() or not is_regular or not executable:
        raise BridgeError(
            "Node executable must be an existing regular executable file",
            "node_service_invalid_executable",
        )
    for extra in list(program[1:]):
        if not isinstance(extra, str) or not extra or "\x00" in extra:
            raise BridgeError("Node executable arguments are invalid", "node_service_invalid_executable")
        if any(ord(c) < 32 or ord(c) == 127 for c in extra):
            raise BridgeError("Node executable arguments are invalid", "node_service_invalid_executable")
    return list(program)


def systemd_program(executable: str | os.PathLike[str] | None = None) -> list[str]:
    """Resolve the stable top-level ``workspace-bridge`` entrypoint.

    The lexical symlink path is preserved so a stable uv-tool shim (for
    example ``~/.local/bin/workspace-bridge``) stays stable across
    ``uv tool upgrade`` target changes. Development fallback uses the
    absolute ``sys.executable -m workspace_bridge.cli``. No ``uv``,
    package, registry, shell-activation, or environment-setup command is
    persisted. The module never inspects uv's internal environment target.
    """
    if executable is not None:
        path = Path(executable).expanduser()
        if not path.is_absolute():
            raise BridgeError("Node executable must be an absolute path", "node_service_invalid_executable")
        return _validate_systemd_prefix([str(_absolute_path(path))])
    argv0 = Path(sys.argv[0])
    if argv0.name == "workspace-bridge" and argv0.is_absolute():
        try:
            st = argv0.stat()
            if argv0.is_file() and stat.S_ISREG(st.st_mode) and os.access(argv0, os.X_OK):
                return _validate_systemd_prefix([str(_absolute_path(argv0))])
        except OSError:
            pass
    found = shutil.which("workspace-bridge")
    if found:
        return _validate_systemd_prefix([str(_absolute_path(found))])
    python = _absolute_path(sys.executable)
    return _validate_systemd_prefix([str(python), "-m", "workspace_bridge.cli"])


_MAX_EXE_LINK_DEPTH = 16
_MAX_PARENT_DEPTH = 128


def _validate_root_executable_trust(
    lexical: str | os.PathLike[str],
    *,
    stat_fn: Callable[[Path], os.stat_result] | None = None,
    lstat_fn: Callable[[Path], os.stat_result] | None = None,
) -> Path:
    """Validate a root-service executable chain as root-controlled.

    Because a root systemd service executes ``ExecStart`` with uid 0, the
    lexical entry (``lstat``), every symlink target actually followed, and
    the final executable must be root-owned and not group/world-writable;
    every parent directory that could replace the executable must likewise
    be root-owned and not group/world-writable. The stable lexical path is
    returned unchanged for ``ExecStart`` after validation.
    """
    lexical_path = _absolute_path(lexical)
    if not lexical_path.is_absolute():
        raise BridgeError("Node executable must be an absolute path", "node_service_invalid_executable")
    do_lstat = lstat_fn if lstat_fn is not None else os.lstat
    do_stat = stat_fn if stat_fn is not None else (lambda p: Path(p).stat())
    try:
        lexical_lstat = do_lstat(lexical_path)
    except OSError:
        raise BridgeError(
            "Node executable must be an existing regular executable file",
            "node_service_invalid_executable",
        ) from None
    if int(lexical_lstat.st_uid) != 0:
        raise BridgeError(
            "Root Node service executable must be root-owned",
            "node_service_invalid_executable",
        )
    # Follow the symlink chain actually taken, validating each target.
    # Every traversed hop is collected so each hop's parent chain is checked.
    current = lexical_path
    traversed: list[Path] = [lexical_path]
    seen: set[str] = set()
    for _ in range(_MAX_EXE_LINK_DEPTH + 1):
        try:
            current_lstat = do_lstat(current)
        except OSError:
            raise BridgeError(
                "Node executable must be an existing regular executable file",
                "node_service_invalid_executable",
            ) from None
        if int(current_lstat.st_uid) != 0:
            raise BridgeError(
                "Root Node service executable must be root-owned",
                "node_service_invalid_executable",
            )
        if not stat.S_ISLNK(current_lstat.st_mode):
            break
        key = str(current)
        if key in seen:
            raise BridgeError("Node executable symlink loop is unsafe", "node_service_invalid_executable")
        seen.add(key)
        try:
            link_target = os.readlink(current)
        except OSError:
            raise BridgeError(
                "Node executable must be an existing regular executable file",
                "node_service_invalid_executable",
            ) from None
        next_path = Path(link_target)
        if not next_path.is_absolute():
            next_path = current.parent / next_path
        current = Path(os.path.normpath(str(next_path)))
        traversed.append(current)
    else:
        raise BridgeError("Node executable symlink chain is too deep", "node_service_invalid_executable")
    try:
        final_stat = do_stat(current)
    except OSError:
        raise BridgeError(
            "Node executable must be an existing regular executable file",
            "node_service_invalid_executable",
        ) from None
    if not stat.S_ISREG(final_stat.st_mode):
        raise BridgeError(
            "Node executable must be an existing regular executable file",
            "node_service_invalid_executable",
        )
    if int(final_stat.st_uid) != 0:
        raise BridgeError(
            "Root Node service executable must be root-owned",
            "node_service_invalid_executable",
        )
    if stat.S_IMODE(final_stat.st_mode) & 0o022:
        raise BridgeError(
            "Root Node service executable must not be group/world-writable",
            "node_service_invalid_executable",
        )
    if final_stat.st_mode & 0o111 == 0:
        raise BridgeError(
            "Node executable must be an existing regular executable file",
            "node_service_invalid_executable",
        )
    # Parent chains for every traversed hop (lexical entry, each
    # intermediate symlink, final target) must be root-owned and not
    # writable: otherwise an unprivileged user could replace the executable,
    # a symlink hop, or a path component. Anchors are deduplicated.
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
                raise BridgeError("Node executable location is unsafe", "node_service_invalid_executable") from None
            if not stat.S_ISDIR(directory_lstat.st_mode):
                raise BridgeError("Node executable location is unsafe", "node_service_invalid_executable")
            if int(directory_lstat.st_uid) != 0:
                raise BridgeError(
                    "Root Node service executable must live on a root-controlled path",
                    "node_service_invalid_executable",
                )
            if stat.S_IMODE(directory_lstat.st_mode) & 0o022:
                raise BridgeError(
                    "Root Node service executable must not live on a writable path",
                    "node_service_invalid_executable",
                )
            parent = directory.parent
            if parent == directory:
                break
            directory = parent
        else:
            raise BridgeError("Node executable location is unsafe", "node_service_invalid_executable")
    return lexical_path


# ---------------------------------------------------------------------------
# Strict systemd ExecStart encoding (no shell)
# ---------------------------------------------------------------------------

_SAFE_UNQUOTED_RE = re.compile(r"^[A-Za-z0-9_+\-./:=@,]+$")


def _encode_systemd_arg(value: str) -> str:
    """Encode one argv element for systemd ``ExecStart`` without a shell."""
    if not isinstance(value, str) or value == "":
        raise BridgeError("Systemd ExecStart argument is invalid", "node_service_invalid_argument")
    if "\x00" in value or "\n" in value or "\r" in value:
        raise BridgeError("Systemd ExecStart argument contains unsafe characters", "node_service_invalid_argument")
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise BridgeError(
            "Systemd ExecStart argument contains unsafe control characters",
            "node_service_invalid_argument",
        )
    escaped = value.replace("%", "%%").replace("$", "$$")
    if _SAFE_UNQUOTED_RE.fullmatch(escaped):
        return escaped
    inner = escaped.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{inner}"'


def build_systemd_unit(
    state: str | os.PathLike[str],
    program: Sequence[str] | str | os.PathLike[str],
    *,
    user: str,
    group: str,
) -> bytes:
    """Build deterministic system unit bytes with no secrets or roots."""
    prefix = systemd_program(program) if isinstance(program, (str, os.PathLike)) else _validate_systemd_prefix(list(program))
    user = _validate_systemd_name(user, kind="User")
    group = _validate_systemd_name(group, kind="Group")
    state_path = _absolute_path(state)
    if not state_path.is_absolute():
        raise BridgeError("Node state must be an absolute path", "node_state_unsafe")
    full_argv = [*prefix, "node", "--state", str(state_path), "serve"]
    encoded = " ".join(_encode_systemd_arg(part) for part in full_argv)
    text = (
        "[Unit]\n"
        "Description=Workspace Bridge Node\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"User={user}\n"
        f"Group={group}\n"
        f"ExecStart={encoded}\n"
        "Restart=on-failure\n"
        "RestartSec=10\n"
        "UMask=0077\n"
        "\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
    )
    return text.encode("utf-8")


def render_systemd_unit(
    state: str | os.PathLike[str],
    program: Sequence[str] | str | os.PathLike[str],
    *,
    user: str,
    group: str,
) -> bytes:
    return build_systemd_unit(state, program, user=user, group=group)


# ---------------------------------------------------------------------------
# State-local manifest + system-unit file helpers
# ---------------------------------------------------------------------------

def _manifest_for(
    paths: SystemdServicePaths,
    unit: bytes,
    *,
    user: str,
    group: str,
    uid: int,
    gid: int,
) -> bytes:
    value = {
        "schema_version": 1,
        "state_path": str(paths.state),
        "unit": UNIT_NAME,
        "unit_path": str(paths.unit),
        "unit_sha256": hashlib.sha256(unit).hexdigest(),
        "user": user,
        "group": group,
        "uid": uid,
        "gid": gid,
    }
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _ownership_mismatch_message(run_uid: int) -> str:
    if run_uid == 0:
        return (
            "Node state is not root-owned; a root service requires an intentionally "
            "root-owned Node state initialized and managed as root (running with sudo "
            "against a user-owned state is rejected, never converted)"
        )
    return "Node state is not owned by the current user"


def _check_dir_as(
    path: Path,
    *,
    name: str,
    mode: int | None,
    owner_uid: int,
    stat_fn: Callable[[Path], os.stat_result] | None = None,
) -> None:
    # Ownership isolation: the state directory must belong to the run-as
    # identity. A root invocation on user-owned state (and vice versa) fails
    # closed here; state is never chown/migrated automatically.
    if path.is_symlink():
        raise _state_error(f"{name} must not be a symlink")
    try:
        st = stat_fn(path) if stat_fn is not None else path.stat()
    except FileNotFoundError:
        raise _state_error(f"{name} is not initialized", "node_uninitialized") from None
    except OSError:
        raise _state_error(f"Unable to inspect {name}") from None
    if not stat.S_ISDIR(st.st_mode) or int(st.st_uid) != int(owner_uid):
        if int(st.st_uid) != int(owner_uid):
            raise _state_error(_ownership_mismatch_message(owner_uid))
        raise _state_error(f"{name} must be a directory owned by the service user")
    if mode is not None and stat.S_IMODE(st.st_mode) != mode:
        raise _state_error(f"{name} must have mode {mode:04o}")


def _check_file_as(
    path: Path,
    *,
    name: str,
    owner_uid: int,
    required: bool = True,
    stat_fn: Callable[[Path], os.stat_result] | None = None,
) -> bool:
    if path.is_symlink():
        raise _state_error(f"{name} must not be a symlink")
    if not path.exists():
        if required:
            raise _state_error(f"{name} is missing", "node_uninitialized")
        return False
    try:
        st = stat_fn(path) if stat_fn is not None else path.stat()
    except FileNotFoundError:
        if required:
            raise _state_error(f"{name} is missing", "node_uninitialized") from None
        return False
    except OSError:
        raise _state_error(f"Unable to inspect {name}") from None
    if not stat.S_ISREG(st.st_mode) or int(st.st_uid) != int(owner_uid):
        if int(st.st_uid) != int(owner_uid):
            raise _state_error(_ownership_mismatch_message(owner_uid))
        raise _state_error(f"{name} must be a regular file owned by the service user")
    if stat.S_IMODE(st.st_mode) != PRIVATE_FILE_MODE:
        raise _state_error(f"{name} must have mode 0600")
    return True


def _validate_state_as(
    state: Path,
    owner_uid: int,
    *,
    stat_fn: Callable[[Path], os.stat_result] | None = None,
) -> tuple[dict, str]:
    """Validate initialized state owned by the run-as identity."""
    from .node_launchd import _validate_config as _launchd_validate_config
    from .security import digest as _digest

    _check_dir_as(state, name="Node state", mode=STATE_MODE, owner_uid=owner_uid, stat_fn=stat_fn)
    try:
        fd = open_absolute_dir(str(state))
        os.close(fd)
    except BridgeError:
        raise _state_error("Node state path is not a safe directory") from None
    _check_file_as(state / "node-config.json", name="node-config.json", owner_uid=owner_uid, stat_fn=stat_fn)
    try:
        config = json.loads((state / "node-config.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        raise _state_error("Node configuration is not valid JSON", "node_config_invalid") from None
    config = _launchd_validate_config(config, state, require_roots=True)
    for extra in ("node.sqlite3", "node.sqlite3-wal", "node.sqlite3-shm"):
        _check_file_as(state / extra, name=extra, owner_uid=owner_uid, required=False, stat_fn=stat_fn)
    _check_file_as(state / "node-token", name="node-token", owner_uid=owner_uid, stat_fn=stat_fn)
    try:
        token = (state / "node-token").read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        raise _state_error("Node token cannot be read") from None
    if not token or len(token) > 512 or _digest(token.encode()) != config["node_token_hash"]:
        raise _state_error("Node token does not match Node configuration", "node_config_invalid")
    return config, token


def _config_status_as(
    state: Path,
    owner_uid: int,
    *,
    stat_fn: Callable[[Path], os.stat_result] | None = None,
) -> tuple[dict | None, str | None, str | None]:
    from .node_launchd import _validate_config as _launchd_validate_config
    from .security import digest as _digest

    try:
        _check_dir_as(state, name="Node state", mode=STATE_MODE, owner_uid=owner_uid, stat_fn=stat_fn)
        fd = open_absolute_dir(str(state))
        os.close(fd)
        _check_file_as(state / "node-config.json", name="node-config.json", owner_uid=owner_uid, stat_fn=stat_fn)
        config = _launchd_validate_config(
            json.loads((state / "node-config.json").read_text(encoding="utf-8")), state, require_roots=False
        )
        for extra in ("node.sqlite3", "node.sqlite3-wal", "node.sqlite3-shm"):
            _check_file_as(state / extra, name=extra, owner_uid=owner_uid, required=False, stat_fn=stat_fn)
        _check_file_as(state / "node-token", name="node-token", owner_uid=owner_uid, stat_fn=stat_fn)
        token = (state / "node-token").read_text(encoding="utf-8").strip()
        if not token or len(token) > 512 or _digest(token.encode()) != config["node_token_hash"]:
            raise _state_error("Node token does not match Node configuration", "node_config_invalid")
    except BridgeError as exc:
        return None, None, exc.code
    except (OSError, UnicodeError, ValueError):
        return None, None, "node_config_invalid"
    return config, token, None


def _read_manifest_as(
    paths: SystemdServicePaths,
    owner_uid: int,
    *,
    stat_fn: Callable[[Path], os.stat_result] | None = None,
) -> dict | None:
    if paths.manifest.is_symlink():
        raise _state_error("systemd manifest must not be a symlink", "node_service_conflict")
    if not paths.manifest.exists():
        return None
    try:
        st = stat_fn(paths.manifest) if stat_fn is not None else paths.manifest.stat()
    except OSError:
        raise _state_error("Unable to inspect systemd manifest") from None
    if not stat.S_ISREG(st.st_mode) or int(st.st_uid) != int(owner_uid):
        if int(st.st_uid) != int(owner_uid):
            raise _state_error(_ownership_mismatch_message(owner_uid))
        raise _state_error("systemd manifest must be owned by the service user", "node_service_conflict")
    try:
        value = json.loads(paths.manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        raise _state_error("systemd manifest is invalid", "node_service_conflict") from None
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != 1
        or value.get("unit") != UNIT_NAME
        or value.get("unit_path") != str(paths.unit)
        or value.get("state_path") != str(paths.state)
        or not isinstance(value.get("unit_sha256"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", value["unit_sha256"])
        or not isinstance(value.get("uid"), int)
        or not isinstance(value.get("gid"), int)
        or not isinstance(value.get("user"), str)
        or not isinstance(value.get("group"), str)
    ):
        raise _state_error("systemd manifest is invalid", "node_service_conflict")
    try:
        _validate_systemd_name(value["user"], kind="User")
        _validate_systemd_name(value["group"], kind="Group")
    except BridgeError:
        raise _state_error("systemd manifest is invalid", "node_service_conflict") from None
    return value


def _write_manifest_as(
    paths: SystemdServicePaths,
    content: bytes,
    owner_uid: int,
    *,
    overwrite: bool = False,
    stat_fn: Callable[[Path], os.stat_result] | None = None,
) -> None:
    if paths.manifest.is_symlink():
        raise _state_error("systemd manifest must not be a symlink", "node_service_conflict")
    if paths.manifest.exists():
        if not overwrite:
            raise _state_error("Refusing to overwrite existing manifest", "node_service_conflict")
        try:
            st = stat_fn(paths.manifest) if stat_fn is not None else paths.manifest.stat()
        except OSError:
            raise _state_error("Unable to inspect systemd manifest") from None
        if not stat.S_ISREG(st.st_mode) or int(st.st_uid) != int(owner_uid):
            if int(st.st_uid) != int(owner_uid):
                raise _state_error(_ownership_mismatch_message(owner_uid))
            raise _state_error("systemd manifest must be owned by the service user", "node_service_conflict")
    paths.manifest.parent.mkdir(mode=STATE_MODE, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{paths.manifest.name}.", dir=paths.manifest.parent)
    temporary_path = Path(temporary)
    try:
        os.chmod(temporary_path, PRIVATE_FILE_MODE)
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, paths.manifest)
        os.chmod(paths.manifest, PRIVATE_FILE_MODE)
    except OSError:
        raise _state_error("Unable to write private systemd manifest") from None
    finally:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass


def _read_system_unit(paths: SystemdServicePaths, *, expected_owner_uid: int = 0, stat_fn: Callable[[Path], os.stat_result] | None = None) -> bytes | None:
    """Read the fixed system unit, enforcing root-owned non-writable trust.

    Never trusts a user-owned system unit. Symlink, non-regular,
    non-root-owned, or group/world-writable files fail closed as conflicts
    and are never overwritten.
    """
    if paths.unit.is_symlink():
        raise _state_error("managed systemd unit must not be a symlink", "node_service_conflict")
    if not paths.unit.exists():
        return None
    try:
        st = stat_fn(paths.unit) if stat_fn is not None else paths.unit.stat()
    except OSError:
        raise _state_error("Unable to inspect managed systemd unit") from None
    if not stat.S_ISREG(st.st_mode):
        raise _state_error("managed systemd unit must be a regular file", "node_service_conflict")
    if int(st.st_uid) != int(expected_owner_uid):
        raise _state_error("managed systemd unit must be root-owned", "node_service_conflict")
    if stat.S_IMODE(st.st_mode) & 0o022:
        raise _state_error("managed systemd unit must not be group/world-writable", "node_service_conflict")
    try:
        return paths.unit.read_bytes()
    except OSError:
        raise _state_error("Unable to read managed systemd unit") from None


def _safe_command_detail(result: CommandResult) -> str:
    raw = (result.stderr or result.stdout or "").strip().splitlines()
    if not raw:
        return ""
    detail = raw[0][:MAX_COMMAND_DETAIL]
    for pattern in (
        r"(?i)(?:token|secret|password|authorization)[^\s:=]*\s*[:=]\s*[^\s]+",
        r"(?i)(?:environment|env)\s*[:=].*",
    ):
        detail = re.sub(pattern, "[REDACTED]", detail)
    return detail


def _is_privilege_error(stderr: str, error: str | None) -> bool:
    if error in {"sudo_not_found", "sudo_timeout", "sudo_unavailable"}:
        return True
    lowered = (stderr or "").lower()
    return any(
        fragment in lowered
        for fragment in (
            "sudo",
            "sorry, user",
            "not in the sudoers",
            "no tty present",
            "no askpass",
            "authentication failure",
            "incorrect password",
            "permission denied",
            "operation not permitted",
            "must be run as",
        )
    )


def _is_systemd_unavailable(stderr: str, error: str | None) -> bool:
    if error in {"systemctl_not_found", "systemctl_timeout", "systemctl_unavailable"}:
        return True
    lowered = (stderr or "").lower()
    return any(
        fragment in lowered
        for fragment in (
            "system has not been booted",
            "failed to connect to bus",
            "failed to get bus",
            "no such bus",
            "dbus",
            "bus connection",
            "systemd",
            "failed to connect to",
        )
    )


def _resolve_fixed_binary(candidates: Sequence[str], *, override: str | os.PathLike[str] | None) -> Path:
    if override is not None:
        path = Path(os.fspath(override))
        if not path.is_absolute():
            raise BridgeError("Privileged helper path must be absolute", "node_service_privilege_unavailable")
        return path
    for candidate in candidates:
        path = Path(candidate)
        try:
            if path.is_absolute() and path.is_file() and os.access(path, os.X_OK):
                return path
        except OSError:
            continue
    raise BridgeError("Required system helper is unavailable", "node_service_systemd_unavailable")


# ---------------------------------------------------------------------------
# System manager (fixed sudo/systemctl, no shell, system scope only)
# ---------------------------------------------------------------------------

class SystemdManager:
    """Injectable wrapper around fixed system ``systemctl`` operations.

    Status uses the unprivileged absolute ``systemctl`` binary directly.
    Non-root mutations prefix the fixed helper argv with the fixed absolute
    ``sudo`` binary; root (EUID 0) executes the same fixed helper argv
    directly without ``sudo``. No shell is ever used. System scope only;
    it never touches per-user unit directories and never calls
    session persistence helpers.
    """

    def __init__(
        self,
        *,
        runner: Callable[..., object] | None = None,
        platform_name: str | None = None,
        sudo_bin: str | os.PathLike[str] | None = None,
        systemctl_bin: str | os.PathLike[str] | None = None,
        install_bin: str | os.PathLike[str] | None = None,
        rm_bin: str | os.PathLike[str] | None = None,
        unit_path: str | os.PathLike[str] | None = None,
        ids_provider: Callable[[], tuple[str, int, str, int]] | None = None,
        euid: int | None = None,
        unit_owner_uid: int | None = None,
        stat_fn: Callable[[Path], os.stat_result] | None = None,
        lstat_fn: Callable[[Path], os.stat_result] | None = None,
    ):
        self.runner = runner or subprocess.run
        self.platform_name = platform_name or platform.system()
        self._sudo_override = sudo_bin
        self._systemctl_override = systemctl_bin
        self._install_override = install_bin
        self._rm_override = rm_bin
        self._unit_override = Path(os.fspath(unit_path)) if unit_path is not None else None
        if self._unit_override is not None and not self._unit_override.is_absolute():
            raise BridgeError("System unit path override must be absolute", "node_service_invalid_argument")
        self._ids_provider = ids_provider
        self._euid_override = euid
        # None means strict production requirement: root-owned (0).
        # Tests inject os.getuid() to exercise tmp files without real /etc.
        self._unit_owner_override = unit_owner_uid
        # Test-only filesystem hooks for simulated root-owned trees.
        self._stat_fn = stat_fn
        self._lstat_fn = lstat_fn

    def require_linux(self) -> None:
        if self.platform_name != "Linux":
            raise BridgeError(
                "workspace-bridge node service commands require Linux systemd "
                "on this host (macOS uses launchd)",
                "node_service_unsupported_platform",
            )

    def _euid(self) -> int:
        if self._euid_override is not None:
            return int(self._euid_override)
        geteuid = getattr(os, "geteuid", None)
        if geteuid is not None:
            try:
                return int(geteuid())
            except OSError:
                pass
        return int(os.getuid())

    def _stat(self, path: Path) -> os.stat_result:
        if self._stat_fn is not None:
            return self._stat_fn(path)
        return Path(path).stat()

    def _lstat(self, path: Path) -> os.stat_result:
        if self._lstat_fn is not None:
            return self._lstat_fn(path)
        return os.lstat(path)

    def is_root(self) -> bool:
        return self._euid() == 0

    def paths(self, state: str | os.PathLike[str]) -> SystemdServicePaths:
        state_path = _absolute_path(state)
        unit = self._unit_override if self._unit_override is not None else UNIT_PATH
        return SystemdServicePaths(state=state_path, unit=unit, manifest=state_path / MANIFEST_NAME)

    def expected_unit_owner(self) -> int:
        if self._unit_owner_override is not None:
            return int(self._unit_owner_override)
        return 0

    def current_ids(self) -> tuple[str, int, str, int]:
        if self._ids_provider is not None:
            user, uid, group, gid = self._ids_provider()
            return (
                _validate_systemd_name(user, kind="User"),
                int(uid),
                _validate_systemd_name(group, kind="Group"),
                int(gid),
            )
        return _default_current_ids()

    def _sudo(self) -> Path:
        try:
            return _resolve_fixed_binary(_SUDO_CANDIDATES, override=self._sudo_override)
        except BridgeError as exc:
            if exc.code == "node_service_systemd_unavailable":
                raise BridgeError(
                    "sudo is unavailable; install system packages/polkit membership as the host admin, "
                    "or use foreground `workspace-bridge node --state <state> serve`.",
                    "node_service_privilege_unavailable",
                ) from None
            raise

    def _systemctl(self) -> Path:
        try:
            return _resolve_fixed_binary(_SYSTEMCTL_CANDIDATES, override=self._systemctl_override)
        except BridgeError:
            raise BridgeError(
                "systemctl is unavailable; boot with systemd or use foreground "
                "`workspace-bridge node --state <state> serve`.",
                "node_service_systemd_unavailable",
            ) from None

    def _install(self) -> Path:
        try:
            return _resolve_fixed_binary(_INSTALL_CANDIDATES, override=self._install_override)
        except BridgeError:
            raise BridgeError(
                "System install helper is unavailable; foreground "
                "`workspace-bridge node --state <state> serve` remains available.",
                "node_service_privilege_unavailable",
            ) from None

    def _rm(self) -> Path:
        try:
            return _resolve_fixed_binary(_RM_CANDIDATES, override=self._rm_override)
        except BridgeError:
            raise BridgeError(
                "System remove helper is unavailable.",
                "node_service_privilege_unavailable",
            ) from None

    def _run(self, argv: Sequence[str], *, timeout: int) -> CommandResult:
        try:
            completed = self.runner(argv, capture_output=True, text=True, check=False, timeout=timeout)
        except FileNotFoundError:
            return CommandResult(None, error="command_not_found")
        except subprocess.TimeoutExpired:
            return CommandResult(None, error="command_timeout")
        except OSError:
            return CommandResult(None, error="command_unavailable")
        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
        if isinstance(stdout, bytes):
            stdout = stdout.decode("utf-8", "replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", "replace")
        return CommandResult(int(completed.returncode), stdout[:MAX_COMMAND_OUTPUT], stderr[:MAX_COMMAND_OUTPUT])

    def _privilege_error(self, action: str, result: CommandResult) -> BridgeError:
        detail = _safe_command_detail(result)
        suffix = f": {detail}" if detail else ""
        return BridgeError(
            f"Privileged systemd administration unavailable for {action}{suffix}. "
            "Run the plain service command as the Node owner and authorize the sudo "
            "prompt, or use foreground `workspace-bridge node --state <state> serve`.",
            "node_service_privilege_unavailable",
        )

    def _systemd_error(self, action: str, result: CommandResult) -> BridgeError:
        detail = _safe_command_detail(result)
        suffix = f": {detail}" if detail else ""
        return BridgeError(
            f"systemd unavailable for {action}{suffix}. "
            "Boot with systemd or use foreground `workspace-bridge node --state <state> serve`.",
            "node_service_systemd_unavailable",
        )

    def _expect_privileged_success(self, action: str, result: CommandResult) -> None:
        if result.succeeded:
            return
        if result.error is not None and result.error != "command_not_found":
            # Timeout/unavailable without output: distinguish sudo vs systemd by action context.
            # Privileged wrapper failures default to privilege-unavailable unless clearly systemd.
            if _is_systemd_unavailable(result.stderr, None) and not _is_privilege_error(result.stderr, result.error):
                raise self._systemd_error(action, result)
            raise self._privilege_error(action, result)
        if _is_privilege_error(result.stderr, result.error):
            raise self._privilege_error(action, result)
        if _is_systemd_unavailable(result.stderr, result.error):
            raise self._systemd_error(action, result)
        detail = _safe_command_detail(result)
        suffix = f": {detail}" if detail else ""
        code = result.returncode if result.returncode is not None else "unavailable"
        raise BridgeError(f"systemd {action} failed (exit {code}){suffix}", "node_service_systemctl_failed")

    # -- unprivileged status -------------------------------------------------
    def show(self) -> dict:
        self.require_linux()
        systemctl = self._systemctl()
        result = self._run(
            [str(systemctl), "show", UNIT_NAME, "--property=LoadState,ActiveState,SubState,UnitFileState", "--no-pager"],
            timeout=SYSTEMCTL_TIMEOUT,
        )
        if result.error is not None or (result.returncode != 0 and _is_systemd_unavailable(result.stderr, result.error)):
            return {"available": False, "error": result.error or "manager_unavailable"}
        if not result.succeeded:
            detail = _safe_command_detail(result)
            entry: dict = {"available": False, "error": "show_failed"}
            if detail:
                entry["detail"] = detail
            return entry
        values: dict = {"available": True}
        for line in (result.stdout or "").splitlines():
            if "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip()
            if key in {"LoadState", "ActiveState", "SubState", "UnitFileState"}:
                values[key] = val[:64]
        for key in ("LoadState", "ActiveState", "SubState", "UnitFileState"):
            values.setdefault(key, "unknown")
        return values

    # -- privileged mutations (fixed helpers, no shell) ----------------------
    # Non-root executes fixed helper argv via the fixed absolute sudo binary.
    # Root (EUID 0) executes the same fixed helper argv directly and never
    # requires sudo to be installed.
    def _sudo_run(self, action: str, argv: Sequence[str]) -> None:
        sudo = self._sudo()
        full = [str(sudo), *[str(part) for part in argv]]
        result = self._run(full, timeout=SUDO_TIMEOUT)
        # Map missing sudo binary itself to privilege-unavailable.
        if result.error == "command_not_found":
            raise self._privilege_error(action, result)
        self._expect_privileged_success(action, result)

    def _privileged_run(self, action: str, argv: Sequence[str]) -> None:
        if self.is_root():
            result = self._run([str(part) for part in argv], timeout=SUDO_TIMEOUT)
            if result.error == "command_not_found":
                if "systemctl" in str(argv[0]):
                    raise self._systemd_error(action, result)
                raise self._privilege_error(action, result)
            self._expect_privileged_success(action, result)
            return
        self._sudo_run(action, argv)

    def privileged_install_unit(self, *, temp_path: Path, dest: Path) -> None:
        self.require_linux()
        install = self._install()
        if dest != (self._unit_override if self._unit_override is not None else UNIT_PATH):
            raise _state_error("Refusing to install outside the fixed unit path", "node_service_conflict")
        self._privileged_run(
            "install-unit",
            [str(install), "-o", "root", "-g", "root", "-m", "0644", "--", str(temp_path), str(dest)],
        )

    def privileged_remove_unit(self, *, dest: Path) -> None:
        self.require_linux()
        rm = self._rm()
        if dest != (self._unit_override if self._unit_override is not None else UNIT_PATH):
            raise _state_error("Refusing to remove outside the fixed unit path", "node_service_conflict")
        self._privileged_run("remove-unit", [str(rm), "--", str(dest)])

    def daemon_reload(self) -> None:
        self.require_linux()
        systemctl = self._systemctl()
        self._privileged_run("daemon-reload", [str(systemctl), "daemon-reload"])

    def enable_now(self) -> None:
        self.require_linux()
        systemctl = self._systemctl()
        self._privileged_run("enable --now", [str(systemctl), "enable", "--now", UNIT_NAME])

    def start(self) -> None:
        self.require_linux()
        systemctl = self._systemctl()
        self._privileged_run("start", [str(systemctl), "start", UNIT_NAME])

    def stop(self) -> None:
        self.require_linux()
        systemctl = self._systemctl()
        self._privileged_run("stop", [str(systemctl), "stop", UNIT_NAME])

    def restart(self) -> None:
        self.require_linux()
        systemctl = self._systemctl()
        self._privileged_run("restart", [str(systemctl), "restart", UNIT_NAME])

    def disable_now(self) -> None:
        self.require_linux()
        systemctl = self._systemctl()
        self._privileged_run("disable --now", [str(systemctl), "disable", "--now", UNIT_NAME])


# Backwards-incompatible rename guard: the user-unit manager is removed.
# Linux is system-manager only; no per-user manager alias is kept.


# ---------------------------------------------------------------------------
# Status + lifecycle (run-as-aware validation; see _validate_state_as)
# ---------------------------------------------------------------------------

def _parse_enabled(show: dict) -> str:
    if not show.get("available"):
        return "unavailable"
    value = str(show.get("UnitFileState", "unknown"))
    if value in {"enabled", "enabled-runtime"}:
        return "enabled"
    if value in {"disabled", "masked", "masked-runtime"}:
        return "disabled"
    if value in {"static", "indirect", "generated", "transient", "linked", "linked-runtime"}:
        return value
    if value in {"not-found", "bad-setting", "unknown"}:
        return value if value != "unknown" else "unknown"
    return "unknown"


def _high_level_state(*, installed: bool, unit_state: str, show: dict) -> tuple[str, bool]:
    if not installed:
        if unit_state in {"unmanaged", "modified"}:
            return unit_state, False
        if unit_state == "unsafe":
            return "unsafe", False
        return "not_installed", False
    if not show.get("available"):
        return "unavailable", False
    load = str(show.get("LoadState", "unknown"))
    active = str(show.get("ActiveState", "unknown"))
    sub = str(show.get("SubState", "unknown"))
    if load == "not-found":
        return "not_found", False
    if load not in {"loaded"}:
        return "inactive" if active == "inactive" else load, False
    if active == "active" and sub == "running":
        return "running", True
    if active == "failed" or sub == "failed":
        return "failed", False
    if active == "active":
        return "active", sub == "running"
    if active in {"inactive", "deactivating", "activating", "reloading"}:
        return "inactive" if active == "inactive" else active, False
    return active if active != "unknown" else "unknown", False


def service_status(
    state: str | os.PathLike[str],
    *,
    manager: SystemdManager | None = None,
    probe: bool = True,
) -> dict:
    from .node_launchd import _format_endpoint as _launchd_endpoint
    from .node_launchd import probe_node_status as _launchd_probe

    manager = manager or SystemdManager()
    manager.require_linux()
    paths = manager.paths(state)
    try:
        _run_user, _run_uid, _run_group, _run_gid = manager.current_ids()
    except BridgeError as exc:
        return {
            "service_manager": "systemd",
            "unit": UNIT_NAME,
            "state_path": str(paths.state),
            "installed": False,
            "unit_state": "unsafe",
            "enabled": "unavailable",
            "systemd": {
                "available": False,
                "active_state": "unknown",
                "sub_state": "unknown",
                "load_state": "unknown",
                "unit_file_state": "unknown",
            },
            "state": "unsafe",
            "running": False,
            "health": {"status": "unavailable", "code": exc.code},
            "listen": None,
        }
    stat_fn = manager._stat
    config, token, config_error = _config_status_as(paths.state, _run_uid, stat_fn=stat_fn)
    unit_exists = paths.unit.exists() or paths.unit.is_symlink()
    installed = False
    unit_state = "missing"
    if unit_exists:
        try:
            raw_unit = _read_system_unit(paths, expected_owner_uid=manager.expected_unit_owner(), stat_fn=stat_fn)
            manifest = _read_manifest_as(paths, _run_uid, stat_fn=stat_fn)
            if manifest and raw_unit is not None:
                installed = manifest["unit_sha256"] == hashlib.sha256(raw_unit).hexdigest()
                unit_state = "managed" if installed else "modified"
            else:
                unit_state = "unmanaged"
        except BridgeError as exc:
            if exc.code == "node_service_conflict":
                unit_state = "unmanaged"
            else:
                unit_state = "unsafe"
    show = manager.show()
    enabled = _parse_enabled(show)
    level, running = _high_level_state(installed=installed, unit_state=unit_state, show=show)
    if not installed and unit_state in {"unmanaged", "modified", "unsafe"}:
        level = unit_state
        running = False
    if config is None:
        health: dict = {"status": "unavailable", "code": config_error or "node_uninitialized"}
    elif not probe:
        health = {"status": "not_probed", "code": "not_probed"}
    else:
        health = _launchd_probe(config, token or "")
    result: dict = {
        "service_manager": "systemd",
        "unit": UNIT_NAME,
        "state_path": str(paths.state),
        "installed": installed,
        "unit_state": unit_state,
        "enabled": enabled,
        "systemd": {
            "available": bool(show.get("available", False)),
            "active_state": str(show.get("ActiveState", "unknown"))[:64],
            "sub_state": str(show.get("SubState", "unknown"))[:64],
            "load_state": str(show.get("LoadState", "unknown"))[:64],
            "unit_file_state": str(show.get("UnitFileState", "unknown"))[:64],
        },
        "state": level,
        "running": running,
        "health": health,
    }
    if config is not None:
        result["listen"] = {
            "host": config["host"],
            "port": config["port"],
            "endpoint": _launchd_endpoint(config["host"], config["port"]),
        }
    else:
        result["listen"] = None
    return result


def _record_action(action: str, state: Path, manager: SystemdManager) -> dict:
    value = service_status(state, manager=manager, probe=False)
    value["action"] = action
    return value


def install_service(
    state: str | os.PathLike[str],
    *,
    manager: SystemdManager | None = None,
    executable: str | os.PathLike[str] | None = None,
) -> dict:
    manager = manager or SystemdManager()
    manager.require_linux()
    paths = manager.paths(state)
    if paths.unit != (manager._unit_override if manager._unit_override is not None else UNIT_PATH):
        raise _state_error("Refusing to install outside the fixed unit path", "node_service_conflict")
    user, uid, group, gid = manager.current_ids()
    stat_fn = manager._stat
    # Ownership isolation: root on user-owned state (and vice versa) fails
    # closed here; state is never chown/migrated automatically.
    _validate_state_as(paths.state, uid, stat_fn=stat_fn)
    program = systemd_program(executable)
    if uid == 0:
        # A root service executes ExecStart as uid 0: the lexical shim,
        # every followed target, the final executable, and both parent
        # chains must be root-controlled.
        _validate_root_executable_trust(program[0], stat_fn=stat_fn, lstat_fn=manager._lstat)
    try:
        fd = open_absolute_dir(str(paths.state))
        os.close(fd)
    except BridgeError:
        raise _state_error("Node state path is not a safe directory") from None
    desired = build_systemd_unit(paths.state, program, user=user, group=group)
    desired_hash = hashlib.sha256(desired).hexdigest()
    # Existing unit handling (fail closed, never overwrite unmanaged).
    try:
        existing = _read_system_unit(paths, expected_owner_uid=manager.expected_unit_owner(), stat_fn=stat_fn)
    except BridgeError as exc:
        if exc.code == "node_service_conflict":
            # Symlink/non-regular/non-root/writable all map to conflict here.
            raise
        raise
    try:
        manifest = _read_manifest_as(paths, uid, stat_fn=stat_fn)
    except BridgeError:
        raise
    if existing is not None and manifest is not None and manifest["unit_sha256"] != hashlib.sha256(existing).hexdigest():
        raise _state_error("systemd manifest does not match its unit", "node_service_conflict")
    if existing is not None:
        # Unit present: require exact desired identity.
        if manifest is None:
            raise _state_error("Refusing to overwrite a systemd unit without a matching manifest", "node_service_conflict")
        if (
            manifest.get("state_path") != str(paths.state)
            or manifest.get("unit_path") != str(paths.unit)
            or manifest.get("user") != user
            or manifest.get("group") != group
            or manifest.get("uid") != uid
            or manifest.get("gid") != gid
        ):
            raise _state_error("Managed systemd unit identity differs; uninstall first", "node_service_conflict")
        if existing != desired or manifest.get("unit_sha256") != desired_hash:
            if manifest.get("unit_sha256") == hashlib.sha256(existing).hexdigest() and existing != desired:
                raise _state_error("Managed systemd unit differs from the requested configuration; uninstall first", "node_service_conflict")
            raise _state_error("Refusing to overwrite an unrelated or modified systemd unit", "node_service_conflict")
        # Exact match: idempotent, ensure boot persistence without reinstall.
        manager.daemon_reload()
        manager.enable_now()
        return _record_action("install", paths.state, manager)
    # Unit absent.
    if manifest is not None:
        if (
            manifest.get("state_path") != str(paths.state)
            or manifest.get("unit_path") != str(paths.unit)
            or manifest.get("user") != user
            or manifest.get("group") != group
            or manifest.get("uid") != uid
            or manifest.get("gid") != gid
            or manifest.get("unit_sha256") != desired_hash
        ):
            raise _state_error("Existing manifest does not match the desired system unit; uninstall first", "node_service_conflict")
        # Partial retry: manifest already exactly matches desired; recreate unit file.
    else:
        _write_manifest_as(paths, _manifest_for(paths, desired, user=user, group=group, uid=uid, gid=gid), uid, overwrite=False, stat_fn=stat_fn)
    # Private temp bytes inside the private state dir, then fixed sudo install.
    fd, temporary = tempfile.mkstemp(prefix=".workspace-bridge-node.", suffix=".service", dir=paths.state)
    temporary_path = Path(temporary)
    try:
        os.chmod(temporary_path, PRIVATE_FILE_MODE)
        with os.fdopen(fd, "wb") as stream:
            stream.write(desired)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary_path, PRIVATE_FILE_MODE)
        manager.privileged_install_unit(temp_path=temporary_path, dest=paths.unit)
    finally:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass
    manager.daemon_reload()
    manager.enable_now()
    return _record_action("install", paths.state, manager)


def start_service(state: str | os.PathLike[str], *, manager: SystemdManager | None = None) -> dict:
    manager = manager or SystemdManager()
    manager.require_linux()
    paths = manager.paths(state)
    _validate_state_as(paths.state, manager.current_ids()[1], stat_fn=manager._stat)
    _require_managed_with_program(paths, manager)
    manager.start()
    return _record_action("start", paths.state, manager)


def stop_service(state: str | os.PathLike[str], *, manager: SystemdManager | None = None) -> dict:
    manager = manager or SystemdManager()
    manager.require_linux()
    paths = manager.paths(state)
    _stop_uid = manager.current_ids()[1]
    if (not paths.unit.exists() and not paths.unit.is_symlink()) and (_read_manifest_as(paths, _stop_uid, stat_fn=manager._stat) is None):
        return _record_action("stop", paths.state, manager)
    _validate_state_as(paths.state, _stop_uid, stat_fn=manager._stat)
    _require_managed_with_program(paths, manager)
    try:
        manager.stop()
    except BridgeError as exc:
        if exc.code in {"node_service_privilege_unavailable", "node_service_systemd_unavailable"}:
            raise
        show = manager.show()
        if show.get("available") and str(show.get("ActiveState")) == "inactive":
            pass
        else:
            raise
    return _record_action("stop", paths.state, manager)


def restart_service(state: str | os.PathLike[str], *, manager: SystemdManager | None = None) -> dict:
    manager = manager or SystemdManager()
    manager.require_linux()
    paths = manager.paths(state)
    _validate_state_as(paths.state, manager.current_ids()[1], stat_fn=manager._stat)
    _require_managed_with_program(paths, manager)
    manager.restart()
    return _record_action("restart", paths.state, manager)


def _require_managed_with_program(paths: SystemdServicePaths, manager: SystemdManager) -> dict:
    owner = manager.expected_unit_owner()
    stat_fn = manager._stat
    raw = _read_system_unit(paths, expected_owner_uid=owner, stat_fn=stat_fn)
    _req_user, _req_uid, _req_group, _req_gid = manager.current_ids()
    if raw is None:
        if _read_manifest_as(paths, _req_uid, stat_fn=stat_fn) is None:
            raise _state_error("Node systemd unit is not installed", "node_service_not_installed")
        raise _state_error("Refusing to operate on a missing systemd unit with a manifest", "node_service_conflict")
    manifest = _read_manifest_as(paths, _req_uid, stat_fn=stat_fn)
    if manifest is None:
        raise _state_error("Refusing to operate on an unmanaged systemd unit", "node_service_conflict")
    if manifest["unit_sha256"] != hashlib.sha256(raw).hexdigest():
        raise _state_error("systemd manifest does not match its unit", "node_service_conflict")
    user, uid, group, gid = manager.current_ids()
    if manifest.get("user") != user or manifest.get("group") != group or manifest.get("uid") != uid or manifest.get("gid") != gid:
        raise _state_error("Managed systemd unit identity differs from the current user", "node_service_conflict")
    # Re-render desired from the trusted manifest identity is not possible
    # without the executable; instead verify the on-disk unit at least carries
    # the expected User=/Group= lines for this identity (bounded check).
    text = raw.decode("utf-8", "replace")
    if f"\nUser={user}\n" not in text or f"\nGroup={group}\n" not in text:
        raise _state_error("Managed systemd unit differs from the current user", "node_service_conflict")
    return manifest


def uninstall_service(state: str | os.PathLike[str], *, manager: SystemdManager | None = None) -> dict:
    manager = manager or SystemdManager()
    manager.require_linux()
    paths = manager.paths(state)
    _un_user, _un_uid, _un_group, _un_gid = manager.current_ids()
    stat_fn = manager._stat
    if not paths.unit.exists() and not paths.unit.is_symlink():
        manifest = _read_manifest_as(paths, _un_uid, stat_fn=stat_fn)
        if manifest is None:
            return _record_action("uninstall", paths.state, manager)
        # Post-removal cleanup state: the managed unit is already gone but a
        # valid manifest for this exact run-as identity remains (e.g. a prior
        # uninstall removed the unit then failed at daemon-reload). Never
        # infer this from an invalid/foreign manifest: schema, fixed unit
        # path, state path, run-as identity, and state ownership/mode must
        # all match before completing reload + manifest cleanup. Disable and
        # remove are intentionally not retried here.
        if (
            manifest.get("state_path") != str(paths.state)
            or manifest.get("unit_path") != str(paths.unit)
            or manifest.get("user") != _un_user
            or manifest.get("group") != _un_group
            or manifest.get("uid") != _un_uid
            or manifest.get("gid") != _un_gid
        ):
            raise _state_error(
                "Existing manifest does not match this Node service identity; uninstall first",
                "node_service_conflict",
            )
        _validate_state_as(paths.state, _un_uid, stat_fn=stat_fn)
        manager.daemon_reload()
        try:
            paths.manifest.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            raise _state_error("Unable to remove the managed systemd manifest") from None
        return _record_action("uninstall", paths.state, manager)
    _require_managed_with_program(paths, manager)
    # Fixed disable --now (stop+disable), then remove only the exact unit.
    try:
        manager.disable_now()
    except BridgeError as exc:
        if exc.code in {"node_service_privilege_unavailable", "node_service_systemd_unavailable"}:
            raise
        show = manager.show()
        if not (show.get("available") and str(show.get("UnitFileState")) in {"disabled", "unknown"}):
            raise
    # Re-verify exact identity before destructive removal.
    _require_managed_with_program(paths, manager)
    manager.privileged_remove_unit(dest=paths.unit)
    manager.daemon_reload()
    try:
        paths.manifest.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        raise _state_error("Unable to remove the managed systemd manifest") from None
    return _record_action("uninstall", paths.state, manager)
