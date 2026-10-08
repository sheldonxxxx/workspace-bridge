"""Standard Webhooks signing and HTTPS callbacks pinned to public DNS answers.

No proxy, redirects, raw response logging, or second hostname resolution is
used. Callback URLs and signing keys never appear in exceptions or diagnostics.
"""
from __future__ import annotations

import base64
import binascii
import hmac
import http.client
import ipaddress
import json
import secrets
import socket
import ssl
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass

MAX_BODY = 256 * 1024
MAX_RESPONSE = 4096
TIMEOUT = 10.0
_DNS_WORKERS = ThreadPoolExecutor(max_workers=4, thread_name_prefix="event-callback-dns")


class CallbackError(Exception):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__("Callback endpoint could not be verified")


def signing_key(secret: str) -> bytes:
    try:
        if not isinstance(secret, str) or not secret.startswith("whsec_") or len(secret) > 100:
            raise ValueError
        key = base64.b64decode(secret[6:], validate=True)
        if not 24 <= len(key) <= 64:
            raise ValueError
        return key
    except (ValueError, binascii.Error):
        raise CallbackError("invalid_secret") from None


def callback_url(value: str) -> str:
    try:
        if (not isinstance(value, str) or not 1 <= len(value) <= 2048
                or any(ord(char) < 33 or ord(char) == 127 for char in value) or "\\" in value):
            raise ValueError
        parsed = urllib.parse.urlsplit(value)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.fragment or "%" in parsed.hostname):
            raise ValueError
        host = parsed.hostname.rstrip(".").encode("idna").decode("ascii").lower()
        port = parsed.port if parsed.port is not None else 443
        if not 1 <= port <= 65535:
            raise ValueError
        authority = f"[{host}]" if ":" in host else host
        if port != 443:
            authority += f":{port}"
        path = urllib.parse.quote(parsed.path or "/", safe="/%:@!$&'()*+,;=-._~")
        query = urllib.parse.quote(parsed.query, safe="%:@!$&'()*+,;=/?-._~")
        return urllib.parse.urlunsplit(("https", authority, path, query, ""))
    except (ValueError, UnicodeError):
        raise CallbackError("invalid_url") from None


def _public_address(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
        return (address.is_global and not address.is_multicast and not address.is_reserved
                and not address.is_unspecified and not address.is_loopback and not address.is_link_local) and not (
            isinstance(address, ipaddress.IPv6Address)
            and (address.ipv4_mapped is not None or address.sixtofour is not None or address.teredo is not None))
    except ValueError:
        return False


def public_addresses(host: str, port: int, *, timeout: float = 5.0) -> list[tuple]:
    future = _DNS_WORKERS.submit(socket.getaddrinfo, host, port,
                                 type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
    try:
        answers = future.result(timeout=min(max(timeout, 0.1), 5.0))
    except FutureTimeout:
        future.cancel()
        raise CallbackError("timeout") from None
    except OSError:
        raise CallbackError("dns_failed") from None
    if not answers or len(answers) > 32:
        raise CallbackError("invalid_destination")
    addresses = []
    for family, socktype, protocol, _, address in answers:
        if (family not in {socket.AF_INET, socket.AF_INET6} or not _public_address(address[0])
                or (family == socket.AF_INET6 and address[3] != 0)):
            raise CallbackError("invalid_destination")
        entry = (family, socktype, protocol, address)
        if entry not in addresses:
            addresses.append(entry)
    return addresses


def signed_headers(subscription_id: str, webhook_id: str, body: bytes, key: bytes,
                   *, previous_key: bytes | None = None, timestamp: int | None = None) -> dict:
    stamp = str(int(time.time()) if timestamp is None else timestamp)
    message = webhook_id.encode("ascii") + b"." + stamp.encode("ascii") + b"." + body
    signatures = ["v1," + base64.b64encode(hmac.digest(secret, message, "sha256")).decode("ascii")
                  for secret in (key, previous_key) if secret is not None]
    return {"Content-Type": "application/json", "webhook-id": webhook_id,
            "webhook-timestamp": stamp, "webhook-signature": " ".join(signatures),
            "X-MCP-Subscription-Id": subscription_id}


@dataclass(frozen=True)
class CallbackResponse:
    status: int
    body: bytes = b""


class WebhookTransport:
    def post(self, url: str, body: bytes, headers: dict, *, timeout: float = TIMEOUT,
             read_response: bool = False) -> CallbackResponse:
        if len(body) > MAX_BODY:
            raise CallbackError("payload_limit")
        deadline = time.monotonic() + min(max(timeout, 0.1), TIMEOUT)
        parsed = urllib.parse.urlsplit(callback_url(url))
        host, port = parsed.hostname, parsed.port or 443
        # Resolve immediately before connecting and validate every DNS answer.
        # Connect directly to that answer; TLS still verifies the original host.
        addresses = public_addresses(host, port, timeout=deadline - time.monotonic())
        connection = http.client.HTTPSConnection(host, port, timeout=timeout)
        context = ssl.create_default_context()
        try:
            for family, socktype, protocol, address in addresses:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise CallbackError("timeout")
                raw_socket = socket.socket(family, socktype, protocol)
                try:
                    raw_socket.settimeout(remaining)
                    raw_socket.connect(address)
                    connection.sock = context.wrap_socket(raw_socket, server_hostname=host)
                    break
                except (OSError, ssl.SSLError):
                    raw_socket.close()
            if connection.sock is None:
                raise CallbackError("connection_failed")
            connection.sock.settimeout(max(0.1, deadline - time.monotonic()))
            path = parsed.path + ("?" + parsed.query if parsed.query else "")
            connection.request("POST", path, body=body, headers=headers)
            response = connection.getresponse()
            data = response.read(MAX_RESPONSE + 1) if read_response else b""
            if len(data) > MAX_RESPONSE:
                raise CallbackError("response_limit")
            # http.client never follows a redirect. Its status is handled by
            # verification/delivery policy without sending another request.
            return CallbackResponse(response.status, data)
        except (TimeoutError, socket.timeout):
            raise CallbackError("timeout") from None
        except (OSError, http.client.HTTPException):
            raise CallbackError("connection_failed") from None
        finally:
            connection.close()

    def verify(self, url: str, subscription_id: str, key: bytes) -> None:
        challenge = secrets.token_urlsafe(32)
        body = json.dumps({"type": "verification", "challenge": challenge}, separators=(",", ":")).encode()
        ident = "msg_verification_" + secrets.token_hex(12)
        started = time.monotonic()
        response = self.post(url, body, signed_headers(subscription_id, ident, body, key), read_response=True)
        try:
            answer = json.loads(response.body)
        except (ValueError, UnicodeError):
            answer = None
        if (not 200 <= response.status < 300 or time.monotonic() - started > TIMEOUT
                or not isinstance(answer, dict) or not isinstance(answer.get("challenge"), str)
                or not secrets.compare_digest(answer["challenge"].encode("utf-8"), challenge.encode("ascii"))):
            raise CallbackError("challenge_failed")
