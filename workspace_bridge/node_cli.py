"""Command line entry point for the private workspace-bridge-node service."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import secrets
import sys

from .node_service import NodeService
from .security import BridgeError, open_absolute_dir


def initialize_node(state: Path, roots: list[str]) -> dict:
    if not roots:
        raise BridgeError("At least one --allow-root is required")
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
    config = {"schema_version": 1, "allowed_roots": resolved,
              "cursor_secret": secrets.token_hex(32)}
    fd = os.open(state / "node-config.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                 os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(json.dumps(config, indent=2))
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
    if not isinstance(config.get("cursor_secret"), str):
        # 0.3 Nodes keyed cursors off the Node token, which 1.0 no longer has.
        from .node_link import _write_atomic
        config["cursor_secret"] = secrets.token_hex(32)
        _write_atomic(path, json.dumps(config, indent=2).encode())
    return config


async def serve_node(state: Path, config: dict):
    """Dial the paired Bridge and answer its requests; a Node never listens."""
    if not config.get("bridge"):
        raise BridgeError("This Node is not paired yet; run pair with a pairing string first",
                          "node_not_paired")
    from .node_link import run_link
    from .oplog import configure_operational_logging
    configure_operational_logging(log_file=state / "logs" / "node.log")
    service = NodeService(state, config)
    try:
        await run_link(service, config, state)
    finally:
        service.close()


def default_node_state() -> Path:
    return Path.home().absolute() / ".local/state/workspace-bridge-node"



def add_plugin_commands(sub) -> None:
    """``plugin`` and ``adapter`` subcommands, shared by Node and local-node CLIs."""
    plugin = sub.add_parser("plugin", help="List or install agent plugin manifests")
    plugin.set_defaults(plugin_group="plugin")
    plugin_sub = plugin.add_subparsers(dest="plugin_command", required=True)
    plugin_sub.add_parser("list", help="List the agent plugins this Node can run")
    plugin_add = plugin_sub.add_parser("add", help="Install a third-party plugin manifest (TOML)")
    plugin_add.add_argument("manifest", type=Path)
    add_kit_arguments(plugin_sub.add_parser(
        "test", help="Run the conformance kit against a plugin manifest or plugin id"))
    adapter = sub.add_parser("adapter", help="Add, change, list, test or remove agent adapters run as plugins")
    adapter.set_defaults(plugin_group="adapter")
    adapter_sub = adapter.add_subparsers(dest="adapter_command", required=True)
    add = adapter_sub.add_parser("add", help="Add an adapter that runs a plugin under this Node")
    add.add_argument("plugin", help="Plugin id, e.g. claude, codex, opencode or pi")
    add.add_argument("--name", required=True)
    add.add_argument("--projects-root", help="Folder the agent may work in; defaults to the only allowed root")
    add.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                     help="Plugin setting, e.g. binary=/opt/homebrew/bin/claude (repeatable)")
    change = adapter_sub.add_parser("set", help="Change an adapter's plugin settings")
    change.add_argument("adapter_id")
    change.add_argument("--set", action="append", required=True, metavar="KEY=VALUE",
                        help="Plugin setting to change; KEY= clears it (repeatable)")
    adapter_sub.add_parser("list", help="List this Node's adapters")
    test = adapter_sub.add_parser("test", help="Start the plugin and print its descriptor")
    test.add_argument("adapter_id")
    remove = adapter_sub.add_parser("remove", help="Remove an adapter (its plugin state folder is kept)")
    remove.add_argument("adapter_id")


def add_kit_arguments(parser) -> None:
    """Arguments of ``plugin test``, the conformance kit."""
    parser.add_argument("manifest", help="Plugin manifest (TOML) or the id of a known plugin")
    parser.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                        help="Plugin setting, as for adapter add (repeatable)")
    parser.add_argument("--model", help="Model for live runs; defaults to the plugin's default model")
    parser.add_argument("--live", action="store_true",
                        help="Also send a few short prompts to the agent, on its account")
    parser.add_argument("--timeout", type=float, default=180,
                        help="Seconds to wait for each live run (default 180)")
    parser.add_argument("--keep", action="store_true",
                        help="Keep the temporary workspace and plugin state for inspection")
    parser.add_argument("--json", action="store_true", help="Print the report as JSON")


def _settings(pairs: list[str]) -> dict:
    settings = {}
    for item in pairs:
        key, sep, value = item.partition("=")
        if not sep or not key:
            raise BridgeError("--set expects KEY=VALUE", "invalid_arguments")
        settings[key] = value
    return settings


def run_kit_command(args, manifests: dict[str, dict]) -> None:
    """Run the conformance kit and print its report; exit 1 unless it passed."""
    from .plugin_conformance import run_kit
    from .plugin_protocol import load_manifest
    path = Path(args.manifest).expanduser()
    if path.suffix == ".toml" or path.is_file():
        manifest = load_manifest(path)
    elif args.manifest in manifests:
        manifest = manifests[args.manifest]
    else:
        raise BridgeError("Unknown plugin; give a manifest file or a known plugin id",
                          "unknown_plugin")
    settings = _settings(args.set)
    unknown = set(settings) - set(manifest["settings"]) - {"log_level"}
    if unknown:
        raise BridgeError("Unknown plugin settings: " + ", ".join(sorted(unknown)),
                          "invalid_arguments")
    if not 1 <= args.timeout <= 3600:
        raise BridgeError("--timeout must be between 1 and 3600 seconds", "invalid_arguments")

    def progress(result: dict) -> None:
        print(f"[{result['status']}] {result['check']}: {result['detail']}", flush=True)

    report = run_kit(manifest, settings=settings, model=args.model, live=args.live,
                     timeout=args.timeout, keep=args.keep,
                     progress=None if args.json else progress)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        verdict = "PASSED" if report["passed"] else "NOT PASSED"
        note = "" if args.live else " (protocol checks only; add --live to run the agent)"
        print(f"{verdict}: {manifest['id']} {manifest['version']}{note}")
        if report.get("kept"):
            print(f"Kept {report['kept']}")
    if not report["passed"]:
        raise SystemExit(1)


def run_plugin_command(node: NodeService, args) -> dict | None:
    if args.plugin_group == "plugin":
        if args.plugin_command == "test":
            run_kit_command(args, node.manifests())
            return None
        if args.plugin_command == "add":
            manifest = node.install_manifest(args.manifest)
            return {"installed": manifest["id"], "runtime_type": manifest["runtime_type"]}
        return {"plugins": [{"id": m["id"], "runtime_type": m["runtime_type"],
                             "version": m["version"], "settings": m["settings"],
                             "description": m["description"]}
                            for m in node.manifests().values()]}
    if args.adapter_command == "add":
        return node.save_adapter({"name": args.name, "plugin": args.plugin,
                                  "projects_root": args.projects_root,
                                  "settings": _settings(args.set)})
    if args.adapter_command == "set":
        # Settings not named keep their values; a changed configuration
        # restarts the plugin.
        current = json.loads(node.adapter(args.adapter_id)["settings"] or "{}")
        return node.save_adapter({"settings": {**current, **_settings(args.set)}},
                                 args.adapter_id)
    if args.adapter_command == "list":
        return node.list_adapters()
    if args.adapter_command == "test":
        return node.test_adapter({"adapter_id": args.adapter_id})
    return node.delete_adapter(args.adapter_id)


def main(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(description="Private workspace-bridge Node data-plane service")
    parser.add_argument("--state", type=Path, default=default_node_state())
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="Initialize a Node with host-local allowed roots")
    init.add_argument("--allow-root", action="append", required=True)
    sub.add_parser("serve", help="Connect to the paired Bridge and serve its requests")
    pair_parser = sub.add_parser("pair", help="Pair with a Bridge using a one-time pairing string; the Node then dials out")
    pair_parser.add_argument("pairing", help="wbpair1:... string from the Bridge (keep it out of chat)")
    from .os_service import add_service_commands
    add_service_commands(sub, what="Node")
    add_plugin_commands(sub)
    args = parser.parse_args(argv)
    state = args.state.expanduser().absolute()
    os.umask(0o077)
    try:
        if args.command == "init":
            initialize_node(state, args.allow_root)
            print(f"Initialized Node at {state}.")
            print("Create a pairing string in the Bridge Manager (or with workspace-bridge pairing create), "
                  "then run pair with it here and install the service with service install.")
            return
        if args.command == "service":
            from .os_service import NODE, run_action
            print(json.dumps(run_action(NODE, args.service_command, state, root=args.root),
                             indent=2, sort_keys=True))
            return
        config = load_node_config(state)
        if args.command in ("plugin", "adapter"):
            node = NodeService(state, config)
            try:
                result = run_plugin_command(node, args)
                if result is not None:
                    print(json.dumps(result, indent=2, sort_keys=True))
            finally:
                node.close()
            return
        if args.command == "pair":
            from .node_link import pair
            bridge = asyncio.run(pair(state, config, args.pairing))
            print(f"Paired as {bridge['name'] or bridge['node_id']}. Restart the Node service "
                  "(or run serve) so it connects to the Bridge; it no longer listens for inbound requests.")
            return
        asyncio.run(serve_node(state, config))
    except (BridgeError, OSError, ValueError) as exc:
        print(f"workspace-bridge-node: {exc}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
