"""One OS service definition for the Bridge and the Node (launchd or systemd).

User scope is the default: a LaunchAgent in the login session on macOS, a
systemd user unit on Linux. ``--root`` is the explicit opt-in for a
system-wide LaunchDaemon or system unit that starts at boot; it must be run
as root (for example under sudo), and the service still runs as the account
that owns the state folder.

The service runs ``python -m <module> --state <state> serve`` with
``WB_SERVICE=1``, which makes the process write its own size-capped log under
``<state>/logs`` instead of relying on launchd or journald retention. Files
this module writes carry a marker line; a definition without it is never
replaced or removed.
"""
from __future__ import annotations

import json
import os
import platform
import plistlib
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError
from urllib.request import ProxyHandler, build_opener

from .security import BridgeError

MARKER = "Managed by workspace-bridge; edit with the service commands, not by hand."
SERVICE_ENV = "WB_SERVICE"
LOG_DIR = "logs"
ENV_FILE = "bridge.env"
ENV_KEYS = frozenset({
    "WB_NODE_GATEWAY_HOST", "WB_NODE_GATEWAY_URL", "WB_DISCORD_WEBHOOK_URL",
    "WB_ADMIN_ALLOWED_HOSTS", "WB_LOG_LEVEL",
})
_ENV_LINE = re.compile(r"([A-Z][A-Z0-9_]{0,63})=(.*)")
ACTIONS = ("install", "status", "start", "stop", "restart", "uninstall")
COMMAND_TIMEOUT = 30
HEALTH_TIMEOUT = 2.0


@dataclass(frozen=True)
class Component:
    name: str
    label: str
    unit: str
    module: str
    description: str
    config: str


BRIDGE = Component("bridge", "com.workspace-bridge.bridge", "workspace-bridge.service",
                   "workspace_bridge.cli", "Workspace Bridge", "config.json")
NODE = Component("node", "com.workspace-bridge.node", "workspace-bridge-node.service",
                 "workspace_bridge.node_cli", "Workspace Bridge Node", "node-config.json")


def load_env_file(state: Path, environ: dict | None = None) -> list[str]:
    """Apply allowlisted settings from ``bridge.env``; real environment wins.

    A managed service has no shell environment, so this private file supplies
    its settings. Unknown keys, malformed lines and an unsafe file are errors.
    """
    env = environ if environ is not None else os.environ
    path = state / ENV_FILE
    if not path.exists() and not path.is_symlink():
        return []
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, encoding="utf-8") as stream:
        st = os.fstat(stream.fileno())
        if st.st_uid != os.getuid() or st.st_mode & 0o077:
            raise BridgeError(f"{ENV_FILE} must be a private file (0600) owned by the service user")
        text = stream.read(64 * 1024)
    applied = []
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = _ENV_LINE.fullmatch(line)
        if match is None or match.group(1) not in ENV_KEYS:
            raise BridgeError(f"{ENV_FILE} line {number} is not an allowed KEY=value setting")
        key, value = match.group(1), match.group(2).strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if any(ord(char) < 32 for char in value):
            raise BridgeError(f"{ENV_FILE} line {number} contains control characters")
        if key not in env:
            env[key] = value
            applied.append(key)
    return applied


def program(component: Component, state: Path) -> list[str]:
    return [sys.executable, "-m", component.module, "--state", str(state), "serve"]


def _owner(state: Path, root: bool, euid: int) -> tuple[str, str]:
    """Validate the state folder and return the account the service runs as."""
    import grp
    import pwd
    try:
        st = os.lstat(state)
    except FileNotFoundError:
        raise BridgeError("State folder does not exist; initialize it first", "service_state_missing") from None
    if not os.path.isdir(state) or os.path.islink(state):
        raise BridgeError("State path must be a real directory", "service_state_unsafe")
    if st.st_mode & 0o077:
        raise BridgeError("State folder must be private (mode 0700)", "service_state_unsafe")
    if not root and st.st_uid != euid:
        raise BridgeError("State folder must be owned by the account installing the service",
                          "service_state_unsafe")
    try:
        return pwd.getpwuid(st.st_uid).pw_name, grp.getgrgid(st.st_gid).gr_name
    except KeyError:
        raise BridgeError("State folder owner has no account on this host", "service_state_unsafe") from None


