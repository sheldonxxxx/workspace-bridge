"""Agent Protocol v2: JSON-RPC between a Node and an agent plugin over stdio.

The Node starts each plugin as a child process and owns its stdin and
stdout. Every message is one UTF-8 JSON object on one line. The Node sends
``initialize`` first, then named requests; the plugin answers each request by
id and pushes ``event`` notifications. The resource model (descriptor,
models, conversations, runs, interactions, activities) is the same as
Runtime Protocol v1; only the transport changed: no ports, no tokens. Security
profiles belong to the Bridge: each run carries the rules the plugin must
enforce, and the manifest's ``enforces`` list says which rule keys it can.
"""
from __future__ import annotations

import json
import re
import sys
import tomllib
from pathlib import Path
from typing import Any

from . import security_profiles
from .runtime import RuntimeRejected, RuntimeUnavailable, RuntimeUnsupported
from .security import BridgeError, redact

PROTOCOL = 2
MAX_LINE = 16 * 1024 * 1024
RUNTIME_TYPES = frozenset({"pi", "codex", "claude", "opencode"})
_NAME = re.compile(r"[a-z][a-z0-9_-]{0,39}")
_SETTING = re.compile(r"[a-z][a-z0-9_]{0,39}")
_CODE = re.compile(r"[a-z][a-z0-9_]{0,79}")

# (HTTP method, v1 path pattern, v2 method, path parameter names). The Node's
# runtime client still speaks v1 paths internally; this table is the one
# place that maps them onto v2 method names.
ROUTES: tuple[tuple[str, re.Pattern, str, tuple[str, ...]], ...] = tuple(
    (method, re.compile(pattern), name, params) for method, pattern, name, params in (
        ("GET", r"/v1/descriptor", "descriptor", ()),
        ("GET", r"/v1/models", "models", ()),
        ("GET", r"/v1/usage-limits", "usageLimits", ()),
        ("POST", r"/v1/conversations", "conversations.create", ()),
        ("GET", r"/v1/conversations/([^/]+)", "conversations.get", ("conversationId",)),
        ("POST", r"/v1/conversations/([^/]+)/runs", "runs.start", ("conversationId",)),
        ("GET", r"/v1/conversations/([^/]+)/runs/([^/]+)", "runs.find",
         ("conversationId", "clientRunId")),
        ("GET", r"/v1/runs/([^/]+)", "runs.get", ("runId",)),
        ("POST", r"/v1/runs/([^/]+)/cancel", "runs.cancel", ("runId",)),
        ("POST", r"/v1/runs/([^/]+)/steer", "runs.steer", ("runId",)),
        ("POST", r"/v1/runs/([^/]+)/queue", "runs.queue", ("runId",)),
        ("GET", r"/v1/runs/([^/]+)/interactions", "runs.interactions", ("runId",)),
        ("GET", r"/v1/runs/([^/]+)/activities", "runs.activities", ("runId",)),
        ("POST", r"/v1/interactions/([^/]+)/resolve", "interactions.resolve", ("interactionId",)),
        ("GET", r"/v1/activities/([^/]+)", "activities.get", ("activityId",)),
        ("GET", r"/v1/events", "events", ()),
    ))
METHODS = frozenset(name for _, _, name, _ in ROUTES)


def to_rpc(method: str, path: str, query: dict | None, body: dict | None) -> tuple[str, dict]:
    """Translate one v1 request into a v2 method name and params."""
    for verb, pattern, name, names in ROUTES:
        match = pattern.fullmatch(path)
        if verb == method and match:
            params: dict[str, Any] = dict(zip(names, match.groups()))
            for key, value in (query or {}).items():
                if value is None:
                    continue
                if key in ("after", "waitMs"):
                    params[key] = int(value)
                else:
                    params[key] = value
            if body is not None:
                params["body"] = body
            return name, params
    raise BridgeError("Unknown runtime operation", "not_found")


def encode(message: dict) -> bytes:
    line = json.dumps(message, separators=(",", ":"), ensure_ascii=False).encode() + b"\n"
    if len(line) > MAX_LINE:
        raise BridgeError("Plugin message is too large", "too_large")
    return line


