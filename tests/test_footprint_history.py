"""Multi-day footprint: finished sessions of one contract as chart bars, each
day rebuilt by the existing engine (stored sides, never re-classified), CVD
restarting at each session, cached, never mixed across contracts; today from
the live engine. Unit tests use fake loaders; database tests (skipped without
TEST_DATABASE_URL) cover replay parity and the HTTP endpoints."""
import asyncio
import os
from datetime import date, datetime, timedelta, timezone

import pytest

from backend.app.domain.orderflow import TickProcessorState, Trade, restore_trades
from backend.app.domain.orderflow.chart import chart_payload
from backend.app.services.footprint_history import DAY_CAPS, FOOTPRINT_VERSION, FootprintHistory, day_cap
from test_profile_history import D1, D3, D4, FakeStore, day_trades

IST = timezone(timedelta(hours=5, minutes=30))
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL not set")


def run(coro):
    return asyncio.run(coro)


def service(store):
    return FootprintHistory(store.list_dates, store.load_days, store.list_untagged)


def fresh_bars(trades, ppr, interval):
    """The same day rebuilt from scratch through the engine, the reference."""
    st = TickProcessorState(0.1)
    restore_trades(st, [Trade(timestamp=t, price=p, quantity=q, side=s) for t, p, q, s in trades])
    fp = st.footprint
    return chart_payload(fp, sum(fp.candle_delta(t) for t in fp.data), None, ppr, interval_sec=interval, limit=None)


def test_each_day_equals_a_fresh_build_and_session_cvd_starts_at_zero():
    store = FakeStore()
    svc = service(store)
    for interval in (60, 300, 900):
        for ppr in (1, 2, 5):
            out = run(svc.day("NIFTYOCT", 0.1, D3, ppr, interval))
            ref = fresh_bars(store.days[("NIFTYOCT", D3)], ppr, interval)
            assert out["bars"] == ref["bars"] and out["row_size"] == ref["row_size"]
            assert out["cvd_start"] == 0 == ref["cvd_offset"]                       # session CVD restarts
            assert (out["date"], out["state"], out["interval_sec"], out["version"]) == \
                   ("2026-10-07", "historical", interval, FOOTPRINT_VERSION)
    # the whole session, not just the last 60 bars
    assert len(run(svc.day("NIFTYOCT", 0.1, D3, 1, 60))["bars"]) > 60


def test_contracts_never_mix_and_missing_days_are_not_invented():
    store = FakeStore()
    svc = service(store)
    oct_ = run(svc.day("NIFTYOCT", 0.1, D1, 1, 300))
    nov = run(svc.day("NIFTYNOV", 0.1, D1, 1, 300))
    assert oct_["bars"] != nov["bars"] and nov["bars"] == fresh_bars(store.days[("NIFTYNOV", D1)], 1, 300)["bars"]
    assert run(svc.day("NIFTYOCT", 0.1, date(2026, 10, 6), 1, 300)) is None
    listing = run(svc.sessions("NIFTYOCT", D4, 20))
    assert listing["sessions"] == ["2026-10-05", "2026-10-07"]
    assert listing["excluded"] == [{"date": "2026-09-29", "reason": "recorded before contract tagging: contract unknown"}]


def test_cache_one_load_per_day_and_interval_or_row_changes_need_no_reload():
    store = FakeStore()
    svc = service(store)
    run(svc.day("NIFTYOCT", 0.1, D3, 1, 60))
    run(svc.day("NIFTYOCT", 0.1, D3, 1, 60))
    run(svc.day("NIFTYOCT", 0.1, D3, 5, 900))
    assert len(store.loads) == 1
    assert svc.stats == {"days_loaded": 1, "payload_hits": 1, "payload_builds": 2}
    svc.clear("NIFTYOCT")
    run(svc.day("NIFTYOCT", 0.1, D3, 1, 60))
    assert len(store.loads) == 2


def test_day_caps_by_interval():
    assert DAY_CAPS == {60: 5, 180: 10}
    assert [day_cap(i) for i in (60, 180, 300, 900, 1800)] == [5, 10, 20, 20, 20]
    store = FakeStore()
    for i in range(8):
        d = date(2026, 9, 20) + timedelta(days=i)
        store.days[("NIFTYOCT", d)] = day_trades(d, 24000, n=30, seed=i)
    assert len(run(service(store).sessions("NIFTYOCT", D4, day_cap(60)))["sessions"]) == 5


# -- server gateway and HTTP (database) -------------------------------------------------------

if TEST_DATABASE_URL:
    import server
    from backend.app.infrastructure.postgres.database import create_engine
    from backend.app.infrastructure.postgres.repositories import load_trades_by_date
    from backend.app.services.replay import ReplaySession
    from test_profile_history import _seed, db_schema  # noqa: F401  (fixture)


@needs_db
def test_gateway_finished_day_matches_replay_and_today_comes_from_the_live_engine(db_schema, monkeypatch):  # noqa: F811
    store = FakeStore()
    today = day_trades(D4, 25000, n=40, seed=4)

    async def go():
        engine = create_engine(TEST_DATABASE_URL)
        try:
            await _seed(engine, store)
            monkeypatch.setitem(server.STATE, "db_engine", engine)
            monkeypatch.setattr(server, "_FOOTPRINT_HISTORY", None)
            monkeypatch.setattr(server.time, "time", lambda: datetime(2026, 10, 8, 11, 0, tzinfo=IST).timestamp())
            contract = {"token": "NIFTYOCT", "tradingsymbol": "NIFTYOCT", "tick_size": 0.1, "lotsize": 65,
                        "exch_seg": "NFO"}
            eng = TickProcessorState(0.1)
            eng.profile = server._new_profile(0.1)
            restore_trades(eng, [Trade(timestamp=t, price=p, quantity=q, side=s) for t, p, q, s in today])
            eng.profile.add_trade(*today[0])
            monkeypatch.setitem(server.STATE, "engine", eng)
            monkeypatch.setitem(server.STATE, "contract", contract)
            gw = server.ServerMarketGateway()
            listing = await gw.footprint_sessions(60)
            finished = await gw.footprint_day("2026-10-07", 1, 60)
            live = await gw.footprint_day("2026-10-08", 1, 300)
            future = await gw.footprint_day("2026-10-09", 1, 60)
            records = await load_trades_by_date(engine, "angelone", D3)
            return listing, finished, live, future, records, eng
        finally:
            await engine.dispose()
    listing, finished, live, future, records, eng = run(go())
    assert listing["sessions"] == ["2026-10-05", "2026-10-07"] and listing["max_days"] == 5
    assert listing["live_date"] == "2026-10-08"
    # parity: the cached historical day equals the replay of that day at its end
    session = ReplaySession(D3.isoformat(), records, 0.1)
    session.seek(records[-1]["ts_ms"])
    fp = session.state.footprint
    ref = chart_payload(fp, sum(fp.candle_delta(t) for t in fp.data), None, 1, interval_sec=60, limit=None)
    assert finished["bars"] == ref["bars"] and finished["cvd_start"] == 0
    # today: every bar of today from the live engine, CVD from the session start
    assert live["state"] == "live" and live["cvd_start"] == 0
    assert live["bars"] == server._chart_payload(eng.footprint, eng.cvd_tracker.cvd, eng.last_candle_seen, 1,
                                                 interval_sec=300, limit=10_000)["bars"]
    assert future is None
