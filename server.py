"""FastAPI app: resolves the active NIFTY future, runs the Angel One ingest
(embedded, or via the Redis ingest worker), holds the live order-flow engine,
and serves the /api/v1 API, the /ws/v1/stream real-time stream and the
terminal (/app). Angel One tokens never leave the process that holds the
broker connection: only derived market data reaches browsers.

The legacy pages (/, /chart, /replay and their /ws/frontend, /ws/chart and
/api/* endpoints) were retired in Phase 9; their URLs redirect to the terminal.
"""
import asyncio
import contextlib
import logging
import uuid
import os
import time
from datetime import date as date_cls
from datetime import datetime
from datetime import timedelta
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

import angel_client
import config
from observation_store import ObservationStore, list_replayable_sessions, load_session_records
from orderbook_engine import TickProcessorState, process_tick
from backend.app.api.app import configure_api, start_api
from backend.app.core import metrics
from backend.app.core.logging import configure_logging
from backend.app.domain.market_data import MarketTick
from backend.app.infrastructure.providers.synthetic import SYNTHETIC_CONTRACT, SyntheticProvider
from backend.app.domain.orderflow import Trade, restore_trades
from backend.app.domain.orderflow.chart import chart_payload
from backend.app.domain.profile import ALL_DAY_SESSION, NSE_SESSION, SessionProfile, build_profile
from backend.app.infrastructure.postgres.database import create_engine as create_db_engine
from backend.app.infrastructure.postgres.repositories import (last_open_interest_before, load_session_tape,
                                                              load_symbol_sessions,
                                                              load_trades_by_date, previous_session_date,
                                                              symbol_session_dates, trade_counts_by_session,
                                                              untagged_session_dates, upsert_instrument)
from backend.app.services.footprint_history import FootprintHistory, day_cap
from backend.app.services.profile_history import ProfileHistory
from backend.app.services.broker_session import BrokerSession, SessionTimes
from backend.app.services.replay import ReplayLibrary, ReplaySession
from backend.app.infrastructure.postgres.rows import IST, ist_date
from backend.app.infrastructure.postgres.writer import MarketDataWriter
from backend.app.services.market_data.feeds import EmbeddedFeed, RedisFeed
from backend.app.services.market_data.recorder import RawTickRecorder


configure_logging(config.LOG_FORMAT)
log = logging.getLogger("server")

app = FastAPI(
    title="OFMP — Order-Flow Market Platform",
    version="0.4.0",
    description="Live NIFTY futures order flow. Versioned API under /api/v1; errors use "
                '{"error": {"code", "message", "request_id"}}. Browser clients authenticate with '
                "HttpOnly cookies (unsafe requests must send X-CSRF-Token = the ofmp_csrf cookie); "
                "API clients may send Authorization: Bearer <access token>.",
    docs_url="/api/docs" if config.ENVIRONMENT != "production" else None,
    redoc_url=None,
    openapi_url="/api/openapi.json" if config.ENVIRONMENT != "production" else None,
)
configure_api(app, environment=config.ENVIRONMENT, cookie_secure=config.COOKIE_SECURE,
              cors_origins=config.CORS_ORIGINS, max_request_bytes=config.MAX_REQUEST_BYTES,
              metrics_enabled=config.METRICS_ENABLED,
              alert_webhooks_allow_private=config.ALERT_WEBHOOKS_ALLOW_PRIVATE)

STATE = {
    "contract": None,
    "engine": None,
    "connected": False,
    "last_error": None,
    "started_at": time.time(),
    "observation_store": None,
    "db_engine": None,    # set at startup when config.DATABASE_URL is configured
    "db_writer": None,
}
CHART_BARS = 60               # footprint columns per chart snapshot (engine keeps MAX_CANDLES_KEPT)
CONTRACT_SWITCH_LOCK = asyncio.Lock()  # serializes _activate_contract calls (startup and /api/contract)


def _on_status(connected: bool, error):
    STATE["connected"] = connected
    STATE["last_error"] = error


