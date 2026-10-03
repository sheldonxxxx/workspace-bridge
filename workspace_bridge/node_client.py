"""Bridge-side authenticated client and Runtime Protocol proxy for a Node."""
from __future__ import annotations

import json
import re
import socket
import urllib.error
import urllib.parse
import urllib.request

from .media import ImageReadResult
from .runtime import RuntimeRejected, RuntimeUnavailable, RuntimeUnsupported
from .security import BridgeError, redact
from .wbrp import Descriptor

MAX_RESPONSE = 20 * 1024 * 1024
ADAPTER_REVISION_RE = re.compile(r"^rev_[0-9a-f]{32}$")
NODE_PROTOCOL_MAJOR = 1


def validate_node_status(value: object) -> dict:
    """Require a bounded valid Node status object for healthy Bridge use.

    A healthy Node must return a dict with ``status == \"ok\"`` and Node
    Protocol exactly v1 (``protocol == 1``). Product/build/release metadata
    skew never affects reachability: malformed or missing optional release
    metadata is ignored here (diagnostics/staged rollout observe it
    separately). A protocol mismatch raises ``node_protocol_error`` so
    affected routes become unavailable/incompatible without masking skew
    as a generic outage.
    """
    if not isinstance(value, dict):
        raise BridgeError("Node protocol is incompatible", "node_protocol_error")
    if value.get("status") != "ok":
        raise BridgeError("Node protocol is incompatible", "node_protocol_error")
    protocol = value.get("protocol")
    if isinstance(protocol, bool) or protocol != NODE_PROTOCOL_MAJOR:
        raise BridgeError("Node protocol is incompatible", "node_protocol_error")
    return value


def _node_http_error(status: int, raw: bytes, node_token: str) -> BridgeError:
    try:
        detail = json.loads(raw[:16384])
        code = detail.get("code", "node_unavailable") if isinstance(detail, dict) else "node_unavailable"
        message = detail.get("error", "Node rejected the operation") if isinstance(detail, dict) else "Node rejected the operation"
    except (ValueError, UnicodeError):
        code = "node_unavailable"
        message = "Node is unavailable or rejected the request"
    if status == 401:
        return BridgeError("Node credential was rejected", "node_auth_failed")
    if not isinstance(code, str) or not code.replace("_", "").isalnum() or len(code) > 64:
        code = "node_unavailable"
    if not isinstance(message, str):
        message = "Node rejected the operation"
    message, _ = redact(message[:1000])
    if node_token:
        message = message.replace(node_token, "[REDACTED_SECRET]")
    return BridgeError(message[:500] or "Node rejected the operation", code)


