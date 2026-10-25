"""Persistent OS service for the Bridge itself (macOS launchd, Linux systemd).

Reuses the Node service lifecycle (fixed unit/plist paths, manifests,
ownership checks, fixed sudo helpers) through its service specs; only the
unit name, launcher arguments, state checks and health probe differ. The
service runs ``workspace-bridge --state <state> serve``. Settings that would
otherwise come from a shell environment live in the private ``bridge.env``
file in the state folder.
"""
from __future__ import annotations

import json
import os
import platform
import re
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError
from urllib.request import ProxyHandler, build_opener

from .security import BridgeError, open_absolute_dir

ENV_FILE = "bridge.env"
ENV_KEYS = frozenset({
    "WB_NODE_GATEWAY_HOST", "WB_NODE_GATEWAY_URL", "WB_DISCORD_WEBHOOK_URL",
    "WB_ADMIN_ALLOWED_HOSTS", "WB_LOG_LEVEL",
})
_ENV_LINE = re.compile(r"([A-Z][A-Z0-9_]{0,63})=(.*)")
HEALTH_TIMEOUT = 2.0


def load_env_file(state: Path, environ: dict | None = None) -> list[str]:
    """Apply allowlisted settings from ``bridge.env``; real environment wins.

    Returns the keys applied. Unknown keys, malformed lines and an unsafe
    file are errors rather than silently ignored.
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


# ------------------------------------------------------------- state checks
def _read_config(state: Path) -> dict:
    config = json.loads((state / "config.json").read_text(encoding="utf-8"))
    if not isinstance(config, dict) or config.get("schema_version") != 1:
        raise BridgeError("Unsupported Bridge configuration", "bridge_config_invalid")
    return config


def _systemd_validate(state: Path, owner_uid: int, *,
                      stat_fn: Callable[[Path], os.stat_result] | None = None) -> tuple[dict, str]:
    from .node_systemd import STATE_MODE, _check_dir_as, _check_file_as
    _check_dir_as(state, name="Bridge state", mode=STATE_MODE, owner_uid=owner_uid, stat_fn=stat_fn)
    try:
        os.close(open_absolute_dir(str(state)))
    except BridgeError:
        raise BridgeError("Bridge state path is not a safe directory", "bridge_state_unsafe") from None
    _check_file_as(state / "config.json", name="config.json", owner_uid=owner_uid, stat_fn=stat_fn)
    for name in ("bridge.sqlite3", "admin-account.json", ENV_FILE):
        _check_file_as(state / name, name=name, owner_uid=owner_uid, required=False, stat_fn=stat_fn)
    try:
        return _read_config(state), ""
    except (OSError, UnicodeError, ValueError):
        raise BridgeError("Bridge configuration is not valid JSON", "bridge_config_invalid") from None


def _systemd_status(state: Path, owner_uid: int, *,
                    stat_fn: Callable[[Path], os.stat_result] | None = None):
    try:
        config, token = _systemd_validate(state, owner_uid, stat_fn=stat_fn)
    except BridgeError as exc:
        return None, None, exc.code
    return config, token, None


def _launchd_validate(state: Path) -> tuple[dict, str]:
    from .cli import load_config
    return load_config(state), ""


def _launchd_status(state: Path):
    try:
        config, token = _launchd_validate(state)
    except BridgeError as exc:
        return None, None, exc.code
    except (OSError, ValueError):
        return None, None, "bridge_config_invalid"
    return config, token, None


def probe_bridge(config: dict, _token: str = "", _state: Path | None = None) -> dict:
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


def _listen(config: dict) -> dict:
    return {"mcp_port": config.get("mcp_port"), "admin_port": config.get("admin_port"),
            "gateway_port": config.get("gateway_port", 8767)}


def systemd_spec():
    from .node_systemd import ServiceSpec
    return ServiceSpec(
        unit_name="workspace-bridge.service",
        description="Workspace Bridge",
        serve_args=lambda state: ["--state", str(state), "serve"],
        validate_state=_systemd_validate,
        config_status=_systemd_status,
        probe=probe_bridge,
        temp_prefix=".workspace-bridge.",
        listen=_listen,
    )


def launchd_spec():
    from .node_launchd import LaunchAgentSpec
    return LaunchAgentSpec(
        label="com.workspace-bridge.bridge",
        console_script="workspace-bridge",
        module="workspace_bridge.cli",
        validate_state=_launchd_validate,
        config_status=_launchd_status,
        probe=probe_bridge,
        listen=_listen,
    )


def _bridge_error(exc: BridgeError) -> BridgeError:
    """Shared lifecycle errors say Node; report them as Bridge errors."""
    code = exc.code.replace("node_service_", "bridge_service_", 1)
    return BridgeError(str(exc).replace("Node ", "Bridge "), code)


def run_action(action: str, state: Path, *, platform_name: str | None = None,
               manager=None) -> dict:
    """Run one service action for the Bridge on this host's service manager."""
    name = platform_name if platform_name is not None else platform.system()
    try:
        if name == "Darwin":
            from . import node_launchd as backend
            manager = manager or backend.LaunchdManager(spec=launchd_spec())
        elif name == "Linux":
            from . import node_systemd as backend
            manager = manager or backend.SystemdManager(spec=systemd_spec())
        else:
            raise BridgeError("Bridge service commands require macOS launchd or Linux systemd",
                              "node_service_unsupported_platform")
        actions = {
            "install": backend.install_service, "status": backend.service_status,
            "start": backend.start_service, "stop": backend.stop_service,
            "restart": backend.restart_service, "uninstall": backend.uninstall_service,
        }
        return actions[action](state, manager=manager)
    except BridgeError as exc:
        raise _bridge_error(exc) from None
