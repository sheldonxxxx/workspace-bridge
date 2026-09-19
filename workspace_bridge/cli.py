from __future__ import annotations
import argparse
import asyncio
import fcntl
import json
import os
from pathlib import Path
import secrets
import sys

import uvicorn
from .api import make_admin, make_mcp
from .notifications import notifier_from_environment
from .runtime import runtime_from_environment
from .security import BridgeError, digest, open_absolute_dir
from .service import Service

DEFAULT_STATE = Path.home() / ".local" / "state" / "workspace-bridge"


def initialize(state: Path, parents: list[str], mcp_port: int, admin_port: int) -> dict:
    if not parents:
        raise BridgeError("At least one --allow-parent is required; no default broad filesystem access")
    if not 1024 <= mcp_port <= 65535 or not 1024 <= admin_port <= 65535 or mcp_port == admin_port:
        raise BridgeError("Choose distinct unprivileged TCP ports")
    resolved = []
    for value in parents:
        parent = Path(value).expanduser().resolve(strict=True)
        if not parent.is_dir() or len(parent.parts) < 3 or parent == Path.home():
            raise BridgeError("Use a dedicated project-parent directory, not a home or filesystem root")
        resolved.append(str(parent))
    state = state.expanduser().resolve()
    if any(state == Path(p) or Path(p) in state.parents for p in resolved):
        raise BridgeError("Keep server state outside approved project parents")
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    if (state / "config.json").exists():
        raise BridgeError("Already initialized; existing state was not overwritten")
    os.chmod(state, 0o700)
    token = secrets.token_urlsafe(32)
    config = {"schema_version": 1, "allowed_parents": resolved, "mcp_port": mcp_port,
              "admin_port": admin_port, "admin_token_hash": digest(token.encode())}
    for filename, text in (("config.json", json.dumps(config, indent=2)), ("admin-token", token + "\n")):
        fd = os.open(state / filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as out:
            out.write(text)
            out.flush()
            os.fsync(out.fileno())
    return config


def load_config(state: Path) -> dict:
    st = state.stat()
    if st.st_uid != os.getuid() or st.st_mode & 0o077:
        raise BridgeError("State directory must be owned by the service user and mode 0700")
    fd = open_absolute_dir(str(state))
    os.close(fd)
    path = state / "config.json"
    if path.is_symlink() or path.stat().st_mode & 0o077:
        raise BridgeError("config.json must be a regular, private file (0600)")
    config = json.loads(path.read_text())
    if config.get("schema_version") != 1:
        raise BridgeError("Unsupported configuration version")
    return config


async def serve(service: Service, config: dict, *, container_mode: bool = False,
                mcp_public_port: int | None = None, admin_public_port: int | None = None):
    # Only explicit local startup configuration broadens the bind address.
    # Native CLI startup retains loopback binding by default.
    bind_host = "0.0.0.0" if container_mode else "127.0.0.1"
    configs = [
        uvicorn.Config(make_mcp(service, config["mcp_port"], public_port=mcp_public_port,
                                container_mode=container_mode), host=bind_host, port=config["mcp_port"],
                       access_log=False, proxy_headers=False, log_level="warning"),
        uvicorn.Config(make_admin(service, config["admin_token_hash"], config["admin_port"],
                                 public_port=admin_public_port, public_mcp_port=mcp_public_port,
                                 container_mode=container_mode), host=bind_host, port=config["admin_port"],
                       access_log=False, proxy_headers=False, log_level="warning"),
    ]
    servers = [uvicorn.Server(c) for c in configs]
    tasks = [asyncio.create_task(s.serve()) for s in servers]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for server in servers:
            server.should_exit = True
        await asyncio.gather(*tasks, return_exceptions=True)


def main(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(description="Local, workspace-scoped MCP planning and review bridge")
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="Initialize private state and explicit allowed project parents")
    init.add_argument("--allow-parent", action="append", required=True)
    init.add_argument("--mcp-port", type=int, default=8765)
    init.add_argument("--admin-port", type=int, default=8766)
    run = sub.add_parser("serve", help="Start both loopback listeners, single process only")
    run.add_argument("--container", action="store_true",
                     help="Bind 0.0.0.0 INSIDE an isolated container; requires loopback-only published host ports")
    run.add_argument("--mcp-public-port", type=int, help="Exact published loopback port; container mode only")
    run.add_argument("--admin-public-port", type=int, help="Exact published management loopback port; container mode only")
    sub.add_parser("rotate-bridge-token", help="Create/rotate the shared MCP credential while stopped; shown once")
    sub.add_parser("doctor", help="Validate local config and workspace root identities; no network checks")
    sub.add_parser("show-admin-token", help="Print the local UI token to this terminal; never paste it into ChatGPT")
    args = parser.parse_args(argv)
    if args.command == "serve":
        public_ports = (args.mcp_public_port, args.admin_public_port)
        if any(p is not None for p in public_ports) and not args.container:
            parser.error("Published ports require --container; native service remains loopback-only")
        if args.container and (any(p is None or not 1024 <= p <= 65535 for p in public_ports)
                               or public_ports[0] == public_ports[1]):
            parser.error("--container requires distinct --mcp-public-port and --admin-public-port in 1024..65535")
    if os.name != "posix":
        parser.error("This version requires macOS or Linux (or WSL2), not native Windows")
    state = args.state.expanduser().resolve()
    os.umask(0o077)
    try:
        if args.command == "init":
            config = initialize(state, args.allow_parent, args.mcp_port, args.admin_port)
            print(f"Initialized {state}\nLocal management: http://127.0.0.1:{config['admin_port']}/")
            print("Run workspace-bridge serve, then workspace-bridge show-admin-token in another terminal.")
            return
        config = load_config(state)
        if args.command == "show-admin-token":
            token_path = state / "admin-token"
            if token_path.is_symlink():
                raise BridgeError("Unsafe token file")
            print(token_path.read_text().strip())
            return
        lock_fd = os.open(state / "process.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            if args.command in ("serve", "rotate-bridge-token"):
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise BridgeError("A bridge process already owns this state directory; rotate online in the local manager") from None
            service = Service(state, config, recover_incomplete=args.command == "serve",
                              runtime=runtime_from_environment(), notifier=notifier_from_environment())
            try:
                if args.command == "rotate-bridge-token":
                    result = service.manage_bridge("rotate_token")
                    print(result["token"])
                    print("Shared bridge credential shown once. Authorizes every enabled mapping. Keep it out of ChatGPT.", file=sys.stderr)
                elif args.command == "doctor":
                    report = []
                    for ws in service.list_workspaces():
                        try:
                            with service.safe_root(ws):
                                pass
                            result = "root_identity_ok"
                        except BridgeError as exc:
                            result = exc.code
                        report.append({"workspace": ws["name"], "enabled": bool(ws["enabled"]), "check": result})
                    print(json.dumps({"config": "ok", "workspaces": report,
                        "bridge": service.bridge_status(), "tunnel": "not_checked", "chatgpt": "not_checked",
                        "opencode": service.orchestrator.runtime_status(),
                        "model_policy": service.orchestrator.model_policy_status()}, indent=2))
                else:
                    print(f"MCP: http://127.0.0.1:{config['mcp_port']}/mcp | Local admin: http://127.0.0.1:{config['admin_port']}/")
                    if args.container:
                        print("Container bind: 0.0.0.0; publish both ports on host 127.0.0.1 ONLY. Do not tunnel management.")
                        print(f"Host MCP: http://127.0.0.1:{args.mcp_public_port}/mcp | Host admin: http://127.0.0.1:{args.admin_public_port}/")
                    asyncio.run(serve(service, config, container_mode=args.container,
                                      mcp_public_port=args.mcp_public_port, admin_public_port=args.admin_public_port))
            finally:
                service.close()
        finally:
            os.close(lock_fd)
    except (BridgeError, OSError, ValueError) as exc:
        print(f"workspace-bridge: {exc}", file=sys.stderr)
        raise SystemExit(1)

if __name__ == "__main__":
    main()
