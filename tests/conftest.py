from pathlib import Path
import os
import pytest
from workspace_bridge.cli import initialize
from workspace_bridge.node_api import InProcessTransport
from workspace_bridge.node_service import NodeService
from workspace_bridge.service import Service


@pytest.fixture(autouse=True)
def _isolated_bridge_environment(monkeypatch):
    """Scrub ambient WB_* config so tests never inherit the developer shell.

    Service construction reads os.environ (webhook URL, admin hosts, log
    level). Without this, e.g. an exported WB_DISCORD_WEBHOOK_URL leaks a
    Discord channel into test services and breaks channel assertions.
    Tests that need a variable set it explicitly via monkeypatch.
    """
    for name in list(os.environ):
        if name.startswith("WB_"):
            monkeypatch.delenv(name, raising=False)


def start_test_node(state: Path, parent: Path):
    """Build a real Node service that the Bridge calls in process."""
    config = {"allowed_roots": [str(parent)], "cursor_secret": "c" * 64}
    node_service = NodeService(state, config)
    return {"service": node_service, "transport": InProcessTransport(node_service),
            "stop": node_service.close}


def register_test_node(service: Service, name: str, *, enabled: bool = True) -> dict:
    """A paired Node row; tests supply its transport through ``_node_transport``."""
    import secrets
    from datetime import datetime, timezone
    node_id, now = "node_" + secrets.token_hex(12), datetime.now(timezone.utc).isoformat()
    with service.lock, service.db:
        service.db.execute(
            "INSERT INTO nodes(id,name,enabled,revision,created,updated,transport) "
            "VALUES(?,?,?,?,?,?,'paired')",
            (node_id, name, int(enabled), "rev_" + secrets.token_hex(16), now, now))
    return service.node_registry.public(service.node_registry.get(node_id))


def attach_test_node(service: Service, state: Path, parent: Path,
                     name: str = "Local Node"):
    node = start_test_node(state, parent)
    service._node_transport = {}
    record = register_test_node(service, name)
    service._node_transport[record["id"]] = node["transport"]
    service._node_transport_services = {record["id"]: node["service"]}
    service.node_registry.refresh_adapters(record["id"])
    return node, record


def plugin_payload(payload: dict) -> dict:
    """Map a test adapter description onto the built-in plugin of its runtime type.

    Tests intercept the Bridge's adapter client, so the plugin process is
    only started by tests that exercise the Node plugin path itself.
    """
    if "plugin" in payload:
        return payload
    return {key: value for key, value in {
        "name": payload.get("name"), "plugin": payload.get("runtime_type", "pi"),
        "enabled": payload.get("enabled")}.items() if value is not None}


def create_test_adapter(service: Service, node_id: str, payload: dict,
                        adapter_id: str | None = None):
    row = service.adapter_registry.create({**plugin_payload(payload), "node_id": node_id})
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


def patch_node_runtime(monkeypatch, factory):
    """Make every NodeService answer plugin adapters with ``factory``'s client.

    ``factory`` receives ``(adapter_id, runtime_type, timeout=..., row=...)``;
    the Node still checks the adapter row and revision before constructing it.
    """
    from workspace_bridge.node_service import NodeService

    def plugin_client(self, row, *, timeout=30):
        return factory(row["id"], row["runtime_type"], timeout=timeout, row=row)

    monkeypatch.setattr(NodeService, "plugin_client", plugin_client)
