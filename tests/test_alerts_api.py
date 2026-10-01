"""/api/v1/alerts and the live alert runtime against a real database
(skipped unless TEST_DATABASE_URL)."""
import asyncio
import json
import os

import pytest

import config

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL not set")

if TEST_DATABASE_URL:
    import httpx
    import sqlalchemy as sa
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from backend.app.api.app import configure_api
    from backend.app.domain.orderflow import Footprint
    from backend.app.infrastructure.postgres.database import create_engine
    from backend.app.services.alerts.delivery import WebhookProvider, sign, webhook_secret
    from backend.app.services.alerts.runtime import MAX_PER_USER_PER_MIN, AlertRuntime
    from backend.app.services.alerts.service import MAX_CHANNELS_PER_USER, AlertService
    from backend.app.services.auth.email import MemoryEmailSender
    from backend.app.services.auth.service import AuthService

PASSWORD = "good password 1"
SECRET = "alerts-test-secret-" + "q" * 32
INTERVALS = (60, 180, 300, 900, 1800)
PRICE_RULE = {"name": "Breakout", "kind": "price_above", "params": {"level": 24500}, "cooldown_sec": 10}


async def _sql(query):
    engine = create_engine(TEST_DATABASE_URL)
    try:
        async with engine.begin() as conn:
            r = await conn.execute(sa.text(query))
            return r.all() if r.returns_rows else None
    finally:
        await engine.dispose()


@pytest.fixture(scope="module", autouse=True)
def schema():
    from alembic import command
    from alembic.config import Config
    saved = config.DATABASE_URL
    config.DATABASE_URL = TEST_DATABASE_URL
    try:
        command.upgrade(Config("alembic.ini"), "head")
        yield
    finally:
        config.DATABASE_URL = saved


class FakeInApp:
    kind = "in_app"

    def __init__(self):
        self.sent = []

    async def deliver(self, owner_user_id, event, channel=None):
        self.sent.append((owner_user_id, event))
        return True, None


@pytest.fixture
def client():
    asyncio.run(_sql("TRUNCATE users, organizations, organization_members, user_sessions, refresh_tokens, "
                     "email_tokens, audit_logs, alert_rules, alert_events, notification_channels CASCADE"))
    app = FastAPI()
    configure_api(app, environment="development", cookie_secure=False, cors_origins=[],
                  max_request_bytes=200_000, alert_webhooks_allow_private=True)
    engine = create_engine(TEST_DATABASE_URL)
    svc = AuthService(engine, SECRET, MemoryEmailSender(), "http://testserver")
    app.state.auth_service, app.state.db_engine = svc, engine
    app.state.alert_service = AlertService(engine, INTERVALS)
    app.state.alert_signing_secret = SECRET
    app.state.webhook_requests = []

    def receiver(request):
        app.state.webhook_requests.append(request)
        return httpx.Response(200)
    app.state.webhook_provider = WebhookProvider(SECRET, allow_private=True, backoff_sec=0,
                                                 transport=httpx.MockTransport(receiver))
    app.state.in_app = FakeInApp()
    app.state.alert_runtime = AlertRuntime(app.state.alert_service, app.state.in_app,
                                           {"webhook": app.state.webhook_provider})
    with TestClient(app) as c:
        ids = {}
        for email in ("a@example.com", "b@example.com"):
            ids[email] = str(c.portal.call(svc.create_user, email, PASSWORD, "user", None))
        c.user_ids = ids
        yield c
        c.portal.call(app.state.webhook_provider.close)
        c.portal.call(engine.dispose)


def login(client, email):
    other = TestClient(client.app)
    other.portal = client.portal
    assert other.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD}).status_code == 200
    other.headers["X-CSRF-Token"] = other.cookies.get("ofmp_csrf")
    return other


def feed(client, *trades):
    """Live trades into the runtime, then dispatch everything queued."""
    rt = client.app.state.alert_runtime
    fp = Footprint(0.1, 60)
    for ts_s, price in trades:
        rt.on_trade(fp, ts_s * 1000, price, 0, ts_s - ts_s % 60)
    client.portal.call(rt.drain)


# -- rules ----------------------------------------------------------------------------

