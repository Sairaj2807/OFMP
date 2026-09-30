"""Market-data layer: normalization, the Angel One provider (against a fake
WebSocket), the provider runner's failure handling, data-quality checks,
the raw tick recorder, the Redis bus, and the worker -> API path."""
import asyncio
import json
import random
from datetime import datetime, timezone

import fakeredis
import pytest

from angel_packets import pack_snap_quote
from backend.app.domain.market_data import (DepthLevel, InstrumentRef, MarketTick, ProviderState,
                                            TickQualityMonitor)
from backend.app.infrastructure.providers.angelone import AngelOneProvider
from backend.app.infrastructure.providers.angelone.parser import parse_snap_quote
from backend.app.infrastructure.redis_bus import RedisMarketDataBus
from backend.app.services.market_data.feeds import EmbeddedFeed, RedisFeed
from backend.app.services.market_data.provider import MarketDataProvider
from backend.app.services.market_data.recorder import RawTickRecorder
from backend.app.services.market_data.runner import Backoff, ProviderRunner
from backend.app.workers.ingest import run_ingest_worker

INST = InstrumentRef("angelone", "12345", "NFO", "NIFTY27OCT26FUT")


def run(coro):
    return asyncio.run(coro)


def mtick(token="12345", seq=1, ltt=1_790_653_500_000, received=1_790_653_500_100, ltp=24000.0, vol=1000,
          bids=((23999.9, 65, 1),), asks=((24000.1, 65, 1),)):
    return MarketTick(provider="angelone", token=token, exchange_segment="NFO", sequence=seq,
                      exchange_ts_ms=ltt, last_trade_ts_ms=ltt, received_ts_ms=received, ltp=ltp,
                      last_traded_qty=65, cumulative_volume=vol,
                      bids=tuple(DepthLevel(*b) for b in bids), asks=tuple(DepthLevel(*a) for a in asks))


# ---- normalization -----------------------------------------------------------------

def test_parser_normalizes_every_field_and_keeps_timestamps():
    buf = pack_snap_quote(token="53001", seq=77, exch_ts_ms=1_790_653_500_250, ltp=24010.5, ltq=130,
                          volume=987_000, ltt_s=1_790_653_500, oi=4242,
                          bids=((24010.4, 650, 3), (24010.3, 65, 1)), asks=((24010.6, 130, 2),))
    t = parse_snap_quote(buf, received_ts_ms=1_790_653_500_400, exchange_segment="NFO")
    assert (t.provider, t.token, t.exchange_segment, t.sequence) == ("angelone", "53001", "NFO", 77)
    assert (t.exchange_ts_ms, t.last_trade_ts_ms, t.received_ts_ms) == \
        (1_790_653_500_250, 1_790_653_500_000, 1_790_653_500_400)
    assert (t.ltp, t.last_traded_qty, t.cumulative_volume, t.open_interest) == (24010.5, 130, 987_000, 4242)
    assert t.bids[:2] == (DepthLevel(24010.4, 650, 3), DepthLevel(24010.3, 65, 1))
    assert t.asks[0] == DepthLevel(24010.6, 130, 2)
    assert t.to_engine_tick()["ltt"] == 1_790_653_500_000
    assert t.to_engine_tick()["depth_buy"][0] == (24010.4, 650, 3)


def test_market_tick_json_round_trip():
    t = parse_snap_quote(pack_snap_quote(), received_ts_ms=5)
    assert MarketTick.from_dict(json.loads(json.dumps(t.to_dict()))) == t


# ---- data quality ------------------------------------------------------------------

def test_quality_monitor_flags_each_anomaly_without_touching_ticks():
    q = TickQualityMonitor(gap_threshold_ms=10_000)
    assert q.observe(mtick(seq=5)) == []
    kinds = lambda t: sorted(e.kind for e in q.observe(t))
    assert kinds(mtick(seq=5, received=1_790_653_500_200)) == ["sequence_not_increasing"]
    assert kinds(mtick(seq=6, ltt=1_790_653_499_000, received=1_790_653_500_300)) == ["timestamp_reversal"]
    assert kinds(mtick(seq=7, vol=10, received=1_790_653_500_400)) == ["volume_reset"]
    assert kinds(mtick(seq=8, vol=10, received=1_790_653_520_000)) == ["feed_gap"]
    assert kinds(mtick(seq=9, vol=10, received=1_790_653_520_100, bids=((24000.2, 65, 1),))) == ["crossed_book"]
    assert kinds(mtick(seq=10, vol=10, received=1_790_653_520_200, asks=())) == ["empty_book_side"]
    assert kinds(mtick(seq=11, vol=10, received=1_790_653_520_300, ltp=0)) == ["non_positive_price"]


# ---- Angel One provider against a fake socket --------------------------------------

