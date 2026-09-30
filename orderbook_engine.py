"""Order Book reconstruction + Order Flow Footprint aggregation.

Ports the algorithms from the design doc (Sections 2-4) with one deliberate
adaptation: Angel One's Snap Quote WebSocket mode delivers a fresh best-5
snapshot on every tick rather than true incremental L2 deltas, so `OrderBook`
replaces its top-5 windows wholesale each tick instead of merging by price
(see plan doc "Key adaptation"). Everything downstream (best_bid/ask, spread,
mid-price, OBI, top_n) is unaffected since it only ever reads the top-5.
"""
import math
from dataclasses import dataclass, field
from typing import Optional

from sortedcontainers import SortedDict

import config


@dataclass
class Trade:
    timestamp: int          # epoch millis
    price: float
    quantity: int
    side: str                # "BUY" | "SELL"
    coalesced: bool = False   # True if this trade's qty/price may represent
                               # more than one real exchange print merged
                               # into a single feed update (see
                               # TradeClassifier.extract_trade_deltas)


@dataclass
class FootprintCell:
    buy_volume: int = 0
    sell_volume: int = 0
    buy_trades: int = 0
    sell_trades: int = 0
    coalesced_trades: int = 0   # trades added with Trade.coalesced=True

    @property
    def delta(self) -> int:
        return self.buy_volume - self.sell_volume

    @property
    def total_volume(self) -> int:
        return self.buy_volume + self.sell_volume


@dataclass
class FootprintRow:
    """A candle's cell, with its price carried alongside instead of living
    only as the dict key — lets POC/value-area/imbalance and PPR grouping
    walk a plain list instead of re-deriving price from a separate keys list."""
    price: float
    buy_volume: int
    sell_volume: int
    buy_trades: int
    sell_trades: int
    delta: int
    coalesced_trades: int = 0


class OrderBook:
    def __init__(self):
        self.bids = SortedDict()   # price -> (qty, orders), ascending; best = max
        self.asks = SortedDict()   # price -> (qty, orders), ascending; best = min

    def replace_side(self, side: str, levels: list):
        """levels: list of (price, qty, orders). Wholesale replace — see module docstring."""
        book = self.bids if side == "BUY" else self.asks
        book.clear()
        for price, qty, orders in levels:
            if qty > 0:
                book[price] = (qty, orders)

    def best_bid(self) -> Optional[tuple]:
        return self.bids.peekitem(-1) if self.bids else None

    def best_ask(self) -> Optional[tuple]:
        return self.asks.peekitem(0) if self.asks else None

    def spread(self) -> Optional[float]:
        bb, ba = self.best_bid(), self.best_ask()
        return (ba[0] - bb[0]) if (bb and ba) else None

    def mid_price(self) -> Optional[float]:
        bb, ba = self.best_bid(), self.best_ask()
        return (bb[0] + ba[0]) / 2 if (bb and ba) else None

    def micro_price(self) -> Optional[float]:
        bb, ba = self.best_bid(), self.best_ask()
        if not (bb and ba):
            return None
        bid_qty, ask_qty = bb[1][0], ba[1][0]
        total = bid_qty + ask_qty
        if total == 0:
            return self.mid_price()
        return (bb[0] * ask_qty + ba[0] * bid_qty) / total

    def top_n(self, n=5):
        bids = list(self.bids.items())[-n:][::-1]
        asks = list(self.asks.items())[:n]
        return {"bids": bids, "asks": asks}

    def obi(self, depth_n=5) -> float:
        top = self.top_n(depth_n)
        bid_sum = sum(qty for _, (qty, _) in top["bids"])
        ask_sum = sum(qty for _, (qty, _) in top["asks"])
        return (bid_sum - ask_sum) / (bid_sum + ask_sum) if (bid_sum + ask_sum) else 0.0

    def validate(self) -> bool:
        """Crossed-book guard: best_bid must be strictly below best_ask."""
        bb, ba = self.best_bid(), self.best_ask()
        if bb and ba and bb[0] >= ba[0]:
            return False
        return True


