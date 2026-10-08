"""Multi-day market profiles: finished sessions of one contract, each built
independently by the existing engine, cached, never mixed across contracts,
never fabricated; and today's live profile kept separate.

Unit tests use fake loaders; the database tests (skipped without
TEST_DATABASE_URL) cover the lean symbol-filtered queries, replay parity and
live + history through the real server gateway."""
import asyncio
import os
import random
from datetime import date, datetime, timedelta, timezone

import pytest

import config
from backend.app.domain.profile import NSE_SESSION, PROFILE_VERSION, SessionProfile, build_profile
from backend.app.services.profile_history import ProfileHistory

IST = timezone(timedelta(hours=5, minutes=30))
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL not set")


def run(coro):
    return asyncio.run(coro)


def day_trades(d: date, base: float, n: int = 600, seed: int = 1) -> list:
    """A session of classified trades [(ts_ms, price, qty, side)] from 09:15 IST, around `base`."""
    rng, price, out = random.Random(seed), base, []
    t0 = int(datetime(d.year, d.month, d.day, 9, 15, tzinfo=IST).timestamp() * 1000)
    for i in range(n):
        price = round(price + rng.choice((-1, -0.5, 0, 0.5, 1)) * 0.5, 1)
        out.append((t0 + i * 35_000, price, rng.randint(1, 20) * 65, rng.choice(("BUY", "SELL"))))
    return out


def single_day(trades, row, tick=0.1):
    p = SessionProfile(tick, NSE_SESSION)
    for t in trades:
        p.add_trade(*t)
    return build_profile(p, row, include_previous=False)


D1, D2, D3, D4 = date(2026, 10, 5), date(2026, 10, 6), date(2026, 10, 7), date(2026, 10, 8)


class FakeStore:
    """Two contracts and one untagged day, as the loaders see them."""
    def __init__(self):
        self.days = {("NIFTYOCT", D1): day_trades(D1, 24600, seed=1),
                     ("NIFTYOCT", D3): day_trades(D3, 24900, seed=3),
                     ("NIFTYNOV", D1): day_trades(D1, 24750, seed=9)}
        self.untagged = [date(2026, 9, 29)]
        self.loads = []

    async def list_dates(self, symbol, before, limit, date_from):
        ds = sorted(d for s, d in self.days if s == symbol and d < before and (date_from is None or d >= date_from))
        return ds[-limit:]

    async def load_days(self, symbol, dates):
        self.loads.append((symbol, tuple(dates)))
        return {d: self.days.get((symbol, d), []) for d in dates}

    async def list_untagged(self, before, date_from):
        return [d for d in self.untagged if d < before and (date_from is None or d >= date_from)]


def service(store):
    return ProfileHistory(store.list_dates, store.load_days, store.list_untagged)


# -- unit ---------------------------------------------------------------------------------

def test_each_day_equals_its_own_single_day_profile_and_days_are_independent():
    store = FakeStore()
    out = run(service(store).history("NIFTYOCT", 0.1, 5, before=D4, limit=5))
    assert [s["date"] for s in out["sessions"]] == ["2026-10-05", "2026-10-07"]     # 10-06 absent: not invented
    assert all(s["state"] == "historical" and s["symbol"] == "NIFTYOCT" for s in out["sessions"])
    for s, d in zip(out["sessions"], (D1, D3)):
        assert s["profile"] == single_day(store.days[("NIFTYOCT", d)], 5)          # existing maths, untouched
    # independence: day 1's profile is the same whether or not day 3 exists
    store.days.pop(("NIFTYOCT", D3))
    alone = run(service(store).history("NIFTYOCT", 0.1, 5, before=D4, limit=5))
    assert alone["sessions"][0]["profile"] == out["sessions"][0]["profile"]
    assert out["sessions"][0]["profile"]["poc_tpo"] != out["sessions"][1]["profile"]["poc_tpo"]
    assert out["version"] == PROFILE_VERSION and out["before"] == "2026-10-08"


