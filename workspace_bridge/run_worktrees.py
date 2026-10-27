"""Bridge side of worktree isolation: which runs share a worktree, and its state.

A run started with ``isolation: "worktree"`` gets a fresh worktree on its
Node; continuations of that run's conversation keep working in it. The
worktree stays until ChatGPT applies it to the workspace (``apply_run``),
discards it (``discard_run``), or it sits unused for 14 days. The Bridge keeps
only IDs, the branch, the base commit and the state; files stay on the Node.
"""
from __future__ import annotations

import secrets
import time
from datetime import datetime, timezone

from .profile_registry import adapter_security
from .security import BridgeError

RETENTION_SECONDS = 14 * 24 * 3600
# A worktree still "creating" this long after it was asked for was cut off.
STALE_CREATING_SECONDS = 3600
NODE_TIMEOUT = 150
MAX_MESSAGE = 500


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _epoch(value: str) -> float:
    try:
        return datetime.fromisoformat(value).timestamp()
    except (TypeError, ValueError):
        return 0.0


class RunWorktrees:
    def __init__(self, coordinator, *, read_only: bool = False):
        self.coordinator = coordinator
        self.service = coordinator.service
        if not read_only:
            self.service.db.executescript("""
              CREATE TABLE IF NOT EXISTS agent_worktrees (
                id TEXT PRIMARY KEY, workspace TEXT NOT NULL REFERENCES workspaces(id),
                node_id TEXT NOT NULL, branch TEXT NOT NULL DEFAULT '', path TEXT NOT NULL DEFAULT '',
                base TEXT NOT NULL DEFAULT '',
                state TEXT NOT NULL CHECK(state IN
                  ('creating','active','applied','discarded','expired','failed')),
                edit_id TEXT, error TEXT, created TEXT NOT NULL, updated TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS agent_run_worktrees (
                run TEXT PRIMARY KEY REFERENCES agent_runs(id),
                worktree TEXT NOT NULL REFERENCES agent_worktrees(id));
              CREATE INDEX IF NOT EXISTS ix_agent_run_worktrees ON agent_run_worktrees(worktree);
            """)

    # -- rows ------------------------------------------------------------------

    def _row(self, worktree_id: str) -> dict | None:
        with self.service.lock:
            row = self.service.db.execute("SELECT * FROM agent_worktrees WHERE id=?",
                                          (worktree_id,)).fetchone()
        return dict(row) if row is not None else None

    def for_run_locked(self, run_id: str) -> dict | None:
        try:
            row = self.service.db.execute(
                "SELECT w.* FROM agent_worktrees w JOIN agent_run_worktrees r ON r.worktree=w.id "
                "WHERE r.run=?", (run_id,)).fetchone()
        except Exception:  # noqa: BLE001 - a read-only view of state from before worktrees
            return None
        return dict(row) if row is not None else None

    def for_run(self, run_id: str | None) -> dict | None:
        if not run_id:
            return None
        with self.service.lock:
            return self.for_run_locked(run_id)

    def link_locked(self, run_id: str, worktree_id: str) -> None:
        """Bind a run to its worktree in the transaction that admits the run."""
        self.service.db.execute("INSERT INTO agent_run_worktrees(run,worktree) VALUES(?,?)",
                                (run_id, worktree_id))
        self.service.db.execute("UPDATE agent_worktrees SET updated=? WHERE id=?", (_now(), worktree_id))

    @staticmethod
    def view(row: dict) -> dict:
        view = {"mode": "worktree", "worktree_id": row["id"], "state": row["state"],
                "branch": row["branch"], "path": row["path"], "base": row["base"]}
        if row["edit_id"]:
            view["edit_id"] = row["edit_id"]
        if row["error"]:
            view["error"] = row["error"]
        return view

    def _set(self, worktree_id: str, state: str, **fields) -> None:
        assignments = "".join(f",{key}=?" for key in fields)
        with self.service.lock, self.service.db:
            self.service.db.execute(f"UPDATE agent_worktrees SET state=?,updated=?{assignments} WHERE id=?",
                                    (state, _now(), *fields.values(), worktree_id))

    def _node(self, ws: dict):
        return self.service.node_registry.client(ws["node_id"], timeout=NODE_TIMEOUT)

    # -- lifecycle ---------------------------------------------------------------

    @staticmethod
    def supported(adapter: dict) -> bool:
        return adapter_security(adapter)["worktrees"]

    def create(self, ws: dict) -> dict:
        """Make a worktree on the workspace's Node before the run's conversation exists."""
        worktree_id = "wt_" + secrets.token_hex(12)
        stamp = _now()
        with self.service.lock, self.service.db:
            self.service.db.execute(
                "INSERT INTO agent_worktrees(id,workspace,node_id,state,created,updated) "
                "VALUES(?,?,?,'creating',?,?)", (worktree_id, ws["id"], ws["node_id"], stamp, stamp))
        try:
            made = self._node(ws).workspace("worktree_create", ws, {"worktree_id": worktree_id})
        except BridgeError as exc:
            self._set(worktree_id, "failed", error=exc.code)
            if exc.code == "not_found":
                raise BridgeError("This Node predates worktree isolation; update it", "isolation_unavailable") from None
            raise
        self._set(worktree_id, "active", branch=made["branch"], path=made["path"], base=made["base"])
        return self._row(worktree_id)

    def abandon(self, row: dict, ws: dict) -> None:
        """Remove a worktree whose first run never started. Best effort."""
        try:
            self._node(ws).workspace("worktree_remove", ws, {"worktree_id": row["id"]})
        except Exception:  # noqa: BLE001 - retention removes it later
            self._set(row["id"], "failed", error="run_not_started")
            return
        self._set(row["id"], "failed", error="run_not_started")

    def checkpoint_args(self, run_id: str) -> dict:
        """Extra Node arguments for a run's checkpoints and checks; {} for an ordinary run."""
        row = self.for_run(run_id)
        if row is None:
            return {}
        if row["state"] != "active":
            raise BridgeError("The run's worktree was applied, discarded or removed", "worktree_unavailable")
        return {"worktree_id": row["id"]}

    def target(self, ws: dict, run_id: str, *, idle: bool = False) -> dict:
        """Node arguments that point a read or a check at a run's unapplied worktree."""
        self.coordinator._run_row(ws, run_id)
        row = self.for_run(run_id)
        if row is None:
            raise BridgeError("This run did not work in a worktree; read the workspace instead", "not_isolated")
        if row["state"] != "active":
            raise BridgeError(f"The run's worktree is {row['state']}; read the workspace instead",
                              "worktree_unavailable")
        if idle:
            self._settled(ws, run_id)
        return {"worktree_id": row["id"]}

    def commit_quietly(self, run_id: str, ws: dict) -> None:
        """Record a finished run's files on its branch, for anyone reading the branch."""
        row = self.for_run(run_id)
        if row is None or row["state"] != "active":
            return
        # Retention counts a worktree's idle time from its latest run's end.
        with self.service.lock, self.service.db:
            self.service.db.execute("UPDATE agent_worktrees SET updated=? WHERE id=? AND state='active'",
                                    (_now(), row["id"]))
        try:
            self._node(ws).workspace("worktree_commit", ws, {"worktree_id": row["id"]})
        except Exception:  # noqa: BLE001 - apply commits again; the branch is a convenience
            pass

    def _settled(self, ws: dict, run_id: str) -> dict:
        self.coordinator._run_row(ws, run_id)
        row = self.for_run(run_id)
        if row is None:
            raise BridgeError("This run did not work in a worktree; its changes are already in the workspace",
                              "not_isolated")
        with self.service.lock:
            busy = self.service.db.execute(
                "SELECT r.id FROM agent_runs r JOIN agent_run_worktrees w ON w.run=r.id "
                "WHERE w.worktree=? AND r.phase IN ('starting','active') LIMIT 1", (row["id"],)).fetchone()
        if busy is not None and row["state"] == "active":
            raise BridgeError(f"Run {busy['id']} is still working in this worktree; wait for it or cancel it",
                              "run_active")
        return row

    def apply(self, ws: dict, run_id: str) -> dict:
        row = self._settled(ws, run_id)
        if row["state"] == "applied":
            return {"run_id": run_id, "isolation": self.view(row), "repeated": True}
        if row["state"] != "active":
            raise BridgeError(f"The run's worktree is {row['state']}; nothing to apply", "worktree_unavailable")
        title = self.coordinator.service.job(ws, self.coordinator._run_row(ws, run_id)["handoff"])["title"]
        message = f"Apply run {run_id}: {title}"[:MAX_MESSAGE]
        result = self._node(ws).workspace("worktree_apply", ws, {"worktree_id": row["id"], "message": message})
        self._set(row["id"], "applied", edit_id=result.get("edit_id"),
                  error=None if result.get("worktree_removed") else "cleanup_incomplete")
        return {"run_id": run_id, **result, "isolation": self.view(self._row(row["id"]))}

    def discard(self, ws: dict, run_id: str) -> dict:
        row = self._settled(ws, run_id)
        if row["state"] in ("discarded", "expired"):
            return {"run_id": run_id, "isolation": self.view(row), "repeated": True}
        if row["state"] == "applied":
            raise BridgeError("The run's worktree was already applied; use undo_edit to revert it",
                              "worktree_unavailable")
        result = self._node(ws).workspace("worktree_remove", ws, {"worktree_id": row["id"]})
        self._set(row["id"], "discarded", error=None if result.get("removed") else "cleanup_incomplete")
        return {"run_id": run_id, "removed": result.get("removed", False),
                "isolation": self.view(self._row(row["id"]))}

    # -- retention ---------------------------------------------------------------

    def apply_retention(self, now: float | None = None) -> int:
        """Remove worktrees unused for 14 days, and ones whose creation was cut off."""
        now = time.time() if now is None else now
        with self.service.lock:
            rows = [dict(row) for row in self.service.db.execute(
                "SELECT * FROM agent_worktrees WHERE state IN ('creating','active') OR "
                "(state='failed' AND error IS NOT 'removed')")]
        removed = 0
        for row in rows:
            with self.service.lock:
                linked = self.service.db.execute(
                    "SELECT 1 FROM agent_run_worktrees WHERE worktree=? LIMIT 1", (row["id"],)).fetchone()
                busy = self.service.db.execute(
                    "SELECT 1 FROM agent_runs r JOIN agent_run_worktrees w ON w.run=r.id "
                    "WHERE w.worktree=? AND r.phase IN ('starting','active')", (row["id"],)).fetchone()
            # An active worktree with no run is one whose first run never started.
            limit = RETENTION_SECONDS if row["state"] == "active" and linked else STALE_CREATING_SECONDS
            if now - _epoch(row["updated"]) < limit:
                continue
            with self.service.lock:
                try:
                    ws = self.service.workspace(row["workspace"], False)
                except BridgeError:
                    ws = None
            if busy is not None or ws is None or ws["node_id"] != row["node_id"]:
                continue
            try:
                self._node(ws).workspace("worktree_remove", ws, {"worktree_id": row["id"]})
            except Exception:  # noqa: BLE001 - retried on the next pass
                continue
            if row["state"] == "active" and linked:
                self._set(row["id"], "expired")
            else:
                self._set(row["id"], "failed", error="removed")
            removed += 1
        return removed
