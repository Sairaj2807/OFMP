"""Display-interval grouping of native candles. The engine buckets trades at
the native interval only; coarser candles are assembled here from
already-aggregated native bars, outside the hot tick path."""
from .settings import NATIVE_CANDLE_SEC


def _has_ohlc(bar: dict) -> bool:
    return all(bar.get(k) is not None for k in ("open", "high", "low", "close"))


def _validate_display_interval(interval_sec: int, native_sec: int) -> None:
    if interval_sec <= 0 or interval_sec % native_sec != 0:
        raise ValueError(f"interval_sec must be a positive multiple of {native_sec}, got {interval_sec}")


def group_native_bars(bars: list, interval_sec: int, native_sec: int = NATIVE_CANDLE_SEC) -> list:
    """Aggregates a time-ordered list of NATIVE-interval candle-bar dicts (the
    shape server._chart_payload builds — time/open/high/low/close/delta/
    min_delta/max_delta/trades/cells) into `interval_sec`-wide bars.

    This is how the order-flow chart offers 3/5/15/30-minute views without the
    engine ever bucketing trades at more than one granularity: the hot tick
    path (Footprint.add_trade) always buckets at `native_sec` (see
    NATIVE_CANDLE_SEC), and coarser candles are assembled here, purely
    from already-aggregated native bars, at payload-serialization time.

    `interval_sec` must be a positive integer multiple of `native_sec`.
    `interval_sec == native_sec` returns `bars` unchanged (same list, by value).

    `min_delta`/`max_delta` are chained, not just min/maxed independently: each
    native bar's own min/max already describe its running-delta path zeroed at
    that bar's start (Footprint.candle_stats). Because the native bars stay in
    time order within a group, the merged path's extremes are
    `offset_before_i + sub.min_delta` / `offset_before_i + sub.max_delta`, where
    `offset_before_i` is the cumulative sum of every earlier sub-bar's own final
    delta — exact, without re-reading individual trades.

    A field missing (None) on ANY sub-bar in a group makes that field absent on
    the merged bar too, rather than fabricating a value from partial data —
    matching how the chart library treats missing metadata elsewhere."""
    if interval_sec == native_sec:
        return list(bars)
    _validate_display_interval(interval_sec, native_sec)

    groups: dict = {}     # bucket key -> list of native bars, insertion order preserved
    for bar in bars:
        key = bar["time"] - (bar["time"] % interval_sec)
        groups.setdefault(key, []).append(bar)

    out = []
    for key in sorted(groups):
        subs = groups[key]  # already time-ordered: `bars` is, and grouping preserves order
        cells: dict = {}
        for sub in subs:
            for cell in sub["cells"]:
                c = cells.setdefault(cell["price"], {"price": cell["price"], "buy": 0, "sell": 0})
                c["buy"] += cell["buy"]
                c["sell"] += cell["sell"]

        merged = {
            "time": key,
            "delta": sum(sub["delta"] for sub in subs),
            "cells": [cells[p] for p in sorted(cells, reverse=True)],
        }

        if all(_has_ohlc(sub) for sub in subs):
            merged["open"] = subs[0]["open"]
            merged["close"] = subs[-1]["close"]
            merged["high"] = max(sub["high"] for sub in subs)
            merged["low"] = min(sub["low"] for sub in subs)

        if all(sub.get("trades") is not None for sub in subs):
            merged["trades"] = sum(sub["trades"] for sub in subs)

        if all(sub.get("min_delta") is not None and sub.get("max_delta") is not None for sub in subs):
            offset, lo, hi = 0, float("inf"), float("-inf")
            for sub in subs:
                lo = min(lo, offset + sub["min_delta"])
                hi = max(hi, offset + sub["max_delta"])
                offset += sub["delta"]
            merged["min_delta"], merged["max_delta"] = lo, hi

        out.append(merged)
    return out


def group_candle_timestamps(ts_list: list, interval_sec: int,
                            native_sec: int = NATIVE_CANDLE_SEC) -> list:
    """Buckets a time-ordered list of native candle timestamps into groups
    sharing one `interval_sec`-wide display bucket, preserving order.

    Companion to group_native_bars, which groups already-SERIALIZED native
    bar dicts (the chart payload's shape). This instead groups the plain
    timestamps themselves, for a caller (table.candle_table, the per-candle
    table) that
    computes each display candle's POC/value-area/imbalances on demand from
    the underlying Footprint (Footprint.merged_candle_rows) rather than from
    pre-built bar dicts — those need the real candle_ts values to look up,
    not a merged dict that has already thrown them away.

    `interval_sec == native_sec` returns one singleton group per timestamp
    (matching group_native_bars' native-interval no-op). Otherwise
    `interval_sec` must be a positive integer multiple of `native_sec`."""
    if interval_sec == native_sec:
        return [[ts] for ts in ts_list]
    _validate_display_interval(interval_sec, native_sec)

    groups: dict = {}
    for ts in ts_list:
        key = ts - (ts % interval_sec)
        groups.setdefault(key, []).append(ts)
    return [groups[key] for key in sorted(groups)]
