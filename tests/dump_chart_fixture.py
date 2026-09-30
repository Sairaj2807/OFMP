"""Prints, as JSON, the chart payload the server would send for the synthetic
Angel session (helpers.synthetic_ticks) plus the engine's own POC / value area / stacked imbalances per candle.
Used by tests/js/adapter.test.mjs to compare the engine with the chart library's
Footprint primitive. Usage: python -B tests/dump_chart_fixture.py [ppr]"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import server  # noqa: E402
from helpers import TICK, feed_engine, synthetic_ticks  # noqa: E402

ppr = int(sys.argv[1]) if len(sys.argv) > 1 else 2
eng = feed_engine(synthetic_ticks())
fp = eng.footprint


def payload(limit):
    return server._chart_payload(fp, eng.cvd_tracker.cvd, eng.last_candle_seen, ppr, limit=limit)


full = payload(10_000)
out = {
    "ppr": ppr,
    "tick": TICK,
    "full": full,
    "last10": payload(10),
    "engine": {
        str(ts): {
            "poc": fp.poc(ts, ppr),
            "value_area": list(fp.value_area(ts, ppr=ppr)),
            "stacks": [[[k, p] for k, p in stack] for stack in fp.stacked_imbalances(ts, ppr=ppr)],
        }
        for ts in sorted(fp.data)
    },
}
json.dump(out, sys.stdout)
