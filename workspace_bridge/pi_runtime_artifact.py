"""M4.2C1.1 Pi runtime artifacts: self-contained, platform-specific.

Keeps accepted C1 ``pi-host-adapter.tar.gz`` as the platform-independent
source artifact. Each runtime artifact is derived from that exact source
artifact: production source + materialized ``node_modules`` (via
``npm ci --omit=dev`` from the committed lockfile) packed deterministically.

Each archive/sidecar binds C1 source Pi SHA-256, Pi release identity,
package-lock SHA-256, platform, arch, Node major, runtime SHA/size, and a
content-addressed runtime-artifact ID. Construction is cross-platform
deterministic within the same platform: sorted walk, ``mtime=0``,
``uid/gid=0``, dirs ``0755``, files ``0755`` iff executable else ``0644``,
symlinks only relative and resolving within the staged root. Absolute,
out-of-root, device/FIFO/socket, traversal, and unexpected top-level files
fail closed. Validation re-hashes and safely inspects/extracts before
acceptance. Hashes are expected to differ across platforms.
"""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import re
import shutil
import stat as _stat
import tarfile
import tempfile
from pathlib import Path
from typing import Any, Callable

from .release import validate_release

RUNTIME_SCHEMA_VERSION = 1
PRODUCT = "workspace-bridge"

ALLOWED_PLATFORMS = frozenset({"linux", "darwin"})
ALLOWED_ARCHS = frozenset({"x64", "arm64"})

_BUILD_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_VERSION_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._-]{0,31}$")

_RUNTIME_KEYS = frozenset({
    "schema_version", "product", "product_version",
    "platform", "arch", "node_major",
    "source_pi_sha256", "pi_release", "package_lock_sha256",
    "runtime_sha256", "runtime_size", "runtime_artifact_id",
})

_TAR_MTIME = 0
_MAX_JSON_BYTES = 65536


class RuntimeArtifactError(ValueError):
    """Fail-closed Pi runtime-artifact failure with a stable safe code."""

    def __init__(self, message: str, *, code: str = "runtime_failed"):
        super().__init__(message)
        self.code = code


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def compute_runtime_artifact_id(*, product_version: str, platform: str,
                                arch: str, node_major: int,
                                source_pi_sha256: str, pi_release: dict,
                                package_lock_sha256: str,
                                runtime_sha256: str,
                                runtime_size: int) -> str:
    """Content-addressed runtime-artifact ID over canonical safe metadata."""
    canonical = {
        "arch": arch,
        "node_major": node_major,
        "package_lock_sha256": package_lock_sha256,
        "pi_release": pi_release,
        "platform": platform,
        "product": PRODUCT,
        "product_version": product_version,
        "runtime_sha256": runtime_sha256,
        "runtime_size": runtime_size,
        "schema_version": RUNTIME_SCHEMA_VERSION,
        "source_pi_sha256": source_pi_sha256,
    }
    return "sha256:" + hashlib.sha256(
        _canonical_bytes(canonical)).hexdigest()


def _hash_file(path: Path) -> tuple[str, int]:
    hasher = hashlib.sha256()
    size = 0
    with open(path, "rb") as stream:
        while True:
            chunk = stream.read(65536)
            if not chunk:
                break
            hasher.update(chunk)
            size += len(chunk)
    return "sha256:" + hasher.hexdigest(), size


def _validate_platform_arch(*, platform: str, arch: str,
                            node_major: int) -> None:
    if platform not in ALLOWED_PLATFORMS:
        raise RuntimeArtifactError("Runtime platform is invalid",
                                   code="runtime-platform-invalid")
    if arch not in ALLOWED_ARCHS:
        raise RuntimeArtifactError("Runtime arch is invalid",
                                   code="runtime-platform-invalid")
    if not isinstance(node_major, int) or isinstance(node_major, bool):
        raise RuntimeArtifactError("Node major is invalid",
                                   code="runtime-platform-invalid")
    if not 20 <= node_major <= 32:
        raise RuntimeArtifactError("Node major is out of range",
                                   code="runtime-platform-invalid")


def _validate_product_version(value: object) -> str:
    if not isinstance(value, str) or not _VERSION_RE.fullmatch(value):
        raise RuntimeArtifactError("Product version is invalid",
                                   code="runtime-manifest-invalid")
    return value