class TradeClassifier:
    """VTRenders reconstructed trade classifier (see
    VTRENDERS_RECONSTRUCTED_ALGORITHM.md). Primary rule is a midpoint test
    with the opposite polarity of the classic Lee-Ready quote rule —
    P < mid -> BUY, P > mid -> SELL — validated at 96.4% (53/55) on the
    verified dataset.

    Also includes the spec's stale-quote fallback (§4.5), which is backed
    by exactly one example (trade_id 17) but is harmless elsewhere in the
    dataset (every other row has quote_age_ms == 0). Deliberately omits the
    spec's zero-tick carry-forward branch ("price_changed == False -> prev
    side"): applied literally to every price_changed==False row rather than
    hand-picked for its one motivating example (trade_id 25), it flips
    trades 32/33/41 from correct to wrong, dropping verified-set accuracy
    from 53/55 to 50/55 — i.e. it does not generalize past the single
    example the spec derived it from. The spec itself flags both
    refinements as unvalidated and names "drop them, use the primary rule
    alone" as an acceptable fallback (§6.5); that's what's implemented here
    for the zero-tick case.

    `classify`/`extract_trade_qty` above are the *inferred*-side path, used
    when the feed only gives a single blended cumulative volume counter
    (Angel One Snap Quote) and side has to be guessed from price vs. book.
    `extract_trade_deltas` below is a separate, *exact*-side path for feeds
    that report their own cumulative buy-initiated/sell-initiated traded
    volume (e.g. Arrow's `btv`/`atv`) — no inference needed, and unlike
    `classify` it can correctly report both sides at once."""

    def __init__(self):
        self.last_cum_vol: Optional[int] = None
        self.last_trade_key = None
        self.last_price: Optional[float] = None
        self.last_side: Optional[str] = None
        self.last_reason: Optional[str] = None
        self.last_btv: Optional[int] = None
        self.last_atv: Optional[int] = None

    def extract_trade_qty(self, cum_vol: int) -> int:
        """Strategy A (Section 3.2): diff against cumulative day volume."""
        if self.last_cum_vol is None:
            self.last_cum_vol = cum_vol
            return 0
        delta = cum_vol - self.last_cum_vol
        if delta < 0:
            # session/sequence reset — do not treat as a negative trade
            self.last_cum_vol = cum_vol
            return 0
        self.last_cum_vol = cum_vol
        return delta

    def extract_trade_deltas(self, btv: int, atv: int, ltq: Optional[int] = None):
        """Exact-side path: diffs the feed's own cumulative buy-traded-volume
        (`btv`) and sell/ask-traded-volume (`atv`) counters directly, instead
        of inferring side from price vs. quotes. A single feed update can
        legitimately produce both a nonzero buy_qty and a nonzero sell_qty —
        that case means the update coalesced more than one real exchange
        print, and the two counters still tell you exactly how much of each
        side happened even though the caller only has one `ltp` to stamp it
        on.

        Returns (buy_qty, sell_qty, coalesced):
          - buy_qty / sell_qty: volume diffed off btv/atv since the last
            call (0, 0 on the first call — no baseline yet).
          - coalesced: True if buy_qty + sell_qty doesn't match this
            update's own last-traded-quantity (`ltq`), meaning more than one
            real print landed in this single update, so the single `price`
            it gets stamped on by the caller isn't guaranteed exact. False
            if it matches (or `ltq` wasn't supplied — coalescing can't be
            evaluated without it, so it's assumed clean rather than flagged)."""
        if self.last_btv is None or self.last_atv is None:
            self.last_btv = btv
            self.last_atv = atv
            return 0, 0, False

        buy_delta = btv - self.last_btv
        sell_delta = atv - self.last_atv
        if buy_delta < 0 or sell_delta < 0:
            # session/sequence reset — do not treat as trades
            self.last_btv = btv
            self.last_atv = atv
            return 0, 0, False

        self.last_btv = btv
        self.last_atv = atv
        coalesced = ltq is not None and (buy_delta + sell_delta) != ltq
        return buy_delta, sell_delta, coalesced

    def classify(self, price, best_bid, best_ask, quote_age_ms=None,
                 stale_threshold_ms=config.VTRENDERS_STALE_QUOTE_MS) -> str:
        prev_price = self.last_price
        prev_side = self.last_side

        if quote_age_ms is not None and quote_age_ms >= stale_threshold_ms:
            # Unvalidated refinement, single example (trade_id 17): a
            # long-stale quote is no longer a trustworthy fair-value
            # reference, so fall back to price momentum. (>= not > — the
            # spec's own motivating example sits exactly at the threshold.)
            if prev_price is None:
                side = "BUY"
            elif price > prev_price:
                side = "BUY"
            elif price < prev_price:
                side = "SELL"
            else:
                side = prev_side or "BUY"
            reason = "Tick Rule (stale-quote fallback)"
        elif best_bid is not None and best_ask is not None:
            # Primary rule — validated 96.4% (53/55): opposite polarity of
            # the classic Lee-Ready quote rule.
            mid = (best_bid + best_ask) / 2.0
            side = "BUY" if price < mid else "SELL"
            reason = "Midpoint Rule (VTRenders)"
        elif prev_price is None:
            side = "BUY"
            reason = "Cold Start"
        elif price > prev_price:
            side = "BUY"
            reason = "Tick Rule (uptick)"
        elif price < prev_price:
            side = "SELL"
            reason = "Tick Rule (downtick)"
        else:
            side = prev_side or "BUY"
            reason = "Zero Tick Rule"

        self.last_price = price
        self.last_side = side
        self.last_reason = reason
        return side


