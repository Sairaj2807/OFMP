"""Angel One SmartAPI WebSocket 2.0 provider (Snap Quote, mode 3: LTP +
cumulative volume + best-5 depth + OI in one packet, so each tick is a
single consistent snapshot).

Connection lifecycle (retries, backoff, resubscribe) is ProviderRunner's
job; this class only speaks Angel's protocol."""
import asyncio
import json
import logging
import struct
import time
from typing import Callable, Iterable, Optional

import websockets

from backend.app.domain.market_data import InstrumentRef, MarketTick
from backend.app.services.market_data.provider import MarketDataProvider

from .parser import PROVIDER, SNAP_QUOTE_MIN_LEN, parse_snap_quote

log = logging.getLogger(__name__)

WS_URL = "wss://smartapisocket.angelone.in/smart-stream"
SNAP_QUOTE_MODE = 3
HEARTBEAT_INTERVAL_SEC = 30

# Angel exchangeType codes by scrip-master segment.
EXCHANGE_TYPES = {"NSE": 1, "NFO": 2, "BSE": 3, "BFO": 4, "MCX": 5, "NCDEX": 7, "CDS": 13}


def _default_credentials() -> dict:
    """jwt/feed tokens from angel_client plus the API key and client code."""
    import angel_client   # app-level module: imported lazily to keep this package importable alone
    tokens = angel_client.ensure_valid_token()
    return {"jwt_token": tokens["jwt_token"], "feed_token": tokens["feed_token"],
            "api_key": angel_client.autologin.API_KEY, "client_code": angel_client.autologin.CLIENT_CODE}


async def _default_connect(url: str, headers: dict):
    kwargs = dict(open_timeout=15, ping_interval=None)
    try:
        return await websockets.connect(url, additional_headers=headers, **kwargs)
    except TypeError:   # websockets < 14
        return await websockets.connect(url, extra_headers=headers, **kwargs)


class AngelOneProvider(MarketDataProvider):
    name = PROVIDER

    def __init__(self, credentials: Callable[[], dict] = _default_credentials,
                 connect: Callable = _default_connect, url: str = WS_URL,
                 heartbeat_sec: float = HEARTBEAT_INTERVAL_SEC,
                 clock_ms: Callable[[], int] = lambda: int(time.time() * 1000)):
        self._credentials = credentials
        self._connect = connect
        self._url = url
        self._heartbeat_sec = heartbeat_sec
        self._clock_ms = clock_ms
        self._ws = None
        self._segments: dict = {}   # token -> exchange segment, for tagging ticks

    async def connect(self) -> None:
        creds = await asyncio.to_thread(self._credentials)   # REST login/refresh is blocking
        headers = {
            "Authorization": f"Bearer {creds['jwt_token']}",
            "x-api-key": creds["api_key"],
            "x-client-code": creds["client_code"],
            "x-feed-token": creds["feed_token"],
        }
        self._ws = await self._connect(self._url, headers)

    async def _send_subscription(self, action: int, instruments: Iterable[InstrumentRef]) -> None:
        by_type: dict = {}
        for inst in instruments:
            by_type.setdefault(EXCHANGE_TYPES.get(inst.exchange_segment, 2), []).append(inst.token)
        if not by_type:
            return
        await self._ws.send(json.dumps({
            "correlationID": "orderflow01",
            "action": action,
            "params": {"mode": SNAP_QUOTE_MODE,
                       "tokenList": [{"exchangeType": t, "tokens": toks} for t, toks in by_type.items()]},
        }))

    async def subscribe(self, instruments: Iterable[InstrumentRef]) -> None:
        instruments = list(instruments)
        for inst in instruments:
            self._segments[inst.token] = inst.exchange_segment
        await self._send_subscription(1, instruments)

    async def unsubscribe(self, instruments: Iterable[InstrumentRef]) -> None:
        instruments = list(instruments)
        await self._send_subscription(0, instruments)
        for inst in instruments:
            self._segments.pop(inst.token, None)

    async def disconnect(self) -> None:
        ws, self._ws = self._ws, None
        if ws is not None:
            await ws.close()

    async def stream(self):
        ws = self._ws
        last_ping = time.monotonic()
        while True:
            timeout = max(0.1, self._heartbeat_sec - (time.monotonic() - last_ping))
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=timeout)
            except asyncio.TimeoutError:
                await ws.send("ping")
                last_ping = time.monotonic()
                continue
            received = self._clock_ms()
            self.on_message(received)

            if isinstance(msg, (bytes, bytearray)):
                if len(msg) < SNAP_QUOTE_MIN_LEN:
                    self.on_drop(f"short packet ({len(msg)} bytes)")
                    continue
                try:
                    tick = parse_snap_quote(msg, received)
                except struct.error as e:
                    self.on_drop(f"unparseable packet: {e}")
                    continue
                segment = self._segments.get(tick.token)
                yield tick if segment is None else _with_segment(tick, segment)
            elif msg != "pong":
                log.warning("angelone text message: %s", msg)


def _with_segment(tick: MarketTick, segment: str) -> MarketTick:
    from dataclasses import replace
    return replace(tick, exchange_segment=segment)
