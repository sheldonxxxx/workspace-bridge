"""Negative surface: removed updater/backup/admission code is gone.

Proves the manual-update simplification: no remote/automatic update
planning, deployment installer probing, remote backup, admission locking,
release bundles or Node HTTP API remain in product runtime.
"""
from __future__ import annotations
from admin_helpers import admin_cookie

import importlib.metadata
import importlib.util
import sys
from pathlib import Path

import pytest


def test_removed_bridge_update_endpoints_absent(env):
    import asyncio
    import httpx
    from workspace_bridge.api import make_admin
    service = env["service"]
    app = make_admin(service)

    async def _call():
        token = admin_cookie(app)
        async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://127.0.0.1:8766",
                headers={"Cookie": token}) as client:
            for method, path in (
                    ("GET", "/api/deployment/plan"),
                    ("POST", "/api/deployment/plan"),
                    ("GET", "/api/deployment/status"),
                    ("POST", "/api/deployment/status")):
                response = await client.request(method, path, json={})
                assert response.status_code == 404, (method, path)
            # Versions come from /api/status; Nodes join only by pairing.
            assert (await client.get("/api/system/versions")).status_code == 404
            assert (await client.post("/api/nodes", json={})).status_code == 405
            status = (await client.get("/api/status")).json()
            assert "release" not in status and "manager_release" not in status
    asyncio.run(_call())


def test_removed_node_deployment_endpoints_absent(env):
    node = env["node"]
    transport = node["transport"]
    assert transport.request("GET", "/v1/status").status_code == 200
    assert transport.request("GET", "/v1/deployment/status").status_code == 404
    assert transport.request("POST", "/v1/deployment/backup", b"{}").status_code == 404
    assert transport.request("POST", "/v1/deployment/exec", b"{}").status_code == 404
    from workspace_bridge import node_api
    assert not hasattr(node_api, "make_node_api")


def test_removed_cli_deploy_namespace_absent(capsys):
    from workspace_bridge.cli import main as cli_main
    for argv in (["deploy", "plan", "--target", "x"],
                 ["deploy", "backup", "--target", "x"],
                 ["deploy", "select-plan", "--json"],
                 ["deploy", "build", "--output", "x"],
                 ["deploy", "validate-bundle", "--bundle", "x"],
                 ["release", "build", "--output", "x"],
                 ["release", "validate"]):
        with pytest.raises(SystemExit) as exc:
            cli_main(["--state", "/tmp/wb-no-such-state", *argv])
        assert exc.value.code == 2, argv
        capsys.readouterr()
    for name in ("workspace_bridge.release", "workspace_bridge.release_bundle",
                 "workspace_bridge.release_manifest", "workspace_bridge.staged_rollout",
                 "workspace_bridge.pi_runtime_artifact", "workspace_bridge.docker_entrypoint"):
        assert importlib.util.find_spec(name) is None, name


def test_no_deployment_admission_dependency(tmp_path):
    from workspace_bridge.cli import initialize
    from workspace_bridge.service import Service
    for name in ("workspace_bridge.selective_update",
                 "workspace_bridge.backup",
                 "workspace_bridge.deployment",
                 "workspace_bridge.deployment_admission"):
        assert importlib.util.find_spec(name) is None, name
        assert name not in sys.modules, name
    source = Path("workspace_bridge/service.py").read_text()
    assert "deployment_admission" not in source
    assert "shared_admission" not in source
    assert "deployment_busy" not in source
    parent = tmp_path / "projects"
    parent.mkdir()
    state = tmp_path / "state"
    config = initialize(state, 8765, 8766)
    service = Service(state, config, run_coordinator_background=False)
    try:
        assert not (state / "deployment-admission.lock").exists()
        assert not list(state.glob("deployment-admission*"))
    finally:
        service.close()


def test_console_entry_points_exposed():
    points = {entry.name: entry.value for entry in
              importlib.metadata.entry_points(group="console_scripts")}
    assert points.get("workspace-bridge") == "workspace_bridge.cli:main"
    # Existing Node entry retained for service internals/compatibility.
    assert points.get("workspace-bridge-node") == \
        "workspace_bridge.node_cli:main"


def test_cli_version_and_nested_node_help(capsys):
    from workspace_bridge import __version__
    from workspace_bridge.cli import main as cli_main
    with pytest.raises(SystemExit) as exc:
        cli_main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == __version__
    with pytest.raises(SystemExit) as exc:
        cli_main(["node", "--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "Node state directory" in out


def test_nested_node_init_writes_no_token(tmp_path, capsys):
    from workspace_bridge.cli import main as cli_main
    root = tmp_path / "projects" / "alpha"
    root.mkdir(parents=True)
    state = tmp_path / "nstate"
    assert cli_main(["node", "--state", str(state), "init",
                     "--allow-root", str(tmp_path / "projects")]) is None
    assert (state / "node-config.json").exists()
    assert not (state / "node-token").exists()
    capsys.readouterr()
    with pytest.raises(SystemExit) as exc:
        cli_main(["node", "--state", str(state), "show-token"])
    assert exc.value.code == 2
