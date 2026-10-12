"""Footprint chart payload: native candles serialised as chart columns at a
display interval. One function for live snapshots, replay and historical
sessions, so the three can never drift apart.

`closed_cvd` is the CVD over every CLOSED native candle, `open_ts` the
still-forming native candle (None for a finished session). The chart library
accumulates CVD itself from `cvd_offset` across the bars it is given
(including the forming one), so cvd_offset is the CVD just before the first
bar sent: everything closed, minus the closed native candles in the window
(grouping a closed and a forming native candle into one display bar does not
change which native candles are closed).

Cell prices are the PPR band FLOOR, as everywhere else in the engine; the
frontend adapter shifts them to the band centre, which the library draws around.
"""
from typing import Optional

from .bars import group_native_bars
from .settings import NATIVE_CANDLE_SEC


def native_bar(fp, ts: int, ppr: int) -> dict:
    ohlc = fp.candle_ohlc.get(ts) or {}
    stats = fp.candle_stats.get(ts) or {}
    return {
        "time": ts,
        "open": ohlc.get("open"), "high": ohlc.get("high"),
        "low": ohlc.get("low"), "close": ohlc.get("close"),
        "delta": fp.candle_delta(ts),
        "min_delta": stats.get("min"), "max_delta": stats.get("max"),
        "trades": stats.get("trades"),
        "cells": [{"price": r.price, "buy": r.buy_volume, "sell": r.sell_volume} for r in fp.get_candle_rows(ts, ppr)],
    }


def chart_payload(fp, closed_cvd: int, open_ts: Optional[int], ppr: int, interval_sec: int = NATIVE_CANDLE_SEC,
                  limit: Optional[int] = 60, since: Optional[int] = None, until: Optional[int] = None,
                  native_sec: int = NATIVE_CANDLE_SEC) -> dict:
    """The last `limit` display bars (all, when limit is None) of the native candles in
    [since, until) (epoch seconds; unbounded when None)."""
    row_size = max(ppr, fp.tick_size)
    multiple = max(1, interval_sec // native_sec)
    keys = sorted(t for t in fp.data.keys() if (since is None or t >= since) and (until is None or t < until))
    window = keys if limit is None else keys[-(limit * multiple):]
    native_bars, closed_in_window = [], 0
    for ts in window:
        bar = native_bar(fp, ts, ppr)
        if ts != open_ts:
            closed_in_window += bar["delta"]
        native_bars.append(bar)
    bars = group_native_bars(native_bars, interval_sec, native_sec)
    if limit is not None:
        bars = bars[-limit:]
    return {"row_size": row_size, "interval_sec": interval_sec,
            "cvd_offset": closed_cvd - closed_in_window, "bars": bars}
