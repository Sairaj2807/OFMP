"""Batched, non-blocking writer for the high-frequency tables.

record_tick / record_trade / record_quality_event only append to an
in-memory buffer (safe to call from the tick path); run() flushes every
`flush_interval_sec` with one multi-row INSERT ... ON CONFLICT DO NOTHING per
table, so re-sending a row is harmless.

If the database is unavailable, rows stay buffered and are retried on the
next flush. The buffer is bounded: past `max_pending` rows per table the
oldest are dropped and counted in `dropped` (the JSONL files remain the
safety net during the dual-write period)."""
import asyncio
import logging
from collections import deque
from typing import Optional

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine

from backend.app.core import metrics
from backend.app.domain.market_data import DataQualityEvent, MarketTick

from .rows import quality_event_row, tick_row, trade_row
from .schema import data_quality_events, raw_ticks, trades

log = logging.getLogger(__name__)

TABLES = {"raw_ticks": raw_ticks, "trades": trades, "data_quality_events": data_quality_events}


class MarketDataWriter:
    def __init__(self, engine: AsyncEngine, flush_interval_sec: float = 1.0,
                 batch_size: int = 2000, max_pending: int = 500_000):
        self.engine = engine
        self.flush_interval_sec = flush_interval_sec
        self.batch_size = batch_size
        self.max_pending = max_pending
        self._pending = {name: deque() for name in TABLES}
        self.written = {name: 0 for name in TABLES}
        self.dropped = {name: 0 for name in TABLES}
        self.last_error: Optional[str] = None
        self._stopped = False
        self._flush_lock = asyncio.Lock()

    # -- producers (sync, cheap) ---------------------------------------------

    def _append(self, table: str, row: dict) -> None:
        q = self._pending[table]
        if len(q) >= self.max_pending:
            q.popleft()
            self.dropped[table] += 1
            metrics.DB_ROWS_DROPPED.labels(table).inc()
        q.append(row)

    def record_tick(self, tick: MarketTick) -> None:
        self._append("raw_ticks", tick_row(tick))

    def record_trade(self, observation: dict, provider: str, token: Optional[str] = None) -> None:
        self._append("trades", trade_row(observation, provider, token))

    def record_quality_event(self, event: DataQualityEvent) -> None:
        self._append("data_quality_events", quality_event_row(event))

    def record(self, tick: MarketTick) -> None:
        """Recorder interface (same as RawTickRecorder), for use as a feed archiver."""
        self.record_tick(tick)

    def pending(self) -> dict:
        return {name: len(q) for name, q in self._pending.items()}

    def stats(self) -> dict:
        return {"written": dict(self.written), "pending": self.pending(),
                "dropped": dict(self.dropped), "last_error": self.last_error}

    # -- flushing --------------------------------------------------------------

    async def flush(self) -> None:
        async with self._flush_lock:
            for name, table in TABLES.items():
                q = self._pending[name]
                while q:
                    batch = [q[i] for i in range(min(self.batch_size, len(q)))]
                    try:
                        async with self.engine.begin() as conn:
                            await conn.execute(insert(table).on_conflict_do_nothing(), batch)
                    except Exception as e:
                        metrics.DB_WRITE_ERRORS.inc()
                        self._export_pending()
                        if self.last_error is None:
                            log.warning("database write failed, keeping %d %s rows buffered: %r", len(q), name, e)
                        self.last_error = f"{type(e).__name__}: {e}"
                        return                      # retry everything on the next flush
                    for _ in batch:
                        q.popleft()
                    self.written[name] += len(batch)
                    metrics.DB_ROWS_WRITTEN.labels(name).inc(len(batch))
            self._export_pending()
            if self.last_error is not None:
                log.info("database writes recovered")
                self.last_error = None

    def _export_pending(self) -> None:
        for name, q in self._pending.items():
            metrics.DB_ROWS_PENDING.labels(name).set(len(q))

    def stop(self) -> None:
        self._stopped = True

    async def run(self) -> None:
        try:
            while not self._stopped:
                await asyncio.sleep(self.flush_interval_sec)
                await self.flush()
        finally:
            await self.flush()
