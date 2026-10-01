"""Runs alert rules against the live engine.

on_trade() is called from the tick path (server._on_tick) for every
classified trade of the active contract. It is synchronous and cheap: it
evaluates the in-memory rules and puts firings on a bounded queue; nothing in
it waits on the database or the network. run() drains the queue:

  1. per-user rate limit (MAX_PER_USER_PER_MIN deliveries a minute; over the
     limit the event is still recorded, marked suppressed="rate_limited");
  2. record the event (idempotent on (rule, dedup_key): a duplicate stops here);
  3. deliver in-app (open terminals), then to the rule's webhooks concurrently.

Rules are loaded from the database at start, updated in place by the API on
every change (upsert/remove), and fully reloaded every reload_sec so that a
change made through another API process is picked up too.

Only live data is evaluated: replay never calls on_trade, and neither does
the startup restore of today's trades (restore_trades), so a restart does
not re-fire the session's past alerts.
"""
import asyncio
import contextlib
import logging
import time
from typing import Callable, Optional

from backend.app.core import metrics
from backend.app.core.ratelimit import SlidingWindowLimiter
from backend.app.domain.alerts import AlertEvaluator, AlertRule, CandleCloseDetector, TradeObservation

from .service import AlertService, event_public

log = logging.getLogger(__name__)

MAX_PER_USER_PER_MIN = 20
QUEUE_SIZE = 10_000
MAX_CONCURRENT_WEBHOOKS = 10


class AlertRuntime:
    def __init__(self, service: AlertService, in_app, providers: dict, reload_sec: float = 30.0,
                 prune_every_sec: float = 3600.0, clock: Callable[[], float] = time.monotonic):
        self.service = service
        self.in_app = in_app
        self.providers = providers               # channel kind -> NotificationProvider
        self.reload_sec, self.prune_every_sec = reload_sec, prune_every_sec
        self.evaluator = AlertEvaluator()
        self.detector = CandleCloseDetector()
        self.limiter = SlidingWindowLimiter(MAX_PER_USER_PER_MIN, 60, clock=clock)
        self.queue: asyncio.Queue = asyncio.Queue(QUEUE_SIZE)
        self.symbol: Optional[str] = None
        self._webhook_slots = asyncio.Semaphore(MAX_CONCURRENT_WEBHOOKS)
        self._tasks: set = set()

    # -- rule set ------------------------------------------------------------------

    async def reload(self) -> None:
        self.evaluator.set_rules(await self.service.enabled_rules())
        metrics.ALERT_RULES_ACTIVE.set(len(self.evaluator.rules()))

    def upsert(self, rule: Optional[AlertRule], rule_id: str) -> None:
        """After an API change: `rule` is the new definition, or None when it was disabled/deleted."""
        if rule is None:
            self.evaluator.remove(rule_id)
        else:
            self.evaluator.upsert(rule)
        metrics.ALERT_RULES_ACTIVE.set(len(self.evaluator.rules()))

    def reset_market(self, symbol: Optional[str]) -> None:
        """A new active contract: forget market-side state so prices of the old
        contract never count as a crossing on the new one."""
        self.symbol = symbol
        self.evaluator.reset_market()
        self.detector.reset()

    # -- hot path ------------------------------------------------------------------

    def on_trade(self, footprint, ts_ms: int, price: float, cvd: int, native_ts: int) -> None:
        intervals = self.evaluator.candle_intervals()
        closed = self.detector.on_trade(footprint, native_ts, intervals)
        if not self.evaluator.has_rules():
            return
        firings = []
        for c in closed:
            firings.extend(self.evaluator.on_candle(c))
        firings.extend(self.evaluator.on_trade(TradeObservation(ts_ms=ts_ms, price=price, cvd=cvd)))
        for f in firings:
            metrics.ALERTS_FIRED.labels(f.kind).inc()
            try:
                self.queue.put_nowait((f, self.symbol))
            except asyncio.QueueFull:
                metrics.ALERTS_DROPPED.inc()

    # -- dispatch ------------------------------------------------------------------

    async def dispatch(self, firing, symbol: Optional[str]) -> Optional[dict]:
        allowed, _ = self.limiter.hit(firing.owner_user_id)
        row = await self.service.record_firing(firing, symbol, suppressed=None if allowed else "rate_limited")
        if row is None:
            return None                          # already recorded (another process, or a duplicate)
        event = {**event_public(row), "rule_name": firing.rule_name}
        if not allowed:
            metrics.ALERTS_SUPPRESSED.labels("rate_limited").inc()
            return event
        ok, err = await self.in_app.deliver(firing.owner_user_id, event)
        await self.service.set_delivery(row.id, "in_app", "sent" if ok and err is None else (err or "failed"))
        metrics.ALERT_DELIVERIES.labels("in_app", "ok" if ok else "failed").inc()
        for channel in await self.service.rule_channels(firing.rule_id):
            provider = self.providers.get(channel[1])
            if provider is not None:
                self._spawn(self._deliver_channel(provider, firing.owner_user_id, event, channel))
        return event

    async def _deliver_channel(self, provider, owner_user_id: str, event: dict, channel: tuple) -> None:
        async with self._webhook_slots:
            ok, err = await provider.deliver(owner_user_id, event, channel)
        metrics.ALERT_DELIVERIES.labels(provider.kind, "ok" if ok else "failed").inc()
        await self.service.set_delivery(event["id"], channel[0], "ok" if ok else f"failed: {err}")
        await self.service.channel_result(channel[0], ok, err)

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def drain(self) -> None:
        """Dispatch everything queued and wait for deliveries (tests, shutdown)."""
        while not self.queue.empty():
            await self.dispatch(*self.queue.get_nowait())
        if self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def run(self) -> None:
        await self._safe(self.reload())
        next_reload = time.monotonic() + self.reload_sec
        next_prune = time.monotonic() + 60
        while True:
            timeout = max(0.1, min(next_reload, next_prune) - time.monotonic())
            item = None
            with contextlib.suppress(asyncio.TimeoutError):
                item = await asyncio.wait_for(self.queue.get(), timeout)
            if item is not None:
                await self._safe(self.dispatch(*item))
            now = time.monotonic()
            if now >= next_reload:
                next_reload = now + self.reload_sec
                await self._safe(self.reload())
            if now >= next_prune:
                next_prune = now + self.prune_every_sec
                await self._safe(self.service.prune_events())

    @staticmethod
    async def _safe(coro) -> None:
        try:
            await coro
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("alert runtime error")
