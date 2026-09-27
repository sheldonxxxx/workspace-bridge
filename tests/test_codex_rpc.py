"""Codex stdio transport bounds and sanitizes native diagnostics."""
from __future__ import annotations

import sys
import threading
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
        response = {"id": request["id"], "result": {
            "serverInfo": {"version": "test"},
            "experimentalApi": request["params"]["capabilities"]["experimentalApi"]}}
    elif request["method"] == "permissionProfile/list":
        cursor = request["params"].get("cursor")
        result = {"data": ([{"id": ":workspace", "allowed": True,
                             "description": "Workspace profile"}]
                           if cursor else [{"id": ":read-only", "allowed": True,
                                            "description": None}]),
                  "nextCursor": None if cursor else "next-page"}
        response = {"id": request["id"], "result": result}
    elif request["method"] == "config/read":
        response = {"id": request["id"], "result": {
            "config": {"permissions": {"workspace": {"network": {"enabled": False}}}},
            "origins": {}, "paramsEcho": request["params"]}}
    elif request["method"] == "configRequirements/read":
        response = {"id": request["id"], "result": {
            "requirements": None, "paramsEcho": request["params"]}}
    else:
        response = {"id": request["id"], "error": {"code": -32000,
            "message": "denied at /Users/private/project api_key=sk-proj-" + "z" * 40}}
    print(json.dumps(response), flush=True)
'''
    rpc = CodexRpc(command=(sys.executable, "-u", "-c", child))
    try:
        assert rpc.initialize_result["serverInfo"]["version"] == "test"
        assert rpc.initialize_result["experimentalApi"] is True
        profiles = rpc.permission_profiles("/safe/project")
        assert [item["id"] for item in profiles] == [":read-only", ":workspace"]
        config = rpc.read_security_config("/safe/project")
        assert config["config"]["permissions"]
        assert config["paramsEcho"] == {"cwd": "/safe/project", "includeLayers": False}
        assert rpc.read_config_requirements() is None
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


def test_permission_profile_list_rejects_malformed_rows_without_echoing_ids():
    child = r'''import json, sys
for raw in sys.stdin:
    request = json.loads(raw)
    if "id" not in request:
        continue
    if request["method"] == "initialize":
        result = {"serverInfo": {"version": "test"}}
    elif request["method"] == "permissionProfile/list":
        result = {"data": [{"id": "/private/project", "allowed": True}],
                  "nextCursor": None}
    else:
        result = {}
    print(json.dumps({"id": request["id"], "result": result}), flush=True)
'''
    rpc = CodexRpc(command=(sys.executable, "-u", "-c", child))
    try:
        with pytest.raises(CodexRpcError) as exc:
            rpc.permission_profiles("/safe/project")
        assert "invalid" in str(exc.value)
        assert "/private/project" not in str(exc.value)
    finally:
        rpc.close()


def test_thread_settings_update_uses_native_request_and_updated_notification():
    child = r'''import json, sys
for raw in sys.stdin:
    request = json.loads(raw)
    if "id" not in request:
        continue
    if request["method"] == "initialize":
        result = {"serverInfo": {"version": "test"}}
        print(json.dumps({"id": request["id"], "result": result}), flush=True)
    elif request["method"] == "thread/settings/update":
        params = request["params"]
        print(json.dumps({"method": "thread/settings/updated", "params": {
            "threadId": params["threadId"],
            "threadSettings": {"activePermissionProfile": {"id": params["permissions"]},
                "approvalPolicy": "on-request", "approvalsReviewer": "user"}}}), flush=True)
        print(json.dumps({"id": request["id"], "result": {}}), flush=True)
'''
    notifications = []
    rpc = CodexRpc(command=(sys.executable, "-u", "-c", child),
                   on_notification=lambda method, params: notifications.append((method, params)))
    try:
        assert rpc.update_thread_settings(
            "thread-one", {"permissions": "profile-one"}) == {}
        deadline = time.monotonic() + 1
        while not notifications and time.monotonic() < deadline:
            time.sleep(0.01)
        assert notifications == [("thread/settings/updated", {
            "threadId": "thread-one", "threadSettings": {
                "activePermissionProfile": {"id": "profile-one"},
                "approvalPolicy": "on-request", "approvalsReviewer": "user"}})]
        with pytest.raises(CodexRpcError, match="fields are invalid"):
            rpc.update_thread_settings("thread-one", {
                "permissions": "profile-one", "sandboxPolicy": {"type": "workspaceWrite"}})
    finally:
        rpc.close()


def test_unexpected_stdio_exit_notifies_once_after_pending_released():
    child = r'''import json, sys, time
for raw in sys.stdin:
    try:
        request = json.loads(raw)
    except Exception:
        continue
    if "id" not in request:
        continue
    if request["method"] == "initialize":
        print(json.dumps({"id": request["id"],
                          "result": {"serverInfo": {"version": "test"}}}), flush=True)
    elif request["method"] == "blocking":
        time.sleep(10)