def _on_tick(tick: MarketTick):
    engine: TickProcessorState = STATE["engine"]
    if engine is None:
        return
    tick_in = tick.to_engine_tick()
    obs_store = STATE.get("observation_store")
    sink = None
    if obs_store is not None:
        # Tags every logged record with the symbol that was ACTIVE at that
        # instant — important once /api/contract can switch the live feed
        # mid-session: without this, a day's observations.jsonl
        # would silently mix two instruments' trades with
        # no way to tell them apart afterward. contract is read fresh per
        # tick (not captured once) so a switch mid-stream tags correctly on
        # both sides of the switch.
        contract = STATE.get("contract")
        symbol = contract["tradingsymbol"] if contract else None
        token = str(contract["token"]) if contract and contract.get("token") is not None else None

        def sink(obs, symbol=symbol, token=token, provider=tick.provider):
            record = {**obs, "symbol": symbol}
            trade_id = obs_store.log(record)
            writer = STATE.get("db_writer")
            if writer is not None:   # same trade_id as the JSONL record, so the two reconcile exactly
                writer.record_trade({**record, "trade_id": trade_id}, provider=provider, token=token)
    stop = metrics.timed(metrics.TICK_PROCESSING)
    result = process_tick(engine, tick_in, observation_sink=sink)
    stop()
    profile = getattr(engine, "profile", None)
    if profile is not None and result.get("new_trade"):
        profile.add_trade(tick_in["ltt"], tick_in["ltp"], result["qty"], result["side"])
    if result.get("new_trade") and engine.last_classification is not None:
        c = engine.last_classification
        metrics.TRADES_CLASSIFIED.labels(c.classifier_name, c.classifier_version, c.side).inc()
        _evaluate_alerts(engine, tick_in)
    engine.last_quote = tick  # keep the latest quote around for header display


async def _resolve_angel_contract() -> dict:
    await asyncio.to_thread(angel_client.ensure_valid_token)
    return await asyncio.to_thread(angel_client.resolve_configured_future)


async def _resolve_default_contract() -> dict:
    """The contract to use at startup, or as the fallback if an /api/contract
    switch is requested with no explicit target (not currently exposed, but
    keeps auto-roll and manual switching sharing one resolution path)."""
    if config.SYNTHETIC_FEED:
        return dict(SYNTHETIC_CONTRACT)
    return await _resolve_angel_contract()


_REDIS_BUS = None


def _redis_bus():
    global _REDIS_BUS
    if _REDIS_BUS is None:
        import redis.asyncio as aioredis

        from backend.app.infrastructure.redis_bus import RedisMarketDataBus
        _REDIS_BUS = RedisMarketDataBus(aioredis.from_url(config.REDIS_URL))
    return _REDIS_BUS


def LiveFeed(contract: dict, on_tick, on_status):
    """The live feed for config.INGEST_MODE (see backend/app/services/market_data/feeds.py)."""
    if config.INGEST_MODE == "redis":   # the ingest worker owns the provider (Angel One or synthetic)
        return RedisFeed(contract, on_tick=on_tick, on_status=on_status, bus=_redis_bus())
    if config.SYNTHETIC_FEED:   # demo data: never archived or persisted
        return EmbeddedFeed(contract, on_tick=on_tick, on_status=on_status, provider=SyntheticProvider())
    recorder = RawTickRecorder(config.TICKS_DIR) if config.RECORD_RAW_TICKS else None
    writer = STATE.get("db_writer")
    return EmbeddedFeed(contract, on_tick=on_tick, on_status=on_status, recorder=recorder,
                        tick_store=writer, on_quality_event=writer.record_quality_event if writer else None)


def _make_engine_and_client(contract: dict):
    """A fresh TickProcessorState for `contract` and the matching live feed —
    built but not yet wired into STATE or started, so a caller (startup or a
    live switch) can prepare everything before tearing down whatever's
    running now. TickProcessorState is instrument-specific (tick size, and
    everything downstream: order book, footprint, CVD), so there is no
    "carry the old one over" option — a switch always starts the live
    footprint/CVD history over, the same as a server restart would."""
    engine = TickProcessorState(contract["tick_size"])
    engine.last_quote = None
    engine.profile = _new_profile(contract["tick_size"])
    client = LiveFeed(contract, on_tick=_on_tick, on_status=_on_status)
    return engine, client


def _new_profile(tick_size: float) -> SessionProfile:
    """The session's market profile, fed the same classified trades as the footprint."""
    return SessionProfile(tick_size, ALL_DAY_SESSION if config.SYNTHETIC_FEED else NSE_SESSION)


async def _restore_session(engine: TickProcessorState, contract: dict) -> int:
    """Record the instrument and rebuild today's footprint/CVD for `contract`
    from stored trades (see orderflow.restore_trades). Best effort: a
    database problem is logged and the engine simply starts empty."""
    db = STATE.get("db_engine")
    if db is None or config.SYNTHETIC_FEED:
        return 0
    symbol, today = contract["tradingsymbol"], ist_date(int(time.time() * 1000))
    STATE["oi_before"] = None            # the dOI baseline belongs to the contract being restored
    try:
        await upsert_instrument(db, contract, instrument_type=config.INSTRUMENT_TYPE)
        if not config.RESTORE_SESSION_ON_START:
            return 0
        tape, (bids, asks) = await load_session_tape(db, "angelone", symbol, today, with_oi=True)
        STATE["oi_before"] = await last_open_interest_before(db, "angelone", symbol, today)
        prev_day = await previous_session_date(db, "angelone", symbol, today)
        prev_tape = (await load_session_tape(db, "angelone", symbol, prev_day))[0] if prev_day else []
    except Exception as e:
        print(f"[server] Session restore skipped (database: {e!r})")
        return 0
    if getattr(engine, "profile", None) is None:
        engine.profile = _new_profile(contract["tick_size"])
    if prev_tape:                       # yesterday's profile: reference levels (POC / VAH / VAL)
        previous = SessionProfile(contract["tick_size"], engine.profile.session)
        for ts, price, qty, side in prev_tape:
            previous.add_trade(ts, price, qty, side)
        engine.profile.previous = previous
    for ts, price, qty, side, _oi in tape:
        engine.profile.add_trade(ts, price, qty, side)
    trades = [Trade(timestamp=ts, price=price, quantity=qty, side=side, open_interest=oi)
              for ts, price, qty, side, oi in tape]
    return restore_trades(engine, trades, bids, asks)


