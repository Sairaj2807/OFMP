"""Golden check over every recorded live session: re-classifying each stored
trade from its own recorded inputs (price, best bid/ask, prior price/side,
quote age), with the classifier version recorded on it, must reproduce the
side AND reason the live engine wrote at the time. Guards against the classifier drifting from what already sits on disk,
so historical classifications stay reproducible.

Reads whatever data/sessions/<date>/observations.jsonl exist; skips when
there are none (e.g. a fresh checkout). Baseline on 2026-09-30: 40,460
trades across 2026-09-22..30, all reproduced."""
import json

import pytest

import config
from observation_store import list_replayable_sessions, session_records_path
from backend.app.domain.orderflow import ClassificationContext, get_classifier

SESSIONS = list_replayable_sessions()


def _records(date_str):
    with open(session_records_path(date_str), encoding="utf-8") as f:
        for line in f:
            if line.strip():
                yield json.loads(line)


@pytest.mark.skipif(not SESSIONS, reason=f"no recorded sessions under {config.SESSIONS_DIR}")
@pytest.mark.parametrize("date_str", SESSIONS)
def test_stored_classifications_reproduce(date_str):
    classifiers = {}
    mismatches = []
    for obs in _records(date_str):
        # Each record is re-classified by the classifier version that wrote
        # it. Records from before classifier versioning carry no name/version;
        # all of them were produced by vtrender_reconstruction/v1 (the
        # midpoint rule went live before 2026-09-22, the first session).
        key = (obs.get("classifier_name", "vtrender_reconstruction"), obs.get("classifier_version", "v1"))
        classifier = classifiers.get(key) or classifiers.setdefault(key, get_classifier(*key))
        c = classifier.classify(ClassificationContext(
            price=obs["ltp"], best_bid=obs.get("best_bid"), best_ask=obs.get("best_ask"),
            prev_price=obs.get("prev_ltp"), prev_side=obs.get("prev_side"),
            quote_age_ms=obs.get("quote_age_ms")))
        if (c.side, c.method) != (obs["algo_side"], obs["algo_reason"]):
            mismatches.append(obs["trade_id"])
    assert not mismatches, f"{len(mismatches)} trade(s) no longer reproduce, e.g. {mismatches[:10]}"
