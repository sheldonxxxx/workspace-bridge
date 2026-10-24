"""Bridge side of run checkpoints: lifecycle, durable post capture, retention.

The Bridge keeps only checkpoint IDs, statuses, bounded change summaries and
error codes; file content stays in the owning Node's shadow store. A capture
failure never blocks, retries or alters a run. It only makes that run's
checkpoint unavailable, and the reason is visible on the run.
"""
from __future__ import annotations

import json
import re
import secrets
import sqlite3
import threading
import time
from datetime import datetime, timezone

from .security import BridgeError, allowed

PRE_BUDGET_MS = 15000
POST_BUDGET_MS = 55000
RETENTION_SECONDS = 14 * 24 * 3600
STORE_CAP_BYTES = 1024 ** 3
LATE_SECONDS = 60
MAX_ATTEMPTS = 5
SUMMARY_PATHS = 100
SWEEP_SECONDS = 10
RETENTION_INTERVAL = 600
EVICT_BATCH = 5
_CODE = re.compile(r"[a-z_]{1,40}")
_USABLE = ("complete", "partial")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _epoch(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return None


def _code(exc: BaseException, operation: str = "") -> str:
    code = getattr(exc, "code", "")
    if operation == "checkpoint_capture" and code == "not_found":
        # An older Node answers an unknown workspace operation this way.
        return "node_unsupported"
    return code if isinstance(code, str) and _CODE.fullmatch(code) else "checkpoint_failed"


class RunCheckpoints:
    """Pre/post checkpoints for each Bridge run, captured on its owning Node."""

    def __init__(self, coordinator, *, read_only: bool = False):
        self.coordinator = coordinator
        self.service = coordinator.service
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_retention = 0.0
        if not read_only:
            self.service.db.executescript("""
              CREATE TABLE IF NOT EXISTS agent_run_checkpoints (
                run TEXT PRIMARY KEY REFERENCES agent_runs(id),
                workspace TEXT NOT NULL REFERENCES workspaces(id), node_id TEXT NOT NULL,
                pre_id TEXT NOT NULL, pre_status TEXT NOT NULL CHECK(pre_status IN
                  ('pending','complete','partial','unavailable','expired')),
                pre_error TEXT, pre_captured TEXT,
                post_id TEXT NOT NULL, post_status TEXT NOT NULL CHECK(post_status IN
                  ('waiting','pending','complete','partial','unavailable','expired')),
                post_error TEXT, post_captured TEXT,
                late INTEGER NOT NULL DEFAULT 0 CHECK(late IN (0,1)),
                terminal_at TEXT, attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt REAL NOT NULL DEFAULT 0, summary TEXT, created TEXT NOT NULL);
              CREATE INDEX IF NOT EXISTS ix_agent_run_checkpoints_post
                ON agent_run_checkpoints(post_status, next_attempt);
            """)

    # -- lifecycle hooks ---------------------------------------------------

    def begin_locked(self, run_id: str, workspace_id: str, node_id: str) -> None:
        """Record the run's checkpoint IDs in the transaction that admits it."""
        self.service.db.execute(
            "INSERT INTO agent_run_checkpoints(run,workspace,node_id,pre_id,pre_status,"
            "post_id,post_status,created) VALUES(?,?,?,?,?,?,?,?)",
            (run_id, workspace_id, node_id, "ckp_" + secrets.token_hex(12), "pending",
             "ckp_" + secrets.token_hex(12), "waiting", _now()))

    def capture_pre(self, ws: dict, run_id: str) -> None:
        """Capture before the native start. Never raises; failure is recorded."""
        row = self._row(run_id)
        if row is None or row["pre_status"] != "pending":
            return
        try:
            result = self._node(row, timeout=PRE_BUDGET_MS / 1000 + 10).workspace(
                "checkpoint_capture", ws,
                {"checkpoint_id": row["pre_id"], "budget_ms": PRE_BUDGET_MS})
            status, error = result["status"], None
        except Exception as exc:  # noqa: BLE001 - evidence must never block a run
            status, error = "unavailable", _code(exc, "checkpoint_capture")
        with self.service.lock, self.service.db:
            self.service.db.execute(
                "UPDATE agent_run_checkpoints SET pre_status=?,pre_error=?,pre_captured=? "
                "WHERE run=? AND pre_status='pending'", (status, error, _now(), run_id))

    def enqueue_post_locked(self, run_id: str) -> None:
        """Queue the post capture in the transaction that makes the run terminal."""
        self.service.db.execute(
            "UPDATE agent_run_checkpoints SET post_status='pending',terminal_at=?,next_attempt=0 "
            "WHERE run=? AND post_status='waiting'", (_now(), run_id))
        self._wake.set()

    # -- background work ---------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="run-checkpoints", daemon=True)
        self._thread.start()

    def close(self, deadline: float) -> None:
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=max(0, deadline - time.monotonic()))

    def _stopped(self) -> bool:
        return self.coordinator._stop.is_set()

    def _loop(self) -> None:
        while not self._stopped():
            self._wake.clear()
            try:
                self.process_pending()
                if time.monotonic() - self._last_retention >= RETENTION_INTERVAL:
                    self._last_retention = time.monotonic()
                    self.apply_retention()
            except Exception:  # noqa: BLE001 - the next sweep retries durable work
                pass
            self._wake.wait(SWEEP_SECONDS)

    def process_pending(self, limit: int = 20) -> int:
        with self.service.lock:
            if self._stopped():
                return 0
            rows = [dict(row) for row in self.service.db.execute(
                "SELECT * FROM agent_run_checkpoints WHERE post_status='pending' "
                "AND next_attempt<=? ORDER BY terminal_at LIMIT ?", (time.time(), limit))]
        for row in rows:
            if self._stopped():
                break
            self._capture_post(row)
        return len(rows)

    def _capture_post(self, row: dict) -> None:
        if row["pre_status"] not in _USABLE:
            self._finish_post(row["run"], "unavailable", "pre_unavailable")
            return
        try:
            ws = self._workspace(row)
            result = self._node(row, timeout=POST_BUDGET_MS / 1000 + 5).workspace(
                "checkpoint_capture", ws,
                {"checkpoint_id": row["post_id"], "budget_ms": POST_BUDGET_MS})
        except Exception as exc:  # noqa: BLE001 - retried with backoff, then reported
            attempts = row["attempts"] + 1
            if attempts >= MAX_ATTEMPTS:
                self._finish_post(row["run"], "unavailable", _code(exc, "checkpoint_capture"))
                return
            with self.service.lock, self.service.db:
                self.service.db.execute(
                    "UPDATE agent_run_checkpoints SET attempts=?,next_attempt=?,post_error=? "
                    "WHERE run=? AND post_status='pending'",
                    (attempts, time.time() + 10 * 2 ** attempts, _code(exc, "checkpoint_capture"),
                     row["run"]))
            return
        summary = None
        try:
            changes = self._node(row).workspace("checkpoint_changes", ws, {
                "from_id": row["pre_id"], "to_id": row["post_id"], "limit": SUMMARY_PATHS})
            summary = {"changed_paths": changes["total"], "changes": changes["changes"]}
        except Exception:  # noqa: BLE001 - the diff stays readable without a summary
            pass
        captured = _now()
        terminal = _epoch(row["terminal_at"])
        late = terminal is not None and (_epoch(captured) or 0) - terminal > LATE_SECONDS
        self._finish_post(row["run"], result["status"], None, late=late, summary=summary,
                          captured=captured)
        if result.get("store_bytes", 0) > STORE_CAP_BYTES:
            self._evict(row, result["store_bytes"])

    def _finish_post(self, run_id: str, status: str, error: str | None, *, late: bool = False,
                     summary: dict | None = None, captured: str | None = None) -> None:
        with self.service.lock, self.service.db:
            self.service.db.execute(
                "UPDATE agent_run_checkpoints SET post_status=?,post_error=?,post_captured=?,"
                "late=?,summary=? WHERE run=? AND post_status='pending'",
                (status, error, captured, int(late),
                 json.dumps(summary, sort_keys=True) if summary is not None else None, run_id))

    # -- retention ---------------------------------------------------------

    def _candidates(self, workspace_id: str, older_than: float | None = None) -> list[dict]:
        """Oldest first; active runs and the newest terminal run are protected."""
        with self.service.lock:
            rows = [dict(row) for row in self.service.db.execute(
                "SELECT * FROM agent_run_checkpoints WHERE workspace=? AND terminal_at IS NOT NULL "
                "AND post_status NOT IN ('waiting','pending') AND pre_status!='expired' "
                "ORDER BY terminal_at", (workspace_id,))]
        rows = rows[:-1]
        if older_than is not None:
            rows = [row for row in rows if (_epoch(row["terminal_at"]) or 0) < older_than]
        return rows

    def _purge(self, row: dict, rows: list[dict]) -> int | None:
        ws = self._workspace(row)
        result = self._node(row, timeout=150).workspace("checkpoint_purge", ws, {
            "checkpoint_ids": [value for item in rows for value in (item["pre_id"], item["post_id"])]})
        with self.service.lock, self.service.db:
            for item in rows:
                self.service.db.execute(
                    "UPDATE agent_run_checkpoints SET pre_status='expired',post_status='expired' "
                    "WHERE run=?", (item["run"],))
        return result.get("store_bytes")

    def _evict(self, row: dict, store_bytes: int) -> None:
        candidates = self._candidates(row["workspace"])
        while store_bytes > STORE_CAP_BYTES and candidates and not self._stopped():
            batch, candidates = candidates[:EVICT_BATCH], candidates[EVICT_BATCH:]
            try:
                store_bytes = self._purge(row, batch) or 0
            except Exception:  # noqa: BLE001 - retention retries on its next pass
                return

    def apply_retention(self, now: float | None = None) -> int:
        cutoff = (now if now is not None else time.time()) - RETENTION_SECONDS
        with self.service.lock:
            workspaces = [item[0] for item in self.service.db.execute(
                "SELECT DISTINCT workspace FROM agent_run_checkpoints WHERE pre_status!='expired'")]
        expired = 0
        for workspace_id in workspaces:
            candidates = self._candidates(workspace_id, older_than=cutoff)
            for index in range(0, len(candidates), EVICT_BATCH):
                if self._stopped():
                    return expired
                batch = candidates[index:index + EVICT_BATCH]
                try:
                    self._purge(batch[0], batch)
                except Exception:  # noqa: BLE001 - retried on the next pass
                    break
                expired += len(batch)
        return expired

    def purge_workspace(self, ws: dict) -> dict:
        """Local-admin purge of every checkpoint for one workspace."""
        result = self.service.node_registry.client(ws["node_id"], timeout=150).workspace(
            "checkpoint_purge", ws, {"everything": True})
        with self.service.lock, self.service.db:
            self.service.db.execute(
                "UPDATE agent_run_checkpoints SET pre_status='expired',"
                "post_status=CASE WHEN post_status IN ('waiting','pending') THEN 'unavailable' "
                "ELSE 'expired' END,post_error=CASE WHEN post_status IN ('waiting','pending') "
                "THEN 'checkpoint_expired' ELSE post_error END WHERE workspace=?", (ws["id"],))
        return {"removed": result.get("removed", 0), "store_bytes": result.get("store_bytes", 0)}

    def stats(self, ws: dict) -> dict:
        return self.service.node_registry.client(ws["node_id"], timeout=10).workspace(
            "checkpoint_stats", ws, {})

    # -- reads -------------------------------------------------------------

    def _row(self, run_id: str) -> dict | None:
        with self.service.lock:
            try:
                row = self.service.db.execute(
                    "SELECT * FROM agent_run_checkpoints WHERE run=?", (run_id,)).fetchone()
            except sqlite3.OperationalError:
                return None  # A read-only diagnostic view of older state.
        return dict(row) if row is not None else None

    def _workspace(self, row: dict) -> dict:
        with self.service.lock:
            ws = self.service.workspace(row["workspace"], False)
        if ws["node_id"] != row["node_id"]:
            raise BridgeError("Workspace moved to another Node", "node_changed")
        return ws

    def _node(self, row: dict, *, timeout: float = 30):
        return self.service.node_registry.client(row["node_id"], timeout=min(timeout, 60))

    @staticmethod
    def _status(row: dict) -> str:
        states = (row["pre_status"], row["post_status"])
        if "expired" in states:
            return "expired"
        if "unavailable" in states:
            return "unavailable"
        if row["pre_status"] == "pending" or row["post_status"] in ("waiting", "pending"):
            return "pending"
        return "partial" if "partial" in states else "complete"

    def view(self, ws: dict, run_id: str) -> dict | None:
        row = self._row(run_id)
        if row is None:
            return None
        result = {"status": self._status(row),
                  "pre": {"status": row["pre_status"], "captured_at": row["pre_captured"]},
                  "post": {"status": row["post_status"], "captured_at": row["post_captured"]},
                  "late": bool(row["late"])}
        for phase in ("pre", "post"):
            if row[phase + "_error"]:
                result[phase]["error"] = row[phase + "_error"]
        if row["summary"]:
            summary = json.loads(row["summary"])
            excludes = ws.get("excludes") or []
            if isinstance(excludes, str):
                excludes = json.loads(excludes)
            # Re-apply current exclusions: a path excluded since capture is not shown.
            changes = [item for item in summary.get("changes", []) if allowed(item["path"], excludes)]
            hidden = len(summary.get("changes", [])) - len(changes)
            result["changed_paths"] = summary.get("changed_paths", 0) - hidden
            result["changes"] = changes
        with self.service.lock:
            overlapping = [item[0] for item in self.service.db.execute(
                "SELECT c.run FROM agent_run_checkpoints c JOIN agent_run_checkpoints me ON me.run=? "
                "WHERE c.workspace=me.workspace AND c.run!=me.run "
                "AND c.created < COALESCE(me.terminal_at, '9999') "
                "AND COALESCE(c.terminal_at, '9999') > me.created ORDER BY c.created LIMIT 10",
                (run_id,))]
        result["overlapping_runs"] = overlapping
        return result

    def diff(self, ws: dict, run_id: str, *, path: str | None = None, offset: int = 0,
             max_bytes: int = 3000) -> dict:
        row = self._row(run_id)
        if row is None or row["workspace"] != ws["id"]:
            raise BridgeError("This run has no checkpoints", "checkpoint_unavailable")
        status = self._status(row)
        if status == "pending":
            raise BridgeError("Run checkpoints are still being captured; read the run again later",
                              "checkpoint_pending")
        if status == "expired":
            raise BridgeError("Run checkpoints were removed by retention", "checkpoint_expired")
        if status == "unavailable":
            raise BridgeError("Run checkpoints are unavailable for this run", "checkpoint_unavailable")
        result = self._node(row).workspace("checkpoint_diff", self._workspace(row), {
            "from_id": row["pre_id"], "to_id": row["post_id"], "path": path,
            "offset": offset, "max_bytes": max_bytes})
        return {**{key: value for key, value in result.items() if key not in ("from", "to")},
                "run_id": run_id, "checkpoint": self.view(ws, run_id)}
