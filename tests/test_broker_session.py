"""Daily broker session (backend/app/services/broker_session.py) on a fake clock,
plus the Angel login guard, dead-token handling and the feed's 401 path.

The calendar is NSE's shape with one holiday: Tue 2026-10-20 (Dussehra).
Times are IST: the 09:15 open gives the 08:30 login, retries until 09:10 and
the 15:45 feed off."""
import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from backend.app.domain.market_data.calendar import ExchangeCalendar
from backend.app.services.broker_session import BrokerSession

IST = ZoneInfo("Asia/Kolkata")
CAL = ExchangeCalendar.from_dict({"exchange": "NSE", "timezone": "Asia/Kolkata", "years": [2026],
                                  "regular_session": {"open": "09:15", "close": "15:30"},
                                  "holidays": {"2026-10-20": "Dussehra"}})


def ist(day, hm):
    h, m = map(int, hm.split(":"))
    return datetime(2026, 10, day, h, m, tzinfo=IST)


class World:
    """Fake clock + broker. `sleep` advances the clock; the run stops once `until` is reached."""

    def __init__(self, start, until, login_fails=0, rolled=False, ticks=True):
        self.now, self.until = start, until
        self.login_fails, self.rolled, self.ticks = login_fails, rolled, ticks
        self.events, self.notes = [], []
        self.session = BrokerSession(CAL, login=self.login, start_day=self.start_day, stop_feed=self.stop_feed,
                                     notify=self.notify, last_tick_ms=self.last_tick, clock=lambda: self.now,
                                     sleep=self.sleep)

    def hm(self):
        return self.now.astimezone(IST).strftime("%d %H:%M")

    async def sleep(self, sec):
        self.now += timedelta(seconds=sec)
        if self.now >= self.until:
            self.session.stop()
        await asyncio.sleep(0)

    async def login(self):
        self.events.append(("login", self.hm()))
        if self.login_fails:
            self.login_fails -= 1
            raise RuntimeError("Invalid totp (AB1050)")
        return {"expires_at": (self.now + timedelta(hours=15)).timestamp()}

    async def start_day(self):
        self.events.append(("start", self.hm()))
        return {"contract": "NIFTY24NOV26FUT" if self.rolled else "NIFTY27OCT26FUT", "rolled": self.rolled}

    async def stop_feed(self):
        self.events.append(("stop", self.hm()))

    async def notify(self, level, message):
        self.notes.append((level, self.hm(), message))

    def last_tick(self):
        return int(self.now.timestamp() * 1000) if self.ticks else None

    def run(self):
        asyncio.run(asyncio.wait_for(self.session.run(), timeout=10))
        return self


def test_trading_days_log_in_at_0830_and_stop_at_1545_skipping_weekends_and_holidays():
    # Fri 16 Oct 07:00 -> Wed 21 Oct 12:00: Fri, (Sat, Sun), Mon 19, (Tue 20 holiday), Wed 21
    w = World(ist(16, "07:00"), ist(21, "12:00")).run()
    assert w.events == [("login", "16 08:30"), ("start", "16 08:30"), ("stop", "16 15:45"),
                        ("login", "19 08:30"), ("start", "19 08:30"), ("stop", "19 15:45"),
                        ("login", "21 08:30"), ("start", "21 08:30")]
    assert w.notes == []
    s = w.session.status()
    assert s["state"] == "ready" and s["feed_on"] and s["contract"] == "NIFTY27OCT26FUT"
    assert s["next_action"] == "feed off" and s["next_action_at"].startswith("2026-10-21T15:45")
    assert "token" not in str({k: v for k, v in s.items() if k != "token_expires_at"})


def test_failed_logins_retry_every_two_minutes_and_report_each_failure_and_the_recovery():
    w = World(ist(13, "08:00"), ist(13, "10:00"), login_fails=2).run()
    assert w.events == [("login", "13 08:30"), ("login", "13 08:32"), ("login", "13 08:34"), ("start", "13 08:34")]
    assert [(lvl, at) for lvl, at, _ in w.notes] == [("error", "13 08:30"), ("error", "13 08:32"), ("info", "13 08:34")]
    assert "Invalid totp" in w.notes[0][2] and "after 3 attempts" in w.notes[2][2]
    assert w.session.state == "ready" and w.session.last_error is None


