"""Assembles the /api/v1 layer onto a FastAPI app.

configure_api(app, ...)   at import time: middleware, error handlers, routers,
                          the /ws/v1/stream route, rate limiters, defaults.
start_api(app, ...)       at startup: database-backed auth, workspace and alert
                          services, market gateway, the stream hub's background
                          loop and the alert runtime."""
import asyncio
import json
from typing import Optional
from urllib.parse import quote

from fastapi import FastAPI, Response, WebSocket
from fastapi.middleware.cors import CORSMiddleware

from backend.app.core import metrics
from backend.app.core.errors import error_body, install_error_handlers
from backend.app.core.middleware import BodySizeLimitMiddleware, RequestContextMiddleware
from backend.app.core.permissions import has_permission
from backend.app.core.ratelimit import SlidingWindowLimiter
from backend.app.services.auth.email import LogEmailSender
from backend.app.services.auth.service import AuthService

from .deps import ACCESS_COOKIE
from .routes.alerts import router as alerts_router
from .routes.auth import router as auth_router
from .routes.platform import admin_router, health_router, market_router
from .routes.workspaces import router as workspaces_router
from .websocket.stream import StreamHub


def default_limiters() -> dict:
    return {
        "login": SlidingWindowLimiter(20, 60),            # per IP
        "login_email": SlidingWindowLimiter(10, 15 * 60),  # per account
        "register": SlidingWindowLimiter(10, 3600),
        "refresh": SlidingWindowLimiter(60, 60),
        "email": SlidingWindowLimiter(5, 3600),
        "token": SlidingWindowLimiter(20, 3600),
        "alert_test": SlidingWindowLimiter(10, 3600),     # webhook test deliveries, per user
    }


class LegacyAuthGuard:
    """With AUTH_REQUIRED on, the legacy pages, /api/* and /ws/frontend|chart
    need a valid session: pages redirect to /login, APIs get 401, sockets are
    refused. Switching the global live contract needs admin.system."""

    PAGES = ("/", "/chart", "/replay")
    SOCKETS = ("/ws/frontend", "/ws/chart")

    def __init__(self, app, enabled: bool):
        self.app, self.enabled = app, enabled

    def _protected(self, path: str) -> bool:
        return path in self.PAGES or path in self.SOCKETS or (path.startswith("/api/") and not path.startswith("/api/v1/"))

    async def __call__(self, scope, receive, send):
        if not self.enabled or scope["type"] not in ("http", "websocket") or not self._protected(scope["path"]):
            return await self.app(scope, receive, send)
        svc = getattr(scope["app"].state, "auth_service", None)
        cookies = _cookies(scope)
        user = await svc.authenticate(cookies[ACCESS_COOKIE]) if svc and cookies.get(ACCESS_COOKIE) else None
        path = scope["path"]
        if scope["type"] == "websocket":
            if user is None:
                return await send({"type": "websocket.close", "code": 4401})
            return await self.app(scope, receive, send)
        if user is None:
            if path in self.PAGES:
                return await _redirect(send, f"/login?next={quote(path)}")
            return await _json(send, 401, error_body("UNAUTHENTICATED", "authentication required"))
        if path == "/api/contract" and scope["method"] == "POST" and not has_permission(user.role, "admin.system"):
            return await _json(send, 403, error_body("FORBIDDEN", "missing permission: admin.system"))
        return await self.app(scope, receive, send)


def _cookies(scope) -> dict:
    raw = dict(scope.get("headers") or []).get(b"cookie", b"").decode("latin-1")
    out = {}
    for part in raw.split(";"):
        if "=" in part:
            k, v = part.strip().split("=", 1)
            out[k] = v
    return out


async def _redirect(send, location: str):
    await send({"type": "http.response.start", "status": 303, "headers": [(b"location", location.encode())]})
    await send({"type": "http.response.body", "body": b""})


async def _json(send, status: int, body: dict):
    data = json.dumps(body).encode()
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(data)).encode())]})
    await send({"type": "http.response.body", "body": data})


