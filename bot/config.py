"""Runtime configuration, loaded from environment variables (and an optional .env file).

Every path is resolved relative to the project root unless given as an absolute path,
so the bot behaves the same on a laptop and on an EC2 instance.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent

load_dotenv(PROJECT_ROOT / ".env")


def _path(value: str) -> Path:
    p = Path(value).expanduser()
    return p if p.is_absolute() else PROJECT_ROOT / p


def _optional_int(name: str) -> int | None:
    raw = os.getenv(name, "").strip()
    return int(raw) if raw else None


@dataclass(frozen=True)
class Config:
    discord_token: str
    guild_id: int | None
    host_role_id: int | None
    player_role_id: int | None
    cq_channel_id: int | None
    queen_logs_channel_id: int | None
    timezone: ZoneInfo
    log_timezone: ZoneInfo
    database_path: Path
    seed_roster_csv: Path
    seed_squad_timings: Path
    class_icons_dir: Path
    roster_contact: str  # who players message to add characters: a name or a <@id> mention
    log_level: str

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            discord_token=os.getenv("DISCORD_TOKEN", "").strip(),
            guild_id=_optional_int("GUILD_ID"),
            host_role_id=_optional_int("HOST_ROLE_ID"),
            player_role_id=_optional_int("PLAYER_ROLE_ID"),
            cq_channel_id=_optional_int("CQ_CHANNEL_ID"),
            queen_logs_channel_id=_optional_int("QUEEN_LOGS_CHANNEL_ID"),
            timezone=ZoneInfo(os.getenv("BOT_TIMEZONE", "America/New_York")),
            log_timezone=ZoneInfo(os.getenv("LOG_TIMEZONE", "Australia/Sydney")),
            database_path=_path(os.getenv("DATABASE_PATH", "data/monkey_inc.db")),
            seed_roster_csv=_path(os.getenv("SEED_ROSTER_CSV", "bot_data/Monkey, Inc.  - CQ Roster.csv")),
            seed_squad_timings=_path(os.getenv("SEED_SQUAD_TIMINGS", "bot_data/squad_timings.txt")),
            class_icons_dir=_path(os.getenv("CLASS_ICONS_DIR", "bot_data/class_icons")),
            roster_contact=os.getenv("ROSTER_CONTACT", "").strip() or "a host",
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        )