def _evaluate_alerts(engine: TickProcessorState, tick_in: dict) -> None:
    """Live alert rules see every classified trade (never replay or the startup restore)."""
    runtime = getattr(app.state, "alert_runtime", None)
    if runtime is None or engine.last_candle_seen is None:
        return
    fp = engine.footprint
    stats = fp.candle_stats.get(engine.last_candle_seen) or {}
    cvd = engine.cvd_tracker.cvd + stats.get("run", 0)      # closed candles + the forming one
    try:
        runtime.on_trade(fp, int(tick_in["ltt"]), float(tick_in["ltp"]), cvd, engine.last_candle_seen)
    except Exception:                                       # alerts must never break ingestion
        log.exception("alert evaluation failed")


async def _activate_contract(contract: dict, start_feed: bool = True) -> None:
    """Swaps in `contract` as the live one: stops whatever ws_client is
    currently running (a no-op the first time, at startup), builds a fresh
    engine, updates STATE, and starts ingesting (unless `start_feed` is False:
    outside the broker session the engine is restored for the charts but the
    feed stays off). Serialized by CONTRACT_SWITCH_LOCK so two switch requests
    (or a switch racing startup) can't interleave their teardown/startup."""
    async with CONTRACT_SWITCH_LOCK:
        old_client = STATE.get("ws_client")
        old_task = getattr(app.state, "ws_task", None)
        if old_client is not None:
            old_client.stop()
        if old_task is not None:
            old_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await old_task

        engine, client = _make_engine_and_client(contract)
        restored = await _restore_session(engine, contract)
        STATE["contract"] = contract
        STATE["engine"] = engine
        if getattr(app.state, "alert_runtime", None) is not None:
            app.state.alert_runtime.reset_market(contract.get("tradingsymbol"))
        STATE["connected"] = False
        STATE["last_error"] = None
        STATE["ws_client"] = client if start_feed else None
        app.state.ws_task = asyncio.create_task(client.run()) if start_feed else None
        print(f"[server] Active contract: {contract['tradingsymbol']} "
              f"(token {contract.get('token')}, tick size {contract['tick_size']})"
              + (f", restored {restored} trades from today's session" if restored else "")
              + ("" if start_feed else " (feed off until the broker session starts)"))


async def _stop_feed() -> None:
    """Disconnects the live feed and keeps the engine (charts keep today's data)."""
    async with CONTRACT_SWITCH_LOCK:
        client, task = STATE.get("ws_client"), getattr(app.state, "ws_task", None)
        if client is not None:
            client.stop()
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        STATE["ws_client"] = None
        app.state.ws_task = None
        STATE["connected"] = False
    writer = STATE.get("db_writer")
    if writer is not None:
        await writer.flush()


# ---------------------------------------------------------------------------
# Daily broker session (backend/app/services/broker_session.py)
# ---------------------------------------------------------------------------

def _broker_scheduled() -> bool:
    return config.BROKER_SCHEDULE and not config.SYNTHETIC_FEED and config.INGEST_MODE != "redis"


async def _broker_login() -> dict:
    """A full Angel login (never a reused token), proved with a REST call."""
    try:
        if STATE.pop("broker_manual", False):
            angel_client.reset_login_guard()
        tokens = await asyncio.to_thread(angel_client.ensure_valid_token, 120, True)
        await asyncio.to_thread(angel_client.verify_session, tokens["jwt_token"])
    except Exception:
        metrics.BROKER_LOGINS.labels("failed").inc()
        raise
    metrics.BROKER_LOGINS.labels("ok").inc()
    exp = angel_client.token_expiry(tokens)
    if exp:
        metrics.BROKER_TOKEN_EXPIRES.set(exp)
    return {"expires_at": exp or None}


