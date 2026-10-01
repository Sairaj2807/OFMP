"""Display intervals: _clamp_chart_interval, _chart_payload(interval_sec=...) and the
per-candle table (candle_table) — against a synthetic Angel session where practical.
(The legacy sockets and /api/replay endpoints these once covered were retired in Phase 9.)"""

import pytest

import config
import orderbook_engine as oe
import server
from backend.app.domain.orderflow.table import candle_table
from helpers import TICK, feed_engine, synthetic_ticks

BIG = 10_000
ALL_INTERVALS = config.ALLOWED_CHART_INTERVALS_SEC   # (60, 180, 300, 900, 1800)


# ---- _clamp_chart_interval ---------------------------------------------------

@pytest.mark.parametrize("value", ALL_INTERVALS)
def test_clamp_passes_through_every_allowed_value(value):
    assert server._clamp_chart_interval(value) == value
    assert server._clamp_chart_interval(str(value)) == value   # query params arrive as strings


@pytest.mark.parametrize("value,expected", [(61, 60), (119, 60), (121, 180),
                                            (600, 300),   # exact tie between 300 and 900: min() keeps the first
                                            (1799, 1800), (100_000, 1800), (0, 60), (-30, 60)])
def test_clamp_snaps_to_the_nearest_allowed_value(value, expected):
    assert server._clamp_chart_interval(value) == expected


@pytest.mark.parametrize("value", [None, "abc", [], {}, object()])
def test_clamp_falls_back_to_native_on_unparseable_input(value):
    assert server._clamp_chart_interval(value) == config.CANDLE_INTERVAL_SEC


# ---- _chart_payload(interval_sec=...) ----------------------------------------

@pytest.fixture(scope="module")
def engine():
    return feed_engine(synthetic_ticks())


def native_payload(engine, ppr=1, limit=BIG):
    return server._chart_payload(engine.footprint, engine.cvd_tracker.cvd, engine.last_candle_seen, ppr, limit=limit)


def test_default_interval_is_native_and_unchanged_by_the_new_parameter(engine):
    """Calling without interval_sec must behave exactly as before this feature."""
    explicit = server._chart_payload(engine.footprint, engine.cvd_tracker.cvd, engine.last_candle_seen, 2,
                                     interval_sec=config.CANDLE_INTERVAL_SEC, limit=20)
    implicit = server._chart_payload(engine.footprint, engine.cvd_tracker.cvd, engine.last_candle_seen, 2, limit=20)
    assert explicit == implicit


def test_grouped_payload_equals_grouping_the_native_payload_directly(engine):
    """_chart_payload's interval handling must be exactly group_native_bars
    applied to the native bars it would otherwise have returned — no separate
    logic path that could drift from the pure function tested on its own."""
    native = native_payload(engine)
    for interval in ALL_INTERVALS[1:]:
        payload = server._chart_payload(engine.footprint, engine.cvd_tracker.cvd, engine.last_candle_seen,
                                        1, interval_sec=interval, limit=BIG)
        expected_bars = oe.group_native_bars(native["bars"], interval, config.CANDLE_INTERVAL_SEC)
        assert payload["bars"] == expected_bars
        assert payload["interval_sec"] == interval
        assert payload["cvd_offset"] == native["cvd_offset"]   # unaffected by display grouping


@pytest.mark.parametrize("interval", ALL_INTERVALS)
def test_bar_times_land_on_the_interval_grid(engine, interval):
    for bar in native_payload(engine)["bars"] if interval == 60 else \
            server._chart_payload(engine.footprint, engine.cvd_tracker.cvd, engine.last_candle_seen,
                                  1, interval_sec=interval, limit=BIG)["bars"]:
        assert bar["time"] % interval == 0


def test_window_sizing_scales_with_the_multiple_on_real_data_too(engine):
    """Same idea as the deterministic test below, as a sanity check against
    real cell shapes: asking for a coarse interval must never return MORE
    display bars than asked for."""
    payload = server._chart_payload(engine.footprint, engine.cvd_tracker.cvd, engine.last_candle_seen,
                                    1, interval_sec=900, limit=5)
    assert len(payload["bars"]) <= 5


