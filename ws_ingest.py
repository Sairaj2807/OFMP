"""Angel One WebSocket Streaming 2.0 client.

Connects to wss://smartapisocket.angelone.in/smart-stream, subscribes to a
single NSE_FO token in Snap Quote (mode=3) so every tick carries best-5 depth
+ LTP + cumulative volume + OI in one packet — the "single source of truth
per tick" the design doc calls for (no mixing depth from one message with a
trade print from another).
"""
import asyncio
import struct
import time

import websockets

import angel_client
import config


def _parse_best_five(buf: bytes, offset: int, count: int = 10):
    """20-byte sub-packets: flag(int16), qty(int64), price(int64,paise), orders(int16)."""
    buys, sells = [], []
    for i in range(count):
        base = offset + i * 20
        flag, qty, price_paise, orders = struct.unpack_from("<hqqh", buf, base)
        price = price_paise / 100.0
        level = (price, qty, orders)
        if flag == 1:
            buys.append(level)
        else:
            sells.append(level)
    return buys, sells


def parse_snap_quote(buf: bytes) -> dict:
    """Parses a Snap Quote (mode=3) binary packet per the WS 2.0 response contract."""
    mode, exch_type = struct.unpack_from("<bb", buf, 0)
    token_raw = buf[2:27].split(b"\x00", 1)[0].decode("utf-8", errors="ignore")
    seq_no, exch_ts = struct.unpack_from("<qq", buf, 27)
    ltp_paise, ltq, avg_price_paise, volume = struct.unpack_from("<qqqq", buf, 43)
    tot_buy_qty, tot_sell_qty = struct.unpack_from("<dd", buf, 75)
    open_p, high_p, low_p, close_p = struct.unpack_from("<qqqq", buf, 91)
    ltt, oi = struct.unpack_from("<qq", buf, 123)

    buys, sells = _parse_best_five(buf, 147)

    result = {
        "mode": mode,
        "exchange_type": exch_type,
        "token": token_raw,
        "sequence": seq_no,
        "exch_timestamp_ms": exch_ts,
        "ltp": ltp_paise / 100.0,
        "last_traded_qty": ltq,
        "avg_price": avg_price_paise / 100.0,
        "cum_volume": volume,
        "tot_buy_qty": tot_buy_qty,
        "tot_sell_qty": tot_sell_qty,
        "open": open_p / 100.0,
        "high": high_p / 100.0,
        "low": low_p / 100.0,
        "close": close_p / 100.0,
        "ltt": ltt * 1000 if ltt < 10_000_000_000 else ltt,  # seconds -> ms if needed
        "open_interest": oi,
        "depth_buy": buys,
        "depth_sell": sells,
    }
    return result


class AngelWebSocketClient:
    def __init__(self, contract: dict, on_tick, on_status=None):
        self.contract = contract
        self.on_tick = on_tick
        self.on_status = on_status or (lambda **kw: None)
        self._stop = False

    def stop(self):
        self._stop = True

    async def _connect_headers(self, tokens: dict) -> dict:
        return {
            "Authorization": f"Bearer {tokens['jwt_token']}",
            "x-api-key": angel_client.autologin.API_KEY,
            "x-client-code": angel_client.autologin.CLIENT_CODE,
            "x-feed-token": tokens["feed_token"],
        }

    async def run(self):
        while not self._stop:
            try:
                await self._run_once()
            except Exception as e:
                self.on_status(connected=False, error=str(e))
                print(f"[ws_ingest] connection error: {e!r} — reconnecting in "
                      f"{config.RECONNECT_DELAY_SEC}s")
            if self._stop:
                break
            await asyncio.sleep(config.RECONNECT_DELAY_SEC)

    async def _run_once(self):
        tokens = angel_client.ensure_valid_token()
        headers = await self._connect_headers(tokens)

        connect_kwargs = dict(open_timeout=15, ping_interval=None)
        try:
            ws = await websockets.connect(config.WS_URL, additional_headers=headers,
                                           **connect_kwargs)
        except TypeError:
            ws = await websockets.connect(config.WS_URL, extra_headers=headers,
                                           **connect_kwargs)

        async with ws:
            exch_code = config.EXCHANGE_TYPE_CODES["NSE_FO"]
            subscribe_msg = {
                "correlationID": "orderflow01",
                "action": 1,
                "params": {
                    "mode": 3,
                    "tokenList": [
                        {"exchangeType": exch_code, "tokens": [self.contract["token"]]}
                    ],
                },
            }
            await ws.send(_json_dumps(subscribe_msg))
            self.on_status(connected=True, error=None)
            print(f"[ws_ingest] Subscribed to {self.contract['tradingsymbol']} "
                  f"(token {self.contract['token']}) in Snap Quote mode")

            last_ping = time.time()
            while not self._stop:
                timeout = max(0.1, config.HEARTBEAT_INTERVAL_SEC - (time.time() - last_ping))
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=timeout)
                except asyncio.TimeoutError:
                    await ws.send("ping")
                    last_ping = time.time()
                    continue

                if isinstance(msg, (bytes, bytearray)):
                    if len(msg) < 147 + 200:
                        continue  # not a full snap-quote packet, ignore
                    try:
                        tick = parse_snap_quote(msg)
                    except struct.error:
                        continue
                    self.on_tick(tick)
                else:
                    # text frame: "pong", or an error JSON
                    if msg != "pong":
                        print(f"[ws_ingest] text message: {msg}")


def _json_dumps(obj) -> str:
    import json
    return json.dumps(obj)
