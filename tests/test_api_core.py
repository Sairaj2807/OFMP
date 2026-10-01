"""API layer without a database: security primitives, permissions, rate
limiting, error envelope, request ids, body limits, health, and the
/ws/v1/stream gateway (with a fake authenticator)."""
import asyncio
import json
import time
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel
from starlette.websockets import WebSocketDisconnect

from backend.app.api.app import configure_api
from backend.app.api.websocket.stream import Encoded, StreamConnection, StreamHub, encode
from backend.app.core.errors import AppError
from backend.app.core.permissions import ROLE_PERMISSIONS, has_permission
from backend.app.core.ratelimit import SlidingWindowLimiter
from backend.app.core.security import (create_access_token, csrf_matches, decode_access_token, hash_password,
                                       hash_token, needs_rehash, new_opaque_token, validate_password_strength,
                                       verify_password)
from backend.app.services.auth.service import AuthUser

SECRET = "test-secret-" + "x" * 32


# ---- security primitives ------------------------------------------------------------

def test_argon2id_hash_and_verify():
    h = hash_password("correct horse 1")
    assert h.startswith("$argon2id$") and not needs_rehash(h)
    assert verify_password(h, "correct horse 1")
    assert not verify_password(h, "correct horse 2")
    assert not verify_password(None, "anything")          # unknown account: dummy hash, always False
    assert not verify_password("not-a-hash", "x")


@pytest.mark.parametrize("pw,ok", [("short1", False), ("allletterslong", False), ("1234567890123", False),
                                   ("letters and 1 digit", True), ("pässwörd!!!", True)])
def test_password_strength(pw, ok):
    assert (validate_password_strength(pw) is None) == ok


def test_access_token_round_trip_and_rejections():
    uid, sid = uuid.uuid4(), uuid.uuid4()
    tok = create_access_token(SECRET, uid, "admin", sid, 900)
    claims = decode_access_token(SECRET, tok)
    assert (claims.user_id, claims.role, claims.session_id) == (uid, "admin", sid)
    assert decode_access_token("other-secret-" + "y" * 32, tok) is None                      # wrong key
    assert decode_access_token(SECRET, create_access_token(SECRET, uid, "user", sid, 10, now=time.time() - 60)) is None  # expired
    assert decode_access_token(SECRET, tok[:-2] + ("aa" if tok[-2:] != "aa" else "bb")) is None  # tampered
    import jwt
    wrong_type = jwt.encode({"sub": str(uid), "sid": str(sid), "iss": "ofmp", "iat": int(time.time()),
                             "exp": int(time.time()) + 60, "typ": "refresh"}, SECRET, algorithm="HS256")
    assert decode_access_token(SECRET, wrong_type) is None
    none_alg = jwt.encode({"sub": str(uid), "sid": str(sid), "iss": "ofmp", "iat": 1, "exp": 9999999999,
                           "typ": "access"}, None, algorithm="none")
    assert decode_access_token(SECRET, none_alg) is None


def test_opaque_tokens_store_only_a_hash():
    token, digest = new_opaque_token()
    assert len(token) >= 40 and digest == hash_token(token) and token not in digest


def test_csrf_matching():
    assert csrf_matches("abc", "abc")
    assert not csrf_matches("abc", "abd") and not csrf_matches(None, "abc") and not csrf_matches("abc", None)


# ---- permissions and rate limiting ------------------------------------------------------

def test_role_permissions_are_cumulative():
    assert has_permission("user", "market.read") and not has_permission("user", "admin.users")
    assert has_permission("support", "admin.users") and not has_permission("support", "admin.system")
    assert has_permission("admin", "admin.system")
    assert ROLE_PERMISSIONS["user"] < ROLE_PERMISSIONS["support"] < ROLE_PERMISSIONS["admin"] <= ROLE_PERMISSIONS["super_admin"]
    assert not has_permission("unknown-role", "market.read")


def test_sliding_window_limiter():
    now = [0.0]
    lim = SlidingWindowLimiter(3, 10, clock=lambda: now[0])
    assert [lim.hit_now("k")[0] for _ in range(4)] == [True, True, True, False]
    assert lim.hit_now("other")[0]                                # keys are independent
    allowed, retry = lim.hit_now("k")
    assert not allowed and 1 <= retry <= 11
    now[0] = 10.5
    assert lim.hit_now("k")[0]                                    # window slid
    with pytest.raises(AppError) as e:
        for _ in range(5):
            asyncio.run(lim.check("k"))
    assert e.value.status == 429 and "Retry-After" in e.value.headers


# ---- app-level behaviour --------------------------------------------------------------------

