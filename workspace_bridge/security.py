"""POSIX descriptor-relative filesystem policy; never follow workspace symlinks.

This is an application access boundary, not a sandbox for hostile same-user
processes. A separate OS identity/container is required for that threat model.
"""
from __future__ import annotations
import fnmatch
import hashlib
import os
from pathlib import Path
import re
import secrets
import stat
import time
from contextlib import contextmanager
from typing import Callable, Iterator

HANDOFF = ".workspace-handoff"
MAX_WRITE = 256 * 1024
MAX_FILE = 512 * 1024
MAX_FILES = 10000
MAX_OUTPUT = 24000
DENY_NAMES = frozenset({
    ".git", ".hg", ".svn", ".ssh", ".aws", ".azure", ".kube", ".gnupg",
    ".claude", ".mcp.json", HANDOFF, ".config", ".npmrc", ".pypirc", ".netrc", ".DS_Store", "node_modules",
    ".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    "dist", "build", "target", ".next", ".nuxt", "coverage", "vendor",
})
DENY_GLOBS = (".env*", "*.pem", "*.key", "*.p12", "*.pfx", "*.keystore",
              "*.sqlite*", "*.db", "id_rsa*", "id_ed25519*", "*credentials*",
              "secrets", "secrets.*", "*.secret", "*.log")
