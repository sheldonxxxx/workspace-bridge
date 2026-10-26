"""Bridge run checkpoints: lifecycle hooks, durable post capture and retention."""
from pathlib import Path
import time

import pytest

from workspace_bridge import run_checkpoints as module
from workspace_bridge.node_client import NodeClient
from workspace_bridge.runtime import RuntimeRejected
from workspace_bridge.security import BridgeError
from test_run_coordinator import ADAPTER_ID, _finish_test_run, _second_job, modern_env  # noqa: F401


def _start(service, ws_id, token, job, request_id="checkpoint-run"):
    return service.call(ws_id, token, "start_agent_run", {
        "adapter_id": ADAPTER_ID, "job_id": job["id"], "request_id": request_id})


def _root(service, ws_id) -> Path:
    return Path(service.workspace(ws_id)["root"])


def _read(service, ws_id, token, run_id):
    return service.call(ws_id, token, "read_agent_run", {"run_id": run_id})


def _row(service, run_id):
    return service.run_coordinator.checkpoints._row(run_id)


def test_run_window_checkpoint_and_diff(modern_env):
    service, ws_id, token, job, native, _ = modern_env
    root = _root(service, ws_id)
    (root / "app.py").write_text("print('user edit before the run')\n")
    run = _start(service, ws_id, token, job)
    pending = _read(service, ws_id, token, run["run_id"])["checkpoint"]
    assert pending["status"] == "pending" and pending["pre"]["status"] == "complete"
    (root / "agent.py").write_text("print('agent')\n")
    _finish_test_run(service, native, ws_id, run["run_id"])
    assert _row(service, run["run_id"])["post_status"] == "pending"
    assert service.run_coordinator.checkpoints.process_pending() == 1
    checkpoint = _read(service, ws_id, token, run["run_id"])["checkpoint"]
    assert checkpoint["status"] == "complete" and checkpoint["late"] is False
    assert checkpoint["changed_paths"] == 1
    assert checkpoint["changes"] == [{"path": "agent.py", "status": "added"}]
    diff = service.run_coordinator.checkpoints.diff(service.workspace(ws_id), run["run_id"])
    assert "print('agent')" in diff["patch"] and "user edit" not in diff["patch"]
    assert diff["run_id"] == run["run_id"] and diff["checkpoint"]["status"] == "complete"


def test_pre_capture_failure_never_blocks_the_run(modern_env, monkeypatch):
    service, ws_id, token, job, native, _ = modern_env
    original = NodeClient.workspace

    def failing(self, operation, workspace, arguments):
        if operation == "checkpoint_capture":
            raise BridgeError("Node unavailable", "node_unavailable")
        return original(self, operation, workspace, arguments)

    monkeypatch.setattr(NodeClient, "workspace", failing)
    run = _start(service, ws_id, token, job)
    assert run["phase"] in {"starting", "active"}
    checkpoint = _read(service, ws_id, token, run["run_id"])["checkpoint"]
    assert checkpoint["status"] == "unavailable"
    assert checkpoint["pre"] == {"status": "unavailable", "captured_at": checkpoint["pre"]["captured_at"],
                                 "error": "node_unavailable"}
    _finish_test_run(service, native, ws_id, run["run_id"])
    service.run_coordinator.checkpoints.process_pending()
    assert _row(service, run["run_id"])["post_error"] == "pre_unavailable"
    with pytest.raises(BridgeError) as exc:
        service.run_coordinator.checkpoints.diff(service.workspace(ws_id), run["run_id"])
    assert exc.value.code == "checkpoint_unavailable"


def test_older_node_is_reported_as_unsupported(modern_env, monkeypatch):
    service, ws_id, token, job, _, _ = modern_env
    original = NodeClient.workspace

    def old_node(self, operation, workspace, arguments):
        if operation.startswith("checkpoint_"):
            raise BridgeError("Unknown Node workspace operation", "not_found")
        return original(self, operation, workspace, arguments)

    monkeypatch.setattr(NodeClient, "workspace", old_node)
    run = _start(service, ws_id, token, job)
    assert _read(service, ws_id, token, run["run_id"])["checkpoint"]["pre"]["error"] == "node_unsupported"


def test_rejected_start_still_queues_post_capture(modern_env, monkeypatch):
    service, ws_id, token, job, native, _ = modern_env

    def reject(*_args, **_kwargs):
        raise RuntimeRejected("rejected by runtime", "invalid_arguments")

    monkeypatch.setattr(native, "start_run", reject)
    with pytest.raises(RuntimeRejected):
        _start(service, ws_id, token, job)
    with service.lock:
        run_id = service.db.execute("SELECT id FROM agent_runs ORDER BY created DESC LIMIT 1").fetchone()[0]
    assert _row(service, run_id)["post_status"] == "pending"
    service.run_coordinator.checkpoints.process_pending()
    assert _row(service, run_id)["post_status"] == "complete"


