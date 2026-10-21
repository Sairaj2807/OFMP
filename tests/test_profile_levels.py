"""Market profile levels, phase 1 (owner decisions 2026-10-21): VWAP / TWAP,
HVN bands at 70% of the VPOC row, IB extensions, the developing POC and its
trail, weak highs / lows, and the previous session's volume levels."""
from datetime import datetime, timedelta, timezone

from backend.app.domain.profile import SessionProfile, build_profile
from backend.app.domain.profile.analytics import hvn_bands, tpo_median

IST = timezone(timedelta(hours=5, minutes=30))


def ms(h, m, s=0, day=29):
    return int(datetime(2026, 9, day, h, m, s, tzinfo=IST).timestamp() * 1000)


def session(trades, day=29):
    p = SessionProfile(0.05)
    for (h, m), price, qty in trades:
        assert p.add_trade(ms(h, m, day=day), price, qty, "BUY")
    return p


def test_vwap_is_exact_and_twap_is_the_tpo_median():
    p = session([((9, 16), 24500.05, 100), ((9, 20), 24510.00, 300), ((9, 50), 24520.10, 100)])
    r = build_profile(p, 5)
    assert r["vwap"] == round((24500.05 * 100 + 24510 * 300 + 24520.10 * 100) / 500, 8)   # not row prices
    # TPOs low to high (A: 24500-24510, B: one trade at 24520): 24500 A, 24505 A, 24510 A, 24515 -, 24520 B
    assert [x["letters"] for x in reversed(r["rows"])] == ["A", "A", "A", "", "B"]
    assert r["twap"] == 24505                         # 2 of 4 TPOs at or below it
    assert tpo_median([1, 1, 1, 1]) == 1 and tpo_median([0, 0]) is None


def test_hvn_bands_are_runs_near_the_vpoc_volume_away_from_the_vpoc():
    vol = [10, 100, 30, 70, 75, 20, 69]
    #         VPOC        HVN run      69 < 70: not an HVN
    assert hvn_bands(vol, 1) == [(3, 4, 4)]
    assert hvn_bands([10, 100, 70, 10], 1) == []      # touching the VPOC: part of the POC area
    assert hvn_bands([0, 0], None) == []
    p = session([((9, 16), 24500, 1000), ((9, 17), 24520, 800), ((9, 18), 24510, 100)])
    r = build_profile(p, 5)
    assert r["poc_volume"] == 24500
    assert r["hvn_bands"] == [{"low": 24520, "high": 24520, "peak": 24520}] and r["hvn"] == [24520]


def test_ib_extensions_and_how_far_they_were_reached():
    p = session([((9, 16), 24500, 1), ((9, 50), 24510, 1),          # IB 24500-24510, range 10
                 ((10, 20), 24522, 1), ((10, 40), 24496, 1)])       # C: up 1.2 IB, D: down 0.4
    ext = build_profile(p, 1)["ib_extensions"]
    assert [e["price"] for e in ext["up"]] == [24515, 24520, 24530]
    assert [e["price"] for e in ext["down"]] == [24495, 24490, 24480]
    assert ext["reached_up"] == 2.0 and ext["reached_down"] is None
    forming = session([((9, 16), 24500, 1), ((9, 50), 24510, 1)])
    assert build_profile(forming, 1)["ib_extensions"] is None        # not before the IB is final


def test_developing_poc_moves_period_by_period():
    p = session([((9, 16), 24500, 100), ((9, 20), 24510, 50),       # A: POC 24500
                 ((9, 50), 24510, 120),                              # B: 24510 has 170 > 100
                 ((10, 20), 24500, 80)])                             # C: 24500 back to 180
    d = build_profile(p, 5)["dpoc"]
    assert [(t["letter"], t["price"]) for t in d["trail"]] == [("A", 24500), ("B", 24510), ("C", 24500)]
    assert d["price"] == 24500 == build_profile(p, 5)["poc_volume"]


