"""Market Profile engine: the hand-worked examples from the Market Profile
notes (TPO letters, value area), period high-low marking, initial balance,
single prints, sessions, and parity with the footprint's volume figures."""
from datetime import datetime, timedelta, timezone

import pytest

import orderbook_engine as oe
from backend.app.domain.orderflow import value_area_from_rows
from backend.app.domain.profile import (ALL_DAY_SESSION, NSE_SESSION, SessionProfile, build_profile, poc_index,
                                        value_area)
from helpers import TICK, synthetic_ticks

IST = timezone(timedelta(hours=5, minutes=30))


def ms(h, m, s=0, day=29):
    return int(datetime(2026, 9, day, h, m, s, tzinfo=IST).timestamp() * 1000)


def letters(result):
    return {r["price"]: r["letters"] for r in result["rows"]}


def test_tpo_letters_follow_the_worked_example():
    """A trades 24500-24515, B 24505-24520, C 24510-24525 (5-point rows)."""
    p = SessionProfile(0.05)
    for t, price in [((9, 16), 24500), ((9, 30), 24515), ((9, 50), 24505), ((10, 5), 24520),
                     ((10, 20), 24510), ((10, 40), 24525)]:
        assert p.add_trade(ms(*t), price, 65, "BUY")
    r = build_profile(p, 5)
    assert letters(r) == {24525: "C", 24520: "BC", 24515: "ABC", 24510: "ABC", 24505: "AB", 24500: "A"}
    assert [x["price"] for x in r["rows"]] == sorted(letters(r), reverse=True)   # ladder, high to low
    assert r["stats"]["tpo_total"] == 12 and r["current_period"] == 2


def test_period_marks_its_whole_high_low_range_once():
    """A jump from 24500 to 24520 inside one period still marks the rows between,
    and revisiting a price in the same period does not add another letter."""
    p = SessionProfile(0.05)
    for price in (24500, 24520, 24500, 24520):
        p.add_trade(ms(9, 20), price, 1, "SELL")
    r = build_profile(p, 5)
    assert letters(r) == {24520: "A", 24515: "A", 24510: "A", 24505: "A", 24500: "A"}
    assert {x["price"]: x["volume"] for x in r["rows"]} == {24520: 2, 24515: 0, 24510: 0, 24505: 0, 24500: 2}


def test_value_area_matches_the_worked_example():
    """Rows 24500..24540 with TPO counts 1,2,4,7,9,6,5,3,1: POC 24520; 70% of 38
    is 26.6, reached at 24515-24530 (9 -> +7 -> +6 -> +5 = 27)."""
    weights = [1, 2, 4, 7, 9, 6, 5, 3, 1]
    poc = poc_index(weights)
    assert poc == 4 and value_area(weights, poc) == (3, 6)


def test_poc_ties_go_to_the_middle_then_lower():
    assert poc_index([5, 1, 5, 1, 5]) == 2
    assert poc_index([5, 5, 1, 1]) == 1            # middle is 1.5: indexes 1 and 2 tie, 2 has 1 -> index 1
    assert poc_index([0, 0]) is None


def test_volume_value_area_rule_is_the_footprints():
    """Same weights, same answer as orderflow.value_area_from_rows (tie expands downward)."""
    class Row:
        def __init__(self, price, vol):
            self.price, self.buy_volume, self.sell_volume = price, vol, 0
    vols = [3, 8, 8, 20, 9, 9, 4, 1]
    rows = [Row(100 + i, v) for i, v in enumerate(vols)]
    lo, hi = value_area_from_rows(rows, 103, 0.7)
    assert value_area(vols, poc_index(vols)) == (lo - 100, hi - 100)


def test_initial_balance_extensions_and_single_prints():
    p = SessionProfile(0.05)
    trades = [((9, 20), 100), ((9, 40), 110), ((9, 50), 105), ((10, 10), 112),     # A, B: IB 100-112
              ((10, 20), 112), ((10, 30), 140),                                    # C runs up: 115-135 single
              ((10, 50), 140), ((11, 10), 150)]                                    # D above
    for t, price in trades:
        p.add_trade(ms(*t), price, 10, "BUY")
    r = build_profile(p, 5)
    ib = r["initial_balance"]
    assert (ib["high"], ib["low"], ib["range"], ib["final"]) == (112, 100, 12, True)
    assert (ib["extension_up"], ib["extension_down"]) == (38, 0)
    assert r["single_prints"] == [{"low": 115, "high": 135, "letter": "C"}]
    assert r["tails"] == [{"low": 100, "high": 100, "letter": "A", "kind": "buying"}]   # only A traded at 100
    # the period in progress (D) is never reported as single prints
    assert all(sp["letter"] != "D" for sp in r["single_prints"])


