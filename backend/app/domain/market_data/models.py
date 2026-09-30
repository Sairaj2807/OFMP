"""Canonical market-data types. Every provider-specific packet is normalized
into a MarketTick before anything else sees it; nothing downstream depends
on a broker's wire format."""
from dataclasses import asdict, dataclass, field
from typing import NamedTuple, Optional


class DepthLevel(NamedTuple):
    price: float
    qty: int
    orders: int


@dataclass(frozen=True)
class InstrumentRef:
    """What a provider needs to subscribe to one instrument."""
    provider: str               # e.g. "angelone"
    token: str                  # provider's instrument token
    exchange_segment: str       # provider's segment code, e.g. "NFO"
    symbol: str                 # trading symbol, e.g. "NIFTY27OCT26FUT"

    @classmethod
    def from_contract(cls, contract: dict, provider: str = "angelone") -> "InstrumentRef":
        return cls(provider=provider, token=str(contract["token"]),
                   exchange_segment=contract.get("exch_seg", "NFO"), symbol=contract["tradingsymbol"])


@dataclass(frozen=True)
class MarketTick:
    """One normalized quote/trade update.

    Timestamps are epoch milliseconds:
      exchange_ts_ms     exchange timestamp of the packet, as reported by the provider
      last_trade_ts_ms   exchange time of the last trade (Angel: whole seconds)
      received_ts_ms     when this server received the packet
    Provider-reported times are only as good as the provider; they are not
    independently verified exchange times."""
    provider: str
    token: str
    exchange_segment: Optional[str]
    sequence: Optional[int]
    exchange_ts_ms: Optional[int]
    last_trade_ts_ms: int
    received_ts_ms: int
    ltp: float
    last_traded_qty: Optional[int]
    cumulative_volume: int
    bids: tuple = ()             # DepthLevel, best first
    asks: tuple = ()             # DepthLevel, best first
    avg_price: Optional[float] = None
    open: Optional[float] = None
    high: Optional[float] = None
    low: Optional[float] = None
    close: Optional[float] = None
    open_interest: Optional[int] = None
    total_buy_qty: Optional[float] = None
    total_sell_qty: Optional[float] = None
    extra: dict = field(default_factory=dict)   # provider-specific fields kept for audit

    def to_engine_tick(self) -> dict:
        """The input shape orderflow.process_tick consumes."""
        return {
            "ltp": self.ltp,
            "ltt": self.last_trade_ts_ms,
            "cum_volume": self.cumulative_volume,
            "depth_buy": [tuple(level) for level in self.bids],
            "depth_sell": [tuple(level) for level in self.asks],
        }

    def to_dict(self) -> dict:
        """JSON-safe dict (depth levels as [price, qty, orders] lists)."""
        d = asdict(self)
        d["bids"] = [list(level) for level in self.bids]
        d["asks"] = [list(level) for level in self.asks]
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "MarketTick":
        d = dict(d)
        d["bids"] = tuple(DepthLevel(*level) for level in d.get("bids") or ())
        d["asks"] = tuple(DepthLevel(*level) for level in d.get("asks") or ())
        d.setdefault("extra", {})
        return cls(**d)
