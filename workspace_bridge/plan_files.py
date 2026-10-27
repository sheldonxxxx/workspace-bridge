"""Handoff files in Node state: read-only plan copies and uploaded images.

The Bridge keeps each handoff's text. The Node writes a copy under
``<state>/handoffs/<workspace>/<job>/`` so an agent that lost its context can
re-read its plan, and a run gets read access to its own handoff's folder and
nothing else in Node state. Every write restores the published text, so a
changed copy never outlives the next run start.

Images ChatGPT uploads for a handoff are kept by hash under
``<state>/uploads/<workspace>/``, never in the workspace; a plan copies the
ones it names into its own ``attachments/`` folder.
"""
from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

from .media import validate_image
from .security import BridgeError, digest, file_text

DOCUMENTS = ("TASK.md", "CONTEXT.md", "ACCEPTANCE.md")
IDENT = re.compile(r"[A-Za-z0-9_-]{1,80}")
MAX_DOCUMENT_BYTES = 1024 * 1024
EXTENSIONS = {"PNG": "png", "JPEG": "jpg", "WEBP": "webp", "GIF": "gif", "BMP": "bmp", "TIFF": "tiff"}
UPLOAD = re.compile(r"([0-9a-f]{64})\.(?:png|jpg|webp|gif|bmp|tiff)")
MAX_UPLOAD_BYTES = 512 * 1024 * 1024  # Per workspace.


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


def _folders(*directories: Path):
    for directory in directories:
        if directory.is_symlink():
            directory.unlink()
        directory.mkdir(mode=0o700, exist_ok=True)
        os.chmod(directory, 0o700)


def _ident(value: object, what: str) -> str:
    if not isinstance(value, str) or not IDENT.fullmatch(value):
        raise BridgeError(f"Invalid {what}", "invalid_arguments")
    return value


class UploadStore:
    def __init__(self, state: Path):
        self.root = state / "uploads"

    def save(self, workspace_id: object, data: bytes, mime_type: str) -> dict:
        folder = self.root / _ident(workspace_id, "workspace")
        metadata = validate_image(data, "upload", mime_type, require_suffix=False)
        sha = digest(data)
        name = f"{sha}.{EXTENSIONS[metadata['source_format']]}"
        try:
            _folders(self.root, folder)
            if not (folder / name).is_file():
                used = sum(item.stat().st_size for item in folder.iterdir() if UPLOAD.fullmatch(item.name))
                if used + len(data) > MAX_UPLOAD_BYTES:
                    raise BridgeError("This workspace's uploaded images exceed 512 MiB", "storage_limit")
            _write(folder / name, data)
        except OSError:
            raise BridgeError("The Node could not save the image", "plan_unavailable") from None
        return {"sha256": sha, "file": name, "bytes": len(data), **metadata}

    def find(self, workspace_id: object, sha256: str) -> tuple[str, bytes]:
        """The stored file name and bytes of an uploaded image."""
        folder = self.root / _ident(workspace_id, "workspace")
        for extension in EXTENSIONS.values():
            target = folder / f"{sha256}.{extension}"
            if target.is_file() and not target.is_symlink():
                try:
                    data = target.read_bytes()
                except OSError:
                    break
                if digest(data) == sha256:
                    return target.name, data
                break
        raise BridgeError("No uploaded image has this sha256; upload it again", "stale_context")


class PlanStore:
    def __init__(self, state: Path):
        self.root = state / "handoffs"

    def write(self, workspace_id: object, job_id: object, documents: object, uploads: object = ()) -> dict:
        if not isinstance(workspace_id, str) or not IDENT.fullmatch(workspace_id) \
                or not isinstance(job_id, str) or not IDENT.fullmatch(job_id):
            raise BridgeError("Invalid handoff identity", "invalid_arguments")
        if not isinstance(uploads, (list, tuple)) or len(uploads) > 20 \
                or any(not isinstance(name, str) or not UPLOAD.fullmatch(name) for name in uploads):
            raise BridgeError("Invalid plan attachments", "invalid_arguments")
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
        images = {name: UploadStore(self.root.parent).find(workspace_id, UPLOAD.fullmatch(name)[1])[1]
                  for name in uploads}
        folder = self.root / workspace_id / job_id
        try:
            _folders(self.root, self.root / workspace_id, folder)
            restored = [name for name in DOCUMENTS if _write(folder / name, prepared[name])]
            if images:
                _folders(folder / "attachments")
                restored += [f"attachments/{name}" for name, data in images.items()
                             if _write(folder / "attachments" / name, data)]
        except OSError:
            raise BridgeError("The Node could not save the plan files", "plan_unavailable") from None
        return {"folder": str(folder), "written": restored}
