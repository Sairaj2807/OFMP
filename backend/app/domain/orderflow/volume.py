"""Trade-quantity extraction from a cumulative volume counter."""
from typing import Optional


class CumulativeVolumeExtractor:
    """Strategy A (diff against cumulative day volume): the feed sends a
    running session total, not discrete prints, so a tick's traded quantity
    is the increase since the previous tick.

    - The first tick only primes the baseline (returns 0).
    - A decrease is a session/sequence reset, not a negative trade: the
      baseline resynchronises and 0 is returned.
    - Several prints between two ticks arrive as one jump; they cannot be
      split, so the whole quantity is one trade downstream."""

    def __init__(self):
        self.last_cum_vol: Optional[int] = None

    def extract(self, cum_vol: int) -> int:
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
