"""Reconstructs footprint/CVD/order-book state from a day's saved
observations, for the Orderflow Replay verification workflow (see
REPLAY_WORKFLOW.md) — lets a reviewer scrub a saved session to any
timestamp and see the same footprint chart the live dashboard renders,
side by side with Vtrender's own Orderflow Replay for that date.

Each saved observation (`qty` + `algo_side`) is one classified trade.
Replay never re-classifies: it uses the side stored with each record, so a
past session always replays with the classifier version that produced it.

Reconstruction is exact for the footprint: it's built purely from the
already-classified trades replayed through the same Footprint.add_trade and
the same candle-rollover step (pipeline.advance_candle) the live
engine uses, so delta/POC/value-area/imbalances/CVD all match what the live
engine would have shown at that instant.

It is NOT exact for the order book. Only the depth attached to each *trade*
tick was ever logged (observation["bid_levels"]/["ask_levels"]) — quote-only
ticks with no trade were never persisted. So the "book" during replay is
only as fresh as the most recent trade at or before as_of_ms, not truly
continuous like the live book. This is fine for verifying trade
classification (which only ever looks at the book at trade instants
anyway — see TRADE_CLASSIFICATION.md §3.1) but the replay book should not
be read as a live-quality L2 reconstruction between trades.
"""
from typing import Optional

import config
from backend.app.domain.profile import NSE_SESSION, SessionProfile
from orderbook_engine import CVDTracker, Footprint, OrderBook, Trade, _advance_candle


class ReplayState:
    def __init__(self, tick_size: float):
        self.book = OrderBook()
        self.footprint = Footprint(tick_size, config.CANDLE_INTERVAL_SEC)
        self.cvd_tracker = CVDTracker()
        self.cvd_by_candle: dict = {}
        self.last_candle_seen: Optional[int] = None
        self.trade_count = 0
        self.last_observation: Optional[dict] = None
        self.profile = SessionProfile(tick_size, NSE_SESSION)   # stored sessions are NSE sessions


def trades_from_record(obs: dict) -> list:
    """The Trade objects one saved record stands for (always one)."""
    return [Trade(timestamp=obs["ts_ms"], price=obs["ltp"], quantity=obs["qty"], side=obs["algo_side"])]


def record_summary(obs: dict) -> dict:
    """qty / side / reason stored with a saved record."""
    return {"qty": obs["qty"], "algo_side": obs["algo_side"], "algo_reason": obs.get("algo_reason")}


def apply_record(state: ReplayState, obs: dict) -> None:
    """Advance a replay by one saved record: the same Footprint.add_trade and
    candle rollover the live engine runs, then the record's depth snapshot.
    Shared by the one-shot rebuild below and the streaming replay session, so
    the two cannot drift."""
    state.trade_count += 1
    for trade in trades_from_record(obs):
        candle_ts = state.footprint.add_trade(trade)
        _advance_candle(state, candle_ts)
        state.profile.add_trade(trade.timestamp, trade.price, trade.quantity, trade.side)
    if obs.get("bid_levels") is not None:
        state.book.replace_side("BUY", [tuple(lvl) for lvl in obs["bid_levels"]])
    if obs.get("ask_levels") is not None:
        state.book.replace_side("SELL", [tuple(lvl) for lvl in obs["ask_levels"]])
    state.last_observation = obs


def build_replay_state(observations: list, tick_size: float,
                        as_of_ms: Optional[int] = None) -> ReplayState:
    """Replays a day's saved records (sorted by ts_ms, as returned by
    observation_store.load_session_records) up to and including as_of_ms
    through the same footprint/CVD bookkeeping process_tick uses live, and
    leaves the order book set to the latest record's attached depth
    snapshot."""
    state = ReplayState(tick_size)
    for obs in observations:
        if as_of_ms is not None and obs["ts_ms"] > as_of_ms:
            break
        apply_record(state, obs)
    return state


def session_bounds(observations: list) -> Optional[dict]:
    """First/last trade timestamp and count — the range a replay scrubber
    UI needs. None if the session has no trades."""
    if not observations:
        return None
    return {
        "start_ms": observations[0]["ts_ms"],
        "end_ms": observations[-1]["ts_ms"],
        "trade_count": len(observations),
    }
