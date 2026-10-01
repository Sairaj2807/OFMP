"""Evaluates alert rules against the live engine's output.

AlertEvaluator holds the loaded rules and each rule's small state (which side
of its level the market was on, when it last fired). Time is market time
(trade / candle timestamps), never the wall clock, so the same tape always
produces the same firings — the tests replay a tape and assert exactly that.

CandleCloseDetector turns "a new native candle opened" into CandleClosed
values for every interval some rule needs, grouping native candles exactly
as the chart does (epoch-aligned buckets, see bars.py), and running the
engine's own analytics on the merged rows (analytics.py)."""
from typing import Iterable, Optional

from backend.app.domain.orderflow.analytics import (poc_from_rows, stacked_imbalances_from_rows,
                                                    value_area_from_rows)
from backend.app.domain.orderflow.settings import IMBALANCE_THRESHOLD, MIN_STACK, VALUE_AREA_PCT

from .rules import CANDLE_KINDS, TRADE_KINDS, AlertRule, CandleClosed, Firing, TradeObservation, describe


class _RuleState:
    __slots__ = ("rule", "above", "last_fired_ms", "done")

    def __init__(self, rule: AlertRule):
        self.rule = rule
        self.above: Optional[bool] = None   # trade rules: market side of the level at the last trade
        self.last_fired_ms: Optional[int] = None
        self.done = False                   # "once" rule that has fired


class AlertEvaluator:
    def __init__(self, rules: Iterable[AlertRule] = ()):
        self._states: dict = {}
        self.set_rules(rules)

    # -- rule set ----------------------------------------------------------------

    def set_rules(self, rules: Iterable[AlertRule]) -> None:
        """Replaces the rule set. A rule whose definition is unchanged keeps
        its state (so a reload never re-fires or re-arms it)."""
        new = {}
        for r in rules:
            old = self._states.get(r.id)
            new[r.id] = old if old is not None and old.rule == r else _RuleState(r)
        self._states = new

    def upsert(self, rule: AlertRule) -> None:
        old = self._states.get(rule.id)
        if old is None or old.rule != rule:
            self._states[rule.id] = _RuleState(rule)

    def remove(self, rule_id: str) -> None:
        self._states.pop(rule_id, None)

    def rules(self) -> list:
        return [s.rule for s in self._states.values()]

    def has_rules(self) -> bool:
        return bool(self._states)

    def reset_market(self) -> None:
        """Forget which side of each level the market was on (new contract):
        trade rules re-arm on the next trade instead of comparing across instruments."""
        for s in self._states.values():
            s.above = None

    def candle_intervals(self) -> set:
        return {s.rule.params["interval"] for s in self._states.values()
                if s.rule.kind in CANDLE_KINDS and not s.done}

    # -- evaluation --------------------------------------------------------------

    def _may_fire(self, s: _RuleState, ts_ms: int) -> bool:
        if s.done:
            return False
        return s.last_fired_ms is None or ts_ms - s.last_fired_ms >= s.rule.cooldown_sec * 1000

    def _fire(self, s: _RuleState, ts_ms: int, dedup_key: str, value: float, details: dict) -> Firing:
        s.last_fired_ms = ts_ms
        if s.rule.mode == "once":
            s.done = True
        r = s.rule
        return Firing(rule_id=r.id, owner_user_id=r.owner_user_id, rule_name=r.name, kind=r.kind, ts_ms=ts_ms,
                      dedup_key=dedup_key, message=f"{r.name}: {describe(r.kind, r.params)}", value=value,
                      details=details)

    def on_trade(self, t: TradeObservation) -> list:
        out = []
        for s in self._states.values():
            kind = s.rule.kind
            if kind not in TRADE_KINDS:
                continue
            value = t.price if kind.startswith("price") else t.cvd
            level = s.rule.params["level"]
            if kind.endswith("_above"):
                now_above = value >= level
                crossed = s.above is False and now_above
            else:
                now_above = value > level
                crossed = s.above is True and not now_above
            s.above = now_above
            if crossed and self._may_fire(s, t.ts_ms):
                out.append(self._fire(s, t.ts_ms, f"trade:{t.ts_ms}", value,
                                      {"price": t.price, "cvd": t.cvd, "level": level}))
        return out

    def on_candle(self, c: CandleClosed) -> list:
        out = []
        for s in self._states.values():
            r = s.rule
            if r.kind not in CANDLE_KINDS or r.params["interval"] != c.interval_sec:
                continue
            hit, value = _candle_hit(r, c)
            if hit and self._may_fire(s, c.close_ms):
                out.append(self._fire(s, c.close_ms, f"candle:{c.interval_sec}:{c.time}", value, {
                    "candle_time": c.time, "interval": c.interval_sec, "open": c.open, "high": c.high,
                    "low": c.low, "close": c.close, "delta": c.delta, "volume": c.volume,
                    "buy_stacks": c.buy_stacks, "sell_stacks": c.sell_stacks,
                    "prev_value_area": list(c.prev_value_area) if c.prev_value_area else None}))
        return out


