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

# Bosses. Everything boss-specific (squads, availability, damage, character status, player
# status, some settings) is stored per boss. Crimson Queen is the only boss so far; functions
# default to it, so callers pass boss_id only when they deal with another boss.
CQ = 1

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
    # 6: boss-aware schema. Everything that was Crimson Queen data gets a boss_id (CQ = 1).
    # Players and characters keep only their identity; per-boss fields move to player_boss and
    # character_boss. Tables are rebuilt (create new_X, copy, drop X, rename), which is how SQLite
    # changes a primary key; migrate() runs this with foreign keys off and checks them afterwards.
    """
    CREATE TABLE bosses (
        id   INTEGER PRIMARY KEY,
        key  TEXT NOT NULL UNIQUE,
        name TEXT NOT NULL
    );
    INSERT INTO bosses (id, key, name) VALUES (1, 'cq', 'Crimson Queen');

    -- players: status moves to player_boss
    CREATE TABLE player_boss (
        player_id INTEGER NOT NULL REFERENCES players (id) ON DELETE CASCADE,
        boss_id   INTEGER NOT NULL REFERENCES bosses (id) ON DELETE CASCADE,
        status    TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'inactive')),
        PRIMARY KEY (player_id, boss_id)
    );
    INSERT INTO player_boss (player_id, boss_id, status)
        SELECT id, 1, CASE WHEN status = 'inactive' THEN 'inactive' ELSE 'active' END FROM players;
    CREATE TABLE new_players (
        id             INTEGER PRIMARY KEY,
        discord_id     INTEGER UNIQUE,
        discord_handle TEXT COLLATE NOCASE,
        name           TEXT NOT NULL,
        created_at     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    INSERT INTO new_players (id, discord_id, discord_handle, name, created_at)
        SELECT id, discord_id, discord_handle, name, created_at FROM players;
    DROP TABLE players;
    ALTER TABLE new_players RENAME TO players;
    CREATE INDEX idx_players_handle ON players (discord_handle);

    -- characters: damage, status and slotting move to character_boss
    CREATE TABLE character_boss (
        character_id INTEGER NOT NULL REFERENCES characters (id) ON DELETE CASCADE,
        boss_id      INTEGER NOT NULL REFERENCES bosses (id) ON DELETE CASCADE,
        status       TEXT NOT NULL DEFAULT 'static' CHECK (status IN ('static', 'sub', 'inactive')),
        dmg          REAL,
        base_dmg     REAL,  -- from the roster sheet or entered by a host; used when there are no logged runs
        run_time     REAL,
        squad        INTEGER,
        slot_status  TEXT,
        perm         INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (character_id, boss_id)
    );
    INSERT INTO character_boss (character_id, boss_id, status, dmg, base_dmg, run_time, squad, slot_status, perm)
        SELECT id, 1, status, dmg, base_dmg, run_time, squad, slot_status, perm FROM characters;
    CREATE TABLE new_characters (
        id        INTEGER PRIMARY KEY,
        player_id INTEGER NOT NULL REFERENCES players (id) ON DELETE CASCADE,
        ign       TEXT NOT NULL UNIQUE COLLATE NOCASE,
        job       TEXT,
        buff      TEXT
    );
    INSERT INTO new_characters (id, player_id, ign, job, buff) SELECT id, player_id, ign, job, buff FROM characters;
    DROP TABLE characters;
    ALTER TABLE new_characters RENAME TO characters;

    -- squads: numbered per boss
    CREATE TABLE new_squads (
        boss_id INTEGER NOT NULL REFERENCES bosses (id) ON DELETE CASCADE,
        number  INTEGER NOT NULL,
        weekday INTEGER NOT NULL CHECK (weekday BETWEEN 0 AND 6),
        time    TEXT NOT NULL,
        PRIMARY KEY (boss_id, number)
    );
    INSERT INTO new_squads (boss_id, number, weekday, time) SELECT 1, number, weekday, time FROM squads;
    DROP TABLE squads;
    ALTER TABLE new_squads RENAME TO squads;

    CREATE TABLE new_squad_week_overrides (
        boss_id    INTEGER NOT NULL,
        week_start TEXT NOT NULL,
        squad      INTEGER NOT NULL,
        starts_at  INTEGER NOT NULL,
        PRIMARY KEY (boss_id, week_start, squad),
        FOREIGN KEY (boss_id, squad) REFERENCES squads (boss_id, number) ON DELETE CASCADE
    );
    INSERT INTO new_squad_week_overrides (boss_id, week_start, squad, starts_at)
        SELECT 1, week_start, squad, starts_at FROM squad_week_overrides;
    DROP TABLE squad_week_overrides;
    ALTER TABLE new_squad_week_overrides RENAME TO squad_week_overrides;

    -- availability and confirmations: per boss
    CREATE TABLE new_default_availability (
        boss_id   INTEGER NOT NULL REFERENCES bosses (id) ON DELETE CASCADE,
        player_id INTEGER NOT NULL REFERENCES players (id) ON DELETE CASCADE,
        squad     INTEGER NOT NULL,
        level     TEXT NOT NULL CHECK (level IN ('Preferred', 'Available', 'Not Available')),
        PRIMARY KEY (boss_id, player_id, squad)
    );
    INSERT INTO new_default_availability (boss_id, player_id, squad, level)
        SELECT 1, player_id, squad, level FROM default_availability;
    DROP TABLE default_availability;
    ALTER TABLE new_default_availability RENAME TO default_availability;

    CREATE TABLE new_weekly_availability (
        boss_id    INTEGER NOT NULL REFERENCES bosses (id) ON DELETE CASCADE,
        player_id  INTEGER NOT NULL REFERENCES players (id) ON DELETE CASCADE,
        week_start TEXT NOT NULL,
        squad      INTEGER NOT NULL,
        level      TEXT NOT NULL CHECK (level IN ('Preferred', 'Available', 'Not Available')),
        PRIMARY KEY (boss_id, player_id, week_start, squad)
    );
    INSERT INTO new_weekly_availability (boss_id, player_id, week_start, squad, level)
        SELECT 1, player_id, week_start, squad, level FROM weekly_availability;
    DROP TABLE weekly_availability;
    ALTER TABLE new_weekly_availability RENAME TO weekly_availability;

    CREATE TABLE new_character_availability (
        boss_id      INTEGER NOT NULL REFERENCES bosses (id) ON DELETE CASCADE,
        character_id INTEGER NOT NULL REFERENCES characters (id) ON DELETE CASCADE,
        squad        INTEGER NOT NULL,
        level        TEXT NOT NULL CHECK (level IN ('Preferred', 'Available', 'Not Available')),
        PRIMARY KEY (boss_id, character_id, squad)
    );
    INSERT INTO new_character_availability (boss_id, character_id, squad, level)
        SELECT 1, character_id, squad, level FROM character_availability;
    DROP TABLE character_availability;
    ALTER TABLE new_character_availability RENAME TO character_availability;

    CREATE TABLE new_weekly_confirmations (
        boss_id      INTEGER NOT NULL REFERENCES bosses (id) ON DELETE CASCADE,
        player_id    INTEGER NOT NULL REFERENCES players (id) ON DELETE CASCADE,
        week_start   TEXT NOT NULL,
        kind         TEXT NOT NULL CHECK (kind IN ('no_change', 'updated')),
        confirmed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (boss_id, player_id, week_start)
    );
    INSERT INTO new_weekly_confirmations (boss_id, player_id, week_start, kind, confirmed_at)
        SELECT 1, player_id, week_start, kind, confirmed_at FROM weekly_confirmations;
    DROP TABLE weekly_confirmations;
    ALTER TABLE new_weekly_confirmations RENAME TO weekly_confirmations;

    -- damage logs: per boss (damage_runs follows its log)
    CREATE TABLE new_damage_logs (
        id          INTEGER PRIMARY KEY,
        boss_id     INTEGER NOT NULL REFERENCES bosses (id) ON DELETE CASCADE,
        message_id  INTEGER,
        filename    TEXT,
        started_at  INTEGER NOT NULL,
        finished_at INTEGER NOT NULL,
        uploaded_by INTEGER,
        uploaded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE (boss_id, started_at, finished_at)
    );
    INSERT INTO new_damage_logs (id, boss_id, message_id, filename, started_at, finished_at, uploaded_by, uploaded_at)
        SELECT id, 1, message_id, filename, started_at, finished_at, uploaded_by, uploaded_at FROM damage_logs;
    DROP TABLE damage_logs;
    ALTER TABLE new_damage_logs RENAME TO damage_logs;
    CREATE INDEX idx_damage_logs_message ON damage_logs (message_id);

    -- character status log: per boss
    CREATE TABLE new_character_status_log (
        id           INTEGER PRIMARY KEY,
        boss_id      INTEGER NOT NULL REFERENCES bosses (id) ON DELETE CASCADE,
        character_id INTEGER NOT NULL REFERENCES characters (id) ON DELETE CASCADE,
        old_status   TEXT NOT NULL,
        new_status   TEXT NOT NULL,
        changed_at   INTEGER NOT NULL DEFAULT (strftime('%s', 'now')),
        changed_by   INTEGER
    );
    INSERT INTO new_character_status_log (id, boss_id, character_id, old_status, new_status, changed_at, changed_by)
        SELECT id, 1, character_id, old_status, new_status, changed_at, changed_by FROM character_status_log;
    DROP TABLE character_status_log;
    ALTER TABLE new_character_status_log RENAME TO character_status_log;
    CREATE INDEX idx_character_status_log_time ON character_status_log (changed_at);

    -- settings that belong to a boss
    CREATE TABLE boss_settings (
        boss_id INTEGER NOT NULL REFERENCES bosses (id) ON DELETE CASCADE,
        key     TEXT NOT NULL,
        value   TEXT NOT NULL,
        PRIMARY KEY (boss_id, key)
    );
    INSERT INTO boss_settings (boss_id, key, value)
        SELECT 1, key, value FROM settings WHERE key IN (
            'reminder_enabled', 'reminder_weekdays', 'reminder_weekday', 'reminder_time',
            'deadline_weekday', 'deadline_time', 'cq_channel_id', 'queen_logs_channel_id',
            'damage_average_runs', 'last_reminder_slot', 'last_reminder_week'
        );
    DELETE FROM settings WHERE key IN (SELECT key FROM boss_settings);
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
    if version >= len(MIGRATIONS):
        return
    # Foreign keys go off while migrating: rebuilding a table drops the old one, and with foreign
    # keys on that would cascade-delete its children. They're checked before being turned back on.
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        for i, script in enumerate(MIGRATIONS[version:], start=version + 1):
            try:
                conn.executescript(f"BEGIN;\n{script}\nPRAGMA user_version = {i};\nCOMMIT;")
            except Exception:
                conn.rollback()  # a failed migration leaves the database as it was
                raise
        problems = conn.execute("PRAGMA foreign_key_check").fetchall()
        if problems:
            raise RuntimeError(f"Database migration left broken references: {[tuple(p) for p in problems[:5]]}")
    finally:
        conn.execute("PRAGMA foreign_keys = ON")


# --------------------------------------------------------------------------- settings
# Global settings (per-person preferences, class icon fingerprints) live in `settings`; settings
# that belong to one boss (reminders, channels, damage averaging) live in `boss_settings`.

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


def get_boss_setting(conn: sqlite3.Connection, key: str, default: str | None = None, *, boss_id: int = CQ) -> str | None:
    row = conn.execute("SELECT value FROM boss_settings WHERE boss_id = ? AND key = ?", (boss_id, key)).fetchone()
    return row["value"] if row else default


def set_boss_setting(conn: sqlite3.Connection, key: str, value: str, *, boss_id: int = CQ) -> None:
    with conn:
        conn.execute(
            "INSERT INTO boss_settings (boss_id, key, value) VALUES (?, ?, ?) "
            "ON CONFLICT (boss_id, key) DO UPDATE SET value = excluded.value",
            (boss_id, key, value),
        )


# --------------------------------------------------------------------------- players
# A player's status (active / inactive) is per boss (player_boss). A player without a row for a
# boss isn't on that boss's roster, so counts as inactive for it.

@dataclass
class Player:
    id: int
    discord_id: int | None
    discord_handle: str | None
    name: str
    status: str  # for the boss the player was loaded for

    @property
    def display(self) -> str:
        if self.discord_handle:
            return f"{self.name} ({self.discord_handle})"
        return self.name

    @property
    def mention(self) -> str:
        return f"<@{self.discord_id}>" if self.discord_id else self.display


_PLAYER_SELECT = """
    SELECT p.id, p.discord_id, p.discord_handle, p.name, COALESCE(pb.status, 'inactive') AS status
    FROM players p
    LEFT JOIN player_boss pb ON pb.player_id = p.id AND pb.boss_id = ?
