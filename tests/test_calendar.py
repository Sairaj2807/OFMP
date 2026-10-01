"""NSE trading calendar: sessions, holidays, special sessions, next open,
coverage, and the shipped calendar file itself."""
import json
from datetime import date, datetime, timedelta, timezone

import pytest

from backend.app.domain.market_data.calendar import CALENDAR_DIR, ExchangeCalendar

IST = timezone(timedelta(hours=5, minutes=30))
NSE = ExchangeCalendar.load("nse")


def ist(*a):
    return datetime(*a, tzinfo=IST)


@pytest.mark.parametrize("at,open_", [
    (ist(2026, 10, 1, 9, 14, 59), False),      # Thursday, before the open
    (ist(2026, 10, 1, 9, 15), True),
    (ist(2026, 10, 1, 15, 29, 59), True),
    (ist(2026, 10, 1, 15, 30), False),         # close is exclusive
    (ist(2026, 10, 2, 11, 0), False),          # Gandhi Jayanti (Friday)
    (ist(2026, 10, 3, 11, 0), False),          # Saturday
    (ist(2026, 10, 5, 11, 0), True),           # Monday
    (ist(2026, 3, 3, 11, 0), False),           # Holi
])
def test_is_open(at, open_):
    assert NSE.is_open(at) is open_
    assert NSE.is_open(at.astimezone(timezone.utc)) is open_        # any time zone in, same answer


def test_next_open_skips_holidays_and_weekends():
    # Thursday after the close -> Friday is Gandhi Jayanti -> weekend -> Monday 09:15 IST
    assert NSE.next_open(ist(2026, 10, 1, 16, 0)) == ist(2026, 10, 5, 9, 15)
    assert NSE.next_open(ist(2026, 10, 1, 8, 0)) == ist(2026, 10, 1, 9, 15)       # later today
    assert NSE.next_open(ist(2026, 10, 1, 10, 0)) == ist(2026, 10, 5, 9, 15)      # in session: the next one
    status = NSE.status(ist(2026, 10, 2, 12, 0))
    assert status["open"] is False and status["holiday"] == "Mahatma Gandhi Jayanti" and status["session"] is None
    assert status["next_open"] == "2026-10-05T09:15:00+05:30" and status["calendar_covers_today"]


def test_special_sessions_and_coverage():
    cal = ExchangeCalendar.from_dict({
        "timezone": "Asia/Kolkata", "regular_session": {"open": "09:15", "close": "15:30"}, "years": [2026],
        "holidays": {"2026-11-10": "Diwali Balipratipada"},
        "special_sessions": {"2026-11-08": {"open": "18:00", "close": "19:00", "name": "Muhurat trading"}}})
    assert cal.is_open(ist(2026, 11, 8, 18, 30))                 # a Sunday, open for the special session
    assert not cal.is_open(ist(2026, 11, 8, 11, 0))
    assert cal.status(ist(2026, 11, 8, 18, 30))["session"]["name"] == "Muhurat trading"
    assert cal.covers(date(2026, 6, 1)) and not cal.covers(date(2027, 1, 4))
    assert cal.days_covered_ahead(date(2026, 12, 1)) == 30
    assert cal.is_open(ist(2027, 1, 26, 11, 0))                  # uncovered year: weekdays only (and says so)
    with pytest.raises(ValueError, match="outside the calendar's years"):
        ExchangeCalendar.from_dict({"timezone": "Asia/Kolkata", "regular_session": {"open": "09:15", "close": "15:30"},
                                    "years": [2026], "holidays": {"2027-01-26": "Republic Day"}})


def test_shipped_calendar_is_consistent():
    raw = json.loads((CALENDAR_DIR / "nse.json").read_text(encoding="utf-8"))
    days = [date.fromisoformat(d) for d in raw["holidays"]]
    assert days == sorted(days) and len(days) == len(set(days)) == 15
    assert all(d.weekday() < 5 for d in days)                    # weekend festivals close nothing extra
    assert NSE.years == (2026,) and NSE.exchange == "NSE"
