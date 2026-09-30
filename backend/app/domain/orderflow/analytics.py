"""Per-candle footprint analytics over FootprintRow lists: POC, value area,
stacked imbalances. Pure functions — a single native candle and a merged
group of candles go through the same code.

Stacked imbalance (v1) is the engine's own rule and the authoritative
definition for the platform (owner decision 2026-09-30). It is not
validated against Vtrender: a BUY imbalance compares buy[P] with
sell[P - row] (the row below), a SELL imbalance compares sell[P] with
buy[P - row]; a zero opposing row never flags; flagged rows of the same
kind join into one stack even when unflagged rows sit between them."""
from typing import Optional


def poc_from_rows(rows: list) -> Optional[float]:
    if not rows:
        return None
    return max(rows, key=lambda r: r.buy_volume + r.sell_volume).price


def value_area_from_rows(rows: list, poc_price: Optional[float], target_pct: float):
    """`rows` need not be sorted; `poc_price` must be the POC of these same
    rows (Footprint.value_area passes what Footprint.poc would already
    compute from them, so a caller merging several candles' rows only pays
    for that scan once)."""
    if not rows or poc_price is None:
        return None
    rows = sorted(rows, key=lambda r: r.price)
    prices = [r.price for r in rows]
    volumes = [r.buy_volume + r.sell_volume for r in rows]
    total = sum(volumes)
    target = total * target_pct
    poc_idx = prices.index(poc_price)
    lo, hi = poc_idx, poc_idx
    covered = volumes[poc_idx]

    while covered < target and (lo > 0 or hi < len(prices) - 1):
        vol_below = volumes[lo - 1] if lo > 0 else -1
        vol_above = volumes[hi + 1] if hi < len(prices) - 1 else -1
        if vol_below >= vol_above:
            lo -= 1
            covered += vol_below
        else:
            hi += 1
            covered += vol_above

    return prices[lo], prices[hi]


def stacked_imbalances_from_rows(rows: list, threshold: float, min_stack: int):
    rows = sorted(rows, key=lambda r: r.price)
    flags = []
    for i in range(1, len(rows)):
        row, row_below = rows[i], rows[i - 1]
        if row_below.sell_volume > 0 and row.buy_volume / row_below.sell_volume >= threshold:
            flags.append(("BUY_IMBALANCE", row.price))
        if row_below.buy_volume > 0 and row.sell_volume / row_below.buy_volume >= threshold:
            flags.append(("SELL_IMBALANCE", row.price))

    stacks, run = [], []
    for kind, price in flags:
        if run and run[-1][0] == kind:
            run.append((kind, price))
        else:
            if len(run) >= min_stack:
                stacks.append(run)
            run = [(kind, price)]
    if len(run) >= min_stack:
        stacks.append(run)
    return stacks