def _candle_hit(r: AlertRule, c: CandleClosed) -> tuple:
    p = r.params
    if r.kind == "candle_delta_above":
        return c.delta >= p["level"], c.delta
    if r.kind == "candle_delta_below":
        return c.delta <= p["level"], c.delta
    if r.kind == "candle_volume_above":
        return c.volume >= p["level"], c.volume
    if r.kind == "stacked_imbalance":
        n = {"buy": c.buy_stacks, "sell": c.sell_stacks, "any": c.buy_stacks + c.sell_stacks}[p["side"]]
        return n > 0, n
    if r.kind == "value_area_break":
        if c.prev_value_area is None:
            return False, c.close
        val, vah = c.prev_value_area
        up, down = c.close > vah, c.close < val
        return {"up": up, "down": down, "any": up or down}[p["side"]], c.close
    raise ValueError(r.kind)


class CandleCloseDetector:
    """Feed it every trade's native candle timestamp (Footprint buckets);
    when a later native candle opens, it returns the CandleClosed values for
    each requested interval whose bucket ended."""

    def __init__(self):
        self._last_native: Optional[int] = None
        self._prev_va: dict = {}     # interval -> value area of its last closed candle

    def reset(self) -> None:
        self._last_native = None
        self._prev_va.clear()

    def on_trade(self, footprint, native_ts: int, intervals: Iterable[int]) -> list:
        prev = self._last_native
        if prev is not None and native_ts <= prev:
            return []                   # same candle, or a late trade stamped earlier: nothing closed
        self._last_native = native_ts
        if prev is None:
            return []
        out = []
        for interval in sorted(set(intervals)):
            bucket = prev - prev % interval
            if native_ts - native_ts % interval == bucket:
                continue                                  # the coarser candle is still forming
            c = build_candle(footprint, bucket, interval, self._prev_va.get(interval))
            if c is not None:
                self._prev_va[interval] = c.value_area
                out.append(c)
        return out


def build_candle(footprint, bucket: int, interval: int, prev_value_area: Optional[tuple]) -> Optional[CandleClosed]:
    natives = sorted(ts for ts in footprint.data if bucket <= ts < bucket + interval)
    natives = [ts for ts in natives if ts in footprint.candle_ohlc]
    if not natives:
        return None
    rows = footprint.merged_candle_rows(natives)
    stacks = stacked_imbalances_from_rows(rows, IMBALANCE_THRESHOLD, MIN_STACK)
    first, last = footprint.candle_ohlc[natives[0]], footprint.candle_ohlc[natives[-1]]
    va = value_area_from_rows(rows, poc_from_rows(rows), VALUE_AREA_PCT)
    return CandleClosed(
        interval_sec=interval, time=bucket, open=first["open"], close=last["close"],
        high=max(footprint.candle_ohlc[ts]["high"] for ts in natives),
        low=min(footprint.candle_ohlc[ts]["low"] for ts in natives),
        delta=sum(r.buy_volume - r.sell_volume for r in rows),
        volume=sum(r.buy_volume + r.sell_volume for r in rows),
        buy_stacks=sum(1 for s in stacks if s[0][0] == "BUY_IMBALANCE"),
        sell_stacks=sum(1 for s in stacks if s[0][0] == "SELL_IMBALANCE"),
        value_area=tuple(va) if va else None, prev_value_area=prev_value_area)
