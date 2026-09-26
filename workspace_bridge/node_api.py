"""Authenticated private HTTP API for ``workspace-bridge-node``."""
from __future__ import annotations

import hashlib
import hmac
import json

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from .security import BridgeError, digest

MAX_REQUEST = 1024 * 1024
MAX_RESPONSE = 20 * 1024 * 1024


async def _body(request: Request, limit: int = MAX_REQUEST) -> dict:
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > limit:
            raise BridgeError("Node request is too large", "too_large")
        raw.extend(chunk)
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError):
        raise BridgeError("Invalid JSON request", "invalid_arguments") from None
    if not isinstance(value, dict):
        raise BridgeError("JSON object required", "invalid_arguments")
    return value


def _json(value, status_code: int = 200):
    try:
        raw = json.dumps(value, ensure_ascii=False, separators=(",", ":"),
                         allow_nan=False).encode("utf-8")
    except (TypeError, ValueError):
        return JSONResponse({"error": "Node response could not be serialized",
                             "code": "node_internal_error"}, 500)
    if len(raw) > MAX_RESPONSE:
        return JSONResponse({"error": "Node response exceeds the payload limit",
                             "code": "output_limit"}, 413)
    return Response(raw, status_code=status_code, media_type="application/json")


def make_node_api(service, token_hash: str):
    """Build the private Node app; the token itself is never a response field."""
    async def dispatch(request: Request):
        supplied = request.headers.get("x-node-token", "")
        if (not supplied or len(supplied) > 512 or
                not hmac.compare_digest(digest(supplied.encode()), token_hash)):
            return _json({"error": "Invalid Node credential"}, 401)
        path = request.url.path
        try:
            if path == "/v1/status" and request.method == "GET":
                return _json(service.status())
            if path == "/v1/workspaces/validate" and request.method == "POST":
                body = await _body(request)
                return _json(service.validate_root(body.get("root"), body.get("excludes", [])))
            if path == "/v1/workspaces/bind" and request.method == "POST":
                body = await _body(request)
                return _json(service.register_workspace(body.get("workspace")))
            if path.startswith("/v1/workspaces/") and path.endswith("/configure") and request.method == "POST":
                workspace_id = path.split("/")[-2]
                body = await _body(request)
                return _json(service.configure_workspace(
                    workspace_id, body.get("excludes"), body.get("write_scope")))
            if path.startswith("/v1/workspaces/") and request.method == "POST":
                operation = path.rsplit("/", 1)[-1]
                body = await _body(request)
                result = service.workspace_call(operation, body)
                return _json(result)
            if path == "/v1/adapters" and request.method == "GET":
                return _json(service.list_adapters())
            if path == "/v1/adapters" and request.method == "POST":
                return _json(service.save_adapter(await _body(request)), 201)
            if path == "/v1/adapters/test" and request.method == "POST":
                return _json(service.test_adapter(await _body(request)))
            if path.startswith("/v1/adapters/"):
                pieces = [item for item in path.split("/") if item]
                if len(pieces) < 3:
                    return _json({"error": "Unknown adapter route"}, 404)
                adapter_id = pieces[2]
                if len(pieces) == 3 and request.method == "GET":
                    # Normal reads are write-only for runtime credentials.
                    from .node_service import _public_adapter
                    return _json(_public_adapter(service.adapter(adapter_id)))
                if len(pieces) == 3 and request.method == "PATCH":
                    return _json(service.save_adapter(await _body(request), adapter_id))
                if len(pieces) == 3 and request.method == "DELETE":
                    return _json(service.delete_adapter(adapter_id))
                if len(pieces) == 4 and pieces[3] == "test" and request.method == "POST":
                    # Saved-adapter probes are explicit configuration checks;
                    # runtime calls use the revision-pinned envelope below.
                    # Honor supplied form values so edits test the new input:
                    # blank token reuses the saved token (handled by service).
                    try:
                        try:
                            body = await _body(request)
                        except BridgeError:
                            body = {}
                        if not isinstance(body, dict):
                            body = {}
                        payload = {key: body[key] for key in
                                   ("name", "runtime_type", "base_url", "token", "enabled")
                                   if key in body}
                        payload["adapter_id"] = adapter_id
                        return _json(service.test_adapter(payload))
                    except BridgeError as exc:
                        return _json({"success": False, "code": exc.code,
                                      "message": "Connection could not be verified."})
            if path.startswith("/v1/runtime/") and request.method == "POST":
                pieces = [item for item in path.split("/") if item]
                if len(pieces) != 4:
                    return _json({"error": "Unknown runtime operation"}, 404)
                body = await _body(request, 2 * 1024 * 1024)
                result = service.runtime_call(pieces[2], pieces[3], body)
                return _json(result)
            return _json({"error": "Not found"}, 404)
        except BridgeError as exc:
            return _json({"error": str(exc), "code": exc.code}, 400)
        except Exception:
            return _json({"error": "Node operation failed", "code": "node_internal_error"}, 500)

    return Starlette(routes=[
        Route("/v1/status", dispatch, methods=["GET"]),
        Route("/v1/workspaces/validate", dispatch, methods=["POST"]),
        Route("/v1/workspaces/bind", dispatch, methods=["POST"]),
        Route("/v1/workspaces/{workspace_id}/configure", dispatch, methods=["POST"]),
        Route("/v1/workspaces/{operation}", dispatch, methods=["POST"]),
        Route("/v1/adapters", dispatch, methods=["GET", "POST"]),
        Route("/v1/adapters/test", dispatch, methods=["POST"]),
        Route("/v1/adapters/{adapter_id}", dispatch, methods=["GET", "PATCH", "DELETE"]),
        Route("/v1/adapters/{adapter_id}/test", dispatch, methods=["POST"]),
        Route("/v1/runtime/{adapter_id}/{operation}", dispatch, methods=["POST"]),
    ])
