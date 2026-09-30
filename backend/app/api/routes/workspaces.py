"""/api/v1/workspaces — the signed-in user's saved terminal layouts."""
import uuid
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field

from backend.app.api.deps import require_permission
from backend.app.core.errors import AppError
from backend.app.services.auth.service import AuthUser
from backend.app.services.workspaces import WorkspaceService

router = APIRouter(prefix="/api/v1/workspaces", tags=["workspaces"])

NAME_RULES = {"min_length": 1, "max_length": 80, "pattern": r"^[^\x00-\x1f]+$"}   # no control characters


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class WorkspaceCreate(_Strict):
    name: str = Field(**NAME_RULES)
    config: dict
    config_version: int = Field(ge=1, le=1000)


class WorkspaceUpdate(_Strict):
    revision: int = Field(ge=1, description="the revision this change is based on")
    name: Optional[str] = Field(None, **NAME_RULES)
    config: Optional[dict] = None
    config_version: Optional[int] = Field(None, ge=1, le=1000)
    is_default: Optional[bool] = None


class WorkspaceDuplicate(_Strict):
    name: str = Field(**NAME_RULES)


class WorkspaceSummary(BaseModel):
    id: str
    name: str
    config_version: int
    revision: int
    is_default: bool
    created_at: datetime
    updated_at: datetime


class WorkspaceOut(WorkspaceSummary):
    config: dict


def get_service(request: Request) -> WorkspaceService:
    svc = getattr(request.app.state, "workspace_service", None)
    if svc is None:
        raise AppError("WORKSPACES_UNAVAILABLE", "workspaces need the database (DATABASE_URL)", 503)
    return svc


read = require_permission("workspace.read")
write = require_permission("workspace.write")


@router.get("", response_model=list[WorkspaceSummary], summary="Your workspaces (default first)")
async def list_workspaces(user: AuthUser = Depends(read), svc: WorkspaceService = Depends(get_service)):
    return await svc.list(user.id)


@router.post("", response_model=WorkspaceOut, status_code=status.HTTP_201_CREATED)
async def create_workspace(body: WorkspaceCreate, user: AuthUser = Depends(write),
                           svc: WorkspaceService = Depends(get_service)):
    return await svc.create(user.id, body.name, body.config, body.config_version)


@router.get("/{workspace_id}", response_model=WorkspaceOut)
async def get_workspace(workspace_id: uuid.UUID, user: AuthUser = Depends(read),
                        svc: WorkspaceService = Depends(get_service)):
    return await svc.get(user.id, workspace_id)


@router.patch("/{workspace_id}", response_model=WorkspaceOut,
              summary="Rename, replace config or make default (optimistic concurrency on revision)")
async def update_workspace(workspace_id: uuid.UUID, body: WorkspaceUpdate, user: AuthUser = Depends(write),
                           svc: WorkspaceService = Depends(get_service)):
    return await svc.update(user.id, workspace_id, body.revision, name=body.name, config=body.config,
                            config_version=body.config_version, is_default=body.is_default)


@router.post("/{workspace_id}/duplicate", response_model=WorkspaceOut, status_code=status.HTTP_201_CREATED)
async def duplicate_workspace(workspace_id: uuid.UUID, body: WorkspaceDuplicate, user: AuthUser = Depends(write),
                              svc: WorkspaceService = Depends(get_service)):
    return await svc.duplicate(user.id, workspace_id, body.name)


@router.delete("/{workspace_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_workspace(workspace_id: uuid.UUID, user: AuthUser = Depends(write),
                           svc: WorkspaceService = Depends(get_service)):
    await svc.delete(user.id, workspace_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