def _synthetic_footprint(n_candles, tick_size=1.0, price=100.0):
    """n_candles native (60s) candles starting at epoch 0, candle i carrying a
    single BUY trade of quantity i+1 -- deterministic and distinguishable by
    index, independent of whatever real session happens to be on disk."""
    fp = oe.Footprint(tick_size, config.CANDLE_INTERVAL_SEC)
    for i in range(n_candles):
        fp.add_trade(oe.Trade(timestamp=i * config.CANDLE_INTERVAL_SEC * 1000, price=price,
                              quantity=i + 1, side="BUY"))
    return fp


def test_window_pulls_enough_native_bars_for_every_requested_display_bar():
    """20 synthetic native (1-min) candles, candle i carrying BUY quantity i+1.
    Requesting 3 display bars at 5 minutes/bar (multiple=5) must pull the LAST
    15 native candles (indices 5..19) to group, not just the last 3 -- under-
    pulling would starve the grouping and silently return fewer or
    lighter-than-real bars instead of the true trailing window."""
    fp = _synthetic_footprint(20)
    bars = server._chart_payload(fp, closed_cvd=0, open_ts=None, ppr=1, interval_sec=300, limit=3)["bars"]
    assert len(bars) == 3
    assert [b["time"] for b in bars] == [300, 600, 900]
    # group 0 = native indices 5-9 (qty 6..10), group 1 = 10-14 (11..15), group 2 = 15-19 (16..20)
    assert [b["delta"] for b in bars] == [sum(range(6, 11)), sum(range(11, 16)), sum(range(16, 21))]


def test_live_snapshot_accepts_interval(monkeypatch):
    monkeypatch.setitem(server.STATE, "engine", oe.TickProcessorState(TICK))
    monkeypatch.setitem(server.STATE, "contract", {"tradingsymbol": "T", "tick_size": TICK, "lotsize": 65})
    snap = server._build_chart_snapshot(1, 300)
    assert snap["ready"] and snap["chart"]["interval_sec"] == 300


# ---- candle_table(interval_sec=...) -- the per-candle table (golden-pinned) ---
# Unlike
# _chart_payload, POC/value-area/imbalances are computed server-side per
# candle (Footprint.poc/value_area/stacked_imbalances -> the pure
# poc_from_rows/value_area_from_rows/stacked_imbalances_from_rows), so
# grouping happens at the native-timestamp level (group_candle_timestamps)
# and merges via Footprint.merged_candle_rows, not by merging pre-built bar
# dicts the way group_native_bars does for the chart page.

def _build_footprint(candles: dict, tick_size=1.0):
    """candles: {ts: [(price, buy_qty, sell_qty), ...]} -> a Footprint with
    exactly those native (60s) candles, each trade added BUY-then-SELL per
    row so delta/OHLC/candle_stats all come from the real add_trade path,
    not hand-set."""
    fp = oe.Footprint(tick_size, config.CANDLE_INTERVAL_SEC)
    for ts, rows in candles.items():
        for price, buy, sell in rows:
            if buy:
                fp.add_trade(oe.Trade(timestamp=ts * 1000, price=price, quantity=buy, side="BUY"))
            if sell:
                fp.add_trade(oe.Trade(timestamp=ts * 1000, price=price, quantity=sell, side="SELL"))
    return fp


def test_merged_candle_rows_sums_correctly_across_native_candles():
    fp = _build_footprint({0: [(100, 5, 0), (101, 0, 2)], 60: [(100, 3, 0), (102, 0, 1)]})
    rows = {r.price: r for r in fp.merged_candle_rows([0, 60], ppr=1)}
    assert (rows[100].buy_volume, rows[100].sell_volume, rows[100].delta) == (8, 0, 8)
    assert (rows[101].buy_volume, rows[101].sell_volume, rows[101].delta) == (0, 2, -2)
    assert (rows[102].buy_volume, rows[102].sell_volume) == (0, 1)
    assert [r.price for r in fp.merged_candle_rows([0, 60], ppr=1)] == [102, 101, 100]   # descending


def test_merged_candle_rows_of_a_single_ts_equals_get_candle_rows():
    fp = _build_footprint({0: [(100, 5, 2), (101, 1, 0)]})
    assert fp.merged_candle_rows([0], ppr=1) == fp.get_candle_rows(0, ppr=1)


