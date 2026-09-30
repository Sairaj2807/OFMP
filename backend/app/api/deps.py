"""Request dependencies: authentication, authorization, rate limits, cookies.

Browser clients authenticate with HttpOnly cookies; API clients may send
`Authorization: Bearer <access token>` instead. Cookie-authenticated unsafe
requests (POST/PUT/PATCH/DELETE) must echo the ofmp_csrf cookie in the
X-CSRF-Token header (double-submit), which a cross-site page cannot read."""
from typing import Optional

from fastapi import Depends, Request, Response

from backend.app.core.errors import AppError
from backend.app.core.logging import user_id_var
from backend.app.core.permissions import has_permission
from backend.app.core.security import csrf_matches, new_csrf_token
from backend.app.services.auth.service import AuthService, AuthUser, RequestInfo, TokenPair

ACCESS_COOKIE = "ofmp_access"
REFRESH_COOKIE = "ofmp_refresh"
CSRF_COOKIE = "ofmp_csrf"
CSRF_HEADER = "x-csrf-token"
REFRESH_COOKIE_PATH = "/api/v1/auth"
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def get_auth_service(request: Request) -> AuthService:
    svc = getattr(request.app.state, "auth_service", None)
    if svc is None:
        raise AppError("AUTH_UNAVAILABLE", "authentication is not configured (DATABASE_URL and JWT_SECRET)", 503)
    return svc


def request_info(request: Request) -> RequestInfo:
    return RequestInfo(ip=request.client.host if request.client else None,
                       user_agent=request.headers.get("user-agent"),
                       request_id=getattr(request.state, "request_id", None))


def bearer_token(request: Request) -> Optional[str]:
    auth = request.headers.get("authorization", "")
    return auth[7:].strip() if auth[:7].lower() == "bearer " else None


def check_csrf(request: Request) -> None:
    if not csrf_matches(request.cookies.get(CSRF_COOKIE), request.headers.get(CSRF_HEADER)):
        raise AppError("CSRF_FAILED", "missing or invalid CSRF token", 403)


async def current_user(request: Request, svc: AuthService = Depends(get_auth_service)) -> AuthUser:
    token = bearer_token(request)
    via_cookie = token is None
    if via_cookie:
        token = request.cookies.get(ACCESS_COOKIE)
    if not token:
        raise AppError("UNAUTHENTICATED", "authentication required", 401)
    if via_cookie and request.method in UNSAFE_METHODS:
        check_csrf(request)
    user = await svc.authenticate(token)
    if user is None:
        raise AppError("UNAUTHENTICATED", "session is invalid or has expired", 401)
    user_id_var.set(str(user.id))
    return user


def require_permission(permission: str):
    async def dependency(user: AuthUser = Depends(current_user)) -> AuthUser:
        if not has_permission(user.role, permission):
            raise AppError("FORBIDDEN", f"missing permission: {permission}", 403)
        return user
    return dependency


def rate_limit(name: str, key: str = "ip"):
    """Dependency applying app.state.limiters[name], keyed by client IP."""
    async def dependency(request: Request) -> None:
        limiter = request.app.state.limiters.get(name)
        if limiter is not None:
            limiter.check(f"{name}:{request.client.host if request.client else '?'}")
    return dependency


def set_auth_cookies(response: Response, pair: TokenPair, secure: bool) -> None:
    response.set_cookie(ACCESS_COOKIE, pair.access_token, max_age=pair.access_expires_in,
                        httponly=True, secure=secure, samesite="lax", path="/")
    response.set_cookie(REFRESH_COOKIE, pair.refresh_token, expires=pair.refresh_expires_at,
                        httponly=True, secure=secure, samesite="strict", path=REFRESH_COOKIE_PATH)
    response.set_cookie(CSRF_COOKIE, new_csrf_token(), expires=pair.refresh_expires_at,
                        httponly=False, secure=secure, samesite="lax", path="/")


def clear_auth_cookies(response: Response, secure: bool) -> None:
    for name, path in ((ACCESS_COOKIE, "/"), (REFRESH_COOKIE, REFRESH_COOKIE_PATH), (CSRF_COOKIE, "/")):
        response.delete_cookie(name, path=path, secure=secure, httponly=name != CSRF_COOKIE,
                               samesite="strict" if name == REFRESH_COOKIE else "lax")
