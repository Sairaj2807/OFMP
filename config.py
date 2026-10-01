"""Tunable constants for the order book / order flow footprint engine."""
import os

from dotenv import load_dotenv

# Secrets (Angel One credentials) come from the environment; a git-ignored
# .env file in the project root is loaded for local runs. Real environment
# variables take precedence over .env.
load_dotenv()

from backend.app.domain.orderflow import settings as _orderflow  # noqa: E402

# Footprint aggregation
CANDLE_INTERVAL_SEC = _orderflow.NATIVE_CANDLE_SEC  # native 1-minute footprint candles — the ONLY
                                   # interval the engine ever buckets trades into,
                                   # live or in replay. Coarser display intervals
                                   # (see ALLOWED_CHART_INTERVALS_SEC) are built by
                                   # grouping native candles at payload time
                                   # (orderbook_engine.group_native_bars), not by
                                   # running extra aggregation in the hot tick path.
MAX_CANDLES_KEPT = _orderflow.MAX_CANDLES_KEPT  # rolling window of NATIVE candles retained (live
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
VALUE_AREA_PCT = _orderflow.VALUE_AREA_PCT

# Stacked imbalance detection
IMBALANCE_THRESHOLD = _orderflow.IMBALANCE_THRESHOLD
MIN_STACK = _orderflow.MIN_STACK

# Order book
DEPTH_LEVELS = _orderflow.DEPTH_LEVELS  # Angel One Snap Quote gives best-5 only

# VTRenders trade classification (see VTRENDERS_RECONSTRUCTED_ALGORITHM.md).
# Primary rule (midpoint, opposite polarity of classic Lee-Ready) is
# validated at 96.4% on 55 trades. This stale-quote fallback threshold
# rests on a single observed example (trade_id 17, 14000ms) — treat as a
# placeholder, not a calibrated parameter, until more stale-quote examples
# are collected.
VTRENDERS_STALE_QUOTE_MS = _orderflow.VTRENDER_STALE_QUOTE_MS

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

# Broadcast cadence to browser clients
BROADCAST_INTERVAL_SEC = 0.5

# Writable runtime files; in containers these point into the data volume.
TOKENS_FILE = os.environ.get("TOKENS_FILE", "angel_tokens.txt")
INSTRUMENTS_CACHE_FILE = os.environ.get("INSTRUMENTS_CACHE_FILE", "instruments_cache.json")

# ---------------------------------------------------------------------------
# Data source
# ---------------------------------------------------------------------------
# Angel One Snap Quote (backend/app/infrastructure/providers/angelone/) is
# the only market-data provider. Only a single blended cumulative volume
# counter is available, so trade side is inferred by the configured
# classifier (backend/app/domain/orderflow/classification.py).

# Ingestion mode:
#   "embedded" (default) — the provider runs inside the API process; no Redis.
#   "redis" — the ingest worker (python -m backend.app.workers.ingest) owns the
#             broker connection and publishes ticks to Redis; the API consumes
#             them. API restarts then leave the broker connection untouched.
INGEST_MODE = os.environ.get("INGEST_MODE", "embedded")
if INGEST_MODE not in ("embedded", "redis"):
    raise ValueError(f"INGEST_MODE must be 'embedded' or 'redis', got {INGEST_MODE!r}")
# Default points at this project's dev Redis (docker-compose.dev.yml, port 56379),
# not the default 6379, which other local projects may be using.
REDIS_URL = os.environ.get("REDIS_URL", "redis://127.0.0.1:56379/0")

# "angelone" (default) or "synthetic": a random-walk demo feed for development
# without a broker, credentials or market hours. Synthetic data is never
# recorded (no observations, tick archive or database trades).
MARKET_DATA_PROVIDER = os.environ.get("MARKET_DATA_PROVIDER", "angelone")
if MARKET_DATA_PROVIDER not in ("angelone", "synthetic"):
    raise ValueError(f"MARKET_DATA_PROVIDER must be 'angelone' or 'synthetic', got {MARKET_DATA_PROVIDER!r}")
SYNTHETIC_FEED = MARKET_DATA_PROVIDER == "synthetic"

# PostgreSQL + TimescaleDB (docker-compose.dev.yml runs one on port 55432).
# Unset = persistence off: the platform runs exactly as before on JSONL only.
# Example: postgresql+asyncpg://ofmp:<password>@127.0.0.1:55432/ofmp
DATABASE_URL = os.environ.get("DATABASE_URL") or None
# On startup/contract switch, rebuild today's live footprint for the contract
# from stored trades (needs DATABASE_URL).
RESTORE_SESSION_ON_START = os.environ.get("RESTORE_SESSION_ON_START", "1").lower() not in ("0", "false", "no")

