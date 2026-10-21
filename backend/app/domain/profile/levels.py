"""Market profile levels across sessions (owner decisions 2026-10-21), from the
finished sessions' own profiles (analytics.build_profile output), oldest first.

- Naked (virgin) POC: a session's volume POC row that no LATER finished
  session has traded into (a later session's low-high range overlapping the
  row [poc, poc + row size)). The chart also hides one today's live range has
  reached. `sessions_ago` counts the finished sessions after it, plus today.
- Composite profile: several sessions merged into one profile at the same row
  size: per row, TPOs and buy / sell volume summed; then the usual POC (TPO and
  volume), 70% value areas, VWAP (volume-weighted across the days' VWAPs),
  TWAP (TPO median) and HVN bands, with the same rules as a single session.
"""
from typing import Optional

from .analytics import VALUE_AREA_PCT, hvn_bands, poc_index, tpo_median, value_area

LOOKBACK_SESSIONS = 20
COMPOSITE_WINDOWS = ("5d", "10d", "20d", "week", "month")


def _r(x: float) -> float:
    return round(x, 8)


def naked_pocs(days: list, row_size: float) -> list:
    """[{date, price, sessions_ago}] for `days` = [(iso date, profile)] oldest first."""
    out = []
    for k, (day, p) in enumerate(days):
        poc = p.get("poc_volume")
        if poc is None:
            continue
        later = [q["stats"] for _, q in days[k + 1:] if q.get("stats")]
        touched = any(s["low"] < poc + row_size - 1e-9 and s["high"] >= poc - 1e-9 for s in later)
        if not touched:
            out.append({"date": day, "price": poc, "sessions_ago": len(days) - k})
    return out


def composite_profile(days: list, row_size: float, label: str, va_pct: float = VALUE_AREA_PCT) -> Optional[dict]:
    """One profile merged from `days` = [(iso date, profile)] (None without any traded day)."""
    days = [(d, p) for d, p in days if p and p.get("rows")]
    if not days:
        return None
    acc: dict = {}
    for _d, p in days:
        for r in p["rows"]:
            key = round(r["price"] / row_size)
            a = acc.setdefault(key, [0, 0, 0])
            a[0] += r["tpo"]
            a[1] += r["buy"]
            a[2] += r["sell"]
    lo, hi = min(acc), max(acc)
    keys = list(range(lo, hi + 1))                              # contiguous ladder, low to high
    tpo = [acc.get(k, [0, 0, 0])[0] for k in keys]
    buy = [acc.get(k, [0, 0, 0])[1] for k in keys]
    sell = [acc.get(k, [0, 0, 0])[2] for k in keys]
    vol = [b + s for b, s in zip(buy, sell)]
    price = [_r(k * row_size) for k in keys]
    p_tpo, p_vol = poc_index(tpo), poc_index(vol)
    va_t, va_v = value_area(tpo, p_tpo, va_pct), value_area(vol, p_vol, va_pct)
    p_twap = tpo_median(tpo)
    traded = sum(p["stats"]["volume"] for _d, p in days if p.get("vwap") is not None)
    vwap = _r(sum(p["vwap"] * p["stats"]["volume"] for _d, p in days if p.get("vwap") is not None) / traded) \
        if traded else None
    bands = hvn_bands(vol, p_vol)
    return {
        "date": None,
        "composite": {"label": label, "days": len(days), "from": days[0][0], "to": days[-1][0],
                      "dates": [d for d, _p in days]},
        "row_size": row_size,
        "rows": [{"price": price[i], "letters": "", "tpo": tpo[i], "buy": buy[i], "sell": sell[i],
                  "volume": vol[i], "delta": buy[i] - sell[i]} for i in reversed(range(len(keys)))],
        "poc_tpo": price[p_tpo] if p_tpo is not None else None,
        "poc_volume": price[p_vol] if p_vol is not None else None,
        "value_area_tpo": {"low": price[va_t[0]], "high": price[va_t[1]]} if va_t else None,
        "value_area_volume": {"low": price[va_v[0]], "high": price[va_v[1]]} if va_v else None,
        "value_area_pct": va_pct,
        "vwap": vwap,
        "twap": price[p_twap] if p_twap is not None else None,
        "hvn": [price[peak] for _lo, _hi, peak in bands],
        "hvn_bands": [{"low": price[a], "high": price[b], "peak": price[c]} for a, b, c in bands],
        "stats": {"open": days[0][1]["stats"]["open"], "close": days[-1][1]["stats"]["close"],
                  "high": max(p["stats"]["high"] for _d, p in days), "low": min(p["stats"]["low"] for _d, p in days),
                  "tpo_total": sum(tpo), "volume": sum(vol), "delta": sum(buy) - sum(sell),
                  "trades": sum(p["stats"]["trades"] for _d, p in days)},
    }
