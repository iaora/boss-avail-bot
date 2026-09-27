"""SQLite storage.

The database file is created automatically on first run and the schema is upgraded in
place using PRAGMA user_version, so a fresh machine (or EC2 instance) needs no manual
database setup. To add a schema change, append a new entry to MIGRATIONS -- never edit
an existing one.

The data set is small (tens of players, a few hundred characters), so plain synchronous
sqlite3 calls are fast enough to run directly inside the bot's event loop.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from . import timeutil

LEVELS = ("Preferred", "Available", "Not Available")
# How a player wants each character used when hosts slot squads
CHARACTER_STATUSES = ("static", "sub", "inactive")
PLAYER_STATUSES = ("active", "inactive")  # "sub" existed before migration 5; see MIGRATIONS
BUFFS = ("DPS", "HASTE", "SE", "HSH", "SI/TL", "SI", "THORNS", "HB")

MIGRATIONS: list[str] = [
    # 1: initial schema
    """
    CREATE TABLE settings (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );

    CREATE TABLE players (
        id             INTEGER PRIMARY KEY,
        discord_id     INTEGER UNIQUE,
        discord_handle TEXT COLLATE NOCASE,
        name           TEXT NOT NULL,
        status         TEXT NOT NULL DEFAULT 'active'
                       CHECK (status IN ('active', 'inactive', 'sub')),
        created_at     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE INDEX idx_players_handle ON players (discord_handle);

    CREATE TABLE characters (
        id          INTEGER PRIMARY KEY,
        player_id   INTEGER NOT NULL REFERENCES players (id) ON DELETE CASCADE,
        ign         TEXT NOT NULL UNIQUE COLLATE NOCASE,
        job         TEXT,
        buff        TEXT,
        dmg         REAL,
        run_time    REAL,
        squad       INTEGER,
        slot_status TEXT,
        perm        INTEGER NOT NULL DEFAULT 0
    );

    CREATE TABLE squads (
        number  INTEGER PRIMARY KEY,
        weekday INTEGER NOT NULL CHECK (weekday BETWEEN 0 AND 6),
        time    TEXT NOT NULL
    );

    CREATE TABLE squad_week_overrides (
        week_start TEXT NOT NULL,
        squad      INTEGER NOT NULL REFERENCES squads (number) ON DELETE CASCADE,
        starts_at  INTEGER NOT NULL,
        PRIMARY KEY (week_start, squad)
    );

    CREATE TABLE default_availability (
        player_id INTEGER NOT NULL REFERENCES players (id) ON DELETE CASCADE,
        squad     INTEGER NOT NULL,
        level     TEXT NOT NULL CHECK (level IN ('Preferred', 'Available', 'Not Available')),
        PRIMARY KEY (player_id, squad)
    );

    CREATE TABLE character_availability (
        character_id INTEGER NOT NULL REFERENCES characters (id) ON DELETE CASCADE,
        squad        INTEGER NOT NULL,
        level        TEXT NOT NULL CHECK (level IN ('Preferred', 'Available', 'Not Available')),
        PRIMARY KEY (character_id, squad)
    );

    CREATE TABLE weekly_availability (
        player_id  INTEGER NOT NULL REFERENCES players (id) ON DELETE CASCADE,
        week_start TEXT NOT NULL,
        squad      INTEGER NOT NULL,
        level      TEXT NOT NULL CHECK (level IN ('Preferred', 'Available', 'Not Available')),
        PRIMARY KEY (player_id, week_start, squad)
    );

    CREATE TABLE weekly_confirmations (
        player_id    INTEGER NOT NULL REFERENCES players (id) ON DELETE CASCADE,
        week_start   TEXT NOT NULL,
        kind         TEXT NOT NULL CHECK (kind IN ('no_change', 'updated')),
        confirmed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (player_id, week_start)
    );
    """,
    # 2: damage logs uploaded to #queen-logs
    """
    CREATE TABLE damage_logs (
        id          INTEGER PRIMARY KEY,
        message_id  INTEGER,
        filename    TEXT,
        started_at  INTEGER NOT NULL,
        finished_at INTEGER NOT NULL,
        uploaded_by INTEGER,
        uploaded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE (started_at, finished_at)
    );

    CREATE TABLE damage_runs (
        log_id       INTEGER NOT NULL REFERENCES damage_logs (id) ON DELETE CASCADE,
        character_id INTEGER NOT NULL REFERENCES characters (id) ON DELETE CASCADE,
        damage       INTEGER NOT NULL,
        normalized   REAL NOT NULL,
        PRIMARY KEY (log_id, character_id)
    );
    CREATE INDEX idx_damage_runs_character ON damage_runs (character_id);
    CREATE INDEX idx_damage_logs_message ON damage_logs (message_id);

    -- dmg from the roster sheet or entered by hand; used when a character has no logged runs
    ALTER TABLE characters ADD COLUMN base_dmg REAL;
    UPDATE characters SET base_dmg = dmg;
    """,
    # 3: character status, set by the player: static (prioritize), sub (if needed), inactive (don't slot)
    """
    ALTER TABLE characters ADD COLUMN status TEXT NOT NULL DEFAULT 'static'
        CHECK (status IN ('static', 'sub', 'inactive'));
    UPDATE characters SET status = CASE WHEN perm = 1 THEN 'static' ELSE 'sub' END;
    """,
    # 4: log of character status changes, shown to hosts in /host status
    """
    CREATE TABLE character_status_log (
        id           INTEGER PRIMARY KEY,
        character_id INTEGER NOT NULL REFERENCES characters (id) ON DELETE CASCADE,
        old_status   TEXT NOT NULL,
        new_status   TEXT NOT NULL,
        changed_at   INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
        changed_by   INTEGER  -- Discord user id of whoever made the change
    );
    CREATE INDEX idx_character_status_log_time ON character_status_log (changed_at);
    """,
    # 5: players are only active or inactive now (the "sub" player status was dropped; characters
    # still have their own static/sub/inactive status). Former substitutes become active.
    """
    UPDATE players SET status = 'active' WHERE status = 'sub';
    """,
]


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    for i, script in enumerate(MIGRATIONS[version:], start=version + 1):
        with conn:
            conn.executescript(script)
            conn.execute(f"PRAGMA user_version = {i}")


# --------------------------------------------------------------------------- settings

def get_setting(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    with conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


# --------------------------------------------------------------------------- players

@dataclass
class Player:
    id: int
    discord_id: int | None
    discord_handle: str | None
    name: str
    status: str

    @property
    def display(self) -> str:
        if self.discord_handle:
            return f"{self.name} ({self.discord_handle})"
        return self.name

    @property
    def mention(self) -> str:
        return f"<@{self.discord_id}>" if self.discord_id else self.display


def _player(row: sqlite3.Row | None) -> Player | None:
    if row is None:
        return None
    return Player(row["id"], row["discord_id"], row["discord_handle"], row["name"], row["status"])


def get_player(conn: sqlite3.Connection, player_id: int) -> Player | None:
    return _player(conn.execute("SELECT * FROM players WHERE id = ?", (player_id,)).fetchone())


def get_player_by_discord_id(conn: sqlite3.Connection, discord_id: int) -> Player | None:
    return _player(conn.execute("SELECT * FROM players WHERE discord_id = ?", (discord_id,)).fetchone())


def get_player_by_handle(conn: sqlite3.Connection, handle: str) -> Player | None:
    handle = handle if handle.startswith("@") else f"@{handle}"
    return _player(conn.execute("SELECT * FROM players WHERE discord_handle = ?", (handle,)).fetchone())


def list_players(conn: sqlite3.Connection, statuses: tuple[str, ...] = PLAYER_STATUSES) -> list[Player]:
    marks = ",".join("?" * len(statuses))
    rows = conn.execute(
        f"SELECT * FROM players WHERE status IN ({marks}) ORDER BY name COLLATE NOCASE", statuses
    ).fetchall()
    return [_player(r) for r in rows]


def search_players(conn: sqlite3.Connection, text: str, limit: int = 25) -> list[Player]:
    """Match players by name, Discord handle, or any of their character IGNs."""
    like = f"%{text.strip().lstrip('@')}%"
    rows = conn.execute(
        """
        SELECT DISTINCT p.* FROM players p
        LEFT JOIN characters c ON c.player_id = p.id
        WHERE p.name LIKE ? OR p.discord_handle LIKE ? OR c.ign LIKE ?
        ORDER BY p.name COLLATE NOCASE
        LIMIT ?
        """,
        (like, like, like, limit),
    ).fetchall()
    return [_player(r) for r in rows]


def create_player(
    conn: sqlite3.Connection,
    name: str,
    discord_handle: str | None = None,
    discord_id: int | None = None,
    status: str = "active",
) -> Player:
    with conn:
        cur = conn.execute(
            "INSERT INTO players (name, discord_handle, discord_id, status) VALUES (?, ?, ?, ?)",
            (name, discord_handle, discord_id, status),
        )
    return get_player(conn, cur.lastrowid)


def link_discord(conn: sqlite3.Connection, player_id: int, discord_id: int, handle: str | None) -> None:
    with conn:
        conn.execute("UPDATE players SET discord_id = NULL WHERE discord_id = ? AND id != ?", (discord_id, player_id))
        conn.execute(
            "UPDATE players SET discord_id = ?, discord_handle = COALESCE(?, discord_handle) WHERE id = ?",
            (discord_id, handle, player_id),
        )


def set_player_status(conn: sqlite3.Connection, player_id: int, status: str) -> None:
    if status not in PLAYER_STATUSES:
        raise ValueError(status)
    with conn:
        conn.execute("UPDATE players SET status = ? WHERE id = ?", (status, player_id))


def set_player_name(conn: sqlite3.Connection, player_id: int, name: str) -> None:
    with conn:
        conn.execute("UPDATE players SET name = ? WHERE id = ?", (name, player_id))


# --------------------------------------------------------------------------- characters

def list_characters(conn: sqlite3.Connection, player_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM characters WHERE player_id = ? ORDER BY dmg DESC, ign COLLATE NOCASE", (player_id,)
    ).fetchall()


def get_character(conn: sqlite3.Connection, ign: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM characters WHERE ign = ?", (ign.strip(),)).fetchone()


def add_character(
    conn: sqlite3.Connection,
    player_id: int,
    ign: str,
    job: str,
    buff: str,
    dmg: float | None,
    status: str = "static",
) -> None:
    with conn:
        conn.execute(
            "INSERT INTO characters (player_id, ign, job, buff, dmg, base_dmg, slot_status, status) "
            "VALUES (?, ?, ?, ?, ?, ?, 'Available', ?)",
            (player_id, ign.strip(), job.strip().upper(), buff, dmg, dmg, status),
        )


def set_character_statuses(
    conn: sqlite3.Connection, player_id: int, statuses: dict[int, str], changed_by: int | None = None
) -> None:
    """Set static/sub/inactive for several of a player's characters ({character id: status}).

    Every actual change is logged (see status_changes_since)."""
    if any(s not in CHARACTER_STATUSES for s in statuses.values()):
        raise ValueError(statuses)
    with conn:
        for cid, status in statuses.items():
            row = conn.execute(
                "SELECT status FROM characters WHERE id = ? AND player_id = ?", (cid, player_id)
            ).fetchone()
            if row is not None and row["status"] != status:
                _log_status_change(conn, cid, row["status"], status, changed_by)
                conn.execute("UPDATE characters SET status = ? WHERE id = ?", (status, cid))


def _log_status_change(conn: sqlite3.Connection, character_id: int, old: str, new: str, changed_by: int | None) -> None:
    conn.execute(
        "INSERT INTO character_status_log (character_id, old_status, new_status, changed_by) VALUES (?, ?, ?, ?)",
        (character_id, old, new, changed_by),
    )


def status_changes_since(conn: sqlite3.Connection, since: int) -> list[dict]:
    """Characters whose status changed since `since` (unix seconds): one entry each, with the
    status before the first change and after the last. Characters that ended where they started
    are left out. Sorted by player name, then character name."""
    rows = conn.execute(
        """
        SELECT l.character_id, l.old_status, l.new_status, l.changed_at, l.changed_by,
               c.ign, c.job, p.name AS player_name, p.discord_id AS owner_id
        FROM character_status_log l
        JOIN characters c ON c.id = l.character_id
        JOIN players p ON p.id = c.player_id
        WHERE l.changed_at >= ?
        ORDER BY l.changed_at, l.id
        """,
        (since,),
    ).fetchall()
    changes: dict[int, dict] = {}
    for r in rows:
        entry = changes.setdefault(
            r["character_id"],
            {"ign": r["ign"], "job": r["job"], "player": r["player_name"], "before": r["old_status"], "by_host": False},
        )
        entry["after"] = r["new_status"]
        entry["changed_at"] = r["changed_at"]
        entry["by_host"] |= r["changed_by"] is not None and r["changed_by"] != r["owner_id"]
    result = [c for c in changes.values() if c["before"] != c["after"]]
    return sorted(result, key=lambda c: (c["player"].lower(), c["ign"].lower()))


def update_character(conn: sqlite3.Connection, character_id: int, changed_by: int | None = None, **fields) -> None:
    allowed = {"job", "buff", "dmg", "status", "player_id"}
    fields = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if not fields:
        return
    if "status" in fields:
        row = conn.execute("SELECT status FROM characters WHERE id = ?", (character_id,)).fetchone()
        if row is not None and row["status"] != fields["status"]:
            with conn:
                _log_status_change(conn, character_id, row["status"], fields["status"], changed_by)
    sets = ", ".join(f"{k} = ?" for k in fields if k != "dmg")
    params = [v for k, v in fields.items() if k != "dmg"]
    if "dmg" in fields:
        # A hand-entered dmg is the fallback; damage from uploaded logs takes priority.
        sets = ", ".join(filter(None, [
            sets,
            "base_dmg = ?",
            "dmg = CASE WHEN EXISTS (SELECT 1 FROM damage_runs WHERE character_id = characters.id) THEN dmg ELSE ? END",
        ]))
        params += [fields["dmg"], fields["dmg"]]
    with conn:
        conn.execute(f"UPDATE characters SET {sets} WHERE id = ?", (*params, character_id))


def delete_character(conn: sqlite3.Connection, character_id: int) -> None:
    with conn:
        conn.execute("DELETE FROM characters WHERE id = ?", (character_id,))


def known_jobs(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute("SELECT DISTINCT UPPER(job) AS job FROM characters WHERE job != '' ORDER BY job").fetchall()
    return [r["job"] for r in rows]


# --------------------------------------------------------------------------- squads

@dataclass
class SquadTime:
    number: int
    starts_at: int  # unix seconds
    overridden: bool

    @property
    def label(self) -> str:
        return f"Squad {self.number}"


def list_squad_template(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM squads ORDER BY weekday, time, number").fetchall()


def squad_numbers(conn: sqlite3.Connection) -> list[int]:
    return [r["number"] for r in conn.execute("SELECT number FROM squads ORDER BY number")]


def set_squad_template(conn: sqlite3.Connection, number: int, weekday: int, hhmm: str) -> None:
    timeutil.parse_hhmm(hhmm)  # validate
    with conn:
        conn.execute(
            "INSERT INTO squads (number, weekday, time) VALUES (?, ?, ?) "
            "ON CONFLICT (number) DO UPDATE SET weekday = excluded.weekday, time = excluded.time",
            (number, weekday, hhmm),
        )


def delete_squad(conn: sqlite3.Connection, number: int) -> None:
    with conn:
        conn.execute("DELETE FROM squads WHERE number = ?", (number,))


def squads_for_week(conn: sqlite3.Connection, week_start: date, tz: ZoneInfo) -> list[SquadTime]:
    """Squad start times for a given week: the template, with any per-week overrides applied."""
    overrides = {
        r["squad"]: r["starts_at"]
        for r in conn.execute(
            "SELECT squad, starts_at FROM squad_week_overrides WHERE week_start = ?", (week_start.isoformat(),)
        )
    }
    result = []
    for row in list_squad_template(conn):
        if row["number"] in overrides:
            result.append(SquadTime(row["number"], overrides[row["number"]], True))
        else:
            dt = timeutil.at_weekday(week_start, row["weekday"], row["time"], tz)
            result.append(SquadTime(row["number"], int(dt.timestamp()), False))
    result.sort(key=lambda s: (s.starts_at, s.number))
    return result


def set_squad_override(conn: sqlite3.Connection, week_start: date, number: int, starts_at: datetime) -> None:
    with conn:
        conn.execute(
            "INSERT INTO squad_week_overrides (week_start, squad, starts_at) VALUES (?, ?, ?) "
            "ON CONFLICT (week_start, squad) DO UPDATE SET starts_at = excluded.starts_at",
            (week_start.isoformat(), number, int(starts_at.timestamp())),
        )


def clear_squad_override(conn: sqlite3.Connection, week_start: date, number: int | None = None) -> None:
    with conn:
        if number is None:
            conn.execute("DELETE FROM squad_week_overrides WHERE week_start = ?", (week_start.isoformat(),))
        else:
            conn.execute(
                "DELETE FROM squad_week_overrides WHERE week_start = ? AND squad = ?", (week_start.isoformat(), number)
            )


# --------------------------------------------------------------------------- availability

Availability = dict[int, str]  # squad number -> level


def get_default_availability(conn: sqlite3.Connection, player_id: int) -> Availability:
    rows = conn.execute("SELECT squad, level FROM default_availability WHERE player_id = ?", (player_id,))
    return {r["squad"]: r["level"] for r in rows}


def set_default_availability(conn: sqlite3.Connection, player_id: int, availability: Availability) -> None:
    with conn:
        conn.execute("DELETE FROM default_availability WHERE player_id = ?", (player_id,))
        conn.executemany(
            "INSERT INTO default_availability (player_id, squad, level) VALUES (?, ?, ?)",
            [(player_id, s, lvl) for s, lvl in availability.items()],
        )


def get_character_overrides(conn: sqlite3.Connection, character_id: int) -> Availability:
    rows = conn.execute("SELECT squad, level FROM character_availability WHERE character_id = ?", (character_id,))
    return {r["squad"]: r["level"] for r in rows}


def set_character_overrides(conn: sqlite3.Connection, character_id: int, overrides: Availability) -> None:
    with conn:
        conn.execute("DELETE FROM character_availability WHERE character_id = ?", (character_id,))
        conn.executemany(
            "INSERT INTO character_availability (character_id, squad, level) VALUES (?, ?, ?)",
            [(character_id, s, lvl) for s, lvl in overrides.items()],
        )


def get_weekly_availability(conn: sqlite3.Connection, player_id: int, week_start: date) -> Availability:
    rows = conn.execute(
        "SELECT squad, level FROM weekly_availability WHERE player_id = ? AND week_start = ?",
        (player_id, week_start.isoformat()),
    )
    return {r["squad"]: r["level"] for r in rows}


def weeks_with_changes(conn: sqlite3.Connection, player_id: int, from_week: date) -> list[date]:
    """Weeks (starting on or after `from_week`) where the player submitted a schedule change."""
    rows = conn.execute(
        "SELECT DISTINCT week_start FROM weekly_availability WHERE player_id = ? AND week_start >= ? "
        "ORDER BY week_start",
        (player_id, from_week.isoformat()),
    )
    return [date.fromisoformat(r["week_start"]) for r in rows]


def set_weekly_availability(conn: sqlite3.Connection, player_id: int, week_start: date, availability: Availability) -> None:
    wk = week_start.isoformat()
    with conn:
        conn.execute("DELETE FROM weekly_availability WHERE player_id = ? AND week_start = ?", (player_id, wk))
        conn.executemany(
            "INSERT INTO weekly_availability (player_id, week_start, squad, level) VALUES (?, ?, ?, ?)",
            [(player_id, wk, s, lvl) for s, lvl in availability.items()],
        )
        _confirm(conn, player_id, week_start, "updated")


def confirm_no_change(conn: sqlite3.Connection, player_id: int, week_start: date) -> None:
    """Player keeps their default for the week; any earlier weekly submission is discarded."""
    with conn:
        conn.execute(
            "DELETE FROM weekly_availability WHERE player_id = ? AND week_start = ?", (player_id, week_start.isoformat())
        )
        _confirm(conn, player_id, week_start, "no_change")


def _confirm(conn: sqlite3.Connection, player_id: int, week_start: date, kind: str) -> None:
    conn.execute(
        "INSERT INTO weekly_confirmations (player_id, week_start, kind) VALUES (?, ?, ?) "
        "ON CONFLICT (player_id, week_start) DO UPDATE SET kind = excluded.kind, confirmed_at = CURRENT_TIMESTAMP",
        (player_id, week_start.isoformat(), kind),
    )


def get_confirmations(conn: sqlite3.Connection, week_start: date) -> dict[int, str]:
    rows = conn.execute(
        "SELECT player_id, kind FROM weekly_confirmations WHERE week_start = ?", (week_start.isoformat(),)
    )
    return {r["player_id"]: r["kind"] for r in rows}


def get_confirmation(conn: sqlite3.Connection, player_id: int, week_start: date) -> str | None:
    row = conn.execute(
        "SELECT kind FROM weekly_confirmations WHERE player_id = ? AND week_start = ?",
        (player_id, week_start.isoformat()),
    ).fetchone()
    return row["kind"] if row else None


LEVEL_RANK = {level: i for i, level in enumerate(LEVELS)}  # lower = more available


def character_week_availability(
    conn: sqlite3.Connection, character: sqlite3.Row, week_start: date
) -> Availability:
    """A character's availability for a week.

    Without a weekly change it's the player's default with the character's overrides on top.
    With a weekly change, each squad uses the less available of the weekly level and the
    character's override, so a character-specific limit ("DRK weekends only") still applies.
    """
    overrides = get_character_overrides(conn, character["id"])
    weekly = get_weekly_availability(conn, character["player_id"], week_start)
    if not weekly:
        return {**get_default_availability(conn, character["player_id"]), **overrides}
    result = dict(weekly)
    for squad, level in overrides.items():
        if squad in result and LEVEL_RANK[level] > LEVEL_RANK[result[squad]]:
            result[squad] = level
    return result


def effective_week_availability(
    conn: sqlite3.Connection, player_id: int, week_start: date
) -> tuple[Availability, str]:
    """This week's availability for a player and where it came from ('weekly' or 'default')."""
    weekly = get_weekly_availability(conn, player_id, week_start)
    if weekly:
        return weekly, "weekly"
    return get_default_availability(conn, player_id), "default"
