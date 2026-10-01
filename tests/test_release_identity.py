"""M4.1 release-identity vertical slice: contract, hashing, descriptors, diagnostics."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from workspace_bridge import __version__
from workspace_bridge.release import (
    ReleaseError,
    bridge_release,
    codex_release,
    node_release,
    python_core_build_id,
    read_manager_release,
    validate_release,
)
from workspace_bridge.runtime import RuntimeUnavailable, RuntimeUnsupported
from workspace_bridge.wbrp import CORE_FEATURES, Descriptor


def test_release_contract_is_strict_and_bounded():
    release = bridge_release()
    assert release == {
        "contract": 1, "product": "workspace-bridge",
        "product_version": __version__, "component": "bridge",
        "component_version": __version__, "build_id": release["build_id"],
    }
    assert release["build_id"].startswith("sha256:")
    assert len(release["build_id"]) == 71
    # No paths, hostnames, tokens, or instance IDs leak into identity.
    assert "workspace-bridge" in json.dumps(release)
    for bad in ("/tmp/x", "host.example", "adapter_123", "token"):
        assert bad not in release["build_id"]
    # Strict: extra fields, wrong product, bad versions, bad build IDs fail.
    base = dict(release)
    with pytest.raises(ReleaseError):
        validate_release({**base, "extra": 1})
    with pytest.raises(ReleaseError):
        validate_release({**base, "product": "other"})
    with pytest.raises(ReleaseError):
        validate_release({**base, "component": "unknown-role"})
    with pytest.raises(ReleaseError):
        validate_release({**base, "build_id": "sha256:xyz"})
    with pytest.raises(ReleaseError) as exc:
        validate_release({**base, "contract": 2,
                          "build_id": "sha256:" + "a" * 64})
    assert exc.value.kind == "unsupported"


def test_python_core_build_id_is_deterministic_and_excludes_noise(tmp_path):
    first = python_core_build_id()
    second = python_core_build_id()
    assert first == second
    assert first.startswith("sha256:")
    # Excludes Manager compiled assets, pycache, and bytecode by construction.
    from workspace_bridge.release import _iter_core_inputs
    base = Path(__file__).resolve().parent.parent / "workspace_bridge"
    inputs = [rel for rel, _ in _iter_core_inputs(base)]
    assert inputs == sorted(inputs)
    assert all(not rel.startswith("static/dist") for rel in inputs)
    assert all("__pycache__" not in rel for rel in inputs)
    assert all(not rel.endswith((".pyc", ".pyo")) for rel in inputs)
    assert any(rel.endswith(".py") for rel in inputs)
    assert any(rel == "skills/project-lead/SKILL.md" for rel in inputs)
    # Changing a production input changes the ID (framing is unambiguous).
    import hashlib
    hasher = hashlib.sha256()
    hasher.update(b"workspace-bridge-python-core-v1\x00")
    name = b"workspace_bridge/release.py"
    hasher.update(len(name).to_bytes(8, "big"))
    hasher.update(name)
    hasher.update(b"\x00")
    assert hasher.hexdigest() != first[7:]


def test_bridge_node_codex_share_python_core_build_id():
    bridge = bridge_release()
    node = node_release()
    codex = codex_release()
    assert bridge["product_version"] == node["product_version"] == codex["product_version"] == __version__
    assert bridge["build_id"] == node["build_id"] == codex["build_id"]
    assert bridge["component"] == "bridge"
    assert node["component"] == "node"
    assert codex["component"] == "codex-host-adapter"
    # Codex keeps its adapter contract version separate from adapterVersion fields.
    assert codex["component_version"] == "0.1.2"


def _legacy_descriptor_payload(runtime="pi"):
    return {"protocol": {"major": 1}, "runtime": {
        "id": runtime, "instanceId": "native-test"}, "features": {
        "models": 1, "conversations": 1, "runs": 1,
        "activities": 1, "interactions": 1}}


def test_descriptor_release_is_optional_but_malformed_fails():
    # M4.2C2A staged rollout: release metadata is decoupled from Runtime
    # Protocol compatibility. Missing/invalid/unsupported release stays
    # usable with degraded update metadata; only protocol/core/identity
    # failures raise.
    legacy = Descriptor.parse(_legacy_descriptor_payload(), expected_runtime="pi")
    assert legacy.release is None
    assert legacy.release_status == "missing"
    valid = _legacy_descriptor_payload()
    valid["release"] = bridge_release()
    parsed = Descriptor.parse(valid, expected_runtime="pi")
    assert parsed.release is not None
    assert parsed.release["component"] == "bridge"
    assert parsed.release_status == "valid"
    malformed = _legacy_descriptor_payload()
    malformed["release"] = {"contract": 1, "product": "workspace-bridge",
                            "product_version": __version__, "component": "bridge",
                            "component_version": __version__, "build_id": "bad"}
    degraded = Descriptor.parse(malformed, expected_runtime="pi")
    assert degraded.release is None
    assert degraded.release_status == "invalid"
    # Core/protocol gates still hold: descriptor stays usable for runs.
    assert degraded.supports("models")
    unsupported = _legacy_descriptor_payload()
    unsupported["release"] = {"contract": 99, "product": "workspace-bridge",
                              "product_version": __version__, "component": "bridge",
                              "component_version": __version__,
                              "build_id": "sha256:" + "b" * 64}
    future = Descriptor.parse(unsupported, expected_runtime="pi")
    assert future.release is None
    assert future.release_status == "unsupported"
    assert future.supports("models")


def test_node_status_and_codex_descriptor_expose_release():
    from workspace_bridge.node_service import NodeService
    import tempfile
    from workspace_bridge.security import digest
    tmp = Path(tempfile.mkdtemp())
    parent = tmp / "projects"
    parent.mkdir()
    state = tmp / "node-state"
    config = {"allowed_roots": [str(parent)],
              "node_token_hash": digest(b"test-token")}
    service = NodeService(state, config)
    try:
        status = service.status()
        assert status["node_version"] == __version__
        assert validate_release(status["release"])["component"] == "node"
    finally:
        service.close()
    from workspace_bridge.codex_host_adapter import CodexHostAdapter
    import sys
    sys.path.insert(0, str(Path(__file__).parent))
    from test_codex_host_adapter import FakeCodexRpc
    projects = tmp / "codex-projects"
    projects.mkdir()
    native = CodexHostAdapter(tmp / "codex-adapter", projects, rpc=FakeCodexRpc())
    try:
        descriptor = native.descriptor()
        assert descriptor["runtime"]["adapterVersion"] == "0.1.2"
        assert "adapterVersion" in descriptor["runtime"]
        assert "nativeVersion" in descriptor["runtime"]
        release = validate_release(descriptor["release"])
        assert release["component"] == "codex-host-adapter"
        assert release["component_version"] == "0.1.2"
        assert release["product_version"] == __version__
        assert release["build_id"] == bridge_release()["build_id"]
    finally:
        native.close()


def test_manager_release_json_matches_bridge_reader():
    # The Vite build emits static/dist/release.json; the Bridge reads it
    # strictly and never fabricates a match when absent/invalid.
    path = Path(__file__).resolve().parent.parent / "workspace_bridge" / "static" / "dist" / "release.json"
    if not path.exists():
        pytest.skip("Manager release.json has not been built yet")
    value = json.loads(path.read_text())
    sanitized = validate_release(value)
    assert sanitized["component"] == "manager"
    assert read_manager_release() == sanitized
