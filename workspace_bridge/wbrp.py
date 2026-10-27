"""Bounded client and validation for the agent resource protocol.

This module knows only Bridge resource names. Native Pi/Codex wire formats stay
inside their adapters. No adapter endpoint accepts an arbitrary command.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from .plugin_protocol import to_rpc
from .runtime import RuntimeRejected, RuntimeUnavailable, RuntimeUnsupported
from .security import BridgeError, redact

CORE_FEATURES = frozenset({"models", "conversations", "runs", "activities", "interactions"})
OPTIONAL_FEATURES = frozenset({"events", "steering", "queueing", "inputControls", "imageInput",
                               "usageLimits"})
ALL_FEATURES = CORE_FEATURES | OPTIONAL_FEATURES
RUN_PHASES = frozenset({"starting", "active", "terminal"})
ACTIVE_STATES = frozenset({"running", "waiting_interaction"})
OUTCOMES = frozenset({"succeeded", "failed", "cancelled", "interrupted", "orphaned"})
SECURITY_PROVENANCE = frozenset({"named-profile", "implicit/default", "legacy-sandbox"})
SECURITY_APPROVAL_CATEGORIES = frozenset({
    "untrusted", "on-request", "never", "granular", "default", "other"})
SECURITY_REVIEWER_CATEGORIES = frozenset({
    "user", "auto_review", "guardian_subagent", "default", "other"})
MAX_SAFE_INTEGER = 9007199254740991
RUN_USAGE_FIELDS = frozenset({
    "inputTokens", "cachedInputTokens", "cacheWriteInputTokens",
    "outputTokens", "reasoningOutputTokens", "totalTokens"})
# Account usage limits are an account-level snapshot, separate from the
# run-scoped token usage above and from adapter health/status.
USAGE_BUCKETS_MAX = 12
USAGE_WINDOWS_MAX = 4
USAGE_WINDOW_MINS_MAX = 10_000_000


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
    if "inputMessages" in value:
        value = {**value, "inputMessages": validate_input_messages(value["inputMessages"])}
    return value


def validate_input_messages(value: Any) -> list[dict]:
    """Keep only bounded, text-free input receipts at the runtime boundary."""
    if not isinstance(value, list) or len(value) > 50:
        raise RuntimeUnavailable("Runtime input receipts are invalid")
    result = []
    seen = set()
    for row in value:
        if (not isinstance(row, dict)
                or not isinstance(row.get("requestId"), str)
                or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}", row["requestId"]) is None
                or row["requestId"] in seen
                or not isinstance(row.get("mode"), str)
                or row["mode"] not in {"steer", "queue"}
                or not isinstance(row.get("status"), str)
                or row["status"] not in {"queued", "delivered", "discarded", "unknown"}):
            raise RuntimeUnavailable("Runtime input receipts are invalid")
        seen.add(row["requestId"])
        result.append({key: row[key] for key in ("requestId", "mode", "status")})
    return result


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


def _usage_text(value: Any, limit: int) -> str | None:
    if isinstance(value, str) and value and len(value) <= limit:
        return value
    return None


def _usage_int(value: Any, minimum: int, maximum: int) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if minimum <= value <= maximum else None


def _usage_limit_window(value: Any) -> dict | None:
    """Validate one normalized quota window; unusable windows are dropped.

    A missing or invalid ``usedPercent`` means the window carries no
    observable remaining quota, so it is omitted entirely (never rendered
    as 0% remaining). ``remainingPercent`` is always derived here as
    clamp(100 - usedPercent) for display/summary only; ``usedPercent``
    stays preserved and null duration/reset stay null. Unknown fields are
    ignored, never forwarded.
    """
    if not isinstance(value, dict):
        return None
    used = _usage_int(value.get("usedPercent"), 0, 1000)
    if used is None:
        return None
    return {
        "usedPercent": used,
        "remainingPercent": max(0, min(100, 100 - used)),
        "windowDurationMins": _usage_int(value.get("windowDurationMins"),
                                         0, USAGE_WINDOW_MINS_MAX),
        "resetsAt": _usage_int(value.get("resetsAt"), 0, MAX_SAFE_INTEGER),
    }


def _usage_limit_bucket(value: Any) -> dict | None:
    """Validate one normalized quota bucket.

    The bucket carries bounded snapshot metadata plus a nested ``windows``
    list (primary then secondary when present, normalized by the adapter).
    A bucket with neither valid windows nor usable metadata is dropped;
    remaining-quota display always requires at least one valid window.
    Unknown native fields are ignored, never forwarded.
    """
    if not isinstance(value, dict):
        return None
    windows_raw = value.get("windows")
    windows = []
    if isinstance(windows_raw, list):
        for row in windows_raw[:USAGE_WINDOWS_MAX]:
            window = _usage_limit_window(row)
            if window is not None:
                windows.append(window)
    spend_control = value.get("spendControlReached")
    bucket = {
        "limitId": _usage_text(value.get("limitId"), 200),
        "limitName": _usage_text(value.get("limitName"), 120),
        "planType": _usage_text(value.get("planType"), 60),
        "rateLimitReachedType": _usage_text(value.get("rateLimitReachedType"), 60),
        "spendControlReached": (spend_control
                                if isinstance(spend_control, bool) else None),
        "credits": None,
        "individualLimit": None,
        "windows": windows,
    }
    credits = value.get("credits")
    if (isinstance(credits, dict)
            and isinstance(credits.get("hasCredits"), bool)
            and isinstance(credits.get("unlimited"), bool)):
        bucket["credits"] = {
            "hasCredits": credits["hasCredits"],
            "unlimited": credits["unlimited"],
            "balance": _usage_text(credits.get("balance"), 200)}
    individual = value.get("individualLimit")
    if isinstance(individual, dict):
        limit = _usage_text(individual.get("limit"), 60)
        used_text = _usage_text(individual.get("used"), 60)
        remaining = _usage_int(individual.get("remainingPercent"), 0, 100)
        if limit is not None and used_text is not None and remaining is not None:
            bucket["individualLimit"] = {
                "limit": limit, "used": used_text,
                "remainingPercent": remaining,
                "resetsAt": _usage_int(individual.get("resetsAt"),
                                       0, MAX_SAFE_INTEGER)}
    metadata = (bucket["credits"] is not None
                or bucket["individualLimit"] is not None
                or bucket["spendControlReached"] is not None
                or bucket["rateLimitReachedType"] is not None)
    if not windows and not metadata:
        return None
    return bucket


def validate_usage_limits(value: Any) -> dict:
    """Validate the optional additive account usage-limits snapshot.

    The contract is runtime-neutral and never invents data: a successful
    read reports ``available`` plus bounded quota buckets whose nested
    ``windows`` carry the remaining-quota observations. Malformed buckets
    and windows are dropped rather than guessed at; a structurally
    invalid payload fails closed as unavailable runtime data.
    """
    if not isinstance(value, dict):
        raise RuntimeUnavailable("Runtime usage limits are invalid")
    available = value.get("available")
    ordinary = value.get("ordinaryUsageAllowed")
    if (not isinstance(available, bool)
            or (ordinary is not None and not isinstance(ordinary, bool))):
        raise RuntimeUnavailable("Runtime usage limits are invalid")
    rows = value.get("buckets")
    if not isinstance(rows, list):
        raise RuntimeUnavailable("Runtime usage limits are invalid")
    buckets = []
    for row in rows[:USAGE_BUCKETS_MAX]:
        bucket = _usage_limit_bucket(row)
        if bucket is not None:
            buckets.append(bucket)
    return {"available": available, "ordinaryUsageAllowed": ordinary,
            "buckets": buckets}


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
    """Validate the binding a plugin echoes for a conversation or run.

    ``profile`` names the Bridge profile the plugin compiled and enforces;
    ``native`` means the agent's own configuration governs the run.
    """
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
    if source != "native":
        raise RuntimeUnavailable("Runtime security binding is invalid")
    result: dict = {"source": "native"}
    if value.get("resolvedSummary") is not None:
        result["resolvedSummary"] = _validate_security_summary(value["resolvedSummary"])
    return result


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


class RuntimeClient:
    """One strict client for any conforming agent plugin.

    Every method validates what the plugin returns. The transport carries
    named Agent Protocol v2 requests: ``call(method, params, timeout)`` and
    ``events(after, wait_ms)``. On a Node it is the supervised plugin
    process; tests may use an in-process loopback.
    """

    def __init__(self, adapter_id: str, runtime_type: str, transport: Any, *,
                 timeout: float = 30):
        if (not isinstance(adapter_id, str)
                or not re.fullmatch(r"adapter_[0-9a-f]{24}", adapter_id)):
            raise BridgeError("Invalid adapter id", "invalid_arguments")
        if runtime_type not in {"pi", "codex", "claude", "opencode"}:
            raise BridgeError("Invalid runtime type", "invalid_arguments")
        self.adapter_id = adapter_id
        self.runtime_type = runtime_type
        self.transport = transport
        self.timeout = min(max(float(timeout), 0.5), 60.0)

    @staticmethod
    def _scrub_secret(value: Any) -> Any:
        """Redact credential-shaped text from every string a plugin returns."""
        if isinstance(value, str):
            return redact(value)[0]
        if isinstance(value, list):
            return [RuntimeClient._scrub_secret(item) for item in value]
        if isinstance(value, dict):
            return {key: RuntimeClient._scrub_secret(item) for key, item in value.items()}
        return value

    @staticmethod
    def _check_request_size(method: str, path: str, body: dict | None) -> None:
        """Bound one request body: 12 MiB for a run start with images, else 512 KiB."""
        raw_body = None if body is None else json.dumps(body, separators=(",", ":")).encode()
        if raw_body is not None and len(raw_body) > (12 * 1024 * 1024 if method == "POST" and path.endswith("/runs") and isinstance(body, dict) and any(isinstance(item, dict) and item.get("type") == "image" for item in body.get("input", [])) else 512 * 1024):
            raise BridgeError("Adapter request is too large", "invalid_arguments")

    def _request(self, method: str, path: str, *, body: dict | None = None,
                 query: dict | None = None, timeout: float | None = None) -> dict:
        if not path.startswith("/v1/"):
            raise BridgeError("Invalid adapter path", "invalid_arguments")
        self._check_request_size(method, path, body)
        name, params = to_rpc(method, path, query, body)
        if name == "events":
            value = self.transport.events(int(params.get("after", 0)),
                                          int(params.get("waitMs", 0)))
        else:
            value = self.transport.call(name, params, timeout or self.timeout)
        if not isinstance(value, dict):
            raise RuntimeUnavailable("Runtime response is invalid")
        return self._scrub_secret(value)

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

    def _run_input(self, mode: str, run_id: str, input_items: list[dict], request_id: str) -> dict:
        ident = _identifier(run_id, "run id")
        if (not isinstance(request_id, str)
                or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}", request_id) is None
                or not isinstance(input_items, list) or not 1 <= len(input_items) <= 10
                or any(not isinstance(item, dict) or set(item) != {"type", "text"}
                       or item.get("type") != "text" or not isinstance(item.get("text"), str)
                       or not item["text"].strip() or len(item["text"]) > 60000
                       for item in input_items)
                or sum(len(item["text"]) for item in input_items) > 60000):
            raise BridgeError("Invalid run input", "invalid_arguments")
        feature = "steering" if mode == "steer" else "queueing"
        descriptor = self.descriptor()
        if not descriptor.supports(feature) or not descriptor.supports("inputControls"):
            raise RuntimeUnsupported(f"Runtime does not support {feature}")
        return validate_run_state(self._request("POST", f"/v1/runs/{ident}/{mode}",
            body={"expectedRunId": ident, "requestId": request_id, "input": input_items}))

    def steer(self, run_id: str, input_items: list[dict], request_id: str) -> dict:
        return self._run_input("steer", run_id, input_items, request_id)

    def queue(self, run_id: str, input_items: list[dict], request_id: str) -> dict:
        return self._run_input("queue", run_id, input_items, request_id)

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

    def usage_limits(self) -> dict:
        """Read the optional account usage-limits snapshot (usageLimits).

        Account quota belongs to the AdapterInstance/account, so the call
        needs no workspace authority context. Unsupported adapters fail as
        unsupported, never as unhealthy.
        """
        if not self.descriptor().supports("usageLimits"):
            raise RuntimeUnsupported("Runtime does not support usage limits")
        return validate_usage_limits(self._request("GET", "/v1/usage-limits"))

    def events(self, *, after: int, wait_ms: int = 0) -> dict:
        if not self.descriptor().supports("events"):
            raise RuntimeUnsupported("Runtime does not support events")
        return self._request("GET", "/v1/events",
                             query={"after": max(0, after), "waitMs": min(max(wait_ms, 0), 25000)},
                             timeout=min(self.timeout, wait_ms / 1000 + 5))
