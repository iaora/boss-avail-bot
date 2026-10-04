from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from bot import damage, db, importer

SYDNEY = ZoneInfo("Australia/Sydney")
ROOT = Path(__file__).resolve().parent.parent


def make_log(start: str, finish: str, rows: dict[str, int]) -> str:
    lines = ["[Deaths] Total: 1 (Alpha)", "[Alpha] obtained Yggdrasil Rune Stone"]
    lines += [f"[Start Time] {start}", f"[Finish Time] {finish}"]
    for i, (name, dmg) in enumerate(rows.items()):
        lines.append(f">>{name}: {dmg:,}" + (" | deaths: 1" if i == 0 else ""))
    return "\n".join(lines) + "\n"


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "test.db")
    player = db.create_player(c, name="Tester", discord_handle="@tester")
    for ign in ("Alpha", "Bravo"):
        db.add_character(c, player.id, ign, "NL", 1.0)
    yield c
    c.close()


def test_parse_log():
    text = make_log("27-09-2026 01:50:49", "27-09-2026 02:15:09", {"Alpha": 4_027_079_300, "Bravo": 2_189_928_746})
    log = damage.parse_log(text, SYDNEY)
    assert log.started_at == datetime(2026, 9, 27, 1, 50, 49, tzinfo=SYDNEY)
    assert round(log.minutes, 2) == 24.33
    assert log.damage == {"Alpha": 4_027_079_300, "Bravo": 2_189_928_746}


def test_parse_log_across_midnight():
    log = damage.parse_log(make_log("27-09-2026 23:50:00", "28-09-2026 00:15:00", {"Alpha": 1}), SYDNEY)
    assert log.minutes == 25


@pytest.mark.parametrize(
    "text",
    [
        ">>Alpha: 100",
        "[Start Time] 27-09-2026 02:00:00\n[Finish Time] 27-09-2026 01:00:00\n>>Alpha: 100",
        "[Start Time] 27-09-2026 01:00:00\n[Finish Time] 27-09-2026 01:30:00\n",
    ],
)
def test_parse_log_rejects_bad_files(text):
    with pytest.raises(ValueError):
        damage.parse_log(text, SYDNEY)


def test_normalized_to_27_5_minutes():
    assert damage.normalized(4_000_000_000, 27.5) == 4.0
    assert damage.normalized(2_000_000_000, 25) == 2.2


def test_record_updates_damage_and_reports_unknown(conn):
    log = damage.parse_log(
        make_log("27-09-2026 01:00:00", "27-09-2026 01:27:30", {"alpha": 3_000_000_000, "Nobody": 5}), SYDNEY
    )
    result = damage.record_log(conn, log, message_id=1)
    assert result.unknown == ["Nobody"]
    assert result.updated == [("Alpha", 3.0, 3.0)]  # names match case-insensitively
    assert db.get_character(conn, "Alpha")["dmg"] == 3.0
    assert db.get_character(conn, "Bravo")["dmg"] == 1.0  # untouched


def test_average_of_recent_runs(conn):
    for i, dmg in enumerate([1, 2, 3, 4]):  # billions, 27.5-minute runs
        text = make_log(f"0{i + 1}-09-2026 01:00:00", f"0{i + 1}-09-2026 01:27:30", {"Alpha": dmg * 1_000_000_000})
        damage.record_log(conn, damage.parse_log(text, SYDNEY), message_id=100 + i)
    assert db.get_character(conn, "Alpha")["dmg"] == 3.0  # average of the latest 3: 2, 3, 4

    db.set_boss_setting(conn, "damage_average_runs", "2")  # a Crimson Queen setting
    damage.recompute_all(conn)
    assert db.get_character(conn, "Alpha")["dmg"] == 3.5


def test_reupload_is_duplicate_but_fills_new_characters(conn):
    text = make_log("27-09-2026 01:00:00", "27-09-2026 01:27:30", {"Alpha": 2_000_000_000, "Charlie": 4_000_000_000})
    first = damage.record_log(conn, damage.parse_log(text, SYDNEY), message_id=1)
    assert first.unknown == ["Charlie"]

    again = damage.record_log(conn, damage.parse_log(text, SYDNEY), message_id=2)
    assert again.duplicate

    player = db.get_player_by_handle(conn, "@tester")
    db.add_character(conn, player.id, "Charlie", "BM", None)
    third = damage.record_log(conn, damage.parse_log(text, SYDNEY), message_id=3)
    assert not third.duplicate and [u[0] for u in third.updated] == ["Charlie"]
    assert conn.execute("SELECT COUNT(*) FROM damage_logs").fetchone()[0] == 1


def test_deleting_upload_reverts_damage(conn):
    for i, dmg in enumerate([2, 4]):
        text = make_log(f"0{i + 1}-09-2026 01:00:00", f"0{i + 1}-09-2026 01:27:30", {"Alpha": dmg * 1_000_000_000})
        damage.record_log(conn, damage.parse_log(text, SYDNEY), message_id=500 + i)
    assert db.get_character(conn, "Alpha")["dmg"] == 3.0
    assert damage.delete_logs_for_message(conn, 501) == ["Alpha"]
    assert db.get_character(conn, "Alpha")["dmg"] == 2.0
    damage.delete_logs_for_message(conn, 500)
    assert db.get_character(conn, "Alpha")["dmg"] == 1.0  # back to the value entered by hand
    assert damage.delete_logs_for_message(conn, 999) is None


def test_roster_import_keeps_logged_damage(conn):
    damage.record_log(
        conn,
        damage.parse_log(make_log("27-09-2026 01:00:00", "27-09-2026 01:27:30", {"Alpha": 5_000_000_000}), SYDNEY),
    )
    csv_text = "IGN,Name,Discord,Dmg/27.5\nAlpha,Tester,@tester,1.5\nBravo,Tester,@tester,2.5\n"
    importer.import_roster(conn, csv_text)
    assert db.get_character(conn, "Alpha")["dmg"] == 5.0  # from the log, not the sheet
    assert db.get_character(conn, "Bravo")["dmg"] == 2.5  # no logs, so the sheet wins


@pytest.mark.parametrize("name", sorted(p.name for p in (ROOT / "bot_data").glob("*_*_*_*.txt")))
def test_real_example_logs_parse(name):
    log = damage.parse_log((ROOT / "bot_data" / name).read_text(), SYDNEY)
    assert 10 < log.minutes < 40 and len(log.damage) >= 1


def test_manual_dmg_edit_does_not_override_logs(conn):
    alpha = db.get_character(conn, "Alpha")
    damage.record_log(
        conn,
        damage.parse_log(make_log("27-09-2026 01:00:00", "27-09-2026 01:27:30", {"Alpha": 5_000_000_000}), SYDNEY),
        message_id=1,
    )
    db.update_character(conn, alpha["id"], dmg=9.9, job="BM")
    row = db.get_character(conn, "Alpha")
    assert (row["dmg"], row["base_dmg"], row["job"]) == (5.0, 9.9, "BM")
    damage.delete_logs_for_message(conn, 1)
    assert db.get_character(conn, "Alpha")["dmg"] == 9.9
    db.update_character(conn, db.get_character(conn, "Bravo")["id"], dmg=2.2)
    assert db.get_character(conn, "Bravo")["dmg"] == 2.2
