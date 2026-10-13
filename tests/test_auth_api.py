"""End-to-end /api/v1 auth against a real database (skipped unless
TEST_DATABASE_URL is set). See tests/test_persistence.py for the URL."""
import asyncio
import os
import re

import pytest

import config

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL not set")

if TEST_DATABASE_URL:
    import sqlalchemy as sa
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    from backend.app.api.app import configure_api
    from backend.app.api.websocket.stream import StreamHub
    from backend.app.infrastructure.postgres.database import create_engine
    from backend.app.services.auth.email import MemoryEmailSender
    from backend.app.services.auth.service import AuthService
    from test_api_core import FakeGateway

SECRET = "auth-test-secret-" + "z" * 32
PASSWORD = "good password 1"


def run(coro):
    return asyncio.run(coro)


async def _sql(query, params=None):
    engine = create_engine(TEST_DATABASE_URL)
    try:
        async with engine.begin() as conn:
            result = await conn.execute(sa.text(query), params or {})
            return result.all() if result.returns_rows else None
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


@pytest.fixture(autouse=True)
def clean():
    run(_sql("TRUNCATE users, organizations, organization_members, user_sessions, refresh_tokens, "
             "email_tokens, audit_logs CASCADE"))


@pytest.fixture
def env():
    """(client, outbox, service) on a fresh app wired to the test database."""
    app = FastAPI()
    configure_api(app, environment="development", cookie_secure=False, cors_origins=[],
                  max_request_bytes=100_000)
    outbox = MemoryEmailSender()
    engine = create_engine(TEST_DATABASE_URL)
    svc = AuthService(engine, SECRET, outbox, "http://testserver")
    app.state.auth_service, app.state.db_engine, app.state.market = svc, engine, FakeGateway()
    app.state.stream_hub = StreamHub(app.state.market, svc.authenticate, allowed_intervals=(60,))
    with TestClient(app) as client:
        yield client, outbox.outbox, svc
        client.portal.call(engine.dispose)


def browser(client):
    """Another browser (own cookie jar) on the same app and event loop — the
    database pool is bound to one loop, so clients must share its portal."""
    other = TestClient(client.app)
    other.portal = client.portal
    return other


def token_from(mail):
    return re.search(r"token=([\w\-]+)", mail["body"]).group(1)


def csrf(client):
    return {"X-CSRF-Token": client.cookies.get("ofmp_csrf")}


def register_and_login(client, email="trader@example.com", password=PASSWORD):
    assert client.post("/api/v1/auth/register", json={"email": email, "password": password}).status_code == 202
    r = client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return r


def audit_actions():
    return [r[0] for r in run(_sql("SELECT action FROM audit_logs ORDER BY id"))]


# ---- registration, verification, login ------------------------------------------------------

def test_register_verify_login_me(env):
    client, outbox, _ = env
    r = client.post("/api/v1/auth/register", json={"email": "Trader@Example.com", "password": PASSWORD,
                                                    "display_name": "T"})
    assert r.status_code == 202
    assert outbox[-1]["to"] == "trader@example.com" and "/app/verify-email/?token=" in outbox[-1]["body"]
    assert client.post("/api/v1/auth/verify-email", json={"token": token_from(outbox[-1])}).status_code == 204
    assert client.post("/api/v1/auth/verify-email", json={"token": token_from(outbox[-1])}).json()["error"]["code"] == "INVALID_TOKEN"

    r = client.post("/api/v1/auth/login", json={"email": "trader@example.com", "password": PASSWORD})
    assert r.status_code == 200 and r.json()["token_type"] == "bearer" and r.json()["expires_in"] == 900
    set_cookie = r.headers.get_list("set-cookie")
    access = next(c for c in set_cookie if c.startswith("ofmp_access="))
    refresh = next(c for c in set_cookie if c.startswith("ofmp_refresh="))
    assert "HttpOnly" in access and "SameSite=lax" in access
    assert "HttpOnly" in refresh and "SameSite=strict" in refresh and "Path=/api/v1/auth" in refresh
    me = client.get("/api/v1/auth/me").json()
    assert me["email"] == "trader@example.com" and me["email_verified"] and "market.read" in me["permissions"]
    orgs = run(_sql("SELECT o.is_personal, m.role FROM organizations o JOIN organization_members m "
                    "ON m.organization_id = o.id"))
    assert [tuple(o) for o in orgs] == [(True, "owner")]


def test_registration_does_not_reveal_existing_accounts(env):
    client, outbox, _ = env
    first = client.post("/api/v1/auth/register", json={"email": "a@example.com", "password": PASSWORD})
    again = client.post("/api/v1/auth/register", json={"email": "a@example.com", "password": "other pass 2"})
    assert first.status_code == again.status_code == 202 and first.json() == again.json()
    assert outbox[-1]["subject"] == "Account already exists"
    assert run(_sql("SELECT count(*) FROM users"))[0][0] == 1