async def _broker_start_day() -> dict:
    """The trading day: fresh instrument master, the contract (an admin's pinned choice while it is
    unexpired, else the front month: auto-roll), a new engine restored from today's trades, feed on."""
    previous = STATE.get("contract") or {}
    instruments = await asyncio.to_thread(angel_client.fetch_instrument_master, True)
    today = date_cls.fromisoformat(_live_session_date())
    pinned = STATE.get("contract_pinned") and previous
    expiry = angel_client.parse_expiry_date(previous.get("expiry", "")) if pinned else None
    if pinned and expiry is not None and expiry >= today:
        contract = previous
    else:
        STATE["contract_pinned"] = False
        contract = await asyncio.to_thread(angel_client.resolve_configured_future, instruments)
    await _activate_contract(contract)
    symbol = contract["tradingsymbol"]
    return {"contract": symbol, "rolled": bool(previous) and previous.get("tradingsymbol") != symbol}


def _last_tick_ms() -> Optional[int]:
    engine = STATE.get("engine")
    quote = getattr(engine, "last_quote", None) if engine is not None else None
    return getattr(quote, "received_ts_ms", None)


async def _system_notify(level: str, message: str) -> None:
    """Broker notices: the log, the terminal of every signed-in admin, and every admin's webhooks."""
    db = STATE.get("db_engine")
    event = {"id": f"system-{uuid.uuid4()}", "type": "system.broker", "rule_id": None,
             "rule_name": "Broker session", "kind": "system.broker", "level": level, "message": message,
             "value": None, "symbol": (STATE.get("contract") or {}).get("tradingsymbol"),
             "fired_at": datetime.now().astimezone().isoformat(), "details": {}}
    if db is None:
        return
    import sqlalchemy as sa
    from backend.app.infrastructure.postgres.schema import notification_channels, users
    async with db.connect() as conn:
        admins = [str(r[0]) for r in (await conn.execute(
            sa.select(users.c.id).where(users.c.role.in_(("admin", "super_admin")), users.c.is_active))).all()]
        channels = (await conn.execute(
            sa.select(notification_channels.c.id, notification_channels.c.owner_user_id, notification_channels.c.config)
            .where(notification_channels.c.owner_user_id.in_(admins) if admins else sa.false(),
                   notification_channels.c.enabled, notification_channels.c.deleted_at.is_(None)))).all()
    hub = getattr(app.state, "stream_hub", None)
    for user_id in admins:
        if hub is not None:
            await hub.notify_user(user_id, "alert", event)
    webhook = getattr(app.state, "webhook_provider", None)
    if webhook is not None:
        for channel_id, owner, cfg in channels:
            ok, err = await webhook.deliver(str(owner), event, (str(channel_id), "webhook", cfg))
            if not ok:
                log.warning("broker notice to webhook %s failed: %s", channel_id, err)


def _broker() -> Optional["BrokerSession"]:
    return getattr(app.state, "broker_session", None)


def _broker_status() -> Optional[dict]:
    b = _broker()
    if b is None:
        return None
    guard = angel_client.login_state()
    return {**b.status(), "login_paused_until": guard["paused_until"] or None}


@app.on_event("startup")
async def startup():
    if config.DATABASE_URL:
        STATE["db_engine"] = create_db_engine(config.DATABASE_URL)
        STATE["db_writer"] = MarketDataWriter(STATE["db_engine"])
        app.state.db_writer_task = asyncio.create_task(STATE["db_writer"].run())
        print("[server] Persisting raw ticks, trades and data-quality events to the database")

    if config.SYNTHETIC_FEED:
        print("[server] SYNTHETIC market data (development only): nothing is recorded")
    elif config.COLLECT_OBSERVATIONS:
        STATE["observation_store"] = ObservationStore()
        print(f"[server] Logging trade observations to {config.SESSIONS_DIR}/<date>/observations.jsonl "
              f"(review live or later via review_cli.py, or visually via /replay)")

    if config.RATE_LIMIT_BACKEND == "redis":
        import redis.asyncio as aioredis
        # short timeouts: a hung Redis must not hold up logins; the limiter falls back per process
        STATE["limiter_redis"] = aioredis.from_url(config.REDIS_URL, socket_timeout=0.5, socket_connect_timeout=0.5)
        print("[server] Rate limits shared through Redis")

    app.state.stream_task = start_api(
        app, db_engine=STATE["db_engine"], jwt_secret=config.JWT_SECRET, public_base_url=config.PUBLIC_BASE_URL,
        access_ttl_sec=config.ACCESS_TOKEN_TTL_SEC, refresh_ttl_sec=config.REFRESH_TOKEN_TTL_SEC,
        allow_registration=config.ALLOW_REGISTRATION, gateway=ServerMarketGateway(),
        allowed_intervals=config.ALLOWED_CHART_INTERVALS_SEC, redis=STATE.get("limiter_redis"))
    if app.state.auth_service is None:
        print("[server] /api/v1 auth disabled: needs DATABASE_URL and JWT_SECRET")

    if not _broker_scheduled():
        await _activate_contract(await _resolve_default_contract())
        return
    # Broker session: restore the engine for the charts now (the contract comes from the public
    # instrument master: no login needed), and let the schedule log in and start the feed.
    try:
        contract = await asyncio.to_thread(angel_client.resolve_configured_future)
        await _activate_contract(contract, start_feed=False)
    except Exception as e:
        log.warning("contract not resolved at startup (the broker session retries): %r", e)
    calendar = app.state.market_calendar
    app.state.broker_session = BrokerSession(
        calendar, login=_broker_login, start_day=_broker_start_day, stop_feed=_stop_feed, notify=_system_notify,
        last_tick_ms=_last_tick_ms,
        times=SessionTimes(login_lead_min=config.BROKER_LOGIN_LEAD_MIN, give_up_lead_min=config.BROKER_GIVE_UP_LEAD_MIN,
                           stop_after_close_min=config.BROKER_STOP_AFTER_CLOSE_MIN, retry_sec=config.BROKER_RETRY_SEC))
    app.state.broker_task = asyncio.create_task(app.state.broker_session.run())
    print("[server] Broker session scheduled: Angel login before each NSE session, feed off after the close")