def _require_safe_rel(name: str) -> None:
    if not name or name.startswith("/") or "\\" in name:
        raise RuntimeArtifactError("Runtime member is unsafe",
                                   code="runtime-traversal")
    if name in (".", "..") or name.startswith("../") or name.startswith("./"):
        raise RuntimeArtifactError("Runtime member is unsafe",
                                   code="runtime-traversal")
    if "/../" in f"/{name}/" or "//" in name:
        raise RuntimeArtifactError("Runtime member is unsafe",
                                   code="runtime-traversal")
    parts = name.split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise RuntimeArtifactError("Runtime member is unsafe",
                                   code="runtime-traversal")


def _allowed_top_level(name: str) -> bool:
    # Production top-level: *.mjs, package.json, package-lock.json, plus
    # materialized node_modules. Everything else (test/, launchd/,
    # README-only, state) is unexpected.
    if name == "node_modules":
        return True
    if name in ("package.json", "package-lock.json"):
        return True
    if name.endswith(".mjs") and "/" not in name:
        # No slashes: top-level .mjs only.
        return True
    return False


def _collect_runtime_entries(staged_root: Path) -> list[dict]:
    """Walk staged root deterministically, validating each entry.

    Returns sorted list of ``{arcname, full, kind}`` where kind is
    ``dir``/``file``/``symlink``. Rejects devices/FIFOs/sockets,
    absolute/out-of-root symlinks, traversal, and unexpected top-level.
    """
    try:
        if os.path.islink(str(staged_root)):
            raise RuntimeArtifactError("Staged root is unsafe",
                                       code="runtime-traversal")
        if not staged_root.is_dir():
            raise RuntimeArtifactError("Staged root is not a directory",
                                       code="runtime-unavailable")
    except OSError as exc:
        if isinstance(exc, RuntimeArtifactError):
            raise
        raise RuntimeArtifactError("Staged root is unavailable",
                                   code="runtime-unavailable") from exc
    try:
        top = sorted(p.name for p in staged_root.iterdir())
    except OSError as exc:
        raise RuntimeArtifactError("Staged root is unavailable",
                                   code="runtime-unavailable") from exc
    if not top:
        raise RuntimeArtifactError("Staged root is empty",
                                   code="runtime-integrity-failed")
    for name in top:
        if not _allowed_top_level(name):
            raise RuntimeArtifactError(
                f"Unexpected top-level file {name!r}",
                code="runtime-integrity-failed")
        child = staged_root / name
        try:
            if child.is_symlink():
                # Top-level symlinks are unexpected (node_modules is a dir).
                raise RuntimeArtifactError("Top-level symlink is unsafe",
                                           code="runtime-traversal")
        except OSError as exc:
            if isinstance(exc, RuntimeArtifactError):
                raise
            raise RuntimeArtifactError("Staged root is unavailable",
                                       code="runtime-unavailable") from exc
    # Require production essentials: at least one .mjs + package.json +
    # node_modules for self-contained runtime. Fixtures always create these.
    has_mjs = any(n.endswith(".mjs") for n in top)
    if not has_mjs or "package.json" not in top or "node_modules" not in top:
        raise RuntimeArtifactError("Staged runtime is incomplete",
                                   code="runtime-integrity-failed")
    try:
        if (staged_root / "node_modules").is_symlink() or not (
                staged_root / "node_modules").is_dir():
            raise RuntimeArtifactError("node_modules must be a directory",
                                       code="runtime-integrity-failed")
    except OSError as exc:
        if isinstance(exc, RuntimeArtifactError):
            raise
        raise RuntimeArtifactError("Staged root is unavailable",
                                   code="runtime-unavailable") from exc

    entries: list[dict] = []
    for dirpath, dirnames, filenames, symlinks in _walk_sorted(staged_root):
        rel_dir = dirpath.relative_to(staged_root).as_posix()
        if rel_dir == ".":
            rel_dir = ""
        # Directories themselves (except root) are entries.
        if rel_dir:
            _require_safe_rel(rel_dir)
            entries.append({"arcname": rel_dir, "full": dirpath,
                            "kind": "dir"})
        for name in dirnames:
            # dirnames already sorted by _walk_sorted; validate traversal.
            arc = f"{rel_dir}/{name}" if rel_dir else name
            _require_safe_rel(arc)
        for name in filenames:
            arc = f"{rel_dir}/{name}" if rel_dir else name
            _require_safe_rel(arc)
            full = dirpath / name
            try:
                st = full.lstat()
            except OSError as exc:
                raise RuntimeArtifactError("Staged file is unavailable",
                                           code="runtime-unavailable") from exc
            if not _stat.S_ISREG(st.st_mode):
                raise RuntimeArtifactError("Unexpected non-regular file",
                                           code="runtime-integrity-failed")
            entries.append({"arcname": arc, "full": full, "kind": "file"})
        for name in symlinks:
            arc = f"{rel_dir}/{name}" if rel_dir else name
            _require_safe_rel(arc)
            full = dirpath / name
            try:
                target = os.readlink(str(full))
            except OSError as exc:
                raise RuntimeArtifactError("Symlink is unavailable",
                                           code="runtime-traversal") from exc
            if not target or target.startswith("/") or "\\" in target:
                raise RuntimeArtifactError("Absolute symlink is unsafe",
                                           code="runtime-traversal")
            # Resolve within root: no absolute, no escaping .. .
            base = Path(arc).parent
            resolved = (base / Path(target)).as_posix()
            # Normalize ./ and redundant parts without touching FS.
            parts: list[str] = []
            for part in resolved.split("/"):
                if part in ("", "."):
                    continue
                if part == "..":
                    if not parts:
                        raise RuntimeArtifactError(
                            "Symlink escapes runtime root",
                            code="runtime-traversal")
                    parts.pop()
                else:
                    parts.append(part)
            if not parts:
                raise RuntimeArtifactError("Symlink is invalid",
                                           code="runtime-traversal")
            entries.append({"arcname": arc, "full": full, "kind": "symlink",
                            "linkname": target})
    entries.sort(key=lambda e: e["arcname"])
    return entries


