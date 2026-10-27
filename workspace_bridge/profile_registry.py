"""Bridge-owned security profiles and the route checks that use them.

The built-in profiles are fixed; custom profiles live in the Bridge state.
A route names one profile (or ``native`` for an agent that governs itself),
and every run carries that profile's rules at the moment it starts. An edit
therefore reaches new runs and continuations, never a run already going.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from . import security_profiles as sp
from .security import BridgeError


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def adapter_security(row: dict) -> dict:
    """The enforcement capabilities a Node reported for one adapter."""
    try:
        value = json.loads(row.get("security") or "{}")
    except (TypeError, ValueError):
        value = {}
    if not isinstance(value, dict):
        value = {}
    enforces = value.get("enforces")
    enforces = sorted({key for key in enforces if key in sp.KEYS}) if isinstance(enforces, list) else []
    return {"enforces": enforces, "native": value.get("native") is True,
            "worktrees": value.get("worktrees") is True}


class ProfileRegistry:
    def __init__(self, service):
        self.service = service

    def _custom_rows(self) -> list[dict]:
        with self.service.lock:
            return [dict(row) for row in self.service.db.execute(
                "SELECT id,rules,revision,created,updated FROM security_profiles ORDER BY id")]

    def _public(self, profile_id: str, rules: dict, *, builtin: bool,
                updated: str | None = None) -> dict:
        result = {"id": profile_id, "revision": sp.revision(rules), "rules": rules,
                  "builtin": builtin, "restricts": sp.restricted_keys(rules)}
        if builtin:
            result["description"] = sp.DESCRIPTIONS.get(profile_id, "")
        if updated:
            result["updated"] = updated
        return result

    def list(self) -> list[dict]:
        rows = [self._public(name, rules, builtin=True) for name, rules in sp.BUILTINS.items()]
        for row in self._custom_rows():
            rows.append(self._public(row["id"], json.loads(row["rules"]), builtin=False,
                                     updated=row["updated"]))
        for row in rows:
            row["routes"] = self.routes_using(row["id"])
        return rows

    def get(self, profile_id: str) -> dict:
        if not isinstance(profile_id, str) or not sp.PROFILE_ID.fullmatch(profile_id):
            raise BridgeError("Unknown security profile", "profile_unavailable")
        if profile_id in sp.BUILTINS:
            return self._public(profile_id, sp.BUILTINS[profile_id], builtin=True)
        with self.service.lock:
            row = self.service.db.execute(
                "SELECT rules,updated FROM security_profiles WHERE id=?", (profile_id,)).fetchone()
        if row is None:
            raise BridgeError("Unknown security profile", "profile_unavailable")
        return self._public(profile_id, json.loads(row["rules"]), builtin=False,
                            updated=row["updated"])

    def routes_using(self, profile_id: str) -> list[dict]:
        with self.service.lock:
            return [dict(row) for row in self.service.db.execute(
                "SELECT r.workspace AS workspace_id,w.name AS workspace_name,r.adapter_id,"
                "a.name AS adapter_name,r.enabled FROM workspace_routes r "
                "JOIN workspaces w ON w.id=r.workspace JOIN node_adapters a ON a.adapter_id=r.adapter_id "
                "WHERE r.security_source='profile' AND r.profile_id=? "
                "ORDER BY w.name COLLATE NOCASE,a.name COLLATE NOCASE", (profile_id,))]

    def _unenforceable_routes(self, profile_id: str, rules: dict) -> list[dict]:
        result = []
        for route in self.routes_using(profile_id):
            adapter = self.service.adapter_registry.get(route["adapter_id"])
            missing = sp.unenforced(rules, adapter_security(adapter)["enforces"])
            if missing:
                result.append({**route, "unenforced": missing})
        return result

    def save(self, profile_id: str, rules: object, *, expected_revision: str | None = None,
             confirm: bool = False) -> dict:
        """Create or replace one custom profile.

        A change that loosens any rule, or that restricts a key an assigned
        route's agent cannot enforce, is returned for confirmation first and
        saved only when ``confirm`` is true.
        """
        if not isinstance(profile_id, str) or not sp.PROFILE_ID.fullmatch(profile_id):
            raise BridgeError("Profile ID must be a short lowercase name", "invalid_profile")
        if profile_id in sp.BUILTINS or profile_id == "native":
            raise BridgeError("Built-in profiles cannot be changed", "invalid_profile")
        canonical = sp.validate(rules)
        new_revision = sp.revision(canonical)
        with self.service.lock:
            row = self.service.db.execute(
                "SELECT rules,revision FROM security_profiles WHERE id=?", (profile_id,)).fetchone()
        if expected_revision is not None and (row is None or row["revision"] != expected_revision):
            raise BridgeError("Profile changed since it was read", "conflict")
        if row is not None and expected_revision is None:
            raise BridgeError("Profile already exists; pass its current revision to replace it",
                              "conflict")
        loosened = sp.loosened(json.loads(row["rules"]), canonical) if row is not None else []
        unenforceable = self._unenforceable_routes(profile_id, canonical)
        routes = self.routes_using(profile_id)
        if (loosened or unenforceable) and not confirm:
            return {"saved": False, "needs_confirmation": True, "id": profile_id,
                    "loosened": loosened, "routes": routes if loosened else [],
                    "unenforceable_routes": unenforceable}
        if row is not None and row["revision"] == new_revision:
            return {"saved": True, **self.get(profile_id)}
        timestamp = _now()
        with self.service.lock, self.service.db:
            current = self.service.db.execute(
                "SELECT revision FROM security_profiles WHERE id=?", (profile_id,)).fetchone()
            if (current["revision"] if current else None) != (row["revision"] if row else None):
                raise BridgeError("Profile changed since it was read", "conflict")
            self.service.db.execute(
                "INSERT INTO security_profiles(id,rules,revision,created,updated) VALUES(?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET rules=excluded.rules,revision=excluded.revision,"
                "updated=excluded.updated",
                (profile_id, sp.canonical_json(canonical), new_revision, timestamp, timestamp))
        self.service.event(None, "save_security_profile", "loosened" if loosened else "saved")
        return {"saved": True, **self.get(profile_id), "loosened": loosened,
                "unenforceable_routes": unenforceable}

    def delete(self, profile_id: str) -> dict:
        if profile_id in sp.BUILTINS:
            raise BridgeError("Built-in profiles cannot be deleted", "invalid_profile")
        self.get(profile_id)
        if self.routes_using(profile_id):
            raise BridgeError("Assign another profile to its routes before deleting this one",
                              "conflict")
        with self.service.lock, self.service.db:
            self.service.db.execute("DELETE FROM security_profiles WHERE id=?", (profile_id,))
        self.service.event(None, "delete_security_profile", "deleted")
        return {"deleted": True, "id": profile_id}

    def binding(self, adapter: dict, source: str | None, profile_id: str | None) -> dict:
        """The security block for a new run on one route, or raise.

        ``profile`` routes carry the full rules; ``native`` is allowed only
        for an agent whose plugin declares native security.
        """
        capabilities = adapter_security(adapter)
        if source == "native":
            if not capabilities["native"]:
                raise BridgeError("This agent cannot run under its native configuration",
                                  "security_unavailable")
            return {"source": "native"}
        if source != "profile" or not profile_id:
            raise BridgeError("Workspace route security is not configured", "profile_unconfigured")
        profile = self.get(profile_id)
        missing = sp.unenforced(profile["rules"], capabilities["enforces"])
        if missing:
            raise BridgeError("This agent cannot enforce the profile's " + ", ".join(missing)
                              + " rules; choose a profile that allows them or another agent",
                              "security_unavailable")
        return sp.wire(profile_id, profile["rules"])

    def run_binding(self, adapter: dict, binding: dict, plan_folder: str | None) -> dict:
        """The binding one run carries: the route's, plus read access to its plan files."""
        if not plan_folder:
            return binding
        return sp.grant_read(binding, plan_folder, adapter_security(adapter)["enforces"])
