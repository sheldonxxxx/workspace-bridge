"""Private workspace data-plane service hosted by one authoritative Node.

The Node owns filesystem policy, search cursors, Git evidence, and runtime
adapter connection secrets. Bridge communicates with it through ``node_api``.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import threading
from datetime import datetime, timezone

from .browse import Browser
from .keyed_locks import KeyedLocks
from .checkpoint_store import CheckpointStore
from .checks import CheckRunner
from .plan_files import PlanStore
from . import edit_journal
from .git_evidence import GitEvidence
from .media import (DEFAULT_DIMENSION, SUPPORTED_SUFFIXES, ImageReadResult,
                    decode_image_upload, image_capabilities, read_image,
                    selected_read_limit, sniff_image, validate_image)
from .security import (BridgeError, HANDOFF, MAX_FILE, MAX_OUTPUT, MAX_WRITE,
                       SafeRoot, allowed, digest, file_text, handoff_allowed,
                       parts, redact, require_write_path, text_content,
                       validate_excludes)
from .wbrp import RuntimeClient
from .schema_upgrade import add_plugin_adapters, widen_runtime_type_check
from .plugin_protocol import builtin_manifests, load_manifest
from .plugin_supervisor import PluginRegistry
from . import __version__


CHECKPOINT_OPERATIONS = {
    "checkpoint_capture": frozenset({"checkpoint_id", "budget_ms", "max_files", "max_bytes"}),
    "checkpoint_changes": frozenset({"from_id", "to_id", "offset", "limit"}),
    "checkpoint_diff": frozenset({"from_id", "to_id", "path", "offset", "max_bytes"}),
    "checkpoint_purge": frozenset({"checkpoint_ids", "everything"}),
    "checkpoint_stats": frozenset(),
}
CHECK_OPERATIONS = {
    "checks_config": frozenset(),
    "set_checks_config": frozenset({"text"}),
    "start_checks": frozenset({"names", "run_id"}),
    "read_checks": frozenset({"check_id", "wait_ms"}),
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _inside(path: Path, parent: Path) -> bool:
    return path == parent or parent in path.parents


def _public_adapter(row: dict) -> dict:
    view = {"id": row["id"], "name": row["name"],
            "runtime_type": row["runtime_type"], "base_url": row["base_url"],
            "enabled": bool(row["enabled"]), "revision": row["revision"],
            "created": row["created"], "updated": row["updated"],
            "has_token": bool(row["token"]), "kind": row.get("kind", "http")}
    if view["kind"] == "plugin":
        view.update(plugin=row["plugin"], projects_root=row["projects_root"],
                    settings=json.loads(row.get("settings") or "{}"))
    return view


PLUGIN_SETTING_LIMIT = 4096
HTTP_REMOVED_MESSAGE = ("HTTP adapters were removed; add the agent as a plugin with "
                        "`workspace-bridge node adapter add <plugin> --name <name>`")


ADAPTER_REVISION_RE = re.compile(r"^rev_[0-9a-f]{32}$")


def _node_host_info(config: dict | None = None) -> dict:
    """Bounded Node host identity (platform/arch only, no subprocess).

    ``status()`` carries host identity only: this helper never executes
    subprocesses, never probes installers, and never resolves a login
    shell or PATH. Runtime ``platform``/``arch`` stay bounded and never
    expose service paths, tokens, or environment values. Config overrides
    are explicit test doubles only (``host_platform``/``host_arch``).
    """
    cfg = config if isinstance(config, dict) else {}
    raw_platform = cfg.get("host_platform")
    raw_arch = cfg.get("host_arch")
    if isinstance(raw_platform, str) and raw_platform in ("linux", "darwin"):
        platform_name: str | None = raw_platform
    else:
        try:
            import sys as _sys
            if _sys.platform.startswith("linux"):
                platform_name = "linux"
            elif _sys.platform == "darwin":
                platform_name = "darwin"
            else:
                platform_name = "unknown"
        except Exception:
            platform_name = "unknown"
        if isinstance(raw_platform, str) and raw_platform:
            # Explicit unknown override stays fail-closed, never a path.
            if raw_platform not in ("linux", "darwin"):
                platform_name = "unknown"
    if isinstance(raw_arch, str) and raw_arch in ("x64", "arm64"):
        arch_name: str | None = raw_arch
    else:
        try:
            import platform as _platform
            machine = str(_platform.machine() or "").lower()
            if machine in ("x86_64", "x64", "amd64"):
                arch_name = "x64"
            elif machine in ("arm64", "aarch64"):
                arch_name = "arm64"
            else:
                arch_name = "unknown"
        except Exception:
            arch_name = "unknown"
        if isinstance(raw_arch, str) and raw_arch:
            if raw_arch not in ("x64", "arm64"):
                arch_name = "unknown"
    return {"platform": platform_name, "arch": arch_name}


class NodeService:
    """Node-owned safe workspace operations and runtime adapter registry."""

    def __init__(self, state: Path, config: dict, *, read_only: bool = False):
        self.state = state.expanduser().resolve()
        self.config = config
        self.read_only = read_only
        self.allowed_roots = [Path(root).expanduser().resolve(strict=not read_only)
                              for root in config["allowed_roots"]]
        self.lock = threading.RLock()
        # Checked writes (expected_sha256, then write) serialize per workspace.
        self.write_locks = KeyedLocks()
        db_path = self.state / "node.sqlite3"
        if read_only:
            # An immutable connection avoids SQLite creating a shared-memory
            # sidecar when the database is closed.  An active writer may have
            # its schema and latest rows in the WAL, however, which immutable
            # mode deliberately ignores.  In that case use SQLite's regular
            # read-only WAL-aware connection; the writer has already created
            # both sidecars, so opening it does not add a filesystem mutation.
            wal_path = db_path.with_name(db_path.name + "-wal")
            shm_path = db_path.with_name(db_path.name + "-shm")
            query = "?mode=ro" if wal_path.exists() or shm_path.exists() else "?mode=ro&immutable=1"
            self.db = sqlite3.connect(db_path.as_uri() + query, uri=True,
                                      check_same_thread=False)
        else:
            self.state.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(self.state, 0o700)
            self.db = sqlite3.connect(db_path, check_same_thread=False)
            os.chmod(db_path, 0o600)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        tables = {r[0] for r in self.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        if not tables:
            if read_only:
                self.db.close()
                raise BridgeError("Node state schema is unavailable", "state_schema_incompatible")
            self.db.executescript("""
              PRAGMA journal_mode=WAL;
              CREATE TABLE runtime_adapters (
                id TEXT PRIMARY KEY, name TEXT NOT NULL COLLATE NOCASE UNIQUE,
                runtime_type TEXT NOT NULL CHECK(runtime_type IN ('pi','codex','claude','opencode')),
                base_url TEXT NOT NULL, token TEXT NOT NULL,
                enabled INTEGER NOT NULL CHECK(enabled IN (0,1)), revision TEXT NOT NULL,
                created TEXT NOT NULL, updated TEXT NOT NULL);
              CREATE TABLE node_workspaces (
                id TEXT PRIMARY KEY, root TEXT NOT NULL UNIQUE,
                excludes TEXT NOT NULL, write_scope TEXT NOT NULL
                  CHECK(write_scope IN ('none','handoff','workspace')),
                created TEXT NOT NULL, updated TEXT NOT NULL);
            """)
            self.db.commit()
            os.chmod(db_path, 0o600)
        elif not {"runtime_adapters", "node_workspaces"}.issubset(tables):
            self.db.close()
            raise BridgeError("Node state schema is incompatible", "state_schema_incompatible")
        if tables and not read_only:
            widen_runtime_type_check(self.db, "runtime_adapters")
        if not read_only:
            add_plugin_adapters(self.db)
        self.browser = Browser(self)
        self.plugins = PluginRegistry(self.state / "plugins")
        self._checkpoints = None
        self._journal = None
        self._checks = None

    @property
    def journal(self) -> edit_journal.EditJournal:
        if self.read_only:
            raise BridgeError("The edit journal is unavailable in read-only mode", "journal_unavailable")
        with self.lock:
            if self._journal is None:
                self._journal = edit_journal.EditJournal(self.state / "edit-journal.sqlite3")
            return self._journal

    @property
    def checks(self) -> CheckRunner:
        if self.read_only:
            raise BridgeError("Checks are unavailable in read-only mode", "checks_unavailable")
        with self.lock:
            if self._checks is None:
                self._checks = CheckRunner(self.state)
            return self._checks

    @property
    def checkpoints(self) -> CheckpointStore:
        if self.read_only:
            raise BridgeError("Checkpoints are unavailable in read-only mode", "checkpoint_storage")
        with self.lock:
            if self._checkpoints is None:
                self._checkpoints = CheckpointStore(self.state, self.db, self.lock)
            return self._checkpoints

    def close(self) -> None:
        self.plugins.close()
        if self._journal is not None:
            self._journal.close()
        if self._checks is not None:
            self._checks.close()
        self.db.close()

    def safe_root(self, workspace: dict) -> SafeRoot:
        raw = workspace.get("root")
        if not isinstance(raw, str) or not Path(raw).is_absolute() or os.path.normpath(raw) != raw:
            raise BridgeError("Canonical workspace root required", "root_invalid")
        path = Path(raw)
        containing = next((root for root in self.allowed_roots
                           if _inside(path, root) and path != root), None)
        if containing is None:
            raise BridgeError("Workspace root is outside this Node's allowed roots", "root_denied")
        try:
            if path.resolve(strict=True) != path:
                raise BridgeError("Workspace root must be canonical", "root_invalid")
        except (OSError, RuntimeError):
            raise BridgeError("Workspace root unavailable", "root_unavailable") from None
        relative_root = path.relative_to(containing).as_posix()
        if not allowed(relative_root):
            raise BridgeError("Sensitive or excluded directory cannot be a workspace root", "root_denied")
        if _inside(self.state, path) or _inside(path, self.state):
            raise BridgeError("Node state cannot be exposed through a workspace", "root_denied")
        if path.is_symlink() or not path.is_dir():
            raise BridgeError("Workspace root unavailable", "root_unavailable")
        return SafeRoot(raw, None, validate_excludes(workspace.get("excludes", [])))

    def validate_root(self, root: str, excludes: list[str] | None = None) -> dict:
        workspace = {"root": root, "excludes": excludes or []}
        with self.safe_root(workspace) as safe:
            inventory, skipped = safe.walk("", include_dirs=True, depth_limit=1)
            identity = safe.identity
        return {"valid": True, "root": root,
                "identity": {"device": identity[0], "inode": identity[1]},
                "sample_entries": len(inventory), "skipped_entries": len(skipped)}

    def register_workspace(self, workspace: dict) -> dict:
        if not isinstance(workspace, dict) or set(workspace) != {
                "id", "root", "excludes", "write_scope"}:
            raise BridgeError("Invalid Node workspace registration", "invalid_arguments")
        if not isinstance(workspace["id"], str) or not re.fullmatch(
                r"ws_[0-9a-f]{24}", workspace["id"]):
            raise BridgeError("Invalid workspace identity", "invalid_arguments")
        scope = workspace["write_scope"]
        if scope not in {"none", "handoff", "workspace"}:
            raise BridgeError("Invalid workspace write scope", "invalid_arguments")
        self.validate_root(workspace["root"], workspace["excludes"])
        with self.lock, self.db:
            existing = self.db.execute("SELECT * FROM node_workspaces WHERE id=?",
                                       (workspace["id"],)).fetchone()
            if existing and existing["root"] != workspace["root"]:
                raise BridgeError("Workspace identity is already bound to another root",
                                  "workspace_authority_mismatch")
            if not existing:
                path = Path(workspace["root"])
                for row in self.db.execute("SELECT id,root FROM node_workspaces"):
                    old = Path(row["root"])
                    if path == old or path in old.parents or old in path.parents:
                        raise BridgeError("Overlapping workspace roots are forbidden", "conflict")
                self.db.execute("INSERT INTO node_workspaces(id,root,excludes,write_scope,created,updated) "
                                "VALUES(?,?,?,?,?,?)", (workspace["id"], workspace["root"],
                                json.dumps(workspace["excludes"]), scope, _now(), _now()))
        return {"registered": True, "workspace_id": workspace["id"]}

    def configure_workspace(self, workspace_id: str, excludes: list[str], write_scope: str) -> dict:
        with self.lock:
            row = self.db.execute("SELECT * FROM node_workspaces WHERE id=?", (workspace_id,)).fetchone()
        if row is None:
            raise BridgeError("Workspace is not registered on this Node", "unknown_workspace")
        validate_excludes(excludes)
        self.validate_root(row["root"], excludes)
        if write_scope not in {"none", "handoff", "workspace"}:
            raise BridgeError("Invalid workspace configuration", "invalid_arguments")
        with self.lock, self.db:
            self.db.execute("UPDATE node_workspaces SET excludes=?,write_scope=?,updated=? WHERE id=?",
                            (json.dumps(excludes), write_scope, _now(), workspace_id))
        return {"updated": True, "workspace_id": workspace_id}

    def authoritative_workspace(self, supplied: dict) -> dict:
        workspace_id = supplied.get("id") if isinstance(supplied, dict) else None
        with self.lock:
            row = self.db.execute("SELECT * FROM node_workspaces WHERE id=?", (workspace_id,)).fetchone()
        if row is None:
            raise BridgeError("Workspace is not registered on this Node", "unknown_workspace")
        if supplied.get("root") != row["root"]:
            raise BridgeError("Workspace root differs from this Node's authority record",
                              "workspace_authority_mismatch")
        return {"id": row["id"], "root": row["root"],
                "excludes": json.loads(row["excludes"]), "write_scope": row["write_scope"]}

    def status(self) -> dict:
        from .release import node_release
        roots = []
        for root in self.allowed_roots:
            roots.append({"available": root.is_dir() and not root.is_symlink(),
                          "root_label": root.name[:80] or "root"})
        # ``status()`` is a plain health/identity read: platform/arch
        # only, no installer probing, no login-shell PATH resolution,
        # no package-manager commands.
        return {"status": "ok", "protocol": 1, "node_version": __version__,
                "release": node_release(), "capabilities": [
            "files", "search", "git", "handoff-artifacts", "runtime-adapters", "checkpoints", "checks",
            "plan-files"],
            "host": _node_host_info(self.config),
            "allowed_roots": roots,
            "runtime_adapters": len(self.adapters())}

    WRITE_OPERATIONS = frozenset({"write_image", "write_file", "edit_file", "apply_patch",
                                  "undo_edit", "publish_handoff_artifacts",
                                  "set_checks_config"})

    def workspace_call(self, operation: str, body: dict):
        if operation in self.WRITE_OPERATIONS:
            supplied = body.get("workspace")
            key = supplied.get("id") if isinstance(supplied, dict) else None
            with self.write_locks.hold(key):
                return self._workspace_call(operation, body)
        return self._workspace_call(operation, body)

    def _workspace_call(self, operation: str, body: dict):
        supplied_workspace = body.get("workspace")
        if not isinstance(supplied_workspace, dict):
            raise BridgeError("Workspace authority context is required", "invalid_arguments")
        workspace = self.authoritative_workspace(supplied_workspace)
        checked = self.safe_root(workspace)
        root = checked.path
        checked.close()
        ws = {**workspace, "root": root}
        if operation == "list_dir":
            return self.browser.list_dir(ws, **body["arguments"])
        if operation == "glob":
            return self.browser.glob(ws, **body["arguments"])
        if operation == "grep_files":
            return self.browser.grep_files(ws, **body["arguments"])
        if operation == "read_file":
            args = body["arguments"]
            if args.get("representation", "auto") not in ("auto", "text", "image"):
                raise BridgeError("Unknown read representation", "invalid_arguments")
            with self.safe_root(ws) as safe:
                data, _ = safe.read(args["path"], limit_selector=(
                    selected_read_limit if args.get("representation", "auto") != "text" else None))
            sha = digest(data)
            if args.get("expected_sha256") and sha != args["expected_sha256"]:
                raise BridgeError("File changed since the referenced read", "stale_evidence")
            detected = sniff_image(data[:32])
            image = args.get("representation", "auto") == "image" or (
                args.get("representation", "auto") == "auto" and
                (detected is not None or Path(args["path"]).suffix.casefold() in SUPPORTED_SUFFIXES))
            if image:
                if args.get("start_line", 1) != 1 or args.get("max_lines", 200) != 200:
                    raise BridgeError("Image reads do not accept line pagination; omit offset and limit",
                                      "invalid_arguments")
                result = read_image(data, args["path"], sha,
                                    DEFAULT_DIMENSION if args.get("max_image_dimension") is None
                                    else args["max_image_dimension"])
                return {"__image__": True, "metadata": result.metadata,
                        "data": __import__("base64").b64encode(result.data).decode("ascii"),
                        "mime_type": result.mime_type}
            if args.get("max_image_dimension") is not None:
                raise BridgeError("max_image_dimension only applies to image reads", "invalid_arguments")
            try:
                text = data.decode("utf-8")
                if "\x00" in text:
                    raise UnicodeError()
            except UnicodeError:
                raise BridgeError("Text reading does not support binary files", "binary_file") from None
            text, redacted = redact(text)
            lines = text.splitlines()
            start, maximum = args.get("start_line", 1), args.get("max_lines", 200)
            selected, count, next_line = [], 0, None
            for index in range(start - 1, min(len(lines), start - 1 + maximum)):
                line = lines[index]
                if len(line) > MAX_OUTPUT - 100:
                    raise BridgeError("A line is too long for safe line-based output", "output_limit")
                if count + len(line) > MAX_OUTPUT - 1000:
                    next_line = index + 1
                    break
                selected.append({"line": index + 1, "text": line})
                count += len(line) + 50
            if next_line is None and start - 1 + len(selected) < len(lines):
                next_line = start + len(selected)
            return {"path": args["path"], "sha256": sha, "lines": selected,
                    "total_lines": len(lines), "next_line": next_line,
                    "redacted": redacted, "trust": "untrusted_project_content"}
        if operation == "write_image":
            args = body["arguments"]
            path = args.get("path")
            scope = workspace.get("write_scope", "none")
            require_write_path(path, workspace.get("excludes", []), scope=scope)
            if not handoff_allowed(path, workspace.get("excludes", [])):
                raise BridgeError("Image uploads require an allowed handoff path", "policy_denied")
            if args.get("mime_type") not in {"image/png", "image/jpeg", "image/webp", "image/gif", "image/bmp", "image/tiff"}:
                raise BridgeError("Unsupported image MIME type", "invalid_arguments")
            data = decode_image_upload(args.get("data_base64"))
            with self.safe_root(ws) as safe:
                result = safe.write_image(path, data, args["mime_type"],
                                          args.get("expected_sha256"), write_scope=scope)
            return {**result, "absolute_path": str(Path(ws["root"]) / path), "write_scope": scope,
                    "note": "Original image bytes were written. No command was executed or agent started."}
        if operation == "write_file":
            args = body["arguments"]
            scope = workspace.get("write_scope", "none")
            require_write_path(args["path"], workspace.get("excludes", []), scope=scope)
            try:
                data = args["content"].encode("utf-8")
            except (AttributeError, UnicodeEncodeError):
                raise BridgeError("Content must be valid UTF-8 text", "invalid_arguments") from None
            text_content(data)
            with self.safe_root(ws) as safe:
                previous = None
                if args.get("expected_sha256") is not None:
                    try:
                        previous, _ = safe.read(args["path"], limit=MAX_WRITE)
                    except BridgeError as exc:
                        if exc.code != "not_found":
                            raise
                        raise BridgeError("Expected file is missing; re-read before writing",
                                          "stale_evidence") from None
                    if digest(previous) != args["expected_sha256"]:
                        raise BridgeError("File changed; re-read before writing", "stale_evidence")
                else:
                    file_text(data)
                result = safe.write_file(args["path"], data, args.get("expected_sha256"),
                                         write_scope=scope, previous=previous)
            return {**result, "absolute_path": str(Path(root) / args["path"]),
                    "write_scope": scope, "note": "Only this file was written. No command was executed or agent started."}
        if operation == "edit_file":
            args = body["arguments"]
            scope = workspace.get("write_scope", "none")
            require_write_path(args["path"], workspace.get("excludes", []), scope=scope)
            if not args["old_text"]:
                raise BridgeError("old_text must be nonempty", "invalid_arguments")
            with self.safe_root(ws) as safe:
                raw, _ = safe.read(args["path"], limit=MAX_WRITE)
                if digest(raw) != args["expected_sha256"]:
                    raise BridgeError("File changed; re-read before editing", "stale_evidence")
                text = text_content(raw)
                first = text.find(args["old_text"])
                if first < 0:
                    raise BridgeError("old_text was not found; re-read and use exact text", "match_not_found")
                if text.find(args["old_text"], first + 1) >= 0:
                    raise BridgeError("old_text is ambiguous; include more surrounding text", "ambiguous_match")
                updated = text[:first] + args["new_text"] + text[first + len(args["old_text"]):]
                try:
                    updated_data = updated.encode("utf-8")
                except UnicodeEncodeError:
                    raise BridgeError("Content must be valid UTF-8 text", "invalid_arguments") from None
                file_text(updated_data, raw)
                result = safe.write_file(args["path"], updated_data, args["expected_sha256"],
                                         write_scope=scope, previous=raw)
            return {**result, "absolute_path": str(Path(root) / args["path"]),
                    "replacements": 1, "write_scope": scope}
        if operation in ("apply_patch", "undo_edit"):
            args = body["arguments"]
            if not isinstance(args, dict) or set(args) != (
                    {"changes", "message"} if operation == "apply_patch" else {"edit_id"}):
                raise BridgeError("Invalid edit arguments", "invalid_arguments")
            if workspace.get("write_scope", "none") == "none":
                raise BridgeError("Writes are disabled by workspace write scope", "policy_denied")
            with self.safe_root(ws) as safe:
                if operation == "apply_patch":
                    result = edit_journal.apply_patch(safe, self.journal, ws, args["changes"], args["message"])
                else:
                    result = edit_journal.undo_edit(safe, self.journal, ws, args["edit_id"])
            return {**result, "write_scope": workspace["write_scope"],
                    "note": "Only these files changed. No command was executed or agent started."}
        if operation == "list_edits":
            args = body["arguments"]
            return self.journal.list(workspace["id"], args.get("offset", 0), args.get("limit", 50),
                                     args.get("since"), args.get("until"))
        if operation in CHECK_OPERATIONS:
            args = body.get("arguments")
            if not isinstance(args, dict) or not set(args) <= CHECK_OPERATIONS[operation]:
                raise BridgeError("Invalid check arguments", "invalid_arguments")
            runner = self.checks
            if operation == "checks_config":
                return runner.read_config(workspace)
            if operation == "set_checks_config":
                return runner.write_config(workspace, args.get("text"))
            if operation == "start_checks":
                run_id = args.get("run_id")
                # Checks a user asked for after every run need no write scope;
                # an ad hoc request runs commands that can change files.
                if run_id is None and workspace["write_scope"] != "workspace":
                    raise BridgeError("Running checks needs the edit write scope", "policy_denied")
                return runner.start(workspace, root, args.get("names"), run_id)
            wait_ms = args.get("wait_ms", 0)
            if isinstance(wait_ms, bool) or not isinstance(wait_ms, int) or not 0 <= wait_ms <= 20000:
                raise BridgeError("wait_ms must be 0 to 20000", "invalid_arguments")
            runner.read(workspace, args.get("check_id"))
            runner.wait(args["check_id"], wait_ms / 1000)
            return runner.read(workspace, args["check_id"])
        if operation == "git_status":
            with self.safe_root(ws) as safe:
                return GitEvidence.status(safe, ws, **body["arguments"])
        if operation == "git_diff":
            with self.safe_root(ws) as safe:
                return GitEvidence.diff(safe, ws, **body["arguments"])
        if operation in CHECKPOINT_OPERATIONS:
            args = body.get("arguments")
            if not isinstance(args, dict) or not set(args) <= CHECKPOINT_OPERATIONS[operation]:
                raise BridgeError("Invalid checkpoint arguments", "invalid_arguments")
            store = self.checkpoints
            if operation == "checkpoint_capture":
                with self.safe_root(ws) as safe:
                    return store.capture(safe, ws, **args)
            if operation == "checkpoint_changes":
                return store.changes(ws, **args)
            if operation == "checkpoint_diff":
                return store.diff(ws, **args)
            if operation == "checkpoint_purge":
                return store.purge(ws, **args)
            return store.stats(ws)
        if operation == "read_handoff_artifact":
            args = body["arguments"]
            path = args["path"]
            if not handoff_allowed(path, workspace.get("excludes", [])):
                raise BridgeError("Artifact path outside handoff folder")
            with self.safe_root(ws) as safe:
                data, _ = safe.read(path, artifact=True)
            return {"data": __import__("base64").b64encode(data).decode("ascii")}
        if operation == "write_plan":
            if self.read_only:
                raise BridgeError("Plan files are unavailable in read-only mode", "plan_unavailable")
            args = body.get("arguments")
            if not isinstance(args, dict) or set(args) != {"job_id", "documents"}:
                raise BridgeError("Invalid plan arguments", "invalid_arguments")
            return PlanStore(self.state).write(workspace["id"], args["job_id"], args["documents"])
        if operation == "hash_files":
            paths = body["arguments"].get("paths")
            attachments = body["arguments"].get("attachments", [])
            if not isinstance(paths, list) or len(paths) > 100 or not isinstance(attachments, list) or len(attachments) > 20:
                raise BridgeError("Invalid context file list", "invalid_arguments")
            references = {}
            for attachment in attachments:
                if (not isinstance(attachment, dict) or set(attachment) != {"path", "description", "sha256"}
                        or not isinstance(attachment["path"], str)
                        or not isinstance(attachment["description"], str)
                        or not 1 <= len(attachment["description"]) <= 2000
                        or not isinstance(attachment["sha256"], str)
                        or not re.fullmatch(r"[0-9a-f]{64}", attachment["sha256"])
                        or attachment["path"] in references):
                    raise BridgeError("Invalid handoff attachment", "invalid_arguments")
                references[attachment["path"]] = attachment
            if any(not isinstance(path, str) for path in paths):
                raise BridgeError("Invalid context file list", "invalid_arguments")
            paths = list(dict.fromkeys([*paths, *references]))
            result, total = {}, 0
            inspected = {}
            with self.safe_root(ws) as safe:
                for path in paths:
                    try:
                        data, _ = safe.read(path, limit_selector=selected_read_limit)
                    except BridgeError:
                        raise BridgeError("A context file is unavailable or excluded; re-read before planning",
                                          "stale_context") from None
                    total += len(data)
                    if total > 64 * 1024 * 1024:
                        raise BridgeError("Referenced context exceeds 64 MiB; use fewer context files or attachments",
                                          "context_limit")
                    result[path] = digest(data)
                    if path in references:
                        reference = references[path]
                        if result[path] != reference["sha256"]:
                            raise BridgeError("An attachment changed; re-read before planning", "stale_context")
                        metadata = validate_image(data, path, require_suffix=False)
                        inspected[path] = {**reference, **metadata, "bytes": len(data)}
            return {"hashes": result, "attachments": [inspected[a["path"]] for a in attachments]}
        if operation == "publish_handoff_artifacts":
            args = body["arguments"]
            files = args.get("files")
            if not isinstance(files, dict) or len(files) != 3:
                raise BridgeError("Invalid handoff publication", "invalid_arguments")
            require_write_path(HANDOFF + "/jobs", workspace.get("excludes", []),
                               scope=workspace.get("write_scope", "none"))
            hashes = {}
            prepared = {}
            # Validate every document before opening SafeRoot for creation so a
            # secret or malformed later document cannot leave a partial tree.
            for name in ("TASK.md", "CONTEXT.md", "ACCEPTANCE.md"):
                content = files.get(name)
                if not isinstance(content, str) or not handoff_allowed(
                        args["base"] + "/" + name, workspace.get("excludes", [])):
                    raise BridgeError("Invalid handoff publication", "invalid_arguments")
                try:
                    data = content.encode("utf-8")
                except UnicodeEncodeError:
                    raise BridgeError("Content must be valid UTF-8 text", "invalid_arguments") from None
                file_text(data)
                prepared[name] = data
            with self.safe_root(ws) as safe:
                for name in ("TASK.md", "CONTEXT.md", "ACCEPTANCE.md"):
                    data = prepared[name]
                    hashes[name] = safe.create_artifact(args["base"] + "/" + name, data)
            return {"hashes": hashes}
        raise BridgeError("Unknown Node workspace operation", "not_found")

    def adapters(self) -> list[dict]:
        with self.lock:
            return [dict(row) for row in self.db.execute(
                "SELECT * FROM runtime_adapters ORDER BY name COLLATE NOCASE,id")]

    def adapter(self, adapter_id: str, *, require_enabled: bool = False) -> dict:
        with self.lock:
            row = self.db.execute("SELECT * FROM runtime_adapters WHERE id=?", (adapter_id,)).fetchone()
        if row is None:
            raise BridgeError("Unknown adapter on this Node", "unknown_adapter")
        result = dict(row)
        if require_enabled and not result["enabled"]:
            raise BridgeError("Adapter is disabled", "adapter_disabled")
        return result

    def list_adapters(self) -> dict:
        manifests = self.manifests()
        adapters = []
        for row in self.adapters():
            view = _public_adapter(row)
            manifest = manifests.get(row.get("plugin") or "")
            if manifest is not None:
                # What the Bridge needs to refuse a profile this plugin
                # cannot enforce, and to offer the agent's own security.
                view["security"] = {"enforces": manifest["enforces"],
                                    "native": "native_security" in manifest["features"]}
            adapters.append(view)
        return {"adapters": adapters}

    # -- plugins -----------------------------------------------------------

    def manifests(self) -> dict[str, dict]:
        """Built-in plugin manifests plus any installed on this Node."""
        manifests = builtin_manifests()
        for path in sorted((self.state / "plugins" / "manifests").glob("*.toml")):
            try:
                manifest = load_manifest(path)
            except BridgeError:
                continue
            manifests.setdefault(manifest["id"], manifest)
        return manifests

    def install_manifest(self, source: Path) -> dict:
        """Copy one third-party manifest into this Node's plugin folder."""
        manifest = load_manifest(source)
        if manifest["id"] in builtin_manifests():
            raise BridgeError("A built-in plugin already uses this id", "conflict")
        folder = self.state / "plugins" / "manifests"
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        target = folder / f"{manifest['id']}.toml"
        target.write_bytes(source.read_bytes())
        os.chmod(target, 0o600)
        return manifest

    def _manifest(self, plugin: str) -> dict:
        manifest = self.manifests().get(plugin) if isinstance(plugin, str) else None
        if manifest is None:
            raise BridgeError("Unknown plugin on this Node", "unknown_plugin")
        return manifest

    def _projects_root(self, raw: object) -> str:
        if raw in (None, ""):
            if len(self.allowed_roots) != 1:
                raise BridgeError("Choose a projects root inside this Node's allowed roots",
                                  "invalid_arguments")
            return str(self.allowed_roots[0])
        if not isinstance(raw, str) or not os.path.isabs(raw) or len(raw) > 4096:
            raise BridgeError("Projects root must be an absolute path", "invalid_arguments")
        resolved = Path(raw).resolve()
        if str(resolved) != os.path.normpath(raw) or not resolved.is_dir():
            raise BridgeError("Projects root must be an existing canonical folder", "invalid_arguments")
        if not any(resolved == root or root in resolved.parents for root in self.allowed_roots):
            raise BridgeError("Projects root is outside this Node's allowed roots", "root_denied")
        return str(resolved)

    @staticmethod
    def _plugin_settings(raw: object, manifest: dict) -> dict:
        if raw is None:
            return {}
        allowed = set(manifest["settings"]) | {"log_level"}
        if (not isinstance(raw, dict) or set(raw) - allowed
                or any(not isinstance(value, str) or len(value) > PLUGIN_SETTING_LIMIT
                       for value in raw.values())):
            raise BridgeError("Plugin settings accept only: " + ", ".join(sorted(allowed)),
                              "invalid_arguments")
        if raw.get("log_level", "INFO") not in ("DEBUG", "INFO", "WARNING", "ERROR"):
            raise BridgeError("log_level must be DEBUG, INFO, WARNING or ERROR", "invalid_arguments")
        return {key: value for key, value in raw.items() if value}

    def _save_plugin_adapter(self, payload: dict, current: dict | None) -> dict:
        if set(payload) - {"name", "plugin", "projects_root", "settings", "enabled"}:
            raise BridgeError("A plugin adapter has no URL or token", "invalid_arguments")
        plugin = payload.get("plugin", current["plugin"] if current else None)
        if current and plugin != current["plugin"]:
            raise BridgeError("Adapter plugin is immutable", "invalid_arguments")
        manifest = self._manifest(plugin)
        name = payload.get("name", current["name"] if current else "")
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 80:
            raise BridgeError("Invalid adapter name", "invalid_arguments")
        projects_root = self._projects_root(
            payload.get("projects_root", current["projects_root"] if current else None))
        settings = self._plugin_settings(
            payload.get("settings", json.loads(current["settings"]) if current else None), manifest)
        enabled = payload.get("enabled", bool(current["enabled"]) if current else True)
        if not isinstance(enabled, bool):
            raise BridgeError("enabled must be a boolean", "invalid_arguments")
        encoded = json.dumps(settings, sort_keys=True)
        stamp = _now()
        ident = current["id"] if current else "adapter_" + secrets.token_hex(12)
        revision = current["revision"] if current else "rev_" + secrets.token_hex(16)
        if current and (projects_root != current["projects_root"] or encoded != current["settings"]):
            revision = "rev_" + secrets.token_hex(16)
        try:
            with self.lock, self.db:
                self.db.execute(
                    "INSERT INTO runtime_adapters(id,name,runtime_type,base_url,token,enabled,revision,"
                    "created,updated,kind,plugin,projects_root,settings) "
                    "VALUES(?,?,?,'','',?,?,?,?,'plugin',?,?,?) ON CONFLICT(id) DO UPDATE SET "
                    "name=excluded.name,enabled=excluded.enabled,revision=excluded.revision,"
                    "updated=excluded.updated,projects_root=excluded.projects_root,"
                    "settings=excluded.settings",
                    (ident, name.strip(), manifest["runtime_type"], int(enabled), revision,
                     current["created"] if current else stamp, stamp, plugin, projects_root, encoded))
        except sqlite3.IntegrityError:
            raise BridgeError("Adapter name is already in use", "conflict") from None
        if not enabled or (current and revision != current["revision"]):
            self.plugins.stop(ident)
        return _public_adapter(self.adapter(ident))

    def plugin_client(self, row: dict, *, timeout: float = 30) -> RuntimeClient:
        process = self.plugins.process(row, self._manifest(row["plugin"]))
        return RuntimeClient(row["id"], row["runtime_type"], process, timeout=timeout)

    def save_adapter(self, payload: dict, adapter_id: str | None = None) -> dict:
        if not isinstance(payload, dict):
            raise BridgeError("Adapter object required", "invalid_arguments")
        current = self.adapter(adapter_id) if adapter_id else None
        if current is not None and current.get("kind", "http") != "plugin":
            return self._save_legacy_adapter(payload, current)
        if current is None and "plugin" not in payload:
            raise BridgeError(HTTP_REMOVED_MESSAGE, "adapter_removed")
        return self._save_plugin_adapter(payload, current)

    def _save_legacy_adapter(self, payload: dict, current: dict) -> dict:
        """A 0.3 HTTP adapter can only be renamed, disabled or deleted now."""
        if set(payload) - {"name", "enabled"}:
            raise BridgeError(HTTP_REMOVED_MESSAGE, "adapter_removed")
        name = payload.get("name", current["name"])
        enabled = payload.get("enabled", bool(current["enabled"]))
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 80:
            raise BridgeError("Invalid adapter name", "invalid_arguments")
        if not isinstance(enabled, bool):
            raise BridgeError("enabled must be a boolean", "invalid_arguments")
        try:
            with self.lock, self.db:
                self.db.execute("UPDATE runtime_adapters SET name=?,enabled=?,updated=? WHERE id=?",
                                (name.strip(), int(enabled), _now(), current["id"]))
        except sqlite3.IntegrityError:
            raise BridgeError("Adapter name is already in use", "conflict") from None
        return _public_adapter(self.adapter(current["id"]))

    def delete_adapter(self, adapter_id: str) -> dict:
        self.adapter(adapter_id)
        with self.lock, self.db:
            self.db.execute("DELETE FROM runtime_adapters WHERE id=?", (adapter_id,))
        self.plugins.stop(adapter_id)
        return {"deleted": adapter_id}

    def test_adapter(self, payload: dict) -> dict:
        if not isinstance(payload, dict) or not isinstance(payload.get("adapter_id"), str):
            raise BridgeError("Save the plugin adapter, then test it by id", "invalid_arguments")
        saved = self.adapter(payload["adapter_id"])
        if saved.get("kind", "http") != "plugin":
            return {"success": False, "runtime_type": saved["runtime_type"],
                    "code": "adapter_removed", "message": HTTP_REMOVED_MESSAGE}
        return self._test_plugin_adapter(saved)

    def _test_plugin_adapter(self, row: dict) -> dict:
        try:
            descriptor = self.plugin_client(row, timeout=10).descriptor()
        except BridgeError as exc:
            log = self.state / "plugins" / row["id"] / "plugin.log"
            return {"success": False, "runtime_type": row["runtime_type"], "code": exc.code,
                    "message": f"{exc} (details in {log})"}
        return {"success": True, "adapter_id": row["id"], "runtime_type": row["runtime_type"],
                "native_runtime": descriptor.runtime_id,
                "native_instance": descriptor.instance_id[:200],
                "adapter_version": descriptor.adapter_version,
                "native_version": descriptor.native_version,
                "protocol": 2, "features": descriptor.features}

    def runtime_call(self, adapter_id: str, operation: str, body: dict):
        allowed_ops = {"descriptor", "models", "create_conversation", "conversation", "start_run",
                       "find_run", "run", "cancel", "steer", "queue", "interactions", "resolve",
                       "activities", "activity", "events", "usage_limits"}
        if operation not in allowed_ops or not isinstance(body, dict):
            raise BridgeError("Unknown runtime operation", "not_found")
        if set(body) - {"arguments", "workspace", "expected_adapter_revision"}:
            raise BridgeError("Invalid runtime request envelope", "invalid_arguments")
        expected_revision = body.get("expected_adapter_revision")
        if (not isinstance(expected_revision, str)
                or not ADAPTER_REVISION_RE.fullmatch(expected_revision)):
            raise BridgeError("Expected adapter revision is required", "invalid_arguments")
        arguments = body.get("arguments")
        if not isinstance(arguments, dict):
            raise BridgeError("Runtime arguments must be an object", "invalid_arguments")
        workspace = body.get("workspace")
        if workspace is not None:
            if not isinstance(workspace, dict):
                raise BridgeError("Invalid workspace authority context", "invalid_arguments")
            workspace = self.authoritative_workspace(workspace)
            with self.safe_root(workspace) as safe:
                root = safe.path
            if operation == "models":
                if arguments.get("workspace_id") != workspace.get("id"):
                    raise BridgeError("Runtime workspace identity mismatch", "invalid_arguments")
                if arguments.get("directory") not in (None, root):
                    raise BridgeError("Runtime directory must match the Node workspace root", "root_denied")
            if operation == "create_conversation":
                payload = arguments.get("payload")
                if (not isinstance(payload, dict)
                        or payload.get("workspaceId") != workspace.get("id")
                        or payload.get("directory") != root):
                    raise BridgeError("Runtime conversation must use the authoritative Node workspace",
                                      "root_denied")
        # Keep the authoritative revision check and native client construction
        # in one Node-state critical section.  A concurrent adapter update can
        # therefore never cause a caller with an old revision to construct a
        # client for the replacement endpoint.
        with self.lock:
            adapter_row = self.adapter(adapter_id, require_enabled=True)
            if expected_revision != adapter_row["revision"]:
                raise BridgeError("The Node adapter configuration changed", "adapter_changed")
            if adapter_row.get("kind", "http") != "plugin":
                raise BridgeError(HTTP_REMOVED_MESSAGE, "adapter_removed")
            client = self.plugin_client(adapter_row,
                                        timeout=10 if operation == "usage_limits" else 30)
        args = arguments
        if operation == "descriptor":
            value = client.descriptor()
            result: dict = {"runtime_id": value.runtime_id, "display_name": value.display_name,
                    "adapter_version": value.adapter_version, "native_version": value.native_version,
                    "instance_id": value.instance_id, "features": value.features}
            if value.release is not None:
                result["release"] = value.release
            return result
        if operation == "models": return client.models(args["workspace_id"])
        if operation == "create_conversation": return client.create_conversation(args["payload"])
        if operation == "conversation": return client.conversation(args["conversation_id"])
        if operation == "start_run":
            from .run_images import validate_images
            payload = dict(args["payload"])
            images = validate_images(payload.pop("images", None))
            metadata = []
            if images:
                if workspace is None:
                    raise BridgeError("Images require authoritative workspace context", "root_denied")
                if not client.descriptor().supports("imageInput"):
                    raise BridgeError("Adapter does not support images", "unsupported_input")
                models = client.models(workspace["id"])
                selected = next((m for m in models if m["selector"] == payload.get("model")), None) if payload.get("model") else next((m for m in models if m.get("default")), None)
                if selected is None or "image" not in selected.get("inputModalities", []):
                    raise BridgeError("Model image support is unavailable", "unsupported_input")
                items = list(payload.get("input", []))
                for image in images:
                    decoded = self.workspace_call("read_file", {"workspace": workspace,
                        "arguments": {**image, "representation": "image"}})
                    metadata.append(decoded["metadata"])
                    items.append({"type": "image", "data": decoded["data"], "mimeType": decoded["mime_type"], "source": decoded["metadata"]})
                payload["input"] = items
            result = client.start_run(args["conversation_id"], payload)
            if images:
                result = {**result, "inputImages": metadata}
            return result
        if operation == "find_run": return client.find_run(args["conversation_id"], args["client_run_id"])
        if operation == "run": return client.run(args["run_id"])
        if operation == "cancel": return client.cancel(args["run_id"])
        if operation in {"steer", "queue"}:
            if set(args) != {"run_id", "input_items", "request_id"}:
                raise BridgeError("Invalid run input arguments", "invalid_arguments")
            return getattr(client, operation)(args["run_id"], args["input_items"], args["request_id"])
        if operation == "interactions": return client.interactions(args["run_id"])
        if operation == "resolve": return client.resolve(args["interaction_id"], args["response"])
        if operation == "activities": return client.activities(args["run_id"])
        if operation == "activity": return client.activity(args["activity_id"])
        if operation == "usage_limits": return client.usage_limits()
        if operation == "events": return client.events(after=args["after"], wait_ms=args.get("wait_ms", 0))
        raise BridgeError("Unknown runtime operation", "not_found")
