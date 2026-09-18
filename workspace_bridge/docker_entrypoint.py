"""Local Docker entrypoint, never an MCP tool.

Only initializes fresh private state. Existing credentials, mappings, enabled
flags and write policy are not rewritten. Internal ports are fixed; published
host ports are independent runtime options passed to the existing CLI.
"""
from __future__ import annotations

import fcntl
import os
from pathlib import Path
import stat
import sys

from .cli import initialize, load_config, main as cli_main
from .security import BridgeError, open_absolute_dir

INTERNAL_MCP_PORT = 8765
INTERNAL_ADMIN_PORT = 8766


def port_value(name: str, default: int) -> int:
    text = os.environ.get(name, str(default))
    if not text.isascii() or not text.isdecimal() or not 1024 <= int(text) <= 65535:
        raise BridgeError(f"{name} must be a TCP port in 1024..65535")
    return int(text)


def project_parent(value: str) -> Path:
    path = Path(value)
    if (not path.is_absolute() or len(path.parts) < 3 or str(path) != value
            or path == Path.home() or path.resolve(strict=True) != path):
        raise BridgeError("WB_PROJECTS_DIR must be a canonical absolute dedicated project-parent path")
    # Do not let a configured bind hide the application, state or system paths.
    reserved = (Path('/state'), Path('/opt'), Path('/usr'), Path('/etc'), Path('/bin'),
                Path('/sbin'), Path('/lib'), Path('/lib64'), Path('/dev'), Path('/proc'), Path('/sys'))
    if any(path == p or p in path.parents or path in p.parents for p in reserved):
        raise BridgeError("Project mount overlaps a reserved container path")
    fd = open_absolute_dir(str(path))
    os.close(fd)
    return path


def bootstrap(state: Path, parent: Path) -> dict:
    """Called with a prepared, user-owned state bind, not a root-owned volume."""
    if not state.is_absolute() or state.resolve(strict=True) != state:
        raise BridgeError("Container state must be an existing canonical directory")
    if state == parent or state in parent.parents or parent in state.parents:
        raise BridgeError("Private state and project parent must not overlap")
    st = state.stat()
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
        raise BridgeError("State bind must be owned by WB_UID and mode 0700; prepare it on the host")
    fd = open_absolute_dir(str(state)); os.close(fd)
    lock = os.open(state / 'bootstrap.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        lock_st = os.fstat(lock)
        if not stat.S_ISREG(lock_st.st_mode) or lock_st.st_nlink != 1 or lock_st.st_uid != os.getuid():
            raise BridgeError("Unsafe bootstrap lock")
        fcntl.flock(lock, fcntl.LOCK_EX)
        config_path = state / 'config.json'
        if not config_path.exists():
            if config_path.is_symlink() or any(p.name != 'bootstrap.lock' for p in state.iterdir()):
                raise BridgeError("Missing config in nonempty state; restore a backup or use fresh private state")
            initialize(state, [str(parent)], INTERNAL_MCP_PORT, INTERNAL_ADMIN_PORT)
            print("Initialized fresh Docker state. No workspace is enabled. Retrieve the admin token with docker compose exec.", flush=True)
        config = load_config(state)
        if (config.get('allowed_parents') != [str(parent)]
                or config.get('mcp_port') != INTERNAL_MCP_PORT
                or config.get('admin_port') != INTERNAL_ADMIN_PORT):
            raise BridgeError("State configuration does not match this Compose deployment. Do not reinitialize or overwrite it; see docs/DOCKER.md")
        return config
    finally:
        os.close(lock)


def main(argv: list[str] | None = None):
    args = sys.argv[1:] if argv is None else argv
    args = args or ['serve']
    os.umask(0o077)
    try:
        if os.getuid() == 0:
            raise BridgeError("Run the container as a non-root user: set WB_UID/WB_GID to your host user")
        if args not in (['serve'], ['doctor'], ['show-admin-token'], ['rotate-bridge-token']):
            raise BridgeError("Supported container commands: serve, doctor, show-admin-token, rotate-bridge-token")
        parent = project_parent(os.environ.get('WB_PROJECTS_DIR', ''))
        state = Path(os.environ.get('WB_STATE_DIR', '/state'))
        mcp_port = port_value('WB_MCP_PORT', INTERNAL_MCP_PORT)
        admin_port = port_value('WB_ADMIN_PORT', INTERNAL_ADMIN_PORT)
        if mcp_port == admin_port:
            raise BridgeError("Published MCP and management ports must differ")
        if args == ['serve']:
            bootstrap(state, parent)
            cli_main(['--state', str(state), 'serve', '--container',
                      '--mcp-public-port', str(mcp_port), '--admin-public-port', str(admin_port)])
        else:
            # Administrative commands never create missing state implicitly.
            cli_main(['--state', str(state), *args])
    except (BridgeError, OSError, ValueError) as exc:
        print(f"workspace-bridge container: {exc}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