class NodeClient:
    def __init__(self, node: dict, *, timeout: float = 30, transport=None):
        self.node_id = node["id"]
        self.name = node["name"]
        self.base_url = node["base_url"].rstrip("/")
        self.token = node["token"]
        self.revision = node["revision"]
        self.enabled = bool(node["enabled"])
        self.timeout = min(max(float(timeout), 0.5), 60)
        self.transport = transport
        if not self.enabled:
            raise BridgeError("Node is disabled", "node_disabled")

    def _request(self, method: str, path: str, body: dict | None = None,
                 *, timeout: float | None = None):
        if not path.startswith("/v1/"):
            raise BridgeError("Invalid Node API path", "invalid_arguments")
        # Escape non-ASCII in the wire envelope so malformed surrogate input
        # reaches the Node validator as JSON instead of failing in Bridge's
        # transport serializer.
        raw_body = None if body is None else json.dumps(
            body, separators=(",", ":"), ensure_ascii=True).encode("ascii")
        if raw_body is not None and len(raw_body) > 2 * 1024 * 1024:
            raise BridgeError("Node request exceeds the payload limit", "too_large")
        request = urllib.request.Request(self.base_url + path, data=raw_body,
            headers={"Accept": "application/json", "X-Node-Token": self.token,
                     **({"Content-Type": "application/json"} if raw_body is not None else {})},
            method=method)
        try:
            if self.transport is not None:
                response = self.transport.request(method, path, content=raw_body,
                    headers={"Accept": "application/json", "X-Node-Token": self.token,
                             **({"Content-Type": "application/json"} if raw_body is not None else {})})
                raw = response.content
                if len(raw) > MAX_RESPONSE:
                    raise BridgeError("Node response exceeds the payload limit", "output_limit")
                if response.status_code >= 400:
                    raise _node_http_error(response.status_code, raw, self.token)
            else:
                with urllib.request.urlopen(request, timeout=timeout or self.timeout) as response:
                    raw = response.read(MAX_RESPONSE + 1)
                    if len(raw) > MAX_RESPONSE:
                        raise BridgeError("Node response exceeds the payload limit", "output_limit")
        except urllib.error.HTTPError as exc:
            raise _node_http_error(exc.code, exc.read(16384), self.token) from None
        except BridgeError:
            raise
        except (urllib.error.URLError, socket.timeout, TimeoutError, OSError):
            raise BridgeError("Node is unavailable", "node_unavailable") from None
        except (ValueError, UnicodeError):
            raise BridgeError("Node returned invalid JSON", "node_protocol_error") from None
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeError):
            raise BridgeError("Node returned invalid JSON", "node_protocol_error") from None
        return value

    def status(self) -> dict:
        # Central Node Protocol gate: status==ok and protocol==1 are
        # required for Bridge operations that treat the Node as healthy.
        # Release/product/build skew never affects reachability here.
        return validate_node_status(
            self._request("GET", "/v1/status", timeout=min(self.timeout, 5)))

    def validate_root(self, root: str, excludes: list[str] | None = None) -> dict:
        return self._request("POST", "/v1/workspaces/validate",
                             {"root": root, "excludes": excludes or []})

    def register_workspace(self, workspace: dict) -> dict:
        return self._request("POST", "/v1/workspaces/bind", {"workspace": workspace})

    def configure_workspace(self, workspace_id: str, excludes: list[str],
                            write_scope: str) -> dict:
        return self._request("POST", f"/v1/workspaces/{workspace_id}/configure",
                             {"excludes": excludes, "write_scope": write_scope})

    def workspace(self, operation: str, workspace: dict, arguments: dict):
        allowed = {"list_dir", "read_file", "glob", "grep_files", "write_file",
                   "edit_file", "git_status", "git_diff", "read_handoff_artifact",
                   "hash_files", "publish_handoff_artifacts"}
        if operation not in allowed:
            raise BridgeError("Unknown Node workspace operation", "invalid_arguments")
        result = self._request("POST", "/v1/workspaces/" + operation,
            {"workspace": {key: workspace[key] for key in
                            ("id", "root", "excludes", "write_scope") if key in workspace},
             "arguments": arguments})
        if isinstance(result, dict) and result.get("__image__") is True:
            import base64
            return ImageReadResult(result["metadata"], base64.b64decode(result["data"], validate=True),
                                   result["mime_type"])
        if operation == "read_handoff_artifact" and isinstance(result, dict):
            import base64
            return base64.b64decode(result["data"], validate=True)
        return result

    def list_adapters(self) -> dict:
        return self._request("GET", "/v1/adapters")

    def create_adapter(self, payload: dict) -> dict:
        return self._request("POST", "/v1/adapters", payload)

    def update_adapter(self, adapter_id: str, payload: dict) -> dict:
        return self._request("PATCH", "/v1/adapters/" + adapter_id, payload)

    def delete_adapter(self, adapter_id: str) -> dict:
        return self._request("DELETE", "/v1/adapters/" + adapter_id)

    def test_adapter_config(self, payload: dict) -> dict:
        return self._request("POST", "/v1/adapters/test", payload)

    def test_adapter(self, adapter_id: str, config: dict | None = None) -> dict:
        body: dict = {"adapter_id": adapter_id}
        if isinstance(config, dict):
            for key in ("name", "runtime_type", "base_url", "token", "enabled"):
                if key in config:
                    body[key] = config[key]
        return self._request("POST", "/v1/adapters/" + adapter_id + "/test", body)

    def runtime(self, adapter_id: str, operation: str, arguments: dict,
                workspace: dict | None = None, *, expected_revision: str):
        if (not isinstance(expected_revision, str)
                or not ADAPTER_REVISION_RE.fullmatch(expected_revision)):
            raise BridgeError("Invalid adapter revision", "invalid_arguments")
        if not isinstance(arguments, dict):
            raise BridgeError("Runtime arguments must be an object", "invalid_arguments")
        body = {"arguments": dict(arguments),
                "expected_adapter_revision": expected_revision}
        if workspace is not None:
            body["workspace"] = {key: workspace[key] for key in
                                  ("id", "root", "excludes", "write_scope") if key in workspace}
        return self._request("POST", f"/v1/runtime/{adapter_id}/{operation}", body)


