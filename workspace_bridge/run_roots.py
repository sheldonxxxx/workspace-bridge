"""Which folders a plugin may start an agent in."""
from __future__ import annotations

from pathlib import Path
from typing import Any


def optional_root(raw: Any) -> Path | None:
    """The Node's worktrees folder from ``initialize``, when it sent one."""
    if not isinstance(raw, str) or not raw:
        return None
    path = Path(raw).resolve(strict=True)
    if not path.is_dir():
        raise ValueError("Worktrees root must be a directory")
    return path


def run_directory(requested: Any, projects_root: Path, worktrees_root: Path | None) -> str:
    """Resolve an agent's working folder: inside the projects root, or a Node-owned run worktree.

    Raises ValueError (or OSError) for anything else.
    """
    if not isinstance(requested, str) or not requested:
        raise ValueError("Workspace directory is required")
    cwd = Path(requested).resolve(strict=True)
    if not cwd.is_dir():
        raise ValueError("Workspace directory is not a folder")
    if cwd.is_relative_to(projects_root):
        return str(cwd)
    # <worktrees root>/<workspace>/<worktree>: a run's own checkout, never the folder above it.
    if worktrees_root is not None and cwd.parent.parent == worktrees_root:
        return str(cwd)
    raise ValueError("Workspace directory is outside the configured projects root")