def error_payload(status: int, code: str, message: str) -> dict:
    """The ``error`` member of a failed response; ``data`` keeps the v1 meaning."""
    if not isinstance(code, str) or not _CODE.fullmatch(code):
        code = "runtime_rejected"
    if isinstance(status, bool) or not isinstance(status, int) or not 400 <= status <= 599:
        status = 502
    return {"code": -32000, "message": str(message)[:300],
            "data": {"status": status, "code": code}}


def raise_for_error(error: Any) -> None:
    """Raise the runtime exception a plugin error stands for (v1 meaning)."""
    error = error if isinstance(error, dict) else {}
    data = error.get("data") if isinstance(error.get("data"), dict) else {}
    status, code = data.get("status"), str(data.get("code") or "runtime_rejected")[:80]
    if not _CODE.fullmatch(code):
        code = "runtime_rejected"
    detail = redact(str(error.get("message") or ""))[0][:300]
    if status == 501:
        raise RuntimeUnsupported(detail or "Runtime feature is unsupported")
    if status in (400, 404, 409, 412, 422):
        raise RuntimeRejected(detail or "Runtime rejected the request", code=code, status=status)
    raise RuntimeUnavailable(detail or "Runtime adapter returned an error")


# ---------------------------------------------------------------------------
# Manifests

BUILTIN_DIR = Path(__file__).parent / "plugins"


def _python_command(module_args: list[str]) -> list[str]:
    return [sys.executable, "-m", "workspace_bridge.plugin_host", *module_args]


def load_manifest(path: Path) -> dict:
    """Read and validate one plugin manifest (TOML)."""
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        raise BridgeError("Plugin manifest is unreadable", "plugin_manifest_invalid") from None
    return validate_manifest(raw)


def validate_manifest(raw: Any) -> dict:
    def bad(what: str) -> BridgeError:
        return BridgeError(f"Plugin manifest {what}", "plugin_manifest_invalid")

    if not isinstance(raw, dict):
        raise bad("must be a table")
    allowed = {"id", "version", "protocol", "runtime_type", "command", "python_module",
               "description", "features", "settings", "enforces"}
    if set(raw) - allowed:
        raise bad("has unknown keys: " + ", ".join(sorted(set(raw) - allowed)))
    ident = raw.get("id")
    if not isinstance(ident, str) or not _NAME.fullmatch(ident):
        raise bad("id must be a short lowercase name")
    if raw.get("protocol") != PROTOCOL:
        raise bad(f"protocol must be {PROTOCOL}")
    if raw.get("runtime_type") not in RUNTIME_TYPES:
        raise bad("runtime_type must be one of " + ", ".join(sorted(RUNTIME_TYPES)))
    version = raw.get("version")
    if not isinstance(version, str) or not version or len(version) > 40:
        raise bad("version is required")
    command, module = raw.get("command"), raw.get("python_module")
    if (command is None) == (module is None):
        raise bad("needs exactly one of command or python_module")
    if command is not None and (
            not isinstance(command, list) or not 1 <= len(command) <= 16
            or any(not isinstance(part, str) or not part or len(part) > 4096 for part in command)):
        raise bad("command must be a list of strings")
    if module is not None and (not isinstance(module, str) or not _NAME.fullmatch(module)):
        raise bad("python_module must name a built-in plugin")
    settings = raw.get("settings", [])
    if (not isinstance(settings, list) or len(settings) > 16
            or any(not isinstance(key, str) or not _SETTING.fullmatch(key) for key in settings)):
        raise bad("settings must be a list of setting names")
    for key in ("features", "enforces"):
        value = raw.get(key, [])
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            raise bad(f"{key} must be a list of strings")
    unknown = set(raw.get("enforces", [])) - set(security_profiles.KEYS)
    if unknown:
        raise bad("enforces names unknown rule keys: " + ", ".join(sorted(unknown)))
    return {"id": ident, "version": version, "runtime_type": raw["runtime_type"],
            "command": list(command) if command is not None else _python_command([module]),
            "description": str(raw.get("description", ""))[:200],
            "settings": list(settings), "features": list(raw.get("features", [])),
            "enforces": sorted(set(raw.get("enforces", [])))}


def builtin_manifests() -> dict[str, dict]:
    return {manifest["id"]: manifest for manifest in
            (load_manifest(path) for path in sorted(BUILTIN_DIR.glob("*.toml")))}