class Body(BaseModel):
    n: int


def make_app(max_bytes=2000):
    app = FastAPI()
    configure_api(app, environment="development", cookie_secure=False, cors_origins=[],
                  max_request_bytes=max_bytes)

    @app.post("/api/v1/_echo")
    async def echo(body: Body):
        return body

    @app.get("/api/v1/_boom")
    async def boom():
        raise RuntimeError("secret internal detail")
    return app


def test_error_envelope_validation_and_request_id():
    c = TestClient(make_app())
    r = c.post("/api/v1/_echo", json={"n": "x"}, headers={"X-Request-ID": "req-12345678"})
    assert r.status_code == 422 and r.headers["x-request-id"] == "req-12345678"
    err = r.json()["error"]
    assert err["code"] == "VALIDATION_ERROR" and err["request_id"] == "req-12345678"
    assert err["details"][0]["field"] == "n"
    generated = c.get("/live").headers["x-request-id"]
    assert len(generated) == 32 and c.get("/live", headers={"X-Request-ID": "bad id!"}).headers["x-request-id"] != "bad id!"


def test_unexpected_errors_do_not_leak_internals():
    r = TestClient(make_app(), raise_server_exceptions=False).get("/api/v1/_boom")
    assert r.status_code == 500 and r.json()["error"]["code"] == "INTERNAL_ERROR"
    assert "secret" not in r.text


def test_body_size_limit():
    r = TestClient(make_app(max_bytes=50)).post("/api/v1/_echo", content=b'{"n": 1, "pad": "' + b"x" * 100 + b'"}',
                                                headers={"content-type": "application/json"})
    assert r.status_code == 413 and r.json()["error"]["code"] == "PAYLOAD_TOO_LARGE"


def test_health_without_dependencies_and_auth_unavailable():
    c = TestClient(make_app())
    assert c.get("/live").json() == {"status": "alive"}
    ready = c.get("/ready")
    assert ready.status_code == 200 and ready.json()["checks"]["database"] == "not_configured"
    r = c.post("/api/v1/auth/login", json={"email": "a@example.com", "password": "x"})
    assert r.status_code == 503 and r.json()["error"]["code"] == "AUTH_UNAVAILABLE"
    assert c.get("/api/v1/auth/me").status_code == 503


# ---- WebSocket stream ------------------------------------------------------------------------------

class FakeGateway:
    def __init__(self):
        self.snapshots = 0

    def active_contract(self):
        return {"tradingsymbol": "NIFTY27OCT26FUT"}

    def feed_status(self):
        return {"connected": True}

    def database_stats(self):
        return None

    async def list_contracts(self):
        return [{"token": "1", "tradingsymbol": "NIFTY27OCT26FUT"}, {"token": "2", "tradingsymbol": "NIFTY24NOV26FUT"}]

    async def switch_contract(self, token):
        for c in await self.list_contracts():
            if c["token"] == token:
                return c
        raise ValueError(f"token {token} is not among the switchable contracts")

    def chart_snapshot(self, ppr, interval):
        self.snapshots += 1
        return {"ppr": ppr, "interval": interval, "n": self.snapshots}


def user(role="user"):
    return AuthUser(uuid.UUID(int=1), "u@example.com", "U", role, True, uuid.UUID(int=2))


def ws_app(valid_tokens, max_per_user=5, gateway=None):
    app = make_app()
    tokens = dict(valid_tokens)

    async def authenticate(token):
        return tokens.get(token)
    app.state.stream_hub = StreamHub(gateway or FakeGateway(), authenticate, allowed_intervals=(60, 300),
                                     max_per_user=max_per_user)
    return app, tokens


def test_ws_rejects_missing_or_bad_token_and_foreign_origin():
    app, _ = ws_app({"good-token-123": user()})
    c = TestClient(app)
    with pytest.raises(WebSocketDisconnect) as e:
        with c.websocket_connect("/ws/v1/stream") as ws:
            ws.send_json({"action": "auth", "token": "wrong-token-123"})
            ws.receive_json()
    assert e.value.code == 4401
    with pytest.raises(WebSocketDisconnect) as e:
        with c.websocket_connect("/ws/v1/stream", headers={"origin": "https://evil.example"}) as ws:
            ws.receive_json()
    assert e.value.code == 4403
    assert app.state.stream_hub.rejected == {"origin": 1, "auth": 1, "limit": 0}


