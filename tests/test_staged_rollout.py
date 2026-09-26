"""Read-only release/version compatibility: 0.1.0 is the first supported baseline.

Generic Runtime Protocol release metadata stays optional and never blocks
protocol-supported execution by itself. Missing/invalid/unsupported
first-party Workspace Bridge release identity maps to `unsupported_build`
with execution compatibility reflecting actual protocol proof.
Compatibility state is informational only; component updates are manual
local host operations.
"""
from __future__ import annotations

import json

import httpx
import pytest

from workspace_bridge import __version__
from workspace_bridge.api import make_admin
from workspace_bridge.cli import initialize
from workspace_bridge.release import bridge_release, validate_release
from workspace_bridge.runtime import RuntimeUnavailable, RuntimeUnsupported
from workspace_bridge.security import BridgeError
from workspace_bridge.service import Service
from workspace_bridge.staged_rollout import (
    build_staged_status,
    classify_component,
    compare_product_versions,
    rollout_live,
)
from workspace_bridge.wbrp import CORE_FEATURES, Descriptor

from conftest import attach_test_node


def _synthetic(component: str, build: str, product: str = "0.1.0",
               component_version: str = "0.1.0") -> dict:
    return validate_release({
        "contract": 1, "product": "workspace-bridge",
        "product_version": product, "component": component,
        "component_version": component_version, "build_id": build,
    })


def _descriptor_payload(runtime="pi", features=None):
    feats = dict.fromkeys(CORE_FEATURES, 1) if features is None else features
    return {"protocol": {"major": 1}, "runtime": {
        "id": runtime, "instanceId": "native-test"}, "features": feats}


# ---------------------------------------------------------------- pure versions

def test_compare_product_versions_numeric_and_non_comparable():
    assert compare_product_versions("0.1.0", "0.1.0") == "equal"
    assert compare_product_versions("0.1.0", "0.2.0") == "older"
    assert compare_product_versions("0.2.0", "0.1.0") == "newer"
    assert compare_product_versions("0.1.0", "not-a-version") == "non-comparable"
    assert compare_product_versions("not-a-version", "0.1.0") == "non-comparable"
    assert compare_product_versions("a", "b") == "non-comparable"


def test_classifier_exact_is_current():
    bridge = bridge_release()
    entry = classify_component(
        component="node", instance="node_000000000000000000000001",
        current=dict(bridge, component="node"),
        observation="valid",
        target_product_version=bridge["product_version"],
        target_build_id=bridge["build_id"], target_precision="exact",
        reachable=True, protocol_compatible=True)
    assert entry["state"] == "current"
    assert entry["execution_compatible"] is True
    assert entry["reason"] == "exact-match"


def test_classifier_older_product_is_update_available():
    bridge = bridge_release()
    old = _synthetic("node", bridge["build_id"], product="0.1.0")
    entry = classify_component(
        component="node", instance="n1", current=old, observation="valid",
        target_product_version="0.2.0", target_build_id=bridge["build_id"],
        target_precision="exact", reachable=True, protocol_compatible=True)
    assert entry["state"] == "update_available"
    assert entry["execution_compatible"] is True
    assert entry["reason"] == "product-older"


def test_classifier_newer_product_is_target_mismatch_never_downgrade():
    bridge = bridge_release()
    new = _synthetic("node", bridge["build_id"], product="0.3.0")
    entry = classify_component(
        component="node", instance="n1", current=new, observation="valid",
        target_product_version="0.2.0", target_build_id=bridge["build_id"],
        target_precision="exact", reachable=True, protocol_compatible=True)
    assert entry["state"] == "target_mismatch"
    assert entry["execution_compatible"] is True
    assert entry["reason"] == "product-newer"


def test_classifier_non_comparable_is_target_mismatch():
    bridge = bridge_release()
    odd = _synthetic("node", bridge["build_id"], product="custom-build")
    entry = classify_component(
        component="node", instance="n1", current=odd, observation="valid",
        target_product_version="0.2.0", target_build_id=bridge["build_id"],
        target_precision="exact", reachable=True, protocol_compatible=True)
    assert entry["state"] == "target_mismatch"
    assert entry["reason"] == "non-comparable"


