"""Deterministic release target-manifest helpers (release engineering only).

A target manifest pins exact M4.1 release identities for all five released
components (bridge, manager, node, codex-host-adapter, pi-host-adapter) plus
a content-addressed ``manifest_id``. No paths, hosts, tokens, timestamps,
prompts, or results ever appear in manifest output.

This module is pure: manifest construction/validation with no live
topology, no planning, no staging, no backup, no install, no restart, and
no network. It exists so release tooling and release CI can verify C1/C1.1
artifacts deterministically. Product runtime never plans or applies
updates; component updates are manual local operations on each host.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from .release import ReleaseError, validate_release

MANIFEST_SCHEMA_VERSION = 1
PRODUCT = "workspace-bridge"

TARGET_COMPONENTS = ("bridge", "manager", "node",
                     "codex-host-adapter", "pi-host-adapter")

RUNTIME_TO_TARGET = {"pi": "pi-host-adapter",
                     "codex": "codex-host-adapter"}

_MANIFEST_KEYS = frozenset({
    "schema_version", "product", "product_version",
    "manifest_id", "components",
})

_VERSION_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._-]{0,31}$")
_BUILD_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class ManifestError(ValueError):
    """Strict target-manifest validation failure.

    ``kind`` is ``"unsupported"`` for an explicitly incompatible release
    contract and ``"invalid"`` for any other malformed manifest.
    """

    def __init__(self, message: str, *, kind: str = "invalid"):
        super().__init__(message)
        self.kind = kind


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def compute_manifest_id(*, product_version: str,
                        components: dict[str, dict]) -> str:
    """Deterministic content-addressed ID over canonical manifest content.

    The ID covers ``schema_version``/``product``/``product_version`` plus
    the exact component release identities, excluding ``manifest_id``
    itself. Key order, whitespace, and encoding are fixed so local and
    pipeline builds agree.
    """
    canonical = {"components": components, "product": PRODUCT,
                 "product_version": product_version,
                 "schema_version": MANIFEST_SCHEMA_VERSION}
    digest = hashlib.sha256(_canonical_bytes(canonical)).hexdigest()
    return "sha256:" + digest


def make_manifest(*, product_version: str, bridge: dict,
                  manager: dict, node: dict,
                  codex: dict, pi: dict) -> dict:
    """Build a validated target manifest with a deterministic ID."""
    components = {"bridge": bridge, "manager": manager, "node": node,
                  "codex-host-adapter": codex, "pi-host-adapter": pi}
    manifest_id = compute_manifest_id(product_version=product_version,
                                      components=components)
    return validate_manifest({
        "schema_version": MANIFEST_SCHEMA_VERSION, "product": PRODUCT,
        "product_version": product_version, "manifest_id": manifest_id,
        "components": components,
    })


def validate_manifest(value: object) -> dict:
    """Strictly validate a release target manifest, returning a copy."""
    if not isinstance(value, dict):
        raise ManifestError("Deployment manifest must be an object",
                            kind="invalid")
    if set(value) != set(_MANIFEST_KEYS):
        raise ManifestError("Deployment manifest has unexpected fields",
                            kind="invalid")
    schema = value.get("schema_version")
    if isinstance(schema, bool) or schema != MANIFEST_SCHEMA_VERSION:
        raise ManifestError("Deployment manifest schema is unsupported",
                            kind="invalid")
    product = value.get("product")
    if product != PRODUCT:
        raise ManifestError("Deployment manifest product is invalid",
                            kind="invalid")
    product_version = value.get("product_version")
    if (not isinstance(product_version, str)
            or not _VERSION_RE.fullmatch(product_version)):
        raise ManifestError("Deployment manifest product version is invalid",
                            kind="invalid")
    manifest_id = value.get("manifest_id")
    if (not isinstance(manifest_id, str)
            or not _BUILD_ID_RE.fullmatch(manifest_id)):
        raise ManifestError("Deployment manifest ID is invalid",
                            kind="invalid")
    components = value.get("components")
    if not isinstance(components, dict) or set(components) != set(TARGET_COMPONENTS):
        raise ManifestError("Deployment manifest components are invalid",
                            kind="invalid")
    sanitized: dict[str, dict] = {}
    unsupported_seen = False
    for name in TARGET_COMPONENTS:
        raw = components.get(name)
        try:
            release = validate_release(raw)
        except ReleaseError as exc:
            if getattr(exc, "kind", "invalid") == "unsupported":
                unsupported_seen = True
                raise ManifestError(
                    f"Deployment target {name} release contract is unsupported",
                    kind="unsupported") from None
            raise ManifestError(
                f"Deployment target {name} release is invalid",
                kind="invalid") from None
        if release["component"] != name:
            raise ManifestError(
                f"Deployment target {name} component mismatch",
                kind="invalid")
        if release["product_version"] != product_version:
            raise ManifestError(
                f"Deployment target {name} product version mismatch",
                kind="invalid")
        sanitized[name] = release
    # Python-core parity: Bridge, Node and Codex share the same source
    # package, so their build IDs must agree. Pi and Manager use
    # independent artifact build IDs and are never compared to core.
    core_ids = {sanitized[name]["build_id"]
                for name in ("bridge", "node", "codex-host-adapter")}
    if len(core_ids) != 1:
        raise ManifestError(
            "Deployment target Python-core build IDs must match",
            kind="invalid")
    expected = compute_manifest_id(product_version=product_version,
                                   components=sanitized)
    if expected != manifest_id:
        raise ManifestError("Deployment manifest ID mismatch",
                            kind="invalid")
    _ = unsupported_seen
    return {"schema_version": MANIFEST_SCHEMA_VERSION, "product": PRODUCT,
            "product_version": product_version, "manifest_id": manifest_id,
            "components": sanitized}
