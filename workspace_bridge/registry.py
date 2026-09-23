"""Package-owned multi-runtime registry (milestone 3A2).

Maps stable runtime ids (``"pi"`` today) to configured
``AgentRuntime`` instances. Entries are constructed only from local
environment configuration by ``runtime_registry_from_environment``; there is
no dynamic plugin loading and project content can never register entries.

Factory construction performs no network calls: HTTP clients are lazy and
only speak on first use.
"""
from __future__ import annotations

import os

from .runtime import (PI_RUNTIME_ID, AgentRuntime, HttpPiRuntime, is_valid_runtime_id)
from .security import BridgeError


class RuntimeRegistry:
    """Small stable mapping of runtime id -> configured AgentRuntime."""

    def __init__(self, runtimes: dict[str, AgentRuntime] | None = None):
        self._runtimes: dict[str, AgentRuntime] = {}
        for runtime_id, runtime in (runtimes or {}).items():
            self.register(runtime, runtime_id=runtime_id)

    def register(self, runtime: AgentRuntime, *, runtime_id: str | None = None) -> AgentRuntime:
        """Register one runtime; reject invalid, duplicate, or mismatched ids.

        Both the runtime's own stable identity and any explicit mapping key
        must satisfy the canonical package grammar
        (``^[a-z0-9][a-z0-9_-]{0,31}$``); anything else fails closed
        (``invalid_arguments``) before registration, even when the explicit
        key matches. The registry key must always equal
        ``runtime.runtime_id`` exactly. An explicit key that disagrees with
        the runtime's own stable identity fails closed
        (``runtime_mismatch``) without registering, so a miswired mapping
        can never undermine the persisted runtime-ownership invariant (e.g.
        ``"pi"`` pointing at the wrong backend). An empty or
        unimplemented runtime identity can never be aliased into the
        registry via an explicit key either.
        """
        try:
            actual = runtime.runtime_id
        except NotImplementedError:
            actual = ""
        if not is_valid_runtime_id(actual):
            raise BridgeError("Runtime must expose a canonical stable runtime_id "
                              "(^[a-z0-9][a-z0-9_-]{0,31}$)",
                              "invalid_arguments")
        if runtime_id is not None:
            if not is_valid_runtime_id(runtime_id):
                raise BridgeError("Runtime id must match ^[a-z0-9][a-z0-9_-]{0,31}$",
                                   "invalid_arguments")
            if runtime_id != actual:
                raise BridgeError(
                    f"Runtime id {runtime_id!r} does not match "
                    f"runtime identity {actual!r}",
                    "runtime_mismatch")
        if actual in self._runtimes:
            raise BridgeError(f"Runtime {actual!r} is already registered", "conflict")
        self._runtimes[actual] = runtime
        return runtime

    def get(self, runtime_id: str) -> AgentRuntime:
        """Return the configured runtime or fail closed for unknown ids."""
        try:
            return self._runtimes[runtime_id]
        except KeyError:
            raise BridgeError(f"Runtime {runtime_id!r} is not configured", "unknown_runtime") from None

    def optional(self, runtime_id: str) -> AgentRuntime | None:
        """Return the runtime or None when that id is not configured."""
        return self._runtimes.get(runtime_id)

    def ids(self) -> list[str]:
        """Stable sorted list of configured runtime ids."""
        return sorted(self._runtimes)

    def __len__(self) -> int:
        return len(self._runtimes)

    def __contains__(self, runtime_id: object) -> bool:
        return runtime_id in self._runtimes

    def items(self):
        return self._runtimes.items()

    def status(self) -> dict:
        """Sanitized per-runtime diagnostics: ids plus bounded health/capabilities.

        Never includes paths, tokens, or raw backend payloads. A failing
        health probe reports healthy=False with a bounded detail string.
        """
        runtimes: dict[str, dict] = {}
        for runtime_id, runtime in self._runtimes.items():
            try:
                caps = runtime.capabilities
                capabilities = {name: bool(getattr(caps, name)) for name in (
                    "model_discovery", "session_reuse", "event_polling", "session_status",
                    "pending_snapshot", "permission_response", "question_detection",
                    "question_response", "session_branching")}
            except Exception:  # noqa: BLE001 - diagnostics never raise
                capabilities = {}
            try:
                health = runtime.health()
                if not isinstance(health, dict):
                    raise ValueError("invalid health")
                runtimes[runtime_id] = {
                    "configured": True,
                    "healthy": bool(health.get("ok")),
                    "locked": bool(health.get("locked", False)),
                    "health": {k: health.get(k) for k in (
                        "ok", "version", "adapter_version", "locked", "instance",
                        "status", "server_configured", "cursor",
                        # 3B1 Pi deployed permission support: bounded
                        # booleans only (absent on backends without them).
                        "deployed_capabilities", "permissions_supported") if k in health},
                    "capabilities": capabilities,
                }
            except BridgeError as exc:
                runtimes[runtime_id] = {"configured": True, "healthy": False,
                                        "locked": False, "detail": str(exc)[:200],
                                        "capabilities": capabilities}
            except Exception:  # noqa: BLE001 - diagnostics never raise
                runtimes[runtime_id] = {"configured": True, "healthy": False,
                                        "locked": False, "detail": "health check failed",
                                        "capabilities": capabilities}
        return {"configured": self.ids(), "runtimes": runtimes}

    def close(self) -> None:
        """Close every registered runtime; one failure never blocks the rest."""
        for runtime in list(self._runtimes.values()):
            try:
                runtime.close()
            except Exception:  # noqa: BLE001 - shutdown best effort
                pass


def runtime_registry_from_environment(environ: dict | None = None) -> RuntimeRegistry:
    """Build the configured registry from local environment only.

    - ``WB_PI_RUNTIME_URL`` -> ``HttpPiRuntime``
    - shared ``WB_RUNTIME_TOKEN`` is supplied to each configured adapter.

    No network calls are performed. No credentials are read from MCP,
    project files, manager JSON, or handoffs.
    """
    env = environ if environ is not None else os.environ
    registry = RuntimeRegistry()
    pi_url = (env.get("WB_PI_RUNTIME_URL") or "").strip()
    token = (env.get("WB_RUNTIME_TOKEN") or "").strip()
    if pi_url:
        registry.register(HttpPiRuntime(pi_url, token), runtime_id=PI_RUNTIME_ID)
    return registry
