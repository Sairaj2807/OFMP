"""Golden check over every recorded live session: re-classifying each stored
trade from its own recorded inputs (price, best bid/ask, prior price/side,
quote age) must reproduce the side AND reason the live engine wrote at the
time. Guards against the classifier drifting from what already sits on disk,
so historical classifications stay reproducible.

Reads whatever data/sessions/<date>/observations.jsonl exist; skips when
there are none (e.g. a fresh checkout). Baseline on 2026-09-30: 40,460
trades across 2026-09-22..30, all reproduced."""
import json

import pytest

import config
from observation_store import list_replayable_sessions, session_records_path
from orderbook_engine import TradeClassifier

SESSIONS = list_replayable_sessions()


def _records(date_str):
    with open(session_records_path(date_str), encoding="utf-8") as f:
        for line in f:
            if line.strip():
                yield json.loads(line)


@pytest.mark.skipif(not SESSIONS, reason=f"no recorded sessions under {config.SESSIONS_DIR}")
@pytest.mark.parametrize("date_str", SESSIONS)
def test_stored_classifications_reproduce(date_str):
    mismatches = []
    for obs in _records(date_str):
        c = TradeClassifier()
        c.last_price = obs.get("prev_ltp")
        c.last_side = obs.get("prev_side")
        side = c.classify(obs["ltp"], obs.get("best_bid"), obs.get("best_ask"), obs.get("quote_age_ms"))
        if (side, c.last_reason) != (obs["algo_side"], obs["algo_reason"]):
            mismatches.append(obs["trade_id"])
    assert not mismatches, f"{len(mismatches)} trade(s) no longer reproduce, e.g. {mismatches[:10]}"
