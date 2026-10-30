"""Paired Nodes dial the Bridge gateway; the embedded local Node runs in-process."""
import asyncio
import gc
import json
import logging
import socket
import ssl
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn
from websockets.asyncio.client import connect

from admin_helpers import admin_cookie
from workspace_bridge import __version__, pairing
from workspace_bridge.api import make_admin
from workspace_bridge.cli import enable_local_node, initialize
from workspace_bridge.node_cli import initialize_node, load_node_config
from workspace_bridge.node_gateway import MAX_FRAME
from workspace_bridge.node_link import pair, run_link
from workspace_bridge.node_service import NodeService
from workspace_bridge.security import BridgeError
from workspace_bridge.service import Service


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _wait(predicate, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError("condition was not met in time")


class _Gateway:
    def __init__(self, service: Service, port: int):
        cert, key = pairing.ensure_gateway_certificate(service.state)
        self.server = uvicorn.Server(uvicorn.Config(
            service.node_gateway.app(), host="127.0.0.1", port=port,
            ssl_certfile=str(cert), ssl_keyfile=str(key), ws_max_size=MAX_FRAME,
            lifespan="off", log_level="warning"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        _wait(lambda: self.server.started)

    def stop(self):
        self.server.should_exit = True
        self.thread.join(10)


class _Link:
    """Run one Node's dial-out loop on its own event loop and thread."""

    def __init__(self, node_service, state: Path):
        self.loop = asyncio.new_event_loop()
        self.stop_event = None
        ready = threading.Event()

        async def main():
            self.stop_event = asyncio.Event()
            ready.set()
            await run_link(node_service, load_node_config(state), state,
                           stop=self.stop_event, max_backoff=0.2)

        self.thread = threading.Thread(target=self.loop.run_until_complete, args=(main(),),
                                       daemon=True)
        self.thread.start()
        ready.wait(5)

    def stop(self):
        self.loop.call_soon_threadsafe(self.stop_event.set)
        self.thread.join(10)


@pytest.fixture
def paired(tmp_path):
    parent = tmp_path / "projects"
    root = parent / "alpha"
    root.mkdir(parents=True)
    (root / "README.md").write_text("# Alpha\n")
    port = _free_port()
    bridge_state = tmp_path / "bridge-state"
    config = initialize(bridge_state, 8765, 8766, port)
    service = Service(bridge_state, config, run_coordinator_background=False)
    gateway = _Gateway(service, port)
    node_state = tmp_path / "node-state"
    initialize_node(node_state, [str(parent)])
    issued = service.create_node_pairing("Laptop")
    bridge = asyncio.run(pair(node_state, load_node_config(node_state), issued["pairing"]))
    node_service = NodeService(node_state, load_node_config(node_state))
    link = _Link(node_service, node_state)
    _wait(lambda: service.node_gateway.connected(bridge["node_id"]))
    env = {"service": service, "gateway": gateway, "link": link, "node_state": node_state,
           "node_service": node_service, "node_id": bridge["node_id"], "root": root,
           "parent": parent, "port": port, "issued": issued, "tmp": tmp_path}
    yield env
    env["link"].stop()
    gateway.stop()
    node_service.close()
    service.close()


def test_paired_node_serves_files_and_git_without_a_node_port(paired):
    service, node_id = paired["service"], paired["node_id"]
    node = service.node_registry.get(node_id)
    assert node["transport"] == "paired"
    assert node["revision"] == pairing.node_revision(node["public_key"])
    config = load_node_config(paired["node_state"])
    assert config["bridge"]["node_id"] == node_id
    assert (paired["node_state"] / pairing.NODE_KEY).stat().st_mode & 0o777 == 0o600
    ws_id = service.add_workspace("Alpha", str(paired["root"]), [], node_id)["workspace"]["id"]
    service.manage_workspace(ws_id, "enable")
    token = service.manage_bridge("rotate_token")["token"]
    read = service.call(ws_id, token, "read_file", {
        "path": "README.md", "start_line": 1, "max_lines": 10, "expected_sha256": None})
    assert read["lines"][0]["text"] == "# Alpha"
    listing = service.call(ws_id, token, "list_dir", {"path": "", "depth": 1})
    assert any(entry["path"] == "README.md" for entry in listing["entries"])
    assert service.call(ws_id, token, "git_status", {})["available"] is False
    public = next(item for item in service.node_registry.list_public()
                  if item["id"] == node_id)
    assert (public["transport"], public["health"]) == ("paired", "healthy")


def test_reconnect_keeps_revision_and_outage_fails_closed(paired):
    service, node_id = paired["service"], paired["node_id"]
    revision = service.node_registry.get(node_id)["revision"]
    paired["link"].stop()
    _wait(lambda: not service.node_gateway.connected(node_id))
    with pytest.raises(BridgeError) as outage:
        service.node_registry.client(node_id, timeout=2).status()
    assert outage.value.code == "node_unavailable"
    # The Manager's view comes from the cached connection, without a probe.
    health = lambda: next(item["health"] for item in service.node_registry.list_public()
                          if item["id"] == node_id)
    assert health() == "unavailable"
    paired["link"] = _Link(paired["node_service"], paired["node_state"])
    _wait(lambda: service.node_gateway.connected(node_id))
    assert service.node_registry.client(node_id, timeout=5).status()["status"] == "ok"
    assert service.node_registry.get(node_id)["revision"] == revision
    _wait(lambda: health() == "healthy")


def test_bridge_restart_is_a_clean_disconnect_and_reconnect(paired, caplog):
    service, node_id, link = paired["service"], paired["node_id"], paired["link"]
    unhandled = []
    link.loop.call_soon_threadsafe(
        link.loop.set_exception_handler, lambda _loop, context: unhandled.append(context))
    caplog.set_level(logging.INFO, logger="workspace_bridge.ops")
    paired["gateway"].stop()
    _wait(lambda: not service.node_gateway.connected(node_id))
    paired["gateway"] = _Gateway(service, paired["port"])
    _wait(lambda: service.node_gateway.connected(node_id))
    assert service.node_registry.client(node_id, timeout=5).status()["status"] == "ok"
    gc.collect()
    assert [str(item.get('message')) + ' ' + repr(item.get('exception')) for item in unhandled] == []
    assert "node_link_disconnected" in caplog.text


def test_disabling_a_paired_node_closes_its_socket_until_enabled(paired):
    service, node_id = paired["service"], paired["node_id"]
    service.node_registry.update(node_id, {"enabled": False})
    _wait(lambda: not service.node_gateway.connected(node_id))
    time.sleep(0.6)  # Several reconnect attempts at the test backoff.
    assert not service.node_gateway.connected(node_id)
    service.node_registry.update(node_id, {"enabled": True})
    _wait(lambda: service.node_gateway.connected(node_id))


def test_deleting_a_paired_node_closes_its_socket(paired):
    service, node_id = paired["service"], paired["node_id"]
    assert service.node_registry.delete(node_id) == {"deleted": node_id}
    _wait(lambda: not service.node_gateway.connected(node_id))
    time.sleep(0.6)
    assert not service.node_gateway.connected(node_id)


def test_the_embedded_local_node_cannot_be_deleted(tmp_path):
    projects = tmp_path / "projects"
    projects.mkdir()
    state = tmp_path / "bridge-state"
    config = initialize(state, 8765, 8766)
    Service(state, config, run_coordinator_background=False).close()
    node_id = enable_local_node(state, [str(projects)])["node_id"]
    service = Service(state, config, run_coordinator_background=False)
    try:
        with pytest.raises(BridgeError) as refused:
            service.node_registry.delete(node_id)
        assert refused.value.code == "conflict"
    finally:
        service.close()


def test_pairing_codes_are_single_use(paired):
    second_state = paired["tmp"] / "second-node"
    initialize_node(second_state, [str(paired["parent"])])
    with pytest.raises(BridgeError) as reused:
        asyncio.run(pair(second_state, load_node_config(second_state),
                         paired["issued"]["pairing"]))
    assert reused.value.code == "pairing_rejected"
    assert "bridge" not in load_node_config(second_state)


def test_expired_pairing_code_is_refused(paired):
    service = paired["service"]
    issued = service.create_node_pairing("Desktop")
    with service.lock, service.db:
        service.db.execute("UPDATE node_pairings SET expires='2000-01-01T00:00:00+00:00' "
                           "WHERE used IS NULL")
    state = paired["tmp"] / "late-node"
    initialize_node(state, [str(paired["parent"])])
    with pytest.raises(BridgeError) as expired:
        asyncio.run(pair(state, load_node_config(state), issued["pairing"]))
    assert expired.value.code == "pairing_rejected"


def test_wrong_gateway_certificate_is_refused_before_pairing(paired):
    service = paired["service"]
    issued = service.create_node_pairing("Spoofed")
    url, fingerprint, code = pairing.parse_pairing(issued["pairing"])
    forged = pairing.format_pairing(url, "0" * 64, code)
    state = paired["tmp"] / "spoofed-node"
    initialize_node(state, [str(paired["parent"])])
    with pytest.raises(BridgeError) as untrusted:
        asyncio.run(pair(state, load_node_config(state), forged))
    assert untrusted.value.code == "gateway_untrusted"
    with service.lock:
        unused = service.db.execute("SELECT used FROM node_pairings WHERE name='Spoofed'").fetchone()
    assert unused["used"] is None


async def _raw_hello(port: int, node_id: str, key, fingerprint: str, *, origin=None,
                     version: str | None = __version__) -> dict:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    headers = {"Origin": origin} if origin else None
    async with connect(f"wss://127.0.0.1:{port}{pairing.GATEWAY_PATH}", ssl=context,
                       additional_headers=headers) as websocket:
        challenge = json.loads(await websocket.recv())
        await websocket.send(json.dumps({
            "type": "hello", "node_id": node_id, "version": version,
            "signature": pairing.sign(key, "hello", challenge["nonce"], fingerprint, node_id)}))
        return json.loads(await websocket.recv())


def test_hello_with_a_different_key_is_refused(paired):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    fingerprint = paired["service"].node_gateway.fingerprint()
    answer = asyncio.run(_raw_hello(paired["port"], paired["node_id"],
                                    Ed25519PrivateKey.generate(), fingerprint))
    assert answer == {"type": "error", "code": "auth_failed"}
    real_key = pairing.load_or_create_node_key(paired["node_state"])
    replay = asyncio.run(_raw_hello(paired["port"], paired["node_id"], real_key, "f" * 64))
    assert replay == {"type": "error", "code": "auth_failed"}


def test_an_incompatible_node_version_is_refused(paired):
    key = pairing.load_or_create_node_key(paired["node_state"])
    fingerprint = paired["service"].node_gateway.fingerprint()
    for version in (None, "garbage", "99.0.0"):
        answer = asyncio.run(_raw_hello(paired["port"], paired["node_id"], key, fingerprint,
                                        version=version))
        assert answer == {"type": "error", "code": "version_incompatible"}
    assert pairing.compatible("1.4.2", "1.0.0") and pairing.compatible("0.3.1", "0.3.0")
    assert not pairing.compatible("0.2.9", "0.3.0") and not pairing.compatible("2.0.0", "1.9.0")


def test_browser_origin_is_refused(paired):
    key = pairing.load_or_create_node_key(paired["node_state"])
    with pytest.raises(Exception):
        asyncio.run(_raw_hello(paired["port"], paired["node_id"], key,
                               paired["service"].node_gateway.fingerprint(),
                               origin="https://evil.example"))


def test_repairing_rotates_key_and_revision(paired):
    service, node_id = paired["service"], paired["node_id"]
    old_revision = service.node_registry.get(node_id)["revision"]
    old_key = pairing.load_or_create_node_key(paired["node_state"])
    with pytest.raises(BridgeError):
        service.create_node_pairing("Laptop")
    issued = service.create_node_pairing("Laptop", replace=True)
    paired["link"].stop()
    bridge = asyncio.run(pair(paired["node_state"], load_node_config(paired["node_state"]),
                              issued["pairing"]))
    assert bridge["node_id"] == node_id
    assert service.node_registry.get(node_id)["revision"] != old_revision
    fingerprint = service.node_gateway.fingerprint()
    stale = asyncio.run(_raw_hello(paired["port"], node_id, old_key, fingerprint))
    assert stale == {"type": "error", "code": "auth_failed"}
    paired["link"] = _Link(paired["node_service"], paired["node_state"])
    _wait(lambda: service.node_gateway.connected(node_id))


def test_paired_node_rejects_url_and_token_edits(paired):
    service = paired["service"]
    with pytest.raises(BridgeError) as edit:
        service.node_registry.update(paired["node_id"], {"base_url": "http://elsewhere:1"})
    assert edit.value.code == "invalid_arguments"
    renamed = service.node_registry.update(paired["node_id"], {"name": "Work laptop"})
    assert renamed["name"] == "Work laptop"


@pytest.mark.asyncio
async def test_manager_pairing_endpoint_requires_admin(paired):
    app = make_admin(paired["service"])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://127.0.0.1:8766") as client:
        assert (await client.post("/api/nodes/pairings", json={"name": "Tablet"})).status_code == 401
        client.headers["Cookie"] = admin_cookie(app)
        created = await client.post("/api/nodes/pairings", json={"name": "Tablet"})
        assert created.status_code == 201, created.text
        assert created.json()["pairing"].startswith(pairing.PAIRING_PREFIX)
        assert (await client.post("/api/nodes/pairings", json={"name": "Tablet",
                                                               "extra": 1})).status_code == 400


def test_local_node_runs_inside_the_bridge(tmp_path):
    parent = tmp_path / "projects"
    root = parent / "alpha"
    root.mkdir(parents=True)
    (root / "README.md").write_text("# Local\n")
    state = tmp_path / "bridge-state"
    config = initialize(state, 8765, 8766)
    Service(state, config, run_coordinator_background=False).close()
    created = enable_local_node(state, [str(parent)])
    with pytest.raises(BridgeError):
        enable_local_node(state, [str(parent)], "Second")
    service = Service(state, config, run_coordinator_background=False)
    try:
        node = service.node_registry.get(created["node_id"])
        assert node["transport"] == "local"
        ws_id = service.add_workspace("Alpha", str(root), [], node["id"])["workspace"]["id"]
        service.manage_workspace(ws_id, "enable")
        token = service.manage_bridge("rotate_token")["token"]
        read = service.call(ws_id, token, "read_file", {
            "path": "README.md", "start_line": 1, "max_lines": 10, "expected_sha256": None})
        assert read["lines"][0]["text"] == "# Local"
    finally:
        service.close()


def test_a_0_3_local_node_gets_a_cursor_secret_on_start(tmp_path):
    parent = tmp_path / "projects"
    (parent / "alpha").mkdir(parents=True)
    state = tmp_path / "bridge-state"
    config = initialize(state, 8765, 8766)
    Service(state, config, run_coordinator_background=False).close()
    enable_local_node(state, [str(parent)])
    node_config = state / "local-node" / "node-config.json"
    legacy = json.loads(node_config.read_text())
    del legacy["cursor_secret"]
    legacy["node_token_hash"] = "a" * 64
    node_config.write_text(json.dumps(legacy))
    Service(state, config, run_coordinator_background=False).close()
    upgraded = json.loads(node_config.read_text())
    assert len(upgraded["cursor_secret"]) == 64
    assert node_config.stat().st_mode & 0o777 == 0o600
    assert load_node_config(state / "local-node")["cursor_secret"] == upgraded["cursor_secret"]


def test_gateway_records_connection_state_for_doctor(paired):
    service, node_id = paired["service"], paired["node_id"]
    row = service.node_registry.get(node_id)
    assert row["connected_at"] and row["last_seen"] and not row["disconnected_at"]
    paired["link"].stop()
    _wait(lambda: service.node_registry.get(node_id)["disconnected_at"])
    public = service.node_registry.public(service.node_registry.get(node_id))
    assert public["connection"]["disconnected_at"] >= public["connection"]["connected_at"]
    paired["link"] = _Link(paired["node_service"], paired["node_state"])
    _wait(lambda: service.node_gateway.connected(node_id))


def test_doctor_reports_paired_nodes_from_recorded_state(paired):
    service, node_id = paired["service"], paired["node_id"]
    ws_id = service.add_workspace("Alpha", str(paired["root"]), [], node_id)["workspace"]["id"]
    doctor = Service(service.state, service.config, read_only=True,
                     run_coordinator_background=False)
    try:
        report = doctor.diagnostic_report()
    finally:
        doctor.close()
    reachable = next(check for check in report["checks"] if check["code"] == "node.reachable")
    assert reachable["status"] == "pass" and "connected" in reachable["summary"]
    root = next(check for check in report["checks"]
                if check["code"] == "workspace.root_accessible" and check.get("workspace_id") == ws_id)
    assert root["status"] == "unknown"


def test_paired_connection_states():
    from datetime import datetime, timedelta, timezone
    from workspace_bridge.diagnostics import paired_connection_state
    now = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)
    stamp = lambda seconds: (now - timedelta(seconds=seconds)).isoformat()
    assert paired_connection_state({}, now)[0] == "action_required"
    assert paired_connection_state({"connected_at": stamp(600), "last_seen": stamp(10)}, now)[0] == "pass"
    assert paired_connection_state({"connected_at": stamp(600), "last_seen": stamp(600)}, now)[0] == "unknown"
    assert paired_connection_state({"connected_at": stamp(600), "last_seen": stamp(300),
                                    "disconnected_at": stamp(300)}, now)[0] == "failed"


def test_node_records_its_link_status(paired):
    from workspace_bridge.node_link import read_link_status
    state = paired["node_state"]
    assert (state / "link-status.json").stat().st_mode & 0o777 == 0o600
    connected = read_link_status(state)
    assert connected["status"] == "connected" and connected["since"]
    paired["link"].stop()
    assert read_link_status(state)["status"] == "stopped"
    paired["gateway"].stop()
    paired["link"] = _Link(paired["node_service"], state)
    _wait(lambda: read_link_status(state)["status"] == "disconnected")
    assert read_link_status(state)["code"] == "ConnectionRefusedError"
    paired["gateway"] = _Gateway(paired["service"], paired["port"])
    _wait(lambda: paired["service"].node_gateway.connected(paired["node_id"]))


def test_link_status_staleness(tmp_path):
    from datetime import datetime, timedelta, timezone
    from workspace_bridge.node_link import read_link_status, write_link_status
    assert read_link_status(tmp_path)["status"] == "unknown"
    write_link_status(tmp_path, "connected", since="2026-10-25T00:00:00+00:00")
    assert read_link_status(tmp_path)["status"] == "connected"
    later = datetime.now(timezone.utc) + timedelta(minutes=10)
    assert read_link_status(tmp_path, later) == {"status": "unknown",
                                                 "code": "paired_node_status_stale"}
