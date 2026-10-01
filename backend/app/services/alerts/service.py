"""Alert rules, notification channels and the alert history, per user.

Same ownership model as workspaces: every operation is scoped to the calling
user and another user's rule/channel/event is reported exactly like a missing
one (NOT_FOUND). Rule updates use optimistic concurrency on `revision`.
Deletes are soft for rules and channels; history rows reference the rule
(CASCADE only applies to a hard delete by an operator).

Recording a firing (record_firing) is idempotent on (rule_id, dedup_key): the
second insert of the same market event is ignored and returns None, so a
firing is delivered at most once even when several processes evaluate the
same tape."""
import uuid
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine

from backend.app.core.errors import AppError
from backend.app.domain.alerts import AlertRule, Firing, describe, validate_params
from backend.app.infrastructure.postgres.schema import (alert_events, alert_rules, audit_logs,
                                                        notification_channels, organization_members,
                                                        organizations)

MAX_RULES_PER_USER = 50
MAX_CHANNELS_PER_USER = 5
EVENT_RETENTION_DAYS = 90


def _rule_public(row) -> dict:
    return {"id": str(row.id), "name": row.name, "kind": row.kind, "params": row.params,
            "description": describe(row.kind, row.params), "mode": row.mode, "cooldown_sec": row.cooldown_sec,
            "channel_ids": [str(c) for c in row.channel_ids], "enabled": row.enabled, "revision": row.revision,
            "fire_count": row.fire_count, "last_fired_at": row.last_fired_at, "created_at": row.created_at,
            "updated_at": row.updated_at}


def _channel_public(row) -> dict:
    return {"id": str(row.id), "kind": row.kind, "name": row.name, "url": row.config.get("url"),
            "enabled": row.enabled, "last_status": row.last_status, "last_error": row.last_error,
            "last_delivery_at": row.last_delivery_at, "created_at": row.created_at}


def event_public(row) -> dict:
    return {"id": row.id, "rule_id": str(row.rule_id), "fired_at": row.fired_at, "kind": row.kind,
            "symbol": row.symbol, "message": row.message, "value": row.value, "details": row.details,
            "delivery": row.delivery, "suppressed": row.suppressed, "read_at": row.read_at}


def to_domain(row) -> AlertRule:
    return AlertRule(id=str(row.id), owner_user_id=str(row.owner_user_id), name=row.name, kind=row.kind,
                     params=dict(row.params), mode=row.mode, cooldown_sec=row.cooldown_sec)


