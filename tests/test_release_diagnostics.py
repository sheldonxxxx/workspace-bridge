"""Diagnostics release-compatibility: severities without route blockers."""
from __future__ import annotations

import pytest

from workspace_bridge.release import bridge_release
from workspace_bridge.wbrp import CORE_FEATURES, Descriptor
from conftest import attach_test_node
from workspace_bridge.cli import initialize
from workspace_bridge.service import Service


class ReleaseAdapter:
    def __init__(self, runtime_type, release):
        self.runtime_type = runtime_type
        self._release = release
        self.model_rows = [{"selector": "model-a", "reasoningOptions": ["low"]}]
        self.profile_rows = [{"id": "reviewed", "revision": "rev-1", "available": True}]
        self.features = dict.fromkeys(CORE_FEATURES, 1)

    def descriptor(self):
        return Descriptor(self.runtime_type, self.runtime_type, "1.0.0", "test",
                          f"native-{self.runtime_type}", self.features,
                          release=self._release)

    def profile_catalog(self, _workspace_id=None, _directory=None, *, fresh=False):
        return {"profiles": list(self.profile_rows)}

    def models(self, workspace_id: str):
        return list(self.model_rows)


@pytest.fixture
def release_env(tmp_path):
    parent = tmp_path / "projects"
    parent.mkdir()
    state = tmp_path / "state"
    config = initialize(state, 8765, 8766)
    service = Service(state, config, run_coordinator_background=False)
    node, node_record = attach_test_node(service, tmp_path / "node-state", parent)
    yield {"service": service, "parent": parent, "node": node,
           "node_id": node_record["id"], "tmp": tmp_path}
    service.close()
    node["stop"]()


def _ready_workspace(env, adapter_id):
    service = env["service"]
    root = env["parent"] / "alpha"
    root.mkdir(exist_ok=True)
    ws_id = service.add_workspace("Alpha", str(root), [])["workspace"]["id"]
    service.manage_workspace(ws_id, "enable")
    service.manage_workspace(ws_id, "set_agent_enabled", agent_enabled=True)
    if not service.bridge_status()["configured"]:
        service.manage_bridge("rotate_token")
    ws = service.workspace(ws_id)
    client_release = None
    # Find the fake adapter's release via its descriptor for profile setup.
    service.run_coordinator.set_model_policy(adapter_id, ["model-a"], "model-a", ws)
    service.set_workspace_route(ws, adapter_id, True, "reviewed")
    return service.workspace(ws_id)


def _install_adapter(env, runtime_type, release):
    service = env["service"]
    row = service.adapter_registry.create({
        "node_id": env["node_id"], "name": f"Test {runtime_type}",
        "runtime_type": runtime_type,
        "base_url": "http://127.0.0.1:9", "token": "secret-token",
    })
    fake = ReleaseAdapter(runtime_type, release)
    original = service.adapter_registry.client
    service.adapter_registry.client = lambda adapter_id, **kwargs: (
        fake if adapter_id == row["id"] else original(adapter_id, **kwargs))
    return row["id"], fake, original


def test_matching_release_is_pass_and_never_blocks(release_env):
    env = release_env
    bridge = bridge_release()
    codex_release = {"contract": 1, "product": "workspace-bridge",
                     "product_version": bridge["product_version"],
                     "component": "codex-host-adapter",
                     "component_version": "1.0.0", "build_id": bridge["build_id"]}
    adapter_id, _, _ = _install_adapter(env, "codex", codex_release)
    ws = _ready_workspace(env, adapter_id)
    report = env["service"].diagnostic_report()
    assert report["release"]["bridge"]["build_id"] == bridge["build_id"]
    assert report["release"]["adapters"][adapter_id] == codex_release
    codes = {(c["code"], c["status"]) for c in report["checks"] if c["code"].startswith("release.")}
    assert ("release.bridge_identity", "pass") in codes
    assert ("release.adapter_identity", "pass") in {
        (c["code"], c["status"]) for c in report["checks"]
        if c.get("adapter_id") == adapter_id}
    route = next(r for r in report["runnable_routes"] if r["adapter_id"] == adapter_id)
    assert route["ready"] is True
    assert not any(c.startswith("release.") for c in route["blockers"])


