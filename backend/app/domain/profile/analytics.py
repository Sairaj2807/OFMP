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
- HVN (v2, owner decision 2026-10-21: "volumes almost similar to VPOC"): a
  run of adjacent rows each with at least 70% of the VPOC row's volume that
  does not contain the VPOC row; reported as a band and its peak row.
- LVN (v1): rows that are the local volume minimum within two rows either side,
  with at most 0.5x the mean row volume, strictly inside the range (not the
  first or last two rows).
- VWAP: sum(price x qty) / sum(qty) over the session's trades, exact (volume is
  kept per tick). TWAP: the TPO median, the row where the TPOs at or below it
  first reach half of all TPOs (time spent above and below is equal).
- IB extensions: IBH + 0.5 / 1 / 2 x the IB range (1.5x / 2x / 3x) and the same
  below IBL, once the IB is final; `reached` is the largest multiple traded.
- Developing POC: the volume POC of the session up to the end of each period
  (the POC tie rule); the last entry is the current DPOC.
- Weak high / low: the top / bottom row has two or more TPOs and no tail there
  (flat edge, no rejection).
"""
from typing import Optional

from .engine import SessionProfile, letter

VALUE_AREA_PCT = 0.70
NODE_WINDOW = 2
HVN_PCT_OF_POC = 0.70
LVN_FACTOR = 0.5
IB_MULTIPLES = (1.5, 2.0, 3.0)


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


def hvn_bands(volumes: list, poc: Optional[int], pct: float = HVN_PCT_OF_POC) -> list:
    """[(low index, high index, peak index)] per the v2 rule above."""
    if poc is None or volumes[poc] <= 0:
        return []
    floor = volumes[poc] * pct
    bands, start = [], None
    for i, v in enumerate(volumes + [-1]):                     # sentinel closes the last run
        if v >= floor and v > 0:
            start = i if start is None else start
            continue
        if start is not None:
            lo, hi = start, i - 1
            if not lo <= poc <= hi:
                peak = max(range(lo, hi + 1), key=lambda k: (volumes[k], -k))
                bands.append((lo, hi, peak))
            start = None
    return bands


def low_volume_nodes(volumes: list) -> list:
    """LVN indexes per the v1 rule above."""
    traded = [v for v in volumes if v > 0]
    if len(volumes) < 2 * NODE_WINDOW + 1 or not traded:
        return []
    mean = sum(traded) / len(traded)
    return [i for i, v in enumerate(volumes)
            if NODE_WINDOW <= i < len(volumes) - NODE_WINDOW
            and v == min(volumes[i - NODE_WINDOW: i + NODE_WINDOW + 1]) and v <= LVN_FACTOR * mean]


def tpo_median(tpo: list) -> Optional[int]:
    """Index of the row where the cumulative TPOs from the bottom first reach half of the total."""
    total = sum(tpo)
    if total <= 0:
        return None
    run = 0
    for i, t in enumerate(tpo):
        run += t
        if run * 2 >= total:
            return i
    return len(tpo) - 1


def developing_poc(profile: SessionProfile, row: int, lo_row: int, n: int) -> list:
    """[(period, row index of the volume POC up to the end of that period)]."""
    cum, out = [0] * n, []
    for period in sorted(profile.period_volume):
        for t, q in profile.period_volume[period].items():
            cum[t // row - lo_row] += q
        idx = poc_index(cum)
        if idx is not None:
            out.append((period, idx))
    return out


def _levels(profile: SessionProfile, row_points: float) -> Optional[dict]:
    """POCs and value areas only: what a previous session contributes as reference lines."""
    full = build_profile(profile, row_points, include_previous=False)
    if full is None:
        return None
    va_v = full["value_area_volume"]
    return {"date": full["date"], "poc_tpo": full["poc_tpo"], "poc_volume": full["poc_volume"],
            "vah": full["value_area_tpo"]["high"] if full["value_area_tpo"] else None,
            "val": full["value_area_tpo"]["low"] if full["value_area_tpo"] else None,
            "vah_volume": va_v["high"] if va_v else None, "val_volume": va_v["low"] if va_v else None,
            "vwap": full["vwap"], "high": full["stats"]["high"], "low": full["stats"]["low"]}


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
    bands, lvn = hvn_bands(vol, p_vol), low_volume_nodes(vol)
    runs = single_print_runs(rows, in_progress)
    p_twap = tpo_median(tpo)
    traded = sum(vol)
    vwap = _round(sum(t * ts * (b + s) for t, (b, s) in profile.volume.items()) / traded) if traded else None
    trail = developing_poc(profile, row, lo_row, n)

    session = profile.session
    ib = [profile.periods[i] for i in range(session.ib_periods) if i in profile.periods]
    ib_high = _round(max(p.high for p in ib) * ts) if ib else None
    ib_low = _round(min(p.low for p in ib) * ts) if ib else None
    ib_final = bool(ib) and in_progress is not None and in_progress >= session.ib_periods
    high, low = _round(profile.high_tick * ts), _round(profile.low_tick * ts)
    extensions = None
    if ib_final and ib_high > ib_low:
        rng = ib_high - ib_low
        up = [{"multiple": m, "price": _round(ib_high + (m - 1) * rng)} for m in IB_MULTIPLES]
        down = [{"multiple": m, "price": _round(ib_low - (m - 1) * rng)} for m in IB_MULTIPLES]
        extensions = {"up": up, "down": down,
                      "reached_up": max((e["multiple"] for e in up if high >= e["price"]), default=None),
                      "reached_down": max((e["multiple"] for e in down if low <= e["price"]), default=None)}
    tail_top = any(r["hi"] == n - 1 for r in runs if r["lo"] == 0 or r["hi"] == n - 1)
    tail_bottom = any(r["lo"] == 0 for r in runs if r["lo"] == 0 or r["hi"] == n - 1)

    result = {
        "date": profile.date,
        "row_size": _round(row * ts),
        "tick_size": ts,
        "period_minutes": session.period_minutes,
        "periods": [{"index": i, "letter": letter(i), "start_ms": session.period_start_ms(profile.date, i),
                     "high": _round(profile.periods[i].high * ts), "low": _round(profile.periods[i].low * ts),
                     "open": _round(profile.periods[i].open * ts), "close": _round(profile.periods[i].close * ts)}
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
        "hvn": [price[peak] for _lo, _hi, peak in bands],
        "hvn_bands": [{"low": price[lo], "high": price[hi], "peak": price[peak]} for lo, hi, peak in bands],
        "lvn": [price[i] for i in lvn],
        "vwap": vwap,
        "twap": price[p_twap] if p_twap is not None else None,
        "ib_extensions": extensions,
        "dpoc": {"price": price[trail[-1][1]] if trail else None,
                 "trail": [{"period": k, "letter": letter(k), "price": price[i]} for k, i in trail]},
        "weak_high": n > 1 and tpo[n - 1] >= 2 and not tail_top,
        "weak_low": n > 1 and tpo[0] >= 2 and not tail_bottom,
        "stats": {
            "open": _round(profile.open_tick * ts), "high": high, "low": low,
            "close": _round(profile.close_tick * ts),
            "tpo_total": sum(tpo), "volume": sum(vol), "delta": sum(buy) - sum(sell), "trades": profile.trades,
        },
    }
    if include_previous and profile.previous is not None:
        result["previous"] = _levels(profile.previous, row_points)
    return result
