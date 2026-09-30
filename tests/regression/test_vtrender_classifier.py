"""Regression guard for the validated trade classifier (see
VTRENDERS_RECONSTRUCTED_ALGORITHM.md). Any change to TradeClassifier.classify
that alters a single verified trade's output fails here.

Baseline, measured 2026-09-30: 54/55 against the hand-verified labels; the
only miss is trade 25, the zero-tick case the classifier's docstring
documents as intentionally unhandled. Trade 17 is the one row decided by the
stale-quote fallback.

Each row is classified in isolation, seeded with that row's recorded prior
state (prev_ltp, prev_side) and quote age — the same inputs the live engine
had at that instant."""
import csv
from pathlib import Path

import pytest

from orderbook_engine import TradeClassifier

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "verified_trades" / "verified_dataset.csv"

# Pinned output of the current classifier for trade_id 1..55, B = BUY, S = SELL.
EXPECTED_SIDES = "BSBBBBSBSSBBBBBBBBSSBBSBBSBBBBBSSBBBBBBBSSBSBSBBSBBBBBB"
STALE_FALLBACK_TRADES = {17}
KNOWN_MISSES = {25}


def _num(v):
    return float(v) if v not in ("", None) else None


def _rows():
    with FIXTURE.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _classify(row):
    c = TradeClassifier()
    c.last_price = _num(row["prev_ltp"])
    c.last_side = row["prev_side"] or None
    side = c.classify(float(row["ltp"]), _num(row["best_bid"]), _num(row["best_ask"]),
                      _num(row["quote_age_ms"]))
    return side, c.last_reason


ROWS = _rows()


def test_fixture_is_the_55_row_verified_set():
    assert len(ROWS) == 55
    assert [int(r["trade_id"]) for r in ROWS] == list(range(1, 56))


@pytest.mark.parametrize("row", ROWS, ids=lambda r: f"trade{r['trade_id']}")
def test_each_verified_trade_keeps_its_pinned_side_and_reason(row):
    i = int(row["trade_id"])
    side, reason = _classify(row)
    assert side[0] == EXPECTED_SIDES[i - 1]
    if i in STALE_FALLBACK_TRADES:
        assert reason == "Tick Rule (stale-quote fallback)"
    else:
        assert reason == "Midpoint Rule (VTRenders)"


def test_accuracy_against_verified_labels_is_54_of_55_missing_only_trade_25():
    misses = {int(r["trade_id"]) for r in ROWS if _classify(r)[0] != r["verified_side"]}
    assert misses == KNOWN_MISSES
