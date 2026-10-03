"""The Bridge coordinator drives the Claude adapter through Runtime Protocol v1."""
from __future__ import annotations

import time
import types

import pytest

from workspace_bridge.api import Handoff
from workspace_bridge.claude_host_adapter import ClaudeHostAdapter
from workspace_bridge.cli import initialize
from workspace_bridge.service import Service
from workspace_bridge.wbrp import Descriptor

from conftest import attach_test_node, create_test_adapter
from test_claude_host_adapter import (AssistantMessage, ResultMessage, SystemMessage,
                                      TextBlock, make_sdk)

ADAPTER_ID = "adapter_0000000000000000000000c1"


class ClaudeDirect:
    """In-process WBRP transport; production uses HttpRuntimeAdapter."""

    def __init__(self, native):
        self.native = native

    def descriptor(self):
        return Descriptor.parse(self.native.descriptor(), expected_runtime="claude")

    def models(self, workspace_id):
        return self.native.models()["models"]

    def profile_catalog(self, workspace_id=None, directory=None, *, fresh=False):
        if workspace_id is None:
            return self.native.profiles()
        return self.native.profiles(workspace_id, directory, fresh=fresh)

    def profiles(self):
        return self.profile_catalog()["profiles"]

    def create_conversation(self, payload):
        return self.native.create_conversation(payload)

    def conversation(self, ident):
        return self.native.conversation(ident)

    def rebind_conversation(self, ident, binding):
        return self.native.rebind_conversation(ident, binding)

    def start_run(self, ident, payload):
        return self.native.start_run(ident, payload)

    def run(self, ident):
        return self.native.run(ident)

    def find_run(self, conversation_id, client_run_id):
        return self.native.find_run(conversation_id, client_run_id)

    def interactions(self, ident):
        return self.native.interactions(ident)["interactions"]

    def resolve(self, ident, payload):
        return self.native.resolve(ident, payload)

    def activities(self, ident):
        return self.native.activities(ident)["activities"]

    def cancel(self, ident):
        return self.native.cancel(ident)


@pytest.fixture
def claude_env(tmp_path, payload):
    parent = tmp_path / "projects"
    parent.mkdir()
    root = parent / "alpha"
    root.mkdir()
    state = tmp_path / "bridge-state"
    cfg = initialize(state, 8765, 8766)
    sdk = make_sdk()
    native = ClaudeHostAdapter(tmp_path / "claude-state", parent, sdk=sdk, _runtime_env={})
    service = Service(state, cfg, run_coordinator_background=False)
    node, node_record = attach_test_node(service, tmp_path / "node-state", parent)
    create_test_adapter(service, node_record["id"], {
        "name": "Local Claude", "runtime_type": "claude",
        "base_url": "http://127.0.0.1:8774", "token": "claude-test-secret"}, ADAPTER_ID)
    client = ClaudeDirect(native)
    client_factory = service.adapter_registry.client

    def adapter_client(adapter_id, **kwargs):
        if adapter_id == ADAPTER_ID:
            service.adapter_registry.get(
                adapter_id, require_enabled=kwargs.get("require_enabled", False))
            return client
        return client_factory(adapter_id, **kwargs)

    service.adapter_registry.client = adapter_client
    ws_id = service.add_workspace("Alpha", str(root), [])["workspace"]["id"]
    token = service.manage_bridge("rotate_token")["token"]
    service.manage_workspace(ws_id, "enable")
    service.manage_workspace(ws_id, "set_agent_enabled", agent_enabled=True)
    service.set_workspace_route(service.workspace(ws_id), ADAPTER_ID, True,
                                "workspace-write-reviewed")
    service.run_coordinator.set_model_policy(ADAPTER_ID, ["default"], "default")
    job = service.call(ws_id, token, "prepare_handoff",
                       Handoff.model_validate(payload).model_dump())
    yield service, ws_id, token, job, native, sdk
    service.close()
    native.close()
    node["stop"]()


def _read(service, ws_id, token, run_id):
    return service.call(ws_id, token, "read_agent_run", {"run_id": run_id})


def _until(fn, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = fn()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("condition was not reached in time")


def test_adapter_is_listed_as_a_claude_instance(claude_env):
    service, ws_id, token, _, _, _ = claude_env
    listed = service.call(ws_id, token, "list_agent_adapters", {})
    row = next(item for item in listed["adapters"] if item["adapter_id"] == ADAPTER_ID)
    assert row["runtime_type"] == "claude"
    assert "token" not in str(row)


def test_run_completes_through_the_bridge_with_usage(claude_env):
    service, ws_id, token, job, _, sdk = claude_env

    async def scenario(client):
        session = client.options.session_id or client.options.resume
        sdk.sessions.add(session)
        yield SystemMessage("init", {"session_id": session})
        yield AssistantMessage([TextBlock("done")], message_id="m1",
                               usage={"input_tokens": 7, "output_tokens": 3})
        yield ResultMessage(result="finished the task",
                            usage={"input_tokens": 7, "output_tokens": 3})

    sdk.scenario = scenario
    started = service.call(ws_id, token, "start_agent_run", {
        "adapter_id": ADAPTER_ID, "job_id": job["id"], "request_id": "claude-run-1"})
    assert started["runtime_type"] == "claude"
    finished = _until(lambda: (lambda row: row if row["outcome"] else None)(
        _read(service, ws_id, token, started["run_id"])))
    assert finished["outcome"] == "succeeded"
    assert finished["token_usage"] == {"input_tokens": 7, "output_tokens": 3,
                                       "total_tokens": 10}
    retry = service.call(ws_id, token, "start_agent_run", {
        "adapter_id": ADAPTER_ID, "job_id": job["id"], "request_id": "claude-run-1"})
    assert retry["run_id"] == started["run_id"] and retry["idempotent"] is True


def test_tool_approval_is_resolved_through_the_bridge(claude_env):
    service, ws_id, token, job, _, sdk = claude_env
    seen = {}

    async def scenario(client):
        session = client.options.session_id or client.options.resume
        sdk.sessions.add(session)
        yield SystemMessage("init", {"session_id": session})
        seen["result"] = await client.options.can_use_tool(
            "Bash", {"command": "pytest -q"},
            types.SimpleNamespace(tool_use_id="tu", title=None))
        yield ResultMessage(result="ok")

    sdk.scenario = scenario
    started = service.call(ws_id, token, "start_agent_run", {
        "adapter_id": ADAPTER_ID, "job_id": job["id"], "request_id": "claude-run-2"})
    viewed = _until(lambda: (lambda row: row if row.get("interactions") else None)(
        _read(service, ws_id, token, started["run_id"])))
    assert viewed["active_state"] == "waiting_interaction"
    interaction = viewed["interactions"][0]
    choice = next(item for item in interaction["details"]["choices"]
                  if item["semantic"] == "approve")
    service.call(ws_id, token, "respond_agent_interaction", {
        "run_id": started["run_id"], "interaction_id": interaction["id"],
        "response": {"choiceId": choice["id"]}})
    finished = _until(lambda: (lambda row: row if row["outcome"] else None)(
        _read(service, ws_id, token, started["run_id"])))
    assert finished["outcome"] == "succeeded"
    assert seen["result"].behavior == "allow"
