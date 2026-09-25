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
from .git_evidence import GitEvidence
from .media import (DEFAULT_DIMENSION, SUPPORTED_SUFFIXES, ImageReadResult,
                    image_capabilities, read_image, selected_read_limit, sniff_image)
from .security import (BridgeError, HANDOFF, MAX_FILE, MAX_OUTPUT, MAX_WRITE,
                       SafeRoot, allowed, digest, file_text, handoff_allowed,
                       parts, redact, require_write_path)
from .wbrp import HttpRuntimeAdapter
from .adapter_registry import validate_base_url
from . import __version__


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _inside(path: Path, parent: Path) -> bool:
    return path == parent or parent in path.parents


def _public_adapter(row: dict) -> dict:
    return {"id": row["id"], "name": row["name"],
            "runtime_type": row["runtime_type"], "base_url": row["base_url"],
            "enabled": bool(row["enabled"]), "revision": row["revision"],
            "created": row["created"], "updated": row["updated"],
            "has_token": bool(row["token"])}


ADAPTER_REVISION_RE = re.compile(r"^rev_[0-9a-f]{32}$")


class NodeService:
    """Node-owned safe workspace operations and runtime adapter registry."""

    def __init__(self, state: Path, config: dict, *, read_only: bool = False):
        self.state = state.expanduser().resolve()
        self.config = config
        self.read_only = read_only
        self.allowed_roots = [Path(root).expanduser().resolve(strict=not read_only)
                              for root in config["allowed_roots"]]
        self.lock = threading.RLock()
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
                runtime_type TEXT NOT NULL CHECK(runtime_type IN ('pi','codex')),
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
        self.browser = Browser(self)

    def close(self) -> None:
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
        excludes = workspace.get("excludes", [])
        if not isinstance(excludes, list) or len(excludes) > 40 or any(
                not isinstance(value, str) or not value or len(value) > 120
                for value in excludes):
            raise BridgeError("Invalid workspace exclusions", "invalid_arguments")
        return SafeRoot(raw, None, excludes)

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
        self.validate_root(row["root"], excludes)
        if (not isinstance(excludes, list) or len(excludes) > 40
                or any(not isinstance(item, str) or not item or len(item) > 120
                       for item in excludes)
                or write_scope not in {"none", "handoff", "workspace"}):
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
        return {"status": "ok", "protocol": 1, "node_version": __version__,
                "release": node_release(), "capabilities": [
            "files", "search", "git", "handoff-artifacts", "runtime-adapters"],
            "allowed_roots": roots,
            "runtime_adapters": len(self.adapters())}

    def workspace_call(self, operation: str, body: dict):
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
        if operation == "write_file":
            args = body["arguments"]
            scope = workspace.get("write_scope", "none")
            require_write_path(args["path"], workspace.get("excludes", []), scope=scope)
            try:
                data = args["content"].encode("utf-8")
            except (AttributeError, UnicodeEncodeError):
                raise BridgeError("Content must be valid UTF-8 text", "invalid_arguments") from None
            file_text(data)
            with self.safe_root(ws) as safe:
                result = safe.write_file(args["path"], data,
                                         args.get("expected_sha256"), write_scope=scope)
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
                text = file_text(raw)
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
                file_text(updated_data)
                result = safe.write_file(args["path"], updated_data,
                                         args["expected_sha256"], write_scope=scope)
            return {**result, "absolute_path": str(Path(root) / args["path"]),
                    "replacements": 1, "write_scope": scope}
        if operation == "git_status":
            with self.safe_root(ws) as safe:
                return GitEvidence.status(safe, ws, **body["arguments"])
        if operation == "git_diff":
            with self.safe_root(ws) as safe:
                return GitEvidence.diff(safe, ws, **body["arguments"])
        if operation == "read_handoff_artifact":
            args = body["arguments"]
            path = args["path"]
            if not handoff_allowed(path, workspace.get("excludes", [])):
                raise BridgeError("Artifact path outside handoff folder")
            with self.safe_root(ws) as safe:
                data, _ = safe.read(path, artifact=True)
            return {"data": __import__("base64").b64encode(data).decode("ascii")}
        if operation == "hash_files":
            paths = body["arguments"].get("paths")
            if not isinstance(paths, list) or len(paths) > 100:
                raise BridgeError("Invalid context file list", "invalid_arguments")
            result, total = {}, 0
            with self.safe_root(ws) as safe:
                for path in paths:
                    if not isinstance(path, str):
                        raise BridgeError("Invalid context file list", "invalid_arguments")
                    try:
                        data, _ = safe.read(path, limit_selector=selected_read_limit)
                    except BridgeError:
                        raise BridgeError("A context file is unavailable or excluded; re-read before planning",
                                          "stale_context") from None
                    total += len(data)
                    if total > 64 * 1024 * 1024:
                        raise BridgeError("Referenced context exceeds 64 MiB; use fewer context hashes",
                                          "context_limit")
                    result[path] = digest(data)
            return {"hashes": result}
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
        return {"adapters": [_public_adapter(row) for row in self.adapters()]}

    def save_adapter(self, payload: dict, adapter_id: str | None = None) -> dict:
        allowed_fields = {"name", "runtime_type", "base_url", "token", "enabled"}
        if not isinstance(payload, dict) or set(payload) - allowed_fields:
            raise BridgeError("Unexpected adapter fields", "invalid_arguments")
        current = self.adapter(adapter_id) if adapter_id else None
        name = payload.get("name", current["name"] if current else "")
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 80:
            raise BridgeError("Invalid adapter name", "invalid_arguments")
        runtime_type = payload.get("runtime_type", current["runtime_type"] if current else None)
        if runtime_type not in {"pi", "codex"} or (current and runtime_type != current["runtime_type"]):
            raise BridgeError("Adapter runtime type is immutable", "invalid_arguments")
        base_url = validate_base_url(payload.get("base_url", current["base_url"] if current else None))
        token = payload.get("token", "")
        if not isinstance(token, str) or len(token) > 4096:
            raise BridgeError("Invalid adapter token", "invalid_arguments")
        if current and not token:
            token = current["token"]
        if not token:
            raise BridgeError("Adapter token is required", "invalid_arguments")
        enabled = payload.get("enabled", bool(current["enabled"]) if current else True)
        if not isinstance(enabled, bool):
            raise BridgeError("enabled must be a boolean", "invalid_arguments")
        stamp = _now()
        ident = current["id"] if current else "adapter_" + secrets.token_hex(12)
        revision = current["revision"] if current else "rev_" + secrets.token_hex(16)
        if current and (base_url != current["base_url"] or token != current["token"]):
            revision = "rev_" + secrets.token_hex(16)
        try:
            with self.lock, self.db:
                self.db.execute("INSERT INTO runtime_adapters(id,name,runtime_type,base_url,token,enabled,revision,created,updated) "
                                "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,"
                                "base_url=excluded.base_url,token=excluded.token,enabled=excluded.enabled,"
                                "revision=excluded.revision,updated=excluded.updated",
                                (ident, name.strip(), runtime_type, base_url, token, int(enabled), revision,
                                 current["created"] if current else stamp, stamp))
        except sqlite3.IntegrityError:
            raise BridgeError("Adapter name is already in use", "conflict") from None
        os.chmod(self.state / "node.sqlite3", 0o600)
        return _public_adapter(self.adapter(ident))

    def delete_adapter(self, adapter_id: str) -> dict:
        self.adapter(adapter_id)
        with self.lock, self.db:
            self.db.execute("DELETE FROM runtime_adapters WHERE id=?", (adapter_id,))
        return {"deleted": adapter_id}

    def test_adapter(self, payload: dict) -> dict:
        if not isinstance(payload, dict) or set(payload) - {
                "adapter_id", "name", "runtime_type", "base_url", "token", "enabled"}:
            raise BridgeError("Unexpected adapter test fields", "invalid_arguments")
        adapter_id = payload.get("adapter_id")
        saved = self.adapter(adapter_id) if adapter_id else None
        runtime_type = payload.get("runtime_type", saved["runtime_type"] if saved else None)
        if runtime_type not in {"pi", "codex"} or (saved and runtime_type != saved["runtime_type"]):
            raise BridgeError("Invalid or immutable runtime type", "invalid_arguments")
        base_url = validate_base_url(payload.get("base_url", saved["base_url"] if saved else None))
        token = payload.get("token", "")
        if saved and not token:
            token = saved["token"]
        if not isinstance(token, str) or not token or len(token) > 4096:
            raise BridgeError("Adapter token is required", "invalid_arguments")
        test_id = adapter_id or "adapter_" + secrets.token_hex(12)
        try:
            descriptor = HttpRuntimeAdapter(test_id, runtime_type, base_url, token,
                                            timeout=5).descriptor()
        except BridgeError as exc:
            return {"success": False, "runtime_type": runtime_type,
                    "code": exc.code, "message": "Connection could not be verified."}
        result: dict = {"success": True, "adapter_id": adapter_id, "runtime_type": runtime_type,
                "native_runtime": descriptor.runtime_id,
                "native_instance": descriptor.instance_id[:200],
                "adapter_version": descriptor.adapter_version,
                "native_version": descriptor.native_version,
                "protocol": 1, "features": descriptor.features}
        if descriptor.release is not None:
            result["release"] = descriptor.release
        return result

    def runtime_call(self, adapter_id: str, operation: str, body: dict):
        allowed_ops = {"descriptor", "models", "profile_catalog", "profiles", "save_profile",
                       "delete_profile", "create_conversation", "conversation",
                       "rebind_conversation", "start_run",
                       "find_run", "run", "cancel", "steer", "interactions", "resolve",
                       "activities", "activity", "events"}
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
            if operation in {"models", "profile_catalog", "profiles"}:
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
            client = HttpRuntimeAdapter(adapter_row["id"], adapter_row["runtime_type"],
                                        adapter_row["base_url"], adapter_row["token"], timeout=30)
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
        if operation == "profile_catalog": return client.profile_catalog(
            args.get("workspace_id"), args.get("directory"), fresh=bool(args.get("fresh", False)))
        if operation == "profiles": return client.profiles(
            args.get("workspace_id"), args.get("directory"), fresh=bool(args.get("fresh", False)))
        if operation == "save_profile": return client.save_profile(
            args["profile_id"], args["config"], args.get("expected_revision"))
        if operation == "delete_profile": return client.delete_profile(args["profile_id"])
        if operation == "rebind_conversation":
            binding = args.get("security_binding")
            conv = args.get("conversation_id")
            if (not isinstance(conv, str) or not conv or len(conv) > 200
                    or not isinstance(binding, dict) or binding.get("source") != "profile"
                    or not isinstance(binding.get("profile"), dict)
                    or not isinstance(binding["profile"].get("id"), str)
                    or not binding["profile"]["id"] or len(binding["profile"]["id"]) > 100
                    or not isinstance(binding["profile"].get("revision"), str)
                    or not binding["profile"]["revision"]
                    or len(binding["profile"]["revision"]) > 100
                    or set(binding) != {"source", "profile"}
                    or set(binding["profile"]) != {"id", "revision"}):
                raise BridgeError("Invalid security rebind binding", "invalid_arguments")
            return client.rebind_conversation(conv, binding)
        if operation == "create_conversation": return client.create_conversation(args["payload"])
        if operation == "conversation": return client.conversation(args["conversation_id"])
        if operation == "start_run": return client.start_run(args["conversation_id"], args["payload"])
        if operation == "find_run": return client.find_run(args["conversation_id"], args["client_run_id"])
        if operation == "run": return client.run(args["run_id"])
        if operation == "cancel": return client.cancel(args["run_id"])
        if operation == "steer": return client.steer(args["run_id"], args["input_items"])
        if operation == "interactions": return client.interactions(args["run_id"])
        if operation == "resolve": return client.resolve(args["interaction_id"], args["response"])
        if operation == "activities": return client.activities(args["run_id"])
        if operation == "activity": return client.activity(args["activity_id"])
        if operation == "events": return client.events(after=args["after"], wait_ms=args.get("wait_ms", 0))
        raise BridgeError("Unknown runtime operation", "not_found")
