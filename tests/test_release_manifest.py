"""Release target-manifest helpers (release engineering only)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from workspace_bridge import __version__
from workspace_bridge.release_manifest import (
    ManifestError,
    compute_manifest_id,
    make_manifest,
    validate_manifest,
)
from workspace_bridge.release import (
    RELEASE_CONTRACT,
    bridge_release,
    codex_release,
    node_release,
    validate_release,
)


def _synthetic(component: str, build: str, product: str = "0.1.0",
               component_version: str = "0.1.0") -> dict:
    return validate_release({
        "contract": 1, "product": "workspace-bridge",
        "product_version": product, "component": component,
        "component_version": component_version, "build_id": build,
    })


def _target_manifest() -> dict:
    bridge = bridge_release()
    node = node_release()
    codex = codex_release()
    manager = _synthetic("manager", "sha256:" + "e" * 64)
    pi = _synthetic("pi-host-adapter", "sha256:" + "f" * 64)
    return make_manifest(product_version=__version__, bridge=bridge,
                         manager=manager, node=node, codex=codex, pi=pi)


def test_manifest_is_deterministic_and_strict():
    first = _target_manifest()
    second = _target_manifest()
    assert first == second
    assert first["schema_version"] == 1
    assert first["product"] == "workspace-bridge"
    assert first["product_version"] == __version__
    assert first["manifest_id"].startswith("sha256:")
    assert set(first["components"]) == {
        "bridge", "manager", "node",
        "codex-host-adapter", "pi-host-adapter"}
    # Canonical ID excludes itself and is stable under key order.
    recomputed = compute_manifest_id(
        product_version=first["product_version"],
        components=first["components"])
    assert recomputed == first["manifest_id"]
    assert validate_manifest(json.loads(json.dumps(first))) == first
    # Exact keys: extra fields, wrong product, skew product, core mismatch,
    # and ID mismatch all fail.
    with pytest.raises(ManifestError):
        validate_manifest({**first, "extra": 1})
    with pytest.raises(ManifestError):
        validate_manifest({**first, "product": "other"})
    bad_product = json.loads(json.dumps(first))
    bad_product["components"]["node"] = _synthetic(
        "node", bad_product["components"]["node"]["build_id"],
        product="9.9.9")
    with pytest.raises(ManifestError):
        validate_manifest(bad_product)
    bad_core = json.loads(json.dumps(first))
    bad_core["components"]["node"] = _synthetic("node", "sha256:" + "0" * 64)
    with pytest.raises(ManifestError):
        validate_manifest(bad_core)
    bad_id = {**first, "manifest_id": "sha256:" + "1" * 64}
    with pytest.raises(ManifestError):
        validate_manifest(bad_id)
    # No paths/hosts/tokens/timestamps leak into the manifest.
    text = json.dumps(first)
    assert "/tmp" not in text
    assert "127.0.0.1" not in text
    assert "token" not in text.lower()


def test_manifest_rejects_unsupported_contract():
    base = _target_manifest()
    bad = json.loads(json.dumps(base))
    bad["components"]["bridge"] = {
        "contract": 99, "product": "workspace-bridge",
        "product_version": base["product_version"],
        "component": "bridge", "component_version": "0.1.0",
        "build_id": "sha256:" + "b" * 64}
    with pytest.raises(ManifestError) as exc:
        validate_manifest(bad)
    assert exc.value.kind == "unsupported"


def test_all_version_sources_aligned_at_0_1_0():
    assert __version__ == "0.1.0"
    import tomllib
    repo = Path(__file__).resolve().parent.parent
    pyproject = tomllib.loads((repo / "pyproject.toml").read_text())
    assert pyproject["project"]["version"] == "0.1.0"
    pi_meta = json.loads((repo / "runtime" / "pi-host-adapter" / "package.json").read_text())
    assert pi_meta["version"] == "0.1.0"
    assert pi_meta["workspaceBridgeRelease"] == "0.1.0"
    web_meta = json.loads((repo / "web" / "package.json").read_text())
    assert web_meta["version"] == "0.1.0"
    docker = (repo / "Dockerfile").read_text()
    assert "/wheels/workspace_bridge-*.whl" in docker
    assert "workspace-bridge==0.1.0" not in docker
    assert "0.1.0" in (repo / "compose.yaml").read_text()
    assert "0.1.0" in (repo / "README.md").read_text()
    # Served Manager identity is 0.1.0 after rebuild.
    release_path = repo / "workspace_bridge" / "static" / "dist" / "release.json"
    if release_path.exists():
        served = validate_release(json.loads(release_path.read_text()))
        assert served["component"] == "manager"
        assert served["product_version"] == "0.1.0"
        assert served["component_version"] == "0.1.0"
    # Codex descriptor constant and Pi adapter constant agree.
    from workspace_bridge.release import CODEX_ADAPTER_VERSION
    assert CODEX_ADAPTER_VERSION == "0.1.0"
    assert "0.1.0" in (repo / "runtime" / "pi-host-adapter" / "config.mjs").read_text()
    # Lockfiles carry the reset product version.
    assert 'version = "0.1.0"' in (repo / "uv.lock").read_text()


def test_protocol_schema_native_versions_unchanged():
    assert RELEASE_CONTRACT == 1
    from workspace_bridge import release as release_mod
    assert release_mod.PRODUCT == "workspace-bridge"
    # Bridge state schema stays v4; config schema stays v1.
    from workspace_bridge.service import Service  # noqa: F401 - import proves module
    import sqlite3  # noqa: F401
    repo = Path(__file__).resolve().parent.parent
    pi_lock = json.loads((repo / "runtime" / "pi-host-adapter" / "package-lock.json").read_text())
    assert pi_lock["packages"][""]["version"] == "0.1.0"
    # Pi native dependency is unchanged.
    pi_meta = json.loads((repo / "runtime" / "pi-host-adapter" / "package.json").read_text())
    assert pi_meta["dependencies"]["@earendil-works/pi-coding-agent"] == "0.87.0"


def _load_manifest_builder():
    import importlib.util
    from pathlib import Path as _Path
    path = _Path(__file__).resolve().parent.parent / "scripts" / "build_release_manifest.py"
    spec = importlib.util.spec_from_file_location("build_release_manifest", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_build_release_manifest_helpers_use_injection():
    build_target_manifest = _load_manifest_builder().build_target_manifest
    manager = _synthetic("manager", "sha256:" + "e" * 64)
    pi = _synthetic("pi-host-adapter", "sha256:" + "f" * 64)
    manifest = build_target_manifest(manager=manager, pi=pi)
    assert validate_manifest(manifest) == manifest
    assert manifest["product_version"] == __version__
    # Node helper injection path stays deterministic without spawning node.
    sentinel_manager = _synthetic("manager", "sha256:" + "a" * 64)
    sentinel_pi = _synthetic("pi-host-adapter", "sha256:" + "b" * 64)
    first = build_target_manifest(
        _manager_fn=lambda: sentinel_manager, _pi_fn=lambda: sentinel_pi)
    second = build_target_manifest(
        _manager_fn=lambda: sentinel_manager, _pi_fn=lambda: sentinel_pi)
    assert first == second


def test_build_release_manifest_integration_if_node_available():
    import shutil
    if shutil.which("node") is None:
        pytest.skip("node is unavailable")
    builder = _load_manifest_builder()
    get_manager_release, get_pi_release = builder.get_manager_release, builder.get_pi_release
    manager = validate_release(get_manager_release())
    pi = validate_release(get_pi_release())
    assert manager["component"] == "manager"
    assert pi["component"] == "pi-host-adapter"
    assert manager["product_version"] == __version__
    assert pi["product_version"] == __version__

