"""ASGI middleware (pure ASGI, so WebSocket connections pass through too)."""
import json
import logging
import re
import time
import uuid

from . import metrics
from .errors import error_body
from .logging import request_id_var, user_id_var

access_log = logging.getLogger("ofmp.access")
_VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{8,64}$")


class RequestContextMiddleware:
    """Assigns a request_id (a well-formed incoming X-Request-ID is kept),
    exposes it in the response header and logs one access line per HTTP
    request with method, path, status, latency and user id."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        incoming = dict(scope.get("headers") or []).get(b"x-request-id", b"").decode("latin-1")
        request_id = incoming if _VALID_REQUEST_ID.match(incoming) else uuid.uuid4().hex
        rid_token = request_id_var.set(request_id)
        uid_token = user_id_var.set(None)
        scope.setdefault("state", {})["request_id"] = request_id
        status = {"code": None}
        start = time.perf_counter()

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
                message.setdefault("headers", []).append((b"x-request-id", request_id.encode()))
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            if scope["type"] == "http":
                elapsed = time.perf_counter() - start
                route = scope.get("route")
                template = getattr(route, "path", None) or "unmatched"   # template, not raw path: bounded labels
                metrics.HTTP_REQUESTS.labels(scope["method"], template, str(status["code"])).inc()
                metrics.HTTP_LATENCY.labels(scope["method"], template).observe(elapsed)
                access_log.info("%s %s %s", scope["method"], scope["path"], status["code"], extra={
                    "method": scope["method"], "path": scope["path"], "status": status["code"],
                    "latency_ms": round((time.perf_counter() - start) * 1000, 2)})
            request_id_var.reset(rid_token)
            user_id_var.reset(uid_token)


class BodySizeLimitMiddleware:
    """Rejects HTTP requests whose body exceeds max_bytes (413), by declared
    Content-Length or while streaming a chunked body."""

    def __init__(self, app, max_bytes: int):
        self.app, self.max_bytes = app, max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        declared = dict(scope.get("headers") or []).get(b"content-length")
        if declared is not None and declared.isdigit() and int(declared) > self.max_bytes:
            return await self._reject(send)
        received = {"n": 0}

        async def limited_receive():
            message = await receive()
            if message["type"] == "http.request":
                received["n"] += len(message.get("body", b""))
                if received["n"] > self.max_bytes:
                    raise _TooLarge()
            return message

        try:
            await self.app(scope, limited_receive, send)
        except _TooLarge:
            await self._reject(send)

    async def _reject(self, send):
        body = json.dumps(error_body("PAYLOAD_TOO_LARGE", f"request body exceeds {self.max_bytes} bytes")).encode()
        await send({"type": "http.response.start", "status": 413,
                    "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})


class _TooLarge(Exception):
    pass