def test_ws_subscribe_snapshot_sequence_and_validation():
    app, _ = ws_app({"good-token-123": user()})
    c = TestClient(app)
    c.cookies.set("ofmp_access", "good-token-123")
    with c.websocket_connect("/ws/v1/stream", headers={"origin": "http://testserver"}) as ws:
        welcome = ws.receive_json()
        assert welcome["type"] == "welcome" and welcome["seq"] == 1 and welcome["data"]["intervals"] == [60, 300]
        ws.send_json({"action": "subscribe", "streams": ["chart"], "ppr": 2, "interval": 999})
        assert ws.receive_json()["data"]["code"] == "INVALID_INTERVAL"
        ws.send_json({"action": "subscribe", "streams": ["chart"], "interval": 60, "contract": "OTHER"})
        assert ws.receive_json()["data"]["code"] == "CONTRACT_NOT_AVAILABLE"
        ws.send_json({"action": "subscribe", "streams": ["bogus"]})
        assert ws.receive_json()["data"]["code"] == "INVALID_MESSAGE"
        ws.send_json({"action": "subscribe", "streams": ["chart"], "ppr": 2, "interval": 300,
                      "contract": "NIFTY27OCT26FUT"})
        sub = ws.receive_json()
        snap = ws.receive_json()
        assert sub["type"] == "subscribed" and snap["type"] == "snapshot"
        assert snap["data"]["ppr"] == 2 and snap["data"]["interval"] == 300
        ws.send_json({"action": "ping"})
        pong = ws.receive_json()
        assert pong["type"] == "pong" and pong["seq"] == snap["seq"] + 1 == 7


def test_ws_connection_limit_per_user():
    app, _ = ws_app({"good-token-123": user()}, max_per_user=1)
    c = TestClient(app)
    c.cookies.set("ofmp_access", "good-token-123")
    with c.websocket_connect("/ws/v1/stream") as first:
        first.receive_json()
        with pytest.raises(WebSocketDisconnect) as e:
            with c.websocket_connect("/ws/v1/stream") as second:
                second.receive_json()
        assert e.value.code == 4429


def test_ws_backpressure_keeps_only_the_latest_snapshot_per_subscription():
    conn = StreamConnection(ws=None, user=user(), token="t")
    for n in range(5):
        conn.offer_snapshot("a", {"n": n})
    conn.offer_snapshot("b", {"n": 9})
    assert conn._pending == {"a": {"n": 4}, "b": {"n": 9}} and conn.snapshots_coalesced == 4


def test_push_builds_and_encodes_each_distinct_snapshot_once():
    gw = FakeGateway()
    hub = StreamHub(gw, authenticate=None, allowed_intervals=(60, 300))
    conns = [StreamConnection(ws=None, user=user(), token="t") for _ in range(3)]
    for c in conns:
        c.subscriptions = {"a": (1, 60), "b": (1, 300)}
    hub.connections = set(conns)
    hub.push_once()
    assert gw.snapshots == 2                                   # one build per distinct (ppr, interval)
    shared = {id(c._pending["a"]) for c in conns}
    assert len(shared) == 1 and isinstance(conns[0]._pending["a"], Encoded)
    assert json.loads(conns[0]._pending["a"]) == {"ppr": 1, "interval": 60, "n": 1}


class _CaptureWs:
    def __init__(self):
        self.sent = []

    async def send_text(self, text):
        self.sent.append(text)


def test_encoded_and_plain_payloads_produce_the_same_message():
    conn = StreamConnection(ws=_CaptureWs(), user=user(), token="t")
    asyncio.run(conn.send("snapshot", encode({"x": [1, 2]}), "c1"))
    asyncio.run(conn.send("snapshot", {"x": [1, 2]}, "c1"))
    asyncio.run(conn.send("ping"))
    a, b, ping = (json.loads(t) for t in conn.ws.sent)
    assert a["data"] == b["data"] == {"x": [1, 2]} and a["id"] == b["id"] == "c1"
    assert (a["seq"], b["seq"], ping["seq"]) == (1, 2, 3) and ping["data"] is None and "id" not in ping


def test_ws_multiple_subscriptions_on_one_connection():
    app, _ = ws_app({"good-token-123": user()})
    c = TestClient(app)
    c.cookies.set("ofmp_access", "good-token-123")
    with c.websocket_connect("/ws/v1/stream") as ws:
        ws.receive_json()
        for sid, interval in (("c1", 60), ("c2", 300)):
            ws.send_json({"action": "subscribe", "id": sid, "streams": ["chart"], "interval": interval})
        got = {}
        while len(got) < 2:
            m = ws.receive_json()
            if m["type"] == "snapshot":
                got[m["id"]] = m["data"]["interval"]
        assert got == {"c1": 60, "c2": 300}
        ws.send_json({"action": "unsubscribe", "id": "c1"})
        while (m := ws.receive_json())["type"] != "unsubscribed":
            pass
        assert m["id"] == "c1"
        ws.send_json({"action": "subscribe", "id": "bad id!", "streams": ["chart"]})
        assert ws.receive_json()["data"]["code"] == "INVALID_MESSAGE"
        for i in range(6):
            ws.send_json({"action": "subscribe", "id": f"x{i}", "streams": ["chart"], "interval": 60})
        codes = []
        while len(codes) < 1:
            m = ws.receive_json()
            if m["type"] == "error":
                codes.append(m["data"]["code"])
        assert codes == ["TOO_MANY_SUBSCRIPTIONS"]


