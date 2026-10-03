"""In-place SQLite upgrades that keep existing Bridge and Node state usable."""
from __future__ import annotations

import re
import sqlite3

_OLD_CHECK = "CHECK(runtime_type IN ('pi','codex'))"
_NEW_CHECK = "CHECK(runtime_type IN ('pi','codex','claude'))"
_TABLE_NAME_RE = re.compile(r"^[a-z_]{1,64}$")


def widen_runtime_type_check(db: sqlite3.Connection, table: str) -> bool:
    """Allow the ``claude`` runtime type in a table's ``runtime_type`` CHECK.

    SQLite cannot alter a CHECK constraint, so the table is rebuilt with the
    documented create/copy/drop/rename procedure inside one transaction with
    foreign-key enforcement suspended. Rows, indexes and references by name are
    preserved. Returns True only when a rebuild happened; a table that is
    absent or already allows ``claude`` is left untouched.
    """
    if not _TABLE_NAME_RE.fullmatch(table):
        raise ValueError("Invalid table name")
    row = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                     (table,)).fetchone()
    sql = row[0] if row is not None else None
    if not isinstance(sql, str) or _OLD_CHECK not in sql:
        return False
    indexes = [item[0] for item in db.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=? "
        "AND sql IS NOT NULL", (table,))]
    temporary = table + "_upgrade"
    create = sql.replace(_OLD_CHECK, _NEW_CHECK, 1).replace(
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
