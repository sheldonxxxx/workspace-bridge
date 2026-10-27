"""Atomic multi-file patches and the Node's edit journal.

A patch is checked in full first: every path against the write policy, every
existing file against its hash, every new text against the binary and secret
checks. The before-content of each touched file is then journaled durably,
each new file is staged next to its target, and only then are the targets
switched. A failed switch puts back the files already switched, so all
changes apply or none do.

Like single-file writes, this is an application boundary, not a filesystem
transaction: a process that edits a file between the last check and the
switch can still race it. The final signature check narrows that window.
"""
from __future__ import annotations

import os
import re
import secrets
import sqlite3
import stat
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .security import (BridgeError, MAX_FILE, MAX_WRITE, SafeRoot, digest, file_text,
                       parts, require_write_path, text_content)

MAX_PATCH_FILES = 50
MAX_PATCH_OPS = 200
MAX_PATCH_BYTES = 1024 * 1024
MAX_MESSAGE = 500
RETENTION_DAYS = 30
RETENTION_BYTES = 500 * 1024 * 1024
EDIT_ID = re.compile(r"edit_[0-9a-f]{24}")
HASH = re.compile(r"[0-9a-f]{64}")
OPS = {
    "edit": frozenset({"op", "path", "sha256", "old", "new"}),
    "create": frozenset({"op", "path", "content"}),
    "move": frozenset({"op", "path", "to", "sha256"}),
    "delete": frozenset({"op", "path", "sha256"}),
}
ACTORS = frozenset({"chatgpt"})


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _signature(st: os.stat_result) -> tuple:
    return (st.st_dev, st.st_ino, st.st_mode, st.st_nlink, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


@dataclass
class FileChange:
    """One path's move from ``before`` to ``after``; None means the file is absent."""
    path: str
    op: str
    before: bytes | None
    before_mode: int | None
    signature: tuple | None
    after: bytes | None
    mode: int | None

    def public(self) -> dict:
        return {"path": self.path, "op": self.op,
                "previous_sha256": None if self.before is None else digest(self.before),
                "sha256": None if self.after is None else digest(self.after)}


@dataclass
class _Current:
    data: bytes | None
    mode: int | None
    signature: tuple | None


def _lstat(safe: SafeRoot, path: str) -> os.stat_result | None:
    ps = parts(path)
    try:
        with safe.directory(ps[:-1]) as parent:
            try:
                return os.stat(ps[-1], dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                return None
    except BridgeError:
        # A missing parent means the file is absent. An unsafe parent fails
        # later, when staging opens it without following links.
        return None


def _read_current(safe: SafeRoot, path: str, limit: int) -> _Current:
    st = _lstat(safe, path)
    if st is None:
        return _Current(None, None, None)
    if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_dev != safe.identity[0]:
        raise BridgeError(f"Only single-link regular files may be changed: {path}")
    data, observed = safe.read(path, limit=limit)
    if _signature(st) != _signature(observed):
        raise BridgeError(f"{path} changed while it was read; retry", "workspace_busy")
    return _Current(data, stat.S_IMODE(st.st_mode), _signature(st))


def _require_hash(value: object) -> str:
    if not isinstance(value, str) or not HASH.fullmatch(value):
        raise BridgeError("Invalid sha256", "invalid_arguments")
    return value


def _check_shape(changes: object) -> int:
    """Validate the request shape and return the bytes of new text it carries."""
    if not isinstance(changes, list) or not 1 <= len(changes) <= MAX_PATCH_OPS:
        raise BridgeError(f"A patch needs 1 to {MAX_PATCH_OPS} changes", "invalid_arguments")
    size = 0
    for change in changes:
        if not isinstance(change, dict) or change.get("op") not in OPS or set(change) != OPS[change["op"]]:
            raise BridgeError("Invalid patch change", "invalid_arguments")
        for key in ("path", "to", "old", "new", "content"):
            if key in change and not isinstance(change[key], str):
                raise BridgeError(f"Patch field {key} must be text", "invalid_arguments")
        if "sha256" in change:
            _require_hash(change["sha256"])
        try:
            size += sum(len(change[key].encode("utf-8")) for key in ("old", "new", "content") if key in change)
        except UnicodeEncodeError:
            raise BridgeError("Content must be valid UTF-8 text", "invalid_arguments") from None
    if size > MAX_PATCH_BYTES:
        raise BridgeError("A patch may carry at most 1 MiB of text", "too_large")
    return size


def compile_patch(safe: SafeRoot, changes: list, excludes: list[str], scope: str) -> list[FileChange]:
    """Check a whole patch against the current files without changing anything."""
    _check_shape(changes)
    plan: dict[str, FileChange] = {}
    folded: dict[str, str] = {}
    original: dict[str, str] = {}

    def claim(path: str):
        require_write_path(path, excludes, scope=scope)
        other = folded.setdefault(path.casefold(), path)
        if path in plan or other != path:
            raise BridgeError(f"A patch may change {path} only once (several edits to one file are allowed)",
                              "invalid_arguments")

    for change in changes:
        op, path = change["op"], change["path"]
        if op == "edit":
            if not change["old"]:
                raise BridgeError(f"old must be nonempty for {path}", "invalid_arguments")
            if path in plan and plan[path].op == "edit":
                if change["sha256"] != original[path]:
                    raise BridgeError(f"Every edit to {path} must give the same original sha256",
                                      "invalid_arguments")
                item = plan[path]
            else:
                claim(path)
                current = _read_current(safe, path, MAX_WRITE)
                if current.data is None:
                    raise BridgeError(f"{path} is missing; re-read before editing", "stale_evidence")
                if digest(current.data) != change["sha256"]:
                    raise BridgeError(f"{path} changed; re-read before editing", "stale_evidence")
                text_content(current.data)
                item = FileChange(path, "edit", current.data, current.mode, current.signature,
                                  current.data, current.mode)
                plan[path], original[path] = item, change["sha256"]
            text = item.after.decode("utf-8")
            first = text.find(change["old"])
            if first < 0:
                raise BridgeError(f"old text was not found in {path}; re-read and use exact text",
                                  "match_not_found")
            if text.find(change["old"], first + 1) >= 0:
                raise BridgeError(f"old text is ambiguous in {path}; include more surrounding text",
                                  "ambiguous_match")
            item.after = (text[:first] + change["new"] + text[first + len(change["old"]):]).encode("utf-8")
        elif op == "create":
            claim(path)
            if _lstat(safe, path) is not None:
                raise BridgeError(f"{path} already exists; read it and edit it instead", "conflict")
            plan[path] = FileChange(path, "create", None, None, None, change["content"].encode("utf-8"), 0o600)
        else:
            claim(path)
            current = _read_current(safe, path, MAX_FILE)
            if current.data is None:
                raise BridgeError(f"{path} is missing; re-read before changing it", "stale_evidence")
            if digest(current.data) != change["sha256"]:
                raise BridgeError(f"{path} changed; re-read before changing it", "stale_evidence")
            if op == "delete":
                plan[path] = FileChange(path, "delete", current.data, current.mode, current.signature, None, None)
            else:
                target = change["to"]
                claim(target)
                if _lstat(safe, target) is not None:
                    raise BridgeError(f"{target} already exists; a move never overwrites", "conflict")
                plan[path] = FileChange(path, "move_from", current.data, current.mode, current.signature,
                                        None, None)
                plan[target] = FileChange(target, "move_to", None, None, None, current.data, current.mode)
    if len(plan) > MAX_PATCH_FILES:
        raise BridgeError(f"A patch may touch at most {MAX_PATCH_FILES} files", "too_large")
    for item in plan.values():
        if item.op in ("edit", "create"):
            # Matches the file already holds may stay; new ones are refused.
            file_text(item.after, item.before)
    return list(plan.values())


@dataclass
class _Staged:
    name: str
    parent: tuple[int, int]


def _write_all(fd: int, data: bytes):
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("Write made no progress")
        view = view[written:]


def _stage(safe: SafeRoot, path: str, data: bytes, mode: int) -> _Staged:
    ps = parts(path)
    with safe.directory(ps[:-1], create=True) as parent:
        name = ".wb-write-" + secrets.token_hex(16)
        try:
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                         0o600, dir_fd=parent)
        except OSError:
            raise BridgeError(f"Could not stage {path}", "write_failed") from None
        try:
            try:
                _write_all(fd, data)
                # Ordinary permission bits only, never setuid/setgid/sticky.
                os.fchmod(fd, mode & 0o777)
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError:
            _unlink_quietly(parent, name)
            raise BridgeError(f"Could not stage {path}", "write_failed") from None
        st = os.fstat(parent)
        return _Staged(name, (st.st_dev, st.st_ino))


def _unlink_quietly(parent: int, name: str):
    try:
        os.unlink(name, dir_fd=parent)
    except OSError:
        pass


def _discard(safe: SafeRoot, path: str, staged: _Staged):
    try:
        with safe.directory(parts(path)[:-1]) as parent:
            _unlink_quietly(parent, staged.name)
    except BridgeError:
        pass


def _switch(safe: SafeRoot, item: FileChange, staged: _Staged | None):
    ps = parts(item.path)
    with safe.directory(ps[:-1]) as parent:
        if staged is not None:
            st = os.fstat(parent)
            if (st.st_dev, st.st_ino) != staged.parent:
                raise BridgeError(f"The folder of {item.path} changed; re-read", "stale_evidence")
        try:
            if item.before is None:
                try:
                    os.link(staged.name, ps[-1], src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
                except FileExistsError:
                    raise BridgeError(f"{item.path} was created meanwhile; re-read", "conflict") from None
                os.unlink(staged.name, dir_fd=parent)
            else:
                try:
                    current = os.stat(ps[-1], dir_fd=parent, follow_symlinks=False)
                except FileNotFoundError:
                    current = None
                if current is None or _signature(current) != item.signature:
                    raise BridgeError(f"{item.path} changed after it was checked; re-read", "stale_evidence")
                if item.after is None:
                    os.unlink(ps[-1], dir_fd=parent)
                else:
                    os.replace(staged.name, ps[-1], src_dir_fd=parent, dst_dir_fd=parent)
            os.fsync(parent)
        except OSError:
            raise BridgeError(f"Could not switch {item.path}", "write_failed") from None


def _put_back(safe: SafeRoot, item: FileChange) -> bool:
    """Undo one switched change if the file still holds what the patch wrote."""
    try:
        current = _read_current(safe, item.path, MAX_FILE)
        if (None if current.data is None else digest(current.data)) != (
                None if item.after is None else digest(item.after)):
            return False
        reverse = FileChange(item.path, "restore", item.after, current.mode, current.signature,
                             item.before, item.before_mode)
        staged = None if item.before is None else _stage(safe, item.path, item.before, item.before_mode or 0o600)
        try:
            _switch(safe, reverse, staged)
        except BridgeError:
            if staged is not None:
                _discard(safe, item.path, staged)
            raise
        return True
    except BridgeError:
        return False


def missing_folders(safe: SafeRoot, plan: list[FileChange]) -> list[str]:
    """Folders that staging the plan's new files will create, parents first."""
    missing: list[str] = []
    for item in plan:
        if item.after is None:
            continue
        ps = parts(item.path)
        for depth in range(1, len(ps)):
            folder = "/".join(ps[:depth])
            if folder not in missing and _lstat(safe, folder) is None:
                missing.append(folder)
    return missing


def remove_empty_folders(safe: SafeRoot, folders: list[str]):
    """Remove folders a patch created, deepest first, while they are empty."""
    for folder in sorted(folders, key=lambda value: value.count("/"), reverse=True):
        ps = parts(folder)
        try:
            with safe.directory(ps[:-1]) as parent:
                os.rmdir(ps[-1], dir_fd=parent)
        except (BridgeError, OSError):
            pass


def apply_plan(safe: SafeRoot, plan: list[FileChange]) -> None:
    """Switch every change or none.

    A failure that rollback could not fully undo carries the affected paths in ``stuck``.
    """
    staged: dict[str, _Staged] = {}
    try:
        for item in plan:
            if item.after is not None:
                staged[item.path] = _stage(safe, item.path, item.after, item.mode or 0o600)
        done: list[FileChange] = []
        try:
            for item in plan:
                _switch(safe, item, staged.get(item.path))
                staged.pop(item.path, None)
                done.append(item)
        except BridgeError as exc:
            stuck = [item.path for item in reversed(done) if not _put_back(safe, item)]
            if stuck:
                exc.stuck = stuck
            raise
    finally:
        for path, entry in staged.items():
            _discard(safe, path, entry)


class EditJournal:
    """Before-content of every patched file, kept per workspace for undo."""

    def __init__(self, path: Path):
        self.lock = threading.Lock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        os.chmod(path, 0o600)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        with self.db:
            self.db.executescript("""
              PRAGMA journal_mode=WAL;
              CREATE TABLE IF NOT EXISTS edits (
                id TEXT PRIMARY KEY, workspace TEXT NOT NULL, actor TEXT NOT NULL,
                message TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('patch','undo')),
                undoes TEXT, undone_by TEXT,
                state TEXT NOT NULL CHECK(state IN ('applying','applied','interrupted','failed')),
                bytes INTEGER NOT NULL, created TEXT NOT NULL, updated TEXT NOT NULL);
              CREATE INDEX IF NOT EXISTS edits_workspace ON edits(workspace, created);
              CREATE TABLE IF NOT EXISTS edit_files (
                edit TEXT NOT NULL REFERENCES edits(id) ON DELETE CASCADE,
                seq INTEGER NOT NULL, path TEXT NOT NULL, op TEXT NOT NULL,
                before BLOB, before_sha256 TEXT, before_mode INTEGER, after_sha256 TEXT,
                PRIMARY KEY(edit, seq));
            """)
            # Only this process applies patches; one still applying when the
            # journal opens was cut off by a crash or restart.
            self.db.execute("UPDATE edits SET state='interrupted' WHERE state='applying'")

    def close(self):
        self.db.close()

    def begin(self, workspace: str, actor: str, message: str, kind: str,
              plan: list[FileChange], folders: list[str], undoes: str | None = None) -> str:
        edit_id = "edit_" + secrets.token_hex(12)
        stamp = _now().isoformat()
        size = sum(len(item.before or b"") for item in plan)
        with self.lock, self.db:
            self.db.execute(
                "INSERT INTO edits(id,workspace,actor,message,kind,undoes,undone_by,state,bytes,created,updated) "
                "VALUES(?,?,?,?,?,?,NULL,'applying',?,?,?)",
                (edit_id, workspace, actor, message, kind, undoes, size, stamp, stamp))
            self.db.executemany(
                "INSERT INTO edit_files(edit,seq,path,op,before,before_sha256,before_mode,after_sha256) "
                "VALUES(?,?,?,?,?,?,?,?)",
                [(edit_id, seq, item.path, item.op, item.before,
                  None if item.before is None else digest(item.before), item.before_mode,
                  None if item.after is None else digest(item.after)) for seq, item in enumerate(plan)]
                + [(edit_id, len(plan) + seq, folder, "mkdir", None, None, None, None)
                   for seq, folder in enumerate(folders)])
            self._prune(workspace, edit_id)
        return edit_id

    def _prune(self, workspace: str, keep: str):
        cutoff = (_now() - timedelta(days=RETENTION_DAYS)).isoformat()
        self.db.execute("DELETE FROM edits WHERE workspace=? AND created<? AND id<>?", (workspace, cutoff, keep))
        total = self.db.execute("SELECT COALESCE(SUM(bytes),0) FROM edits WHERE workspace=?",
                                (workspace,)).fetchone()[0]
        for row in self.db.execute("SELECT id,bytes FROM edits WHERE workspace=? AND id<>? "
                                   "ORDER BY created,id", (workspace, keep)).fetchall():
            if total <= RETENTION_BYTES:
                break
            self.db.execute("DELETE FROM edits WHERE id=?", (row["id"],))
            total -= row["bytes"]

    def finish(self, edit_id: str, state: str):
        with self.lock, self.db:
            self.db.execute("UPDATE edits SET state=?,updated=? WHERE id=?", (state, _now().isoformat(), edit_id))

    def discard(self, edit_id: str):
        with self.lock, self.db:
            self.db.execute("DELETE FROM edits WHERE id=?", (edit_id,))

    def mark_undone(self, edit_id: str, by: str):
        with self.lock, self.db:
            self.db.execute("UPDATE edits SET undone_by=?,updated=? WHERE id=?", (by, _now().isoformat(), edit_id))

    def entry(self, workspace: str, edit_id: str) -> tuple[dict, list[dict]]:
        with self.lock:
            row = self.db.execute("SELECT * FROM edits WHERE id=? AND workspace=?", (edit_id, workspace)).fetchone()
            if row is None:
                raise BridgeError("Edit not found in this workspace's journal; it may have expired", "not_found")
            files = [dict(item) for item in self.db.execute(
                "SELECT * FROM edit_files WHERE edit=? ORDER BY seq", (edit_id,))]
        return dict(row), files

    @staticmethod
    def _public(row: dict, files: list[dict]) -> dict:
        return {"edit_id": row["id"], "actor": row["actor"], "message": row["message"], "kind": row["kind"],
                "undoes": row["undoes"], "undone_by": row["undone_by"], "state": row["state"],
                "created": row["created"],
                "files": [{"path": item["path"], "op": item["op"], "previous_sha256": item["before_sha256"],
                           "sha256": item["after_sha256"]} for item in files if item["op"] != "mkdir"],
                "created_folders": [item["path"] for item in files if item["op"] == "mkdir"]}

    def list(self, workspace: str, offset: int = 0, limit: int = 50,
             since: str | None = None, until: str | None = None) -> dict:
        """Newest first; ``since``/``until`` bound ``created`` to a run's window."""
        if not isinstance(offset, int) or not isinstance(limit, int) or offset < 0 or not 1 <= limit <= 100:
            raise BridgeError("Invalid edit journal page", "invalid_arguments")
        bounds = []
        for value in (since, until):
            if value is not None:
                try:
                    stamp = datetime.fromisoformat(value)
                except (TypeError, ValueError):
                    raise BridgeError("Invalid edit journal time bound", "invalid_arguments") from None
                if stamp.tzinfo is None:
                    raise BridgeError("Edit journal time bounds need a time zone", "invalid_arguments")
                value = stamp.astimezone(timezone.utc).isoformat()
            bounds.append(value)
        with self.lock:
            rows = [dict(row) for row in self.db.execute(
                "SELECT * FROM edits WHERE workspace=? AND created>=COALESCE(?,'') "
                "AND created<=COALESCE(?,'9999') ORDER BY created DESC,id DESC LIMIT ? OFFSET ?",
                (workspace, *bounds, limit + 1, offset))]
            items = [self._public(row, [dict(item) for item in self.db.execute(
                "SELECT path,op,before_sha256,after_sha256 FROM edit_files WHERE edit=? ORDER BY seq",
                (row["id"],))]) for row in rows[:limit]]
        return {"edits": items, "next_offset": offset + limit if len(rows) > limit else None,
                "retention": {"days": RETENTION_DAYS, "bytes_per_workspace": RETENTION_BYTES}}


def _check_message(actor: str, message: object) -> str:
    if actor not in ACTORS:
        raise BridgeError("Invalid edit actor", "invalid_arguments")
    if not isinstance(message, str):
        raise BridgeError("A patch needs a message", "invalid_arguments")
    message = " ".join(message.split())
    if not 1 <= len(message) <= MAX_MESSAGE:
        raise BridgeError(f"A patch message needs 1 to {MAX_MESSAGE} characters", "invalid_arguments")
    return message


def _commit(safe: SafeRoot, journal: EditJournal, workspace: str, actor: str, message: str,
            kind: str, plan: list[FileChange], undoes: str | None = None) -> tuple[str, list[str]]:
    folders = missing_folders(safe, plan)
    edit_id = journal.begin(workspace, actor, message, kind, plan, folders, undoes)
    try:
        apply_plan(safe, plan)
    except BridgeError as exc:
        remove_empty_folders(safe, folders)
        stuck = getattr(exc, "stuck", None)
        if stuck:
            journal.finish(edit_id, "failed")
            raise BridgeError(f"{exc} Rolling back left these files as the patch wrote them: "
                              f"{', '.join(stuck[:20])}. Journal entry {edit_id} holds their earlier content.",
                              "write_failed") from None
        journal.discard(edit_id)
        raise
    journal.finish(edit_id, "applied")
    return edit_id, folders


def apply_patch(safe: SafeRoot, journal: EditJournal, workspace: dict, changes: list,
                message: object, actor: str = "chatgpt") -> dict:
    message = _check_message(actor, message)
    scope = workspace.get("write_scope", "none")
    plan = compile_patch(safe, changes, workspace.get("excludes", []), scope)
    edit_id, folders = _commit(safe, journal, workspace["id"], actor, message, "patch", plan)
    return {"edit_id": edit_id, "message": message, "files": [item.public() for item in plan],
            "created_folders": folders,
            "undo": "undo_edit restores these files while they still match the new hashes."}


def undo_edit(safe: SafeRoot, journal: EditJournal, workspace: dict, edit_id: object,
              actor: str = "chatgpt") -> dict:
    if not isinstance(edit_id, str) or not EDIT_ID.fullmatch(edit_id):
        raise BridgeError("Invalid edit_id", "invalid_arguments")
    row, files = journal.entry(workspace["id"], edit_id)
    if row["undone_by"]:
        raise BridgeError(f"Edit already undone by {row['undone_by']}", "already_undone")
    if row["state"] not in ("applied", "interrupted"):
        raise BridgeError("Only an applied edit can be undone", "conflict")
    scope, excludes = workspace.get("write_scope", "none"), workspace.get("excludes", [])
    plan, changed = [], []
    folders = [item["path"] for item in files if item["op"] == "mkdir"]
    for item in files:
        if item["op"] == "mkdir":
            continue
        require_write_path(item["path"], excludes, scope=scope)
        current = _read_current(safe, item["path"], MAX_FILE)
        observed = None if current.data is None else digest(current.data)
        if observed == item["after_sha256"]:
            before = item["before"]
            op = "create" if current.data is None else ("delete" if before is None else "restore")
            plan.append(FileChange(item["path"], op, current.data, current.mode, current.signature,
                                   before, item["before_mode"]))
        elif row["state"] == "interrupted" and observed == item["before_sha256"]:
            continue  # The interrupted patch never reached this file.
        else:
            changed.append(item["path"])
    if changed:
        shown = ", ".join(changed[:20]) + (f" and {len(changed) - 20} more" if len(changed) > 20 else "")
        raise BridgeError(f"Files changed since this edit, so nothing was undone: {shown}. "
                          "Re-read them and fix by hand or with a new patch.", "undo_conflict")
    if not plan:
        raise BridgeError("Nothing to undo; the files already hold their earlier content", "conflict")
    undo_id, created = _commit(safe, journal, workspace["id"], actor,
                               f"Undo {edit_id}: {row['message']}"[:MAX_MESSAGE], "undo", plan, undoes=edit_id)
    journal.mark_undone(edit_id, undo_id)
    remove_empty_folders(safe, folders)
    return {"edit_id": undo_id, "undid": edit_id, "files": [item.public() for item in plan],
            "created_folders": created}
