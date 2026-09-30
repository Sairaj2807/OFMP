"""Order-flow domain: order book, trade extraction and classification,
footprint aggregation, CVD and footprint analytics.

Pure Python — no web framework, database or broker imports — so live
ingestion, replay and research all run the same code."""
from .analytics import poc_from_rows, stacked_imbalances_from_rows, value_area_from_rows
from .bars import group_candle_timestamps, group_native_bars
from .classification import (Classification, ClassificationContext, LeeReadyClassifier,
                             TickRuleClassifier, TradeClassifier, VtrenderReconstructedClassifierV1)
from .cvd import CVDTracker
from .footprint import Footprint
from .models import FootprintCell, FootprintRow, Trade, TradeSide
from .orderbook import OrderBook
from .pipeline import TickProcessorState, TradeEvent, advance_candle, process_tick, restore_trades
from .registry import CLASSIFIERS, DEFAULT_CLASSIFIER, ENGINE_ALGORITHMS, get_classifier
from .volume import CumulativeVolumeExtractor

__all__ = [
    "CLASSIFIERS", "CVDTracker", "Classification", "ClassificationContext", "CumulativeVolumeExtractor",
    "DEFAULT_CLASSIFIER", "ENGINE_ALGORITHMS", "Footprint", "FootprintCell", "FootprintRow",
    "LeeReadyClassifier", "OrderBook", "TickProcessorState", "TickRuleClassifier", "Trade",
    "TradeClassifier", "TradeEvent", "TradeSide", "VtrenderReconstructedClassifierV1", "advance_candle",
    "get_classifier", "group_candle_timestamps", "group_native_bars", "poc_from_rows", "process_tick",
    "restore_trades",
    "stacked_imbalances_from_rows", "value_area_from_rows",
]
