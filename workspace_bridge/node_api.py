"""Private Node request handling, shared by every Bridge-to-Node transport.

The same ``handle`` serves requests that arrive over a paired Node's
outbound connection and the Node embedded in the Bridge process.
Authentication belongs to each transport; the handler only routes and
bounds requests.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

from .security import BridgeError
from .media import MAX_IMAGE_REQUEST_BYTES

MAX_REQUEST = 1024 * 1024
MAX_RUNTIME_REQUEST = 2 * 1024 * 1024
# 1 MiB of patch text can grow to about 6 MiB once JSON escapes it.
MAX_PATCH_REQUEST = 8 * 1024 * 1024
MAX_RESPONSE = 20 * 1024 * 1024


def request_limit(method: str, path: str) -> int:
    """Largest request body accepted for one Node route."""
    if method == "POST" and path == "/v1/workspaces/upload_image":
        return MAX_IMAGE_REQUEST_BYTES
    if method == "POST" and path == "/v1/workspaces/apply_patch":
        return MAX_PATCH_REQUEST
    if method == "POST" and path.startswith("/v1/runtime/"):
        return MAX_RUNTIME_REQUEST
    return MAX_REQUEST


def _body(raw: bytes, limit: int) -> dict:
    if len(raw) > limit:
        raise BridgeError("Node request is too large", "too_large")
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError):
        raise BridgeError("Invalid JSON request", "invalid_arguments") from None
    if not isinstance(value, dict):
        raise BridgeError("JSON object required", "invalid_arguments")
    return value


def encode(value, status_code: int = 200) -> tuple[int, bytes]:
    """Serialize one bounded JSON response body."""
    try:
        raw = json.dumps(value, ensure_ascii=False, separators=(",", ":"),
                         allow_nan=False).encode("utf-8")
    except (TypeError, ValueError):
        return 500, b'{"error":"Node response could not be serialized","code":"node_internal_error"}'
    if len(raw) > MAX_RESPONSE:
        return 413, b'{"error":"Node response exceeds the payload limit","code":"output_limit"}'
    return status_code, raw


def handle(service, method: str, path: str, raw: bytes) -> tuple[int, bytes]:
    """Route one authenticated Node request; blocking, so call it from a worker thread."""
    try:
        status, value = _route(service, method, path, raw)
        return encode(value, status)
    except BridgeError as exc:
        return encode({"error": str(exc), "code": exc.code}, 400)
    except Exception:
        return encode({"error": "Node operation failed", "code": "node_internal_error"}, 500)


def _route(service, method: str, path: str, raw: bytes) -> tuple[int, object]:
    limit = request_limit(method, path)
    if path == "/v1/status" and method == "GET":
        return 200, service.status()
    if path == "/v1/workspaces/validate" and method == "POST":
        body = _body(raw, limit)
        return 200, service.validate_root(body.get("root"), body.get("excludes", []))
    if path == "/v1/workspaces/bind" and method == "POST":
        return 200, service.register_workspace(_body(raw, limit).get("workspace"))
    if path.startswith("/v1/workspaces/") and path.endswith("/configure") and method == "POST":
        pieces = [item for item in path.split("/") if item]
        if len(pieces) != 4:
            return 404, {"error": "Not found"}
        body = _body(raw, limit)
        return 200, service.configure_workspace(
            pieces[2], body.get("excludes"), body.get("write_scope"))
    if path.startswith("/v1/workspaces/") and method == "POST":
        pieces = [item for item in path.split("/") if item]
        if len(pieces) != 3:
            return 404, {"error": "Not found"}
        return 200, service.workspace_call(pieces[2], _body(raw, limit))
    if path == "/v1/adapters" and method == "GET":
        return 200, service.list_adapters()
    if path == "/v1/plugins" and method == "GET":
        return 200, {"plugins": [{key: m[key] for key in ("id", "runtime_type", "version",
                                                          "description", "settings", "features")}
                                 for m in service.manifests().values()]}
    if path == "/v1/adapters" and method == "POST":
        return 201, service.save_adapter(_body(raw, limit))
    if path == "/v1/adapters/test" and method == "POST":
        return 200, service.test_adapter(_body(raw, limit))
    if path.startswith("/v1/adapters/"):
        pieces = [item for item in path.split("/") if item]
        if len(pieces) < 3:
            return 404, {"error": "Unknown adapter route"}
        adapter_id = pieces[2]
        if len(pieces) == 3 and method == "GET":
            # Normal reads are write-only for runtime credentials.
            from .node_service import _public_adapter
            return 200, _public_adapter(service.adapter(adapter_id))
        if len(pieces) == 3 and method == "PATCH":
            return 200, service.save_adapter(_body(raw, limit), adapter_id)
        if len(pieces) == 3 and method == "DELETE":
            return 200, service.delete_adapter(adapter_id)
        if len(pieces) == 4 and pieces[3] == "test" and method == "POST":
            # Saved-adapter probes are explicit configuration checks;
            # runtime calls use the revision-pinned envelope below.
            # Honor supplied form values so edits test the new input:
            # blank token reuses the saved token (handled by service).
            try:
                try:
                    body = _body(raw, limit)
                except BridgeError:
                    body = {}
                payload = {key: body[key] for key in
                           ("name", "runtime_type", "base_url", "token", "enabled")
                           if key in body}
                payload["adapter_id"] = adapter_id
                return 200, service.test_adapter(payload)
            except BridgeError as exc:
                return 200, {"success": False, "code": exc.code,
                             "message": "Connection could not be verified."}
        return 404, {"error": "Unknown adapter route"}
    if path.startswith("/v1/runtime/") and method == "POST":
        pieces = [item for item in path.split("/") if item]
        if len(pieces) != 4:
            return 404, {"error": "Unknown runtime operation"}
        return 200, service.runtime_call(pieces[2], pieces[3], _body(raw, limit))
    return 404, {"error": "Not found"}


class InProcessTransport:
    """Calls one Node's handler directly: the embedded Node, and tests."""

    def __init__(self, service):
        self.service = service

    def request(self, method, path, content=None, headers=None):
        status, raw = handle(self.service, method, path, content or b"")
        return SimpleNamespace(status_code=status, content=raw)
