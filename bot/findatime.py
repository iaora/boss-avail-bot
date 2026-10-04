"""/findatime: find hours when several people are free. Pure logic (no Discord): reading
plain-English availability, drawing the hour grid, and finding the best overlapping times.

A poll covers the 7 days (168 hours) from when it was started. Each person types their availability
in their own timezone; it's stored as absolute UTC hours ("epoch hours": unix seconds // 3600), so
people in different timezones line up, and every viewer sees the grid in their own timezone. A
person's dates run from their own today to the poll's end, so "Sunday" means the next Sunday that's
still ahead for them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from collections.abc import Callable
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

POLL_DAYS = 7
POLL_HOURS = POLL_DAYS * 24
FREE, BUSY, PAST = "🟩", "⬛", "⬜"

# python weekday numbers (Monday = 0)
DAY_NAMES = {
    "mon": 0, "monday": 0, "tue": 1, "tues": 1, "tuesday": 1, "wed": 2, "weds": 2, "wednesday": 2,
    "thu": 3, "thur": 3, "thurs": 3, "thursday": 3, "fri": 4, "friday": 4, "sat": 5, "saturday": 5,
    "sun": 6, "sunday": 6,
}
DAY_GROUPS = {
    "weekdays": range(0, 5), "weekday": range(0, 5), "weekends": range(5, 7), "weekend": range(5, 7),
    "every day": range(7), "everyday": range(7), "daily": range(7), "any day": range(7), "all week": range(7),
    "each day": range(7),
}
# words for parts of the day: [start hour, end hour)
TIME_WORDS = {
    "all day": (0, 24), "anytime": (0, 24), "any time": (0, 24), "whole day": (0, 24),
    "morning": (6, 12), "afternoon": (12, 17), "evening": (17, 22), "tonight": (18, 24), "night": (20, 24),
}
MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}

_DAY = "|".join(sorted(DAY_NAMES, key=len, reverse=True))
_TIME = r"(?:\d{1,2}(?::\d{2})?\s*(?:am|pm|a|p)?|noon|midnight)"
TOKEN = re.compile(
    rf"""
    (?P<dayrange>\b(?:{_DAY})\s*(?:-|to|thru|through)\s*(?:{_DAY})\b)
  | (?P<group>\b(?:{"|".join(re.escape(g) for g in sorted(DAY_GROUPS, key=len, reverse=True))})\b)
  | (?P<day>\b(?:{_DAY})\b)
  | (?P<relday>\b(?:today|tomorrow)\b)
  | (?P<mdate>\b(?:{"|".join(MONTHS)})[a-z]*\.?\s+\d{{1,2}}\b|\b\d{{1,2}}/\d{{1,2}}\b)
  | (?P<range>(?<![\d/]){_TIME}\s*(?:-|to|until|till)\s*{_TIME}(?![\d/]))
  | (?P<after>\b(?:after|from)\s+{_TIME})
  | (?P<before>\b(?:before|until|till)\s+{_TIME})
  | (?P<word>\b(?:{"|".join(re.escape(w) for w in sorted(TIME_WORDS, key=len, reverse=True))})\b)
  | (?P<single>\b(?:at\s+)?{_TIME}(?![\d/]))
    """,
    re.VERBOSE,
)
_CLOCK = re.compile(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm|a|p)?|(noon)|(midnight)")


@dataclass
class Parsed:
    slots: set[tuple[date, int]] = field(default_factory=set)  # (local date, hour 0-23)
    unread: list[str] = field(default_factory=list)  # parts that weren't understood
    notes: list[str] = field(default_factory=list)  # assumptions the user should check


def poll_dates(start: date) -> list[date]:
    return [start + timedelta(days=i) for i in range(POLL_DAYS)]


def window_dates(start_hour: int, end_hour: int, now_hour: int, tz: ZoneInfo) -> list[date]:
    """The local dates in `tz` from today (or the poll's start, if later) to the poll's end. Up to 8
    dates, since the first and last days can be partial. Once the poll is over, its whole span."""
    first = start_hour if now_hour >= end_hour else min(max(start_hour, now_hour), end_hour - 1)
    first_day = datetime.fromtimestamp(first * 3600, tz).date()
    last_day = datetime.fromtimestamp((end_hour - 1) * 3600, tz).date()
    return [first_day + timedelta(days=i) for i in range((last_day - first_day).days + 1)]


def _clock(text: str) -> tuple[int, int, str | None]:
    """'5', '5:30pm', 'noon' -> (hour, minute, 'am'/'pm'/None)."""
    m = _CLOCK.search(text.strip())
    if m.group(4):
        return 12, 0, "pm"
    if m.group(5):
        return 0, 0, "am"
    suffix = (m.group(3) or "")[:1]
    return int(m.group(1)), int(m.group(2) or 0), {"a": "am", "p": "pm"}.get(suffix)


def _to_24(hour: int, suffix: str | None) -> int:
    if suffix == "am":
        return 0 if hour == 12 else hour
    if suffix == "pm":
        return 12 if hour == 12 else hour + 12
    return hour


def _time_range(first: str, second: str, notes: list[str]) -> tuple[int, int]:
    """'5', '7pm' -> (17, 19). End hours past midnight are 24+ (they spill into the next day)."""
    (h1, m1, s1), (h2, m2, s2) = _clock(first), _clock(second)
    if s1 is None and s2 is None:
        if h1 > 12 or h2 > 12 or ":" in first + second and (h1 == 0 or h2 == 0):
            pass  # 24-hour times, e.g. 17-19 or 17:00-19:00
        else:
            s1 = s2 = "pm"
            notes.append(f"Read “{first.strip()}-{second.strip()}” as PM; add am/pm if that's wrong.")
    elif s1 is None:  # "5-7pm" -> 5pm; "11-1pm" -> 11am; "10-2am" -> 10pm
        s1 = s2
        if h1 != 12 and h1 > h2 % 12:
            s1 = "am" if s2 == "pm" else "pm"
    elif s2 is None:
        s2 = s1
    start, end = _to_24(h1, s1), _to_24(h2, s2) + (1 if m2 else 0)
    if end <= start:
        end += 24  # overnight, e.g. 10pm-2am
    return start, end


def _single_hour(text: str, notes: list[str]) -> int:
    hour, _, suffix = _clock(text)
    if suffix is None and 1 <= hour <= 11:
        suffix = "pm"
        notes.append(f"Read “{text.strip()}” as {hour} PM; add am/pm if that's wrong.")
    return _to_24(hour, suffix)


def _dates_for(match: re.Match, dates: list[date], today: date) -> list[list[date]]:
    """The days a token names, each as its candidate dates in order. A weekday can be on two of the
    dates (today, and the same day next week); which one is meant is decided once its hours are known."""
    kind, text = match.lastgroup, match.group().strip()
    by_weekday: dict[int, list[date]] = {}
    for d in dates:
        by_weekday.setdefault(d.weekday(), []).append(d)

    def weekdays(numbers) -> list[list[date]]:
        return [by_weekday[i] for i in numbers if i in by_weekday]

    if kind == "day":
        return weekdays([DAY_NAMES[text]])
    if kind == "group":
        return weekdays(DAY_GROUPS[text])
    if kind == "dayrange":
        first, last = re.split(r"\s*(?:-|to|thru|through)\s*", text)
        a, b = DAY_NAMES[first], DAY_NAMES[last]
        return weekdays([(a + i) % 7 for i in range((b - a) % 7 + 1)])
    if kind == "relday":
        wanted = today if text == "today" else today + timedelta(days=1)
        return [[wanted]] if wanted in dates else []
    # a date: "oct 9", "10/9"
    if "/" in text:
        month, day = (int(x) for x in text.split("/"))
    else:
        name, day = re.match(r"([a-z]+)\.?\s+(\d+)", text).groups()
        month, day = MONTHS[name[:3]], int(day)
    return [[d] for d in dates if (d.month, d.day) == (month, day)]


def _hours_for(match: re.Match, notes: list[str]) -> list[tuple[int, int]]:
    kind, text = match.lastgroup, match.group()
    if kind == "range":
        first, second = re.split(r"\s*(?:-|to|until|till)\s*", text, maxsplit=1)
        return [_time_range(first, second, notes)]
    if kind == "after":
        start = _single_hour(re.sub(r"^(after|from)\s+", "", text), notes)
        return [(start, 24)]
    if kind == "before":
        end = _single_hour(re.sub(r"^(before|until|till)\s+", "", text), notes)
        return [(0, end)]
    if kind == "word":
        return [TIME_WORDS[text]]
    start = _single_hour(re.sub(r"^at\s+", "", text), notes)  # a single time: that hour
    return [(start, start + 1)]


def parse(
    text: str, dates: list[date], today: date, is_open: Callable[[date, int], bool] | None = None
) -> Parsed:
    """Read availability like "Fri 5-7pm, Sat after 2pm, weekdays 8-11pm" for the given dates.

    Each part (split by commas, semicolons or new lines) can name days and times. Days with no time
    mean the whole day; times with no day use the days from the part before ("Fri 1-3pm, 6-9pm").
    Within a part, "Fri 5-7pm and Sat 1-4pm" pairs each day with its own times.
    `is_open(date, hour)` says whether an hour can be chosen (not past, not after the poll ends).
    When a weekday is on two dates, the first one with open hours is used; closed hours are left out
    (with a note).
    """
    is_open = is_open or (lambda d, h: True)
    result = Parsed()
    normalized = text.lower().replace("–", "-").replace("—", "-")
    last_days: list[list[date]] = []
    left_out = 0
    for part in re.split(r"[,;\n]+", normalized):
        if not part.strip():
            continue
        segments: list[tuple[list[list[date]], list[tuple[int, int]]]] = []
        days: list[list[date]] = []
        hours: list[tuple[int, int]] = []
        found = False
        for match in TOKEN.finditer(part):
            found = True
            if match.lastgroup in ("day", "group", "dayrange", "relday", "mdate"):
                if days and hours:  # a new day after a finished day+time: start a new pairing
                    segments.append((days, hours))
                    days, hours = [], []
                matched = _dates_for(match, dates, today)
                if not matched:
                    result.notes.append(f"“{match.group().strip()}” isn't in this poll's dates.")
                days += matched
            else:
                hours += _hours_for(match, result.notes)
        if not found:
            result.unread.append(part.strip())
            continue
        segments.append((days, hours))
        for seg_days, seg_hours in segments:
            if not seg_days:
                if not last_days:
                    result.unread.append(part.strip())  # a time, but no day to put it on
                    continue
                seg_days = last_days
            last_days = seg_days
            ranges = seg_hours or [(0, 24)]

            def hours_on(d: date) -> list[tuple[date, int]]:
                return [(d + timedelta(days=h // 24), h % 24) for start, end in ranges for h in range(start, end)]

            for candidates in seg_days:
                d = next((c for c in candidates if any(is_open(*s) for s in hours_on(c))), candidates[0])
                for day, hour in hours_on(d):
                    if day not in dates:
                        continue
                    if is_open(day, hour):
                        result.slots.add((day, hour))
                    else:
                        left_out += 1
    if left_out:
        result.notes.append(
            f"{left_out} hour(s) you typed have already passed or are after the poll ends, so they were left out."
        )
    return result


# --------------------------------------------------------------------------- time conversion


def to_epoch_hours(slots: set[tuple[date, int]], tz: ZoneInfo) -> set[int]:
    return {int(datetime(d.year, d.month, d.day, h, tzinfo=tz).timestamp()) // 3600 for d, h in slots}


def local_hour(d: date, hour: int, tz: ZoneInfo) -> int:
    """The epoch hour of `hour` o'clock on `d` in `tz`."""
    return int(datetime(d.year, d.month, d.day, hour, tzinfo=tz).timestamp()) // 3600


# --------------------------------------------------------------------------- drawing


def day_label(d: date) -> str:
    return f"{d:%a} {d.month}/{d.day}"


def squares(hours: set[int], d: date, tz: ZoneInfo, now_hour: int | None = None) -> str:
    """24 squares for `d` in `tz`, one per hour from midnight, in groups of 6 (12-6 AM, 6 AM-12,
    12-6 PM, 6 PM-12). Hours before `now_hour` are greyed out (⬜), free or not: Best time
    only looks ahead, so a past overlap shouldn't look like an option."""
    cells = []
    for h in range(24):
        epoch = local_hour(d, h, tz)
        cells.append(PAST if now_hour is not None and epoch < now_hour else FREE if epoch in hours else BUSY)
    return " ".join("".join(cells[i : i + 6]) for i in range(0, 24, 6))


def hour_text(hour: int) -> str:
    hour %= 24
    return f"{hour % 12 or 12} {'AM' if hour < 12 else 'PM'}"


def day_ranges(hours: set[int], d: date, tz: ZoneInfo, now_hour: int | None = None) -> str:
    """e.g. '5 PM-7 PM, 9 PM-12 AM' for the free hours on `d` in `tz` (only those from `now_hour` on,
    if given)."""
    free = [
        h for h in range(24)
        if local_hour(d, h, tz) in hours and (now_hour is None or local_hour(d, h, tz) >= now_hour)
    ]
    if len(free) == 24:
        return "all day"
    ranges, start = [], None
    for h in range(25):
        if h < 24 and h in free:
            start = h if start is None else start
        elif start is not None:
            ranges.append(f"{hour_text(start)}-{hour_text(h)}")
            start = None
    return ", ".join(ranges)


# Labels for the four 6-hour groups of squares. Emoji are wider than text and Discord has no
# tables, so each label is padded with em spaces to sit roughly over its group of 6 squares.
GROUP_LABELS = ("12-6AM", "6-12PM", "12-6PM", "6-12AM")
LABEL_PAD = "\u2003" * 5


def header() -> str:
    """The row of group labels. Rows of squares start at the left edge (their name or date is on the
    line above), so all 24 squares fit on one line in an embed and line up under these labels."""
    return LABEL_PAD.join(GROUP_LABELS)


def grid(hours: set[int], dates: list[date], tz: ZoneInfo, now_hour: int | None = None) -> str:
    """A label row, then for each date: its name and the free hours in words, and its 24 squares."""
    lines = [header()]
    for d in dates:
        ranges = day_ranges(hours, d, tz)
        lines.append(f"**{day_label(d)}**" + (f" · {ranges}" if ranges else ""))
        lines.append(squares(hours, d, tz, now_hour))
    return "\n".join(lines)


def compact(hours: set[int], dates: list[date], tz: ZoneInfo, now_hour: int | None = None) -> str:
    """The phone-friendly version of grid(): each date with free hours still ahead, in words."""
    lines = [
        f"**{day_label(d)}** · {ranges}" for d in dates if (ranges := day_ranges(hours, d, tz, now_hour))
    ]
    return "\n".join(lines) or "*No free hours still ahead.*"


COMPACT_LEGEND = "Free hours still ahead"
LEGEND = "🟩 free · ⬛ not · ⬜ already passed · each square is 1 hour, in groups of 6 hours from 12 AM"


def first_block(hours: set[int]) -> tuple[int, int] | None:
    """The earliest run of back-to-back hours, as (start, end) epoch hours; None if there are none."""
    if not hours:
        return None
    start = end = min(hours)
    while end + 1 in hours:
        end += 1
    return start, end + 1


def start_label(start: int, tz: ZoneInfo) -> str:
    """e.g. 'Tue 10/13 1 PM': an epoch hour in `tz`."""
    local = datetime.fromtimestamp(start * 3600, tz)
    return f"{day_label(local.date())} {hour_text(local.hour)}"


def window_label(start: int, end: int, tz: ZoneInfo) -> str:
    """e.g. 'Fri 10/9 5 PM-7 PM' in `tz` -- for dropdowns, which can't show Discord timestamps."""
    local = datetime.fromtimestamp(start * 3600, tz)
    return f"{day_label(local.date())} {hour_text(local.hour)}-{hour_text(local.hour + (end - start))}"


# --------------------------------------------------------------------------- best times


@dataclass(frozen=True)
class Window:
    start: int  # epoch hour
    end: int  # epoch hour (exclusive)
    people: frozenset[int]  # user ids free for the whole window

    @property
    def hours(self) -> int:
        return self.end - self.start


def best_windows(availability: dict[int, set[int]], from_hour: int, limit: int = 5) -> list[Window]:
    """Stretches of hours where the same people are free, most people first, then longest, then
    soonest. Only hours from `from_hour` on count (no recommending the past)."""
    free_at: dict[int, set[int]] = {}
    for user, hours in availability.items():
        for h in hours:
            if h >= from_hour:
                free_at.setdefault(h, set()).add(user)
    windows: list[Window] = []
    for h in sorted(free_at):
        people = frozenset(free_at[h])
        last = windows[-1] if windows else None
        if last and last.end == h and last.people == people:
            windows[-1] = Window(last.start, h + 1, people)
        else:
            windows.append(Window(h, h + 1, people))
    windows.sort(key=lambda w: (-len(w.people), -w.hours, w.start))
    return windows[:limit]
