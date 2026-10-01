"""/api/v1/alerts — the signed-in user's alert rules, notification channels
and alert history. Rules are evaluated on live data only (see
backend/app/services/alerts/runtime.py); changes take effect immediately."""
import uuid
from datetime import datetime
from typing import Literal, Optional

from fastapi import APIRouter, Depends, Query, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.app.api.deps import require_permission
from backend.app.core.errors import AppError
from backend.app.domain.alerts import (CANDLE_KINDS, DEFAULT_COOLDOWN_SEC, KINDS, MAX_COOLDOWN_SEC,
                                       MIN_COOLDOWN_SEC, MODES, TRADE_KINDS)
from backend.app.services.alerts.delivery import check_webhook_url, webhook_secret
from backend.app.services.alerts.service import (MAX_CHANNELS_PER_USER, MAX_RULES_PER_USER, AlertService)
from backend.app.services.auth.service import AuthUser

router = APIRouter(prefix="/api/v1/alerts", tags=["alerts"])

NAME_RULES = {"min_length": 1, "max_length": 80, "pattern": r"^[^\x00-\x1f]+$"}   # no control characters
Kind = Literal[KINDS]                       # type: ignore[valid-type]
Mode = Literal[MODES]                       # type: ignore[valid-type]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class RuleCreate(_Strict):
    name: str = Field(**NAME_RULES)
    kind: Kind
    params: dict
    mode: Mode = "repeat"
    cooldown_sec: int = Field(DEFAULT_COOLDOWN_SEC, ge=MIN_COOLDOWN_SEC, le=MAX_COOLDOWN_SEC)
    channel_ids: list[uuid.UUID] = Field(default_factory=list, max_length=MAX_CHANNELS_PER_USER)
    enabled: bool = True


class RuleUpdate(_Strict):
    revision: int = Field(ge=1, description="the revision this change is based on")
    name: Optional[str] = Field(None, **NAME_RULES)
    kind: Optional[Kind] = None
    params: Optional[dict] = None
    mode: Optional[Mode] = None
    cooldown_sec: Optional[int] = Field(None, ge=MIN_COOLDOWN_SEC, le=MAX_COOLDOWN_SEC)
    channel_ids: Optional[list[uuid.UUID]] = Field(None, max_length=MAX_CHANNELS_PER_USER)
    enabled: Optional[bool] = None

    @model_validator(mode="after")
    def _kind_needs_params(self):
        if self.kind is not None and self.params is None:
            raise ValueError("changing kind needs params")
        return self


class RuleOut(BaseModel):
    id: str
    name: str
    kind: str
    params: dict
    description: str
    mode: str
    cooldown_sec: int
    channel_ids: list[str]
    enabled: bool
    revision: int
    fire_count: int
    last_fired_at: Optional[datetime]
    created_at: datetime
    updated_at: datetime


class ChannelCreate(_Strict):
    name: str = Field(**NAME_RULES)
    url: str = Field(min_length=8, max_length=2048)


class ChannelOut(BaseModel):
    id: str
    kind: str
    name: str
    url: Optional[str]
    enabled: bool
    last_status: Optional[str]
    last_error: Optional[str]
    last_delivery_at: Optional[datetime]
    created_at: datetime


class ChannelCreated(ChannelOut):
    secret: str = Field(description="HMAC signing secret; shown only once")


class EventOut(BaseModel):
    id: int
    rule_id: str
    fired_at: datetime
    kind: str
    symbol: Optional[str]
    message: str
    value: Optional[float]
    details: Optional[dict]
    delivery: Optional[dict]
    suppressed: Optional[str]
    read_at: Optional[datetime]


class MarkRead(_Strict):
    ids: Optional[list[int]] = Field(None, max_length=500, description="omit to mark everything read")


def get_service(request: Request) -> AlertService:
    svc = getattr(request.app.state, "alert_service", None)
    if svc is None:
        raise AppError("ALERTS_UNAVAILABLE", "alerts need the database (DATABASE_URL)", 503)
    return svc


def _runtime(request: Request):
    return getattr(request.app.state, "alert_runtime", None)


read = require_permission("alerts.read")
create = require_permission("alerts.create")
delete = require_permission("alerts.delete")


@router.get("/kinds", summary="Condition kinds and limits, for building a rule form")
async def kinds(request: Request, user: AuthUser = Depends(read)):
    return {"trade_kinds": list(TRADE_KINDS), "candle_kinds": list(CANDLE_KINDS), "modes": list(MODES),
            "intervals": list(get_service(request).allowed_intervals),
            "cooldown_sec": {"min": MIN_COOLDOWN_SEC, "max": MAX_COOLDOWN_SEC, "default": DEFAULT_COOLDOWN_SEC},
            "max_rules": MAX_RULES_PER_USER, "max_channels": MAX_CHANNELS_PER_USER}


# -- rules ------------------------------------------------------------------------------

@router.get("/rules", response_model=list[RuleOut])
async def list_rules(user: AuthUser = Depends(read), svc: AlertService = Depends(get_service)):
    return await svc.list_rules(user.id)