def test_contracts_are_never_mixed_and_untagged_days_are_excluded_with_a_reason():
    store = FakeStore()
    svc = service(store)
    oct_ = run(svc.history("NIFTYOCT", 0.1, 5, before=D4, limit=10))
    nov = run(svc.history("NIFTYNOV", 0.1, 5, before=D4, limit=10))
    assert [s["profile"]["stats"]["open"] for s in nov["sessions"]] == [store.days[("NIFTYNOV", D1)][0][1]]
    assert oct_["sessions"][0]["profile"] != nov["sessions"][0]["profile"]           # same date, other contract
    full = run(svc.history("NIFTYOCT", 0.1, 5, before=D4, limit=2))
    assert full["excluded"] == []                          # window full: the older untagged day is outside it
    assert {"date": "2026-09-29", "reason": "recorded before contract tagging: contract unknown"} in oct_["excluded"]
    wide = run(svc.history("NIFTYOCT", 0.1, 5, before=D4, limit=10, date_from=date(2026, 9, 1)))
    assert {"date": "2026-09-29", "reason": "recorded before contract tagging: contract unknown"} in wide["excluded"]
    assert all(s["date"] != "2026-09-29" for s in wide["sessions"])          # never guessed into a contract


def test_cache_first_request_builds_then_hits_and_row_change_needs_no_reload():
    store = FakeStore()
    svc = service(store)
    run(svc.history("NIFTYOCT", 0.1, 5, before=D4, limit=5))
    assert len(store.loads) == 1 and svc.stats["profile_builds"] == 2
    again = run(svc.history("NIFTYOCT", 0.1, 5, before=D4, limit=5))
    assert len(store.loads) == 1 and svc.stats["profile_hits"] == 2                 # no reload, no rebuild
    for row in (1, 2, 10):                                                           # other rows: build, no reload
        r = run(svc.history("NIFTYOCT", 0.1, row, before=D4, limit=5))
        assert [s["profile"]["row_size"] for s in r["sessions"]] == [row, row]
        assert r["sessions"][1]["profile"] == single_day(store.days[("NIFTYOCT", D3)], row)
    assert len(store.loads) == 1
    cached = run(svc.history("NIFTYOCT", 0.1, 5, before=D4, limit=5))
    assert cached["sessions"][0]["profile"] is again["sessions"][0]["profile"]        # the very same cached object
    svc.clear("NIFTYOCT")
    run(svc.history("NIFTYOCT", 0.1, 5, before=D4, limit=5))
    assert len(store.loads) == 2                                                      # cleared: reloaded


def test_a_failing_or_empty_day_does_not_sink_the_others():
    store = FakeStore()
    store.days[("NIFTYOCT", D2)] = [(int(datetime(2026, 10, 6, 16, 0, tzinfo=IST).timestamp() * 1000), 1.0, 1, "BUY")]
    store.days[("NIFTYOCT", date(2026, 10, 2))] = [("not a time", 1, 1, "BUY")]       # breaks the fold
    out = run(service(store).history("NIFTYOCT", 0.1, 5, before=D4, limit=10))
    states = {s["date"]: s["state"] for s in out["sessions"]}
    assert states == {"2026-10-02": "error", "2026-10-05": "historical", "2026-10-07": "historical"}
    assert {"date": "2026-10-06", "reason": "no trades inside the session hours"} in out["excluded"]


def test_limit_and_date_range():
    store = FakeStore()
    svc = service(store)
    assert [s["date"] for s in run(svc.history("NIFTYOCT", 0.1, 5, before=D4, limit=1))["sessions"]] == ["2026-10-07"]
    r = run(svc.history("NIFTYOCT", 0.1, 5, before=D4, limit=5, date_from=D2))
    assert [s["date"] for s in r["sessions"]] == ["2026-10-07"]
    assert run(svc.history("NIFTYOCT", 0.1, 5, before=D1, limit=5))["sessions"] == []


# -- database -------------------------------------------------------------------------------

if TEST_DATABASE_URL:
    import sqlalchemy as sa

    import server
    from backend.app.infrastructure.postgres.database import create_engine
    from backend.app.infrastructure.postgres.repositories import (load_symbol_sessions, load_trades_by_date,
                                                                  symbol_session_dates, untagged_session_dates)
    from backend.app.infrastructure.postgres.writer import MarketDataWriter
    from backend.app.services.replay import ReplaySession


@pytest.fixture(scope="module")
def db_schema():
    if not TEST_DATABASE_URL:
        yield
        return
    from alembic import command
    from alembic.config import Config
    saved = config.DATABASE_URL
    config.DATABASE_URL = TEST_DATABASE_URL
    try:
        command.upgrade(Config("alembic.ini"), "head")
        yield
    finally:
        config.DATABASE_URL = saved


