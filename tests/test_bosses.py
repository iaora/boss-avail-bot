"""Boss-aware schema (migration 6): Crimson Queen data is kept, and bosses don't leak into each other."""

import sqlite3
from datetime import date
from zoneinfo import ZoneInfo

import pytest

from bot import damage, db

ET = ZoneInfo("America/New_York")
SYDNEY = ZoneInfo("Australia/Sydney")
WEEK = date(2026, 9, 27)
OTHER = 2  # a second boss, for isolation tests


def build_v5(path):
    """A database at schema version 5 (before bosses) with one of everything."""
    raw = sqlite3.connect(path)
    for version, script in enumerate(db.MIGRATIONS[:5], start=1):
        raw.executescript(script)
        raw.execute(f"PRAGMA user_version = {version}")
    raw.executescript(
        """
        INSERT INTO players (id, name, discord_handle, discord_id, status) VALUES
            (1, 'Alice', '@alice', 11, 'active'), (2, 'Bob', '@bob', 22, 'inactive');
        INSERT INTO characters (id, player_id, ign, job, buff, dmg, base_dmg, run_time, squad, slot_status, perm, status)
            VALUES (1, 1, 'Ace', 'NL', 'DPS', 4.5, 4.0, 26.0, 3, 'Slotted', 1, 'static'),
                   (2, 2, 'Bolt', 'DRK', 'HB', NULL, NULL, NULL, NULL, 'Available', 0, 'inactive');
        INSERT INTO squads (number, weekday, time) VALUES (1, 0, '12:00'), (3, 6, '11:00');
        INSERT INTO squad_week_overrides (week_start, squad, starts_at) VALUES ('2026-09-27', 1, 1790530000);
        INSERT INTO default_availability (player_id, squad, level) VALUES (1, 1, 'Preferred'), (1, 3, 'Available');
        INSERT INTO weekly_availability (player_id, week_start, squad, level) VALUES (1, '2026-09-27', 1, 'Not Available');
        INSERT INTO character_availability (character_id, squad, level) VALUES (1, 3, 'Not Available');
        INSERT INTO weekly_confirmations (player_id, week_start, kind) VALUES (1, '2026-09-27', 'updated');
        INSERT INTO damage_logs (id, message_id, filename, started_at, finished_at) VALUES (1, 99, 'run.txt', 100, 1750);
        INSERT INTO damage_runs (log_id, character_id, damage, normalized) VALUES (1, 1, 4500000000, 4.5);
        INSERT INTO character_status_log (character_id, old_status, new_status, changed_by) VALUES (1, 'sub', 'static', 11);
        INSERT INTO settings (key, value) VALUES
            ('reminder_weekdays', '2,4'), ('cq_channel_id', '555'), ('damage_average_runs', '5'),
            ('last_reminder_slot', '123'), ('squad_order_user:11', 'number'), ('class_icon_sha256:NL', 'abc');
        """
    )
    raw.commit()
    raw.close()


def test_migration_keeps_everything_as_crimson_queen(tmp_path):
    path = tmp_path / "v5.db"
    build_v5(path)
    conn = db.connect(path)

    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(db.MIGRATIONS)
    assert [tuple(r) for r in conn.execute("SELECT id, key, name FROM bosses")] == [(1, "cq", "Crimson Queen")]
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []

    # players and characters keep their identity; their CQ data moved to the per-boss tables
    assert [p.status for p in db.list_players(conn, ("active", "inactive"))] == ["active", "inactive"]
    ace = db.get_character(conn, "Ace")
    assert (ace["dmg"], ace["base_dmg"], ace["run_time"], ace["squad"], ace["slot_status"], ace["perm"], ace["status"]) == (
        4.5, 4.0, 26.0, 3, "Slotted", 1, "static"
    )
    assert db.get_character(conn, "Bolt")["status"] == "inactive"
    assert "status" not in {r[1] for r in conn.execute("PRAGMA table_info(players)")}
    assert "dmg" not in {r[1] for r in conn.execute("PRAGMA table_info(characters)")}

    # every boss-specific table now has its rows under Crimson Queen
    for table in ("squads", "squad_week_overrides", "default_availability", "weekly_availability",
                  "character_availability", "weekly_confirmations", "damage_logs", "character_status_log"):
        assert conn.execute(f"SELECT COUNT(*) FROM {table} WHERE boss_id = 1").fetchone()[0] > 0, table
        assert conn.execute(f"SELECT COUNT(*) FROM {table} WHERE boss_id != 1").fetchone()[0] == 0, table
    assert db.get_default_availability(conn, 1) == {1: "Preferred", 3: "Available"}
    assert db.get_weekly_availability(conn, 1, WEEK) == {1: "Not Available"}
    assert db.get_character_overrides(conn, 1) == {3: "Not Available"}
    assert db.get_confirmation(conn, 1, WEEK) == "updated"
    assert [s.overridden for s in db.squads_for_week(conn, WEEK, ET)] == [True, False]
    assert conn.execute("SELECT COUNT(*) FROM damage_runs").fetchone()[0] == 1

    # boss settings moved; per-person and class icon settings stayed global
    assert db.get_boss_setting(conn, "reminder_weekdays") == "2,4"
    assert db.get_boss_setting(conn, "cq_channel_id") == "555"
    assert damage.average_runs(conn) == 5
    assert db.get_setting(conn, "reminder_weekdays") is None
    assert db.get_setting(conn, "squad_order_user:11") == "number"
    assert db.get_setting(conn, "class_icon_sha256:NL") == "abc"
    conn.close()


