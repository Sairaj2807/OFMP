"""Trade-side classifiers.

A classifier is stateless: everything it needs about the past (the previous
trade's price and side) arrives in the ClassificationContext, and the
pipeline owns that history. Given the same context, a classifier always
returns the same Classification — which is what makes historical output
reproducible.

Every Classification carries the classifier's name and version, and every
stored trade records them, so a change of algorithm is always a new version
next to the old one, never a silent in-place change.

The production default is VtrenderReconstructedClassifierV1
(see VTRENDERS_RECONSTRUCTED_ALGORITHM.md), pinned by
tests/regression/test_vtrender_classifier.py.
"""
from dataclasses import dataclass
from typing import Optional, Protocol

from .models import TradeSide
from .settings import VTRENDER_STALE_QUOTE_MS


@dataclass(frozen=True)
class ClassificationContext:
    price: float
    best_bid: Optional[float]
    best_ask: Optional[float]
    prev_price: Optional[float] = None     # previous classified trade's price
    prev_side: Optional[str] = None        # previous classified trade's side
    quote_age_ms: Optional[int] = None     # time since the best bid/ask last changed


@dataclass(frozen=True)
class Classification:
    side: TradeSide
    method: str                 # human-readable rule that decided, e.g. "Midpoint Rule (VTRenders)"
    classifier_name: str
    classifier_version: str
    confidence: Optional[float] = None   # None: the rule gives no calibrated confidence


class TradeClassifier(Protocol):
    name: str
    version: str

    def classify(self, ctx: ClassificationContext) -> Classification:
        ...


def _tick_rule(price, prev_price, prev_side):
    """Shared tick-rule fallback: (side, method)."""
    if prev_price is None:
        return "BUY", "Cold Start"
    if price > prev_price:
        return "BUY", "Tick Rule (uptick)"
    if price < prev_price:
        return "SELL", "Tick Rule (downtick)"
    return prev_side or "BUY", "Zero Tick Rule"


class VtrenderReconstructedClassifierV1:
    """VTRenders reconstructed trade classifier (see
    VTRENDERS_RECONSTRUCTED_ALGORITHM.md). Primary rule is a midpoint test
    with the opposite polarity of the classic Lee-Ready quote rule —
    P < mid -> BUY, P > mid -> SELL — validated at 96.4% (53/55) on the
    verified dataset, 54/55 including the stale-quote fallback.

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

    Note the midpoint rule sends a trade exactly AT mid to SELL (P < mid is
    false). That is the validated behaviour and is kept as-is."""

    name = "vtrender_reconstruction"
    version = "v1"

    def __init__(self, stale_threshold_ms: int = VTRENDER_STALE_QUOTE_MS):
        self.stale_threshold_ms = stale_threshold_ms

    def classify(self, ctx: ClassificationContext) -> Classification:
        price, prev_price, prev_side = ctx.price, ctx.prev_price, ctx.prev_side

        if ctx.quote_age_ms is not None and ctx.quote_age_ms >= self.stale_threshold_ms:
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
            method = "Tick Rule (stale-quote fallback)"
        elif ctx.best_bid is not None and ctx.best_ask is not None:
            # Primary rule — validated 96.4% (53/55): opposite polarity of
            # the classic Lee-Ready quote rule.
            mid = (ctx.best_bid + ctx.best_ask) / 2.0
            side = "BUY" if price < mid else "SELL"
            method = "Midpoint Rule (VTRenders)"
        else:
            side, method = _tick_rule(price, prev_price, prev_side)

        return Classification(side, method, self.name, self.version)


class LeeReadyClassifier:
    """Textbook Lee & Ready (1991): quote rule (at/above ask -> BUY, at/below
    bid -> SELL), then tick rule, zero-tick carry-forward, cold-start BUY.
    Scored 13/55 on the verified set; kept as a reference baseline."""

    name = "lee_ready"
    version = "v1"

    def classify(self, ctx: ClassificationContext) -> Classification:
        if ctx.best_ask is not None and ctx.price >= ctx.best_ask:
            side, method = "BUY", "Quote Rule (at/above Ask)"
        elif ctx.best_bid is not None and ctx.price <= ctx.best_bid:
            side, method = "SELL", "Quote Rule (at/below Bid)"
        else:
            side, method = _tick_rule(ctx.price, ctx.prev_price, ctx.prev_side)
        return Classification(side, method, self.name, self.version)


class TickRuleClassifier:
    """Tick rule only: uptick -> BUY, downtick -> SELL, zero tick carries the
    previous side, cold start BUY. Ignores the book."""

    name = "tick_rule"
    version = "v1"

    def classify(self, ctx: ClassificationContext) -> Classification:
        side, method = _tick_rule(ctx.price, ctx.prev_price, ctx.prev_side)
        return Classification(side, method, self.name, self.version)
