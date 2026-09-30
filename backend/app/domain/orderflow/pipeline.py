"""Per-tick processing pipeline.

    tick
     -> order book update       (_update_book)
     -> book validation         (crossed book: tick discarded)
     -> quote-change tracking   (feeds the classifier's quote age)
     -> trade extraction        (CumulativeVolumeExtractor)
     -> trade classification    (the configured TradeClassifier)
     -> aggregation             (Footprint.add_trade, candle rollover, CVD)
     -> trade event             (optional on_trade callback)

The stage order is the pre-refactor process_tick's order and is pinned by
tests/regression/test_engine_golden.py. Live processing and replay share
advance_candle, so candle rollover cannot drift between them.
"""
from dataclasses import dataclass
from typing import Callable, Optional

from .classification import Classification, ClassificationContext, TradeClassifier
from .cvd import CVDTracker
from .footprint import Footprint
from .models import Trade
from .orderbook import OrderBook
from .registry import get_classifier
from .settings import DEPTH_LEVELS, MAX_CANDLES_KEPT, NATIVE_CANDLE_SEC
from .volume import CumulativeVolumeExtractor


@dataclass(frozen=True)
class TradeEvent:
    """Everything known about one classified trade at the instant it was
    produced. `top5`/`prev_top5` are snapshots; derived book values (mid,
    micro price, OBI) are computed from the post-tick book."""
    tick: dict
    price: float
    qty: int
    candle_ts: int
    classification: Classification
    prev_price: Optional[float]
    prev_side: Optional[str]
    best_bid: Optional[float]
    best_ask: Optional[float]
    prev_best_bid: Optional[float]
    prev_best_ask: Optional[float]
    bid_changed: bool
    ask_changed: bool
    top5: dict
    prev_top5: dict
    mid_price: Optional[float]
    micro_price: Optional[float]
    book_imbalance_top5: float
    quote_age_ms: Optional[int]
    time_since_prev_trade_ms: Optional[int]
    ticks_since_last_trade: int
    volume_since_last_price_change: int


class TickProcessorState:
    """All mutable engine state for one instrument."""

    def __init__(self, tick_size: float, classifier: Optional[TradeClassifier] = None):
        self.book = OrderBook()
        self.volume = CumulativeVolumeExtractor()
        self.classifier: TradeClassifier = classifier or get_classifier()
        self.footprint = Footprint(tick_size, NATIVE_CANDLE_SEC)
        self.cvd_tracker = CVDTracker()
        self.cvd_by_candle: dict = {}   # candle_ts -> running CVD after that candle
        self.tick_count = 0
        self.last_candle_seen: Optional[int] = None

        # Trade tape: the previous classified trade, handed to the classifier.
        self.last_price: Optional[float] = None
        self.last_side: Optional[str] = None
        self.last_classification: Optional[Classification] = None

        # When the best bid/ask last changed (drives quote age, which the v1
        # classifier's stale-quote fallback reads).
        self.quote_last_changed_ms: Optional[int] = None

        # Trade-event bookkeeping (reported in TradeEvent, does not affect
        # classification).
        self.ticks_since_last_trade: int = 0
        self.last_trade_ts_ms: Optional[int] = None
        self.volume_since_last_price_change: int = 0


def advance_candle(state, candle_ts) -> None:
    """Shared candle-rollover bookkeeping: when a trade lands in a new
    candle, close out the previous one's CVD contribution. Used by live
    processing and replay so rollover can't drift between them."""
    if state.last_candle_seen is not None and candle_ts != state.last_candle_seen:
        closed_delta = state.footprint.candle_delta(state.last_candle_seen)
        running_cvd = state.cvd_tracker.update(closed_delta)
        state.cvd_by_candle[state.last_candle_seen] = running_cvd
        state.footprint.prune_old(MAX_CANDLES_KEPT)
    state.last_candle_seen = candle_ts


def _update_book(state: TickProcessorState, tick: dict) -> None:
    if tick.get("depth_buy") is not None:
        state.book.replace_side("BUY", tick["depth_buy"])
    if tick.get("depth_sell") is not None:
        state.book.replace_side("SELL", tick["depth_sell"])


