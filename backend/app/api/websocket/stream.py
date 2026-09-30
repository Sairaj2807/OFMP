"""Authenticated real-time stream: /ws/v1/stream.

Protocol (JSON text frames):

  client -> server
    {"action": "auth", "token": "<access token>"}      only if no ofmp_access cookie; first message, within 5 s
    {"action": "subscribe", "streams": ["chart"], "ppr": 1, "interval": 60, "contract": "NIFTY27OCT26FUT"}
    {"action": "unsubscribe"}
    {"action": "pong"}  /  {"action": "ping"}

  server -> client: {"type": ..., "seq": n, "ts": <server epoch ms>, "data": ...}
    welcome | subscribed | snapshot | ping | pong | error

Guarantees and limits:
  - Origin must match the Host (or be an allowed CORS origin): blocks cross-site WebSocket hijacking.
  - Requires the market.read permission; the session is re-checked on every heartbeat, so a
    logout / revoked session closes the socket (code 4401).
  - Per-user and global connection limits (4429). Client frames over 4 KB close the socket (1009).
  - seq increases by one per message on a connection; a gap means nothing (messages are never
    skipped mid-send), a coalesced snapshot is simply the newer state.
  - Backpressure: each connection holds at most ONE pending snapshot. If the client reads slower
    than snapshots are produced, older pending snapshots are replaced by newer ones (counted in
    `snapshots_coalesced`); memory per connection stays constant.
  - Idle connections (nothing received for idle_timeout_sec, pongs included) are closed (4408).
"""
import asyncio
import contextlib
import json
import logging
import time
from typing import Awaitable, Callable, Literal, Optional, Union
from urllib.parse import urlparse

from fastapi import WebSocket, WebSocketDisconnect
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from backend.app.core.permissions import has_permission

log = logging.getLogger(__name__)

STREAMS = ("chart",)
MAX_CLIENT_FRAME = 4096

CLOSE_UNAUTHENTICATED = 4401
CLOSE_FORBIDDEN = 4403
CLOSE_IDLE = 4408
CLOSE_LIMIT = 4429