def _check_config(component: Component, state: Path) -> None:
    try:
        config = json.loads((state / component.config).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise BridgeError(f"{component.config} is missing or invalid; initialize the state first",
                          "service_config_invalid") from None
    if component is NODE and not isinstance(config.get("bridge"), dict):
        raise BridgeError("Pair the Node with a Bridge before installing its service", "node_not_paired")


def _write_managed(path: Path, data: bytes, mode: int) -> None:
    if path.is_symlink():
        raise BridgeError(f"{path} is a symlink; remove it first", "service_file_unmanaged")
    if path.exists() and MARKER.encode() not in path.read_bytes():
        raise BridgeError(f"{path} was not written by workspace-bridge; remove it first",
                          "service_file_unmanaged")
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, mode)
    try:
        os.write(fd, data)
        os.fchmod(fd, mode)
    finally:
        os.close(fd)
    os.replace(temp, path)


def _managed(path: Path) -> bool:
    if not path.exists():
        return False
    if path.is_symlink() or MARKER.encode() not in path.read_bytes():
        raise BridgeError(f"{path} was not written by workspace-bridge; leaving it alone",
                          "service_file_unmanaged")
    return True


def _state_argument(argv: list[str]) -> str | None:
    return argv[argv.index("--state") + 1] if "--state" in argv[:-1] else None


Runner = Callable[[list[str]], subprocess.CompletedProcess]


def _run(argv: list[str]) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=COMMAND_TIMEOUT, check=False)
    except FileNotFoundError:
        raise BridgeError(f"{argv[0]} is not available on this host", "service_manager_unavailable") from None
    except subprocess.TimeoutExpired:
        raise BridgeError(f"{argv[0]} did not answer in time", "service_manager_timeout") from None


class Launchd:
    def __init__(self, component: Component, root: bool, *, run: Runner = _run,
                 directory: Path | None = None, uid: int | None = None):
        self.component, self.root, self.run = component, root, run
        base = directory or (Path("/Library/LaunchDaemons") if root
                             else Path.home() / "Library" / "LaunchAgents")
        self.path = base / f"{component.label}.plist"
        self.domain = "system" if root else f"gui/{os.getuid() if uid is None else uid}"
        self.target = f"{self.domain}/{component.label}"

    def render(self, state: Path, user: str, group: str) -> bytes:
        log = str(state / LOG_DIR / f"{self.component.name}.stderr.log")
        value = {"Label": self.component.label, "ProgramArguments": program(self.component, state),
                 "EnvironmentVariables": {SERVICE_ENV: "1"}, "RunAtLoad": True,
                 "KeepAlive": {"SuccessfulExit": False}, "ThrottleInterval": 10,
                 "Umask": 0o077, "StandardOutPath": log, "StandardErrorPath": log}
        if self.root:
            value.update(UserName=user, GroupName=group)
        head, body = plistlib.dumps(value).split(b"\n", 1)
        return head + f"\n<!-- {MARKER} -->\n".encode() + body

    def defined_state(self) -> str | None:
        try:
            argv = plistlib.loads(self.path.read_bytes()).get("ProgramArguments")
        except (OSError, ValueError, plistlib.InvalidFileException):
            return None
        return _state_argument([str(part) for part in argv]) if isinstance(argv, list) else None

    def _check(self, action: str, result: subprocess.CompletedProcess) -> None:
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip().splitlines()[-1:] or [""]
            raise BridgeError(f"launchctl {action} failed (exit {result.returncode}) {detail[0][:200]}".strip(),
                              "service_manager_failed")

    def state(self) -> dict:
        result = self.run(["launchctl", "print", self.target])
        if result.returncode != 0:
            return {"loaded": False, "running": False, "pid": None}
        pid = re.search(r"^\s*pid = (\d+)", result.stdout, re.M)
        running = re.search(r"^\s*state = running", result.stdout, re.M) is not None
        return {"loaded": True, "running": running, "pid": int(pid.group(1)) if pid else None}

    def install(self, data: bytes) -> None:
        _write_managed(self.path, data, 0o644)
        if self.state()["loaded"]:
            self.run(["launchctl", "bootout", self.target])
        self._check("bootstrap", self.run(["launchctl", "bootstrap", self.domain, str(self.path)]))

    def start(self) -> None:
        if not self.state()["loaded"]:
            self._check("bootstrap", self.run(["launchctl", "bootstrap", self.domain, str(self.path)]))
        else:
            self._check("kickstart", self.run(["launchctl", "kickstart", self.target]))

    def stop(self) -> None:
        if self.state()["loaded"]:
            self._check("bootout", self.run(["launchctl", "bootout", self.target]))

    def restart(self) -> None:
        if self.state()["loaded"]:
            self._check("kickstart", self.run(["launchctl", "kickstart", "-k", self.target]))
        else:
            self.start()

    def uninstall(self) -> None:
        self.stop()
        self.path.unlink()


