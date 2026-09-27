"""Bounded client and validation for the private Runtime Protocol v1.

This module knows only Bridge resource names. Native Pi/Codex wire formats stay
inside their adapters. No adapter endpoint accepts an arbitrary command.
"""
from __future__ import annotations

import json
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from .runtime import RuntimeRejected, RuntimeUnavailable, RuntimeUnsupported
from .security import BridgeError, redact

MAX_RESPONSE_BYTES = 2 * 1024 * 1024
CORE_FEATURES = frozenset({"models", "conversations", "runs", "activities", "interactions"})
OPTIONAL_FEATURES = frozenset({"events", "steering", "imageInput", "securityRebind"})
ALL_FEATURES = CORE_FEATURES | OPTIONAL_FEATURES
RUN_PHASES = frozenset({"starting", "active", "terminal"})
ACTIVE_STATES = frozenset({"running", "waiting_interaction"})
OUTCOMES = frozenset({"succeeded", "failed", "cancelled", "interrupted", "orphaned"})
SECURITY_PROVENANCE = frozenset({"named-profile", "implicit/default", "legacy-sandbox"})
SECURITY_APPROVAL_CATEGORIES = frozenset({
    "untrusted", "on-request", "never", "granular", "default", "other"})
SECURITY_REVIEWER_CATEGORIES = frozenset({
    "user", "auto_review", "guardian_subagent", "default", "other"})
SECURITY_REPLACEMENT_REASONS = frozenset({
    "unresolved-permission-profile", "permission-profile-unavailable",
    "legacy-sandbox-transition", "settings-update-already-pending",
    "settings-update-unconfirmed", "settings-update-mismatch",
    "settings-update-failed"})
MAX_SAFE_INTEGER = 9007199254740991
RUN_USAGE_FIELDS = frozenset({
    "inputTokens", "cachedInputTokens", "cacheWriteInputTokens",
    "outputTokens", "reasoningOutputTokens", "totalTokens"})


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
    if "securityBinding" in value:
        value = {**value, "securityBinding": _validate_security_binding(
            value.get("securityBinding"))}
    if "usage" in value:
        value = {**value, "usage": _validate_run_usage(value.get("usage"))}
    return value


def _validate_run_usage(value: Any) -> dict:
    """Validate the optional additive run-scoped native token usage snapshot.

    The field carries normalized native provider counters for exactly one
    Bridge run, including continuation runs which start a fresh accounting
    boundary. Counters are run-scoped and may be partial while the run is
    active. Absent native usage is represented by omitting the field entirely;
    present counters must be non-negative safe integers. No counter is
    synthesized, estimated, or derived from cumulative conversation/session
    totals. Unknown or malformed usage fails closed as an invalid snapshot.
    """
    if not isinstance(value, dict) or not value:
        raise RuntimeUnavailable("Runtime run usage is invalid")
    if set(value) - RUN_USAGE_FIELDS:
        raise RuntimeUnavailable("Runtime run usage is invalid")
    result: dict[str, int] = {}
    for key, counter in value.items():
        if (isinstance(counter, bool) or not isinstance(counter, int)
                or counter < 0 or counter > MAX_SAFE_INTEGER):
            raise RuntimeUnavailable("Runtime run usage is invalid")
        result[key] = int(counter)
    return result


def _validate_security_summary(value: Any) -> dict:
    if not isinstance(value, dict):
        raise RuntimeUnavailable("Runtime security summary is invalid")
    profile_id = value.get("activePermissionProfile")
    if (profile_id is not None and
            (not isinstance(profile_id, str) or not profile_id or len(profile_id) > 128
             or any(ord(char) < 33 or char.isspace() for char in profile_id)
             or "/" in profile_id or "\\" in profile_id)):
        raise RuntimeUnavailable("Runtime security summary is invalid")
    approval = value.get("approvalPolicy")
    reviewer = value.get("approvalsReviewer")
    provenance = value.get("provenance")
    if (approval not in SECURITY_APPROVAL_CATEGORIES
            or reviewer not in SECURITY_REVIEWER_CATEGORIES
            or provenance not in SECURITY_PROVENANCE):
        raise RuntimeUnavailable("Runtime security summary is invalid")
    return {"activePermissionProfile": profile_id, "approvalPolicy": approval,
            "approvalsReviewer": reviewer, "provenance": provenance}