def test_weak_extremes_have_flat_edges_without_tails():
    flat_top = session([((9, 16), 24500, 1), ((9, 20), 24520, 1),   # A 24500-24520
                        ((9, 50), 24510, 1), ((9, 55), 24520, 1),   # B 24510-24520: top row has A and B
                        ((10, 20), 24505, 1)])
    r = build_profile(flat_top, 5)
    assert r["weak_high"] is True and r["weak_low"] is False         # bottom row 24500: only A, a tail
    spike = session([((9, 16), 24500, 1), ((9, 20), 24510, 1), ((9, 50), 24505, 1), ((9, 55), 24530, 1),
                     ((10, 20), 24505, 1)])                           # B alone reaches 24515-24530: selling tail
    r = build_profile(spike, 5)
    assert r["weak_high"] is False and any(t["kind"] == "selling" for t in r["tails"])


def test_previous_session_carries_volume_levels_and_its_range():
    p = session([((9, 16), 24500, 100), ((9, 50), 24520, 300)], day=28)
    p.add_trade(ms(9, 20, day=29), 24600, 1, "SELL")                  # a new day rolls the profile
    prev = build_profile(p, 5)["previous"]
    assert prev["poc_volume"] == 24520 and prev["vwap"] == 24515.0
    assert (prev["high"], prev["low"]) == (24520, 24500)
    assert prev["vah_volume"] is not None and prev["val_volume"] is not None
    assert p.previous.period_volume and not p.period_volume.get(1)   # per-period volume moved with the day


# ---- across sessions (phase 2): naked POCs, composite profile ----------------------------------

def day_profile(day, trades):
    return build_profile(session(trades, day=day), 5)


def test_naked_pocs_are_the_vpocs_no_later_session_traded_into():
    from backend.app.domain.profile.levels import naked_pocs
    d1 = day_profile(21, [((9, 16), 24500, 900), ((9, 50), 24540, 10)])   # VPOC 24500
    d2 = day_profile(22, [((9, 16), 24520, 900), ((9, 50), 24560, 10)])   # VPOC 24520; range 24520-24560
    d3 = day_profile(23, [((9, 16), 24600, 900), ((9, 50), 24610, 10)])   # VPOC 24600; range 24600-24610
    out = naked_pocs([("2026-09-21", d1), ("2026-09-22", d2), ("2026-09-23", d3)], 5)
    # d1's 24500 was never revisited (d2 starts at 24520); d2's 24520 neither (d3 stays above); d3 is the last day
    assert out == [{"date": "2026-09-21", "price": 24500, "sessions_ago": 3},
                   {"date": "2026-09-22", "price": 24520, "sessions_ago": 2},
                   {"date": "2026-09-23", "price": 24600, "sessions_ago": 1}]
    d4 = day_profile(24, [((9, 16), 24604, 10), ((9, 50), 24515, 900)])   # range 24515-24604, VPOC 24515
    out = naked_pocs([("2026-09-21", d1), ("2026-09-22", d2), ("2026-09-23", d3), ("2026-09-24", d4)], 5)
    # d4 traded through the 24600 and 24520 rows (visited); 24500 stays naked (d4's low 24515 is above its row)
    assert [(n["date"], n["price"]) for n in out] == [("2026-09-21", 24500), ("2026-09-24", 24515)]


def test_composite_merges_sessions_with_the_single_session_rules():
    from backend.app.domain.profile.levels import composite_profile
    d1 = day_profile(21, [((9, 16), 24500, 600), ((9, 50), 24510, 100)])
    d2 = day_profile(22, [((9, 16), 24510, 600), ((9, 50), 24520, 100)])
    c = composite_profile([("2026-09-21", d1), ("2026-09-22", d2)], 5, "5d")
    by = {r["price"]: r for r in c["rows"]}
    assert by[24510]["volume"] == 700 and by[24510]["tpo"] == d1_tpo(d1, 24510) + d1_tpo(d2, 24510)
    assert c["poc_volume"] == 24510 and c["stats"]["volume"] == 1400
    assert c["vwap"] == round((d1["vwap"] * 700 + d2["vwap"] * 700) / 1400, 8)
    assert (c["stats"]["low"], c["stats"]["high"]) == (24500, 24520)
    assert c["composite"] == {"label": "5d", "days": 2, "from": "2026-09-21", "to": "2026-09-22",
                              "dates": ["2026-09-21", "2026-09-22"]}
    assert [r["price"] for r in c["rows"]] == [24520, 24515, 24510, 24505, 24500]   # contiguous, high to low
    assert composite_profile([], 5, "5d") is None


