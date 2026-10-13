"""Footprint table fields per display bar (owner decisions 2026-10-13): Volume,
VPOC, VPOC Vol, COT (buy - sell), OI (closing) and dOI (vs the previous candle,
the session's first candle vs the previous session's close).

Pure tests run always; the storage tests (trades.open_interest, the 0005
backfill from raw_ticks, the previous-session baseline) need TEST_DATABASE_URL."""
import asyncio
import os
from datetime import date

import pytest

import config
import replay_engine
import server
from backend.app.domain.orderflow import TickProcessorState, Trade, restore_trades
from backend.app.domain.orderflow.chart import chart_payload, table_fields
from backend.app.infrastructure.postgres.rows import trade_row
from helpers import TICK, synthetic_ticks
import orderbook_engine as oe

T0 = 1_790_653_500           # 2026-09-29 09:15 IST, on the 30-minute grid
TABLE = ("volume", "cot", "vpoc", "vpoc_volume", "oi", "doi")


def bar(cells, delta=None):
    cells = [{"price": p, "buy": b, "sell": s} for p, b, s in cells]
    return {"cells": cells, "delta": sum(c["buy"] - c["sell"] for c in cells) if delta is None else delta}


def test_volume_cot_and_vpoc_with_the_charts_tie_rule():
    f = table_fields(bar([(100.0, 30, 10), (101.0, 5, 35), (102.0, 1, 2)]))
    assert f == {"volume": 83, "cot": -11, "vpoc": 101.0, "vpoc_volume": 40}
    # a tie goes to the higher price (the chart library's POC rule: the pink line and the table agree)
    assert table_fields(bar([(100.0, 20, 20), (101.0, 0, 40)]))["vpoc"] == 101.0
    assert table_fields(bar([(101.0, 0, 40), (100.0, 20, 20)]))["vpoc"] == 101.0
    assert table_fields(bar([])) == {"volume": 0, "cot": 0, "vpoc": None, "vpoc_volume": None}


def fp_with(trades):
    state = TickProcessorState(TICK)
    restore_trades(state, [Trade(T0 * 1000 + ms, price, qty, side, oi) for ms, price, qty, side, oi in trades])
    return state


def test_closing_oi_per_candle_and_doi_vs_the_previous_candle():
    st = fp_with([(1_000, 100.0, 65, "BUY", 5_000), (30_000, 100.1, 65, "SELL", 5_100),
                  (50_000, 100.2, 65, "BUY", None),                 # no OI on this trade: the close stays 5_100
                  (61_000, 100.3, 130, "BUY", 5_400),
                  (125_000, 100.1, 65, "SELL", None),               # a candle without any OI
                  (190_000, 100.0, 65, "SELL", 5_350)])
    bars = chart_payload(st.footprint, 0, None, 1, limit=None, oi_before=4_900)["bars"]
    assert [(b["oi"], b["doi"]) for b in bars] == [(5_100, 200), (5_400, 300), (None, None), (5_350, -50)]
    # without the previous session's close the first candle's dOI is unknown, never 0
    first = chart_payload(st.footprint, 0, None, 1, limit=None)["bars"][0]
    assert (first["oi"], first["doi"]) == (5_100, None)


def test_grouped_bars_take_the_last_candles_oi_and_compare_with_the_previous_group():
    st = fp_with([(m * 60_000 + 1_000, 100.0 + m / 10, 65, "BUY", 1_000 + 10 * m) for m in range(12)])
    bars = chart_payload(st.footprint, 0, None, 1, interval_sec=300, limit=None, oi_before=990)["bars"]
    assert [(b["oi"], b["doi"]) for b in bars] == [(1_040, 50), (1_090, 50), (1_110, 20)]
    assert [b["volume"] for b in bars] == [325, 325, 130]


def test_a_limited_window_still_compares_its_first_bar_with_the_candle_before_it():
    st = fp_with([(m * 60_000 + 1_000, 100.0, 65, "BUY", 2_000 + m) for m in range(10)])
    bars = chart_payload(st.footprint, 0, None, 1, limit=3, oi_before=1_000)["bars"]
    assert [(b["oi"], b["doi"]) for b in bars] == [(2_007, 1), (2_008, 1), (2_009, 1)]