def test_ws_heartbeat_closes_revoked_sessions():
    app, tokens = ws_app({"good-token-123": user()})
    hub = app.state.stream_hub
    with TestClient(app) as c:                            # shared portal: the hub and socket share a loop
        c.cookies.set("ofmp_access", "good-token-123")
        with c.websocket_connect("/ws/v1/stream") as ws:
            ws.receive_json()
            tokens.pop("good-token-123")                  # session revoked
            conn = next(iter(hub.connections))
            c.portal.call(hub.heartbeat_once)
            with pytest.raises(WebSocketDisconnect) as e:
                ws.receive_json()
            assert e.value.code == 4401
    assert conn not in hub.connections


def test_ws_requires_market_permission():
    app, _ = ws_app({"good-token-123": AuthUser(uuid.UUID(int=3), "x@example.com", None, "nobody", True)})
    c = TestClient(app)
    c.cookies.set("ofmp_access", "good-token-123")
    with pytest.raises(WebSocketDisconnect) as e:
        with c.websocket_connect("/ws/v1/stream") as ws:
            ws.receive_json()
    assert e.value.code == 4403


# ---- the real application --------------------------------------------------------------------------

def test_server_mounts_v1_api_and_gateway_reads_live_state(monkeypatch):
    import server
    from orderbook_engine import TickProcessorState
    # FastAPI nests included routers, so check the published schema and real requests, not app.routes
    documented = set(server.app.openapi()["paths"])
    assert {"/api/v1/auth/login", "/api/v1/auth/refresh", "/api/v1/market/status", "/api/v1/admin/users",
            "/health", "/ready", "/live", "/api/v1/admin/contract"} <= documented
    # the legacy API is gone (nothing documented outside /api/v1)
    assert not [p for p in documented if p.startswith("/api/") and not p.startswith("/api/v1/")]
    c = TestClient(server.app)                                   # no lifespan: startup (broker login) not run
    assert c.get("/live").json() == {"status": "alive"}
    assert c.post("/api/v1/auth/login", json={"email": "a@example.com", "password": "x"}).status_code == 503
    # retired legacy pages redirect to the terminal; account links keep their token
    for old, new in (("/", "/app/"), ("/chart", "/app/"), ("/replay", "/app/"), ("/login", "/app/login/"),
                     ("/reset-password?token=abc", "/app/reset-password/?token=abc"),
                     ("/verify-email?token=xyz", "/app/verify-email/?token=xyz")):
        r = c.get(old, follow_redirects=False)
        assert r.status_code == 308 and r.headers["location"] == new, old
    for gone in ("/api/status", "/api/contracts", "/api/replay/sessions", "/static/app.js"):
        assert c.get(gone).status_code == 404, gone
    monkeypatch.setitem(server.STATE, "contract", {"tradingsymbol": "T", "tick_size": 0.1, "lotsize": 65, "token": "1"})
    monkeypatch.setitem(server.STATE, "engine", TickProcessorState(0.1))
    gw = server.ServerMarketGateway()
    assert gw.active_contract()["tradingsymbol"] == "T"
    snap = gw.chart_snapshot(1, 60)
    assert snap["ready"] and snap["chart"]["interval_sec"] == 60


# ---- metrics ---------------------------------------------------------------------------------------

def test_metrics_endpoint_exposes_series_with_bounded_route_labels():
    app, _ = ws_app({"good-token-123": user()})
    c = TestClient(app)
    c.get("/live")
    c.get("/api/v1/auth/sessions/00000000-0000-0000-0000-000000000001")   # templated route, raw id must not leak
    body = c.get("/metrics").text
    for name in ("ofmp_http_requests_total", "ofmp_http_request_duration_seconds", "ofmp_ws_connections",
                 "ofmp_ticks_received_total", "ofmp_trades_classified_total", "ofmp_db_rows_pending",
                 "ofmp_ratelimit_fallbacks_total"):
        assert name in body
    open_line = next(line for line in body.splitlines() if line.startswith("ofmp_market_session_open "))
    assert open_line.split()[1] in ("0.0", "1.0")
    assert float(next(line for line in body.splitlines()
                      if line.startswith("ofmp_market_calendar_days_left ")).split()[1]) > -400
    assert 'route="/live"' in body
    assert "00000000-0000-0000-0000-000000000001" not in body


