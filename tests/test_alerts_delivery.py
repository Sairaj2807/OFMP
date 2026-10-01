"""Alert delivery without a database: webhook signing, URL and address
checks (SSRF), retries, pinned-address requests, and in-app delivery over
the stream hub."""
import asyncio
import hashlib
import hmac
import json
import socket
import uuid

import httpx
import pytest

from backend.app.api.websocket.stream import StreamHub
from backend.app.domain.alerts import CandleCloseDetector
from backend.app.services.alerts.delivery import (InAppProvider, WebhookProvider, check_webhook_url,
                                                  resolve_public, sign, webhook_secret)

SECRET = "server-secret-" + "x" * 32
CHANNEL = ("6f1c9a52-0000-4000-8000-000000000001", "webhook", {"url": "http://127.0.0.1:9/hook?x=1"})
EVENT = {"id": 7, "rule_id": "r", "rule_name": "R", "kind": "price_above", "message": "m", "value": 1.0,
         "symbol": "NIFTY", "fired_at": "2026-10-01T09:30:00+05:30", "details": {}}


def test_signature_is_hmac_over_timestamp_and_body_and_secret_is_per_channel():
    s = webhook_secret(SECRET, "a")
    assert s != webhook_secret(SECRET, "b") and s == webhook_secret(SECRET, "a") and len(s) == 64
    body = b'{"x":1}'
    expected = hmac.new(s.encode(), b"1700000000." + body, hashlib.sha256).hexdigest()
    assert sign(s, 1700000000, body) == "sha256=" + expected


@pytest.mark.parametrize("url,insecure,ok", [
    ("https://hooks.example.com/x", False, True),
    ("http://hooks.example.com/x", False, False),
    ("https://127.0.0.1/x", False, False),
    ("https://10.1.2.3/x", False, False),
    ("https://[::1]/x", False, False),
    ("https://localhost/x", False, False),
    ("https://user:pw@hooks.example.com/x", False, False),
    ("ftp://hooks.example.com/x", True, False),
    ("http://127.0.0.1:8080/x", True, True),
])
def test_url_checks(url, insecure, ok):
    if ok:
        assert check_webhook_url(url, insecure) == url
    else:
        with pytest.raises(ValueError):
            check_webhook_url(url, insecure)


def test_every_resolved_address_must_be_public(monkeypatch):
    answers = {"good.test": ["93.184.215.14"], "mixed.test": ["93.184.215.14", "192.168.1.5"],
               "meta.test": ["169.254.169.254"]}

    async def fake_getaddrinfo(host, port, type=0):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, port)) for a in answers[host]]

    async def go():
        monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", fake_getaddrinfo)
        assert await resolve_public("good.test", 443, False) == "93.184.215.14"
        for host in ("mixed.test", "meta.test"):
            with pytest.raises(ValueError, match="non-public"):
                await resolve_public(host, 443, False)
        assert await resolve_public("mixed.test", 443, True) == "93.184.215.14"
    asyncio.run(go())


def provider(handler, **kw):
    return WebhookProvider(SECRET, allow_private=True, backoff_sec=0, transport=httpx.MockTransport(handler), **kw)


def test_webhook_delivery_is_signed_and_sent_to_the_checked_address():
    seen = []

    def handler(request: httpx.Request):
        seen.append(request)
        return httpx.Response(204)

    async def go():
        p = provider(handler)
        assert await p.deliver("u", EVENT, CHANNEL) == (True, None)
        await p.close()
    asyncio.run(go())
    req = seen[0]
    assert str(req.url) == "http://127.0.0.1:9/hook?x=1" and req.headers["host"] == "127.0.0.1:9"
    assert req.extensions["sni_hostname"] == "127.0.0.1"
    body = req.content
    assert json.loads(body)["type"] == "alert.fired" and json.loads(body)["id"] == 7
    ts = int(req.headers["x-ofmp-timestamp"])
    assert req.headers["x-ofmp-signature"] == sign(webhook_secret(SECRET, CHANNEL[0]), ts, body)
    assert req.headers["x-ofmp-event-id"] == "7"


def test_webhook_retries_server_errors_but_not_client_errors(monkeypatch):
    calls = {"n": 0}

    def flaky(request):
        calls["n"] += 1
        return httpx.Response(503 if calls["n"] < 3 else 200)

    def rejecting(request):
        calls["n"] += 1
        return httpx.Response(410)

    def broken(request):
        raise httpx.ConnectError("refused")

    async def go():
        assert await provider(flaky).deliver("u", EVENT, CHANNEL) == (True, None)
        assert calls["n"] == 3
        calls["n"] = 0
        assert await provider(rejecting).deliver("u", EVENT, CHANNEL) == (False, "HTTP 410")
        assert calls["n"] == 1
        assert await provider(broken).deliver("u", EVENT, CHANNEL) == (False, "ConnectError")
        strict = WebhookProvider(SECRET, allow_private=False, backoff_sec=0, transport=httpx.MockTransport(flaky))
        assert await strict.deliver("u", EVENT, CHANNEL) == (False, "host resolves to a non-public address")
    asyncio.run(go())


class FakeConn:
    def __init__(self, user_id, fail=False):
        self.user = type("U", (), {"id": uuid.UUID(user_id)})()
        self.sent, self.fail = [], fail

    async def send(self, type_, data=None, sub_id=None):
        if self.fail:
            raise RuntimeError("socket closed")
        self.sent.append((type_, data))


def test_in_app_delivery_reaches_every_connection_of_the_owner_only():
    hub = StreamHub(gateway=None, authenticate=None, allowed_intervals=(60,))
    a1, a2 = FakeConn("00000000-0000-0000-0000-00000000000a"), FakeConn("00000000-0000-0000-0000-00000000000a")
    b, broken = FakeConn("00000000-0000-0000-0000-00000000000b"), FakeConn("00000000-0000-0000-0000-00000000000c",
                                                                           fail=True)
    hub.connections = {a1, a2, b, broken}
    p = InAppProvider(lambda: hub)

    async def go():
        assert await p.deliver("00000000-0000-0000-0000-00000000000a", EVENT) == (True, None)
        assert await p.deliver("00000000-0000-0000-0000-00000000000c", EVENT) == (True, "no open connection")
        assert await InAppProvider(lambda: None).deliver("x", EVENT) == (False, "stream not running")
    asyncio.run(go())
    assert a1.sent == a2.sent == [("alert", EVENT)] and b.sent == []


def test_detector_ignores_late_trades_stamped_earlier():
    class FP:
        data, candle_ohlc = {}, {}
    det = CandleCloseDetector()
    assert det.on_trade(FP, 120, [60]) == []
    assert det.on_trade(FP, 60, [60]) == []          # out of order: does not "close" the forming candle
    assert det._last_native == 120

