"""Market-data ingest worker: the only process holding the broker connection
when INGEST_MODE=redis.

    broker --(provider)--> normalize --> Redis stream md:ticks:<token> --> API process(es)
                                    +--> raw tick archive (data/ticks/)

It subscribes to whatever the API last asked for (md:desired, changes via
md:control) and publishes its health to md:health every second. Restarting
or redeploying the API does not touch the broker connection.

Run:  python -m backend.app.workers.ingest
"""
import asyncio
import contextlib
import logging
from typing import Optional

from backend.app.infrastructure.redis_bus import RedisMarketDataBus
from backend.app.services.market_data.provider import MarketDataProvider
from backend.app.services.market_data.recorder import RawTickRecorder
from backend.app.services.market_data.runner import ProviderRunner

log = logging.getLogger(__name__)


async def run_ingest_worker(bus: RedisMarketDataBus, provider: MarketDataProvider,
                            recorder: Optional[RawTickRecorder] = None,
                            health_interval_sec: float = 1.0, control_block_ms: int = 1000,
                            runner_kwargs: Optional[dict] = None, writer=None) -> None:
    """writer: optional MarketDataWriter — raw ticks and data-quality events
    go to the database too; its flush loop runs inside this worker."""
    async def sink(tick):
        if recorder is not None:
            recorder.record(tick)
        if writer is not None:
            writer.record_tick(tick)
        await bus.publish_tick(tick)

    runner_kwargs = dict(runner_kwargs or {})
    if writer is not None:
        runner_kwargs.setdefault("on_quality_event", writer.record_quality_event)

    control_id = await bus.latest_control_id()        # before reading desired: no change can slip between
    runner = ProviderRunner(provider, sink, await bus.get_desired(), **runner_kwargs)

    async def control_loop():
        nonlocal control_id
        while True:
            try:
                control_id, desired = await bus.wait_control(control_id, block_ms=control_block_ms)
                if desired is not None:
                    log.info("subscription change: %s", [i.symbol for i in desired])
                    await runner.set_instruments(desired)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("control loop error: %r", e)
                await asyncio.sleep(1)

    async def health_loop():
        while True:
            try:
                await bus.publish_health(runner.health.to_dict())
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("health publish error: %r", e)
            await asyncio.sleep(health_interval_sec)

    tasks = [asyncio.create_task(control_loop()), asyncio.create_task(health_loop())]
    if recorder is not None:
        tasks.append(asyncio.create_task(recorder.run()))
    if writer is not None:
        tasks.append(asyncio.create_task(writer.run()))
    try:
        await runner.run()
    finally:
        runner.stop()
        if recorder is not None:
            recorder.stop()
        if writer is not None:
            writer.stop()
        for t in tasks:
            t.cancel()
        for t in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await t


def main() -> None:
    import redis.asyncio as aioredis

    import config
    from backend.app.infrastructure.providers.angelone import AngelOneProvider

    from prometheus_client import start_http_server

    from backend.app.core.logging import configure_logging

    configure_logging(config.LOG_FORMAT)
    if config.METRICS_ENABLED:
        start_http_server(config.METRICS_PORT)   # internal network only
    bus = RedisMarketDataBus(aioredis.from_url(config.REDIS_URL))
    if config.SYNTHETIC_FEED:              # development feed: published, never recorded
        from backend.app.infrastructure.providers.synthetic import SyntheticProvider
        log.warning("SYNTHETIC market data (development only): nothing is recorded")
        asyncio.run(run_ingest_worker(bus, SyntheticProvider(), None))
        return
    recorder = RawTickRecorder(config.TICKS_DIR) if config.RECORD_RAW_TICKS else None
    writer = None
    if config.DATABASE_URL:
        from backend.app.infrastructure.postgres.database import create_engine
        from backend.app.infrastructure.postgres.writer import MarketDataWriter
        writer = MarketDataWriter(create_engine(config.DATABASE_URL))
    log.info("ingest worker starting (redis %s, raw ticks %s, database %s)", config.REDIS_URL,
             config.TICKS_DIR if recorder else "off", "on" if writer else "off")
    asyncio.run(run_ingest_worker(bus, AngelOneProvider(), recorder, writer=writer))


if __name__ == "__main__":
    main()
