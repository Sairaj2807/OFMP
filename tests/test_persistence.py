"""PostgreSQL/TimescaleDB integration: migrations, the batched writer
(idempotency, outage buffering), session restore, instrument upsert and the
import/reconcile CLI.

Needs a disposable database; skipped unless TEST_DATABASE_URL is set, e.g.
    TEST_DATABASE_URL=postgresql+asyncpg://ofmp:ofmp_dev_password@127.0.0.1:55432/ofmp_test
The schema is dropped and recreated at the start of the run."""
import asyncio
import json
import os

import pytest

import config

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL not set")

if TEST_DATABASE_URL:
    import sqlalchemy as sa

    import server
    from backend.app.domain.orderflow import TickProcessorState, Trade, restore_trades
    from backend.app.infrastructure.postgres.database import create_engine
    from backend.app.infrastructure.postgres.repositories import (load_session_tape, load_session_trades,
                                                                  trade_counts_by_session, upsert_instrument)
    from backend.app.infrastructure.postgres.rows import ist_date
    from backend.app.infrastructure.postgres.writer import MarketDataWriter
    from helpers import TICK, synthetic_ticks
    import orderbook_engine as oe
    from test_market_data import mtick


def run(coro):
    return asyncio.run(coro)


async def _sql(query):
    engine = create_engine(TEST_DATABASE_URL)
    try:
        async with engine.begin() as conn:
            result = await conn.execute(sa.text(query))
            return result.all() if result.returns_rows else None
    finally:
        await engine.dispose()


@pytest.fixture(scope="module", autouse=True)
def schema():
    from alembic import command
    from alembic.config import Config
    saved = config.DATABASE_URL
    config.DATABASE_URL = TEST_DATABASE_URL
    try:
        cfg = Config("alembic.ini")
        command.downgrade(cfg, "base")
        command.upgrade(cfg, "head")
        yield
    finally:
        config.DATABASE_URL = saved


@pytest.fixture(autouse=True)
def clean_tables():
    run(_sql("TRUNCATE raw_ticks, trades, data_quality_events, instruments"))


async def with_engine(fn):
    engine = create_engine(TEST_DATABASE_URL)
    try:
        return await fn(engine)
    finally:
        await engine.dispose()


# ---- schema -------------------------------------------------------------------------------

def test_migration_creates_hypertables_and_seeds_versions():
    rows = run(_sql("SELECT hypertable_name FROM timescaledb_information.hypertables ORDER BY 1"))
    assert [r[0] for r in rows] == ["raw_ticks", "trades"]
    versions = run(_sql("SELECT name, version FROM algorithm_versions WHERE kind = 'classifier' ORDER BY name"))
    assert ("vtrender_reconstruction", "v1") in [tuple(r) for r in versions]


def test_constraints_reject_invalid_trades():
    with pytest.raises(Exception, match="ck_trades_side_valid"):
        run(_sql("INSERT INTO trades (trade_ts, provider, session_date, trade_seq, price, qty, side, "
                 "classifier_name, classifier_version) VALUES (now(), 'x', current_date, 1, 1, 1, 'HOLD', 'c', 'v1')"))


# ---- writer ----------------------------------------------------------------------------------

def test_writer_is_idempotent_and_round_trips_ticks():
    async def go(engine):
        w = MarketDataWriter(engine)
        for _ in range(2):                                   # same rows twice
            w.record_tick(mtick(seq=1))
            w.record_tick(mtick(seq=2, received=1_790_653_500_200))
            w.record_trade({"trade_id": 1, "ts_ms": 1_790_653_500_000, "ltp": 24000.0, "qty": 65,
                            "algo_side": "BUY", "algo_reason": "Midpoint Rule (VTRenders)", "symbol": "S"},
                           provider="angelone", token="12345")
            await w.flush()
        async with engine.connect() as conn:
            ticks = (await conn.execute(sa.text(
                "SELECT sequence, ltp, bid_px, bid_qty, last_trade_ts FROM raw_ticks ORDER BY sequence"))).all()
            trades = (await conn.execute(sa.text(
                "SELECT trade_seq, side, classifier_name, classifier_version, session_date FROM trades"))).all()
        return w, ticks, trades
    w, ticks, trades = run(with_engine(go))
    assert [t[0] for t in ticks] == [1, 2]
    assert ticks[0][2] == [23999.9] and ticks[0][3] == [65]
    assert trades == [(1, "BUY", "vtrender_reconstruction", "v1", ist_date(1_790_653_500_000))]
    assert w.written["raw_ticks"] == 4 and w.pending() == {"raw_ticks": 0, "trades": 0, "data_quality_events": 0}


def test_writer_buffers_through_an_outage_and_recovers():
    async def go(engine):
        dead = create_engine("postgresql+asyncpg://ofmp:x@127.0.0.1:1/none")
        w = MarketDataWriter(dead, max_pending=3)
        for seq in range(1, 6):
            w.record_tick(mtick(seq=seq, received=1_790_653_500_000 + seq))
        await w.flush()
        outage = (w.last_error is not None, w.pending()["raw_ticks"], w.dropped["raw_ticks"])
        await dead.dispose()
        w.engine = engine                                    # database back
        await w.flush()
        async with engine.connect() as conn:
            seqs = [r[0] for r in (await conn.execute(sa.text("SELECT sequence FROM raw_ticks ORDER BY 1"))).all()]
        return outage, w.last_error, seqs
    outage, error_after, seqs = run(with_engine(go))
    assert outage == (True, 3, 2)          # bounded: oldest 2 dropped, newest 3 kept
    assert error_after is None and seqs == [3, 4, 5]


