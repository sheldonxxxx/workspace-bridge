"""Command line entry point for the private workspace-bridge-node service."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import secrets
import sys

import uvicorn

from .node_api import make_node_api
from .node_launchd import (LaunchdManager, default_node_state, install_service,
                           restart_service, service_status, start_service,
                           stop_service, uninstall_service)
from .node_service import NodeService
from .security import BridgeError, digest, open_absolute_dir


def initialize_node(state: Path, roots: list[str], host: str, port: int) -> dict:
    if not roots:
        raise BridgeError("At least one --allow-root is required")
    if not 1024 <= port <= 65535:
        raise BridgeError("Choose an unprivileged TCP port")
    resolved = []
    for raw in roots:
        path = Path(raw).expanduser().resolve(strict=True)
        if not path.is_dir() or path == Path.home() or len(path.parts) < 3:
            raise BridgeError("Use a dedicated project root, not a home or filesystem root")
        if str(path) not in resolved:
            resolved.append(str(path))
    state = state.expanduser().resolve()
    if any(state == Path(root) or Path(root) in state.parents for root in resolved):
        raise BridgeError("Keep Node state outside approved workspace roots")
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(state, 0o700)
    if (state / "node-config.json").exists():
        raise BridgeError("Node already initialized; existing state was not overwritten")
    token = secrets.token_urlsafe(32)
    config = {"schema_version": 1, "allowed_roots": resolved,
              "host": host, "port": port, "node_token_hash": digest(token.encode())}
    for name, content in (("node-config.json", json.dumps(config, indent=2)),
                          ("node-token", token + "\n")):
        fd = os.open(state / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                     os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    return config


def load_node_config(state: Path) -> dict:
    state = state.expanduser().resolve()
    st = state.stat()
    if st.st_uid != os.getuid() or st.st_mode & 0o077:
        raise BridgeError("Node state directory must be owned by the service user and mode 0700")
    fd = open_absolute_dir(str(state))
    os.close(fd)
    path = state / "node-config.json"
    if path.is_symlink() or path.stat().st_mode & 0o077:
        raise BridgeError("node-config.json must be a private file (0600)")
    config = json.loads(path.read_text())
    if config.get("schema_version") != 1 or not isinstance(config.get("allowed_roots"), list):
        raise BridgeError("Unsupported Node configuration")
    return config


async def serve_node(state: Path, config: dict):
    service = NodeService(state, config)
    try:
        server = uvicorn.Server(uvicorn.Config(
            make_node_api(service, config["node_token_hash"]),
            host=config.get("host", "127.0.0.1"), port=config["port"],
            access_log=False, proxy_headers=False, log_level="warning"))
        await server.serve()
    finally:
        service.close()


def main(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(description="Private workspace-bridge Node data-plane service")
    parser.add_argument("--state", type=Path, default=default_node_state())
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="Initialize a Node with host-local allowed roots")
    init.add_argument("--allow-root", action="append", required=True)
    init.add_argument("--host", default="127.0.0.1")
    init.add_argument("--port", type=int, default=8770)
    sub.add_parser("serve", help="Run the private Node API")
    sub.add_parser("show-token", help="Print the Node token once for Bridge configuration")
    service = sub.add_parser("service", help="Manage the macOS per-user Node LaunchAgent")
    service_sub = service.add_subparsers(dest="service_command", required=True)
    service_sub.add_parser("install", help="Install and bootstrap the managed LaunchAgent")
    service_sub.add_parser("status", help="Show LaunchAgent, listen and Node health status")
    service_sub.add_parser("start", help="Start the managed LaunchAgent")
    service_sub.add_parser("stop", help="Stop the managed LaunchAgent")
    service_sub.add_parser("restart", help="Restart the managed LaunchAgent")
    service_sub.add_parser("uninstall", help="Remove only the managed LaunchAgent plist")
    args = parser.parse_args(argv)
    state = args.state.expanduser().absolute()
    os.umask(0o077)
    try:
        if args.command == "init":
            config = initialize_node(state, args.allow_root, args.host, args.port)
            print(f"Initialized Node at {state}; API: http://{args.host}:{config['port']}")
            print("Run workspace-bridge-node serve for foreground use, or on macOS install the persistent LaunchAgent with service install.")
            print("Use show-token once to add this Node in Bridge.")
            return
        if args.command == "service":
            manager = LaunchdManager()
            actions = {
                "install": lambda: install_service(state, manager=manager),
                "status": lambda: service_status(state, manager=manager),
                "start": lambda: start_service(state, manager=manager),
                "stop": lambda: stop_service(state, manager=manager),
                "restart": lambda: restart_service(state, manager=manager),
                "uninstall": lambda: uninstall_service(state, manager=manager),
            }
            result = actions[args.service_command]()
            print(json.dumps(result, indent=2, sort_keys=True))
            return
        config = load_node_config(state)
        if args.command == "show-token":
            path = state / "node-token"
            if path.is_symlink() or path.stat().st_mode & 0o077:
                raise BridgeError("Unsafe Node token file")
            print(path.read_text().strip())
            return
        asyncio.run(serve_node(state, config))
    except (BridgeError, OSError, ValueError) as exc:
        print(f"workspace-bridge-node: {exc}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
