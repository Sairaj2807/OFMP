"""Structured logging with request correlation.

Every log record carries the current request_id (set per HTTP request /
WebSocket connection by RequestContextMiddleware) and user_id when known.
LOG_FORMAT=json emits one JSON object per line; "text" is for local reading.

Never log secrets: passwords, TOTP codes/seeds, API keys, JWTs, refresh
tokens. Fields listed in REDACT_KEYS are masked if passed via `extra`."""
import contextvars
import json
import logging
import sys
import time

request_id_var: contextvars.ContextVar = contextvars.ContextVar("request_id", default=None)
user_id_var: contextvars.ContextVar = contextvars.ContextVar("user_id", default=None)

REDACT_KEYS = {"password", "new_password", "token", "access_token", "refresh_token", "jwt",
               "totp", "totp_secret", "api_key", "authorization", "cookie"}
_STANDARD_ATTRS = set(vars(logging.makeLogRecord({})))


class ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        record.user_id = user_id_var.get()
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)) + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", None),
            "user_id": getattr(record, "user_id", None),
        }
        for key, value in vars(record).items():
            if key not in _STANDARD_ATTRS and key not in out and not key.startswith("_"):
                out[key] = "[redacted]" if key.lower() in REDACT_KEYS else value
        if record.exc_info:
            out["exception"] = self.formatException(record.exc_info)
        return json.dumps(out, default=str)


def configure_logging(fmt: str = "text", level: int = logging.INFO) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(ContextFilter())
    if fmt == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s [%(request_id)s] %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
