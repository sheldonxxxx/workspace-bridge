"""Bridge-side Node authority inventory with write-only credentials."""
from __future__ import annotations

import secrets
import re
import sqlite3
from datetime import datetime, timezone

from .node_client import NodeClient
from .security import BridgeError
from .adapter_registry import validate_base_url


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class NodeRegistry:
    def __init__(self, service):
        self.service = service

    def rows(self) -> list[dict]:
        with self.service.lock:
            return [dict(row) for row in self.service.db.execute(
                "SELECT * FROM nodes ORDER BY name COLLATE NOCASE,id")]

    def get(self, node_id: str) -> dict:
        if not isinstance(node_id, str) or not re.fullmatch(r"node_[0-9a-f]{24}", node_id):
            raise BridgeError("Unknown Node", "unknown_node")
        with self.service.lock:
            row = self.service.db.execute("SELECT * FROM nodes WHERE id=?", (node_id,)).fetchone()
        if row is None:
            raise BridgeError("Unknown Node", "unknown_node")
        return dict(row)

    @staticmethod
    def public(row: dict) -> dict:
        view: dict = {"id": row["id"], "name": row["name"], "base_url": row["base_url"],
                "enabled": bool(row["enabled"]), "revision": row["revision"],
                "created": row["created"], "updated": row["updated"],
                "has_token": bool(row["token"]), "health": row.get("health", "unknown"),
                "protocol": row.get("protocol"), "capabilities": row.get("capabilities", [])}
        release = row.get("release")
        if isinstance(release, dict):
            try:
                from .release import validate_release
                view["release"] = validate_release(release)
            except Exception:
                pass
        return view

    def client(self, node_id: str, *, timeout: float = 30) -> NodeClient:
        row = self.get(node_id)
        transport = getattr(self.service, "_node_transport", {}).get(node_id)
        return NodeClient(row, timeout=timeout, transport=transport)

    def list_public(self, *, probe: bool = False) -> list[dict]:
        result = []
        for row in self.rows():
            view = self.public(row)
            if probe and row["enabled"]:
                try:
                    state = self.client(row["id"], timeout=3).status()
                    self.refresh_adapters(row["id"])
                    view.update(health="healthy", protocol=state.get("protocol"),
                                node_version=state.get("node_version"),
                                capabilities=state.get("capabilities", []),
                                allowed_root_count=len(state.get("allowed_roots", [])))
                    release = state.get("release") if isinstance(state, dict) else None
                    if isinstance(release, dict):
                        try:
                            from .release import validate_release
                            view["release"] = validate_release(release)
                        except Exception:
                            pass
                except BridgeError as exc:
                    if exc.code == "adapter_identity_conflict":
                        view.update(health="failed", error_code=exc.code,
                                    error="Adapter identity conflict detected.")
                    else:
                        view.update(health="unavailable")
            elif not row["enabled"]:
                view["health"] = "disabled"
            result.append(view)
        return result

    def create(self, payload: dict) -> dict:
        if not isinstance(payload, dict) or set(payload) - {"name", "base_url", "token", "enabled"}:
            raise BridgeError("Unexpected Node fields", "invalid_arguments")
        name = payload.get("name")
        token = payload.get("token")
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 80:
            raise BridgeError("Node name must contain 1 to 80 characters", "invalid_arguments")
        if not isinstance(token, str) or not token or len(token) > 4096:
            raise BridgeError("Node token is required", "invalid_arguments")
        base_url = validate_base_url(payload.get("base_url"))
        enabled = payload.get("enabled", True)
        if not isinstance(enabled, bool):
            raise BridgeError("enabled must be a boolean", "invalid_arguments")
        node_id = "node_" + secrets.token_hex(12)
        now = _now()
        revision = "rev_" + secrets.token_hex(16)
        try:
            with self.service.lock, self.service.db:
                self.service.db.execute(
                    "INSERT INTO nodes(id,name,base_url,token,enabled,revision,created,updated) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (node_id, name.strip(), base_url, token, int(enabled), revision, now, now))
        except sqlite3.IntegrityError:
            raise BridgeError("Node name is already in use", "conflict") from None
        row = self.get(node_id)
        if enabled:
            self.refresh_adapters(node_id)
        return self.public(row)

    def update(self, node_id: str, payload: dict) -> dict:
        if not isinstance(payload, dict) or not payload or set(payload) - {
                "name", "base_url", "token", "enabled"}:
            raise BridgeError("Unexpected Node fields", "invalid_arguments")
        row = self.get(node_id)
        name = payload.get("name", row["name"])
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 80:
            raise BridgeError("Invalid Node name", "invalid_arguments")
        base_url = validate_base_url(payload.get("base_url", row["base_url"]))
        token = payload.get("token", "")
        if not isinstance(token, str) or len(token) > 4096:
            raise BridgeError("Invalid Node token", "invalid_arguments")
        if not token:
            token = row["token"]
        enabled = payload.get("enabled", bool(row["enabled"]))
        if not isinstance(enabled, bool):
            raise BridgeError("enabled must be a boolean", "invalid_arguments")
        connection_changed = (base_url != row["base_url"] or token != row["token"])
        revision = "rev_" + secrets.token_hex(16) if connection_changed else row["revision"]
        try:
            with self.service.lock, self.service.db:
                self.service.db.execute(
                    "UPDATE nodes SET name=?,base_url=?,token=?,enabled=?,revision=?,updated=? WHERE id=?",
                    (name.strip(), base_url, token, int(enabled), revision, _now(), node_id))
        except sqlite3.IntegrityError:
            raise BridgeError("Node name is already in use", "conflict") from None
        if enabled:
            self.refresh_adapters(node_id)
        return self.public(self.get(node_id))

    def delete(self, node_id: str) -> dict:
        self.get(node_id)
        with self.service.lock, self.service.db:
            if self.service.db.execute("SELECT 1 FROM workspaces WHERE node_id=? LIMIT 1", (node_id,)).fetchone():
                raise BridgeError("Node is referenced by workspaces", "conflict")
            if self.service.db.execute("SELECT 1 FROM node_adapters WHERE node_id=? LIMIT 1", (node_id,)).fetchone():
                raise BridgeError("Node is referenced by adapter history", "conflict")
            self.service.db.execute("DELETE FROM nodes WHERE id=?", (node_id,))
        return {"deleted": node_id}

    def test_connection(self, payload: dict) -> dict:
        if not isinstance(payload, dict) or set(payload) - {
                "node_id", "name", "base_url", "token", "enabled"}:
            raise BridgeError("Unexpected Node test fields", "invalid_arguments")
        if payload.get("node_id"):
            row = self.get(payload["node_id"])
            token = payload.get("token") or row["token"]
            base_url = validate_base_url(payload.get("base_url", row["base_url"]))
            name = payload.get("name", row["name"])
            revision = row["revision"]
            enabled = payload.get("enabled", bool(row["enabled"]))
        else:
            token = payload.get("token")
            base_url = validate_base_url(payload.get("base_url"))
            name = payload.get("name", "New Node")
            revision = "test"
            enabled = True
        if not isinstance(token, str) or not token or len(token) > 4096:
            raise BridgeError("Node token is required", "invalid_arguments")
        try:
            node_id = payload.get("node_id", "node_" + "0" * 24)
            transport = getattr(self.service, "_node_transport", {}).get(node_id)
            status = NodeClient({"id": node_id, "name": name, "base_url": base_url,
                "token": token, "revision": revision, "enabled": enabled},
                timeout=5, transport=transport).status()
            return {"success": True, "protocol": status.get("protocol"),
                    "capabilities": status.get("capabilities", []),
                    "allowed_root_count": len(status.get("allowed_roots", []))}
        except BridgeError as exc:
            return {"success": False, "code": exc.code,
                    "message": "Node connection could not be verified."}

    def refresh_adapters(self, node_id: str) -> list[dict]:
        client = self.client(node_id, timeout=5)
        try:
            catalog = client.list_adapters().get("adapters", [])
        except BridgeError:
            return []
        adapters = []
        for adapter in catalog:
            adapter_id = adapter.get("id")
            if not isinstance(adapter_id, str) or not adapter_id.startswith("adapter_"):
                continue
            adapters.append((adapter_id, adapter))
        now = _now()
        with self.service.lock, self.service.db:
            # Adapter IDs are global identities.  Refuse a cross-Node claim
            # before changing any cache row, so a stale or malicious catalog
            # cannot silently re-home an existing runtime destination.
            for adapter_id, _ in adapters:
                existing = self.service.db.execute(
                    "SELECT node_id FROM node_adapters WHERE adapter_id=?",
                    (adapter_id,)).fetchone()
                if existing is not None and existing["node_id"] != node_id:
                    raise BridgeError(
                        "Adapter identity is already owned by another Node",
                        "adapter_identity_conflict")
            for adapter_id, adapter in adapters:
                self.service.db.execute(
                    "INSERT INTO node_adapters(adapter_id,node_id,name,runtime_type,base_url,revision,enabled,has_token,last_seen) "
                    "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(adapter_id) DO UPDATE SET "
                    "name=excluded.name,runtime_type=excluded.runtime_type,base_url=excluded.base_url,"
                    "revision=excluded.revision,enabled=excluded.enabled,has_token=excluded.has_token,"
                    "last_seen=excluded.last_seen",
                    (adapter_id, node_id, adapter.get("name", "Adapter"),
                     adapter.get("runtime_type", "pi"), adapter.get("base_url", ""), adapter.get("revision", ""),
                     int(bool(adapter.get("enabled"))), int(bool(adapter.get("has_token"))), now))
            self.service.db.execute("UPDATE node_adapters SET enabled=0 WHERE node_id=? AND last_seen<>?",
                                    (node_id, now))
        return catalog

    def sync_all(self) -> None:
        for node in self.rows():
            if node["enabled"]:
                self.refresh_adapters(node["id"])
