"""Import the roster CSV (a Google Sheet export) and the squad timings file.

The CSV is matched by column header name, not position, so the sheet can gain extra
columns without breaking the import. Required headers: IGN, Name, Discord. Optional:
Job, Squad, Dmg/27.5 (or Dmg), Run time, Perm, Status, and "Squad 1" ... "Squad N".
A Buff column is ignored: the buff always comes from the job (db.JOB_BUFFS).
"""

from __future__ import annotations

import csv
import io
import logging
import re
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from . import db, timeutil

BOSS = db.CQ  # the roster sheet and squad timings are Crimson Queen's

log = logging.getLogger(__name__)

SQUAD_COLUMN = re.compile(r"^Squad (\d+)$", re.IGNORECASE)
MENTION = re.compile(r"^<@!?(\d+)>$")
TIMING_LINE = re.compile(r"Squad\s+(\d+)\s*:\s*<t:(\d+)(?::\w)?>", re.IGNORECASE)

# Sheet values -> bot levels. The sheet already uses the bot's three levels.
LEVEL_ALIASES = {level.lower(): level for level in db.LEVELS}
LEVEL_RANK = {level: i for i, level in enumerate(db.LEVELS)}  # lower = more available


@dataclass
class ImportResult:
    players_created: int = 0
    characters_created: int = 0
    characters_updated: int = 0
    skipped_rows: list[str] = field(default_factory=list)
    missing_from_csv: list[str] = field(default_factory=list)

    def summary(self) -> str:
        lines = [
            f"Players created: {self.players_created}",
            f"Characters created: {self.characters_created}",
            f"Characters updated: {self.characters_updated}",
        ]
        if self.skipped_rows:
            lines.append(f"Skipped rows ({len(self.skipped_rows)}): " + ", ".join(self.skipped_rows[:15]))
        if self.missing_from_csv:
            lines.append(
                f"Characters in the bot but not in this CSV ({len(self.missing_from_csv)}, left unchanged): "
                + ", ".join(self.missing_from_csv[:15])
                + (" ..." if len(self.missing_from_csv) > 15 else "")
            )
        return "\n".join(lines)


def _header_index(header: list[str]) -> dict[str, int]:
    """Map header name -> first column index (the sheet repeats some headers in helper columns)."""
    index: dict[str, int] = {}
    for i, name in enumerate(header):
        key = name.strip().lower()
        if key and key not in index:
            index[key] = i
    return index


def _float(value: str) -> float | None:
    try:
        return float(value.strip())
    except (ValueError, AttributeError):
        return None


def _squad_number(value: str) -> int | None:
    m = re.search(r"(\d+)", value or "")
    return int(m.group(1)) if m else None


def _mode_level(levels: list[str]) -> str:
    counts = Counter(levels)
    return min(counts, key=lambda lvl: (-counts[lvl], LEVEL_RANK[lvl]))


