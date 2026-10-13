"""Daily broker session: log in to Angel One before the open, start the trading
day, stop the feed after the close, and say so when something goes wrong.

Angel One tokens last a day, so a process that runs for weeks needs a fresh
login every trading morning. On each NSE trading day (holidays and special
sessions from the exchange calendar), relative to the session's open and close:

    open - 45 min (08:30)  full login (never a reused token), checked with a REST
                           call, then the server starts the day: fresh instrument
                           master, front-month contract (rolls on expiry), a new
                           live engine restored from today's stored trades, and
                           the feed connected.
    on failure             retry every 2 minutes until open - 5 min (09:10); every
                           failure is notified. After that it waits for a manual
                           re-login (admins: POST /api/v1/admin/broker/relogin).
    open .. close          watchdog: no tick for 5 minutes while the market is open
                           is notified once per day.
    close + 15 min (15:45) feed off. Overnight, weekends and holidays the feed
                           stays off (no reconnect loops); charts, history and
                           replay keep working.

A process started inside the window (a reboot at 10:00) logs in at once; its
retry window is 40 minutes from the first attempt. The broker calls are
injected (`login`, `start_day`, `stop_feed`), so this module knows nothing
about Angel, and the schedule is tested on a fake clock.
"""
import asyncio
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Awaitable, Callable, Optional

from backend.app.domain.market_data.calendar import ExchangeCalendar

log = logging.getLogger("ofmp.broker_session")


@dataclass(frozen=True)
class SessionTimes:
    login_lead_min: int = 45          # 09:15 open -> 08:30 login
    give_up_lead_min: int = 5         # last retry before 09:10
    stop_after_close_min: int = 15    # 15:30 close -> 15:45 feed off
    retry_sec: int = 120
    late_retry_window_min: int = 40   # a start inside the window retries this long
    no_tick_min: int = 5              # watchdog


@dataclass(frozen=True)
class Window:
    day: date
    login_at: datetime
    give_up_at: datetime
    open: datetime
    close: datetime
    stop_at: datetime


Notify = Callable[[str, str], Awaitable[None]]       # (level: "info" | "error", message)


