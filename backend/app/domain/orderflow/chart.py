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

Footprint table fields, per display bar (owner decisions 2026-10-13):
- `volume`: buy + sell; `cot`: buy - sell (who was more aggressive in the bar);
- `vpoc` / `vpoc_volume`: the row (band floor, at this row size) with the most
  volume and that volume; a tie goes to the HIGHER price, the chart library's
  own POC rule, so the pink POC line and the table always agree;
- `oi`: the closing open interest (the bar's last native candle that has one);
- `doi`: `oi` minus the OI before the bar: the previous candle's closing OI, or
  for the session's first candle `oi_before` (the previous session's close).
  Unknown (None) whenever either side is unknown; never guessed.
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


def table_fields(bar: dict) -> dict:
    """volume / cot / vpoc / vpoc_volume of one display bar, from its cells."""
    volume, vpoc, vpoc_volume = 0, None, -1
    for c in bar["cells"]:
        v = c["buy"] + c["sell"]
        volume += v
        if v > vpoc_volume or (v == vpoc_volume and c["price"] > vpoc):
            vpoc, vpoc_volume = c["price"], v
    return {"volume": volume, "cot": bar["delta"], "vpoc": vpoc, "vpoc_volume": vpoc_volume if vpoc is not None else None}


def add_open_interest(bars: list, fp, interval_sec: int, oi_before: Optional[int]) -> None:
    """Sets each display bar's `oi` (closing) and `doi` in place. The OI before a bar is the
    closing OI of the latest earlier native candle the engine holds (inside the payload's
    window or not), else `oi_before`."""
    known = sorted(fp.candle_oi.items())
    i, prev = 0, oi_before
    for bar in bars:
        start, end = bar["time"], bar["time"] + interval_sec
        while i < len(known) and known[i][0] < start:
            prev = known[i][1]
            i += 1
        oi = None
        while i < len(known) and known[i][0] < end:
            oi = known[i][1]
            i += 1
        bar["oi"] = oi
        bar["doi"] = oi - prev if oi is not None and prev is not None else None
        if oi is not None:
            prev = oi


def chart_payload(fp, closed_cvd: int, open_ts: Optional[int], ppr: int, interval_sec: int = NATIVE_CANDLE_SEC,
                  limit: Optional[int] = 60, since: Optional[int] = None, until: Optional[int] = None,
                  native_sec: int = NATIVE_CANDLE_SEC, oi_before: Optional[int] = None) -> dict:
    """The last `limit` display bars (all, when limit is None) of the native candles in
    [since, until) (epoch seconds; unbounded when None). `oi_before`: the previous session's
    closing OI, for the session's first candle's dOI."""
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
    bars = [{**b, **table_fields(b)} for b in bars]
    add_open_interest(bars, fp, interval_sec, oi_before)
    return {"row_size": row_size, "interval_sec": interval_sec,
            "cvd_offset": closed_cvd - closed_in_window, "bars": bars}