def import_roster(conn: sqlite3.Connection, csv_text: str) -> ImportResult:
    """Create/update players and characters from the roster CSV.

    - New players are created as 'active' and get their default availability from the
      CSV: per squad, the most common level across their characters. Characters that
      differ from that get a per-character override.
    - Existing players keep their status and bot-managed availability; only character
      details (job, buff, dmg, run time, squad, slot status, perm) are refreshed.
    - A character's dmg is not overwritten once it has runs from uploaded damage logs.
    """
    rows = list(csv.reader(io.StringIO(csv_text.lstrip("﻿"))))
    if not rows:
        raise ValueError("The CSV is empty.")
    header, body = rows[0], rows[1:]
    idx = _header_index(header)
    for required in ("ign", "name", "discord"):
        if required not in idx:
            raise ValueError(f"The CSV is missing the '{required.upper()}' column.")
    squad_cols = {int(m.group(1)): i for i, h in enumerate(header) if (m := SQUAD_COLUMN.match(h.strip()))}

    def cell(row: list[str], name: str) -> str:
        i = idx.get(name)
        return row[i].strip() if i is not None and i < len(row) else ""

    result = ImportResult()
    new_player_ids: set[int] = set()
    char_levels: dict[int, dict[int, str]] = {}  # character id -> squad -> level (new players only)
    seen_igns: set[str] = set()

    for line_no, row in enumerate(body, start=2):
        ign = cell(row, "ign")
        if not ign:
            continue
        discord = cell(row, "discord")
        if not discord:
            result.skipped_rows.append(f"{ign} (row {line_no}, no Discord)")
            continue
        seen_igns.add(ign.lower())

        player = _find_or_create_player(conn, discord, cell(row, "name") or ign, result, new_player_ids)

        squad = _squad_number(cell(row, "squad"))
        values = dict(
            player_id=player.id,
            job=cell(row, "job").upper(),
            buff=db.buff_for_job(cell(row, "job")),  # from the job; the sheet's Buff column isn't used
            # Sheet damage is already scaled to a 27.5-minute run (billions), the same scale as the
            # damage logs, so it is stored as-is. Dmg/27.5 is preferred; Dmg is the fallback.
            dmg=_float(cell(row, "dmg/27.5")) or _float(cell(row, "dmg")),
            run_time=_float(cell(row, "run time")),
            squad=squad,
            slot_status=cell(row, "status") or ("Slotted" if squad else "Available"),
            perm=1 if cell(row, "perm").upper() == "TRUE" else 0,
        )
        existing = db.get_character(conn, ign, boss_id=BOSS)
        with conn:
            if existing:
                char_id = existing["id"]
                conn.execute(
                    "UPDATE characters SET player_id = :player_id, job = :job, buff = :buff WHERE id = :id",
                    {**values, "id": char_id},
                )
                result.characters_updated += 1
            else:
                char_id = conn.execute(
                    "INSERT INTO characters (player_id, ign, job, buff) VALUES (:player_id, :ign, :job, :buff)",
                    {**values, "ign": ign},
                ).lastrowid
                result.characters_created += 1
            # The sheet is Crimson Queen's roster: its damage, squad, slot status and Perm are CQ data.
            conn.execute(
                "INSERT INTO character_boss "
                "(character_id, boss_id, status, dmg, base_dmg, run_time, squad, slot_status, perm) "
                # a character new to this boss starts as static if the sheet marks it Perm, else sub;
                # after that the status belongs to the player and re-imports don't change it
                "VALUES (:id, :boss, CASE WHEN :perm = 1 THEN 'static' ELSE 'sub' END, :dmg, :dmg, :run_time, "
                ":squad, :slot_status, :perm) "
                "ON CONFLICT (character_id, boss_id) DO UPDATE SET "
                "run_time = excluded.run_time, squad = excluded.squad, slot_status = excluded.slot_status, "
                "perm = excluded.perm, base_dmg = excluded.base_dmg, "
                # damage from uploaded logs wins over the sheet
                "dmg = CASE WHEN EXISTS (SELECT 1 FROM damage_runs r JOIN damage_logs l ON l.id = r.log_id "
                "WHERE r.character_id = :id AND l.boss_id = :boss) THEN character_boss.dmg ELSE excluded.dmg END",
                {**values, "id": char_id, "boss": BOSS},
            )

        if player.id in new_player_ids:
            levels = {}
            for number, col in squad_cols.items():
                raw = row[col].strip().lower() if col < len(row) else ""
                if raw in LEVEL_ALIASES:
                    levels[number] = LEVEL_ALIASES[raw]
            char_levels[char_id] = levels

    _derive_default_availability(conn, new_player_ids, char_levels)

    for r in conn.execute("SELECT ign FROM characters ORDER BY ign COLLATE NOCASE"):
        if r["ign"].lower() not in seen_igns:
            result.missing_from_csv.append(r["ign"])
    return result


