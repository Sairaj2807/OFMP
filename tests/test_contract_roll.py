"""angel_client's auto-roll: parse_expiry_date, list_configured_futures,
resolve_configured_future. Synthetic rows give exact control over expiry
dates for boundary testing; a real-scrip-master check (gated on the cache
file existing) proves it against actual Angel data too."""
import json
from datetime import date
from pathlib import Path

import pytest

import angel_client as ac
import config

CACHE = Path(__file__).resolve().parent.parent / "instruments_cache.json"


def row(symbol, expiry, name="NIFTY", instrumenttype="FUTIDX", exch_seg="NFO",
       tick_size="10", lotsize="65", token=None):
    return {"symbol": symbol, "expiry": expiry, "name": name, "instrumenttype": instrumenttype,
            "exch_seg": exch_seg, "tick_size": tick_size, "lotsize": lotsize,
            "token": token or symbol}


# ---- parse_expiry_date ---------------------------------------------------------

@pytest.mark.parametrize("s,expected", [
    ("28AUG2026", date(2026, 8, 28)),
    ("29SEP2026", date(2026, 9, 29)),
    ("1JAN2027", date(2027, 1, 1)),      # single-digit day still parses
    ("  27oct2026  ", date(2026, 10, 27)),  # case/whitespace tolerant
])
def test_parse_expiry_date_valid(s, expected):
    assert ac.parse_expiry_date(s) == expected


@pytest.mark.parametrize("s", ["", None, "garbage", "2026AUG", "32AUG2026", "AUG2026XX"])
def test_parse_expiry_date_invalid_returns_none(s):
    assert ac.parse_expiry_date(s) is None


# ---- list_configured_futures ----------------------------------------------------

SYNTH_ROWS = [
    row("NIFTY27AUG26FUT", "27AUG2026"),
    row("NIFTY24SEP26FUT", "24SEP2026"),
    row("NIFTY29OCT26FUT", "29OCT2026"),
    row("NIFTY26NOV26FUT", "26NOV2026"),
    row("BANKNIFTY24SEP26FUT", "24SEP2026", name="BANKNIFTY"),   # wrong name
    row("NIFTY24SEP26CE", "24SEP2026", instrumenttype="OPTIDX"),  # wrong type
    row("NIFTY24SEP26FUT-BSE", "24SEP2026", exch_seg="BFO"),      # wrong exchange
    row("NIFTYGARBAGE", "not-a-date"),                            # unparseable expiry
]


def test_list_configured_futures_filters_and_sorts():
    out = ac.list_configured_futures(SYNTH_ROWS, as_of=date(2026, 9, 1))
    assert [r["tradingsymbol"] for r in out] == ["NIFTY24SEP26FUT", "NIFTY29OCT26FUT", "NIFTY26NOV26FUT"]
    # August already expired (as_of Sep 1st) and the wrong-name/type/exchange/unparseable
    # rows are excluded regardless of date


def test_list_configured_futures_excludes_expired_but_keeps_expiry_day_itself():
    out = ac.list_configured_futures(SYNTH_ROWS, as_of=date(2026, 8, 27))   # exactly August's expiry day
    symbols = [r["tradingsymbol"] for r in out]
    assert "NIFTY27AUG26FUT" in symbols       # still tradeable on its own expiry day
    out2 = ac.list_configured_futures(SYNTH_ROWS, as_of=date(2026, 8, 28))  # the very next day
    assert "NIFTY27AUG26FUT" not in [r["tradingsymbol"] for r in out2]


def test_list_configured_futures_rolls_forward_automatically_once_the_front_month_expires():
    """The exact bug this fixes: no code/config change needed between dates."""
    before = ac.list_configured_futures(SYNTH_ROWS, as_of=date(2026, 9, 1))[0]
    after = ac.list_configured_futures(SYNTH_ROWS, as_of=date(2026, 9, 25))  # day after Sep's expiry
    assert before["tradingsymbol"] == "NIFTY24SEP26FUT"
    assert after[0]["tradingsymbol"] == "NIFTY29OCT26FUT"


def test_list_configured_futures_expiry_month_filter():
    out = ac.list_configured_futures(SYNTH_ROWS, as_of=date(2026, 1, 1), expiry_month="OCT")
    assert [r["tradingsymbol"] for r in out] == ["NIFTY29OCT26FUT"]


def test_list_configured_futures_empty_when_nothing_matches():
    assert ac.list_configured_futures(SYNTH_ROWS, as_of=date(2026, 1, 1), expiry_month="FEB") == []
    assert ac.list_configured_futures(SYNTH_ROWS, as_of=date(2030, 1, 1)) == []   # everything expired


def test_list_configured_futures_returns_full_contract_shape():
    out = ac.list_configured_futures(SYNTH_ROWS, as_of=date(2026, 9, 1))
    c = out[0]
    assert set(c) == {"token", "tradingsymbol", "name", "expiry", "tick_size", "lotsize", "exch_seg"}
    assert c["tick_size"] == 0.1        # "10" paise -> 0.10 rupees
    assert c["lotsize"] == 65
    assert isinstance(c["tick_size"], float) and isinstance(c["lotsize"], int)


# ---- resolve_configured_future ---------------------------------------------------

def test_resolve_configured_future_picks_the_soonest(monkeypatch):
    monkeypatch.setattr(config, "INSTRUMENT_EXPIRY_MONTH", None)
    c = ac.resolve_configured_future(SYNTH_ROWS, as_of=date(2026, 9, 1))
    assert c["tradingsymbol"] == "NIFTY24SEP26FUT"


def test_resolve_configured_future_respects_a_pinned_month(monkeypatch):
    monkeypatch.setattr(config, "INSTRUMENT_EXPIRY_MONTH", "NOV")
    c = ac.resolve_configured_future(SYNTH_ROWS, as_of=date(2026, 9, 1))
    assert c["tradingsymbol"] == "NIFTY26NOV26FUT"


def test_resolve_configured_future_raises_with_an_informative_message_when_empty(monkeypatch):
    monkeypatch.setattr(config, "INSTRUMENT_EXPIRY_MONTH", None)
    with pytest.raises(RuntimeError, match="No unexpired NIFTY FUTIDX contract"):
        ac.resolve_configured_future([], as_of=date(2026, 1, 1))


def test_resolve_configured_future_error_message_names_the_pinned_month(monkeypatch):
    monkeypatch.setattr(config, "INSTRUMENT_EXPIRY_MONTH", "FEB")
    with pytest.raises(RuntimeError, match="expiring in FEB"):
        ac.resolve_configured_future(SYNTH_ROWS, as_of=date(2026, 9, 1))


# ---- against the real, current scrip master (if present) -------------------------

needs_cache = pytest.mark.skipif(not CACHE.exists(), reason="instruments_cache.json not on disk")


@needs_cache
def test_resolve_configured_future_on_real_cached_scrip_master():
    rows = json.loads(CACHE.read_text(encoding="utf-8"))
    c = ac.resolve_configured_future(rows)   # as_of defaults to today: proves it against the REAL current date
    assert c["tradingsymbol"].startswith("NIFTY") and c["tradingsymbol"].endswith("FUT")
    assert ac.parse_expiry_date(c["expiry"]) >= date.today()
    # every other unexpired NIFTY future must expire no sooner than the resolved one
    for other in ac.list_configured_futures(rows)[1:]:
        assert ac.parse_expiry_date(other["expiry"]) >= ac.parse_expiry_date(c["expiry"])
