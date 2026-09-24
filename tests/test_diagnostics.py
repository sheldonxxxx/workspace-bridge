"""Canonical diagnostics, exact runnable routes, and read-only Doctor behavior."""
from __future__ import annotations

import fcntl
import json
import sqlite3

import httpx
import pytest

from workspace_bridge import cli as cli_module
from workspace_bridge.api import make_admin
from workspace_bridge.cli import initialize, main as cli_main
from workspace_bridge.diagnostics import MAX_ROUTES
from workspace_bridge.runtime import RuntimeUnavailable, RuntimeUnsupported
from workspace_bridge.security import BridgeError
from workspace_bridge.service import Service
from workspace_bridge.wbrp import CORE_FEATURES, Descriptor


class DiagnosticAdapter:
    def __init__(self, runtime: str, *, descriptor_error: Exception | None = None,
                 features: set[str] | None = None):
        self.runtime = runtime
        self.descriptor_error = descriptor_error
        self.features = dict.fromkeys(features if features is not None else CORE_FEATURES, 1)
        self.profile_rows = [{"id": "reviewed", "revision": "rev-1"}]
        self.model_rows: dict[str, list[dict]] = {}
        self.descriptor_calls = 0
        self.profile_calls = 0
        self.model_calls: list[str] = []

    def descriptor(self):
        self.descriptor_calls += 1
        if self.descriptor_error:
            raise self.descriptor_error
        return Descriptor(self.runtime, self.runtime, "1", "test", "instance",
                          dict(self.features))

    def profiles(self):
        self.profile_calls += 1
        return self.profile_rows

    def models(self, workspace_id: str):
        self.model_calls.append(workspace_id)
        return self.model_rows.get(workspace_id, [{
            "selector": "model-one", "reasoningOptions": ["low", "high"]}])


@pytest.fixture
def diagnostic_env(tmp_path):
    parent = tmp_path / "projects"
    parent.mkdir()
    state = tmp_path / "state"
    config = initialize(state, [str(parent)], 8765, 8766)
    adapters = {runtime: DiagnosticAdapter(runtime) for runtime in ("codex", "pi")}
    service = Service(state, config, adapters=adapters,
                      run_coordinator_background=False)
    yield {"service": service, "state": state, "parent": parent,
           "adapters": adapters, "tmp": tmp_path}
    service.close()


def add_workspace(env, name: str):
    root = env["parent"] / name.lower()
    root.mkdir()
    return env["service"].add_workspace(name, str(root), [])['workspace']


def make_ready(env, workspace: dict, runtime: str = "codex", *, policy: bool = True):
    service = env["service"]
    if not service.bridge_status()["configured"]:
        service.manage_bridge("rotate_token")
    if not workspace["enabled"]:
        service.manage_workspace(workspace["id"], "enable")
    if not workspace["agent_enabled"]:
        service.manage_workspace(workspace["id"], "set_agent_enabled", agent_enabled=True)
    service.set_workspace_runtime(service.workspace(workspace["id"], False), runtime,
                                  True, "reviewed")
    if policy:
        service.run_coordinator.set_model_policy(runtime, ["model-one"], "model-one",
                                                 service.workspace(workspace["id"], False))


def route(report: dict, workspace_id: str, runtime: str = "codex"):
    return next(item for item in report["runnable_routes"]
                if item["workspace_id"] == workspace_id and item["runtime"] == runtime)


def test_exact_workspace_runtime_facts_do_not_combine_across_routes(diagnostic_env):
    env = diagnostic_env
    one = add_workspace(env, "One")
    two = add_workspace(env, "Two")
    make_ready(env, one, "codex", policy=False)
    service = env["service"]
    env["adapters"]["pi"].model_rows[one["id"]] = []
    service.manage_workspace(two["id"], "enable")
    service.manage_workspace(two["id"], "set_agent_enabled", agent_enabled=True)
    service.run_coordinator.set_model_policy("pi", ["model-one"], "model-one",
                                              service.workspace(two["id"], False))
    # The `pi` policy is global to pi, but only workspace Two can discover it;
    # the `codex` grant in One has no matching model policy.
    report = env["service"].diagnostic_report()
    assert not any(item["ready"] for item in report["runnable_routes"])
    assert "model.policy" in route(report, one["id"], "codex")["blockers"]
    assert "workspace.runtime_grant" in route(report, two["id"], "codex")["blockers"]
    assert "workspace.runtime_grant" in route(report, one["id"], "pi")["blockers"]


