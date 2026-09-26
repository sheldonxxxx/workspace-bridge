"""Bounded native logging policy and package-owned macOS log guard (M4.3B).

Product policy:

- Docker: ``json-file`` rotation ``max-size 10m`` / ``max-file 3`` (Compose).
- macOS LaunchAgent-managed Node/adapter logs: private
  ``<state>/logs/{stdout,stderr}.log`` with 10 MiB target active-file
  limit plus two retained archives per stream (``.1`` / ``.2``) under a
  private (0700) log directory and private (0600) files. A package-owned
  no-shell log-guard child performs bounded copy-truncate rotation because
  launchd keeps stdout/stderr descriptors open. The guard is spawned only
  for managed LaunchAgents carrying the fixed nonsecret marker
  ``WB_NATIVE_LOG_GUARD=1`` on Darwin; foreground/manual ``serve`` without
  the marker never spawns the guard. The guard never supervises or restarts
  the service or runtime.
- Linux: systemd/journald-managed; Workspace Bridge does not modify
  host-global journal retention. Units declare ``StandardOutput=journal``
  and ``StandardError=journal`` explicitly. Support exports bound what is
  read (newest ~200 lines via fixed ``journalctl``).

The guard owns only ``<state>/logs/{stdout,stderr}.log`` and ``.1``/``.2``;
symlinks, non-regular files, wrong-owner files, non-private files, and
arbitrary paths are rejected. It emits nothing to the service logs (parent
spawns it with stdio to DEVNULL) and exits when its original parent service
process is gone/reparented. No token/config/workspace content is read.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import platform
import stat
import subprocess
import sys
import tempfile
import time
from typing import Callable

from .security import BridgeError


ACTIVE_LIMIT = 10 * 1024 * 1024
RETAIN_COUNT = 2
POLL_INTERVAL_SECONDS = 7.0
POLL_INTERVAL_MIN = 5.0
POLL_INTERVAL_MAX = 10.0
LOG_DIRNAME = "logs"
STDOUT_NAME = "stdout.log"
STDERR_NAME = "stderr.log"
GUARD_ENV = "WB_NATIVE_LOG_GUARD"
GUARD_VALUE = "1"
STATE_MODE = 0o700
FILE_MODE = 0o600

GUARD_MODULE = "workspace_bridge.native_logs"
GUARD_SUBCOMMAND = "guard"


def _absolute_state(value: str | os.PathLike[str]) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return Path(os.path.normpath(str(path)))


def log_paths(state: str | os.PathLike[str]) -> tuple[Path, Path, Path]:
    """Return (log_dir, stdout_log, stderr_log) for a state directory."""
    state_path = _absolute_state(state)
    log_dir = state_path / LOG_DIRNAME
    return log_dir, log_dir / STDOUT_NAME, log_dir / STDERR_NAME


def _current_uid() -> int | None:
    getuid = getattr(os, "getuid", None)
    return getuid() if getuid is not None else None


def _owned_by_current_user(st: os.stat_result) -> bool:
    uid = _current_uid()
    return uid is None or int(st.st_uid) == int(uid)


def _check_state_dir(state: Path) -> None:
    if state.is_symlink():
        raise BridgeError("Native log state must not be a symlink", "native_log_unsafe")
    try:
        st = state.stat()
    except FileNotFoundError:
        raise BridgeError("Native log state is not initialized", "native_log_uninitialized") from None
    except OSError:
        raise BridgeError("Unable to inspect native log state", "native_log_unsafe") from None
    if not stat.S_ISDIR(st.st_mode) or not _owned_by_current_user(st):
        raise BridgeError("Native log state must be a directory owned by the service user",
                          "native_log_unsafe")
    if stat.S_IMODE(st.st_mode) != STATE_MODE:
        raise BridgeError("Native log state must have mode 0700", "native_log_unsafe")


def _check_log_dir(log_dir: Path) -> None:
    if log_dir.is_symlink():
        raise BridgeError("Native log directory must not be a symlink", "native_log_unsafe")
    try:
        st = log_dir.stat()
    except FileNotFoundError:
        raise BridgeError("Native log directory is not initialized", "native_log_uninitialized") from None
    except OSError:
        raise BridgeError("Unable to inspect native log directory", "native_log_unsafe") from None
    if not stat.S_ISDIR(st.st_mode) or not _owned_by_current_user(st):
        raise BridgeError("Native log directory must be a directory owned by the service user",
                          "native_log_unsafe")
    if stat.S_IMODE(st.st_mode) != STATE_MODE:
        raise BridgeError("Native log directory must have mode 0700", "native_log_unsafe")


def _is_owned_log_file(path: Path, log_dir: Path) -> bool:
    try:
        # Lexical containment only; callers never pass arbitrary paths.
        if path.parent != log_dir:
            return False
    except Exception:
        return False
    name = path.name
    allowed = {
        STDOUT_NAME, STDERR_NAME,
        STDOUT_NAME + ".1", STDOUT_NAME + ".2",
        STDERR_NAME + ".1", STDERR_NAME + ".2",
    }
    return name in allowed


def _validate_log_file(path: Path, *, name: str) -> os.stat_result:
    if path.is_symlink():
        raise BridgeError(f"{name} must not be a symlink", "native_log_unsafe")
    try:
        st = path.stat()
    except FileNotFoundError:
        raise BridgeError(f"{name} is missing", "native_log_uninitialized") from None
    except OSError:
        raise BridgeError(f"Unable to inspect {name}", "native_log_unsafe") from None
    if not stat.S_ISREG(st.st_mode) or not _owned_by_current_user(st):
        raise BridgeError(f"{name} must be a regular file owned by the service user",
                          "native_log_unsafe")
    if stat.S_IMODE(st.st_mode) != FILE_MODE:
        raise BridgeError(f"{name} must have mode 0600", "native_log_unsafe")
    return st


def _ensure_log_file(path: Path, log_dir: Path) -> None:
    """Ensure an active log file exists as a private regular file."""
    if not _is_owned_log_file(path, log_dir):
        raise BridgeError("Refusing to touch unexpected log path", "native_log_unsafe")
    if path.is_symlink():
        raise BridgeError("Native log file must not be a symlink", "native_log_unsafe")
    if path.exists():
        _validate_log_file(path, name=path.name)
        return
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, FILE_MODE)
        os.close(fd)
    except FileExistsError:
        _validate_log_file(path, name=path.name)
    except OSError:
        raise BridgeError("Unable to create private native log file", "native_log_unsafe") from None
    try:
        os.chmod(path, FILE_MODE)
    except OSError:
        raise BridgeError("Unable to secure native log file", "native_log_unsafe") from None


def _archive_for(active: Path) -> tuple[Path, Path]:
    return active.parent / (active.name + ".1"), active.parent / (active.name + ".2")


def rotate_stream(active: Path, log_dir: Path | None = None) -> str:
    """Bounded copy-truncate rotation for one active stream.

    Returns ``"no-op"``, ``"rotated"``, ``"created"`` or a bounded
    ``"refused:*"`` / ``"error"`` code. Never raises; callers treat
    non-``rotated`` as isolated logging-only outcomes so logging cannot
    corrupt service state. Only ``stdout.log`` / ``stderr.log`` under the
    owned log directory are ever touched.
    """
    try:
        directory = log_dir if log_dir is not None else active.parent
        if active.parent != directory:
            return "refused:unexpected-path"
        if active.name not in (STDOUT_NAME, STDERR_NAME):
            return "refused:unexpected-path"
        if not _is_owned_log_file(active, directory):
            return "refused:unexpected-path"
        # Validate the containing directory lexically (no traversal).
        if directory.name != LOG_DIRNAME:
            return "refused:unexpected-path"
        if active.is_symlink():
            return "refused:symlink"
        if not active.exists():
            try:
                _ensure_log_file(active, directory)
            except BridgeError:
                return "refused:unsafe"
            return "created"
        try:
            st = _validate_log_file(active, name=active.name)
        except BridgeError:
            return "refused:unsafe"
        if int(st.st_size) <= int(ACTIVE_LIMIT):
            return "no-op"
        first_archive, second_archive = _archive_for(active)
        for candidate in (first_archive, second_archive):
            if not _is_owned_log_file(candidate, directory):
                return "refused:unexpected-path"
            if candidate.is_symlink():
                return "refused:symlink"
            if candidate.exists():
                try:
                    _validate_log_file(candidate, name=candidate.name)
                except BridgeError:
                    return "refused:unsafe"
        # Read at most the last ACTIVE_LIMIT bytes (bounded 10 MiB).
        try:
            with open(active, "rb") as stream:
                try:
                    stream.seek(max(0, int(st.st_size) - int(ACTIVE_LIMIT)))
                except OSError:
                    return "error"
                tail = stream.read(int(ACTIVE_LIMIT) + 1)
        except OSError:
            return "error"
        if len(tail) > int(ACTIVE_LIMIT):
            tail = tail[-int(ACTIVE_LIMIT):]
        # Shift prior .1 to .2 atomically where practical.
        try:
            if first_archive.exists():
                os.replace(first_archive, second_archive)
                try:
                    os.chmod(second_archive, FILE_MODE)
                except OSError:
                    return "error"
        except OSError:
            return "error"
        # Create/replace .1 atomically with 0600 + fsync.
        try:
            fd, temporary = tempfile.mkstemp(prefix="." + active.name + ".",
                                             dir=str(directory))
            temporary_path = Path(temporary)
            try:
                os.chmod(temporary_path, FILE_MODE)
                with os.fdopen(fd, "wb") as out:
                    out.write(tail)
                    out.flush()
                    try:
                        os.fsync(out.fileno())
                    except OSError:
                        pass
                os.replace(temporary_path, first_archive)
                try:
                    os.chmod(first_archive, FILE_MODE)
                except OSError:
                    pass
            finally:
                try:
                    temporary_path.unlink()
                except FileNotFoundError:
                    pass
        except OSError:
            return "error"
        # Truncate the same active inode (launchd keeps descriptors open).
        try:
            fd = os.open(active, os.O_WRONLY | os.O_NOFOLLOW)
            try:
                os.ftruncate(fd, 0)
                try:
                    os.fsync(fd)
                except OSError:
                    pass
            finally:
                os.close(fd)
        except OSError:
            return "error"
        return "rotated"
    except Exception:
        return "error"


def rotate_all(state: str | os.PathLike[str]) -> dict:
    """Rotate both stdout/stderr streams; never raises."""
    try:
        state_path = _absolute_state(state)
        log_dir, stdout_log, stderr_log = log_paths(state_path)
    except Exception:
        return {"stdout": "error", "stderr": "error"}
    out: dict = {}
    for name, active in (("stdout", stdout_log), ("stderr", stderr_log)):
        try:
            out[name] = rotate_stream(active, log_dir)
        except Exception:
            out[name] = "error"
    return out


def guard_once(state: str | os.PathLike[str]) -> dict:
    """Validate state/log ownership explicitly, then rotate once.

    Startup validation raises :class:`BridgeError` so misconfiguration is
    explicit and testable. Per-stream rotation outcomes are isolated codes.
    """
    state_path = _absolute_state(state)
    _check_state_dir(state_path)
    log_dir, _, _ = log_paths(state_path)
    _check_log_dir(log_dir)
    return rotate_all(state_path)


def _parent_exited(initial_ppid: int) -> bool:
    try:
        return int(os.getppid()) != int(initial_ppid)
    except Exception:
        return True


def guard_loop(state: str | os.PathLike[str], *,
               interval: float | None = None,
               max_iterations: int | None = None,
               initial_ppid: int | None = None) -> str:
    """Run the guard until the original parent is gone/reparented.

    Returns a bounded reason: ``"parent-exited"`` or ``"iterations"``.
    Startup validation raises; rotation errors are isolated per iteration.
    """
    state_path = _absolute_state(state)
    _check_state_dir(state_path)
    log_dir, _, _ = log_paths(state_path)
    _check_log_dir(log_dir)
    chosen = float(POLL_INTERVAL_SECONDS if interval is None else interval)
    if not (float(POLL_INTERVAL_MIN) - 1e-9 <= chosen <= 60.0):
        # Tests may use a tiny interval; production spawn always uses the
        # default 5-10s poll. Clamp only absurd values.
        if chosen <= 0 or chosen > 3600:
            raise BridgeError("Guard poll interval is invalid", "native_log_unsafe")
    ppid = int(initial_ppid) if initial_ppid is not None else int(os.getppid())
    count = 0
    while True:
        if _parent_exited(ppid):
            return "parent-exited"
        try:
            rotate_all(state_path)
        except Exception:
            pass
        count += 1
        if max_iterations is not None and count >= int(max_iterations):
            return "iterations"
        try:
            time.sleep(chosen)
        except Exception:
            return "parent-exited"


def should_spawn_guard(*, platform_name: str | None = None,
                       environ: dict | None = None) -> bool:
    """Return True only for managed LaunchAgents on Darwin with the exact marker."""
    name = platform_name if platform_name is not None else platform.system()
    if name != "Darwin":
        return False
    env = environ if environ is not None else os.environ
    try:
        value = env.get(GUARD_ENV)
    except Exception:
        return False
    return value == GUARD_VALUE


def guard_argv(state: str | os.PathLike[str]) -> list[str]:
    """Return the fixed package-owned guard argv (no shell, no caller input)."""
    state_path = _absolute_state(state)
    return [str(_absolute_state(sys.executable)), "-m", GUARD_MODULE,
            GUARD_SUBCOMMAND, "--state", str(state_path)]


def spawn_log_guard(state: str | os.PathLike[str], *,
                    platform_name: str | None = None,
                    environ: dict | None = None,
                    popen: Callable | None = None) -> bool:
    """Spawn one package-owned log-guard child with stdio to DEVNULL.

    Returns True when a child was spawned, False when no guard applies or
    spawning failed (isolated so logging never breaks service startup).
    Uses a fixed argv with no shell and never inherits stdout/stderr.
    """
    if not should_spawn_guard(platform_name=platform_name, environ=environ):
        return False
    try:
        argv = guard_argv(state)
    except Exception:
        return False
    runner = popen if popen is not None else subprocess.Popen
    try:
        runner(argv, stdin=subprocess.DEVNULL,
               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
               close_fds=True, start_new_session=True, shell=False)
        return True
    except Exception:
        return False


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Package-owned native log guard")
    sub = parser.add_subparsers(dest="command", required=True)
    guard = sub.add_parser(GUARD_SUBCOMMAND, help="Bound macOS log rotation")
    guard.add_argument("--state", type=Path, required=True,
                       help="Service state directory owning logs/")
    guard.add_argument("--interval", type=float, default=POLL_INTERVAL_SECONDS,
                       help="Poll interval in seconds (default 7; tests may override)")
    guard.add_argument("--once", action="store_true",
                       help="Rotate once and exit (diagnostics/tests only)")
    guard.add_argument("--max-iterations", type=int, default=None,
                       help="Bounded loop iterations (tests only)")
    args = parser.parse_args(argv)
    if args.command == GUARD_SUBCOMMAND:
        state = _absolute_state(args.state)
        if args.once:
            result = guard_once(state)
            # Guard emits nothing to service logs; stdout here is only for
            # the --once diagnostic path (never used by the spawned child
            # whose stdio is DEVNULL). Keep it out of the service streams.
            sys.stdout.write("ok\n")
            _ = result
            return
        guard_loop(state, interval=args.interval,
                   max_iterations=args.max_iterations)
        return
    parser.error("Unknown native-log subcommand")


if __name__ == "__main__":
    main()
