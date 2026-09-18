#!/usr/bin/env python3
"""Explicit local-only launcher for the official OpenAI tunnel client.

Never imported by the MCP server. Secrets stay out of command arguments/history.
"""
from __future__ import annotations

import argparse
import getpass
import os
from pathlib import Path
import shutil
import subprocess
import sys


def build_command(binary: str, config: Path, action: str) -> list[str]:
    if action not in {"doctor", "run"}:
        raise ValueError("Only doctor or run is supported")
    result = [binary, action, "--config", str(config)]
    if action == "doctor":
        result.append("--explain")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("action", choices=("doctor", "run"))
    args = parser.parse_args()
    config = args.config.expanduser().resolve()
    if not config.is_file():
        parser.error("Configuration file does not exist")
    binary = shutil.which("tunnel-client")
    if binary is None:
        parser.error("Install the official OpenAI tunnel-client first; see docs/TUNNEL_SETUP.md")
    env = os.environ.copy()
    try:
        for key, label in (
            ("CONTROL_PLANE_API_KEY", "OpenAI tunnel runtime key"),
            ("WORKSPACE_BRIDGE_TOKEN", "Shared bridge token (not the admin token)"),
        ):
            value = env.get(key) or getpass.getpass(label + ": ")
            if not value or "\n" in value or "\r" in value:
                parser.error("A nonempty, single-line credential is required")
            env[key] = value
        # No shell, no secret command arguments, and no automatic installation.
        return subprocess.run(build_command(binary, config, args.action), env=env, check=False).returncode
    except (EOFError, KeyboardInterrupt):
        print("Cancelled.", file=sys.stderr)
        return 130
    except OSError:
        print("Unable to start tunnel-client. Check the installation and local permissions.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