'''
    notified: list[int] = []
    rpc = CodexRpc(command=(sys.executable, "-u", "-c", child),
                   on_unexpected_exit=lambda: notified.append(1))
    try:
        assert rpc.initialize_result["serverInfo"]["version"] == "test"
        outcome: list[str] = []

        def blocking_call() -> None:
            try:
                rpc.call("blocking", {}, timeout=10)
                outcome.append("returned")
            except CodexRpcError:
                outcome.append("released")

        waiter = threading.Thread(target=blocking_call, daemon=True)
        waiter.start()
        deadline = time.monotonic() + 2
        while not rpc._pending and time.monotonic() < deadline:
            time.sleep(0.01)
        assert rpc._pending, "blocking call was not registered as pending"
        rpc._process.kill()
        waiter.join(timeout=5)
        assert outcome == ["released"]
        deadline = time.monotonic() + 2
        while not notified and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(notified) == 1
        time.sleep(0.2)
        assert len(notified) == 1
        rpc.close()
        time.sleep(0.2)
        assert len(notified) == 1
    finally:
        rpc.close()


def test_close_does_not_report_unexpected_exit():
    child = r'''import json, sys
for raw in sys.stdin:
    try:
        request = json.loads(raw)
    except Exception:
        continue
    if "id" not in request:
        continue
    if request["method"] == "initialize":
        print(json.dumps({"id": request["id"],
                          "result": {"serverInfo": {"version": "test"}}}), flush=True)
'''
    notified: list[int] = []
    rpc = CodexRpc(command=(sys.executable, "-u", "-c", child),
                   on_unexpected_exit=lambda: notified.append(1))
    try:
        assert rpc.initialize_result["serverInfo"]["version"] == "test"
        rpc.close()
        time.sleep(0.5)
        assert notified == []
    finally:
        rpc.close()


def test_default_managed_command_enables_default_mode_request_user_input():
    import inspect

    from workspace_bridge import codex_rpc as rpc_module

    managed = tuple(rpc_module.DEFAULT_CODEX_APP_SERVER_COMMAND)
    assert managed[:2] == ("codex", "app-server")
    assert managed[-1] == "--stdio"
    override = "features.default_mode_request_user_input=true"
    assert managed.count(override) == 1
    index = managed.index(override)
    # Override lives in the app-server argument segment, after the
    # subcommand, introduced by a single post-subcommand `-c`.
    assert index > 1
    assert managed[index - 1] == "-c"
    assert managed[:2].count("-c") == 0
    assert tuple(rpc_module.default_codex_app_server_command()) == managed
    signature_default = inspect.signature(CodexRpc).parameters["command"].default
    assert tuple(signature_default) == managed


def test_custom_command_is_honored_verbatim_without_managed_flag(monkeypatch):
    from workspace_bridge import codex_rpc as rpc_module

    child = r'''import json, sys
for raw in sys.stdin:
    try:
        request = json.loads(raw)
    except Exception:
        continue
    if "id" not in request:
        continue
    if request["method"] == "initialize":
        print(json.dumps({"id": request["id"],
                          "result": {"serverInfo": {"version": "test"}}}), flush=True)
'''
    custom = (sys.executable, "-u", "-c", child)
    seen: dict[str, tuple] = {}
    real_popen = rpc_module.subprocess.Popen

    def _capturing_popen(cmd, **kwargs):
        seen["cmd"] = tuple(cmd)
        return real_popen(cmd, **kwargs)

    monkeypatch.setattr(rpc_module.subprocess, "Popen", _capturing_popen)
    rpc = CodexRpc(command=custom)
    try:
        assert seen["cmd"] == custom
        assert "features.default_mode_request_user_input=true" not in seen["cmd"]
        assert rpc.initialize_result["serverInfo"]["version"] == "test"
    finally:
        rpc.close()


def test_version_probe_executable_extraction_with_managed_default_argv(monkeypatch):
    from workspace_bridge import codex_host_adapter as adapter_module
    from workspace_bridge import codex_rpc as rpc_module

    managed = tuple(rpc_module.DEFAULT_CODEX_APP_SERVER_COMMAND)
    # Descriptor probes `command[0]` only, so extra managed argv must not
    # change the resolved executable.
    executable = managed[0] if isinstance(managed, (list, tuple)) and managed else None
    assert executable == "codex"
    seen: dict[str, object] = {}

    def _fake_run(cmd, **kwargs):
        seen["cmd"] = list(cmd)
        seen["env"] = kwargs.get("env")
        box = type("R", (), {})()
        box.returncode = 0
        box.stdout = "codex-cli 1.2.3\n"
        box.stderr = ""
        return box

    monkeypatch.setattr(adapter_module.subprocess, "run", _fake_run)
    env = {"PATH": "/usr/bin:/bin"}
    assert adapter_module._codex_cli_version(executable, env) == "1.2.3"
    assert seen["cmd"] == ["codex", "--version"]
    assert seen["env"] == env