def test_post_capture_retries_with_backoff_then_reports(modern_env, monkeypatch):
    service, ws_id, token, job, native, _ = modern_env
    run = _start(service, ws_id, token, job)
    _finish_test_run(service, native, ws_id, run["run_id"])
    original = NodeClient.workspace

    def failing(self, operation, workspace, arguments):
        if operation == "checkpoint_capture":
            raise BridgeError("Node unavailable", "node_unavailable")
        return original(self, operation, workspace, arguments)

    monkeypatch.setattr(NodeClient, "workspace", failing)
    checkpoints = service.run_coordinator.checkpoints
    for attempt in range(1, module.MAX_ATTEMPTS):
        assert checkpoints.process_pending() == 1
        row = _row(service, run["run_id"])
        assert row["post_status"] == "pending" and row["attempts"] == attempt
        assert row["next_attempt"] > time.time()
        assert checkpoints.process_pending() == 0
        with service.lock, service.db:
            service.db.execute("UPDATE agent_run_checkpoints SET next_attempt=0")
    checkpoints.process_pending()
    row = _row(service, run["run_id"])
    assert row["post_status"] == "unavailable" and row["post_error"] == "node_unavailable"


def test_late_post_capture_is_flagged(modern_env):
    service, ws_id, token, job, native, _ = modern_env
    run = _start(service, ws_id, token, job)
    _finish_test_run(service, native, ws_id, run["run_id"])
    with service.lock, service.db:
        service.db.execute("UPDATE agent_run_checkpoints SET terminal_at='2020-01-01T00:00:00+00:00'")
    service.run_coordinator.checkpoints.process_pending()
    assert _read(service, ws_id, token, run["run_id"])["checkpoint"]["late"] is True


def _two_finished_runs(modern_env, payload):
    service, ws_id, token, job, native, _ = modern_env
    first = _start(service, ws_id, token, job, "first")
    _finish_test_run(service, native, ws_id, first["run_id"])
    second_job = _second_job(service, ws_id, token, payload, "second-job")
    second = _start(service, ws_id, token, second_job, "second")
    _finish_test_run(service, native, ws_id, second["run_id"])
    service.run_coordinator.checkpoints.process_pending()
    return first["run_id"], second["run_id"]


def test_retention_expires_old_runs_but_keeps_the_newest(modern_env, payload):
    service, ws_id, token, *_ = modern_env
    first, second = _two_finished_runs(modern_env, payload)
    checkpoints = service.run_coordinator.checkpoints
    assert checkpoints.apply_retention(now=time.time()) == 0
    assert checkpoints.apply_retention(now=time.time() + module.RETENTION_SECONDS + 60) == 1
    assert _read(service, ws_id, token, first)["checkpoint"]["status"] == "expired"
    assert _read(service, ws_id, token, second)["checkpoint"]["status"] == "complete"
    with pytest.raises(BridgeError) as exc:
        checkpoints.diff(service.workspace(ws_id), first)
    assert exc.value.code == "checkpoint_expired"
    assert checkpoints.stats(service.workspace(ws_id))["checkpoints"] == 2


def test_store_cap_evicts_oldest_unprotected_checkpoints(modern_env, payload, monkeypatch):
    monkeypatch.setattr(module, "STORE_CAP_BYTES", 1)
    service, ws_id, token, *_ = modern_env
    first, second = _two_finished_runs(modern_env, payload)
    assert _row(service, first)["pre_status"] == "expired"
    assert _row(service, second)["post_status"] == "complete"


def test_workspace_purge_expires_everything(modern_env, payload):
    service, ws_id, token, *_ = modern_env
    first, second = _two_finished_runs(modern_env, payload)
    result = service.run_coordinator.checkpoints.purge_workspace(service.workspace(ws_id))
    assert result["removed"] == 4 and result["store_bytes"] == 0
    assert {_read(service, ws_id, token, run_id)["checkpoint"]["status"]
            for run_id in (first, second)} == {"expired"}


