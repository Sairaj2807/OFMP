"""Synthetic market-data provider for development and demos: a random-walk
NIFTY-like futures feed (best-5 depth, cumulative volume, trades at the bid,
ask or inside the spread) with no broker, credentials or market hours.

Never use it for anything recorded: the server disables observation logging,
the raw tick archive and database trade writes while it is active, and
refuses to start with it in production."""
import asyncio
import random
import time
from typing import Iterable, Optional

from backend.app.domain.market_data import DepthLevel, InstrumentRef, MarketTick
from backend.app.services.market_data.provider import MarketDataProvider

SYNTHETIC_CONTRACT = {
    "token": "SYNTH1", "tradingsymbol": "NIFTYSYNTHFUT", "name": "NIFTY", "expiry": "SYNTHETIC",
    "tick_size": 0.1, "lotsize": 65, "exch_seg": "NFO",
}


class SyntheticProvider(MarketDataProvider):
    name = "synthetic"

    def __init__(self, interval_sec: float = 0.25, start_price: float = 24000.0, seed: Optional[int] = None,
                 tick: float = 0.1, lot: int = 65):
        self.interval_sec = interval_sec
        self.rng = random.Random(seed)
        self.mid = start_price
        self.tick, self.lot = tick, lot
        self.cum_volume = 0
        self.seq = 0
        self.tokens: set = set()
        self.connected = False

    async def connect(self) -> None:
        self.connected = True

    async def subscribe(self, instruments: Iterable[InstrumentRef]) -> None:
        self.tokens |= {i.token for i in instruments}

    async def unsubscribe(self, instruments: Iterable[InstrumentRef]) -> None:
        self.tokens -= {i.token for i in instruments}

    async def disconnect(self) -> None:
        self.connected = False

    def next_tick(self, token: str, now_ms: int) -> MarketTick:
        r = self.rng
        self.seq += 1
        # mean-reverting random walk with occasional bursts
        drift = r.choice((-2, -1, -1, 0, 0, 0, 1, 1, 2)) * (3 if r.random() < 0.03 else 1)
        self.mid = round(self.mid + drift * self.tick, 1)
        half = r.randint(1, 4) * self.tick
        bid, ask = round(self.mid - half, 1), round(self.mid + half, 1)
        if r.random() < 0.7:
            self.cum_volume += r.randint(1, 25) * self.lot
            ltp = r.choice((bid, ask, round(bid + self.tick, 1), round(ask - self.tick, 1)))
        else:
            ltp = self.mid
        depth = lambda base, sign: tuple(
            DepthLevel(round(base + sign * k * self.tick, 1), r.randint(1, 60) * self.lot, r.randint(1, 12)) for k in range(5))
        return MarketTick(provider=self.name, token=token, exchange_segment="NFO", sequence=self.seq,
                          exchange_ts_ms=now_ms, last_trade_ts_ms=now_ms - now_ms % 1000, received_ts_ms=now_ms,
                          ltp=ltp, last_traded_qty=None, cumulative_volume=self.cum_volume,
                          bids=depth(bid, -1), asks=depth(ask, 1), open=24000.0, high=None, low=None,
                          close=24000.0, open_interest=None)

    async def stream(self):
        while self.connected:
            await asyncio.sleep(self.interval_sec)
            now = int(time.time() * 1000)
            for token in sorted(self.tokens):
                yield self.next_tick(token, now)
