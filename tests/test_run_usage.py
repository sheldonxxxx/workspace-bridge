"""Per-run native token usage accounting across WBRP, adapters and Bridge."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from workspace_bridge.api import Handoff, make_admin
from workspace_bridge.cli import initialize
from workspace_bridge.codex_host_adapter import (
    CodexHostAdapter,
    _normalize_codex_usage,
)
from workspace_bridge.run_coordinator import _normalize_bridge_usage, _stored_bridge_usage
from workspace_bridge.runtime import RuntimeUnavailable
from workspace_bridge.security import BridgeError
from workspace_bridge.service import Service
from workspace_bridge.wbrp import validate_run_state

from test_codex_host_adapter import FakeCodexRpc
from test_run_coordinator import DirectAdapter, modern_env, ADAPTER_ID  # noqa: F401
from conftest import attach_test_node, create_test_adapter


# ------------------------------------------------ WBRP trust boundary

def test_wbrp_accepts_valid_run_usage():
    snapshot = validate_run_state({
        "phase": "active", "activeState": "running", "outcome": None,
        "usage": {"inputTokens": 10, "cachedInputTokens": 2,
                  "cacheWriteInputTokens": 1, "outputTokens": 5,
                  "reasoningOutputTokens": 3, "totalTokens": 15},
    })
    assert snapshot["usage"] == {"inputTokens": 10, "cachedInputTokens": 2,
                                 "cacheWriteInputTokens": 1, "outputTokens": 5,
                                 "reasoningOutputTokens": 3, "totalTokens": 15}


def test_wbrp_accepts_partial_run_usage():
    snapshot = validate_run_state({
        "phase": "terminal", "activeState": None, "outcome": "succeeded",
        "usage": {"inputTokens": 7},
    })
    assert snapshot["usage"] == {"inputTokens": 7}


def test_wbrp_run_without_usage_stays_absent():
    snapshot = validate_run_state({
        "phase": "active", "activeState": "running", "outcome": None,
    })
    assert "usage" not in snapshot


@pytest.mark.parametrize("usage", [
    {},
    None,
    [],
    "tokens",
    {"inputTokens": -1},
    {"inputTokens": 1.5},
    {"inputTokens": True},
    {"inputTokens": 2 ** 53},
    {"unknownCounter": 1},
    {"inputTokens": 1, "bogus": 2},
    {"inputTokens": 10, "outputTokens": -1},
    {"inputTokens": 10, "outputTokens": 1.5},
    {"inputTokens": 10, "outputTokens": True},
    {"inputTokens": 10, "outputTokens": 2 ** 53},
])
def test_wbrp_rejects_malformed_usage(usage):
    with pytest.raises(RuntimeUnavailable):
        validate_run_state({
            "phase": "active", "activeState": "running", "outcome": None,
            "usage": usage,
        })


# ------------------------------------------------ Codex adapter ownership

def _codex_conversation_run(tmp_path):
    projects = tmp_path / "projects"
    projects.mkdir(exist_ok=True)
    workspace = projects / "workspace"
    workspace.mkdir(exist_ok=True)
    rpc = FakeCodexRpc()
    adapter = CodexHostAdapter(tmp_path / "adapter-state", projects, rpc=rpc)
    profile = next(row for row in adapter.profiles(
        "ws_test", str(workspace))["profiles"]
        if row["id"] == "workspace-write-reviewed")
    conversation = adapter.create_conversation({
        "workspaceId": "ws_test", "directory": str(workspace),
        "securityProfile": {"id": "workspace-write-reviewed",
                            "revision": profile["revision"]},
    })
    run = adapter.start_run(conversation["id"], {
        "input": [{"type": "text", "text": "Check the project"}]})
    return adapter, rpc, conversation, run


def test_codex_usage_uses_last_not_thread_total(tmp_path):
    adapter, _, conversation, run = _codex_conversation_run(tmp_path)
    try:
        adapter._notification("thread/tokenUsage/updated", {
            "threadId": conversation["nativeId"], "turnId": run["nativeId"],
            "tokenUsage": {
                "last": {"inputTokens": 100, "outputTokens": 20, "totalTokens": 120},
                "total": {"inputTokens": 9999, "outputTokens": 9999,
                          "totalTokens": 19998},
            }})
        public = adapter.run(run["id"])
        assert public["usage"] == {"inputTokens": 100, "outputTokens": 20,
                                   "totalTokens": 120}
    finally:
        adapter.close()


def test_codex_duplicate_usage_replaces_instead_of_summing(tmp_path):
    adapter, _, conversation, run = _codex_conversation_run(tmp_path)
    try:
        for payload in ({"inputTokens": 10, "totalTokens": 10},
                        {"inputTokens": 30, "totalTokens": 30}):
            adapter._notification("thread/tokenUsage/updated", {
                "threadId": conversation["nativeId"], "turnId": run["nativeId"],
                "tokenUsage": {"last": payload}})
        assert adapter.run(run["id"])["usage"] == {"inputTokens": 30,
                                                   "totalTokens": 30}
    finally:
        adapter.close()


def test_codex_wrong_thread_or_turn_does_not_alter_run(tmp_path):
    adapter, _, conversation, run = _codex_conversation_run(tmp_path)
    try:
        adapter._notification("thread/tokenUsage/updated", {
            "threadId": conversation["nativeId"], "turnId": run["nativeId"],
            "tokenUsage": {"last": {"inputTokens": 11, "totalTokens": 11}}})
        before = adapter.run(run["id"])["usage"]
        adapter._notification("thread/tokenUsage/updated", {
            "threadId": "other-thread", "turnId": run["nativeId"],
            "tokenUsage": {"last": {"inputTokens": 999}}})
        adapter._notification("thread/tokenUsage/updated", {
            "threadId": conversation["nativeId"], "turnId": "other-turn",
            "tokenUsage": {"last": {"inputTokens": 999}}})
        adapter._notification("thread/tokenUsage/updated", {
            "threadId": conversation["nativeId"], "turnId": run["nativeId"],
            "tokenUsage": {"total": {"inputTokens": 999}}})
        assert adapter.run(run["id"])["usage"] == before
    finally:
        adapter.close()


def test_codex_usage_survives_terminal_failure_and_restart(tmp_path):
    projects = tmp_path / "projects"
    projects.mkdir(exist_ok=True)
    workspace = projects / "workspace"
    workspace.mkdir(exist_ok=True)
    rpc = FakeCodexRpc()
    adapter = CodexHostAdapter(tmp_path / "adapter-state", projects, rpc=rpc)
    profile = next(row for row in adapter.profiles(
        "ws_test", str(workspace))["profiles"]
        if row["id"] == "workspace-write-reviewed")
    conversation = adapter.create_conversation({
        "workspaceId": "ws_test", "directory": str(workspace),
        "securityProfile": {"id": "workspace-write-reviewed",
                            "revision": profile["revision"]},
    })
    run = adapter.start_run(conversation["id"], {
        "input": [{"type": "text", "text": "Failing task"}]})
    adapter._notification("thread/tokenUsage/updated", {
        "threadId": conversation["nativeId"], "turnId": run["nativeId"],
        "tokenUsage": {"last": {"inputTokens": 42, "outputTokens": 7,
                                "totalTokens": 49}}})
    adapter._notification("turn/completed", {
        "threadId": conversation["nativeId"],
        "turn": {"id": run["nativeId"], "status": "failed",
                 "error": {"message": "boom"}}})
    terminal = adapter.run(run["id"])
    assert terminal["outcome"] == "failed"
    assert terminal["usage"] == {"inputTokens": 42, "outputTokens": 7,
                                 "totalTokens": 49}
    state = adapter.state
    root = workspace.parent
    adapter.close()
    restarted = CodexHostAdapter(state, root, rpc=FakeCodexRpc())
    try:
        assert restarted.run(run["id"])["usage"] == {
            "inputTokens": 42, "outputTokens": 7, "totalTokens": 49}
    finally:
        restarted.close()


def test_codex_absent_usage_is_omitted_not_zero(tmp_path):
    adapter, _, _, run = _codex_conversation_run(tmp_path)
    try:
        assert "usage" not in adapter.run(run["id"])
    finally:
        adapter.close()


def test_codex_normalize_rejects_mixed_valid_and_malformed():
    assert _normalize_codex_usage({"inputTokens": 10}) == {"inputTokens": 10}
    assert _normalize_codex_usage({}) is None
    # Unknown provider fields are ignored for forward compatibility.
    assert _normalize_codex_usage({"inputTokens": 10, "cost": 5}) == {
        "inputTokens": 10}
    # A present-but-malformed recognized counter invalidates the whole
    # snapshot instead of keeping a misleading partial.
    assert _normalize_codex_usage({"inputTokens": 10, "outputTokens": -1}) is None
    assert _normalize_codex_usage({"inputTokens": 10, "output": 1.5}) is None
    assert _normalize_codex_usage({"inputTokens": 10, "output": True}) is None
    assert _normalize_codex_usage({"inputTokens": 10, "output": 2 ** 53}) is None
    assert _normalize_codex_usage({"input": -1}) is None
    # Snake-case and short aliases map to the same canonical counters.
    assert _normalize_codex_usage({"input_tokens": 4, "output": 2}) == {
        "inputTokens": 4, "outputTokens": 2}


def test_codex_malformed_usage_preserves_prior_snapshot(tmp_path):
    adapter, _, conversation, run = _codex_conversation_run(tmp_path)
    try:
        adapter._notification("thread/tokenUsage/updated", {
            "threadId": conversation["nativeId"], "turnId": run["nativeId"],
            "tokenUsage": {"last": {"inputTokens": 11, "totalTokens": 11}}})
        before = adapter.run(run["id"])["usage"]
        # Mixed valid + malformed must be dropped entirely, not partially kept.
        adapter._notification("thread/tokenUsage/updated", {
            "threadId": conversation["nativeId"], "turnId": run["nativeId"],
            "tokenUsage": {"last": {"inputTokens": 99, "outputTokens": -1}}})
        adapter._notification("thread/tokenUsage/updated", {
            "threadId": conversation["nativeId"], "turnId": run["nativeId"],
            "tokenUsage": {"last": {"inputTokens": True}}})
        assert adapter.run(run["id"])["usage"] == before
    finally:
        adapter.close()


# ------------------------------------------------ Bridge persistence

def test_bridge_normalize_usage_maps_camel_to_snake():
    assert _normalize_bridge_usage({
        "inputTokens": 10, "cachedInputTokens": 2,
        "cacheWriteInputTokens": 1, "outputTokens": 5,
        "reasoningOutputTokens": 3, "totalTokens": 15}) == {
        "input_tokens": 10, "cached_input_tokens": 2,
        "cache_write_input_tokens": 1, "output_tokens": 5,
        "reasoning_output_tokens": 3, "total_tokens": 15}
    assert _normalize_bridge_usage({"inputTokens": 0}) == {"input_tokens": 0}
    assert _normalize_bridge_usage({}) is None
    assert _normalize_bridge_usage(None) is None
    assert _normalize_bridge_usage({"inputTokens": -1}) is None
    assert _normalize_bridge_usage({"inputTokens": 10, "outputTokens": -1}) is None
    assert _normalize_bridge_usage({"inputTokens": 10, "outputTokens": True}) is None
    assert _normalize_bridge_usage({"bogus": 1}) is None
    assert _stored_bridge_usage('{"input_tokens": 3}') == {"input_tokens": 3}
    assert _stored_bridge_usage(None) is None
    assert _stored_bridge_usage('{"input_tokens": -1}') is None


def test_bridge_persists_and_returns_usage_through_list_and_read(modern_env):
    service, ws_id, token, job, native, _ = modern_env
    ws = service.workspace(ws_id)
    started = service.call(ws_id, token, "start_agent_run", {
        "adapter_id": ADAPTER_ID, "job_id": job["id"],
        "request_id": "usage-persist-1"})
    run = service.run_coordinator._run_row(ws, started["run_id"])
    conversation = service.run_coordinator._conversation(run)
    native_thread = native.conversation(conversation["native_id"])["nativeId"]
    native_turn = native.run(run["native_id"])["nativeId"]
    native._notification("thread/tokenUsage/updated", {
        "threadId": native_thread, "turnId": native_turn,
        "tokenUsage": {"last": {"inputTokens": 120, "cachedInputTokens": 20,
                                "outputTokens": 30, "totalTokens": 150}}})
    # Reconcile through the durable snapshot path.
    read = service.call(ws_id, token, "read_agent_run",
                        {"run_id": started["run_id"]})
    assert read["token_usage"] == {"input_tokens": 120,
                                   "cached_input_tokens": 20,
                                   "output_tokens": 30, "total_tokens": 150}
    listed = service.call(ws_id, token, "list_agent_runs", {})["runs"]
    assert next(item for item in listed
                if item["run_id"] == started["run_id"])["token_usage"] == {
        "input_tokens": 120, "cached_input_tokens": 20,
        "output_tokens": 30, "total_tokens": 150}
    with service.lock:
        stored = service.db.execute(
            "SELECT token_usage FROM agent_runs WHERE id=?",
            (started["run_id"],)).fetchone()["token_usage"]
    assert json.loads(stored) == {"cached_input_tokens": 20, "input_tokens": 120,
                                  "output_tokens": 30, "total_tokens": 150}


def test_bridge_absent_usage_is_null_and_partial_survives_terminal(modern_env):
    service, ws_id, token, job, native, _ = modern_env
    ws = service.workspace(ws_id)
    started = service.call(ws_id, token, "start_agent_run", {
        "adapter_id": ADAPTER_ID, "job_id": job["id"],
        "request_id": "usage-absent-1"})
    assert service.call(ws_id, token, "read_agent_run",
                        {"run_id": started["run_id"]})["token_usage"] is None
    run = service.run_coordinator._run_row(ws, started["run_id"])
    snapshot = service.run_coordinator.adapter(ADAPTER_ID).run(run["native_id"])
    terminal = {**snapshot, "phase": "terminal", "activeState": None,
                "outcome": "failed", "result": "",
                "usage": {"inputTokens": 9, "totalTokens": 9}}
    service.run_coordinator._persist_snapshot(run, terminal, [], [])
    failed = service.call(ws_id, token, "read_agent_run",
                          {"run_id": started["run_id"]})
    assert failed["outcome"] == "failed"
    assert failed["token_usage"] == {"input_tokens": 9, "total_tokens": 9}


def test_bridge_rejects_malformed_native_usage(modern_env):
    service, ws_id, token, job, _, _ = modern_env
    ws = service.workspace(ws_id)
    started = service.call(ws_id, token, "start_agent_run", {
        "adapter_id": ADAPTER_ID, "job_id": job["id"],
        "request_id": "usage-malformed-1"})
    run = service.run_coordinator._run_row(ws, started["run_id"])
    snapshot = service.run_coordinator.adapter(ADAPTER_ID).run(run["native_id"])
    bad = {**snapshot, "usage": {"inputTokens": -5}}
    with pytest.raises(RuntimeUnavailable):
        service.run_coordinator._persist_snapshot(run, bad, [], [])


def test_bridge_schema_v4_fail_fast(tmp_path, payload):
    parent = tmp_path / "projects"
    parent.mkdir()
    root = parent / "alpha"
    root.mkdir()
    state = tmp_path / "bridge-state"
    cfg = initialize(state, 8765, 8766)
    service = Service(state, cfg, run_coordinator_background=False)
    try:
        assert service.db.execute(
            "SELECT value FROM bridge_meta WHERE key='schema_version'").fetchone()[0] == "4"
        columns = {row["name"] for row in service.db.execute(
            "PRAGMA table_info(agent_runs)")}
        assert "token_usage" in columns
    finally:
        service.close()
    # A v3 database fails closed without migration.
    import sqlite3
    legacy = tmp_path / "legacy-state"
    legacy.mkdir()
    conn = sqlite3.connect(legacy / "bridge.sqlite3")
    conn.execute("CREATE TABLE bridge_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.execute("INSERT INTO bridge_meta VALUES('schema_version','3')")
    conn.commit()
    conn.close()
    with pytest.raises(BridgeError) as exc:
        Service(legacy, cfg, run_coordinator_background=False)
    assert exc.value.code == "state_schema_incompatible"
    # A v4 database that is structurally incomplete (agent_runs without
    # token_usage) fails closed instead of being silently migrated.
    import sqlite3
    incomplete = tmp_path / "incomplete-state"
    incomplete.mkdir()
    conn = sqlite3.connect(incomplete / "bridge.sqlite3")
    conn.execute("CREATE TABLE bridge_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.execute("INSERT INTO bridge_meta VALUES('schema_version','4')")
    conn.execute("CREATE TABLE nodes (id TEXT PRIMARY KEY, name TEXT NOT NULL, base_url TEXT NOT NULL, token TEXT NOT NULL, enabled INTEGER NOT NULL, revision TEXT NOT NULL, created TEXT NOT NULL, updated TEXT NOT NULL)")
    conn.execute("CREATE TABLE node_adapters (adapter_id TEXT PRIMARY KEY, node_id TEXT NOT NULL, name TEXT NOT NULL, runtime_type TEXT NOT NULL, base_url TEXT NOT NULL DEFAULT '', revision TEXT NOT NULL, enabled INTEGER NOT NULL, has_token INTEGER NOT NULL, last_seen TEXT NOT NULL)")
    conn.execute("CREATE TABLE workspace_routes (workspace TEXT NOT NULL, adapter_id TEXT NOT NULL, enabled INTEGER NOT NULL, is_default INTEGER NOT NULL DEFAULT 0, security_source TEXT, profile_id TEXT, profile_revision TEXT, updated TEXT NOT NULL, PRIMARY KEY(workspace, adapter_id))")
    conn.execute("CREATE TABLE adapter_model_policies (adapter_id TEXT PRIMARY KEY, enabled_models_json TEXT NOT NULL, default_model TEXT NOT NULL, reasoning_defaults_json TEXT NOT NULL, updated TEXT NOT NULL)")
    conn.execute("CREATE TABLE agent_conversations (id TEXT PRIMARY KEY, workspace TEXT NOT NULL, node_id TEXT NOT NULL, node_revision TEXT NOT NULL, adapter_id TEXT NOT NULL, runtime_type TEXT NOT NULL, adapter_revision TEXT NOT NULL, native_id TEXT NOT NULL, profile TEXT NOT NULL, revision TEXT NOT NULL, instance_id TEXT NOT NULL, created TEXT NOT NULL, source TEXT NOT NULL DEFAULT 'profile', security_snapshot TEXT, permission_revision TEXT NOT NULL DEFAULT '', approval_revision TEXT NOT NULL DEFAULT '', replacement_reason TEXT, UNIQUE(adapter_id,native_id))")
    # agent_runs without the v4 token_usage column.
    conn.execute("CREATE TABLE agent_runs (id TEXT PRIMARY KEY, conversation TEXT NOT NULL, workspace TEXT NOT NULL, node_id TEXT NOT NULL, node_revision TEXT NOT NULL, adapter_id TEXT NOT NULL, runtime_type TEXT NOT NULL, adapter_revision TEXT NOT NULL, handoff TEXT NOT NULL, request_id TEXT NOT NULL, request_hash TEXT NOT NULL, continue_from TEXT, parent_run TEXT, native_id TEXT, model TEXT NOT NULL, reasoning TEXT, phase TEXT NOT NULL, active_state TEXT, outcome TEXT, result TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '', effective_security TEXT NOT NULL, created TEXT NOT NULL, updated TEXT NOT NULL, UNIQUE(workspace,adapter_id,request_id))")
    conn.commit()
    conn.close()
    with pytest.raises(BridgeError) as incomplete_exc:
        Service(incomplete, cfg, run_coordinator_background=False)
    assert incomplete_exc.value.code == "state_schema_incompatible"


def test_bridge_admin_and_mcp_share_canonical_run_shape(modern_env):
    service, ws_id, token, job, _, _ = modern_env
    started = service.call(ws_id, token, "start_agent_run", {
        "adapter_id": ADAPTER_ID, "job_id": job["id"],
        "request_id": "usage-canonical-1"})
    admin = service.admin_read_agent_run(started["run_id"])
    workspace_view = service.read_agent_run(service.workspace(ws_id),
                                            started["run_id"])
    assert admin["token_usage"] == workspace_view["token_usage"] is None
    assert admin["run_id"] == workspace_view["run_id"] == started["run_id"]
