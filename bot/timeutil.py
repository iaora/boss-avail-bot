"""Week and time helpers.

Conventions used throughout the bot:
- A week starts on Sunday 00:00 in the bot timezone (BOT_TIMEZONE, default America/New_York).
- A week is identified by the ISO date of that Sunday, e.g. "2026-09-27".
- Weekdays are numbered Sunday=0 ... Saturday=6.
- Anything shown to players uses Discord timestamps (<t:unix:F>) so it renders in each
  viewer's own timezone.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

WEEKDAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def sunday_index(d: date) -> int:
    """Weekday with Sunday=0 ... Saturday=6."""
    return (d.weekday() + 1) % 7


def current_week_start(now: datetime, tz: ZoneInfo) -> date:
    """The Sunday that started the week containing `now`."""
    local = now.astimezone(tz).date()
    return local - timedelta(days=sunday_index(local))


def upcoming_week_start(now: datetime, tz: ZoneInfo) -> date:
    """The next Sunday strictly after today -- the week players submit availability for."""
    return current_week_start(now, tz) + timedelta(days=7)


def parse_hhmm(value: str) -> time:
    hours, minutes = value.strip().split(":")
    return time(int(hours), int(minutes))


def parse_weekday(value: str) -> int:
    v = value.strip().lower()
    matches = [i for i, name in enumerate(WEEKDAYS) if v and name.lower().startswith(v)]
    if len(matches) != 1:
        raise ValueError(f"Unknown weekday: {value!r}")
    return matches[0]


def at_weekday(week_start: date, weekday: int, hhmm: str, tz: ZoneInfo) -> datetime:
    """The datetime for `weekday` at `hhmm` (local) within the week starting `week_start`."""
    day = week_start + timedelta(days=weekday)
    return datetime.combine(day, parse_hhmm(hhmm), tzinfo=tz)


def before_week(week_start: date, weekday: int, hhmm: str, tz: ZoneInfo) -> datetime:
    """The last `weekday` at `hhmm` strictly before the week starting `week_start`.

    Used for the reminder and deadline, which happen in the week *before* the target week.
    """
    return at_weekday(week_start - timedelta(days=7), weekday, hhmm, tz)


def discord_ts(dt: datetime | int, style: str = "F") -> str:
    unix = dt if isinstance(dt, int) else int(dt.timestamp())
    return f"<t:{unix}:{style}>"


def local_label(unix: int, tz: ZoneInfo, with_zone: bool = False, twelve_hour: bool = False) -> str:
    """e.g. 'Sun 12:00', 'Sun 12:00 EDT', or with twelve_hour 'Sun 12:00 PM EDT', in the given
    timezone -- for host-facing text."""
    local = datetime.fromtimestamp(unix, timezone.utc).astimezone(tz)
    if twelve_hour:
        # built by hand: strftime's no-leading-zero hour (%-I) isn't available on every platform
        text = f"{local:%a} {local.hour % 12 or 12}:{local:%M} {local:%p}"
    else:
        text = f"{local:%a %H:%M}"
    return f"{text} {local:%Z}" if with_zone else text


def short_week_label(week_start: date) -> str:
    """e.g. 'Sun Oct 4, 2026' -- for dropdown labels, which can't show Discord timestamps."""
    return week_start.strftime("%a %b %d, %Y").replace(" 0", " ")


def week_label(week_start: date, capital: bool = False) -> str:
    """e.g. 'week of Sunday Sep 27, 2026' (or 'Week of ...' with capital=True)."""
    return f"{'W' if capital else 'w'}eek of Sunday {week_start.strftime('%b %d, %Y').replace(' 0', ' ')}"
