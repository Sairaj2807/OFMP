"""Supervises a MarketDataProvider: the single place that handles
connection lifecycle, so providers stay simple and every provider gets the
same failure behaviour.

    connect -> subscribe -> stream ticks -> (error / stream end)
       ^                                          |
       +---- backoff (exponential, jittered) <----+  disconnect, count reconnect

A failing consumer never takes ingestion down: an exception raised by the
tick sink is counted (health.ticks_failed) and logged, and streaming goes on.
"""
import asyncio
import logging
import random
import time
from typing import Awaitable, Callable, Iterable, Optional, Union

from backend.app.core import metrics
from backend.app.domain.market_data import (DataQualityEvent, InstrumentRef, MarketTick, ProviderHealth,
                                            ProviderState, TickQualityMonitor)

from .provider import MarketDataProvider

log = logging.getLogger(__name__)

TickSink = Callable[[MarketTick], Union[None, Awaitable[None]]]


def _now_ms() -> int:
    return int(time.time() * 1000)


class Backoff:
    """Exponential backoff with full jitter: delay n is uniform in
    [0, min(max_delay, base * factor**n)]. Resets after a connection has
    stayed up for `stable_after_sec`."""

    def __init__(self, base: float = 1.0, factor: float = 2.0, max_delay: float = 30.0,
                 stable_after_sec: float = 60.0, rng: Optional[random.Random] = None):
        self.base, self.factor, self.max_delay = base, factor, max_delay
        self.stable_after_sec = stable_after_sec
        self.attempt = 0
        self._rng = rng or random.Random()

    def next_delay(self) -> float:
        cap = min(self.max_delay, self.base * (self.factor ** self.attempt))
        self.attempt += 1
        return self._rng.uniform(0, cap)

    def connection_lasted(self, seconds: float) -> None:
        if seconds >= self.stable_after_sec:
            self.attempt = 0


