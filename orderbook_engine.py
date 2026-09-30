"""Compatibility layer over the order-flow domain package.

The engine lives in backend/app/domain/orderflow/. This module keeps the
names server.py, replay_engine.py and the tests import, and adapts
process_tick's observation_sink (a callable taking the flat observation
dict) onto the pipeline's TradeEvent callback. New code should import from
backend.app.domain.orderflow directly.
"""
from backend.app.domain.orderflow import (CVDTracker, Footprint, FootprintCell, FootprintRow, OrderBook,
                                          TickProcessorState, Trade, advance_candle,
                                          group_candle_timestamps, group_native_bars, poc_from_rows,
                                          stacked_imbalances_from_rows, value_area_from_rows)
from backend.app.domain.orderflow import process_tick as _pipeline_process_tick
from research.observation_features import build_observation

_advance_candle = advance_candle

__all__ = [
    "CVDTracker", "Footprint", "FootprintCell", "FootprintRow", "OrderBook", "TickProcessorState", "Trade",
    "_advance_candle", "group_candle_timestamps", "group_native_bars", "poc_from_rows", "process_tick",
    "stacked_imbalances_from_rows", "value_area_from_rows",
]


def process_tick(state: TickProcessorState, tick: dict, observation_sink=None) -> dict:
    """pipeline.process_tick, with observation_sink receiving the flat
    observation record (research.observation_features.build_observation)
    for each classified trade."""
    on_trade = (lambda ev: observation_sink(build_observation(ev))) if observation_sink is not None else None
    return _pipeline_process_tick(state, tick, on_trade=on_trade)
