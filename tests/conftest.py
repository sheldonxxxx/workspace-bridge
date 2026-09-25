from pathlib import Path
import pytest
from starlette.testclient import TestClient
from workspace_bridge.cli import initialize
from workspace_bridge.node_api import make_node_api
from workspace_bridge.node_service import NodeService
from workspace_bridge.security import digest
from workspace_bridge.service import Service


def start_test_node(state: Path, parent: Path):
    """Build the real private Node API with an in-process HTTP transport."""
    token = "node-test-token"
    config = {"allowed_roots": [str(parent)],
              "node_token_hash": digest(token.encode()), "host": "127.0.0.1"}
    node_service = NodeService(state, config)
    transport = TestClient(make_node_api(node_service, config["node_token_hash"]))

    def stop():
        transport.close()
        node_service.close()

    return {"url": "http://node-test.invalid", "token": token,
            "service": node_service, "transport": transport, "stop": stop}


def attach_test_node(service: Service, state: Path, parent: Path,
                     name: str = "Local Node"):
    node = start_test_node(state, parent)
    service._node_transport = {}
    record = service.node_registry.create({"name": name, "base_url": node["url"],
                                           "token": node["token"]})
    service._node_transport[record["id"]] = node["transport"]
    service._node_transport_services = {record["id"]: node["service"]}
    service.node_registry.refresh_adapters(record["id"])
    return node, record


def create_test_adapter(service: Service, node_id: str, payload: dict,
                        adapter_id: str | None = None):
    row = service.adapter_registry.create({**payload, "node_id": node_id})
    if adapter_id is None:
        return row
    node_service = service._node_transport_services[node_id]
    with node_service.lock, node_service.db:
        node_service.db.execute("UPDATE runtime_adapters SET id=? WHERE id=?",
                                (adapter_id, row["id"]))
    with service.lock, service.db:
        service.db.execute("DELETE FROM node_adapters WHERE adapter_id=?", (row["id"],))
    service.node_registry.refresh_adapters(node_id)
    return service.adapter_registry.get(adapter_id)

@pytest.fixture
def env(tmp_path):
    parent = tmp_path / "projects"; parent.mkdir()
    root = parent / "alpha"; root.mkdir()
    (root / "src").mkdir()
    (root / "src" / "main.py").write_text("def add(a, b):\n    return a + b\n")
    (root / "README.md").write_text("# Alpha\nSmall example project\n")
    state = tmp_path / "private-state"
    cfg = initialize(state, 8765, 8766)
    service = Service(state, cfg)
    node, node_record = attach_test_node(service, tmp_path / "node-state", parent)
    result = service.add_workspace("Alpha", str(root), [], node_record["id"])
    ws_id, token = result["workspace"]["id"], service.manage_bridge("rotate_token")["token"]
    service.manage_workspace(ws_id, "enable")
    yield {"service": service, "root": root, "parent": parent, "state": state,
           "config": cfg, "id": ws_id, "token": token, "tmp": tmp_path,
           "node": node, "node_id": node_record["id"]}
    service.close()
    node["stop"]()

@pytest.fixture
def payload():
    return {"request_id": "example-1", "title": "Improve add", "goal": "Improve arithmetic behavior",
        "plan": "Inspect src/main.py; make the smallest necessary change and update tests.",
        "acceptance": "Run the unit tests. Preserve pre-existing user edits.",
        "constraints": "Do not commit or push.", "context": "Small Python module.", "context_hashes": {}}