def test_fully_configured_exact_route_is_ready_and_safe(diagnostic_env):
    env = diagnostic_env
    workspace = add_workspace(env, "Ready")
    make_ready(env, workspace)
    make_ready(env, workspace, "pi")
    report = env["service"].diagnostic_report()
    actual = route(report, workspace["id"])
    assert actual["ready"] is True
    assert actual["blockers"] == []
    assert actual["profile"] == {"id": "reviewed", "revision": "rev-1"}
    assert actual["default_model_selector"] == "model-one"
    assert report["overall"]["status"] == "unknown"  # external reachability is never guessed
    assert next(item for item in report["checks"]
                if item["code"] == "external_connection.not_observed")["status"] == "unknown"
    assert str(env["parent"]) not in json.dumps(report)


@pytest.mark.parametrize(("change", "expected"), [
    ("disabled", "workspace.enabled"),
    ("write_scope", "workspace.write_scope"),
    ("agent", "workspace.agent_enabled"),
    ("gateway_disabled", "core.gateway_enabled"),
    ("grant_disabled", "workspace.runtime_grant"),
])
def test_known_route_prerequisites_block_with_stable_codes(diagnostic_env, change, expected):
    env = diagnostic_env
    workspace = add_workspace(env, "Ready")
    make_ready(env, workspace)
    service = env["service"]
    if change == "disabled":
        service.manage_workspace(workspace["id"], "disable")
    elif change == "write_scope":
        service.manage_workspace(workspace["id"], "set_write_scope", write_scope="none")
    elif change == "agent":
        service.manage_workspace(workspace["id"], "set_agent_enabled", agent_enabled=False)
    elif change == "gateway_disabled":
        service.manage_bridge("disable")
    elif change == "grant_disabled":
        service.set_workspace_runtime(service.workspace(workspace["id"], False), "codex", False)
    actual = route(service.diagnostic_report(), workspace["id"])
    assert actual["ready"] is False
    assert expected in actual["blockers"]


def test_missing_gateway_credential_is_a_route_blocker(diagnostic_env):
    env = diagnostic_env
    workspace = add_workspace(env, "Ready")
    service = env["service"]
    service.manage_workspace(workspace["id"], "enable")
    service.manage_workspace(workspace["id"], "set_agent_enabled", agent_enabled=True)
    service.set_workspace_runtime(service.workspace(workspace["id"], False), "codex",
                                  True, "reviewed")
    service.run_coordinator.set_model_policy("codex", ["model-one"], "model-one",
                                              service.workspace(workspace["id"], False))
    actual = route(service.diagnostic_report(), workspace["id"])
    assert "core.gateway_credential_configured" in actual["blockers"]


@pytest.mark.parametrize(("runtime_error", "features", "expected"), [
    (RuntimeUnavailable("https://private.invalid/path?token=sentinel"), None, "runtime.reachable"),
    (RuntimeUnsupported("wrong protocol"), None, "runtime.protocol_compatible"),
    (None, {"models", "conversations", "runs", "activities"}, "runtime.required_features"),
])
def test_runtime_failure_protocol_and_feature_gates(diagnostic_env, runtime_error, features, expected):
    env = diagnostic_env
    workspace = add_workspace(env, "Ready")
    make_ready(env, workspace)
    adapter = env["adapters"]["codex"]
    adapter.descriptor_error = runtime_error
    if features is not None:
        adapter.features = dict.fromkeys(features, 1)
    report = env["service"].diagnostic_report()
    actual = route(report, workspace["id"])
    assert actual["ready"] is False and expected in actual["blockers"]
    assert "sentinel" not in json.dumps(report)
    assert "https://private.invalid" not in json.dumps(report)


