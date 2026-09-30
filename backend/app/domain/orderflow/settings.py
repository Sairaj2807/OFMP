"""Default parameters of the order-flow engine.

The engine is pure domain code and does not read application config; the
application's config.py re-exports these so there is one source of truth.
Changing a value here changes derived output, so it goes together with an
algorithm version bump (see registry.py)."""

# Native footprint candle width. The ONLY interval the engine buckets trades
# into; coarser display intervals are grouped from native bars (bars.py).
NATIVE_CANDLE_SEC = 60

# Rolling window of native candles kept in memory (~ a full NSE session).
MAX_CANDLES_KEPT = 400

# Value area coverage.
VALUE_AREA_PCT = 0.70

# Stacked imbalance detection (engine rule, v1 — see analytics.py).
IMBALANCE_THRESHOLD = 3.0
MIN_STACK = 3

# Order book depth delivered by the feed (Angel One Snap Quote: best 5).
DEPTH_LEVELS = 5

# Stale-quote fallback threshold of the v1 classifier. Rests on a single
# observed example (verified trade 17, 14000 ms): a placeholder, not a
# calibrated parameter.
VTRENDER_STALE_QUOTE_MS = 14000
