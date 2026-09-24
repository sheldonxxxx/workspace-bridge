"""Codex stdio transport bounds and sanitizes native diagnostics."""
from __future__ import annotations

import sys
import time

import pytest

from workspace_bridge.codex_rpc import CodexRpc, CodexRpcError


def test_stderr_is_drained_bounded_and_native_errors_are_sanitized():
    child = r'''import json, sys
sys.stderr.write("failed at /Users/private/project WB_RUNTIME_TOKEN=super-secret-token\n")
sys.stderr.write("x" * 12000 + "\n")
sys.stderr.write("-----BEGIN PRIVATE KEY-----\nprivate-material\n-----END PRIVATE KEY-----\n")
sys.stderr.flush()
for raw in sys.stdin:
    request = json.loads(raw)
    if "id" not in request:
        continue
    if request["method"] == "initialize":
        response = {"id": request["id"], "result": {"serverInfo": {"version": "test"}}}
    else:
        response = {"id": request["id"], "error": {"code": -32000,
            "message": "denied at /Users/private/project api_key=sk-proj-" + "z" * 40}}
    print(json.dumps(response), flush=True)
'''
    rpc = CodexRpc(command=(sys.executable, "-u", "-c", child))
    try:
        assert rpc.initialize_result["serverInfo"]["version"] == "test"
        with pytest.raises(CodexRpcError) as exc:
            rpc.call("thread/start", {"cwd": "/Users/private/project"})
        message = str(exc.value)
        assert "thread/start rejected (code -32000)" in message
        assert "/Users/" not in message and "sk-proj-" not in message
        assert len(message) <= 300

        deadline = time.monotonic() + 2
        summary = rpc.stderr_summary()
        while not summary and time.monotonic() < deadline:
            time.sleep(0.01)
            summary = rpc.stderr_summary()
        assert len(summary) <= 2048
        assert "/Users/" not in summary
        assert "super-secret-token" not in summary
        assert "private-material" not in summary
        assert "[oversized app-server stderr line omitted]" in summary
        assert "[private key material omitted]" in summary
    finally:
        rpc.close()
