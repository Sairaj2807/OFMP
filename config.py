"""Tunable constants for the order book / order flow footprint engine."""
import os

from dotenv import load_dotenv

# Secrets (Angel One credentials) come from the environment; a git-ignored
# .env file in the project root is loaded for local runs. Real environment
# variables take precedence over .env.
load_dotenv()

# Footprint aggregation
CANDLE_INTERVAL_SEC = 60          # native 1-minute footprint candles — the ONLY
                                   # interval the engine ever buckets trades into,
                                   # live or in replay. Coarser display intervals
                                   # (see ALLOWED_CHART_INTERVALS_SEC) are built by
                                   # grouping native candles at payload time
                                   # (orderbook_engine.group_native_bars), not by
                                   # running extra aggregation in the hot tick path.
MAX_CANDLES_KEPT = 400            # rolling window of NATIVE candles retained (live
                                   # pruning and replay's own rollover both use this).
                                   # ~ a full NSE session (375 one-minute candles),
                                   # so a 30-minute chart view still has a useful
                                   # number of bars (400 // 30 ≈ 13) and replay keeps
                                   # full-day depth. Was 120 (2h) before the chart's
                                   # interval selector needed multi-hour retention.

# Display candle intervals offered on the order-flow chart page (/chart's
# "Interval" control). Every value must be a positive integer multiple of
# CANDLE_INTERVAL_SEC; server._clamp_chart_interval snaps anything else to the
# nearest one of these.
ALLOWED_CHART_INTERVALS_SEC = (60, 180, 300, 900, 1800)   # 1, 3, 5, 15, 30 min

# Value Area
VALUE_AREA_PCT = 0.70

# Stacked imbalance detection
IMBALANCE_THRESHOLD = 3.0
MIN_STACK = 3

# Order book
DEPTH_LEVELS = 5                  # Angel One Snap Quote gives best-5 only

# VTRenders trade classification (see VTRENDERS_RECONSTRUCTED_ALGORITHM.md).
# Primary rule (midpoint, opposite polarity of classic Lee-Ready) is
# validated at 96.4% on 55 trades. This stale-quote fallback threshold
# rests on a single observed example (trade_id 17, 14000ms) — treat as a
# placeholder, not a calibrated parameter, until more stale-quote examples
# are collected.
VTRENDERS_STALE_QUOTE_MS = 14000

# WebSocket
WS_URL = "wss://smartapisocket.angelone.in/smart-stream"
HEARTBEAT_INTERVAL_SEC = 30
RECONNECT_DELAY_SEC = 3

# REST
BASE_URL = "https://apiconnect.angelone.in"
INSTRUMENT_MASTER_URL = "https://margincalculator.angelone.in/OpenAPI_File/files/OpenAPIScripMaster.json"

# Instrument to resolve at startup
# NOTE: OpenAPIScripMaster.json uses the short segment code "NFO" for all
# NSE F&O contracts (futures + options).
INSTRUMENT_NAME = "NIFTY"
INSTRUMENT_EXCH_SEG = "NFO"
INSTRUMENT_TYPE = "FUTIDX"
# None (default): auto-roll — angel_client.resolve_configured_future() always
# picks whichever unexpired contract expires soonest, so this never goes
# stale month to month (that question is safe to ask repeatedly, unlike
# "the September one").
# Set to a month code (e.g. "DEC") to pin a specific far-month contract
# instead — also overridable per-request via the /api/contract switcher.
INSTRUMENT_EXPIRY_MONTH = os.environ.get("INSTRUMENT_EXPIRY_MONTH") or None

# Exchange type codes for WebSocket subscription (per SmartAPI docs)
EXCHANGE_TYPE_CODES = {
    "NSE_CM": 1,
    "NSE_FO": 2,
    "BSE_CM": 3,
    "BSE_FO": 4,
    "MCX_FO": 5,
}

# Broadcast cadence to browser clients
BROADCAST_INTERVAL_SEC = 0.5

TOKENS_FILE = "angel_tokens.txt"
INSTRUMENTS_CACHE_FILE = "instruments_cache.json"

# ---------------------------------------------------------------------------
# Data source
# ---------------------------------------------------------------------------
# Angel One Snap Quote (ws_ingest.py) is the only market-data source. Only a
# single blended cumulative volume counter is available, so orderbook_engine's
# heuristic classifier (TradeClassifier.classify — quote rule + tick rule)
# infers trade side. See TRADE_CLASSIFICATION.md.

# Evidence-collection pipeline for reverse-engineering Vtrender's trade
# classification (see TRADE_CLASSIFICATION.md). Logs one observation per
# classified trade; review_cli.py verifies against Vtrender separately
# (either live, or later against Vtrender's Orderflow Replay — see
# REPLAY_WORKFLOW.md).
#
# Storage is partitioned by trading day (data/sessions/<YYYY-MM-DD>/...)
# rather than one flat file, because verification now happens well after
# the fact: a reviewer picks "which day am I replaying" independently of
# when the server happened to be running.
COLLECT_OBSERVATIONS = True
DATA_DIR = "data"
SESSIONS_DIR = f"{DATA_DIR}/sessions"
VERIFIED_DATASET_CSV = f"{DATA_DIR}/verified_dataset.csv"

# Local timezone the exchange timestamps (ltt) are effectively already in
# (NSE trading hours), used only to bucket observations into calendar-day
# session folders. datetime.fromtimestamp() (system-local) is used
# throughout for this — kept as one named constant so it's easy to audit.
SESSION_DATE_FMT = "%Y-%m-%d"


def session_dir(date_str: str) -> str:
    return f"{SESSIONS_DIR}/{date_str}"


def observations_path(date_str: str) -> str:
    return f"{session_dir(date_str)}/observations.jsonl"


def labels_path(date_str: str) -> str:
    return f"{session_dir(date_str)}/labels.jsonl"
