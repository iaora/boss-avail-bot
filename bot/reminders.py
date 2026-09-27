"""Reminder settings and deadline helpers (stored in the settings table, editable by hosts)."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

from . import db, timeutil

DEFAULTS = {
    "reminder_enabled": "1",
    "reminder_weekdays": "3,5",  # Wednesday and Friday
    "reminder_time": "18:00",
    "deadline_weekday": "6",  # Saturday
    "deadline_time": "12:00",
}


def parse_weekdays(value: str) -> list[int]:
    """'Wed, Fri' or '3,5' -> [3, 5] (Sunday=0)."""
    days = set()
    for part in value.replace(" ", ",").split(","):
        if part:
            days.add(int(part) if part.isdigit() else timeutil.parse_weekday(part))
    if not days or any(d not in range(7) for d in days):
        raise ValueError(f"Invalid days: {value!r}")
    return sorted(days)


@dataclass
class ReminderSettings:
    enabled: bool
    channel_id: int | None
    reminder_weekdays: list[int]
    reminder_time: str
    deadline_weekday: int
    deadline_time: str

    def reminder_times(self, week_start: date, tz: ZoneInfo) -> list[datetime]:
        """Every reminder for the week starting `week_start`, in order (all fall in the week before)."""
        return sorted(
            timeutil.before_week(week_start, day, self.reminder_time, tz) for day in self.reminder_weekdays
        )

    def latest_due(self, week_start: date, now: datetime, tz: ZoneInfo) -> datetime | None:
        """The most recent reminder for `week_start` whose time has passed, if any."""
        due = [t for t in self.reminder_times(week_start, tz) if t <= now]
        return due[-1] if due else None

    def deadline_at(self, week_start: date, tz: ZoneInfo) -> datetime:
        return timeutil.before_week(week_start, self.deadline_weekday, self.deadline_time, tz)

    def describe(self, tz: ZoneInfo) -> str:
        channel = f"<#{self.channel_id}>" if self.channel_id else "*not set*"
        days = " and ".join(timeutil.WEEKDAYS[d] for d in self.reminder_weekdays)
        return (
            f"**Automatic reminder:** {'on' if self.enabled else 'off'}\n"
            f"**Channel:** {channel}\n"
            f"**Reminders:** every {days} at {self.reminder_time} ({tz.key})\n"
            f"**Deadline:** {timeutil.WEEKDAYS[self.deadline_weekday]} at {self.deadline_time} ({tz.key}), "
            f"before the week starts"
        )


def slot_key(when: datetime) -> str:
    """Identifies one reminder slot, so each one is posted exactly once."""
    return str(int(when.timestamp()))


def load(conn: sqlite3.Connection, fallback_channel_id: int | None) -> ReminderSettings:
    def get(key: str) -> str:
        return db.get_setting(conn, key, DEFAULTS.get(key))

    channel = db.get_setting(conn, "cq_channel_id")
    return ReminderSettings(
        enabled=get("reminder_enabled") == "1",
        channel_id=int(channel) if channel else fallback_channel_id,
        reminder_weekdays=parse_weekdays(get("reminder_weekdays")),
        reminder_time=get("reminder_time"),
        deadline_weekday=int(get("deadline_weekday")),
        deadline_time=get("deadline_time"),
    )
