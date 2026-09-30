"""FastAPI app: resolves the NIFTY Aug future, runs the Angel One WS ingest as
a background task, and pushes processed order book + footprint snapshots to
any connected browser over /ws/frontend. Angel One tokens never leave this
process — only derived market data crosses the /ws/frontend boundary.
"""
import asyncio
import contextlib
import json
import os
import time
from datetime import date as date_cls
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import angel_client
import config
import replay_engine
from observation_store import (ObservationStore, list_replayable_sessions,
                               load_session_records, session_records_path)
from orderbook_engine import (TickProcessorState, group_candle_timestamps, group_native_bars,
                              poc_from_rows, process_tick, stacked_imbalances_from_rows,
                              value_area_from_rows)
from backend.app.api.app import configure_api, start_api
from backend.app.core import metrics
from backend.app.core.logging import configure_logging
from backend.app.domain.market_data import MarketTick
from backend.app.infrastructure.providers.synthetic import SYNTHETIC_CONTRACT, SyntheticProvider
from backend.app.domain.orderflow import Trade, restore_trades
from backend.app.infrastructure.postgres.database import create_engine as create_db_engine
from backend.app.infrastructure.postgres.repositories import (load_session_tape, load_trades_by_date,
                                                              trade_counts_by_session, upsert_instrument)
from backend.app.services.replay import ReplayLibrary, ReplaySession
from backend.app.infrastructure.postgres.rows import ist_date
from backend.app.infrastructure.postgres.writer import MarketDataWriter
from backend.app.services.market_data.feeds import EmbeddedFeed, RedisFeed
from backend.app.services.market_data.recorder import RawTickRecorder

class NoCacheStaticFiles(StaticFiles):
    """Forces the browser to always revalidate static assets instead of
    silently serving a stale cached app.js/index.html after an edit."""
    def file_response(self, *args, **kwargs):
        resp = super().file_response(*args, **kwargs)
        resp.headers["Cache-Control"] = "no-cache"
        return resp


configure_logging(config.LOG_FORMAT)

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
              legacy_auth_required=config.AUTH_REQUIRED, metrics_enabled=config.METRICS_ENABLED)

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
FRONTEND_CLIENTS: dict = {}   # websocket -> ppr (price-per-row, 1-5) requested by that client
CHART_CLIENTS: dict = {}      # same, for the order-flow chart page (/ws/chart)
CHART_BARS = 60               # footprint columns sent to the chart page (engine keeps MAX_CANDLES_KEPT)
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
    if result.get("new_trade") and engine.last_classification is not None:
        c = engine.last_classification
        metrics.TRADES_CLASSIFIED.labels(c.classifier_name, c.classifier_version, c.side).inc()
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
    client = LiveFeed(contract, on_tick=_on_tick, on_status=_on_status)
    return engine, client


async def _restore_session(engine: TickProcessorState, contract: dict) -> int:
    """Record the instrument and rebuild today's footprint/CVD for `contract`
    from stored trades (see orderflow.restore_trades). Best effort: a
    database problem is logged and the engine simply starts empty."""
    db = STATE.get("db_engine")
    if db is None or config.SYNTHETIC_FEED:
        return 0
    try:
        await upsert_instrument(db, contract, instrument_type=config.INSTRUMENT_TYPE)
        if not config.RESTORE_SESSION_ON_START:
            return 0
        tape, (bids, asks) = await load_session_tape(db, "angelone", contract["tradingsymbol"],
                                                     ist_date(int(time.time() * 1000)))
    except Exception as e:
        print(f"[server] Session restore skipped (database: {e!r})")
        return 0
    trades = [Trade(timestamp=ts, price=price, quantity=qty, side=side) for ts, price, qty, side in tape]
    return restore_trades(engine, trades, bids, asks)


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

    app.state.stream_task = start_api(
        app, db_engine=STATE["db_engine"], jwt_secret=config.JWT_SECRET, public_base_url=config.PUBLIC_BASE_URL,
        access_ttl_sec=config.ACCESS_TOKEN_TTL_SEC, refresh_ttl_sec=config.REFRESH_TOKEN_TTL_SEC,
        allow_registration=config.ALLOW_REGISTRATION, gateway=ServerMarketGateway(),
        allowed_intervals=config.ALLOWED_CHART_INTERVALS_SEC)
    if app.state.auth_service is None:
        print("[server] /api/v1 auth disabled: needs DATABASE_URL and JWT_SECRET")

    await _activate_contract(await _resolve_default_contract())

    app.state.broadcast_task = asyncio.create_task(_broadcast_loop())


