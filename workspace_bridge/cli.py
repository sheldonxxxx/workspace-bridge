from __future__ import annotations
import argparse
import asyncio
import fcntl
import json
import os
from pathlib import Path
import ipaddress
import re
import secrets
import sqlite3
import sys
import threading
from datetime import datetime, timezone

import uvicorn
from . import __version__
from .api import make_admin, make_mcp
from .oplog import configure_operational_logging, emit, error_code
from .security import BridgeError, open_absolute_dir
from .admin_account import AdminAccount
from .service import Service
from .diagnostics import failure_report
import logging
_ops_log = logging.getLogger("workspace_bridge.ops")

DEFAULT_STATE = Path.home() / ".local" / "state" / "workspace-bridge"

ADMIN_ALLOWED_HOSTS_ENV = "WB_ADMIN_ALLOWED_HOSTS"
MAX_ADMIN_ALLOWED_HOSTS = 20
GATEWAY_HOST_ENV = "WB_NODE_GATEWAY_HOST"
GATEWAY_URL_ENV = "WB_NODE_GATEWAY_URL"
DEFAULT_GATEWAY_PORT = 8767
_GATEWAY_HOSTNAME = re.compile(r"(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?")


def gateway_settings(config: dict, environ: dict | None = None) -> dict:
    """Bind address, port and advertised URL of the Node gateway.

    Paired Nodes dial this listener. It binds loopback unless
    WB_NODE_GATEWAY_HOST names another address, normally a VPN interface.
    WB_NODE_GATEWAY_URL overrides the URL put into pairing strings, for
    example a VPN hostname; the certificate is pinned, so any name works.
    """
    from .pairing import GATEWAY_PATH, gateway_url
    env = environ if environ is not None else os.environ
    port = config.get("gateway_port", DEFAULT_GATEWAY_PORT)
    if type(port) is not int or not 1024 <= port <= 65535:
        raise BridgeError("Invalid Node gateway port in config.json")
    if port in (config.get("mcp_port"), config.get("admin_port")):
        raise BridgeError("The Node gateway port must differ from the MCP and Manager ports")
    host = (env.get(GATEWAY_HOST_ENV) or "127.0.0.1").strip()
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
        if not _GATEWAY_HOSTNAME.fullmatch(host):
            raise BridgeError(f"{GATEWAY_HOST_ENV} must be an IP address or hostname")
    url = (env.get(GATEWAY_URL_ENV) or "").strip()
    if not url:
        if address is not None and address.is_unspecified:
            raise BridgeError(f"Set {GATEWAY_URL_ENV} when {GATEWAY_HOST_ENV} is a wildcard address")
        url = gateway_url(host, port)
    if (not url.startswith("wss://") or not url.endswith(GATEWAY_PATH) or len(url) > 2048
            or any(ord(char) < 33 for char in url)):
        raise BridgeError(f"{GATEWAY_URL_ENV} must be a wss:// URL ending in {GATEWAY_PATH}")
    return {"host": host, "port": port, "url": url}


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


