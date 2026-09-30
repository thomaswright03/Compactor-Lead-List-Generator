"""Utah time, the one clock the site shows (Arco's shop is in Salt Lake City).

Every date and time on the pages, in the downloads and in the once-a-day rule
comes from here, so they always agree.
"""

import datetime as dt

UTAH: dt.tzinfo | None
try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    try:
        UTAH = ZoneInfo("America/Denver")
    except ZoneInfoNotFoundError:      # no time zone data on this machine
        UTAH = None
except ImportError:                    # pragma: no cover - Python without zoneinfo
    UTAH = None

# Without time zone data, Mountain Standard Time (Utah in winter) is close enough.
_FALLBACK = dt.timezone(dt.timedelta(hours=-7), "MST")


def utah(when: dt.datetime | float) -> dt.datetime:
    """An aware datetime (or epoch seconds) in Utah time."""
    if not isinstance(when, dt.datetime):
        when = dt.datetime.fromtimestamp(when, dt.UTC)
    return when.astimezone(UTAH or _FALLBACK)


def now() -> dt.datetime:
    return utah(dt.datetime.now(dt.UTC))


def _clock(when: dt.datetime) -> str:
    """'4:43 PM': the one way the site writes a time of day."""
    hour = when.hour % 12 or 12
    return f"{hour}:{when.minute:02d} {'AM' if when.hour < 12 else 'PM'}"


def date_time_text(ts: float | None) -> str:
    """'Sep 29, 2026, 10:14 AM' in Utah time; '' for no time."""
    if not ts:
        return ""
    when = utah(ts)
    return f"{when:%b} {when.day}, {when.year}, {_clock(when)}"


def day_clock_text(ts: float, today: dt.datetime | float) -> str:
    """'today at 4:43 PM', 'tomorrow at 9:05 AM' or 'Oct 2 at 9:05 AM' (Utah time),
    for a time seen from today."""
    when, day = utah(ts), utah(today).date()
    if when.date() == day:
        name = "today"
    elif when.date() == day + dt.timedelta(days=1):
        name = "tomorrow"
    else:
        name = f"{when:%b} {when.day}"
    return f"{name} at {_clock(when)}"


def clock_text(ts: float) -> str:
    """'12:19 PM' (Utah time) for a timestamp, written like date_time_text's time."""
    return _clock(utah(ts))
