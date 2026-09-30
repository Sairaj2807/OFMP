"""/health, /live, /ready; /api/v1/market; /api/v1/admin.

Market data comes from app.state.market, a MarketGateway the hosting
application provides (see backend/app/api/gateway.py)."""
from typing import Optional

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse

from backend.app.api.deps import get_auth_service, require_permission
from backend.app.api.schemas import Page
from backend.app.core.errors import AppError
from backend.app.services.auth.service import AuthService

health_router = APIRouter(tags=["health"])
market_router = APIRouter(prefix="/api/v1/market", tags=["market"])
admin_router = APIRouter(prefix="/api/v1/admin", tags=["admin"])


def _gateway(request: Request):
    gw = getattr(request.app.state, "market", None)
    if gw is None:
        raise AppError("MARKET_UNAVAILABLE", "market data is not available", 503)
    return gw


async def _database_ok(request: Request) -> Optional[bool]:
    engine = getattr(request.app.state, "db_engine", None)
    if engine is None:
        return None                      # not configured
    try:
        async with engine.connect() as conn:
            await conn.execute(sa.text("SELECT 1"))
        return True
    except Exception:
        return False


# ---- health ----------------------------------------------------------------------

@health_router.get("/live", summary="Process is alive")
async def live():
    return {"status": "alive"}


@health_router.get("/ready", summary="Ready to serve: dependencies reachable")
async def ready(request: Request):
    db = await _database_ok(request)
    gw = getattr(request.app.state, "market", None)
    feed = gw.feed_status() if gw is not None else None
    checks = {"database": {True: "ok", False: "unavailable", None: "not_configured"}[db],
              "market_feed": "ok" if feed and feed.get("connected") else ("not_configured" if feed is None else "disconnected")}
    is_ready = db is not False           # the feed being down (e.g. market closed) does not make the API unready
    return JSONResponse({"status": "ready" if is_ready else "not_ready", "checks": checks},
                        status_code=200 if is_ready else 503)


@health_router.get("/health", summary="Summary of process and dependency health")
async def health(request: Request):
    db = await _database_ok(request)
    gw = getattr(request.app.state, "market", None)
    return {"status": "ok" if db is not False else "degraded",
            "database": {True: "ok", False: "unavailable", None: "not_configured"}[db],
            "market_feed": gw.feed_status() if gw is not None else None,
            "environment": request.app.state.environment}


# ---- market ------------------------------------------------------------------------

@market_router.get("/status", dependencies=[Depends(require_permission("market.read"))],
                   summary="Active contract and live feed status")
async def market_status(request: Request):
    gw = _gateway(request)
    return {"contract": gw.active_contract(), "feed": gw.feed_status()}


@market_router.get("/replay/sessions", dependencies=[Depends(require_permission("market.read"))],
                   summary="Stored sessions available for replay")
async def replay_sessions(request: Request):
    return {"data": await _gateway(request).replay_sessions()}


@market_router.get("/contracts", dependencies=[Depends(require_permission("market.read"))],
                   summary="Contracts available to switch to")
async def market_contracts(request: Request):
    return {"data": await _gateway(request).list_contracts()}


# ---- admin --------------------------------------------------------------------------

@admin_router.get("/users", response_model=Page, dependencies=[Depends(require_permission("admin.users"))],
                  summary="Users, cursor-paginated by email")
async def admin_users(limit: int = Query(50, ge=1, le=200), cursor: Optional[str] = Query(None, max_length=254),
                      svc: AuthService = Depends(get_auth_service)):
    items, next_cursor = await svc.list_users(limit, cursor)
    return Page(data=items, next_cursor=next_cursor)


@admin_router.get("/system", dependencies=[Depends(require_permission("admin.system"))],
                  summary="Feed, database writer and connection details")
async def admin_system(request: Request):
    gw = getattr(request.app.state, "market", None)
    hub = getattr(request.app.state, "stream_hub", None)
    return {"feed": gw.feed_status() if gw else None,
            "database_writer": gw.database_stats() if gw else None,
            "websocket": hub.stats() if hub else None,
            "database": {True: "ok", False: "unavailable", None: "not_configured"}[await _database_ok(request)]}
