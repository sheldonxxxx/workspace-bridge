"""In-place SQLite upgrades that keep existing Bridge and Node state usable."""
from __future__ import annotations

import re
import sqlite3

_OLD_CHECKS = ("CHECK(runtime_type IN ('pi','codex'))",
               "CHECK(runtime_type IN ('pi','codex','claude'))")
_NEW_CHECK = "CHECK(runtime_type IN ('pi','codex','claude','opencode'))"
_TABLE_NAME_RE = re.compile(r"^[a-z_]{1,64}$")


def widen_runtime_type_check(db: sqlite3.Connection, table: str) -> bool:
    """Allow current runtime types in a table's ``runtime_type`` CHECK.

    SQLite cannot alter a CHECK constraint, so the table is rebuilt with the
    documented create/copy/drop/rename procedure inside one transaction with
    foreign-key enforcement suspended. Rows, indexes and references by name are
    preserved. Returns True only when a rebuild happened; a table that is
    absent or already allows ``opencode`` is left untouched.
    """
    if not _TABLE_NAME_RE.fullmatch(table):
        raise ValueError("Invalid table name")
    row = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                     (table,)).fetchone()
    sql = row[0] if row is not None else None
    old_check = next((item for item in _OLD_CHECKS if isinstance(sql, str)
                      and item in sql), None)
    if old_check is None:
        return False
    indexes = [item[0] for item in db.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=? "
        "AND sql IS NOT NULL", (table,))]
    temporary = table + "_upgrade"
    create = sql.replace(old_check, _NEW_CHECK, 1).replace(
        f"CREATE TABLE {table}", f"CREATE TABLE {temporary}", 1)
    if f"CREATE TABLE {temporary}" not in create:
        return False
    db.commit()
    db.execute("PRAGMA foreign_keys=OFF")
    try:
        db.execute("BEGIN IMMEDIATE")
        try:
            db.execute(create)
            db.execute(f"INSERT INTO {temporary} SELECT * FROM {table}")
            db.execute(f"DROP TABLE {table}")
            db.execute(f"ALTER TABLE {temporary} RENAME TO {table}")
            for statement in indexes:
                db.execute(statement)
            violations = db.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise sqlite3.IntegrityError("Runtime type upgrade broke a reference")
            db.commit()
        except BaseException:
            db.rollback()
            raise
    finally:
        db.execute("PRAGMA foreign_keys=ON")
    return True


NODE_PAIRINGS_SQL = """
CREATE TABLE IF NOT EXISTS node_pairings (
  id TEXT PRIMARY KEY, code_hash TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
  replace_node TEXT, created TEXT NOT NULL, expires TEXT NOT NULL,
  used TEXT, node_id TEXT)"""


def add_node_transports(db: sqlite3.Connection) -> None:
    """Add Node transport and key columns plus the pairing table, if missing.

    Purely additive: existing Nodes keep the ``http`` transport and their
    token, so an upgraded Bridge keeps working with unchanged Nodes.
    """
    columns = {row[1] for row in db.execute("PRAGMA table_info(nodes)")}
    with db:
        if "transport" not in columns:
            db.execute("ALTER TABLE nodes ADD COLUMN transport TEXT NOT NULL DEFAULT 'http' "
                       "CHECK(transport IN ('http','paired','local'))")
        if "public_key" not in columns:
            db.execute("ALTER TABLE nodes ADD COLUMN public_key TEXT NOT NULL DEFAULT ''")
        # Paired Node connection state, written by the running Bridge's
        # gateway so a separate Doctor process can report it.
        for column in ("connected_at", "last_seen", "disconnected_at"):
            if column not in columns:
                db.execute(f"ALTER TABLE nodes ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")
        db.execute(NODE_PAIRINGS_SQL)
