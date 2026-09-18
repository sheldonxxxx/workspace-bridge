"""Explicit dual-era MCP metadata validation for the tools-only HTTP adapter.

References: specification/2026-07-28/basic/{index,versioning,transports/streamable-http}.
Client metadata is never used for filesystem or workspace authorization.
"""
from __future__ import annotations
import base64
import binascii
from starlette.requests import Request
from starlette.responses import JSONResponse

LEGACY = ('2025-03-26', '2025-06-18', '2025-11-25')
MODERN = '2026-07-28'
VERSIONS = (*LEGACY, MODERN)
PREFIX = 'io.modelcontextprotocol/'


def error(ident, code: int, message: str, status: int = 400, data=None):
    value = {'code': code, 'message': message}
    if data is not None:
        value['data'] = data
    return JSONResponse({'jsonrpc': '2.0', 'id': ident, 'error': value}, status)


def header_name(value: str | None) -> str | None:
    if value is None:
        return None
    if value.startswith('=?base64?') and value.endswith('?='):
        try:
            return base64.b64decode(value[9:-2], validate=True).decode('utf-8')
        except (binascii.Error, UnicodeError):
            raise ValueError('Malformed encoded header') from None
    if value != value.strip() or any(ord(c) < 32 or ord(c) > 126 for c in value):
        raise ValueError('Malformed header')
    return value


def validate(request: Request, message: dict) -> tuple[bool, JSONResponse | None]:
    """Return modern-mode flag and a protocol error, if any, before dispatch."""
    ident = message.get('id')
    meta = message.get('params', {}).get('_meta', {})
    header = request.headers.get('mcp-protocol-version')
    version = meta.get(PREFIX + 'protocolVersion') if isinstance(meta, dict) else None
    modern = header not in (*LEGACY, None) or version is not None
    if not modern:
        return False, None
    if not isinstance(meta, dict) or not isinstance(version, str) or not isinstance(meta.get(PREFIX + 'clientCapabilities'), dict):
        return True, error(ident, -32602, 'Modern requests require protocolVersion and clientCapabilities in params._meta')
    mirrored = ('mcp-protocol-version', 'mcp-method', 'mcp-name')
    if any(len(request.headers.getlist(key)) > 1 for key in mirrored):
        return True, error(ident, -32020, 'Duplicate metadata header')
    if header != version or request.headers.get('mcp-method') != message['method']:
        return True, error(ident, -32020, 'Missing or mismatched protocol/method header')
    if version != MODERN:
        return True, error(ident, -32022, 'Unsupported protocol version', data={'supported': list(VERSIONS), 'requested': version})
    if message['method'] in ('tools/call', 'prompts/get', 'resources/read'):
        field = 'uri' if message['method'] == 'resources/read' else 'name'
        name = message['params'].get(field)
        try:
            matched = isinstance(name, str) and header_name(request.headers.get('mcp-name')) == name
        except ValueError:
            matched = False
        if not matched:
            return True, error(ident, -32020, 'Missing, malformed or mismatched Mcp-Name header')
    return True, None