def test_excluded_paths_are_hidden_from_the_stored_summary(modern_env):
    service, ws_id, token, job, native, _ = modern_env
    root = _root(service, ws_id)
    run = _start(service, ws_id, token, job)
    (root / "public.txt").write_text("a\n")
    (root / "private.txt").write_text("a\n")
    _finish_test_run(service, native, ws_id, run["run_id"])
    service.run_coordinator.checkpoints.process_pending()
    service.manage_workspace(ws_id, "set_excludes", excludes=["private.txt"])
    checkpoint = _read(service, ws_id, token, run["run_id"])["checkpoint"]
    assert [item["path"] for item in checkpoint["changes"]] == ["public.txt"]
    assert checkpoint["changed_paths"] == 1



def test_overlapping_runs_in_one_workspace_are_reported(modern_env, payload):
    service, ws_id, token, *_ = modern_env
    first, second = _two_finished_runs(modern_env, payload)
    assert _read(service, ws_id, token, first)["checkpoint"]["overlapping_runs"] == []
    # The fake runtime cannot hold two conversations open at once, so model a
    # second run admitted before the first one finished.
    with service.lock, service.db:
        created = service.db.execute("SELECT created FROM agent_run_checkpoints WHERE run=?",
                                     (second,)).fetchone()[0]
        service.db.execute("UPDATE agent_run_checkpoints SET terminal_at=? WHERE run=?",
                           ("9000-01-01T00:00:00+00:00", first))
    assert created < "9000"
    assert _read(service, ws_id, token, first)["checkpoint"]["overlapping_runs"] == [second]
    assert _read(service, ws_id, token, second)["checkpoint"]["overlapping_runs"] == [first]


def test_mcp_git_diff_run_mode_schema_and_dispatch(modern_env):
    from pydantic import ValidationError

    from workspace_bridge.api import TOOLS
    service, ws_id, token, job, native, _ = modern_env
    run = _start(service, ws_id, token, job)
    model = TOOLS["git_diff"][0]
    arguments = model.model_validate({"workspace_id": ws_id, "mode": "run",
                                      "run_id": run["run_id"]}).model_dump()
    arguments.pop("workspace_id")
    with pytest.raises(BridgeError) as exc:
        service.call(ws_id, token, "git_diff", arguments)
    assert exc.value.code == "checkpoint_pending"
    (_root(service, ws_id) / "feature.py").write_text("VALUE = 1\n")
    _finish_test_run(service, native, ws_id, run["run_id"])
    service.run_coordinator.checkpoints.process_pending()
    result = service.call(ws_id, token, "git_diff", arguments)
    assert "+VALUE = 1" in result["patch"] and result["mode"] == "run"
    for invalid in ({"mode": "run"}, {"mode": "head", "run_id": run["run_id"]},
                    {"mode": "run", "run_id": run["run_id"], "expected_status_sha256": "0" * 64}):
        with pytest.raises(ValidationError):
            model.model_validate({"workspace_id": ws_id, **invalid})


async def test_manager_and_mcp_http_surfaces(modern_env):
    import httpx

    from admin_helpers import admin_cookie
    from workspace_bridge.api import make_admin, make_mcp
    service, ws_id, token, job, native, _ = modern_env
    run = _start(service, ws_id, token, job)
    (_root(service, ws_id) / "feature.py").write_text("VALUE = 2\n")
    _finish_test_run(service, native, ws_id, run["run_id"])
    service.run_coordinator.checkpoints.process_pending()
    app = make_admin(service)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://127.0.0.1:8766",
                                 headers={"Cookie": admin_cookie(app)}) as client:
        detail = (await client.get(f"/api/runs/{run['run_id']}")).json()
        assert detail["checkpoint"]["status"] == "complete"
        diff = await client.get(f"/api/runs/{run['run_id']}/diff", params={"path": "feature.py"})
        assert diff.status_code == 200 and "+VALUE = 2" in diff.json()["patch"]
        stats = (await client.get(f"/api/workspaces/{ws_id}/checkpoints")).json()
        assert stats["checkpoints"] == 2 and stats["store_bytes"] > 0
        purged = await client.delete(f"/api/workspaces/{ws_id}/checkpoints")
        assert purged.status_code == 200 and purged.json()["removed"] == 2
        expired = await client.get(f"/api/runs/{run['run_id']}/diff")
        assert expired.status_code == 400
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=make_mcp(service)),
                                 base_url="http://127.0.0.1:8765",
                                 headers={"X-Bridge-Token": token,
                                          "Accept": "application/json, text/event-stream"}) as client:
        response = await client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "git_diff", "arguments": {
                "workspace_id": ws_id, "mode": "run", "run_id": run["run_id"]}}})
        result = response.json()["result"]
        assert result["isError"] is True and "checkpoint_expired" in result["content"][0]["text"]
