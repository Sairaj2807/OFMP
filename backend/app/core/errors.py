"""Standard error envelope for every /api/v1 error:

    {"error": {"code": "INVALID_CREDENTIALS", "message": "...", "request_id": "..."}}

Unexpected exceptions are logged with their stack trace and returned as a
generic INTERNAL_ERROR — never with internal details."""
import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from .logging import request_id_var

log = logging.getLogger(__name__)


class AppError(Exception):
    def __init__(self, code: str, message: str, status: int = 400, headers: dict = None):
        super().__init__(message)
        self.code, self.message, self.status, self.headers = code, message, status, headers


def error_body(code: str, message: str, details=None) -> dict:
    err = {"code": code, "message": message, "request_id": request_id_var.get()}
    if details is not None:
        err["details"] = details
    return {"error": err}


_HTTP_CODES = {400: "BAD_REQUEST", 401: "UNAUTHENTICATED", 403: "FORBIDDEN", 404: "NOT_FOUND",
               405: "METHOD_NOT_ALLOWED", 409: "CONFLICT", 413: "PAYLOAD_TOO_LARGE",
               429: "RATE_LIMITED", 503: "SERVICE_UNAVAILABLE"}


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(request: Request, exc: AppError):
        return JSONResponse(error_body(exc.code, exc.message), status_code=exc.status, headers=exc.headers)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException):
        message = exc.detail if isinstance(exc.detail, str) else "request failed"
        return JSONResponse(error_body(_HTTP_CODES.get(exc.status_code, "HTTP_ERROR"), message),
                            status_code=exc.status_code, headers=getattr(exc, "headers", None))

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError):
        details = [{"field": ".".join(str(p) for p in e["loc"][1:]), "message": e["msg"]} for e in exc.errors()]
        return JSONResponse(error_body("VALIDATION_ERROR", "request validation failed", details), status_code=422)

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception):
        log.exception("unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse(error_body("INTERNAL_ERROR", "internal server error"), status_code=500)