class FakeWS:
    def __init__(self, frames):
        self.frames = list(frames)
        self.sent = []
        self.closed = False

    async def send(self, msg):
        self.sent.append(msg)

    async def recv(self):
        if not self.frames:
            raise ConnectionError("socket closed")
        frame = self.frames.pop(0)
        if frame == "SLOW":
            await asyncio.sleep(1)
            return "pong"
        return frame

    async def close(self):
        self.closed = True


def angel_provider(ws, heartbeat_sec=30):
    creds = lambda: {"jwt_token": "j", "feed_token": "f", "api_key": "k", "client_code": "c"}

    async def connect(url, headers):
        connect.headers = headers
        return ws
    return AngelOneProvider(credentials=creds, connect=connect, heartbeat_sec=heartbeat_sec,
                            clock_ms=lambda: 42), connect


def test_angel_provider_subscribes_streams_and_drops_bad_packets():
    ws = FakeWS([pack_snap_quote(token="12345"), b"\x00" * 10, "pong", "{\"error\":1}", pack_snap_quote(token="12345", seq=2)])
    provider, connect = angel_provider(ws)
    drops = []
    provider.on_drop = drops.append

    async def go():
        await provider.connect()
        await provider.subscribe([INST])
        got = []
        with pytest.raises(ConnectionError):
            async for t in provider.stream():
                got.append(t)
        await provider.disconnect()
        return got
    ticks = run(go())
    assert connect.headers == {"Authorization": "Bearer j", "x-api-key": "k", "x-client-code": "c", "x-feed-token": "f"}
    assert json.loads(ws.sent[0]) == {"correlationID": "orderflow01", "action": 1,
                                      "params": {"mode": 3, "tokenList": [{"exchangeType": 2, "tokens": ["12345"]}]}}
    assert [t.sequence for t in ticks] == [1, 2]
    assert all(t.exchange_segment == "NFO" and t.received_ts_ms == 42 for t in ticks)
    assert len(drops) == 1 and "short packet" in drops[0]
    assert ws.closed


def test_angel_provider_sends_heartbeat_ping_when_idle():
    ws = FakeWS(["SLOW"])
    provider, _ = angel_provider(ws, heartbeat_sec=0.1)

    async def go():
        await provider.connect()
        with pytest.raises(ConnectionError):
            async for _ in provider.stream():
                pass
    run(go())
    assert "ping" in ws.sent


def test_angel_provider_unsubscribe_message():
    ws = FakeWS([])
    provider, _ = angel_provider(ws)

    async def go():
        await provider.connect()
        await provider.subscribe([INST])
        await provider.unsubscribe([INST])
    run(go())
    assert json.loads(ws.sent[1])["action"] == 0


# ---- runner ----------------------------------------------------------------------------

class ScriptedProvider(MarketDataProvider):
    """Each connect() consumes one session from the script: an Exception to
    fail the connect, or a list of ticks to stream before the stream ends."""
    name = "fake"

    def __init__(self, sessions):
        self.sessions = list(sessions)
        self.current = []
        self.subscribed = []
        self.unsubscribed = []
        self.disconnects = 0

    async def connect(self):
        s = self.sessions.pop(0)
        if isinstance(s, Exception):
            raise s
        self.current = s

    async def subscribe(self, instruments):
        self.subscribed.append([i.token for i in instruments])

    async def unsubscribe(self, instruments):
        self.unsubscribed.append([i.token for i in instruments])

    async def disconnect(self):
        self.disconnects += 1

    async def stream(self):
        for t in self.current:
            yield t


def test_runner_reconnects_resubscribes_and_survives_consumer_errors():
    provider = ScriptedProvider([OSError("refused"), [mtick(seq=1), mtick(seq=2), mtick(seq=3)], [mtick(seq=4)]])
    received, statuses, sleeps = [], [], []

    def sink(t):
        if t.sequence == 2:
            raise ValueError("bad tick")
        received.append(t.sequence)

    runner = None

    async def sleep(d):
        sleeps.append(d)
        if len(sleeps) == 3:
            runner.stop()

    runner = ProviderRunner(provider, sink, [INST], on_status=lambda **kw: statuses.append(kw["connected"]),
                            backoff=Backoff(rng=random.Random(1)), sleep=sleep)
    run(runner.run())
    assert received == [1, 3, 4]                       # tick 2's consumer error did not stop the stream
    assert provider.subscribed == [["12345"], ["12345"]]   # resubscribed after reconnect
    assert statuses == [False, True, False, True, False]
    h = runner.health
    assert (h.reconnects, h.ticks_received, h.ticks_failed, h.state) == (3, 4, 1, ProviderState.STOPPED)
    assert provider.disconnects == 3


