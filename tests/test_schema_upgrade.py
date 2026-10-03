"""In-place widening of runtime_type constraints keeps existing state usable."""
from __future__ import annotations

import sqlite3

import pytest

from workspace_bridge.node_service import NodeService
from workspace_bridge.schema_upgrade import widen_runtime_type_check

OLD_NODE_ADAPTERS = """
CREATE TABLE node_adapters (
  adapter_id TEXT PRIMARY KEY, name TEXT NOT NULL,
  runtime_type TEXT NOT NULL CHECK(runtime_type IN ('pi','codex')),
  last_seen TEXT NOT NULL);
CREATE INDEX ix_node_adapters_name ON node_adapters(name);
CREATE TABLE refs (adapter_id TEXT REFERENCES node_adapters(adapter_id));
"""


def _legacy(path):
    db = sqlite3.connect(path)
    db.executescript(OLD_NODE_ADAPTERS)
    db.execute("INSERT INTO node_adapters VALUES('a1','one','codex','t')")
    db.execute("INSERT INTO refs VALUES('a1')")
    db.commit()
    return db


def test_rebuild_preserves_rows_indexes_and_references(tmp_path):
    db = _legacy(tmp_path / "x.sqlite3")
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO node_adapters VALUES('a2','two','claude','t')")
    db.rollback()
    assert widen_runtime_type_check(db, "node_adapters") is True
    db.execute("INSERT INTO node_adapters VALUES('a2','two','claude','t')")
    db.commit()
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO node_adapters VALUES('a3','three','other','t')")
    db.rollback()
    assert db.execute("SELECT adapter_id,runtime_type FROM node_adapters "
                      "ORDER BY adapter_id").fetchall() == [("a1", "codex"), ("a2", "claude")]
    assert db.execute("SELECT name FROM sqlite_master WHERE type='index' "
                      "AND name='ix_node_adapters_name'").fetchone() is not None
    assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    # Idempotent: an already-widened or absent table is left alone.
    assert widen_runtime_type_check(db, "node_adapters") is False
    assert widen_runtime_type_check(db, "missing_table") is False


def test_invalid_table_name_is_rejected(tmp_path):
    db = sqlite3.connect(tmp_path / "y.sqlite3")
    with pytest.raises(ValueError):
        widen_runtime_type_check(db, "x; DROP TABLE y")


def test_node_state_created_before_claude_is_upgraded_on_open(tmp_path):
    state = tmp_path / "node"
    root = tmp_path / "allowed"
    root.mkdir()
    config = {"allowed_roots": [str(root)], "node_token_hash": "x" * 64}
    service = NodeService(state, config)
    service.close()
    db = sqlite3.connect(state / "node.sqlite3")
    sql = db.execute("SELECT sql FROM sqlite_master WHERE name='runtime_adapters'").fetchone()[0]
    assert "'claude'" in sql
    # Rewrite the table as an older release created it.
    db.executescript(f"""
      ALTER TABLE runtime_adapters RENAME TO legacy_adapters;
      {sql.replace("'pi','codex','claude'", "'pi','codex'").replace(
          "CREATE TABLE runtime_adapters", "CREATE TABLE runtime_adapters")};
      INSERT INTO runtime_adapters SELECT * FROM legacy_adapters;
      DROP TABLE legacy_adapters;
    """)
    db.commit()
    db.close()
    reopened = NodeService(state, config)
    try:
        sql = reopened.db.execute(
            "SELECT sql FROM sqlite_master WHERE name='runtime_adapters'").fetchone()[0]
        assert "'claude'" in sql
    finally:
        reopened.close()