def _walk_sorted(root: Path):
    """Yield (dirpath, dirnames, filenames, symlinks) in sorted order.

    Symlinks to dirs are not descended. Devices/FIFOs/sockets fail closed.
    """
    stack: list[Path] = [root]
    while stack:
        dirpath = stack.pop(0)
        try:
            children = sorted(dirpath.iterdir(), key=lambda p: p.name)
        except OSError as exc:
            raise RuntimeArtifactError("Staged root is unavailable",
                                       code="runtime-unavailable") from exc
        dirnames: list[str] = []
        filenames: list[str] = []
        symlinks: list[str] = []
        subdirs: list[Path] = []
        for child in children:
            try:
                st = child.lstat()
            except OSError as exc:
                raise RuntimeArtifactError("Staged file is unavailable",
                                           code="runtime-unavailable") from exc
            if _stat.S_ISLNK(st.st_mode):
                symlinks.append(child.name)
            elif _stat.S_ISDIR(st.st_mode):
                dirnames.append(child.name)
                subdirs.append(child)
            elif _stat.S_ISREG(st.st_mode):
                filenames.append(child.name)
            else:
                raise RuntimeArtifactError(
                    "Device/FIFO/socket is not allowed",
                    code="runtime-traversal")
        dirnames.sort()
        filenames.sort()
        symlinks.sort()
        # Descend in sorted order (BFS sorted overall via entries sort later).
        for sub in sorted(subdirs, key=lambda p: p.name):
            stack.append(sub)
        yield dirpath, dirnames, filenames, symlinks


