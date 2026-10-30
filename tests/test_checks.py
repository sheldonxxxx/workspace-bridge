"""checks.toml on the Node, run_checks, and after-run checks on finished notices."""
import json
import sys
import time

import httpx
import pytest

from admin_helpers import admin_cookie
from workspace_bridge import checks as module
from mcp_helpers import McpTransport
from workspace_bridge.api import TOOLS, make_admin, make_mcp
from workspace_bridge.event_broker import EventBroker
from workspace_bridge.notifications import DiscordChannel, NotificationEvent
from workspace_bridge.security import BridgeError
from test_run_coordinator import ADAPTER_ID, _finish_test_run, modern_env  # noqa: F401


def py(code: str) -> str:
    """A TOML command array running one Python snippet."""
    return json.dumps([sys.executable, "-c", code])


CONFIG = f"""
[checks]
ok = {{ command = {py("print('all good')")} }}
bad = {{ command = {py("import sys; print('boom'); sys.exit(3)")} }}
"""


def call(e, name, **args):
    return e["service"].call(e["id"], e["token"], name, args)


def configure(service, ws_id, text=CONFIG, after_run=None):
    ws = service.workspace(ws_id, False)
    service.run_coordinator.run_checks.write_config(ws, text)
    if after_run is not None:
        service.run_coordinator.run_checks.set_after_run(ws, after_run)


@pytest.fixture
def ws(env):
    env["service"].manage_workspace(env["id"], "set_write_scope", write_scope="workspace")
    return env


def finished(e, result):
    deadline = time.monotonic() + 20
    while result["state"] == "running" and time.monotonic() < deadline:
        result = call(e, "run_checks", check_id=result["check_id"], wait_seconds=5)
    return result


@pytest.mark.parametrize("text, message", [
    ("[checks]\n'Bad Name' = 'true'\n", "Invalid check name"),
    ("[checks]\nx = ''\n", "empty"),
    ("[checks]\nx = 'echo \"open'\n", "unbalanced"),
    ("[checks]\nx = { command = 'true', cwd = '../up' }\n", "plain relative folder"),
    ("[checks]\nx = { command = 'true', cwd = '.git' }\n", "excluded"),
    ("[checks]\nx = { command = 'true', timeout_seconds = 0 }\n", "timeout_seconds"),
    ("[checks]\nx = { command = 'true', shell = true }\n", "optionally cwd"),
    ("[other]\nx = 1\n", "only a [checks] table"),
    ("[checks\n", "not valid TOML"),
])
def test_config_validation(text, message):
    with pytest.raises(BridgeError) as caught:
        module.parse_config(text)
    assert message in str(caught.value) and caught.value.code == "invalid_checks"


def test_config_forms():
    parsed = module.parse_config("[checks]\ntest = 'uv run pytest -q'\n"
                                 "lint = { command = ['npm', 'run', 'lint'], cwd = 'web', timeout_seconds = 90 }\n")
    assert parsed["test"] == {"command": "uv run pytest -q", "argv": ["uv", "run", "pytest", "-q"],
                              "cwd": "", "timeout_seconds": module.DEFAULT_TIMEOUT}
    assert parsed["lint"]["argv"] == ["npm", "run", "lint"] and parsed["lint"]["cwd"] == "web"


def test_run_checks_lists_runs_and_records_results(ws):
    service = ws["service"]
    listed = call(ws, "run_checks")
    assert listed["configured"] == [] and "user edits checks.toml" in listed["note"]
    with pytest.raises(BridgeError) as caught:
        call(ws, "run_checks", names=["ok"])
    assert caught.value.code == "checks_unconfigured"
    configure(service, ws["id"])
    assert [item["name"] for item in call(ws, "run_checks")["configured"]] == ["ok", "bad"]
    result = finished(ws, call(ws, "run_checks", names=["ok", "bad"], wait_seconds=20))
    assert result["state"] == "complete" and result["passed"] == 1 and result["failed"] == 1
    ok, bad = result["results"]
    assert ok["status"] == "passed" and ok["exit_code"] == 0 and "all good" in ok["log_tail"]
    assert bad["status"] == "failed" and bad["exit_code"] == 3 and "boom" in bad["log_tail"]
    assert isinstance(ok["duration_ms"], int) and "untrusted" in result["trust"]
    # The result stays readable by its check_id.
    again = call(ws, "run_checks", check_id=result["check_id"], wait_seconds=0)
    assert again["results"] == result["results"]
    with pytest.raises(BridgeError) as caught:
        call(ws, "run_checks", names=["nope"])
    assert caught.value.code == "unknown_check" and "ok, bad" in str(caught.value)
    # Nothing was written into the workspace.
    assert sorted(p.name for p in ws["root"].iterdir()) == ["README.md", "src"]


