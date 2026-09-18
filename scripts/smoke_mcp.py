#!/usr/bin/env python3
"""Read-only real-HTTP smoke checks for both supported MCP protocol eras."""
from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

PREFIX = "io.modelcontextprotocol/"
MODERN = "2026-07-28"
LEGACY = "2025-11-25"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward a bridge secret to a redirected destination.
        return None


def validate_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Invalid port") from exc
    if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or port is None or not 1024 <= port <= 65535
            or parsed.path != "/mcp"):
        raise ValueError("Use the exact loopback shared MCP URL from the manager")
    return url


class Client:
    def __init__(self, url: str, token: str):
        self.url, self.token = validate_url(url), token
        self.sequence = 0
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def call(self, method: str, params: dict | None = None, *, modern: bool = False, notification: bool = False, max_response_bytes: int = 262144) -> dict:
        self.sequence += 1
        params = dict(params or {})
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                   "X-Bridge-Token": self.token, "MCP-Protocol-Version": MODERN if modern else LEGACY}
        if modern:
            params["_meta"] = {PREFIX + "protocolVersion": MODERN, PREFIX + "clientCapabilities": {}}
            headers["Mcp-Method"] = method
            if "name" in params:
                headers["Mcp-Name"] = params["name"]
        message = {"jsonrpc": "2.0", "method": method, "params": params}
        if not notification:
            message["id"] = self.sequence
        request = urllib.request.Request(self.url, data=json.dumps(message).encode(), headers=headers, method="POST")
        with self.opener.open(request, timeout=20) as response:
            body = response.read(max_response_bytes + 1)
            if len(body) > max_response_bytes:
                raise ValueError("Unexpectedly large response")
            if notification:
                if response.status != 202:
                    raise ValueError("Unexpected notification response")
                return {}
            result = json.loads(body)
        if "error" in result or result.get("id") != self.sequence:
            raise ValueError("RPC failed or returned an unexpected request ID")
        result = result["result"]
        if result.get("isError"):
            raise ValueError("Tool call reported an error")
        if modern and result.get("resultType") != "complete":
            raise ValueError("Modern response did not contain resultType=complete")
        return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--workspace-id", help="Optional enabled workspace to read; otherwise use the first enabled mapping")
    args = parser.parse_args()
    try:
        validate_url(args.url)
        token = os.environ.get("WORKSPACE_BRIDGE_TOKEN") or getpass.getpass("Shared bridge token: ")
        if not token or "\n" in token or "\r" in token:
            raise ValueError("A nonempty, single-line token is required")
        client = Client(args.url, token)
        initialized = client.call("initialize", {"protocolVersion": LEGACY, "capabilities": {},
            "clientInfo": {"name": "workspace-bridge-smoke", "version": "0.6.0"}})
        if initialized.get("protocolVersion") != LEGACY:
            raise ValueError("Legacy version negotiation failed")
        client.call("notifications/initialized", notification=True)
        tools = client.call("tools/list")["tools"]
        if len(tools) != 12:
            raise ValueError("Unexpected tool count")
        skill = client.call("tools/call", {"name": "read_project_lead_skill", "arguments": {}})
        skill_value = json.loads(skill["content"][0]["text"])
        if skill_value.get("name") != "project-lead" or not skill_value.get("content"):
            raise ValueError("Embedded project-lead skill missing")
        print("PASS: embedded project-lead skill retrieval (legacy)")
        value = client.call("tools/call", {"name": "list_workspaces", "arguments": {}})
        workspaces = json.loads(value["content"][0]["text"])["workspaces"]
        selected = args.workspace_id or (workspaces[0]["workspace_id"] if workspaces else None)
        if selected is None:
            raise ValueError("Enable at least one nonsensitive workspace before smoke testing")
        scoped = {"workspace_id": selected}
        client.call("tools/call", {"name": "workspace_info", "arguments": scoped})
        client.call("tools/call", {"name": "list_dir", "arguments": {**scoped, "depth": 1}})
        print("PASS: legacy initialize, tool discovery (12), workspace discovery, info, directory listing")
        discovery = client.call("server/discover", modern=True)
        if MODERN not in discovery.get("supportedVersions", []):
            raise ValueError("Modern protocol not advertised")
        client.call("tools/list", modern=True)
        modern_skill = client.call("tools/call", {"name": "read_project_lead_skill", "arguments": {}}, modern=True)
        if json.loads(modern_skill["content"][0]["text"]) != skill_value:
            raise ValueError("Skill differs between protocol modes")
        print("PASS: identical embedded skill retrieval (modern)")
        client.call("tools/call", {"name": "workspace_info", "arguments": scoped}, modern=True)
        print("PASS: modern per-request discovery, tools, workspace_info")
        print("Read-only local HTTP checks passed. This did not connect to OpenAI or run an agent.")
        return 0
    except urllib.error.HTTPError as exc:
        print(f"HTTP {exc.code}: check the shared /mcp endpoint, bridge state and credential.", file=sys.stderr)
    except (OSError, ValueError, KeyError, TypeError):
        print("Smoke check failed. Verify local configuration and server diagnostics; no secrets were logged.", file=sys.stderr)
    except (EOFError, KeyboardInterrupt):
        print("Cancelled.", file=sys.stderr)
        return 130
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