@pytest.mark.parametrize("profile_state", ["missing", "drifted"])
def test_profile_binding_missing_or_stale_blocks_route(diagnostic_env, profile_state):
    env = diagnostic_env
    workspace = add_workspace(env, "Ready")
    make_ready(env, workspace)
    service = env["service"]
    adapter = env["adapters"]["codex"]
    if profile_state == "missing":
        with service.lock, service.db:
            service.db.execute("UPDATE runtime_profiles SET profile='gone' WHERE workspace=?",
                               (workspace["id"],))
    else:
        adapter.profile_rows = [{"id": "reviewed", "revision": "rev-2"}]
    actual = route(service.diagnostic_report(), workspace["id"])
    assert "profile.freshness" in actual["blockers"]
    assert actual["ready"] is False


@pytest.mark.parametrize("model_state", ["missing_policy", "unavailable_default", "stale_reasoning"])
def test_model_policy_default_and_reasoning_freshness(diagnostic_env, model_state):
    env = diagnostic_env
    workspace = add_workspace(env, "Ready")
    make_ready(env, workspace, policy=model_state != "missing_policy")
    service = env["service"]
    if model_state == "unavailable_default":
        env["adapters"]["codex"].model_rows[workspace["id"]] = []
    elif model_state == "stale_reasoning":
        service.set_setting("runtime_model_policy:codex", json.dumps({
            "enabled": ["model-one"], "default": "model-one",
            "reasoning_defaults": {"model-one": "max"}}))
    actual = route(service.diagnostic_report(), workspace["id"])
    expected = {"missing_policy": "model.policy",
                "unavailable_default": "model.default_freshness",
                "stale_reasoning": "model.reasoning_default"}[model_state]
    assert expected in actual["blockers"]
    assert actual["ready"] is False


def test_offline_never_calls_adapters_and_never_claims_remote_route_ready(diagnostic_env):
    env = diagnostic_env
    workspace = add_workspace(env, "Ready")
    make_ready(env, workspace)
    for adapter in env["adapters"].values():
        adapter.descriptor_calls = adapter.profile_calls = 0
        adapter.model_calls.clear()
    report = env["service"].diagnostic_report(offline=True)
    assert report["mode"] == "offline"
    assert route(report, workspace["id"])["ready"] is False
    assert "profile.freshness" in route(report, workspace["id"])["blockers"]
    assert "model.default_freshness" in route(report, workspace["id"])["blockers"]
    assert all(not adapter.descriptor_calls and not adapter.profile_calls
               and not adapter.model_calls for adapter in env["adapters"].values())


def test_runtime_descriptor_and_profiles_are_probed_once_per_report(diagnostic_env):
    env = diagnostic_env
    one = add_workspace(env, "One")
    two = add_workspace(env, "Two")
    make_ready(env, one, "codex")
    make_ready(env, two, "codex")
    adapter = env["adapters"]["codex"]
    adapter.descriptor_calls = adapter.profile_calls = 0
    adapter.model_calls.clear()
    env["service"].diagnostic_report()
    assert adapter.descriptor_calls == 1
    assert adapter.profile_calls == 1
    assert sorted(adapter.model_calls) == sorted([one["id"], two["id"]])


def test_git_evidence_failure_is_not_a_route_blocker(diagnostic_env, monkeypatch):
    env = diagnostic_env
    workspace = add_workspace(env, "Ready")
    make_ready(env, workspace)

    def fail(*_args, **_kwargs):
        raise BridgeError("unsupported private repository path", "unsupported_repository_layout")

    monkeypatch.setattr("workspace_bridge.diagnostics.GitEvidence.status", fail)
    report = env["service"].diagnostic_report()
    assert route(report, workspace["id"])["ready"] is True
    git = next(item for item in report["checks"] if item["code"] == "git.evidence")
    assert git["status"] == "warning"
    assert "git.evidence" not in route(report, workspace["id"])["blockers"]
    assert "private repository path" not in json.dumps(report)