class CVDTracker:
    def __init__(self):
        self.cvd = 0

    def update(self, candle_delta: int) -> int:
        self.cvd += candle_delta
        return self.cvd


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


class Footprint:
    def __init__(self, tick_size: float, candle_interval_sec: int):
        self.tick_size = tick_size
        self.candle_interval = candle_interval_sec
        self.data: dict = {}          # candle_ts -> {price: FootprintCell}
        self.candle_ohlc: dict = {}   # candle_ts -> {"open","high","low","close"}
        # candle_ts -> {"run","min","max","trades"}: the candle's running delta
        # in trade order (initial zero included) plus the number of Trade
        # objects added. Min/max cannot be recovered from the aggregated
        # cells afterwards, so they have to be tracked as trades arrive. The
        # order-flow chart's min/max-delta rows are built from this.
        self.candle_stats: dict = {}

    def _bucket_price(self, price: float) -> float:
        return round(round(price / self.tick_size) * self.tick_size, 8)

    def _bucket_time(self, ts_millis: int) -> int:
        secs = ts_millis // 1000
        return secs - (secs % self.candle_interval)

    def row_price_for(self, price: float, ppr: int = 1) -> float:
        """The PPR-bucketed row a raw price falls into (see get_candle_rows)."""
        width = max(ppr, self.tick_size)
        return round(math.floor(price / width) * width, 8)

    def add_trade(self, trade: Trade) -> int:
        """Returns the candle timestamp the trade was bucketed into."""
        candle_ts = self._bucket_time(trade.timestamp)
        price_lvl = self._bucket_price(trade.price)
        candle = self.data.setdefault(candle_ts, {})
        cell = candle.setdefault(price_lvl, FootprintCell())
        if trade.side == "BUY":
            cell.buy_volume += trade.quantity
            cell.buy_trades += 1
        else:
            cell.sell_volume += trade.quantity
            cell.sell_trades += 1
        if trade.coalesced:
            cell.coalesced_trades += 1

        stats = self.candle_stats.get(candle_ts)
        if stats is None:
            stats = self.candle_stats[candle_ts] = {"run": 0, "min": 0, "max": 0, "trades": 0}
        stats["run"] += trade.quantity if trade.side == "BUY" else -trade.quantity
        if stats["run"] < stats["min"]:
            stats["min"] = stats["run"]
        if stats["run"] > stats["max"]:
            stats["max"] = stats["run"]
        stats["trades"] += 1

        ohlc = self.candle_ohlc.get(candle_ts)
        if ohlc is None:
            self.candle_ohlc[candle_ts] = {
                "open": trade.price, "high": trade.price,
                "low": trade.price, "close": trade.price,
            }
        else:
            ohlc["high"] = max(ohlc["high"], trade.price)
            ohlc["low"] = min(ohlc["low"], trade.price)
            ohlc["close"] = trade.price

        return candle_ts

    def candle_delta(self, candle_ts) -> int:
        return sum(cell.delta for cell in self.data.get(candle_ts, {}).values())

    def get_candle_rows(self, candle_ts, ppr: int = 1) -> list:
        """This candle's footprint as price-descending rows. `ppr` (price per
        row) is the price WIDTH of one row in rupees — e.g. ppr=1 folds every
        traded price within the same whole-rupee band (24384.00..24384.99)
        into one row labeled by the band's floor (24384.90 + 24384.00 -> 24384)."""
        candle = self.data.get(candle_ts, {})
        if not candle:
            return []

        grouped: dict = {}
        for price, cell in candle.items():
            row_price = self.row_price_for(price, ppr)
            g = grouped.setdefault(row_price, FootprintCell())
            g.buy_volume += cell.buy_volume
            g.sell_volume += cell.sell_volume
            g.buy_trades += cell.buy_trades
            g.sell_trades += cell.sell_trades
            g.coalesced_trades += cell.coalesced_trades

        rows = [
            FootprintRow(
                price=price,
                buy_volume=c.buy_volume,
                sell_volume=c.sell_volume,
                buy_trades=c.buy_trades,
                sell_trades=c.sell_trades,
                delta=c.delta,
                coalesced_trades=c.coalesced_trades,
            )
            for price, c in grouped.items()
        ]
        rows.sort(key=lambda r: r.price, reverse=True)
        return rows

    def poc(self, candle_ts, ppr: int = 1) -> Optional[float]:
        return poc_from_rows(self.get_candle_rows(candle_ts, ppr))

    def value_area(self, candle_ts, target_pct=config.VALUE_AREA_PCT, ppr: int = 1):
        rows = self.get_candle_rows(candle_ts, ppr)
        return value_area_from_rows(rows, poc_from_rows(rows), target_pct)

    def stacked_imbalances(self, candle_ts, threshold=config.IMBALANCE_THRESHOLD,
                            min_stack=config.MIN_STACK, ppr: int = 1):
        return stacked_imbalances_from_rows(self.get_candle_rows(candle_ts, ppr), threshold, min_stack)

    def merged_candle_rows(self, candle_ts_list: list, ppr: int = 1) -> list:
        """Several native candles' rows (see get_candle_rows), summed by price
        into one price-descending list — how a coarser display interval's POC/
        value-area/imbalances get computed without a second bucketing scheme:
        poc_from_rows/value_area_from_rows/stacked_imbalances_from_rows run
        on this exactly as they do on a single native candle's own rows, so
        native and grouped views share the same analytics code, not a
        parallel reimplementation of it. See group_candle_timestamps, which
        decides which native candles belong in one `candle_ts_list`."""
        merged: dict = {}
        for ts in candle_ts_list:
            for row in self.get_candle_rows(ts, ppr):
                m = merged.get(row.price)
                if m is None:
                    m = merged[row.price] = FootprintRow(
                        price=row.price, buy_volume=0, sell_volume=0,
                        buy_trades=0, sell_trades=0, delta=0, coalesced_trades=0)
                m.buy_volume += row.buy_volume
                m.sell_volume += row.sell_volume
                m.buy_trades += row.buy_trades
                m.sell_trades += row.sell_trades
                m.coalesced_trades += row.coalesced_trades
                m.delta = m.buy_volume - m.sell_volume
        rows = list(merged.values())
        rows.sort(key=lambda r: r.price, reverse=True)
        return rows

    def prune_old(self, keep_last: int):
        if len(self.data) <= keep_last:
            return
        for ts in sorted(self.data.keys())[:-keep_last]:
            del self.data[ts]
            self.candle_stats.pop(ts, None)
            self.candle_ohlc.pop(ts, None)


