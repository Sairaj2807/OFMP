"""Day structure read from one session's profile (owner decisions 2026-10-21):
shape, double distribution, open drive, the Value Area rule (80% rule) and
volume clusters. Every rule is explicit and its thresholds named, so a label
can always be traced back to the numbers (and tuned later).

Inputs are the lists build_profile already has, low to high: TPO counts,
volumes, buy and sell, the period letters touching each row, the periods'
open / high / low / close, and the previous session's levels.

- Shape (TPO distribution; once SHAPE_MIN_PERIODS periods have traded):
    D      the TPO POC in the middle band (35-65% of the range) and the TPO value
           area centred on the range: balanced, rotational;
    p      the TPO POC in the upper part (> 60%) and the lower 40% of the range
           holding at most 25% of the TPOs: a thin lower tail, short covering;
    b      the mirror image: long liquidation;
    trend  none of the above, value area at most 40% of the range and the range at
           least twice the initial balance: an elongated, one-sided day;
    otherwise no label.
- Double distribution: the two highest TPO peaks (local maxima within two rows,
  each at least half the largest count) at least three rows apart, with a thin
  zone between them: at least two adjacent rows at or below 30% of the smaller
  peak. Each side's own 70% value area is reported.
- Open drive: the first period opens within 10% of its range from its low (up)
  or high (down), never trades more than 10% of that range back through the
  open, and the second period extends beyond the first period's extreme in the
  same direction without trading back to the open.
- Value Area rule: the session opens outside the previous session's volume
  value area and two consecutive periods close inside it; the target is the far
  edge (opened above -> VAL, below -> VAH), met once a period from the
  activating one on reaches it.
- Volume clusters: runs of at least two adjacent rows, each with at least 60%
  of the VPOC row's volume, touched by at least three periods; "absorption"
  when the run's buy - sell is at most 10% of its volume (heavy two-sided
  trade with little net direction).
"""
from typing import Optional

from .analytics import value_area

SHAPE_MIN_PERIODS = 4
DRIVE_SLACK = 0.10
CLUSTER_PCT_OF_POC = 0.60
CLUSTER_MIN_ROWS = 2
CLUSTER_MIN_PERIODS = 3
ABSORPTION_DELTA = 0.10


def shape(tpo: list, poc: Optional[int], va: Optional[tuple], ib_range: Optional[float], row_size: float,
          periods: int) -> Optional[str]:
    n = len(tpo)
    total = sum(tpo)
    if periods < SHAPE_MIN_PERIODS or poc is None or va is None or n < 5 or total <= 0:
        return None
    pos = (poc + 0.5) / n
    va_mid = ((va[0] + va[1] + 1) / 2) / n
    va_width = (va[1] - va[0] + 1) / n
    cut = max(1, int(round(n * 0.4)))
    lower, upper = sum(tpo[:cut]) / total, sum(tpo[n - cut:]) / total
    if 0.35 <= pos <= 0.65 and 0.35 <= va_mid <= 0.65:
        return "D"
    if pos > 0.6 and lower <= 0.25:
        return "p"
    if pos < 0.4 and upper <= 0.25:
        return "b"
    if va_width <= 0.4 and ib_range and n * row_size >= 2 * ib_range:
        return "trend"
    return None


def double_distribution(tpo: list) -> Optional[dict]:
    """{"lower": (lo, hi), "upper": (lo, hi), "thin": (lo, hi)} as row indexes, or None."""
    n = len(tpo)
    if n < 7 or max(tpo) <= 0:
        return None
    top = max(tpo)
    peaks = [i for i in range(n) if tpo[i] >= top * 0.5 and tpo[i] == max(tpo[max(0, i - 2): i + 3])]
    peaks.sort(key=lambda i: (-tpo[i], i))
    for a in peaks:
        for b in peaks:
            lo_peak, hi_peak = min(a, b), max(a, b)
            if hi_peak - lo_peak < 3:
                continue
            floor = min(tpo[a], tpo[b]) * 0.3
            run, best = [], []
            for i in range(lo_peak + 1, hi_peak):
                run = run + [i] if tpo[i] <= floor else []
                if len(run) > len(best):
                    best = run
            if len(best) >= 2:
                lower = value_area(tpo[:best[0]], max(range(best[0]), key=lambda k: (tpo[k], -k)))
                upper_seg = tpo[best[-1] + 1:]
                up_poc = max(range(len(upper_seg)), key=lambda k: (upper_seg[k], -k))
                ua = value_area(upper_seg, up_poc)
                return {"lower": lower, "upper": (ua[0] + best[-1] + 1, ua[1] + best[-1] + 1),
                        "thin": (best[0], best[-1])}
    return None


def open_drive(periods: list) -> Optional[str]:
    """periods: [(index, open, high, low, close)] in order (prices)."""
    by = {p[0]: p for p in periods}
    if 0 not in by or 1 not in by:
        return None
    _, o, h, lo, _c = by[0]
    _, _o2, h2, l2, _c2 = by[1]
    r = h - lo
    if r <= 0:
        return None
    if o - lo <= DRIVE_SLACK * r and h2 > h and l2 > o:
        return "up"
    if h - o <= DRIVE_SLACK * r and l2 < lo and h2 < o:
        return "down"
    return None


def value_area_rule(open_price: float, periods: list, prev_vah: Optional[float], prev_val: Optional[float],
                    row_size: float) -> Optional[dict]:
    """The 80% rule against the previous session's volume value area (rows [val, vah + row))."""
    if prev_vah is None or prev_val is None:
        return None
    top = prev_vah + row_size                                  # the VAH row's upper edge
    if prev_val <= open_price < top:
        return {"opened": "inside", "active": False, "target": None, "met": False, "activated": None,
                "vah": prev_vah, "val": prev_val}
    above = open_price >= top
    target = prev_val if above else prev_vah
    inside = [prev_val <= c < top for (_i, _o, _h, _l, c) in periods]
    start = next((k for k in range(1, len(periods)) if inside[k] and inside[k - 1]), None)
    met = start is not None and any((l <= prev_val) if above else (h >= prev_vah)
                                    for (_i, _o, h, l, _c) in periods[start - 1:])
    return {"opened": "above" if above else "below", "active": start is not None, "target": target, "met": met,
            "activated": periods[start][0] if start is not None else None, "vah": prev_vah, "val": prev_val}


def volume_clusters(vol: list, buy: list, sell: list, row_periods: list, poc: Optional[int]) -> list:
    """[(lo, hi, volume, delta, periods, absorption)] as row indexes."""
    if poc is None or vol[poc] <= 0:
        return []
    floor = vol[poc] * CLUSTER_PCT_OF_POC
    out, start = [], None
    for i, v in enumerate(vol + [-1]):
        if v >= floor and v > 0:
            start = i if start is None else start
            continue
        if start is not None:
            lo, hi = start, i - 1
            touched = set().union(*(set(row_periods[k]) for k in range(lo, hi + 1)))
            if hi - lo + 1 >= CLUSTER_MIN_ROWS and len(touched) >= CLUSTER_MIN_PERIODS:
                volume = sum(vol[lo:hi + 1])
                delta = sum(buy[lo:hi + 1]) - sum(sell[lo:hi + 1])
                out.append((lo, hi, volume, delta, len(touched), abs(delta) <= ABSORPTION_DELTA * volume))
            start = None
    return out