class _Msg(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AuthMsg(_Msg):
    action: Literal["auth"]
    token: str = Field(min_length=10, max_length=2048)


class SubscribeMsg(_Msg):
    action: Literal["subscribe"]
    streams: list[Literal["chart"]] = Field(min_length=1, max_length=len(STREAMS))
    ppr: int = Field(1, ge=1, le=5)
    interval: int = 60
    contract: Optional[str] = Field(None, max_length=64)


class UnsubscribeMsg(_Msg):
    action: Literal["unsubscribe"]


class PingMsg(_Msg):
    action: Literal["ping", "pong"]


ClientMessage = TypeAdapter(Union[AuthMsg, SubscribeMsg, UnsubscribeMsg, PingMsg])


def _now_ms() -> int:
    return int(time.time() * 1000)


class StreamConnection:
    def __init__(self, ws: WebSocket, user, token: str):
        self.ws, self.user, self.token = ws, user, token
        self.subscription: Optional[tuple] = None      # (ppr, interval)
        self.seq = 0
        self.last_seen = time.monotonic()
        self.snapshots_coalesced = 0
        self._pending = None
        self._wake = asyncio.Event()
        self._send_lock = asyncio.Lock()

    async def send(self, type_: str, data=None) -> None:
        async with self._send_lock:
            self.seq += 1
            await self.ws.send_text(json.dumps({"type": type_, "seq": self.seq, "ts": _now_ms(), "data": data},
                                               default=str))

    def offer_snapshot(self, text_payload: dict) -> None:
        if self._pending is not None:
            self.snapshots_coalesced += 1
        self._pending = text_payload
        self._wake.set()

    async def sender(self) -> None:
        while True:
            await self._wake.wait()
            self._wake.clear()
            payload, self._pending = self._pending, None
            if payload is not None:
                await self.send("snapshot", payload)


class StreamHub:
    def __init__(self, gateway, authenticate: Callable[[str], Awaitable[Optional[object]]],
                 allowed_intervals: tuple, allowed_origins: tuple = (), max_per_user: int = 5,
                 max_total: int = 1000, push_interval_sec: float = 0.5, heartbeat_sec: float = 15.0,
                 idle_timeout_sec: float = 45.0, auth_timeout_sec: float = 5.0, access_cookie: str = "ofmp_access"):
        self.gateway = gateway
        self.authenticate = authenticate
        self.allowed_intervals = tuple(allowed_intervals)
        self.allowed_origins = {o.rstrip("/") for o in allowed_origins}
        self.max_per_user, self.max_total = max_per_user, max_total
        self.push_interval_sec, self.heartbeat_sec = push_interval_sec, heartbeat_sec
        self.idle_timeout_sec, self.auth_timeout_sec = idle_timeout_sec, auth_timeout_sec
        self.access_cookie = access_cookie
        self.connections: set = set()
        self.rejected = {"origin": 0, "auth": 0, "limit": 0}

    # -- connection lifecycle ------------------------------------------------------

    def _origin_allowed(self, ws: WebSocket) -> bool:
        origin = ws.headers.get("origin")
        if origin is None:
            return True                              # non-browser client; still needs a valid token
        origin = origin.rstrip("/")
        if origin in self.allowed_origins:
            return True
        return urlparse(origin).netloc == ws.headers.get("host")

    async def handle(self, ws: WebSocket) -> None:
        if not self._origin_allowed(ws):
            self.rejected["origin"] += 1
            await ws.close(code=CLOSE_FORBIDDEN)
            return
        await ws.accept()
        token = ws.cookies.get(self.access_cookie)
        if not token:
            try:
                first = await asyncio.wait_for(ws.receive_text(), timeout=self.auth_timeout_sec)
                msg = ClientMessage.validate_json(first) if len(first) <= MAX_CLIENT_FRAME else None
                token = msg.token if isinstance(msg, AuthMsg) else None
            except (asyncio.TimeoutError, ValidationError, WebSocketDisconnect):
                token = None
        user = await self.authenticate(token) if token else None
        if user is None or not has_permission(user.role, "market.read"):
            self.rejected["auth"] += 1
            await ws.close(code=CLOSE_UNAUTHENTICATED if user is None else CLOSE_FORBIDDEN)
            return
        if (len(self.connections) >= self.max_total
                or sum(1 for c in self.connections if c.user.id == user.id) >= self.max_per_user):
            self.rejected["limit"] += 1
            await ws.close(code=CLOSE_LIMIT)
            return

        conn = StreamConnection(ws, user, token)
        self.connections.add(conn)
        sender = asyncio.create_task(conn.sender())
        try:
            await conn.send("welcome", {"user": user.public(), "streams": list(STREAMS),
                                        "intervals": list(self.allowed_intervals), "heartbeat_sec": self.heartbeat_sec})
            await self._read_loop(conn)
        except WebSocketDisconnect:
            pass
        finally:
            self.connections.discard(conn)
            sender.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await sender

    async def _read_loop(self, conn: StreamConnection) -> None:
        while True:
            text = await conn.ws.receive_text()
            conn.last_seen = time.monotonic()
            if len(text) > MAX_CLIENT_FRAME:
                await conn.ws.close(code=1009)
                return
            try:
                msg = ClientMessage.validate_json(text)
            except ValidationError as e:
                await conn.send("error", {"code": "INVALID_MESSAGE",
                                          "message": "; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}"
                                                               for x in e.errors()[:3])})
                continue
            if isinstance(msg, SubscribeMsg):
                await self._subscribe(conn, msg)
            elif isinstance(msg, UnsubscribeMsg):
                conn.subscription = None
                await conn.send("unsubscribed")
            elif isinstance(msg, PingMsg) and msg.action == "ping":
                await conn.send("pong")
            elif isinstance(msg, AuthMsg):
                await conn.send("error", {"code": "ALREADY_AUTHENTICATED", "message": "connection is authenticated"})

    async def _subscribe(self, conn: StreamConnection, msg: SubscribeMsg) -> None:
        if msg.interval not in self.allowed_intervals:
            await conn.send("error", {"code": "INVALID_INTERVAL", "message": f"interval must be one of {list(self.allowed_intervals)}"})
            return
        active = self.gateway.active_contract()
        if msg.contract is not None and (active is None or msg.contract != active.get("tradingsymbol")):
            await conn.send("error", {"code": "CONTRACT_NOT_AVAILABLE",
                                      "message": "that contract is not streaming; subscribe to the active contract"})
            return
        conn.subscription = (msg.ppr, msg.interval)
        await conn.send("subscribed", {"streams": msg.streams, "ppr": msg.ppr, "interval": msg.interval,
                                       "contract": active.get("tradingsymbol") if active else None})
        conn.offer_snapshot(self.gateway.chart_snapshot(msg.ppr, msg.interval))

    # -- background loops ------------------------------------------------------------------

    def push_once(self) -> None:
        """Build each distinct subscription's snapshot once and offer it to its subscribers."""
        cache = {}
        for conn in list(self.connections):
            if conn.subscription is None:
                continue
            if conn.subscription not in cache:
                cache[conn.subscription] = self.gateway.chart_snapshot(*conn.subscription)
            conn.offer_snapshot(cache[conn.subscription])

    async def heartbeat_once(self) -> None:
        now = time.monotonic()
        for conn in list(self.connections):
            try:
                if now - conn.last_seen > self.idle_timeout_sec:
                    await conn.ws.close(code=CLOSE_IDLE)
                    continue
                if await self.authenticate(conn.token) is None:
                    await conn.ws.close(code=CLOSE_UNAUTHENTICATED)
                    continue
                await conn.send("ping")
            except Exception as e:              # a broken socket is cleaned up by its own handler
                log.debug("heartbeat failed: %r", e)

    async def run(self) -> None:
        next_heartbeat = time.monotonic() + self.heartbeat_sec
        while True:
            await asyncio.sleep(self.push_interval_sec)
            try:
                self.push_once()
                if time.monotonic() >= next_heartbeat:
                    next_heartbeat = time.monotonic() + self.heartbeat_sec
                    await self.heartbeat_once()
            except Exception:
                log.exception("stream hub loop error")

    def stats(self) -> dict:
        return {"connections": len(self.connections),
                "subscribed": sum(1 for c in self.connections if c.subscription is not None),
                "users": len({c.user.id for c in self.connections}),
                "snapshots_coalesced": sum(c.snapshots_coalesced for c in self.connections),
                "rejected": dict(self.rejected)}
