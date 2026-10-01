"""Exchange trading calendar: is the market open now, when does it next open.

The data (regular session, holidays, special sessions such as Muhurat
trading) lives in calendars/<exchange>.json, so a new year or an amended
circular is a data change, not a code change. `years` lists the years the
file is authoritative for; outside them the calendar falls back to
"weekdays, regular hours" and says so (covers() is False), which the
monitoring turns into an alert to update the file.

Used for: the ofmp_market_session_open metric (feed alerts stay quiet on
holidays and outside the session), and the session block of
/api/v1/market/status (the terminal shows "market closed, opens ...")."""
import json
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

CALENDAR_DIR = Path(__file__).with_name("calendars")


def _hm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


@dataclass(frozen=True)
class Session:
    day: date
    open: datetime           # timezone-aware, exchange time zone
    close: datetime
    name: Optional[str] = None   # set for special sessions


class ExchangeCalendar:
    def __init__(self, tz: str, open_: time, close: time, holidays: dict, special_sessions: dict,
                 years: tuple, exchange: str = ""):
        self.tz = ZoneInfo(tz)
        self.open_time, self.close_time = open_, close
        self.holidays = dict(holidays)                 # date -> name
        self.special = dict(special_sessions)          # date -> (open time, close time, name)
        self.years = tuple(years)
        self.exchange = exchange

    @classmethod
    def from_dict(cls, d: dict) -> "ExchangeCalendar":
        holidays = {date.fromisoformat(k): v for k, v in d.get("holidays", {}).items()}
        special = {date.fromisoformat(k): (_hm(v["open"]), _hm(v["close"]), v.get("name"))
                   for k, v in d.get("special_sessions", {}).items()}
        for day in holidays:
            if day.year not in d["years"]:
                raise ValueError(f"holiday {day} is outside the calendar's years {d['years']}")
        return cls(d["timezone"], _hm(d["regular_session"]["open"]), _hm(d["regular_session"]["close"]),
                   holidays, special, tuple(d["years"]), d.get("exchange", ""))

    @classmethod
    def load(cls, name: str = "nse", directory: Path = CALENDAR_DIR) -> "ExchangeCalendar":
        return cls.from_dict(json.loads((directory / f"{name}.json").read_text(encoding="utf-8")))

    # -- queries ---------------------------------------------------------------

    def covers(self, day: date) -> bool:
        return day.year in self.years

    def session(self, day: date) -> Optional[Session]:
        """The trading session on `day`, or None (weekend or holiday)."""
        if day in self.special:
            o, c, name = self.special[day]
            return Session(day, datetime.combine(day, o, self.tz), datetime.combine(day, c, self.tz), name)
        if day.weekday() >= 5 or day in self.holidays:
            return None
        return Session(day, datetime.combine(day, self.open_time, self.tz),
                       datetime.combine(day, self.close_time, self.tz))

    def holiday_name(self, day: date) -> Optional[str]:
        return self.holidays.get(day)

    def is_open(self, at: datetime) -> bool:
        local = at.astimezone(self.tz)
        s = self.session(local.date())
        return s is not None and s.open <= local < s.close

    def next_open(self, at: datetime, horizon_days: int = 30) -> Optional[datetime]:
        """The next session start strictly after `at` (or `at`'s session start if it is still ahead)."""
        local = at.astimezone(self.tz)
        for i in range(horizon_days + 1):
            s = self.session(local.date() + timedelta(days=i))
            if s is not None and s.open > local:
                return s.open
        return None

    def days_covered_ahead(self, today: date) -> int:
        """Days from `today` to the end of the last covered year (negative once past it)."""
        return (date(max(self.years), 12, 31) - today).days if self.years else -1

    def status(self, at: datetime) -> dict:
        """JSON-ready summary for the API."""
        local = at.astimezone(self.tz)
        s = self.session(local.date())
        nxt = self.next_open(at)
        return {
            "exchange": self.exchange,
            "open": self.is_open(at),
            "session": None if s is None else {"open": s.open.isoformat(), "close": s.close.isoformat(),
                                               "name": s.name},
            "holiday": self.holiday_name(local.date()),
            "next_open": nxt.isoformat() if nxt else None,
            "calendar_covers_today": self.covers(local.date()),
        }