def _validate_security_binding(value: Any) -> dict:
    if not isinstance(value, dict):
        raise RuntimeUnavailable("Runtime security binding is invalid")
    source = value.get("source")
    if source == "profile":
        profile = value.get("profile")
        if (not isinstance(profile, dict) or not isinstance(profile.get("id"), str)
                or not profile["id"] or len(profile["id"]) > 100
                or not isinstance(profile.get("revision"), str)
                or not profile["revision"] or len(profile["revision"]) > 100):
            raise RuntimeUnavailable("Runtime security binding is invalid")
        return {"source": "profile", "profile": {
            "id": profile["id"], "revision": profile["revision"]}}
    if source != "runtime-config":
        raise RuntimeUnavailable("Runtime security binding is invalid")
    revision = value.get("revision")
    permission_revision = value.get("permissionRevision", "")
    approval_revision = value.get("approvalRevision", "")
    if (not isinstance(revision, str) or not revision or len(revision) > 100
            or not isinstance(permission_revision, str) or len(permission_revision) > 100
            or not isinstance(approval_revision, str) or len(approval_revision) > 100):
        raise RuntimeUnavailable("Runtime security binding is invalid")
    result = {"source": "runtime-config", "revision": revision,
              "permissionRevision": permission_revision,
              "approvalRevision": approval_revision,
              "resolvedSummary": _validate_security_summary(value.get("resolvedSummary"))}
    reason = value.get("replacementReason")
    if reason is not None:
        if reason not in SECURITY_REPLACEMENT_REASONS:
            raise RuntimeUnavailable("Runtime conversation replacement reason is invalid")
        replaced = value.get("replacedConversationId")
        if not isinstance(replaced, str) or not replaced or len(replaced) > 200:
            raise RuntimeUnavailable("Runtime conversation replacement is invalid")
        result["replacementReason"] = reason
        result["replacedConversationId"] = replaced
    return result


@dataclass(frozen=True)
class Descriptor:
    runtime_id: str
    display_name: str
    adapter_version: str
    native_version: str
    instance_id: str
    features: dict[str, int]
    release: dict | None = None
    release_status: str = "missing"

    def __post_init__(self) -> None:
        # Bounded release-observation state. `release` is a validated
        # identity or None; raw validation exceptions never escape.
        # Direct constructions without an explicit status infer it so
        # existing call sites stay correct: present release => valid,
        # absent release => missing.
        allowed = {"valid", "missing", "invalid", "unsupported"}
        status = self.release_status
        if status not in allowed:
            object.__setattr__(self, "release_status", "missing")
            status = "missing"
        if self.release is not None and status == "missing":
            object.__setattr__(self, "release_status", "valid")
        elif self.release is None and status == "valid":
            object.__setattr__(self, "release_status", "missing")

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
        # Runtime release metadata is optional at the generic Runtime
        # Protocol layer and is decoupled from protocol compatibility.
        # Protocol-major, core-feature, and runtime-identity validation stay
        # strict, but a present malformed or unsupported `release` object
        # never makes an otherwise valid descriptor unavailable. It is
        # observed as degraded metadata (invalid/unsupported) while the
        # descriptor stays usable. A missing release likewise stays
        # protocol-usable; managed-deployment support for first-party builds
        # is classified separately, beginning at 0.1.0. Normal runtime
        # operations are never blocked solely by release metadata.
        release: dict | None = None
        release_status = "missing"
        if "release" in value:
            from .release import ReleaseError, validate_release
            try:
                release = validate_release(value.get("release"))
                release_status = "valid"
            except ReleaseError as exc:
                release = None
                if getattr(exc, "kind", "invalid") == "unsupported":
                    release_status = "unsupported"
                else:
                    release_status = "invalid"
        return cls(runtime_id=expected_runtime,
                   display_name=str(runtime.get("displayName") or expected_runtime)[:120],
                   adapter_version=str(runtime.get("adapterVersion") or "")[:80],
                   native_version=str(runtime.get("nativeVersion") or "")[:80],
                   instance_id=instance_id,
                   features={name: int(version) for name, version in features.items()
                             if name in ALL_FEATURES},
                   release=release,
                   release_status=release_status)

    def supports(self, feature: str) -> bool:
        return self.features.get(feature) == 1


