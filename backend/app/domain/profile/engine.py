"""Market Profile (TPO) and volume-at-price for one trading session.

Fed the same classified trades as the footprint engine (live, the startup
restore, and replay), one at a time; nothing is ever rebuilt from scratch.

Definitions (v1, owner decisions 2026-10-01):
- Periods are `period_minutes` (30) wide from the session open: A = 09:15-09:45,
  B = 09:45-10:15, ... Letters run A-Z, then a-z.
- A TPO marks every price row between the period's high and low (the CBOT
  definition), not only the prices that printed. Angel One sends snapshots
  about once a second, so "printed only" would leave gaps that never existed.
- Prices are integer ticks internally; rows group `row` ticks, labelled by
  their floor (as the footprint does).
- Trades outside the session (before the open, at or after the close) are
  not part of the profile.

Storage per session is tiny: each period keeps its (low, high) tick range, and
volume is kept per tick (buy / sell). TPO rows for any row size are derived
when a snapshot is built (see analytics.build_profile).
"""
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"


@dataclass(frozen=True)
class ProfileSession:
    """When a session runs and how it is cut into periods."""
    open: time = time(9, 15)
    close: time = time(15, 30)
    period_minutes: int = 30
    ib_periods: int = 2                    # initial balance = the first hour
    tz: str = "Asia/Kolkata"

    @property
    def length_minutes(self) -> int:
        start = self.open.hour * 60 + self.open.minute
        end = self.close.hour * 60 + self.close.minute
        return (end - start) if end > start else (end - start + 24 * 60)

    @property
    def periods(self) -> int:
        return -(-self.length_minutes // self.period_minutes)

    def locate(self, ts_ms: int) -> Optional[tuple]:
        """(session date ISO, period index) for a trade time, or None outside the session."""
        tz = ZoneInfo(self.tz)
        local = datetime.fromtimestamp(ts_ms / 1000, tz)
        start = datetime.combine(local.date(), self.open, tz)
        if local < start:                  # sessions crossing midnight belong to the day they opened
            start -= timedelta(days=1)
        minutes = (local - start).total_seconds() / 60
        if minutes < 0 or minutes >= self.length_minutes:
            return None
        return start.date().isoformat(), int(minutes // self.period_minutes)

    def period_start_ms(self, session_date: str, period: int) -> int:
        tz = ZoneInfo(self.tz)
        start = datetime.combine(datetime.fromisoformat(session_date).date(), self.open, tz)
        return int((start + timedelta(minutes=period * self.period_minutes)).timestamp() * 1000)


NSE_SESSION = ProfileSession()
# Development feed: the synthetic provider trades around the clock.
ALL_DAY_SESSION = ProfileSession(open=time(0, 0), close=time(0, 0), period_minutes=30)


def letter(period: int) -> str:
    return LETTERS[period] if period < len(LETTERS) else "?"


@dataclass
class Period:
    low: int
    high: int
    open: int
    close: int


@dataclass
class SessionProfile:
    tick_size: float
    session: ProfileSession = NSE_SESSION
    date: Optional[str] = None
    periods: dict = field(default_factory=dict)       # period index -> Period (tick range)
    volume: dict = field(default_factory=dict)        # tick -> [buy qty, sell qty]
    open_tick: Optional[int] = None
    close_tick: Optional[int] = None
    last_ts_ms: Optional[int] = None
    trades: int = 0
    previous: Optional["SessionProfile"] = None       # the session before this one (reference levels)

    def tick(self, price: float) -> int:
        return int(round(price / self.tick_size))

    def add_trade(self, ts_ms: int, price: float, qty: int, side: str) -> bool:
        """Fold one classified trade in. False if it is outside the session."""
        where = self.session.locate(ts_ms)
        if where is None:
            return False
        day, period = where
        if self.date is None:
            self.date = day
        elif day != self.date:
            if day < self.date:            # a late trade from an earlier session: not ours
                return False
            self._roll(day)
        t = self.tick(price)
        p = self.periods.get(period)
        if p is None:
            self.periods[period] = Period(t, t, t, t)
        else:
            p.low, p.high, p.close = min(p.low, t), max(p.high, t), t
        v = self.volume.setdefault(t, [0, 0])
        v[0 if side == "BUY" else 1] += qty
        if self.open_tick is None:
            self.open_tick = t
        self.close_tick = t
        self.last_ts_ms = ts_ms
        self.trades += 1
        return True

    def _roll(self, day: str) -> None:
        """A new session started: keep the finished one as `previous`."""
        finished = SessionProfile(self.tick_size, self.session, self.date, self.periods, self.volume,
                                  self.open_tick, self.close_tick, self.last_ts_ms, self.trades)
        self.previous = finished
        self.date, self.periods, self.volume = day, {}, {}
        self.open_tick = self.close_tick = self.last_ts_ms = None
        self.trades = 0

    @property
    def current_period(self) -> Optional[int]:
        return max(self.periods) if self.periods else None

    @property
    def high_tick(self) -> Optional[int]:
        return max(p.high for p in self.periods.values()) if self.periods else None

    @property
    def low_tick(self) -> Optional[int]:
        return min(p.low for p in self.periods.values()) if self.periods else None
