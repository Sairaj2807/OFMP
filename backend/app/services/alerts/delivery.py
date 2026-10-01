"""Notification delivery.

NotificationProvider is the extension point: one implementation per channel
kind (in-app and webhook today; email, Telegram etc. later implement the same
`deliver`). A provider never raises for a delivery failure; it returns
(ok, error) and the dispatcher records the outcome.

Webhooks
  - Body: JSON {"id", "type": "alert.fired", "rule_id", "rule_name", "kind", "message",
    "value", "symbol", "fired_at", "details"}.
  - Signature: X-OFMP-Signature: sha256=HMAC_SHA256(secret, "<timestamp>.<body>"),
    X-OFMP-Timestamp: unix seconds. Receivers should reject old timestamps (replays).
  - The secret is never stored: HMAC(server secret, "webhook:<channel id>"), shown to
    the owner once when the channel is created. Rotating = delete and re-create.
  - SSRF: in production only https URLs on public addresses are accepted. The host is
    resolved once, every address is checked, and the request is sent to that checked
    address (Host header and TLS SNI keep the original name), so a DNS answer that
    changes between check and use (rebinding) cannot redirect it. Redirects are not
    followed. 5 s timeout, 3 attempts on network errors / 5xx / 429.
"""
import asyncio
import hashlib
import hmac
import ipaddress
import json
import logging
import socket
import time
from typing import Optional, Protocol
from urllib.parse import urlsplit, urlunsplit

import httpx

log = logging.getLogger(__name__)

MAX_URL_LENGTH = 2048


class NotificationProvider(Protocol):
    kind: str

    async def deliver(self, owner_user_id: str, event: dict, channel: Optional[tuple] = None) -> tuple:
        """Returns (ok, error message or None)."""


class InAppProvider:
    """Pushes the event to the owner's open terminal connections (/ws/v1/stream)."""
    kind = "in_app"

    def __init__(self, hub_getter):
        self._hub = hub_getter

    async def deliver(self, owner_user_id: str, event: dict, channel: Optional[tuple] = None) -> tuple:
        hub = self._hub()
        if hub is None:
            return False, "stream not running"
        sent = await hub.notify_user(owner_user_id, "alert", event)
        return True, None if sent else "no open connection"


def webhook_secret(server_secret: str, channel_id: str) -> str:
    return hmac.new(server_secret.encode(), f"webhook:{channel_id}".encode(), hashlib.sha256).hexdigest()


def sign(secret: str, timestamp: int, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()


def check_webhook_url(url: str, allow_insecure: bool) -> str:
    """Syntax/scheme check at channel creation. Raises ValueError."""
    if len(url) > MAX_URL_LENGTH:
        raise ValueError("url is too long")
    parts = urlsplit(url)
    if parts.scheme not in (("https", "http") if allow_insecure else ("https",)):
        raise ValueError("url must use https" if not allow_insecure else "url must use http or https")
    if not parts.hostname or parts.username or parts.password:
        raise ValueError("url must have a host and no credentials")
    if not allow_insecure:   # names are checked again on every delivery, after resolution
        host = parts.hostname.lower()
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            literal = None
        if host == "localhost" or host.endswith(".localhost") or (literal is not None and not literal.is_global):
            raise ValueError("url must not point to a private address")
    return url


async def resolve_public(host: str, port: int, allow_private: bool) -> str:
    """The first address of `host`, after checking that EVERY address it
    resolves to is public (unless allow_private). Raises ValueError."""
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    addresses = list(dict.fromkeys(info[4][0] for info in infos))
    if not addresses:
        raise ValueError("host did not resolve")
    if not allow_private:
        for a in addresses:
            ip = ipaddress.ip_address(a.split("%")[0])
            if not ip.is_global or ip.is_multicast:
                raise ValueError("host resolves to a non-public address")
    return addresses[0]


class WebhookProvider:
    kind = "webhook"

    def __init__(self, server_secret: str, allow_private: bool, timeout_sec: float = 5.0, attempts: int = 3,
                 backoff_sec: float = 1.0, transport: Optional[httpx.AsyncBaseTransport] = None):
        self.server_secret = server_secret
        self.allow_private = allow_private
        self.timeout_sec, self.attempts, self.backoff_sec = timeout_sec, attempts, backoff_sec
        self._client = httpx.AsyncClient(timeout=timeout_sec, follow_redirects=False, transport=transport,
                                         headers={"User-Agent": "OFMP-Webhook/1"})

    async def close(self) -> None:
        await self._client.aclose()

    async def _target(self, url: str) -> tuple:
        parts = urlsplit(url)
        port = parts.port or (443 if parts.scheme == "https" else 80)
        ip = await resolve_public(parts.hostname, port, self.allow_private)
        host_for_url = f"[{ip}]" if ":" in ip else ip
        netloc = f"{host_for_url}:{parts.port}" if parts.port else host_for_url
        pinned = urlunsplit((parts.scheme, netloc, parts.path or "/", parts.query, ""))
        host_header = parts.hostname + (f":{parts.port}" if parts.port else "")
        return pinned, host_header, parts.hostname

    async def deliver(self, owner_user_id: str, event: dict, channel: Optional[tuple] = None) -> tuple:
        channel_id, _kind, config = channel
        body = json.dumps({"type": "alert.fired", **event}, default=str, separators=(",", ":")).encode()
        ts = int(time.time())
        headers = {"Content-Type": "application/json", "X-OFMP-Timestamp": str(ts),
                   "X-OFMP-Signature": sign(webhook_secret(self.server_secret, channel_id), ts, body),
                   "X-OFMP-Event-Id": str(event.get("id"))}
        error = None
        for attempt in range(self.attempts):
            if attempt:
                await asyncio.sleep(self.backoff_sec * (3 ** (attempt - 1)))
            try:
                url, host_header, sni = await self._target(config["url"])
                r = await self._client.post(url, content=body, headers={**headers, "Host": host_header},
                                            extensions={"sni_hostname": sni})
                if 200 <= r.status_code < 300:
                    return True, None
                error = f"HTTP {r.status_code}"
                if r.status_code < 500 and r.status_code != 429:
                    return False, error                 # the receiver rejected it: retrying will not help
            except ValueError as e:                     # resolution / address check
                return False, str(e)
            except httpx.HTTPError as e:
                error = type(e).__name__
        return False, error