def oi_ticks(seed=5):
    """synthetic ticks with an open interest that moves every few ticks."""
    ticks, oi = [], 18_000_000
    for i, t in enumerate(synthetic_ticks(n_ticks=1500, seed=seed)):
        if i % 7 == 0:
            oi += (i % 3 - 1) * 650
        ticks.append({**t, "oi": oi})
    return ticks


def test_live_engine_replay_and_restore_agree_on_the_table():
    live, observations = TickProcessorState(TICK), []
    for t in oi_ticks():
        oe.process_tick(live, t, observation_sink=observations.append)
    assert all("oi" in o for o in observations)
    for i, o in enumerate(observations):
        o["trade_id"] = i + 1
    replay = replay_engine.build_replay_state(observations, TICK)
    restored = TickProcessorState(TICK)
    restore_trades(restored, [Trade(o["ts_ms"], o["ltp"], o["qty"], o["algo_side"], o["oi"]) for o in observations])

    def table(e, interval):
        payload = server._chart_payload(e.footprint, e.cvd_tracker.cvd, e.last_candle_seen, 2,
                                        interval_sec=interval, limit=10_000, oi_before=17_999_350)
        return [{k: b[k] for k in ("time", *TABLE)} for b in payload["bars"]]
    for interval in (60, 180, 300):
        assert table(live, interval) == table(replay, interval) == table(restored, interval)
    bars = table(live, 60)
    assert bars[0]["doi"] == bars[0]["oi"] - 17_999_350
    assert all(b["cot"] == sum(c["buy"] - c["sell"] for c in cells) for b, cells in
               zip(bars, (x["cells"] for x in server._chart_payload(live.footprint, 0, None, 2, limit=10_000)["bars"])))


def test_trade_rows_store_the_ticks_open_interest():
    obs = {"trade_id": 1, "ts_ms": T0 * 1000, "ltp": 100.0, "qty": 65, "algo_side": "BUY", "oi": 18_400_000}
    assert trade_row(obs, "angelone")["open_interest"] == 18_400_000
    assert trade_row({**obs, "oi": None}, "angelone")["open_interest"] is None


# ---- storage ------------------------------------------------------------------------------------

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
db = pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL not set")


@pytest.fixture(scope="module")
def schema():
    from alembic import command
    from alembic.config import Config
    saved = config.DATABASE_URL
    config.DATABASE_URL = TEST_DATABASE_URL
    try:
        cfg = Config("alembic.ini")
        command.downgrade(cfg, "base")
        command.upgrade(cfg, "head")
        yield cfg
    finally:
        config.DATABASE_URL = saved


async def _with_engine(fn):
    from backend.app.infrastructure.postgres.database import create_engine
    engine = create_engine(TEST_DATABASE_URL)
    try:
        return await fn(engine)
    finally:
        await engine.dispose()


@db
def test_open_interest_round_trips_and_the_previous_close_is_the_baseline(schema):
    from backend.app.infrastructure.postgres.repositories import (last_open_interest_before, load_session_tape,
                                                                  load_symbol_sessions, load_trades_by_date)
    from backend.app.infrastructure.postgres.writer import MarketDataWriter

    d1, d2 = date(2026, 9, 29), date(2026, 9, 30)

    async def go(engine):
        w = MarketDataWriter(engine)
        for i, (day_ms, oi) in enumerate(((T0 * 1000, 100), (T0 * 1000 + 60_000, 120), (T0 * 1000 + 120_000, None),
                                          ((T0 + 86_400) * 1000, 150)), start=1):
            w.record_trade({"trade_id": i, "ts_ms": day_ms, "ltp": 100.0, "qty": 65, "algo_side": "BUY",
                            "symbol": "NIFTYTEST", "oi": oi}, provider="angelone", token="7")
        w.record_trade({"trade_id": 9, "ts_ms": T0 * 1000 + 180_000, "ltp": 100.0, "qty": 65, "algo_side": "SELL",
                        "symbol": "OTHER", "oi": 999}, provider="angelone", token="8")
        await w.flush()
        tape = (await load_session_tape(engine, "angelone", "NIFTYTEST", d1, with_oi=True))[0]
        days = await load_symbol_sessions(engine, "angelone", "NIFTYTEST", [d1], with_oi=True)
        plain = await load_symbol_sessions(engine, "angelone", "NIFTYTEST", [d1])
        replay = await load_trades_by_date(engine, "angelone", d1)
        return (tape, days, plain, replay,
                await last_open_interest_before(engine, "angelone", "NIFTYTEST", d2),
                await last_open_interest_before(engine, "angelone", "NIFTYTEST", d1))
    tape, days, plain, replay, before_d2, before_d1 = asyncio.run(_with_engine(go))
    assert [t[4] for t in tape] == [100, 120, None] == [t[4] for t in days[d1]]
    assert all(len(t) == 4 for t in plain[d1])                       # callers that do not ask get 4-tuples
    assert [o["oi"] for o in replay if o["symbol"] == "NIFTYTEST"] == [100, 120, None]
    assert before_d2 == 120          # the last KNOWN OI of the previous session (the NULL is skipped)
    assert before_d1 is None         # nothing earlier; other contracts never count


