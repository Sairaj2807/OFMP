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
import os
import time
from datetime import date as date_cls
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

import angel_client
import config
from observation_store import ObservationStore, list_replayable_sessions, load_session_records
from orderbook_engine import TickProcessorState, group_native_bars, process_tick
from backend.app.api.app import configure_api, start_api
from backend.app.core import metrics
from backend.app.core.logging import configure_logging
from backend.app.domain.market_data import MarketTick
from backend.app.infrastructure.providers.synthetic import SYNTHETIC_CONTRACT, SyntheticProvider
from backend.app.domain.orderflow import Trade, restore_trades
from backend.app.domain.profile import ALL_DAY_SESSION, NSE_SESSION, SessionProfile, build_profile
from backend.app.infrastructure.postgres.database import create_engine as create_db_engine
from backend.app.infrastructure.postgres.repositories import (load_session_tape, load_trades_by_date,
                                                              previous_session_date, trade_counts_by_session,
                                                              upsert_instrument)
from backend.app.services.replay import ReplayLibrary, ReplaySession
from backend.app.infrastructure.postgres.rows import ist_date
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
    try:
        await upsert_instrument(db, contract, instrument_type=config.INSTRUMENT_TYPE)
        if not config.RESTORE_SESSION_ON_START:
            return 0
        tape, (bids, asks) = await load_session_tape(db, "angelone", symbol, today)
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
    for ts, price, qty, side in tape:
        engine.profile.add_trade(ts, price, qty, side)
    trades = [Trade(timestamp=ts, price=price, quantity=qty, side=side) for ts, price, qty, side in tape]
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


async def _activate_contract(contract: dict) -> None:
    """Swaps in `contract` as the live one: stops whatever ws_client is
    currently running (a no-op the first time, at startup), builds a fresh
    engine, updates STATE, and starts ingesting. Serialized by
    CONTRACT_SWITCH_LOCK so two switch requests (or a switch racing startup)
    can't interleave their teardown/startup."""
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
        STATE["ws_client"] = client
        app.state.ws_task = asyncio.create_task(client.run())
        print(f"[server] Active contract: {contract['tradingsymbol']} "
              f"(token {contract.get('token')}, tick size {contract['tick_size']})"
              + (f", restored {restored} trades from today's session" if restored else ""))


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

    await _activate_contract(await _resolve_default_contract())


@app.on_event("shutdown")
async def shutdown():
    ws_client = STATE.get("ws_client")
    if ws_client:
        ws_client.stop()
    for t in (getattr(app.state, "ws_task", None),
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
                   limit: int = CHART_BARS) -> dict:
    """The last `limit` candles as footprint columns, at `interval_sec` (one of
    config.ALLOWED_CHART_INTERVALS_SEC — see group_native_bars for why the
    engine itself never buckets at anything but the native 60s interval).

    `closed_cvd` is the engine's CVD over every CLOSED native candle
    (cvd_tracker.cvd), `open_ts` the still-forming native candle
    (last_candle_seen). The chart library accumulates CVD itself from
    `cvd_offset` across the bars it is given (including the forming one), so
    cvd_offset is the CVD just before the first bar sent: everything closed,
    minus the closed NATIVE candles in the native window this pulls from
    (grouping a closed and a still-forming native candle into one coarser
    display bar doesn't change which native candles are closed).
    Cell prices are the PPR band FLOOR, as everywhere else in the engine; the
    adapter shifts them to the band centre, which is what the library draws
    around."""
    row_size = max(ppr, fp.tick_size)
    multiple = max(1, interval_sec // config.CANDLE_INTERVAL_SEC)
    window = sorted(fp.data.keys())[-(limit * multiple):]
    native_bars = []
    closed_in_window = 0
    for ts in window:
        delta = fp.candle_delta(ts)
        if ts != open_ts:
            closed_in_window += delta
        ohlc = fp.candle_ohlc.get(ts) or {}
        stats = fp.candle_stats.get(ts) or {}
        native_bars.append({
            "time": ts,
            "open": ohlc.get("open"), "high": ohlc.get("high"),
            "low": ohlc.get("low"), "close": ohlc.get("close"),
            "delta": delta,
            "min_delta": stats.get("min"), "max_delta": stats.get("max"),
            "trades": stats.get("trades"),
            "cells": [{"price": r.price, "buy": r.buy_volume, "sell": r.sell_volume}
                      for r in fp.get_candle_rows(ts, ppr)],
        })
    bars = group_native_bars(native_bars, interval_sec, config.CANDLE_INTERVAL_SEC)[-limit:]
    return {"row_size": row_size, "interval_sec": interval_sec,
            "cvd_offset": closed_cvd - closed_in_window, "bars": bars}


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
                                engine.last_candle_seen, ppr, interval_sec=interval_sec),
    }


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
        return ReplaySession(date, records, contract["tick_size"] if contract else 0.1)

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
                      "volume": last.get("cum_volume") or 0, "oi": None} if last else None,
            "chart": _chart_payload(state.footprint, state.cvd_tracker.cvd, state.last_candle_seen, ppr,
                                    interval_sec=_clamp_chart_interval(interval_sec)),
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
    await _activate_contract(contract)
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
