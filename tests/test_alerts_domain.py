"""Alert rules and the evaluator (pure domain code): parameter validation,
crossing semantics, cooldown / once, candle conditions, and parity of the
candle-close detector with the chart's own bar grouping."""
import pytest

import orderbook_engine as oe
from backend.app.domain.alerts import (AlertEvaluator, AlertRule, CandleClosed, CandleCloseDetector,
                                       TradeObservation, validate_params)
from backend.app.domain.orderflow import group_native_bars
from helpers import TICK, synthetic_ticks

INTERVALS = (60, 180, 300, 900, 1800)


def rule(kind, params, mode="repeat", cooldown=10, rid="r1"):
    return AlertRule(id=rid, owner_user_id="u1", name=f"{kind} rule", kind=kind, params=params, mode=mode,
                     cooldown_sec=cooldown)


def trade(ts_s, price, cvd=0):
    return TradeObservation(ts_ms=ts_s * 1000, price=price, cvd=cvd)


# -- validation -----------------------------------------------------------------------

@pytest.mark.parametrize("kind,params,expected", [
    ("price_above", {"level": 24500}, {"level": 24500.0}),
    ("cvd_below", {"level": -15000.0}, {"level": -15000}),
    ("candle_delta_above", {"level": 5000, "interval": 300}, {"level": 5000, "interval": 300}),
    ("stacked_imbalance", {"interval": 60}, {"interval": 60, "side": "any"}),
    ("value_area_break", {"interval": 900, "side": "up"}, {"interval": 900, "side": "up"}),
])
def test_valid_params_are_normalized(kind, params, expected):
    assert validate_params(kind, params, INTERVALS) == expected


@pytest.mark.parametrize("kind,params,fragment", [
    ("nope", {}, "kind must be one of"),
    ("price_above", {}, "level must be a finite number"),
    ("price_above", {"level": float("nan")}, "finite"),
    ("price_above", {"level": True}, "finite"),
    ("price_below", {"level": -1}, "positive price"),
    ("candle_volume_above", {"level": 0, "interval": 60}, "positive volume"),
    ("candle_delta_above", {"level": 10, "interval": 120}, "interval must be one of"),
    ("stacked_imbalance", {"interval": 60, "side": "up"}, "side must be one of"),
    ("price_above", {"level": 1, "interval": 60}, "unexpected params"),
])
def test_invalid_params_are_rejected(kind, params, fragment):
    with pytest.raises(ValueError, match=fragment):
        validate_params(kind, params, INTERVALS)


# -- trade conditions -----------------------------------------------------------------

def test_price_rule_arms_on_first_trade_and_fires_on_crossing_only():
    ev = AlertEvaluator([rule("price_above", {"level": 100.0})])
    assert ev.on_trade(trade(1, 101)) == []          # already above when loaded: arms, never fires on stale state
    assert ev.on_trade(trade(2, 99)) == []
    fired = ev.on_trade(trade(3, 100))               # reaching the level counts as crossing above
    assert len(fired) == 1
    f = fired[0]
    assert (f.rule_id, f.kind, f.value, f.dedup_key) == ("r1", "price_above", 100, "trade:3000")
    assert "price crosses above 100.0" in f.message
    assert ev.on_trade(trade(4, 105)) == []          # staying above is not a new crossing


def test_cooldown_is_market_time_and_once_disables():
    ev = AlertEvaluator([rule("price_below", {"level": 50.0}, cooldown=60)])
    ev.on_trade(trade(0, 51))
    assert len(ev.on_trade(trade(1, 49))) == 1
    ev.on_trade(trade(2, 51))
    assert ev.on_trade(trade(30, 49)) == []          # crossed again inside the cooldown
    ev.on_trade(trade(40, 51))
    assert len(ev.on_trade(trade(61, 50))) == 1      # 60 s later; equal to the level counts as below

    once = AlertEvaluator([rule("cvd_above", {"level": 1000}, mode="once")])
    for i, cvd in enumerate((0, 1200, 0, 1500)):
        fired = once.on_trade(trade(i * 100, 1.0, cvd))
        assert len(fired) == (1 if i == 1 else 0)
    assert once.candle_intervals() == set()


def test_reload_keeps_state_of_unchanged_rules_and_reset_market_rearms():
    r = rule("price_above", {"level": 100.0})
    ev = AlertEvaluator([r])
    ev.on_trade(trade(1, 99))
    ev.set_rules([r])                                # unchanged definition: still armed below
    assert len(ev.on_trade(trade(2, 101))) == 1
    ev.on_trade(trade(100, 99))
    ev.reset_market()                                # new contract: the next trade only re-arms
    assert ev.on_trade(trade(200, 101)) == []
    changed = rule("price_above", {"level": 200.0})
    ev.set_rules([changed])
    assert ev.rules() == [changed]
    ev.remove("r1")
    assert not ev.has_rules()


