from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from bot import db, importer, reminders, timeutil

ROOT = Path(__file__).resolve().parent.parent
ROSTER = ROOT / "bot_data" / "Monkey, Inc.  - CQ Roster.csv"
TIMINGS = ROOT / "bot_data" / "squad_timings.txt"
ET = ZoneInfo("America/New_York")


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "test.db")
    yield c
    c.close()


@pytest.fixture
def seeded(conn):
    if not (ROSTER.exists() and TIMINGS.exists()):
        pytest.skip("bot_data/ seed files not present (they are git-ignored)")
    importer.seed_if_empty(conn, ROSTER, TIMINGS, ET)
    return conn


def some_player(conn) -> db.Player:
    """A seeded player with several characters (picked at runtime so no real names live in the repo)."""
    row = conn.execute(
        """
        SELECT p.id FROM players p JOIN characters c ON c.player_id = p.id
        JOIN default_availability d ON d.player_id = p.id
        WHERE p.discord_handle IS NOT NULL
        GROUP BY p.id ORDER BY COUNT(DISTINCT c.id) DESC, p.id LIMIT 1
        """
    ).fetchone()
    return db.get_player(conn, row["id"])


def test_week_boundaries():
    # Saturday 2026-09-26 23:30 ET -> current week started Sun 9/20, upcoming is Sun 9/27
    now = datetime(2026, 9, 27, 3, 30, tzinfo=timezone.utc)
    assert timeutil.current_week_start(now, ET) == date(2026, 9, 20)
    assert timeutil.upcoming_week_start(now, ET) == date(2026, 9, 27)
    # Sunday 00:30 ET -> a new week has started
    now = datetime(2026, 9, 27, 4, 30, tzinfo=timezone.utc)
    assert timeutil.current_week_start(now, ET) == date(2026, 9, 27)
    assert timeutil.upcoming_week_start(now, ET) == date(2026, 10, 4)


def test_parse_weekday():
    assert timeutil.parse_weekday("sun") == 0
    assert timeutil.parse_weekday("Thursday") == 4
    with pytest.raises(ValueError):
        timeutil.parse_weekday("s")


def test_seed_reproduces_squad_timestamps(seeded):
    expected = {int(n): int(ts) for n, ts in importer.TIMING_LINE.findall(TIMINGS.read_text())}
    got = {s.number: s.starts_at for s in db.squads_for_week(seeded, date(2026, 9, 20), ET)}
    assert got == expected


def test_seed_roster(seeded):
    players = db.list_players(seeded)
    assert len(players) == 73
    assert {p.status for p in players} == {"active"}
    assert seeded.execute("SELECT COUNT(*) FROM characters").fetchone()[0] == 328
    player = some_player(seeded)
    assert len(db.list_characters(seeded, player.id)) >= 2
    assert db.get_player_by_handle(seeded, player.discord_handle).id == player.id
    assert set(db.get_default_availability(seeded, player.id).values()) <= set(db.LEVELS)
    # <@id> handles in the sheet are linked to a Discord account straight away
    assert seeded.execute("SELECT COUNT(*) FROM players WHERE discord_id IS NOT NULL").fetchone()[0] >= 1


def test_reimport_is_idempotent(seeded):
    result = importer.import_roster(seeded, ROSTER.read_text(encoding="utf-8-sig"))
    assert result.players_created == 0 and result.characters_created == 0
    assert len(db.list_players(seeded)) == 73


def test_reimport_keeps_status_and_bot_availability(seeded):
    player = some_player(seeded)
    db.set_player_status(seeded, player.id, "sub")
    db.set_default_availability(seeded, player.id, {1: "Not Available"})
    importer.import_roster(seeded, ROSTER.read_text(encoding="utf-8-sig"))
    assert db.get_player(seeded, player.id).status == "sub"
    assert db.get_default_availability(seeded, player.id) == {1: "Not Available"}


def test_weekly_flow(seeded):
    player = some_player(seeded)
    week = date(2026, 9, 27)
    avail, source = db.effective_week_availability(seeded, player.id, week)
    assert source == "default"

    db.set_weekly_availability(seeded, player.id, week, {1: "Not Available"})
    assert db.effective_week_availability(seeded, player.id, week) == ({1: "Not Available"}, "weekly")
    assert db.get_confirmation(seeded, player.id, week) == "updated"

    db.confirm_no_change(seeded, player.id, week)
    assert db.effective_week_availability(seeded, player.id, week)[1] == "default"
    assert db.get_confirmation(seeded, player.id, week) == "no_change"


def test_squad_override(seeded):
    week = date(2026, 9, 27)
    new_time = timeutil.at_weekday(week, 0, "13:00", ET)
    db.set_squad_override(seeded, week, 1, new_time)
    squad1 = next(s for s in db.squads_for_week(seeded, week, ET) if s.number == 1)
    assert squad1.overridden and squad1.starts_at == int(new_time.timestamp())
    # other weeks are unaffected
    later = next(s for s in db.squads_for_week(seeded, date(2026, 10, 4), ET) if s.number == 1)
    assert not later.overridden


def test_reminder_and_deadline_fall_in_previous_week(conn):
    settings = reminders.load(conn, None)
    week = date(2026, 9, 27)
    times = settings.reminder_times(week, ET)
    assert [t.date() for t in times] == [date(2026, 9, 23), date(2026, 9, 25)]  # Wed, Fri
    assert all(t.strftime("%H:%M") == "18:00" for t in times)
    assert settings.deadline_at(week, ET).date() == date(2026, 9, 26)  # Saturday


def test_latest_due_reminder(conn):
    settings = reminders.load(conn, None)
    week = date(2026, 9, 27)
    wed, fri = settings.reminder_times(week, ET)
    assert settings.latest_due(week, datetime(2026, 9, 23, 12, tzinfo=ET), ET) is None
    assert settings.latest_due(week, datetime(2026, 9, 24, 12, tzinfo=ET), ET) == wed
    assert settings.latest_due(week, datetime(2026, 9, 26, 9, tzinfo=ET), ET) == fri


def test_parse_weekdays():
    assert reminders.parse_weekdays("Wed, Fri") == [3, 5]
    assert reminders.parse_weekdays("fri,wed,wed") == [3, 5]
    assert reminders.parse_weekdays("3,5") == [3, 5]
    with pytest.raises(ValueError):
        reminders.parse_weekdays("Blursday")


def test_migrations_are_repeatable(tmp_path):
    path = tmp_path / "again.db"
    db.connect(path).close()
    c = db.connect(path)
    assert c.execute("PRAGMA user_version").fetchone()[0] == len(db.MIGRATIONS)


def test_week_label_casing():
    assert timeutil.week_label(date(2026, 9, 27)) == "week of Sunday Sep 27, 2026"
    assert timeutil.week_label(date(2026, 10, 4), capital=True) == "Week of Sunday Oct 4, 2026"


def test_local_label_twelve_hour():
    def label(hour, minute, **kw):
        unix = int(datetime(2026, 9, 27, hour, minute, tzinfo=ET).timestamp())
        return timeutil.local_label(unix, ET, **kw)

    assert label(12, 0, with_zone=True, twelve_hour=True) == "Sun 12:00 PM EDT"
    assert label(0, 5, twelve_hour=True) == "Sun 12:05 AM"
    assert label(21, 25, twelve_hour=True) == "Sun 9:25 PM"
    assert label(9, 30, twelve_hour=True) == "Sun 9:30 AM"
    assert label(21, 25) == "Sun 21:25"  # 24-hour stays the default elsewhere
