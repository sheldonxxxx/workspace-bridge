"""M4.2C1 deterministic release bundle (artifact construction/validation only).

A schema-v1 bundle binds exact deployable artifact bytes to a freshly
generated M4.2A target manifest from the CURRENT production source. No
service mutation, install, restart, Docker Compose, launchctl, backup
creation, commit, or push occurs here. Release verification consumes a
validated bundle rather than the live checkout.

Artifacts for this slice:

- ``python-wheel``: one wheel containing the Workspace Bridge Python core
  plus packaged Manager static assets. It covers target components bridge,
  node, codex-host-adapter, and manager. The Manager is built first so
  packaged ``workspace_bridge/static/dist/release.json`` is current.
- ``pi-host-adapter``: one deterministic ``tar.gz`` containing EXACTLY the
  Pi production inputs defined by ``runtime/pi-host-adapter/release.mjs``.
- The target manifest JSON itself is stored as immutable metadata, not as
  an install artifact. No Docker image is built in C1.

Bundle layout (fixed, bundle-relative)::

    <output>/target-manifest.json
    <output>/artifacts/python-wheel.whl
    <output>/artifacts/pi-host-adapter.tar.gz
    <output>/bundle.json

``bundle.json`` lists each artifact with logical name, sha256, size,
covered components, and validated release identities. ``bundle_id`` (and
alias ``receipt_id``) is a SHA-256 over canonical safe metadata excluding
itself. All subprocesses use fixed typed argv, never ``shell=True``.
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
import subprocess
import tarfile
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Callable

from .release_manifest import TARGET_COMPONENTS, validate_manifest
from .release import validate_release

BUNDLE_SCHEMA_VERSION = 1
PRODUCT = "workspace-bridge"

WHEEL_LOGICAL = "artifacts/python-wheel.whl"
PI_LOGICAL = "artifacts/pi-host-adapter.tar.gz"
TARGET_MANIFEST_LOGICAL = "target-manifest.json"
BUNDLE_JSON_LOGICAL = "bundle.json"

ALLOWED_FILES = frozenset({
    TARGET_MANIFEST_LOGICAL, BUNDLE_JSON_LOGICAL,
    WHEEL_LOGICAL, PI_LOGICAL,
})
ALLOWED_TOP = frozenset({TARGET_MANIFEST_LOGICAL, BUNDLE_JSON_LOGICAL, "artifacts"})
ALLOWED_ARTIFACT_NAMES = frozenset({"python-wheel.whl", "pi-host-adapter.tar.gz"})

WHEEL_KIND = "python-wheel"
PI_KIND = "pi-host-adapter"

WHEEL_COMPONENTS = ("bridge", "codex-host-adapter", "manager", "node")
PI_COMPONENTS = ("pi-host-adapter",)

_BUILD_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_BUNDLE_KEYS = frozenset({
    "schema_version", "product", "product_version",
    "manifest_id", "bundle_id", "receipt_id", "artifacts",
})
_ARTIFACT_KEYS = frozenset({
    "logical", "kind", "sha256", "size", "components", "releases",
})

REPO_ROOT = Path(__file__).resolve().parent.parent
WEB_ROOT = REPO_ROOT / "web"
PI_ROOT = REPO_ROOT / "runtime" / "pi-host-adapter"
PI_RELEASE_MJS = PI_ROOT / "release.mjs"
MANAGER_RELEASE_MJS = WEB_ROOT / "manager-release.mjs"

# Fixed Node helper scripts. These are package-owned strings, never
# caller-supplied command text. ``process.argv[1]`` is the trusted helper
# module path, ``process.argv[2]`` is the root to inspect.
_MANAGER_JS = (
    "const mod = await import(process.argv[1]);\n"
    "const root = process.argv[2];\n"
    "const release = mod.computeManagerRelease(root);\n"
    "process.stdout.write(JSON.stringify(release));\n"
)
_PI_JS = (
    "const mod = await import(process.argv[1]);\n"
    "const root = process.argv[2];\n"
    "const release = mod.piRelease(root);\n"
    "process.stdout.write(JSON.stringify(release));\n"
)
_PI_INPUTS_JS = (
    "const mod = await import(process.argv[1]);\n"
    "const root = process.argv[2];\n"
    "const names = mod.listProductionInputs(root);\n"
    "process.stdout.write(JSON.stringify(names));\n"
)

# Deterministic archive epochs. ZIP uses DOS epoch minimum; tar/gzip use 0.
_ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)
_TAR_MTIME = 0

_MAX_JSON_BYTES = 65536


class BundleError(ValueError):
    """Fail-closed release-bundle failure with a stable safe code."""

    def __init__(self, message: str, *, code: str = "bundle_failed"):
        super().__init__(message)
        self.code = code


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def compute_bundle_id(*, product_version: str, manifest_id: str,
                      artifacts: list[dict]) -> str:
    """Content-addressed bundle ID over canonical safe metadata.

    Covers schema/product/product_version/manifest_id plus deterministic
    artifact entries, excluding ``bundle_id``/``receipt_id`` themselves.
    """
    ordered = sorted(artifacts, key=lambda e: e["logical"])
    canonical = {"artifacts": ordered, "manifest_id": manifest_id,
                 "product": PRODUCT, "product_version": product_version,
                 "schema_version": BUNDLE_SCHEMA_VERSION}
    return "sha256:" + hashlib.sha256(_canonical_bytes(canonical)).hexdigest()


def _hash_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


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


def _node_exe() -> str:
    exe = shutil.which("node")
    if not exe:
        raise BundleError("Node is required for release-bundle helpers",
                          code="bundle-node-unavailable")
    return exe


def _npm_exe() -> str:
    exe = shutil.which("npm")
    if not exe:
        raise BundleError("npm is required for the Manager build",
                          code="bundle-npm-unavailable")
    return exe


def _uv_exe() -> str:
    exe = shutil.which("uv")
    if not exe:
        raise BundleError("uv is required for the Python wheel build",
                          code="bundle-uv-unavailable")
    return exe


def _run_node_helper(*, node_exe: str, module_path: Path, root: Path,
                     script: str, label: str,
                     _run: Callable | None = None) -> Any:
    """Run one fixed Node release helper and return its parsed JSON.

    ``_run`` is an injection hook for unit tests; production always uses
    :func:`subprocess.run` with a fixed script, fixed module path, and a
    bounded timeout. Only the helper's JSON value is returned. Never uses
    ``shell=True`` and never accepts caller-supplied command text.
    """
    if not isinstance(node_exe, str) or not node_exe:
        raise BundleError("Node helper requires a typed executable",
                          code="bundle-invalid")
    if script not in (_MANAGER_JS, _PI_JS, _PI_INPUTS_JS):
        raise BundleError("Unknown release helper", code="bundle-invalid")
    argv: list[str] = [node_exe, "--input-type=module", "-e", script,
                       str(module_path), str(root)]
    run = _run or subprocess.run
    try:
        result = run(argv, capture_output=True, text=True, timeout=30,
                     check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise BundleError(f"{label} release helper could not be executed",
                          code="bundle-helper-failed") from exc
    if getattr(result, "returncode", 1) != 0:
        raise BundleError(f"{label} release metadata is unavailable",
                          code="bundle-helper-failed")
    try:
        return json.loads(getattr(result, "stdout", ""))
    except (ValueError, UnicodeError, TypeError) as exc:
        raise BundleError(f"{label} release metadata is invalid",
                          code="bundle-helper-failed") from exc


def get_manager_release(*, node_exe: str | None = None,
                        _run: Callable | None = None) -> dict:
    """Return the live Manager release identity via its fixed Node helper."""
    exe = node_exe or _node_exe()
    value = _run_node_helper(node_exe=exe, module_path=MANAGER_RELEASE_MJS,
                             root=WEB_ROOT, script=_MANAGER_JS,
                             label="manager", _run=_run)
    if not isinstance(value, dict):
        raise BundleError("Manager release metadata is invalid",
                          code="bundle-helper-failed")
    try:
        return validate_release(value)
    except Exception as exc:
        raise BundleError("Manager release identity is invalid",
                          code="bundle-identity-invalid") from exc


def get_pi_release(*, node_exe: str | None = None,
                   _run: Callable | None = None) -> dict:
    """Return the live Pi release identity via its fixed Node helper."""
    exe = node_exe or _node_exe()
    value = _run_node_helper(node_exe=exe, module_path=PI_RELEASE_MJS,
                             root=PI_ROOT, script=_PI_JS,
                             label="pi", _run=_run)
    if not isinstance(value, dict):
        raise BundleError("Pi release metadata is invalid",
                          code="bundle-helper-failed")
    try:
        return validate_release(value)
    except Exception as exc:
        raise BundleError("Pi release identity is invalid",
                          code="bundle-identity-invalid") from exc


def list_pi_inputs(*, node_exe: str | None = None,
                   _run: Callable | None = None) -> list[str]:
    """Return the canonical Pi production input inventory via release.mjs."""
    exe = node_exe or _node_exe()
    value = _run_node_helper(node_exe=exe, module_path=PI_RELEASE_MJS,
                             root=PI_ROOT, script=_PI_INPUTS_JS,
                             label="pi-inputs", _run=_run)
    if not isinstance(value, list) or not value or not all(
            isinstance(n, str) for n in value):
        raise BundleError("Pi input inventory is invalid",
                          code="bundle-helper-failed")
    if sorted(value) != value:
        raise BundleError("Pi input inventory is not deterministic",
                          code="bundle-helper-failed")
    for name in value:
        _require_safe_pi_name(name)
    return list(value)


def _pi_release_for_dir(root: Path, *, node_exe: str | None = None,
                        _run: Callable | None = None) -> dict:
    """Compute Pi identity for an extracted dir with trusted helper code.

    The trusted repo ``release.mjs`` is executed; only the data root is
    untrusted. The extracted ``release.mjs`` is never imported.
    """
    exe = node_exe or _node_exe()
    value = _run_node_helper(node_exe=exe, module_path=PI_RELEASE_MJS,
                             root=root, script=_PI_JS,
                             label="pi", _run=_run)
    if not isinstance(value, dict):
        raise BundleError("Pi release metadata is invalid",
                          code="bundle-identity-invalid")
    try:
        return validate_release(value)
    except Exception as exc:
        raise BundleError("Pi release identity is invalid",
                          code="bundle-identity-invalid") from exc


def _pi_inputs_for_dir(root: Path, *, node_exe: str | None = None,
                       _run: Callable | None = None) -> list[str]:
    """List production inputs for an extracted dir with trusted helper code."""
    exe = node_exe or _node_exe()
    value = _run_node_helper(node_exe=exe, module_path=PI_RELEASE_MJS,
                             root=root, script=_PI_INPUTS_JS,
                             label="pi-inputs", _run=_run)
    if not isinstance(value, list) or not all(isinstance(n, str) for n in value):
        raise BundleError("Pi input inventory is invalid",
                          code="bundle-identity-invalid")
    return list(value)


def _require_safe_pi_name(name: str) -> None:
    if not isinstance(name, str) or not name or len(name) > 256:
        raise BundleError("Pi archive member is invalid",
                          code="bundle-integrity-failed")
    if name.startswith("/") or name.startswith("\\") or "\\" in name:
        raise BundleError("Pi archive member is unsafe",
                          code="bundle-traversal")
    if name in (".", "..") or "/../" in f"/{name}/" or name.startswith("../") \
            or name.endswith("/..") or "//" in name:
        raise BundleError("Pi archive member is unsafe",
                          code="bundle-traversal")
    parts = name.split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise BundleError("Pi archive member is unsafe",
                          code="bundle-traversal")
    if len(parts) != 1:
        # Production inputs are top-level files only.
        raise BundleError("Pi archive member is unexpected",
                          code="bundle-integrity-failed")


def run_manager_build(*, _run: Callable | None = None) -> None:
    """Run the known project Manager build from the current checkout.

    Fixed typed argv ``[npm, run, build]`` in ``web/``; never ``shell=True``
    and never caller-supplied command text.
    """
    npm = _npm_exe() if _run is None else (shutil.which("npm") or "npm")
    argv: list[str] = [npm, "run", "build"]
    run = _run or subprocess.run
    try:
        result = run(argv, cwd=str(WEB_ROOT), capture_output=True, text=True,
                     timeout=600, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise BundleError("Manager build could not be executed",
                          code="bundle-build-failed") from exc
    if getattr(result, "returncode", 1) != 0:
        raise BundleError("Manager build failed",
                          code="bundle-build-failed")


def run_wheel_build(*, out_dir: Path, _run: Callable | None = None) -> Path:
    """Build the Python wheel with fixed typed argv into ``out_dir``.

    Uses project-native ``uv build --wheel --out-dir <dir>``; never
    ``shell=True`` and never caller-supplied command text. Returns the
    single built ``.whl`` path. The gitignored ``build/`` cache is cleared
    first so stale Manager assets cannot leak into the wheel; ``build/``
    itself is ignored build noise, not production source.
    """
    # Clear stale setuptools cache for a deterministic wheel from current
    # checkout. Only the gitignored build/ tree is touched.
    if _run is None:
        try:
            build_cache = REPO_ROOT / "build"
            if build_cache.is_symlink():
                raise BundleError("Build cache is unsafe",
                                  code="bundle-traversal")
            if build_cache.exists():
                shutil.rmtree(build_cache, ignore_errors=True)
        except OSError as exc:
            raise BundleError("Build cache is unavailable",
                              code="bundle-build-failed") from exc
    uv = _uv_exe() if _run is None else (shutil.which("uv") or "uv")
    argv: list[str] = [uv, "build", "--wheel", "--out-dir", str(out_dir)]
    run = _run or subprocess.run
    try:
        result = run(argv, cwd=str(REPO_ROOT), capture_output=True, text=True,
                     timeout=600, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise BundleError("Python wheel build could not be executed",
                          code="bundle-build-failed") from exc
    if getattr(result, "returncode", 1) != 0:
        raise BundleError("Python wheel build failed",
                          code="bundle-build-failed")
    try:
        wheels = sorted(out_dir.glob("*.whl"))
    except OSError as exc:
        raise BundleError("Wheel output is unavailable",
                          code="bundle-build-failed") from exc
    if len(wheels) != 1:
        raise BundleError("Wheel build must produce exactly one wheel",
                          code="bundle-build-failed")
    candidate = wheels[0]
    try:
        if candidate.is_symlink() or not candidate.is_file():
            raise BundleError("Wheel build output is unsafe",
                              code="bundle-traversal")
    except OSError as exc:
        raise BundleError("Wheel output is unavailable",
                          code="bundle-build-failed") from exc
    return candidate


def _normalize_wheel(*, src: Path, dest: Path) -> None:
    """Repack a wheel deterministically (sorted, fixed timestamps/modes)."""
    try:
        with zipfile.ZipFile(src, "r") as zin:
            names = sorted(zin.namelist())
            if not names:
                raise BundleError("Wheel is empty",
                                  code="bundle-integrity-failed")
            payloads: list[tuple[str, bytes, bool]] = []
            for name in names:
                if name.startswith("/") or "\\" in name:
                    raise BundleError("Wheel member is unsafe",
                                      code="bundle-traversal")
                if name in ("", ".", "..") or "/../" in f"/{name}/":
                    raise BundleError("Wheel member is unsafe",
                                      code="bundle-traversal")
                info = zin.getinfo(name)
                # Reject symlinks stored in wheels.
                if (info.external_attr >> 16) & 0o170000 == 0o120000:
                    raise BundleError("Wheel symlink is unsafe",
                                      code="bundle-traversal")
                is_dir = info.is_dir() or name.endswith("/")
                data = b"" if is_dir else zin.read(name)
                payloads.append((name, data, is_dir))
    except zipfile.BadZipFile as exc:
        raise BundleError("Wheel is not a valid zip",
                          code="bundle-integrity-failed") from exc
    except OSError as exc:
        raise BundleError("Wheel could not be read",
                          code="bundle-build-failed") from exc
    try:
        with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_DEFLATED,
                             compresslevel=6) as zout:
            for name, data, is_dir in payloads:
                info = zipfile.ZipInfo(name, date_time=_ZIP_EPOCH)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.create_system = 3
                if is_dir:
                    info.external_attr = (0o755 << 16) | 0x10
                    zout.writestr(info, b"")
                else:
                    info.external_attr = (0o644 << 16)
                    zout.writestr(info, data)
    except OSError as exc:
        raise BundleError("Wheel could not be normalized",
                          code="bundle-build-failed") from exc


def _create_pi_archive(*, src_root: Path, names: list[str], dest: Path) -> None:
    """Create a deterministic ``tar.gz`` from exact production inputs."""
    if sorted(names) != names:
        raise BundleError("Pi inputs must be deterministically ordered",
                          code="bundle-invalid")
    seen: set[str] = set()
    payloads: list[tuple[str, bytes]] = []
    for name in names:
        _require_safe_pi_name(name)
        if name in seen:
            raise BundleError("Pi inputs contain duplicates",
                              code="bundle-invalid")
        seen.add(name)
        path = src_root / name
        try:
            if path.is_symlink() or not path.is_file():
                raise BundleError(f"Pi input {name} is missing or unsafe",
                                  code="bundle-integrity-failed")
            data = path.read_bytes()
        except OSError as exc:
            raise BundleError(f"Pi input {name} is unavailable",
                              code="bundle-integrity-failed") from exc
        if len(data) > 10 * 1024 * 1024:
            raise BundleError("Pi input exceeds size limit",
                              code="bundle-integrity-failed")
        payloads.append((name, data))
    # Deterministic tar (sorted, fixed mtime/uid/gid/mode) then gzip mtime=0.
    tar_buffer = io.BytesIO()
    try:
        with tarfile.open(fileobj=tar_buffer, mode="w", format=tarfile.PAX_FORMAT,
                           pax_headers={}) as tar:
            for name, data in sorted(payloads, key=lambda r: r[0]):
                info = tarfile.TarInfo(name)
                info.size = len(data)
                info.mtime = _TAR_MTIME
                info.uid = 0
                info.gid = 0
                info.uname = ""
                info.gname = ""
                info.mode = 0o644
                info.type = tarfile.REGTYPE
                info.pax_headers = {}
                tar.addfile(info, io.BytesIO(data))
    except (OSError, tarfile.TarError) as exc:
        raise BundleError("Pi archive could not be created",
                          code="bundle-build-failed") from exc
    tar_bytes = tar_buffer.getvalue()
    try:
        with open(dest, "wb") as raw:
            with gzip.GzipFile(filename="", mode="wb", compresslevel=9,
                               mtime=_TAR_MTIME, fileobj=raw) as gz:
                gz.write(tar_bytes)
    except OSError as exc:
        raise BundleError("Pi archive could not be written",
                          code="bundle-build-failed") from exc


def _safe_extract_zip(*, src: Path, dest: Path) -> None:
    """Extract a wheel safely; reject traversal/symlinks/absolute paths."""
    try:
        with zipfile.ZipFile(src, "r") as zin:
            for info in zin.infolist():
                name = info.filename
                if not name or name.startswith("/") or "\\" in name:
                    raise BundleError("Wheel member is unsafe",
                                      code="bundle-traversal")
                if name in ("", ".") or "/../" in f"/{name}/" \
                        or name.startswith("../"):
                    raise BundleError("Wheel member is unsafe",
                                      code="bundle-traversal")
                if (info.external_attr >> 16) & 0o170000 == 0o120000:
                    raise BundleError("Wheel symlink is unsafe",
                                      code="bundle-traversal")
                target = dest / name
                # Resolve without following symlinks in dest (dest is fresh).
                try:
                    rel = target.relative_to(dest)
                except ValueError as exc:
                    raise BundleError("Wheel member escapes",
                                      code="bundle-traversal") from exc
                if str(rel).startswith(".."):
                    raise BundleError("Wheel member escapes",
                                      code="bundle-traversal")
            zin.extractall(dest)
    except zipfile.BadZipFile as exc:
        raise BundleError("Wheel is not a valid zip",
                          code="bundle-integrity-failed") from exc
    # Post-extract symlink sweep (TOCTOU guard for odd zip implementations).
    for child in dest.rglob("*"):
        try:
            if child.is_symlink():
                raise BundleError("Wheel symlink is unsafe",
                                  code="bundle-traversal")
        except OSError as exc:
            raise BundleError("Wheel extraction is unsafe",
                              code="bundle-traversal") from exc


def _safe_extract_tar(*, src: Path, dest: Path) -> list[str]:
    """Extract a Pi ``tar.gz`` safely; return sorted member names."""
    try:
        with tarfile.open(src, "r:gz") as tar:
            members = tar.getmembers()
            names: list[str] = []
            for member in members:
                name = member.name
                if not name or name.startswith("/") or "\\" in name:
                    raise BundleError("Pi archive member is unsafe",
                                      code="bundle-traversal")
                if name in ("", ".", "..") or name.startswith("../") \
                        or "/../" in f"/{name}/" or name.startswith("./"):
                    raise BundleError("Pi archive member is unsafe",
                                      code="bundle-traversal")
                if member.issym() or member.islnk():
                    raise BundleError("Pi archive symlink is unsafe",
                                      code="bundle-traversal")
                if member.isdir():
                    raise BundleError("Pi archive has unexpected directory",
                                      code="bundle-integrity-failed")
                if not member.isfile():
                    raise BundleError("Pi archive member is unexpected",
                                      code="bundle-integrity-failed")
                _require_safe_pi_name(name)
                target = dest / name
                try:
                    rel = target.relative_to(dest)
                except ValueError as exc:
                    raise BundleError("Pi archive member escapes",
                                      code="bundle-traversal") from exc
                if str(rel).startswith("..") or "/" in str(rel):
                    # Top-level files only; no subdirectories.
                    raise BundleError("Pi archive member is unexpected",
                                      code="bundle-integrity-failed")
                names.append(name)
            # Extract with numeric owners only; filter prevents device nodes.
            def _filter(m: tarfile.TarInfo, _p: str) -> tarfile.TarInfo | None:
                if m.issym() or m.islnk() or m.isdev():
                    raise BundleError("Pi archive member is unsafe",
                                      code="bundle-traversal")
                m.uid = 0
                m.gid = 0
                m.uname = ""
                m.gname = ""
                return m
            try:
                tar.extractall(dest, filter=_filter)  # type: ignore[call-arg]
            except TypeError:
                # Python <3.12 without filter: members already validated.
                for member in members:
                    tar.extractfile(member)
                tar.extractall(dest)
    except (tarfile.TarError, OSError, EOFError) as exc:
        if isinstance(exc, BundleError):
            raise
        raise BundleError("Pi archive is invalid",
                          code="bundle-integrity-failed") from exc
    # Symlink sweep.
    for child in dest.iterdir():
        try:
            if child.is_symlink():
                raise BundleError("Pi archive symlink is unsafe",
                                  code="bundle-traversal")
        except OSError as exc:
            raise BundleError("Pi archive is unsafe",
                              code="bundle-traversal") from exc
        if child.is_dir():
            raise BundleError("Pi archive has unexpected directory",
                              code="bundle-integrity-failed")
    try:
        observed = sorted(p.name for p in dest.iterdir() if p.is_file())
    except OSError as exc:
        raise BundleError("Pi extraction is unavailable",
                          code="bundle-integrity-failed") from exc
    if sorted(names) != observed:
        raise BundleError("Pi archive members mismatch",
                          code="bundle-integrity-failed")
    return observed


def _core_build_id_for_dir(package_dir: Path) -> str:
    """Deterministic Python-core build ID for an extracted package dir.

    Reuses :func:`release._iter_core_inputs` so enumeration cannot drift
    from the package's own release logic. Only file bytes + relative names
    are hashed; no code is imported from the untrusted path.
    """
    from .release import _iter_core_inputs
    hasher = hashlib.sha256()
    hasher.update(b"workspace-bridge-python-core-v1\x00")
    try:
        rows = _iter_core_inputs(package_dir)
    except OSError as exc:
        raise BundleError("Wheel package inputs are unavailable",
                          code="bundle-integrity-failed") from exc
    if not rows:
        raise BundleError("Wheel package inputs are missing",
                          code="bundle-integrity-failed")
    for rel, path in rows:
        name_bytes = rel.encode("utf-8")
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise BundleError("Wheel package input is unavailable",
                              code="bundle-integrity-failed") from exc
        hasher.update(len(name_bytes).to_bytes(8, "big"))
        hasher.update(name_bytes)
        hasher.update(b"\x00")
        hasher.update(len(data).to_bytes(8, "big"))
        hasher.update(data)
        hasher.update(b"\x00")
    return "sha256:" + hasher.hexdigest()


def _read_manager_from_dir(package_dir: Path) -> dict:
    """Read/validate embedded Manager identity from an extracted wheel."""
    path = package_dir / "static" / "dist" / "release.json"
    try:
        if path.is_symlink():
            raise BundleError("Wheel Manager metadata is unsafe",
                              code="bundle-traversal")
        raw = path.read_bytes()
    except OSError as exc:
        raise BundleError("Wheel Manager metadata is missing",
                          code="bundle-integrity-failed") from exc
    if len(raw) > 8192:
        raise BundleError("Wheel Manager metadata is invalid",
                          code="bundle-integrity-failed")
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise BundleError("Wheel Manager metadata is invalid",
                          code="bundle-integrity-failed") from exc
    try:
        sanitized = validate_release(value)
    except Exception as exc:
        raise BundleError("Wheel Manager identity is invalid",
                          code="bundle-identity-mismatch") from exc
    if sanitized["component"] != "manager":
        raise BundleError("Wheel Manager component mismatch",
                          code="bundle-identity-mismatch")
    return sanitized


def _validate_wheel_against_manifest(*, wheel_path: Path, manifest: dict,
                                     _run: Callable | None = None) -> dict[str, dict]:
    """Unpack a wheel and require exact bridge/node/codex/manager match.

    Returns the validated ``{component: release}`` mapping for the wheel's
    four covered components. ``_run`` is unused for wheels (pure Python)
    but accepted for a uniform injectable signature.
    """
    _ = _run
    components = manifest.get("components", {})
    for name in WHEEL_COMPONENTS:
        if name not in components:
            raise BundleError("Target manifest is incomplete",
                              code="bundle-manifest-invalid")
    with tempfile.TemporaryDirectory(prefix="wb-wheel-validate-") as tmp:
        tmp_path = Path(tmp)
        try:
            os.chmod(tmp_path, 0o700)
        except OSError:
            pass
        _safe_extract_zip(src=wheel_path, dest=tmp_path)
        # Wheels contain top-level dist-info plus the package dir. Locate it.
        package_dir = tmp_path / "workspace_bridge"
        try:
            if package_dir.is_symlink() or not package_dir.is_dir():
                raise BundleError("Wheel package is missing",
                                  code="bundle-integrity-failed")
        except OSError as exc:
            raise BundleError("Wheel package is unavailable",
                              code="bundle-integrity-failed") from exc
        core_id = _core_build_id_for_dir(package_dir)
        manager = _read_manager_from_dir(package_dir)
        # Bridge/Node/Codex share the Python core; Manager is independent.
        for name in ("bridge", "node", "codex-host-adapter"):
            expected = components[name]
            if expected.get("build_id") != core_id:
                raise BundleError(
                    f"Wheel Python-core identity mismatch for {name}",
                    code="bundle-identity-mismatch")
        if manager != components["manager"]:
            raise BundleError("Wheel Manager identity mismatch",
                              code="bundle-identity-mismatch")
        return {"bridge": components["bridge"], "node": components["node"],
                "codex-host-adapter": components["codex-host-adapter"],
                "manager": components["manager"]}


def _validate_pi_against_manifest(*, archive_path: Path, manifest: dict,
                                  _run: Callable | None = None) -> dict[str, dict]:
    """Extract a Pi archive and require exact Pi identity match."""
    components = manifest.get("components", {})
    if "pi-host-adapter" not in components:
        raise BundleError("Target manifest is incomplete",
                          code="bundle-manifest-invalid")
    expected = components["pi-host-adapter"]
    with tempfile.TemporaryDirectory(prefix="wb-pi-validate-") as tmp:
        tmp_path = Path(tmp)
        try:
            os.chmod(tmp_path, 0o700)
        except OSError:
            pass
        observed = _safe_extract_tar(src=archive_path, dest=tmp_path)
        # Inventory must exactly equal the trusted helper's view of the
        # extracted dir: no missing/extra files beyond canonical inputs.
        trusted_inputs = _pi_inputs_for_dir(tmp_path, _run=_run)
        if sorted(observed) != sorted(trusted_inputs):
            raise BundleError("Pi archive inventory mismatch",
                              code="bundle-integrity-failed")
        actual = _pi_release_for_dir(tmp_path, _run=_run)
        if actual != expected:
            raise BundleError("Pi identity mismatch",
                              code="bundle-identity-mismatch")
        return {"pi-host-adapter": expected}


def _read_sized_json(path: Path, *, label: str) -> Any:
    try:
        if path.is_symlink():
            raise BundleError(f"{label} is unsafe", code="bundle-traversal")
        raw = path.read_bytes()
    except OSError as exc:
        raise BundleError(f"{label} is unavailable",
                          code="bundle-unavailable") from exc
    if len(raw) > _MAX_JSON_BYTES:
        raise BundleError(f"{label} exceeds the size limit",
                          code="bundle-too-large")
    try:
        return json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise BundleError(f"{label} is invalid",
                          code="bundle-manifest-invalid") from exc


def _validate_bundle_json(value: object, *, manifest: dict) -> dict:
    from .release_manifest import validate_manifest as _validate_manifest
    if not isinstance(value, dict):
        raise BundleError("Bundle metadata must be an object",
                          code="bundle-manifest-invalid")
    if set(value) != set(_BUNDLE_KEYS):
        raise BundleError("Bundle metadata has unexpected fields",
                          code="bundle-manifest-invalid")
    if value.get("schema_version") != BUNDLE_SCHEMA_VERSION:
        raise BundleError("Bundle schema is unsupported",
                          code="bundle-manifest-invalid")
    if value.get("product") != PRODUCT:
        raise BundleError("Bundle product is invalid",
                          code="bundle-manifest-invalid")
    product_version = value.get("product_version")
    manifest_id = value.get("manifest_id")
    bundle_id = value.get("bundle_id")
    receipt_id = value.get("receipt_id")
    if not isinstance(product_version, str) or not product_version:
        raise BundleError("Bundle product version is invalid",
                          code="bundle-manifest-invalid")
    if not isinstance(manifest_id, str) or not _BUILD_ID_RE.fullmatch(manifest_id):
        raise BundleError("Bundle manifest ID is invalid",
                          code="bundle-manifest-invalid")
    if not isinstance(bundle_id, str) or not _BUILD_ID_RE.fullmatch(bundle_id):
        raise BundleError("Bundle ID is invalid",
                          code="bundle-manifest-invalid")
    if receipt_id != bundle_id:
        raise BundleError("Bundle receipt ID mismatch",
                          code="bundle-manifest-invalid")
    # Must bind the stored target manifest exactly.
    try:
        sanitized_manifest = _validate_manifest(manifest)
    except Exception as exc:
        raise BundleError("Target manifest is invalid",
                          code="bundle-manifest-invalid") from exc
    if product_version != sanitized_manifest["product_version"]:
        raise BundleError("Bundle product version mismatch",
                          code="bundle-manifest-invalid")
    if manifest_id != sanitized_manifest["manifest_id"]:
        raise BundleError("Bundle manifest ID mismatch",
                          code="bundle-manifest-invalid")
    artifacts = value.get("artifacts")
    if not isinstance(artifacts, list) or len(artifacts) != 2:
        raise BundleError("Bundle artifacts are invalid",
                          code="bundle-manifest-invalid")
    by_logical: dict[str, dict] = {}
    for entry in artifacts:
        if not isinstance(entry, dict) or set(entry) != set(_ARTIFACT_KEYS):
            raise BundleError("Bundle artifact has unexpected fields",
                              code="bundle-manifest-invalid")
        logical = entry.get("logical")
        kind = entry.get("kind")
        sha256 = entry.get("sha256")
        size = entry.get("size")
        components = entry.get("components")
        releases = entry.get("releases")
        if logical not in (WHEEL_LOGICAL, PI_LOGICAL):
            raise BundleError("Bundle artifact is unexpected",
                              code="bundle-manifest-invalid")
        if logical in by_logical:
            raise BundleError("Bundle artifact is duplicated",
                              code="bundle-manifest-invalid")
        if kind not in (WHEEL_KIND, PI_KIND):
            raise BundleError("Bundle artifact kind is invalid",
                              code="bundle-manifest-invalid")
        if (logical == WHEEL_LOGICAL and kind != WHEEL_KIND) or (
                logical == PI_LOGICAL and kind != PI_KIND):
            raise BundleError("Bundle artifact kind mismatch",
                              code="bundle-manifest-invalid")
        if not isinstance(sha256, str) or not _BUILD_ID_RE.fullmatch(sha256):
            raise BundleError("Bundle artifact hash is invalid",
                              code="bundle-manifest-invalid")
        if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
            raise BundleError("Bundle artifact size is invalid",
                              code="bundle-manifest-invalid")
        if not isinstance(components, list) or sorted(components) != components:
            raise BundleError("Bundle artifact components must be sorted",
                              code="bundle-manifest-invalid")
        if logical == WHEEL_LOGICAL and components != sorted(WHEEL_COMPONENTS):
            raise BundleError("Wheel component coverage is invalid",
                              code="bundle-coverage-invalid")
        if logical == PI_LOGICAL and components != sorted(PI_COMPONENTS):
            raise BundleError("Pi component coverage is invalid",
                              code="bundle-coverage-invalid")
        if not isinstance(releases, dict):
            raise BundleError("Bundle artifact releases are invalid",
                              code="bundle-manifest-invalid")
        if set(releases) != set(components):
            raise BundleError("Bundle artifact releases mismatch coverage",
                              code="bundle-manifest-invalid")
        for comp, release in releases.items():
            try:
                sanitized = validate_release(release)
            except Exception as exc:
                raise BundleError(
                    f"Bundle artifact release for {comp} is invalid",
                    code="bundle-identity-mismatch") from exc
            if sanitized["component"] != comp:
                raise BundleError("Bundle artifact release component mismatch",
                                  code="bundle-identity-mismatch")
            expected = sanitized_manifest["components"].get(comp)
            if sanitized != expected:
                raise BundleError(
                    f"Bundle artifact release for {comp} mismatches target",
                    code="bundle-identity-mismatch")
        by_logical[logical] = entry
    if set(by_logical) != {WHEEL_LOGICAL, PI_LOGICAL}:
        raise BundleError("Bundle artifacts are incomplete",
                          code="bundle-coverage-invalid")
    # Union must cover all five target components exactly once.
    covered: list[str] = []
    for entry in artifacts:
        covered.extend(entry["components"])
    if sorted(covered) != sorted(TARGET_COMPONENTS):
        raise BundleError("Bundle component coverage is incomplete",
                          code="bundle-coverage-invalid")
    # Recompute content-addressed ID over canonical metadata.
    recomputed = compute_bundle_id(product_version=product_version,
                                   manifest_id=manifest_id,
                                   artifacts=[
                                       {"logical": e["logical"], "kind": e["kind"],
                                        "sha256": e["sha256"], "size": e["size"],
                                        "components": e["components"],
                                        "releases": e["releases"]}
                                       for e in artifacts])
    if recomputed != bundle_id:
        raise BundleError("Bundle ID mismatch",
                          code="bundle-integrity-failed")
    _assert_safe_metadata(value, sanitized_manifest)
    ordered = sorted(artifacts, key=lambda e: e["logical"])
    return {**value, "artifacts": ordered}


def _assert_safe_metadata(bundle: dict, manifest: dict) -> None:
    """Fail closed when public metadata leaks paths/secrets/timestamps."""
    text = json.dumps(bundle, sort_keys=True, ensure_ascii=False)
    lowered = text.lower()
    # Fixed logical names are bundle-relative and allowlisted; absolute
    # paths, hosts, and secret values are never allowed.
    for bad in ("/tmp/", "/volumes/", "/home/", "/state/", "/private/",
                "http://", "https://"):
        if bad in text:
            raise BundleError("Bundle metadata leaks paths or endpoints",
                              code="bundle-unsafe-metadata")
    # Absolute-path values (other than allowlisted logicals) are unsafe.
    # Logical entries are exactly the allowlist; every other string must
    # not look like an absolute path.
    def _walk(node: Any, *, is_logical: bool = False) -> None:
        if isinstance(node, dict):
            for key, val in node.items():
                if key == "logical":
                    if val not in (WHEEL_LOGICAL, PI_LOGICAL):
                        raise BundleError("Bundle logical path is invalid",
                                          code="bundle-unsafe-metadata")
                    continue
                _walk(val)
        elif isinstance(node, list):
            for item in node:
                _walk(item)
        elif isinstance(node, str):
            if node.startswith("/") and node not in (WHEEL_LOGICAL, PI_LOGICAL):
                raise BundleError("Bundle metadata contains an absolute path",
                                  code="bundle-unsafe-metadata")
    _walk(bundle)
    for bad in ("token", "secret", "redacted", "authorization", "password",
                "api_key", "apikey"):
        if bad in lowered:
            raise BundleError("Bundle metadata leaks secrets",
                              code="bundle-unsafe-metadata")
    for bad in ("timestamp", "created", "mtime", "hostname", "command",
                "argv", "shell", "environ", "git_commit", "git-state"):
        if bad in lowered:
            raise BundleError("Bundle metadata has unexpected fields",
                              code="bundle-unsafe-metadata")
    manifest_text = json.dumps(manifest, sort_keys=True, ensure_ascii=False)
    if "/tmp/" in manifest_text or "http://" in manifest_text:
        raise BundleError("Target manifest leaks paths",
                          code="bundle-unsafe-metadata")


def _ensure_output_dir(path: Path) -> None:
    try:
        if os.path.islink(str(path)):
            raise BundleError("Bundle output is unsafe",
                              code="bundle-unsafe-path")
    except OSError as exc:
        if isinstance(exc, BundleError):
            raise
        raise BundleError("Bundle output is unavailable",
                          code="bundle-unavailable") from exc
    try:
        exists = path.exists()
    except OSError as exc:
        raise BundleError("Bundle output is unavailable",
                          code="bundle-unavailable") from exc
    if exists:
        try:
            if not path.is_dir() or path.is_symlink():
                raise BundleError("Bundle output must be a directory",
                                  code="bundle-unsafe-path")
            entries = list(path.iterdir())
        except OSError as exc:
            raise BundleError("Bundle output is unavailable",
                              code="bundle-unavailable") from exc
        if entries:
            # Reject overwriting an existing complete bundle; any non-empty
            # directory is refused to keep operator selection explicit.
            if (path / BUNDLE_JSON_LOGICAL).exists():
                raise BundleError("Bundle already exists",
                                  code="bundle-exists")
            raise BundleError("Bundle output must be a new/empty directory",
                              code="bundle-exists")
    else:
        try:
            path.mkdir(mode=0o755, parents=True, exist_ok=False)
        except FileExistsError as exc:
            raise BundleError("Bundle already exists",
                              code="bundle-exists") from exc
        except OSError as exc:
            raise BundleError("Bundle output is unavailable",
                              code="bundle-unavailable") from exc
    try:
        if path.is_symlink() or not path.is_dir():
            raise BundleError("Bundle output is unsafe",
                              code="bundle-unsafe-path")
    except OSError as exc:
        raise BundleError("Bundle output is unavailable",
                          code="bundle-unavailable") from exc
    artifacts_dir = path / "artifacts"
    try:
        artifacts_dir.mkdir(mode=0o755, parents=False, exist_ok=True)
    except OSError as exc:
        raise BundleError("Bundle output is unavailable",
                          code="bundle-unavailable") from exc
    try:
        if artifacts_dir.is_symlink() or not artifacts_dir.is_dir():
            raise BundleError("Bundle output is unsafe",
                              code="bundle-unsafe-path")
    except OSError as exc:
        raise BundleError("Bundle output is unavailable",
                          code="bundle-unavailable") from exc


def _write_bundle_file(dest: Path, data: bytes, *, mode: int = 0o644) -> None:
    try:
        if dest.is_symlink() or dest.exists():
            raise BundleError("Bundle destination already exists",
                              code="bundle-exists")
    except OSError as exc:
        if isinstance(exc, BundleError):
            raise
        raise BundleError("Bundle destination is unavailable",
                          code="bundle-unavailable") from exc
    try:
        fd = os.open(str(dest), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    except FileExistsError as exc:
        raise BundleError("Bundle already exists",
                          code="bundle-exists") from exc
    except OSError as exc:
        raise BundleError("Bundle destination is unavailable",
                          code="bundle-unavailable") from exc
    try:
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            view = view[written:]
        os.fsync(fd)
    except OSError as exc:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            dest.unlink()
        except OSError:
            pass
        raise BundleError("Bundle write failed",
                          code="bundle-write-failed") from exc
    os.close(fd)
    try:
        os.chmod(dest, mode)
    except OSError:
        pass


def _clean_temp_dir(temp_dir: Path, parent: Path) -> None:
    try:
        if temp_dir.parent != parent or not temp_dir.name.startswith(".tmp-"):
            return
        if temp_dir.is_symlink():
            try:
                temp_dir.unlink()
            except OSError:
                pass
            return
        if temp_dir.exists():
            shutil.rmtree(temp_dir, ignore_errors=True)
    except Exception:
        pass


def build_target_manifest(*, manager: dict | None = None,
                          pi: dict | None = None,
                          _manager_fn: Callable | None = None,
                          _pi_fn: Callable | None = None,
                          _run: Callable | None = None) -> dict:
    """Assemble a validated target manifest from current source identities.

    When ``manager``/``pi`` are not supplied, the fixed Node helpers are
    used (injectable via ``_manager_fn``/``_pi_fn`` or ``_run`` for tests).
    Python-core identities are always computed live from current source.
    """
    from . import __version__ as product_version
    from .release_manifest import make_manifest
    from .release import bridge_release, codex_release, node_release
    bridge = bridge_release()
    node = node_release()
    codex = codex_release()
    if manager is not None:
        manager_release = manager
    elif _manager_fn is not None:
        manager_release = _manager_fn()
    else:
        manager_release = get_manager_release(_run=_run)
    if pi is not None:
        pi_release = pi
    elif _pi_fn is not None:
        pi_release = _pi_fn()
    else:
        pi_release = get_pi_release(_run=_run)
    return make_manifest(product_version=product_version, bridge=bridge,
                         manager=manager_release, node=node, codex=codex,
                         pi=pi_release)


def validate_release_bundle(path: Path | str, *, _run: Callable | None = None) -> dict:
    """Pure bundle validation used by later apply (fail closed, no mutation).

    Recomputes artifact hashes/sizes, ``bundle_id``, strict manifest
    validity, component coverage, and embedded Python/Pi identities.
    Corrupt/tampered bundles fail before any future mutation. Archive
    extraction rejects path traversal/symlinks. Only the fixed trusted
    validation process runs against extracted data; untrusted code paths
    are never imported.
    """
    bundle_dir = Path(path)
    try:
        if os.path.islink(str(bundle_dir)):
            raise BundleError("Bundle path is unsafe",
                              code="bundle-traversal")
        if not bundle_dir.is_dir():
            raise BundleError("Bundle path is not a directory",
                              code="bundle-unavailable")
    except OSError as exc:
        if isinstance(exc, BundleError):
            raise
        raise BundleError("Bundle path is unavailable",
                          code="bundle-unavailable") from exc
    # Strict allowlist: exactly target-manifest.json, bundle.json, artifacts/.
    try:
        top = sorted(p.name for p in bundle_dir.iterdir())
    except OSError as exc:
        raise BundleError("Bundle path is unavailable",
                          code="bundle-unavailable") from exc
    if top != sorted(ALLOWED_TOP):
        raise BundleError("Bundle contents are unexpected",
                          code="bundle-integrity-failed")
    for name in top:
        child = bundle_dir / name
        try:
            if child.is_symlink():
                raise BundleError("Bundle symlink is unsafe",
                                  code="bundle-traversal")
        except OSError as exc:
            if isinstance(exc, BundleError):
                raise
            raise BundleError("Bundle path is unavailable",
                              code="bundle-unavailable") from exc
        if name == "artifacts":
            if not child.is_dir():
                raise BundleError("Bundle artifacts must be a directory",
                                  code="bundle-integrity-failed")
            try:
                inner = sorted(p.name for p in child.iterdir())
            except OSError as exc:
                raise BundleError("Bundle artifacts are unavailable",
                                  code="bundle-unavailable") from exc
            if inner != sorted(ALLOWED_ARTIFACT_NAMES):
                raise BundleError("Bundle artifacts are unexpected",
                                  code="bundle-integrity-failed")
            for leaf in inner:
                leaf_path = child / leaf
                try:
                    if leaf_path.is_symlink():
                        raise BundleError("Bundle symlink is unsafe",
                                          code="bundle-traversal")
                    st = leaf_path.stat()
                except OSError as exc:
                    if isinstance(exc, BundleError):
                        raise
                    raise BundleError("Bundle artifact is unavailable",
                                      code="bundle-unavailable") from exc
                if not _stat.S_ISREG(st.st_mode):
                    raise BundleError("Bundle artifact must be a regular file",
                                      code="bundle-integrity-failed")
        else:
            try:
                st = child.stat()
            except OSError as exc:
                raise BundleError("Bundle file is unavailable",
                                  code="bundle-unavailable") from exc
            if not _stat.S_ISREG(st.st_mode):
                raise BundleError("Bundle file must be a regular file",
                                  code="bundle-integrity-failed")
    manifest_raw = _read_sized_json(bundle_dir / TARGET_MANIFEST_LOGICAL,
                                    label="Target manifest")
    bundle_raw = _read_sized_json(bundle_dir / BUNDLE_JSON_LOGICAL,
                                  label="Bundle metadata")
    bundle = _validate_bundle_json(bundle_raw, manifest=manifest_raw)
    manifest = validate_manifest(manifest_raw)
    # Recompute artifact hashes/sizes against bundle.json.
    by_logical = {e["logical"]: e for e in bundle["artifacts"]}
    wheel_entry = by_logical[WHEEL_LOGICAL]
    pi_entry = by_logical[PI_LOGICAL]
    wheel_path = bundle_dir / WHEEL_LOGICAL
    pi_path = bundle_dir / PI_LOGICAL
    actual_wheel_sha, actual_wheel_size = _hash_file(wheel_path)
    if actual_wheel_sha != wheel_entry["sha256"] or actual_wheel_size != wheel_entry["size"]:
        raise BundleError("Wheel hash mismatch",
                          code="bundle-integrity-failed")
    actual_pi_sha, actual_pi_size = _hash_file(pi_path)
    if actual_pi_sha != pi_entry["sha256"] or actual_pi_size != pi_entry["size"]:
        raise BundleError("Pi archive hash mismatch",
                          code="bundle-integrity-failed")
    # Independently validate embedded identities (fail closed).
    wheel_releases = _validate_wheel_against_manifest(wheel_path=wheel_path,
                                                      manifest=manifest)
    pi_releases = _validate_pi_against_manifest(archive_path=pi_path,
                                                manifest=manifest, _run=_run)
    # Bundle-listed releases must equal independently validated ones.
    if wheel_releases != wheel_entry["releases"]:
        raise BundleError("Wheel release metadata mismatch",
                          code="bundle-identity-mismatch")
    if pi_releases != pi_entry["releases"]:
        raise BundleError("Pi release metadata mismatch",
                          code="bundle-identity-mismatch")
    return {"bundle": bundle, "manifest": manifest}


def assemble_bundle(*, output: Path, manifest: dict, wheel_path: Path,
                    pi_archive_path: Path, _run: Callable | None = None) -> dict:
    """Assemble + validate a bundle dir from prebuilt artifacts (no builds).

    Deterministic: repeated assembly from identical inputs produces
    identical ``bundle.json`` and artifact hashes. Validates embedded
    identities before publishing. Used by :func:`build_bundle` and by
    fixture tests without network/installation.
    """
    from .release_manifest import validate_manifest as _validate_manifest
    sanitized_manifest = _validate_manifest(manifest)
    _ensure_output_dir(output)
    # Validate wheels/pi before publishing any bundle file.
    wheel_releases = _validate_wheel_against_manifest(wheel_path=wheel_path,
                                                      manifest=sanitized_manifest)
    pi_releases = _validate_pi_against_manifest(archive_path=pi_archive_path,
                                                manifest=sanitized_manifest,
                                                _run=_run)
    wheel_sha, wheel_size = _hash_file(wheel_path)
    pi_sha, pi_size = _hash_file(pi_archive_path)
    artifacts = [
        {"logical": PI_LOGICAL, "kind": PI_KIND, "sha256": pi_sha,
         "size": pi_size, "components": sorted(PI_COMPONENTS),
         "releases": pi_releases},
        {"logical": WHEEL_LOGICAL, "kind": WHEEL_KIND, "sha256": wheel_sha,
         "size": wheel_size, "components": sorted(WHEEL_COMPONENTS),
         "releases": wheel_releases},
    ]
    artifacts.sort(key=lambda e: e["logical"])
    bundle_id = compute_bundle_id(
        product_version=sanitized_manifest["product_version"],
        manifest_id=sanitized_manifest["manifest_id"], artifacts=artifacts)
    bundle = {"schema_version": BUNDLE_SCHEMA_VERSION, "product": PRODUCT,
              "product_version": sanitized_manifest["product_version"],
              "manifest_id": sanitized_manifest["manifest_id"],
              "bundle_id": bundle_id, "receipt_id": bundle_id,
              "artifacts": artifacts}
    _assert_safe_metadata(bundle, sanitized_manifest)
    # Publish fixed bundle-relative paths only.
    manifest_bytes = (json.dumps(sanitized_manifest, sort_keys=True, indent=2)
                      + "\n").encode("utf-8")
    bundle_bytes = (json.dumps(bundle, sort_keys=True, indent=2)
                    + "\n").encode("utf-8")
    try:
        shutil.copyfile(wheel_path, output / WHEEL_LOGICAL)
        try:
            os.chmod(output / WHEEL_LOGICAL, 0o644)
        except OSError:
            pass
        shutil.copyfile(pi_archive_path, output / PI_LOGICAL)
        try:
            os.chmod(output / PI_LOGICAL, 0o644)
        except OSError:
            pass
    except OSError as exc:
        raise BundleError("Bundle artifact copy failed",
                          code="bundle-write-failed") from exc
    _write_bundle_file(output / TARGET_MANIFEST_LOGICAL, manifest_bytes)
    _write_bundle_file(output / BUNDLE_JSON_LOGICAL, bundle_bytes)
    # Final fail-closed validation of what was just published.
    return validate_release_bundle(output, _run=_run)


def build_bundle(*, output: Path | str,
                 _manager_build: Callable | None = None,
                 _wheel_build: Callable | None = None,
                 _pi_build: Callable | None = None,
                 _manifest_fn: Callable | None = None,
                 _run: Callable | None = None) -> dict:
    """Build and validate a deterministic release bundle (no state contact).

    Steps: Manager build → require served ``release.json`` equals target
    Manager identity → regenerate the target manifest AFTER the build →
    wheel build + normalize → Pi deterministic archive → assemble + strict
    validate. The output directory must be new/empty (non-symlink); temp
    work is private and cleaned on failure. Existing complete bundles are
    never overwritten. No Bridge state, admin credentials, live Nodes, or
    adapters are touched.
    """
    out = Path(output)
    # Reserve the output dir first (fail fast on overwrite/unsafe).
    _ensure_output_dir(out)
    parent = out.parent
    temp_dir = parent / (".tmp-bundle-" + os.urandom(4).hex())
    try:
        temp_dir.mkdir(mode=0o700, parents=False, exist_ok=False)
    except FileExistsError as exc:
        raise BundleError("Bundle temporary location exists",
                          code="bundle-unavailable") from exc
    except OSError as exc:
        raise BundleError("Bundle temporary location is unavailable",
                          code="bundle-unavailable") from exc
    try:
        os.chmod(temp_dir, 0o700)
    except OSError:
        _clean_temp_dir(temp_dir, parent)
        raise BundleError("Bundle temporary location is unavailable",
                          code="bundle-unavailable") from None
    try:
        # 1. Manager build from the current checkout (fixed npm argv).
        if _manager_build is not None:
            _manager_build()
        else:
            run_manager_build(_run=_run)
        # 2. Target manifest AFTER the build so it binds final generated state.
        if _manifest_fn is not None:
            manifest = _manifest_fn()
        else:
            manifest = build_target_manifest(_run=_run)
        from .release_manifest import validate_manifest as _validate_manifest
        manifest = _validate_manifest(manifest)
        # 3. Require packaged release.json equals the target Manager identity.
        try:
            release_path = (REPO_ROOT / "workspace_bridge" / "static" / "dist"
                            / "release.json")
            if release_path.is_symlink():
                raise BundleError("Manager release.json is unsafe",
                                  code="bundle-traversal")
            served = json.loads(release_path.read_bytes())
        except OSError as exc:
            raise BundleError("Manager release.json is unavailable",
                              code="bundle-build-failed") from exc
        except (ValueError, UnicodeError) as exc:
            raise BundleError("Manager release.json is invalid",
                              code="bundle-build-failed") from exc
        try:
            served_valid = validate_release(served)
        except Exception as exc:
            raise BundleError("Manager release.json is invalid",
                              code="bundle-identity-mismatch") from exc
        if served_valid != manifest["components"]["manager"]:
            raise BundleError("Manager build identity mismatch",
                              code="bundle-identity-mismatch")
        # 4. Python wheel (fixed uv argv) + deterministic normalize.
        wheel_staging = temp_dir / "wheel-out"
        wheel_staging.mkdir(mode=0o700, exist_ok=False)
        if _wheel_build is not None:
            raw_wheel = Path(_wheel_build(wheel_staging))
        else:
            raw_wheel = run_wheel_build(out_dir=wheel_staging, _run=_run)
        normalized_wheel = temp_dir / "python-wheel.whl"
        _normalize_wheel(src=raw_wheel, dest=normalized_wheel)
        # Validate the normalized wheel immediately (fail closed).
        _validate_wheel_against_manifest(wheel_path=normalized_wheel,
                                         manifest=manifest)
        # 5. Pi deterministic archive from exact release.mjs inventory.
        if _pi_build is not None:
            pi_archive = Path(_pi_build(temp_dir))
        else:
            names = list_pi_inputs(_run=_run)
            pi_archive = temp_dir / "pi-host-adapter.tar.gz"
            _create_pi_archive(src_root=PI_ROOT, names=names, dest=pi_archive)
        _validate_pi_against_manifest(archive_path=pi_archive,
                                      manifest=manifest, _run=_run)
        # 6. Assemble fixed bundle-relative paths + strict validate.
        wheel_sha, wheel_size = _hash_file(normalized_wheel)
        pi_sha, pi_size = _hash_file(pi_archive)
        # Reuse assemble logic but with already-validated artifacts to keep
        # publication atomic. Copy via private temp then rename? Output was
        # reserved empty; publish files directly then validate.
        from .release_manifest import validate_manifest as _vm
        manifest = _vm(manifest)
        wheel_releases = _validate_wheel_against_manifest(
            wheel_path=normalized_wheel, manifest=manifest)
        pi_releases = _validate_pi_against_manifest(
            archive_path=pi_archive, manifest=manifest, _run=_run)
        artifacts = [
            {"logical": PI_LOGICAL, "kind": PI_KIND, "sha256": pi_sha,
             "size": pi_size, "components": sorted(PI_COMPONENTS),
             "releases": pi_releases},
            {"logical": WHEEL_LOGICAL, "kind": WHEEL_KIND,
             "sha256": wheel_sha, "size": wheel_size,
             "components": sorted(WHEEL_COMPONENTS),
             "releases": wheel_releases},
        ]
        artifacts.sort(key=lambda e: e["logical"])
        bundle_id = compute_bundle_id(
            product_version=manifest["product_version"],
            manifest_id=manifest["manifest_id"], artifacts=artifacts)
        bundle = {"schema_version": BUNDLE_SCHEMA_VERSION, "product": PRODUCT,
                  "product_version": manifest["product_version"],
                  "manifest_id": manifest["manifest_id"],
                  "bundle_id": bundle_id, "receipt_id": bundle_id,
                  "artifacts": artifacts}
        _assert_safe_metadata(bundle, manifest)
        manifest_bytes = (json.dumps(manifest, sort_keys=True, indent=2)
                          + "\n").encode("utf-8")
        bundle_bytes = (json.dumps(bundle, sort_keys=True, indent=2)
                        + "\n").encode("utf-8")
        try:
            shutil.copyfile(normalized_wheel, out / WHEEL_LOGICAL)
            try:
                os.chmod(out / WHEEL_LOGICAL, 0o644)
            except OSError:
                pass
            shutil.copyfile(pi_archive, out / PI_LOGICAL)
            try:
                os.chmod(out / PI_LOGICAL, 0o644)
            except OSError:
                pass
        except OSError as exc:
            raise BundleError("Bundle artifact copy failed",
                              code="bundle-write-failed") from exc
        _write_bundle_file(out / TARGET_MANIFEST_LOGICAL, manifest_bytes)
        _write_bundle_file(out / BUNDLE_JSON_LOGICAL, bundle_bytes)
        validated = validate_release_bundle(out, _run=_run)
        return validated
    finally:
        _clean_temp_dir(temp_dir, parent)


def short_id(value: str) -> str:
    """Safe shortened display prefix for a validated ``sha256:`` ID."""
    if (isinstance(value, str) and value.startswith("sha256:")
            and len(value) == 71):
        return "sha256:" + value[7:19]
    return "sha256:unknown"
