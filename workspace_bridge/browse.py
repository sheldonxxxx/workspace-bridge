"""Bounded code-browsing tools. No shell, subprocess, native glob, or file writes.

Globs filter a SafeRoot inventory, never resolve paths themselves. Regex matching
has per-line and overall time budgets. Signed cursors bind a query to one workspace
and one observed inventory; they cannot be reused to switch workspaces.
"""
from __future__ import annotations

import base64
import fnmatch
import hashlib
import hmac
import json
import time
from functools import lru_cache
from typing import TYPE_CHECKING

import regex

from .security import BridgeError, MAX_FILE, MAX_OUTPUT, digest, parts, redact

if TYPE_CHECKING:
    from .service import Service


def packed(value) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()


def glob_matcher(pattern: str):
    # Plain relative POSIX patterns only. Patterns are never filesystem paths.
    if not isinstance(pattern, str) or len(pattern) > 256:
        raise BridgeError("Invalid glob pattern", "invalid_glob")
    ps = parts(pattern)
    if len(ps) > 32 or any("**" in p and p != "**" for p in ps) or any(c in pattern for c in "{}"):
        raise BridgeError("Use *, ?, [], and whole-segment ** globs; braces are not supported", "invalid_glob")

    def match(path: str) -> bool:
        xs = path.split("/")
        @lru_cache(maxsize=2048)
        def go(i: int, j: int) -> bool:
            if i == len(ps):
                return j == len(xs)
            if ps[i] == "**":
                return go(i + 1, j) or (j < len(xs) and go(i, j + 1))
            return j < len(xs) and fnmatch.fnmatchcase(xs[j], ps[i]) and go(i + 1, j + 1)
        return go(0, 0)
    return match