def _create_runtime_archive(*, staged_root: Path, dest: Path) -> None:
    entries = _collect_runtime_entries(staged_root)
    tar_buffer = io.BytesIO()
    try:
        with tarfile.open(fileobj=tar_buffer, mode="w",
                           format=tarfile.PAX_FORMAT,
                           pax_headers={}) as tar:
            for entry in entries:
                arcname: str = entry["arcname"]
                kind: str = entry["kind"]
                full: Path = entry["full"]
                info = tarfile.TarInfo(arcname)
                info.mtime = _TAR_MTIME
                info.uid = 0
                info.gid = 0
                info.uname = ""
                info.gname = ""
                info.pax_headers = {}
                if kind == "dir":
                    info.type = tarfile.DIRTYPE
                    info.mode = 0o755
                    info.size = 0
                    tar.addfile(info)
                elif kind == "symlink":
                    info.type = tarfile.SYMTYPE
                    info.linkname = entry["linkname"]
                    info.mode = 0o777
                    info.size = 0
                    tar.addfile(info)
                else:
                    try:
                        st = full.lstat()
                        data = full.read_bytes()
                    except OSError as exc:
                        raise RuntimeArtifactError(
                            "Staged file is unavailable",
                            code="runtime-unavailable") from exc
                    info.type = tarfile.REGTYPE
                    # Preserve only executable-vs-nonexec.
                    info.mode = 0o755 if (st.st_mode & 0o111) else 0o644
                    info.size = len(data)
                    tar.addfile(info, io.BytesIO(data))
    except (OSError, tarfile.TarError) as exc:
        if isinstance(exc, RuntimeArtifactError):
            raise
        raise RuntimeArtifactError("Runtime archive could not be created",
                                   code="runtime-build-failed") from exc
    tar_bytes = tar_buffer.getvalue()
    try:
        with open(dest, "wb") as raw:
            with gzip.GzipFile(filename="", mode="wb", compresslevel=9,
                               mtime=_TAR_MTIME, fileobj=raw) as gz:
                gz.write(tar_bytes)
    except OSError as exc:
        raise RuntimeArtifactError("Runtime archive could not be written",
                                   code="runtime-build-failed") from exc


def _safe_list_tar(*, src: Path) -> list[tarfile.TarInfo]:
    try:
        with tarfile.open(src, "r:gz") as tar:
            members = tar.getmembers()
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise RuntimeArtifactError("Runtime archive is invalid",
                                   code="runtime-integrity-failed") from exc
    for member in members:
        name = member.name
        if not name or name.startswith("/") or "\\" in name:
            raise RuntimeArtifactError("Runtime member is unsafe",
                                       code="runtime-traversal")
        if name in (".", "..") or name.startswith("../") or name.startswith("./"):
            raise RuntimeArtifactError("Runtime member is unsafe",
                                       code="runtime-traversal")
        if "/../" in f"/{name}/" or "//" in name:
            raise RuntimeArtifactError("Runtime member is unsafe",
                                       code="runtime-traversal")
        if member.issym() or member.islnk():
            target = member.linkname
            if not target or target.startswith("/") or "\\" in target:
                raise RuntimeArtifactError("Absolute symlink is unsafe",
                                           code="runtime-traversal")
            base = Path(name).parent
            resolved = (base / Path(target)).as_posix()
            parts: list[str] = []
            for part in resolved.split("/"):
                if part in ("", "."):
                    continue
                if part == "..":
                    if not parts:
                        raise RuntimeArtifactError(
                            "Symlink escapes runtime root",
                            code="runtime-traversal")
                    parts.pop()
                else:
                    parts.append(part)
        elif member.isdev():
            raise RuntimeArtifactError("Device is not allowed",
                                       code="runtime-traversal")
        elif not (member.isfile() or member.isdir()):
            raise RuntimeArtifactError("Unexpected archive member",
                                       code="runtime-integrity-failed")
        top = name.split("/")[0]
        if not _allowed_top_level(top):
            raise RuntimeArtifactError(
                f"Unexpected top-level file {top!r}",
                code="runtime-integrity-failed")
    return members


def _safe_extract_tar(*, src: Path, dest: Path) -> None:
    members = _safe_list_tar(src=src)
    try:
        with tarfile.open(src, "r:gz") as tar:
            def _filter(m: tarfile.TarInfo, _p: str):
                if m.issym() or m.islnk() or m.isdev():
                    # Already validated; keep numeric owners.
                    if m.issym() or m.islnk():
                        # Re-check absolute/out-of-root (defense in depth).
                        target = m.linkname
                        if not target or target.startswith("/"):
                            raise RuntimeArtifactError(
                                "Absolute symlink is unsafe",
                                code="runtime-traversal")
                    elif m.isdev():
                        raise RuntimeArtifactError("Device is not allowed",
                                                   code="runtime-traversal")
                m.uid = 0
                m.gid = 0
                m.uname = ""
                m.gname = ""
                return m
            try:
                tar.extractall(dest, filter=_filter)  # type: ignore[call-arg]
            except TypeError:
                tar.extractall(dest)
    except (tarfile.TarError, OSError) as exc:
        if isinstance(exc, RuntimeArtifactError):
            raise
        raise RuntimeArtifactError("Runtime archive is invalid",
                                   code="runtime-integrity-failed") from exc
    for child in dest.iterdir():
        try:
            if child.is_symlink() and child.name != "node_modules":
                # Top-level symlinks already rejected; sweep nested.
                pass
        except OSError as exc:
            raise RuntimeArtifactError("Runtime extraction is unsafe",
                                       code="runtime-traversal") from exc


