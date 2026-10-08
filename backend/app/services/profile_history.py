"""Historical market profiles: finished sessions of one contract, each built
independently by the existing engine (SessionProfile + build_profile) and
cached, for the multi-day profile chart.

Policy (v1):
- Contract: only sessions whose trades are tagged with exactly the requested
  symbol are included. Sessions recorded before contract tagging (NULL
  symbol) are never guessed: they are listed under `excluded` with the
  reason. Other contracts are never mixed in.
- Only finished sessions (dates before the live session) are served here;
  today stays on the live engine.
- No session is fabricated: a date without stored trades is simply absent. A
  date whose trades all fall outside the session hours is excluded with a
  reason. A date that fails to build is reported as an error entry; the
  other sessions are still served.

Cache: built profiles are immutable (a finished session never changes), so
they are kept until restart in two LRU levels:
- the folded SessionProfile per (version, symbol, tick size, date): a row
  size change needs no database query;
- the built profile dict per (version, symbol, tick size, date, row size).
PROFILE_VERSION is part of every key, so a change to the profile maths never
reuses an incompatible cached result. clear() drops everything (e.g. after
re-importing trades).
"""
import logging
import time
from collections import OrderedDict
from datetime import date
from typing import Awaitable, Callable, Optional

from backend.app.domain.profile import NSE_SESSION, PROFILE_VERSION, ProfileSession, SessionProfile, build_profile

log = logging.getLogger("ofmp.profile_history")


class _Lru:
    def __init__(self, size: int):
        self.size, self.data = size, OrderedDict()

    def get(self, key):
        if key in self.data:
            self.data.move_to_end(key)
            return self.data[key]
        return None

    def put(self, key, value) -> None:
        self.data[key] = value
        self.data.move_to_end(key)
        while len(self.data) > self.size:
            self.data.popitem(last=False)


class ProfileHistory:
    def __init__(self, list_dates: Callable[..., Awaitable[list]], load_days: Callable[..., Awaitable[dict]],
                 list_untagged: Callable[..., Awaitable[list]], session: ProfileSession = NSE_SESSION,
                 max_sessions: int = 256, max_profiles: int = 2048):
        """list_dates(symbol, before, limit, date_from) -> [date] oldest first;
        load_days(symbol, [date]) -> {date: [(ts_ms, price, qty, side)]};
        list_untagged(before, date_from) -> [date]."""
        self._list_dates, self._load_days, self._list_untagged = list_dates, load_days, list_untagged
        self.session = session
        self._sessions = _Lru(max_sessions)
        self._profiles = _Lru(max_profiles)
        self.stats = {"requests": 0, "profile_hits": 0, "profile_builds": 0, "sessions_loaded": 0}

    def clear(self, symbol: Optional[str] = None) -> None:
        for lru in (self._sessions, self._profiles):
            for key in [k for k in lru.data if symbol is None or k[1] == symbol]:
                del lru.data[key]

    def _fold(self, tick_size: float, trades: list) -> SessionProfile:
        profile = SessionProfile(tick_size, self.session)
        for ts, price, qty, side in trades:
            profile.add_trade(ts, price, qty, side)
        return profile

    async def history(self, symbol: str, tick_size: float, row: float, *, before: date, limit: int,
                      date_from: Optional[date] = None) -> dict:
        """Finished sessions of `symbol` before `before`: the last `limit` (from `date_from`), oldest first."""
        started = time.perf_counter()
        self.stats["requests"] += 1
        dates = await self._list_dates(symbol, before, limit, date_from)
        skey = lambda d: (PROFILE_VERSION, symbol, tick_size, d)            # noqa: E731
        missing = [d for d in dates if self._sessions.get(skey(d)) is None]
        trades_loaded = 0
        if missing:
            loaded = await self._load_days(symbol, missing)
            for d in missing:
                trades = loaded.get(d, [])
                trades_loaded += len(trades)
                try:
                    self._sessions.put(skey(d), self._fold(tick_size, trades))
                except Exception as e:                                    # one bad day never sinks the rest
                    log.exception("profile history: %s %s failed to build", symbol, d)
                    self._sessions.put(skey(d), e)
            self.stats["sessions_loaded"] += len(missing)

        sessions, excluded, hits, builds = [], [], 0, 0
        for d in dates:
            iso = d.isoformat()
            folded = self._sessions.get(skey(d))
            if isinstance(folded, Exception):
                sessions.append({"date": iso, "symbol": symbol, "state": "error",
                                 "error": f"could not build this session ({type(folded).__name__})"})
                continue
            if folded is None or not folded.periods:
                excluded.append({"date": iso, "reason": "no trades inside the session hours"})
                continue
            pkey = (*skey(d), row)
            profile = self._profiles.get(pkey)
            if profile is None:
                profile = build_profile(folded, row, include_previous=False)
                self._profiles.put(pkey, profile)
                builds += 1
            else:
                hits += 1
            sessions.append({"date": iso, "symbol": symbol, "state": "historical", "profile": profile})
        self.stats["profile_hits"] += hits
        self.stats["profile_builds"] += builds

        # untagged sessions that would fall inside the window (at most `limit`): listed with the reason,
        # never included. When fewer sessions than asked for exist, the window reaches further back.
        window_start = date_from or (dates[0] if len(dates) >= limit else None)
        untagged = [d for d in await self._list_untagged(before, date_from)
                    if window_start is None or d >= window_start][-limit:]
        for d in untagged:
            excluded.append({"date": d.isoformat(), "reason": "recorded before contract tagging: contract unknown"})
        excluded.sort(key=lambda e: e["date"])
        log.info("profile history symbol=%s row=%s dates=%s..%s sessions=%d hits=%d builds=%d loaded_days=%d "
                 "trades=%d excluded=%d ms=%.1f", symbol, row, dates[0] if dates else "-",
                 dates[-1] if dates else "-", len(sessions), hits, builds, len(missing), trades_loaded,
                 len(excluded), (time.perf_counter() - started) * 1000)
        return {"symbol": symbol, "row_size": row, "tick_size": tick_size, "version": PROFILE_VERSION,
                "before": before.isoformat(), "sessions": sessions, "excluded": excluded}