def test_classifier_same_version_build_skew_is_update_available():
    bridge = bridge_release()
    skewed = _synthetic("node", "sha256:" + "c" * 64,
                        product=bridge["product_version"])
    entry = classify_component(
        component="node", instance="n1", current=skewed, observation="valid",
        target_product_version=bridge["product_version"],
        target_build_id=bridge["build_id"], target_precision="exact",
        reachable=True, protocol_compatible=True)
    assert entry["state"] == "update_available"
    assert entry["reason"] == "build-skew"


def test_classifier_pi_same_product_is_current_despite_build_skew():
    bridge = bridge_release()
    pi = _synthetic("pi-host-adapter", "sha256:" + "d" * 64,
                    product=bridge["product_version"])
    entry = classify_component(
        component="pi-host-adapter", instance="a1", current=pi,
        observation="valid",
        target_product_version=bridge["product_version"],
        target_build_id=None, target_precision="product-version-only",
        reachable=True, protocol_compatible=True,
        runtime_type="pi", instance_id="pi_instance_x")
    assert entry["state"] == "current"
    assert entry["execution_compatible"] is True
    assert entry["target_precision"] == "product-version-only"


def test_classifier_missing_is_unsupported_build_with_protocol_proof():
    bridge = bridge_release()
    entry = classify_component(
        component="codex-host-adapter", instance="a1", current=None,
        observation="missing",
        target_product_version=bridge["product_version"],
        target_build_id=bridge["build_id"], target_precision="exact",
        reachable=True, protocol_compatible=True, runtime_type="codex")
    assert entry["state"] == "unsupported_build"
    assert entry["execution_compatible"] is True
    assert entry["reason"] == "missing-release"


def test_classifier_invalid_unsupported_are_unsupported_build():
    bridge = bridge_release()
    for obs, reason in (("invalid", "release-invalid"),
                        ("unsupported", "release-unsupported")):
        entry = classify_component(
            component="codex-host-adapter", instance="a1", current=None,
            observation=obs,
            target_product_version=bridge["product_version"],
            target_build_id=bridge["build_id"], target_precision="exact",
            reachable=True, protocol_compatible=True, runtime_type="codex")
        assert entry["state"] == "unsupported_build"
        assert entry["execution_compatible"] is True
        assert entry["reason"] == reason


def test_classifier_incompatible_and_unavailable_are_not_compatible():
    bridge = bridge_release()
    bad = classify_component(
        component="codex-host-adapter", instance="a1", current=None,
        observation="unobserved",
        target_product_version=bridge["product_version"],
        target_build_id=bridge["build_id"], target_precision="exact",
        reachable=True, protocol_compatible=False,
        protocol_reason="protocol-mismatch", runtime_type="codex")
    assert bad["state"] == "incompatible"
    assert bad["execution_compatible"] is False
    down = classify_component(
        component="node", instance="n1", current=None,
        observation="unobserved",
        target_product_version=bridge["product_version"],
        target_build_id=bridge["build_id"], target_precision="exact",
        reachable=False, protocol_compatible=False)
    assert down["state"] == "unavailable"
    assert down["execution_compatible"] is False


def test_bridge_02_target_with_01_components_is_compatible_update():
    target_product = "0.2.0"
    target_build = "sha256:" + "e" * 64
    old_node = _synthetic("node", "sha256:" + "a" * 64, product="0.1.0")
    old_codex = _synthetic("codex-host-adapter", "sha256:" + "b" * 64,
                           product="0.1.0")
    old_pi = _synthetic("pi-host-adapter", "sha256:" + "c" * 64,
                        product="0.1.0")
    for component, current, precision, build in (
            ("node", old_node, "exact", target_build),
            ("codex-host-adapter", old_codex, "exact", target_build),
            ("pi-host-adapter", old_pi, "product-version-only", None)):
        entry = classify_component(
            component=component, instance="x", current=current,
            observation="valid", target_product_version=target_product,
            target_build_id=build, target_precision=precision,
            reachable=True, protocol_compatible=True)
        assert entry["state"] == "update_available"
        assert entry["execution_compatible"] is True


# ------------------------------------------------------- descriptor decoupling

