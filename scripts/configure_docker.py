#!/usr/bin/env python3
"""Prepare Compose .env and a private state directory; never starts Docker.

Standard library only. Run as your normal Linux/macOS/WSL user, not with sudo.
No workspace, permission policy, project source or credential is changed.
"""
from __future__ import annotations
import argparse
import os
from pathlib import Path
import stat
import sys

RESERVED = ('/state', '/opt', '/usr', '/etc', '/bin', '/sbin', '/lib', '/lib64', '/dev', '/proc', '/sys')


def overlaps(a: Path, b: Path) -> bool:
    return a == b or a in b.parents or b in a.parents


def env_path(path: Path) -> str:
    value = str(path)
    if any(ord(c) < 32 or ord(c) == 127 or c in "'\\:" for c in value):
        raise ValueError("Paths must be plain POSIX paths without quotes, backslashes, colons or control characters")
    return "'" + value + "'"


def configure(state: Path, output: Path, mcp_port: int = 8765,
              admin_port: int = 8766) -> Path:
    uid, gid = os.getuid(), os.getgid()
    if uid == 0:
        raise ValueError("Run as a normal host user, not root/sudo; the container refuses root")
    if (not 1024 <= mcp_port <= 65535 or not 1024 <= admin_port <= 65535
            or mcp_port == admin_port):
        raise ValueError("Choose distinct host ports in 1024..65535")
    state = state.expanduser().absolute()
    if state.is_symlink():
        raise ValueError("State path must not be a symlink")
    state = state.resolve()
    output = output.expanduser().absolute()
    package = Path(__file__).resolve().parents[1]
    if any(overlaps(state, Path(p)) for p in RESERVED):
        raise ValueError("State path overlaps a reserved container path")
    if overlaps(package, state):
        raise ValueError("Keep private state outside the source checkout")
    if output == state or state in output.parents:
        raise ValueError("Keep Compose configuration outside private state")
    # Compose single quotes preserve spaces, # and $ without env interpolation.
    state_value = env_path(state)
    if output.exists() or output.is_symlink():
        raise ValueError("Output already exists; edit it deliberately or choose --output .env.new")
    if not output.parent.is_dir():
        raise ValueError("Output directory must already exist")
    if state.exists():
        st = state.stat()
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != uid or st.st_mode & 0o077:
            raise ValueError("Existing state must be owned by your user and mode 0700; it was not modified")
    else:
        state.mkdir(parents=True, mode=0o700)
    text = ("# Local Compose settings; no credentials. Keep this file outside projects.\n"
            f"WB_UID={uid}\nWB_GID={gid}\nWB_STATE_DIR={state_value}\n"
            f"WB_MCP_PORT={mcp_port}\nWB_ADMIN_PORT={admin_port}\n")
    fd = os.open(output, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as handle:
        handle.write(text)
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir', type=Path, default=Path.home()/'.local/state/workspace-bridge-docker')
    parser.add_argument('--mcp-port', type=int, default=8765)
    parser.add_argument('--admin-port', type=int, default=8766)
    parser.add_argument('--output', type=Path, default=Path('.env'))
    args = parser.parse_args()
    try:
        if os.name != 'posix':
            raise ValueError('Use Linux, macOS or WSL2, not native Windows paths')
        output = configure(args.state_dir, args.output, args.mcp_port, args.admin_port)
    except (OSError, ValueError) as exc:
        print(f'Compose setup: {exc}', file=sys.stderr)
        raise SystemExit(1) from None
    print(f'Created {output}. No Docker command was run and no workspace was enabled.')
    print('Next: docker compose config --quiet && docker compose up -d --build')
    print('Admin token: docker compose exec bridge workspace-bridge --state /state show-admin-token')


if __name__ == '__main__':
    main()
