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
from . import __version__
from .api import make_admin, make_mcp
from .oplog import configure_operational_logging, emit, error_code
from .wbrp import adapters_from_environment
from .security import BridgeError, digest, open_absolute_dir
from .service import Service
import logging
_ops_log = logging.getLogger("workspace_bridge.ops")

DEFAULT_STATE = Path.home() / ".local" / "state" / "workspace-bridge"

ADMIN_ALLOWED_HOSTS_ENV = "WB_ADMIN_ALLOWED_HOSTS"
MAX_ADMIN_ALLOWED_HOSTS = 20


def parse_admin_allowed_hosts(raw: str) -> tuple[str, ...]:
    """Parse WB_ADMIN_ALLOWED_HOSTS into validated bare hostnames/IPs.

    Comma-separated, case-insensitive, no ports/schemes/wildcards. Empty
    string means loopback-only (default, fail-closed). Raises BridgeError on
    any invalid entry rather than silently ignoring it.
    """
    if not isinstance(raw, str):
        raise BridgeError(f"{ADMIN_ALLOWED_HOSTS_ENV} must be a comma-separated string")
    if not raw.strip():
        return ()
    seen: list[str] = []
    for part in raw.split(","):
        name = part.strip().lower()
        if not name:
            continue
        if len(seen) >= MAX_ADMIN_ALLOWED_HOSTS:
            raise BridgeError(f"{ADMIN_ALLOWED_HOSTS_ENV} accepts at most {MAX_ADMIN_ALLOWED_HOSTS} hosts")
        if (len(name) > 253 or "/" in name or "\\" in name or "@" in name
                or " " in name or "\t" in name or "*" in name or "://" in name
                or ":" in name or "#" in name or "?" in name or "&" in name
                or "=" in name or "[" in name or "]" in name):
            raise BridgeError(f"{ADMIN_ALLOWED_HOSTS_ENV} entry {name!r} must be a bare hostname/IP without port, scheme or wildcard (IPv4/hostname only)")
        if any(ord(c) < 32 or ord(c) == 127 for c in name):
            raise BridgeError(f"{ADMIN_ALLOWED_HOSTS_ENV} entry {name!r} contains control characters")
        allowed_chars = set("abcdefghijklmnopqrstuvwxyz0123456789.-_")
        if any(c not in allowed_chars for c in name):
            raise BridgeError(f"{ADMIN_ALLOWED_HOSTS_ENV} entry {name!r} uses invalid characters")
        if name.startswith((".", "-", "_")) or name.endswith((".", "-", "_")) or ".." in name:
            raise BridgeError(f"{ADMIN_ALLOWED_HOSTS_ENV} entry {name!r} is not a valid hostname/IP")
        if name not in seen:
            seen.append(name)
    return tuple(seen)


def admin_allowed_hosts_from_env(environ: dict | None = None) -> tuple[str, ...]:
    env = environ if environ is not None else os.environ
    return parse_admin_allowed_hosts(env.get(ADMIN_ALLOWED_HOSTS_ENV, ""))


