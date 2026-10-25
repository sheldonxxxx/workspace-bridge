"""Node side of pairing: one outbound connection to the Bridge's Node gateway.

``pair`` redeems a one-time pairing string: it pins the gateway certificate
by fingerprint, proves possession of a fresh Ed25519 key, and records the
Bridge in the Node config. ``run_link`` keeps the authenticated connection
open, reconnecting with jittered backoff, and answers Bridge requests with
the same handler as the HTTP Node API. No inbound port is opened.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import secrets
import ssl
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

from . import node_api, pairing
from .oplog import emit, error_code
from .security import BridgeError

MAX_FRAME = 40 * 1024 * 1024
MAX_CONCURRENT_REQUESTS = 16
HANDSHAKE_TIMEOUT = 10.0
LINK_STATUS = "link-status.json"
LINK_HEARTBEAT_SECONDS = 30.0
LINK_STATUS_FRESH_SECONDS = 90
_NONCE = re.compile(r"[0-9a-f]{64}")
_METHODS = frozenset({"GET", "POST", "PATCH", "DELETE"})
_log = logging.getLogger("workspace_bridge.ops")


def _peer_fingerprint(websocket) -> str:
    ssl_object = websocket.transport.get_extra_info("ssl_object")
    der = ssl_object.getpeercert(binary_form=True) if ssl_object is not None else None
    if not der:
        raise BridgeError("Gateway presented no certificate", "gateway_untrusted")
    return pairing.certificate_fingerprint(der)


async def _challenge(websocket) -> str:
    frame = json.loads(await asyncio.wait_for(websocket.recv(), HANDSHAKE_TIMEOUT))
    if (not isinstance(frame, dict) or frame.get("type") != "challenge"
            or frame.get("protocol") != pairing.PROTOCOL
            or not isinstance(frame.get("nonce"), str) or not _NONCE.fullmatch(frame["nonce"])):
        raise BridgeError("Gateway protocol is incompatible", "gateway_protocol_error")
    return frame["nonce"]


async def _answer(websocket, expected: str) -> dict:
    frame = json.loads(await asyncio.wait_for(websocket.recv(), HANDSHAKE_TIMEOUT))
    if not isinstance(frame, dict):
        raise BridgeError("Gateway protocol is incompatible", "gateway_protocol_error")
    if frame.get("type") == "error":
        code = frame.get("code") if isinstance(frame.get("code"), str) else "rejected"
        raise BridgeError("The Bridge refused this Node", code[:60])
    if frame.get("type") != expected:
        raise BridgeError("Gateway protocol is incompatible", "gateway_protocol_error")
    return frame


def _write_atomic(path: Path, data: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def write_link_status(state: Path, status: str, *, since: str | None = None,
                      code: str | None = None, retry_at: str | None = None) -> None:
    """Record this Node's connection state for ``node service status``; never raises."""
    value = {"status": status, "since": since, "code": code, "retry_at": retry_at,
             "updated": datetime.now(timezone.utc).isoformat()}
    try:
        _write_atomic(state / LINK_STATUS, json.dumps(value, sort_keys=True).encode())
    except OSError:
        pass


def read_link_status(state: Path, now: datetime | None = None) -> dict:
    """Health view of the dial-out connection; stale or missing state is unknown."""
    try:
        fd = os.open(state / LINK_STATUS, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, encoding="utf-8") as stream:
            value = json.loads(stream.read(4096))
        updated = datetime.fromisoformat(value["updated"])
    except (OSError, ValueError, KeyError, TypeError):
        return {"status": "unknown", "code": "paired_node_no_status"}
    age = ((now or datetime.now(timezone.utc)) - updated).total_seconds()
    if value.get("status") == "stopped":
        return {"status": "stopped", "code": "paired_node"}
    if value.get("status") == "connected" and age <= LINK_STATUS_FRESH_SECONDS:
        return {"status": "connected", "code": "paired_node", "since": value.get("since")}
    if value.get("status") == "retrying" and age <= max(LINK_STATUS_FRESH_SECONDS, 120):
        return {"status": "disconnected", "code": str(value.get("code") or "retrying")[:80],
                "retry_at": value.get("retry_at")}
    return {"status": "unknown", "code": "paired_node_status_stale"}