@router.post("/rules", response_model=RuleOut, status_code=status.HTTP_201_CREATED)
async def create_rule(body: RuleCreate, request: Request, user: AuthUser = Depends(create),
                      svc: AlertService = Depends(get_service)):
    out, rule = await svc.create_rule(user.id, name=body.name, kind=body.kind, params=body.params,
                                      mode=body.mode, cooldown_sec=body.cooldown_sec,
                                      channel_ids=body.channel_ids, enabled=body.enabled)
    if (rt := _runtime(request)) is not None:
        rt.upsert(rule if body.enabled else None, out["id"])
    return out


@router.get("/rules/{rule_id}", response_model=RuleOut)
async def get_rule(rule_id: uuid.UUID, user: AuthUser = Depends(read), svc: AlertService = Depends(get_service)):
    return await svc.get_rule(user.id, rule_id)


@router.patch("/rules/{rule_id}", response_model=RuleOut,
              summary="Change a rule (optimistic concurrency on revision); enabled=false pauses it")
async def update_rule(rule_id: uuid.UUID, body: RuleUpdate, request: Request, user: AuthUser = Depends(create),
                      svc: AlertService = Depends(get_service)):
    out, rule = await svc.update_rule(user.id, rule_id, body.revision, name=body.name, kind=body.kind,
                                      params=body.params, mode=body.mode, cooldown_sec=body.cooldown_sec,
                                      channel_ids=body.channel_ids, enabled=body.enabled)
    if (rt := _runtime(request)) is not None:
        rt.upsert(rule, out["id"])
    return out


@router.delete("/rules/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_rule(rule_id: uuid.UUID, request: Request, user: AuthUser = Depends(delete),
                      svc: AlertService = Depends(get_service)):
    await svc.delete_rule(user.id, rule_id)
    if (rt := _runtime(request)) is not None:
        rt.upsert(None, str(rule_id))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# -- history ----------------------------------------------------------------------------

@router.get("/events", response_model=list[EventOut], summary="Alert history, newest first")
async def list_events(user: AuthUser = Depends(read), svc: AlertService = Depends(get_service),
                      limit: int = Query(50, ge=1, le=200), before: Optional[int] = Query(None, ge=1),
                      unread: bool = False):
    return await svc.list_events(user.id, limit=limit, before_id=before, unread_only=unread)


@router.get("/events/unread-count")
async def unread_count(user: AuthUser = Depends(read), svc: AlertService = Depends(get_service)):
    return {"unread": await svc.unread_count(user.id)}


@router.post("/events/read", summary="Mark alerts read (the given ids, or all)")
async def mark_read(body: MarkRead, user: AuthUser = Depends(read), svc: AlertService = Depends(get_service)):
    return {"updated": await svc.mark_read(user.id, body.ids)}


# -- channels ---------------------------------------------------------------------------

@router.get("/channels", response_model=list[ChannelOut])
async def list_channels(user: AuthUser = Depends(read), svc: AlertService = Depends(get_service)):
    return await svc.list_channels(user.id)


@router.post("/channels", response_model=ChannelCreated, status_code=status.HTTP_201_CREATED,
             summary="Add a webhook. The response carries its signing secret, shown only once")
async def create_channel(body: ChannelCreate, request: Request, user: AuthUser = Depends(create),
                         svc: AlertService = Depends(get_service)):
    secret_key = request.app.state.alert_signing_secret
    if not secret_key:
        raise AppError("WEBHOOKS_UNAVAILABLE", "webhooks need JWT_SECRET to sign deliveries", 503)
    try:
        check_webhook_url(body.url, allow_insecure=request.app.state.alert_webhooks_allow_private)
    except ValueError as e:
        raise AppError("INVALID_URL", str(e), 422) from None
    out = await svc.create_channel(user.id, name=body.name, url=body.url)
    return {**out, "secret": webhook_secret(secret_key, out["id"])}


@router.delete("/channels/{channel_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_channel(channel_id: uuid.UUID, request: Request, user: AuthUser = Depends(delete),
                         svc: AlertService = Depends(get_service)):
    await svc.delete_channel(user.id, channel_id)
    if (rt := _runtime(request)) is not None:
        await rt.reload()            # rules that used it no longer list it
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/channels/{channel_id}/test", summary="Send a signed test delivery to a webhook")
async def test_channel(channel_id: uuid.UUID, request: Request, user: AuthUser = Depends(create),
                       svc: AlertService = Depends(get_service)):
    request.app.state.limiters["alert_test"].check(str(user.id))
    channel = await svc.get_channel(user.id, channel_id)
    provider = getattr(request.app.state, "webhook_provider", None)
    if provider is None:
        raise AppError("WEBHOOKS_UNAVAILABLE", "webhooks need JWT_SECRET to sign deliveries", 503)
    event = {"id": f"test-{uuid.uuid4()}", "rule_id": None, "rule_name": "Test", "kind": "test",
             "message": "Test delivery from OFMP", "value": None, "symbol": None,
             "fired_at": datetime.now().astimezone().isoformat(), "details": {}}
    ok, err = await provider.deliver(str(user.id), event, (channel["id"], "webhook", {"url": channel["url"]}))
    await svc.channel_result(channel["id"], ok, err)
    return {"ok": ok, "error": err}
