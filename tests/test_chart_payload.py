"""server._chart_payload against a synthetic Angel session run through the
real engine (see helpers.synthetic_ticks)."""
import pytest

import server
from helpers import feed_engine, synthetic_ticks

BIG = 10_000   # window large enough to include every candle


@pytest.fixture(scope="module")
def engine():
    return feed_engine(synthetic_ticks())


@pytest.fixture(scope="module")
def records(engine):
    return engine.trade_log


def payload(engine, ppr=1, limit=BIG):
    return server._chart_payload(engine.footprint, engine.cvd_tracker.cvd, engine.last_candle_seen,
                                 ppr, limit=limit)


@pytest.mark.parametrize("ppr", [1, 2, 3, 5])
def test_every_saved_lot_lands_in_a_cell(engine, records, ppr):
    bars = payload(engine, ppr)["bars"]
    assert sum(c["buy"] for b in bars for c in b["cells"]) == sum(r["buy_qty"] for r in records)
    assert sum(c["sell"] for b in bars for c in b["cells"]) == sum(r["sell_qty"] for r in records)
    assert sum(b["delta"] for b in bars) == sum(r["buy_qty"] - r["sell_qty"] for r in records)


def test_min_max_delta_match_the_trade_order_path(engine, records):
    """Recompute each candle's running delta straight from the engine's
    classified trades, in the order they were added."""
    expected = {}
    for r in records:
        ts = r["ts_ms"] // 1000
        ts -= ts % 60
        s = expected.setdefault(ts, {"run": 0, "min": 0, "max": 0, "trades": 0})
        for qty, sign in ((r["buy_qty"], 1), (r["sell_qty"], -1)):
            if qty:
                s["run"] += sign * qty
                s["min"], s["max"] = min(s["min"], s["run"]), max(s["max"], s["run"])
                s["trades"] += 1
    bars = {b["time"]: b for b in payload(engine)["bars"]}
    assert set(bars) == set(expected)
    for ts, s in expected.items():
        assert (bars[ts]["min_delta"], bars[ts]["max_delta"], bars[ts]["trades"]) == (s["min"], s["max"], s["trades"])
        assert bars[ts]["delta"] == s["run"]


@pytest.mark.parametrize("limit", [1, 5, 20, BIG])
def test_cvd_offset_plus_window_deltas_is_the_true_cvd(engine, limit):
    """The chart library adds bar deltas onto cvd_offset, so offset + the
    window's deltas must equal the whole session's running total whatever
    the window size (forming candle included)."""
    full = payload(engine)["bars"]
    total = sum(b["delta"] for b in full)
    p = payload(engine, limit=limit)
    assert len(p["bars"]) == min(limit, len(full))
    assert p["cvd_offset"] + sum(b["delta"] for b in p["bars"]) == total


def test_engine_cvd_counts_closed_candles_only(engine):
    """Documents the semantic the chart deliberately differs from: the
    engine's own tracker excludes the forming candle, the chart's CVD does not."""
    full = payload(engine)["bars"]
    assert engine.cvd_tracker.cvd == sum(b["delta"] for b in full[:-1])
    assert full[-1]["time"] == engine.last_candle_seen


@pytest.mark.parametrize("ppr", [1, 3, 5])
def test_rows_are_floor_bands_on_the_ppr_grid(engine, ppr):
    p = payload(engine, ppr)
    assert p["row_size"] == ppr
    for b in p["bars"]:
        prices = [c["price"] for c in b["cells"]]
        assert prices == sorted(prices, reverse=True) and len(set(prices)) == len(prices)
        for price in prices:
            assert abs(price / ppr - round(price / ppr)) < 1e-6   # a multiple of ppr


def test_ohlc_is_consistent(engine):
    for b in payload(engine)["bars"]:
        assert b["low"] <= min(b["open"], b["close"]) <= max(b["open"], b["close"]) <= b["high"]
        traded = [c["price"] for c in b["cells"]]
        assert b["low"] >= min(traded) and b["high"] < max(traded) + 1   # raw prices sit inside their bands


def test_default_window_is_capped(engine):
    assert len(server._chart_payload(engine.footprint, engine.cvd_tracker.cvd,
                                     engine.last_candle_seen, 1)["bars"]) <= server.CHART_BARS


def test_live_snapshot_shape(engine, monkeypatch):
    monkeypatch.setitem(server.STATE, "engine", engine)
    monkeypatch.setitem(server.STATE, "contract", {"tradingsymbol": "T", "tick_size": 0.1, "lotsize": 65})
    snap = server._build_chart_snapshot(2)
    assert snap["ready"] and snap["mode"] == "live"
    assert set(snap) >= {"status", "source", "book", "quote", "chart"}
    assert snap["chart"]["row_size"] == 2
    assert snap["book"]["bids"] and snap["book"]["asks"]


def test_not_ready_before_the_engine_exists(monkeypatch):
    monkeypatch.setitem(server.STATE, "engine", None)
    assert server._build_chart_snapshot(1) == {"ready": False}
