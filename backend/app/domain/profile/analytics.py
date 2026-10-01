"""Market Profile calculations on a SessionProfile, at a chosen row size.

All rules are explicit so results are repeatable (backtests, replay):

- Rows cover the session's whole range (low to high), including any row no
  period touched (a gap), so the profile is a contiguous ladder.
- TPO count of a row = number of periods whose high-low range overlaps it.
- POC (TPO and volume, computed separately): the row with the largest count /
  volume; ties go to the row closest to the middle of the session's range,
  then to the lower row.
- Value area (70% by default, on TPOs and on volume separately): start at the
  POC and repeatedly add the single neighbouring row (above or below) with
  more weight, the row below on a tie, until the covered weight reaches the
  target. The same rule as the footprint's value area
  (orderflow/analytics.value_area_from_rows).
- Initial balance: high / low of the first `ib_periods` periods (the first
  hour). Final once a later period has traded. Extensions are measured from it.
- Single prints: runs of adjacent rows with exactly one TPO, of the same
  period, excluding the period still in progress (its rows are trivially
  single until the next period starts). A run that reaches the top or bottom
  row of the profile is a tail (selling tail at the top, buying tail at the
  bottom), reported separately; single prints are the interior runs.
- HVN / LVN (v1): rows that are the local volume maximum / minimum within two
  rows either side, with volume at least 1.5x / at most 0.5x the mean row
  volume; LVNs only strictly inside the range (not the first or last two rows).
"""
from typing import Optional

from .engine import SessionProfile, letter

VALUE_AREA_PCT = 0.70
NODE_WINDOW = 2
HVN_FACTOR = 1.5
LVN_FACTOR = 0.5


def _round(x: float) -> float:
    return round(x, 8)


def row_ticks(profile: SessionProfile, row_points: float) -> int:
    return max(1, int(round(row_points / profile.tick_size)))


def poc_index(weights: list) -> Optional[int]:
    """Index of the largest weight; ties: closest to the middle, then the lower index."""
    if not weights or max(weights) <= 0:
        return None
    top = max(weights)
    mid = (len(weights) - 1) / 2
    return min((i for i, w in enumerate(weights) if w == top), key=lambda i: (abs(i - mid), i))


def value_area(weights: list, poc: Optional[int], pct: float = VALUE_AREA_PCT) -> Optional[tuple]:
    """(low index, high index) covering `pct` of the total weight, grown from `poc`."""
    if poc is None:
        return None
    target = sum(weights) * pct
    lo = hi = poc
    covered = weights[poc]
    while covered < target and (lo > 0 or hi < len(weights) - 1):
        below = weights[lo - 1] if lo > 0 else -1
        above = weights[hi + 1] if hi < len(weights) - 1 else -1
        if below >= above:
            lo -= 1
            covered += below
        else:
            hi += 1
            covered += above
    return lo, hi


def single_print_runs(rows: list, in_progress: Optional[int]) -> list:
    """Runs of adjacent single-TPO rows of the same (finished) period. `rows` low to high."""
    runs, cur = [], None
    for i, r in enumerate(rows):
        single = len(r["periods"]) == 1 and r["periods"][0] != in_progress
        key = r["periods"][0] if single else None
        if single and cur is not None and cur["period"] == key and cur["hi"] == i - 1:
            cur["hi"] = i
            continue
        if cur is not None:
            runs.append(cur)
        cur = {"period": key, "lo": i, "hi": i} if single else None
    if cur is not None:
        runs.append(cur)
    return runs


def volume_nodes(volumes: list) -> tuple:
    """(HVN indexes, LVN indexes) per the v1 rule above."""
    traded = [v for v in volumes if v > 0]
    if len(volumes) < 2 * NODE_WINDOW + 1 or not traded:
        return [], []
    mean = sum(traded) / len(traded)
    hvn, lvn = [], []
    for i, v in enumerate(volumes):
        window = volumes[max(0, i - NODE_WINDOW): i + NODE_WINDOW + 1]
        if v == max(window) and v >= HVN_FACTOR * mean and v > 0:
            hvn.append(i)
        inside = NODE_WINDOW <= i < len(volumes) - NODE_WINDOW
        if inside and v == min(window) and v <= LVN_FACTOR * mean:
            lvn.append(i)
    return hvn, lvn


def _levels(profile: SessionProfile, row_points: float) -> Optional[dict]:
    """POCs and value areas only: what a previous session contributes as reference lines."""
    full = build_profile(profile, row_points, include_previous=False)
    if full is None:
        return None
    return {"date": full["date"], "poc_tpo": full["poc_tpo"], "poc_volume": full["poc_volume"],
            "vah": full["value_area_tpo"]["high"] if full["value_area_tpo"] else None,
            "val": full["value_area_tpo"]["low"] if full["value_area_tpo"] else None,
            "high": full["stats"]["high"], "low": full["stats"]["low"]}