def test_malformed_and_future_release_stay_usable():
    malformed = _descriptor_payload()
    malformed["release"] = {"contract": 1, "product": "workspace-bridge",
                            "product_version": __version__,
                            "component": "bridge",
                            "component_version": __version__,
                            "build_id": "bad"}
    parsed = Descriptor.parse(malformed, expected_runtime="pi")
    assert parsed.release is None
    assert parsed.release_status == "invalid"
    assert parsed.supports("models")

    future = _descriptor_payload()
    future["release"] = {"contract": 999, "product": "workspace-bridge",
                         "product_version": __version__,
                         "component": "bridge",
                         "component_version": __version__,
                         "build_id": "sha256:" + "b" * 64}
    parsed_future = Descriptor.parse(future, expected_runtime="pi")
    assert parsed_future.release is None
    assert parsed_future.release_status == "unsupported"
    assert parsed_future.supports("models")


def test_protocol_major_and_core_mismatch_stay_incompatible():
    bad_major = _descriptor_payload()
    bad_major["protocol"] = {"major": 2}
    with pytest.raises(RuntimeUnsupported):
        Descriptor.parse(bad_major, expected_runtime="pi")
    missing_core = _descriptor_payload(features={"models": 1})
    with pytest.raises(RuntimeUnsupported):
        Descriptor.parse(missing_core, expected_runtime="pi")
    bad_core_version = _descriptor_payload()
    bad_core_version["features"] = {**dict.fromkeys(CORE_FEATURES, 1),
                                    "models": 2}
    # Core feature version !=1 is unsupported (incompatible).
    with pytest.raises(RuntimeUnsupported):
        Descriptor.parse(bad_core_version, expected_runtime="pi")


def test_missing_optional_feature_never_disables_descriptor():
    minimal = _descriptor_payload()
    parsed = Descriptor.parse(minimal, expected_runtime="pi")
    assert parsed.supports("models")
    # Optional features gate only their own operation.
    assert not parsed.supports("securityRebind")
    assert not parsed.supports("steering")
    assert not parsed.supports("events")


# ------------------------------------------------------------- node protocol

def test_node_status_validator_requires_ok_and_v1():
    from workspace_bridge.node_client import validate_node_status
    good = {"status": "ok", "protocol": 1, "release": None}
    assert validate_node_status(good) is good
    # Release skew never affects reachability validation.
    skewed = {"status": "ok", "protocol": 1,
              "release": {"contract": 1, "product": "workspace-bridge",
                          "product_version": "9.9.9", "component": "node",
                          "component_version": "9.9.9",
                          "build_id": "sha256:" + "a" * 64}}
    assert validate_node_status(skewed) is skewed
    malformed_release = {"status": "ok", "protocol": 1,
                         "release": {"contract": 1, "bad": True}}
    assert validate_node_status(malformed_release) is malformed_release
    for bad in ({"status": "ok", "protocol": 2},
                {"status": "ok", "protocol": 0},
                {"status": "error", "protocol": 1},
                {"protocol": 1}, {"status": "ok"}, {}, [], None,
                {"status": "ok", "protocol": True}):
        with pytest.raises(BridgeError) as exc:
            validate_node_status(bad)
        assert exc.value.code == "node_protocol_error"


# ---------------------------------------------------------------- live service

class _FakeDescriptorAdapter:
    def __init__(self, runtime_type, release=None, release_status=None,
                 features=None, instance="native-test"):
        self.runtime_type = runtime_type
        self._release = release
        self._status = release_status or ("valid" if release else "missing")
        self.features = dict.fromkeys(CORE_FEATURES, 1) if features is None else features
        self.instance = instance
        self.model_rows = [{"selector": "model-a", "reasoningOptions": ["low"]}]
        self.profile_rows = [{"id": "reviewed", "revision": "rev-1",
                              "available": True}]

    def descriptor(self):
        return Descriptor(self.runtime_type, self.runtime_type, "0.1.0",
                          "test", self.instance, dict(self.features),
                          release=self._release,
                          release_status=self._status)

    def profile_catalog(self, _ws=None, _dir=None, *, fresh=False):
        return {"profiles": list(self.profile_rows)}

    def models(self, workspace_id: str):
        return list(self.model_rows)