def test_checks_need_edit_scope_and_run_one_batch_at_a_time(ws):
    service = ws["service"]
    configure(service, ws["id"], f"[checks]\nslow = {{ command = {py('import time; time.sleep(2)')} }}\n")
    started = call(ws, "run_checks", names=["slow"], wait_seconds=0)
    assert started["state"] == "running" and "check_id" in started["note"]
    with pytest.raises(BridgeError) as caught:
        call(ws, "run_checks", names=["slow"], wait_seconds=0)
    assert caught.value.code == "checks_busy" and started["check_id"] in str(caught.value)
    assert finished(ws, started)["results"][0]["status"] == "passed"
    service.manage_workspace(ws["id"], "set_write_scope", write_scope="none")
    with pytest.raises(BridgeError) as caught:
        call(ws, "run_checks", names=["slow"])
    assert caught.value.code == "policy_denied"
    assert call(ws, "run_checks")["configured"][0]["name"] == "slow"


def test_timeout_kills_the_command_and_output_is_capped_and_redacted(ws, monkeypatch):
    monkeypatch.setattr(module, "KEPT_OUTPUT", 4096)
    service = ws["service"]
    marker = ws["tmp"] / "survived"
    configure(service, ws["id"], (
        "[checks]\n"
        f"hang = {{ command = {py(f'import time; time.sleep(30); open({str(marker)!r}, \"w\")')}, timeout_seconds = 1 }}\n"
        f"noisy = {{ command = {py('print(chr(10).join(str(i) * 50 for i in range(2000))); print(\"token sk-proj-\" + \"a\" * 30)')} }}\n"
        "missing = 'definitely-not-a-command-wb --flag'\n"))
    result = finished(ws, call(ws, "run_checks", names=["hang", "noisy", "missing"], wait_seconds=20))
    hang, noisy, missing = result["results"]
    assert hang["status"] == "timed_out" and hang["exit_code"] is None
    time.sleep(0.2)
    assert not marker.exists()
    assert noisy["status"] == "passed" and noisy["truncated"] is True
    assert noisy["output_bytes"] > 4096 and len(noisy["log_tail"]) <= 4096
    assert "sk-proj-" not in noisy["log_tail"] and "[REDACTED_SECRET]" in noisy["log_tail"]
    assert missing["status"] == "error" and "command not found" in missing["log_tail"]
    assert result["failed"] == 2 and result["passed"] == 1


def test_cwd_must_exist_inside_the_workspace(ws):
    configure(ws["service"], ws["id"], f"[checks]\nhere = {{ command = {py('import os; print(os.getcwd())')}, cwd = 'src' }}\n"
                                        f"gone = {{ command = {py('pass')}, cwd = 'missing' }}\n")
    result = finished(ws, call(ws, "run_checks", names=["here", "gone"], wait_seconds=20))
    assert result["results"][0]["log_tail"].strip().endswith("/alpha/src")
    assert result["results"][1]["status"] == "error" and "does not exist" in result["results"][1]["log_tail"]


def test_checks_toml_lives_in_node_state_and_the_node_restarts_cleanly(ws):
    service = ws["service"]
    configure(service, ws["id"])
    node = ws["node"]["service"]
    path = module.config_path(node.state, ws["id"])
    assert path.is_file() and ws["root"] not in path.parents
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    runner = node.checks
    with runner.lock, runner.db:
        runner.db.execute("INSERT INTO batches(id,workspace,state,created) VALUES('chk_' || hex(randomblob(12)),?,"
                          "'running','2026-01-01T00:00:00+00:00')", (ws["id"],))
    runner.close()
    node._checks = None
    reopened = node.checks
    assert reopened.db.execute("SELECT state FROM batches").fetchone()["state"] == "interrupted"