@app.on_event("shutdown")
async def shutdown():
    ws_client = STATE.get("ws_client")
    if ws_client:
        ws_client.stop()
    for t in (getattr(app.state, "ws_task", None), getattr(app.state, "broadcast_task", None),
              getattr(app.state, "stream_task", None)):
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
    if STATE.get("db_engine") is not None:
        await STATE["db_engine"].dispose()


def _candles_payload(fp, cvd_by_candle: dict, ppr: int,
                     interval_sec: int = config.CANDLE_INTERVAL_SEC, limit: int = 30) -> dict:
    """Shared candle/imbalance serialization used by both the live snapshot
    and the replay snapshot — the two build different `fp`/`cvd_by_candle`
    but render identically on the frontend.

    `interval_sec` groups native (60s) candles the same way _chart_payload's
    group_native_bars does, but at the TIMESTAMP level
    (orderbook_engine.group_candle_timestamps) rather than pre-built bar
    dicts: POC/value-area/imbalances need Footprint.get_candle_rows access
    per native candle to merge correctly (Footprint.merged_candle_rows), not
    an already-flattened cell dict. At the default (native) interval, every
    group is a single timestamp, so this reproduces the pre-interval-selector
    behavior exactly — the pure rows-based helpers it now calls through
    Footprint.poc/value_area/stacked_imbalances give bit-identical results to
    those methods' previous inline implementations (see orderbook_engine's
    _poc_from_rows/_value_area_from_rows/_stacked_imbalances_from_rows)."""
    candle_ts_sorted = sorted(fp.data.keys())
    multiple = max(1, interval_sec // config.CANDLE_INTERVAL_SEC)
    native_window = candle_ts_sorted[-(limit * multiple):]
    groups = group_candle_timestamps(native_window, interval_sec, config.CANDLE_INTERVAL_SEC)[-limit:]

    candles = []
    for group in groups:
        rows = fp.merged_candle_rows(group, ppr)
        poc = poc_from_rows(rows)
        va = value_area_from_rows(rows, poc, config.VALUE_AREA_PCT)
        open_ohlc = fp.candle_ohlc.get(group[0])
        close_ohlc = fp.candle_ohlc.get(group[-1])
        # The running CVD "as of" this (possibly still-forming) group is the
        # last of its native candles that has actually CLOSED — cvd_by_candle
        # only ever has entries for closed candles (see _advance_candle), and
        # a group's later native minutes may not have closed yet even if its
        # earlier ones have.
        cvd = next((cvd_by_candle[t] for t in reversed(group) if t in cvd_by_candle), None)
        candles.append({
            "ts": group[0],
            "poc": poc,
            "value_area": list(va) if va else None,
            "delta": sum(fp.candle_delta(t) for t in group),
            "cvd": cvd,
            "bullish": (close_ohlc["close"] >= open_ohlc["open"]) if (open_ohlc and close_ohlc) else None,
            "close_row": fp.row_price_for(close_ohlc["close"], ppr) if close_ohlc else None,
            "rows": [{"price": row.price, "buy": row.buy_volume, "sell": row.sell_volume, "delta": row.delta}
                     for row in rows],
        })

    imbalances = (stacked_imbalances_from_rows(fp.merged_candle_rows(groups[-1], ppr),
                                               config.IMBALANCE_THRESHOLD, config.MIN_STACK)
                 if groups else [])
    return {
        "interval_sec": interval_sec,
        "candles": candles,
        "imbalances": [
            {"kind": stack[0][0], "prices": [p for _, p in stack]}
            for stack in imbalances
        ],
    }


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


def _build_snapshot(ppr: int = 1, interval_sec: int = config.CANDLE_INTERVAL_SEC) -> dict:
    engine: TickProcessorState = STATE["engine"]
    contract = STATE["contract"]
    if engine is None or contract is None:
        return {"ready": False}

    return {
        "ready": True,
        "status": _status_payload(engine, contract),
        "book": _book_payload(engine.book),
        "quote": _quote_payload(engine),
        "footprint": _candles_payload(engine.footprint, engine.cvd_by_candle, ppr, interval_sec=interval_sec),
    }


def _build_snapshot_from_state(state: tuple) -> dict:
    """FRONTEND_CLIENTS/ws_frontend store (ppr, interval_sec) per client, the
    same shape CHART_CLIENTS does — see _build_chart_snapshot_from_state."""
    ppr, interval_sec = state
    return _build_snapshot(ppr, interval_sec)


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


def _build_chart_snapshot_from_state(state: tuple) -> dict:
    """CHART_CLIENTS/ws_chart store (ppr, interval_sec) per client — this is
    the `build` callback _push_snapshots and _serve_state_socket call."""
    ppr, interval_sec = state
    return _build_chart_snapshot(ppr, interval_sec)


# ---------------------------------------------------------------------------
# Orderflow Replay verification API (see REPLAY_WORKFLOW.md).
#
# Serves saved sessions back as the same footprint/book shape the live
# dashboard renders, so /replay can scrub a past day's trades side by side
# with Vtrender's own Orderflow Replay for that date. Read-only: nothing
# here touches the live engine or the observation store's write path.
# ---------------------------------------------------------------------------

_REPLAY_CACHE: dict = {}  # date_str -> (path, mtime, records_list)


def _get_session_observations(date_str: str) -> list:
    """A day's saved Angel observations (see
    observation_store.session_records_path), cached until the file changes."""
    path = session_records_path(date_str)
    if path is None:
        return []
    mtime = os.path.getmtime(path)
    cached = _REPLAY_CACHE.get(date_str)
    if cached and cached[0] == path and cached[1] == mtime:
        return cached[2]
    observations = load_session_records(date_str)
    _REPLAY_CACHE[date_str] = (path, mtime, observations)
    return observations


def _trade_preview(obs: Optional[dict]) -> Optional[dict]:
    if obs is None:
        return None
    return {
        "trade_id": obs["trade_id"],
        "ts_ms": obs["ts_ms"],
        "ltp": obs["ltp"],
        **replay_engine.record_summary(obs),
    }


def _replay_source(observations: list) -> dict:
    """Source of a saved session — always Angel observations."""
    return _source_info()


def _replay_cursor(date_str: str, observations: list, state, tick_size: float,
                   cursor_ms: int) -> dict:
    """Session and neighbouring-trade fields shared by the replay footprint
    snapshot and the replay chart payload."""
    idx = state.trade_count - 1  # index into `observations` of the last trade included
    current_obs = observations[idx] if idx >= 0 else None
    prev_obs = observations[idx - 1] if idx - 1 >= 0 else None
    next_obs = observations[idx + 1] if idx + 1 < len(observations) else None
    return {
        "session": {
            "date": date_str,
            "start_ms": observations[0]["ts_ms"],
            "end_ms": observations[-1]["ts_ms"],
            "trade_count": len(observations),
            "cursor_ms": cursor_ms,
            "tick_size": tick_size,
        },
        "cursor_trade": _trade_preview(current_obs),
        "prev_trade": _trade_preview(prev_obs),
        "next_trade": _trade_preview(next_obs),
        "gap_to_next_ms": (next_obs["ts_ms"] - current_obs["ts_ms"])
            if (current_obs is not None and next_obs is not None) else None,
        "gap_from_prev_ms": (current_obs["ts_ms"] - prev_obs["ts_ms"])
            if (current_obs is not None and prev_obs is not None) else None,
    }


@app.get("/api/replay/sessions")
async def api_replay_sessions():
    return {"sessions": list_replayable_sessions()}


@app.get("/api/replay/{date}/meta")
async def api_replay_meta(date: str):
    observations = _get_session_observations(date)
    bounds = replay_engine.session_bounds(observations)
    if bounds is None:
        return {"found": False}
    contract = STATE["contract"]
    return {
        "found": True,
        "tick_size": contract["tick_size"] if contract else None,
        "lot_size": contract["lotsize"] if contract else None,
        **bounds,
    }


@app.get("/api/replay/{date}/snapshot")
async def api_replay_snapshot(date: str, as_of_ms: Optional[int] = None, ppr: int = 1,
                              interval: int = config.CANDLE_INTERVAL_SEC):
    observations = _get_session_observations(date)
    if not observations:
        return {"ready": False}

    contract = STATE["contract"]
    tick_size = contract["tick_size"] if contract else 0.05
    ppr = max(1, min(5, ppr))
    interval_sec = _clamp_chart_interval(interval)
    cursor_ms = as_of_ms if as_of_ms is not None else observations[-1]["ts_ms"]

    state = replay_engine.build_replay_state(observations, tick_size, cursor_ms)

    return {
        "ready": True,
        "book": _book_payload(state.book),
        "footprint": _candles_payload(state.footprint, state.cvd_by_candle, ppr, interval_sec=interval_sec),
        **_replay_cursor(date, observations, state, tick_size, cursor_ms),
    }


@app.get("/api/replay/{date}/chart")
async def api_replay_chart(date: str, as_of_ms: Optional[int] = None, ppr: int = 1,
                           interval: int = config.CANDLE_INTERVAL_SEC):
    """Same as /snapshot but with the order-flow chart's payload (see
    _chart_payload) instead of the footprint-table one. `interval` (seconds)
    is snapped to config.ALLOWED_CHART_INTERVALS_SEC — see
    orderbook_engine.group_native_bars."""
    observations = _get_session_observations(date)
    if not observations:
        return {"ready": False}

    contract = STATE["contract"]
    tick_size = contract["tick_size"] if contract else 0.05
    ppr = max(1, min(5, ppr))
    interval_sec = _clamp_chart_interval(interval)
    cursor_ms = as_of_ms if as_of_ms is not None else observations[-1]["ts_ms"]

    state = replay_engine.build_replay_state(observations, tick_size, cursor_ms)

    return {
        "ready": True,
        "mode": "replay",
        "source": _replay_source(observations),
        "book": _book_payload(state.book),
        "chart": _chart_payload(state.footprint, state.cvd_tracker.cvd,
                                state.last_candle_seen, ppr, interval_sec=interval_sec),
        "lot_size": contract["lotsize"] if contract else None,
        **_replay_cursor(date, observations, state, tick_size, cursor_ms),
    }


@app.get("/replay")
async def replay_page():
    return FileResponse("static/replay.html")


@app.get("/chart")
async def chart_page():
    return FileResponse("static/orderflow.html")


@app.get("/login", include_in_schema=False)
async def login_page():
    return FileResponse("static/login.html")


@app.get("/reset-password", include_in_schema=False)
async def reset_password_page():
    return FileResponse("static/reset-password.html")


@app.get("/verify-email", include_in_schema=False)
async def verify_email_page():
    return FileResponse("static/verify-email.html")


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

    def chart_snapshot(self, ppr: int, interval_sec: int) -> dict:
        return _build_chart_snapshot(ppr, interval_sec)

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


async def _push_snapshots(clients: dict, build) -> None:
    if not clients:
        return
    # build once per distinct state in use, not once per client. `state` is
    # whatever a client's parse callback produced (an int ppr for
    # FRONTEND_CLIENTS, a (ppr, interval_sec) tuple for CHART_CLIENTS) — both
    # hashable, so this dedup works unchanged for either.
    payload_by_state = {}
    dead = []
    for ws, state in list(clients.items()):   # copy: clients may connect mid-send
        payload = payload_by_state.get(state)
        if payload is None:
            payload = json.dumps(build(state))
            payload_by_state[state] = payload
        try:
            await ws.send_text(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        clients.pop(ws, None)


async def _broadcast_loop():
    while True:
        await asyncio.sleep(config.BROADCAST_INTERVAL_SEC)
        await _push_snapshots(FRONTEND_CLIENTS, _build_snapshot_from_state)
        await _push_snapshots(CHART_CLIENTS, _build_chart_snapshot_from_state)


def _parse_ppr_interval_message(msg, current: tuple) -> tuple:
    """{"ppr": n, "interval": seconds} parsing shared by /ws/frontend and
    /ws/chart — both send the same two-field message, and both pages always
    send both fields together (see sendState() in app.js/orderflow.js), so
    either one missing from a message keeps the CURRENT value rather than
    resetting to a default. (Pre-interval-selector, /ws/frontend's socket
    only ever carried "ppr" and a bare {} reset it to 1 — that one-field
    quirk doesn't carry over now that ppr and interval share one message.)"""
    if not isinstance(msg, dict):
        return current
    cur_ppr, cur_interval = current
    try:
        ppr = max(1, min(5, int(msg.get("ppr", cur_ppr))))
    except (ValueError, TypeError):
        ppr = cur_ppr
    interval_sec = _clamp_chart_interval(msg.get("interval", cur_interval))
    return (ppr, interval_sec)


async def _serve_state_socket(websocket: WebSocket, clients: dict, default_state, parse, build) -> None:
    """Accept a browser socket, send an immediate snapshot, and track
    whatever per-client `state` each incoming JSON message resolves to via
    `parse(msg, current_state) -> new_state | None` (None = message changed
    nothing, keep the current state)."""
    await websocket.accept()
    clients[websocket] = default_state
    try:
        await websocket.send_text(json.dumps(build(default_state)))
        while True:
            msg = await websocket.receive_text()
            try:
                msg_obj = json.loads(msg)
            except (ValueError, TypeError, json.JSONDecodeError):
                continue
            state = parse(msg_obj, clients[websocket])
            if state is not None:
                clients[websocket] = state
    except WebSocketDisconnect:
        pass
    finally:
        clients.pop(websocket, None)


@app.websocket("/ws/frontend")
async def ws_frontend(websocket: WebSocket):
    await _serve_state_socket(websocket, FRONTEND_CLIENTS, (1, config.CANDLE_INTERVAL_SEC),
                              _parse_ppr_interval_message, _build_snapshot_from_state)


@app.websocket("/ws/chart")
async def ws_chart(websocket: WebSocket):
    await _serve_state_socket(websocket, CHART_CLIENTS, (1, config.CANDLE_INTERVAL_SEC),
                              _parse_ppr_interval_message, _build_chart_snapshot_from_state)


@app.get("/api/status")
async def api_status():
    return {
        "contract": STATE["contract"],
        "connected": STATE["connected"],
        "error": STATE["last_error"],
        "tick_count": STATE["engine"].tick_count if STATE["engine"] else 0,
        "uptime_sec": time.time() - STATE["started_at"],
        "feed": _feed_health(),
        "database": STATE["db_writer"].stats() if STATE.get("db_writer") else None,
    }


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

ANGEL_CANDIDATE_MONTHS = 4   # how many upcoming Angel expiries /api/contracts offers


def _current_contract_key() -> tuple:
    contract = STATE.get("contract")
    if not contract:
        return (None, None)
    return (contract.get("token"), contract.get("tradingsymbol"))


async def _list_candidate_contracts() -> list:
    if config.SYNTHETIC_FEED:
        return [dict(SYNTHETIC_CONTRACT)]
    rows = await asyncio.to_thread(angel_client.list_configured_futures)
    return rows[:ANGEL_CANDIDATE_MONTHS]


@app.get("/api/contracts")
async def api_contracts():
    """Candidate contracts for the switcher dropdown, with the currently
    active one flagged so the frontend can preselect it."""
    contracts = await _list_candidate_contracts()
    cur_token, cur_symbol = _current_contract_key()
    for c in contracts:
        c["active"] = (c.get("token") == cur_token) if cur_token is not None else (c.get("tradingsymbol") == cur_symbol)
    return {"data_source": "angel", "contracts": contracts}


class ContractSwitchRequest(BaseModel):
    token: Optional[str] = None       # the Angel scrip-master token to switch to


@app.post("/api/contract")
async def api_switch_contract(payload: ContractSwitchRequest):
    """Hot-swaps the live feed to a different contract from the /api/contracts
    list. Resets the live footprint/CVD/order book (see _activate_contract —
    they're specific to one instrument's price series and can't carry over);
    never touches saved sessions, and observation_store keeps logging under
    today's date, each record now tagged with whichever symbol was active
    when it was recorded (see _on_tick)."""
    if not payload.token:
        return {"ok": False, "error": "token is required for the angel data source"}
    rows = await asyncio.to_thread(angel_client.list_configured_futures)
    contract = next((r for r in rows if str(r["token"]) == str(payload.token)), None)
    if contract is None:
        return {"ok": False, "error": f"token {payload.token} not found among unexpired contracts"}

    await _activate_contract(contract)
    return {"ok": True, "contract": STATE["contract"]}


@app.get("/")
async def index():
    return FileResponse("static/index.html")


app.mount("/static", NoCacheStaticFiles(directory="static"), name="static")
if os.path.isdir(config.FRONTEND_DIST):
    # The terminal (Next.js static export, basePath /app): same origin as the API and stream.
    app.mount("/app", StaticFiles(directory=config.FRONTEND_DIST, html=True), name="frontend")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