def test_initial_balance_is_provisional_during_the_first_hour():
    p = SessionProfile(0.05)
    p.add_trade(ms(9, 20), 100, 1, "BUY")
    p.add_trade(ms(9, 50), 104, 1, "BUY")
    ib = build_profile(p, 1)["initial_balance"]
    assert (ib["high"], ib["low"], ib["final"], ib["extension_up"]) == (104, 100, False, None)


def test_session_boundaries_and_rollover_keep_the_previous_day():
    p = SessionProfile(0.05)
    assert not p.add_trade(ms(9, 14, 59), 100, 1, "BUY")           # before the open
    assert not p.add_trade(ms(15, 30), 100, 1, "BUY")              # close is exclusive
    assert p.add_trade(ms(15, 29, 59), 100, 1, "BUY")
    assert build_profile(p, 1)["periods"][-1]["letter"] == "M"     # 15:15-15:30
    p.add_trade(ms(9, 20, day=30), 200, 5, "SELL")                 # next session
    r = build_profile(p, 1)
    assert r["date"] == "2026-09-30" and r["stats"]["open"] == 200
    assert r["previous"]["date"] == "2026-09-29" and r["previous"]["poc_tpo"] == 100
    assert not p.add_trade(ms(10, 0, day=29), 50, 1, "BUY")        # late trade from the earlier session


def test_all_day_session_for_the_development_feed():
    p = SessionProfile(0.1, ALL_DAY_SESSION)
    assert p.add_trade(ms(2, 40), 100, 1, "BUY") and p.add_trade(ms(23, 59), 101, 1, "BUY")
    assert [x["letter"] for x in build_profile(p, 1)["periods"]] == ["F", "v"]   # 02:30-03:00, 23:30-24:00
    assert ALL_DAY_SESSION.periods == 48 and NSE_SESSION.periods == 13


def test_volume_at_price_equals_the_footprint_on_a_real_shaped_session():
    """Fed the same classified trades, volume, buy, sell per row and the volume
    POC equal what the footprint aggregated, at 1-point rows."""
    eng, prof = oe.TickProcessorState(TICK), SessionProfile(TICK)
    for t in synthetic_ticks(n_ticks=3000, seed=21):
        res = oe.process_tick(eng, t)
        if res.get("new_trade"):
            assert prof.add_trade(t["ltt"], t["ltp"], res["qty"], res["side"])
    r = build_profile(prof, 1)
    fp_rows = eng.footprint.merged_candle_rows(sorted(eng.footprint.data), ppr=1)
    fp = {row.price: (row.buy_volume, row.sell_volume) for row in fp_rows}
    mine = {row["price"]: (row["buy"], row["sell"]) for row in r["rows"] if row["volume"]}
    assert mine == fp
    assert r["stats"]["volume"] == sum(b + s for b, s in fp.values())
    top = max(b + s for b, s in fp.values())
    assert sum(b + s for b, s in fp.values() if b + s == top) == top          # no tie on this tape
    assert r["poc_volume"] == next(p for p, (b, s) in fp.items() if b + s == top)
    assert r["stats"]["tpo_total"] == sum(len(x["letters"]) for x in r["rows"])


@pytest.mark.parametrize("row", [1, 2, 5, 10, 20, 50])
def test_any_row_size_conserves_volume_and_covers_the_range(row):
    p = SessionProfile(TICK)
    eng = oe.TickProcessorState(TICK)
    for t in synthetic_ticks(n_ticks=1500, seed=4):
        res = oe.process_tick(eng, t)
        if res.get("new_trade"):
            p.add_trade(t["ltt"], t["ltp"], res["qty"], res["side"])
    r = build_profile(p, row)
    assert sum(x["volume"] for x in r["rows"]) == r["stats"]["volume"]
    prices = [x["price"] for x in r["rows"]]
    assert prices == sorted(prices, reverse=True)
    assert all(round(a - b, 6) == r["row_size"] for a, b in zip(prices, prices[1:]))   # contiguous ladder
    assert prices[-1] <= r["stats"]["low"] and prices[0] + r["row_size"] > r["stats"]["high"]
    va = r["value_area_tpo"]
    assert va["low"] <= r["poc_tpo"] <= va["high"]