@pytest.fixture
def staged_env(tmp_path):
    parent = tmp_path / "projects"
    parent.mkdir()
    state = tmp_path / "state"
    config = initialize(state, 8765, 8766)
    service = Service(state, config, run_coordinator_background=False)
    node, node_record = attach_test_node(service, tmp_path / "node-state",
                                         parent)
    yield {"service": service, "parent": parent, "node": node,
           "node_id": node_record["id"], "tmp": tmp_path}
    service.close()
    node["stop"]()


def _install_fake(staged_env, runtime_type, release=None,
                  release_status=None, features=None):
    service = staged_env["service"]
    row = service.adapter_registry.create({
        "node_id": staged_env["node_id"], "name": f"Test {runtime_type}",
        "runtime_type": runtime_type,
        "base_url": "http://127.0.0.1:9", "token": "secret-token",
    })
    fake = _FakeDescriptorAdapter(runtime_type, release, release_status,
                                  features)
    original = service.adapter_registry.client
    service.adapter_registry.client = lambda adapter_id, **kw: (
        fake if adapter_id == row["id"] else original(adapter_id, **kw))
    return row["id"], fake, original


def _ready_workspace(staged_env, adapter_id):
    service = staged_env["service"]
    root = staged_env["parent"] / "alpha"
    root.mkdir(exist_ok=True)
    ws_id = service.add_workspace("Alpha", str(root), [])["workspace"]["id"]
    service.manage_workspace(ws_id, "enable")
    if not service.bridge_status()["configured"]:
        service.manage_bridge("rotate_token")
    ws = service.workspace(ws_id)
    service.run_coordinator.set_model_policy(adapter_id, ["model-a"],
                                             "model-a", ws)
    service.set_workspace_route(ws, adapter_id, True, "reviewed")
    return service.workspace(ws_id)


def test_live_exact_current_is_current(staged_env):
    bridge = bridge_release()
    codex = {"contract": 1, "product": "workspace-bridge",
             "product_version": bridge["product_version"],
             "component": "codex-host-adapter",
             "component_version": "0.1.0", "build_id": bridge["build_id"]}
    adapter_id, _, _ = _install_fake(staged_env, "codex", codex, "valid")
    _ready_workspace(staged_env, adapter_id)
    status = rollout_live(staged_env["service"])
    assert status["schema_version"] == 1
    assert status["status"] == "ok"
    assert status["target"]["product_version"] == bridge["product_version"]
    by_id = {e["instance"]: e for e in status["adapters"]}
    assert by_id[adapter_id]["state"] == "current"
    assert by_id[adapter_id]["execution_compatible"] is True
    node_entry = next(e for e in status["nodes"]
                      if e["instance"] == staged_env["node_id"])
    # Live test Node runs the same package, so it matches the Bridge core.
    assert node_entry["state"] == "current"


def test_live_missing_identity_is_unsupported_build_but_runnable(staged_env):
    adapter_id, _, _ = _install_fake(staged_env, "pi", None, "missing")
    ws = _ready_workspace(staged_env, adapter_id)
    status = rollout_live(staged_env["service"])
    by_id = {e["instance"]: e for e in status["adapters"]}
    assert by_id[adapter_id]["state"] == "unsupported_build"
    assert by_id[adapter_id]["reason"] == "missing-release"
    assert by_id[adapter_id]["execution_compatible"] is True
    report = staged_env["service"].diagnostic_report()
    route = next(r for r in report["runnable_routes"]
                 if r["adapter_id"] == adapter_id)
    assert route["ready"] is True


def test_live_invalid_release_is_unsupported_build(staged_env):
    adapter_id, _, _ = _install_fake(staged_env, "codex", None, "invalid")
    _ready_workspace(staged_env, adapter_id)
    status = rollout_live(staged_env["service"])
    by_id = {e["instance"]: e for e in status["adapters"]}
    assert by_id[adapter_id]["state"] == "unsupported_build"
    assert by_id[adapter_id]["reason"] == "release-invalid"
    assert by_id[adapter_id]["execution_compatible"] is True


