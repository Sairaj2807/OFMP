"""Best-N order book.

Angel One's Snap Quote WebSocket mode delivers a fresh best-5 snapshot on
every tick rather than true incremental L2 deltas, so `OrderBook` replaces
each side wholesale per tick instead of merging by price. Everything
downstream (best bid/ask, spread, mid, OBI, top_n) only reads the top 5.
"""
from typing import Optional

from sortedcontainers import SortedDict


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