def test_weak_password_and_bad_email_are_rejected(env):
    client, _, _ = env
    assert client.post("/api/v1/auth/register", json={"email": "a@example.com", "password": "short"}).json()["error"]["code"] == "WEAK_PASSWORD"
    assert client.post("/api/v1/auth/register", json={"email": "not-an-email", "password": PASSWORD}).json()["error"]["code"] == "INVALID_EMAIL"
    assert client.post("/api/v1/auth/register", json={"email": "a@example.com", "password": PASSWORD,
                                                      "role": "admin"}).status_code == 422   # no privilege fields accepted


def test_failed_login_is_generic_and_audited(env):
    client, _, _ = env
    register_and_login(client)
    wrong = client.post("/api/v1/auth/login", json={"email": "trader@example.com", "password": "wrong pass 9"})
    unknown = client.post("/api/v1/auth/login", json={"email": "nobody@example.com", "password": "wrong pass 9"})
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json()["error"]["message"] == unknown.json()["error"]["message"]
    assert audit_actions().count("auth.login_failed") == 2


# ---- tokens and sessions ------------------------------------------------------------------------

def test_refresh_rotates_and_reuse_revokes_the_session(env):
    client, _, _ = env
    register_and_login(client)
    old_refresh = client.cookies.get("ofmp_refresh", path="/api/v1/auth")
    r = client.post("/api/v1/auth/refresh", headers=csrf(client))
    assert r.status_code == 200
    assert client.cookies.get("ofmp_refresh", path="/api/v1/auth") != old_refresh
    assert client.get("/api/v1/auth/me").status_code == 200

    # an attacker replays the old (already rotated) refresh token
    client.cookies.set("ofmp_refresh", old_refresh, path="/api/v1/auth")
    replay = client.post("/api/v1/auth/refresh", headers=csrf(client))
    assert replay.status_code == 401 and replay.json()["error"]["code"] == "INVALID_REFRESH_TOKEN"
    assert "auth.refresh_token_reuse" in audit_actions()
    reason = run(_sql("SELECT revoked_reason FROM user_sessions"))[0][0]
    assert reason == "refresh_token_reuse"


def test_refresh_requires_csrf_header(env):
    client, _, _ = env
    register_and_login(client)
    r = client.post("/api/v1/auth/refresh")
    assert r.status_code == 403 and r.json()["error"]["code"] == "CSRF_FAILED"


