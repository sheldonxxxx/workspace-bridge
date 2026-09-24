"""Bounded client and validation for the private Runtime Protocol v1.

This module knows only Bridge resource names. Native Pi/Codex wire formats stay
inside their adapters. No adapter endpoint accepts an arbitrary command.
"""
from __future__ import annotations

import json
import os
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from .runtime import RuntimeRejected, RuntimeUnavailable, RuntimeUnsupported, is_valid_runtime_id
from .security import BridgeError, redact

MAX_RESPONSE_BYTES = 2 * 1024 * 1024
CORE_FEATURES = frozenset({"models", "conversations", "runs", "activities", "interactions"})
OPTIONAL_FEATURES = frozenset({"events", "steering", "imageInput"})
ALL_FEATURES = CORE_FEATURES | OPTIONAL_FEATURES
RUN_PHASES = frozenset({"starting", "active", "terminal"})
ACTIVE_STATES = frozenset({"running", "waiting_interaction"})
OUTCOMES = frozenset({"succeeded", "failed", "cancelled", "interrupted", "orphaned"})


def _identifier(value: Any, label: str) -> str:
    if (not isinstance(value, str) or not value or len(value) > 200
            or any(ord(char) < 33 or char in "/?#" for char in value)):
        raise BridgeError(f"Invalid {label}", "invalid_arguments")
    return value


def validate_run_state(value: dict) -> dict:
    """Reject contradictory or unknown lifecycle states at the trust boundary."""
    if not isinstance(value, dict):
        raise RuntimeUnavailable("Runtime returned an invalid run")
    phase = value.get("phase")
    active = value.get("activeState")
    outcome = value.get("outcome")
    if phase not in RUN_PHASES:
        raise RuntimeUnavailable("Runtime returned an invalid run phase")
    if phase == "active" and (active not in ACTIVE_STATES or outcome is not None):
        raise RuntimeUnavailable("Runtime returned an invalid active run")
    if phase == "starting" and (active is not None or outcome is not None):
        raise RuntimeUnavailable("Runtime returned an invalid starting run")
    if phase == "terminal" and (active is not None or outcome not in OUTCOMES):
        raise RuntimeUnavailable("Runtime returned an invalid terminal run")
    return value


@dataclass(frozen=True)
class Descriptor:
    runtime_id: str
    display_name: str
    adapter_version: str
    native_version: str
    instance_id: str
    features: dict[str, int]

    @classmethod
    def parse(cls, value: Any, *, expected_runtime: str) -> "Descriptor":
        if not isinstance(value, dict):
            raise RuntimeUnavailable("Runtime descriptor is invalid")
        protocol = value.get("protocol")
        runtime = value.get("runtime")
        features = value.get("features")
        if not isinstance(protocol, dict) or protocol.get("major") != 1:
            raise RuntimeUnsupported("Runtime protocol major version is unsupported")
        if not isinstance(runtime, dict) or runtime.get("id") != expected_runtime:
            raise RuntimeUnavailable("Runtime identity does not match configuration")
        if not isinstance(features, dict) or not CORE_FEATURES.issubset(features):
            raise RuntimeUnsupported("Runtime omits a required v1 feature")
        if any(not isinstance(name, str) or not isinstance(version, int)
               or isinstance(version, bool) or version < 1
               for name, version in features.items()):
            raise RuntimeUnsupported("Runtime feature versions are invalid")
        if any(features[name] != 1 for name in CORE_FEATURES):
            raise RuntimeUnsupported("Runtime core feature version is unsupported")
        instance_id = runtime.get("instanceId")
        if not isinstance(instance_id, str) or not instance_id or len(instance_id) > 200:
            raise RuntimeUnavailable("Runtime instance identity is invalid")
        return cls(runtime_id=expected_runtime,
                   display_name=str(runtime.get("displayName") or expected_runtime)[:120],
                   adapter_version=str(runtime.get("adapterVersion") or "")[:80],
                   native_version=str(runtime.get("nativeVersion") or "")[:80],
                   instance_id=instance_id,
                   features={name: int(version) for name, version in features.items()
                             if name in ALL_FEATURES})

    def supports(self, feature: str) -> bool:
        return self.features.get(feature) == 1


