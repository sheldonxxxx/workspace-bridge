"""Claude adapter contract checks with a scripted SDK, no model calls."""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import threading
import time
import types
from dataclasses import dataclass, field
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from workspace_bridge import wbrp
from workspace_bridge.claude_host_adapter import (
    AdapterFailure, ClaudeHostAdapter, PROFILES, _normalize_claude_usage,
    allowed_tools, decide_tool_use, make_app, parse_setting_sources)

READ_ONLY = dict(PROFILES["read-only"])
REVIEWED = dict(PROFILES["workspace-write-reviewed"])
YOLO_SHELL = {"edits": "allow", "shell": "allow", "web": "allow", "extensions": "allow"}


# -- scripted SDK ----------------------------------------------------------

@dataclass
class SystemMessage:
    subtype: str
    data: dict


@dataclass
class TextBlock:
    text: str


@dataclass
class ToolUseBlock:
    id: str
    name: str
    input: dict


@dataclass
class ToolResultBlock:
    tool_use_id: str
    content: Any = ""
    is_error: bool = False


@dataclass
class AssistantMessage:
    content: list
    usage: dict | None = None
    message_id: str | None = None
    parent_tool_use_id: str | None = None


@dataclass
class UserMessage:
    content: list


@dataclass
class ResultMessage:
    subtype: str = "success"
    is_error: bool = False
    result: str | None = None
    usage: dict | None = None
    errors: list | None = None
    api_error_status: int | None = None
    terminal_reason: str | None = "completed"


@dataclass
class RateLimitInfo:
    status: str = "allowed"
    resets_at: int | None = None
    rate_limit_type: str | None = None
    utilization: float | None = None
    raw: dict = field(default_factory=dict)


@dataclass
class RateLimitEvent:
    rate_limit_info: RateLimitInfo


@dataclass
class HookMatcher:
    matcher: Any = None
    hooks: list = field(default_factory=list)


@dataclass
class PermissionResultAllow:
    behavior: str = "allow"
    updated_input: Any = None


@dataclass
class PermissionResultDeny:
    behavior: str = "deny"
    message: str = ""
    interrupt: bool = False


class FakeOptions:
    session_id = None
    resume = None

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


def make_sdk():
    sdk = types.ModuleType("fake_claude_sdk")
    sdk.ClaudeAgentOptions = FakeOptions
    sdk.HookMatcher = HookMatcher
    sdk.PermissionResultAllow = PermissionResultAllow
    sdk.PermissionResultDeny = PermissionResultDeny
    sdk.clients = []
    sdk.sessions = set()
    sdk.init_session_override = None
    sdk.models = [
        {"value": "default", "displayName": "Default",
         "supportedEffortLevels": ["low", "high"]},
        {"value": "haiku", "displayName": "Haiku", "supportedEffortLevels": []},
    ]

    async def default_scenario(client):
        session = client.options.session_id or client.options.resume
        sdk.sessions.add(session)
        yield SystemMessage("init", {"session_id": sdk.init_session_override or session})
        yield AssistantMessage(
            [TextBlock("working")], message_id="m1",
            usage={"input_tokens": 10, "output_tokens": 2,
                   "cache_read_input_tokens": 5})
        yield AssistantMessage(
            [ToolUseBlock("tool-1", "Read", {"file_path": "a.txt"})], message_id="m2",
            usage={"input_tokens": 12, "output_tokens": 3})
        yield UserMessage([ToolResultBlock("tool-1", "file body")])
        yield ResultMessage(result="all done",
                            usage={"input_tokens": 22, "output_tokens": 5,
                                   "cache_read_input_tokens": 5})

    sdk.scenario = default_scenario

    class ClaudeSDKClient:
        def __init__(self, options):
            self.options = options
            self.interrupted = asyncio.Event()
            self.disconnected = False
            sdk.clients.append(self)

        async def connect(self):
            return None

        async def query(self, prompt):
            self.prompt = prompt

        async def receive_response(self):
            async for message in sdk.scenario(self):
                yield message

        async def interrupt(self):
            self.interrupted.set()

        async def get_server_info(self):
            return {"models": sdk.models,
                    "account": {"subscriptionType": "Claude Pro", "email": "x@example.test"}}

        async def disconnect(self):
            self.disconnected = True

    sdk.ClaudeSDKClient = ClaudeSDKClient

    def get_session_info(session_id, directory=None):
        return object() if session_id in sdk.sessions else None

    sdk.get_session_info = get_session_info
    return sdk


@pytest.fixture
def claude(tmp_path):
    root = tmp_path / "projects"
    workspace = root / "app"
    workspace.mkdir(parents=True)
    (workspace / "a.txt").write_text("hello")
    sdk = make_sdk()
    adapter = ClaudeHostAdapter(tmp_path / "state", root, sdk=sdk, _runtime_env={})
    yield adapter, workspace, sdk
    adapter.close()


def _revision(adapter, profile_id):
    return next(row["revision"] for row in adapter.profiles()["profiles"]
                if row["id"] == profile_id)