def _read_sized_json(path: Path, *, label: str) -> Any:
    try:
        if path.is_symlink():
            raise RuntimeArtifactError(f"{label} is unsafe",
                                       code="runtime-traversal")
        raw = path.read_bytes()
    except OSError as exc:
        raise RuntimeArtifactError(f"{label} is unavailable",
                                   code="runtime-unavailable") from exc
    if len(raw) > _MAX_JSON_BYTES:
        raise RuntimeArtifactError(f"{label} exceeds the size limit",
                                   code="runtime-too-large")
    try:
        return json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise RuntimeArtifactError(f"{label} is invalid",
                                   code="runtime-manifest-invalid") from exc


def _validate_metadata(value: object) -> dict:
    if not isinstance(value, dict):
        raise RuntimeArtifactError("Runtime metadata must be an object",
                                   code="runtime-manifest-invalid")
    if set(value) != set(_RUNTIME_KEYS):
        raise RuntimeArtifactError("Runtime metadata has unexpected fields",
                                   code="runtime-manifest-invalid")
    if value.get("schema_version") != RUNTIME_SCHEMA_VERSION:
        raise RuntimeArtifactError("Runtime schema is unsupported",
                                   code="runtime-manifest-invalid")
    if value.get("product") != PRODUCT:
        raise RuntimeArtifactError("Runtime product is invalid",
                                   code="runtime-manifest-invalid")
    product_version = _validate_product_version(value.get("product_version"))
    platform = value.get("platform")
    arch = value.get("arch")
    node_major = value.get("node_major")
    _validate_platform_arch(platform=platform, arch=arch,
                            node_major=node_major)
    for key in ("source_pi_sha256", "package_lock_sha256",
                "runtime_sha256"):
        val = value.get(key)
        if not isinstance(val, str) or not _BUILD_ID_RE.fullmatch(val):
            raise RuntimeArtifactError(f"Runtime {key} is invalid",
                                       code="runtime-manifest-invalid")
    size = value.get("runtime_size")
    if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
        raise RuntimeArtifactError("Runtime size is invalid",
                                   code="runtime-manifest-invalid")
    artifact_id = value.get("runtime_artifact_id")
    if not isinstance(artifact_id, str) or not _BUILD_ID_RE.fullmatch(
            artifact_id):
        raise RuntimeArtifactError("Runtime artifact ID is invalid",
                                   code="runtime-manifest-invalid")
    try:
        pi_release = validate_release(value.get("pi_release"))
    except Exception as exc:
        raise RuntimeArtifactError("Pi release is invalid",
                                   code="runtime-identity-mismatch") from exc
    if pi_release["component"] != "pi-host-adapter":
        raise RuntimeArtifactError("Pi component mismatch",
                                   code="runtime-identity-mismatch")
    if pi_release["product_version"] != product_version:
        raise RuntimeArtifactError("Product version mismatch",
                                   code="runtime-manifest-invalid")
    recomputed = compute_runtime_artifact_id(
        product_version=product_version, platform=platform, arch=arch,
        node_major=node_major,
        source_pi_sha256=value["source_pi_sha256"],
        pi_release=pi_release,
        package_lock_sha256=value["package_lock_sha256"],
        runtime_sha256=value["runtime_sha256"],
        runtime_size=size)
    if recomputed != artifact_id:
        raise RuntimeArtifactError("Runtime artifact ID mismatch",
                                   code="runtime-integrity-failed")
    _assert_safe_metadata(value)
    return {**value, "pi_release": pi_release}