def test_cookie_auth_writes_need_csrf_but_bearer_does_not(env):
    client, _, _ = env
    token = register_and_login(client).json()["access_token"]
    assert client.post("/api/v1/auth/logout-all").json()["error"]["code"] == "CSRF_FAILED"
    r = browser(client).post("/api/v1/auth/logout-all", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200 and r.json()["revoked"] == 1


def test_logout_revokes_immediately(env):
    client, _, _ = env
    token = register_and_login(client).json()["access_token"]
    assert client.post("/api/v1/auth/logout", headers=csrf(client)).status_code == 204
    assert browser(client).get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"}).status_code == 401


def test_sessions_list_revoke_and_ownership(env):
    client, _, svc = env
    register_and_login(client, "a@example.com")
    other = browser(client)
    other_login = other.post("/api/v1/auth/login", json={"email": "a@example.com", "password": PASSWORD})
    sessions = client.get("/api/v1/auth/sessions").json()
    assert len(sessions) == 2 and sum(s["current"] for s in sessions) == 1
    victim = next(s["id"] for s in sessions if not s["current"])

    # a different user cannot revoke a's session
    register_and_login(browser(client), "b@example.com")
    b_token = browser(client).post("/api/v1/auth/login", json={"email": "b@example.com", "password": PASSWORD}).json()["access_token"]
    r = browser(client).delete(f"/api/v1/auth/sessions/{victim}", headers={"Authorization": f"Bearer {b_token}"})
    assert r.status_code == 404

    assert client.delete(f"/api/v1/auth/sessions/{victim}", headers=csrf(client)).status_code == 204
    assert browser(client).get("/api/v1/auth/me", headers={"Authorization": f"Bearer {other_login.json()['access_token']}"}).status_code == 401


# ---- passwords -------------------------------------------------------------------------------------

def test_password_reset_signs_out_everywhere(env):
    client, outbox, _ = env
    token = register_and_login(client).json()["access_token"]
    assert client.post("/api/v1/auth/password-reset/request", json={"email": "trader@example.com"}).status_code == 202
    same = client.post("/api/v1/auth/password-reset/request", json={"email": "nobody@example.com"})
    assert same.status_code == 202                            # same answer for unknown accounts
    reset = next(m for m in reversed(outbox) if m["subject"] == "Reset your password")
    r = client.post("/api/v1/auth/password-reset/confirm", json={"token": token_from(reset), "new_password": "brand new pass 3"})
    assert r.status_code == 204
    assert browser(client).get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"}).status_code == 401
    assert client.post("/api/v1/auth/login", json={"email": "trader@example.com", "password": PASSWORD}).status_code == 401
    assert client.post("/api/v1/auth/login", json={"email": "trader@example.com", "password": "brand new pass 3"}).status_code == 200


def test_change_password_keeps_current_session_only(env):
    client, _, _ = env
    register_and_login(client)
    other_token = browser(client).post("/api/v1/auth/login", json={"email": "trader@example.com", "password": PASSWORD}).json()["access_token"]
    bad = client.post("/api/v1/auth/password/change", headers=csrf(client),
                      json={"current_password": "wrong pass 1", "new_password": "another pass 4"})
    assert bad.status_code == 401
    ok = client.post("/api/v1/auth/password/change", headers=csrf(client),
                     json={"current_password": PASSWORD, "new_password": "another pass 4"})
    assert ok.status_code == 204
    assert client.get("/api/v1/auth/me").status_code == 200
    assert browser(client).get("/api/v1/auth/me", headers={"Authorization": f"Bearer {other_token}"}).status_code == 401


# ---- authorization, rate limits, market, websocket -------------------------------------------------------

def test_admin_endpoints_require_permission(env):
    client, _, svc = env
    register_and_login(client, "user@example.com")
    assert client.get("/api/v1/admin/users").status_code == 403
    client.portal.call(svc.create_user, "admin@example.com", PASSWORD, "admin", None)
    admin = browser(client)
    admin.post("/api/v1/auth/login", json={"email": "admin@example.com", "password": PASSWORD})
    page = admin.get("/api/v1/admin/users?limit=1").json()
    assert len(page["data"]) == 1 and page["next_cursor"] == "admin@example.com"
    rest = admin.get(f"/api/v1/admin/users?cursor={page['next_cursor']}").json()
    assert [u["email"] for u in rest["data"]] == ["user@example.com"] and rest["next_cursor"] is None
    assert admin.get("/api/v1/admin/system").json()["database"] == "ok"
    status = admin.get("/api/v1/market/status").json()
    assert status["contract"]["tradingsymbol"] == "NIFTY27OCT26FUT"
    assert status["session"]["exchange"] == "NSE" and isinstance(status["session"]["open"], bool)



def test_broker_session_status_and_relogin(env):
    client, _, svc = env
    register_and_login(client, "user@example.com")
    gw = client.app.state.market
    assert client.get("/api/v1/market/status").json()["broker"] is None        # synthetic feed: no session
    client.portal.call(svc.create_user, "admin@example.com", PASSWORD, "admin", None)
    admin = browser(client)
    admin.post("/api/v1/auth/login", json={"email": "admin@example.com", "password": PASSWORD})
    assert admin.post("/api/v1/admin/broker/relogin", headers=csrf(admin)).json()["error"]["code"] ==         "BROKER_SESSION_UNAVAILABLE"

    gw.broker = {"state": "failed", "feed_on": False, "logged_in_at": None, "next_action": "manual re-login",
                 "next_action_at": None, "last_error": "RuntimeError: Invalid totp (AB1050)", "attempts": 21}
    public = client.get("/api/v1/market/status").json()["broker"]
    assert public == {"state": "failed", "feed_on": False, "logged_in_at": None, "next_action": "manual re-login",
                      "next_action_at": None}                                    # no error text for traders
    assert admin.get("/api/v1/market/status").json()["broker"]["last_error"].endswith("(AB1050)")
    assert admin.get("/api/v1/admin/system").json()["broker_session"]["attempts"] == 21

    assert client.post("/api/v1/admin/broker/relogin", headers=csrf(client)).status_code == 403
    assert admin.post("/api/v1/admin/broker/relogin").status_code == 403         # cookie auth needs CSRF
    assert admin.post("/api/v1/admin/broker/relogin", headers=csrf(admin)).json() == {"accepted": True}
    assert gw.relogins == 1 and audit_actions()[-1] == "broker.relogin"

def test_session_profile_endpoint(env):
    client, _, _ = env
    assert client.get("/api/v1/market/profile?date=2026-09-29").status_code == 401
    register_and_login(client)
    assert client.get("/api/v1/market/profile?date=2026-09-29&row=10").json() == {"date": "2026-09-29", "row_size": 10}
    assert client.get("/api/v1/market/profile?date=2026-09-28").json()["error"]["code"] == "NOT_FOUND"
    assert client.get("/api/v1/market/profile?date=2026-09-29&row=3").json()["error"]["code"] == "INVALID_ROW"
    assert client.get("/api/v1/market/profile?date=yesterday").status_code == 422


def test_profile_history_endpoint_validates_its_inputs(env):
    client, _, _ = env
    assert client.get("/api/v1/market/profile/history").status_code == 401
    register_and_login(client)
    ok = client.get("/api/v1/market/profile/history?row=10&limit=20&from=2026-09-01&to=2026-10-07").json()
    assert ok == {"row_size": 10, "limit": 20, "from": "2026-09-01", "to": "2026-10-07", "sessions": []}
    assert client.get("/api/v1/market/profile/history").json()["limit"] == 5          # default
    bad = {"row=3": "INVALID_ROW", "limit=51": "VALIDATION_ERROR", "limit=0": "VALIDATION_ERROR",
           "from=2026-13-01": "INVALID_DATE", "from=yesterday": "VALIDATION_ERROR",
           "from=2026-10-07&to=2026-10-01": "INVALID_RANGE", "from=2024-01-01&to=2026-01-01": "INVALID_RANGE"}
    for q, code in bad.items():
        assert client.get(f"/api/v1/market/profile/history?{q}").json()["error"]["code"] == code, q


def test_footprint_history_endpoints(env):
    client, _, _ = env
    assert client.get("/api/v1/market/footprint/sessions").status_code == 401
    register_and_login(client)
    assert client.get("/api/v1/market/footprint/sessions?interval=180").json()["max_days"] == 10
    r = client.get("/api/v1/market/footprint/history?date=2026-10-07&interval=60&ppr=2")
    assert r.status_code == 200 and r.json()["row_size"] == 2
    assert "immutable" in r.headers["cache-control"] and r.headers["etag"]
    assert r.headers.get("content-encoding") == "gzip"                       # large days are compressed
    again = client.get("/api/v1/market/footprint/history?date=2026-10-07&interval=60&ppr=2",
                       headers={"If-None-Match": r.headers["etag"]})
    assert again.status_code == 304
    live = client.get("/api/v1/market/footprint/history?date=2026-10-08&interval=300")
    assert live.status_code == 200 and live.headers["cache-control"] == "no-store"
    assert client.get("/api/v1/market/footprint/history?date=2026-10-01").json()["error"]["code"] == "NOT_FOUND"
    for q, code in {"interval=61": "INVALID_INTERVAL", "ppr=9": "INVALID_PPR", "date=bad": "VALIDATION_ERROR"}.items():
        url = "/api/v1/market/footprint/history?" + ("" if q.startswith("date") else "date=2026-10-07&") + q
        assert client.get(url).json()["error"]["code"] == code, q


def test_contract_switch_needs_admin_system_and_is_audited(env):
    client, _, svc = env
    register_and_login(client, "user@example.com")
    client.headers["X-CSRF-Token"] = client.cookies.get("ofmp_csrf")
    denied = client.post("/api/v1/admin/contract", json={"token": "2"})
    assert denied.status_code == 403 and denied.json()["error"]["code"] == "FORBIDDEN"
    client.portal.call(svc.create_user, "root@example.com", PASSWORD, "super_admin", None)
    root = browser(client)
    root.post("/api/v1/auth/login", json={"email": "root@example.com", "password": PASSWORD})
    root.headers["X-CSRF-Token"] = root.cookies.get("ofmp_csrf")
    r = root.post("/api/v1/admin/contract", json={"token": "2"})
    assert r.status_code == 200 and r.json()["contract"]["tradingsymbol"] == "NIFTY24NOV26FUT"
    bad = root.post("/api/v1/admin/contract", json={"token": "999"})
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "UNKNOWN_CONTRACT"
    rows = run(_sql("SELECT target_id, details FROM audit_logs WHERE action = 'contract.switch'"))
    assert [tuple(r) for r in rows] == [("NIFTY24NOV26FUT", {"from": "NIFTY27OCT26FUT"})]


def test_login_rate_limit_per_account(env):
    client, _, _ = env
    register_and_login(client)
    codes = [client.post("/api/v1/auth/login", json={"email": "trader@example.com", "password": "wrong pass 9"}).status_code
             for _ in range(10)]
    assert codes[-1] == 429 and 401 in codes
    r = client.post("/api/v1/auth/login", json={"email": "trader@example.com", "password": PASSWORD})
    assert r.status_code == 429 and "retry-after" in r.headers


def test_websocket_with_a_real_session(env):
    client, _, _ = env
    register_and_login(client)
    with client.websocket_connect("/ws/v1/stream") as ws:
        assert ws.receive_json()["data"]["user"]["email"] == "trader@example.com"
        client.post("/api/v1/auth/logout", headers=csrf(client))
        client.portal.call(client.app.state.stream_hub.heartbeat_once)
        with pytest.raises(WebSocketDisconnect) as e:
            ws.receive_json()
        assert e.value.code == 4401
