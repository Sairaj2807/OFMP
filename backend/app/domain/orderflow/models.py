"""Value types shared across the order-flow engine."""
from dataclasses import dataclass
from typing import Literal

TradeSide = Literal["BUY", "SELL"]


@dataclass
class Trade:
    timestamp: int          # epoch millis
    price: float
    quantity: int
    side: str                # "BUY" | "SELL"


@dataclass
class FootprintCell:
    buy_volume: int = 0
    sell_volume: int = 0
    buy_trades: int = 0
    sell_trades: int = 0

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
