"""Bridge-side gateway for paired Nodes that dial in over TLS WebSockets.

The gateway runs on its own event loop and thread. Bridge worker threads
wait on that loop for Node replies, sometimes while holding the service
lock, so the gateway loop must never block on that lock itself.

A paired Node opens one outbound connection to this gateway and keeps it
alive. After it proves possession of its registered Ed25519 key, the Bridge
sends ordinary Node API requests over that socket and the Node answers them.
No inbound port is needed on the Node, and the Node token is not used.

Pairing codes are single-use, short-lived, and stored only as hashes.
Frames carry identifiers, routes and the same JSON bodies as the HTTP Node
API; nothing is logged.
"""
from __future__ import annotations

import asyncio
import collections
import json
import re
import secrets
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from starlette.applications import Starlette
from starlette.routing import WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from . import pairing
from .security import BridgeError

MAX_FRAME = 40 * 1024 * 1024
AUTH_TIMEOUT = 10.0
MAX_FAILURES_PER_MINUTE = 20
HEARTBEAT_SECONDS = 30.0
_NODE_ID = re.compile(r"node_[0-9a-f]{24}")


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ----------------------------------------------------------- pairing store
def create_pairing(db: sqlite3.Connection, name: str, *, replace: bool = False,
                   ttl_seconds: int = pairing.PAIRING_TTL_SECONDS) -> tuple[str, str]:
    """Record one single-use pairing code; return (code, expiry ISO time).

    The caller serializes access to ``db``. ``replace`` re-pairs an existing
    paired Node under the same name, which keeps its workspaces and gives it
    a new key and connection revision.
    """
    if (not isinstance(name, str) or not 1 <= len(name.strip()) <= 80
            or any(ord(char) < 32 for char in name)):
        raise BridgeError("Node name must contain 1 to 80 characters", "invalid_arguments")
    name = name.strip()
    existing = db.execute("SELECT id,transport FROM nodes WHERE name=? COLLATE NOCASE",
                          (name,)).fetchone()
    if existing is not None and not replace:
        raise BridgeError("Node name is already in use; re-pair it explicitly to replace its key",
                          "conflict")
    if replace and (existing is None or existing["transport"] != "paired"):
        raise BridgeError("Only an existing paired Node can be re-paired", "invalid_arguments")
    now = _now()
    code = pairing.new_pairing_code()
    expires = (now + timedelta(seconds=ttl_seconds)).isoformat()
    with db:
        db.execute("DELETE FROM node_pairings WHERE expires<? OR used IS NOT NULL",
                   ((now - timedelta(days=1)).isoformat(),))
        db.execute("INSERT INTO node_pairings(id,code_hash,name,replace_node,created,expires) "
                   "VALUES(?,?,?,?,?,?)",
                   ("pair_" + secrets.token_hex(12), pairing.code_hash(code), name,
                    existing["id"] if replace else None, now.isoformat(), expires))
    return code, expires


def redeem_pairing(db: sqlite3.Connection, code: str, public_key: str) -> dict:
    """Consume a pairing code and bind ``public_key`` to a Node; return the Node row."""
    now = _now()
    row = db.execute("SELECT * FROM node_pairings WHERE code_hash=? AND used IS NULL",
                     (pairing.code_hash(code),)).fetchone()
    if row is None or row["expires"] <= now.isoformat():
        raise BridgeError("Pairing code is invalid, used, or expired", "pairing_rejected")
    clash = db.execute("SELECT id FROM nodes WHERE public_key=? AND id IS NOT ?",
                       (public_key, row["replace_node"])).fetchone()
    if clash is not None:
        raise BridgeError("This Node key is already paired", "pairing_rejected")
    revision = pairing.node_revision(public_key)
    stamp = now.isoformat()
    with db:
        if db.execute("UPDATE node_pairings SET used=? WHERE id=? AND used IS NULL",
                      (stamp, row["id"])).rowcount != 1:
            raise BridgeError("Pairing code is invalid, used, or expired", "pairing_rejected")
        if row["replace_node"]:
            node_id = row["replace_node"]
            if db.execute("UPDATE nodes SET public_key=?,revision=?,updated=? "
                          "WHERE id=? AND transport='paired'",
                          (public_key, revision, stamp, node_id)).rowcount != 1:
                raise BridgeError("The Node to re-pair no longer exists", "pairing_rejected")
        else:
            node_id = "node_" + secrets.token_hex(12)
            try:
                db.execute("INSERT INTO nodes(id,name,base_url,token,enabled,revision,created,updated,"
                           "transport,public_key) VALUES(?,?,'','',1,?,?,?,'paired',?)",
                           (node_id, row["name"], revision, stamp, stamp, public_key))
            except sqlite3.IntegrityError:
                raise BridgeError("Node name is already in use", "pairing_rejected") from None
        db.execute("UPDATE node_pairings SET node_id=? WHERE id=?", (node_id, row["id"]))
    return dict(db.execute("SELECT * FROM nodes WHERE id=?", (node_id,)).fetchone())


