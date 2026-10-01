"""Authentication and session management.

Sessions and tokens:
- Login creates a user_sessions row and returns a short-lived access JWT
  (carries the session id) plus an opaque refresh token (only its SHA-256 is
  stored).
- Refresh rotates: the presented token is marked used and a new one issued
  for the same session. Presenting an already-used refresh token means it
  was copied, so the whole session is revoked (reuse detection).
- Every authenticated request re-checks that the session is live, so
  logout / "log out everywhere" / password reset take effect immediately.

Account enumeration: register and password-reset return the same response
whether or not the email exists; login spends the same Argon2 time on
unknown emails.
"""
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

import sqlalchemy as sa
from email_validator import EmailNotValidError, validate_email
from sqlalchemy.ext.asyncio import AsyncEngine

from backend.app.core.errors import AppError
from backend.app.core.security import (create_access_token, decode_access_token, hash_password, hash_token,
                                       needs_rehash, new_opaque_token, validate_password_strength,
                                       verify_password)
from backend.app.infrastructure.postgres.schema import (audit_logs, email_tokens, organization_members,
                                                        organizations, refresh_tokens, user_sessions, users)

log = logging.getLogger(__name__)

VERIFY_TOKEN_TTL = timedelta(days=2)
RESET_TOKEN_TTL = timedelta(hours=1)


@dataclass(frozen=True)
class RequestInfo:
    ip: Optional[str] = None
    user_agent: Optional[str] = None
    request_id: Optional[str] = None


@dataclass(frozen=True)
class AuthUser:
    id: uuid.UUID
    email: str
    display_name: Optional[str]
    role: str
    email_verified: bool
    session_id: Optional[uuid.UUID] = None

    def public(self) -> dict:
        return {"id": str(self.id), "email": self.email, "display_name": self.display_name,
                "role": self.role, "email_verified": self.email_verified}


@dataclass(frozen=True)
class TokenPair:
    access_token: str
    access_expires_in: int
    refresh_token: str
    refresh_expires_at: datetime
    session_id: uuid.UUID
    user: AuthUser


def normalize_email(email: str) -> str:
    try:
        return validate_email(email, check_deliverability=False).normalized.lower()
    except EmailNotValidError:
        raise AppError("INVALID_EMAIL", "email address is not valid", 422) from None