def test_after_run_checks_hold_the_finished_notice(modern_env):
    service, ws_id, token, job, native, _ = modern_env
    configure(service, ws_id, after_run=["ok", "bad"])
    run = service.call(ws_id, token, "start_run", {
        "adapter_id": ADAPTER_ID, "job_id": job["id"], "request_id": "checked-run"})
    _finish_test_run(service, native, ws_id, run["run_id"])

    def events():
        return [row["event_type"] for row in service.db.execute(
            "SELECT event_type FROM notification_events WHERE run_id=?", (run["run_id"],))]

    assert "run_completed" not in events()
    read = service.call(ws_id, token, "read_run", {"run_id": run["run_id"]})
    assert read["checks"] == {"state": "waiting", "names": ["ok", "bad"]}
    run_checks = service.run_coordinator.run_checks
    assert run_checks.process() == 0  # the post snapshot comes first
    service.run_coordinator.checkpoints.process_pending()
    assert run_checks.process() == 1
    deadline = time.monotonic() + 20
    while run_checks.view(run["run_id"])["state"] == "running" and time.monotonic() < deadline:
        service.db.execute("UPDATE agent_run_checks SET next_attempt=0")
        run_checks.process()
        time.sleep(0.1)
    view = service.call(ws_id, token, "read_run", {"run_id": run["run_id"]})["checks"]
    assert view["state"] == "complete" and view["passed"] == 1 and view["failed"] == 1
    assert [item["status"] for item in view["results"]] == ["passed", "failed"]
    row = service.db.execute("SELECT * FROM notification_events WHERE run_id=? AND event_type='run_completed'",
                             (run["run_id"],)).fetchone()
    assert row["checks"] == "complete:1:1"
    assert EventBroker.project(row)["data"]["checks"] == {"state": "complete", "passed": 1, "failed": 1}
    event = NotificationEvent.build(event_type="run_completed", run_id=run["run_id"], workspace_id=ws_id,
                                    workspace_name="Alpha", handoff_title="t", adapter_id=ADAPTER_ID,
                                    adapter_name="Codex", runtime_type="codex", checks=row["checks"])
    fields = DiscordChannel._payload(event)["embeds"][0]["fields"]
    assert {"name": "Checks", "value": "1 passed, 1 failed", "inline": True} in fields


def test_after_run_checks_report_unavailable_and_skip_cancelled_runs(modern_env, monkeypatch):
    service, ws_id, token, job, native, _ = modern_env
    configure(service, ws_id, after_run=["ok"])
    run = service.call(ws_id, token, "start_run", {
        "adapter_id": ADAPTER_ID, "job_id": job["id"], "request_id": "unavailable-run"})
    _finish_test_run(service, native, ws_id, run["run_id"])
    service.run_coordinator.checkpoints.process_pending()
    # checks.toml loses the name before the run's checks start.
    service.run_coordinator.run_checks.write_config(service.workspace(ws_id, False),
                                                    f"[checks]\nother = {{ command = {py('pass')} }}\n")
    with service.lock, service.db:
        service.db.execute("UPDATE workspace_checks SET after_run='[\"ok\"]'")
    service.run_coordinator.run_checks.process()
    assert service.run_coordinator.run_checks.view(run["run_id"])["error"] == "checks_unconfigured"
    row = service.db.execute("SELECT checks FROM notification_events WHERE run_id=? AND event_type='run_completed'", (run["run_id"],)).fetchone()
    assert row["checks"] == "unavailable"
    assert service.run_coordinator.run_checks.defer_locked(
        {"id": "run_x", "workspace": ws_id, "node_id": "n"}, "run_cancelled", {}) is False


def test_write_config_drops_removed_names_from_after_run(env):
    service = env["service"]
    configure(service, env["id"], after_run=["ok", "bad"])
    ws = service.workspace(env["id"], False)
    with pytest.raises(BridgeError) as caught:
        service.run_coordinator.run_checks.set_after_run(ws, ["ok", "nope"])
    assert caught.value.code == "unknown_check"
    result = service.run_coordinator.run_checks.write_config(ws, f"[checks]\nok = {{ command = {py('pass')} }}\n")
    assert result["after_run"] == ["ok"]


async def test_mcp_schema_and_manager_api(env):
    model = TOOLS["run_checks"][0]
    assert model.model_json_schema()["additionalProperties"] is False
    assert {"names", "check_id", "wait_seconds"} <= set(model.model_json_schema()["properties"])
    with pytest.raises(Exception):
        model.model_validate({"names": ["ok"], "check_id": "chk_" + "0" * 24})
    with pytest.raises(Exception):
        model.model_validate({"names": ["Bad Name"]})
    assert TOOLS["run_checks"][2] is False
    service = env["service"]
    app = make_admin(service)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8766",
                                 headers={"Cookie": admin_cookie(app)}) as client:
        empty = (await client.get(f"/api/workspaces/{env['id']}/checks")).json()
        assert empty == {"text": "", "checks": [], "error": None, "after_run": []}
        saved = await client.put(f"/api/workspaces/{env['id']}/checks", json={"text": CONFIG, "after_run": ["ok"]})
        assert saved.status_code == 200 and saved.json()["after_run"] == ["ok"]
        assert [item["name"] for item in saved.json()["checks"]] == ["ok", "bad"]
        bad = await client.put(f"/api/workspaces/{env['id']}/checks", json={"text": "[checks]\nX = 1\n"})
        assert bad.status_code == 400
    async with httpx.AsyncClient(transport=McpTransport(make_mcp(service)),
                                 base_url="http://127.0.0.1:8765",
                                 headers={"X-Bridge-Token": env["token"],
                                          "Accept": "application/json, text/event-stream"}) as client:
        listed = await client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        tool = next(item for item in listed.json()["result"]["tools"] if item["name"] == "run_checks")
        assert tool["annotations"]["readOnlyHint"] is False