def test_live_unsupported_release_is_unsupported_build(staged_env):
    adapter_id, _, _ = _install_fake(staged_env, "codex", None, "unsupported")
    _ready_workspace(staged_env, adapter_id)
    status = rollout_live(staged_env["service"])
    by_id = {e["instance"]: e for e in status["adapters"]}
    assert by_id[adapter_id]["state"] == "unsupported_build"
    assert by_id[adapter_id]["reason"] == "release-unsupported"


def test_live_protocol_mismatch_is_incompatible(staged_env):
    bad_features = {"models": 1}  # missing core features
    adapter_id, _, _ = _install_fake(staged_env, "codex", None, "missing",
                                     features=bad_features)
    # Fake bypasses Node validation; exercise the pure classifier for the
    # live incompatible path via a direct descriptor parse failure.
    with pytest.raises(RuntimeUnsupported):
        Descriptor.parse({"protocol": {"major": 1},
                          "runtime": {"id": "codex",
                                      "instanceId": "x"},
                          "features": bad_features},
                         expected_runtime="codex")
    # Live adapter with missing core would be incompatible if observed via
    # HttpRuntimeAdapter; the fake here still returns a descriptor, so
    # assert the classifier maps an incompatible observation correctly.
    entry = classify_component(
        component="codex-host-adapter", instance=adapter_id, current=None,
        observation="unobserved",
        target_product_version=bridge_release()["product_version"],
        target_build_id=bridge_release()["build_id"],
        target_precision="exact", reachable=True,
        protocol_compatible=False, protocol_reason="core-feature-mismatch",
        runtime_type="codex")
    assert entry["state"] == "incompatible"
    assert entry["execution_compatible"] is False


def test_live_node_protocol_mismatch_is_incompatible(staged_env):
    service = staged_env["service"]
    original = service.node_registry.client

    class _BadNode:
        def status(self):
            raise BridgeError("Node protocol is incompatible",
                              "node_protocol_error")

    service.node_registry.client = lambda *a, **k: _BadNode()
    try:
        status = rollout_live(service)
        node_entry = next(e for e in status["nodes"]
                          if e["instance"] == staged_env["node_id"])
        assert node_entry["state"] == "incompatible"
        assert node_entry["execution_compatible"] is False
        assert node_entry["reason"] == "node-protocol-mismatch"
    finally:
        service.node_registry.client = original


def test_build_skew_is_update_available_and_runnable(staged_env):
    bridge = bridge_release()
    skewed = {"contract": 1, "product": "workspace-bridge",
              "product_version": bridge["product_version"],
              "component": "codex-host-adapter", "component_version": "0.1.0",
              "build_id": "sha256:" + "c" * 64}
    assert skewed["build_id"] != bridge["build_id"]
    adapter_id, _, _ = _install_fake(staged_env, "codex", skewed, "valid")
    _ready_workspace(staged_env, adapter_id)
    status = rollout_live(staged_env["service"])
    by_id = {e["instance"]: e for e in status["adapters"]}
    assert by_id[adapter_id]["state"] == "update_available"
    report = staged_env["service"].diagnostic_report()
    route = next(r for r in report["runnable_routes"]
                 if r["adapter_id"] == adapter_id)
    assert route["ready"] is True


def test_higher_version_is_target_mismatch_not_downgrade(staged_env):
    bridge = bridge_release()
    newer = {"contract": 1, "product": "workspace-bridge",
             "product_version": "9.9.9",
             "component": "codex-host-adapter", "component_version": "0.1.0",
             "build_id": bridge["build_id"]}
    adapter_id, _, _ = _install_fake(staged_env, "codex", newer, "valid")
    _ready_workspace(staged_env, adapter_id)
    status = rollout_live(staged_env["service"])
    by_id = {e["instance"]: e for e in status["adapters"]}
    assert by_id[adapter_id]["state"] == "target_mismatch"
    assert by_id[adapter_id]["execution_compatible"] is True