class NodeRuntimeAdapterProxy:
    """Drop-in Runtime Protocol client that can only call its owning Node."""

    def __init__(self, node: NodeClient, adapter: dict, workspace: dict | None = None,
                 *, timeout: float = 30):
        self.node = node
        self.adapter_id = adapter["id"]
        self.runtime_type = adapter["runtime_type"]
        self.revision = adapter["revision"]
        self.workspace_context = workspace
        self.timeout = timeout

    def _call(self, operation: str, **arguments):
        try:
            return self.node.runtime(self.adapter_id, operation, arguments,
                                     self.workspace_context,
                                     expected_revision=self.revision)
        except BridgeError as exc:
            if exc.code in {"node_unavailable", "node_auth_failed", "node_protocol_error"}:
                raise RuntimeUnavailable("Runtime Node is unavailable") from None
            if exc.code == "runtime_unavailable":
                # Preserve the already bounded/redacted Node detail so the
                # sanitized native cause survives the Node boundary. Node
                # auth/protocol failures stay generic above.
                preserved, _ = redact(str(exc)[:500])
                raise RuntimeUnavailable(
                    preserved.strip() or "Runtime adapter is unavailable") from None
            if exc.code == "runtime_unsupported":
                raise RuntimeUnsupported("Runtime feature is unsupported") from None
            if exc.code == "runtime_rejected":
                raise RuntimeRejected("Runtime rejected the request") from None
            if exc.code in {"not_found", "binding_mismatch", "conversation_busy",
                            "interaction_stale", "model_unavailable", "model_not_enabled",
                            "security_rebind_unavailable", "profile_mismatch",
                            "profile_unavailable", "continuation_security_source_changed",
                            "continuation_security_rebind_unsupported"}:
                raise RuntimeRejected("Runtime rejected the request", code=exc.code) from None
            raise

    def descriptor(self) -> Descriptor:
        from .release import ReleaseError, validate_release
        value = self._call("descriptor")
        # Staged rollout: release metadata is decoupled from Runtime
        # Protocol compatibility. A present malformed/unsupported release
        # degrades update metadata only; the descriptor stays usable.
        release = None
        release_status = "missing"
        if isinstance(value, dict) and "release" in value:
            try:
                release = validate_release(value.get("release"))
                release_status = "valid"
            except ReleaseError as exc:
                release = None
                if getattr(exc, "kind", "invalid") == "unsupported":
                    release_status = "unsupported"
                else:
                    release_status = "invalid"
        return Descriptor(value["runtime_id"], value["display_name"],
                          value["adapter_version"], value["native_version"],
                          value["instance_id"], value["features"],
                          release=release, release_status=release_status)

    def models(self, workspace_id: str) -> list[dict]:
        return self._call("models", workspace_id=workspace_id)

    def profile_catalog(self, workspace_id: str | None = None,
                        directory: str | None = None, *, fresh: bool = False) -> dict:
        return self._call("profile_catalog", workspace_id=workspace_id,
                          directory=directory, fresh=fresh)

    def profiles(self, workspace_id: str | None = None,
                 directory: str | None = None, *, fresh: bool = False) -> list[dict]:
        return self._call("profiles", workspace_id=workspace_id,
                          directory=directory, fresh=fresh)

    def save_profile(self, profile_id: str, config: dict, expected_revision: str | None) -> dict:
        return self._call("save_profile", profile_id=profile_id, config=config,
                          expected_revision=expected_revision)

    def delete_profile(self, profile_id: str) -> dict:
        return self._call("delete_profile", profile_id=profile_id)

    def create_conversation(self, payload: dict) -> dict:
        return self._call("create_conversation", payload=payload)

    def conversation(self, conversation_id: str) -> dict:
        return self._call("conversation", conversation_id=conversation_id)

    def rebind_conversation(self, conversation_id: str, security_binding: dict) -> dict:
        return self._call("rebind_conversation", conversation_id=conversation_id,
                          security_binding=security_binding)

    def start_run(self, conversation_id: str, payload: dict) -> dict:
        return self._call("start_run", conversation_id=conversation_id, payload=payload)

    def find_run(self, conversation_id: str, client_run_id: str) -> dict:
        return self._call("find_run", conversation_id=conversation_id,
                          client_run_id=client_run_id)

    def run(self, run_id: str) -> dict:
        return self._call("run", run_id=run_id)

    def cancel(self, run_id: str) -> dict:
        return self._call("cancel", run_id=run_id)

    def steer(self, run_id: str, input_items: list[dict]) -> dict:
        return self._call("steer", run_id=run_id, input_items=input_items)

    def interactions(self, run_id: str) -> list[dict]:
        return self._call("interactions", run_id=run_id)

    def resolve(self, interaction_id: str, response: dict) -> dict:
        return self._call("resolve", interaction_id=interaction_id, response=response)

    def activities(self, run_id: str) -> list[dict]:
        return self._call("activities", run_id=run_id)

    def activity(self, activity_id: str) -> dict:
        return self._call("activity", activity_id=activity_id)

    def usage_limits(self) -> dict:
        return self._call("usage_limits")

    def events(self, *, after: int, wait_ms: int = 0) -> dict:
        return self._call("events", after=after, wait_ms=wait_ms)
