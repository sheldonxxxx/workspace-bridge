"""After-run checks: the Bridge side of the Node's checks.toml.

A workspace may name checks to run after every run that succeeds or fails.
The run's finished notice waits for them, so ChatGPT's run.finished event and
the Discord notice carry the pass/fail count. The post-run snapshot is taken
first, so the checks' own output files never count as the agent's changes.
"""
from __future__ import annotations

import json
import re
import threading
import time
from datetime import datetime, timezone

from .checks import CHECK_ID, MAX_CHECKS, NAME, compact
from .security import BridgeError

DEFERRED_EVENTS = {"run_completed", "run_failed"}
START_ATTEMPTS = 5
# Slack on top of the configured timeouts before the Bridge stops waiting.
DEADLINE_SLACK = 300
MAX_WAIT_SECONDS = 50


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class RunChecks:
    def __init__(self, coordinator, *, read_only: bool = False):
        self.coordinator = coordinator
        self.service = coordinator.service
        self._busy = threading.Lock()
        if not read_only:
            self.service.db.executescript("""
              CREATE TABLE IF NOT EXISTS workspace_checks (
                workspace TEXT PRIMARY KEY REFERENCES workspaces(id),
                after_run TEXT NOT NULL DEFAULT '[]', updated TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS agent_run_checks (
                run TEXT PRIMARY KEY REFERENCES agent_runs(id),
                workspace TEXT NOT NULL REFERENCES workspaces(id), node_id TEXT NOT NULL,
                names TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN
                  ('waiting','running','complete','unavailable')),
                check_id TEXT, summary TEXT, error TEXT, event TEXT,
                attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0,
                deadline REAL, created TEXT NOT NULL, updated TEXT NOT NULL);
              CREATE INDEX IF NOT EXISTS ix_agent_run_checks_state ON agent_run_checks(state, next_attempt);
            """)

    # -- configuration -------------------------------------------------------

    def after_run_locked(self, workspace_id: str) -> list[str]:
        row = self.service.db.execute("SELECT after_run FROM workspace_checks WHERE workspace=?",
                                      (workspace_id,)).fetchone()
        return json.loads(row["after_run"]) if row else []

    def after_run(self, workspace_id: str) -> list[str]:
        with self.service.lock:
            return self.after_run_locked(workspace_id)

    def set_after_run(self, ws: dict, names: object) -> list[str]:
        if not isinstance(names, list) or len(names) > MAX_CHECKS or len(set(names)) != len(names) or any(
                not isinstance(name, str) or not NAME.fullmatch(name) for name in names):
            raise BridgeError("after_run must list up to 20 distinct check names", "invalid_arguments")
        if names:
            configured = {item["name"] for item in self.config(ws)["checks"]}
            unknown = [name for name in names if name not in configured]
            if unknown:
                raise BridgeError(f"These checks are not in the workspace's checks.toml: {', '.join(unknown)}",
                                  "unknown_check")
        with self.service.lock, self.service.db:
            self.service.db.execute(
                "INSERT INTO workspace_checks(workspace,after_run,updated) VALUES(?,?,?) "
                "ON CONFLICT(workspace) DO UPDATE SET after_run=excluded.after_run,updated=excluded.updated",
                (ws["id"], json.dumps(names), _now()))
        return names

    def _client(self, ws: dict, *, timeout: float = 30):
        return self.service.node_registry.client(ws["node_id"], timeout=timeout)

    def config(self, ws: dict) -> dict:
        return self._client(ws).workspace("checks_config", ws, {})

    def write_config(self, ws: dict, text: str) -> dict:
        result = self._client(ws).workspace("set_checks_config", ws, {"text": text})
        configured = {item["name"] for item in result["checks"]}
        # Names dropped from checks.toml stop running after runs.
        with self.service.lock, self.service.db:
            current = self.after_run_locked(ws["id"])
            kept = [name for name in current if name in configured]
            if kept != current:
                self.service.db.execute("UPDATE workspace_checks SET after_run=?,updated=? WHERE workspace=?",
                                        (json.dumps(kept), _now(), ws["id"]))
        return {**result, "after_run": kept}

    def describe(self, ws: dict) -> dict:
        """The Manager's view: checks.toml text, parsed checks and the after-run list."""
        return {**self.config(ws), "after_run": self.after_run(ws["id"])}

    # -- ChatGPT's run_checks ---------------------------------------------------

    def run(self, ws: dict, *, names: list[str] | None = None, check_id: str | None = None,
            wait_seconds: int = 30, target: dict | None = None) -> dict:
        if isinstance(wait_seconds, bool) or not isinstance(wait_seconds, int) \
                or not 0 <= wait_seconds <= MAX_WAIT_SECONDS:
            raise BridgeError(f"wait_seconds must be 0 to {MAX_WAIT_SECONDS}", "invalid_arguments")
        client = self._client(ws)
        if check_id is None and not names:
            configured = client.workspace("checks_config", ws, {})
            return {"configured": configured["checks"], "after_run": self.after_run(ws["id"]),
                    "error": configured["error"],
                    "note": "Pass names to run checks. Only the user edits checks.toml on the Node."}
        if check_id is not None and names:
            raise BridgeError("Pass names to start checks or check_id to read them, not both",
                              "invalid_arguments")
        if check_id is None:
            result = client.workspace("start_checks", ws, {"names": names, **(target or {})})
            check_id = result["check_id"]
        elif not CHECK_ID.fullmatch(check_id):
            raise BridgeError("Invalid check_id", "invalid_arguments")
        deadline = time.monotonic() + wait_seconds
        while True:
            remaining = deadline - time.monotonic()
            result = client.workspace("read_checks", ws, {
                "check_id": check_id, "wait_ms": int(max(0, min(remaining, 20)) * 1000)})
            if result["state"] != "running" or time.monotonic() >= deadline:
                break
        if result["state"] == "running":
            result["note"] = "Still running. Call run_checks with this check_id to read the result."
        return result

    # -- after-run checks ------------------------------------------------------

    def defer_locked(self, run: dict, event_type: str, event: dict) -> bool:
        """Hold a run's finished notice until its after-run checks finish."""
        if event_type not in DEFERRED_EVENTS:
            return False
        names = self.after_run_locked(run["workspace"])
        if not names:
            return False
        stamp = _now()
        self.service.db.execute(
            "INSERT OR IGNORE INTO agent_run_checks(run,workspace,node_id,names,state,event,created,updated) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (run["id"], run["workspace"], run["node_id"], json.dumps(names), "waiting",
             json.dumps(event), stamp, stamp))
        return True

    def view(self, run_id: str) -> dict | None:
        with self.service.lock:
            row = self.service.db.execute("SELECT * FROM agent_run_checks WHERE run=?", (run_id,)).fetchone()
        if row is None:
            return None
        result = {"state": row["state"], "names": json.loads(row["names"])}
        if row["check_id"]:
            result["check_id"] = row["check_id"]
        if row["summary"]:
            result.update({key: value for key, value in json.loads(row["summary"]).items()
                           if key != "check_id"})
        if row["error"]:
            result["error"] = row["error"]
        return result

    def process(self, limit: int = 20) -> int:
        """Start waiting checks once the post snapshot is done; collect running ones."""
        if not self._busy.acquire(blocking=False):
            return 0
        try:
            with self.service.lock:
                rows = [dict(row) for row in self.service.db.execute(
                    "SELECT r.*,c.post_status FROM agent_run_checks r "
                    "LEFT JOIN agent_run_checkpoints c ON c.run=r.run "
                    "WHERE r.state IN ('waiting','running') AND r.next_attempt<=? "
                    "ORDER BY r.created LIMIT ?", (time.time(), limit))]
            done = 0
            for row in rows:
                if self.coordinator._stop.is_set():
                    break
                if row["state"] == "waiting":
                    if row["post_status"] in ("waiting", "pending"):
                        continue
                    self._start(row)
                else:
                    self._collect(row)
                done += 1
            return done
        finally:
            self._busy.release()

    def _workspace(self, row: dict) -> dict:
        with self.service.lock:
            ws = self.service.workspace(row["workspace"], False)
        if ws["node_id"] != row["node_id"]:
            raise BridgeError("Workspace moved to another Node", "node_changed")
        return ws

    def _start(self, row: dict) -> None:
        names = json.loads(row["names"])
        try:
            ws = self._workspace(row)
            client = self._client(ws)
            configured = {item["name"]: item for item in client.workspace("checks_config", ws, {})["checks"]}
            names = [name for name in names if name in configured]
            if not names:
                self._finish(row, "unavailable", error="checks_unconfigured")
                return
            budget = sum(configured[name]["timeout_seconds"] for name in names) + DEADLINE_SLACK
            started = client.workspace("start_checks", ws, {
                "names": names, "run_id": row["run"],
                **self.coordinator.worktrees.checkpoint_args(row["run"])})
        except Exception as exc:  # noqa: BLE001 - retried, then reported as unavailable
            attempts = row["attempts"] + 1
            code = _code(exc)
            if attempts >= START_ATTEMPTS or code in ("checks_unconfigured", "unknown_check",
                                                      "invalid_checks", "worktree_unavailable"):
                self._finish(row, "unavailable", error=code)
                return
            with self.service.lock, self.service.db:
                self.service.db.execute(
                    "UPDATE agent_run_checks SET attempts=?,next_attempt=?,error=?,updated=? "
                    "WHERE run=? AND state='waiting'",
                    (attempts, time.time() + 10 * 2 ** attempts, code, _now(), row["run"]))
            return
        with self.service.lock, self.service.db:
            self.service.db.execute(
                "UPDATE agent_run_checks SET state='running',check_id=?,names=?,deadline=?,attempts=0,"
                "error=NULL,next_attempt=?,updated=? WHERE run=? AND state='waiting'",
                (started["check_id"], json.dumps(names), time.time() + budget, time.time() + 2, _now(),
                 row["run"]))

    def _collect(self, row: dict) -> None:
        try:
            ws = self._workspace(row)
            result = self._client(ws).workspace("read_checks", ws, {"check_id": row["check_id"]})
        except Exception as exc:  # noqa: BLE001 - keep polling until the deadline
            result, code = None, _code(exc)
        else:
            code = None
        if result is not None and result["state"] != "running":
            self._finish(row, "complete", summary=compact(result))
            return
        if row["deadline"] is not None and time.time() > row["deadline"]:
            self._finish(row, "unavailable", error=code or "checks_timed_out")
            return
        with self.service.lock, self.service.db:
            self.service.db.execute("UPDATE agent_run_checks SET next_attempt=?,updated=? "
                                    "WHERE run=? AND state='running'", (time.time() + 5, _now(), row["run"]))

    def _finish(self, row: dict, state: str, *, summary: dict | None = None, error: str | None = None) -> None:
        if summary is not None:
            label = f"complete:{min(summary['passed'], 999)}:{min(summary['failed'], 999)}"
        else:
            label = "unavailable"
        with self.service.lock, self.service.db:
            cursor = self.service.db.execute(
                "UPDATE agent_run_checks SET state=?,summary=?,error=?,event=NULL,updated=? "
                "WHERE run=? AND state IN ('waiting','running')",
                (state, json.dumps(summary) if summary is not None else None, error, _now(), row["run"]))
            if cursor.rowcount == 1 and row["event"]:
                self.service.notification_manager.record_run_event_locked(
                    **json.loads(row["event"]), checks=label)


_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}")


def _code(exc: BaseException) -> str:
    code = getattr(exc, "code", "")
    return code if isinstance(code, str) and _CODE.fullmatch(code) else "checks_failed"