def test_backoff_is_capped_jittered_and_resets_after_a_stable_connection():
    b = Backoff(base=1, factor=2, max_delay=8, stable_after_sec=60, rng=random.Random(0))
    delays = [b.next_delay() for _ in range(6)]
    caps = [1, 2, 4, 8, 8, 8]
    assert all(0 <= d <= c for d, c in zip(delays, caps))
    b.connection_lasted(10)
    assert b.attempt == 6
    b.connection_lasted(61)
    assert b.attempt == 0


def test_set_instruments_applies_live_when_connected():
    provider = ScriptedProvider([])
    runner = ProviderRunner(provider, lambda t: None, [INST])
    runner._connected = True
    other = InstrumentRef("angelone", "999", "NFO", "NIFTY24NOV26FUT")
    run(runner.set_instruments([other]))
    assert provider.unsubscribed == [["12345"]] and provider.subscribed == [["999"]]
    assert runner.health.subscriptions == ["999"]


# ---- recorder ----------------------------------------------------------------------------

def test_recorder_writes_per_ist_day_and_token(tmp_path):
    rec = RawTickRecorder(str(tmp_path))
    before_midnight_ist = int(datetime(2026, 9, 30, 18, 29, tzinfo=timezone.utc).timestamp() * 1000)
    after_midnight_ist = int(datetime(2026, 9, 30, 18, 31, tzinfo=timezone.utc).timestamp() * 1000)
    rec.record(mtick(received=before_midnight_ist))
    rec.record(mtick(received=after_midnight_ist, seq=2))
    rec.record(mtick(token="777", received=after_midnight_ist))
    run(rec.flush())
    a = tmp_path / "2026-09-30" / "angelone_12345.jsonl"
    b = tmp_path / "2026-10-01" / "angelone_12345.jsonl"
    c = tmp_path / "2026-10-01" / "angelone_777.jsonl"
    assert a.exists() and b.exists() and c.exists()
    assert MarketTick.from_dict(json.loads(b.read_text().splitlines()[0])).sequence == 2
    assert rec.ticks_written == 3


# ---- embedded feed --------------------------------------------------------------------------

def test_embedded_feed_records_before_delivering(tmp_path):
    provider = ScriptedProvider([[mtick(seq=1), mtick(seq=2)], OSError("down")])
    rec = RawTickRecorder(str(tmp_path), flush_interval_sec=0.01)
    got = []
    feed = EmbeddedFeed({"token": "12345", "tradingsymbol": "X", "exch_seg": "NFO"},
                        on_tick=lambda t: (_ for _ in ()).throw(ValueError()) if t.sequence == 1 else got.append(t),
                        provider=provider, recorder=rec)
    feed.runner._sleep = lambda d: (feed.stop(), asyncio.sleep(0))[1]
    run(feed.run())
    assert [t.sequence for t in got] == [2]
    assert rec.ticks_written == 2                  # tick 1 archived even though its consumer failed
    assert feed.health()["mode"] == "embedded" and feed.health()["ticks_failed"] == 1


# ---- Redis bus and worker -> API ---------------------------------------------------------------

def test_redis_bus_ticks_control_and_health():
    async def go():
        bus = RedisMarketDataBus(fakeredis.FakeAsyncRedis())
        start = await bus.latest_tick_id("12345")
        await bus.publish_tick(mtick(seq=1))
        await bus.publish_tick(mtick(seq=2))
        last, ticks = await bus.read_ticks("12345", start, block_ms=10)
        assert [t.sequence for t in ticks] == [1, 2]
        assert (await bus.read_ticks("12345", last, block_ms=10))[1] == []

        cid = await bus.latest_control_id()
        await bus.set_desired([INST])
        cid, desired = await bus.wait_control(cid, block_ms=10)
        assert desired == [INST] and await bus.get_desired() == [INST]
        assert (await bus.wait_control(cid, block_ms=10))[1] is None

        assert await bus.get_health() is None
        await bus.publish_health({"connected": True})
        assert await bus.get_health() == {"connected": True}
        assert await bus.redis.ttl("md:health") > 0
    run(go())


class QueueProvider(MarketDataProvider):
    """Streams ticks for whatever is subscribed, from a queue the test fills."""
    name = "angelone"

    def __init__(self):
        self.queue = asyncio.Queue()
        self.subscribed = set()

    async def connect(self):
        pass

    async def subscribe(self, instruments):
        self.subscribed |= {i.token for i in instruments}

    async def unsubscribe(self, instruments):
        self.subscribed -= {i.token for i in instruments}

    async def disconnect(self):
        pass

    async def stream(self):
        while True:
            t = await self.queue.get()
            if t.token in self.subscribed:
                yield t