def test_missing_adapter_release_is_warning_not_blocker(release_env):
    env = release_env
    adapter_id, _, _ = _install_adapter(env, "pi", None)
    ws = _ready_workspace(env, adapter_id)
    report = env["service"].diagnostic_report()
    check = next(c for c in report["checks"]
                 if c["code"] == "release.adapter_identity" and c["adapter_id"] == adapter_id)
    assert check["status"] == "warning"
    route = next(r for r in report["runnable_routes"] if r["adapter_id"] == adapter_id)
    assert route["ready"] is True
    assert "release.adapter_identity" not in route["blockers"]


def test_codex_build_skew_is_warning_not_blocker(release_env):
    env = release_env
    bridge = bridge_release()
    skewed = {"contract": 1, "product": "workspace-bridge",
              "product_version": bridge["product_version"],
              "component": "codex-host-adapter", "component_version": "1.0.0",
              "build_id": "sha256:" + "c" * 64}
    assert skewed["build_id"] != bridge["build_id"]
    adapter_id, _, _ = _install_adapter(env, "codex", skewed)
    _ready_workspace(env, adapter_id)
    report = env["service"].diagnostic_report()
    check = next(c for c in report["checks"]
                 if c["code"] == "release.adapter_build_skew" and c["adapter_id"] == adapter_id)
    assert check["status"] == "warning"
    route = next(r for r in report["runnable_routes"] if r["adapter_id"] == adapter_id)
    assert route["ready"] is True
    assert "release.adapter_build_skew" not in route["blockers"]


def test_pi_build_id_is_not_compared_to_python_core(release_env):
    env = release_env
    bridge = bridge_release()
    pi_release = {"contract": 1, "product": "workspace-bridge",
                  "product_version": bridge["product_version"],
                  "component": "pi-host-adapter", "component_version": "0.4.0",
                  "build_id": "sha256:" + "d" * 64}
    assert pi_release["build_id"] != bridge["build_id"]
    adapter_id, _, _ = _install_adapter(env, "pi", pi_release)
    _ready_workspace(env, adapter_id)
    report = env["service"].diagnostic_report()
    assert not [c for c in report["checks"]
                if c["code"] == "release.adapter_build_skew" and c["adapter_id"] == adapter_id]
    check = next(c for c in report["checks"]
                 if c["code"] == "release.adapter_identity" and c["adapter_id"] == adapter_id)
    assert check["status"] == "pass"


def test_product_skew_is_warning_not_blocker(release_env):
    env = release_env
    bridge = bridge_release()
    skewed = {"contract": 1, "product": "workspace-bridge",
              "product_version": "0.0.0", "component": "codex-host-adapter",
              "component_version": "1.0.0", "build_id": bridge["build_id"]}
    adapter_id, _, _ = _install_adapter(env, "codex", skewed)
    _ready_workspace(env, adapter_id)
    report = env["service"].diagnostic_report()
    check = next(c for c in report["checks"]
                 if c["code"] == "release.adapter_product_skew" and c["adapter_id"] == adapter_id)
    assert check["status"] == "warning"
    route = next(r for r in report["runnable_routes"] if r["adapter_id"] == adapter_id)
    assert route["ready"] is True


def test_offline_release_is_unknown(release_env):
    env = release_env
    adapter_id, _, _ = _install_adapter(env, "pi", bridge_release())
    report = env["service"].diagnostic_report(offline=True)
    unknowns = [c for c in report["checks"] if c["code"].startswith("release.")]
    assert unknowns
    assert all(c["status"] in {"pass", "unknown"} for c in unknowns)
    # Offline adapter identity is unobserved, never a blocker.
    for route in report["runnable_routes"]:
        assert not any(c.startswith("release.") for c in route["blockers"])