@app.on_event("shutdown")
async def shutdown():
    ws_client = STATE.get("ws_client")
    if ws_client:
        ws_client.stop()
    if _broker() is not None:
        _broker().stop()
    for t in (getattr(app.state, "broker_task", None), getattr(app.state, "ws_task", None),
              getattr(app.state, "stream_task", None), getattr(app.state, "alert_task", None)):
        if t:
            t.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await t
    writer, writer_task = STATE.get("db_writer"), getattr(app.state, "db_writer_task", None)
    if writer is not None and writer_task is not None:
        writer.stop()
        writer_task.cancel()                      # run()'s finally flushes what is still buffered
        with contextlib.suppress(asyncio.CancelledError):
            await writer_task
    if getattr(app.state, "webhook_provider", None) is not None:
        await app.state.webhook_provider.close()
    if STATE.get("limiter_redis") is not None:
        await STATE["limiter_redis"].aclose()
    if STATE.get("db_engine") is not None:
        await STATE["db_engine"].dispose()


def _book_payload(book) -> dict:
    top = book.top_n(config.DEPTH_LEVELS)
    return {
        "bids": [[p, q_, o] for p, (q_, o) in top["bids"]],
        "asks": [[p, q_, o] for p, (q_, o) in top["asks"]],
        "spread": book.spread(),
        "mid": book.mid_price(),
        "micro": book.micro_price(),
        "obi": book.obi(config.DEPTH_LEVELS),
    }


def _quote_payload(engine: TickProcessorState) -> Optional[dict]:
    q = getattr(engine, "last_quote", None)
    if not q:
        return None
    return {
        "ltp": q.ltp, "open": q.open, "high": q.high, "low": q.low,
        "close": q.close, "volume": q.cumulative_volume, "oi": q.open_interest,
    }


def _status_payload(engine: TickProcessorState, contract: dict) -> dict:
    return {
        "connected": STATE["connected"],
        "error": STATE["last_error"],
        "tick_count": engine.tick_count,
        "symbol": contract["tradingsymbol"],
        "tick_size": contract["tick_size"],
        "lot_size": contract["lotsize"],
    }


# ---------------------------------------------------------------------------
# Order-flow chart payload (/chart page; see static/orderflow.js).
#
# Deliberately in the ENGINE's vocabulary (buy / sell, BUY = the project's
# Vtrender-polarity convention): the one place that maps it onto the chart
# library's bid/ask is the toFootprintBar adapter in orderflow.js. Live
# (/ws/chart) and replay (/api/replay/{date}/chart) both go through
# _chart_payload, so the two can't drift apart.
# ---------------------------------------------------------------------------

def _clamp_chart_interval(value) -> int:
    """Snaps a requested display interval to one of config.ALLOWED_CHART_INTERVALS_SEC
    (nearest by absolute distance), or the native interval on anything unparseable."""
    try:
        v = int(value)
    except (TypeError, ValueError):
        return config.CANDLE_INTERVAL_SEC
    if v in config.ALLOWED_CHART_INTERVALS_SEC:
        return v
    return min(config.ALLOWED_CHART_INTERVALS_SEC, key=lambda allowed: abs(allowed - v))


def _chart_payload(fp, closed_cvd: int, open_ts: Optional[int], ppr: int,
                   interval_sec: int = config.CANDLE_INTERVAL_SEC,
                   limit: int = CHART_BARS, oi_before: Optional[int] = None) -> dict:
    """The last `limit` candles as footprint columns (see domain/orderflow/chart.py,
    shared with replay and historical sessions)."""
    return chart_payload(fp, closed_cvd, open_ts, ppr, interval_sec=interval_sec, limit=limit,
                         native_sec=config.CANDLE_INTERVAL_SEC, oi_before=oi_before)


def _source_info() -> dict:
    """Where BUY/SELL come from, so the chart can show it: Angel One ticks
    carry no side, so it is inferred by the midpoint rule."""
    return {"kind": "angel", "side_basis": "inferred (midpoint rule)"}


