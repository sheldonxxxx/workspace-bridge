"""Isolated Git worktrees for agent runs, owned by the Node.

A run started with ``isolation: "worktree"`` works in its own linked worktree
at ``<state>/worktrees/<workspace>/<worktree_id>``, on branch
``wb/<worktree_id>``. The branch starts from a snapshot commit of the
workspace: HEAD plus every uncommitted change the workspace policy allows,
built in a temporary index, so the user's index, HEAD, branches and files are
untouched. The Node commits the worktree to its branch, applies the branch's
changes to the workspace through the edit journal, and removes the worktree
and branch. It touches only ``refs/heads/wb/<worktree_id>`` and its own
worktree entries, with hooks off and filter drivers neutralized.
"""
from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path

from . import edit_journal
from .git_evidence import GitEvidence, _run
from .security import BridgeError, MAX_FILE, SafeRoot, allowed, digest, require_write_path

WORKTREE_ID = re.compile(r"wt_[0-9a-f]{24}")
BRANCH_PREFIX = "wb/"
GIT_TIMEOUT = 120.0
MAX_LISTING = 64 * 1024 * 1024
MAX_APPLY_FILES = 1000
MAX_APPLY_BYTES = 64 * 1024 * 1024
MAX_LISTED = 20
_OID = re.compile(rb"[0-9a-f]{40}(?:[0-9a-f]{24})?")
_IDENTITY = {"GIT_AUTHOR_NAME": "Workspace Bridge", "GIT_AUTHOR_EMAIL": "workspace-bridge@localhost",
             "GIT_COMMITTER_NAME": "Workspace Bridge", "GIT_COMMITTER_EMAIL": "workspace-bridge@localhost",
             # Changed paths come back from Git itself; never read them as globs.
             "GIT_LITERAL_PATHSPECS": "1"}
# Node-owned Git operations never run repository hooks.
_NO_HOOKS = ("-c", f"core.hooksPath={os.devnull}")


def _unavailable(message: str) -> BridgeError:
    return BridgeError(message, "isolation_unavailable")


def _shown(paths: list[str]) -> str:
    more = len(paths) - MAX_LISTED
    return ", ".join(paths[:MAX_LISTED]) + (f" and {more} more" if more > 0 else "")


class _Repo:
    """The workspace repository and how to run Git on it or on one of its worktrees."""

    def __init__(self, safe: SafeRoot):
        try:
            found = GitEvidence._repository(safe)
        except BridgeError as exc:
            raise _unavailable(f"Worktree isolation needs a plain Git repository at the workspace root: {exc}") from None
        if found is None:
            raise _unavailable("Worktree isolation needs a Git repository at the workspace root")
        self.binary, self.git_dir, overrides = found
        self.overrides = (*overrides, *_NO_HOOKS)
        self.root = safe.path

    def git(self, *command: str, cwd: str | None = None, git_dir: str | None = None,
            env: dict[str, str] | None = None, limit: int = MAX_LISTING) -> bytes:
        try:
            return _run(self.binary, None, git_dir or self.git_dir, list(command), timeout=GIT_TIMEOUT,
                        output_limit=limit, overrides=self.overrides, cwd=cwd or self.root,
                        env_extra={**_IDENTITY, **(env or {})})
        except BridgeError as exc:
            if exc.code == "output_limit":
                raise
            raise BridgeError(f"Git could not complete {command[0]} for the run's worktree",
                              "worktree_failed") from None

    def oid(self, *command: str, **kwargs) -> str | None:
        """One object ID from a rev-parse style command; None when Git finds none."""
        try:
            value = self.git(*command, limit=4096, **kwargs).strip()
        except BridgeError:
            return None
        return value.decode("ascii") if _OID.fullmatch(value) else None


def _names(raw: bytes) -> list[str]:
    return [os.fsdecode(item) for item in raw.split(b"\0") if item]