def test_rule_crud_validation_and_concurrency(client):
    a = login(client, "a@example.com")
    r = a.post("/api/v1/alerts/rules", json=PRICE_RULE)
    assert r.status_code == 201
    rule = r.json()
    assert rule["params"] == {"level": 24500.0} and rule["description"] == "price crosses above 24500.0"
    assert rule["mode"] == "repeat" and rule["enabled"] and rule["revision"] == 1

    bad = a.post("/api/v1/alerts/rules", json={**PRICE_RULE, "kind": "candle_delta_above"})
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "INVALID_RULE"
    assert a.post("/api/v1/alerts/rules", json={**PRICE_RULE, "cooldown_sec": 1}).status_code == 422
    assert a.post("/api/v1/alerts/rules", json={**PRICE_RULE, "owner_user_id": "x"}).status_code == 422

    up = a.patch(f"/api/v1/alerts/rules/{rule['id']}", json={"revision": 1, "kind": "stacked_imbalance",
                                                             "params": {"interval": 300, "side": "buy"}})
    assert up.status_code == 200 and up.json()["revision"] == 2 and up.json()["params"]["side"] == "buy"
    stale = a.patch(f"/api/v1/alerts/rules/{rule['id']}", json={"revision": 1, "enabled": False})
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "REVISION_CONFLICT"
    assert a.patch(f"/api/v1/alerts/rules/{rule['id']}", json={"revision": 2, "kind": "price_above"}).status_code == 422

    kinds = a.get("/api/v1/alerts/kinds").json()
    assert "value_area_break" in kinds["candle_kinds"] and kinds["intervals"] == list(INTERVALS)
    assert a.delete(f"/api/v1/alerts/rules/{rule['id']}").status_code == 204
    assert a.get("/api/v1/alerts/rules").json() == []
    actions = [r[0] for r in asyncio.run(_sql("SELECT action FROM audit_logs WHERE target_type='alert_rule'"))]
    assert actions == ["alert_rule.create", "alert_rule.delete"]


def test_other_users_rules_channels_and_events_look_missing(client):
    a, b = login(client, "a@example.com"), login(client, "b@example.com")
    rid = a.post("/api/v1/alerts/rules", json=PRICE_RULE).json()["id"]
    cid = a.post("/api/v1/alerts/channels", json={"name": "Hook", "url": "http://127.0.0.1:9/h"}).json()["id"]
    for r in (b.get(f"/api/v1/alerts/rules/{rid}"), b.patch(f"/api/v1/alerts/rules/{rid}", json={"revision": 1}),
              b.delete(f"/api/v1/alerts/rules/{rid}"), b.delete(f"/api/v1/alerts/channels/{cid}"),
              b.post(f"/api/v1/alerts/channels/{cid}/test")):
        assert r.status_code == 404 and r.json()["error"]["code"] == "NOT_FOUND"
    # nor can b attach a's channel to its own rule
    r = b.post("/api/v1/alerts/rules", json={**PRICE_RULE, "channel_ids": [cid]})
    assert r.status_code == 422 and r.json()["error"]["code"] == "INVALID_CHANNEL"
    feed(client, (1, 24400), (2, 24600))
    assert len(a.get("/api/v1/alerts/events").json()) == 1
    assert b.get("/api/v1/alerts/events").json() == []
    eid = a.get("/api/v1/alerts/events").json()[0]["id"]
    assert b.post("/api/v1/alerts/events/read", json={"ids": [eid]}).json() == {"updated": 0}


def test_limits_and_auth(client):
    anon = TestClient(client.app)
    anon.portal = client.portal
    assert anon.get("/api/v1/alerts/rules").status_code == 401
    a = login(client, "a@example.com")
    del a.headers["X-CSRF-Token"]
    assert a.post("/api/v1/alerts/rules", json=PRICE_RULE).json()["error"]["code"] == "CSRF_FAILED"
    a = login(client, "a@example.com")
    for i in range(MAX_CHANNELS_PER_USER):
        assert a.post("/api/v1/alerts/channels", json={"name": f"h{i}", "url": "http://127.0.0.1:9/h"}).status_code == 201
    over = a.post("/api/v1/alerts/channels", json={"name": "more", "url": "http://127.0.0.1:9/h"})
    assert over.status_code == 409 and over.json()["error"]["code"] == "LIMIT_REACHED"


# -- runtime: trade -> event -> delivery ---------------------------------------------------

