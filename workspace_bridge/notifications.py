"""Bounded Discord webhook notifications for run lifecycle events.

The webhook URL is read only from local runtime environment
(``WB_DISCORD_WEBHOOK_URL``). It is never accepted over MCP, returned from
admin APIs, written to project files, or logged. Payloads contain safe metadata
only: workspace display name, handoff title, bridge run id, request kind/action
and timestamps. No external paths, command bodies, source snippets, prompt text,
provider details or transcript are sent.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from .security import BridgeError, redact

DEFAULT_TIMEOUT = 6.0
DEFAULT_ATTEMPTS = 3
# Stable, non-secret identifier so Discord/egress filters can recognize bridge
# traffic. Never includes the webhook URL, tokens, paths or run content.
BRIDGE_USER_AGENT = "Workspace-Bridge/0.8.4 (+local-admin; Discord webhook)"
MAX_DIAGNOSTIC_CHARS = 160
MAX_ERROR_BODY_BYTES = 2048
ATTENTION_STATES = frozenset({"waiting_permission", "waiting_question"})
NOTIFIED_STATES = frozenset({"waiting_permission", "waiting_question", "completed",
                             "blocked", "failed", "cancelled"})

COLORS = {"waiting_permission": 0xE0A800, "waiting_question": 0xE0A800,
          "completed": 0x2E9E5B, "blocked": 0x9E6B2E, "failed": 0xC0392B,
          "cancelled": 0x6B7280}


@dataclass
class NotificationResult:
    status: str
    attempts: int
    code: str = ""
    detail: str = ""

    def public(self) -> dict:
        result = {"status": self.status, "attempts": self.attempts, "code": self.code}
        if self.detail:
            result["detail"] = self.detail
        return result


def _safe_diagnostic(body: bytes, content_type: str = "") -> str:
    """Extract a bounded, non-secret diagnostic from a Discord error body.

    Only JSON bodies carrying Discord's public ``code``/``message`` fields
    produce a detail string. HTML, plain-text or unparseable bodies yield ""
    so raw Cloudflare/proxy pages, IPs, cookies or paths are never persisted.
    """
    if not body:
        return ""
    ctype = (content_type or "").split(";")[0].strip().lower()
    if ctype and "json" not in ctype:
        return ""
    try:
        text = body[:MAX_ERROR_BODY_BYTES].decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return ""
    stripped = text.lstrip()
    if stripped.startswith("<") or "<html" in stripped[:512].lower():
        return ""
    try:
        payload = json.loads(text)
    except ValueError:
        return ""
    if not isinstance(payload, dict):
        return ""
    message = payload.get("message")
    code = payload.get("code")
    parts: list[str] = []
    if isinstance(code, int):
        parts.append(f"discord_{code}")
    if isinstance(message, str) and message.strip():
        cleaned, _ = redact(message.strip())
        # Keep only printable, bounded text; drop anything secret-shaped
        # that survived redaction markers.
        cleaned = "".join(c for c in cleaned if c == " " or ord(c) >= 32)[:120]
        if "[REDACTED_SECRET]" in cleaned:
            cleaned = "[REDACTED_SECRET]"
        if cleaned:
            parts.append(cleaned)
    detail = ": ".join(parts)[:MAX_DIAGNOSTIC_CHARS]
    # Never persist anything that looks like a URL, absolute path or address.
    # (Secret-shaped text is already redacted above; Discord's own public
    # error vocabulary such as "Unknown Webhook" must survive this filter.)
    lowered = detail.lower()
    if any(token in lowered for token in ("http://", "https://", "discord.com",
                                          "/home/", "/users/")):
        return parts[0][:MAX_DIAGNOSTIC_CHARS] if parts else ""
    return detail


class Notifier:
    """Protocol for notification sinks; tests substitute a recorder."""

    enabled = False

    def notify(self, *, state: str, workspace_name: str, handoff_title: str,
               run_id: str, request_kind: str = "", request_action: str = "",
               at: str = "") -> NotificationResult:
        raise NotImplementedError


class NullNotifier(Notifier):
    enabled = False

    def notify(self, **_: object) -> NotificationResult:
        return NotificationResult(status="disabled", attempts=0)


class DiscordNotifier(Notifier):
    enabled = True

    def __init__(self, webhook_url: str, *, timeout: float = DEFAULT_TIMEOUT,
                 attempts: int = DEFAULT_ATTEMPTS, sleep=time.sleep):
        if not webhook_url.startswith(("https://", "http://")):
            raise BridgeError("WB_DISCORD_WEBHOOK_URL must be an http(s) URL")
        self._webhook = webhook_url
        self.timeout = max(1.0, min(float(timeout), 20.0))
        self.attempts = max(1, min(int(attempts), 5))
        self._sleep = sleep

    def _payload(self, *, state: str, workspace_name: str, handoff_title: str,
                 run_id: str, request_kind: str, request_action: str, at: str) -> dict:
        attention = state in ATTENTION_STATES
        headline = "Agent run needs attention" if attention else f"Agent run {state}"
        fields = [
            {"name": "Workspace", "value": (workspace_name or "unknown")[:100], "inline": True},
            {"name": "Handoff", "value": (handoff_title or "unknown")[:100], "inline": True},
            {"name": "Run", "value": run_id[:80], "inline": True},
            {"name": "State", "value": state[:40], "inline": True},
        ]
        if request_kind:
            fields.append({"name": "Request", "value": request_kind[:60], "inline": True})
        if request_action:
            fields.append({"name": "Action", "value": request_action[:80], "inline": True})
        footer = "Review the pending request in ChatGPT/local manager; details are not sent to Discord."
        if not attention:
            footer = "Read the final run result in ChatGPT/local manager."
        embed = {"title": headline, "color": COLORS.get(state, 0x6B7280),
                 "description": footer, "fields": fields}
        if at:
            embed["timestamp"] = at
        return {"embeds": [embed], "allowed_mentions": {"parse": []}}

    def notify(self, *, state: str, workspace_name: str, handoff_title: str,
               run_id: str, request_kind: str = "", request_action: str = "",
               at: str = "") -> NotificationResult:
        if state not in NOTIFIED_STATES:
            return NotificationResult(status="skipped", attempts=0)
        body = json.dumps(self._payload(state=state, workspace_name=workspace_name,
                                        handoff_title=handoff_title, run_id=run_id,
                                        request_kind=request_kind, request_action=request_action,
                                        at=at)).encode("utf-8")
        last_code = ""
        last_detail = ""
        attempt = 0
        for attempt in range(1, self.attempts + 1):
            request = urllib.request.Request(self._webhook, data=body, method="POST",
                                             headers={"Content-Type": "application/json",
                                                      "Accept": "application/json",
                                                      "User-Agent": BRIDGE_USER_AGENT})
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    if 200 <= response.status < 300:
                        return NotificationResult(status="sent", attempts=attempt)
                    last_code = f"http_{response.status}"
                    last_detail = ""
            except urllib.error.HTTPError as exc:
                last_code = f"http_{exc.code}"
                raw_body = b""
                content_type = ""
                try:
                    content_type = str(getattr(exc, "headers", None) and
                                       exc.headers.get_content_type() or "")
                except Exception:  # noqa: BLE001
                    content_type = ""
                try:
                    raw_body = exc.read(MAX_ERROR_BODY_BYTES) if exc.fp else b""
                except Exception:  # noqa: BLE001
                    raw_body = b""
                last_detail = _safe_diagnostic(raw_body, content_type)
                if 400 <= exc.code < 500 and exc.code != 429:
                    break  # Permanent client error; do not retry.
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
                last_code = "unreachable"
                last_detail = ""
            if attempt < self.attempts:
                self._sleep(min(2 ** attempt * 0.5, 4.0))
        return NotificationResult(status="failed", attempts=attempt, code=last_code,
                                  detail=last_detail)


def notifier_from_environment(environ: dict | None = None,
                              factory=DiscordNotifier) -> Notifier:
    env = environ if environ is not None else os.environ
    url = (env.get("WB_DISCORD_WEBHOOK_URL") or "").strip()
    if not url:
        return NullNotifier()
    try:
        return factory(url)
    except BridgeError:
        return NullNotifier()