def test_run_admission_not_blocked_by_skew(staged_env):
    bridge = bridge_release()
    skewed = {"contract": 1, "product": "workspace-bridge",
              "product_version": "0.0.0",
              "component": "codex-host-adapter", "component_version": "0.1.0",
              "build_id": bridge["build_id"]}
    adapter_id, fake, original = _install_fake(staged_env, "codex", skewed,
                                               "valid")
    ws = _ready_workspace(staged_env, adapter_id)
    service = staged_env["service"]
    # Model/profile discovery works despite product skew.
    assert service.run_coordinator.models(ws, adapter_id)["models"]
    assert service.run_coordinator.profile_catalog(adapter_id, ws)["profiles"]
    # Route readiness is not blocked by release skew.
    policy = service.workspace_route_policy(ws)
    assert policy["routes"][adapter_id]["ready"] is True
    # Diagnostics never blocks routes on release skew.
    report = service.diagnostic_report()
    route = next(r for r in report["runnable_routes"]
                 if r["adapter_id"] == adapter_id)
    assert route["ready"] is True
    assert not any(c.startswith("release.") for c in route["blockers"])
    service.adapter_registry.client = original



def test_api_system_versions_is_read_only(staged_env):
    service = staged_env["service"]
    bridge = bridge_release()
    codex = {"contract": 1, "product": "workspace-bridge",
             "product_version": bridge["product_version"],
             "component": "codex-host-adapter",
             "component_version": "0.1.0", "build_id": bridge["build_id"]}
    adapter_id, _, _ = _install_fake(staged_env, "codex", codex, "valid")
    _ready_workspace(staged_env, adapter_id)
    db_path = staged_env["tmp"] / "state" / "bridge.sqlite3"
    before = db_path.read_bytes()
    app = make_admin(service, staged_env["service"].config.get(
        "admin_token_hash", "x"))
    # Use the real admin token hash from config; fall back to state file.
    import asyncio

    async def _call():
        token = (staged_env["tmp"] / "state" / "admin-token").read_text().strip()
        async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://127.0.0.1:8766",
                headers={"Authorization": "Bearer " + token}) as client:
            response = await client.get("/api/system/versions")
            assert response.status_code == 200
            payload = response.json()
            assert payload["schema_version"] == 1
            assert payload["status"] == "ok"
            assert payload["target"]["product_version"] == bridge["product_version"]
            assert "bridge" in payload and "manager" in payload
            assert isinstance(payload["nodes"], list)
            assert isinstance(payload["adapters"], list)
            # No update/apply affordance in the read-only payload.
            text = json.dumps(payload)
            assert "token" not in text.lower()
            assert "http://" not in text.lower()
            # POST is not allowed; read-only only.
            denied = await client.post("/api/system/versions")
            assert denied.status_code == 405
            return payload

    asyncio.run(_call())
    after = db_path.read_bytes()
    assert before == after


def test_descriptor_failure_uses_structured_semantics_not_message_text(staged_env):
    # A release-worded RuntimeUnsupported is still structured
    # incompatibility; compatibility is never inferred from message text.
    from workspace_bridge.runtime import RuntimeUnsupported as _RU
    service = staged_env["service"]
    row = service.adapter_registry.create({
        "node_id": staged_env["node_id"], "name": "Structured codex",
        "runtime_type": "codex",
        "base_url": "http://127.0.0.1:9", "token": "secret-token",
    })

    class _Raising:
        def descriptor(self):
            raise _RU("Runtime release contract is unsupported")

    original = service.adapter_registry.client
    service.adapter_registry.client = lambda adapter_id, **kw: (
        _Raising() if adapter_id == row["id"] else original(adapter_id, **kw))
    try:
        status = rollout_live(service)
        by_id = {e["instance"]: e for e in status["adapters"]}
        entry = by_id[row["id"]]
        assert entry["state"] == "incompatible"
        assert entry["execution_compatible"] is False
    finally:
        service.adapter_registry.client = original