class AlertService:
    def __init__(self, engine: AsyncEngine, allowed_intervals: tuple,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self.engine = engine
        self.allowed_intervals = tuple(allowed_intervals)
        self.now = clock

    # -- helpers --------------------------------------------------------------------

    def _params(self, kind: str, params: dict) -> dict:
        try:
            return validate_params(kind, params, self.allowed_intervals)
        except ValueError as e:
            raise AppError("INVALID_RULE", str(e), 422) from None

    async def _personal_org(self, conn, user_id) -> uuid.UUID:
        org = (await conn.execute(
            sa.select(organizations.c.id)
            .join(organization_members, organization_members.c.organization_id == organizations.c.id)
            .where(organization_members.c.user_id == user_id, organizations.c.is_personal.is_(True))
            .limit(1))).scalar_one_or_none()
        if org is None:
            raise AppError("NO_ORGANIZATION", "account has no organization", 409)
        return org

    async def _owned_rule(self, conn, user_id, rule_id, lock: bool = False):
        stmt = sa.select(alert_rules).where(alert_rules.c.id == rule_id, alert_rules.c.owner_user_id == user_id,
                                            alert_rules.c.deleted_at.is_(None))
        row = (await conn.execute(stmt.with_for_update() if lock else stmt)).first()
        if row is None:
            raise AppError("NOT_FOUND", "alert rule not found", 404)
        return row

    async def _check_channels(self, conn, user_id, channel_ids: list) -> list:
        ids = list(dict.fromkeys(channel_ids))
        if not ids:
            return []
        found = set((await conn.execute(sa.select(notification_channels.c.id).where(
            notification_channels.c.id.in_(ids), notification_channels.c.owner_user_id == user_id,
            notification_channels.c.deleted_at.is_(None)))).scalars())
        if len(found) != len(ids):
            raise AppError("INVALID_CHANNEL", "unknown notification channel", 422)
        return ids

    async def _audit(self, conn, user_id, action: str, target_type: str, target_id, details=None) -> None:
        await conn.execute(audit_logs.insert().values(occurred_at=self.now(), actor_user_id=user_id, action=action,
                                                      target_type=target_type, target_id=str(target_id),
                                                      details=details))

    # -- rules -----------------------------------------------------------------------

    async def list_rules(self, user_id) -> list:
        async with self.engine.connect() as conn:
            rows = (await conn.execute(sa.select(alert_rules).where(
                alert_rules.c.owner_user_id == user_id, alert_rules.c.deleted_at.is_(None))
                .order_by(alert_rules.c.created_at))).all()
        return [_rule_public(r) for r in rows]

    async def get_rule(self, user_id, rule_id) -> dict:
        async with self.engine.connect() as conn:
            return _rule_public(await self._owned_rule(conn, user_id, rule_id))

    async def create_rule(self, user_id, *, name: str, kind: str, params: dict, mode: str, cooldown_sec: int,
                          channel_ids: list, enabled: bool = True) -> tuple:
        """Returns (public dict, domain rule)."""
        params = self._params(kind, params)
        async with self.engine.begin() as conn:
            count = (await conn.execute(sa.select(sa.func.count()).select_from(alert_rules).where(
                alert_rules.c.owner_user_id == user_id, alert_rules.c.deleted_at.is_(None)))).scalar_one()
            if count >= MAX_RULES_PER_USER:
                raise AppError("LIMIT_REACHED", f"at most {MAX_RULES_PER_USER} alert rules", 409)
            row = (await conn.execute(alert_rules.insert().values(
                owner_user_id=user_id, organization_id=await self._personal_org(conn, user_id), name=name,
                kind=kind, params=params, mode=mode, cooldown_sec=cooldown_sec,
                channel_ids=await self._check_channels(conn, user_id, channel_ids), enabled=enabled,
                created_at=self.now(), updated_at=self.now()).returning(*alert_rules.c))).first()
            await self._audit(conn, user_id, "alert_rule.create", "alert_rule", row.id, {"kind": kind})
        return _rule_public(row), to_domain(row)

    async def update_rule(self, user_id, rule_id, expected_revision: int, **changes) -> tuple:
        """`changes`: any of name, kind, params, mode, cooldown_sec, channel_ids, enabled (None = keep).
        Returns (public dict, domain rule or None when the rule is now disabled)."""
        changes = {k: v for k, v in changes.items() if v is not None}
        async with self.engine.begin() as conn:
            row = await self._owned_rule(conn, user_id, rule_id, lock=True)
            if row.revision != expected_revision:
                raise AppError("REVISION_CONFLICT", "alert rule changed since it was loaded; reload it", 409)
            if "kind" in changes or "params" in changes:
                changes["kind"] = changes.get("kind", row.kind)
                changes["params"] = self._params(changes["kind"], changes.get("params", row.params))
            if "channel_ids" in changes:
                changes["channel_ids"] = await self._check_channels(conn, user_id, changes["channel_ids"])
            updated = (await conn.execute(alert_rules.update().where(alert_rules.c.id == row.id).values(
                **changes, revision=row.revision + 1, updated_at=self.now()).returning(*alert_rules.c))).first()
        return _rule_public(updated), (to_domain(updated) if updated.enabled else None)

    async def delete_rule(self, user_id, rule_id) -> None:
        async with self.engine.begin() as conn:
            row = await self._owned_rule(conn, user_id, rule_id, lock=True)
            await conn.execute(alert_rules.update().where(alert_rules.c.id == row.id)
                               .values(deleted_at=self.now(), enabled=False))
            await self._audit(conn, user_id, "alert_rule.delete", "alert_rule", row.id)

    async def enabled_rules(self) -> list:
        """Every enabled rule of every active user, as domain rules (the live evaluator's rule set)."""
        async with self.engine.connect() as conn:
            rows = (await conn.execute(sa.select(alert_rules).where(
                alert_rules.c.deleted_at.is_(None), alert_rules.c.enabled.is_(True)))).all()
        return [to_domain(r) for r in rows]

    async def rule_channels(self, rule_id) -> list:
        """The enabled channels a rule delivers to: [(id, kind, config)]."""
        async with self.engine.connect() as conn:
            ids = (await conn.execute(sa.select(alert_rules.c.channel_ids)
                                      .where(alert_rules.c.id == rule_id))).scalar_one_or_none() or []
            if not ids:
                return []
            rows = (await conn.execute(sa.select(notification_channels).where(
                notification_channels.c.id.in_(ids), notification_channels.c.deleted_at.is_(None),
                notification_channels.c.enabled.is_(True)))).all()
        return [(str(r.id), r.kind, dict(r.config)) for r in rows]

    # -- channels --------------------------------------------------------------------

    async def list_channels(self, user_id) -> list:
        async with self.engine.connect() as conn:
            rows = (await conn.execute(sa.select(notification_channels).where(
                notification_channels.c.owner_user_id == user_id, notification_channels.c.deleted_at.is_(None))
                .order_by(notification_channels.c.created_at))).all()
        return [_channel_public(r) for r in rows]

    async def get_channel(self, user_id, channel_id) -> dict:
        async with self.engine.connect() as conn:
            row = (await conn.execute(sa.select(notification_channels).where(
                notification_channels.c.id == channel_id, notification_channels.c.owner_user_id == user_id,
                notification_channels.c.deleted_at.is_(None)))).first()
        if row is None:
            raise AppError("NOT_FOUND", "notification channel not found", 404)
        return _channel_public(row)

    async def create_channel(self, user_id, *, name: str, url: str) -> dict:
        async with self.engine.begin() as conn:
            count = (await conn.execute(sa.select(sa.func.count()).select_from(notification_channels).where(
                notification_channels.c.owner_user_id == user_id,
                notification_channels.c.deleted_at.is_(None)))).scalar_one()
            if count >= MAX_CHANNELS_PER_USER:
                raise AppError("LIMIT_REACHED", f"at most {MAX_CHANNELS_PER_USER} notification channels", 409)
            row = (await conn.execute(notification_channels.insert().values(
                owner_user_id=user_id, kind="webhook", name=name, config={"url": url},
                created_at=self.now()).returning(*notification_channels.c))).first()
            await self._audit(conn, user_id, "notification_channel.create", "notification_channel", row.id)
        return _channel_public(row)

    async def delete_channel(self, user_id, channel_id) -> None:
        async with self.engine.begin() as conn:
            res = await conn.execute(notification_channels.update().where(
                notification_channels.c.id == channel_id, notification_channels.c.owner_user_id == user_id,
                notification_channels.c.deleted_at.is_(None)).values(deleted_at=self.now(), enabled=False))
            if res.rowcount == 0:
                raise AppError("NOT_FOUND", "notification channel not found", 404)
            await conn.execute(alert_rules.update().where(alert_rules.c.owner_user_id == user_id,
                                                          sa.any_(alert_rules.c.channel_ids) == channel_id)
                               .values(channel_ids=sa.func.array_remove(alert_rules.c.channel_ids, channel_id)))
            await self._audit(conn, user_id, "notification_channel.delete", "notification_channel", channel_id)

    async def channel_result(self, channel_id: str, ok: bool, error: Optional[str]) -> None:
        async with self.engine.begin() as conn:
            await conn.execute(notification_channels.update().where(notification_channels.c.id == channel_id)
                               .values(last_status="ok" if ok else "failed", last_error=error,
                                       last_delivery_at=self.now()))

    # -- history ---------------------------------------------------------------------

    async def record_firing(self, f: Firing, symbol: Optional[str], suppressed: Optional[str] = None):
        """Inserts the event and bumps the rule's counters. None if this
        (rule, dedup_key) was already recorded or the rule no longer exists."""
        async with self.engine.begin() as conn:
            row = (await conn.execute(pg_insert(alert_events).values(
                rule_id=f.rule_id, owner_user_id=f.owner_user_id,
                fired_at=datetime.fromtimestamp(f.ts_ms / 1000, timezone.utc), dedup_key=f.dedup_key,
                kind=f.kind, symbol=symbol, message=f.message, value=f.value, details=f.details,
                delivery={}, suppressed=suppressed)
                .on_conflict_do_nothing(index_elements=["rule_id", "dedup_key"])
                .returning(*alert_events.c))).first()
            if row is None:
                return None
            values = {"fire_count": alert_rules.c.fire_count + 1, "last_fired_at": row.fired_at}
            rule = (await conn.execute(sa.select(alert_rules.c.mode).where(alert_rules.c.id == f.rule_id))).first()
            if rule is not None and rule.mode == "once":
                values["enabled"] = False
            await conn.execute(alert_rules.update().where(alert_rules.c.id == f.rule_id).values(**values))
        return row

    async def set_delivery(self, event_id: int, channel: str, status: str) -> None:
        async with self.engine.begin() as conn:
            await conn.execute(alert_events.update().where(alert_events.c.id == event_id).values(
                delivery=alert_events.c.delivery.op("||")(sa.cast({channel: status}, alert_events.c.delivery.type))))

    async def list_events(self, user_id, limit: int = 50, before_id: Optional[int] = None,
                          unread_only: bool = False) -> list:
        q = sa.select(alert_events).where(alert_events.c.owner_user_id == user_id)
        if before_id is not None:
            q = q.where(alert_events.c.id < before_id)
        if unread_only:
            q = q.where(alert_events.c.read_at.is_(None))
        async with self.engine.connect() as conn:
            rows = (await conn.execute(q.order_by(alert_events.c.id.desc()).limit(limit))).all()
        return [event_public(r) for r in rows]

    async def unread_count(self, user_id) -> int:
        async with self.engine.connect() as conn:
            return (await conn.execute(sa.select(sa.func.count()).select_from(alert_events).where(
                alert_events.c.owner_user_id == user_id, alert_events.c.read_at.is_(None)))).scalar_one()

    async def mark_read(self, user_id, ids: Optional[list]) -> int:
        """Marks the given events (or all, when ids is None) read. Returns how many changed."""
        q = alert_events.update().where(alert_events.c.owner_user_id == user_id, alert_events.c.read_at.is_(None))
        if ids is not None:
            q = q.where(alert_events.c.id.in_(ids))
        async with self.engine.begin() as conn:
            return (await conn.execute(q.values(read_at=self.now()))).rowcount

    async def prune_events(self, older_than_days: int = EVENT_RETENTION_DAYS) -> int:
        async with self.engine.begin() as conn:
            return (await conn.execute(alert_events.delete().where(
                alert_events.c.fired_at < self.now() - timedelta(days=older_than_days)))).rowcount
