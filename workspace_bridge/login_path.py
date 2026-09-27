"""Bounded interactive-login-shell executable search path resolution.

Obtains only the final ``PATH`` value from the current OS user's configured
login shell (``pwd`` user database, ``$SHELL`` fallback, ``/bin/sh`` last
resort) by spawning that shell once in interactive + login + command mode
and printing ``$PATH`` between unique markers. Interactive mode matters:
on zsh ``.zshrc`` is interactive-only, so login-only probing misses normal
terminal PATH entries. Shell configuration files are never read directly
and the complete environment is never captured or imported: stdout is
scanned for the marked value, stderr is discarded at the subprocess
boundary, and only a validated ``PATH`` string is ever applied.

Validation is strict: bounded length, no control/NUL/newline characters,
absolute non-empty entries only, no relative or dot entries. On any failure
or timeout the caller keeps the inherited service search path.

Observability exposes only ``resolved`` (bool), shell basename, entry count
and a bounded failure code; the actual PATH value and all other environment
values are never exposed.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any, Callable, Mapping

MAX_LOGIN_PATH_CHARS = 8192
MAX_LOGIN_PATH_ENTRIES = 256
MAX_LOGIN_SHELL_OUTPUT = 65536
LOGIN_PATH_TIMEOUT_S = 5.0

_MARK_BEGIN = "__WB_LOGIN_PATH_BEGIN__"
_MARK_END = "__WB_LOGIN_PATH_END__"
# Interactive-login probe isolated on dedicated fd 3. Markers isolate the probe
# value so incidental startup output cannot corrupt it; ordinary shell stdout
# and stderr are discarded (DEVNULL) at the spawn boundary and never buffered.
# Only bytes arriving on fd 3 are captured, hard-bounded at/near
# ``MAX_LOGIN_SHELL_OUTPUT`` during capture (never buffered unbounded).
# POSIX shells share a colon-separated ``$PATH``; fish uses a list and needs
# an explicit join to the same colon-separated form. No temp file is used.
_PROBE_COMMAND_POSIX = f"printf '{_MARK_BEGIN}%s{_MARK_END}' \"$PATH\" >&3"
_PROBE_COMMAND_FISH = f"printf '{_MARK_BEGIN}%s{_MARK_END}' (string join : $PATH) >&3"

# Shells supporting `-l -i -c <probe>` (interactive + login + command).
_INTERACTIVE_LOGIN_SHELLS = frozenset({
    "bash", "zsh", "fish", "ksh", "ksh93", "mksh", "pdksh", "yash",
})
# Minimal Bourne-family fallback: `-l` is not portable here (dash rejects
# it), so use conservative interactive-only `-i -c <probe>`.
_CONSERVATIVE_SH_SHELLS = frozenset({"sh", "dash", "ash"})

_SAFE_CODES = frozenset({
    "ok",
    "timeout",
    "spawn_error",
    "unsupported",
    "nonzero_exit",
    "output_too_large",
    "marker_missing",
    "too_long",
    "empty",
    "control",
    "entry_count",
    "empty_entry",
    "relative",
    "dot_entry",
})


def _safe_code(value: Any) -> str:
    text = value if isinstance(value, str) else "spawn_error"
    if text in _SAFE_CODES:
        return text
    cleaned = "".join(c if c.isalnum() or c == "_" else "_" for c in text)[:32]
    return cleaned or "spawn_error"


def _shell_basename(shell: str) -> str:
    try:
        base = os.path.basename(str(shell or ""))
    except Exception:
        return "sh"
    base = "".join(c for c in base if c.isalnum() or c in ("-", "_", ".", "+"))[:64]
    return base or "sh"


def get_login_shell(env: Mapping[str, str] | None = None) -> str:
    """Return the current OS user's configured login shell path.

    Prefers the user database entry (``pwd``), then ``$SHELL`` when it is an
    absolute path, then ``/bin/sh``. Never reads shell configuration files.
    """
    candidate = ""
    try:
        import pwd  # POSIX only; absent on Windows.

        candidate = pwd.getpwuid(os.getuid()).pw_shell or ""
    except Exception:
        candidate = ""
    if isinstance(candidate, str) and candidate.startswith("/") and "\x00" not in candidate \
            and "\n" not in candidate and "\r" not in candidate and len(candidate) <= 1024:
        return candidate
    mapping = env if env is not None else os.environ
    try:
        env_shell = mapping.get("SHELL", "") if mapping is not None else ""
    except Exception:
        env_shell = ""
    if isinstance(env_shell, str) and env_shell.startswith("/") and "\x00" not in env_shell \
            and "\n" not in env_shell and "\r" not in env_shell and len(env_shell) <= 1024:
        return env_shell
    return "/bin/sh"


def validate_search_path(raw: Any) -> tuple[bool, list[str], str]:
    """Strictly validate a candidate PATH string.

    Returns ``(ok, entries, code)`` where ``code`` is a bounded safe token
    (``"ok"`` on success). Never echoes the candidate value.
    """
    if not isinstance(raw, str):
        return False, [], "empty"
    if not raw:
        return False, [], "empty"
    if len(raw) > MAX_LOGIN_PATH_CHARS:
        return False, [], "too_long"
    for char in raw:
        code = ord(char)
        if code < 32 or code == 127:
            return False, [], "control"
    if "\x00" in raw:
        return False, [], "control"
    entries = raw.split(":")
    if not entries or len(entries) > MAX_LOGIN_PATH_ENTRIES:
        return False, [], "entry_count"
    for entry in entries:
        if not entry:
            return False, [], "empty_entry"
        if not entry.startswith("/"):
            return False, [], "relative"
        if entry in (".", ".."):
            return False, [], "dot_entry"
        # Reject dot path components (relative escapes) without echoing.
        try:
            parts = entry.split("/")
        except Exception:
            return False, [], "dot_entry"
        for part in parts:
            if part in (".", ".."):
                return False, [], "dot_entry"
    return True, entries, "ok"


def probe_command_for_shell(shell: str) -> str:
    """Return the documented probe command for a shell path (never a PATH value)."""
    if _shell_basename(shell).lower() == "fish":
        return _PROBE_COMMAND_FISH
    return _PROBE_COMMAND_POSIX


def probe_argv(shell: str) -> list[str] | None:
    """Build interactive-login argv for a shell path, or None if unsupported.

    Supported families use ``-l -i -c <probe>`` (or the shell's documented
    equivalent flags); the minimal ``sh`` family uses conservative
    ``-i -c <probe>`` because ``-l`` is not portable there. Unknown shells
    return None so callers fail safely to the inherited PATH instead of
    guessing unsafe invocation semantics.
    """
    try:
        name = _shell_basename(shell).lower()
    except Exception:
        return None
    if name in _INTERACTIVE_LOGIN_SHELLS:
        return [shell, "-l", "-i", "-c", probe_command_for_shell(shell)]
    if name in _CONSERVATIVE_SH_SHELLS:
        return [shell, "-i", "-c", probe_command_for_shell(shell)]
    return None


def _bounded_fd3_run(cmd: list[str], *, timeout: float) -> Any:
    """Run the probe with hard-bounded capture on a dedicated pipe.

    Ordinary shell stdout/stderr go to DEVNULL at the spawn boundary and are
    never buffered. Only bytes on the dedicated capture pipe (the marked
    probe value, redirected via ``>/dev/fd/<fd>``) are read, capped at
    ``MAX_LOGIN_SHELL_OUTPUT`` during capture: the reader stops and kills
    the child as soon as the bound is exceeded, so unbounded rc noise cannot
    accumulate. Returns a minimal ``(stdout, returncode)`` object matching
    the ``_run`` seam (``stdout`` str, ``returncode`` int). No temp file is
    used and no PATH content is exposed here.
    """
    import select as _select
    import time as _time
    try:
        timeout_value = float(timeout)
    except (TypeError, ValueError):
        timeout_value = LOGIN_PATH_TIMEOUT_S
    if not (0 < timeout_value <= 30):
        timeout_value = LOGIN_PATH_TIMEOUT_S
    r_fd, w_fd = os.pipe()
    # Redirect the probe's trailing `>&3` through `/dev/fd/<fd>` (present on
    # Linux and macOS) so two-digit descriptors work under /bin/sh/dash,
    # which cannot parse `>&10` portably. The inherited pipe fd is passed
    # via pass_fds with no preexec_fn/parent-fd mutation. The replacement
    # only changes the redirection target, never PATH content.
    try:
        eff_cmd = list(cmd)
        if eff_cmd:
            eff_cmd[-1] = str(eff_cmd[-1]).replace(">&3", f">/dev/fd/{w_fd}")
    except Exception:
        eff_cmd = list(cmd)
    proc = None
    try:
        proc = subprocess.Popen(
            eff_cmd, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            pass_fds=(w_fd,), close_fds=True)
    finally:
        try:
            os.close(w_fd)
        except OSError:
            pass
    if proc is None:
        raise OSError("probe spawn failed")
    chunks: list[bytes] = []
    total = 0
    oversize = False
    deadline = _time.monotonic() + timeout_value
    try:
        import fcntl as _fcntl
        try:
            _flags = _fcntl.fcntl(r_fd, _fcntl.F_GETFL)
            _fcntl.fcntl(r_fd, _fcntl.F_SETFL, _flags | os.O_NONBLOCK)
        except (OSError, AttributeError):
            pass
        while True:
            remaining = deadline - _time.monotonic()
            if remaining <= 0:
                try:
                    proc.kill()
                except OSError:
                    pass
                raise subprocess.TimeoutExpired(cmd, timeout_value)
            try:
                _rlist, _, _ = _select.select([r_fd], [], [], min(remaining, 0.2))
            except (OSError, ValueError):
                break
            if _rlist:
                try:
                    data = os.read(r_fd, 8192)
                except BlockingIOError:
                    continue
                except OSError:
                    break
                if not data:
                    break
                # Cap during capture: keep only enough to prove oversize
                # deterministically without buffering unbounded content.
                if total + len(data) > MAX_LOGIN_SHELL_OUTPUT:
                    oversize = True
                    chunks.append(data[:max(0, MAX_LOGIN_SHELL_OUTPUT - total + 1)])
                    total = MAX_LOGIN_SHELL_OUTPUT + 1
                    try:
                        proc.kill()
                    except OSError:
                        pass
                    break
                chunks.append(data)
                total += len(data)
            if proc.poll() is not None:
                while True:
                    try:
                        data = os.read(r_fd, 8192)
                    except BlockingIOError:
                        break
                    except OSError:
                        break
                    if not data:
                        break
                    if total + len(data) > MAX_LOGIN_SHELL_OUTPUT:
                        oversize = True
                        total = MAX_LOGIN_SHELL_OUTPUT + 1
                        break
                    chunks.append(data)
                    total += len(data)
                break
        try:
            proc.wait(timeout=max(0.0, deadline - _time.monotonic()))
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except OSError:
                pass
            raise
        returncode = proc.returncode if proc.returncode is not None else 1
    finally:
        try:
            os.close(r_fd)
        except OSError:
            pass
        try:
            if proc is not None and proc.poll() is None:
                proc.kill()
                try:
                    proc.wait(timeout=1)
                except Exception:
                    pass
        except Exception:
            pass
    raw = b"".join(chunks)
    if oversize:
        # Bounded oversize sentinel: long enough to trip the after-capture
        # bound deterministically without retaining unbounded content.
        stdout_text = "x" * (MAX_LOGIN_SHELL_OUTPUT + 1)
    else:
        try:
            stdout_text = raw.decode("utf-8", errors="replace")
        except Exception:
            stdout_text = ""
    box = type("R", (), {})()
    box.stdout = stdout_text
    box.returncode = returncode
    return box


def _extract_marked_path(output: str) -> str | None:
    try:
        start = output.find(_MARK_BEGIN)
        if start < 0:
            return None
        start += len(_MARK_BEGIN)
        end = output.find(_MARK_END, start)
        if end < 0:
            return None
        return output[start:end]
    except Exception:
        return None


def resolve_login_path(
    *,
    shell: str | None = None,
    timeout: float = LOGIN_PATH_TIMEOUT_S,
    _run: Callable[..., Any] | None = None,
    _shell: str | None = None,
) -> dict:
    """Resolve the login-shell PATH once with bounds and strict validation.

    ``_run`` and ``_shell`` are injectable seams for deterministic unit tests;
    production callers omit them. Returns a dict with only safe observability
    plus the validated ``path`` on success::

        {"resolved": bool, "path": str|None, "shell": str,
         "shell_basename": str, "entry_count": int, "code": str}

    ``shell`` in the result is the full shell path used for spawning (needed
    by the caller to spawn); ``shell_basename`` is the safe observability
    token. The actual PATH value is present as ``path`` only when resolved.
    """
    chosen = _shell if isinstance(_shell, str) and _shell else (
        shell if isinstance(shell, str) and shell else get_login_shell())
    if not isinstance(chosen, str) or not chosen.startswith("/"):
        chosen = "/bin/sh"
    # Bound the shell string without echoing it beyond basename in logs.
    if len(chosen) > 1024 or "\x00" in chosen or "\n" in chosen or "\r" in chosen:
        return {"resolved": False, "path": None, "shell": "/bin/sh",
                "shell_basename": "sh", "entry_count": 0, "code": "spawn_error"}
    basename = _shell_basename(chosen)
    try:
        timeout_value = float(timeout)
    except (TypeError, ValueError):
        timeout_value = LOGIN_PATH_TIMEOUT_S
    if not (0 < timeout_value <= 30):
        timeout_value = LOGIN_PATH_TIMEOUT_S
    argv = probe_argv(chosen)
    if argv is None:
        return {"resolved": False, "path": None, "shell": chosen,
                "shell_basename": basename, "entry_count": 0, "code": "unsupported"}
    runner = _run
    if runner is None:
        runner = lambda cmd, *, timeout: _bounded_fd3_run(cmd, timeout=timeout)
    try:
        result = runner(argv, timeout=timeout_value)
    except subprocess.TimeoutExpired:
        return {"resolved": False, "path": None, "shell": chosen,
                "shell_basename": basename, "entry_count": 0, "code": "timeout"}
    except Exception:
        return {"resolved": False, "path": None, "shell": chosen,
                "shell_basename": basename, "entry_count": 0, "code": "spawn_error"}
    try:
        stdout = getattr(result, "stdout", "")
        returncode = getattr(result, "returncode", 1)
    except Exception:
        return {"resolved": False, "path": None, "shell": chosen,
                "shell_basename": basename, "entry_count": 0, "code": "spawn_error"}
    if not isinstance(stdout, str):
        try:
            stdout = str(stdout)
        except Exception:
            stdout = ""
    if len(stdout) > MAX_LOGIN_SHELL_OUTPUT:
        return {"resolved": False, "path": None, "shell": chosen,
                "shell_basename": basename, "entry_count": 0, "code": "output_too_large"}
    if returncode != 0:
        # Nonzero exit still allows a marked value when the shell printed one
        # (e.g. noisy rc with nonzero status); otherwise fall back safely.
        candidate = _extract_marked_path(stdout)
        if candidate is None:
            return {"resolved": False, "path": None, "shell": chosen,
                    "shell_basename": basename, "entry_count": 0, "code": "nonzero_exit"}
    else:
        candidate = _extract_marked_path(stdout)
        if candidate is None:
            return {"resolved": False, "path": None, "shell": chosen,
                    "shell_basename": basename, "entry_count": 0, "code": "marker_missing"}
    ok, entries, code = validate_search_path(candidate)
    if not ok:
        return {"resolved": False, "path": None, "shell": chosen,
                "shell_basename": basename, "entry_count": 0, "code": _safe_code(code)}
    return {"resolved": True, "path": candidate, "shell": chosen,
            "shell_basename": basename, "entry_count": len(entries), "code": "ok"}


def safe_summary(result: Mapping[str, Any] | None) -> dict:
    """Return only safe observability fields for logging (never PATH values)."""
    try:
        resolved = bool(result.get("resolved")) if isinstance(result, Mapping) else False
        basename = _shell_basename(result.get("shell", "") if isinstance(result, Mapping) else "")
        try:
            count = int(result.get("entry_count", 0)) if isinstance(result, Mapping) else 0
        except (TypeError, ValueError):
            count = 0
        count = max(0, min(count, MAX_LOGIN_PATH_ENTRIES + 1))
        code = _safe_code(result.get("code", "spawn_error") if isinstance(result, Mapping) else "spawn_error")
    except Exception:
        return {"resolved": False, "shell_basename": "sh", "entry_count": 0, "code": "spawn_error"}
    return {"resolved": resolved, "shell_basename": basename,
            "entry_count": count, "code": code}


def runtime_env_with_login_path(
    base_env: Mapping[str, str] | None = None,
    *,
    _resolve: Callable[[], dict] | None = None,
) -> tuple[dict, dict]:
    """Build a child-process environment with the resolved login PATH.

    Copies ``base_env`` (default ``os.environ``) and replaces only ``PATH``
    when resolution succeeds and validates. All other environment values are
    untouched and no complete environment is captured from the shell. Returns
    ``(env, result)`` where ``result`` is the resolver dict.
    """
    if base_env is None:
        try:
            env = dict(os.environ)
        except Exception:
            env = {}
    else:
        try:
            env = dict(base_env)
        except Exception:
            env = {}
    resolver = _resolve if _resolve is not None else resolve_login_path
    try:
        result = resolver()
    except Exception:
        result = {"resolved": False, "path": None, "shell": "/bin/sh",
                  "shell_basename": "sh", "entry_count": 0, "code": "spawn_error"}
    if not isinstance(result, dict) or not result.get("resolved"):
        if not isinstance(result, dict):
            result = {"resolved": False, "path": None, "shell": "/bin/sh",
                      "shell_basename": "sh", "entry_count": 0, "code": "spawn_error"}
        return env, result
    candidate = result.get("path")
    ok, _entries, _code = validate_search_path(candidate)
    if not ok or not isinstance(candidate, str):
        result = {"resolved": False, "path": None, "shell": str(result.get("shell", "/bin/sh")),
                  "shell_basename": _shell_basename(str(result.get("shell", ""))),
                  "entry_count": 0, "code": "spawn_error"}
        return env, result
    env["PATH"] = candidate
    return env, result


def login_shell_basename(shell: str | None = None) -> str:
    """Safe shell basename token for observability (never a PATH value)."""
    return _shell_basename(shell or get_login_shell())
