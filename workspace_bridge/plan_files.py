"""Read-only copies of handoff plans in Node state.

The Bridge keeps each handoff's text. The Node writes a copy under
``<state>/handoffs/<workspace>/<job>/`` so an agent that lost its context can
re-read its plan, and a run gets read access to its own handoff's folder and
nothing else in Node state. Every write restores the published text, so a
changed copy never outlives the next run start.
"""
from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

from .security import BridgeError, file_text

DOCUMENTS = ("TASK.md", "CONTEXT.md", "ACCEPTANCE.md")
IDENT = re.compile(r"[A-Za-z0-9_-]{1,80}")
MAX_DOCUMENT_BYTES = 1024 * 1024


def _write(target: Path, data: bytes) -> bool:
    """Make ``target`` a read-only regular file holding ``data``; True when it changed."""
    if target.is_file() and not target.is_symlink():
        try:
            if target.read_bytes() == data:
                os.chmod(target, 0o400)
                return False
        except OSError:
            pass
    elif target.is_dir() and not target.is_symlink():
        shutil.rmtree(target)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        os.chmod(temporary, 0o400)
        os.replace(temporary, target)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return True


class PlanStore:
    def __init__(self, state: Path):
        self.root = state / "handoffs"

    def write(self, workspace_id: object, job_id: object, documents: object) -> dict:
        if not isinstance(workspace_id, str) or not IDENT.fullmatch(workspace_id) \
                or not isinstance(job_id, str) or not IDENT.fullmatch(job_id):
            raise BridgeError("Invalid handoff identity", "invalid_arguments")
        if not isinstance(documents, dict) or set(documents) != set(DOCUMENTS):
            raise BridgeError("A plan needs TASK.md, CONTEXT.md and ACCEPTANCE.md", "invalid_arguments")
        prepared = {}
        for name in DOCUMENTS:
            text = documents[name]
            if not isinstance(text, str):
                raise BridgeError("Plan documents must be text", "invalid_arguments")
            data = text.encode("utf-8")
            if len(data) > MAX_DOCUMENT_BYTES:
                raise BridgeError("A plan document is too large", "invalid_arguments")
            file_text(data)
            prepared[name] = data
        folder = self.root / workspace_id / job_id
        try:
            for directory in (self.root, self.root / workspace_id, folder):
                if directory.is_symlink():
                    directory.unlink()
                directory.mkdir(mode=0o700, exist_ok=True)
                os.chmod(directory, 0o700)
            restored = [name for name in DOCUMENTS if _write(folder / name, prepared[name])]
        except OSError:
            raise BridgeError("The Node could not save the plan files", "plan_unavailable") from None
        return {"folder": str(folder), "written": restored}
