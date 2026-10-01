"""Per-candle footprint table: POC, value area, delta, CVD and rows per
display candle, plus the latest candle's stacked imbalances.

This was the legacy dashboard's payload (server._candles_payload). The page
is gone, but the numbers are the platform's definition of POC / value area /
stacked imbalances per display candle (the alert rules use the same
analytics), so the golden regression test keeps pinning them through this
function.

`interval_sec` groups native candles at the timestamp level
(group_candle_timestamps), so POC / value area / imbalances are computed on
the merged rows of each group (Footprint.merged_candle_rows), exactly as for
a single native candle."""
from .analytics import poc_from_rows, stacked_imbalances_from_rows, value_area_from_rows
from .bars import group_candle_timestamps
from .settings import IMBALANCE_THRESHOLD, MIN_STACK, NATIVE_CANDLE_SEC, VALUE_AREA_PCT


def candle_table(fp, cvd_by_candle: dict, ppr: int, interval_sec: int = NATIVE_CANDLE_SEC, limit: int = 30,
                 native_sec: int = NATIVE_CANDLE_SEC, value_area_pct: float = VALUE_AREA_PCT,
                 imbalance_threshold: float = IMBALANCE_THRESHOLD, min_stack: int = MIN_STACK) -> dict:
    candle_ts_sorted = sorted(fp.data.keys())
    multiple = max(1, interval_sec // native_sec)
    native_window = candle_ts_sorted[-(limit * multiple):]
    groups = group_candle_timestamps(native_window, interval_sec, native_sec)[-limit:]

    candles = []
    for group in groups:
        rows = fp.merged_candle_rows(group, ppr)
        poc = poc_from_rows(rows)
        va = value_area_from_rows(rows, poc, value_area_pct)
        open_ohlc = fp.candle_ohlc.get(group[0])
        close_ohlc = fp.candle_ohlc.get(group[-1])
        # CVD "as of" a (possibly still-forming) group is that of its last
        # CLOSED native candle: cvd_by_candle only has entries for closed ones.
        cvd = next((cvd_by_candle[t] for t in reversed(group) if t in cvd_by_candle), None)
        candles.append({
            "ts": group[0],
            "poc": poc,
            "value_area": list(va) if va else None,
            "delta": sum(fp.candle_delta(t) for t in group),
            "cvd": cvd,
            "bullish": (close_ohlc["close"] >= open_ohlc["open"]) if (open_ohlc and close_ohlc) else None,
            "close_row": fp.row_price_for(close_ohlc["close"], ppr) if close_ohlc else None,
            "rows": [{"price": row.price, "buy": row.buy_volume, "sell": row.sell_volume, "delta": row.delta}
                     for row in rows],
        })

    imbalances = (stacked_imbalances_from_rows(fp.merged_candle_rows(groups[-1], ppr), imbalance_threshold, min_stack)
                  if groups else [])
    return {
        "interval_sec": interval_sec,
        "candles": candles,
        "imbalances": [{"kind": stack[0][0], "prices": [p for _, p in stack]} for stack in imbalances],
    }
