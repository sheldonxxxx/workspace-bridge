#!/usr/bin/env python3
"""Emit a deterministic release target manifest.

Uses the existing M4.1 release identities: Python-core identities for
bridge/node/codex plus fixed, bounded local ``node`` subprocesses that call
the existing Manager (web/manager-release.mjs) and Pi
(runtime/pi-host-adapter/release.mjs) helpers. No arbitrary commands are
accepted and no filesystem paths appear in manifest output.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
WEB_ROOT = REPO_ROOT / "web"
PI_ROOT = REPO_ROOT / "runtime" / "pi-host-adapter"

MANAGER_JS = (
    "const mod = await import(process.argv[1]);\n"
    "const root = process.argv[2];\n"
    "const release = mod.computeManagerRelease(root);\n"
    "process.stdout.write(JSON.stringify(release));\n"
)
PI_JS = (
    "const mod = await import(process.argv[1]);\n"
    "const root = process.argv[2];\n"
    "const release = mod.piRelease(root);\n"
    "process.stdout.write(JSON.stringify(release));\n"
)


def _node_exe() -> str:
    exe = shutil.which("node")
    if not exe:
        raise SystemExit(
            "error: node is required to compute Manager/Pi release identities")
    return exe


def _run_node_helper(*, node_exe: str, module_path: Path, root: Path,
                     label: str, _run=None) -> dict:
    """Run one fixed Node release helper and return its parsed identity.

    ``_run`` is an injection hook for unit tests; production always uses
    :func:`subprocess.run` with a fixed script, fixed module path, and a
    bounded timeout. Only the helper's JSON object is returned.
    """
    helper = (MANAGER_JS if label == "manager" else PI_JS)
    argv = [node_exe, "--input-type=module", "-e", helper,
            str(module_path), str(root)]
    run = _run or subprocess.run
    try:
        result = run(argv, capture_output=True, text=True, timeout=30,
                     check=False)
    except (OSError, subprocess.SubprocessError):
        raise SystemExit(
            f"error: {label} release helper could not be executed") from None
    if result.returncode != 0:
        raise SystemExit(
            f"error: {label} release metadata is unavailable or invalid")
    try:
        value = json.loads(result.stdout)
    except (ValueError, UnicodeError):
        raise SystemExit(
            f"error: {label} release metadata is unavailable or invalid"
        ) from None
    if not isinstance(value, dict):
        raise SystemExit(
            f"error: {label} release metadata is unavailable or invalid")
    return value


def get_manager_release(*, node_exe: str | None = None, _run=None) -> dict:
    """Return the live Manager release identity via its Node helper."""
    exe = node_exe or _node_exe()
    return _run_node_helper(node_exe=exe,
                            module_path=WEB_ROOT / "manager-release.mjs",
                            root=WEB_ROOT, label="manager", _run=_run)


def get_pi_release(*, node_exe: str | None = None, _run=None) -> dict:
    """Return the live Pi release identity via its Node helper."""
    exe = node_exe or _node_exe()
    return _run_node_helper(node_exe=exe,
                            module_path=PI_ROOT / "release.mjs",
                            root=PI_ROOT, label="pi", _run=_run)


def build_target_manifest(*, manager: dict | None = None,
                          pi: dict | None = None,
                          _manager_fn=None, _pi_fn=None) -> dict:
    """Assemble a validated target manifest from current source identities."""
    from workspace_bridge import __version__ as product_version
    from workspace_bridge.release_manifest import make_manifest
    from workspace_bridge.release import (bridge_release, codex_release,
                                          node_release)

    bridge = bridge_release()
    node = node_release()
    codex = codex_release()
    manager_release = (manager if manager is not None
                       else (_manager_fn() if _manager_fn is not None
                             else get_manager_release()))
    pi_release = (pi if pi is not None
                  else (_pi_fn() if _pi_fn is not None else get_pi_release()))
    return make_manifest(product_version=product_version, bridge=bridge,
                         manager=manager_release, node=node, codex=codex,
                         pi=pi_release)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Emit a deterministic release target manifest")
    parser.add_argument("--output", type=Path, default=None,
                        help="Explicit output file for the manifest JSON")
    args = parser.parse_args(argv)
    try:
        manifest = build_target_manifest()
    except SystemExit as exc:
        print(str(exc), file=sys.stderr)
        return 2 if isinstance(exc.code, str) else (exc.code or 2)
    except Exception as exc:  # noqa: BLE001 - fail closed without paths
        print(f"error: target manifest is unavailable ({type(exc).__name__})",
              file=sys.stderr)
        return 1
    text = json.dumps(manifest, sort_keys=True, indent=2) + "\n"
    if args.output is not None:
        try:
            args.output.write_text(text, encoding="utf-8")
        except OSError as exc:
            print(f"error: could not write output ({type(exc).__name__})",
                  file=sys.stderr)
            return 1
    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