def configure_api(app: FastAPI, *, environment: str, cookie_secure: bool, cors_origins: list,
                  max_request_bytes: int, legacy_auth_required: bool, metrics_enabled: bool = True,
                  alert_webhooks_allow_private: bool = False) -> None:
    # Starlette runs the LAST added middleware first: request context wraps everything.
    app.add_middleware(LegacyAuthGuard, enabled=legacy_auth_required)
    if cors_origins:
        app.add_middleware(CORSMiddleware, allow_origins=cors_origins, allow_credentials=True,
                           allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
                           allow_headers=["content-type", "x-csrf-token", "authorization", "x-request-id"])
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=max_request_bytes)
    app.add_middleware(RequestContextMiddleware)
    install_error_handlers(app)
    for router in (health_router, auth_router, market_router, admin_router, workspaces_router,
                   alerts_router):
        app.include_router(router)

    app.state.environment = environment
    app.state.cookie_secure = cookie_secure
    app.state.cors_origins = list(cors_origins)
    app.state.limiters = default_limiters()
    app.state.auth_service = None
    app.state.workspace_service = None
    app.state.alert_service = None
    app.state.alert_runtime = None
    app.state.webhook_provider = None
    app.state.alert_signing_secret = None
    # development only: lets webhooks target http:// and private addresses (a local receiver)
    app.state.alert_webhooks_allow_private = alert_webhooks_allow_private
    app.state.db_engine = None
    app.state.market = None
    app.state.stream_hub = None

    if metrics_enabled:
        # Scraped by Prometheus on the internal network; nginx blocks it from outside.
        @app.get("/metrics", include_in_schema=False)
        async def metrics_endpoint():
            body, content_type = metrics.render()
            return Response(body, media_type=content_type)

    @app.websocket("/ws/v1/stream")
    async def ws_stream(websocket: WebSocket):
        hub = app.state.stream_hub
        if hub is None:
            await websocket.close(code=1013)      # try again later: not started
            return
        await hub.handle(websocket)


def start_api(app: FastAPI, *, db_engine, jwt_secret: Optional[str], public_base_url: str,
              access_ttl_sec: int, refresh_ttl_sec: int, allow_registration: bool, gateway,
              allowed_intervals: tuple, email_sender=None) -> Optional[asyncio.Task]:
    """Returns the stream hub's background task (cancel it on shutdown). The
    alert runtime's task, when there is a database, is app.state.alert_task."""
    app.state.db_engine = db_engine
    app.state.market = gateway
    if db_engine is not None and jwt_secret:
        app.state.auth_service = AuthService(
            db_engine, jwt_secret, email_sender or LogEmailSender(app.state.environment), public_base_url,
            access_ttl_sec=access_ttl_sec, refresh_ttl_sec=refresh_ttl_sec, allow_registration=allow_registration)
    if db_engine is not None:
        from backend.app.services.workspaces import WorkspaceService
        app.state.workspace_service = WorkspaceService(db_engine)

    async def authenticate(token):
        svc = app.state.auth_service
        return await svc.authenticate(token) if svc else None

    app.state.stream_hub = StreamHub(gateway, authenticate, allowed_intervals=allowed_intervals,
                                     allowed_origins=tuple(app.state.cors_origins))
    app.state.alert_task = None
    if db_engine is not None:
        from backend.app.services.alerts.delivery import InAppProvider, WebhookProvider
        from backend.app.services.alerts.runtime import AlertRuntime
        from backend.app.services.alerts.service import AlertService
        app.state.alert_service = AlertService(db_engine, allowed_intervals)
        app.state.alert_signing_secret = jwt_secret
        providers = {}
        if jwt_secret:
            app.state.webhook_provider = WebhookProvider(jwt_secret, app.state.alert_webhooks_allow_private)
            providers["webhook"] = app.state.webhook_provider
        app.state.alert_runtime = AlertRuntime(app.state.alert_service, InAppProvider(lambda: app.state.stream_hub),
                                               providers)
        app.state.alert_task = asyncio.create_task(app.state.alert_runtime.run())
    return asyncio.create_task(app.state.stream_hub.run())
