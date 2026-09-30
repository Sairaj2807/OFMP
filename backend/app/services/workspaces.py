"""Workspaces: a user's saved terminal state (layout, per-chart settings,
units) as versioned JSON.

- Ownership: every operation is scoped to the calling user. A workspace
  that exists but belongs to someone else is reported exactly like a missing
  one (NOT_FOUND), so ids cannot be probed.
- Concurrency: `revision` increments on each update; an update must state
  the revision it was based on. A stale write gets 409 REVISION_CONFLICT with
  the current copy, instead of silently overwriting another tab's change.
- Limits: at most MAX_PER_USER workspaces, config at most MAX_CONFIG_BYTES.
- Delete is soft (deleted_at), so an accidental delete is recoverable by an
  operator.
"""
import json
import uuid
from datetime import datetime, timezone
from typing import Callable, Optional

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from backend.app.core.errors import AppError
from backend.app.infrastructure.postgres.schema import audit_logs, organization_members, organizations, workspaces

MAX_PER_USER = 50
MAX_CONFIG_BYTES = 64 * 1024


def _public(row) -> dict:
    return {"id": str(row.id), "name": row.name, "config": row.config, "config_version": row.config_version,
            "revision": row.revision, "is_default": row.is_default, "created_at": row.created_at,
            "updated_at": row.updated_at}


def _summary(row) -> dict:
    d = _public(row)
    d.pop("config")
    return d


class WorkspaceService:
    def __init__(self, engine: AsyncEngine, clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self.engine = engine
        self.now = clock

    @staticmethod
    def _check_config(config: dict) -> None:
        if not isinstance(config, dict):
            raise AppError("INVALID_CONFIG", "config must be a JSON object", 422)
        if len(json.dumps(config, separators=(",", ":"))) > MAX_CONFIG_BYTES:
            raise AppError("CONFIG_TOO_LARGE", f"config exceeds {MAX_CONFIG_BYTES} bytes", 413)

    async def _personal_org(self, conn, user_id) -> uuid.UUID:
        org = (await conn.execute(
            sa.select(organizations.c.id)
            .join(organization_members, organization_members.c.organization_id == organizations.c.id)
            .where(organization_members.c.user_id == user_id, organizations.c.is_personal.is_(True))
            .limit(1))).scalar_one_or_none()
        if org is None:
            raise AppError("NO_ORGANIZATION", "account has no organization", 409)
        return org

    async def _owned(self, conn, user_id, workspace_id, lock: bool = False):
        stmt = sa.select(workspaces).where(workspaces.c.id == workspace_id, workspaces.c.owner_user_id == user_id,
                                           workspaces.c.deleted_at.is_(None))
        row = (await conn.execute(stmt.with_for_update() if lock else stmt)).first()
        if row is None:
            raise AppError("NOT_FOUND", "workspace not found", 404)
        return row

    async def _audit(self, conn, user_id, action: str, workspace_id) -> None:
        await conn.execute(audit_logs.insert().values(occurred_at=self.now(), actor_user_id=user_id, action=action,
                                                      target_type="workspace", target_id=str(workspace_id)))

    async def list(self, user_id) -> list:
        async with self.engine.connect() as conn:
            rows = (await conn.execute(
                sa.select(workspaces).where(workspaces.c.owner_user_id == user_id, workspaces.c.deleted_at.is_(None))
                .order_by(workspaces.c.is_default.desc(), workspaces.c.updated_at.desc()))).all()
        return [_summary(r) for r in rows]

    async def get(self, user_id, workspace_id) -> dict:
        async with self.engine.connect() as conn:
            return _public(await self._owned(conn, user_id, workspace_id))

    async def create(self, user_id, name: str, config: dict, config_version: int) -> dict:
        self._check_config(config)
        try:
            async with self.engine.begin() as conn:
                count = (await conn.execute(sa.select(sa.func.count()).select_from(workspaces).where(
                    workspaces.c.owner_user_id == user_id, workspaces.c.deleted_at.is_(None)))).scalar_one()
                if count >= MAX_PER_USER:
                    raise AppError("LIMIT_REACHED", f"at most {MAX_PER_USER} workspaces", 409)
                row = (await conn.execute(workspaces.insert().values(
                    owner_user_id=user_id, organization_id=await self._personal_org(conn, user_id), name=name,
                    config=config, config_version=config_version, is_default=count == 0,
                    created_at=self.now(), updated_at=self.now()).returning(*workspaces.c))).first()
                await self._audit(conn, user_id, "workspace.create", row.id)
                return _public(row)
        except IntegrityError:
            raise AppError("NAME_TAKEN", "a workspace with that name already exists", 409) from None

    async def update(self, user_id, workspace_id, expected_revision: int, name: Optional[str] = None,
                     config: Optional[dict] = None, config_version: Optional[int] = None,
                     is_default: Optional[bool] = None) -> dict:
        if config is not None:
            self._check_config(config)
        try:
            async with self.engine.begin() as conn:
                row = await self._owned(conn, user_id, workspace_id, lock=True)
                if row.revision != expected_revision:
                    raise AppError("REVISION_CONFLICT", "workspace changed since it was loaded; reload it", 409)
                values = {"revision": row.revision + 1, "updated_at": self.now()}
                if name is not None:
                    values["name"] = name
                if config is not None:
                    values["config"], values["config_version"] = config, config_version or row.config_version
                if is_default:
                    await conn.execute(workspaces.update().where(workspaces.c.owner_user_id == user_id,
                                                                 workspaces.c.id != workspace_id)
                                       .values(is_default=False))
                    values["is_default"] = True
                updated = (await conn.execute(workspaces.update().where(workspaces.c.id == workspace_id)
                                              .values(**values).returning(*workspaces.c))).first()
                return _public(updated)
        except IntegrityError:
            raise AppError("NAME_TAKEN", "a workspace with that name already exists", 409) from None

    async def duplicate(self, user_id, workspace_id, name: str) -> dict:
        src = await self.get(user_id, workspace_id)
        return await self.create(user_id, name, src["config"], src["config_version"])

    async def delete(self, user_id, workspace_id) -> None:
        async with self.engine.begin() as conn:
            row = await self._owned(conn, user_id, workspace_id, lock=True)
            await conn.execute(workspaces.update().where(workspaces.c.id == row.id)
                               .values(deleted_at=self.now(), is_default=False))
            if row.is_default:   # promote the most recently used remaining workspace
                nxt = (await conn.execute(sa.select(workspaces.c.id).where(
                    workspaces.c.owner_user_id == user_id, workspaces.c.deleted_at.is_(None))
                    .order_by(workspaces.c.updated_at.desc()).limit(1))).scalar_one_or_none()
                if nxt is not None:
                    await conn.execute(workspaces.update().where(workspaces.c.id == nxt).values(is_default=True))
            await self._audit(conn, user_id, "workspace.delete", row.id)
