"""M4.1 content-addressed release identity for Workspace Bridge.

Every deployed component exposes a bounded, content-addressed identity:

    { contract: 1, product: "workspace-bridge",
      product_version: <root release>, component: <bounded role>,
      component_version: <bounded version>, build_id: "sha256:<64 hex>" }

``build_id`` is deterministic over production inputs only. It never uses
live Git state, paths, timestamps, hostnames, tokens, mutable instance IDs,
or environment-only values. Filesystem paths never appear in the public
result. This is source/package identity, not a container image digest or
code-signing provenance; image/artifact provenance belongs to later work.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from . import __version__

RELEASE_CONTRACT = 1
PRODUCT = "workspace-bridge"

COMPONENTS = frozenset({
    "bridge", "node", "codex-host-adapter", "pi-host-adapter", "manager",
})

_VERSION_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._-]{0,31}$")
_BUILD_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_RELEASE_KEYS = frozenset({
    "contract", "product", "product_version",
    "component", "component_version", "build_id",
})

CODEX_ADAPTER_VERSION = "0.1.0"


class ReleaseError(ValueError):
    """Strict release-identity validation failure.

    ``kind`` is ``"unsupported"`` for an explicitly incompatible release
    contract and ``"invalid"`` for any other malformed present identity.
    Missing identity is not an error; callers represent it as ``None``.
    """

    def __init__(self, message: str, *, kind: str = "invalid"):
        super().__init__(message)
        self.kind = kind


def validate_release(value: object) -> dict:
    """Strictly validate a bounded release identity, returning a sanitized copy."""
    if not isinstance(value, dict):
        raise ReleaseError("Release identity must be an object", kind="invalid")
    if set(value) != set(_RELEASE_KEYS):
        raise ReleaseError("Release identity has unexpected fields", kind="invalid")
    contract = value.get("contract")
    if isinstance(contract, bool) or contract != RELEASE_CONTRACT:
        # An explicitly versioned but incompatible contract is actionable,
        # not a routine skew warning.
        raise ReleaseError("Runtime release contract is unsupported", kind="unsupported")
    product = value.get("product")
    product_version = value.get("product_version")
    component = value.get("component")
    component_version = value.get("component_version")
    build_id = value.get("build_id")
    if product != PRODUCT:
        raise ReleaseError("Release product is invalid", kind="invalid")
    if (not isinstance(product_version, str) or not _VERSION_RE.fullmatch(product_version)):
        raise ReleaseError("Release product version is invalid", kind="invalid")
    if not isinstance(component, str) or component not in COMPONENTS:
        raise ReleaseError("Release component is invalid", kind="invalid")
    if (not isinstance(component_version, str)
            or not _VERSION_RE.fullmatch(component_version)):
        raise ReleaseError("Release component version is invalid", kind="invalid")
    if not isinstance(build_id, str) or not _BUILD_ID_RE.fullmatch(build_id):
        raise ReleaseError("Release build ID is invalid", kind="invalid")
    return {"contract": 1, "product": PRODUCT,
            "product_version": product_version, "component": component,
            "component_version": component_version, "build_id": build_id}


def make_release(*, component: str, component_version: str,
                 build_id: str, product_version: str | None = None) -> dict:
    """Build a validated release identity (raises ReleaseError when invalid)."""
    return validate_release({
        "contract": RELEASE_CONTRACT, "product": PRODUCT,
        "product_version": product_version or __version__,
        "component": component, "component_version": component_version,
        "build_id": build_id,
    })


def short_build_id(build_id: str) -> str:
    """Concise display prefix for a validated ``sha256:`` build ID."""
    if isinstance(build_id, str) and build_id.startswith("sha256:") and len(build_id) == 71:
        return "sha256:" + build_id[7:19]
    return "sha256:unknown"


_CACHED_CORE_BUILD_ID: str | None = None


def _iter_core_inputs(base: Path) -> list[tuple[str, Path]]:
    rows: list[tuple[str, Path]] = []
    for path in base.rglob("*"):
        if not path.is_file() or path.is_symlink():
            # Installed/source trees must not follow symlinks for identity.
            # Symlinked files are deployment noise, not production inputs.
            continue
        try:
            rel = path.relative_to(base).as_posix()
        except ValueError:
            continue
        parts = rel.split("/")
        if "__pycache__" in parts or ".pytest_cache" in parts:
            continue
        if path.suffix in {".pyc", ".pyo", ".pyd"}:
            continue
        if rel.startswith("static/dist/") or rel == "static/dist":
            continue
        if path.suffix == ".py":
            rows.append((rel, path))
        elif rel.startswith("skills/") and path.name == "SKILL.md":
            rows.append((rel, path))
        # All other runtime/generated noise (egg-info, logs, sqlite, dist
        # artifacts, OS metadata) is excluded so Manager identity stays
        # independently diagnosable.
    rows.sort(key=lambda row: row[0])
    return rows


def python_core_build_id(*, refresh: bool = False) -> str:
    """Deterministic ``sha256:`` ID over production Python/skill inputs.

    Identical for Bridge, Node and Codex when they run the same Workspace
    Bridge source/package; changes when production Python or embedded-skill
    behavior changes. Excludes Manager compiled assets, bytecode and
    runtime noise. No filesystem paths enter the public result.
    """
    global _CACHED_CORE_BUILD_ID
    if _CACHED_CORE_BUILD_ID is not None and not refresh:
        return _CACHED_CORE_BUILD_ID
    base = Path(__file__).resolve().parent
    hasher = hashlib.sha256()
    hasher.update(b"workspace-bridge-python-core-v1\x00")
    for rel, path in _iter_core_inputs(base):
        name_bytes = rel.encode("utf-8")
        try:
            data = path.read_bytes()
        except OSError:
            # An unreadable production input fails closed as empty bytes?
            # No: identity must change loudly rather than silently match.
            # Re-raise so callers surface a startup/diagnostic failure.
            raise
        hasher.update(len(name_bytes).to_bytes(8, "big"))
        hasher.update(name_bytes)
        hasher.update(b"\x00")
        hasher.update(len(data).to_bytes(8, "big"))
        hasher.update(data)
        hasher.update(b"\x00")
    value = "sha256:" + hasher.hexdigest()
    _CACHED_CORE_BUILD_ID = value
    return value


def bridge_release() -> dict:
    """Release identity for the Bridge control plane (component ``bridge``)."""
    return make_release(component="bridge", component_version=__version__,
                        build_id=python_core_build_id(),
                        product_version=__version__)


def node_release() -> dict:
    """Release identity for the authoritative Node (component ``node``)."""
    return make_release(component="node", component_version=__version__,
                        build_id=python_core_build_id(),
                        product_version=__version__)


def codex_release() -> dict:
    """Release identity for the Codex host adapter.

    The Python core is shared with Bridge/Node, so the build ID is the same
    Python-core ID. The component version stays the adapter contract version
    (``0.1.0``); ``adapterVersion``/``nativeVersion`` remain separate fields.
    The descriptor ``adapterVersion`` reuses this constant so release identity
    cannot drift from the advertised descriptor.
    """
    return make_release(component="codex-host-adapter",
                        component_version=CODEX_ADAPTER_VERSION,
                        build_id=python_core_build_id(),
                        product_version=__version__)


def read_manager_release() -> dict | None:
    """Read/strictly validate the served Manager ``static/dist/release.json``.

    Returns the sanitized ``manager`` identity, or ``None`` when compiled
    Manager metadata is missing or invalid. Never fabricates a match.
    """
    path = Path(__file__).resolve().parent / "static" / "dist" / "release.json"
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    if len(raw) > 8192:
        return None
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError):
        return None
    try:
        sanitized = validate_release(value)
    except ReleaseError:
        return None
    if sanitized["component"] != "manager":
        return None
    return sanitized