class Browser:
    def __init__(self, service: Service):
        self.service = service
        # Private, high-entropy key material already outside all mapped roots.
        self.cursor_key = hashlib.sha256((service.config["admin_token_hash"] + ":browse-v1").encode()).digest()

    def _cursor(self, payload: dict) -> str:
        raw = packed(payload)
        return base64.urlsafe_b64encode(raw + hmac.digest(self.cursor_key, raw, "sha256")).decode().rstrip("=")

    def _decode(self, value: str, scope: str, inventory: str) -> tuple[int, int]:
        try:
            if len(value) > 2048:
                raise ValueError()
            data = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
            raw, mac = data[:-32], data[-32:]
            if not hmac.compare_digest(mac, hmac.digest(self.cursor_key, raw, "sha256")):
                raise ValueError()
            item = json.loads(raw)
            if item["q"] != scope:
                raise ValueError()
            if item["tree"] != inventory:
                raise BridgeError("Search inventory changed; restart without cursor", "stale_cursor")
            fi, li = item["f"], item["l"]
            if type(fi) is not int or type(li) is not int or fi < 0 or li < 0:
                raise ValueError()
            return fi, li
        except BridgeError:
            raise
        except (ValueError, KeyError, TypeError):
            raise BridgeError("Invalid cursor for this query and workspace", "invalid_cursor") from None

    @staticmethod
    def _relative(path: str, base: str) -> str:
        return path[len(base) + 1:] if base else path

    @staticmethod
    def _page(entries: list[dict], skipped: list[dict], *, scope: dict, offset: int, limit: int,
              expected_listing_sha256: str | None) -> dict:
        fingerprint = digest(packed({"scope": scope, "entries": entries, "skipped": skipped}))
        if expected_listing_sha256 and expected_listing_sha256 != fingerprint:
            raise BridgeError("Listing changed; restart at offset 0", "stale_listing")
        page = []
        # Bound serialized bytes, not merely number of filesystem entries.
        for entry in entries[offset:offset + limit]:
            if len(packed(page + [entry])) > MAX_OUTPUT - 2000:
                break
            page.append(entry)
        end = offset + len(page)
        return {"entries": page, "total": len(entries), "next_offset": end if end < len(entries) else None,
                "listing_sha256": fingerprint, "skipped_count": len(skipped), "listing_is_live": True,
                "trust": "untrusted_project_metadata"}

    def list_dir(self, ws: dict, path: str = "", depth: int = 1, offset: int = 0, limit: int = 60,
                 expected_listing_sha256: str | None = None) -> dict:
        with self.service.safe_root(ws) as safe:
            entries, skipped = safe.walk(path, include_dirs=True, depth_limit=depth)
        result = self._page(entries, skipped, scope={"workspace_id": ws["id"], "path": path, "depth": depth,
                            "policy": ws["excludes"]}, offset=offset, limit=limit,
                            expected_listing_sha256=expected_listing_sha256)
        return {"path": path, "depth": depth, **result,
                "note": "Directories at the requested depth are listed but not descended. Exclusions remain enforced."}

    def glob(self, ws: dict, pattern: str, path: str = "", offset: int = 0, limit: int = 60,
             expected_listing_sha256: str | None = None) -> dict:
        match = glob_matcher(pattern)
        with self.service.safe_root(ws) as safe:
            entries, skipped = safe.walk(path)
        entries = [e for e in entries if match(self._relative(e["path"], path))]
        result = self._page(entries, skipped, scope={"workspace_id": ws["id"], "path": path, "pattern": pattern,
                            "policy": ws["excludes"]}, offset=offset, limit=limit,
                            expected_listing_sha256=expected_listing_sha256)
        return {"path": path, "pattern": pattern, **result}

    def grep_files(self, ws: dict, pattern: str, path: str = "", include: str = "**/*",
                   fixed_strings: bool = False, case_sensitive: bool = False, context_lines: int = 0,
                   limit: int = 40, cursor: str | None = None) -> dict:
        match_file = glob_matcher(include)
        try:
            expression = regex.compile(regex.escape(pattern) if fixed_strings else pattern,
                                       regex.VERSION1 | (0 if case_sensitive else regex.IGNORECASE))
        except (regex.error, OverflowError):
            raise BridgeError("Invalid regular expression", "invalid_regex") from None
        total_bytes, files_read = 0, 0
        matches, skipped_files = [], []
        scope = digest(packed({"workspace_id": ws["id"], "root_identity": [ws["dev"], ws["ino"]],
                               "policy": ws["excludes"], "pattern": pattern, "path": path,
                               "include": include, "fixed_strings": fixed_strings,
                               "case_sensitive": case_sensitive, "context_lines": context_lines}))
        with self.service.safe_root(ws) as safe:
            all_entries, unsafe = safe.walk(path, metadata=True)
            entries = [e for e in all_entries if match_file(self._relative(e["path"], path))]
            inventory = digest(packed({"entries": entries, "unsafe": unsafe, "scope": scope}))
            fi, li = self._decode(cursor, scope, inventory) if cursor else (0, 0)
            if fi > len(entries):
                raise BridgeError("Cursor is outside this inventory", "invalid_cursor")
            # Traversal has its own bound. Start a fresh matching budget so a
            # slow inventory scan cannot produce a forever-repeating cursor.
            started = time.monotonic()
            stop_reason = None
            while fi < len(entries):
                if files_read >= 100 or time.monotonic() - started > 5:
                    stop_reason = "scan_budget"
                    break
                ent = entries[fi]
                if ent["size"] > MAX_FILE:
                    skipped_files.append({"path": ent["path"], "reason": "too_large"})
                    fi, li = fi + 1, 0
                    # Skipped entries count toward scan budget too.
                    files_read += 1
                    continue
                if total_bytes + ent["size"] > 8 * 1024 * 1024:
                    stop_reason = "byte_budget"
                    break
                data, st = safe.read(ent["path"])
                if (st.st_size, st.st_mtime_ns, st.st_ctime_ns, st.st_ino) != (
                        ent["size"], ent["mtime_ns"], ent["ctime_ns"], ent["ino"]):
                    raise BridgeError("File changed during search; restart without cursor", "stale_cursor")
                total_bytes += len(data)
                files_read += 1
                try:
                    text = data.decode("utf-8")
                    if "\x00" in text:
                        raise UnicodeError()
                    text, redacted = redact(text)
                except UnicodeError:
                    skipped_files.append({"path": ent["path"], "reason": "binary_or_non_utf8"})
                    fi, li = fi + 1, 0
                    continue
                lines, sha = text.splitlines(), digest(data)
                if li > len(lines):
                    raise BridgeError("Cursor line is no longer valid", "stale_cursor")
                while li < len(lines):
                    if time.monotonic() - started > 5:
                        stop_reason = "time_budget"
                        break
                    line = lines[li]
                    try:
                        hit = expression.search(line, timeout=0.02)
                    except TimeoutError:
                        raise BridgeError("Regex exceeded its time budget; simplify the pattern", "regex_timeout") from None
                    if hit is not None:
                        start = max(0, hit.start() - 100)
                        end = min(len(line), start + 400)
                        item = {"path": ent["path"], "line": li + 1, "column": hit.start() + 1,
                                "text": line[start:end], "text_truncated": start > 0 or end < len(line),
                                "sha256": sha, "redacted": redacted}
                        if context_lines:
                            item["context"] = [{"line": j + 1, "text": lines[j][:400],
                                                 "text_truncated": len(lines[j]) > 400}
                                                for j in range(max(0, li - context_lines), min(len(lines), li + context_lines + 1))]
                        if len(packed(matches + [item])) > MAX_OUTPUT - 4000:
                            stop_reason = "output_budget"
                            break  # Current line has NOT been consumed.
                        matches.append(item)
                    li += 1
                    if len(matches) >= limit:
                        stop_reason = "match_limit"
                        break
                if li >= len(lines):
                    fi, li = fi + 1, 0
                if stop_reason:
                    break
            next_cursor = self._cursor({"q": scope, "tree": inventory, "f": fi, "l": li}) if fi < len(entries) else None
        # Keep skipped-path output bounded too; counts make omissions explicit.
        shown_skips = []
        for entry in skipped_files:
            if len(packed(shown_skips + [entry])) > 1700:
                break
            shown_skips.append(entry)
        return {"matches": matches, "next_cursor": next_cursor, "search_complete": next_cursor is None,
                "stop_reason": stop_reason if next_cursor else None, "inventory_files": len(entries),
                "files_read_or_skipped_this_page": files_read, "bytes_read_this_page": total_bytes,
                "unsafe_entry_count": len(unsafe), "skipped_files": shown_skips,
                "skipped_this_page": len(skipped_files), "skipped_list_truncated": len(shown_skips) < len(skipped_files),
                "scope": "Policy-filtered UTF-8 text only. Repository .gitignore does not define access policy.",
                "trust": "untrusted_project_content"}