def process_tick(state: TickProcessorState, tick: dict,
                 on_trade: Optional[Callable[[TradeEvent], None]] = None) -> dict:
    """Runs one tick through the pipeline.

    tick: {"ltp": float, "ltt": int (epoch ms), "cum_volume": int,
           "depth_buy"/"depth_sell": [(price, qty, orders), ...]}

    on_trade: optional callable(TradeEvent), invoked once per classified
    trade. Never affects processing.

    Returns {"discarded": True, "reason": "crossed_book"} for a rejected
    tick, otherwise {"discarded": False, "new_trade": bool, "candle_ts": ...}
    plus "side"/"qty" when a trade was produced."""
    state.tick_count += 1
    state.ticks_since_last_trade += 1

    # Book as it stood just before this tick — for quote-change detection
    # and the trade event's previous-book snapshot.
    prev_bb = state.book.best_bid()
    prev_ba = state.book.best_ask()
    prev_top5 = state.book.top_n(DEPTH_LEVELS) if on_trade is not None else None

    _update_book(state, tick)
    if not state.book.validate():
        return {"discarded": True, "reason": "crossed_book"}

    bb = state.book.best_bid()
    ba = state.book.best_ask()
    best_bid = bb[0] if bb else None
    best_ask = ba[0] if ba else None
    prev_best_bid = prev_bb[0] if prev_bb else None
    prev_best_ask = prev_ba[0] if prev_ba else None

    bid_changed = best_bid != prev_best_bid
    ask_changed = best_ask != prev_best_ask
    if bid_changed or ask_changed:
        state.quote_last_changed_ms = tick.get("ltt")

    result = {"discarded": False, "new_trade": False, "candle_ts": None}
    qty = state.volume.extract(tick["cum_volume"])
    if qty <= 0:
        return result

    price = tick["ltp"]
    prev_price, prev_side = state.last_price, state.last_side
    quote_age_ms = (tick["ltt"] - state.quote_last_changed_ms) \
        if state.quote_last_changed_ms is not None else None

    classification = state.classifier.classify(ClassificationContext(
        price=price, best_bid=best_bid, best_ask=best_ask,
        prev_price=prev_price, prev_side=prev_side, quote_age_ms=quote_age_ms))
    side = classification.side
    state.last_price, state.last_side = price, side
    state.last_classification = classification

    candle_ts = state.footprint.add_trade(Trade(timestamp=tick["ltt"], price=price, quantity=qty, side=side))
    result.update(new_trade=True, candle_ts=candle_ts, side=side, qty=qty)
    advance_candle(state, candle_ts)

    ticks_since_last_trade = state.ticks_since_last_trade  # pre-reset, includes this tick
    if prev_price is not None and price == prev_price:
        vol_since_price_change = state.volume_since_last_price_change + qty
    else:
        vol_since_price_change = qty

    if on_trade is not None:
        on_trade(TradeEvent(
            tick=tick, price=price, qty=qty, candle_ts=candle_ts, classification=classification,
            prev_price=prev_price, prev_side=prev_side,
            best_bid=best_bid, best_ask=best_ask,
            prev_best_bid=prev_best_bid, prev_best_ask=prev_best_ask,
            bid_changed=bid_changed, ask_changed=ask_changed,
            top5=state.book.top_n(DEPTH_LEVELS), prev_top5=prev_top5,
            mid_price=state.book.mid_price(), micro_price=state.book.micro_price(),
            book_imbalance_top5=state.book.obi(DEPTH_LEVELS),
            quote_age_ms=quote_age_ms,
            time_since_prev_trade_ms=(tick["ltt"] - state.last_trade_ts_ms)
                if state.last_trade_ts_ms is not None else None,
            ticks_since_last_trade=ticks_since_last_trade,
            volume_since_last_price_change=vol_since_price_change,
        ))

    state.ticks_since_last_trade = 0
    state.last_trade_ts_ms = tick["ltt"]
    state.volume_since_last_price_change = vol_since_price_change
    return result
