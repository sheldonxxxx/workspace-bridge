"""Node-owned run checkpoints in a private shadow Git store.

The Node reads workspace files through SafeRoot, with the same exclusions and
per-file limit as ``read_file``, and streams them into one bare repository per
workspace under its own state directory. Git writes only that shadow store.
The workspace and its ``.git`` are never written; a Git workspace is consulted
only through fixed read-only ``ls-files`` listings with optional locks off.
Checkpoints are evidence of what changed during a time window, not authorship.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from .git_evidence import MAX_DIFF_OUTPUT, MAX_DIFF_PATHS, MAX_PAGE_BYTES, GitEvidence, _git_binary, _run, page_patch
from .security import MAX_FILE, BridgeError, SafeRoot, allowed

DEFAULT_MAX_FILES = 10000
DEFAULT_MAX_BYTES = 256 * 1024 * 1024
MAX_ENTRIES = 50000
MAX_SKIP_PATHS = 2000
MAX_LISTING_OUTPUT = 16 * 1024 * 1024
RACY_NS = 2_000_000_000
SHADOW_TIMEOUT = 30.0
GC_TIMEOUT = 120.0
_CHECKPOINT_ID = re.compile(r"ckp_[0-9a-f]{24}")
_WORKSPACE_ID = re.compile(r"ws_[0-9a-f]{24}")
_OID = re.compile(r"[0-9a-f]{40}")
_COMMITTER = "Workspace Bridge <checkpoint@workspace-bridge.invalid>"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _blob_oid(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def _quote(path: str) -> bytes:
    """C-style quote one path for a fast-import file command."""
    raw = os.fsencode(path)
    return b'"' + raw.replace(b"\\", b"\\\\").replace(b'"', b'\\"') + b'"'


def _placeholder(reason: str, size: int, mtime_ns: int) -> bytes:
    # A deterministic stand-in keeps an uncaptured file present in the tree,
    # so a run diff shows it as changed instead of deleted.
    return (f"[workspace-bridge] content not captured: {reason}; "
            f"size={size}; mtime_ns={mtime_ns}\n").encode()


def _validate_id(value, pattern: re.Pattern, label: str) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise BridgeError(f"Invalid {label}", "invalid_arguments")
    return value


class CheckpointStore:
    """Capture, compare and purge checkpoints for workspaces on one Node."""

    def __init__(self, state: Path, db, lock):
        self.root = state / "checkpoints"
        self.db = db
        self.lock = lock
        self._workspace_locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        with self.lock, self.db:
            self.db.executescript("""
              CREATE TABLE IF NOT EXISTS checkpoints (
                id TEXT PRIMARY KEY, workspace TEXT NOT NULL, commit_oid TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('complete','partial')),
                files INTEGER NOT NULL, bytes INTEGER NOT NULL,
                skipped TEXT NOT NULL, created TEXT NOT NULL);
              CREATE INDEX IF NOT EXISTS ix_checkpoints_workspace ON checkpoints(workspace);
              CREATE TABLE IF NOT EXISTS checkpoint_manifest (
                workspace TEXT NOT NULL, path TEXT NOT NULL, dev INTEGER NOT NULL,
                ino INTEGER NOT NULL, size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL,
                ctime_ns INTEGER NOT NULL, mode INTEGER NOT NULL, blob TEXT NOT NULL,
                PRIMARY KEY(workspace, path));
            """)

    # -- shadow repository ------------------------------------------------

    def _workspace_lock(self, workspace_id: str) -> threading.Lock:
        with self._locks_guard:
            return self._workspace_locks.setdefault(workspace_id, threading.Lock())

    def _repo(self, workspace_id: str) -> Path:
        return self.root / (workspace_id + ".git")

    @staticmethod
    def _env(repo: Path) -> dict:
        return {
            "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
            "LC_ALL": "C", "LANG": "C",
            "GIT_DIR": str(repo),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_PAGER": "cat", "PAGER": "cat",
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_NO_LAZY_FETCH": "1",
            "GIT_ALLOW_PROTOCOL": "",
        }

    def _git(self, repo: Path, args: list[str], *, timeout: float = SHADOW_TIMEOUT,
             output_limit: int = 1024 * 1024, stdin_file=None) -> bytes:
        binary = _git_binary()
        if binary is None:
            raise BridgeError("Git executable is unavailable", "git_unavailable")
        argv = [binary, "--no-pager", "--literal-pathspecs", "-c", "core.hooksPath=/dev/null",
                "-c", "core.fsync=objects,reference", "-c", "core.fsyncMethod=fsync", *args]
        with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
            try:
                completed = subprocess.run(argv, cwd=str(repo if repo.exists() else self.root),
                                           env=self._env(repo), stdin=stdin_file or subprocess.DEVNULL,
                                           stdout=out, stderr=err, timeout=timeout,
                                           close_fds=True, start_new_session=True, check=False)
            except subprocess.TimeoutExpired:
                raise BridgeError("Checkpoint Git operation timed out", "checkpoint_timeout") from None
            except (OSError, ValueError):
                raise BridgeError("Checkpoint store is unavailable", "checkpoint_storage") from None
            if completed.returncode != 0:
                raise BridgeError("Checkpoint store operation failed", "checkpoint_storage")
            if out.tell() > output_limit:
                raise BridgeError("Checkpoint output exceeded its bound", "output_limit")
            out.seek(0)
            return out.read()

    def _ensure_repo(self, workspace_id: str) -> Path:
        repo = self._repo(workspace_id)
        if (repo / "HEAD").is_file():
            return repo
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        staging = Path(tempfile.mkdtemp(prefix=".init-", dir=self.root))
        try:
            self._git(staging, ["init", "--bare", "--quiet", "--object-format=sha1", str(staging)])
            for key, value in (("core.logAllRefUpdates", "false"), ("gc.auto", "0"),
                               ("core.hooksPath", "/dev/null"), ("receive.denyAll", "true")):
                self._git(staging, ["config", "--local", key, value])
            os.rename(staging, repo)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        os.chmod(repo, 0o700)
        return repo

    def _store_bytes(self, workspace_id: str) -> int:
        total = 0
        for directory, _, files in os.walk(self._repo(workspace_id)):
            for name in files:
                try:
                    total += os.lstat(os.path.join(directory, name)).st_size
                except OSError:
                    continue
        return total

    # -- capture ----------------------------------------------------------

    @staticmethod
    def _listings(safe: SafeRoot, deadline: float) -> tuple[set[str], set[str], set[str], bool]:
        """Read-only tracked and ignored listings; empty for non-Git workspaces."""
        try:
            repository = GitEvidence._repository(safe)
        except BridgeError:
            return set(), set(), set(), False
        if repository is None:
            return set(), set(), set(), True
        binary, git_dir, overrides = repository
        timeout = max(0.5, deadline - time.monotonic())
        try:
            tracked_raw = _run(binary, safe, git_dir, ["ls-files", "-z", "--cached"],
                               timeout=timeout, output_limit=MAX_LISTING_OUTPUT,
                               overrides=overrides)
            ignored_raw = _run(binary, safe, git_dir,
                               ["ls-files", "-z", "--others", "--ignored", "--exclude-standard",
                                "--directory"],
                               timeout=max(0.5, deadline - time.monotonic()),
                               output_limit=MAX_LISTING_OUTPUT, overrides=overrides)
        except BridgeError:
            return set(), set(), set(), False
        tracked = {os.fsdecode(item) for item in tracked_raw.split(b"\0") if item}
        ignored = [os.fsdecode(item) for item in ignored_raw.split(b"\0") if item]
        return (tracked, {item for item in ignored if not item.endswith("/")},
                {item for item in ignored if item.endswith("/")}, True)

    @staticmethod
    def _walk(safe: SafeRoot, deadline: float, ignored_files: set[str],
              ignored_dirs: set[str]) -> tuple[list[dict], dict, bool]:
        """Policy-filtered regular files, like SafeRoot.walk but budgeted and partial-safe."""
        entries: list[dict] = []
        skipped = {"symlink_or_mount": 0, "special_or_hardlinked": 0}
        truncated = False

        def visit(fd: int, rel: str, depth: int) -> None:
            nonlocal truncated
            if time.monotonic() > deadline:
                raise BridgeError("Checkpoint capture exceeded its time budget", "checkpoint_timeout")
            if depth > 40:
                truncated = True
                return
            with os.scandir(fd) as iterator:
                names = sorted(entry.name for entry in iterator)
            for name in names:
                if truncated:
                    return
                relative = f"{rel}/{name}" if rel else name
                if not allowed(relative, safe.extra) or relative in ignored_files:
                    continue
                try:
                    st = os.stat(name, dir_fd=fd, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if st.st_dev != safe.identity[0] or stat.S_ISLNK(st.st_mode):
                    skipped["symlink_or_mount"] += 1
                elif stat.S_ISDIR(st.st_mode):
                    if relative + "/" in ignored_dirs:
                        continue
                    try:
                        child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                                        dir_fd=fd)
                    except OSError:
                        skipped["symlink_or_mount"] += 1
                        continue
                    try:
                        opened = os.fstat(child)
                        if (opened.st_dev, opened.st_ino) != (st.st_dev, st.st_ino):
                            raise BridgeError("Directory changed during checkpoint scan", "workspace_busy")
                        visit(child, relative, depth + 1)
                    finally:
                        os.close(child)
                elif stat.S_ISREG(st.st_mode) and st.st_nlink == 1:
                    if len(entries) >= MAX_ENTRIES:
                        truncated = True
                        return
                    entries.append({"path": relative, "dev": st.st_dev, "ino": st.st_ino,
                                    "size": st.st_size, "mtime_ns": st.st_mtime_ns,
                                    "ctime_ns": st.st_ctime_ns, "mode": st.st_mode})
                else:
                    skipped["special_or_hardlinked"] += 1

        try:
            with safe.directory([]) as root_fd:
                visit(root_fd, "", 0)
        except OSError:
            raise BridgeError("Workspace changed or became unreadable during checkpoint scan",
                              "workspace_busy") from None
        return entries, skipped, truncated

    def capture(self, safe: SafeRoot, ws: dict, checkpoint_id: str, *, budget_ms: int = 15000,
                max_files: int = DEFAULT_MAX_FILES, max_bytes: int = DEFAULT_MAX_BYTES) -> dict:
        workspace_id = _validate_id(ws.get("id"), _WORKSPACE_ID, "workspace identity")
        _validate_id(checkpoint_id, _CHECKPOINT_ID, "checkpoint id")
        if (type(budget_ms) is not int or not 1000 <= budget_ms <= 120000
                or type(max_files) is not int or not 1 <= max_files <= MAX_ENTRIES
                or type(max_bytes) is not int or not 1 <= max_bytes <= 4 * DEFAULT_MAX_BYTES):
            raise BridgeError("Invalid checkpoint budget", "invalid_arguments")
        deadline = time.monotonic() + budget_ms / 1000
        with self._workspace_lock(workspace_id):
            existing = self._row(workspace_id, checkpoint_id, required=False)
            if existing is not None:
                return self._summary(existing, workspace_id)
            repo = self._ensure_repo(workspace_id)
            tracked, ignored_files, ignored_dirs, listing_ok = self._listings(safe, deadline)
            entries, walk_skipped, truncated = self._walk(safe, deadline, ignored_files, ignored_dirs)
            # Tracked files first, so a cap drops untracked noise before source.
            entries.sort(key=lambda item: (item["path"] not in tracked, item["path"]))
            with self.lock:
                manifest = {row["path"]: dict(row) for row in self.db.execute(
                    "SELECT * FROM checkpoint_manifest WHERE workspace=?", (workspace_id,))}
            started_ns = time.time_ns()
            commit_oid, stats, new_manifest = self._import(
                safe, repo, checkpoint_id, entries, manifest, started_ns, deadline,
                max_files, max_bytes)
            skipped = {**walk_skipped, **stats["skipped_counts"],
                       "paths": stats["skipped_paths"], "truncated": truncated,
                       "listing": "ok" if listing_ok else "unavailable"}
            # Oversized files are expected policy; anything else left out means
            # the checkpoint is incomplete evidence.
            partial = truncated or any(stats["skipped_counts"][reason] > 0
                                       for reason in ("over_cap", "unstable", "unreadable"))
            row = {"id": checkpoint_id, "workspace": workspace_id, "commit_oid": commit_oid,
                   "status": "partial" if partial else "complete", "files": stats["files"],
                   "bytes": stats["bytes"], "skipped": json.dumps(skipped, sort_keys=True),
                   "created": _now()}
            with self.lock, self.db:
                self.db.execute(
                    "INSERT INTO checkpoints(id,workspace,commit_oid,status,files,bytes,skipped,created) "
                    "VALUES(:id,:workspace,:commit_oid,:status,:files,:bytes,:skipped,:created)", row)
                self.db.execute("DELETE FROM checkpoint_manifest WHERE workspace=?", (workspace_id,))
                self.db.executemany(
                    "INSERT INTO checkpoint_manifest(workspace,path,dev,ino,size,mtime_ns,ctime_ns,mode,blob) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    [(workspace_id, item["path"], item["dev"], item["ino"], item["size"],
                      item["mtime_ns"], item["ctime_ns"], item["mode"], item["blob"])
                     for item in new_manifest])
            return self._summary(row, workspace_id)

    def _import(self, safe: SafeRoot, repo: Path, checkpoint_id: str, entries: list[dict],
                manifest: dict, started_ns: int, deadline: float, max_files: int,
                max_bytes: int) -> tuple[str, dict, list[dict]]:
        counts = {"too_large": 0, "over_cap": 0, "unstable": 0, "unreadable": 0}
        skipped_paths: dict[str, str] = {}
        new_manifest: list[dict] = []
        files = total = 0
        ref = f"refs/wb/checkpoints/{checkpoint_id}"
        message = checkpoint_id.encode()
        with tempfile.TemporaryFile(dir=repo) as feed:
            feed.write(b"commit %s\ncommitter %s %d +0000\ndata %d\n%s\ndeleteall\n"
                       % (ref.encode(), _COMMITTER.encode(), started_ns // 1_000_000_000,
                          len(message), message))

            def placeholder(item: dict, reason: str) -> None:
                counts[reason] += 1
                if len(skipped_paths) < MAX_SKIP_PATHS:
                    skipped_paths[item["path"]] = reason
                data = _placeholder(reason, item["size"], item["mtime_ns"])
                feed.write(b"M 100644 inline %s\ndata %d\n%s\n" % (_quote(item["path"]), len(data), data))

            for item in entries:
                if time.monotonic() > deadline:
                    raise BridgeError("Checkpoint capture exceeded its time budget", "checkpoint_timeout")
                mode = b"100755" if item["mode"] & 0o111 else b"100644"
                if item["size"] > MAX_FILE:
                    placeholder(item, "too_large")
                    continue
                if files >= max_files or total + item["size"] > max_bytes:
                    placeholder(item, "over_cap")
                    continue
                previous = manifest.get(item["path"])
                if (previous is not None and _OID.fullmatch(previous["blob"])
                        and all(previous[key] == item[key]
                                for key in ("dev", "ino", "size", "mtime_ns", "ctime_ns", "mode"))
                        and item["mtime_ns"] < started_ns - RACY_NS):
                    feed.write(b"M %s %s %s\n" % (mode, previous["blob"].encode(), _quote(item["path"])))
                    new_manifest.append({**item, "blob": previous["blob"]})
                    files += 1
                    total += item["size"]
                    continue
                outcome, data, observed = self._read(safe, item["path"])
                if outcome == "missing":
                    continue
                if outcome != "ok":
                    placeholder(item, outcome)
                    continue
                feed.write(b"M %s inline %s\ndata %d\n" % (mode, _quote(item["path"]), len(data)))
                feed.write(data)
                feed.write(b"\n")
                new_manifest.append({**item, "dev": observed.st_dev, "ino": observed.st_ino,
                                     "size": observed.st_size, "mtime_ns": observed.st_mtime_ns,
                                     "ctime_ns": observed.st_ctime_ns, "mode": observed.st_mode,
                                     "blob": _blob_oid(data)})
                files += 1
                total += len(data)
            feed.write(b"done\n")
            feed.flush()
            feed.seek(0)
            remaining = max(1.0, deadline - time.monotonic())
            self._git(repo, ["fast-import", "--quiet", "--done"], timeout=remaining, stdin_file=feed)
        oid = self._git(repo, ["rev-parse", "--verify", "--end-of-options", ref + "^{commit}"],
                        output_limit=256).strip().decode()
        if not _OID.fullmatch(oid):
            raise BridgeError("Checkpoint commit could not be verified", "checkpoint_storage")
        return oid, {"files": files, "bytes": total, "skipped_counts": counts,
                     "skipped_paths": skipped_paths}, new_manifest

    @staticmethod
    def _read(safe: SafeRoot, path: str) -> tuple[str, bytes, os.stat_result | None]:
        """Read one file under the normal read policy, retrying one concurrent change."""
        for attempt in range(2):
            try:
                data, observed = safe.read(path, limit=MAX_FILE)
                return "ok", data, observed
            except BridgeError as exc:
                if exc.code == "not_found":
                    return "missing", b"", None
                if exc.code == "too_large":
                    return "too_large", b"", None
                if exc.code != "workspace_busy":
                    return "unreadable", b"", None
                if attempt == 0:
                    time.sleep(0.05)
        return "unstable", b"", None

    # -- reads ------------------------------------------------------------

    def _row(self, workspace_id: str, checkpoint_id: str, *, required: bool = True) -> dict | None:
        _validate_id(checkpoint_id, _CHECKPOINT_ID, "checkpoint id")
        with self.lock:
            row = self.db.execute("SELECT * FROM checkpoints WHERE id=? AND workspace=?",
                                  (checkpoint_id, workspace_id)).fetchone()
        if row is None and required:
            raise BridgeError("Checkpoint is unavailable or expired", "checkpoint_expired")
        return dict(row) if row is not None else None

    def _summary(self, row: dict, workspace_id: str) -> dict:
        skipped = json.loads(row["skipped"])
        return {"checkpoint_id": row["id"], "status": row["status"], "files": row["files"],
                "bytes": row["bytes"], "created": row["created"],
                "skipped": {key: value for key, value in skipped.items() if key != "paths"},
                "store_bytes": self._store_bytes(workspace_id)}

    def _changed(self, ws: dict, from_id: str, to_id: str) -> tuple[Path, dict, dict, list[dict]]:
        workspace_id = _validate_id(ws.get("id"), _WORKSPACE_ID, "workspace identity")
        before = self._row(workspace_id, from_id)
        after = self._row(workspace_id, to_id)
        repo = self._repo(workspace_id)
        raw = self._git(repo, ["diff-tree", "-r", "-z", "--no-renames", "--name-status",
                               before["commit_oid"], after["commit_oid"]],
                        output_limit=MAX_LISTING_OUTPUT)
        fields = raw.split(b"\0")
        not_captured = {**json.loads(before["skipped"]).get("paths", {}),
                        **json.loads(after["skipped"]).get("paths", {})}
        changes = []
        for index in range(0, len(fields) - 1, 2):
            status_code, path = fields[index].decode("ascii", "replace"), os.fsdecode(fields[index + 1])
            if not path or not allowed(path, ws.get("excludes", [])):
                continue
            entry = {"path": path, "status": {"A": "added", "D": "deleted"}.get(status_code, "modified")}
            if path in not_captured:
                entry["not_captured"] = not_captured[path]
            changes.append(entry)
        return repo, before, after, changes

    def changes(self, ws: dict, *, from_id: str, to_id: str, offset: int = 0, limit: int = 100) -> dict:
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 500:
            raise BridgeError("Invalid checkpoint page", "invalid_arguments")
        with self._workspace_lock(ws.get("id", "")):
            _, before, after, changes = self._changed(ws, from_id, to_id)
        page = changes[offset:offset + limit]
        return {"from": from_id, "to": to_id, "from_status": before["status"],
                "to_status": after["status"], "total": len(changes), "changes": page,
                "offset": offset,
                "next_offset": offset + len(page) if offset + len(page) < len(changes) else None}

    def diff(self, ws: dict, *, from_id: str, to_id: str, path: str | None = None,
             offset: int = 0, max_bytes: int = MAX_PAGE_BYTES) -> dict:
        if type(max_bytes) is not int or not 256 <= max_bytes <= MAX_PAGE_BYTES:
            raise BridgeError("Diff page size exceeds the output budget", "output_limit")
        if type(offset) is not int or offset < 0:
            raise BridgeError("Invalid diff offset", "invalid_arguments")
        with self._workspace_lock(ws.get("id", "")):
            repo, before, after, changes = self._changed(ws, from_id, to_id)
            names = [entry["path"] for entry in changes]
            if path is not None:
                if path not in names:
                    raise BridgeError("Requested path is unavailable or unchanged in this run",
                                      "not_found")
                names = [path]
            if len(names) > MAX_DIFF_PATHS:
                raise BridgeError("Too many changed paths for one bounded diff; request one path",
                                  "output_limit")
            raw = b""
            if names:
                raw = self._git(repo, ["diff-tree", "-r", "-p", "--no-ext-diff", "--no-textconv",
                                       "--no-renames", "--no-color", before["commit_oid"],
                                       after["commit_oid"], "--", *names],
                                output_limit=MAX_DIFF_OUTPUT)
        return {"available": True, "mode": "run", "from": from_id, "to": to_id,
                "from_status": before["status"], "to_status": after["status"],
                **page_patch(raw, offset, max_bytes)}

    # -- retention --------------------------------------------------------

    def purge(self, ws: dict, *, checkpoint_ids: list[str] | None = None, everything: bool = False) -> dict:
        workspace_id = _validate_id(ws.get("id"), _WORKSPACE_ID, "workspace identity")
        if everything == (checkpoint_ids is not None):
            raise BridgeError("Purge needs either checkpoint IDs or everything", "invalid_arguments")
        with self._workspace_lock(workspace_id):
            repo = self._repo(workspace_id)
            if everything:
                with self.lock, self.db:
                    removed = self.db.execute("DELETE FROM checkpoints WHERE workspace=?",
                                              (workspace_id,)).rowcount
                    self.db.execute("DELETE FROM checkpoint_manifest WHERE workspace=?", (workspace_id,))
                shutil.rmtree(repo, ignore_errors=True)
                return {"removed": removed, "store_bytes": 0}
            if not isinstance(checkpoint_ids, list) or len(checkpoint_ids) > 200:
                raise BridgeError("Invalid checkpoint ID list", "invalid_arguments")
            ids = [_validate_id(value, _CHECKPOINT_ID, "checkpoint id") for value in checkpoint_ids]
            with self.lock:
                present = [row["id"] for row in self.db.execute(
                    f"SELECT id FROM checkpoints WHERE workspace=? AND id IN ({','.join('?' * len(ids))})",
                    (workspace_id, *ids))] if ids else []
            if present and repo.exists():
                feed = "".join(f"delete refs/wb/checkpoints/{value}\n" for value in present).encode()
                with tempfile.TemporaryFile() as stdin_file:
                    stdin_file.write(feed)
                    stdin_file.seek(0)
                    self._git(repo, ["update-ref", "--stdin"], stdin_file=stdin_file)
                self._git(repo, ["gc", "--quiet", "--prune=now"], timeout=GC_TIMEOUT)
            with self.lock, self.db:
                for value in present:
                    self.db.execute("DELETE FROM checkpoints WHERE id=?", (value,))
                if present:
                    # gc may have pruned blobs the stat cache points at; the
                    # next capture rereads everything instead of trusting it.
                    self.db.execute("DELETE FROM checkpoint_manifest WHERE workspace=?",
                                    (workspace_id,))
            return {"removed": len(present), "store_bytes": self._store_bytes(workspace_id)}

    def stats(self, ws: dict) -> dict:
        workspace_id = _validate_id(ws.get("id"), _WORKSPACE_ID, "workspace identity")
        with self.lock:
            count = self.db.execute("SELECT COUNT(*) FROM checkpoints WHERE workspace=?",
                                    (workspace_id,)).fetchone()[0]
        return {"checkpoints": count, "store_bytes": self._store_bytes(workspace_id)}
