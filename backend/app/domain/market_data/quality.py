"""Data-quality checks on the normalized tick stream.

The monitor only observes and records; it never alters, drops or fills
ticks (the engine has its own crossed-book and volume-reset guards). Each
finding is a DataQualityEvent for logging now and the data_quality_events
table later.

Deliberately NOT checked: gaps in the sequence number. Angel's Snap Quote
sequence is not documented as contiguous per instrument, so a jump does not
prove a missed tick; only non-increasing sequences are flagged."""
from dataclasses import asdict, dataclass
from typing import Optional

from .models import MarketTick


@dataclass(frozen=True)
class DataQualityEvent:
    kind: str                  # see TickQualityMonitor for the kinds
    severity: str              # "info" | "warning" | "error"
    provider: str
    token: str
    ts_ms: int                 # server receive time of the tick that triggered it
    detail: str

    def to_dict(self) -> dict:
        return asdict(self)


class TickQualityMonitor:
    """Per-instrument checks:

    sequence_not_increasing  sequence <= previous sequence (duplicate or out of order)
    timestamp_reversal       last-trade time earlier than the previous tick's
    volume_reset             cumulative volume decreased (session reset or bad packet)
    crossed_book             best bid >= best ask
    empty_book_side          no bids or no asks
    non_positive_price       ltp <= 0
    feed_gap                 no tick for longer than gap_threshold_ms between two ticks
    """

    def __init__(self, gap_threshold_ms: int = 60_000):
        self.gap_threshold_ms = gap_threshold_ms
        self._last: dict = {}   # token -> MarketTick

    def observe(self, tick: MarketTick) -> list:
        events = []

        def add(kind, severity, detail):
            events.append(DataQualityEvent(kind, severity, tick.provider, tick.token, tick.received_ts_ms, detail))

        prev: Optional[MarketTick] = self._last.get(tick.token)
        if prev is not None:
            if tick.sequence is not None and prev.sequence is not None and tick.sequence <= prev.sequence:
                add("sequence_not_increasing", "warning", f"sequence {tick.sequence} after {prev.sequence}")
            if tick.last_trade_ts_ms < prev.last_trade_ts_ms:
                add("timestamp_reversal", "warning",
                    f"last trade time {tick.last_trade_ts_ms} after {prev.last_trade_ts_ms}")
            if tick.cumulative_volume < prev.cumulative_volume:
                add("volume_reset", "warning",
                    f"cumulative volume {tick.cumulative_volume} after {prev.cumulative_volume}")
            gap = tick.received_ts_ms - prev.received_ts_ms
            if gap > self.gap_threshold_ms:
                add("feed_gap", "warning", f"{gap} ms without a tick")

        if tick.ltp <= 0:
            add("non_positive_price", "error", f"ltp {tick.ltp}")
        if not tick.bids or not tick.asks:
            add("empty_book_side", "info", f"{len(tick.bids)} bid / {len(tick.asks)} ask levels")
        elif tick.bids[0].price >= tick.asks[0].price:
            add("crossed_book", "warning", f"bid {tick.bids[0].price} >= ask {tick.asks[0].price}")

        self._last[tick.token] = tick
        return events

    def reset(self, token: Optional[str] = None) -> None:
        if token is None:
            self._last.clear()
        else:
            self._last.pop(token, None)