"""


def _player(row: sqlite3.Row | None) -> Player | None:
    if row is None:
        return None
    return Player(row["id"], row["discord_id"], row["discord_handle"], row["name"], row["status"])


def get_player(conn: sqlite3.Connection, player_id: int, *, boss_id: int = CQ) -> Player | None:
    return _player(conn.execute(_PLAYER_SELECT + " WHERE p.id = ?", (boss_id, player_id)).fetchone())


def get_player_by_discord_id(conn: sqlite3.Connection, discord_id: int, *, boss_id: int = CQ) -> Player | None:
    return _player(conn.execute(_PLAYER_SELECT + " WHERE p.discord_id = ?", (boss_id, discord_id)).fetchone())


def get_player_by_handle(conn: sqlite3.Connection, handle: str, *, boss_id: int = CQ) -> Player | None:
    handle = handle if handle.startswith("@") else f"@{handle}"
    return _player(conn.execute(_PLAYER_SELECT + " WHERE p.discord_handle = ?", (boss_id, handle)).fetchone())


def list_players(
    conn: sqlite3.Connection, statuses: tuple[str, ...] = PLAYER_STATUSES, *, boss_id: int = CQ
) -> list[Player]:
    marks = ",".join("?" * len(statuses))
    rows = conn.execute(
        _PLAYER_SELECT + f" WHERE COALESCE(pb.status, 'inactive') IN ({marks}) ORDER BY p.name COLLATE NOCASE",
        (boss_id, *statuses),
    ).fetchall()
    return [_player(r) for r in rows]


def search_players(conn: sqlite3.Connection, text: str, limit: int = 25, *, boss_id: int = CQ) -> list[Player]:
    """Match players by name, Discord handle, or any of their character IGNs."""
    like = f"%{text.strip().lstrip('@')}%"
    rows = conn.execute(
        _PLAYER_SELECT
        + """
        WHERE p.name LIKE ? OR p.discord_handle LIKE ?
           OR EXISTS (SELECT 1 FROM characters c WHERE c.player_id = p.id AND c.ign LIKE ?)
        ORDER BY p.name COLLATE NOCASE
        LIMIT ?
        """,
        (boss_id, like, like, like, limit),
    ).fetchall()
    return [_player(r) for r in rows]


def create_player(
    conn: sqlite3.Connection,
    name: str,
    discord_handle: str | None = None,
    discord_id: int | None = None,
    status: str = "active",
    *,
    boss_id: int = CQ,
) -> Player:
    """Add a player, on the roster of `boss_id` with `status`."""
    with conn:
        cur = conn.execute(
            "INSERT INTO players (name, discord_handle, discord_id) VALUES (?, ?, ?)",
            (name, discord_handle, discord_id),
        )
        conn.execute(
            "INSERT INTO player_boss (player_id, boss_id, status) VALUES (?, ?, ?)", (cur.lastrowid, boss_id, status)
        )
    return get_player(conn, cur.lastrowid, boss_id=boss_id)


def link_discord(conn: sqlite3.Connection, player_id: int, discord_id: int, handle: str | None) -> None:
    with conn:
        conn.execute("UPDATE players SET discord_id = NULL WHERE discord_id = ? AND id != ?", (discord_id, player_id))
        conn.execute(
            "UPDATE players SET discord_id = ?, discord_handle = COALESCE(?, discord_handle) WHERE id = ?",
            (discord_id, handle, player_id),
        )


def set_player_status(conn: sqlite3.Connection, player_id: int, status: str, *, boss_id: int = CQ) -> None:
    if status not in PLAYER_STATUSES:
        raise ValueError(status)
    with conn:
        conn.execute(
            "INSERT INTO player_boss (player_id, boss_id, status) VALUES (?, ?, ?) "
            "ON CONFLICT (player_id, boss_id) DO UPDATE SET status = excluded.status",
            (player_id, boss_id, status),
        )


def set_player_name(conn: sqlite3.Connection, player_id: int, name: str) -> None:
    with conn:
        conn.execute("UPDATE players SET name = ? WHERE id = ?", (name, player_id))


# --------------------------------------------------------------------------- characters
# `characters` is identity only (owner, IGN, job, buff). Damage, status and slotting are per boss
# (character_boss). The functions below return both joined, with the same column names as before:
# id, player_id, ign, job, buff, status, dmg, base_dmg, run_time, squad, slot_status, perm.

_CHARACTER_SELECT = """
    SELECT c.id, c.player_id, c.ign, c.job, c.buff,
           COALESCE(cb.status, 'inactive') AS status, cb.dmg, cb.base_dmg, cb.run_time,
           cb.squad, cb.slot_status, COALESCE(cb.perm, 0) AS perm
    FROM characters c
    LEFT JOIN character_boss cb ON cb.character_id = c.id AND cb.boss_id = ?