def test_live_trade_fires_records_once_and_delivers_in_app_and_webhook(client):
    a = login(client, "a@example.com")
    ch = a.post("/api/v1/alerts/channels", json={"name": "Hook", "url": "http://127.0.0.1:9/hook"})
    assert ch.status_code == 201
    channel = ch.json()
    assert channel["secret"] == webhook_secret(SECRET, channel["id"])
    assert "secret" not in a.get("/api/v1/alerts/channels").json()[0]            # shown once only
    a.post("/api/v1/alerts/rules", json={**PRICE_RULE, "channel_ids": [channel["id"]]})

    feed(client, (100, 24400), (101, 24500))
    events = a.get("/api/v1/alerts/events").json()
    assert len(events) == 1
    ev = events[0]
    assert ev["kind"] == "price_above" and ev["value"] == 24500 and ev["read_at"] is None
    assert ev["delivery"] == {"in_app": "sent", channel["id"]: "ok"}

    owner, pushed = client.app.state.in_app.sent[0]
    assert owner == client.user_ids["a@example.com"] and pushed["id"] == ev["id"] and pushed["rule_name"] == "Breakout"
    req = client.app.state.webhook_requests[0]
    assert json.loads(req.content)["id"] == ev["id"]
    assert req.headers["x-ofmp-signature"] == sign(channel["secret"], int(req.headers["x-ofmp-timestamp"]), req.content)
    assert a.get("/api/v1/alerts/channels").json()[0]["last_status"] == "ok"
    rule = a.get("/api/v1/alerts/rules").json()[0]
    assert rule["fire_count"] == 1 and rule["last_fired_at"] is not None

    # the same market event evaluated again (e.g. by a second process): recorded once, delivered once
    rt = client.app.state.alert_runtime
    from backend.app.domain.alerts import Firing
    dup = Firing(rule_id=rule["id"], owner_user_id=client.user_ids["a@example.com"], rule_name="Breakout",
                 kind="price_above", ts_ms=101_000, dedup_key="trade:101000", message="m", value=24500)
    assert client.portal.call(rt.dispatch, dup, None) is None
    assert len(client.app.state.in_app.sent) == 1

    assert a.get("/api/v1/alerts/events/unread-count").json() == {"unread": 1}
    assert a.post("/api/v1/alerts/events/read", json={}).json() == {"updated": 1}
    assert a.get("/api/v1/alerts/events?unread=true").json() == []


def test_once_rule_disables_itself_and_pause_stops_evaluation(client):
    a = login(client, "a@example.com")
    once = a.post("/api/v1/alerts/rules", json={**PRICE_RULE, "name": "Once", "mode": "once"}).json()
    feed(client, (1, 24400), (2, 24600), (100, 24400), (200, 24600))
    assert len(a.get("/api/v1/alerts/events").json()) == 1
    assert a.get(f"/api/v1/alerts/rules/{once['id']}").json()["enabled"] is False
    client.portal.call(client.app.state.alert_runtime.reload)                    # stays off after a reload
    assert client.app.state.alert_runtime.evaluator.rules() == []

    paused = a.post("/api/v1/alerts/rules", json={**PRICE_RULE, "name": "Paused"}).json()
    a.patch(f"/api/v1/alerts/rules/{paused['id']}", json={"revision": 1, "enabled": False})
    feed(client, (300, 24400), (301, 24600))
    assert len(a.get("/api/v1/alerts/events").json()) == 1


def test_per_user_rate_limit_records_but_does_not_deliver(client):
    a = login(client, "a@example.com")
    for i in range(3):
        a.post("/api/v1/alerts/rules", json={**PRICE_RULE, "name": f"R{i}", "params": {"level": 24500 + i}})
    # each crossing up fires all 3 rules; 10 s apart so the cooldown allows every one
    trades = []
    for k in range(10):
        trades += [(k * 20, 24400), (k * 20 + 10, 24600)]
    feed(client, *trades)
    events = a.get("/api/v1/alerts/events?limit=200").json()
    assert len(events) == 30
    delivered = [e for e in events if e["suppressed"] is None]
    assert len(delivered) == MAX_PER_USER_PER_MIN == len(client.app.state.in_app.sent)
    assert {e["suppressed"] for e in events if e not in delivered} == {"rate_limited"}


def test_deleting_a_channel_detaches_it_from_rules(client):
    a = login(client, "a@example.com")
    cid = a.post("/api/v1/alerts/channels", json={"name": "Hook", "url": "http://127.0.0.1:9/h"}).json()["id"]
    rid = a.post("/api/v1/alerts/rules", json={**PRICE_RULE, "channel_ids": [cid]}).json()["id"]
    assert a.post(f"/api/v1/alerts/channels/{cid}/test").json() == {"ok": True, "error": None}
    assert a.delete(f"/api/v1/alerts/channels/{cid}").status_code == 204
    assert a.get(f"/api/v1/alerts/rules/{rid}").json()["channel_ids"] == []
    bad = a.post("/api/v1/alerts/channels", json={"name": "x", "url": "ftp://example.com/h"})
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "INVALID_URL"