# --------------------------------------------------------------- transport
class NodeConnection:
    """One authenticated Node socket; requests may be in flight concurrently."""

    def __init__(self, node_id: str, websocket: WebSocket, loop: asyncio.AbstractEventLoop):
        self.node_id = node_id
        self.websocket = websocket
        self.loop = loop
        self.loop_thread = threading.get_ident()
        self.closed = False
        self._pending: dict[int, asyncio.Future] = {}
        self._next = 0
        self._send_lock = asyncio.Lock()

    async def call(self, method: str, path: str, content: bytes | None,
                   timeout: float) -> tuple[int, bytes]:
        if self.closed:
            raise BridgeError("Node is unavailable", "node_unavailable")
        self._next += 1
        ident = self._next
        future = self.loop.create_future()
        self._pending[ident] = future
        # The Node client already produced an ASCII JSON body; embed it as is.
        body = content.decode("ascii") if content else "null"
        frame = (f'{{"type":"request","id":{ident},"method":{json.dumps(method)},'
                 f'"path":{json.dumps(path)},"body":{body}}}')
        try:
            async with self._send_lock:
                await self.websocket.send_text(frame)
            return await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError:
            raise BridgeError("Node did not answer in time", "node_unavailable") from None
        except (WebSocketDisconnect, RuntimeError, OSError):
            raise BridgeError("Node is unavailable", "node_unavailable") from None
        finally:
            self._pending.pop(ident, None)

    def resolve(self, frame: dict) -> None:
        ident, status = frame.get("id"), frame.get("status")
        if (type(ident) is not int or type(status) is not int
                or not 100 <= status <= 599 or ident not in self._pending):
            return
        future = self._pending[ident]
        if not future.done():
            raw = json.dumps(frame.get("body"), ensure_ascii=False,
                             separators=(",", ":")).encode("utf-8")
            future.set_result((status, raw))

    def fail_all(self) -> None:
        self.closed = True
        for future in self._pending.values():
            if not future.done():
                future.set_exception(BridgeError("Node is unavailable", "node_unavailable"))


class GatewayTransport:
    """Synchronous transport for ``NodeClient``; must be called off the event loop."""

    def __init__(self, gateway: "NodeGateway", node_id: str, timeout: float):
        self.gateway = gateway
        self.node_id = node_id
        self.timeout = timeout

    def request(self, method: str, path: str, content: bytes | None = None,
                headers: dict | None = None):
        connection = self.gateway.connection(self.node_id)
        if connection is None:
            raise BridgeError("Node is not connected", "node_unavailable")
        if threading.get_ident() == connection.loop_thread:
            raise BridgeError("Node requests cannot run on the event loop", "node_unavailable")
        future = asyncio.run_coroutine_threadsafe(
            connection.call(method, path, content, self.timeout), connection.loop)
        try:
            status, raw = future.result(self.timeout + 5)
        except TimeoutError:
            future.cancel()
            raise BridgeError("Node did not answer in time", "node_unavailable") from None
        return SimpleNamespace(status_code=status, content=raw)