"""

# damage logged for this character for this boss (used where dmg falls back to base_dmg)
_HAS_LOGGED_RUNS = """
    EXISTS (SELECT 1 FROM damage_runs r JOIN damage_logs l ON l.id = r.log_id
            WHERE r.character_id = character_boss.character_id AND l.boss_id = character_boss.boss_id)
"""


def list_characters(conn: sqlite3.Connection, player_id: int, *, boss_id: int = CQ) -> list[sqlite3.Row]:
    return conn.execute(
        _CHARACTER_SELECT + " WHERE c.player_id = ? ORDER BY cb.dmg DESC, c.ign COLLATE NOCASE", (boss_id, player_id)
    ).fetchall()


def get_character(conn: sqlite3.Connection, ign: str, *, boss_id: int = CQ) -> sqlite3.Row | None:
    return conn.execute(_CHARACTER_SELECT + " WHERE c.ign = ?", (boss_id, ign.strip())).fetchone()


def add_character(
    conn: sqlite3.Connection,
    player_id: int,
    ign: str,
    job: str,
    buff: str,
    dmg: float | None,
    status: str = "static",
    *,
    boss_id: int = CQ,
) -> None:
    with conn:
        cur = conn.execute(
            "INSERT INTO characters (player_id, ign, job, buff) VALUES (?, ?, ?, ?)",
            (player_id, ign.strip(), job.strip().upper(), buff),
        )
        conn.execute(
            "INSERT INTO character_boss (character_id, boss_id, status, dmg, base_dmg, slot_status) "
            "VALUES (?, ?, ?, ?, ?, 'Available')",
            (cur.lastrowid, boss_id, status, dmg, dmg),
        )


def _ensure_character_boss(conn: sqlite3.Connection, character_id: int, boss_id: int) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO character_boss (character_id, boss_id) VALUES (?, ?)", (character_id, boss_id)
    )


def set_character_statuses(
    conn: sqlite3.Connection,
    player_id: int,
    statuses: dict[int, str],
    changed_by: int | None = None,
    *,
    boss_id: int = CQ,
) -> None:
    """Set static/sub/inactive for several of a player's characters ({character id: status}).

    Every actual change is logged (see status_changes_since)."""
    if any(s not in CHARACTER_STATUSES for s in statuses.values()):
        raise ValueError(statuses)
    with conn:
        for cid, status in statuses.items():
            row = conn.execute(
                _CHARACTER_SELECT + " WHERE c.id = ? AND c.player_id = ?", (boss_id, cid, player_id)
            ).fetchone()
            if row is not None and row["status"] != status:
                _log_status_change(conn, boss_id, cid, row["status"], status, changed_by)
                _ensure_character_boss(conn, cid, boss_id)
                conn.execute(
                    "UPDATE character_boss SET status = ? WHERE character_id = ? AND boss_id = ?", (status, cid, boss_id)
                )


def _log_status_change(
    conn: sqlite3.Connection, boss_id: int, character_id: int, old: str, new: str, changed_by: int | None
) -> None:
    conn.execute(
        "INSERT INTO character_status_log (boss_id, character_id, old_status, new_status, changed_by) "
        "VALUES (?, ?, ?, ?, ?)",
        (boss_id, character_id, old, new, changed_by),
    )


def status_changes_since(conn: sqlite3.Connection, since: int, *, boss_id: int = CQ) -> list[dict]:
    """Characters whose status for `boss_id` changed since `since` (unix seconds): one entry each,
    with the status before the first change and after the last. Characters that ended where they
    started are left out. Sorted by player name, then character name."""
    rows = conn.execute(
        """
        SELECT l.character_id, l.old_status, l.new_status, l.changed_at, l.changed_by,
               c.ign, c.job, p.name AS player_name, p.discord_id AS owner_id
        FROM character_status_log l
        JOIN characters c ON c.id = l.character_id
        JOIN players p ON p.id = c.player_id
        WHERE l.boss_id = ? AND l.changed_at >= ?
        ORDER BY l.changed_at, l.id
        """,
        (boss_id, since),
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


def update_character(
    conn: sqlite3.Connection, character_id: int, changed_by: int | None = None, *, boss_id: int = CQ, **fields
) -> None:
    """Change a character. job, buff and player_id (owner) are shared by every boss; dmg and
    status are for `boss_id`. A hand-entered dmg is the fallback: damage from uploaded logs wins."""
    identity = {k: v for k, v in fields.items() if k in {"job", "buff", "player_id"} and v is not None}
    status, dmg = fields.get("status"), fields.get("dmg")
    with conn:
        if identity:
            sets = ", ".join(f"{k} = ?" for k in identity)
            conn.execute(f"UPDATE characters SET {sets} WHERE id = ?", (*identity.values(), character_id))
        if status is None and dmg is None:
            return
        current = conn.execute(_CHARACTER_SELECT + " WHERE c.id = ?", (boss_id, character_id)).fetchone()
        if current is None:
            return
        _ensure_character_boss(conn, character_id, boss_id)
        if status is not None and current["status"] != status:
            _log_status_change(conn, boss_id, character_id, current["status"], status, changed_by)
            conn.execute(
                "UPDATE character_boss SET status = ? WHERE character_id = ? AND boss_id = ?",
                (status, character_id, boss_id),
            )
        if dmg is not None:
            conn.execute(
                f"UPDATE character_boss SET base_dmg = ?, dmg = CASE WHEN {_HAS_LOGGED_RUNS} THEN dmg ELSE ? END "
                "WHERE character_id = ? AND boss_id = ?",
                (dmg, dmg, character_id, boss_id),
            )


def delete_character(conn: sqlite3.Connection, character_id: int) -> None:
    """Remove a character everywhere (every boss's data for it goes too)."""
    with conn:
        conn.execute("DELETE FROM characters WHERE id = ?", (character_id,))


def known_jobs(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute("SELECT DISTINCT UPPER(job) AS job FROM characters WHERE job != '' ORDER BY job").fetchall()
    return [r["job"] for r in rows]


# --------------------------------------------------------------------------- squads (per boss)

@dataclass
class SquadTime:
    number: int
    starts_at: int  # unix seconds
    overridden: bool

    @property
    def label(self) -> str:
        return f"Squad {self.number}"


def list_squad_template(conn: sqlite3.Connection, *, boss_id: int = CQ) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM squads WHERE boss_id = ? ORDER BY weekday, time, number", (boss_id,)
    ).fetchall()


def squad_numbers(conn: sqlite3.Connection, *, boss_id: int = CQ) -> list[int]:
    return [r["number"] for r in conn.execute("SELECT number FROM squads WHERE boss_id = ? ORDER BY number", (boss_id,))]


def set_squad_template(conn: sqlite3.Connection, number: int, weekday: int, hhmm: str, *, boss_id: int = CQ) -> None:
    timeutil.parse_hhmm(hhmm)  # validate
    with conn:
        conn.execute(
            "INSERT INTO squads (boss_id, number, weekday, time) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (boss_id, number) DO UPDATE SET weekday = excluded.weekday, time = excluded.time",
            (boss_id, number, weekday, hhmm),
        )


def delete_squad(conn: sqlite3.Connection, number: int, *, boss_id: int = CQ) -> None:
    with conn:
        conn.execute("DELETE FROM squads WHERE boss_id = ? AND number = ?", (boss_id, number))


def squads_for_week(
    conn: sqlite3.Connection, week_start: date, tz: ZoneInfo, *, boss_id: int = CQ
) -> list[SquadTime]:
    """Squad start times for a given week: the template, with any per-week overrides applied."""
    overrides = {
        r["squad"]: r["starts_at"]
        for r in conn.execute(
            "SELECT squad, starts_at FROM squad_week_overrides WHERE boss_id = ? AND week_start = ?",
            (boss_id, week_start.isoformat()),
        )
    }
    result = []
    for row in list_squad_template(conn, boss_id=boss_id):
        if row["number"] in overrides:
            result.append(SquadTime(row["number"], overrides[row["number"]], True))
        else:
            dt = timeutil.at_weekday(week_start, row["weekday"], row["time"], tz)
            result.append(SquadTime(row["number"], int(dt.timestamp()), False))
    result.sort(key=lambda s: (s.starts_at, s.number))
    return result


def set_squad_override(
    conn: sqlite3.Connection, week_start: date, number: int, starts_at: datetime, *, boss_id: int = CQ
) -> None:
    with conn:
        conn.execute(
            "INSERT INTO squad_week_overrides (boss_id, week_start, squad, starts_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (boss_id, week_start, squad) DO UPDATE SET starts_at = excluded.starts_at",
            (boss_id, week_start.isoformat(), number, int(starts_at.timestamp())),
        )


def clear_squad_override(
    conn: sqlite3.Connection, week_start: date, number: int | None = None, *, boss_id: int = CQ
) -> None:
    with conn:
        if number is None:
            conn.execute(
                "DELETE FROM squad_week_overrides WHERE boss_id = ? AND week_start = ?", (boss_id, week_start.isoformat())
            )
        else:
            conn.execute(
                "DELETE FROM squad_week_overrides WHERE boss_id = ? AND week_start = ? AND squad = ?",
                (boss_id, week_start.isoformat(), number),
            )


# --------------------------------------------------------------------------- availability (per boss)

Availability = dict[int, str]  # squad number -> level


def get_default_availability(conn: sqlite3.Connection, player_id: int, *, boss_id: int = CQ) -> Availability:
    rows = conn.execute(
        "SELECT squad, level FROM default_availability WHERE boss_id = ? AND player_id = ?", (boss_id, player_id)
    )
    return {r["squad"]: r["level"] for r in rows}


def set_default_availability(
    conn: sqlite3.Connection, player_id: int, availability: Availability, *, boss_id: int = CQ
) -> None:
    with conn:
        conn.execute("DELETE FROM default_availability WHERE boss_id = ? AND player_id = ?", (boss_id, player_id))
        conn.executemany(
            "INSERT INTO default_availability (boss_id, player_id, squad, level) VALUES (?, ?, ?, ?)",
            [(boss_id, player_id, s, lvl) for s, lvl in availability.items()],
        )


def get_character_overrides(conn: sqlite3.Connection, character_id: int, *, boss_id: int = CQ) -> Availability:
    rows = conn.execute(
        "SELECT squad, level FROM character_availability WHERE boss_id = ? AND character_id = ?",
        (boss_id, character_id),
    )
    return {r["squad"]: r["level"] for r in rows}


def set_character_overrides(
    conn: sqlite3.Connection, character_id: int, overrides: Availability, *, boss_id: int = CQ
) -> None:
    with conn:
        conn.execute(
            "DELETE FROM character_availability WHERE boss_id = ? AND character_id = ?", (boss_id, character_id)
        )
        conn.executemany(
            "INSERT INTO character_availability (boss_id, character_id, squad, level) VALUES (?, ?, ?, ?)",
            [(boss_id, character_id, s, lvl) for s, lvl in overrides.items()],
        )


def get_weekly_availability(
    conn: sqlite3.Connection, player_id: int, week_start: date, *, boss_id: int = CQ
) -> Availability:
    rows = conn.execute(
        "SELECT squad, level FROM weekly_availability WHERE boss_id = ? AND player_id = ? AND week_start = ?",
        (boss_id, player_id, week_start.isoformat()),
    )
    return {r["squad"]: r["level"] for r in rows}


def weeks_with_changes(conn: sqlite3.Connection, player_id: int, from_week: date, *, boss_id: int = CQ) -> list[date]:
    """Weeks (starting on or after `from_week`) where the player submitted a schedule change."""
    rows = conn.execute(
        "SELECT DISTINCT week_start FROM weekly_availability WHERE boss_id = ? AND player_id = ? AND week_start >= ? "
        "ORDER BY week_start",
        (boss_id, player_id, from_week.isoformat()),
    )
    return [date.fromisoformat(r["week_start"]) for r in rows]


def set_weekly_availability(
    conn: sqlite3.Connection, player_id: int, week_start: date, availability: Availability, *, boss_id: int = CQ
) -> None:
    wk = week_start.isoformat()
    with conn:
        conn.execute(
            "DELETE FROM weekly_availability WHERE boss_id = ? AND player_id = ? AND week_start = ?",
            (boss_id, player_id, wk),
        )
        conn.executemany(
            "INSERT INTO weekly_availability (boss_id, player_id, week_start, squad, level) VALUES (?, ?, ?, ?, ?)",
            [(boss_id, player_id, wk, s, lvl) for s, lvl in availability.items()],
        )
        _confirm(conn, player_id, week_start, "updated", boss_id)


def confirm_no_change(conn: sqlite3.Connection, player_id: int, week_start: date, *, boss_id: int = CQ) -> None:
    """Player keeps their default for the week; any earlier weekly submission is discarded."""
    with conn:
        conn.execute(
            "DELETE FROM weekly_availability WHERE boss_id = ? AND player_id = ? AND week_start = ?",
            (boss_id, player_id, week_start.isoformat()),
        )
        _confirm(conn, player_id, week_start, "no_change", boss_id)


def _confirm(conn: sqlite3.Connection, player_id: int, week_start: date, kind: str, boss_id: int) -> None:
    conn.execute(
        "INSERT INTO weekly_confirmations (boss_id, player_id, week_start, kind) VALUES (?, ?, ?, ?) "
        "ON CONFLICT (boss_id, player_id, week_start) "
        "DO UPDATE SET kind = excluded.kind, confirmed_at = CURRENT_TIMESTAMP",
        (boss_id, player_id, week_start.isoformat(), kind),
    )


def get_confirmations(conn: sqlite3.Connection, week_start: date, *, boss_id: int = CQ) -> dict[int, str]:
    rows = conn.execute(
        "SELECT player_id, kind FROM weekly_confirmations WHERE boss_id = ? AND week_start = ?",
        (boss_id, week_start.isoformat()),
    )
    return {r["player_id"]: r["kind"] for r in rows}


def get_confirmation(conn: sqlite3.Connection, player_id: int, week_start: date, *, boss_id: int = CQ) -> str | None:
    row = conn.execute(
        "SELECT kind FROM weekly_confirmations WHERE boss_id = ? AND player_id = ? AND week_start = ?",
        (boss_id, player_id, week_start.isoformat()),
    ).fetchone()
    return row["kind"] if row else None


LEVEL_RANK = {level: i for i, level in enumerate(LEVELS)}  # lower = more available


def character_week_availability(
    conn: sqlite3.Connection, character: sqlite3.Row, week_start: date, *, boss_id: int = CQ
) -> Availability:
    """A character's availability for a week.

    Without a weekly change it's the player's default with the character's overrides on top.
    With a weekly change, each squad uses the less available of the weekly level and the
    character's override, so a character-specific limit ("DRK weekends only") still applies.
    """
    overrides = get_character_overrides(conn, character["id"], boss_id=boss_id)
    weekly = get_weekly_availability(conn, character["player_id"], week_start, boss_id=boss_id)
    if not weekly:
        return {**get_default_availability(conn, character["player_id"], boss_id=boss_id), **overrides}
    result = dict(weekly)
    for squad, level in overrides.items():
        if squad in result and LEVEL_RANK[level] > LEVEL_RANK[result[squad]]:
            result[squad] = level
    return result


def effective_week_availability(
    conn: sqlite3.Connection, player_id: int, week_start: date, *, boss_id: int = CQ
) -> tuple[Availability, str]:
    """This week's availability for a player and where it came from ('weekly' or 'default')."""
    weekly = get_weekly_availability(conn, player_id, week_start, boss_id=boss_id)
    if weekly:
        return weekly, "weekly"
    return get_default_availability(conn, player_id, boss_id=boss_id), "default"
