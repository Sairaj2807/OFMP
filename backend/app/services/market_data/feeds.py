"""Live feeds for the API process. Both have the same shape — built with
(contract, on_tick, on_status), then run() / stop() / health() — so the
server does not care where ticks come from.

EmbeddedFeed   the provider runs inside this process (INGEST_MODE=embedded,
               the default; no Redis needed).
RedisFeed      the ingest worker (backend.app.workers.ingest) owns the broker
               connection; this process asks it to subscribe and consumes
               its tick stream from Redis (INGEST_MODE=redis).

on_tick receives a MarketTick; on_status(connected=bool, error=str|None)."""
import asyncio
import contextlib
import logging
import time
from typing import Callable, Optional

from backend.app.domain.market_data import InstrumentRef, MarketTick

from .provider import MarketDataProvider
from .recorder import RawTickRecorder
from .runner import ProviderRunner

log = logging.getLogger(__name__)


class EmbeddedFeed:
    def __init__(self, contract: dict, on_tick: Callable[[MarketTick], None],
                 on_status: Optional[Callable[..., None]] = None,
                 provider: Optional[MarketDataProvider] = None,
                 recorder: Optional[RawTickRecorder] = None,
                 tick_store=None, on_quality_event=None):
        """recorder: raw tick archive, run and stopped by this feed.
        tick_store: object with record_tick(tick) (e.g. MarketDataWriter);
        its lifecycle is owned by the caller.
        on_quality_event: callable(DataQualityEvent), default logs it."""
        if provider is None:
            from backend.app.infrastructure.providers.angelone import AngelOneProvider
            provider = AngelOneProvider()
        self.contract = contract
        self.instrument = InstrumentRef.from_contract(contract, provider=provider.name)
        self.on_tick = on_tick
        self.recorder = recorder
        self.tick_store = tick_store
        self.runner = ProviderRunner(provider, self._sink, [self.instrument], on_status=on_status,
                                     on_quality_event=on_quality_event)

    def _sink(self, tick: MarketTick) -> None:
        if self.recorder is not None:
            self.recorder.record(tick)     # archive first: a consumer failure must not lose the tick
        if self.tick_store is not None:
            self.tick_store.record_tick(tick)
        self.on_tick(tick)

    def stop(self) -> None:
        self.runner.stop()

    def health(self) -> dict:
        return {"mode": "embedded", **self.runner.health.to_dict()}

    async def run(self) -> None:
        rec_task = asyncio.create_task(self.recorder.run()) if self.recorder else None
        try:
            await self.runner.run()
        finally:
            if rec_task is not None:
                self.recorder.stop()
                rec_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await rec_task


class RedisFeed:
    def __init__(self, contract: dict, on_tick: Callable[[MarketTick], None],
                 on_status: Optional[Callable[..., None]] = None, bus=None,
                 provider_name: str = "angelone", health_poll_sec: float = 1.0,
                 block_ms: int = 500, retry_sec: float = 1.0):
        self.contract = contract
        self.instrument = InstrumentRef.from_contract(contract, provider=provider_name)
        self.on_tick = on_tick
        self.on_status = on_status or (lambda **kw: None)
        self.bus = bus
        self.health_poll_sec = health_poll_sec
        self.block_ms = block_ms
        self.retry_sec = retry_sec
        self._stopped = False
        self._status = None            # last (connected, error) reported
        self._worker_health: Optional[dict] = None
        self.ticks_consumed = 0
        self.ticks_failed = 0

    def stop(self) -> None:
        self._stopped = True

    def health(self) -> dict:
        return {"mode": "redis", "worker": self._worker_health,
                "ticks_consumed": self.ticks_consumed, "ticks_failed": self.ticks_failed,
                "connected": bool(self._status and self._status[0])}

    def _report(self, connected: bool, error: Optional[str]) -> None:
        if self._status != (connected, error):
            self._status = (connected, error)
            self.on_status(connected=connected, error=error)

    async def _refresh_health(self) -> None:
        h = self._worker_health = await self.bus.get_health()
        if h is None:
            self._report(False, "ingest worker not running")
        elif not h.get("connected"):
            self._report(False, h.get("last_error") or f"ingest worker {h.get('state')}")
        elif self.instrument.token not in h.get("subscriptions", []):
            self._report(False, "ingest worker not yet subscribed")
        else:
            self._report(True, None)

    async def run(self) -> None:
        token = self.instrument.token
        last_id = None
        next_health = 0.0
        while not self._stopped:
            try:
                if last_id is None:
                    last_id = await self.bus.latest_tick_id(token)   # only ticks from now on
                    await self.bus.set_desired([self.instrument])
                if time.monotonic() >= next_health:
                    await self._refresh_health()
                    next_health = time.monotonic() + self.health_poll_sec
                last_id, ticks = await self.bus.read_ticks(token, last_id, block_ms=self.block_ms)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._report(False, f"redis: {e}")
                log.warning("redis feed error: %r", e)
                await asyncio.sleep(self.retry_sec)
                continue
            for tick in ticks:
                try:
                    self.on_tick(tick)
                    self.ticks_consumed += 1
                except Exception:
                    self.ticks_failed += 1
                    log.exception("tick consumer failed on %s tick at %s", tick.token, tick.received_ts_ms)