# ---- replay over the stream -----------------------------------------------------------------------

REPLAY_RECORDS = [{"ts_ms": 1_790_653_500_000 + i * 20_000, "ltp": 100.0 + (i % 3) / 10, "qty": 65,
                   "algo_side": "BUY" if i % 2 else "SELL", "trade_id": i + 1} for i in range(12)]


class ReplayGateway(FakeGateway):
    async def replay_sessions(self):
        return [{"date": "2026-09-29", "trades": len(REPLAY_RECORDS)}]

    async def open_replay(self, date):
        from backend.app.services.replay import ReplaySession
        if date != "2026-09-29":
            raise ValueError(f"no trades recorded on {date}")
        return ReplaySession(date, REPLAY_RECORDS, 0.1)

    def replay_snapshot(self, session, ppr, interval):
        return {"ready": True, "mode": "replay", "ppr": ppr, "interval": interval, "replay": session.meta()}


def next_of(ws, type_):
    while True:
        m = ws.receive_json()
        if m["type"] == type_:
            return m


def test_ws_replay_start_controls_and_back_to_live():
    app, _ = ws_app({"good-token-123": user()}, gateway=ReplayGateway())
    c = TestClient(app)
    c.cookies.set("ofmp_access", "good-token-123")
    with c.websocket_connect("/ws/v1/stream") as ws:
        ws.receive_json()
        ws.send_json({"action": "replay", "id": "c1", "date": "2026-09-29", "interval": 60, "speed": 5, "autoplay": False})
        started = next_of(ws, "replay_started")
        assert started["id"] == "c1" and started["data"]["total"] == 12 and started["data"]["speed"] == 5
        snap = next_of(ws, "snapshot")
        assert snap["data"]["mode"] == "replay" and snap["data"]["replay"]["index"] == 0

        ws.send_json({"action": "replay_control", "id": "c1", "command": "step", "unit": "trade"})
        assert next_of(ws, "snapshot")["data"]["replay"]["index"] == 1
        ws.send_json({"action": "replay_control", "id": "c1", "command": "seek", "value": REPLAY_RECORDS[6]["ts_ms"]})
        assert next_of(ws, "snapshot")["data"]["replay"]["index"] == 7
        ws.send_json({"action": "replay_control", "id": "c1", "command": "speed", "value": 3})
        assert next_of(ws, "error")["data"]["code"] == "INVALID_CONTROL"
        ws.send_json({"action": "replay", "id": "c1", "date": "2026-09-29", "ppr": 3, "interval": 300})
        kept = next_of(ws, "snapshot")["data"]
        assert kept["replay"]["index"] == 7 and kept["ppr"] == 3             # same day: position kept

        ws.send_json({"action": "subscribe", "id": "c1", "streams": ["chart"], "interval": 60})   # back to live
        assert next_of(ws, "subscribed")["id"] == "c1"
        assert "replay" not in next_of(ws, "snapshot")["data"]
        ws.send_json({"action": "replay_control", "id": "c1", "command": "play"})
        assert next_of(ws, "error")["data"]["code"] == "NO_REPLAY"


def test_ws_replay_errors_and_limits():
    app, _ = ws_app({"good-token-123": user()}, gateway=ReplayGateway())
    c = TestClient(app)
    c.cookies.set("ofmp_access", "good-token-123")
    with c.websocket_connect("/ws/v1/stream") as ws:
        ws.receive_json()
        ws.send_json({"action": "replay", "id": "c1", "date": "2020-01-01"})
        err = next_of(ws, "error")
        assert err["data"]["code"] == "REPLAY_UNAVAILABLE" and "2020-01-01" in err["data"]["message"]
        ws.send_json({"action": "replay", "id": "c1", "date": "not-a-date"})
        assert next_of(ws, "error")["data"]["code"] == "INVALID_MESSAGE"
        for sid in ("r1", "r2", "r3"):
            ws.send_json({"action": "replay", "id": sid, "date": "2026-09-29", "autoplay": False})
        assert next_of(ws, "error")["data"]["code"] == "TOO_MANY_REPLAYS"


def test_replay_sessions_endpoint_requires_market_read():
    app, _ = ws_app({}, gateway=ReplayGateway())
    app.state.market = ReplayGateway()
    assert TestClient(app).get("/api/v1/market/replay/sessions").status_code == 503   # no auth service here
