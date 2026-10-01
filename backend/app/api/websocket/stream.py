"""Authenticated real-time stream: /ws/v1/stream.

Protocol (JSON text frames):

  client -> server
    {"action": "auth", "token": "<access token>"}      only if no ofmp_access cookie; first message, within 5 s
    {"action": "subscribe", "id": "c1", "streams": ["chart"], "ppr": 1, "interval": 60, "contract": "NIFTY27OCT26FUT"}
    {"action": "unsubscribe", "id": "c1"}         (no id: drop every subscription)
    {"action": "pong"}  /  {"action": "ping"}
    {"action": "replay", "id": "c1", "date": "2026-09-29", "ppr": 1, "interval": 60, "speed": 10,
     "autoplay": true, "at_ms": null}          replay a stored session on this chart id
    {"action": "replay_control", "id": "c1", "command": "play" | "pause" | "speed" | "seek" | "step",
     "value": <speed or epoch ms>, "unit": "trade" | "candle"}

  One connection carries up to MAX_SUBSCRIPTIONS subscriptions (e.g. one per chart
  in a multi-chart layout), of which at most MAX_REPLAYS replays; re-subscribing an
  existing id replaces its settings. An id is either live or replaying, never both.
  Re-sending "replay" for the same date keeps the playback position (settings change only).

  server -> client: {"type": ..., "seq": n, "ts": <server epoch ms>, "id"?: <subscription id>, "data": ...}
    welcome | subscribed | replay_started | unsubscribed | snapshot | ping | pong | error
    alert         one of the user's alert rules fired (data = the alert event); sent to every
                  open connection of that user, no subscription needed
  Replay snapshots carry data.replay = {date, start_ms, end_ms, cursor_ms, index, total, playing, speed, speeds}.

Guarantees and limits:
  - Origin must match the Host (or be an allowed CORS origin): blocks cross-site WebSocket hijacking.
  - Requires the market.read permission; the session is re-checked on every heartbeat, so a
    logout / revoked session closes the socket (code 4401).
  - Per-user and global connection limits (4429). Client frames over 4 KB close the socket (1009).
  - seq increases by one per message on a connection; a gap means nothing (messages are never
    skipped mid-send), a coalesced snapshot is simply the newer state.
  - Backpressure: each subscription holds at most ONE pending snapshot. If the client reads slower
    than snapshots are produced, older pending snapshots are replaced by newer ones (counted in
    `snapshots_coalesced`); memory per connection stays bounded.
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

from backend.app.core import metrics
from backend.app.core.permissions import has_permission

log = logging.getLogger(__name__)

STREAMS = ("chart",)
MAX_CLIENT_FRAME = 4096
MAX_SUBSCRIPTIONS = 6
MAX_REPLAYS = 2
REPLAY_SPEEDS = (1, 2, 5, 10, 25, 50)
SUB_ID_PATTERN = r"^[A-Za-z0-9_-]{1,32}$"

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
    id: str = Field("default", pattern=SUB_ID_PATTERN)
    streams: list[Literal["chart"]] = Field(min_length=1, max_length=len(STREAMS))
    ppr: int = Field(1, ge=1, le=5)
    interval: int = 60
    contract: Optional[str] = Field(None, max_length=64)


class UnsubscribeMsg(_Msg):
    action: Literal["unsubscribe"]
    id: Optional[str] = Field(None, pattern=SUB_ID_PATTERN)


class PingMsg(_Msg):
    action: Literal["ping", "pong"]


class ReplayMsg(_Msg):
    action: Literal["replay"]
    id: str = Field("default", pattern=SUB_ID_PATTERN)
    date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    ppr: int = Field(1, ge=1, le=5)
    interval: int = 60
    speed: int = 10
    autoplay: bool = True
    at_ms: Optional[int] = Field(None, ge=0)


class ReplayControlMsg(_Msg):
    action: Literal["replay_control"]
    id: str = Field("default", pattern=SUB_ID_PATTERN)
    command: Literal["play", "pause", "speed", "seek", "step"]
    value: Optional[int] = Field(None, ge=0)
    unit: Optional[Literal["trade", "candle"]] = None


ClientMessage = TypeAdapter(Union[AuthMsg, SubscribeMsg, UnsubscribeMsg, PingMsg, ReplayMsg, ReplayControlMsg])


def _now_ms() -> int:
    return int(time.time() * 1000)


class StreamConnection:
    def __init__(self, ws: WebSocket, user, token: str):
        self.ws, self.user, self.token = ws, user, token
        self.subscriptions: dict = {}      # live: id -> (ppr, interval)
        self.replays: dict = {}            # replay: id -> {"session": ReplaySession, "ppr": .., "interval": ..}
        self.seq = 0
        self.last_seen = time.monotonic()
        self.snapshots_coalesced = 0
        self._pending: dict = {}           # id -> latest unsent snapshot
        self._wake = asyncio.Event()
        self._send_lock = asyncio.Lock()

    async def send(self, type_: str, data=None, sub_id: Optional[str] = None) -> None:
        msg = {"type": type_, "seq": 0, "ts": 0, "data": data}
        if sub_id is not None:
            msg["id"] = sub_id
        async with self._send_lock:
            self.seq += 1
            msg["seq"], msg["ts"] = self.seq, _now_ms()
            await self.ws.send_text(json.dumps(msg, default=str))
        metrics.WS_MESSAGES.labels(type_).inc()

    def offer_snapshot(self, sub_id: str, payload: dict) -> None:
        if sub_id in self._pending:
            self.snapshots_coalesced += 1
            metrics.WS_COALESCED.inc()
        self._pending[sub_id] = payload
        self._wake.set()

    def drop(self, sub_id: Optional[str]) -> None:
        if sub_id is None:
            self.subscriptions.clear()
            self.replays.clear()
            self._pending.clear()
        else:
            self.subscriptions.pop(sub_id, None)
            self.replays.pop(sub_id, None)
            self._pending.pop(sub_id, None)

    def ids(self) -> set:
        return set(self.subscriptions) | set(self.replays)

    async def sender(self) -> None:
        while True:
            await self._wake.wait()
            self._wake.clear()
            pending, self._pending = self._pending, {}
            for sub_id, payload in pending.items():
                if sub_id in self.subscriptions or sub_id in self.replays:   # skip just-dropped ids
                    await self.send("snapshot", payload, sub_id)


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
            metrics.WS_REJECTED.labels("origin").inc()
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
            metrics.WS_REJECTED.labels("auth").inc()
            await ws.close(code=CLOSE_UNAUTHENTICATED if user is None else CLOSE_FORBIDDEN)
            return
        if (len(self.connections) >= self.max_total
                or sum(1 for c in self.connections if c.user.id == user.id) >= self.max_per_user):
            self.rejected["limit"] += 1
            metrics.WS_REJECTED.labels("limit").inc()
            await ws.close(code=CLOSE_LIMIT)
            return

        conn = StreamConnection(ws, user, token)
        self.connections.add(conn)
        metrics.WS_CONNECTIONS.set(len(self.connections))
        sender = asyncio.create_task(conn.sender())
        try:
            await conn.send("welcome", {"user": user.public(), "streams": list(STREAMS),
                                        "intervals": list(self.allowed_intervals), "heartbeat_sec": self.heartbeat_sec})
            await self._read_loop(conn)
        except WebSocketDisconnect:
            pass
        finally:
            self.connections.discard(conn)
            metrics.WS_CONNECTIONS.set(len(self.connections))
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
                conn.drop(msg.id)
                await conn.send("unsubscribed", sub_id=msg.id)
            elif isinstance(msg, PingMsg) and msg.action == "ping":
                await conn.send("pong")
            elif isinstance(msg, ReplayMsg):
                await self._start_replay(conn, msg)
            elif isinstance(msg, ReplayControlMsg):
                await self._control_replay(conn, msg)
            elif isinstance(msg, AuthMsg):
                await conn.send("error", {"code": "ALREADY_AUTHENTICATED", "message": "connection is authenticated"})

    async def _subscribe(self, conn: StreamConnection, msg: SubscribeMsg) -> None:
        if msg.interval not in self.allowed_intervals:
            await conn.send("error", {"code": "INVALID_INTERVAL",
                                      "message": f"interval must be one of {list(self.allowed_intervals)}"}, msg.id)
            return
        if msg.id not in conn.ids() and len(conn.ids()) >= MAX_SUBSCRIPTIONS:
            await conn.send("error", {"code": "TOO_MANY_SUBSCRIPTIONS",
                                      "message": f"at most {MAX_SUBSCRIPTIONS} subscriptions per connection"}, msg.id)
            return
        active = self.gateway.active_contract()
        if msg.contract is not None and (active is None or msg.contract != active.get("tradingsymbol")):
            await conn.send("error", {"code": "CONTRACT_NOT_AVAILABLE",
                                      "message": "that contract is not streaming; subscribe to the active contract"}, msg.id)
            return
        conn.replays.pop(msg.id, None)                  # back to live
        conn.subscriptions[msg.id] = (msg.ppr, msg.interval)
        await conn.send("subscribed", {"streams": msg.streams, "ppr": msg.ppr, "interval": msg.interval,
                                       "contract": active.get("tradingsymbol") if active else None}, msg.id)
        conn.offer_snapshot(msg.id, self.gateway.chart_snapshot(msg.ppr, msg.interval))

    # -- replay ---------------------------------------------------------------------------

    async def _start_replay(self, conn: StreamConnection, msg: ReplayMsg) -> None:
        err = lambda code, text: conn.send("error", {"code": code, "message": text}, msg.id)  # noqa: E731
        if msg.interval not in self.allowed_intervals:
            return await err("INVALID_INTERVAL", f"interval must be one of {list(self.allowed_intervals)}")
        if msg.speed not in REPLAY_SPEEDS:
            return await err("INVALID_SPEED", f"speed must be one of {list(REPLAY_SPEEDS)}")
        existing = conn.replays.get(msg.id)
        if existing and existing["session"].date == msg.date:   # same day: keep position, new settings
            existing.update(ppr=msg.ppr, interval=msg.interval)
            conn.offer_snapshot(msg.id, self.gateway.replay_snapshot(existing["session"], msg.ppr, msg.interval))
            return
        if msg.id not in conn.ids() and len(conn.ids()) >= MAX_SUBSCRIPTIONS:
            return await err("TOO_MANY_SUBSCRIPTIONS", f"at most {MAX_SUBSCRIPTIONS} subscriptions per connection")
        if msg.id not in conn.replays and len(conn.replays) >= MAX_REPLAYS:
            return await err("TOO_MANY_REPLAYS", f"at most {MAX_REPLAYS} replays per connection")
        try:
            session = await self.gateway.open_replay(msg.date)
        except Exception as e:
            return await err("REPLAY_UNAVAILABLE", str(e) or "session could not be loaded")
        session.set_speed(msg.speed)
        if msg.at_ms is not None:
            session.seek(msg.at_ms)
        if msg.autoplay:
            session.play()
        conn.subscriptions.pop(msg.id, None)            # this id now replays
        conn.replays[msg.id] = {"session": session, "ppr": msg.ppr, "interval": msg.interval}
        await conn.send("replay_started", session.meta(), msg.id)
        conn.offer_snapshot(msg.id, self.gateway.replay_snapshot(session, msg.ppr, msg.interval))

    async def _control_replay(self, conn: StreamConnection, msg: ReplayControlMsg) -> None:
        entry = conn.replays.get(msg.id)
        if entry is None:
            await conn.send("error", {"code": "NO_REPLAY", "message": "no replay on this id"}, msg.id)
            return
        session = entry["session"]
        try:
            if msg.command == "play":
                session.play()
            elif msg.command == "pause":
                session.pause()
            elif msg.command == "speed":
                session.set_speed(int(msg.value or 0))
            elif msg.command == "seek":
                if msg.value is None:
                    raise ValueError("seek needs value (epoch ms)")
                session.seek(msg.value)
            elif msg.command == "step":
                session.step(msg.unit or "trade")
        except ValueError as e:
            await conn.send("error", {"code": "INVALID_CONTROL", "message": str(e)}, msg.id)
            return
        conn.offer_snapshot(msg.id, self.gateway.replay_snapshot(session, entry["ppr"], entry["interval"]))

    # -- user notifications -----------------------------------------------------------------

    async def notify_user(self, user_id: str, type_: str, data) -> bool:
        """Sends one message to every open connection of `user_id` (alerts).
        True if at least one connection received it."""
        sent = False
        for conn in [c for c in self.connections if str(c.user.id) == str(user_id)]:
            try:
                await asyncio.wait_for(conn.send(type_, data), timeout=2.0)
                sent = True
            except Exception as e:              # a broken or stalled socket is cleaned up by its own handler
                log.debug("notify failed: %r", e)
        return sent

    # -- background loops ------------------------------------------------------------------

    def push_once(self) -> None:
        """Build each distinct subscription's snapshot once and offer it to its subscribers."""
        cache = {}
        metrics.WS_SUBSCRIPTIONS.set(sum(len(c.ids()) for c in self.connections))
        for conn in list(self.connections):
            for sub_id, settings in list(conn.subscriptions.items()):
                if settings not in cache:
                    cache[settings] = self.gateway.chart_snapshot(*settings)
                conn.offer_snapshot(sub_id, cache[settings])
            for sub_id, entry in list(conn.replays.items()):
                session = entry["session"]
                was_playing = session.playing
                session.advance()
                if was_playing or session.playing:      # paused replays only change on a control message
                    conn.offer_snapshot(sub_id, self.gateway.replay_snapshot(session, entry["ppr"], entry["interval"]))

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
                "subscriptions": sum(len(c.subscriptions) for c in self.connections),
                "replays": sum(len(c.replays) for c in self.connections),
                "users": len({c.user.id for c in self.connections}),
                "snapshots_coalesced": sum(c.snapshots_coalesced for c in self.connections),
                "rejected": dict(self.rejected)}
