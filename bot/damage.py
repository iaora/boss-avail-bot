"""Parse boss damage logs and keep each character's damage up to date.

A log is a .txt file whose bottom section looks like:

    [Start Time] 27-09-2026 01:50:49
    [Finish Time] 27-09-2026 02:15:09
    >>SomeChar: 4,027,079,300 | deaths: 2
    >>OtherChar: 3,644,272,981

Times are in the game server's timezone (LOG_TIMEZONE). Each character's damage for a run is
normalized to a 27.5-minute run, in billions: damage / 1e9 * 27.5 / run_minutes. The
character's damage is the average of their most recent N normalized runs.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

from . import db

NORMALIZE_MINUTES = 27.5
DEFAULT_AVERAGE_RUNS = 3

TIME_LINE = re.compile(r"^\[(Start|Finish) Time\]\s*(\d{1,2}-\d{1,2}-\d{4} \d{1,2}:\d{2}:\d{2})\s*$", re.MULTILINE)
DAMAGE_LINE = re.compile(r"^>>\s*([^:\n]+?)\s*:\s*([\d,]+)", re.MULTILINE)


@dataclass
class ParsedLog:
    started_at: datetime
    finished_at: datetime
    damage: dict[str, int]  # IGN as written in the log -> total damage

    @property
    def minutes(self) -> float:
        return (self.finished_at - self.started_at).total_seconds() / 60


@dataclass
class RecordResult:
    log: ParsedLog
    duplicate: bool = False
    updated: list[tuple[str, float, float]] = field(default_factory=list)  # (ign, this run, new average)
    unknown: list[str] = field(default_factory=list)


def parse_log(text: str, tz: ZoneInfo) -> ParsedLog:
    times = {kind.lower(): value for kind, value in TIME_LINE.findall(text)}
    if "start" not in times or "finish" not in times:
        raise ValueError("couldn't find the [Start Time] and [Finish Time] lines")
    started = datetime.strptime(times["start"], "%d-%m-%Y %H:%M:%S").replace(tzinfo=tz)
    finished = datetime.strptime(times["finish"], "%d-%m-%Y %H:%M:%S").replace(tzinfo=tz)
    if finished <= started:
        raise ValueError("the finish time is not after the start time")

    damage = {name: int(value.replace(",", "")) for name, value in DAMAGE_LINE.findall(text)}
    if not damage:
        raise ValueError("couldn't find any `>>Name: damage` lines")
    return ParsedLog(started, finished, damage)


def normalized(damage: int, minutes: float) -> float:
    return round(damage / 1e9 * NORMALIZE_MINUTES / minutes, 3)


def average_runs(conn: sqlite3.Connection, *, boss_id: int = db.CQ) -> int:
    return int(db.get_boss_setting(conn, "damage_average_runs", str(DEFAULT_AVERAGE_RUNS), boss_id=boss_id))


def record_log(
    conn: sqlite3.Connection,
    log: ParsedLog,
    *,
    message_id: int | None = None,
    filename: str | None = None,
    uploaded_by: int | None = None,
    boss_id: int = db.CQ,
) -> RecordResult:
    """Save a run for `boss_id` and update that boss's damage for every character in it.

    Uploading the same run again (same start and finish time) only adds characters that
    weren't saved the first time, e.g. names that have since been added to the roster.
    `duplicate` is set when a re-upload adds nothing new.
    """
    result = RecordResult(log)
    start, finish = int(log.started_at.timestamp()), int(log.finished_at.timestamp())
    existing = conn.execute(
        "SELECT id FROM damage_logs WHERE boss_id = ? AND started_at = ? AND finished_at = ?", (boss_id, start, finish)
    ).fetchone()

    with conn:
        if existing:
            log_id = existing["id"]
        else:
            log_id = conn.execute(
                "INSERT INTO damage_logs (boss_id, message_id, filename, started_at, finished_at, uploaded_by) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (boss_id, message_id, filename, start, finish, uploaded_by),
            ).lastrowid
        new_character_ids = []
        for name, total in log.damage.items():
            character = db.get_character(conn, name, boss_id=boss_id)
            if character is None:
                result.unknown.append(name)
                continue
            inserted = conn.execute(
                "INSERT OR IGNORE INTO damage_runs (log_id, character_id, damage, normalized) VALUES (?, ?, ?, ?)",
                (log_id, character["id"], total, normalized(total, log.minutes)),
            ).rowcount
            if inserted:
                new_character_ids.append((character["id"], character["ign"], normalized(total, log.minutes)))

    result.duplicate = existing is not None and not new_character_ids
    result.updated = [
        (ign, run, recompute_character_damage(conn, cid, boss_id=boss_id)) for cid, ign, run in new_character_ids
    ]
    return result


def recompute_character_damage(conn: sqlite3.Connection, character_id: int, *, boss_id: int = db.CQ) -> float | None:
    """Set the character's dmg for `boss_id` to the average of their latest N runs for that boss.

    With no runs left (e.g. their only log was deleted), dmg falls back to base_dmg, the value
    from the roster sheet or entered by a host.
    """
    rows = conn.execute(
        """
        SELECT r.normalized FROM damage_runs r JOIN damage_logs l ON l.id = r.log_id
        WHERE r.character_id = ? AND l.boss_id = ? ORDER BY l.started_at DESC LIMIT ?
        """,
        (character_id, boss_id, average_runs(conn, boss_id=boss_id)),
    ).fetchall()
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO character_boss (character_id, boss_id) VALUES (?, ?)", (character_id, boss_id)
        )
        if not rows:
            conn.execute(
                "UPDATE character_boss SET dmg = base_dmg WHERE character_id = ? AND boss_id = ?",
                (character_id, boss_id),
            )
            return None
        value = round(sum(r["normalized"] for r in rows) / len(rows), 3)
        conn.execute(
            "UPDATE character_boss SET dmg = ? WHERE character_id = ? AND boss_id = ?", (value, character_id, boss_id)
        )
    return value


def delete_logs_for_message(conn: sqlite3.Connection, message_id: int) -> list[str] | None:
    """Remove the log(s) uploaded in `message_id` and recompute affected characters.

    Returns the affected IGNs, or None if the message had no logs.
    """
    logs = [r["id"] for r in conn.execute("SELECT id FROM damage_logs WHERE message_id = ?", (message_id,))]
    if not logs:
        return None
    marks = ",".join("?" * len(logs))
    affected = conn.execute(
        f"SELECT DISTINCT c.id, c.ign, l.boss_id FROM damage_runs r "
        f"JOIN characters c ON c.id = r.character_id JOIN damage_logs l ON l.id = r.log_id "
        f"WHERE r.log_id IN ({marks})",
        logs,
    ).fetchall()
    with conn:
        conn.execute(f"DELETE FROM damage_logs WHERE id IN ({marks})", logs)
    for row in affected:
        recompute_character_damage(conn, row["id"], boss_id=row["boss_id"])
    return list(dict.fromkeys(row["ign"] for row in affected))


def recompute_all(conn: sqlite3.Connection, *, boss_id: int = db.CQ) -> None:
    rows = conn.execute(
        "SELECT DISTINCT r.character_id FROM damage_runs r JOIN damage_logs l ON l.id = r.log_id WHERE l.boss_id = ?",
        (boss_id,),
    ).fetchall()
    for row in rows:
        recompute_character_damage(conn, row["character_id"], boss_id=boss_id)