def test_gives_up_at_0910_then_waits_for_a_manual_relogin_or_the_next_day():
    w = World(ist(13, "08:00"), ist(14, "09:00"), login_fails=999).run()
    attempts = [at for what, at in w.events if what == "login"]
    assert attempts[0] == "13 08:30" and attempts[-1] == "14 08:58"
    day13 = [a for a in attempts if a.startswith("13")]
    assert day13[-1] == "13 09:10" and len(day13) == 21           # 08:30 .. 09:10 every 2 minutes
    assert "gave up for 2026-10-13" in [m for _, at, m in w.notes if at == "13 09:10"][-1]
    assert not any(what == "start" for what, _ in w.events)
    assert w.session.state == "logging_in"                        # the next morning's retries are running


def test_a_start_inside_market_hours_logs_in_at_once_with_a_40_minute_retry_window():
    w = World(ist(13, "10:00"), ist(13, "12:00"), login_fails=999).run()
    attempts = [at for what, at in w.events if what == "login"]
    assert attempts[0] == "13 10:00" and attempts[-1] == "13 10:40"
    assert w.session.state == "failed" and w.session.status()["gave_up_for"] == "2026-10-13"
    assert w.session.status()["next_action"] == "manual re-login"


def test_manual_relogin_restarts_the_day_after_giving_up():
    w = World(ist(13, "10:00"), ist(13, "11:30"), login_fails=21)

    async def go():
        task = asyncio.ensure_future(w.session.run())
        while w.session.state != "failed":
            await asyncio.sleep(0)
        w.login_fails = 0
        w.session.relogin()
        await asyncio.wait_for(task, timeout=10)
    asyncio.run(go())
    clicked = w.hm()
    # (the fake clock had moved on a minute by the time the test "clicked"; the login follows at once)
    assert w.events[-2:] == [("login", "13 10:41"), ("start", "13 10:41")] and clicked >= "13 10:41"
    assert w.session.state == "ready"


def test_manual_relogin_outside_market_hours_only_checks_the_login():
    w = World(ist(13, "18:00"), ist(13, "19:00"))

    async def go():
        task = asyncio.ensure_future(w.session.run())
        while w.session.state != "waiting":
            await asyncio.sleep(0)
        w.session.relogin()
        await asyncio.wait_for(task, timeout=10)
    asyncio.run(go())
    assert w.events == [("login", "13 18:01")]                   # no feed start in the evening (fake clock: +1 min)
    assert w.notes[-1][0] == "info" and "next session" in w.notes[-1][2]


def test_contract_roll_is_announced_and_missing_ticks_are_reported_once():
    w = World(ist(13, "08:00"), ist(13, "11:00"), rolled=True, ticks=False).run()
    msgs = [m for _, _, m in w.notes]
    assert msgs[0] == "Contract rolled to NIFTY24NOV26FUT."
    no_data = [(at, m) for _, at, m in w.notes if m.startswith("No market data")]
    assert len(no_data) == 1 and no_data[0][0] == "13 09:20"


def test_the_feed_is_turned_off_when_the_process_is_found_running_after_the_close():
    w = World(ist(13, "15:44"), ist(13, "16:00"))
    w.session.feed_on, w.session.day_ready = True, ist(13, "00:00").date()   # started earlier today
    w.run()
    assert w.events == [("stop", "13 15:45")]
    assert w.session.state == "stopped" and not w.session.feed_on


# ---- Angel login guard and dead tokens ----------------------------------------------------------

@pytest.fixture
def angel(tmp_path, monkeypatch):
    import angel_client
    import config
    monkeypatch.setattr(config, "TOKENS_FILE", str(tmp_path / "tokens.txt"))
    calls = []

    def login():
        calls.append("login")
        if angel_fail["n"]:
            angel_fail["n"] -= 1
            raise RuntimeError("[LOGIN] Failed: Invalid totp (AB1050)")
        return {"jwtToken": _jwt(+3600), "refreshToken": "r", "feedToken": "f"}

    def refresh(jwt, rt):
        calls.append("refresh")
        return {"jwtToken": _jwt(+3600), "refreshToken": "r2", "feedToken": "f2"}
    angel_fail = {"n": 0}
    monkeypatch.setattr(angel_client.autologin, "login", login)
    monkeypatch.setattr(angel_client.autologin, "generate_token", refresh)
    monkeypatch.setattr(angel_client, "_login_state", {"invalid": False, "failures": 0, "paused_until": 0.0,
                                                      "last_login_at": None, "last_error": None})
    return angel_client, calls, angel_fail