def _conversation(adapter, workspace, profile="read-only"):
    return adapter.create_conversation({
        "workspaceId": "ws_test", "directory": str(workspace),
        "securityProfile": {"id": profile, "revision": _revision(adapter, profile)}})


def _wait(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("condition was not reached in time")


def _terminal(adapter, run_id):
    return _wait(lambda: (lambda run: run if run["phase"] == "terminal" else None)(
        adapter.run(run_id)))


def _text(value="go"):
    return [{"type": "text", "text": value}]


# -- tool decisions --------------------------------------------------------

def test_read_tools_stay_inside_workspace(tmp_path):
    cwd = tmp_path / "ws"
    cwd.mkdir()
    (cwd / "ok.txt").write_text("x")
    outside = tmp_path / "secret.txt"
    outside.write_text("s")
    (cwd / "link").symlink_to(outside)
    assert decide_tool_use(READ_ONLY, str(cwd), "Read", {"file_path": "ok.txt"})[0] == "allow"
    assert decide_tool_use(READ_ONLY, str(cwd), "Read",
                           {"file_path": str(cwd / "ok.txt")})[0] == "allow"
    assert decide_tool_use(READ_ONLY, str(cwd), "Read", {"file_path": str(outside)})[0] == "deny"
    assert decide_tool_use(READ_ONLY, str(cwd), "Read", {"file_path": "../secret.txt"})[0] == "deny"
    assert decide_tool_use(READ_ONLY, str(cwd), "Read", {"file_path": "link"})[0] == "deny"
    assert decide_tool_use(READ_ONLY, str(cwd), "Read", {})[0] == "deny"
    assert decide_tool_use(READ_ONLY, str(cwd), "Glob", {"pattern": "../*"})[0] == "deny"
    assert decide_tool_use(READ_ONLY, str(cwd), "Glob", {"pattern": "/etc/*"})[0] == "deny"
    assert decide_tool_use(READ_ONLY, str(cwd), "Glob", {"pattern": "~/*"})[0] == "deny"
    assert decide_tool_use(READ_ONLY, str(cwd), "Glob",
                           {"pattern": str(cwd) + "/**/*.py"})[0] == "allow"
    assert decide_tool_use(READ_ONLY, str(cwd), "Glob", {"pattern": "**/*.py"})[0] == "allow"
    assert decide_tool_use(READ_ONLY, str(cwd), "Grep", {"pattern": "x"})[0] == "allow"


def test_protected_paths_are_denied_even_for_reads(tmp_path):
    cwd = tmp_path / "ws"
    (cwd / ".git").mkdir(parents=True)
    for name in (".env", ".env.local", ".git/config"):
        decision = decide_tool_use(READ_ONLY, str(cwd), "Read", {"file_path": name})
        assert decision[0] == "deny", name
    assert decide_tool_use(READ_ONLY, str(cwd), "Read",
                           {"file_path": ".env.example"})[0] == "allow"
    assert decide_tool_use(REVIEWED, str(cwd), "Write", {"file_path": ".git/hooks/x"})[0] == "deny"


def test_profile_modes_for_edits_shell_and_web(tmp_path):
    cwd = tmp_path
    edit = {"file_path": "new.txt"}
    assert decide_tool_use(READ_ONLY, str(cwd), "Edit", edit)[0] == "deny"
    assert decide_tool_use(READ_ONLY, str(cwd), "Bash", {"command": "ls"})[0] == "deny"
    assert decide_tool_use(REVIEWED, str(cwd), "Edit", edit)[0] == "ask"
    assert decide_tool_use(REVIEWED, str(cwd), "Write", edit)[0] == "ask"
    assert decide_tool_use(REVIEWED, str(cwd), "Bash", {"command": "ls"})[0] == "ask"
    assert decide_tool_use(REVIEWED, str(cwd), "WebFetch", {"url": "https://x.test"})[0] == "deny"
    assert decide_tool_use(YOLO_SHELL, str(cwd), "Bash", {"command": "ls"})[0] == "allow"
    assert decide_tool_use(YOLO_SHELL, str(cwd), "WebFetch", {"url": "https://x.test"})[0] == "allow"
    assert decide_tool_use(YOLO_SHELL, str(cwd), "Edit", {"file_path": "../x"})[0] == "deny"


def test_unknown_tools_and_bad_inputs_fail_closed(tmp_path):
    for name in ("EnterWorktree", "AskUserQuestion", "ExitPlanMode", "mcp__", "mcp__bad name",
                 "mcp__" + "x" * 300, "", None, 3):
        assert decide_tool_use(YOLO_SHELL, str(tmp_path), name, {})[0] == "deny"
    assert decide_tool_use(YOLO_SHELL, str(tmp_path), "Agent", "x")[0] == "deny"
    assert decide_tool_use(YOLO_SHELL, str(tmp_path), "Bash", {"command": ""})[0] == "deny"
    assert decide_tool_use(YOLO_SHELL, str(tmp_path), "Bash",
                           {"command": "x" * 20000})[0] == "deny"
    assert decide_tool_use(YOLO_SHELL, str(tmp_path), "Bash", "ls")[0] == "deny"


def test_extensions_follow_the_profile_and_meta_tools_are_allowed(tmp_path):
    call = {"x": 1}
    assert decide_tool_use(READ_ONLY, str(tmp_path), "mcp__demo__ping", call)[0] == "deny"
    assert decide_tool_use(REVIEWED, str(tmp_path), "mcp__demo__ping", call)[0] == "ask"
    assert decide_tool_use(YOLO_SHELL, str(tmp_path), "mcp__demo__ping", call)[0] == "allow"
    for name in ("Agent", "Task", "Skill", "TodoWrite", "ToolSearch"):
        assert decide_tool_use(READ_ONLY, str(tmp_path), name, {})[0] == "allow", name
    assert decide_tool_use(REVIEWED, str(tmp_path), "Monitor", {"command": "tail x"})[0] == "ask"
    assert decide_tool_use(REVIEWED, str(tmp_path), "Monitor", {"command": ""})[0] == "deny"


def test_edits_never_reach_claude_configuration(tmp_path):
    for name in (".claude/settings.json", ".claude/hooks/x.sh", "pkg/.claude/skills/a.md",
                 ".mcp.json", "sub/.mcp.json"):
        assert decide_tool_use(YOLO_SHELL, str(tmp_path), "Write", {"file_path": name})[0] == "deny", name
    assert decide_tool_use(YOLO_SHELL, str(tmp_path), "NotebookEdit",
                           {"notebook_path": ".claude/n.ipynb"})[0] == "deny"
    # Reading configuration, and editing project instructions, stays possible.
    assert decide_tool_use(READ_ONLY, str(tmp_path), "Read",
                           {"file_path": ".claude/settings.json"})[0] == "allow"
    assert decide_tool_use(YOLO_SHELL, str(tmp_path), "Write",
                           {"file_path": "CLAUDE.md"})[0] == "allow"


def test_setting_sources_parsing():
    assert parse_setting_sources(None) == ("user", "project", "local")
    assert parse_setting_sources("local, user") == ("user", "local")
    assert parse_setting_sources("none") == ()
    for bad in ("", "user,managed", "none,user", 3):
        with pytest.raises(AdapterFailure):
            parse_setting_sources(bad)


def test_allowed_tools_follow_profile():
    meta = ["Agent", "Task", "Skill", "TodoWrite", "ToolSearch"]
    assert allowed_tools(READ_ONLY) == ["Read", "Glob", "Grep"] + meta
    assert "Bash" in allowed_tools(REVIEWED) and "Edit" in allowed_tools(REVIEWED)
    assert "WebFetch" not in allowed_tools(REVIEWED)
    assert "WebFetch" in allowed_tools(YOLO_SHELL)


# -- usage -----------------------------------------------------------------

def test_usage_normalization():
    assert _normalize_claude_usage({"input_tokens": 3, "output_tokens": 4,
                                    "cache_read_input_tokens": 5,
                                    "cache_creation_input_tokens": 6}) == {
        "inputTokens": 3, "outputTokens": 4, "cachedInputTokens": 5,
        "cacheWriteInputTokens": 6, "totalTokens": 18}
    assert _normalize_claude_usage({"input_tokens": 3}) == {"inputTokens": 3}
    assert _normalize_claude_usage({"input_tokens": -1}) is None
    assert _normalize_claude_usage({"input_tokens": True}) is None
    assert _normalize_claude_usage({}) is None
    assert _normalize_claude_usage("x") is None


# -- descriptor, models and profiles ---------------------------------------

def test_descriptor_is_accepted_by_the_bridge_client(claude):
    adapter, _, _ = claude
    descriptor = adapter.descriptor()
    parsed = wbrp.Descriptor.parse(descriptor, expected_runtime="claude")
    assert parsed.release_status == "valid"
    assert parsed.release["component"] == "claude-host-adapter"
    assert descriptor["features"]["securityRebind"] == 1
    assert "steering" not in descriptor["features"]
    assert descriptor["features"]["usageLimits"] == 1


def test_models_and_reasoning_validation(claude):
    adapter, workspace, _ = claude
    models = adapter.models()["models"]
    assert [row["selector"] for row in models] == ["default", "haiku"]
    assert models[0]["default"] is True and models[0]["reasoningOptions"] == ["low", "high"]
    conversation = _conversation(adapter, workspace)
    with pytest.raises(AdapterFailure) as raised:
        adapter.start_run(conversation["id"], {"input": _text(), "model": "haiku",
                                               "reasoning": "high"})
    assert raised.value.code == "unsupported_reasoning"
    with pytest.raises(AdapterFailure):
        adapter.start_run(conversation["id"], {"input": _text(), "reasoning": "bogus"})


def test_builtin_and_custom_profiles(claude):
    adapter, workspace, _ = claude
    rows = {row["id"]: row for row in adapter.profiles()["profiles"]}
    assert set(rows) == {"read-only", "workspace-write-reviewed"}
    assert all(not row["mutable"] for row in rows.values())
    saved = adapter.save_profile({"id": "shell-ok", "config": YOLO_SHELL})
    assert saved["mutable"] is True
    with pytest.raises(AdapterFailure) as raised:
        adapter.save_profile({"id": "shell-ok", "config": READ_ONLY})
    assert raised.value.code == "profile_mismatch"
    updated = adapter.save_profile({"id": "shell-ok", "config": READ_ONLY,
                                    "expectedRevision": saved["revision"]})
    assert updated["revision"] != saved["revision"]
    for bad in ({"id": "read-only", "config": READ_ONLY},
                {"id": "Bad Id", "config": READ_ONLY},
                {"id": "x", "config": {"edits": "deny"}},
                {"id": "x", "config": {**READ_ONLY, "shell": "maybe"}}):
        with pytest.raises(AdapterFailure):
            adapter.save_profile(bad)
    scoped = adapter.profiles("ws_test", str(workspace))["profiles"]
    assert all(row["available"] for row in scoped)
    with pytest.raises(AdapterFailure):
        adapter.delete_profile("read-only")
    assert adapter.delete_profile("shell-ok") == {"deleted": "shell-ok"}
    with pytest.raises(AdapterFailure) as raised:
        adapter.delete_profile("shell-ok")
    assert raised.value.status == 404


# -- conversations ---------------------------------------------------------

def test_conversation_creation_rules(claude, tmp_path):
    adapter, workspace, _ = claude
    created = _conversation(adapter, workspace)
    assert created["runtime"] == "claude" and created["status"] == "idle"
    assert created["securityBinding"]["source"] == "profile"
    assert adapter.conversation(created["id"])["workspaceId"] == "ws_test"
    base = {"workspaceId": "ws_test", "directory": str(workspace)}
    with pytest.raises(AdapterFailure) as raised:
        adapter.create_conversation({**base, "securityProfile": {
            "id": "read-only", "revision": "stale"}})
    assert raised.value.code == "profile_mismatch"
    with pytest.raises(AdapterFailure) as raised:
        adapter.create_conversation({**base, "securityBinding": {"source": "runtime-config"}})
    assert raised.value.code == "runtime_config_unavailable"
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    with pytest.raises(AdapterFailure):
        adapter.create_conversation({
            "workspaceId": "ws_test", "directory": str(outside),
            "securityProfile": {"id": "read-only", "revision": _revision(adapter, "read-only")}})
    with pytest.raises(AdapterFailure) as raised:
        adapter.conversation("conv_missing")
    assert raised.value.status == 404


# -- runs ------------------------------------------------------------------

def test_run_lifecycle_activities_and_usage(claude):
    adapter, workspace, sdk = claude
    conversation = _conversation(adapter, workspace)
    started = adapter.start_run(conversation["id"], {"input": _text("hi"),
                                                     "clientRunId": "c1"})
    wbrp.validate_run_state(started)
    done = _terminal(adapter, started["id"])
    wbrp.validate_run_state(done)
    assert done["outcome"] == "succeeded" and done["result"] == "all done"
    assert done["usage"] == {"inputTokens": 22, "outputTokens": 5, "cachedInputTokens": 5,
                             "totalTokens": 32}
    assert done["securityBinding"]["profile"]["id"] == "read-only"
    activities = adapter.activities(started["id"])["activities"]
    assert len(activities) == 1
    assert activities[0]["kind"] == "tool_call" and activities[0]["status"] == "completed"
    assert activities[0]["result"]["outputPreview"] == "file body"
    options = sdk.clients[-1].options
    assert options.setting_sources == ["user", "project", "local"]
    assert options.strict_mcp_config is True  # read-only denies extensions
    assert options.system_prompt == {"type": "preset", "preset": "claude_code"}
    assert json.loads(options.settings) == {"permissions": {
        "disableBypassPermissionsMode": "disable"}}
    assert options.tools == ["Read", "Glob", "Grep", "Agent", "Task", "Skill",
                             "TodoWrite", "ToolSearch"]
    assert options.session_id == conversation["nativeId"] and options.resume is None
    assert sdk.clients[-1].prompt == "hi"
    assert sdk.clients[-1].disconnected is True
    # Continuation resumes the session proven to exist in Claude's own store.
    second = adapter.start_run(conversation["id"], {"input": _text("again")})
    _terminal(adapter, second["id"])
    resumed = sdk.clients[-1].options
    assert resumed.resume == conversation["nativeId"] and resumed.session_id is None
    assert adapter.conversation(conversation["id"])["status"] == "idle"
    types_seen = [event["type"] for event in adapter.events(0, 0)["events"]]
    assert {"conversation.created", "run.started", "run.completed"} <= set(types_seen)


def test_continuation_resumes_even_before_the_started_flag_is_recorded(claude):
    adapter, workspace, sdk = claude
    conversation = _conversation(adapter, workspace)
    first = adapter.start_run(conversation["id"], {"input": _text()})
    _terminal(adapter, first["id"])
    # A run turns terminal slightly before its owner records the flag, so an
    # immediate continuation must decide from the live session lookup.
    with adapter.lock, adapter.db:
        adapter.db.execute("UPDATE conversations SET started=0 WHERE id=?",
                           (conversation["id"],))
    second = adapter.start_run(conversation["id"], {"input": _text("again")})
    _terminal(adapter, second["id"])
    options = sdk.clients[-1].options
    assert options.resume == conversation["nativeId"] and options.session_id is None


def test_client_run_id_is_idempotent(claude):
    adapter, workspace, _ = claude
    conversation = _conversation(adapter, workspace)
    first = adapter.start_run(conversation["id"], {"input": _text("x"), "clientRunId": "same"})
    _terminal(adapter, first["id"])
    again = adapter.start_run(conversation["id"], {"input": _text("x"), "clientRunId": "same"})
    assert again["id"] == first["id"]
    assert adapter.find_run(conversation["id"], "same")["id"] == first["id"]
    with pytest.raises(AdapterFailure) as raised:
        adapter.start_run(conversation["id"], {"input": _text("different"),
                                               "clientRunId": "same"})
    assert raised.value.code == "idempotency_conflict"
    with pytest.raises(AdapterFailure) as raised:
        adapter.find_run(conversation["id"], "unknown")
    assert raised.value.status == 404


def test_only_text_input_is_accepted(claude):
    adapter, workspace, _ = claude
    conversation = _conversation(adapter, workspace)
    with pytest.raises(AdapterFailure) as raised:
        adapter.start_run(conversation["id"], {"input": [{"type": "image", "url": "x"}]})
    assert raised.value.code == "unsupported_input"
    with pytest.raises(AdapterFailure):
        adapter.start_run(conversation["id"], {"input": []})


def test_busy_conversation_and_cancel(claude):
    adapter, workspace, sdk = claude
    release = threading.Event()

    async def blocking(client):
        session = client.options.session_id or client.options.resume
        sdk.sessions.add(session)
        yield SystemMessage("init", {"session_id": session})
        await client.interrupted.wait()
        release.set()
        yield ResultMessage(subtype="error_during_execution", is_error=True,
                            terminal_reason="aborted_streaming")

    sdk.scenario = blocking
    conversation = _conversation(adapter, workspace)
    started = adapter.start_run(conversation["id"], {"input": _text()})
    assert adapter.conversation(conversation["id"])["status"] == "active"
    with pytest.raises(AdapterFailure) as raised:
        adapter.start_run(conversation["id"], {"input": _text("other")})
    assert raised.value.code == "conversation_busy"
    with pytest.raises(AdapterFailure) as raised:
        adapter.rebind_conversation(conversation["id"], {
            "source": "profile", "profile": {
                "id": "workspace-write-reviewed",
                "revision": _revision(adapter, "workspace-write-reviewed")}})
    assert raised.value.code == "conversation_busy"
    adapter.cancel(started["id"])
    assert release.wait(5)
    done = _terminal(adapter, started["id"])
    assert done["outcome"] == "cancelled"
    assert adapter.cancel(started["id"])["outcome"] == "cancelled"


def test_native_failure_is_reported_with_bounded_error(claude):
    adapter, workspace, sdk = claude

    async def failing(client):
        session = client.options.session_id
        sdk.sessions.add(session)
        yield SystemMessage("init", {"session_id": session})
        yield ResultMessage(subtype="error_during_execution", is_error=True,
                            errors=["quota exhausted"], terminal_reason="model_error")

    sdk.scenario = failing
    conversation = _conversation(adapter, workspace)
    started = adapter.start_run(conversation["id"], {"input": _text()})
    done = _terminal(adapter, started["id"])
    assert done["outcome"] == "failed" and "quota exhausted" in done["error"]


def test_unconfirmed_session_binding_fails_the_start(claude):
    adapter, workspace, sdk = claude
    sdk.init_session_override = "someone-elses-session"
    conversation = _conversation(adapter, workspace)
    with pytest.raises(AdapterFailure) as raised:
        adapter.start_run(conversation["id"], {"input": _text()})
    assert raised.value.code == "runtime_unavailable"
    assert adapter.conversation(conversation["id"])["status"] == "idle"


def test_sdk_connect_failure_fails_closed(claude):
    adapter, workspace, sdk = claude
    original = sdk.ClaudeSDKClient.connect

    async def boom(self):
        raise RuntimeError("cli exploded at /Users/private WB_RUNTIME_TOKEN=secret-token")

    sdk.ClaudeSDKClient.connect = boom
    try:
        conversation = _conversation(adapter, workspace)
        with pytest.raises(AdapterFailure) as raised:
            adapter.start_run(conversation["id"], {"input": _text()})
    finally:
        sdk.ClaudeSDKClient.connect = original
    assert "secret-token" not in str(raised.value)
    assert adapter.conversation(conversation["id"])["status"] == "idle"


def test_missing_sdk_is_a_bounded_unavailable_error(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    root.mkdir()
    adapter = ClaudeHostAdapter(tmp_path / "state", root, _runtime_env={})
    try:
        monkeypatch.setitem(__import__("sys").modules, "claude_agent_sdk", None)
        with pytest.raises(AdapterFailure) as raised:
            adapter.descriptor()
        assert raised.value.status == 503 and "workspace-bridge[claude]" in str(raised.value)
    finally:
        adapter.close()


# -- hook and approvals ----------------------------------------------------

def _hook_decision(options, tool, tool_input):
    hook = options.hooks["PreToolUse"][0].hooks[0]
    result = asyncio.run(hook({"tool_name": tool, "tool_input": tool_input}, "id", None))
    return result["hookSpecificOutput"]


def test_hook_enforces_profile_before_any_native_permission(claude):
    adapter, workspace, sdk = claude
    holder = {}

    async def capture(client):
        session = client.options.session_id or client.options.resume
        sdk.sessions.add(session)
        holder.setdefault("options", []).append(client.options)
        yield SystemMessage("init", {"session_id": session})
        yield ResultMessage(result="ok")

    sdk.scenario = capture
    for profile, expected in (("read-only", "deny"), ("workspace-write-reviewed", "ask")):
        conversation = _conversation(adapter, workspace, profile)
        started = adapter.start_run(conversation["id"], {"input": _text()})
        _terminal(adapter, started["id"])
        options = holder["options"][-1]
        output = _hook_decision(options, "Bash", {"command": "rm -rf /"})
        assert output["hookEventName"] == "PreToolUse"
        assert output["permissionDecision"] == expected
        assert _hook_decision(options, "Read", {"file_path": "a.txt"})["permissionDecision"] == "allow"
        assert _hook_decision(options, "Read", {"file_path": "../x"})["permissionDecision"] == "deny"
        assert _hook_decision(options, "EnterWorktree", {})["permissionDecision"] == "deny"
        assert _hook_decision(options, "Agent", {})["permissionDecision"] == "allow"
        assert _hook_decision(options, "mcp__demo__ping", {})["permissionDecision"] == expected
        assert options.strict_mcp_config is (expected == "deny")


@pytest.mark.parametrize("choice,expected", [("approve", "allow"), ("deny", "deny")])
def test_interaction_is_resolved_through_the_bridge(claude, choice, expected):
    adapter, workspace, sdk = claude
    outcome = {}

    async def asking(client):
        session = client.options.session_id or client.options.resume
        sdk.sessions.add(session)
        yield SystemMessage("init", {"session_id": session})
        context = types.SimpleNamespace(tool_use_id="tu-1", title=None)
        outcome["result"] = await client.options.can_use_tool(
            "Bash", {"command": "make test"}, context)
        yield ResultMessage(result="ok")

    sdk.scenario = asking
    conversation = _conversation(adapter, workspace, "workspace-write-reviewed")
    started = adapter.start_run(conversation["id"], {"input": _text()})
    pending = _wait(lambda: adapter.interactions(started["id"])["interactions"])[0]
    assert pending["resource"] == "make test" and pending["state"] == "pending"
    assert adapter.run(started["id"])["activeState"] == "waiting_interaction"
    choice_id = next(item["id"] for item in pending["choices"] if item["semantic"] == choice)
    with pytest.raises(AdapterFailure):
        adapter.resolve(pending["id"], {"choiceId": "choice_unknown"})
    assert adapter.resolve(pending["id"], {"choiceId": choice_id})["state"] == "resolved"
    with pytest.raises(AdapterFailure) as raised:
        adapter.resolve(pending["id"], {"choiceId": choice_id})
    assert raised.value.code == "interaction_stale"
    _terminal(adapter, started["id"])
    assert outcome["result"].behavior == expected


def test_cancel_denies_pending_interaction(claude):
    adapter, workspace, sdk = claude
    outcome = {}

    async def asking(client):
        session = client.options.session_id
        sdk.sessions.add(session)
        yield SystemMessage("init", {"session_id": session})
        context = types.SimpleNamespace(tool_use_id="tu-2", title="Run a thing")
        outcome["result"] = await client.options.can_use_tool(
            "Edit", {"file_path": "a.txt"}, context)
        yield ResultMessage(subtype="error_during_execution", is_error=True,
                            terminal_reason="aborted_tools")

    sdk.scenario = asking
    conversation = _conversation(adapter, workspace, "workspace-write-reviewed")
    started = adapter.start_run(conversation["id"], {"input": _text()})
    _wait(lambda: adapter.interactions(started["id"])["interactions"])
    adapter.cancel(started["id"])
    done = _terminal(adapter, started["id"])
    assert done["outcome"] == "cancelled"
    assert outcome["result"].behavior == "deny" and outcome["result"].interrupt is True
    assert adapter.interactions(started["id"])["interactions"] == []


def test_can_use_tool_rechecks_policy(claude):
    adapter, workspace, sdk = claude
    outcome = {}

    async def asking(client):
        session = client.options.session_id
        sdk.sessions.add(session)
        yield SystemMessage("init", {"session_id": session})
        outcome["result"] = await client.options.can_use_tool(
            "Write", {"file_path": ".env"}, types.SimpleNamespace(tool_use_id="x", title=None))
        yield ResultMessage(result="ok")

    sdk.scenario = asking
    conversation = _conversation(adapter, workspace, "workspace-write-reviewed")
    started = adapter.start_run(conversation["id"], {"input": _text()})
    _terminal(adapter, started["id"])
    assert outcome["result"].behavior == "deny"
    assert adapter.interactions(started["id"])["interactions"] == []


# -- rebind and restart ----------------------------------------------------

def test_rebind_changes_the_profile_for_the_next_run(claude):
    adapter, workspace, sdk = claude
    conversation = _conversation(adapter, workspace)
    target = {"source": "profile", "profile": {
        "id": "workspace-write-reviewed",
        "revision": _revision(adapter, "workspace-write-reviewed")}}
    rebound = adapter.rebind_conversation(conversation["id"], target)
    assert rebound["securityBinding"] == target and rebound["status"] == "idle"
    started = adapter.start_run(conversation["id"], {"input": _text()})
    _terminal(adapter, started["id"])
    assert "Bash" in sdk.clients[-1].options.tools
    with pytest.raises(AdapterFailure) as raised:
        adapter.rebind_conversation(conversation["id"], {"source": "profile", "profile": {
            "id": "read-only", "revision": "stale"}})
    assert raised.value.code == "profile_mismatch"
    with pytest.raises(AdapterFailure):
        adapter.rebind_conversation(conversation["id"], {"source": "runtime-config"})


def test_restart_interrupts_active_runs_and_keeps_sessions(tmp_path):
    root = tmp_path / "projects"
    workspace = root / "app"
    workspace.mkdir(parents=True)
    sdk = make_sdk()
    first = ClaudeHostAdapter(tmp_path / "state", root, sdk=sdk, _runtime_env={})
    conversation = _conversation(first, workspace)
    done = first.start_run(conversation["id"], {"input": _text()})
    _terminal(first, done["id"])
    first.close()
    database = sqlite3.connect(tmp_path / "state" / "claude-adapter.sqlite3")
    database.execute(
        "INSERT INTO runs(id,conversation,phase,active_state,result,error,created,updated) "
        "VALUES('run_orphan',?,'active','running','','','t','t')", (conversation["id"],))
    database.commit()
    database.close()
    second = ClaudeHostAdapter(tmp_path / "state", root, sdk=sdk, _runtime_env={})
    try:
        orphan = second.run("run_orphan")
        assert orphan["phase"] == "terminal" and orphan["outcome"] == "interrupted"
        assert second.conversation(conversation["id"])["status"] == "idle"
        again = second.start_run(conversation["id"], {"input": _text("later")})
        _terminal(second, again["id"])
        assert sdk.clients[-1].options.resume == conversation["nativeId"]
    finally:
        second.close()


def test_state_is_exclusively_locked(tmp_path):
    root = tmp_path / "projects"
    root.mkdir()
    first = ClaudeHostAdapter(tmp_path / "state", root, sdk=make_sdk(), _runtime_env={})
    try:
        with pytest.raises(RuntimeError):
            ClaudeHostAdapter(tmp_path / "state", root, sdk=make_sdk(), _runtime_env={})
    finally:
        first.close()


def test_unsupported_optional_capabilities(claude):
    adapter, _, _ = claude
    with pytest.raises(AdapterFailure) as raised:
        adapter.steer("run_x", {})
    assert raised.value.status == 501


# -- usage limits ----------------------------------------------------------

def _rate_event(now, **windows):
    unified = {key: {"utilization": used, "resetsAt": now + reset}
               for key, (used, reset) in windows.items()}
    return RateLimitEvent(RateLimitInfo(
        status="allowed", rate_limit_type="five_hour", raw={"unifiedWindows": unified}))


def test_usage_limits_report_the_last_observed_windows(claude):
    adapter, workspace, sdk = claude
    assert adapter.usage_limits() == {"available": False, "ordinaryUsageAllowed": None,
                                      "buckets": []}
    adapter.models()  # the model probe learns the subscription plan
    now = int(time.time())

    async def limited(client):
        session = client.options.session_id or client.options.resume
        sdk.sessions.add(session)
        yield SystemMessage("init", {"session_id": session})
        yield _rate_event(now, five_hour=(0.456, 3600), seven_day=(0.66, 86400))
        yield ResultMessage(result="ok")

    sdk.scenario = limited
    conversation = _conversation(adapter, workspace)
    _terminal(adapter, adapter.start_run(conversation["id"], {"input": _text()})["id"])
    limits = wbrp.validate_usage_limits(adapter.usage_limits())
    assert limits["available"] is True and limits["ordinaryUsageAllowed"] is None
    (bucket,) = limits["buckets"]
    assert bucket["planType"] == "Claude Pro" and bucket["limitName"] == "Claude Code"
    assert [(w["windowDurationMins"], w["usedPercent"], w["remainingPercent"])
            for w in bucket["windows"]] == [(300, 46, 54), (10080, 66, 34)]
    assert bucket["windows"][0]["resetsAt"] == now + 3600
    # Nothing outside the snapshot leaks (no account email).
    assert "x@example.test" not in json.dumps(limits)


def test_usage_limits_drop_reset_windows_and_ignore_bad_events(claude):
    adapter, workspace, sdk = claude
    now = int(time.time())

    async def mixed(client):
        session = client.options.session_id or client.options.resume
        sdk.sessions.add(session)
        yield SystemMessage("init", {"session_id": session})
        yield _rate_event(now, five_hour=(0.5, -10), seven_day=(0.2, 600),
                          seven_day_opus=(0.9, 600))
        yield RateLimitEvent(RateLimitInfo(rate_limit_type="five_hour",
                                           utilization=True, resets_at=now + 5))
        yield RateLimitEvent(None)
        yield ResultMessage(result="ok")

    sdk.scenario = mixed
    conversation = _conversation(adapter, workspace)
    done = _terminal(adapter, adapter.start_run(conversation["id"], {"input": _text()})["id"])
    assert done["outcome"] == "succeeded"
    (bucket,) = adapter.usage_limits()["buckets"]
    assert [w["windowDurationMins"] for w in bucket["windows"]] == [10080]


def test_rejected_window_marks_ordinary_usage_blocked(claude):
    adapter, workspace, sdk = claude
    now = int(time.time())

    async def rejected(client):
        session = client.options.session_id or client.options.resume
        sdk.sessions.add(session)
        yield SystemMessage("init", {"session_id": session})
        yield RateLimitEvent(RateLimitInfo(
            status="rejected", rate_limit_type="five_hour", utilization=1.0,
            resets_at=now + 900, raw={}))
        yield ResultMessage(result="ok")

    sdk.scenario = rejected
    conversation = _conversation(adapter, workspace)
    _terminal(adapter, adapter.start_run(conversation["id"], {"input": _text()})["id"])
    limits = adapter.usage_limits()
    assert limits["ordinaryUsageAllowed"] is False
    assert limits["buckets"][0]["rateLimitReachedType"] == "five_hour"


def test_catalog_probe_never_loads_workspace_configuration(tmp_path):
    root = tmp_path / "projects"
    root.mkdir()
    sdk = make_sdk()
    adapter = ClaudeHostAdapter(tmp_path / "state", root, sdk=sdk, _runtime_env={},
                                setting_sources="user,project,local")
    try:
        adapter.models()
        probe = sdk.clients[-1].options
        assert probe.setting_sources == ["user"] and probe.strict_mcp_config is True
    finally:
        adapter.close()
    adapter = ClaudeHostAdapter(tmp_path / "state2", root, sdk=sdk, _runtime_env={},
                                setting_sources="none")
    try:
        adapter.models()
        assert sdk.clients[-1].options.setting_sources == []
    finally:
        adapter.close()


# -- HTTP surface ----------------------------------------------------------

async def test_private_http_surface_requires_token(claude):
    adapter, workspace, _ = claude
    app = make_app(adapter, "private-token")
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://127.0.0.1:8774") as client:
        assert (await client.get("/v1/descriptor")).status_code == 401
        wrong = await client.get("/v1/descriptor", headers={"X-Runtime-Token": "nope"})
        assert wrong.status_code == 401
        headers = {"X-Runtime-Token": "private-token"}
        allowed = await client.get("/v1/descriptor", headers=headers)
        assert allowed.status_code == 200 and allowed.json()["runtime"]["id"] == "claude"
        profiles = (await client.get("/v1/profiles", headers=headers)).json()["profiles"]
        revision = next(row["revision"] for row in profiles if row["id"] == "read-only")
        created = await client.post("/v1/conversations", headers=headers, json={
            "workspaceId": "ws_test", "directory": str(workspace),
            "securityProfile": {"id": "read-only", "revision": revision}})
        assert created.status_code == 200
        missing = await client.get("/v1/conversations/conv_missing", headers=headers)
        assert missing.status_code == 404 and missing.json()["code"] == "not_found"
        limits = await client.get("/v1/usage-limits", headers=headers)
        assert limits.status_code == 200 and limits.json()["available"] is False
        unknown = await client.get("/v1/nope", headers=headers)
        assert unknown.status_code == 404


def test_make_app_requires_a_token(claude):
    adapter, _, _ = claude
    with pytest.raises(ValueError):
        make_app(adapter, "")