@pytest.mark.asyncio
async def test_admin_diagnostics_requires_auth_and_returns_canonical_report(diagnostic_env):
    env = diagnostic_env
    service = env["service"]
    app = make_admin(service, service.config["admin_token_hash"])
    token = (env["state"] / "admin-token").read_text().strip()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://127.0.0.1:8766") as client:
        assert (await client.get("/api/diagnostics?offline=1")).status_code == 401
        response = await client.get("/api/diagnostics?offline=1",
                                    headers={"Authorization": "Bearer " + token})
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"generated_at", "mode", "overall", "checks", "runnable_routes"}
    assert body["mode"] == "offline"
    assert body["overall"]["status"] in {"unknown", "action_required"}


def test_doctor_human_and_json_use_report_and_exit_codes(diagnostic_env, capsys, monkeypatch):
    env = diagnostic_env
    workspace = add_workspace(env, "Ready")
    make_ready(env, workspace)
    make_ready(env, workspace, "pi")
    monkeypatch.setattr("workspace_bridge.cli.adapters_from_environment",
                        lambda: env["adapters"])
    expected = env["service"].diagnostic_report(offline=True)
    lock_path = env["state"] / "process.lock"
    fd = lock_path.open("w+")
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        cli_main(["--state", str(env["state"]), "doctor", "--offline", "--json"])
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        fd.close()
    report = json.loads(capsys.readouterr().out)
    assert report["mode"] == "offline"
    assert report["overall"]["status"] == "unknown"
    assert report["checks"] == expected["checks"]
    assert report["runnable_routes"] == expected["runnable_routes"]
    assert not any(item["ready"] for item in report["runnable_routes"])

    assert cli_main(["--state", str(env["state"]), "doctor", "--offline"]) is None
    human = capsys.readouterr().out
    for heading in ("Overall:", "Core:", "Workspaces:", "Runtimes:",
                    "Runnable routes:", "Git evidence:", "External connection:"):
        assert heading in human


def test_doctor_missing_default_state_is_safe_and_actionable(capsys, monkeypatch, tmp_path):
    selected = tmp_path / "selected native state"
    raw_error = f"private exception detail at {selected}: token=secret-value"

    def missing_config(_state):
        raise FileNotFoundError(raw_error)

    monkeypatch.setattr(cli_module, "DEFAULT_STATE", selected)
    monkeypatch.setattr(cli_module, "load_config", missing_config)

    with pytest.raises(SystemExit) as json_exit:
        cli_main(["doctor", "--offline", "--json"])
    assert json_exit.value.code == 1
    json_output = capsys.readouterr().out
    report = json.loads(json_output)
    check = report["checks"][0]
    assert check["code"] == "core.config_state_readable"
    assert check["status"] == "failed"
    assert check["summary"] == "No Workspace Bridge state was found at the selected state location."
    assert check["remediation"]
    assert "workspace-bridge init --allow-parent <projects-dir>" in check["remediation"]
    assert "docker exec workspace-bridge workspace-bridge --state /state doctor" in check["remediation"]
    assert len(check["remediation"]) <= 240
    assert "secret-value" not in json_output
    assert raw_error not in json_output
    assert str(selected.resolve()) not in json_output

    with pytest.raises(SystemExit) as human_exit:
        cli_main(["doctor", "--offline"])
    assert human_exit.value.code == 1
    human_output = capsys.readouterr().out
    assert human_output.startswith(f"Overall: FAILED — {report['overall']['summary']}")
    assert check["summary"] in human_output
    for instruction in check["remediation"].splitlines():
        assert instruction in human_output
    assert "    fix:\n      - Native setup:" in human_output
    assert "secret-value" not in human_output
    assert raw_error not in human_output
    assert str(selected.resolve()) not in human_output


