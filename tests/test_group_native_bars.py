"""orderbook_engine.group_native_bars: pure aggregation of native (60s) candle
bars into coarser display intervals for the /chart interval selector."""
import pytest

import orderbook_engine as oe

# Divisible by every interval this file groups by (60, 120, 180, 300, 900,
# 1800), so a test's own bar times land exactly where the arithmetic below
# expects, with no incidental flooring from the grouping's own boundary
# alignment muddying what each test is actually checking. (Unlike
# test_footprint_stats.py's T0, which is only ever bucketed at 60s and so
# only needs 60s alignment.)
T0 = 1_786_536_000


def native(t, open_, high, low, close, delta, min_delta, max_delta, trades, cells):
    return {"time": t, "open": open_, "high": high, "low": low, "close": close,
            "delta": delta, "min_delta": min_delta, "max_delta": max_delta,
            "trades": trades, "cells": cells}


def test_native_interval_is_a_no_op_returning_an_equal_but_distinct_list():
    bars = [native(T0, 100, 101, 99, 100.5, 5, -2, 7, 3, [{"price": 100, "buy": 3, "sell": 2}])]
    out = oe.group_native_bars(bars, 60, native_sec=60)
    assert out == bars and out is not bars   # same content, but list(bars) not bars itself


def test_rejects_non_multiple_or_nonpositive_interval():
    with pytest.raises(ValueError):
        oe.group_native_bars([], 90, native_sec=60)
    with pytest.raises(ValueError):
        oe.group_native_bars([], 0, native_sec=60)
    with pytest.raises(ValueError):
        oe.group_native_bars([], -180, native_sec=60)


def test_groups_three_one_minute_bars_into_one_three_minute_bar():
    bars = [
        native(T0, 100, 102, 99, 101, 5,
               min_delta=-3, max_delta=6, trades=4,
               cells=[{"price": 100, "buy": 5, "sell": 2}, {"price": 101, "buy": 2, "sell": 2}]),
        native(T0 + 60, 101, 103, 100, 102, -2,
               min_delta=-4, max_delta=1, trades=3,
               cells=[{"price": 101, "buy": 1, "sell": 3}, {"price": 102, "buy": 0, "sell": 0}]),
        native(T0 + 120, 102, 104, 101, 103.5, 7,
               min_delta=0, max_delta=7, trades=5,
               cells=[{"price": 102, "buy": 4, "sell": 0}, {"price": 103, "buy": 3, "sell": 0}]),
    ]
    out = oe.group_native_bars(bars, 180, native_sec=60)
    assert len(out) == 1
    g = out[0]
    assert g["time"] == T0                       # floor of T0 to a 180s boundary
    assert (g["open"], g["close"]) == (100, 103.5)
    assert (g["high"], g["low"]) == (104, 99)
    assert g["delta"] == 5 - 2 + 7                # == 10, matches sum
    assert g["trades"] == 4 + 3 + 5
    # cells merged by price across all three sub-bars, sorted descending
    assert g["cells"] == [
        {"price": 103, "buy": 3, "sell": 0},
        {"price": 102, "buy": 4, "sell": 0},
        {"price": 101, "buy": 3, "sell": 5},
        {"price": 100, "buy": 5, "sell": 2},
    ]


def test_min_max_delta_are_chained_across_sub_bars_not_independently_combined():
    """Sub-bar deltas: +5, -2, +7 (offsets 0, 5, 3 before each sub starts).
    Sub-bar (min,max) relative to ITS OWN start: (-3,6), (-4,1), (0,7).
    Chained absolute extremes: min(0-3, 5-4, 3+0) = min(-3,1,3) = -3
                               max(0+6, 5+1, 3+7) = max(6,6,10) = 10
    Naively min/maxing the raw sub values (-4 and 7) would be WRONG here."""
    bars = [
        native(T0, 1, 1, 1, 1, 5, min_delta=-3, max_delta=6, trades=1, cells=[]),
        native(T0 + 60, 1, 1, 1, 1, -2, min_delta=-4, max_delta=1, trades=1, cells=[]),
        native(T0 + 120, 1, 1, 1, 1, 7, min_delta=0, max_delta=7, trades=1, cells=[]),
    ]
    g = oe.group_native_bars(bars, 180, native_sec=60)[0]
    assert (g["min_delta"], g["max_delta"]) == (-3, 10)
    assert g["delta"] == 10


def test_a_single_missing_field_drops_it_from_the_whole_group_not_zero():
    bars = [
        native(T0, 100, 101, 99, 100, 5, min_delta=-1, max_delta=5, trades=2, cells=[]),
        native(T0 + 60, None, None, None, None, 3, min_delta=None, max_delta=None, trades=None, cells=[]),
    ]
    g = oe.group_native_bars(bars, 120, native_sec=60)[0]
    for key in ("open", "high", "low", "close", "trades", "min_delta", "max_delta"):
        assert key not in g, key
    assert g["delta"] == 8   # delta itself always present, never dropped


def test_two_consecutive_groups_and_an_incomplete_trailing_one():
    """5 one-minute bars into 2-minute groups: [T0,T0+60] | [T0+120,T0+180] | [T0+240]
    (the trailing group has only 1 of its 2 sub-minutes, same as a live session's
    still-forming coarser candle -- not a bug, the real data just stops there)."""
    bars = [native(T0 + 60 * i, i, i, i, i, 1, min_delta=0, max_delta=1, trades=1, cells=[])
            for i in range(5)]
    out = oe.group_native_bars(bars, 120, native_sec=60)
    assert [b["time"] for b in out] == [T0, T0 + 120, T0 + 240]
    assert [b["trades"] for b in out] == [2, 2, 1]
    assert [b["delta"] for b in out] == [2, 2, 1]


def test_ppr_grouped_cells_at_different_prices_never_collide():
    """Regression for a plausible bug: merging by price must not confuse two
    different price bands that happen to share a dict insertion order."""
    bars = [
        native(T0, 1, 1, 1, 1, 0, None, None, None, [{"price": 100.0, "buy": 1, "sell": 0}]),
        native(T0 + 60, 1, 1, 1, 1, 0, None, None, None, [{"price": 105.0, "buy": 0, "sell": 1}]),
    ]
    g = oe.group_native_bars(bars, 120, native_sec=60)[0]
    assert g["cells"] == [{"price": 105.0, "buy": 0, "sell": 1}, {"price": 100.0, "buy": 1, "sell": 0}]


@pytest.mark.parametrize("interval", [180, 300, 900, 1800])
def test_total_volume_is_conserved_for_every_allowed_interval(interval):
    """However bars get grouped, no buy/sell quantity is created or lost."""
    bars = [native(T0 + 60 * i, i, i, i, i, (i % 3) - 1, min_delta=-1, max_delta=1, trades=1,
                   cells=[{"price": 100 + (i % 4), "buy": i, "sell": i + 1}])
            for i in range(40)]
    grouped = oe.group_native_bars(bars, interval, native_sec=60)
    total_before = sum(c["buy"] + c["sell"] for b in bars for c in b["cells"])
    total_after = sum(c["buy"] + c["sell"] for b in grouped for c in b["cells"])
    assert total_before == total_after
    assert sum(b["delta"] for b in bars) == sum(b["delta"] for b in grouped)