def build_profile(profile: SessionProfile, row_points: float = 5.0, va_pct: float = VALUE_AREA_PCT,
                  include_previous: bool = True) -> Optional[dict]:
    """The profile as JSON-ready data. None before the session's first trade."""
    if not profile.periods:
        prev = _levels(profile.previous, row_points) if include_previous and profile.previous else None
        return None if prev is None else {"date": None, "rows": [], "previous": prev, "empty": True}
    ts, row = profile.tick_size, row_ticks(profile, row_points)
    lo_row, hi_row = profile.low_tick // row, profile.high_tick // row
    n = hi_row - lo_row + 1
    in_progress = profile.current_period

    periods = [[] for _ in range(n)]
    for idx in sorted(profile.periods):
        p = profile.periods[idx]
        for r in range(p.low // row, p.high // row + 1):
            periods[r - lo_row].append(idx)
    buy, sell = [0] * n, [0] * n
    for t, (b, s) in profile.volume.items():
        buy[t // row - lo_row] += b
        sell[t // row - lo_row] += s

    tpo = [len(x) for x in periods]
    vol = [b + s for b, s in zip(buy, sell)]
    price = [_round((lo_row + i) * row * ts) for i in range(n)]
    rows = [{"price": price[i], "periods": periods[i], "tpo": tpo[i], "buy": buy[i], "sell": sell[i]}
            for i in range(n)]

    p_tpo, p_vol = poc_index(tpo), poc_index(vol)
    va_t, va_v = value_area(tpo, p_tpo, va_pct), value_area(vol, p_vol, va_pct)
    hvn, lvn = volume_nodes(vol)
    runs = single_print_runs(rows, in_progress)

    session = profile.session
    ib = [profile.periods[i] for i in range(session.ib_periods) if i in profile.periods]
    ib_high = _round(max(p.high for p in ib) * ts) if ib else None
    ib_low = _round(min(p.low for p in ib) * ts) if ib else None
    ib_final = bool(ib) and in_progress is not None and in_progress >= session.ib_periods
    high, low = _round(profile.high_tick * ts), _round(profile.low_tick * ts)

    result = {
        "date": profile.date,
        "row_size": _round(row * ts),
        "tick_size": ts,
        "period_minutes": session.period_minutes,
        "periods": [{"index": i, "letter": letter(i), "start_ms": session.period_start_ms(profile.date, i),
                     "high": _round(profile.periods[i].high * ts), "low": _round(profile.periods[i].low * ts)}
                    for i in sorted(profile.periods)],
        "current_period": in_progress,
        "rows": [{"price": r["price"], "letters": "".join(letter(i) for i in r["periods"]), "tpo": r["tpo"],
                  "buy": r["buy"], "sell": r["sell"], "volume": r["buy"] + r["sell"],
                  "delta": r["buy"] - r["sell"]} for r in reversed(rows)],        # high to low, like a ladder
        "poc_tpo": price[p_tpo] if p_tpo is not None else None,
        "poc_volume": price[p_vol] if p_vol is not None else None,
        "value_area_tpo": {"low": price[va_t[0]], "high": price[va_t[1]]} if va_t else None,
        "value_area_volume": {"low": price[va_v[0]], "high": price[va_v[1]]} if va_v else None,
        "value_area_pct": va_pct,
        "initial_balance": {
            "high": ib_high, "low": ib_low, "final": ib_final,
            "range": _round(ib_high - ib_low) if ib else None,
            "extension_up": _round(max(0.0, high - ib_high)) if ib_final else None,
            "extension_down": _round(max(0.0, ib_low - low)) if ib_final else None,
        },
        "single_prints": [{"low": price[r["lo"]], "high": price[r["hi"]], "letter": letter(r["period"])}
                          for r in runs if 0 < r["lo"] and r["hi"] < n - 1],
        "tails": [{"low": price[r["lo"]], "high": price[r["hi"]], "letter": letter(r["period"]),
                   "kind": "selling" if r["hi"] == n - 1 else "buying"}
                  for r in runs if r["lo"] == 0 or r["hi"] == n - 1],
        "hvn": [price[i] for i in hvn],
        "lvn": [price[i] for i in lvn],
        "stats": {
            "open": _round(profile.open_tick * ts), "high": high, "low": low,
            "close": _round(profile.close_tick * ts),
            "tpo_total": sum(tpo), "volume": sum(vol), "delta": sum(buy) - sum(sell), "trades": profile.trades,
        },
    }
    if include_previous and profile.previous is not None:
        result["previous"] = _levels(profile.previous, row_points)
    return result