async def pair(state: Path, config: dict, pairing_string: str) -> dict:
    """Redeem one pairing string; return the stored ``bridge`` block."""
    url, fingerprint, code = pairing.parse_pairing(pairing_string)
    key = Ed25519PrivateKey.generate()
    public_key = pairing.public_key_text(key)
    # The certificate is not known yet, only its fingerprint: verify the
    # fingerprint before sending anything beyond the WebSocket upgrade.
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    try:
        async with connect(url, ssl=context, max_size=MAX_FRAME, open_timeout=HANDSHAKE_TIMEOUT,
                           compression=None) as websocket:
            if _peer_fingerprint(websocket) != fingerprint:
                raise BridgeError("Gateway certificate does not match the pairing string",
                                  "gateway_untrusted")
            der = websocket.transport.get_extra_info("ssl_object").getpeercert(binary_form=True)
            nonce = await _challenge(websocket)
            await websocket.send(json.dumps({
                "type": "pair", "code": code, "public_key": public_key,
                "signature": pairing.sign(key, "pair", nonce, fingerprint,
                                          pairing.code_hash(code))}))
            answer = await _answer(websocket, "paired")
    except (OSError, WebSocketException, asyncio.TimeoutError, ValueError) as exc:
        raise BridgeError("Could not reach the Bridge Node gateway", "gateway_unavailable") from exc
    node_id = answer.get("node_id")
    if not isinstance(node_id, str) or not re.fullmatch(r"node_[0-9a-f]{24}", node_id):
        raise BridgeError("Gateway protocol is incompatible", "gateway_protocol_error")
    bridge = {"url": url, "fingerprint": fingerprint, "node_id": node_id,
              "name": str(answer.get("name", ""))[:80],
              "certificate": ssl.DER_cert_to_PEM_cert(der)}
    _write_atomic(state / pairing.NODE_KEY, key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()))
    _write_atomic(state / "node-config.json",
                  json.dumps({**config, "bridge": bridge}, indent=2).encode())
    return bridge


async def _serve(websocket, service) -> None:
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)
    send_lock = asyncio.Lock()
    tasks: set[asyncio.Task] = set()

    async def answer(frame: dict) -> None:
        async with semaphore:
            ident, method, path = frame.get("id"), frame.get("method"), frame.get("path")
            if (type(ident) is not int or method not in _METHODS or not isinstance(path, str)
                    or not path.startswith("/v1/") or len(path) > 512):
                return
            body = frame.get("body")
            raw = b"" if body is None else json.dumps(body, separators=(",", ":")).encode()
            # The shared handler blocks on files, Git and adapters.
            status, out = await asyncio.to_thread(node_api.handle, service, method, path, raw)
            text = f'{{"type":"response","id":{ident},"status":{status},"body":{out.decode("utf-8")}}}'
            async with send_lock:
                await websocket.send(text)

    try:
        async for message in websocket:
            try:
                frame = json.loads(message)
            except (ValueError, TypeError):
                continue
            if isinstance(frame, dict) and frame.get("type") == "request":
                task = asyncio.create_task(answer(frame))
                tasks.add(task)
                task.add_done_callback(tasks.discard)
    finally:
        for task in tasks:
            task.cancel()


async def _heartbeat(state: Path, since: str) -> None:
    while True:
        await asyncio.sleep(LINK_HEARTBEAT_SECONDS)
        write_link_status(state, "connected", since=since)


async def run_link(service, config: dict, state: Path, *,
                   stop: asyncio.Event | None = None, max_backoff: float = 60.0) -> None:
    """Keep the Node connected to its Bridge until ``stop`` is set."""
    bridge = config["bridge"]
    key = pairing.load_or_create_node_key(state) if (state / pairing.NODE_KEY).exists() else None
    if key is None:
        raise BridgeError("Node key is missing; pair this Node again", "pairing_state_unsafe")
    context = pairing.pinned_client_context(bridge["certificate"])
    stop = stop or asyncio.Event()
    backoff = 1.0
    last_code = "connecting"
    while not stop.is_set():
        try:
            async with connect(bridge["url"], ssl=context, max_size=MAX_FRAME,
                               open_timeout=HANDSHAKE_TIMEOUT, ping_interval=20,
                               ping_timeout=20, compression=None) as websocket:
                if _peer_fingerprint(websocket) != bridge["fingerprint"]:
                    raise BridgeError("Gateway certificate changed", "gateway_untrusted")
                nonce = await _challenge(websocket)
                await websocket.send(json.dumps({
                    "type": "hello", "node_id": bridge["node_id"],
                    "signature": pairing.sign(key, "hello", nonce, bridge["fingerprint"],
                                              bridge["node_id"])}))
                await _answer(websocket, "ready")
                backoff = 1.0
                last_code = "connection_closed"
                since = datetime.now(timezone.utc).isoformat()
                write_link_status(state, "connected", since=since)
                emit(_log, "INFO", "node", "node_link_connected")
                serving = asyncio.create_task(_serve(websocket, service))
                stopping = asyncio.create_task(stop.wait())
                heartbeat = asyncio.create_task(_heartbeat(state, since))
                await asyncio.wait({serving, stopping}, return_when=asyncio.FIRST_COMPLETED)
                stopping.cancel()
                heartbeat.cancel()
                if not serving.done():
                    serving.cancel()
        except Exception as exc:  # noqa: BLE001 - every failure retries with backoff
            last_code = error_code(exc)
            emit(_log, "WARNING", "node", "node_link_retry", code=last_code)
        if stop.is_set():
            write_link_status(state, "stopped")
            break
        delay = backoff * (0.5 + random.random() / 2)
        backoff = min(max_backoff, backoff * 2)
        write_link_status(state, "retrying", code=last_code,
                          retry_at=(datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat())
        try:
            await asyncio.wait_for(stop.wait(), delay)
        except asyncio.TimeoutError:
            pass