def _assert_safe_metadata(meta: dict) -> None:
    text = json.dumps(meta, sort_keys=True, ensure_ascii=False)
    lowered = text.lower()
    for bad in ("/tmp/", "/volumes/", "/home/", "/state/", "/private/",
                "http://", "https://"):
        if bad in text:
            raise RuntimeArtifactError("Runtime metadata leaks paths",
                                       code="runtime-unsafe-metadata")
    for bad in ("token", "secret", "redacted", "authorization", "password",
                "api_key", "apikey"):
        if bad in lowered:
            raise RuntimeArtifactError("Runtime metadata leaks secrets",
                                       code="runtime-unsafe-metadata")
    for bad in ("timestamp", "created", "mtime", "hostname", "command",
                "argv", "shell", "environ", "git_commit"):
        if bad in lowered:
            raise RuntimeArtifactError("Runtime metadata has unsafe fields",
                                       code="runtime-unsafe-metadata")


def runtime_asset_names(*, product_version: str, platform: str,
                        arch: str) -> tuple[str, str]:
    """Fixed asset names including version + platform/arch."""
    _validate_product_version(product_version)
    _validate_platform_arch(platform=platform, arch=arch, node_major=24)
    base = f"workspace-bridge-pi-runtime-{product_version}-{platform}-{arch}"
    return f"{base}.tar.gz", f"{base}.json"


def build_runtime_artifact(*, staged_root: Path | str,
                           source_pi_sha256: str,
                           pi_release: dict,
                           package_lock_sha256: str,
                           product_version: str,
                           platform: str, arch: str, node_major: int,
                           output_dir: Path | str,
                           _run: Callable | None = None) -> dict:
    """Build a deterministic runtime archive + metadata sidecar.

    ``staged_root`` is an already-materialized private staging dir
    (production source + ``node_modules`` via ``npm ci --omit=dev``).
    Output names are fixed and include version/platform/arch. Existing
    complete outputs are never overwritten.
    """
    staged = Path(staged_root)
    out = Path(output_dir)
    _validate_platform_arch(platform=platform, arch=arch,
                            node_major=node_major)
    product_version = _validate_product_version(product_version)
    for key, val in (("source_pi_sha256", source_pi_sha256),
                     ("package_lock_sha256", package_lock_sha256)):
        if not isinstance(val, str) or not _BUILD_ID_RE.fullmatch(val):
            raise RuntimeArtifactError(f"Runtime {key} is invalid",
                                       code="runtime-manifest-invalid")
    try:
        pi_release = validate_release(pi_release)
    except Exception as exc:
        raise RuntimeArtifactError("Pi release is invalid",
                                   code="runtime-identity-mismatch") from exc
    if pi_release["component"] != "pi-host-adapter":
        raise RuntimeArtifactError("Pi component mismatch",
                                   code="runtime-identity-mismatch")
    if pi_release["product_version"] != product_version:
        raise RuntimeArtifactError("Product version mismatch",
                                   code="runtime-manifest-invalid")
    try:
        if os.path.islink(str(out)):
            raise RuntimeArtifactError("Output is unsafe",
                                       code="runtime-traversal")
        out.mkdir(mode=0o755, parents=True, exist_ok=True)
        if not out.is_dir() or out.is_symlink():
            raise RuntimeArtifactError("Output is unsafe",
                                       code="runtime-traversal")
    except OSError as exc:
        if isinstance(exc, RuntimeArtifactError):
            raise
        raise RuntimeArtifactError("Output is unavailable",
                                   code="runtime-unavailable") from exc
    archive_name, meta_name = runtime_asset_names(
        product_version=product_version, platform=platform, arch=arch)
    archive_path = out / archive_name
    meta_path = out / meta_name
    for existing in (archive_path, meta_path):
        try:
            if existing.is_symlink() or existing.exists():
                raise RuntimeArtifactError("Runtime artifact already exists",
                                           code="runtime-exists")
        except OSError as exc:
            if isinstance(exc, RuntimeArtifactError):
                raise
            raise RuntimeArtifactError("Output is unavailable",
                                       code="runtime-unavailable") from exc
    # Deterministic archive from staged root (validates top-level + walk).
    tmp_archive = out / (archive_name + ".tmp")
    try:
        _create_runtime_archive(staged_root=staged, dest=tmp_archive)
        runtime_sha, runtime_size = _hash_file(tmp_archive)
        artifact_id = compute_runtime_artifact_id(
            product_version=product_version, platform=platform, arch=arch,
            node_major=node_major, source_pi_sha256=source_pi_sha256,
            pi_release=pi_release,
            package_lock_sha256=package_lock_sha256,
            runtime_sha256=runtime_sha, runtime_size=runtime_size)
        meta = {"schema_version": RUNTIME_SCHEMA_VERSION, "product": PRODUCT,
                "product_version": product_version, "platform": platform,
                "arch": arch, "node_major": node_major,
                "source_pi_sha256": source_pi_sha256,
                "pi_release": pi_release,
                "package_lock_sha256": package_lock_sha256,
                "runtime_sha256": runtime_sha, "runtime_size": runtime_size,
                "runtime_artifact_id": artifact_id}
        _assert_safe_metadata(meta)
        meta_bytes = (json.dumps(meta, sort_keys=True, indent=2) + "\n"
                      ).encode("utf-8")
        # Publish atomically: archive then sidecar, no overwrite.
        try:
            os.rename(tmp_archive, archive_path)
        except OSError as exc:
            raise RuntimeArtifactError("Runtime publish failed",
                                       code="runtime-write-failed") from exc
        try:
            os.chmod(archive_path, 0o644)
        except OSError:
            pass
        try:
            fd = os.open(str(meta_path),
                         os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o644)
        except FileExistsError as exc:
            raise RuntimeArtifactError("Runtime artifact already exists",
                                       code="runtime-exists") from exc
        except OSError as exc:
            raise RuntimeArtifactError("Runtime publish failed",
                                       code="runtime-write-failed") from exc
        try:
            view = memoryview(meta_bytes)
            while view:
                written = os.write(fd, view)
                view = view[written:]
            os.fsync(fd)
        except OSError as exc:
            try:
                os.close(fd)
            except OSError:
                pass
            raise RuntimeArtifactError("Runtime publish failed",
                                       code="runtime-write-failed") from exc
        os.close(fd)
    finally:
        try:
            if tmp_archive.exists() and not tmp_archive.is_symlink():
                tmp_archive.unlink()
        except OSError:
            pass
    # Fail-closed validation of what was just published.
    return validate_runtime_artifact(archive_path=archive_path,
                                     metadata_path=meta_path, _run=_run)