class ProviderRunner:
    def __init__(self, provider: MarketDataProvider, sink: TickSink,
                 instruments: Iterable[InstrumentRef] = (),
                 on_status: Optional[Callable[..., None]] = None,
                 on_quality_event: Optional[Callable[[DataQualityEvent], None]] = None,
                 backoff: Optional[Backoff] = None,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 quality: Optional[TickQualityMonitor] = None):
        self.provider = provider
        self.sink = sink
        self.instruments = {i.token: i for i in instruments}
        self.on_status = on_status or (lambda **kw: None)
        self.on_quality_event = on_quality_event or self._log_quality_event
        self.backoff = backoff or Backoff()
        self._sleep = sleep
        self.quality = quality or TickQualityMonitor()
        self.health = ProviderHealth(provider=provider.name, subscriptions=list(self.instruments))
        self._stopped = False
        self._connected = False
        provider.on_message = self._on_message
        provider.on_drop = self._on_drop

    # -- lifecycle ---------------------------------------------------------

    def stop(self) -> None:
        self._stopped = True

    async def run(self) -> None:
        try:
            while not self._stopped:
                connected_at = None
                try:
                    self._set_state(ProviderState.CONNECTING if self.health.reconnects == 0
                                    else ProviderState.RECONNECTING)
                    await self.provider.connect()
                    if self.instruments:
                        await self.provider.subscribe(list(self.instruments.values()))
                    connected_at = time.monotonic()
                    self._connected = True
                    self.health.last_connect_ms = _now_ms()
                    self.health.last_error = None
                    self._set_state(ProviderState.CONNECTED)
                    self.on_status(connected=True, error=None)
                    log.info("%s connected, subscribed to %s", self.provider.name, list(self.instruments))

                    async for tick in self.provider.stream():
                        await self._handle_tick(tick)
                        if self._stopped:
                            break
                    if not self._stopped:
                        raise ConnectionError("stream ended")
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    self.health.last_error = f"{type(e).__name__}: {e}"
                    self.health.last_disconnect_ms = _now_ms()
                    self.on_status(connected=False, error=str(e))
                    log.warning("%s connection error: %r", self.provider.name, e)
                finally:
                    self._connected = False
                    await self._safe_disconnect()
                if self._stopped:
                    break
                if connected_at is not None:
                    self.backoff.connection_lasted(time.monotonic() - connected_at)
                self.health.reconnects += 1
                metrics.PROVIDER_RECONNECTS.labels(self.provider.name).inc()
                self._set_state(ProviderState.RECONNECTING)
                delay = self.backoff.next_delay()
                log.info("%s reconnecting in %.1fs (attempt %d)", self.provider.name, delay, self.backoff.attempt)
                await self._sleep(delay)
        finally:
            self._set_state(ProviderState.STOPPED)

    async def _safe_disconnect(self) -> None:
        try:
            await self.provider.disconnect()
        except Exception as e:   # never let teardown mask the original failure
            log.debug("%s disconnect error: %r", self.provider.name, e)

    # -- subscriptions ------------------------------------------------------

    async def set_instruments(self, instruments: Iterable[InstrumentRef]) -> None:
        """Replace the subscription set; applied immediately if connected,
        otherwise on the next connect."""
        new = {i.token: i for i in instruments}
        removed = [i for t, i in self.instruments.items() if t not in new]
        added = [i for t, i in new.items() if t not in self.instruments]
        self.instruments = new
        self.health.subscriptions = list(new)
        for token in (i.token for i in removed):
            self.quality.reset(token)
        if self._connected:
            if removed:
                await self.provider.unsubscribe(removed)
            if added:
                await self.provider.subscribe(added)

    # -- ticks --------------------------------------------------------------

    async def _handle_tick(self, tick: MarketTick) -> None:
        self.health.ticks_received += 1
        self.health.last_tick_ms = tick.received_ts_ms
        name = self.provider.name
        metrics.TICKS_RECEIVED.labels(name).inc()
        metrics.LAST_TICK_TIME.labels(name).set(tick.received_ts_ms / 1000)
        if tick.exchange_ts_ms:
            metrics.TICK_PROVIDER_LATENCY.labels(name).observe(max(0.0, (tick.received_ts_ms - tick.exchange_ts_ms) / 1000))
        if self.health.state == ProviderState.DEGRADED:
            self._set_state(ProviderState.CONNECTED)
        for event in self.quality.observe(tick):
            self.health.quality_events += 1
            metrics.QUALITY_EVENTS.labels(event.kind, event.severity).inc()
            self.on_quality_event(event)
        try:
            result = self.sink(tick)
            if asyncio.iscoroutine(result):
                await result
        except Exception:
            self.health.ticks_failed += 1
            metrics.TICKS_FAILED.labels(self.provider.name).inc()
            log.exception("tick consumer failed on %s tick at %s", tick.token, tick.received_ts_ms)

    def _on_message(self, received_ts_ms: int) -> None:
        self.health.last_message_ms = received_ts_ms

    def _on_drop(self, reason: str) -> None:
        self.health.ticks_dropped += 1
        metrics.TICKS_DROPPED.labels(self.provider.name).inc()
        log.debug("%s dropped a packet: %s", self.provider.name, reason)

    def check_liveness(self, now_ms: Optional[int] = None, stale_after_ms: int = 60_000) -> None:
        """Mark the feed DEGRADED when connected but silent for too long.
        Callers decide when this matters (e.g. only during market hours)."""
        now_ms = now_ms or _now_ms()
        last = self.health.last_tick_ms or self.health.last_connect_ms
        if self.health.state == ProviderState.CONNECTED and last and now_ms - last > stale_after_ms:
            self._set_state(ProviderState.DEGRADED)

    def _set_state(self, state: ProviderState) -> None:
        self.health.state = state
        metrics.PROVIDER_CONNECTED.labels(self.provider.name).set(
            1 if state in (ProviderState.CONNECTED, ProviderState.DEGRADED) else 0)

    @staticmethod
    def _log_quality_event(event: DataQualityEvent) -> None:
        log.log(logging.WARNING if event.severity != "info" else logging.DEBUG,
                "data quality: %s %s %s", event.kind, event.token, event.detail)