def _migration_0005():
    import importlib.util
    spec = importlib.util.spec_from_file_location("m0005", "backend/alembic/versions/0005_trade_open_interest.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@db
def test_migration_backfills_trades_from_the_raw_ticks_of_the_same_token(schema):
    import sqlalchemy as sa

    async def go(engine):
        async with engine.begin() as conn:
            await conn.execute(sa.text("DELETE FROM trades"))
            await conn.execute(sa.text("DELETE FROM raw_ticks"))
            await conn.execute(sa.text("""
                INSERT INTO raw_ticks (received_at, provider, token, sequence, last_trade_ts, ltp, open_interest,
                                       cumulative_volume, bid_px, bid_qty, bid_orders, ask_px, ask_qty, ask_orders)
                SELECT received, 'angelone', token, seq, to_timestamp(ltt), 100, oi, 0, '{}', '{}', '{}', '{}', '{}', '{}'
                FROM (VALUES (to_timestamp(:t0 + 0.4), '7', 1, :t0, 500),
                             (to_timestamp(:t0 + 5.2), '7', 2, :t0 + 5, 525),
                             (to_timestamp(:t0 + 5.3), '8', 3, :t0 + 5, 9999)) v(received, token, seq, ltt, oi)"""),
                {"t0": T0})
            await conn.execute(sa.text("""
                INSERT INTO trades (trade_ts, provider, session_date, trade_seq, symbol, token, price, qty, side,
                                    classifier_name, classifier_version)
                VALUES (to_timestamp(:t0), 'angelone', '2026-09-29', 1, 'S', '7', 100, 65, 'BUY', 'c', 'v1'),
                       (to_timestamp(:t0 + 7), 'angelone', '2026-09-29', 2, 'S', '7', 100, 65, 'BUY', 'c', 'v1'),
                       (to_timestamp(:t0 + 900), 'angelone', '2026-09-29', 3, 'S', '7', 100, 65, 'BUY', 'c', 'v1'),
                       (to_timestamp(:t0 + 6), 'angelone', '2026-09-29', 4, NULL, NULL, 100, 65, 'SELL', 'c', 'v1')"""),
                {"t0": T0})
            await conn.execute(sa.text(_migration_0005().BACKFILL))
            return (await conn.execute(sa.text("SELECT trade_seq, open_interest FROM trades ORDER BY trade_seq"))).all()
    rows = asyncio.run(_with_engine(go))
    # the latest tick of the same token at or before the trade; no tick within 2 minutes or no token: unknown
    assert [tuple(r) for r in rows] == [(1, 500), (2, 525), (3, None), (4, None)]


def test_history_days_compare_their_first_candle_with_the_previous_close():
    from backend.app.services.footprint_history import FOOTPRINT_VERSION, FootprintHistory
    d = date(2026, 9, 29)
    trades = [(T0 * 1000 + m * 60_000, 100.0, 65, "BUY", 700 + m) for m in range(3)]

    async def list_dates(*_):
        return [d]

    async def load_days(symbol, dates):
        return {d: trades}

    async def untagged(*_):
        return []

    async def oi_before(symbol, day):
        assert (symbol, day) == ("S", d)
        return 650
    out = asyncio.run(FootprintHistory(list_dates, load_days, untagged, oi_before=oi_before).day("S", TICK, d, 1, 60))
    assert out["version"] == FOOTPRINT_VERSION == "footprint-v2"     # browsers drop days cached without the table
    assert [(b["oi"], b["doi"], b["volume"], b["cot"]) for b in out["bars"]] == [
        (700, 50, 65, 65), (701, 1, 65, 65), (702, 1, 65, 65)]