def initialize(state: Path, mcp_port: int, admin_port: int,
               gateway_port: int = DEFAULT_GATEWAY_PORT) -> dict:
    ports = (mcp_port, admin_port, gateway_port)
    if any(not 1024 <= port <= 65535 for port in ports) or len(set(ports)) != 3:
        raise BridgeError("Choose distinct unprivileged TCP ports")
    state = state.expanduser().resolve()
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    if (state / "config.json").exists():
        raise BridgeError("Already initialized; existing state was not overwritten")
    os.chmod(state, 0o700)
    cursor_secret = secrets.token_urlsafe(32)
    config = {"schema_version": 1, "mcp_port": mcp_port,
              "admin_port": admin_port, "gateway_port": gateway_port,
              "cursor_secret": cursor_secret}
    for filename, text in (("config.json", json.dumps(config, indent=2)),):
        fd = os.open(state / filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as out:
            out.write(text)
            out.flush()
            os.fsync(out.fileno())
    AdminAccount(state).ensure()
    from .pairing import ensure_gateway_certificate
    ensure_gateway_certificate(state)
    return config


def enable_local_node(state: Path, roots: list[str], name: str = "This machine") -> dict:
    """Create the Node embedded in this Bridge and register it; no Node service needed."""
    from .node_cli import initialize_node
    from .schema_upgrade import add_node_transports
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80 or any(ord(c) < 32 for c in name):
        raise BridgeError("Node name must contain 1 to 80 characters")
    db = sqlite3.connect(state / "bridge.sqlite3", timeout=10)
    db.row_factory = sqlite3.Row
    try:
        if db.execute("SELECT name FROM sqlite_master WHERE name='nodes'").fetchone() is None:
            raise BridgeError("Start the Bridge once before enabling the local Node")
        add_node_transports(db)
        if db.execute("SELECT 1 FROM nodes WHERE transport='local'").fetchone():
            raise BridgeError("This Bridge already has a local Node")
        if db.execute("SELECT 1 FROM nodes WHERE name=? COLLATE NOCASE", (name.strip(),)).fetchone():
            raise BridgeError("Node name is already in use")
        initialize_node(state / Service.LOCAL_NODE_DIR, roots, "127.0.0.1", 8770)
        node_id = "node_" + secrets.token_hex(12)
        stamp = datetime.now(timezone.utc).isoformat()
        with db:
            db.execute("INSERT INTO nodes(id,name,base_url,token,enabled,revision,created,updated,"
                       "transport,public_key) VALUES(?,?,'','',1,?,?,?,'local','')",
                       (node_id, name.strip(), "rev_" + secrets.token_hex(16), stamp, stamp))
    finally:
        db.close()
    return {"node_id": node_id, "name": name.strip()}


def create_pairing_offline(state: Path, config: dict, name: str, replace: bool) -> dict:
    """Issue a pairing string from the CLI, safely beside a running Bridge.

    Uses its own short SQLite connection rather than a Service, so no second
    set of background workers starts.
    """
    from .node_gateway import create_pairing
    from .pairing import format_pairing, gateway_fingerprint
    from .schema_upgrade import add_node_transports
    settings = gateway_settings(config)
    fingerprint = gateway_fingerprint(state)
    db = sqlite3.connect(state / "bridge.sqlite3", timeout=10)
    db.row_factory = sqlite3.Row
    try:
        if db.execute("SELECT name FROM sqlite_master WHERE name='nodes'").fetchone() is None:
            raise BridgeError("Start the Bridge once before pairing a Node")
        add_node_transports(db)
        code, expires = create_pairing(db, name, replace=replace)
    finally:
        db.close()
    return {"pairing": format_pairing(settings["url"], fingerprint, code), "expires": expires}


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


_DOCTOR_SECTIONS = (
    ("core", "Core"), ("nodes", "Nodes"), ("workspaces", "Workspaces"), ("adapters", "Adapters"),
    ("runnable_routes", "Runnable routes"), ("git_evidence", "Git evidence"),
    ("release", "Release"),
)


def _print_doctor_human(report: dict) -> None:
    overall = report["overall"]
    print(f"Overall: {overall['status'].upper()} — {overall['summary']}")
    print("Checks: " + ", ".join(
        f"{key}={value}" for key, value in overall["counts"].items()))
    checks = report.get("checks", [])
    for section, title in _DOCTOR_SECTIONS:
        print(f"\n{title}:")
        rows = [check for check in checks if check.get("section") == section]
        if section == "runnable_routes":
            rows += [check for check in checks if check.get("section") == "models_profiles"]
            routes = report.get("runnable_routes", [])
            if not routes and not rows:
                print("  No configured workspace/adapter routes.")
            for route in routes:
                state = "ready" if route.get("ready") else "blocked"
                model = route.get("default_model_selector")
                suffix = f"; default model {model}" if model else ""
                print(f"  [{state}] {route.get('workspace_name')} / {route.get('adapter_name')} ({route.get('runtime_type')}){suffix}")
                if route.get("blockers"):
                    print("    blockers: " + ", ".join(route["blockers"]))
        if section in {"workspaces", "models_profiles", "adapters", "core", "git_evidence", "nodes"}:
            for check in rows:
                scope = []
                if check.get("workspace_id"):
                    scope.append(check["workspace_id"])
                if check.get("adapter_id"):
                    scope.append(check["adapter_id"])
                prefix = f"{' / '.join(scope)}: " if scope else ""
                print(f"  [{check['status']}] {prefix}{check['summary']}")
                if check["status"] in {"failed", "action_required"}:
                    print(f"    code: {check['code']}")
                    if check.get("remediation"):
                        instructions = [line.strip() for line in check["remediation"].splitlines()
                                        if line.strip()]
                        if len(instructions) > 1:
                            print("    fix:")
                            for instruction in instructions:
                                print(f"      - {instruction}")
                        elif instructions:
                            print(f"    fix: {instructions[0]}")


def _doctor_state_is_missing(state: Path) -> bool:
    """Return whether the expected state directory or config file is absent.

    Other metadata errors are treated as unreadable state, so permissions and
    malformed existing locations do not get mislabeled as uninitialized.
    """
    try:
        state.stat()
    except FileNotFoundError:
        return True
    except OSError:
        return False

    try:
        (state / "config.json").lstat()
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return False


def _doctor(state: Path, *, offline: bool, as_json: bool,
            container_mode: bool = False, mcp_public_port: int | None = None,
            admin_public_port: int | None = None) -> int:
    try:
        config = load_config(state)
    except Exception:  # noqa: BLE001 - initialization failures stay safe and canonical
        state_missing = _doctor_state_is_missing(state)
        if state_missing:
            summary = "No Workspace Bridge state was found at the selected state location."
            remediation = (
                "Native setup: run `workspace-bridge init`, then add an authoritative Node in the Manager.\n"
                "Then rerun `workspace-bridge doctor`.\n"
                "For Docker, run `docker exec workspace-bridge workspace-bridge --state /state doctor`."
            )
        else:
            summary = "Local configuration could not be read."
            remediation = (
                "Check local state/config file permissions and configuration validity, "
                "then rerun `workspace-bridge doctor`."
            )
        report = failure_report(mode="offline" if offline else "live",
                                summary=summary)
        report["checks"][0]["remediation"] = remediation[:240]
        _print_doctor(report, as_json)
        return 1

    try:
        from .bridge_service import load_env_file
        load_env_file(state)
        extra_hosts = admin_allowed_hosts_from_env()
        invalid_admin_host_config = False
    except BridgeError:
        extra_hosts = ()
        invalid_admin_host_config = True
    listener = {
        "mcp_port": mcp_public_port or config.get("mcp_port"),
        "admin_port": admin_public_port or config.get("admin_port"),
        "container_mode": container_mode,
        "extra_admin_host_count": len(extra_hosts),
        "invalid_admin_host_config": invalid_admin_host_config,
    }
    try:
        service = Service(state, config, read_only=True,
                          run_coordinator_background=False)
    except BridgeError as exc:
        report = failure_report(mode="offline" if offline else "live",
                                code=exc.code,
                                summary=("Bridge state is incompatible with schema v4; use a fresh state path."
                                         if exc.code == "state_schema_incompatible"
                                         else "Private Bridge state database could not be opened."))
        _print_doctor(report, as_json)
        return 1
    except Exception:  # noqa: BLE001 - no private initialization detail is printed
        report = failure_report(mode="offline" if offline else "live",
                                code="core.state_readable",
                                summary="Private Bridge state database could not be opened.")
        _print_doctor(report, as_json)
        return 1
    try:
        report = service.diagnostic_report(offline=offline, listener=listener)
    finally:
        service.close()
    _print_doctor(report, as_json)
    return 1 if report["overall"]["status"] in {"failed", "action_required"} else 0


def _print_doctor(report: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        _print_doctor_human(report)


def _print_release_bundle_human(result: dict) -> None:
    bundle = result.get("bundle", result)
    product_version = bundle.get("product_version", "unknown")
    bundle_id = bundle.get("bundle_id", "unknown")
    manifest_id = bundle.get("manifest_id", "unknown")
    short_bundle = (bundle_id[7:19] if isinstance(bundle_id, str)
                    and bundle_id.startswith("sha256:") and len(bundle_id) == 71 else "unknown")
    short_manifest = (manifest_id[7:19] if isinstance(manifest_id, str)
                      and manifest_id.startswith("sha256:") and len(manifest_id) == 71 else "unknown")
    print(f"Release bundle: COMPLETE — product {product_version} "
          f"bundle sha256:{short_bundle} manifest sha256:{short_manifest}")
    artifacts = bundle.get("artifacts", [])
    print(f"Artifacts: {len(artifacts)}")
    for entry in sorted(artifacts, key=lambda e: e.get("logical", "")):
        kind = entry.get("kind", "unknown")
        sha = entry.get("sha256", "unknown")
        short = (sha[7:19] if isinstance(sha, str)
                 and sha.startswith("sha256:") and len(sha) == 71 else "unknown")
        comps = ",".join(entry.get("components", []))
        print(f"  [{kind}] sha256:{short} components={comps}")


def _print_release_bundle_failure_human(code: str, summary: str) -> None:
    print(f"Release bundle: FAILED [{code}]: {summary}")


def _release_build(*, output: Path, as_json: bool) -> int:
    from .release_bundle import BundleError, build_bundle
    try:
        result = build_bundle(output=output)
    except BundleError as exc:
        code = getattr(exc, "code", "bundle_failed") or "bundle_failed"
        summaries = {
            "bundle-exists": "Bundle output already exists.",
            "bundle-unsafe-path": "Bundle output location is unsafe.",
            "bundle-unavailable": "Bundle output is unavailable.",
            "bundle-build-failed": "Release bundle build failed.",
            "bundle-identity-mismatch": "Embedded release identity mismatch.",
            "bundle-integrity-failed": "Bundle integrity check failed.",
            "bundle-helper-failed": "Release helper is unavailable.",
            "bundle-node-unavailable": "Node is required for release helpers.",
            "bundle-npm-unavailable": "npm is required for the Manager build.",
            "bundle-uv-unavailable": "uv is required for the wheel build.",
        }
        summary = summaries.get(code, "Release bundle build failed.")
        if as_json:
            print(json.dumps({"status": "failed", "code": code,
                              "summary": summary},
                             sort_keys=True, ensure_ascii=False, indent=2))
        else:
            _print_release_bundle_failure_human(code, summary)
        return 1
    except Exception:  # noqa: BLE001 - fail closed without paths
        if as_json:
            print(json.dumps({"status": "failed", "code": "bundle_failed",
                              "summary": "Release bundle build failed."},
                             sort_keys=True, ensure_ascii=False, indent=2))
        else:
            _print_release_bundle_failure_human("bundle_failed",
                                               "Release bundle build failed.")
        return 1
    bundle = result.get("bundle", {})
    if as_json:
        print(json.dumps(bundle, sort_keys=True, ensure_ascii=False, indent=2))
    else:
        _print_release_bundle_human(result)
    return 0


def _release_validate(*, bundle: Path, as_json: bool) -> int:
    from .release_bundle import BundleError, validate_release_bundle
    try:
        result = validate_release_bundle(bundle)
    except BundleError as exc:
        code = getattr(exc, "code", "bundle_failed") or "bundle_failed"
        summaries = {
            "bundle-traversal": "Bundle contains an unsafe path.",
            "bundle-integrity-failed": "Bundle integrity check failed.",
            "bundle-identity-mismatch": "Embedded release identity mismatch.",
            "bundle-manifest-invalid": "Bundle metadata is invalid.",
            "bundle-coverage-invalid": "Bundle component coverage is invalid.",
            "bundle-unsafe-metadata": "Bundle metadata is unsafe.",
            "bundle-unavailable": "Bundle path is unavailable.",
        }
        summary = summaries.get(code, "Release bundle is invalid.")
        if as_json:
            print(json.dumps({"status": "failed", "code": code,
                              "summary": summary},
                             sort_keys=True, ensure_ascii=False, indent=2))
        else:
            _print_release_bundle_failure_human(code, summary)
        return 1
    except Exception:  # noqa: BLE001 - fail closed without paths
        if as_json:
            print(json.dumps({"status": "failed", "code": "bundle_failed",
                              "summary": "Release bundle is invalid."},
                             sort_keys=True, ensure_ascii=False, indent=2))
        else:
            _print_release_bundle_failure_human("bundle_failed",
                                               "Release bundle is invalid.")
        return 1
    data = result.get("bundle", {})
    if as_json:
        print(json.dumps(data, sort_keys=True, ensure_ascii=False, indent=2))
    else:
        _print_release_bundle_human(result)
    return 0





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
    gateway = gateway_settings(config)
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
         adapters_configured=bool(service.adapter_registry.rows()),
         workspace_count=total, enabled_count=enabled)
    configs = [
        uvicorn.Config(make_mcp(service, config["mcp_port"], public_port=mcp_public_port,
                                container_mode=container_mode), host=mcp_bind, port=config["mcp_port"],
                       access_log=False, proxy_headers=False, log_level="warning"),
        uvicorn.Config(make_admin(service, port=config["admin_port"],
                                 public_port=admin_public_port, public_mcp_port=mcp_public_port,
                                 container_mode=container_mode,
                                 extra_hosts=extra_admin_hosts), host=admin_bind, port=config["admin_port"],
                       access_log=False, proxy_headers=False, log_level="warning"),
    ]
    servers = [uvicorn.Server(c) for c in configs]
    # The Node gateway runs on its own loop and thread: worker threads wait on
    # it for Node replies, sometimes holding the service lock, so it must not
    # share a loop with request handlers that take that lock.
    from .node_gateway import MAX_FRAME
    from .pairing import ensure_gateway_certificate
    cert, key = ensure_gateway_certificate(service.state)
    gateway_server = uvicorn.Server(uvicorn.Config(
        service.node_gateway.app(), host=gateway["host"], port=gateway["port"],
        ssl_certfile=str(cert), ssl_keyfile=str(key), ws_max_size=MAX_FRAME,
        ws_ping_interval=20, ws_ping_timeout=20, lifespan="off",
        access_log=False, proxy_headers=False, log_level="warning"))
    gateway_thread = threading.Thread(target=gateway_server.run, name="node-gateway",
                                      daemon=True)
    gateway_thread.start()
    tasks = [asyncio.create_task(s.serve()) for s in servers]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for server in servers:
            server.should_exit = True
        gateway_server.should_exit = True
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.to_thread(gateway_thread.join, 10)


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
    from .node_launchd import default_node_state
    parser = argparse.ArgumentParser(description="Local, workspace-scoped MCP planning and review bridge")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="Initialize private Bridge control-plane state")
    init.add_argument("--mcp-port", type=int, default=8765)
    init.add_argument("--admin-port", type=int, default=8766)
    init.add_argument("--gateway-port", type=int, default=DEFAULT_GATEWAY_PORT,
                      help="TLS port paired Nodes dial (bind address from WB_NODE_GATEWAY_HOST)")
    run = sub.add_parser("serve", help="Start both loopback listeners, single process only")
    run.add_argument("--container", action="store_true",
                     help="Bind 0.0.0.0 INSIDE an isolated container; requires loopback-only published host ports")
    run.add_argument("--mcp-public-port", type=int, help="Exact published loopback port; container mode only")
    run.add_argument("--admin-public-port", type=int, help="Exact published management loopback port; container mode only")
    sub.add_parser("rotate-bridge-token", help="Create/rotate the shared MCP credential while stopped; shown once")
    doctor = sub.add_parser("doctor", help="Report local readiness and adapter diagnostics")
    doctor.add_argument("--json", action="store_true", help="Emit canonical DiagnosticReport JSON")
    doctor.add_argument("--offline", action="store_true", help="Skip adapter/network calls")
    doctor.add_argument("--container", action="store_true", help=argparse.SUPPRESS)
    doctor.add_argument("--mcp-public-port", type=int, help=argparse.SUPPRESS)
    doctor.add_argument("--admin-public-port", type=int, help=argparse.SUPPRESS)
    sub.add_parser("reset-admin-password", help="Reset admin to the temporary bootstrap password; invalidates all sessions")
    service_parser = sub.add_parser("service", help="Manage the Bridge as a persistent service (macOS launchd or Linux systemd)")
    service_sub = service_parser.add_subparsers(dest="service_command", required=True)
    for action, text in (("install", "Install and start the Bridge service"),
                         ("status", "Show service state and listener health"),
                         ("start", "Start the Bridge service"), ("stop", "Stop the Bridge service"),
                         ("restart", "Restart the Bridge service"),
                         ("uninstall", "Remove only the managed service definition")):
        service_sub.add_parser(action, help=text)
    local_node = sub.add_parser("local-node", help="Run a Node inside this Bridge process for this machine's projects")
    local_node_sub = local_node.add_subparsers(dest="local_node_command", required=True)
    local_enable = local_node_sub.add_parser("enable", help="Create the embedded Node (restart serve afterwards)")
    local_enable.add_argument("--allow-root", action="append", required=True,
                              help="Project root this Node may expose (repeatable)")
    local_enable.add_argument("--name", default="This machine", help="Node display name")
    from .node_cli import add_plugin_commands
    add_plugin_commands(local_node_sub)
    pairing_parser = sub.add_parser("pairing", help="Issue one-time pairing strings for Nodes that dial this Bridge")
    pairing_sub = pairing_parser.add_subparsers(dest="pairing_command", required=True)
    pairing_create = pairing_sub.add_parser("create", help="Create a single-use pairing string (valid 10 minutes)")
    pairing_create.add_argument("--name", required=True, help="Node display name")
    pairing_create.add_argument("--replace", action="store_true",
                                help="Re-pair the existing paired Node with this name (new key; its runs stop)")
    release = sub.add_parser("release", help="Deterministic release bundle build and validation (release engineering)")
    release_sub = release.add_subparsers(dest="release_command", required=True)
    release_build = release_sub.add_parser("build", help="Build deterministic release bundle")
    release_build.add_argument("--output", type=Path, required=True, help="New/empty output directory for the bundle")
    release_build.add_argument("--json", action="store_true", help="Emit canonical bundle JSON")
    release_validate = release_sub.add_parser("validate", help="Validate a release bundle")
    release_validate.add_argument("--bundle", type=Path, required=True, help="Release bundle directory")
    release_validate.add_argument("--json", action="store_true", help="Emit canonical bundle JSON")
    node = sub.add_parser("node", help="Administer the host-local Node data-plane service")
    node.add_argument("--state", dest="node_state", type=Path, default=default_node_state(),
                      help="Node state directory (separate from Bridge --state)")
    node.add_argument("node_argv", nargs=argparse.REMAINDER,
                      help="Node command: init | pair | serve | show-token | service | plugin | adapter ...")
    args = parser.parse_args(argv)
    if args.command == "serve":
        public_ports = (args.mcp_public_port, args.admin_public_port)
        if any(p is not None for p in public_ports) and not args.container:
            parser.error("Published ports require --container; native service remains loopback-only")
        if args.container and (any(p is None or not 1024 <= p <= 65535 for p in public_ports)
                               or public_ports[0] == public_ports[1]):
            parser.error("--container requires distinct --mcp-public-port and --admin-public-port in 1024..65535")
    if args.command == "doctor":
        public_ports = (args.mcp_public_port, args.admin_public_port)
        if any(port is not None for port in public_ports) and (
                not args.container or any(port is None or not 1024 <= port <= 65535
                                          for port in public_ports)
                or public_ports[0] == public_ports[1]):
            parser.error("Container Doctor requires distinct published ports and --container")
    if os.name != "posix":
        parser.error("This version requires macOS or Linux (or WSL2), not native Windows")
    state = args.state.expanduser().resolve()
    os.umask(0o077)
    try:
        # Fail fast on an invalid WB_LOG_LEVEL before any service work; serve()
        # re-resolves it when listeners start.
        configure_operational_logging()
        if args.command == "init":
            config = initialize(state, args.mcp_port, args.admin_port, args.gateway_port)
            print(f"Initialized {state}\nLocal management: http://127.0.0.1:{config['admin_port']}/")
            print("Run workspace-bridge serve. Sign in as admin with temporary password admin; change it on first login.")
            return
        if args.command == "doctor":
            exit_code = _doctor(state, offline=args.offline, as_json=args.json,
                                container_mode=args.container,
                                mcp_public_port=args.mcp_public_port,
                                admin_public_port=args.admin_public_port)
            if exit_code:
                raise SystemExit(exit_code)
            return
        if args.command == "node":
            # Nested Node administration reuses the Node CLI grammar with a
            # Node-local --state (separate from Bridge --state).
            from .node_cli import main as node_main
            node_main(["--state", str(args.node_state), *args.node_argv])
            return
        if args.command == "release":
            if args.release_command == "build":
                # Release-bundle build never touches Bridge state, admin
                # credentials, or live Nodes/adapters.
                exit_code = _release_build(output=args.output, as_json=args.json)
                if exit_code:
                    raise SystemExit(exit_code)
                return
            if args.release_command == "validate":
                exit_code = _release_validate(bundle=args.bundle, as_json=args.json)
                if exit_code:
                    raise SystemExit(exit_code)
                return
            parser.error("Unknown release subcommand")
        config = load_config(state)
        # A managed service has no shell environment; private bridge.env
        # supplies the allowlisted settings, and real variables still win.
        from .bridge_service import load_env_file
        load_env_file(state)
        if args.command == "service":
            from .bridge_service import run_action
            print(json.dumps(run_action(args.service_command, state), indent=2, sort_keys=True))
            return
        if args.command == "local-node" and getattr(args, "plugin_group", None):
            from .node_cli import load_node_config, run_plugin_command
            from .node_service import NodeService
            local = state / Service.LOCAL_NODE_DIR
            node = NodeService(local, load_node_config(local))
            try:
                print(json.dumps(run_plugin_command(node, args), indent=2, sort_keys=True))
            finally:
                node.close()
            return
        if args.command == "local-node":
            result = enable_local_node(state, args.allow_root, args.name)
            print(f"Local Node {result['name']} ({result['node_id']}) enabled. "
                  "Restart workspace-bridge serve, then add workspaces on it in the Manager.")
            return
        if args.command == "pairing":
            result = create_pairing_offline(state, config, args.name, args.replace)
            print(result["pairing"])
            print(f"Single use, expires {result['expires']}. On the Node run: "
                  "workspace-bridge node --state <node state> pair '<pairing>'. Keep it out of chat.",
                  file=sys.stderr)
            return
        if args.command == "reset-admin-password":
            AdminAccount(state).reset()
            print("Admin password reset. Sign in as admin with temporary password admin; change it on first login.")
            return
        lock_fd = os.open(state / "process.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            if args.command in ("serve", "rotate-bridge-token"):
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise BridgeError("A bridge process already owns this state directory; rotate online in the local manager") from None
            service = Service(state, config, recover_incomplete=args.command == "serve")
            try:
                if args.command == "rotate-bridge-token":
                    result = service.manage_bridge("rotate_token")
                    print(result["token"])
                    print("Shared bridge credential shown once. Authorizes every enabled mapping. Keep it out of ChatGPT.", file=sys.stderr)
                else:
                    extra_admin_hosts = admin_allowed_hosts_from_env()
                    try:
                        # Managed macOS services bound their log files.
                        from .native_logs import spawn_log_guard
                        spawn_log_guard(state)
                    except Exception:  # noqa: BLE001 - logging never blocks startup
                        pass
                    print(f"MCP: http://127.0.0.1:{config['mcp_port']}/mcp | Local admin: http://127.0.0.1:{config['admin_port']}/")
                    if extra_admin_hosts:
                        print(f"WARNING: {ADMIN_ALLOWED_HOSTS_ENV}={','.join(extra_admin_hosts)} widens the admin listener to 0.0.0.0 with those Host values allowed. Prefer SSH port-forwarding or VPN; HTTP carries admin credentials in clear. Never tunnel the manager.", file=sys.stderr)
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
