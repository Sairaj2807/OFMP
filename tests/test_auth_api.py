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
