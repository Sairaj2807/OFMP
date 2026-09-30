"""Raw tick archive: every normalized tick, quote-only updates included,
appended to data/ticks/<YYYY-MM-DD>/<provider>_<token>.jsonl.

This is the input needed to re-run the engine over a past session (the
trade-only observations.jsonl is not). Writes are buffered and flushed on a
worker thread, so recording never blocks the event loop. The day folder is
the IST calendar date of the server receive time (fixed +05:30, NSE has no
DST), independent of the host's timezone.

Interim storage until the Phase 3 database; the format is one
MarketTick.to_dict() JSON object per line."""
import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone

from backend.app.domain.market_data import MarketTick

log = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))


def tick_day(tick: MarketTick) -> str:
    return datetime.fromtimestamp(tick.received_ts_ms / 1000, IST).strftime("%Y-%m-%d")


class RawTickRecorder:
    def __init__(self, root: str, flush_interval_sec: float = 1.0, max_buffer: int = 5000):
        self.root = root
        self.flush_interval_sec = flush_interval_sec
        self.max_buffer = max_buffer
        self._buffer: list = []
        self._stopped = False
        self.ticks_written = 0

    def path_for(self, tick: MarketTick) -> str:
        return os.path.join(self.root, tick_day(tick), f"{tick.provider}_{tick.token}.jsonl")

    def record(self, tick: MarketTick) -> None:
        self._buffer.append(tick)
        if len(self._buffer) >= self.max_buffer:
            self._write(self._take())   # bounded memory if the flush loop falls behind

    def _take(self) -> list:
        batch, self._buffer = self._buffer, []
        return batch

    def _write(self, batch: list) -> None:
        by_path: dict = {}
        for tick in batch:
            by_path.setdefault(self.path_for(tick), []).append(json.dumps(tick.to_dict(), separators=(",", ":")))
        for path, lines in by_path.items():
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
        self.ticks_written += len(batch)

    async def flush(self) -> None:
        batch = self._take()
        if batch:
            try:
                await asyncio.to_thread(self._write, batch)
            except Exception:
                log.exception("raw tick recorder failed to write %d ticks", len(batch))

    def stop(self) -> None:
        self._stopped = True

    async def run(self) -> None:
        try:
            while not self._stopped:
                await asyncio.sleep(self.flush_interval_sec)
                await self.flush()
        finally:
            await self.flush()
