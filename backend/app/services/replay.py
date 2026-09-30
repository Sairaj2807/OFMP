"""Server-side replay of a stored session.

A ReplaySession walks a day's classified trades (oldest first) through
replay_engine.apply_record — the same footprint/candle/CVD code the live
engine uses — on a virtual clock the viewer controls: play/pause, speed,
seek, and step by trade or candle. Only snapshots leave the server, so a
browser never loads a whole day.

Stored sides are used as recorded (never re-classified), so a past session
always replays exactly as it happened, with the classifier version that
produced it.
"""
import time
from collections import OrderedDict
from typing import Awaitable, Callable, Optional

from backend.app.domain.orderflow.settings import NATIVE_CANDLE_SEC
from replay_engine import ReplayState, apply_record

SPEEDS = (1, 2, 5, 10, 25, 50)


class ReplaySession:
    def __init__(self, date: str, records: list, tick_size: float, clock: Callable[[], float] = time.monotonic):
        if not records:
            raise ValueError("no trades recorded for this session")
        self.date = date
        self.records = records
        self.tick_size = tick_size
        self._clock = clock
        self.start_ms = records[0]["ts_ms"]
        self.end_ms = records[-1]["ts_ms"]
        self.speed = 10
        self.playing = False
        self._last_clock: Optional[float] = None
        self._reset()

    def _reset(self) -> None:
        self.state = ReplayState(self.tick_size)
        self.index = 0                       # next record to apply
        self.cursor_ms = self.start_ms - 1   # virtual clock: nothing applied yet

    def _apply_until(self, ts_ms: int) -> None:
        while self.index < len(self.records) and self.records[self.index]["ts_ms"] <= ts_ms:
            apply_record(self.state, self.records[self.index])
            self.index += 1

    # -- controls ------------------------------------------------------------------

    def play(self) -> None:
        if self.index >= len(self.records):
            self.seek(self.start_ms - 1)            # at the end: start over
        self.playing = True
        self._last_clock = self._clock()

    def pause(self) -> None:
        self.advance()
        self.playing = False

    def set_speed(self, speed: int) -> None:
        if speed not in SPEEDS:
            raise ValueError(f"speed must be one of {SPEEDS}")
        self.advance()
        self.speed = speed

    def seek(self, ts_ms: int) -> None:
        ts_ms = max(self.start_ms - 1, min(int(ts_ms), self.end_ms))
        if ts_ms < self.cursor_ms:                  # backwards: rebuild from the start
            self._reset()
        self._apply_until(ts_ms)
        self.cursor_ms = ts_ms
        self._last_clock = self._clock()

    def step(self, unit: str) -> None:
        self.playing = False
        if self.index >= len(self.records):
            return
        if unit == "trade":
            self.seek(self.records[self.index]["ts_ms"])
        elif unit == "candle":
            # to the end of the candle holding the next trade
            first = self.records[self.index]["ts_ms"] // 1000
            candle_end_ms = (first - first % NATIVE_CANDLE_SEC + NATIVE_CANDLE_SEC) * 1000 - 1
            self.seek(candle_end_ms)
        else:
            raise ValueError("unit must be 'trade' or 'candle'")

    def advance(self) -> None:
        """Move the virtual clock by elapsed real time × speed while playing."""
        now = self._clock()
        if self.playing and self._last_clock is not None:
            target = self.cursor_ms + int((now - self._last_clock) * 1000 * self.speed)
            self.seek(min(target, self.end_ms))
            if self.index >= len(self.records):
                self.playing = False
        self._last_clock = now

    def meta(self) -> dict:
        return {"date": self.date, "start_ms": self.start_ms, "end_ms": self.end_ms,
                "cursor_ms": max(self.cursor_ms, self.start_ms), "index": self.index, "total": len(self.records),
                "playing": self.playing, "speed": self.speed, "speeds": list(SPEEDS)}


class ReplayLibrary:
    """Loads session records on demand with a small LRU cache, so several
    viewers of the same day share one read-only copy."""

    def __init__(self, loader: Callable[[str], Awaitable[list]], lister: Callable[[], Awaitable[list]],
                 max_cached: int = 3):
        self._loader, self._lister, self._max = loader, lister, max_cached
        self._cache: OrderedDict = OrderedDict()

    async def sessions(self) -> list:
        return await self._lister()

    async def records(self, date: str) -> list:
        if date in self._cache:
            self._cache.move_to_end(date)
            return self._cache[date]
        records = await self._loader(date)
        self._cache[date] = records
        while len(self._cache) > self._max:
            self._cache.popitem(last=False)
        return records
