"""Cumulative volume delta."""


class CVDTracker:
    def __init__(self):
        self.cvd = 0

    def update(self, candle_delta: int) -> int:
        self.cvd += candle_delta
        return self.cvd