async def _seed(engine, store):
    async with engine.begin() as conn:
        await conn.execute(sa.text("TRUNCATE trades"))
    w = MarketDataWriter(engine)
    for (symbol, d), trades in store.days.items():
        for i, (ts, price, qty, side) in enumerate(trades, start=1):
            w.record_trade({"trade_id": i, "ts_ms": ts, "ltp": price, "qty": qty, "algo_side": side,
                            "symbol": symbol}, provider="angelone", token=symbol)
    untagged = day_trades(date(2026, 9, 29), 24400, n=50, seed=7)
    for i, (ts, price, qty, side) in enumerate(untagged, start=1):
        w.record_trade({"trade_id": i, "ts_ms": ts, "ltp": price, "qty": qty, "algo_side": side},
                       provider="angelone", token=None)
    await w.flush()


@needs_db
def test_lean_queries_filter_by_contract_and_match_replay(db_schema):
    store = FakeStore()

    async def go():
        engine = create_engine(TEST_DATABASE_URL)
        try:
            await _seed(engine, store)
            dates = await symbol_session_dates(engine, "angelone", "NIFTYOCT", D4, 10)
            untagged = await untagged_session_dates(engine, "angelone", D4)
            loaded = await load_symbol_sessions(engine, "angelone", "NIFTYOCT", dates)
            replay_records = await load_trades_by_date(engine, "angelone", D3)
            return dates, untagged, loaded, replay_records
        finally:
            await engine.dispose()
    dates, untagged, loaded, replay_records = run(go())
    assert dates == [D1, D3] and untagged == [date(2026, 9, 29)]
    assert loaded[D1] == store.days[("NIFTYOCT", D1)]                      # NOV trades on the same date not mixed in
    # the historical profile equals the replay's profile of that day, at the end of the session
    session = ReplaySession(D3.isoformat(), replay_records, 0.1)
    session.seek(replay_records[-1]["ts_ms"])
    assert build_profile(session.state.profile, 5, include_previous=False) == single_day(loaded[D3], 5)


@needs_db
def test_gateway_history_is_static_while_today_stays_live(db_schema, monkeypatch):
    """Through the real server gateway: history comes from the database once (cached),
    today's profile comes from the live engine, and live trades never touch history."""
    store = FakeStore()
    store.days[("NIFTYOCT", D4)] = day_trades(D4, 25000, n=20, seed=4)      # today, partly recorded

    async def go():
        engine = create_engine(TEST_DATABASE_URL)
        try:
            await _seed(engine, store)
            monkeypatch.setitem(server.STATE, "db_engine", engine)
            monkeypatch.setattr(server, "_PROFILE_HISTORY", None)
            monkeypatch.setattr(server.time, "time", lambda: datetime(2026, 10, 8, 11, 0, tzinfo=IST).timestamp())
            contract = {"token": "NIFTYOCT", "tradingsymbol": "NIFTYOCT", "tick_size": 0.1, "lotsize": 65,
                        "exch_seg": "NFO"}
            from backend.app.domain.orderflow import TickProcessorState
            eng = TickProcessorState(0.1)
            eng.profile = server._new_profile(0.1)
            monkeypatch.setitem(server.STATE, "engine", eng)
            monkeypatch.setitem(server.STATE, "contract", contract)
            gw = server.ServerMarketGateway()
            first = await gw.profile_history(5, 10)
            epoch = gw.history_epoch()
            for ts, price, qty, side in store.days[("NIFTYOCT", D4)]:        # today's trades arrive live
                eng.profile.add_trade(ts, price, qty, side)
            second = await gw.profile_history(5, 10)
            live = server._build_profile_snapshot(5)["profile"]
            stats = dict(server._PROFILE_HISTORY.stats)
            return first, second, epoch, gw.history_epoch(), live, stats
        finally:
            await engine.dispose()
    first, second, epoch_before, epoch_after, live, stats = run(go())
    assert [s["date"] for s in first["sessions"]] == ["2026-10-05", "2026-10-07"]    # today is not history
    assert first["live_date"] == "2026-10-08" and first["sessions"] == second["sessions"]
    assert stats["sessions_loaded"] == 2 and stats["profile_hits"] == 2           # second call served from cache
    assert live["date"] == "2026-10-08" and live["stats"]["trades"] == 20
    assert epoch_before == epoch_after == ("NIFTYOCT", "2026-10-08")
