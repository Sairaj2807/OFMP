"""Engine-wide golden output. Seeded tick streams (including crossed books,
volume resets, one-sided books and stale quotes) run through process_tick,
and everything the engine produces is hashed: per-tick results, the
observation records, the chart and footprint-table payloads at several
price-per-row / interval settings, and the replay state rebuilt from the
observations.

The digests were pinned against the pre-refactor engine (commit bc5f113).
A change to any of them means engine output changed — which must only ever
happen together with a new algorithm version, never as a side effect."""
import hashlib
import json
import random

import pytest

import replay_engine
import server
from backend.app.domain.orderflow.table import candle_table
from helpers import TICK, synthetic_ticks
import orderbook_engine as oe

# Fields added to observation records after the baseline (additive metadata,
# not engine output). Excluded from the digest so the pinned values stay
# comparable with the baseline.
ADDED_OBSERVATION_FIELDS = {"classifier_name", "classifier_version"}


def edge_ticks(seed):
    """synthetic_ticks plus injected edge cases the plain random walk never
    produces."""
    rng = random.Random(seed)
    ticks = synthetic_ticks(n_ticks=3000, seed=seed)
    out = []
    for i, t in enumerate(ticks):
        t = dict(t)
        r = rng.random()
        if r < 0.01:                                   # crossed book
            bid = t["depth_sell"][0][0] + TICK
            t["depth_buy"] = [(round(bid - k * TICK, 1), 65, 1) for k in range(5)]
        elif r < 0.015:                                # cumulative volume reset
            t["cum_volume"] = max(0, t["cum_volume"] - 10_000)
        elif r < 0.025:                                # one-sided book
            t["depth_sell"] = []
        out.append(t)
        if i % 500 == 250:                             # stale-quote stretch: same book, time moves on
            for k in range(1, 4):
                s = dict(t)
                s["ltt"] = t["ltt"] + k * 8000
                s["cum_volume"] = t["cum_volume"] + k * 65
                s["ltp"] = round(t["ltp"] + rng.choice((-1, 0, 1)) * TICK, 1)
                out.append(s)
    # ensure time never goes backwards after the stale stretches
    last = 0
    for t in out:
        t["ltt"] = max(t["ltt"], last)
        last = t["ltt"]
    return out


def _digest(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()


def engine_outputs(seed):
    eng = oe.TickProcessorState(TICK)
    observations, results = [], []

    def sink(obs):
        observations.append({k: v for k, v in obs.items() if k not in ADDED_OBSERVATION_FIELDS})

    for t in edge_ticks(seed):
        results.append(oe.process_tick(eng, t, observation_sink=sink))

    fp = eng.footprint
    payloads = {}
    for ppr in (1, 2, 5):
        for interval in (60, 300, 1800):
            payloads[f"chart_{ppr}_{interval}"] = server._chart_payload(
                fp, eng.cvd_tracker.cvd, eng.last_candle_seen, ppr, interval_sec=interval, limit=10_000)
            payloads[f"table_{ppr}_{interval}"] = candle_table(
                fp, eng.cvd_by_candle, ppr, interval_sec=interval, limit=10_000)

    for i, o in enumerate(observations):
        o["trade_id"] = i + 1
    rs = replay_engine.build_replay_state(observations, TICK)
    replay = server._chart_payload(rs.footprint, rs.cvd_tracker.cvd, rs.last_candle_seen, 1, limit=10_000)

    return {
        "results": _digest(results),
        "observations": _digest(observations),
        "payloads": _digest(payloads),
        "replay": _digest(replay),
        "cvd": eng.cvd_tracker.cvd,
        "tick_count": eng.tick_count,
        "trades": len(observations),
    }


GOLDEN = {
    1: {
        "results": "08e2bd2a8a543130a2d49fc6b7f5ceaa42387166888a25cbafa167ddd17567af",
        "observations": "d4a1587c2b3001ba6913f66fd897a0f1565bde2998095712026d38f4c31d79ab",
        "payloads": "e399544f614cee32dd9824153aece07448b585a0dcc84f9f8963ce9ef6519f27",
        "replay": "56daf135c7e94ae0df467de0266f8355248a01987cccb99bf99c82ac665cdeb4",
        "cvd": -18350,
        "tick_count": 3018,
        "trades": 2101
    },
    7: {
        "results": "0325fb561d265693876765499981f9b83c2fe20f55a74a1aa960db79aab44e5c",
        "observations": "1a5d6653707920c92e8fcf1ab1e1fe4fc5cf3b9cc788ac9e38a179312a35a03c",
        "payloads": "18eec9671a0e88a6633040c042234e793329dca1ea996c5fa813dea0efa83991",
        "replay": "88627ad00acb2a16a0b07997b61ddccbb7acddc6d5d646a978b8cd98556fbaac",
        "cvd": -2355,
        "tick_count": 3018,
        "trades": 2101
    },
    42: {
        "results": "d70747d0a002771eacdf10a3c66fb6e4c83fac38423dd2358be5cc8f87190e2f",
        "observations": "43d01daafe6e10f39569a3ace713e511551e14a7c4562b7d9589948fa6e4603c",
        "payloads": "a8a734048b28e0fc3601dc4ed604ca10007b960e76f4fbda979463bc5002e2fb",
        "replay": "e80028763f637791f41846c5a6d341c6e690ba1a9c42e699bbc21dcd09e1ad95",
        "cvd": 16970,
        "tick_count": 3018,
        "trades": 2147
    },
}


@pytest.mark.parametrize("seed", sorted(GOLDEN))
def test_engine_output_matches_baseline(seed):
    assert engine_outputs(seed) == GOLDEN[seed]


if __name__ == "__main__":
    print(json.dumps({s: engine_outputs(s) for s in (1, 7, 42)}, indent=2))
