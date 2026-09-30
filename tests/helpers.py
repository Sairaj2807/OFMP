"""Shared helpers for the order-flow chart tests.

The real-data-shaped tests run on a synthetic Angel One session generated
here from a fixed seed — no saved session file is needed, and the result is
identical on every run. Ticks go through the real engine (process_tick's
inferred-side path), exactly as live ticks from ws_ingest.py do."""
import random

import orderbook_engine as oe

TICK = 0.1
LOT = 65


def synthetic_ticks(n_ticks=4000, seed=7, start_s=1_790_653_500, price=24000.0):
    """Angel-shaped Snap Quote ticks (ltp, ltt, cum_volume, best-5 depth) over
    roughly two hours: a random walk on the tick grid, a 1-10 tick spread, a
    mix of quote-only updates and trades printing at the bid, at the ask or
    inside the spread. ltt is whole seconds (in ms), like Angel's."""
    rng = random.Random(seed)
    ticks, cum, ts = [], 0, start_s
    mid = price
    for _ in range(n_ticks):
        ts += rng.choice((0, 1, 1, 2, 3))
        mid = round(mid + rng.choice((-2, -1, 0, 0, 1, 2)) * TICK, 1)
        half = rng.randint(1, 5) * TICK
        bid, ask = round(mid - half, 1), round(mid + half, 1)
        if rng.random() < 0.7:
            cum += rng.randint(1, 20) * LOT
            ltp = rng.choice((bid, ask, round(bid + TICK, 1), mid))
        else:
            ltp = ticks[-1]["ltp"] if ticks else mid
        ticks.append({
            "ltp": ltp, "ltt": ts * 1000, "cum_volume": cum,
            "depth_buy": [(round(bid - i * TICK, 1), rng.randint(1, 40) * LOT, rng.randint(1, 9)) for i in range(5)],
            "depth_sell": [(round(ask + i * TICK, 1), rng.randint(1, 40) * LOT, rng.randint(1, 9)) for i in range(5)],
        })
    return ticks


def feed_engine(ticks, tick_size=TICK):
    """Runs ticks through the real engine. Returns the engine with
    `trade_log` attached: one {"ts_ms", "buy_qty", "sell_qty"} record per
    trade the engine classified, captured from the observation sink."""
    eng = oe.TickProcessorState(tick_size)
    eng.trade_log = []

    def sink(obs):
        buy = obs["qty"] if obs["algo_side"] == "BUY" else 0
        eng.trade_log.append({"ts_ms": obs["ts_ms"], "buy_qty": buy, "sell_qty": obs["qty"] - buy})

    for t in ticks:
        oe.process_tick(eng, t, observation_sink=sink)
    return eng


def trade(ts_s, price, qty, side):
    return oe.Trade(timestamp=ts_s * 1000, price=price, quantity=qty, side=side)