def resolve_bind_hosts(container_mode: bool, extra_admin_hosts: tuple[str, ...]) -> tuple[str, str]:
    """Return (mcp_bind, admin_bind). MCP stays loopback natively; the admin
    listener widens to 0.0.0.0 only when explicitly allowed hosts exist."""
    mcp_bind = "0.0.0.0" if container_mode else "127.0.0.1"
    admin_bind = "0.0.0.0" if (container_mode or extra_admin_hosts) else "127.0.0.1"
    return mcp_bind, admin_bind


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
                mcp_public_port: int | None = None, admin_public_port: int | None = None,
                extra_admin_hosts: tuple[str, ...] | None = None):
    # Only explicit local startup configuration broadens the bind address.
    # Native CLI startup retains loopback binding by default; setting
    # WB_ADMIN_ALLOWED_HOSTS widens only the admin listener to 0.0.0.0 so the
    # explicitly allowed Host values can actually connect. MCP stays loopback.
    if extra_admin_hosts is None:
        extra_admin_hosts = admin_allowed_hosts_from_env()
    mcp_bind, admin_bind = resolve_bind_hosts(container_mode, extra_admin_hosts)
    # Operational logs use the configured WB_LOG_LEVEL; Uvicorn's own access
    # logs stay disabled and its internal level stays warning (no noisy
    # request logs just to create output).
    configure_operational_logging()
    try:
        total = enabled = 0
        for ws in service.list_workspaces():
            total += 1
            if ws.get("enabled"):
                enabled += 1
    except Exception:  # noqa: BLE001 - counts are best-effort only
        total = enabled = 0
    emit(_ops_log, "INFO", "bridge", "bridge_ready", version=__version__,
         runtime_configured=bool(service.run_coordinator.adapters),
         workspace_count=total, enabled_count=enabled)
    configs = [
        uvicorn.Config(make_mcp(service, config["mcp_port"], public_port=mcp_public_port,
                                container_mode=container_mode), host=mcp_bind, port=config["mcp_port"],
                       access_log=False, proxy_headers=False, log_level="warning"),
        uvicorn.Config(make_admin(service, config["admin_token_hash"], config["admin_port"],
                                 public_port=admin_public_port, public_mcp_port=mcp_public_port,
                                 container_mode=container_mode,
                                 extra_hosts=extra_admin_hosts), host=admin_bind, port=config["admin_port"],
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


def _emit_serve_startup_error(exc: BaseException) -> None:
    """Emit one sanitized structured ERROR for a serve startup failure.

    A plain fallback handler is attached when normal WB_LOG_LEVEL
    configuration itself failed, so the record is still emitted (as ERROR;
    a safe INFO fallback for the transport is acceptable). Never logs raw
    exception text, paths, env values, or credentials. Never raises.
    """
    try:
        if not _ops_log.handlers:
            fallback = logging.StreamHandler(sys.stderr)
            fallback.setFormatter(logging.Formatter("%(message)s"))
            _ops_log.addHandler(fallback)
        emit(_ops_log, "ERROR", "bridge", "process_error",
             code=error_code(exc), source="startup", action="serve")
    except Exception:  # noqa: BLE001 - startup logging must never raise
        pass


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
        # Fail fast on an invalid WB_LOG_LEVEL before any service work; serve()
        # re-resolves it when listeners start.
        configure_operational_logging()
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
                              adapters=adapters_from_environment())
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
                        "runtimes": service.runtime_diagnostics(),
                        "runtime_policies": service.runtime_policy_summaries(),
                        "admin_allowed_hosts": list(admin_allowed_hosts_from_env())}, indent=2))
                else:
                    extra_admin_hosts = admin_allowed_hosts_from_env()
                    print(f"MCP: http://127.0.0.1:{config['mcp_port']}/mcp | Local admin: http://127.0.0.1:{config['admin_port']}/")
                    if extra_admin_hosts:
                        print(f"WARNING: {ADMIN_ALLOWED_HOSTS_ENV}={','.join(extra_admin_hosts)} widens the admin listener to 0.0.0.0 with those Host values allowed. Prefer SSH port-forwarding or VPN; HTTP bears the admin token in clear. Never tunnel the manager.", file=sys.stderr)
                    if args.container:
                        print("Container bind: 0.0.0.0; publish both ports on host 127.0.0.1 ONLY. Do not tunnel management.")
                        print(f"Host MCP: http://127.0.0.1:{args.mcp_public_port}/mcp | Host admin: http://127.0.0.1:{args.admin_public_port}/")
                    asyncio.run(serve(service, config, container_mode=args.container,
                                      mcp_public_port=args.mcp_public_port, admin_public_port=args.admin_public_port,
                                      extra_admin_hosts=extra_admin_hosts))
            finally:
                service.close()
        finally:
            os.close(lock_fd)
    except (BridgeError, OSError, ValueError) as exc:
        if args.command == "serve":
            # Production serve path: one sanitized structured ERROR record
            # only. The raw exception message (paths, values) is never
            # printed here; interactive commands below keep human output.
            _emit_serve_startup_error(exc)
        else:
            print(f"workspace-bridge: {exc}", file=sys.stderr)
        raise SystemExit(1)

if __name__ == "__main__":
    main()
