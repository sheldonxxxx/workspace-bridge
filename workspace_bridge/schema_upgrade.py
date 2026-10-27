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


PLUGIN_ADAPTER_COLUMNS = (
    ("kind", "TEXT NOT NULL DEFAULT 'http' CHECK(kind IN ('http','plugin'))"),
    ("plugin", "TEXT NOT NULL DEFAULT ''"),
    ("projects_root", "TEXT NOT NULL DEFAULT ''"),
    ("settings", "TEXT NOT NULL DEFAULT '{}'"),
)


def add_plugin_adapters(db: sqlite3.Connection) -> None:
    """Let a Node adapter be a supervised stdio plugin instead of an HTTP URL."""
    columns = {row[1] for row in db.execute("PRAGMA table_info(runtime_adapters)")}
    with db:
        for name, definition in PLUGIN_ADAPTER_COLUMNS:
            if name not in columns:
                db.execute(f"ALTER TABLE runtime_adapters ADD COLUMN {name} {definition}")


SECURITY_PROFILES_SQL = """
CREATE TABLE IF NOT EXISTS security_profiles (
  id TEXT PRIMARY KEY, rules TEXT NOT NULL, revision TEXT NOT NULL,
  created TEXT NOT NULL, updated TEXT NOT NULL)"""
_OLD_SOURCE_CHECK = "CHECK(security_source IN ('profile','runtime-config'))"
_NEW_SOURCE_CHECK = "CHECK(security_source IN ('profile','native'))"
# Agent-owned profile names from before profiles moved to the Bridge.
_LEGACY_PROFILE_NAMES = {"read-only": "read-only", "workspace-write-reviewed": "reviewed"}


def unify_security_profiles(db: sqlite3.Connection) -> None:
    """Move route security onto Bridge-owned profiles.

    Adds the profile table and each adapter's enforcement capabilities, and
    rebuilds ``workspace_routes`` so its source is ``profile`` or ``native``.
    Routes on a former agent-owned custom profile lose their binding and are
    disabled until a Bridge profile is chosen; the two former built-ins map
    onto their Bridge equivalents.
    """
    with db:
        db.execute(SECURITY_PROFILES_SQL)
        columns = {row[1] for row in db.execute("PRAGMA table_info(node_adapters)")}
        if columns and "security" not in columns:
            db.execute("ALTER TABLE node_adapters ADD COLUMN security TEXT NOT NULL DEFAULT '{}'")
    row = db.execute("SELECT sql FROM sqlite_master WHERE type='table' "
                     "AND name='workspace_routes'").fetchone()
    sql = row[0] if row is not None else None
    if not isinstance(sql, str) or _OLD_SOURCE_CHECK not in sql:
        return
    indexes = [item[0] for item in db.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name='workspace_routes' "
        "AND sql IS NOT NULL")]
    create = sql.replace(_OLD_SOURCE_CHECK, _NEW_SOURCE_CHECK, 1).replace(
        "CREATE TABLE workspace_routes", "CREATE TABLE workspace_routes_upgrade", 1)
    db.commit()
    db.execute("PRAGMA foreign_keys=OFF")
    try:
        db.execute("BEGIN IMMEDIATE")
        try:
            db.execute(create)
            db.execute("INSERT INTO workspace_routes_upgrade SELECT * FROM workspace_routes")
            db.execute("UPDATE workspace_routes_upgrade SET security_source='native',"
                       "profile_id='',profile_revision='' WHERE security_source='runtime-config'")
            for old, new in _LEGACY_PROFILE_NAMES.items():
                db.execute("UPDATE workspace_routes_upgrade SET profile_id=?,profile_revision='' "
                           "WHERE security_source='profile' AND profile_id=?", (new, old))
            db.execute("UPDATE workspace_routes_upgrade SET security_source=NULL,profile_id=NULL,"
                       "profile_revision=NULL,enabled=0,is_default=0 WHERE security_source='profile' "
                       "AND profile_id NOT IN (" + ",".join("?" * len(_LEGACY_PROFILE_NAMES)) + ")",
                       tuple(_LEGACY_PROFILE_NAMES.values()))
            db.execute("DROP TABLE workspace_routes")
            db.execute("ALTER TABLE workspace_routes_upgrade RENAME TO workspace_routes")
            for statement in indexes:
                db.execute(statement)
            if db.execute("PRAGMA foreign_key_check").fetchall():
                raise sqlite3.IntegrityError("Route security upgrade broke a reference")
            db.commit()
        except BaseException:
            db.rollback()
            raise
    finally:
        db.execute("PRAGMA foreign_keys=ON")


INTERACTION_DECISION_COLUMNS = ("decided_by", "reason", "decided")


def add_interaction_decisions(db: sqlite3.Connection) -> None:
    """Record who answered each interaction, why, and when, if missing."""
    columns = {row[1] for row in db.execute("PRAGMA table_info(agent_interactions)")}
    with db:
        for column in INTERACTION_DECISION_COLUMNS:
            if columns and column not in columns:
                db.execute(f"ALTER TABLE agent_interactions ADD COLUMN {column} TEXT")


def store_handoffs_in_bridge(db: sqlite3.Connection) -> None:
    """Keep handoff text in Bridge state and drop the handoff-only write scope.

    Handoffs published by 0.3 keep ``content`` NULL and can no longer be
    read. ``plan_folder`` records where the Node keeps a handoff's read-only
    copy, and ``uploads`` the uploaded images it copies there.
    """
    columns = {row[1] for row in db.execute("PRAGMA table_info(jobs)")}
    scoped = bool(list(db.execute("PRAGMA table_info(workspaces)")))
    with db:
        if columns and "content" not in columns:
            db.execute("ALTER TABLE jobs ADD COLUMN content TEXT")
        if columns and "plan_folder" not in columns:
            db.execute("ALTER TABLE jobs ADD COLUMN plan_folder TEXT")
        if columns and "uploads" not in columns:
            db.execute("ALTER TABLE jobs ADD COLUMN uploads TEXT")
        if scoped:
            db.execute("UPDATE workspaces SET write_scope='none' WHERE write_scope='handoff'")