SECRET_PATTERNS = (
    re.compile(r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----[\s\S]*?-----END (?:[A-Z ]+ )?PRIVATE KEY-----"),
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    re.compile(r"\b(?:sk-(?:proj-)?[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
    re.compile(r"(?i)(?:api[_-]?key|access[_-]?token|client[_-]?secret|password|authorization)\s*[\"']?\s*[:=]\s*[\"']?[^\s\"',;}{]{8,}"),
)

class BridgeError(Exception):
    def __init__(self, message: str, code: str = "policy_denied"):
        super().__init__(message)
        self.code = code


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def redact(text: str) -> tuple[str, bool]:
    original = text
    for pattern in SECRET_PATTERNS:
        text = pattern.sub(lambda m: "[REDACTED_SECRET]" + "\n" * m.group(0).count("\n"), text)
    return text, text != original


def parts(path: str, allow_empty: bool = False) -> list[str]:
    if allow_empty and path == "":
        return []
    if not isinstance(path, str) or not path or len(path) > 1024:
        raise BridgeError("Invalid relative path")
    if path.startswith("/") or "\\" in path or ":" in path or any(ord(c) < 32 or ord(c) == 127 for c in path):
        raise BridgeError("Only plain workspace-relative POSIX paths are accepted")
    result = path.split("/")
    if any(p in ("", ".", "..") or len(p.encode()) > 255 for p in result):
        raise BridgeError("Non-canonical path rejected")
    return result


def allowed(path: str, extra: list[str] | None = None) -> bool:
    try:
        ps = parts(path)
    except BridgeError:
        return False
    if any(p.casefold().startswith(".wb-write-") for p in ps):
        return False
    if any(p.lower() in {n.lower() for n in DENY_NAMES} or
           any(fnmatch.fnmatchcase(p.lower(), g) for g in DENY_GLOBS) for p in ps):
        return False
    if ps[0].casefold() == HANDOFF.casefold():
        return False
    return not any(fnmatch.fnmatchcase(path.casefold(), g.casefold()) or any(fnmatch.fnmatchcase(p.casefold(), g.casefold()) for p in ps)
                   for g in (extra or []))


def handoff_allowed(path: str, extra: list[str] | None = None, *, allow_root: bool = False) -> bool:
    """Explicit handoff access only; secret/build names and admin exclusions still win."""
    try:
        ps = parts(path)
    except BridgeError:
        return False
    if ps[0] != HANDOFF or (len(ps) == 1 and not allow_root):
        return False
    # Internal staging files must never be readable or caller-addressable.
    if any(p.casefold().startswith(".wb-write-") for p in ps[1:]):
        return False
    if len(ps) > 1 and not allowed("/".join(ps[1:]), extra):
        return False
    return not any(fnmatch.fnmatchcase(path.casefold(), g.casefold()) or
                   any(fnmatch.fnmatchcase(p.casefold(), g.casefold()) for p in ps)
                   for g in (extra or []))


WRITE_SCOPES = frozenset({"none", "handoff", "workspace"})


def write_allowed(path: str, extra: list[str] | None = None, *, scope: str = "handoff") -> bool:
    """Fail closed. Scope is local administrator policy, never an MCP argument."""
    if scope not in WRITE_SCOPES or scope == "none":
        return False
    if handoff_allowed(path, extra):
        return True
    return scope == "workspace" and allowed(path, extra)


def require_write_path(path: str, extra: list[str] | None = None, *, scope: str = "handoff"):
    if not write_allowed(path, extra, scope=scope):
        raise BridgeError("Write denied by workspace write scope or path exclusions", "policy_denied")


def file_text(data: bytes) -> str:
    """Writes reject binary/secret-bearing text, rather than silently redacting it."""
    if len(data) > MAX_WRITE:
        raise BridgeError("File exceeds the 256 KiB write limit", "too_large")
    try:
        text = data.decode("utf-8")
    except UnicodeError:
        raise BridgeError("Writes require UTF-8 text", "binary_file") from None
    if any((ord(c) < 32 and c not in "\n\r\t") or ord(c) == 127 for c in text):
        raise BridgeError("Binary/control characters are not accepted in files", "binary_file")
    if redact(text)[1]:
        raise BridgeError("Secret-like content cannot be written or replaced; remove it locally first", "secret_content")
    return text


def open_absolute_dir(path: str) -> int:
    """Open every ancestor without following symlinks, including the root itself."""
    if not os.path.isabs(path) or os.path.normpath(path) != path:
        raise BridgeError("Canonical absolute directory required")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for part in Path(path).parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except OSError:
        os.close(fd)
        raise BridgeError("Directory unavailable or symlink detected") from None


class SafeRoot:
    def __init__(self, path: str, identity: tuple[int, int] | None = None, extra: list[str] | None = None):
        # Authorization boundary is the configured canonical path itself.
        # `identity`, when supplied, is a legacy historical (st_dev, st_ino)
        # fingerprint retained only for backward-compatible callers and an
        # optional non-blocking diagnostic. It is never used to gate access:
        # a reboot/remount that changes device/inode for the same configured
        # path must remain usable. Per-request containment (canonical path,
        # O_NOFOLLOW resolution, same-device file checks, excludes, scopes)
        # is still enforced below.
        self.path = path
        self.fd = open_absolute_dir(path)
        self.extra = extra or []
        st = os.fstat(self.fd)
        self.identity = (st.st_dev, st.st_ino)
        if identity is not None and self.identity != identity:
            import logging
            logging.getLogger("workspace_bridge.root").debug(
                "workspace root filesystem identity changed for %s", path)

    def close(self):
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    @contextmanager
    def directory(self, ps: list[str], create: bool = False) -> Iterator[int]:
        fd = os.dup(self.fd)
        try:
            for name in ps:
                if create:
                    try:
                        os.mkdir(name, 0o700, dir_fd=fd)
                    except FileExistsError:
                        pass
                nxt = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
                st = os.fstat(nxt)
                if st.st_dev != self.identity[0]:
                    os.close(nxt)
                    raise BridgeError("Cross-device directory traversal rejected")
                os.close(fd)
                fd = nxt
            yield fd
        except OSError:
            raise BridgeError("Directory unavailable or unsafe") from None
        finally:
            os.close(fd)

    def read(self, path: str, *, artifact: bool = False, limit: int = MAX_FILE,
             limit_selector: Callable[[bytes], int] | None = None) -> tuple[bytes, os.stat_result]:
        ps = parts(path)
        if artifact and ps[0] != HANDOFF:
            raise BridgeError("Artifact path outside handoff folder")
        policy = handoff_allowed if ps[0] == HANDOFF else allowed
        if not policy(path, self.extra):
            raise BridgeError("Path excluded by workspace policy")
        with self.directory(ps[:-1]) as parent:
            try:
                fd = os.open(ps[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=parent)
            except FileNotFoundError:
                raise BridgeError("File not found", "not_found") from None
            except OSError:
                raise BridgeError("File unavailable or unsafe") from None
            try:
                st = os.fstat(fd)
                if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_dev != self.identity[0]:
                    raise BridgeError("Only single-link regular files on the root device may be read")
                # A trusted internal selector can grant the separate image budget
                # after sniffing the same already-authorized descriptor. Text limits
                # stay unchanged; never re-open an unchecked caller path to decode.
                prefix = os.read(fd, 32) if limit_selector is not None else b""
                if limit_selector is not None:
                    limit = limit_selector(prefix)
                if st.st_size > limit:
                    raise BridgeError("File exceeds configured read limit", "too_large")
                chunks, total = [prefix], len(prefix)
                while True:
                    chunk = os.read(fd, min(65536, limit + 1 - total))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    total += len(chunk)
                    if total > limit:
                        raise BridgeError("File exceeds configured read limit", "too_large")
                after = os.fstat(fd)
                if (st.st_size, st.st_mtime_ns, st.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                    raise BridgeError("File changed during read; retry when agent is idle", "workspace_busy")
                return b"".join(chunks), st
            finally:
                os.close(fd)

    def walk(self, path: str = "", max_entries: int = MAX_FILES, *, include_dirs: bool = False,
             depth_limit: int | None = None, metadata: bool = False) -> tuple[list[dict], list[dict]]:
        prefix = parts(path, allow_empty=True)
        # Root source browsing still omits handoffs; callers select that folder explicitly.
        policy = (lambda p, e: handoff_allowed(p, e, allow_root=True)) if prefix and prefix[0] == HANDOFF else allowed
        if prefix and not policy(path, self.extra):
            raise BridgeError("Directory excluded by workspace policy")
        entries, skipped = [], []
        count = 0
        started = time.monotonic()
        def visit(fd: int, rel: str, depth: int):
            nonlocal count
            if time.monotonic() - started > 5:
                raise BridgeError("Directory scan time budget exceeded; use a narrower path", "scan_limit")
            if depth > 40:
                raise BridgeError("Directory depth limit reached", "scan_limit")
            # Collect at most max_entries + 1 names; do not allocate an unbounded directory listing.
            with os.scandir(fd) as it:
                names = []
                for ent in it:
                    count += 1
                    if count > max_entries:
                        raise BridgeError("Workspace entry limit reached; add administrator exclusions", "scan_limit")
                    names.append(ent.name)
            for name in sorted(names):
                relative = f"{rel}/{name}" if rel else name
                if not policy(relative, self.extra):
                    continue
                try:
                    st = os.stat(name, dir_fd=fd, follow_symlinks=False)
                    if st.st_dev != self.identity[0] or stat.S_ISLNK(st.st_mode):
                        skipped.append({"path": relative, "reason": "symlink_or_mount"})
                    elif stat.S_ISDIR(st.st_mode):
                        if include_dirs:
                            entries.append({"path": relative, "type": "directory"})
                        if depth_limit is not None and depth + 1 >= depth_limit:
                            continue
                        child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
                        try:
                            if (os.fstat(child).st_dev, os.fstat(child).st_ino) != (st.st_dev, st.st_ino):
                                raise BridgeError("Directory changed during scan", "workspace_busy")
                            visit(child, relative, depth + 1)
                        finally:
                            os.close(child)
                    elif stat.S_ISREG(st.st_mode) and st.st_nlink == 1:
                        item = {"path": relative, "size": st.st_size, "mode": stat.S_IMODE(st.st_mode)}
                        if include_dirs:
                            item["type"] = "file"
                        if metadata:
                            item.update(mtime_ns=st.st_mtime_ns, ctime_ns=st.st_ctime_ns, ino=st.st_ino)
                        entries.append(item)
                    else:
                        skipped.append({"path": relative, "reason": "special_or_hardlinked"})
                except OSError:
                    raise BridgeError("Workspace changed or became unreadable during scan", "workspace_busy") from None
        with self.directory(prefix) as fd:
            visit(fd, path, 0)
        return sorted(entries, key=lambda x: x["path"]), skipped

    def create_artifact(self, path: str, data: bytes) -> str:
        """Append-only: caller supplies a server-generated fixed artifact path, never user paths."""
        ps = parts(path)
        if not handoff_allowed(path, self.extra):
            raise BridgeError("Invalid or excluded handoff artifact")
        file_text(data)
        with self.directory(ps[:-1], create=True) as fd:
            try:
                out = os.open(ps[-1], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                              0o600, dir_fd=fd)
            except FileExistsError:
                raise BridgeError("Artifact already exists; it was not overwritten", "conflict") from None
            try:
                view = memoryview(data)
                while view:
                    written = os.write(out, view)
                    view = view[written:]
                os.fsync(out)
            finally:
                os.close(out)
            os.fsync(fd)
        return digest(data)


    def write_file(self, path: str, data: bytes, expected_sha256: str | None = None, *, write_scope: str = "handoff") -> dict:
        """Bounded policy-scoped text writes; no in-place truncation or link following.

        The service lock serializes bridge calls. Hash checks detect ordinary stale
        local edits, but no portable compare-and-rename can lock out a hostile same-
        user process. Stop other writers; this is not an OS sandbox or a filesystem CAS.
        """
        require_write_path(path, self.extra, scope=write_scope)
        if expected_sha256 is not None and not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
            raise BridgeError("Invalid expected_sha256", "invalid_arguments")
        file_text(data)  # Validate before creating even a parent directory.
        ps = parts(path)
        def signature(st):
            return (st.st_dev, st.st_ino, st.st_mode, st.st_nlink, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
        with self.directory(ps[:-1], create=expected_sha256 is None) as parent:
            try:
                before = os.stat(ps[-1], dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                before = None
            if before is None:
                if expected_sha256 is not None:
                    raise BridgeError("Expected file is missing; re-read before writing", "stale_evidence")
            else:
                if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_dev != self.identity[0]:
                    raise BridgeError("Only single-link regular files may be replaced")
                if expected_sha256 is None:
                    raise BridgeError("File exists; read it and supply expected_sha256 to replace it", "conflict")
                previous, observed = self.read(path, limit=MAX_WRITE)
                if signature(before) != signature(observed) or digest(previous) != expected_sha256:
                    raise BridgeError("File changed; re-read before writing", "stale_evidence")
                file_text(previous)  # Never overwrite a redacted or binary local file.
            temporary = ".wb-write-" + secrets.token_hex(16)
            staged = False
            try:
                out = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                              0o600, dir_fd=parent)
                staged = True
                try:
                    view = memoryview(data)
                    while view:
                        written = os.write(out, view)
                        if written <= 0:
                            raise OSError("Write made no progress")
                        view = view[written:]
                    # Source replacement preserves ordinary permission bits (including
                    # executable bits), never setuid/setgid/sticky. Handoff notes and
                    # newly created files remain private. ACLs/xattrs are not copied.
                    mode = (before.st_mode & 0o777) if before is not None and ps[0] != HANDOFF else 0o600
                    os.fchmod(out, mode)
                    os.fsync(out)
                finally:
                    os.close(out)
                # Reopen from the pinned root to detect an ordinary moved/replaced parent.
                with self.directory(ps[:-1]) as current_parent:
                    a, b = os.fstat(parent), os.fstat(current_parent)
                    if (a.st_dev, a.st_ino) != (b.st_dev, b.st_ino):
                        raise BridgeError("Parent directory changed; re-read before writing", "stale_evidence")
                    if before is not None:
                        previous, observed = self.read(path, limit=MAX_WRITE)
                        if signature(before) != signature(observed) or digest(previous) != expected_sha256:
                            raise BridgeError("File changed during write; re-read", "stale_evidence")
                        try:
                            os.replace(temporary, ps[-1], src_dir_fd=parent, dst_dir_fd=parent)
                        except OSError:
                            raise BridgeError("File replacement failed; read the path before retrying", "write_failed") from None
                        staged = False
                    else:
                        # Publish a complete file without overwriting a concurrently created name.
                        try:
                            os.link(temporary, ps[-1], src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
                            os.unlink(temporary, dir_fd=parent)
                        except FileExistsError:
                            raise BridgeError("File was created concurrently; re-read before writing", "conflict") from None
                        except OSError:
                            raise BridgeError("File creation could not be confirmed; read before retrying", "write_failed") from None
                        staged = False
                    try:
                        os.fsync(parent)
                    except OSError:
                        raise BridgeError("File write durability could not be confirmed; read before retrying", "write_failed") from None
            except FileExistsError:
                raise BridgeError("File was created concurrently; re-read before writing", "conflict") from None
            except OSError:
                raise BridgeError("File write could not be confirmed; read the path before retrying", "write_failed") from None
            finally:
                if staged:
                    try:
                        os.unlink(temporary, dir_fd=parent)
                    except FileNotFoundError:
                        pass
        return {"path": path, "sha256": digest(data), "bytes": len(data),
                "created": before is None, "previous_sha256": expected_sha256}