# ---- session restore -------------------------------------------------------------------------------

def test_restored_engine_matches_the_live_engine():
    """Trades written by the live path, read back and restored into a fresh
    engine, reproduce the live engine's footprint, CVD and book."""
    async def go(engine):
        w = MarketDataWriter(engine)
        live = TickProcessorState(TICK)
        seq = [0]

        def sink(obs):
            seq[0] += 1
            w.record_trade({**obs, "symbol": "SYN", "trade_id": seq[0]}, provider="angelone", token="1")

        for t in synthetic_ticks(n_ticks=2500, seed=11):
            oe.process_tick(live, t, observation_sink=sink)
        await w.flush()
        session = ist_date(live.last_trade_ts_ms)
        tape, (bids, asks) = await load_session_tape(engine, "angelone", "SYN", session)
        restored = TickProcessorState(TICK)
        n = restore_trades(restored, [Trade(ts, p, q, s) for ts, p, q, s in tape], bids, asks)
        full = await load_session_trades(engine, "angelone", "SYN", session)
        return live, restored, n, seq[0], len(full)
    live, restored, n, written, loaded = run(with_engine(go))
    assert n == written == loaded
    payload = lambda e: server._chart_payload(e.footprint, e.cvd_tracker.cvd, e.last_candle_seen, 1, limit=10_000)
    assert payload(restored) == payload(live)
    assert restored.cvd_by_candle == live.cvd_by_candle
    assert (restored.last_price, restored.last_side) == (live.last_price, live.last_side)
    assert restored.book.top_n(5) == live.book.top_n(5)
    assert restored.volume.last_cum_vol is None             # first live tick re-primes, no fabricated trade


def test_server_restore_session_uses_todays_trades(monkeypatch):
    async def go(engine):
        now_ms = 1_790_653_500_000
        monkeypatch.setattr(server.time, "time", lambda: now_ms / 1000)
        w = MarketDataWriter(engine)
        for i, side in enumerate(("BUY", "SELL", "BUY"), start=1):
            w.record_trade({"trade_id": i, "ts_ms": now_ms - 60_000 + i * 1000, "ltp": 100.0 + i / 10, "qty": 65,
                            "algo_side": side, "symbol": "NIFTYTEST", "bid_levels": [[100.0, 65, 1]],
                            "ask_levels": [[100.5, 65, 1]]}, provider="angelone", token="9")
        await w.flush()
        monkeypatch.setitem(server.STATE, "db_engine", engine)
        eng = TickProcessorState(0.1)
        contract = {"token": "9", "tradingsymbol": "NIFTYTEST", "tick_size": 0.1, "lotsize": 65,
                    "exch_seg": "NFO", "name": "NIFTY", "expiry": "27OCT2026"}
        n = await server._restore_session(eng, contract)
        async with engine.connect() as conn:
            inst = (await conn.execute(sa.text("SELECT symbol, expiry, lot_size FROM instruments"))).all()
        return n, eng, inst
    n, eng, inst = run(with_engine(go))
    assert n == 3 and eng.last_side == "BUY" and eng.book.best_ask()[0] == 100.5
    assert [(s, str(e), l) for s, e, l in inst] == [("NIFTYTEST", "2026-10-27", 65)]


def test_upsert_instrument_updates_in_place():
    async def go(engine):
        c = {"token": "5", "tradingsymbol": "X", "tick_size": 0.1, "lotsize": 65, "exch_seg": "NFO"}
        await upsert_instrument(engine, c)
        await upsert_instrument(engine, {**c, "lotsize": 75})
        async with engine.connect() as conn:
            return (await conn.execute(sa.text("SELECT lot_size FROM instruments"))).all()
    assert run(with_engine(go)) == [(75,)]


# ---- CLI: import + reconcile --------------------------------------------------------------------------

def test_import_sessions_and_reconcile(tmp_path, monkeypatch, capsys):
    from backend.app import cli
    monkeypatch.setattr(config, "DATABASE_URL", TEST_DATABASE_URL)
    day = tmp_path / "2026-09-29"
    day.mkdir()
    base = 1_790_653_500_000                                   # 2026-09-29 IST
    rows = [{"trade_id": i, "ts_ms": base + i * 1000, "ltp": 24000.0, "qty": 65,
             "algo_side": "SELL", "algo_reason": "Midpoint Rule (VTRenders)"} for i in range(1, 6)]
    (day / "observations.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")

    assert cli.main(["reconcile", "--sessions-dir", str(tmp_path)]) == 1        # nothing imported yet
    assert cli.main(["import-sessions", "--sessions-dir", str(tmp_path)]) == 0
    assert cli.main(["import-sessions", "--sessions-dir", str(tmp_path)]) == 0  # idempotent
    assert cli.main(["reconcile", "--sessions-dir", str(tmp_path)]) == 0
    assert "2026-09-29           5         5  ok" in capsys.readouterr().out
    counts = run(with_engine(lambda e: trade_counts_by_session(e)))
    assert sum(counts.values()) == 5