def _build_chart_snapshot(ppr: int = 1, interval_sec: int = config.CANDLE_INTERVAL_SEC) -> dict:
    engine: TickProcessorState = STATE["engine"]
    contract = STATE["contract"]
    if engine is None or contract is None:
        return {"ready": False}

    return {
        "ready": True,
        "mode": "live",
        "status": _status_payload(engine, contract),
        "source": _source_info(),
        "book": _book_payload(engine.book),
        "quote": _quote_payload(engine),
        "chart": _chart_payload(engine.footprint, engine.cvd_tracker.cvd,
                                engine.last_candle_seen, ppr, interval_sec=interval_sec,
                                oi_before=STATE.get("oi_before")),
    }


_PROFILE_HISTORY: Optional[ProfileHistory] = None


def _profile_history() -> Optional[ProfileHistory]:
    """Finished sessions of the active contract, from the database (None without one)."""
    global _PROFILE_HISTORY
    db = STATE.get("db_engine")
    if db is None:
        return None
    if _PROFILE_HISTORY is None:
        async def list_dates(symbol, before, limit, date_from):
            return await symbol_session_dates(db, "angelone", symbol, before, limit, date_from)

        async def load_days(symbol, dates):
            return await load_symbol_sessions(db, "angelone", symbol, dates)

        async def list_untagged(before, date_from):
            return await untagged_session_dates(db, "angelone", before, date_from)
        _PROFILE_HISTORY = ProfileHistory(list_dates, load_days, list_untagged)
    return _PROFILE_HISTORY


_FOOTPRINT_HISTORY: Optional[FootprintHistory] = None


def _footprint_history() -> Optional[FootprintHistory]:
    """Finished sessions of the active contract as footprint bars (None without a database)."""
    global _FOOTPRINT_HISTORY
    db = STATE.get("db_engine")
    if db is None:
        return None
    if _FOOTPRINT_HISTORY is None:
        async def list_dates(symbol, before, limit, date_from):
            return await symbol_session_dates(db, "angelone", symbol, before, limit, date_from)

        async def load_days(symbol, dates):
            return await load_symbol_sessions(db, "angelone", symbol, dates, with_oi=True)

        async def list_untagged(before, date_from):
            return await untagged_session_dates(db, "angelone", before, date_from)

        async def oi_before(symbol, day):
            return await last_open_interest_before(db, "angelone", symbol, day)
        _FOOTPRINT_HISTORY = FootprintHistory(list_dates, load_days, list_untagged, oi_before=oi_before)
    return _FOOTPRINT_HISTORY


def _ist_day_bounds(day: str) -> tuple:
    """[start, end) of an IST calendar day in epoch seconds."""
    start = int(datetime.combine(date_cls.fromisoformat(day), datetime.min.time(), IST).timestamp())
    return start, start + 86_400


def _live_session_date() -> str:
    """The live profile's session date (today, IST, before its first trade)."""
    engine = STATE.get("engine")
    date_ = getattr(getattr(engine, "profile", None), "date", None)
    return date_ or ist_date(int(time.time() * 1000)).isoformat()


def _profile_view(profile: SessionProfile, row: float) -> dict:
    return build_profile(profile, row) or {"date": None, "rows": [], "empty": True}


def _build_profile_snapshot(row: float) -> dict:
    engine: TickProcessorState = STATE["engine"]
    contract = STATE["contract"]
    if engine is None or contract is None:
        return {"ready": False, "view": "profile"}
    return {
        "ready": True,
        "mode": "live",
        "view": "profile",
        "status": _status_payload(engine, contract),
        "quote": _quote_payload(engine),
        "profile": _profile_view(engine.profile, row),
    }


