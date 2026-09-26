"""Runtime-neutral ``workspace-bridge adapter`` lifecycle CLI.

Minimum commands: ``init``, ``serve`` (internal/service entrypoint),
``show-token``, and ``service install|status|start|stop|restart|uninstall``.
The CLI never installs/upgrades packages and never writes Bridge/Node
registry state. ``serve`` is a small trusted launcher that ``execve``s the
stored adapter executable directly with an allowlisted environment.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

from .adapter_service import (
    _absolute_path,
    build_adapter_env,
    initialize_adapter,
    load_adapter_config,
    read_adapter_token,
    serve_argv,
    validate_adapter_state,
)
from .login_path import runtime_env_with_login_path
from .security import BridgeError


def select_service_backend(platform_name: str | None = None) -> str:
    """Return ``launchd`` on macOS, ``systemd`` on Linux, else raise."""
    import platform as _platform

    name = platform_name if platform_name is not None else _platform.system()
    if name == "Darwin":
        return "launchd"
    if name == "Linux":
        return "systemd"
    raise BridgeError(
        "workspace-bridge adapter service commands require macOS launchd or "
        "Linux systemd on this host",
        "adapter_service_unsupported_platform",
    )


def _serve(state: Path) -> None:
    """Validate private state and ``execve`` the stored adapter executable."""
    # Managed LaunchAgents carry WB_NATIVE_LOG_GUARD=1 on Darwin; spawn one
    # package-owned log-guard child (fixed argv, stdio to DEVNULL, no shell)
    # then still execve the runtime exactly as before. Foreground use without
    # the marker never spawns the guard. The guard never supervises the runtime.
    try:
        from .native_logs import spawn_log_guard as _spawn_guard
        _spawn_guard(state)
    except Exception:
        pass
    config, token = validate_adapter_state(state)
    # Ensure the private runtime subdir exists for the Codex child without
    # changing ownership or modes of existing state.
    runtime_dir = _absolute_path(state) / "runtime"
    try:
        if not runtime_dir.exists():
            runtime_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(runtime_dir, 0o700)
    except OSError:
        raise BridgeError("Adapter runtime state is unavailable",
                          "adapter_state_unsafe") from None
    # launchd/systemd start with a reduced PATH. Resolve only the current
    # user's validated login PATH before exec so env-shebang runtime shims
    # (for example ``#!/usr/bin/env node`` from npm) can find their interpreter.
    # On probe failure, runtime_env_with_login_path safely keeps the inherited
    # service environment; no shell output or arbitrary environment is imported.
    base_env, _login_path = runtime_env_with_login_path()
    env = build_adapter_env(config, token, state, base_env=base_env)
    argv = serve_argv(config)
    try:
        os.execve(argv[0], argv, env)
    except OSError:
        # Never echo the token, paths, or environment on exec failure.
        raise BridgeError("Adapter executable could not be started",
                          "adapter_service_exec_failed") from None


def main(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(
        description="Manage one native Pi/Codex adapter instance")
    parser.add_argument("--state", type=Path, required=True,
                        help="Adapter instance state directory")
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="Initialize one adapter instance state")
    init.add_argument("--runtime", required=True, choices=["pi", "codex"],
                      help="Runtime type for this instance")
    init.add_argument("--projects-root", required=True,
                      help="Canonical existing projects parent")
    init.add_argument("--port", type=int, default=None,
                      help="Loopback port (default 8780 for pi, 8772 for codex)")
    init.add_argument("--executable", type=Path, default=None,
                      help="Explicit absolute adapter executable (development/tests only)")
    init.add_argument("--pi-binary", default=None,
                      help="Pi executable override (pi runtime only)")
    init.add_argument("--agent-dir", type=Path, default=None,
                      help="Isolated Pi agent dir (pi runtime only)")
    init.add_argument("--log-level", default=None,
                      help="WB_LOG_LEVEL for the adapter child")
    sub.add_parser("serve", help="Launch the configured adapter (service entrypoint)")
    sub.add_parser("show-token", help="Print the instance token once for Node registration")
    service = sub.add_parser("service", help="Manage the adapter service (macOS launchd or Linux systemd)")
    service_sub = service.add_subparsers(dest="service_command", required=True)
    service_sub.add_parser("install", help="Install and start the managed service")
    service_sub.add_parser("status", help="Show service and descriptor health status")
    service_sub.add_parser("start", help="Start the managed service")
    service_sub.add_parser("stop", help="Stop the managed service")
    service_sub.add_parser("restart", help="Restart the managed service")
    service_sub.add_parser("uninstall", help="Remove only the managed service artifact")
    args = parser.parse_args(argv)
    state = _absolute_path(args.state.expanduser())
    os.umask(0o077)
    try:
        if os.name != "posix":
            raise BridgeError(
                "workspace-bridge adapter commands require macOS or Linux",
                "adapter_service_unsupported_platform",
            )
        if args.command == "init":
            config, token = initialize_adapter(
                state,
                runtime_type=args.runtime,
                projects_root=str(args.projects_root),
                port=args.port,
                executable=args.executable,
                pi_binary=args.pi_binary,
                agent_dir=str(args.agent_dir) if args.agent_dir is not None else None,
                log_level=args.log_level,
            )
            print(f"Initialized {config['runtime_type']} adapter at {state}; port {config['port']}")
            print(token)
            print("Adapter token shown once. Register it as an AdapterInstance on the owning Node. "
                  "Keep it out of ChatGPT.", file=sys.stderr)
            return
        if args.command == "serve":
            _serve(state)
            return
        if args.command == "show-token":
            # Validate config ownership before revealing the token.
            load_adapter_config(state)
            token_path = state / "runtime-token"
            if token_path.is_symlink():
                raise BridgeError("Unsafe adapter token file")
            try:
                if token_path.stat().st_mode & 0o077:
                    raise BridgeError("Unsafe adapter token file")
            except OSError:
                raise BridgeError("Adapter token cannot be read") from None
            print(read_adapter_token(state))
            return
        if args.command == "service":
            backend = select_service_backend()
            if backend == "launchd":
                from .adapter_launchd import (
                    LaunchdManager,
                    install_service,
                    restart_service,
                    service_status,
                    start_service,
                    stop_service,
                    uninstall_service,
                )

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
                from .adapter_systemd import (
                    SystemdManager,
                    install_service as systemd_install,
                    restart_service as systemd_restart,
                    service_status as systemd_status,
                    start_service as systemd_start,
                    stop_service as systemd_stop,
                    uninstall_service as systemd_uninstall,
                )

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
        parser.error("Unknown adapter subcommand")
    except (BridgeError, OSError, ValueError) as exc:
        print(f"workspace-bridge adapter: {exc}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
