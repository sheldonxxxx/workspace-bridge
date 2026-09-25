"""Sanitized Bridge cache for Node-owned runtime adapter configurations."""
from __future__ import annotations

import re

from .node_client import NodeRuntimeAdapterProxy
from .security import BridgeError

ADAPTER_ID_RE = re.compile(r"^adapter_[0-9a-f]{24}$")
RUNTIME_TYPES = frozenset({"pi", "codex"})


def validate_base_url(value: object) -> str:
    import urllib.parse
    if not isinstance(value, str) or len(value) > 2048:
        raise BridgeError("Invalid service URL", "invalid_arguments")
    parsed = urllib.parse.urlsplit(value.strip())
    try:
        _ = parsed.port
    except ValueError:
        raise BridgeError("Invalid service URL", "invalid_arguments") from None
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment):
        raise BridgeError("Service URL must be http or https without credentials",
                          "invalid_arguments")
    return value.strip().rstrip("/")


class AdapterRegistry:
    """Route an opaque adapter identity to its owning Node on every operation."""
    def __init__(self, service):
        self.service = service

    def rows(self, *, include_disabled: bool = True) -> list[dict]:
        query = ("SELECT a.*,n.name AS node_name,n.enabled AS node_enabled "
                 "FROM node_adapters a JOIN nodes n ON n.id=a.node_id")
        if not include_disabled:
            query += " WHERE a.enabled=1 AND n.enabled=1"
        query += " ORDER BY a.name COLLATE NOCASE,a.adapter_id"
        with self.service.lock:
            rows = [dict(row) for row in self.service.db.execute(query)]
        for row in rows:
            row["id"] = row["adapter_id"]
        return rows

    def get(self, adapter_id: str, *, require_enabled: bool = False) -> dict:
        if not isinstance(adapter_id, str) or not ADAPTER_ID_RE.fullmatch(adapter_id):
            raise BridgeError("Unknown adapter", "unknown_adapter")
        with self.service.lock:
            row = self.service.db.execute(
                "SELECT a.*,n.name AS node_name,n.enabled AS node_enabled "
                "FROM node_adapters a JOIN nodes n ON n.id=a.node_id WHERE a.adapter_id=?",
                (adapter_id,)).fetchone()
        if row is None:
            raise BridgeError("Unknown adapter", "unknown_adapter")
        result = dict(row)
        result["id"] = result["adapter_id"]
        if require_enabled and (not result["enabled"] or not result["node_enabled"]):
            raise BridgeError("Adapter or its Node is disabled", "adapter_disabled")
        return result

    @staticmethod
    def public(row: dict) -> dict:
        return {"id": row.get("id", row.get("adapter_id")), "node_id": row["node_id"],
                "node_name": row.get("node_name", ""), "name": row["name"],
                "runtime_type": row["runtime_type"], "base_url": row.get("base_url", ""),
                "enabled": bool(row["enabled"]),
                "revision": row["revision"], "last_seen": row.get("last_seen"),
                "has_token": bool(row.get("has_token"))}

    def client(self, adapter_id: str, *, require_enabled: bool = False,
               timeout: float = 30, workspace: dict | None = None) -> NodeRuntimeAdapterProxy:
        row = self.get(adapter_id, require_enabled=require_enabled)
        node = self.service.node_registry.client(row["node_id"], timeout=timeout)
        return NodeRuntimeAdapterProxy(node, row, workspace, timeout=timeout)

    def create(self, payload: dict) -> dict:
        if not isinstance(payload, dict):
            raise BridgeError("Adapter object required", "invalid_arguments")
        if "node_id" not in payload:
            enabled_nodes = [row for row in self.service.node_registry.rows() if row["enabled"]]
            if len(enabled_nodes) != 1:
                raise BridgeError("Select an authoritative Node", "node_required")
            payload = {**payload, "node_id": enabled_nodes[0]["id"]}
        payload = dict(payload)
        node_id = payload.pop("node_id")
        node = self.service.node_registry.client(node_id, timeout=5)
        result = node.create_adapter(payload)
        self.service.node_registry.refresh_adapters(node_id)
        row = self.get(result["id"])
        return self.public(row)

    def update(self, adapter_id: str, payload: dict) -> dict:
        row = self.get(adapter_id)
        result = self.service.node_registry.client(row["node_id"], timeout=5).update_adapter(adapter_id, payload)
        self.service.node_registry.refresh_adapters(row["node_id"])
        return self.public(self.get(result["id"]))

    def delete(self, adapter_id: str) -> dict:
        row = self.get(adapter_id)
        with self.service.lock:
            if self.service.db.execute(
                    "SELECT 1 FROM workspace_routes WHERE adapter_id=? LIMIT 1",
                    (adapter_id,)).fetchone():
                raise BridgeError("Adapter is referenced by workspace routes", "conflict")
            if self.service.db.execute(
                    "SELECT 1 FROM agent_runs WHERE adapter_id=? LIMIT 1", (adapter_id,)).fetchone():
                raise BridgeError("Adapter is referenced by execution history", "conflict")
        result = self.service.node_registry.client(row["node_id"], timeout=5).delete_adapter(adapter_id)
        self.service.node_registry.refresh_adapters(row["node_id"])
        return result