class BrokerSession:
    def __init__(self, calendar: ExchangeCalendar, login: Callable[[], Awaitable[dict]],
                 start_day: Callable[[], Awaitable[dict]], stop_feed: Callable[[], Awaitable[None]],
                 notify: Notify, last_tick_ms: Callable[[], Optional[int]] = lambda: None,
                 times: Optional[SessionTimes] = None,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep):
        """login() -> {"expires_at": epoch s} raises on failure; start_day() -> {"contract", "rolled"};
        stop_feed(); notify(level, message) never raises (failures are logged)."""
        self.calendar, self.times = calendar, times or SessionTimes()
        self._login, self._start_day, self._stop_feed, self._notify = login, start_day, stop_feed, notify
        self._last_tick_ms, self._clock, self._sleep = last_tick_ms, clock, sleep
        self._wake = asyncio.Event()
        self._stopped = False
        self.state = "starting"       # starting | waiting | logging_in | ready | failed | stopped
        self.feed_on = False
        self.day_ready: Optional[date] = None      # trading day whose feed was started
        self.day_failed: Optional[date] = None     # gave up for this day (until a manual re-login)
        self.attempts = 0
        self.last_error: Optional[str] = None
        self.logged_in_at: Optional[datetime] = None
        self.token_expires_at: Optional[datetime] = None
        self.contract: Optional[str] = None
        self.next_action: Optional[tuple] = None   # (what, at)
        self._watchdog_day: Optional[date] = None
        self._manual = False

    # -- schedule ------------------------------------------------------------------

    def window(self, day: date) -> Optional[Window]:
        s = self.calendar.session(day)
        if s is None:
            return None
        t = self.times
        return Window(day, s.open - timedelta(minutes=t.login_lead_min), s.open - timedelta(minutes=t.give_up_lead_min),
                      s.open, s.close, s.close + timedelta(minutes=t.stop_after_close_min))

    def _today(self, now: datetime) -> date:
        return now.astimezone(self.calendar.tz).date()

    def next_window(self, now: datetime, horizon_days: int = 30) -> Optional[Window]:
        """Today's window if it has not ended, else the next trading day's."""
        today = self._today(now)
        for i in range(horizon_days + 1):
            w = self.window(today + timedelta(days=i))
            if w is not None and now < w.stop_at:
                return w
        return None

    def plan(self, now: datetime) -> tuple:
        """("start", window) | ("stop", None) | ("watch", window) | ("wait", until)."""
        w = self.window(self._today(now))
        if w is not None and w.login_at <= now < w.stop_at:
            if self.day_failed == w.day:
                return ("wait", w.stop_at)
            if self.day_ready != w.day:
                return ("start", w)
            return ("watch", w)
        if self.feed_on:
            return ("stop", None)
        nxt = self.next_window(now)
        return ("wait", nxt.login_at if nxt else now + timedelta(days=1))

    # -- loop ------------------------------------------------------------------------

    def stop(self) -> None:
        self._stopped = True
        self._wake.set()

    async def run(self) -> None:
        while not self._stopped:
            now = self._clock()
            action, arg = self.plan(now)
            if action == "start":
                await self._start(arg)
            elif action == "stop":
                await self._stop_day()
            elif action == "watch":
                await self._watchdog(now, arg)
                self.next_action = ("feed off", arg.stop_at)
                await self._wait_until(min(arg.stop_at, now + timedelta(seconds=60)))
            elif self._manual:
                await self._check_login()            # outside the window: verify the login only
            else:
                if self.state not in ("failed", "stopped"):   # "stopped": feed off after today's close
                    self.state = "waiting"
                failed_today = self.day_failed == self._today(now)
                self.next_action = ("manual re-login", None) if failed_today else ("login", arg)
                await self._wait_until(arg)

    async def _wait_until(self, at: datetime) -> None:
        """Sleeps until `at` in steps of at most a minute (so a changed clock is noticed), or a wake-up."""
        while not self._stopped:
            if self._wake.is_set():              # a re-login requested before we got here is not lost
                self._wake.clear()
                return
            remaining = (at - self._clock()).total_seconds()
            if remaining <= 0:
                return
            sleeper = asyncio.ensure_future(self._sleep(min(remaining, 60.0)))
            waker = asyncio.ensure_future(self._wake.wait())
            done, pending = await asyncio.wait({sleeper, waker}, return_when=asyncio.FIRST_COMPLETED)
            for p in pending:
                p.cancel()
            if waker in done:
                self._wake.clear()
                return

    async def _start(self, w: Window) -> None:
        self._manual = False
        first = self._clock()
        give_up = max(w.give_up_at, first + timedelta(minutes=self.times.late_retry_window_min))
        give_up = min(give_up, w.stop_at)
        self.attempts = 0
        while not self._stopped:
            self.state = "logging_in"
            self.attempts += 1
            try:
                info = await self._login()
                now = self._clock()
                self.logged_in_at = now
                exp = (info or {}).get("expires_at")
                self.token_expires_at = datetime.fromtimestamp(exp, timezone.utc) if exp else None
                started = await self._start_day()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.last_error = f"{type(e).__name__}: {e}"
                now = self._clock()
                log.warning("broker session attempt %d failed: %s", self.attempts, self.last_error)
                if now >= give_up:
                    self.state, self.day_failed = "failed", w.day
                    await self._say("error", f"Angel login failed {self.attempts} times; gave up for {w.day}. "
                                             f"Last error: {self.last_error}. Use Re-login now when fixed.")
                    return
                await self._say("error", f"Angel login failed at {self._hm(now)} (attempt {self.attempts}): "
                                         f"{self.last_error}. Retrying in {self.times.retry_sec // 60} min.")
                self.next_action = ("retry", now + timedelta(seconds=self.times.retry_sec))
                await self._wait_until(now + timedelta(seconds=self.times.retry_sec))
                continue
            failed_before = self.attempts > 1
            self.state, self.feed_on, self.day_ready, self.day_failed = "ready", True, w.day, None
            self.last_error = None
            self.contract = (started or {}).get("contract")
            log.info("broker session ready for %s: contract %s", w.day, self.contract)
            if (started or {}).get("rolled"):
                await self._say("info", f"Contract rolled to {self.contract}.")
            if failed_before:
                await self._say("info", f"Angel login succeeded at {self._hm(now)} after {self.attempts} attempts.")
            return

    async def _stop_day(self) -> None:
        try:
            await self._stop_feed()
        except Exception as e:                      # the feed stopping badly must not stop the schedule
            log.warning("stopping the feed failed: %r", e)
        self.feed_on = False
        if self.state != "failed":
            self.state = "stopped"
        log.info("broker session: feed off after the close")

    async def _watchdog(self, now: datetime, w: Window) -> None:
        t = self.times
        if not self.feed_on or self._watchdog_day == w.day:
            return
        if not (w.open + timedelta(minutes=t.no_tick_min) <= now < w.close):
            return
        last = self._last_tick_ms()
        if last is None or now.timestamp() * 1000 - last > t.no_tick_min * 60_000:
            self._watchdog_day = w.day
            since = "since the open" if last is None else f"since {self._hm(datetime.fromtimestamp(last / 1000, timezone.utc))}"
            await self._say("error", f"No market data {since} although the market is open.")

    # -- manual ----------------------------------------------------------------------

    def relogin(self) -> None:
        """Admin: inside today's window, log in and restart the day now (also after giving up);
        outside it, log in once to check the credentials (the feed stays off)."""
        self.day_failed = None
        self.day_ready = None
        self._manual = True
        self._wake.set()

    async def _check_login(self) -> None:
        self._manual = False
        try:
            info = await self._login()
        except Exception as e:
            self.last_error = f"{type(e).__name__}: {e}"
            await self._say("error", f"Manual Angel login failed: {self.last_error}")
            return
        self.logged_in_at = self._clock()
        exp = (info or {}).get("expires_at")
        self.token_expires_at = datetime.fromtimestamp(exp, timezone.utc) if exp else None
        self.last_error = None
        if self.state == "failed":
            self.state = "waiting"
        await self._say("info", "Manual Angel login succeeded (the feed starts at the next session).")

    # -- reporting -------------------------------------------------------------------

    async def _say(self, level: str, message: str) -> None:
        (log.error if level == "error" else log.info)("broker session: %s", message)
        try:
            await self._notify(level, message)
        except Exception:
            log.exception("broker notification failed")

    def _hm(self, at: datetime) -> str:
        return at.astimezone(self.calendar.tz).strftime("%H:%M")

    def status(self) -> dict:
        iso = lambda d: d.isoformat() if d else None   # noqa: E731
        what, at = self.next_action or (None, None)
        return {"state": self.state, "feed_on": self.feed_on, "attempts": self.attempts,
                "last_error": self.last_error, "logged_in_at": iso(self.logged_in_at),
                "token_expires_at": iso(self.token_expires_at), "contract": self.contract,
                "day_ready": iso(self.day_ready), "gave_up_for": iso(self.day_failed),
                "next_action": what, "next_action_at": iso(at)}