class ServerMarketGateway:
    """The live engine, as the /api/v1 layer sees it (backend/app/api/gateway.py)."""

    def active_contract(self) -> Optional[dict]:
        return STATE.get("contract")

    def feed_status(self) -> Optional[dict]:
        health = _feed_health()
        if health is None:
            return None
        return {**health, "connected": STATE["connected"], "error": STATE["last_error"]}

    def broker_status(self) -> Optional[dict]:
        return _broker_status()

    def broker_relogin(self) -> bool:
        b = _broker()
        if b is None:
            return False
        STATE["broker_manual"] = True        # a manual re-login lifts the failed-login pause
        b.relogin()
        return True

    def database_stats(self) -> Optional[dict]:
        writer = STATE.get("db_writer")
        return writer.stats() if writer else None

    async def list_contracts(self) -> list:
        return await _list_candidate_contracts()

    async def switch_contract(self, token: str) -> dict:
        return await _switch_contract(token)

    def chart_snapshot(self, ppr: int, interval_sec: int) -> dict:
        return _build_chart_snapshot(ppr, interval_sec)

    def profile_snapshot(self, row: float) -> dict:
        return _build_profile_snapshot(row)

    def history_epoch(self) -> tuple:
        """Changes when the live contract or the live session changes: history must then be re-sent."""
        contract = STATE.get("contract") or {}
        return contract.get("tradingsymbol"), _live_session_date()

    async def profile_history(self, row: float, limit: int, date_from: Optional[str] = None,
                              date_to: Optional[str] = None) -> dict:
        """Finished sessions of the active contract (see services/profile_history.py)."""
        contract = STATE.get("contract") or {}
        symbol, tick = contract.get("tradingsymbol"), contract.get("tick_size", 0.1)
        live = _live_session_date()
        before = date_cls.fromisoformat(live)
        if date_to is not None:
            before = min(before, date_cls.fromisoformat(date_to) + timedelta(days=1))
        service = _profile_history()
        if service is None or symbol is None:
            return {"symbol": symbol, "row_size": row, "tick_size": tick, "before": before.isoformat(),
                    "live_date": live, "sessions": [], "excluded": [],
                    "unavailable": "history needs the database" if service is None else "no active contract"}
        out = await service.history(symbol, tick, row, before=before, limit=limit,
                                    date_from=date_cls.fromisoformat(date_from) if date_from else None)
        return {**out, "live_date": live}

    async def footprint_sessions(self, interval_sec: int) -> dict:
        """Finished sessions of the active contract available for the footprint, newest
        day_cap(interval) of them, plus the excluded (untagged) ones."""
        contract = STATE.get("contract") or {}
        symbol, live = contract.get("tradingsymbol"), _live_session_date()
        cap = day_cap(interval_sec)
        service = _footprint_history()
        base = {"symbol": symbol, "live_date": live, "interval_sec": interval_sec, "max_days": cap}
        if service is None or symbol is None:
            return {**base, "sessions": [], "excluded": [],
                    "unavailable": "history needs the database" if service is None else "no active contract"}
        return {**await service.sessions(symbol, date_cls.fromisoformat(live), cap), **base}

    async def footprint_day(self, day: str, ppr: int, interval_sec: int) -> Optional[dict]:
        """One session as footprint bars: a finished day from the history cache, or today
        (every bar of today so far) from the live engine."""
        contract = STATE.get("contract") or {}
        symbol, tick, live = contract.get("tradingsymbol"), contract.get("tick_size", 0.1), _live_session_date()
        if symbol is None:
            return None
        if day == live:
            engine = STATE.get("engine")
            if engine is None:
                return None
            fp = engine.footprint
            since, until = _ist_day_bounds(day)
            before = [t for t in fp.data if t < since]
            closed_before = sum(fp.candle_delta(t) for t in before)    # an engine running overnight: earlier days
            payload = chart_payload(fp, engine.cvd_tracker.cvd - closed_before, engine.last_candle_seen, ppr,
                                    interval_sec=interval_sec, limit=None, since=since, until=until,
                                    native_sec=config.CANDLE_INTERVAL_SEC, oi_before=STATE.get("oi_before"))
            return {"date": day, "symbol": symbol, "state": "live", "interval_sec": interval_sec,
                    "row_size": payload["row_size"], "cvd_start": payload["cvd_offset"], "bars": payload["bars"]}
        if day >= live:
            return None
        service = _footprint_history()
        if service is None:
            return None
        return await service.day(symbol, tick, date_cls.fromisoformat(day), ppr, interval_sec)

    async def session_profile(self, date: str, row: float) -> Optional[dict]:
        records = await _replay_library().records(date)
        if not records:
            return None
        contract = STATE.get("contract")
        profile = SessionProfile(contract["tick_size"] if contract else 0.1, NSE_SESSION)
        for r in records:
            profile.add_trade(r["ts_ms"], r["ltp"], r["qty"], r["algo_side"])
        return build_profile(profile, row)

    # -- replay (server-side, same aggregation code as live) --------------------------

    async def replay_sessions(self) -> list:
        return await _replay_library().sessions()

    async def open_replay(self, date: str) -> ReplaySession:
        records = await _replay_library().records(date)
        if not records:
            raise ValueError(f"no trades recorded on {date}")
        contract = STATE.get("contract")
        session = ReplaySession(date, records, contract["tick_size"] if contract else 0.1)
        db, symbol = STATE.get("db_engine"), records[0].get("symbol")
        if db is not None and symbol:
            try:
                session.oi_before = await last_open_interest_before(db, "angelone", symbol,
                                                                    date_cls.fromisoformat(date))
            except Exception as e:                       # the dOI baseline is optional
                log.warning("replay OI baseline unavailable for %s: %r", date, e)
        return session

    def replay_snapshot(self, session: ReplaySession, ppr: int, interval_sec: int) -> dict:
        state = session.state
        contract = STATE.get("contract") or {}
        last = state.last_observation
        return {
            "ready": True,
            "mode": "replay",
            "source": _source_info(),
            "status": {"connected": True, "error": None, "tick_count": session.index, "symbol":
                       (last or {}).get("symbol") or f"Replay {session.date}",
                       "tick_size": session.tick_size, "lot_size": contract.get("lotsize", 65)},
            "book": _book_payload(state.book),
            "quote": {"ltp": last["ltp"], "open": None, "high": None, "low": None, "close": None,
                      "volume": last.get("cum_volume") or 0, "oi": last.get("oi")} if last else None,
            "chart": _chart_payload(state.footprint, state.cvd_tracker.cvd, state.last_candle_seen, ppr,
                                    interval_sec=_clamp_chart_interval(interval_sec),
                                    oi_before=getattr(session, "oi_before", None)),
            "replay": session.meta(),
        }

    def replay_profile_snapshot(self, session: ReplaySession, row: float) -> dict:
        snap = self.replay_snapshot(session, 1, config.CANDLE_INTERVAL_SEC)
        del snap["chart"], snap["book"], snap["source"]
        return {**snap, "view": "profile", "profile": _profile_view(session.state.profile, row)}