class Systemd:
    def __init__(self, component: Component, root: bool, *, run: Runner = _run,
                 directory: Path | None = None):
        self.component, self.root, self.run = component, root, run
        base = directory or (Path("/etc/systemd/system") if root
                             else Path.home() / ".config" / "systemd" / "user")
        self.path = base / component.unit
        self.ctl = ["systemctl"] if root else ["systemctl", "--user"]

    def render(self, state: Path, user: str, group: str) -> bytes:
        def quote(part: str) -> str:
            if re.fullmatch(r"[A-Za-z0-9_./:=@+-]+", part):
                return part
            escaped = part.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
            return f'"{escaped}"'
        identity = f"User={user}\nGroup={group}\n" if self.root else ""
        text = (f"# {MARKER}\n[Unit]\nDescription={self.component.description}\n"
                "After=network-online.target\nWants=network-online.target\n\n"
                f"[Service]\nType=simple\n{identity}"
                f"ExecStart={' '.join(quote(part) for part in program(self.component, state))}\n"
                f"Environment={SERVICE_ENV}=1\nRestart=on-failure\nRestartSec=10\nUMask=0077\n\n"
                f"[Install]\nWantedBy={'multi-user.target' if self.root else 'default.target'}\n")
        return text.encode()

    def defined_state(self) -> str | None:
        import shlex
        try:
            line = next(line for line in self.path.read_text().splitlines() if line.startswith("ExecStart="))
            return _state_argument(shlex.split(line.removeprefix("ExecStart=")))
        except (OSError, ValueError, StopIteration):
            return None

    def _ctl(self, *args: str) -> None:
        result = self.run([*self.ctl, *args])
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip().splitlines()[-1:] or [""]
            raise BridgeError(f"systemctl {args[0]} failed (exit {result.returncode}) {detail[0][:200]}".strip(),
                              "service_manager_failed")

    def state(self) -> dict:
        result = self.run([*self.ctl, "show", self.component.unit, "--no-pager",
                           "--property=LoadState,ActiveState,MainPID"])
        values = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        pid = int(values.get("MainPID", "0") or 0)
        return {"loaded": values.get("LoadState") == "loaded",
                "running": values.get("ActiveState") == "active", "pid": pid or None}

    def linger(self) -> bool | None:
        """User units start at boot only with lingering enabled for the account."""
        if self.root:
            return None
        import pwd
        result = self.run(["loginctl", "show-user", pwd.getpwuid(os.getuid()).pw_name,
                           "--property=Linger", "--value"])
        return result.stdout.strip() == "yes" if result.returncode == 0 else None

    def install(self, data: bytes) -> None:
        _write_managed(self.path, data, 0o644)
        self._ctl("daemon-reload")
        self._ctl("enable", self.component.unit)
        self._ctl("restart", self.component.unit)

    def start(self) -> None:
        self._ctl("start", self.component.unit)

    def stop(self) -> None:
        self._ctl("stop", self.component.unit)

    def restart(self) -> None:
        self._ctl("restart", self.component.unit)

    def uninstall(self) -> None:
        self.run([*self.ctl, "disable", "--now", self.component.unit])
        self.path.unlink()
        self._ctl("daemon-reload")