def _stage_allowed(repo: _Repo, excludes: list[str], head: str | None, **where) -> str:
    """Stage every change policy allows into the index ``where`` names; return its tree."""
    repo.git("add", "-A", **where)
    if head is None:
        staged = _names(repo.git("ls-files", "-z", "--cached", **where))
    else:
        staged = _names(repo.git("diff-index", "--cached", "--name-only", "-z", "--no-renames", head, **where))
    # Credentials, version-control paths and admin exclusions stay as HEAD has them.
    denied = [path for path in staged if not allowed(path, excludes)]
    if denied:
        if head is None:
            repo.git("rm", "--cached", "-q", "--ignore-unmatch", "--", *denied, **where)
        else:
            repo.git("reset", "-q", head, "--", *denied, **where)
    tree = repo.git("write-tree", limit=4096, **where).strip()
    if not _OID.fullmatch(tree):
        raise BridgeError("Git returned no tree for the worktree", "worktree_failed")
    return tree.decode("ascii")


class WorktreeStore:
    def __init__(self, state: Path):
        self.root = state / "worktrees"

    def ensure_root(self) -> Path:
        self.root.mkdir(mode=0o700, exist_ok=True)
        return self.root

    @staticmethod
    def _id(worktree_id: object) -> str:
        if not isinstance(worktree_id, str) or not WORKTREE_ID.fullmatch(worktree_id):
            raise BridgeError("Invalid worktree_id", "invalid_arguments")
        return worktree_id

    def path(self, workspace_id: str, worktree_id: str) -> Path:
        return self.root / workspace_id / self._id(worktree_id)

    def _meta_path(self, workspace_id: str, worktree_id: str) -> Path:
        return self.root / workspace_id / f".{self._id(worktree_id)}.json"

    def meta(self, workspace_id: str, worktree_id: str) -> dict:
        try:
            meta = json.loads(self._meta_path(workspace_id, worktree_id).read_text())
        except (OSError, ValueError):
            raise BridgeError("The run's worktree is gone", "worktree_unavailable") from None
        if not self.path(workspace_id, worktree_id).is_dir():
            raise BridgeError("The run's worktree is gone", "worktree_unavailable")
        return meta

    def owns(self, workspace_id: str, directory: object) -> bool:
        """True when ``directory`` is a live worktree this Node made for the workspace."""
        if not isinstance(directory, str):
            return False
        path = Path(directory)
        if path.parent != self.root / workspace_id or not WORKTREE_ID.fullmatch(path.name):
            return False
        try:
            self.meta(workspace_id, path.name)
        except BridgeError:
            return False
        return True

    def repository(self, safe: SafeRoot, workspace_id: str, worktree_id: str) -> tuple[str, str, tuple[str, ...]]:
        """What checkpoint listings need to run Git inside the worktree."""
        repo = _Repo(safe)
        return repo.binary, self.meta(workspace_id, worktree_id)["gitdir"], repo.overrides

    def create(self, safe: SafeRoot, ws: dict, worktree_id: object) -> dict:
        worktree_id = self._id(worktree_id)
        repo = _Repo(safe)
        folder = self.ensure_root() / ws["id"]
        folder.mkdir(mode=0o700, exist_ok=True)
        target = folder / worktree_id
        if target.exists() or self._meta_path(ws["id"], worktree_id).exists():
            raise BridgeError("This worktree already exists", "conflict")
        branch = BRANCH_PREFIX + worktree_id
        head = repo.oid("rev-parse", "--verify", "-q", "HEAD^{commit}")
        index = folder / f".{worktree_id}.index"
        try:
            # A copy of the user's index keeps Git's stat cache; their own stays as it is.
            if (Path(repo.git_dir) / "index").is_file():
                shutil.copyfile(Path(repo.git_dir) / "index", index)
            tree = _stage_allowed(repo, safe.extra, head, env={"GIT_INDEX_FILE": str(index)})
        finally:
            index.unlink(missing_ok=True)
        parents = ["-p", head] if head else []
        base = repo.git("commit-tree", "--no-gpg-sign", tree, *parents, "-m",
                        f"Workspace Bridge snapshot for {worktree_id}", limit=4096).strip().decode("ascii")
        # An empty old value creates the branch only if it does not exist yet.
        repo.git("update-ref", f"refs/heads/{branch}", base, "")
        try:
            repo.git("worktree", "add", "--quiet", str(target), branch)
            gitdir = (target / ".git").read_text().removeprefix("gitdir:").strip()
            if Path(gitdir).resolve().parent != (Path(repo.git_dir) / "worktrees").resolve():
                raise BridgeError("Git placed the worktree metadata unexpectedly", "worktree_failed")
            meta = {"base": base, "branch": branch, "gitdir": gitdir}
            meta_path = self._meta_path(ws["id"], worktree_id)
            meta_path.write_text(json.dumps(meta))
            os.chmod(meta_path, 0o600)
        except BaseException:
            self._remove(repo, ws["id"], worktree_id)
            raise
        return {"worktree_id": worktree_id, "path": str(target), "branch": branch, "base": base}

    def commit(self, safe: SafeRoot, ws: dict, worktree_id: object) -> dict:
        """Record the worktree's files on its branch; the tip is unchanged when nothing changed."""
        worktree_id = self._id(worktree_id)
        repo = _Repo(safe)
        meta = self.meta(ws["id"], worktree_id)
        where = {"cwd": str(self.path(ws["id"], worktree_id)), "git_dir": meta["gitdir"]}
        head = repo.oid("rev-parse", "--verify", "-q", "HEAD^{commit}", **where)
        if head is None:
            raise BridgeError("The run's worktree has no commit", "worktree_failed")
        tree = _stage_allowed(repo, safe.extra, head, **where)
        ref = f"refs/heads/{meta['branch']}"
        if tree != repo.oid("rev-parse", "--verify", "-q", f"{head}^{{tree}}", **where):
            commit = repo.git("commit-tree", "--no-gpg-sign", tree, "-p", head, "-m",
                              f"Workspace Bridge: state of {worktree_id}", limit=4096, **where).strip()
            repo.git("update-ref", ref, commit.decode("ascii"), **where)
        tip = repo.oid("rev-parse", "--verify", "-q", ref)
        if tip is None:
            raise BridgeError("The run's branch is gone", "worktree_unavailable")
        return {"worktree_id": worktree_id, "branch": meta["branch"], "base": meta["base"], "tip": tip}

    def _changes(self, repo: _Repo, base: str, tip: str) -> list[dict]:
        raw = repo.git("diff-tree", "-r", "-z", "--no-renames", "--raw", base, tip)
        records = raw.split(b"\0")
        changes = []
        for index in range(0, len(records) - 1, 2):
            head, path = records[index], records[index + 1]
            fields = head.lstrip(b":").split(b" ")
            if len(fields) != 5:
                raise BridgeError("Git returned an unexpected change list", "worktree_failed")
            src_mode, dst_mode, src, dst, status = (item.decode("ascii") for item in fields)
            changes.append({"path": os.fsdecode(path), "status": status,
                            "before": None if set(src) == {"0"} else src, "before_mode": src_mode,
                            "after": None if set(dst) == {"0"} else dst, "after_mode": dst_mode})
        return changes

    def apply(self, safe: SafeRoot, journal: edit_journal.EditJournal, ws: dict, worktree_id: object,
              message: object) -> dict:
        """Apply the branch's changes since its snapshot to the workspace, all or none.

        Each workspace file must still hold what the snapshot recorded (or
        already hold the run's result). Nothing changes when any file differs
        or cannot be applied; the branch keeps the work for a manual merge.
        """
        message = edit_journal._check_message("chatgpt", message)
        state = self.commit(safe, ws, worktree_id)
        repo = _Repo(safe)
        changes = self._changes(repo, state["base"], state["tip"])
        if len(changes) > MAX_APPLY_FILES:
            raise BridgeError(f"The run changed more than {MAX_APPLY_FILES} files; merge branch "
                              f"{state['branch']} by hand", "too_large")
        excludes, scope = ws.get("excludes", []), ws.get("write_scope", "none")
        plan, refused, conflicts, already, total = [], [], [], [], 0
        for change in changes:
            path = change["path"]
            try:
                require_write_path(path, excludes, scope=scope)
            except BridgeError:
                refused.append(path)
                continue
            modes = {change["before_mode"], change["after_mode"]} - {"000000"}
            if not modes <= {"100644", "100755"}:
                refused.append(path)  # symlinks and submodules
                continue
            try:
                before = None if change["before"] is None else repo.git(
                    "cat-file", "blob", change["before"], limit=MAX_FILE)
                after = None if change["after"] is None else repo.git(
                    "cat-file", "blob", change["after"], limit=MAX_FILE)
            except BridgeError as exc:
                if exc.code != "output_limit":
                    raise
                refused.append(path)
                continue
            total += len(after or b"")
            if total > MAX_APPLY_BYTES:
                raise BridgeError(f"The run's changes exceed {MAX_APPLY_BYTES >> 20} MiB; merge branch "
                                  f"{state['branch']} by hand", "too_large")
            current = edit_journal._read_current(safe, path, MAX_FILE)
            observed = None if current.data is None else digest(current.data)
            executable = change["after_mode"] == "100755"
            if observed == (None if after is None else digest(after)) and (
                    after is None or bool((current.mode or 0) & 0o100) == executable):
                already.append(path)
                continue
            if observed != (None if before is None else digest(before)):
                conflicts.append(path)
                continue
            if after is None:
                op, mode = "delete", None
            elif current.data is None:
                op, mode = "create", 0o755 if executable else 0o644
            else:
                op = "edit"
                mode = (current.mode | 0o111) if executable else (current.mode & ~0o111)
            plan.append(edit_journal.FileChange(path, op, current.data, current.mode, current.signature,
                                                after, mode))
        if refused or conflicts:
            parts = []
            if conflicts:
                parts.append("changed in the workspace since the run started: " + _shown(conflicts))
            if refused:
                parts.append("not appliable here (excluded, protected, symlink, submodule or too large): "
                             + _shown(refused))
            raise BridgeError("Nothing was applied. Files " + "; files ".join(parts) + f". Branch "
                              f"{state['branch']} keeps the run's work for a manual merge, or discard_run "
                              "removes it.", "apply_conflict")
        edit_id = folders = None
        if plan:
            edit_id, folders = edit_journal._commit(safe, journal, ws["id"], "chatgpt", message, "patch", plan)
        removed = self._remove(repo, ws["id"], state["worktree_id"])
        return {"edit_id": edit_id, "message": message, "files": [item.public() for item in plan],
                "already_applied": already, "created_folders": folders or [], "branch": state["branch"],
                "base": state["base"], "tip": state["tip"], "worktree_removed": removed,
                "undo": "undo_edit restores these files while they still match the new hashes."
                        if edit_id else None}

    def _remove(self, repo: _Repo | None, workspace_id: str, worktree_id: str) -> bool:
        target = self.path(workspace_id, worktree_id)
        removed = True
        if repo is not None:
            try:
                if target.exists():
                    repo.git("worktree", "remove", "--force", "--force", str(target))
            except BridgeError:
                removed = False
            ref = f"refs/heads/{BRANCH_PREFIX}{worktree_id}"
            if repo.oid("rev-parse", "--verify", "-q", ref) is not None:
                try:
                    repo.git("update-ref", "-d", ref)
                except BridgeError:
                    removed = False
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
            removed = removed and not target.exists()
            if repo is not None:
                try:
                    repo.git("worktree", "prune")
                except BridgeError:
                    pass
        self._meta_path(workspace_id, worktree_id).unlink(missing_ok=True)
        return removed and not target.exists()

    def remove(self, safe: SafeRoot | None, ws: dict, worktree_id: object) -> dict:
        """Remove the worktree, its branch and its record; repeating it is harmless."""
        worktree_id = self._id(worktree_id)
        try:
            repo = _Repo(safe) if safe is not None else None
        except BridgeError:
            repo = None  # The repository is gone; still drop the Node's copy.
        return {"worktree_id": worktree_id, "removed": self._remove(repo, ws["id"], worktree_id)}