class HttpRuntimeAdapter:
    """One strict HTTP client for any conforming private runtime adapter."""

    def __init__(self, adapter_id: str, runtime_type: str, base_url: str,
                 token: str, *, timeout: float = 30):
        if (not isinstance(adapter_id, str)
                or not re.fullmatch(r"adapter_[0-9a-f]{24}", adapter_id)):
            raise BridgeError("Invalid adapter id", "invalid_arguments")
        if runtime_type not in {"pi", "codex"}:
            raise BridgeError("Invalid runtime type", "invalid_arguments")
        parsed = urllib.parse.urlparse(base_url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username:
            raise BridgeError("Invalid runtime adapter URL", "invalid_arguments")
        if not isinstance(token, str) or not token:
            raise BridgeError("Runtime adapter token is required", "invalid_arguments")
        self.adapter_id = adapter_id
        self.runtime_type = runtime_type
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = min(max(float(timeout), 0.5), 60.0)

    def _scrub_secret(self, value: Any) -> Any:
        """Remove this connection's credential from every remote string value."""
        if isinstance(value, str):
            return redact(value.replace(self.token, "[REDACTED_SECRET]"))[0]
        if isinstance(value, list):
            return [self._scrub_secret(item) for item in value]
        if isinstance(value, dict):
            return {self._scrub_secret(key) if isinstance(key, str) else key:
                    self._scrub_secret(item) for key, item in value.items()}
        return value

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
                return self._scrub_secret(value)
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
            detail = redact(detail.replace(self.token, "[REDACTED_SECRET]"))[0]
            code = code.replace(self.token, "redacted")
            if not re.fullmatch(r"[a-z][a-z0-9_]{0,79}", code):
                code = "runtime_rejected"
            if exc.code == 501:
                raise RuntimeUnsupported(detail or "Runtime feature is unsupported") from None
            if exc.code in (400, 404, 409, 412, 422):
                raise RuntimeRejected(detail or "Runtime rejected the request",
                                      code=code, status=exc.code) from None
            raise RuntimeUnavailable(detail or "Runtime adapter returned an error") from None
        except (urllib.error.URLError, socket.timeout, TimeoutError, OSError):
            raise RuntimeUnavailable("Runtime adapter is unavailable") from None
        except (ValueError, UnicodeError):
            raise RuntimeUnavailable("Runtime returned invalid JSON") from None

    def descriptor(self) -> Descriptor:
        return Descriptor.parse(self._request("GET", "/v1/descriptor", timeout=3),
                                expected_runtime=self.runtime_type)

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

    def profile_catalog(self, workspace_id: str | None = None,
                        directory: str | None = None, *,
                        fresh: bool = False) -> dict:
        if (workspace_id is None) != (directory is None):
            raise BridgeError("Workspace ID and directory must be supplied together",
                              "invalid_arguments")
        if workspace_id is not None and (
                not isinstance(workspace_id, str) or not workspace_id
                or len(workspace_id) > 100):
            raise BridgeError("Invalid workspace ID", "invalid_arguments")
        if directory is not None and (
                not isinstance(directory, str) or not directory or len(directory) > 4096):
            raise BridgeError("Invalid workspace directory", "invalid_arguments")
        query = None
        if workspace_id is not None:
            query = {"workspaceId": workspace_id, "directory": directory,
                     "fresh": "1" if fresh else None}
        value = self._request("GET", "/v1/profiles", query=query)
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
            item = {"id": row["id"], "revision": row["revision"],
                           "config": config, "mutable": row.get("mutable") is True,
                           "enforcement": [item[:80] for item in enforcement[:20]
                                           if isinstance(item, str)]}
            definition_revision = row.get("definitionRevision")
            if (isinstance(definition_revision, str)
                    and 1 <= len(definition_revision) <= 100):
                item["definitionRevision"] = definition_revision
            if isinstance(row.get("available"), bool):
                item["available"] = row["available"]
            result.append(item)
        permission_profiles = value.get("permissionProfiles")
        if not isinstance(permission_profiles, list):
            permission_profiles = []
        choices = []
        for row in permission_profiles[:500]:
            if (not isinstance(row, dict) or not isinstance(row.get("id"), str)
                    or not row["id"] or len(row["id"]) > 128
                    or not isinstance(row.get("allowed"), bool)):
                continue
            description = row.get("description")
            choices.append({"id": row["id"], "allowed": row["allowed"],
                            "description": (redact(str(description)[:240])[0]
                                            if isinstance(description, str) else "")})
        catalog = {"profiles": result, "permissionProfiles": choices}
        runtime_config = value.get("runtimeConfig")
        if runtime_config is not None:
            if (not isinstance(runtime_config, dict)
                    or not isinstance(runtime_config.get("supported"), bool)
                    or not isinstance(runtime_config.get("available"), bool)
                    or runtime_config.get("status") not in {"ready", "unavailable"}
                    or not isinstance(runtime_config.get("revision"), str)
                    or not runtime_config["revision"]
                    or len(runtime_config["revision"]) > 100):
                raise RuntimeUnavailable("Runtime config security state is invalid")
            catalog["runtimeConfig"] = {
                "supported": runtime_config["supported"],
                "available": runtime_config["available"],
                "status": runtime_config["status"],
                "revision": runtime_config["revision"],
                "resolvedSummary": _validate_security_summary(
                    runtime_config.get("resolvedSummary")),
            }
        return catalog

    def profiles(self, workspace_id: str | None = None,
                 directory: str | None = None, *, fresh: bool = False) -> list[dict]:
        return self.profile_catalog(workspace_id, directory,
                                    fresh=fresh)["profiles"]

    def save_profile(self, profile_id: str, config: dict,
                     expected_revision: str | None) -> dict:
        return self._request("POST", "/v1/profiles", body={
            "id": profile_id, "config": config,
            "expectedRevision": expected_revision})

    def delete_profile(self, profile_id: str) -> dict:
        return self._request("DELETE", "/v1/profiles/" +
                             _identifier(profile_id, "profile id"))

    def create_conversation(self, payload: dict) -> dict:
        value = self._request("POST", "/v1/conversations", body=payload)
        if not isinstance(value, dict):
            raise RuntimeUnavailable("Runtime conversation binding is invalid")
        if "securityBinding" in value:
            value = {**value, "securityBinding": _validate_security_binding(
                value.get("securityBinding"))}
        return value

    def conversation(self, conversation_id: str) -> dict:
        value = self._request("GET", f"/v1/conversations/{_identifier(conversation_id, 'conversation id')}")
        if not isinstance(value, dict):
            raise RuntimeUnavailable("Runtime conversation binding is invalid")
        if "securityBinding" in value:
            value = {**value, "securityBinding": _validate_security_binding(
                value.get("securityBinding"))}
        return value

    def rebind_conversation(self, conversation_id: str, security_binding: dict) -> dict:
        """Rebind an idle profile conversation to the requested named profile.

        Optional Runtime Protocol v1 operation behind ``securityRebind``.
        Only ``{source:'profile', profile:{id,revision}}`` is accepted; no
        arbitrary payload is forwarded. The adapter must prove the same
        runtime conversation is idle under the requested binding.
        """
        ident = _identifier(conversation_id, "conversation id")
        if (not isinstance(security_binding, dict)
                or security_binding.get("source") != "profile"
                or not isinstance(security_binding.get("profile"), dict)
                or not isinstance(security_binding["profile"].get("id"), str)
                or not security_binding["profile"]["id"]
                or len(security_binding["profile"]["id"]) > 100
                or not isinstance(security_binding["profile"].get("revision"), str)
                or not security_binding["profile"]["revision"]
                or len(security_binding["profile"]["revision"]) > 100
                or set(security_binding) != {"source", "profile"}
                or set(security_binding["profile"]) != {"id", "revision"}):
            raise BridgeError("Invalid security rebind binding", "invalid_arguments")
        value = self._request(
            "POST", f"/v1/conversations/{ident}/security",
            body={"securityBinding": security_binding})
        if not isinstance(value, dict):
            raise RuntimeUnavailable("Runtime conversation binding is invalid")
        if "securityBinding" in value:
            value = {**value, "securityBinding": _validate_security_binding(
                value.get("securityBinding"))}
        return value

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
