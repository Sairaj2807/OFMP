"""Footprint.candle_stats: the running delta path the chart's min/max-delta rows need."""
import orderbook_engine as oe
from helpers import trade

T0 = 1_786_524_540   # a minute boundary (epoch s)


def make():
    return oe.Footprint(tick_size=0.1, candle_interval_sec=60)


def test_running_delta_path_min_max_and_trades():
    fp = make()
    # Same sequence the chart library documents: bid 5, ask 12, bid 10.
    # SELL=bid, BUY=ask: run is -5, +7, -3 -> min -5, max 7, final -3.
    fp.add_trade(trade(T0, 100.0, 5, "SELL"))
    fp.add_trade(trade(T0 + 1, 100.1, 12, "BUY"))
    fp.add_trade(trade(T0 + 2, 100.0, 10, "SELL"))
    s = fp.candle_stats[T0]
    assert (s["min"], s["max"], s["run"], s["trades"]) == (-5, 7, -3, 3)
    assert fp.candle_delta(T0) == -3          # consistent with the cell arithmetic


def test_initial_zero_is_part_of_the_range():
    fp = make()
    fp.add_trade(trade(T0, 100.0, 5, "BUY"))
    s = fp.candle_stats[T0]
    assert (s["min"], s["max"]) == (0, 5)     # never sold: min stays at the initial 0
    fp2 = make()
    fp2.add_trade(trade(T0, 100.0, 5, "SELL"))
    assert (fp2.candle_stats[T0]["min"], fp2.candle_stats[T0]["max"]) == (-5, 0)


def test_candles_are_independent():
    fp = make()
    fp.add_trade(trade(T0, 100.0, 5, "SELL"))
    fp.add_trade(trade(T0 + 60, 100.0, 7, "BUY"))
    assert fp.candle_stats[T0]["run"] == -5
    assert fp.candle_stats[T0 + 60]["run"] == 7 and fp.candle_stats[T0 + 60]["min"] == 0


def test_prune_drops_stats_and_ohlc_with_the_candle():
    fp = make()
    for i in range(5):
        fp.add_trade(trade(T0 + 60 * i, 100.0, 1, "BUY"))
    fp.prune_old(keep_last=2)
    assert sorted(fp.data) == [T0 + 180, T0 + 240]
    assert sorted(fp.candle_stats) == [T0 + 180, T0 + 240]
    assert sorted(fp.candle_ohlc) == [T0 + 180, T0 + 240]


def test_volumes_unchanged_by_tracking():
    """The tracking is additive: cells, delta and OHLC are what they always were."""
    fp = make()
    fp.add_trade(trade(T0, 100.0, 5, "SELL"))
    fp.add_trade(trade(T0, 100.0, 8, "BUY"))
    cell = fp.data[T0][100.0]
    assert (cell.buy_volume, cell.sell_volume, cell.buy_trades, cell.sell_trades) == (8, 5, 1, 1)
    assert fp.candle_ohlc[T0] == {"open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0}
