"""/api/v1/workspaces against a real database (skipped unless TEST_DATABASE_URL)."""
import asyncio
import os

import pytest

import config

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL not set")

if TEST_DATABASE_URL:
    import sqlalchemy as sa
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from backend.app.api.app import configure_api
    from backend.app.infrastructure.postgres.database import create_engine
    from backend.app.services.auth.email import MemoryEmailSender
    from backend.app.services.auth.service import AuthService
    from backend.app.services.workspaces import MAX_PER_USER, WorkspaceService

PASSWORD = "good password 1"
CONFIG = {"version": 1, "layout": 2, "charts": [{"id": "c1", "interval": 60}], "units": "qty"}


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


@pytest.fixture
def client():
    asyncio.run(_sql("TRUNCATE users, organizations, organization_members, user_sessions, refresh_tokens, "
                     "email_tokens, audit_logs, workspaces CASCADE"))
    app = FastAPI()
    configure_api(app, environment="development", cookie_secure=False, cors_origins=[],
                  max_request_bytes=200_000)
    engine = create_engine(TEST_DATABASE_URL)
    svc = AuthService(engine, "ws-test-secret-" + "q" * 32, MemoryEmailSender(), "http://testserver")
    app.state.auth_service, app.state.db_engine = svc, engine
    app.state.workspace_service = WorkspaceService(engine)
    with TestClient(app) as c:
        for email in ("a@example.com", "b@example.com"):
            c.portal.call(svc.create_user, email, PASSWORD, "user", None)
        yield c
        c.portal.call(engine.dispose)


def login(client, email):
    other = TestClient(client.app)
    other.portal = client.portal
    assert other.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD}).status_code == 200
    other.headers["X-CSRF-Token"] = other.cookies.get("ofmp_csrf")
    return other


def test_create_list_get_and_first_is_default(client):
    a = login(client, "a@example.com")
    r = a.post("/api/v1/workspaces", json={"name": "Scalping", "config": CONFIG, "config_version": 1})
    assert r.status_code == 201
    first = r.json()
    assert first["is_default"] and first["revision"] == 1 and first["config"] == CONFIG
    second = a.post("/api/v1/workspaces", json={"name": "Swing", "config": CONFIG, "config_version": 1}).json()
    assert not second["is_default"]
    listing = a.get("/api/v1/workspaces").json()
    assert [w["name"] for w in listing] == ["Scalping", "Swing"] and "config" not in listing[0]
    assert a.get(f"/api/v1/workspaces/{second['id']}").json()["config"] == CONFIG


def test_other_users_workspaces_look_missing(client):
    a, b = login(client, "a@example.com"), login(client, "b@example.com")
    wid = a.post("/api/v1/workspaces", json={"name": "Mine", "config": CONFIG, "config_version": 1}).json()["id"]
    for call in (lambda: b.get(f"/api/v1/workspaces/{wid}"),
                 lambda: b.patch(f"/api/v1/workspaces/{wid}", json={"revision": 1, "name": "Stolen"}),
                 lambda: b.delete(f"/api/v1/workspaces/{wid}"),
                 lambda: b.post(f"/api/v1/workspaces/{wid}/duplicate", json={"name": "Copy"})):
        r = call()
        assert r.status_code == 404 and r.json()["error"]["code"] == "NOT_FOUND"
    assert b.get("/api/v1/workspaces").json() == []
    assert a.get(f"/api/v1/workspaces/{wid}").json()["name"] == "Mine"


def test_optimistic_concurrency(client):
    a = login(client, "a@example.com")
    wid = a.post("/api/v1/workspaces", json={"name": "W", "config": CONFIG, "config_version": 1}).json()["id"]
    tab1 = a.patch(f"/api/v1/workspaces/{wid}", json={"revision": 1, "config": {**CONFIG, "layout": 4}})
    assert tab1.status_code == 200 and tab1.json()["revision"] == 2
    stale = a.patch(f"/api/v1/workspaces/{wid}", json={"revision": 1, "config": {**CONFIG, "layout": 1}})
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "REVISION_CONFLICT"
    assert a.get(f"/api/v1/workspaces/{wid}").json()["config"]["layout"] == 4


def test_names_unique_rename_duplicate_default_and_delete(client):
    a = login(client, "a@example.com")
    w1 = a.post("/api/v1/workspaces", json={"name": "One", "config": CONFIG, "config_version": 1}).json()
    w2 = a.post("/api/v1/workspaces", json={"name": "Two", "config": CONFIG, "config_version": 1}).json()
    dup = a.post("/api/v1/workspaces", json={"name": "One", "config": CONFIG, "config_version": 1})
    assert dup.status_code == 409 and dup.json()["error"]["code"] == "NAME_TAKEN"
    assert a.patch(f"/api/v1/workspaces/{w2['id']}", json={"revision": 1, "name": "One"}).status_code == 409
    copy = a.post(f"/api/v1/workspaces/{w1['id']}/duplicate", json={"name": "One (copy)"})
    assert copy.status_code == 201 and copy.json()["config"] == CONFIG
    made = a.patch(f"/api/v1/workspaces/{w2['id']}", json={"revision": 1, "is_default": True}).json()
    assert made["is_default"]
    assert [w["name"] for w in a.get("/api/v1/workspaces").json() if w["is_default"]] == ["Two"]
    assert a.delete(f"/api/v1/workspaces/{w2['id']}").status_code == 204
    remaining = a.get("/api/v1/workspaces").json()
    assert len(remaining) == 2 and sum(w["is_default"] for w in remaining) == 1   # default promoted
    # the freed name can be reused (unique among non-deleted only)
    assert a.post("/api/v1/workspaces", json={"name": "Two", "config": CONFIG, "config_version": 1}).status_code == 201
    actions = [r[0] for r in asyncio.run(_sql("SELECT action FROM audit_logs WHERE target_type='workspace'"))]
    assert actions.count("workspace.create") == 4 and actions.count("workspace.delete") == 1


def test_limits_and_validation(client):
    a = login(client, "a@example.com")
    big = {"blob": "x" * (70 * 1024)}
    r = a.post("/api/v1/workspaces", json={"name": "Big", "config": big, "config_version": 1})
    assert r.status_code == 413 and r.json()["error"]["code"] == "CONFIG_TOO_LARGE"
    assert a.post("/api/v1/workspaces", json={"name": "", "config": CONFIG, "config_version": 1}).status_code == 422
    assert a.post("/api/v1/workspaces", json={"name": "x", "config": CONFIG, "config_version": 1,
                                              "owner_user_id": "someone"}).status_code == 422   # no ownership fields
    for i in range(MAX_PER_USER):
        assert a.post("/api/v1/workspaces", json={"name": f"w{i}", "config": CONFIG, "config_version": 1}).status_code == 201
    over = a.post("/api/v1/workspaces", json={"name": "one more", "config": CONFIG, "config_version": 1})
    assert over.status_code == 409 and over.json()["error"]["code"] == "LIMIT_REACHED"


def test_requires_login_and_csrf(client):
    anon = TestClient(client.app)
    anon.portal = client.portal
    assert anon.get("/api/v1/workspaces").status_code == 401
    a = login(client, "a@example.com")
    del a.headers["X-CSRF-Token"]
    r = a.post("/api/v1/workspaces", json={"name": "x", "config": CONFIG, "config_version": 1})
    assert r.status_code == 403 and r.json()["error"]["code"] == "CSRF_FAILED"
