"""Request/response schemas for /api/v1. Stable API shapes — never database
models passed through."""
from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class RegisterRequest(_Strict):
    email: str = Field(max_length=254)
    password: str = Field(min_length=1, max_length=256)
    display_name: Optional[str] = Field(default=None, max_length=100)


class LoginRequest(_Strict):
    email: str = Field(max_length=254)
    password: str = Field(min_length=1, max_length=256)


class TokenRequest(_Strict):
    token: str = Field(min_length=10, max_length=200)


class PasswordResetRequest(_Strict):
    email: str = Field(max_length=254)


class PasswordResetConfirm(_Strict):
    token: str = Field(min_length=10, max_length=200)
    new_password: str = Field(min_length=1, max_length=256)


class PasswordChange(_Strict):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=1, max_length=256)


class UserOut(BaseModel):
    id: str
    email: str
    display_name: Optional[str]
    role: str
    email_verified: bool


class MeOut(UserOut):
    permissions: list


class LoginOut(BaseModel):
    user: UserOut
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int


class AcceptedOut(BaseModel):
    status: Literal["accepted"] = "accepted"
    message: str


class SessionOut(BaseModel):
    id: str
    created_at: datetime
    last_used_at: datetime
    expires_at: datetime
    user_agent: Optional[str]
    ip: Optional[str]
    current: bool


class RevokedOut(BaseModel):
    revoked: int


class AdminUserOut(UserOut):
    is_active: bool
    created_at: datetime
    last_login_at: Optional[datetime]


class Page(BaseModel):
    data: list
    next_cursor: Optional[str] = None


class ErrorOut(BaseModel):
    """Every error: {"error": {"code", "message", "request_id", "details"?}}."""
    error: dict
