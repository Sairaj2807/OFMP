"""/health, /live, /ready; /api/v1/market; /api/v1/admin.

Market data comes from app.state.market, a MarketGateway the hosting
application provides (see backend/app/api/gateway.py)."""
from datetime import datetime, timezone
from typing import Optional

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from backend.app.api.deps import get_auth_service, request_info, require_permission
from backend.app.api.schemas import Page
from backend.app.core.errors import AppError
from backend.app.domain.profile import DEFAULT_ROW, ROW_SIZES
from backend.app.infrastructure.postgres.schema import audit_logs
from backend.app.services.auth.service import AuthService, AuthUser

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
    calendar = request.app.state.market_calendar
    return {"contract": gw.active_contract(), "feed": gw.feed_status(),
            "session": calendar.status(datetime.now(timezone.utc))}


@market_router.get("/replay/sessions", dependencies=[Depends(require_permission("market.read"))],
                   summary="Stored sessions available for replay")
async def replay_sessions(request: Request):
    return {"data": await _gateway(request).replay_sessions()}


@market_router.get("/profile", dependencies=[Depends(require_permission("market.read"))],
                   summary="A stored session's market profile (TPO + volume at price)")
async def session_profile(request: Request, date: str = Query(pattern=r"^\d{4}-\d{2}-\d{2}$"),
                          row: int = Query(DEFAULT_ROW)):
    if row not in ROW_SIZES:
        raise AppError("INVALID_ROW", f"row must be one of {list(ROW_SIZES)}", 422)
    profile = await _gateway(request).session_profile(date, row)
    if profile is None:
        raise AppError("NOT_FOUND", f"no trades stored for {date}", 404)
    return profile


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


admin_system = require_permission("admin.system")


class ContractSwitch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str = Field(min_length=1, max_length=32, description="token from /api/v1/market/contracts")


@admin_router.post("/contract", summary="Switch the live feed to another contract (resets the live footprint)")
async def switch_contract(body: ContractSwitch, request: Request,
                          user: AuthUser = Depends(admin_system)):
    gw = _gateway(request)
    previous = (gw.active_contract() or {}).get("tradingsymbol")
    try:
        contract = await gw.switch_contract(body.token)
    except ValueError as e:
        raise AppError("UNKNOWN_CONTRACT", str(e), 422) from None
    engine = getattr(request.app.state, "db_engine", None)
    if engine is not None:
        info = request_info(request)
        async with engine.begin() as conn:
            await conn.execute(audit_logs.insert().values(
                actor_user_id=user.id, action="contract.switch", target_type="contract",
                target_id=contract.get("tradingsymbol"), ip=info.ip, user_agent=info.user_agent,
                request_id=info.request_id, details={"from": previous}))
    return {"contract": contract}


@admin_router.get("/system", dependencies=[Depends(require_permission("admin.system"))],
                  summary="Feed, database writer and connection details")
async def admin_system(request: Request):
    gw = getattr(request.app.state, "market", None)
    hub = getattr(request.app.state, "stream_hub", None)
    return {"feed": gw.feed_status() if gw else None,
            "database_writer": gw.database_stats() if gw else None,
            "websocket": hub.stats() if hub else None,
            "database": {True: "ok", False: "unavailable", None: "not_configured"}[await _database_ok(request)]}