# ----------------------------------------------------------------- gateway
class NodeGateway:
    def __init__(self, service):
        self.service = service
        self._connections: dict[str, NodeConnection] = {}
        self._lock = threading.Lock()
        self._failures: collections.deque[float] = collections.deque()
        self._fingerprint: str | None = None

    def connection(self, node_id: str) -> NodeConnection | None:
        with self._lock:
            connection = self._connections.get(node_id)
        return connection if connection is not None and not connection.closed else None

    def connected(self, node_id: str) -> bool:
        return self.connection(node_id) is not None

    def disconnect(self, node_id: str) -> None:
        """Close a Node's live socket after it was disabled or deleted.

        The Node's reconnect is then refused at ``hello`` until it is enabled
        again or re-paired.
        """
        connection = self.connection(node_id)
        if connection is None:
            return
        connection.fail_all()
        try:
            asyncio.run_coroutine_threadsafe(
                connection.websocket.close(code=4403), connection.loop)
        except RuntimeError:
            pass  # The gateway loop already stopped.

    def transport(self, node_id: str, *, timeout: float = 30) -> GatewayTransport:
        return GatewayTransport(self, node_id, timeout)

    def fingerprint(self) -> str:
        if self._fingerprint is None:
            self._fingerprint = pairing.gateway_fingerprint(self.service.state)
        return self._fingerprint

    def _throttled(self) -> bool:
        cutoff = time.monotonic() - 60
        while self._failures and self._failures[0] < cutoff:
            self._failures.popleft()
        return len(self._failures) >= MAX_FAILURES_PER_MINUTE

    async def _refuse(self, websocket: WebSocket, code: str) -> None:
        self._failures.append(time.monotonic())
        try:
            await websocket.send_json({"type": "error", "code": code})
            await websocket.close(code=4403)
        except Exception:  # noqa: BLE001 - the peer may already be gone
            pass

    async def endpoint(self, websocket: WebSocket) -> None:
        # Not a browser surface; a browser origin is never legitimate here.
        if websocket.headers.get("origin") is not None or self._throttled():
            await websocket.close(code=4403)
            return
        await websocket.accept()
        nonce = secrets.token_hex(32)
        fingerprint = self.fingerprint()
        await websocket.send_json({"type": "challenge", "nonce": nonce,
                                   "protocol": pairing.PROTOCOL})
        try:
            message = await asyncio.wait_for(websocket.receive_json(), AUTH_TIMEOUT)
        except (asyncio.TimeoutError, WebSocketDisconnect, ValueError, KeyError, RuntimeError):
            await self._refuse(websocket, "auth_timeout")
            return
        if not isinstance(message, dict):
            await self._refuse(websocket, "protocol_error")
            return
        if message.get("type") == "pair":
            await self._pair(websocket, message, nonce, fingerprint)
            return
        if message.get("type") != "hello":
            await self._refuse(websocket, "protocol_error")
            return
        node_id = message.get("node_id")
        node = None
        if isinstance(node_id, str) and _NODE_ID.fullmatch(node_id):
            # Database work stays off the gateway loop: worker threads may hold
            # the service lock while they wait for this loop to deliver replies.
            node = await asyncio.to_thread(self._paired_node, node_id)
        if (node is None or not node["enabled"]
                or not pairing.verify(node["public_key"], str(message.get("signature", "")),
                                      "hello", nonce, fingerprint, node_id)):
            await self._refuse(websocket, "auth_failed")
            return
        connection = NodeConnection(node_id, websocket, asyncio.get_running_loop())
        with self._lock:
            previous = self._connections.get(node_id)
            self._connections[node_id] = connection
        if previous is not None:
            previous.fail_all()
            try:
                await previous.websocket.close(code=4000)
            except Exception:  # noqa: BLE001 - the replaced socket may be gone
                pass
        await websocket.send_json({"type": "ready", "node_id": node_id})
        await asyncio.to_thread(self._record, node_id, "connected")
        heartbeat = asyncio.create_task(self._heartbeat(connection))
        try:
            while True:
                frame = json.loads(await websocket.receive_text())
                if isinstance(frame, dict) and frame.get("type") == "response":
                    connection.resolve(frame)
        except (WebSocketDisconnect, ValueError, KeyError, RuntimeError):
            pass
        finally:
            heartbeat.cancel()
            connection.fail_all()
            with self._lock:
                current = self._connections.get(node_id) is connection
                if current:
                    del self._connections[node_id]
            # A replaced socket must not mark the Node's newer connection gone.
            if current:
                await asyncio.to_thread(self._record, node_id, "disconnected")

    async def _pair(self, websocket: WebSocket, message: dict, nonce: str,
                    fingerprint: str) -> None:
        code, public_key = message.get("code"), message.get("public_key")
        if (not isinstance(code, str) or not re.fullmatch(r"[0-9a-f]{32}", code)
                or not pairing.valid_public_key(public_key)
                or not pairing.verify(public_key, str(message.get("signature", "")),
                                      "pair", nonce, fingerprint, pairing.code_hash(code))):
            await self._refuse(websocket, "pairing_rejected")
            return
        try:
            node = await asyncio.to_thread(self._redeem, code, public_key)
        except BridgeError as exc:
            await self._refuse(websocket, exc.code)
            return
        await websocket.send_json({"type": "paired", "node_id": node["id"],
                                   "name": node["name"]})
        await websocket.close()

    async def _heartbeat(self, connection: NodeConnection) -> None:
        while not connection.closed:
            await asyncio.sleep(HEARTBEAT_SECONDS)
            if not connection.closed:
                await asyncio.to_thread(self._record, connection.node_id, "seen")

    def _record(self, node_id: str, event: str) -> None:
        """Persist connection state for Doctor and the Manager; never raises."""
        stamp = _now().isoformat()
        statements = {
            "connected": ("UPDATE nodes SET connected_at=?,last_seen=? WHERE id=?", (stamp, stamp, node_id)),
            "seen": ("UPDATE nodes SET last_seen=? WHERE id=?", (stamp, node_id)),
            "disconnected": ("UPDATE nodes SET disconnected_at=?,last_seen=? WHERE id=?",
                             (stamp, stamp, node_id)),
        }
        sql, params = statements[event]
        try:
            with self.service.lock, self.service.db:
                self.service.db.execute(sql, params)
        except Exception:  # noqa: BLE001 - observability must not break the connection
            pass

    def _paired_node(self, node_id: str) -> dict | None:
        with self.service.lock:
            row = self.service.db.execute(
                "SELECT * FROM nodes WHERE id=? AND transport='paired'", (node_id,)).fetchone()
        return dict(row) if row is not None else None

    def _redeem(self, code: str, public_key: str) -> dict:
        with self.service.lock:
            node = redeem_pairing(self.service.db, code, public_key)
        self.service.event(None, "node_paired")
        # A re-paired Node has a new revision; runs bound to the old key stop.
        self.service.run_coordinator.wake()
        return node

    def app(self) -> Starlette:
        return Starlette(routes=[WebSocketRoute(pairing.GATEWAY_PATH, self.endpoint)])