def test_worker_to_redis_feed_end_to_end(tmp_path):
    async def go():
        server = fakeredis.FakeServer()
        worker_bus = RedisMarketDataBus(fakeredis.FakeAsyncRedis(server=server))
        api_bus = RedisMarketDataBus(fakeredis.FakeAsyncRedis(server=server))
        provider = QueueProvider()
        rec = RawTickRecorder(str(tmp_path), flush_interval_sec=0.01)
        worker = asyncio.create_task(run_ingest_worker(worker_bus, provider, rec, health_interval_sec=0.02,
                                                       control_block_ms=20))
        got, statuses = [], []
        feed = RedisFeed({"token": "12345", "tradingsymbol": "NIFTY27OCT26FUT", "exch_seg": "NFO"},
                         on_tick=got.append, on_status=lambda **kw: statuses.append(kw), bus=api_bus,
                         health_poll_sec=0.02, block_ms=20)
        consumer = asyncio.create_task(feed.run())

        for _ in range(200):                               # wait until the worker has subscribed
            if "12345" in provider.subscribed:
                break
            await asyncio.sleep(0.01)
        assert "12345" in provider.subscribed
        for seq in (1, 2, 3):
            await provider.queue.put(mtick(seq=seq))
        await provider.queue.put(mtick(token="555", seq=9))   # not subscribed: never delivered
        for _ in range(200):
            if len(got) == 3 and any(s["connected"] for s in statuses):
                break
            await asyncio.sleep(0.01)

        feed.stop()
        for t in (consumer, worker):
            t.cancel()
        await asyncio.gather(consumer, worker, return_exceptions=True)
        return got, statuses, feed.health()

    got, statuses, health = run(go())
    assert [t.sequence for t in got] == [1, 2, 3]
    assert statuses[0]["connected"] is False and statuses[-1] == {"connected": True, "error": None}
    assert health["worker"]["subscriptions"] == ["12345"] and health["ticks_consumed"] == 3
    archived = list(tmp_path.rglob("angelone_12345.jsonl"))
    assert len(archived) == 1 and len(archived[0].read_text().splitlines()) == 3   # worker archived the ticks


def test_redis_feed_reports_missing_worker():
    async def go():
        statuses = []
        feed = RedisFeed({"token": "1", "tradingsymbol": "X"}, on_tick=lambda t: None,
                         on_status=lambda **kw: statuses.append(kw),
                         bus=RedisMarketDataBus(fakeredis.FakeAsyncRedis()), health_poll_sec=0.01, block_ms=10)
        task = asyncio.create_task(feed.run())
        await asyncio.sleep(0.05)
        feed.stop()
        await asyncio.wait_for(task, 1)
        return statuses
    assert run(go())[0] == {"connected": False, "error": "ingest worker not running"}


# ---- synthetic provider (development feed) ------------------------------------------------------

def test_synthetic_provider_ticks_are_valid_and_drive_the_engine():
    from backend.app.domain.orderflow import TickProcessorState, process_tick
    from backend.app.infrastructure.providers.synthetic import SyntheticProvider
    p = SyntheticProvider(seed=3)
    state, trades = TickProcessorState(0.1), []
    quality = TickQualityMonitor()
    for i in range(500):
        t = p.next_tick("SYNTH1", 1_790_653_500_000 + i * 250)
        assert t.bids[0].price < t.asks[0].price and len(t.bids) == len(t.asks) == 5
        assert [e.kind for e in quality.observe(t)] in ([], ["timestamp_reversal"])   # never crossed or reset
        r = process_tick(state, t.to_engine_tick())
        if r.get("new_trade"):
            trades.append(r)
    assert len(trades) > 200 and state.footprint.data


def test_live_feed_uses_synthetic_provider_and_records_nothing(monkeypatch):
    import config
    import server
    from backend.app.infrastructure.providers.synthetic import SYNTHETIC_CONTRACT, SyntheticProvider
    monkeypatch.setattr(config, "SYNTHETIC_FEED", True)
    monkeypatch.setitem(server.STATE, "db_writer", object())       # must not be wired in synthetic mode
    feed = server.LiveFeed(dict(SYNTHETIC_CONTRACT), on_tick=lambda t: None, on_status=lambda **kw: None)
    assert isinstance(feed.runner.provider, SyntheticProvider)
    assert feed.recorder is None and feed.tick_store is None
    assert run(server._resolve_default_contract())["tradingsymbol"] == "NIFTYSYNTHFUT"
    assert run(server._restore_session(None, dict(SYNTHETIC_CONTRACT))) == 0


def test_terminal_is_served_at_app_when_built():
    import os

    import config
    if not os.path.isdir(config.FRONTEND_DIST):
        pytest.skip("frontend not built (cd frontend && npm run build)")
    from fastapi.testclient import TestClient

    import server
    c = TestClient(server.app)
    for path in ("/app/", "/app/login/"):
        r = c.get(path)
        assert r.status_code == 200 and "<html" in r.text.lower()