# -- candle conditions ----------------------------------------------------------------

def candle(delta=0, volume=10, buy=0, sell=0, close=100.0, prev_va=None, t=600, interval=300):
    return CandleClosed(interval_sec=interval, time=t, open=100, high=101, low=99, close=close, delta=delta,
                        volume=volume, buy_stacks=buy, sell_stacks=sell, prev_value_area=prev_va)


def test_candle_conditions():
    ev = AlertEvaluator([
        rule("candle_delta_above", {"level": 500, "interval": 300}, rid="d"),
        rule("candle_volume_above", {"level": 1000, "interval": 300}, rid="v"),
        rule("stacked_imbalance", {"interval": 300, "side": "sell"}, rid="s"),
        rule("value_area_break", {"interval": 300, "side": "down"}, rid="va"),
        rule("candle_delta_below", {"level": -500, "interval": 60}, rid="other_tf"),
    ])
    assert ev.candle_intervals() == {60, 300}
    fired = {f.rule_id: f for f in ev.on_candle(candle(delta=600, volume=1500, sell=1, close=98.0,
                                                       prev_va=(99.0, 102.0)))}
    assert set(fired) == {"d", "v", "s", "va"}
    assert fired["d"].dedup_key == "candle:300:600" and fired["d"].ts_ms == 900_000
    assert fired["va"].details["prev_value_area"] == [99.0, 102.0]
    assert ev.on_candle(candle(delta=-600, t=1200)) == []           # 5m rules: none hit; 1m rule: other interval
    assert [f.rule_id for f in ev.on_candle(candle(delta=-600, t=1260, interval=60))] == ["other_tf"]
    assert ev.on_candle(candle(close=200.0, prev_va=None, t=1500)) == []   # no previous value area yet


# -- detector parity with the chart ---------------------------------------------------

def test_detector_closes_match_the_chart_bars_and_fire_deterministically():
    """Every candle the detector reports closed equals the chart's grouped bar
    for that bucket (delta, volume, OHLC), and the same tape gives the same firings."""
    def run():
        eng = oe.TickProcessorState(TICK)
        det, ev = CandleCloseDetector(), AlertEvaluator([
            rule("candle_delta_above", {"level": 1, "interval": 300}, cooldown=10),
            rule("stacked_imbalance", {"interval": 60, "side": "any"}, cooldown=10, rid="si"),
            rule("price_above", {"level": 24000.0}, cooldown=10, rid="px"),
        ])
        closed, fired = [], []
        for t in synthetic_ticks(n_ticks=3000, seed=11):
            res = oe.process_tick(eng, t)
            if not res.get("new_trade"):
                continue
            cs = det.on_trade(eng.footprint, eng.last_candle_seen, ev.candle_intervals() | {900})
            closed.extend(cs)
            for c in cs:
                fired.extend(ev.on_candle(c))
            fired.extend(ev.on_trade(TradeObservation(t["ltt"], t["ltp"], 0)))
        return eng, closed, fired

    eng, closed, fired = run()
    assert {c.interval_sec for c in closed} == {60, 300, 900}
    fp = eng.footprint
    native = [{"time": ts, **fp.candle_ohlc[ts], "delta": fp.candle_delta(ts),
               "cells": [{"price": r.price, "buy": r.buy_volume, "sell": r.sell_volume}
                         for r in fp.get_candle_rows(ts)]} for ts in sorted(fp.data)]
    for interval in (60, 300, 900):
        bars = {b["time"]: b for b in group_native_bars(native, interval)}
        mine = [c for c in closed if c.interval_sec == interval]
        assert mine and len(mine) == len(bars) - 1                       # all but the still-forming one
        for c in mine:
            b = bars[c.time]
            vol = sum(x["buy"] + x["sell"] for x in b["cells"])
            assert (c.delta, c.volume, c.open, c.high, c.low, c.close) == \
                   (b["delta"], vol, b["open"], b["high"], b["low"], b["close"])
        for prev, cur in zip(mine, mine[1:]):
            assert cur.prev_value_area == prev.value_area
    assert fired and [(f.rule_id, f.dedup_key) for f in fired] == [(f.rule_id, f.dedup_key) for f in run()[2]]
