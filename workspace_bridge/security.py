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
import stat
import time
from collections import Counter
from contextlib import contextmanager
from typing import Callable, Iterator

HANDOFF = ".workspace-handoff"
MAX_WRITE = 256 * 1024
MAX_FILE = 512 * 1024
MAX_FILES = 10000
MAX_OUTPUT = 24000
# Credential, VCS and Bridge-internal names are always denied, including the
# handoff folder 0.3 kept in workspaces.
ALWAYS_DENY_NAMES = frozenset({
    ".git", ".hg", ".svn", ".ssh", ".aws", ".azure", ".kube", ".gnupg",
    ".claude", ".mcp.json", HANDOFF, ".config", ".npmrc", ".pypirc", ".netrc",
})
ALWAYS_DENY_GLOBS = (".env*", "*.pem", "*.key", "*.p12", "*.pfx", "*.keystore",
                     "*.sqlite*", "*.db", "id_rsa*", "id_ed25519*", "*credentials*",
                     "secrets", "secrets.*", "*.secret")
# Generated and dependency names are hidden by default; a workspace may show
# one with a "!name" exclusion entry naming it exactly.
DEFAULT_DENY_NAMES = frozenset({
    ".DS_Store", "node_modules", ".venv", "venv", "__pycache__", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", "dist", "build", "target", ".next", ".nuxt",
    "coverage", "vendor",
})
DEFAULT_DENY_GLOBS = ("*.log",)
DENY_NAMES = ALWAYS_DENY_NAMES | DEFAULT_DENY_NAMES
DENY_GLOBS = ALWAYS_DENY_GLOBS + DEFAULT_DENY_GLOBS
OVERRIDABLE = {name.casefold(): name for name in (*DEFAULT_DENY_NAMES, *DEFAULT_DENY_GLOBS)}
_ALWAYS_FOLDED = frozenset(name.casefold() for name in ALWAYS_DENY_NAMES)
MAX_EXCLUDES = 40
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


def validate_excludes(excludes: object) -> list[str]:
    """Extra deny patterns, plus "!name" entries that show one default-denied name."""
    if not isinstance(excludes, list) or len(excludes) > MAX_EXCLUDES or any(
            not isinstance(value, str) or not value or len(value) > 120 for value in excludes):
        raise BridgeError("Invalid workspace exclusions", "invalid_arguments")
    for value in excludes:
        if value.startswith("!") and value[1:].casefold() not in OVERRIDABLE:
            raise BridgeError(f"{value[1:]!r} is not a default-hidden name that can be shown; "
                              "credential and version-control paths are always denied",
                              "invalid_arguments")
    return excludes


def _split_excludes(extra: list[str] | None) -> tuple[list[str], frozenset[str]]:
    denies = [value for value in extra or [] if not value.startswith("!")]
    shown = frozenset(value[1:].casefold() for value in extra or [] if value.startswith("!"))
    return denies, shown


def _default_denied(name: str, shown: frozenset[str]) -> bool:
    folded = name.casefold()
    if folded in _ALWAYS_FOLDED or any(
            fnmatch.fnmatchcase(folded, g) for g in ALWAYS_DENY_GLOBS):
        return True
    return any(entry not in shown and (folded == entry or fnmatch.fnmatchcase(folded, entry))
               for entry in OVERRIDABLE)


def _extra_denied(path: str, ps: list[str], denies: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(path.casefold(), g.casefold()) or
               any(fnmatch.fnmatchcase(p.casefold(), g.casefold()) for p in ps)
               for g in denies)


def allowed(path: str, extra: list[str] | None = None) -> bool:
    try:
        ps = parts(path)
    except BridgeError:
        return False
    if any(p.casefold().startswith(".wb-write-") for p in ps):
        return False
    denies, shown = _split_excludes(extra)
    if any(_default_denied(p, shown) for p in ps):
        return False
    if ps[0].casefold() == HANDOFF.casefold():
        return False
    return not _extra_denied(path, ps, denies)


WRITE_SCOPES = frozenset({"none", "workspace"})


def write_allowed(path: str, extra: list[str] | None = None, *, scope: str = "none") -> bool:
    """Fail closed. Scope is local administrator policy, never an MCP argument."""
    return scope == "workspace" and allowed(path, extra)


def require_write_path(path: str, extra: list[str] | None = None, *, scope: str = "none"):
    if not write_allowed(path, extra, scope=scope):
        raise BridgeError("Write denied by workspace write scope or path exclusions", "policy_denied")


def text_content(data: bytes, limit: int = MAX_WRITE) -> str:
    """UTF-8 text without binary or control characters."""
    if len(data) > limit:
        raise BridgeError("File exceeds the 256 KiB write limit", "too_large")
    try:
        text = data.decode("utf-8")
    except UnicodeError:
        raise BridgeError("Writes require UTF-8 text", "binary_file") from None
    if any((ord(c) < 32 and c not in "\n\r\t") or ord(c) == 127 for c in text):
        raise BridgeError("Binary/control characters are not accepted in files", "binary_file")
    return text


def secret_matches(text: str) -> Counter:
    return Counter(match.group(0) for pattern in SECRET_PATTERNS for match in pattern.finditer(text))


def adds_secrets(before: str | None, after: str) -> bool:
    """True when after holds a secret-like match that before did not hold as often."""
    found = secret_matches(after)
    if not found:
        return False
    existing = secret_matches(before) if before else Counter()
    return any(count > existing[match] for match, count in found.items())


def file_text(data: bytes, previous: bytes | None = None) -> str:
    """Writes reject binary text and newly added secret-like content, never redacting it.

    Matches already present in ``previous`` stay, so a file holding one remains editable.
    """
    text = text_content(data)
    before = None
    if previous is not None:
        try:
            before = previous.decode("utf-8")
        except UnicodeError:
            before = None
    if adds_secrets(before, text):
        raise BridgeError("The change adds secret-like content; add it locally instead", "secret_content")
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

    def read(self, path: str, *, limit: int = MAX_FILE,
             limit_selector: Callable[[bytes], int] | None = None) -> tuple[bytes, os.stat_result]:
        ps = parts(path)
        if not allowed(path, self.extra):
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
        if prefix and not allowed(path, self.extra):
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
                if not allowed(relative, self.extra):
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