def validate_runtime_artifact(*, archive_path: Path | str,
                              metadata_path: Path | str,
                              source_pi_path: Path | str | None = None,
                              _run: Callable | None = None) -> dict:
    """Pure runtime-artifact validation (fail closed, no mutation).

    Re-hashes the archive, checks SHA/size binding, safely inspects and
    extracts before acceptance, verifies the Pi release of the extracted
    production source matches metadata, and recomputes the artifact ID.
    When ``source_pi_path`` is given, its hash must equal metadata
    ``source_pi_sha256`` (C1 source binding).
    """
    archive = Path(archive_path)
    meta_file = Path(metadata_path)
    for path in (archive, meta_file):
        try:
            if os.path.islink(str(path)):
                raise RuntimeArtifactError("Runtime path is unsafe",
                                           code="runtime-traversal")
            if not path.is_file():
                raise RuntimeArtifactError("Runtime file is missing",
                                           code="runtime-unavailable")
        except OSError as exc:
            if isinstance(exc, RuntimeArtifactError):
                raise
            raise RuntimeArtifactError("Runtime path is unavailable",
                                       code="runtime-unavailable") from exc
    if source_pi_path is not None:
        source = Path(source_pi_path)
        try:
            if os.path.islink(str(source)) or not source.is_file():
                raise RuntimeArtifactError("Source Pi artifact is unsafe",
                                           code="runtime-traversal")
        except OSError as exc:
            if isinstance(exc, RuntimeArtifactError):
                raise
            raise RuntimeArtifactError("Source Pi artifact is unavailable",
                                       code="runtime-unavailable") from exc
    meta_raw = _read_sized_json(meta_file, label="Runtime metadata")
    meta = _validate_metadata(meta_raw)
    actual_sha, actual_size = _hash_file(archive)
    if actual_sha != meta["runtime_sha256"] or actual_size != meta[
            "runtime_size"]:
        raise RuntimeArtifactError("Runtime hash mismatch",
                                   code="runtime-integrity-failed")
    if source_pi_path is not None:
        source_sha, _ = _hash_file(Path(source_pi_path))
        if source_sha != meta["source_pi_sha256"]:
            raise RuntimeArtifactError("Source Pi SHA binding mismatch",
                                       code="runtime-integrity-failed")
    # Safe inspect + extract before acceptance.
    _safe_list_tar(src=archive)
    with tempfile.TemporaryDirectory(prefix="wb-runtime-validate-") as tmp:
        tmp_path = Path(tmp)
        try:
            os.chmod(tmp_path, 0o700)
        except OSError:
            pass
        _safe_extract_tar(src=archive, dest=tmp_path)
        # Extracted production source must still identify as metadata Pi
        # release (node_modules is ignored by release.mjs inventory).
        from .release_bundle import _pi_release_for_dir
        try:
            actual_release = _pi_release_for_dir(tmp_path, _run=_run)
        except Exception as exc:
            raise RuntimeArtifactError("Extracted Pi identity is invalid",
                                       code="runtime-identity-mismatch"
                                       ) from exc
        if actual_release != meta["pi_release"]:
            raise RuntimeArtifactError("Pi identity mismatch",
                                       code="runtime-identity-mismatch")
        # Lock bytes must match metadata when present in the archive.
        lock_path = tmp_path / "package-lock.json"
        try:
            if lock_path.is_symlink() or not lock_path.is_file():
                raise RuntimeArtifactError("package-lock is missing",
                                           code="runtime-integrity-failed")
            lock_sha, _ = _hash_file(lock_path)
        except OSError as exc:
            raise RuntimeArtifactError("package-lock is unavailable",
                                       code="runtime-unavailable") from exc
        if lock_sha != meta["package_lock_sha256"]:
            raise RuntimeArtifactError("package-lock SHA mismatch",
                                       code="runtime-integrity-failed")
    return meta