class HttpRuntimeAdapter:
    """One strict HTTP client for any conforming private runtime adapter."""

    def __init__(self, runtime_id: str, base_url: str, token: str, *, timeout: float = 30):
        if not is_valid_runtime_id(runtime_id):
            raise BridgeError("Invalid runtime id", "invalid_arguments")
        parsed = urllib.parse.urlparse(base_url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username:
            raise BridgeError("Invalid runtime adapter URL", "invalid_arguments")
        if not isinstance(token, str) or not token:
            raise BridgeError("Runtime adapter token is required", "invalid_arguments")
        self.runtime_id = runtime_id
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = min(max(float(timeout), 0.5), 60.0)

    def _request(self, method: str, path: str, *, body: dict | None = None,
                 query: dict | None = None, timeout: float | None = None) -> dict:
        if not path.startswith("/v1/"):
            raise BridgeError("Invalid adapter path", "invalid_arguments")
        url = self.base_url + path
        if query:
            url += "?" + urllib.parse.urlencode({k: v for k, v in query.items()
                                                   if v is not None})
        raw_body = None if body is None else json.dumps(body, separators=(",", ":")).encode()
        if raw_body is not None and len(raw_body) > 512 * 1024:
            raise BridgeError("Adapter request is too large", "invalid_arguments")
        headers = {"Accept": "application/json", "X-Runtime-Token": self.token}
        if raw_body is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=raw_body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout or self.timeout) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise RuntimeUnavailable("Runtime response is too large")
                value = json.loads(raw)
                if not isinstance(value, dict):
                    raise RuntimeUnavailable("Runtime response is invalid")
                return value
        except urllib.error.HTTPError as exc:
            detail = ""
            code = "runtime_rejected"
            try:
                payload = json.loads(exc.read(8192))
                if isinstance(payload, dict):
                    detail = str(payload.get("error") or "")[:300]
                    code = str(payload.get("code") or code)[:80]
            except (ValueError, UnicodeError):
                pass
            detail = redact(detail)[0]
            if not re.fullmatch(r"[a-z][a-z0-9_]{0,79}", code):
                code = "runtime_rejected"
            if exc.code == 501:
                raise RuntimeUnsupported(detail or "Runtime feature is unsupported") from None
            if exc.code in (400, 404, 409, 412, 422):
                raise RuntimeRejected(detail or "Runtime rejected the request",
                                      code=code, status=exc.code) from None
            raise RuntimeUnavailable("Runtime adapter returned an error") from None
        except (urllib.error.URLError, socket.timeout, TimeoutError, OSError):
            raise RuntimeUnavailable("Runtime adapter is unavailable") from None
        except (ValueError, UnicodeError):
            raise RuntimeUnavailable("Runtime returned invalid JSON") from None

    def descriptor(self) -> Descriptor:
        return Descriptor.parse(self._request("GET", "/v1/descriptor", timeout=3),
                                expected_runtime=self.runtime_id)

    def models(self, workspace_id: str) -> list[dict]:
        value = self._request("GET", "/v1/models", query={"workspaceId": workspace_id})
        rows = value.get("models")
        if not isinstance(rows, list):
            raise RuntimeUnavailable("Runtime model list is invalid")
        result = []
        for row in rows[:2000]:
            if not isinstance(row, dict):
                continue
            selector = row.get("selector")
            if not isinstance(selector, str) or not selector or len(selector) > 260:
                continue
            modalities = row.get("inputModalities")
            if not isinstance(modalities, list):
                modalities = ["text"]
            reasoning = row.get("reasoningOptions")
            if not isinstance(reasoning, list):
                reasoning = []
            efforts = []
            for item in reasoning[:20]:
                effort = (item if isinstance(item, str) else
                          item.get("reasoningEffort", item.get("effort"))
                          if isinstance(item, dict) else None)
                if isinstance(effort, str) and effort and len(effort) <= 40:
                    if effort not in efforts:
                        efforts.append(effort)
            default_effort = row.get("defaultReasoningEffort")
            result.append({"selector": selector,
                           "displayName": redact(str(row.get("displayName") or selector)[:200])[0],
                           "inputModalities": [str(item)[:40] for item in modalities[:10]
                                               if isinstance(item, str)],
                           "reasoningOptions": efforts,
                           "defaultReasoningEffort": (default_effort[:40]
                               if isinstance(default_effort, str)
                               and default_effort in efforts else None),
                           "default": row.get("default") is True})
        return result

    def profiles(self) -> list[dict]:
        value = self._request("GET", "/v1/profiles")
        rows = value.get("profiles")
        if not isinstance(rows, list):
            raise RuntimeUnavailable("Runtime profile list is invalid")
        result = []
        for row in rows[:100]:
            if (not isinstance(row, dict) or not isinstance(row.get("id"), str)
                    or not isinstance(row.get("revision"), str)
                    or len(row["id"]) > 100 or len(row["revision"]) > 100):
                continue
            enforcement = row.get("enforcement")
            if not isinstance(enforcement, list):
                enforcement = []
            config = row.get("config")
            if config is not None and (not isinstance(config, dict)
                                       or len(json.dumps(config)) > 32768):
                continue
            result.append({"id": row["id"], "revision": row["revision"],
                           "config": config, "mutable": row.get("mutable") is True,
                           "enforcement": [item[:80] for item in enforcement[:20]
                                           if isinstance(item, str)]})
        return result

    def save_profile(self, profile_id: str, config: dict,
                     expected_revision: str | None) -> dict:
        return self._request("POST", "/v1/profiles", body={
            "id": profile_id, "config": config,
            "expectedRevision": expected_revision})

    def delete_profile(self, profile_id: str) -> dict:
        return self._request("DELETE", "/v1/profiles/" +
                             _identifier(profile_id, "profile id"))

    def create_conversation(self, payload: dict) -> dict:
        return self._request("POST", "/v1/conversations", body=payload)

    def conversation(self, conversation_id: str) -> dict:
        return self._request("GET", f"/v1/conversations/{_identifier(conversation_id, 'conversation id')}")

    def start_run(self, conversation_id: str, payload: dict) -> dict:
        value = self._request("POST", f"/v1/conversations/{_identifier(conversation_id, 'conversation id')}/runs",
                              body=payload)
        return validate_run_state(value)

    def find_run(self, conversation_id: str, client_run_id: str) -> dict:
        value = self._request("GET", f"/v1/conversations/"
            f"{_identifier(conversation_id, 'conversation id')}/runs/"
            f"{_identifier(client_run_id, 'client run id')}")
        return validate_run_state(value)

    def run(self, run_id: str) -> dict:
        value = self._request("GET", f"/v1/runs/{_identifier(run_id, 'run id')}")
        return validate_run_state(value)

    def cancel(self, run_id: str) -> dict:
        return self._request("POST", f"/v1/runs/{_identifier(run_id, 'run id')}/cancel", body={})

    def steer(self, run_id: str, input_items: list[dict]) -> dict:
        if not self.descriptor().supports("steering"):
            raise RuntimeUnsupported("Runtime does not support steering")
        return self._request("POST", f"/v1/runs/{_identifier(run_id, 'run id')}/steer",
                             body={"expectedRunId": run_id, "input": input_items})

    def interactions(self, run_id: str) -> list[dict]:
        value = self._request("GET", f"/v1/runs/{_identifier(run_id, 'run id')}/interactions")
        rows = value.get("interactions")
        if not isinstance(rows, list):
            raise RuntimeUnavailable("Runtime interaction snapshot is invalid")
        return [row for row in rows[:100] if isinstance(row, dict)]

    def resolve(self, interaction_id: str, response: dict) -> dict:
        return self._request("POST", f"/v1/interactions/{_identifier(interaction_id, 'interaction id')}/resolve",
                             body=response)

    def activities(self, run_id: str) -> list[dict]:
        value = self._request("GET", f"/v1/runs/{_identifier(run_id, 'run id')}/activities")
        rows = value.get("activities")
        if not isinstance(rows, list):
            raise RuntimeUnavailable("Runtime activity snapshot is invalid")
        return [row for row in rows[:1000] if isinstance(row, dict)]

    def activity(self, activity_id: str) -> dict:
        return self._request("GET", f"/v1/activities/{_identifier(activity_id, 'activity id')}")

    def events(self, *, after: int, wait_ms: int = 0) -> dict:
        if not self.descriptor().supports("events"):
            raise RuntimeUnsupported("Runtime does not support events")
        return self._request("GET", "/v1/events",
                             query={"after": max(0, after), "waitMs": min(max(wait_ms, 0), 25000)},
                             timeout=min(self.timeout, wait_ms / 1000 + 5))


def adapters_from_environment(environ: dict | None = None) -> dict[str, HttpRuntimeAdapter]:
    """Trusted host configuration only; project files cannot register runtimes."""
    env = os.environ if environ is None else environ
    raw = env.get("WB_RUNTIME_ADAPTERS") or "{}"
    try:
        configured = json.loads(raw)
    except ValueError:
        raise BridgeError("WB_RUNTIME_ADAPTERS is invalid JSON", "invalid_configuration") from None
    if not isinstance(configured, dict):
        raise BridgeError("WB_RUNTIME_ADAPTERS must be an object", "invalid_configuration")
    token = env.get("WB_RUNTIME_TOKEN") or ""
    return {runtime_id: HttpRuntimeAdapter(runtime_id, url, token)
            for runtime_id, url in configured.items()}