_REPLAY_LIBRARY: Optional[ReplayLibrary] = None


def _replay_library() -> ReplayLibrary:
    """Stored sessions for replay: the database when configured, else the
    JSONL session folders."""
    global _REPLAY_LIBRARY
    if _REPLAY_LIBRARY is None:
        db = STATE.get("db_engine")
        if db is not None:
            async def loader(date_str):
                return await load_trades_by_date(db, "angelone", date_cls.fromisoformat(date_str))

            async def lister():
                counts = await trade_counts_by_session(db, provider="angelone")
                return [{"date": d.isoformat(), "trades": n} for d, n in sorted(counts.items())]
        else:
            async def loader(date_str):
                return await asyncio.to_thread(load_session_records, date_str)

            async def lister():
                dates = await asyncio.to_thread(list_replayable_sessions)
                return [{"date": d, "trades": None} for d in dates]
        _REPLAY_LIBRARY = ReplayLibrary(loader, lister)
    return _REPLAY_LIBRARY


def _feed_health() -> Optional[dict]:
    feed = STATE.get("ws_client")
    health = getattr(feed, "health", None)
    return health() if callable(health) else None


# ---------------------------------------------------------------------------
# Contract switcher (the "Interval"-style dropdown, but for which instrument
# is live): every upcoming monthly expiry found in the Angel scrip master
# (auto-roll already guarantees the FRONT one is never stale — see
# angel_client.list_configured_futures).
#
# Scope: switches between EXPIRIES of the currently configured underlying
# (config.INSTRUMENT_NAME) only, not to a different underlying entirely.
# ---------------------------------------------------------------------------

ANGEL_CANDIDATE_MONTHS = 4   # how many upcoming Angel expiries /api/v1/market/contracts offers


async def _list_candidate_contracts() -> list:
    if config.SYNTHETIC_FEED:
        return [dict(SYNTHETIC_CONTRACT)]
    rows = await asyncio.to_thread(angel_client.list_configured_futures)
    return rows[:ANGEL_CANDIDATE_MONTHS]


async def _switch_contract(token: str) -> dict:
    """Hot-swaps the live feed to another contract of the list. Resets the live
    footprint / CVD / order book (they belong to one instrument's price
    series); stored sessions are untouched, and every stored trade carries the
    symbol that was active when it was recorded (see _on_tick)."""
    contract = next((c for c in await _list_candidate_contracts() if str(c.get("token")) == str(token)), None)
    if contract is None:
        raise ValueError(f"token {token} is not among the switchable contracts")
    # outside the broker session the engine switches but the feed stays off until the session starts
    broker = _broker()
    await _activate_contract(contract, start_feed=broker is None or broker.feed_on)
    STATE["contract_pinned"] = True      # the morning start keeps an admin's choice while it is unexpired
    return STATE["contract"]


# ---------------------------------------------------------------------------
# Retired legacy URLs: redirect to the terminal. The account pages keep their
# query string, so links in emails sent before the change still work.
# ---------------------------------------------------------------------------

_LEGACY_REDIRECTS = {"/": "/app/", "/chart": "/app/", "/replay": "/app/", "/login": "/app/login/",
                     "/reset-password": "/app/reset-password/", "/verify-email": "/app/verify-email/"}


def _legacy_redirect(target: str):
    async def redirect(request: Request):
        query = request.url.query
        return RedirectResponse(target + (f"?{query}" if query else ""), status_code=308)
    return redirect


for _path, _target in _LEGACY_REDIRECTS.items():
    app.add_api_route(_path, _legacy_redirect(_target), methods=["GET"], include_in_schema=False)


if os.path.isdir(config.FRONTEND_DIST):
    # The terminal (Next.js static export, basePath /app): same origin as the API and stream.
    app.mount("/app", StaticFiles(directory=config.FRONTEND_DIST, html=True), name="frontend")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