# Archive every normalized tick (quote-only updates included) under
# data/ticks/<date>/ — the input needed to re-run the engine over a past
# session. Recorded by whichever process holds the broker connection.
RECORD_RAW_TICKS = os.environ.get("RECORD_RAW_TICKS", "1").lower() not in ("0", "false", "no")

# ---------------------------------------------------------------------------
# Environment, API and authentication
# ---------------------------------------------------------------------------
ENVIRONMENT = os.environ.get("ENVIRONMENT", "development")   # development | staging | production
if ENVIRONMENT not in ("development", "staging", "production"):
    raise ValueError(f"ENVIRONMENT must be development, staging or production, got {ENVIRONMENT!r}")
LOG_FORMAT = os.environ.get("LOG_FORMAT", "json" if ENVIRONMENT != "development" else "text")

# HS256 signing key for access tokens. Required for /api/v1 auth; generate with
#   python -c "import secrets; print(secrets.token_urlsafe(48))"
JWT_SECRET = os.environ.get("JWT_SECRET") or None
if ENVIRONMENT == "production" and (not JWT_SECRET or len(JWT_SECRET) < 32):
    raise ValueError("JWT_SECRET must be set (>= 32 characters) in production")
ACCESS_TOKEN_TTL_SEC = int(os.environ.get("ACCESS_TOKEN_TTL_SEC", 15 * 60))
REFRESH_TOKEN_TTL_SEC = int(os.environ.get("REFRESH_TOKEN_TTL_SEC", 30 * 24 * 3600))
# Secure cookies are only sent over HTTPS (browsers also allow http://localhost).
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "1").lower() not in ("0", "false", "no")
ALLOW_REGISTRATION = os.environ.get("ALLOW_REGISTRATION", "1").lower() not in ("0", "false", "no")
# Require login for the legacy pages and sockets (/, /chart, /replay, /ws/*, /api/*).
# Off by default until the new frontend lands; /api/v1 always requires auth.
AUTH_REQUIRED = os.environ.get("AUTH_REQUIRED", "0").lower() not in ("0", "false", "no")
# Comma-separated origins allowed to call the API cross-origin (empty = same-origin only).
CORS_ORIGINS = [o.strip() for o in os.environ.get("CORS_ORIGINS", "").split(",") if o.strip()]
MAX_REQUEST_BYTES = int(os.environ.get("MAX_REQUEST_BYTES", 1_000_000))
# Public base URL, used in email links (verification, password reset).
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "http://127.0.0.1:8000")
if ENVIRONMENT == "production" and SYNTHETIC_FEED:
    raise ValueError("MARKET_DATA_PROVIDER=synthetic is for development only")
# Prometheus metrics: /metrics on the API (block it at the reverse proxy) and
# METRICS_PORT on the ingest worker.
METRICS_ENABLED = os.environ.get("METRICS_ENABLED", "1").lower() not in ("0", "false", "no")
METRICS_PORT = int(os.environ.get("METRICS_PORT", 9108))
# Alert webhooks may target http:// URLs and private/loopback addresses (a
# receiver on your own machine). Development only: never in production.
ALERT_WEBHOOKS_ALLOW_PRIVATE = os.environ.get(
    "ALERT_WEBHOOKS_ALLOW_PRIVATE", "1" if ENVIRONMENT == "development" else "0").lower() not in ("0", "false", "no")
if ENVIRONMENT == "production" and ALERT_WEBHOOKS_ALLOW_PRIVATE:
    raise ValueError("ALERT_WEBHOOKS_ALLOW_PRIVATE is for development only")
# Built frontend (cd frontend && npm run build), served at /app when present.
FRONTEND_DIST = os.environ.get("FRONTEND_DIST", "frontend/out")

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
DATA_DIR = os.environ.get("DATA_DIR", "data")
SESSIONS_DIR = f"{DATA_DIR}/sessions"
TICKS_DIR = f"{DATA_DIR}/ticks"
VERIFIED_DATASET_CSV = f"{DATA_DIR}/verified_dataset.csv"

# Session dates are IST calendar dates (fixed +05:30, NSE has no DST),
# computed explicitly rather than from the host's timezone — see
# observation_store.date_str_from_ts_ms and the database's session_date.
SESSION_DATE_FMT = "%Y-%m-%d"


def session_dir(date_str: str) -> str:
    return f"{SESSIONS_DIR}/{date_str}"


def observations_path(date_str: str) -> str:
    return f"{session_dir(date_str)}/observations.jsonl"


def labels_path(date_str: str) -> str:
    return f"{session_dir(date_str)}/labels.jsonl"