def build_runtime_from_source_bundle(*, source_bundle_dir: Path | str,
                                     staged_root: Path | str,
                                     platform: str, arch: str,
                                     node_major: int,
                                     output_dir: Path | str,
                                     _run: Callable | None = None,
                                     _validate_run: Callable | None = None) -> dict:
    """Build a runtime artifact bound to an accepted C1 source bundle.

    Reads ``source_pi_sha256`` + ``pi_release`` + ``product_version`` from
    the validated source bundle, hashes the staged ``package-lock.json``,
    and delegates to :func:`build_runtime_artifact`. Validates the source
    bundle first (fail closed).
    """
    from .release_bundle import validate_release_bundle
    source_dir = Path(source_bundle_dir)
    staged = Path(staged_root)
    result = validate_release_bundle(source_dir, _run=_run)
    bundle = result["bundle"]
    by_logical = {e["logical"]: e for e in bundle["artifacts"]}
    pi_entry = by_logical.get("artifacts/pi-host-adapter.tar.gz")
    if pi_entry is None:
        raise RuntimeArtifactError("Source bundle is incomplete",
                                   code="runtime-manifest-invalid")
    source_sha: str = pi_entry["sha256"]
    pi_release: dict = pi_entry["releases"]["pi-host-adapter"]
    product_version: str = bundle["product_version"]
    try:
        lock_path = staged / "package-lock.json"
        if lock_path.is_symlink() or not lock_path.is_file():
            raise RuntimeArtifactError("package-lock is missing",
                                       code="runtime-integrity-failed")
        lock_sha, _ = _hash_file(lock_path)
    except OSError as exc:
        if isinstance(exc, RuntimeArtifactError):
            raise
        raise RuntimeArtifactError("package-lock is unavailable",
                                   code="runtime-unavailable") from exc
    effective_validate = _validate_run if _validate_run is not None else _run
    return build_runtime_artifact(
        staged_root=staged, source_pi_sha256=source_sha,
        pi_release=pi_release, package_lock_sha256=lock_sha,
        product_version=product_version, platform=platform, arch=arch,
        node_major=node_major, output_dir=output_dir,
        _run=effective_validate)


def short_id(value: str) -> str:
    """Safe shortened display prefix for a validated ``sha256:`` ID."""
    if (isinstance(value, str) and value.startswith("sha256:")
            and len(value) == 71):
        return "sha256:" + value[7:19]
    return "sha256:unknown"
