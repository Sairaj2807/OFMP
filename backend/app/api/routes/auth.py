"""/api/v1/auth — registration, login, token refresh, logout, email
verification, password reset/change, current user and sessions."""
import uuid

from fastapi import APIRouter, Depends, Request, Response, status

from backend.app.api.deps import (REFRESH_COOKIE, check_csrf, clear_auth_cookies, current_user, get_auth_service,
                                  rate_limit, request_info, set_auth_cookies)
from backend.app.api.schemas import (AcceptedOut, LoginOut, LoginRequest, MeOut, PasswordChange, PasswordResetConfirm,
                                     PasswordResetRequest, RegisterRequest, RevokedOut, SessionOut, TokenRequest)
from backend.app.core.errors import AppError
from backend.app.core.permissions import ROLE_PERMISSIONS
from backend.app.services.auth.service import AuthService, AuthUser, TokenPair

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


def _secure(request: Request) -> bool:
    return request.app.state.cookie_secure


def _login_out(pair: TokenPair) -> LoginOut:
    return LoginOut(user=pair.user.public(), access_token=pair.access_token, expires_in=pair.access_expires_in)


@router.post("/register", status_code=status.HTTP_202_ACCEPTED, response_model=AcceptedOut,
             dependencies=[Depends(rate_limit("register"))],
             summary="Create an account (same response whether or not the email is already registered)")
async def register(body: RegisterRequest, request: Request, svc: AuthService = Depends(get_auth_service)):
    await svc.register(body.email, body.password, body.display_name, request_info(request))
    return AcceptedOut(message="check your email to verify your address, then sign in")


@router.post("/login", response_model=LoginOut, dependencies=[Depends(rate_limit("login"))],
             summary="Sign in; sets HttpOnly session cookies and returns a short-lived access token")
async def login(body: LoginRequest, request: Request, response: Response,
                svc: AuthService = Depends(get_auth_service)):
    limiter = request.app.state.limiters.get("login_email")
    if limiter is not None:                          # slows guessing against one account from many IPs
        await limiter.check(f"login_email:{body.email.strip().lower()}")
    pair = await svc.login(body.email, body.password, request_info(request))
    set_auth_cookies(response, pair, _secure(request))
    return _login_out(pair)


@router.post("/refresh", response_model=LoginOut, dependencies=[Depends(rate_limit("refresh"))],
             summary="Rotate the refresh cookie and issue a new access token")
async def refresh(request: Request, response: Response, svc: AuthService = Depends(get_auth_service)):
    check_csrf(request)
    token = request.cookies.get(REFRESH_COOKIE)
    if not token:
        raise AppError("INVALID_REFRESH_TOKEN", "session expired, sign in again", 401)
    try:
        pair = await svc.refresh(token, request_info(request))
    except AppError:
        clear_auth_cookies(response, _secure(request))
        raise
    set_auth_cookies(response, pair, _secure(request))
    return _login_out(pair)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT, summary="End this session")
async def logout(request: Request, response: Response, user: AuthUser = Depends(current_user),
                 svc: AuthService = Depends(get_auth_service)):
    await svc.logout(user, request_info(request))
    clear_auth_cookies(response, _secure(request))
    response.status_code = status.HTTP_204_NO_CONTENT
    return response


@router.post("/logout-all", response_model=RevokedOut, summary="End every session of this account")
async def logout_all(request: Request, response: Response, user: AuthUser = Depends(current_user),
                     svc: AuthService = Depends(get_auth_service)):
    n = await svc.logout_all(user, request_info(request))
    clear_auth_cookies(response, _secure(request))
    return RevokedOut(revoked=n)


@router.post("/verify-email", status_code=status.HTTP_204_NO_CONTENT, dependencies=[Depends(rate_limit("token"))])
async def verify_email(body: TokenRequest, request: Request, svc: AuthService = Depends(get_auth_service)):
    await svc.verify_email(body.token, request_info(request))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/verify-email/resend", status_code=status.HTTP_202_ACCEPTED, response_model=AcceptedOut,
             dependencies=[Depends(rate_limit("email"))])
async def resend_verification(user: AuthUser = Depends(current_user), svc: AuthService = Depends(get_auth_service)):
    await svc.resend_verification(user)
    return AcceptedOut(message="if the address is not yet verified, a new link was sent")


@router.post("/password-reset/request", status_code=status.HTTP_202_ACCEPTED, response_model=AcceptedOut,
             dependencies=[Depends(rate_limit("email"))],
             summary="Email a reset link (same response whether or not the account exists)")
async def password_reset_request(body: PasswordResetRequest, request: Request,
                                 svc: AuthService = Depends(get_auth_service)):
    await svc.request_password_reset(body.email, request_info(request))
    return AcceptedOut(message="if an account exists for that address, a reset link was sent")


@router.post("/password-reset/confirm", status_code=status.HTTP_204_NO_CONTENT,
             dependencies=[Depends(rate_limit("token"))], summary="Set a new password; signs out every session")
async def password_reset_confirm(body: PasswordResetConfirm, request: Request,
                                 svc: AuthService = Depends(get_auth_service)):
    await svc.reset_password(body.token, body.new_password, request_info(request))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/password/change", status_code=status.HTTP_204_NO_CONTENT,
             summary="Change password; signs out every other session")
async def password_change(body: PasswordChange, request: Request, user: AuthUser = Depends(current_user),
                          svc: AuthService = Depends(get_auth_service)):
    await svc.change_password(user, body.current_password, body.new_password, request_info(request))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/me", response_model=MeOut, summary="The signed-in user and their permissions")
async def me(user: AuthUser = Depends(current_user)):
    return MeOut(**user.public(), permissions=sorted(ROLE_PERMISSIONS.get(user.role, ())))


@router.get("/sessions", response_model=list[SessionOut], summary="Active sessions of this account")
async def sessions(user: AuthUser = Depends(current_user), svc: AuthService = Depends(get_auth_service)):
    return await svc.list_sessions(user)


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Revoke one session")
async def revoke_session(session_id: uuid.UUID, request: Request, user: AuthUser = Depends(current_user),
                         svc: AuthService = Depends(get_auth_service)):
    await svc.revoke_session(user, session_id, request_info(request))
    return Response(status_code=status.HTTP_204_NO_CONTENT)