def _has_ohlc(bar: dict) -> bool:
    return all(bar.get(k) is not None for k in ("open", "high", "low", "close"))


def _validate_display_interval(interval_sec: int, native_sec: int) -> None:
    if interval_sec <= 0 or interval_sec % native_sec != 0:
        raise ValueError(f"interval_sec must be a positive multiple of {native_sec}, got {interval_sec}")


def group_native_bars(bars: list, interval_sec: int, native_sec: int = config.CANDLE_INTERVAL_SEC) -> list:
    """Aggregates a time-ordered list of NATIVE-interval candle-bar dicts (the
    shape server._chart_payload builds — time/open/high/low/close/delta/
    min_delta/max_delta/trades/cells) into `interval_sec`-wide bars.

    This is how the order-flow chart offers 3/5/15/30-minute views without the
    engine ever bucketing trades at more than one granularity: the hot tick
    path (Footprint.add_trade) always buckets at `native_sec` (see
    config.CANDLE_INTERVAL_SEC), and coarser candles are assembled here, purely
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
                            native_sec: int = config.CANDLE_INTERVAL_SEC) -> list:
    """Buckets a time-ordered list of native candle timestamps into groups
    sharing one `interval_sec`-wide display bucket, preserving order.

    Companion to group_native_bars, which groups already-SERIALIZED native
    bar dicts (the /chart footprint-primitive payload's shape). This instead
    groups the plain timestamps themselves, for a caller (server.
    _candles_payload, the older per-candle footprint-TABLE payload) that
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


class TickProcessorState:
    def __init__(self, tick_size: float):
        self.book = OrderBook()
        self.classifier = TradeClassifier()
        self.footprint = Footprint(tick_size, config.CANDLE_INTERVAL_SEC)
        self.cvd_tracker = CVDTracker()
        self.cvd_by_candle: dict = {}   # candle_ts -> running CVD after that candle
        self.tick_count = 0
        self.last_candle_seen: Optional[int] = None

        # Evidence-collection bookkeeping (see TRADE_CLASSIFICATION.md's
        # reverse-engineering plan, Phase 1) — tracks quote/tick history
        # purely for observation logging, does not affect classification.
        self.quote_last_changed_ms: Optional[int] = None
        self.ticks_since_last_trade: int = 0
        self.last_trade_ts_ms: Optional[int] = None
        self.volume_since_last_price_change: int = 0


def _advance_candle(state: TickProcessorState, candle_ts) -> None:
    """Shared candle-rollover bookkeeping: when a trade lands in a new
    candle, close out the previous one's CVD contribution. Used by both the
    exact-side and heuristic trade paths so rollover can't drift between
    them."""
    if state.last_candle_seen is not None and candle_ts != state.last_candle_seen:
        closed_delta = state.footprint.candle_delta(state.last_candle_seen)
        running_cvd = state.cvd_tracker.update(closed_delta)
        state.cvd_by_candle[state.last_candle_seen] = running_cvd
        state.footprint.prune_old(config.MAX_CANDLES_KEPT)
    state.last_candle_seen = candle_ts


def _process_exact_sides(state: TickProcessorState, tick: dict, observation_sink=None) -> dict:
    """Exact-side trade path — used when the feed reports its own cumulative
    buy/sell traded volume (`btv`/`atv`, e.g. Arrow's HFT stream) instead of
    only a single blended `cum_volume` counter. See
    TradeClassifier.extract_trade_deltas. No quote-rule/tick-rule inference
    happens here — side is read directly off the feed, so up to two Trade
    objects (one BUY, one SELL) can be emitted for a single update when the
    feed coalesced more than one real print together.

    observation_sink (if given) is invoked once per update that produced a
    trade, with the raw btv/atv/ltq-derived record — no algo_side/reason,
    since there's nothing inferred here to evidence-collect against. See
    observation_store.ArrowTradeStore."""
    price = tick["ltp"]
    buy_qty, sell_qty, coalesced = state.classifier.extract_trade_deltas(
        tick["btv"], tick["atv"], tick.get("ltq"))

    result = {"discarded": False, "new_trade": False, "candle_ts": None,
              "coalesced": coalesced}
    if buy_qty <= 0 and sell_qty <= 0:
        return result

    candle_ts = None
    for qty, side in ((buy_qty, "BUY"), (sell_qty, "SELL")):
        if qty <= 0:
            continue
        trade = Trade(timestamp=tick["ltt"], price=price, quantity=qty,
                       side=side, coalesced=coalesced)
        candle_ts = state.footprint.add_trade(trade)
        _advance_candle(state, candle_ts)

    result["new_trade"] = True
    result["candle_ts"] = candle_ts
    result["buy_qty"] = buy_qty
    result["sell_qty"] = sell_qty

    state.ticks_since_last_trade = 0
    state.last_trade_ts_ms = tick["ltt"]

    if observation_sink is not None:
        top5 = state.book.top_n(config.DEPTH_LEVELS)
        bb = state.book.best_bid()
        ba = state.book.best_ask()
        observation_sink({
            "trade_id": None,  # assigned by ArrowTradeStore's writer
            "ts_ms": tick["ltt"],
            "ltp": price,
            "buy_qty": buy_qty,
            "sell_qty": sell_qty,
            "coalesced": coalesced,
            "btv": tick["btv"],
            "atv": tick["atv"],
            "ltq": tick.get("ltq"),
            "cum_volume": tick.get("cum_volume"),
            "candle_ts": candle_ts,
            "best_bid": bb[0] if bb else None,
            "best_ask": ba[0] if ba else None,
            "bid_levels": [[p, q_, o] for p, (q_, o) in top5["bids"]],
            "ask_levels": [[p, q_, o] for p, (q_, o) in top5["asks"]],
        })

    return result


def _process_heuristic_trade(state: TickProcessorState, tick: dict, observation_sink,
                              best_bid_price, best_ask_price,
                              prev_best_bid_price, prev_best_ask_price,
                              prev_top5, bid_changed, ask_changed) -> dict:
    """Inferred-side trade path (the original algorithm) — used when the
    feed only gives a single blended cumulative volume counter (Angel One
    Snap Quote), so side has to be guessed via TradeClassifier.classify.
    See TRADE_CLASSIFICATION.md."""
    qty = state.classifier.extract_trade_qty(tick["cum_volume"])
    result = {"discarded": False, "new_trade": False, "candle_ts": None}

    if qty <= 0:
        return result

    price = tick["ltp"]
    prev_price = state.classifier.last_price
    prev_side = state.classifier.last_side

    quote_age_ms = (tick["ltt"] - state.quote_last_changed_ms) \
        if state.quote_last_changed_ms is not None else None

    side = state.classifier.classify(price, best_bid_price, best_ask_price, quote_age_ms)
    reason = state.classifier.last_reason
    trade = Trade(timestamp=tick["ltt"], price=price, quantity=qty, side=side)
    candle_ts = state.footprint.add_trade(trade)
    result["new_trade"] = True
    result["candle_ts"] = candle_ts
    result["side"] = side
    result["qty"] = qty

    _advance_candle(state, candle_ts)

    ticks_since_last_trade = state.ticks_since_last_trade  # pre-reset, includes this tick
    if prev_price is not None and price == prev_price:
        vol_since_price_change = state.volume_since_last_price_change + qty
    else:
        vol_since_price_change = qty

    if observation_sink is not None:
        observation = _build_observation(
            state, tick, price, qty, side, reason,
            best_bid_price, best_ask_price,
            prev_best_bid_price, prev_best_ask_price,
            prev_price, prev_side, prev_top5,
            bid_changed, ask_changed,
            ticks_since_last_trade, vol_since_price_change,
            quote_age_ms,
        )
        observation_sink(observation)

    # Advance the observation-only bookkeeping (after building the
    # observation above, which needs the *pre-trade* values).
    state.ticks_since_last_trade = 0
    state.last_trade_ts_ms = tick["ltt"]
    state.volume_since_last_price_change = vol_since_price_change

    return result


def process_tick(state: TickProcessorState, tick: dict, observation_sink=None) -> Optional[dict]:
    """Section 3.4 ProcessTick, adapted for the Snap Quote wholesale-replace book.

    Dispatches to one of two trade-classification paths depending on what
    the tick carries:

    - Exact-side path (`_process_exact_sides`), when `tick["btv"]` and
      `tick["atv"]` are present (e.g. Arrow's HFT stream) — reads side
      directly off the feed's own cumulative buy/sell traded volume, no
      inference. tick also accepts optional `ltq` (last traded quantity)
      for coalescing detection.
    - Heuristic path (`_process_heuristic_trade`), otherwise (e.g. Angel One
      Snap Quote) — infers side via TradeClassifier.classify (quote rule +
      tick rule fallback), as documented in TRADE_CLASSIFICATION.md.

    Common tick fields (either path):
        "ltp": float, "ltt": int (epoch ms), "depth_buy"/"depth_sell":
        [(price, qty, orders), ...]
    Heuristic-path-only: "cum_volume": int
    Exact-path-only: "btv": int, "atv": int, "ltq": Optional[int]

    observation_sink: optional callable(dict) -> None, invoked once per
    trade-producing update on whichever path fires — a full quote-rule
    feature snapshot on the heuristic path (see TRADE_CLASSIFICATION.md
    Phase 1, meant for ObservationStore), or the raw btv/atv/ltq-derived
    trade record on the exact-side path (meant for
    observation_store.ArrowTradeStore — no algo_side/reason, since nothing
    is inferred there). Never affects classification — purely a logging tap.

    Returns a dict describing what happened to this tick, or None if discarded.
    """
    state.tick_count += 1
    state.ticks_since_last_trade += 1

    # Book state as it stood just before this tick's update — needed to
    # detect quote changes and to log a "previous book snapshot".
    prev_bb = state.book.best_bid()
    prev_ba = state.book.best_ask()
    prev_top5 = state.book.top_n(config.DEPTH_LEVELS)

    if tick.get("depth_buy") is not None:
        state.book.replace_side("BUY", tick["depth_buy"])
    if tick.get("depth_sell") is not None:
        state.book.replace_side("SELL", tick["depth_sell"])

    if not state.book.validate():
        return {"discarded": True, "reason": "crossed_book"}

    bb = state.book.best_bid()
    ba = state.book.best_ask()
    best_bid_price = bb[0] if bb else None
    best_ask_price = ba[0] if ba else None
    prev_best_bid_price = prev_bb[0] if prev_bb else None
    prev_best_ask_price = prev_ba[0] if prev_ba else None

    bid_changed = best_bid_price != prev_best_bid_price
    ask_changed = best_ask_price != prev_best_ask_price
    if bid_changed or ask_changed:
        state.quote_last_changed_ms = tick.get("ltt")

    if tick.get("btv") is not None and tick.get("atv") is not None:
        return _process_exact_sides(state, tick, observation_sink)

    return _process_heuristic_trade(
        state, tick, observation_sink,
        best_bid_price, best_ask_price,
        prev_best_bid_price, prev_best_ask_price,
        prev_top5, bid_changed, ask_changed,
    )


def _build_observation(state, tick, price, qty, side, reason,
                        best_bid_price, best_ask_price,
                        prev_best_bid_price, prev_best_ask_price,
                        prev_price, prev_side, prev_top5,
                        bid_changed, ask_changed,
                        ticks_since_last_trade, vol_since_price_change,
                        quote_age_ms) -> dict:
    """Full per-trade feature snapshot for the evidence-collection pipeline.
    trade_id is left unset here — ObservationStore.log() assigns it on write."""
    spread = (best_ask_price - best_bid_price) \
        if (best_bid_price is not None and best_ask_price is not None) else None
    top5 = state.book.top_n(config.DEPTH_LEVELS)
    bid_qty_l1 = top5["bids"][0][1][0] if top5["bids"] else None
    ask_qty_l1 = top5["asks"][0][1][0] if top5["asks"] else None
    bid_sum = sum(q for _, (q, _o) in top5["bids"])
    ask_sum = sum(q for _, (q, _o) in top5["asks"])
    weighted_mid_top5 = None
    if best_bid_price is not None and best_ask_price is not None and (bid_sum + ask_sum) > 0:
        weighted_mid_top5 = (best_bid_price * ask_sum + best_ask_price * bid_sum) / (bid_sum + ask_sum)

    inside_spread = (best_bid_price is not None and best_ask_price is not None
                      and best_bid_price < price < best_ask_price)
    at_bid = best_bid_price is not None and price <= best_bid_price
    at_ask = best_ask_price is not None and price >= best_ask_price

    time_since_prev_trade_ms = (tick["ltt"] - state.last_trade_ts_ms) \
        if state.last_trade_ts_ms is not None else None

    return {
        "trade_id": None,  # assigned by ObservationStore.log()
        "ts_ms": tick["ltt"],
        "ltp": price,
        "qty": qty,
        "cum_volume": tick["cum_volume"],

        "best_bid": best_bid_price,
        "best_ask": best_ask_price,
        "spread": spread,
        "mid_price": state.book.mid_price(),
        "micro_price": state.book.micro_price(),
        "weighted_mid_top5": weighted_mid_top5,

        "distance_from_bid": (price - best_bid_price) if best_bid_price is not None else None,
        "distance_from_ask": (best_ask_price - price) if best_ask_price is not None else None,
        "relative_position_in_spread": ((price - best_bid_price) / spread) if spread else None,
        "inside_spread": inside_spread,
        "at_bid": at_bid,
        "at_ask": at_ask,

        "bid_qty_l1": bid_qty_l1,
        "ask_qty_l1": ask_qty_l1,
        "bid_ask_ratio_l1": (bid_qty_l1 / ask_qty_l1) if ask_qty_l1 else None,
        "book_imbalance_top5": state.book.obi(config.DEPTH_LEVELS),

        "bid_levels": [[p, q_, o] for p, (q_, o) in top5["bids"]],
        "ask_levels": [[p, q_, o] for p, (q_, o) in top5["asks"]],
        "prev_bid_levels": [[p, q_, o] for p, (q_, o) in prev_top5["bids"]],
        "prev_ask_levels": [[p, q_, o] for p, (q_, o) in prev_top5["asks"]],

        "prev_best_bid": prev_best_bid_price,
        "prev_best_ask": prev_best_ask_price,
        "best_bid_changed": bid_changed,
        "best_ask_changed": ask_changed,
        "bid_movement": (best_bid_price - prev_best_bid_price)
            if (best_bid_price is not None and prev_best_bid_price is not None) else None,
        "ask_movement": (best_ask_price - prev_best_ask_price)
            if (best_ask_price is not None and prev_best_ask_price is not None) else None,

        "prev_ltp": prev_price,
        "prev_side": prev_side,
        "price_changed": (prev_price is not None and price != prev_price),

        "algo_side": side,
        "algo_reason": reason,

        "time_since_prev_trade_ms": time_since_prev_trade_ms,
        "quote_age_ms": quote_age_ms,
        "ticks_since_last_trade": ticks_since_last_trade,
        "volume_since_last_price_change": vol_since_price_change,
    }