def backend(component: Component, root: bool, *, platform_name: str | None = None, **kwargs):
    name = platform_name or platform.system()
    if name == "Darwin":
        return Launchd(component, root, **kwargs)
    if name == "Linux":
        return Systemd(component, root, **kwargs)
    raise BridgeError("Service commands need macOS launchd or Linux systemd", "service_unsupported_platform")


def probe_bridge(config: dict) -> dict:
    """Both listeners answer on loopback: Manager 200, MCP 401 without a token."""
    opener = build_opener(ProxyHandler({}))
    checks = ((f"http://127.0.0.1:{config.get('admin_port', 8766)}/", 200),
              (f"http://127.0.0.1:{config.get('mcp_port', 8765)}/mcp", 401))
    for url, expected in checks:
        try:
            with opener.open(url, timeout=HEALTH_TIMEOUT) as response:
                status = response.status
        except HTTPError as exc:
            status = exc.code
            exc.close()
        except OSError:
            return {"status": "unavailable", "code": "bridge_unavailable"}
        if status != expected:
            return {"status": "invalid_response", "code": "bridge_unexpected_status"}
    return {"status": "healthy", "code": "ok"}


def _health(component: Component, state: Path) -> dict:
    try:
        if component is BRIDGE:
            return probe_bridge(json.loads((state / component.config).read_text(encoding="utf-8")))
        from .node_link import read_link_status
        return read_link_status(state)
    except (OSError, ValueError):
        return {"status": "unknown", "code": "config_unreadable"}


def run_action(component: Component, action: str, state: Path, *, root: bool = False,
               manager=None, euid: int | None = None, probe: Callable[[Component, Path], dict] = _health) -> dict:
    """Run one service action and report the resulting state."""
    if action not in ACTIONS:
        raise BridgeError("Unknown service action", "invalid_arguments")
    euid = os.geteuid() if euid is None else euid
    if root and euid != 0:
        raise BridgeError("--root manages a system service; run this command as root, for example with sudo",
                          "service_needs_root")
    if not root and euid == 0:
        raise BridgeError("Running as root: pass --root for a system service, or run as the state owner "
                          "for a user service", "service_scope_ambiguous")
    manager = manager or backend(component, root)
    scope = "system" if root else "user"
    if action == "install":
        user, group = _owner(state, root, euid)
        _check_config(component, state)
        (state / LOG_DIR).mkdir(mode=0o700, exist_ok=True)
        manager.install(manager.render(state, user, group))
    elif action == "uninstall":
        if _managed(manager.path):
            manager.uninstall()
        return {"component": component.name, "scope": scope, "installed": False,
                "definition": str(manager.path)}
    elif action != "status":
        if not _managed(manager.path):
            raise BridgeError("The service is not installed; run service install first", "service_not_installed")
        getattr(manager, action)()
    installed = manager.path.exists()
    result = {"component": component.name, "scope": scope, "installed": installed,
              "definition": str(manager.path), **manager.state(),
              "log": str(state / LOG_DIR / f"{component.name}.log")}
    serves = manager.defined_state() if installed else None
    if installed and serves != str(state):
        # Another state folder (or a definition from an earlier release) owns
        # this service name; probing this folder's ports would mislead.
        result["serves_state"] = serves
    elif installed and result["running"]:
        result["health"] = probe(component, state)
    if isinstance(manager, Systemd) and not root:
        result["starts_at_boot"] = manager.linger()
    return result


def add_service_commands(sub, *, what: str) -> None:
    parser = sub.add_parser("service", help=f"Run the {what} as an OS service (launchd or systemd)")
    actions = parser.add_subparsers(dest="service_command", required=True)
    for action, text in (("install", f"Install and start the {what} service"),
                         ("status", "Show service state and health"),
                         ("start", f"Start the {what} service"), ("stop", f"Stop the {what} service"),
                         ("restart", f"Restart the {what} service"),
                         ("uninstall", "Stop the service and remove its definition (state is kept)")):
        command = actions.add_parser(action, help=text)
        command.add_argument("--root", action="store_true",
                             help="System-wide service started at boot; run as root (for example with sudo)")