def _find_or_create_player(
    conn: sqlite3.Connection, discord: str, name: str, result: ImportResult, new_ids: set[int]
) -> db.Player:
    """The sheet row's player. A player new to this boss's roster (new to the bot, or known only for
    another boss) joins it as active and gets their default availability from the sheet."""
    m = MENTION.match(discord)
    if m:
        discord_id = int(m.group(1))
        player = db.get_player_by_discord_id(conn, discord_id, boss_id=BOSS)
        if player is None:
            player = db.create_player(conn, name=name, discord_id=discord_id, boss_id=BOSS)
            result.players_created += 1
            new_ids.add(player.id)
    else:
        handle = discord if discord.startswith("@") else f"@{discord}"
        player = db.get_player_by_handle(conn, handle, boss_id=BOSS)
        if player is None:
            player = db.create_player(conn, name=name, discord_handle=handle, boss_id=BOSS)
            result.players_created += 1
            new_ids.add(player.id)
    on_roster = conn.execute(
        "SELECT 1 FROM player_boss WHERE player_id = ? AND boss_id = ?", (player.id, BOSS)
    ).fetchone()
    if not on_roster:
        db.set_player_status(conn, player.id, "active", boss_id=BOSS)
        new_ids.add(player.id)
        player = db.get_player(conn, player.id, boss_id=BOSS)
    return player


def _derive_default_availability(
    conn: sqlite3.Connection, player_ids: set[int], char_levels: dict[int, dict[int, str]]
) -> None:
    by_player: dict[int, list[int]] = defaultdict(list)
    for char_id in char_levels:
        pid = conn.execute("SELECT player_id FROM characters WHERE id = ?", (char_id,)).fetchone()[0]
        if pid in player_ids:
            by_player[pid].append(char_id)

    for pid, char_ids in by_player.items():
        squads = sorted({s for c in char_ids for s in char_levels[c]})
        default = {s: _mode_level([char_levels[c][s] for c in char_ids if s in char_levels[c]]) for s in squads}
        db.set_default_availability(conn, pid, default, boss_id=BOSS)
        for c in char_ids:
            overrides = {s: lvl for s, lvl in char_levels[c].items() if default.get(s) != lvl}
            if overrides:
                db.set_character_overrides(conn, c, overrides, boss_id=BOSS)


def parse_squad_timings(text: str, tz: ZoneInfo) -> dict[int, tuple[int, str]]:
    """Parse lines like '**Squad 1: <t:1789920000:F>**' into {squad: (weekday, 'HH:MM')} in `tz`."""
    template = {}
    for m in TIMING_LINE.finditer(text):
        local = datetime.fromtimestamp(int(m.group(2)), timezone.utc).astimezone(tz)
        template[int(m.group(1))] = (timeutil.sunday_index(local.date()), local.strftime("%H:%M"))
    return template


def seed_if_empty(conn: sqlite3.Connection, roster_csv: Path, squad_timings: Path, tz: ZoneInfo) -> None:
    """First-run bootstrap: load the squad template and roster if the database is empty."""
    if not db.squad_numbers(conn, boss_id=BOSS):
        if squad_timings.exists():
            template = parse_squad_timings(squad_timings.read_text(encoding="utf-8"), tz)
            for number, (weekday, hhmm) in template.items():
                db.set_squad_template(conn, number, weekday, hhmm, boss_id=BOSS)
            log.info("Seeded %d squads from %s", len(template), squad_timings)
        else:
            log.warning("No squads configured and %s not found; hosts can add squads with /cq_config > Squad time > Every week", squad_timings)

    if conn.execute("SELECT COUNT(*) FROM players").fetchone()[0] == 0:
        if roster_csv.exists():
            result = import_roster(conn, roster_csv.read_text(encoding="utf-8-sig"))
            log.info("Seeded roster from %s\n%s", roster_csv, result.summary())
        else:
            log.warning("No players and %s not found; hosts can load one with /cq_config > Import roster", roster_csv)
