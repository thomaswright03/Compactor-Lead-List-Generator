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


def utah(when):
    """An aware datetime (or epoch seconds) in Utah time."""
    if not isinstance(when, dt.datetime):
        when = dt.datetime.fromtimestamp(when, dt.UTC)
    return when.astimezone(UTAH or _FALLBACK)


def now():
    return utah(dt.datetime.now(dt.UTC))


def _clock(when, upper=False):
    hour = when.hour % 12 or 12
    half = "am" if when.hour < 12 else "pm"
    return f"{hour}:{when.minute:02d} {half.upper() if upper else half}"


def date_time_text(ts):
    """'Sep 29, 2026, 10:14 am' in Utah time; '' for no time."""
    if not ts:
        return ""
    when = utah(ts)
    return f"{when:%b} {when.day}, {when.year}, {_clock(when)}"


def clock_text(ts):
    """'12:19 PM Utah time' for a timestamp."""
    return f"{_clock(utah(ts), upper=True)} Utah time"
