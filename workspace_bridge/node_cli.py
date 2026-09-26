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
from .node_launchd import default_node_state as _default_node_state
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


def default_node_state() -> Path:
    return _default_node_state()


def select_service_backend(platform_name: str | None = None) -> str:
    """Return ``launchd`` on macOS, ``systemd`` on Linux, else raise."""
    import platform as _platform

    name = platform_name if platform_name is not None else _platform.system()
    if name == "Darwin":
        return "launchd"
    if name == "Linux":
        return "systemd"
    raise BridgeError(
        "workspace-bridge node service commands require macOS launchd or "
        "Linux systemd on this host",
        "node_service_unsupported_platform",
    )


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
    service = sub.add_parser("service", help="Manage the Node service (macOS launchd or Linux systemd system service)")
    service_sub = service.add_subparsers(dest="service_command", required=True)
    service_sub.add_parser("install", help="Install and start the managed system service")
    service_sub.add_parser("status", help="Show system service, listen and Node health status")
    service_sub.add_parser("start", help="Start the managed system service")
    service_sub.add_parser("stop", help="Stop the managed system service")
    service_sub.add_parser("restart", help="Restart the managed system service")
    service_sub.add_parser("uninstall", help="Remove only the managed system unit")
    args = parser.parse_args(argv)
    state = args.state.expanduser().absolute()
    os.umask(0o077)
    try:
        if args.command == "init":
            config = initialize_node(state, args.allow_root, args.host, args.port)
            print(f"Initialized Node at {state}; API: http://{args.host}:{config['port']}")
            print(f"Run workspace-bridge node --state {state} serve for foreground use, or install the persistent service (macOS launchd or Linux systemd system service) with service install.")
            print("Use show-token once to add this Node in Bridge.")
            return
        if args.command == "service":
            backend = select_service_backend()
            if backend == "launchd":
                from .node_launchd import (LaunchdManager, install_service,
                                           restart_service, service_status,
                                           start_service, stop_service,
                                           uninstall_service)

                manager = LaunchdManager()
                actions = {
                    "install": lambda: install_service(state, manager=manager),
                    "status": lambda: service_status(state, manager=manager),
                    "start": lambda: start_service(state, manager=manager),
                    "stop": lambda: stop_service(state, manager=manager),
                    "restart": lambda: restart_service(state, manager=manager),
                    "uninstall": lambda: uninstall_service(state, manager=manager),
                }
            else:
                from .node_systemd import (SystemdManager,
                                           install_service as systemd_install,
                                           restart_service as systemd_restart,
                                           service_status as systemd_status,
                                           start_service as systemd_start,
                                           stop_service as systemd_stop,
                                           uninstall_service as systemd_uninstall)

                manager = SystemdManager()
                actions = {
                    "install": lambda: systemd_install(state, manager=manager),
                    "status": lambda: systemd_status(state, manager=manager),
                    "start": lambda: systemd_start(state, manager=manager),
                    "stop": lambda: systemd_stop(state, manager=manager),
                    "restart": lambda: systemd_restart(state, manager=manager),
                    "uninstall": lambda: systemd_uninstall(state, manager=manager),
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
        if args.command == "serve":
            # Managed LaunchAgents carry WB_NATIVE_LOG_GUARD=1 on Darwin;
            # spawn one package-owned log-guard child (stdio to DEVNULL,
            # fixed argv, no shell) then serve normally. Foreground use
            # without the marker never spawns the guard. The guard never
            # supervises or restarts the service.
            try:
                from .native_logs import spawn_log_guard as _spawn_guard
                _spawn_guard(state)
            except Exception:
                pass
        asyncio.run(serve_node(state, config))
    except (BridgeError, OSError, ValueError) as exc:
        print(f"workspace-bridge-node: {exc}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