def _jwt(exp_in_sec):
    import base64
    import json
    import time
    body = base64.urlsafe_b64encode(json.dumps({"exp": int(time.time()) + exp_in_sec}).encode()).decode().rstrip("=")
    return f"e30.{body}.sig"


def test_a_forced_login_never_reuses_and_a_rejected_token_is_never_refreshed(angel):
    ac, calls, _ = angel
    ac.save_tokens(_jwt(+3600), "r", "f")
    assert ac.ensure_valid_token()["refresh_token"] == "r" and calls == []        # valid: reused
    ac.ensure_valid_token(force_login=True)
    assert calls == ["login"]
    ac.mark_token_invalid()
    ac.ensure_valid_token()
    assert calls == ["login", "login"]                    # cancelled token: full login, no refresh attempt
    ac.save_tokens(_jwt(-10), "r", "f")
    ac.ensure_valid_token()
    assert calls[-1] == "refresh"                         # merely expired: the cheap refresh is still tried


def test_repeated_login_failures_pause_automatic_logins_until_a_manual_reset(angel):
    ac, calls, fail = angel
    fail["n"] = 5
    for _ in range(3):
        with pytest.raises(RuntimeError, match="Invalid totp"):
            ac.ensure_valid_token(force_login=True)
    with pytest.raises(ac.LoginBlocked, match="paused"):
        ac.ensure_valid_token(force_login=True)
    assert calls == ["login"] * 3                         # the 4th never reached Angel
    assert ac.login_state()["failures"] == 3 and "Invalid totp" in ac.login_state()["last_error"]
    ac.reset_login_guard()
    fail["n"] = 0
    ac.ensure_valid_token(force_login=True)
    assert ac.login_state()["failures"] == 0 and ac.login_state()["last_login_at"] is not None


def test_the_feed_marks_the_token_dead_when_angel_rejects_the_handshake():
    from backend.app.infrastructure.providers.angelone.provider import AngelOneProvider

    class Rejected(Exception):
        def __init__(self, code):
            super().__init__(f"server rejected WebSocket connection: HTTP {code}")
            self.response = type("R", (), {"status_code": code})()

    for code, expect in ((401, 1), (403, 1), (503, 0)):
        dead = []

        async def connect(url, headers, code=code):
            raise Rejected(code)
        p = AngelOneProvider(credentials=lambda: {"jwt_token": "j", "feed_token": "f", "api_key": "k",
                                                  "client_code": "c"},
                             connect=connect, on_auth_failed=lambda: dead.append(1))
        with pytest.raises(Rejected):
            asyncio.run(p.connect())
        assert len(dead) == expect


def test_morning_start_keeps_an_admins_unexpired_contract_and_rolls_an_expired_one(monkeypatch):
    import angel_client
    import server
    activated = []

    async def activate(contract, start_feed=True):
        activated.append((contract["tradingsymbol"], start_feed))
        server.STATE["contract"] = contract
    monkeypatch.setattr(server, "_activate_contract", activate)
    monkeypatch.setattr(server, "_live_session_date", lambda: "2026-10-27")
    monkeypatch.setattr(angel_client, "fetch_instrument_master", lambda force=False: [])
    monkeypatch.setattr(angel_client, "resolve_configured_future",
                        lambda instruments=None: {"tradingsymbol": "NIFTY24NOV26FUT", "expiry": "24NOV2026"})
    saved = dict(server.STATE)
    try:
        server.STATE.update(contract={"tradingsymbol": "NIFTY29DEC26FUT", "expiry": "29DEC2026"}, contract_pinned=True)
        assert asyncio.run(server._broker_start_day()) == {"contract": "NIFTY29DEC26FUT", "rolled": False}
        server.STATE.update(contract={"tradingsymbol": "NIFTY27OCT26FUT", "expiry": "26OCT2026"}, contract_pinned=True)
        assert asyncio.run(server._broker_start_day()) == {"contract": "NIFTY24NOV26FUT", "rolled": True}
        assert server.STATE["contract_pinned"] is False
        server.STATE.update(contract={"tradingsymbol": "NIFTY27OCT26FUT", "expiry": "27OCT2026"}, contract_pinned=False)
        assert asyncio.run(server._broker_start_day())["contract"] == "NIFTY24NOV26FUT"   # not pinned: front month
    finally:
        server.STATE.clear()
        server.STATE.update(saved)
    assert activated == [("NIFTY29DEC26FUT", True), ("NIFTY24NOV26FUT", True), ("NIFTY24NOV26FUT", True)]
