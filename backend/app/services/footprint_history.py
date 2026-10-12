"""Historical footprint sessions: finished sessions of one contract as chart
bars, each day rebuilt by the existing engine and cached, for the multi-day
order-flow chart. Loaded one day at a time (the chart asks for older days as
the user scrolls back), because a day at 1-minute bars is ~200 KB.

Same policy as profile history (services/profile_history.py):
- only sessions tagged with exactly the requested contract; untagged
  (pre-tagging) sessions are listed as excluded, never guessed;
- only finished sessions (before the live session); today comes from the live
  engine;
- nothing is fabricated: a date without stored trades is absent.

Each day: stored trades (with their stored BUY/SELL side, never re-classified)
-> orderflow.restore_trades into a fresh TickProcessorState (the footprint,
candle rollover and CVD code the live engine runs) -> chart.chart_payload for
the whole day. CVD restarts at 0 at each session start (cvd_offset 0 for the
day's first bar), which is how the chart shows session CVD.

Cache (in memory, until restart; a finished session never changes): the
rebuilt engine state per (version, symbol, tick size, date) and the serialised
bars per row size and interval. FOOTPRINT_VERSION is part of every key.
"""
import logging
import time
from datetime import date
from typing import Awaitable, Callable, Optional

from backend.app.domain.orderflow import TickProcessorState, Trade, restore_trades
from backend.app.domain.orderflow.chart import chart_payload
from backend.app.domain.orderflow.settings import NATIVE_CANDLE_SEC

from .profile_history import _Lru

log = logging.getLogger("ofmp.footprint_history")

# Bump when the footprint/candle maths or the bar format changes.
FOOTPRINT_VERSION = "footprint-v1"
# Days that may be shown before today, by display interval (owner decision 2026-10-12).
DAY_CAPS = {60: 5, 180: 10}
DEFAULT_DAY_CAP = 20


def day_cap(interval_sec: int) -> int:
    return DAY_CAPS.get(interval_sec, DEFAULT_DAY_CAP)


class FootprintHistory:
    def __init__(self, list_dates: Callable[..., Awaitable[list]], load_days: Callable[..., Awaitable[dict]],
                 list_untagged: Callable[..., Awaitable[list]], max_states: int = 64, max_payloads: int = 512):
        """list_dates(symbol, before, limit, date_from) -> [date] oldest first;
        load_days(symbol, [date]) -> {date: [(ts_ms, price, qty, side)]};
        list_untagged(before, date_from) -> [date]."""
        self._list_dates, self._load_days, self._list_untagged = list_dates, load_days, list_untagged
        self._states = _Lru(max_states)
        self._payloads = _Lru(max_payloads)
        self.stats = {"days_loaded": 0, "payload_hits": 0, "payload_builds": 0}

    def clear(self, symbol: Optional[str] = None) -> None:
        for lru in (self._states, self._payloads):
            for key in [k for k in lru.data if symbol is None or k[1] == symbol]:
                del lru.data[key]

    async def sessions(self, symbol: str, before: date, limit: int) -> dict:
        """The finished sessions available before `before` (newest `limit`, oldest first) and
        the untagged sessions in that window (excluded, with the reason)."""
        dates = await self._list_dates(symbol, before, limit, None)
        window_start = dates[0] if len(dates) >= limit else None
        untagged = [d for d in await self._list_untagged(before, None)
                    if window_start is None or d >= window_start][-limit:]
        return {"symbol": symbol, "before": before.isoformat(), "version": FOOTPRINT_VERSION,
                "sessions": [d.isoformat() for d in dates],
                "excluded": [{"date": d.isoformat(), "reason": "recorded before contract tagging: contract unknown"}
                             for d in untagged]}

    async def _state(self, symbol: str, tick_size: float, day: date) -> Optional[TickProcessorState]:
        key = (FOOTPRINT_VERSION, symbol, tick_size, day)
        state = self._states.get(key)
        if state is None:
            trades = (await self._load_days(symbol, [day])).get(day, [])
            self.stats["days_loaded"] += 1
            if not trades:
                return None
            state = TickProcessorState(tick_size)
            restore_trades(state, [Trade(timestamp=ts, price=p, quantity=q, side=s) for ts, p, q, s in trades])
            self._states.put(key, state)
        return state

    async def day(self, symbol: str, tick_size: float, day: date, ppr: int, interval_sec: int) -> Optional[dict]:
        """One finished session as chart bars (None if nothing is stored for it)."""
        started = time.perf_counter()
        key = (FOOTPRINT_VERSION, symbol, tick_size, day, ppr, interval_sec)
        out = self._payloads.get(key)
        if out is not None:
            self.stats["payload_hits"] += 1
            return out
        state = await self._state(symbol, tick_size, day)
        if state is None:
            return None
        # a finished session: every native candle is closed; with the session's own closed CVD
        # (its total delta) the payload's cvd_offset is the CVD before the first bar: 0, the session start
        fp = state.footprint
        payload = chart_payload(fp, sum(fp.candle_delta(t) for t in fp.data), None, ppr, interval_sec=interval_sec,
                                limit=None, native_sec=NATIVE_CANDLE_SEC)
        out = {"date": day.isoformat(), "symbol": symbol, "version": FOOTPRINT_VERSION, "state": "historical",
               "interval_sec": interval_sec, "row_size": payload["row_size"], "cvd_start": payload["cvd_offset"],
               "bars": payload["bars"]}
        self._payloads.put(key, out)
        self.stats["payload_builds"] += 1
        log.info("footprint history symbol=%s date=%s ppr=%s interval=%s bars=%d ms=%.1f", symbol, day, ppr,
                 interval_sec, len(out["bars"]), (time.perf_counter() - started) * 1000)
        return out
