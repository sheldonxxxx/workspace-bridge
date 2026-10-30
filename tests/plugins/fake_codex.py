"""The real Codex adapter as a plugin, backed by the test suite's fake app-server."""
from __future__ import annotations

import os
import sys
from pathlib import Path

TESTS = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(TESTS.parent), str(TESTS)]

from workspace_bridge.codex_host_adapter import CodexHostAdapter  # noqa: E402
from workspace_bridge.plugin_host import PluginServer  # noqa: E402
from test_codex_host_adapter import FakeCodexRpc  # noqa: E402

if __name__ == "__main__":
    out = os.fdopen(os.dup(1), "wb", buffering=0)
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    PluginServer(lambda p: CodexHostAdapter(Path(p["state"]), Path(p["projectsRoot"]),
                                            rpc=FakeCodexRpc())).serve(sys.stdin.buffer, out)
