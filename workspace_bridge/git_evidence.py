"""Bounded, read-only Git evidence for one mapped workspace.

This module deliberately exposes only porcelain status and three fixed diff
modes. It never accepts a Git command, revision, or option from the caller.
Repository metadata stays behind SafeRoot's ordinary ``.git`` exclusion.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import selectors
import secrets
import shutil
import stat
import subprocess
import time
from dataclasses import dataclass

from .security import BridgeError, MAX_FILE, MAX_OUTPUT, SafeRoot, allowed, digest, parts, redact

STATUS_TIMEOUT = 4.0
DIFF_TIMEOUT = 5.0
MAX_STATUS_OUTPUT = 2 * 1024 * 1024
MAX_DIFF_OUTPUT = 8 * 1024 * 1024
MAX_GIT_STDERR = 64 * 1024
MAX_DIFF_PATHS = 512
MAX_PAGE_BYTES = 3000
DEFAULT_PAGE_BYTES = 3000
MAX_STATUS_ENTRIES = 10000
MAX_STATUS_FINGERPRINT_BYTES = 8 * 1024 * 1024
_SAFE_REF = re.compile(r"^[A-Za-z0-9._/-]{1,200}$")
_OID = re.compile(r"^[0-9a-f]{40,64}$")
_STATUS_HASH_KEY = secrets.token_bytes(32)


def _state_sha256(material: dict) -> str:
    payload = json.dumps(material, sort_keys=True, ensure_ascii=False,
                         separators=(",", ":")).encode("utf-8")
    return hmac.new(_STATUS_HASH_KEY, payload, hashlib.sha256).hexdigest()


@dataclass(frozen=True)
class _Change:
    kind: str
    path: str
    original_path: str | None
    index: str
    worktree: str
    submodule: bool
    modes: tuple[str, ...]
    oids: tuple[str, ...]
    conflict_stages: tuple[int, ...] = ()

    @property
    def conflicted(self) -> bool:
        return self.kind == "conflict"

    @property
    def untracked(self) -> bool:
        return self.kind == "untracked"


def _metadata_error() -> BridgeError:
    return BridgeError("Git metadata is unavailable or unsupported", "unsupported_repository_layout")


def _metadata_stat(parent_fd: int, name: str, device: int, *,
                   kind: str, optional: bool = False) -> os.stat_result | None:
    """Inspect one named metadata node without following links."""
    try:
        item = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        if optional:
            return None
        raise _metadata_error() from None
    except OSError:
        raise _metadata_error() from None

    valid_type = stat.S_ISDIR(item.st_mode) if kind == "directory" else stat.S_ISREG(item.st_mode)
    if (not valid_type or item.st_dev != device
            or (kind == "file" and item.st_nlink != 1)):
        raise _metadata_error()
    return item


def _open_metadata_directory(parent_fd: int, name: str, device: int, *,
                             optional: bool = False) -> int | None:
    item = _metadata_stat(parent_fd, name, device, kind="directory", optional=optional)
    if item is None:
        return None
    try:
        child_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                           dir_fd=parent_fd)
    except OSError:
        raise _metadata_error() from None
    opened = os.fstat(child_fd)
    if (not stat.S_ISDIR(opened.st_mode) or opened.st_dev != device
            or (opened.st_dev, opened.st_ino) != (item.st_dev, item.st_ino)):
        os.close(child_fd)
        raise BridgeError("Git metadata changed during inspection", "unsupported_repository_layout")
    return child_fd


def _open_metadata_file(parent_fd: int, name: str, device: int, *,
                        optional: bool = False) -> tuple[int, os.stat_result] | None:
    item = _metadata_stat(parent_fd, name, device, kind="file", optional=optional)
    if item is None:
        return None
    try:
        file_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                          dir_fd=parent_fd)
    except OSError:
        raise _metadata_error() from None
    opened = os.fstat(file_fd)
    if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
            or opened.st_dev != device
            or (opened.st_dev, opened.st_ino) != (item.st_dev, item.st_ino)):
        os.close(file_fd)
        raise BridgeError("Git metadata changed during inspection", "unsupported_repository_layout")
    return file_fd, opened


def _reject_metadata_marker(parent_fd: int, name: str) -> None:
    try:
        os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    except OSError:
        raise _metadata_error() from None
    raise _metadata_error()


def _safe_metadata(safe: SafeRoot) -> tuple[bool, int | None]:
    """Validate a direct .git directory without following metadata symlinks.

    A missing entry is an ordinary non-Git workspace. Unsupported layouts and
    unsafe metadata fail closed with a generic error that cannot disclose a
    repository path or config value.
    """
    try:
        root_stat = os.stat(".git", dir_fd=safe.fd, follow_symlinks=False)
    except FileNotFoundError:
        return False, None
    except OSError:
        raise BridgeError("Git metadata is unavailable or unsupported", "unsupported_repository_layout") from None
    if not stat.S_ISDIR(root_stat.st_mode) or root_stat.st_dev != safe.identity[0]:
        raise _metadata_error()

    try:
        git_fd = os.open(".git", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                         dir_fd=safe.fd)
    except OSError:
        raise _metadata_error() from None
    try:
        opened = os.fstat(git_fd)
        if (opened.st_dev, opened.st_ino) != (root_stat.st_dev, root_stat.st_ino):
            raise BridgeError("Git metadata changed during inspection", "unsupported_repository_layout")

        # These markers identify linked worktrees or shared object databases.
        # They are intentionally outside the supported v1 layout.
        for marker in ("gitdir", "commondir", "config.worktree"):
            _reject_metadata_marker(git_fd, marker)

        # Validate only paths that Git can use to locate repository config,
        # refs, the index, or object storage. Object IDs are numerous by
        # design, so do not recurse through loose objects, packs, or refs.
        refs_fd = _open_metadata_directory(git_fd, "refs", safe.identity[0])
        assert refs_fd is not None
        try:
            objects_fd = _open_metadata_directory(git_fd, "objects", safe.identity[0])
            assert objects_fd is not None
            try:
                info_fd = _open_metadata_directory(objects_fd, "info", safe.identity[0], optional=True)
                try:
                    pack_fd = _open_metadata_directory(objects_fd, "pack", safe.identity[0], optional=True)
                    if pack_fd is not None:
                        os.close(pack_fd)
                    if info_fd is not None:
                        for marker in ("alternates", "http-alternates"):
                            _reject_metadata_marker(info_fd, marker)
                finally:
                    if info_fd is not None:
                        os.close(info_fd)
            finally:
                os.close(objects_fd)
        finally:
            os.close(refs_fd)

        config_file = _open_metadata_file(git_fd, "config", safe.identity[0])
        assert config_file is not None
        config_fd, config_stat = config_file
        try:
            if config_stat.st_size > MAX_FILE:
                raise _metadata_error()
            chunks = bytearray()
            while len(chunks) <= config_stat.st_size:
                block = os.read(config_fd, min(65536, config_stat.st_size + 1 - len(chunks)))
                if not block:
                    break
                chunks.extend(block)
            if len(chunks) != config_stat.st_size:
                raise BridgeError("Git metadata changed during inspection", "unsupported_repository_layout")
            config = bytes(chunks).decode("utf-8")
        except FileNotFoundError:
            raise _metadata_error() from None
        except UnicodeError:
            raise _metadata_error() from None
        except OSError:
            raise _metadata_error() from None
        finally:
            os.close(config_fd)
        if re.search(r"(?im)^\s*\[\s*include(?:if)?(?:\s|\])", config):
            raise _metadata_error()

        # HEAD, index and packed refs are direct metadata inputs to status and
        # diff. The index and packed-refs file may not exist in new repositories.
        head_file = _open_metadata_file(git_fd, "HEAD", safe.identity[0])
        assert head_file is not None
        os.close(head_file[0])
        for name in ("index", "packed-refs", "shallow"):
            optional_file = _open_metadata_file(git_fd, name, safe.identity[0], optional=True)
            if optional_file is not None:
                os.close(optional_file[0])
        return True, os.dup(git_fd)
    finally:
        os.close(git_fd)


def _git_binary() -> str | None:
    candidate = shutil.which("git")
    if not candidate:
        return None
    try:
        resolved = os.path.realpath(candidate)
        st = os.stat(resolved, follow_symlinks=False)
    except OSError:
        return None
    return resolved if stat.S_ISREG(st.st_mode) and os.access(resolved, os.X_OK) else None


def _run(binary: str, safe: SafeRoot, git_dir: str, command: list[str], *,
         timeout: float, output_limit: int) -> bytes:
    """Run one fixed Git subcommand with bounded pipes and a minimal environment."""
    argv = [binary, "--no-pager", "--git-dir", git_dir, "--work-tree", ".",
            "-c", "core.fsmonitor=false", "-c", "core.quotepath=false",
            "-c", "core.pager=cat", *command]
    env = {
        "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
        "LC_ALL": "C",
        "LANG": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_PAGER": "cat",
        "PAGER": "cat",
        "GIT_ATTR_NOSYSTEM": "1",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_ALLOW_PROTOCOL": "",
    }
    try:
        process = subprocess.Popen(argv, cwd=safe.path, env=env, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   close_fds=True, start_new_session=True)
    except (OSError, ValueError):
        raise BridgeError("Git evidence could not be collected safely", "git_evidence_failed") from None

    stdout = bytearray()
    stderr_size = 0
    selector = selectors.DefaultSelector()
    assert process.stdout is not None and process.stderr is not None
    for stream, label in ((process.stdout, "stdout"), (process.stderr, "stderr")):
        os.set_blocking(stream.fileno(), False)
        selector.register(stream, selectors.EVENT_READ, label)
    started = time.monotonic()
    overflow = False
    try:
        while selector.get_map():
            remaining = timeout - (time.monotonic() - started)
            if remaining <= 0:
                process.kill()
                raise BridgeError("Git evidence collection timed out", "git_timeout")
            events = selector.select(min(remaining, 0.1))
            for key, _ in events:
                try:
                    read_limit = (output_limit + 1 - len(stdout) if key.data == "stdout"
                                  else MAX_GIT_STDERR + 1 - stderr_size)
                    block = os.read(key.fd, min(65536, read_limit))
                except BlockingIOError:
                    continue
                if not block:
                    selector.unregister(key.fileobj)
                    continue
                if key.data == "stdout":
                    stdout.extend(block)
                    if len(stdout) > output_limit:
                        overflow = True
                        process.kill()
                        break
                else:
                    stderr_size += len(block)
                    if stderr_size > MAX_GIT_STDERR:
                        overflow = True
                        process.kill()
                        break
            if overflow:
                break
        try:
            return_code = process.wait(timeout=max(0.1, timeout - (time.monotonic() - started)))
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            raise BridgeError("Git evidence collection timed out", "git_timeout") from None
    finally:
        selector.close()
        process.stdout.close()
        process.stderr.close()
        if process.poll() is None:
            process.kill()
            process.wait()
    if overflow:
        raise BridgeError("Git evidence exceeded its output bound", "output_limit")
    if return_code != 0:
        raise BridgeError("Git evidence could not be collected safely", "git_evidence_failed")
    return bytes(stdout)


def _split_status(data: bytes) -> tuple[dict, list[_Change]]:
    headers: dict[str, str] = {}
    changes: list[_Change] = []
    records = data.split(b"\0")
    index = 0
    try:
        while index < len(records):
            record = records[index]
            index += 1
            if not record:
                continue
            if record.startswith(b"# "):
                key, _, value = record[2:].partition(b" ")
                headers[key.decode("ascii")] = value.decode("utf-8")
                continue
            if record.startswith(b"? "):
                path = record[2:].decode("utf-8")
                changes.append(_Change("untracked", path, None, "?", "?", False, (), ()))
                continue
            if record.startswith(b"1 "):
                fields = record.split(b" ", 8)
                if len(fields) != 9:
                    raise ValueError
                _, xy, sub, mode_h, mode_i, mode_w, oid_h, oid_i, path_b = fields
                changes.append(_Change("ordinary", path_b.decode("utf-8"), None,
                    xy[:1].decode("ascii"), xy[1:2].decode("ascii"), sub.startswith(b"S"),
                    (mode_h.decode("ascii"), mode_i.decode("ascii"), mode_w.decode("ascii")),
                    (oid_h.decode("ascii"), oid_i.decode("ascii"))))
                continue
            if record.startswith(b"2 "):
                fields = record.split(b" ", 9)
                if len(fields) != 10 or index >= len(records):
                    raise ValueError
                _, xy, sub, mode_h, mode_i, mode_w, oid_h, oid_i, _score, path_b = fields
                original_b = records[index]
                index += 1
                changes.append(_Change("rename", path_b.decode("utf-8"), original_b.decode("utf-8"),
                    xy[:1].decode("ascii"), xy[1:2].decode("ascii"), sub.startswith(b"S"),
                    (mode_h.decode("ascii"), mode_i.decode("ascii"), mode_w.decode("ascii")),
                    (oid_h.decode("ascii"), oid_i.decode("ascii"))))
                continue
            if record.startswith(b"u "):
                fields = record.split(b" ", 10)
                if len(fields) != 11:
                    raise ValueError
                _, xy, sub, mode_1, mode_2, mode_3, mode_w, oid_1, oid_2, oid_3, path_b = fields
                modes = (mode_1.decode("ascii"), mode_2.decode("ascii"),
                         mode_3.decode("ascii"), mode_w.decode("ascii"))
                stages = tuple(i for i, mode in enumerate(modes[:3], 1) if mode != "000000")
                changes.append(_Change("conflict", path_b.decode("utf-8"), None,
                    xy[:1].decode("ascii"), xy[1:2].decode("ascii"), sub.startswith(b"S"), modes,
                    (oid_1.decode("ascii"), oid_2.decode("ascii"), oid_3.decode("ascii")), stages))
                continue
            raise ValueError
    except (UnicodeError, ValueError, IndexError):
        raise BridgeError("Git status format was unsupported", "git_evidence_failed") from None
    return headers, changes


def _path_ok(path: str, safe: SafeRoot) -> bool:
    return allowed(path, safe.extra)


def _metadata_fingerprint(safe: SafeRoot, path: str) -> list[int] | None:
    """Return private file metadata for stale checks without reading contents."""
    try:
        path_parts = parts(path)
        with safe.directory(path_parts[:-1]) as parent_fd:
            st = os.stat(path_parts[-1], dir_fd=parent_fd, follow_symlinks=False)
        return [st.st_mode, st.st_dev, st.st_ino, st.st_nlink, st.st_size,
                st.st_mtime_ns, st.st_ctime_ns]
    except (BridgeError, OSError):
        return None


def _content_fingerprint(safe: SafeRoot, path: str) -> str | None:
    try:
        raw, _ = safe.read(path, limit=MAX_FILE)
        return digest(raw)
    except BridgeError:
        return None


def _entry(change: _Change) -> dict:
    item = {
        "path": change.path,
        "index_status": change.index,
        "worktree_status": change.worktree,
        "staged": not change.untracked and change.index != ".",
        "unstaged": not change.untracked and change.worktree != ".",
        "untracked": change.untracked,
        "conflicted": change.conflicted,
        "submodule": change.submodule,
    }
    if change.original_path is not None:
        item["original_path"] = change.original_path
    if change.conflicted:
        item["conflict_stages"] = list(change.conflict_stages)
    return item


class GitEvidence:
    """Fixed-function read-only evidence collector tied to a Service lifetime."""

    @staticmethod
    def _repository(safe: SafeRoot) -> tuple[str, str] | None:
        exists, git_fd = _safe_metadata(safe)
        if not exists:
            return None
        assert git_fd is not None
        os.close(git_fd)
        binary = _git_binary()
        if binary is None:
            raise BridgeError("Git executable is unavailable", "git_unavailable")
        git_dir = str(Path(safe.path) / ".git")
        bare_config = _run(binary, safe, git_dir,
                           ["config", "--local", "--bool", "--default=false", "core.bare"],
                           timeout=STATUS_TIMEOUT, output_limit=128)
        if bare_config.strip() == b"true":
            raise BridgeError("Bare Git repositories are unsupported", "unsupported_repository_layout")
        if bare_config.strip() != b"false":
            raise BridgeError("Git metadata is unavailable or unsupported", "unsupported_repository_layout")
        bare = _run(binary, safe, git_dir, ["rev-parse", "--is-bare-repository"],
                    timeout=STATUS_TIMEOUT, output_limit=128)
        if bare.strip() == b"true":
            raise BridgeError("Bare Git repositories are unsupported", "unsupported_repository_layout")
        if bare.strip() != b"false":
            raise BridgeError("Git metadata is unavailable or unsupported", "unsupported_repository_layout")
        top = _run(binary, safe, git_dir, ["rev-parse", "--show-toplevel"],
                   timeout=STATUS_TIMEOUT, output_limit=4096).strip()
        try:
            resolved = os.path.realpath(top.decode("utf-8"))
        except UnicodeError:
            raise BridgeError("Git worktree metadata is unsupported", "unsupported_repository_layout") from None
        if resolved != os.path.realpath(safe.path):
            raise BridgeError("Git worktree metadata is unsupported", "unsupported_repository_layout")
        return binary, git_dir

    @staticmethod
    def _status_state(safe: SafeRoot, ws: dict) -> dict:
        repository = GitEvidence._repository(safe)
        policy = {"workspace": ws["id"], "root": hashlib.sha256(safe.path.encode()).hexdigest(),
                  "excludes": sorted(safe.extra, key=lambda value: value.casefold())}
        if repository is None:
            material = {"available": False, "policy": policy, "hidden_count": 0}
            return {"available": False, "reason": "not_a_repository", "entries": [],
                    "hidden_count": 0,
                    "status_sha256": _state_sha256(material)}
        binary, git_dir = repository
        raw = _run(binary, safe, git_dir,
                    ["status", "--porcelain=v2", "--branch", "--untracked-files=all", "--ignore-submodules=all", "-z"],
                    timeout=STATUS_TIMEOUT, output_limit=MAX_STATUS_OUTPUT)
        headers, changes = _split_status(raw)
        if len(changes) > MAX_STATUS_ENTRIES:
            raise BridgeError("Git status exceeds its entry bound", "output_limit")
        oid = headers.get("branch.oid", "")
        head = oid if _OID.fullmatch(oid) else None
        raw_branch = headers.get("branch.head", "")
        detached = raw_branch == "(detached)"
        branch = raw_branch if _SAFE_REF.fullmatch(raw_branch) and raw_branch not in {".", ".."} else None
        raw_upstream = headers.get("branch.upstream", "")
        upstream = raw_upstream if _SAFE_REF.fullmatch(raw_upstream) and ".." not in raw_upstream else None
        ahead = behind = None
        ahead_behind = headers.get("branch.ab", "")
        match = re.fullmatch(r"\+(\d+) -(\d+)", ahead_behind)
        if match and upstream:
            ahead, behind = int(match.group(1)), int(match.group(2))

        visible: list[tuple[_Change, dict]] = []
        hidden_count = 0
        hidden_state: list[dict] = []
        fingerprint_budget = MAX_STATUS_FINGERPRINT_BYTES
        for change in changes:
            paths_ok = _path_ok(change.path, safe) and (
                change.original_path is None or _path_ok(change.original_path, safe))
            row_state = {"kind": change.kind, "index": change.index, "worktree": change.worktree,
                         "submodule": change.submodule, "modes": change.modes, "oids": change.oids,
                         "conflict_stages": change.conflict_stages}
            if not paths_ok:
                hidden_count += 1
                row_state["path"] = change.path
                row_state["original_path"] = change.original_path
                row_state["metadata"] = _metadata_fingerprint(safe, change.path)
                hidden_state.append(row_state)
                continue
            public = _entry(change)
            # Content hashes make same-status edits stale for paths the normal
            # file policy allows. Unsafe, missing, or oversized files still
            # contribute only safe descriptor metadata.
            row_state["path"] = change.path
            row_state["original_path"] = change.original_path
            metadata = _metadata_fingerprint(safe, change.path)
            size = metadata[4] if metadata is not None else None
            if size is not None and size <= min(MAX_FILE, fingerprint_budget):
                row_state["content"] = _content_fingerprint(safe, change.path)
                if row_state["content"] is not None:
                    fingerprint_budget -= size
            if row_state.get("content") is None:
                row_state["metadata"] = metadata
            visible.append((change, {"public": public, "state": row_state}))

        visible.sort(key=lambda pair: pair[0].path.encode("utf-8"))
        hidden_state.sort(key=lambda row: json.dumps(row, sort_keys=True,
                                                      separators=(",", ":")))
        state_material = {
            "version": 1,
            "policy": policy,
            "head": head,
            "branch": branch,
            "detached": detached,
            "upstream": upstream,
            "ahead": ahead,
            "behind": behind,
            "visible": [pair[1]["state"] for pair in visible],
            "hidden_count": hidden_count,
            "hidden_state": hidden_state,
        }
        status_hash = _state_sha256(state_material)
        return {
            "available": True,
            "branch": branch,
            "detached": detached,
            "head": head,
            "upstream": upstream,
            "ahead": ahead,
            "behind": behind,
            "ahead_behind_source": "local_refs" if ahead is not None else None,
            "entries": [pair[1]["public"] for pair in visible],
            "hidden_count": hidden_count,
            "status_sha256": status_hash,
            "_binary": binary,
            "_git_dir": git_dir,
            "_changes": [pair[0] for pair in visible],
        }

    @staticmethod
    def status(safe: SafeRoot, ws: dict, *, offset: int = 0, limit: int = 50,
               expected_status_sha256: str | None = None) -> dict:
        state = GitEvidence._status_state(safe, ws)
        observed = state["status_sha256"]
        if expected_status_sha256 is not None and observed != expected_status_sha256:
            raise BridgeError("Git status changed; request a fresh status page", "stale_evidence")
        entries = state.get("entries", [])
        page = entries[offset:offset + limit]
        result = {key: value for key, value in state.items() if not key.startswith("_") and key != "entries"} | {
            "entries": page,
            "offset": offset,
            "limit": limit,
            "total_visible": len(entries),
            "next_offset": offset + len(page) if offset + len(page) < len(entries) else None,
        }
        while len(page) > 1 and len(json.dumps(result, ensure_ascii=False,
                                               separators=(",", ":"))) > MAX_OUTPUT - 256:
            page.pop()
            result["entries"] = page
            result["next_offset"] = offset + len(page) if offset + len(page) < len(entries) else None
        if page and len(json.dumps(result, ensure_ascii=False,
                                   separators=(",", ":"))) > MAX_OUTPUT - 256:
            raise BridgeError("One Git status entry exceeds the output budget", "output_limit")
        return result

    @staticmethod
    def _diff_paths(safe: SafeRoot, changes: list[_Change], mode: str,
                    requested: str | None) -> list[str]:
        candidates: list[_Change] = []
        for change in changes:
            if change.conflicted or change.submodule or change.untracked:
                continue
            if change.index == "." and mode == "staged":
                continue
            if change.worktree == "." and mode == "worktree":
                continue
            names = [change.path] + ([change.original_path] if change.original_path is not None else [])
            if not all(_path_ok(name, safe) for name in names):
                continue
            # Normal reads reject links and special files. Do not let a diff
            # expose a symlink target or a mount outside the mapped workspace.
            if any(name in change.modes for name in ("120000", "160000")):
                continue
            readable = True
            for name in names:
                try:
                    safe.read(name, limit=MAX_FILE)
                except BridgeError as exc:
                    if exc.code != "not_found":
                        readable = False
                        break
            if not readable:
                continue
            candidates.append(change)

        if requested is not None:
            try:
                parts(requested)
            except BridgeError:
                raise BridgeError("Requested path is unavailable for Git diff", "path_denied") from None
            if not _path_ok(requested, safe):
                raise BridgeError("Requested path is unavailable for Git diff", "path_denied")
            candidates = [change for change in candidates
                          if requested == change.path or requested == change.original_path]
            if not candidates:
                raise BridgeError("Requested path is unavailable or unchanged in this diff mode", "not_found")

        names: set[str] = set()
        for change in candidates:
            names.add(change.path)
            if change.original_path is not None:
                names.add(change.original_path)
        if len(names) > MAX_DIFF_PATHS:
            raise BridgeError("Too many changed paths for one bounded Git diff", "output_limit")
        return sorted(names, key=lambda value: value.encode("utf-8"))

    @staticmethod
    def diff(safe: SafeRoot, ws: dict, *, mode: str, path: str | None = None,
             offset: int = 0, max_bytes: int = DEFAULT_PAGE_BYTES,
             expected_status_sha256: str | None = None) -> dict:
        if type(max_bytes) is not int or not 256 <= max_bytes <= MAX_PAGE_BYTES:
            raise BridgeError("Diff page size exceeds the output budget", "output_limit")
        state = GitEvidence._status_state(safe, ws)
        if not state["available"]:
            raise BridgeError("Git evidence is unavailable for this workspace", "repository_unavailable")
        observed = state["status_sha256"]
        if expected_status_sha256 is not None and observed != expected_status_sha256:
            raise BridgeError("Git status changed; request a fresh status page", "stale_evidence")
        changes = state["_changes"]
        paths = GitEvidence._diff_paths(safe, changes, mode, path)
        binary, git_dir = state["_binary"], state["_git_dir"]
        if mode == "head" and state["head"] is None:
            raise BridgeError("HEAD diff is unavailable before the first commit", "repository_unavailable")
        if paths:
            literal_paths = [":(literal)" + path_value for path_value in paths]
            base = ["diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--no-color",
                    "--no-relative", "--ignore-submodules=all"]
            if mode == "head":
                args = [*base, "HEAD", "--", *literal_paths]
            elif mode == "staged":
                args = [*base, "--cached", *( ["HEAD"] if state["head"] is not None else ["--root"] ), "--", *literal_paths]
            else:
                args = [*base, "--", *literal_paths]
            raw = _run(binary, safe, git_dir, args, timeout=DIFF_TIMEOUT, output_limit=MAX_DIFF_OUTPUT)
        else:
            raw = b""
        after = GitEvidence._status_state(safe, ws)
        if after["status_sha256"] != observed:
            raise BridgeError("Git status changed while collecting the diff; request a fresh status page",
                              "stale_evidence")
        text, redacted = redact(raw.decode("utf-8", errors="replace"))
        patch = text.encode("utf-8")
        start = min(offset, len(patch))
        while start < len(patch) and start > 0 and patch[start] & 0xC0 == 0x80:
            start += 1
        end = start
        while end < len(patch):
            byte = patch[end]
            width = 1 if byte < 0x80 else 2 if byte < 0xE0 else 3 if byte < 0xF0 else 4
            if end + width > len(patch) or end + width - start > max_bytes:
                break
            end += width
        if end == start and start < len(patch):
            raise BridgeError("Requested diff page limit cannot fit one character", "output_limit")
        page = patch[start:end].decode("utf-8", errors="strict")
        return {
            "available": True,
            "mode": mode,
            "patch": page,
            "offset": start,
            "next_offset": end if end < len(patch) else None,
            "total_bytes": len(patch),
            "redacted": redacted,
            "status_sha256": observed,
        }