def test_failed_migration_leaves_database_untouched(tmp_path, monkeypatch):
    path = tmp_path / "v5.db"
    build_v5(path)
    monkeypatch.setattr(db, "MIGRATIONS", [*db.MIGRATIONS[:5], db.MIGRATIONS[5] + "\nTHIS IS NOT SQL;"])
    with pytest.raises(sqlite3.OperationalError):
        db.connect(path)
    raw = sqlite3.connect(path)
    assert raw.execute("PRAGMA user_version").fetchone()[0] == 5
    assert raw.execute("SELECT status FROM players WHERE id = 1").fetchone()[0] == "active"  # old schema intact
    raw.close()


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "bosses.db")
    c.execute("INSERT INTO bosses (id, key, name) VALUES (?, 'other', 'Other Boss')", (OTHER,))
    c.commit()
    yield c
    c.close()


def test_bosses_are_isolated(conn):
    player = db.create_player(conn, name="Alice", discord_handle="@alice", discord_id=11)
    db.add_character(conn, player.id, "Ace", "NL", "DPS", 4.0, "static")
    ace = db.get_character(conn, "Ace")

    # squads and availability
    db.set_squad_template(conn, 1, 0, "12:00")
    db.set_squad_template(conn, 1, 3, "20:00", boss_id=OTHER)  # same number, different boss and time
    assert [s.starts_at for s in db.squads_for_week(conn, WEEK, ET)] != [
        s.starts_at for s in db.squads_for_week(conn, WEEK, ET, boss_id=OTHER)
    ]
    db.set_default_availability(conn, player.id, {1: "Preferred"})
    db.set_default_availability(conn, player.id, {1: "Not Available"}, boss_id=OTHER)
    db.set_weekly_availability(conn, player.id, WEEK, {1: "Available"}, boss_id=OTHER)
    assert db.effective_week_availability(conn, player.id, WEEK) == ({1: "Preferred"}, "default")
    assert db.get_confirmation(conn, player.id, WEEK) is None
    assert db.get_confirmation(conn, player.id, WEEK, boss_id=OTHER) == "updated"

    # player status: on Crimson Queen's roster (active); not on the other boss's until added
    assert db.get_player(conn, player.id).status == "active"
    assert db.get_player(conn, player.id, boss_id=OTHER).status == "inactive"
    assert db.list_players(conn, ("active",), boss_id=OTHER) == []
    db.set_player_status(conn, player.id, "active", boss_id=OTHER)
    db.set_player_status(conn, player.id, "inactive")  # CQ only
    assert (db.get_player(conn, player.id).status, db.get_player(conn, player.id, boss_id=OTHER).status) == (
        "inactive", "active"
    )

    # character status and its log
    db.set_character_statuses(conn, player.id, {ace["id"]: "sub"}, changed_by=11, boss_id=OTHER)
    assert db.get_character(conn, "Ace")["status"] == "static"
    assert db.get_character(conn, "Ace", boss_id=OTHER)["status"] == "sub"
    assert db.status_changes_since(conn, 0) == []
    assert [c["after"] for c in db.status_changes_since(conn, 0, boss_id=OTHER)] == ["sub"]

    # damage: a log for the other boss doesn't touch Crimson Queen's damage
    text = "[Start Time] 27-09-2026 01:00:00\n[Finish Time] 27-09-2026 01:27:30\n>>Ace: 9,000,000,000\n"
    damage.record_log(conn, damage.parse_log(text, SYDNEY), message_id=1, boss_id=OTHER)
    assert db.get_character(conn, "Ace")["dmg"] == 4.0
    assert db.get_character(conn, "Ace", boss_id=OTHER)["dmg"] == 9.0
    # the same run can be logged for Crimson Queen too; duplicate detection is per boss
    result = damage.record_log(conn, damage.parse_log(text, SYDNEY), message_id=2)
    assert not result.duplicate and db.get_character(conn, "Ace")["dmg"] == 9.0
    # deleting the other boss's upload only recomputes the other boss
    damage.delete_logs_for_message(conn, 1)
    assert db.get_character(conn, "Ace")["dmg"] == 9.0
    assert db.get_character(conn, "Ace", boss_id=OTHER)["dmg"] is None

    # boss settings
    db.set_boss_setting(conn, "damage_average_runs", "7", boss_id=OTHER)
    assert (damage.average_runs(conn), damage.average_runs(conn, boss_id=OTHER)) == (3, 7)


def test_removing_a_character_removes_it_for_every_boss(conn):
    player = db.create_player(conn, name="Alice", discord_handle="@alice")
    db.add_character(conn, player.id, "Ace", "NL", "DPS", 4.0)
    ace = db.get_character(conn, "Ace")
    db.update_character(conn, ace["id"], status="sub", boss_id=OTHER)
    db.delete_character(conn, ace["id"])
    assert conn.execute("SELECT COUNT(*) FROM character_boss").fetchone()[0] == 0