def testcandle_table_delta_and_bullish_use_the_whole_group_not_just_one_native_candle():
    """open/close are deliberately DISTINCT (90 vs 110) so a bug that reads
    them from the wrong end of the group (e.g. swapped, or from a single
    native candle instead of the group's true first/last) flips `bullish`
    and `close_row` rather than passing by coincidence."""
    fp = _build_footprint({0: [(90, 10, 0)], 60: [(95, 0, 4)], 120: [(110, 6, 0)]})
    payload = candle_table(fp, {}, ppr=1, interval_sec=180, limit=10)
    assert len(payload["candles"]) == 1
    c = payload["candles"][0]
    assert c["ts"] == 0
    assert c["delta"] == 10 - 4 + 6          # summed across all three native candles
    assert c["bullish"] is True              # group open 90 (candle 0) <= group close 110 (candle 120)
    assert c["close_row"] == fp.row_price_for(110, 1)


def testcandle_table_cvd_is_the_last_CLOSED_native_candle_in_the_group():
    """A display group's last native candle may still be forming (no
    cvd_by_candle entry) even though earlier native candles in the SAME
    group have already closed -- the group's cvd must fall back to the last
    one that actually closed, not silently go missing."""
    fp = _build_footprint({0: [(100, 1, 0)], 60: [(100, 1, 0)], 120: [(100, 1, 0)]})
    cvd_by_candle = {0: 111, 60: 222}   # 120 (the group's last native candle) hasn't closed
    payload = candle_table(fp, cvd_by_candle, ppr=1, interval_sec=180, limit=10)
    assert payload["candles"][0]["cvd"] == 222


def testcandle_table_imbalances_cover_the_whole_newest_display_group():
    """Four price rows, ONE per native minute: a single native candle never
    has more than one price row, so the diagonal-imbalance scan (which needs
    a row AND the row below it) can never flag anything for any one of them
    alone -- stacked_imbalances_from_rows needs len(rows) >= 2 just to run
    its loop once. Grouped at 4 minutes/bar, the four rows merge into one
    ladder with a genuine 3-row BUY_IMBALANCE stack (101-102-103, each
    dominating the sell resting at the row below by 5x, threshold 3x). This
    is only reachable through Footprint.merged_candle_rows -- proving
    candle_table's imbalances now come from groups[-1] (however many
    native candles that display bar spans), not a single native candle."""
    fp = _build_footprint({
        0: [(100, 0, 10)],          # resting sell only -- the bottom reference, never itself flagged
        60: [(101, 50, 10)],        # buy dominates row100's sell (50/10=5x) -> flagged; its own sell seeds row102
        120: [(102, 50, 10)],       # buy dominates row101's sell (50/10=5x) -> flagged; its own sell seeds row103
        180: [(103, 50, 0)],        # buy dominates row102's sell (50/10=5x) -> flagged
    })
    payload = candle_table(fp, {}, ppr=1, interval_sec=240, limit=10)
    assert len(payload["candles"]) == 1        # all four native candles fall in one 4-minute group
    assert payload["imbalances"] == [{"kind": "BUY_IMBALANCE", "prices": [101, 102, 103]}]
    for ts in (0, 60, 120, 180):
        assert fp.stacked_imbalances(ts, ppr=1) == [], "a lone native candle has only one row, never a stack"


def testcandle_table_window_pulls_enough_native_candles_for_every_display_candle():
    """Same lesson as the chart payload's equivalent test: the native window
    must scale with limit * multiple, not just limit, or a coarse interval
    silently starves on under-pulled data. Deterministic, independent of
    whatever real session is on disk."""
    fp = _build_footprint({i * 60: [(100, i + 1, 0)] for i in range(20)})
    payload = candle_table(fp, {}, ppr=1, interval_sec=300, limit=3)
    assert len(payload["candles"]) == 3
    assert [c["ts"] for c in payload["candles"]] == [300, 600, 900]
    assert [c["delta"] for c in payload["candles"]] == [sum(range(6, 11)), sum(range(11, 16)), sum(range(16, 21))]


def testcandle_table_conserves_volume_at_every_allowed_interval(engine):
    native = candle_table(engine.footprint, engine.cvd_by_candle, 1, limit=BIG)
    native_total = sum(r["buy"] + r["sell"] for c in native["candles"] for r in c["rows"])
    for interval in ALL_INTERVALS[1:]:
        grouped = candle_table(engine.footprint, engine.cvd_by_candle, 1,
                                          interval_sec=interval, limit=BIG)
        grouped_total = sum(r["buy"] + r["sell"] for c in grouped["candles"] for r in c["rows"])
        assert grouped_total == native_total, interval
        assert sum(c["delta"] for c in grouped["candles"]) == sum(c["delta"] for c in native["candles"]), interval
        assert grouped["interval_sec"] == interval