def test_doctor_existing_malformed_config_gets_generic_guidance(capsys, tmp_path):
    selected = tmp_path / "state"
    selected.mkdir(mode=0o700)
    malformed = selected / "config.json"
    malformed.write_text('{ "private-value": "secret-value", broken json }')
    malformed.chmod(0o600)

    with pytest.raises(SystemExit) as exit_status:
        cli_main(["--state", str(selected), "doctor", "--offline", "--json"])
    assert exit_status.value.code == 1
    output = capsys.readouterr().out
    report = json.loads(output)
    check = report["checks"][0]
    assert check["code"] == "core.config_state_readable"
    assert check["summary"] == "Local configuration could not be read."
    assert "No Workspace Bridge state was found" not in output
    assert "permissions" in check["remediation"]
    assert "validity" in check["remediation"]
    assert "secret-value" not in output
    assert str(selected.resolve()) not in output


def test_doctor_read_only_does_not_requeue_or_start_workers(diagnostic_env, capsys):
    env = diagnostic_env
    service = env["service"]
    workspace = add_workspace(env, "Ready")
    with service.lock, service.db:
        event_id = "notice-for-doctor-test"
        service.db.execute(
            "INSERT INTO notification_events(id,dedupe_key,run_id,workspace_id,workspace_name,"
            "handoff_title,runtime,event_type,occurred_at,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (event_id, "doctor-test", "run-test", workspace["id"], "Ready", "handoff",
             "codex", "run_failed", "2026-09-24T00:00:00+00:00", "2026-09-24T00:00:00+00:00"))
        service.db.execute(
            "INSERT INTO notification_deliveries(event_id,channel_id,status,updated) "
            "VALUES(?,?,?,?)", (event_id, "test", "sending", "2026-09-24T00:00:00+00:00"))
    before = service.db.execute(
        "SELECT status,attempts,code,detail,updated FROM notification_deliveries WHERE event_id=?",
        (event_id,)).fetchone()
    tables_before = {table: service.db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                     for table in ("jobs", "runtime_runs", "runtime_interactions",
                                   "runtime_activities", "notification_events",
                                   "notification_deliveries", "settings", "gateway",
                                   "workspaces", "events")}
    with pytest.raises(SystemExit) as exit_status:
        cli_main(["--state", str(env["state"]), "doctor", "--offline", "--json"])
    assert exit_status.value.code == 1
    capsys.readouterr()
    after = service.db.execute(
        "SELECT status,attempts,code,detail,updated FROM notification_deliveries WHERE event_id=?",
        (event_id,)).fetchone()
    assert tuple(after) == tuple(before)
    tables_after = {table: service.db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                    for table in tables_before}
    assert tables_after == tables_before

    diagnostic = Service(env["state"], service.config, adapters=env["adapters"],
                         read_only=True, run_coordinator_background=False)
    try:
        assert diagnostic.read_only
        assert diagnostic.run_coordinator._thread is None
        assert diagnostic.notification_manager.status()["configured"] is False
        assert getattr(diagnostic.notification_manager, "_thread", None) is None
        with pytest.raises(sqlite3.OperationalError):
            with diagnostic.db:
                diagnostic.db.execute("UPDATE gateway SET enabled=0 WHERE id=1")
    finally:
        diagnostic.close()


def test_container_entrypoint_preserves_doctor_flags(tmp_path, monkeypatch):
    import workspace_bridge.docker_entrypoint as entry

    parent = tmp_path / "projects"
    parent.mkdir()
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    initialize(state, [str(parent)], 8765, 8766)
    monkeypatch.setattr(entry.os, "getuid", lambda: 1001)
    monkeypatch.setenv("WB_PROJECTS_DIR", str(parent))
    monkeypatch.setenv("WB_STATE_DIR", str(state))
    invoked = []
    monkeypatch.setattr(entry, "cli_main", lambda args: invoked.append(args))
    entry.main(["doctor", "--json", "--offline"])
    assert invoked == [["--state", str(state), "doctor", "--container",
                        "--mcp-public-port", "8765", "--admin-public-port", "8766",
                        "--json", "--offline"]]


def test_diagnostics_route_and_check_output_are_bounded(diagnostic_env):
    env = diagnostic_env
    report = env["service"].diagnostic_report(offline=True)
    assert len(report["runnable_routes"]) <= MAX_ROUTES
    assert len(report["checks"]) <= 5000