def d1_tpo(p, price):
    return next(r["tpo"] for r in p["rows"] if r["price"] == price)


# ---- day structure (phase 3) --------------------------------------------------------------------

def test_shapes_from_the_tpo_distribution():
    from backend.app.domain.profile.analytics import poc_index, value_area
    from backend.app.domain.profile.structure import shape

    def label(tpo, ib=None, periods=6, row=5):
        poc = poc_index(tpo)
        return shape(tpo, poc, value_area(tpo, poc), ib, row, periods)
    assert label([1, 2, 4, 6, 4, 2, 1]) == "D"                     # bell, centred
    assert label([1, 1, 1, 2, 5, 7, 6, 4]) == "p"                  # fat top, thin lower tail
    assert label([4, 6, 7, 5, 2, 1, 1, 1]) == "b"                  # fat bottom, thin upper tail
    assert label([1, 2, 4, 6, 4, 2, 1], periods=3) is None          # too early in the day


def test_double_distribution_needs_a_thin_zone_between_two_peaks():
    from backend.app.domain.profile.structure import double_distribution
    dd = double_distribution([2, 5, 6, 5, 1, 1, 1, 4, 7, 5, 2])
    assert dd["thin"] == (4, 6)
    assert dd["lower"][0] <= 2 <= dd["lower"][1] and dd["upper"][0] <= 8 <= dd["upper"][1]
    assert double_distribution([2, 5, 6, 5, 3, 4, 7, 5, 2]) is None            # no thin zone (3 > 30% of 6)
    assert double_distribution([2, 5, 6, 5, 1, 4, 7, 5, 2]) is None            # one thin row is not a zone


def test_open_drive_up_and_down_and_a_failed_drive():
    from backend.app.domain.profile.structure import open_drive
    # (index, open, high, low, close)
    assert open_drive([(0, 100, 120, 99, 118), (1, 118, 130, 110, 128)]) == "up"
    assert open_drive([(0, 120, 121, 100, 102), (1, 102, 110, 90, 92)]) == "down"
    assert open_drive([(0, 100, 120, 99, 118), (1, 118, 130, 99, 128)]) is None    # B came back to the open
    assert open_drive([(0, 110, 120, 99, 118), (1, 118, 130, 112, 128)]) is None   # A opened mid-range
    assert open_drive([(0, 100, 120, 99, 118)]) is None


def test_value_area_rule_activates_after_two_periods_inside_and_tracks_the_target():
    from backend.app.domain.profile.structure import value_area_rule
    # previous volume VA rows 100..120 (VAH row 120 covers 120-125); opened above at 130
    per = [(0, 130, 132, 122, 123), (1, 123, 124, 112, 115), (2, 115, 118, 99, 101)]
    r = value_area_rule(130, per, 120, 100, 5)
    assert r["opened"] == "above" and r["active"] and r["activated"] == 1 and r["target"] == 100 and r["met"]
    r = value_area_rule(130, per[:2], 120, 100, 5)
    assert r["active"] and not r["met"]
    assert not value_area_rule(130, [(0, 130, 132, 122, 123), (1, 123, 135, 126, 130)], 120, 100, 5)["active"]
    assert value_area_rule(110, per, 120, 100, 5)["opened"] == "inside"
    assert value_area_rule(90, [(0, 90, 104, 89, 103), (1, 103, 110, 100, 108)], 120, 100, 5)["target"] == 120
    assert value_area_rule(130, per, None, None, 5) is None


def test_volume_clusters_and_absorption():
    from backend.app.domain.profile.structure import volume_clusters
    vol = [10, 70, 80, 100, 20, 65, 66, 10]
    buy = [5, 36, 41, 60, 10, 60, 60, 5]
    sell = [5, 34, 39, 40, 10, 5, 6, 5]
    periods = [[0], [0, 1, 2], [1, 2], [2, 3], [3], [4, 5], [5], [6]]
    out = volume_clusters(vol, buy, sell, periods, 3)
    assert out == [(1, 3, 250, 24, 4, True)]            # rows 5-6: only 2 periods touched them: not a cluster
    assert volume_clusters(vol, buy, sell, [[0, 1, 2, 3]] * 8, 3)[1][5] is False    # 120 buy vs 11 sell: directional