def test_modern_degraded_descriptor_is_unsupported_build():
    # A successfully parsed Descriptor proves protocol compatibility, so
    # degraded release metadata maps to unsupported but runnable.
    bridge = bridge_release()
    for release_status, reason in (("invalid", "release-invalid"),
                                   ("unsupported", "release-unsupported")):
        bad = dict(_descriptor_payload(runtime="codex"))
        bad["release"] = ({"contract": 1, "product": "workspace-bridge",
                             "product_version": __version__,
                             "component": "codex-host-adapter",
                             "component_version": "0.1.0",
                             "build_id": "bad"} if release_status == "invalid"
                            else {"contract": 999, "product": "workspace-bridge",
                                  "product_version": __version__,
                                  "component": "codex-host-adapter",
                                  "component_version": "0.1.0",
                                  "build_id": "sha256:" + "b" * 64})
        parsed = Descriptor.parse(bad, expected_runtime="codex")
        assert parsed.release is None
        assert parsed.release_status == release_status
        status = build_staged_status(
            bridge_current=bridge, manager_current=None,
            node_observations={},
            adapter_observations={"adapter_000000000000000000000001": {
                "current": parsed.release, "observation": parsed.release_status,
                "reachable": True, "protocol_compatible": True,
                "runtime_type": "codex", "instance_id": parsed.instance_id}})
        entry = status["adapters"][0]
        assert entry["state"] == "unsupported_build"
        assert entry["execution_compatible"] is True
        assert entry["reason"] == reason


def test_missing_release_identity_does_not_block_protocol_execution(staged_env):
    adapter_id, _, original = _install_fake(staged_env, "pi", None, "missing")
    try:
        ws = _ready_workspace(staged_env, adapter_id)
        service = staged_env["service"]
        status = rollout_live(service)
        by_id = {e["instance"]: e for e in status["adapters"]}
        assert by_id[adapter_id]["state"] == "unsupported_build"
        assert by_id[adapter_id]["execution_compatible"] is True
        assert service.run_coordinator.models(ws, adapter_id)["models"]
        assert service.run_coordinator.profile_catalog(adapter_id, ws)["profiles"]
        assert service.workspace_route_policy(ws)["routes"][adapter_id]["ready"] is True
    finally:
        service.adapter_registry.client = original


def test_structured_descriptor_failure_blocks_start(staged_env):
    from workspace_bridge.runtime import RuntimeUnsupported as _RU
    service = staged_env["service"]
    row = service.adapter_registry.create({
        "node_id": staged_env["node_id"], "name": "Failing codex",
        "runtime_type": "codex",
        "base_url": "http://127.0.0.1:9", "token": "secret-token",
    })

    class _Failing:
        def descriptor(self):
            raise _RU("Runtime protocol major version is unsupported")

        def models(self, workspace_id: str):
            return [{"selector": "model-a", "reasoningOptions": ["low"]}]

        def profile_catalog(self, _ws=None, _dir=None, *, fresh=False):
            return {"profiles": [{"id": "reviewed", "revision": "rev-1",
                                     "available": True}]}

    original = service.adapter_registry.client
    service.adapter_registry.client = lambda adapter_id, **kw: (
        _Failing() if adapter_id == row["id"] else original(adapter_id, **kw))
    try:
        ws = _ready_workspace(staged_env, row["id"])
        status = rollout_live(service)
        by_id = {e["instance"]: e for e in status["adapters"]}
        assert by_id[row["id"]]["state"] == "incompatible"
        assert by_id[row["id"]]["execution_compatible"] is False
        job = service.prepare_handoff(ws, {
            "request_id": "structured-fail-1", "title": "Structured fail",
            "goal": "Prove structured failure stays blocked.",
            "plan": "Attempt a run on the incompatible adapter.",
            "acceptance": "Start fails closed.",
            "constraints": "No commits.",
            "context": "No context.",
            "context_hashes": {},
        })
        with pytest.raises(BridgeError):
            service.start_agent_run(ws, row["id"], job["id"], "req-structured-1")
    finally:
        service.adapter_registry.client = original


def test_true_protocol_mismatch_stays_incompatible(staged_env):
    entry = classify_component(
        component="codex-host-adapter", instance="a1", current=None,
        observation="unobserved",
        target_product_version=bridge_release()["product_version"],
        target_build_id=bridge_release()["build_id"],
        target_precision="exact", reachable=True,
        protocol_compatible=False, protocol_reason="core-feature-mismatch",
        runtime_type="codex")
    assert entry["state"] == "incompatible"
    assert entry["execution_compatible"] is False
    assert entry["reason"] == "core-feature-mismatch"
