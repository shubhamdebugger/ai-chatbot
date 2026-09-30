"""Time helpers — everything in India time (IST, UTC+5:30).

Plain English: every fact, message and case carries a date, day and time.
This module gives one clock for the whole code, and lets tests set the clock.
"""
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
_override = None  # tests can freeze the clock


def now() -> datetime:
    """The current time in IST (or the frozen test time)."""
    return _override if _override is not None else datetime.now(IST)


def set_clock(dt):
    """Tests only: freeze the clock at dt (None = real clock)."""
    global _override
    _override = dt


def iso(dt: datetime) -> str:
    """Machine format stored in memory, e.g. 2026-09-29T21:05:00+05:30."""
    return dt.isoformat(timespec="seconds")


def parse(s: str) -> datetime:
    """Read back a stored time."""
    return datetime.fromisoformat(s)


def stamp(dt: datetime) -> str:
    """Human format with day, e.g. 'Tue 29-Sep-2026 21:05' (Tushar's rule)."""
    return dt.strftime("%a %d-%b-%Y %H:%M")


def day_key(dt: datetime) -> str:
    """Calendar day in IST, used for daily counters, e.g. '2026-09-29'."""
    return dt.strftime("%Y-%m-%d")


def hm(s: str):
    """'10:00' -> (10, 0)."""
    h, m = s.split(":")
    return int(h), int(m)
