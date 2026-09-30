"""Per-trade feature snapshot for the classification evidence pipeline
(TRADE_CLASSIFICATION.md Phase 1, REPLAY_WORKFLOW.md).

Turns the engine's TradeEvent into the flat observation record written to
data/sessions/<date>/observations.jsonl and consumed by review_cli.py,
export_dataset.py and replay. Kept out of the engine: it is research
tooling, not order-flow logic. trade_id is left unset — ObservationStore.log()
assigns it on write."""
from backend.app.domain.orderflow import TradeEvent


def build_observation(ev: TradeEvent) -> dict:
    tick, price, top5, prev_top5 = ev.tick, ev.price, ev.top5, ev.prev_top5
    best_bid, best_ask = ev.best_bid, ev.best_ask
    prev_best_bid, prev_best_ask = ev.prev_best_bid, ev.prev_best_ask

    spread = (best_ask - best_bid) if (best_bid is not None and best_ask is not None) else None
    bid_qty_l1 = top5["bids"][0][1][0] if top5["bids"] else None
    ask_qty_l1 = top5["asks"][0][1][0] if top5["asks"] else None
    bid_sum = sum(q for _, (q, _o) in top5["bids"])
    ask_sum = sum(q for _, (q, _o) in top5["asks"])
    weighted_mid_top5 = None
    if best_bid is not None and best_ask is not None and (bid_sum + ask_sum) > 0:
        weighted_mid_top5 = (best_bid * ask_sum + best_ask * bid_sum) / (bid_sum + ask_sum)

    inside_spread = (best_bid is not None and best_ask is not None and best_bid < price < best_ask)
    at_bid = best_bid is not None and price <= best_bid
    at_ask = best_ask is not None and price >= best_ask

    return {
        "trade_id": None,  # assigned by ObservationStore.log()
        "ts_ms": tick["ltt"],
        "ltp": price,
        "qty": ev.qty,
        "cum_volume": tick["cum_volume"],

        "best_bid": best_bid,
        "best_ask": best_ask,
        "spread": spread,
        "mid_price": ev.mid_price,
        "micro_price": ev.micro_price,
        "weighted_mid_top5": weighted_mid_top5,

        "distance_from_bid": (price - best_bid) if best_bid is not None else None,
        "distance_from_ask": (best_ask - price) if best_ask is not None else None,
        "relative_position_in_spread": ((price - best_bid) / spread) if spread else None,
        "inside_spread": inside_spread,
        "at_bid": at_bid,
        "at_ask": at_ask,

        "bid_qty_l1": bid_qty_l1,
        "ask_qty_l1": ask_qty_l1,
        "bid_ask_ratio_l1": (bid_qty_l1 / ask_qty_l1) if ask_qty_l1 else None,
        "book_imbalance_top5": ev.book_imbalance_top5,

        "bid_levels": [[p, q_, o] for p, (q_, o) in top5["bids"]],
        "ask_levels": [[p, q_, o] for p, (q_, o) in top5["asks"]],
        "prev_bid_levels": [[p, q_, o] for p, (q_, o) in prev_top5["bids"]],
        "prev_ask_levels": [[p, q_, o] for p, (q_, o) in prev_top5["asks"]],

        "prev_best_bid": prev_best_bid,
        "prev_best_ask": prev_best_ask,
        "best_bid_changed": ev.bid_changed,
        "best_ask_changed": ev.ask_changed,
        "bid_movement": (best_bid - prev_best_bid)
            if (best_bid is not None and prev_best_bid is not None) else None,
        "ask_movement": (best_ask - prev_best_ask)
            if (best_ask is not None and prev_best_ask is not None) else None,

        "prev_ltp": ev.prev_price,
        "prev_side": ev.prev_side,
        "price_changed": (ev.prev_price is not None and price != ev.prev_price),

        "algo_side": ev.classification.side,
        "algo_reason": ev.classification.method,
        "classifier_name": ev.classification.classifier_name,
        "classifier_version": ev.classification.classifier_version,

        "time_since_prev_trade_ms": ev.time_since_prev_trade_ms,
        "quote_age_ms": ev.quote_age_ms,
        "ticks_since_last_trade": ev.ticks_since_last_trade,
        "volume_since_last_price_change": ev.volume_since_last_price_change,
    }