class AuthService:
    def __init__(self, engine: AsyncEngine, jwt_secret: str, email_sender, public_base_url: str,
                 access_ttl_sec: int = 900, refresh_ttl_sec: int = 30 * 24 * 3600,
                 allow_registration: bool = True, clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        if not jwt_secret:
            raise ValueError("jwt_secret is required")
        self.engine = engine
        self.jwt_secret = jwt_secret
        self.email = email_sender
        self.base_url = public_base_url.rstrip("/")
        self.access_ttl_sec = access_ttl_sec
        self.refresh_ttl = timedelta(seconds=refresh_ttl_sec)
        self.allow_registration = allow_registration
        self.now = clock

    # -- helpers ---------------------------------------------------------------

    async def _audit(self, conn, action: str, info: RequestInfo, actor: Optional[uuid.UUID] = None,
                     target_type: Optional[str] = None, target_id=None, details: Optional[dict] = None) -> None:
        await conn.execute(audit_logs.insert().values(
            occurred_at=self.now(), actor_user_id=actor, action=action, target_type=target_type,
            target_id=str(target_id) if target_id is not None else None, ip=info.ip,
            user_agent=info.user_agent, request_id=info.request_id, details=details))

    @staticmethod
    def _user(row, session_id=None) -> AuthUser:
        return AuthUser(row.id, row.email, row.display_name, row.role, row.email_verified_at is not None, session_id)

    async def _issue_email_token(self, conn, user_id, purpose: str, ttl: timedelta) -> str:
        token, token_hash = new_opaque_token()
        await conn.execute(email_tokens.insert().values(user_id=user_id, purpose=purpose, token_hash=token_hash,
                                                        expires_at=self.now() + ttl))
        return token

    async def _consume_email_token(self, conn, token: str, purpose: str):
        row = (await conn.execute(
            sa.select(email_tokens).where(email_tokens.c.token_hash == hash_token(token),
                                          email_tokens.c.purpose == purpose).with_for_update())).first()
        if row is None or row.used_at is not None or row.expires_at <= self.now():
            raise AppError("INVALID_TOKEN", "link is invalid or has expired", 400)
        await conn.execute(email_tokens.update().where(email_tokens.c.id == row.id).values(used_at=self.now()))
        return row

    async def _new_session(self, conn, user_row, info: RequestInfo) -> TokenPair:
        now = self.now()
        session_id = (await conn.execute(user_sessions.insert().values(
            user_id=user_row.id, created_at=now, last_used_at=now, expires_at=now + self.refresh_ttl,
            user_agent=(info.user_agent or "")[:512] or None, ip=info.ip).returning(user_sessions.c.id))).scalar_one()
        return await self._issue_pair(conn, user_row, session_id)

    async def _issue_pair(self, conn, user_row, session_id) -> TokenPair:
        now = self.now()
        refresh, refresh_hash = new_opaque_token()
        expires = now + self.refresh_ttl
        await conn.execute(refresh_tokens.insert().values(session_id=session_id, token_hash=refresh_hash,
                                                          issued_at=now, expires_at=expires))
        access = create_access_token(self.jwt_secret, user_row.id, user_row.role, session_id,
                                     self.access_ttl_sec, now=now.timestamp())
        return TokenPair(access, self.access_ttl_sec, refresh, expires, session_id, self._user(user_row, session_id))

    async def _revoke_sessions(self, conn, user_id, reason: str, except_session: Optional[uuid.UUID] = None) -> int:
        stmt = (user_sessions.update()
                .where(user_sessions.c.user_id == user_id, user_sessions.c.revoked_at.is_(None))
                .values(revoked_at=self.now(), revoked_reason=reason))
        if except_session is not None:
            stmt = stmt.where(user_sessions.c.id != except_session)
        return (await conn.execute(stmt)).rowcount

    async def _active_user_by_email(self, conn, email: str):
        return (await conn.execute(sa.select(users).where(users.c.email == email, users.c.deleted_at.is_(None)))).first()

    # -- registration and email verification -------------------------------------

    async def register(self, email: str, password: str, display_name: Optional[str], info: RequestInfo,
                       role: str = "user") -> None:
        """Always completes silently for an existing email (no enumeration);
        the owner of that address gets a notice instead."""
        if not self.allow_registration:
            raise AppError("REGISTRATION_CLOSED", "registration is disabled", 403)
        email = normalize_email(email)
        problem = validate_password_strength(password)
        if problem:
            raise AppError("WEAK_PASSWORD", problem, 422)
        password_hash = hash_password(password)
        async with self.engine.begin() as conn:
            existing = await self._active_user_by_email(conn, email)
            if existing is not None:
                await self._audit(conn, "user.register_duplicate", info, target_type="user", target_id=existing.id)
                notice = ("Someone tried to create an account with this email address. If it was you, "
                          f"sign in or reset your password at {self.base_url}/app/login/.")
                token = None
            else:
                user_id = (await conn.execute(users.insert().values(
                    email=email, password_hash=password_hash, display_name=display_name, role=role)
                    .returning(users.c.id))).scalar_one()
                org_id = (await conn.execute(organizations.insert().values(
                    name=display_name or email, is_personal=True).returning(organizations.c.id))).scalar_one()
                await conn.execute(organization_members.insert().values(organization_id=org_id, user_id=user_id,
                                                                        role="owner"))
                token = await self._issue_email_token(conn, user_id, "verify_email", VERIFY_TOKEN_TTL)
                await self._audit(conn, "user.register", info, actor=user_id, target_type="user", target_id=user_id)
        if token:
            await self.email.send(email, "Verify your email",
                                  f"Confirm your email address: {self.base_url}/app/verify-email/?token={token}")
        else:
            await self.email.send(email, "Account already exists", notice)

    async def verify_email(self, token: str, info: RequestInfo) -> None:
        async with self.engine.begin() as conn:
            row = await self._consume_email_token(conn, token, "verify_email")
            await conn.execute(users.update().where(users.c.id == row.user_id, users.c.email_verified_at.is_(None))
                               .values(email_verified_at=self.now(), updated_at=self.now()))
            await self._audit(conn, "user.verify_email", info, actor=row.user_id, target_type="user",
                              target_id=row.user_id)

    async def resend_verification(self, user: AuthUser) -> None:
        if user.email_verified:
            return
        async with self.engine.begin() as conn:
            token = await self._issue_email_token(conn, user.id, "verify_email", VERIFY_TOKEN_TTL)
        await self.email.send(user.email, "Verify your email",
                              f"Confirm your email address: {self.base_url}/app/verify-email/?token={token}")

    # -- login, refresh, logout ------------------------------------------------------

    async def login(self, email: str, password: str, info: RequestInfo) -> TokenPair:
        try:
            email = normalize_email(email)
        except AppError:
            email = None
        failed = False
        async with self.engine.begin() as conn:
            row = await self._active_user_by_email(conn, email) if email else None
            ok = verify_password(row.password_hash if row is not None else None, password)
            if not ok or not row.is_active:
                # recorded, then raised after commit (raising inside would roll the audit row back)
                await self._audit(conn, "auth.login_failed", info, actor=row.id if row is not None else None,
                                  details={"email": email})
                failed = True
        if failed:
            raise AppError("INVALID_CREDENTIALS", "email or password is incorrect", 401)
        async with self.engine.begin() as conn:
            if needs_rehash(row.password_hash):
                await conn.execute(users.update().where(users.c.id == row.id).values(password_hash=hash_password(password)))
            await conn.execute(users.update().where(users.c.id == row.id).values(last_login_at=self.now()))
            pair = await self._new_session(conn, row, info)
            await self._audit(conn, "auth.login", info, actor=row.id, target_type="session", target_id=pair.session_id)
            return pair

    async def refresh(self, refresh_token: str, info: RequestInfo) -> TokenPair:
        async with self.engine.begin() as conn:
            tok = (await conn.execute(sa.select(refresh_tokens).where(
                refresh_tokens.c.token_hash == hash_token(refresh_token)).with_for_update())).first()
            if tok is None:
                raise AppError("INVALID_REFRESH_TOKEN", "session expired, sign in again", 401)
            session = (await conn.execute(sa.select(user_sessions).where(
                user_sessions.c.id == tok.session_id).with_for_update())).first()
            if tok.used_at is not None:
                if session.revoked_at is None:
                    await conn.execute(user_sessions.update().where(user_sessions.c.id == session.id)
                                       .values(revoked_at=self.now(), revoked_reason="refresh_token_reuse"))
                    await self._audit(conn, "auth.refresh_token_reuse", info, actor=session.user_id,
                                      target_type="session", target_id=session.id)
                    log.warning("refresh token reuse detected; session %s revoked", session.id)
                reused = True
            else:
                reused = False
        if reused:   # raised after commit, so the revocation is kept
            raise AppError("INVALID_REFRESH_TOKEN", "session expired, sign in again", 401)
        async with self.engine.begin() as conn:
            tok = (await conn.execute(sa.select(refresh_tokens).where(
                refresh_tokens.c.id == tok.id).with_for_update())).first()
            session = (await conn.execute(sa.select(user_sessions).where(
                user_sessions.c.id == tok.session_id).with_for_update())).first()
            now = self.now()
            if tok.used_at is not None:          # lost a race with a concurrent refresh of the same token
                raise AppError("INVALID_REFRESH_TOKEN", "session expired, sign in again", 401)
            if session.revoked_at is not None or session.expires_at <= now or tok.expires_at <= now:
                raise AppError("INVALID_REFRESH_TOKEN", "session expired, sign in again", 401)
            user_row = (await conn.execute(sa.select(users).where(users.c.id == session.user_id))).first()
            if user_row is None or not user_row.is_active or user_row.deleted_at is not None:
                raise AppError("INVALID_REFRESH_TOKEN", "session expired, sign in again", 401)
            await conn.execute(refresh_tokens.update().where(refresh_tokens.c.id == tok.id).values(used_at=now))
            await conn.execute(user_sessions.update().where(user_sessions.c.id == session.id).values(last_used_at=now))
            return await self._issue_pair(conn, user_row, session.id)

    async def logout(self, user: AuthUser, info: RequestInfo) -> None:
        async with self.engine.begin() as conn:
            await conn.execute(user_sessions.update().where(user_sessions.c.id == user.session_id,
                                                            user_sessions.c.revoked_at.is_(None))
                               .values(revoked_at=self.now(), revoked_reason="logout"))
            await self._audit(conn, "auth.logout", info, actor=user.id, target_type="session", target_id=user.session_id)

    async def logout_all(self, user: AuthUser, info: RequestInfo) -> int:
        async with self.engine.begin() as conn:
            n = await self._revoke_sessions(conn, user.id, "logout_all")
            await self._audit(conn, "auth.logout_all", info, actor=user.id, details={"sessions": n})
            return n

    # -- sessions -------------------------------------------------------------------------

    async def list_sessions(self, user: AuthUser) -> list:
        async with self.engine.connect() as conn:
            rows = (await conn.execute(sa.select(user_sessions).where(
                user_sessions.c.user_id == user.id, user_sessions.c.revoked_at.is_(None),
                user_sessions.c.expires_at > self.now()).order_by(user_sessions.c.last_used_at.desc()))).all()
        return [{"id": str(r.id), "created_at": r.created_at, "last_used_at": r.last_used_at,
                 "expires_at": r.expires_at, "user_agent": r.user_agent, "ip": r.ip,
                 "current": r.id == user.session_id} for r in rows]

    async def revoke_session(self, user: AuthUser, session_id: uuid.UUID, info: RequestInfo) -> None:
        async with self.engine.begin() as conn:
            n = (await conn.execute(user_sessions.update().where(
                user_sessions.c.id == session_id, user_sessions.c.user_id == user.id,   # ownership check
                user_sessions.c.revoked_at.is_(None)).values(revoked_at=self.now(), revoked_reason="revoked"))).rowcount
            if not n:
                raise AppError("NOT_FOUND", "session not found", 404)
            await self._audit(conn, "auth.session_revoked", info, actor=user.id, target_type="session", target_id=session_id)

    # -- passwords -------------------------------------------------------------------------

    async def request_password_reset(self, email: str, info: RequestInfo) -> None:
        """Same outcome whether or not the account exists."""
        try:
            email = normalize_email(email)
        except AppError:
            return
        async with self.engine.begin() as conn:
            row = await self._active_user_by_email(conn, email)
            if row is None or not row.is_active:
                return
            token = await self._issue_email_token(conn, row.id, "reset_password", RESET_TOKEN_TTL)
            await self._audit(conn, "auth.password_reset_requested", info, actor=row.id)
        await self.email.send(email, "Reset your password",
                              f"Reset your password (valid 1 hour): {self.base_url}/app/reset-password/?token={token}")

    async def reset_password(self, token: str, new_password: str, info: RequestInfo) -> None:
        problem = validate_password_strength(new_password)
        if problem:
            raise AppError("WEAK_PASSWORD", problem, 422)
        new_hash = hash_password(new_password)
        async with self.engine.begin() as conn:
            row = await self._consume_email_token(conn, token, "reset_password")
            await conn.execute(users.update().where(users.c.id == row.user_id)
                               .values(password_hash=new_hash, updated_at=self.now()))
            n = await self._revoke_sessions(conn, row.user_id, "password_reset")
            await self._audit(conn, "auth.password_reset", info, actor=row.user_id, details={"sessions_revoked": n})

    async def change_password(self, user: AuthUser, current: str, new_password: str, info: RequestInfo) -> None:
        problem = validate_password_strength(new_password)
        if problem:
            raise AppError("WEAK_PASSWORD", problem, 422)
        async with self.engine.begin() as conn:
            row = (await conn.execute(sa.select(users).where(users.c.id == user.id))).first()
            if not verify_password(row.password_hash, current):
                raise AppError("INVALID_CREDENTIALS", "current password is incorrect", 401)
            await conn.execute(users.update().where(users.c.id == user.id)
                               .values(password_hash=hash_password(new_password), updated_at=self.now()))
            n = await self._revoke_sessions(conn, user.id, "password_changed", except_session=user.session_id)
            await self._audit(conn, "auth.password_changed", info, actor=user.id, details={"other_sessions_revoked": n})

    # -- request authentication ----------------------------------------------------------

    async def authenticate(self, access_token: str) -> Optional[AuthUser]:
        """The user behind a valid access token whose session is still live
        and whose account is active; None otherwise."""
        claims = decode_access_token(self.jwt_secret, access_token)
        if claims is None:
            return None
        async with self.engine.connect() as conn:
            row = (await conn.execute(
                sa.select(users, user_sessions.c.revoked_at.label("s_revoked"),
                          user_sessions.c.expires_at.label("s_expires"))
                .join(user_sessions, user_sessions.c.user_id == users.c.id)
                .where(users.c.id == claims.user_id, user_sessions.c.id == claims.session_id))).first()
        if (row is None or row.s_revoked is not None or row.s_expires <= self.now()
                or not row.is_active or row.deleted_at is not None):
            return None
        return self._user(row, claims.session_id)

    # -- administration -----------------------------------------------------------------------

    async def list_users(self, limit: int = 50, after_email: Optional[str] = None) -> tuple:
        """(users, next_cursor): cursor pagination ordered by email."""
        limit = max(1, min(limit, 200))
        stmt = sa.select(users).where(users.c.deleted_at.is_(None)).order_by(users.c.email).limit(limit + 1)
        if after_email:
            stmt = stmt.where(users.c.email > after_email)
        async with self.engine.connect() as conn:
            rows = (await conn.execute(stmt)).all()
        page = rows[:limit]
        items = [{**self._user(r).public(), "is_active": r.is_active, "created_at": r.created_at,
                  "last_login_at": r.last_login_at} for r in page]
        return items, (page[-1].email if len(rows) > limit else None)

    async def create_user(self, email: str, password: str, role: str, display_name: Optional[str] = None) -> uuid.UUID:
        """CLI/bootstrap: create a verified user directly (e.g. the first admin)."""
        email = normalize_email(email)
        problem = validate_password_strength(password)
        if problem:
            raise AppError("WEAK_PASSWORD", problem, 422)
        async with self.engine.begin() as conn:
            if await self._active_user_by_email(conn, email) is not None:
                raise AppError("EMAIL_TAKEN", "a user with this email already exists", 409)
            user_id = (await conn.execute(users.insert().values(
                email=email, password_hash=hash_password(password), display_name=display_name, role=role,
                email_verified_at=self.now()).returning(users.c.id))).scalar_one()
            org_id = (await conn.execute(organizations.insert().values(
                name=display_name or email, is_personal=True).returning(organizations.c.id))).scalar_one()
            await conn.execute(organization_members.insert().values(organization_id=org_id, user_id=user_id, role="owner"))
            await self._audit(conn, "user.created_by_cli", RequestInfo(), target_type="user", target_id=user_id,
                              details={"role": role})
        return user_id
