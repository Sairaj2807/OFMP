"""Alert rule kinds, parameter validation, and the value types the evaluator
works with. Pure domain code: no I/O, no clock, no database.

Two families of condition:

- Trade conditions are checked on every classified trade and fire on a
  CROSSING: the first trade seen after a rule is loaded only arms it (records
  which side of the level the market is on), and the rule fires when a later
  trade reaches the other side. A rule created while price is already above
  its level therefore waits for price to come back below and cross again,
  instead of firing at once on stale information.

    price_above / price_below    last traded price crosses `level`
    cvd_above / cvd_below        session CVD (closed + forming candles) crosses `level`

- Candle conditions are checked when a candle of `interval` seconds closes.
  A candle is known to be closed when the first trade of a later candle
  arrives (the same moment the chart shows it closed); nothing fires during
  a gap in trading.

    candle_delta_above           closed candle delta >= `level`
    candle_delta_below           closed candle delta <= `level`
    candle_volume_above          closed candle volume >= `level`
    stacked_imbalance            closed candle has a stacked imbalance on `side`
                                 (buy | sell | any), by the engine's own rule
                                 (threshold and minimum stack from settings.py)
    value_area_break             closed candle closes above the previous closed
                                 candle's value-area high (`side` up), below its
                                 value-area low (down), or either (any)

`mode` "once" disables a rule after it fires; "repeat" re-arms it, but it
cannot fire again within `cooldown_sec` of market time.
"""
import math
from dataclasses import dataclass, field
from typing import Optional

TRADE_KINDS = ("price_above", "price_below", "cvd_above", "cvd_below")
CANDLE_KINDS = ("candle_delta_above", "candle_delta_below", "candle_volume_above",
                "stacked_imbalance", "value_area_break")
KINDS = TRADE_KINDS + CANDLE_KINDS
MODES = ("once", "repeat")

MIN_COOLDOWN_SEC = 10
MAX_COOLDOWN_SEC = 24 * 3600
DEFAULT_COOLDOWN_SEC = 300
MAX_LEVEL = 1e12

_SIDES = {"stacked_imbalance": ("buy", "sell", "any"), "value_area_break": ("up", "down", "any")}


def validate_params(kind: str, params: dict, allowed_intervals: tuple) -> dict:
    """Returns the normalized params for `kind` or raises ValueError with a
    message fit for an API client. Unknown keys are rejected so a typo never
    silently becomes a rule that means something else."""
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {list(KINDS)}")
    if not isinstance(params, dict):
        raise ValueError("params must be an object")
    out: dict = {}
    expected = set()
    if kind in TRADE_KINDS or kind in ("candle_delta_above", "candle_delta_below", "candle_volume_above"):
        expected.add("level")
        level = params.get("level")
        if isinstance(level, bool) or not isinstance(level, (int, float)) or not math.isfinite(level) \
                or abs(level) > MAX_LEVEL:
            raise ValueError("params.level must be a finite number")
        if kind in ("price_above", "price_below") and level <= 0:
            raise ValueError("params.level must be a positive price")
        if kind == "candle_volume_above" and level <= 0:
            raise ValueError("params.level must be a positive volume")
        out["level"] = int(level) if kind != "price_above" and kind != "price_below" else float(level)
    if kind in CANDLE_KINDS:
        expected.add("interval")
        interval = params.get("interval")
        if isinstance(interval, bool) or interval not in allowed_intervals:
            raise ValueError(f"params.interval must be one of {list(allowed_intervals)}")
        out["interval"] = int(interval)
    if kind in _SIDES:
        expected.add("side")
        side = params.get("side", "any")
        if side not in _SIDES[kind]:
            raise ValueError(f"params.side must be one of {list(_SIDES[kind])}")
        out["side"] = side
    extra = set(params) - expected
    if extra:
        raise ValueError(f"unexpected params for {kind}: {sorted(extra)}")
    return out


def describe(kind: str, params: dict) -> str:
    """Short human description of a condition, e.g. 'price crosses above 24510.5'."""
    iv = params.get("interval")
    tf = f"{iv // 60}m" if iv else ""
    level = params.get("level")
    return {
        "price_above": f"price crosses above {level}",
        "price_below": f"price crosses below {level}",
        "cvd_above": f"CVD crosses above {level}",
        "cvd_below": f"CVD crosses below {level}",
        "candle_delta_above": f"{tf} candle closes with delta >= {level}",
        "candle_delta_below": f"{tf} candle closes with delta <= {level}",
        "candle_volume_above": f"{tf} candle closes with volume >= {level}",
        "stacked_imbalance": f"{tf} candle closes with a stacked {params.get('side')} imbalance",
        "value_area_break": f"{tf} candle closes outside the previous value area ({params.get('side')})",
    }[kind]


@dataclass(frozen=True)
class AlertRule:
    id: str
    owner_user_id: str
    name: str
    kind: str
    params: dict
    mode: str = "repeat"
    cooldown_sec: int = DEFAULT_COOLDOWN_SEC


@dataclass(frozen=True)
class TradeObservation:
    """One classified trade of the active contract."""
    ts_ms: int
    price: float
    cvd: int                 # session CVD including this trade


@dataclass(frozen=True)
class CandleClosed:
    """A closed candle of `interval_sec`, aggregated from native candles."""
    interval_sec: int
    time: int                # bucket start, epoch seconds
    open: float
    high: float
    low: float
    close: float
    delta: int
    volume: int
    buy_stacks: int          # stacked BUY imbalances (engine rule)
    sell_stacks: int
    value_area: Optional[tuple] = None        # this candle's (VAL, VAH)
    prev_value_area: Optional[tuple] = None   # previous closed candle's (VAL, VAH)

    @property
    def close_ms(self) -> int:
        return (self.time + self.interval_sec) * 1000


@dataclass(frozen=True)
class Firing:
    """A rule that fired. `dedup_key` is stable for the same market event, so
    two processes evaluating the same trade produce the same key (the store
    keeps one row per (rule, dedup_key))."""
    rule_id: str
    owner_user_id: str
    rule_name: str
    kind: str
    ts_ms: int
    dedup_key: str
    message: str
    value: float
    details: dict = field(default_factory=dict)
