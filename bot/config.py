"""Runtime configuration, loaded from environment variables and the environment's .env file.

There are two environments, each its own Discord bot with its own settings file:
  prod  .env.prod  the bot in the official server
  test  .env.test  a separate test bot in a test server, for trying out changes
Entry points call load_environment() first; Config.from_env() then reads the variables.

Every path is resolved relative to the project root unless given as an absolute path,
so the bot behaves the same on a laptop and on an EC2 instance.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import dotenv_values, load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent

ENVIRONMENTS = ("prod", "test")
# Each environment keeps its own database, so test clicks never change real players' data.
DEFAULT_DATABASE_PATHS = {"prod": "data/monkey_inc.db", "test": "data/test/monkey_inc.db"}
# Class icons are shared: both bots upload the same images (one per class, named after the job),
# so this folder is not a per-environment setting.
CLASS_ICONS_DIR = PROJECT_ROOT / "bot_data" / "class_icons"


def env_file(environment: str, root: Path = PROJECT_ROOT) -> Path:
    return root / f".env.{environment}"


def _database_path(values: dict, environment: str) -> Path:
    return _path((values.get("DATABASE_PATH") or "").strip() or DEFAULT_DATABASE_PATHS[environment])


def load_environment(environment: str, root: Path = PROJECT_ROOT) -> None:
    """Load .env.<environment> into os.environ (variables already set in the shell win) and
    record the environment in BOT_ENV. Exits with a message if the file is missing, or if prod
    and test share a bot token (two programs on one token both answer every button press) or
    a database."""
    if environment not in ENVIRONMENTS:
        raise SystemExit(f"Unknown environment {environment!r}: use one of {', '.join(ENVIRONMENTS)}.")
    path = env_file(environment, root)
    if not path.exists():
        hint = " (to keep your old .env as production: mv .env .env.prod)" if (root / ".env").exists() else ""
        raise SystemExit(f"{path.name} not found. Copy .env.example to {path.name} and fill it in{hint}.")

    values = dotenv_values(path)
    for other in ENVIRONMENTS:
        other_path = env_file(other, root)
        if other == environment or not other_path.exists():
            continue
        other_values = dotenv_values(other_path)
        token = (values.get("DISCORD_TOKEN") or "").strip()
        if token and token == (other_values.get("DISCORD_TOKEN") or "").strip():
            raise SystemExit(
                f"{path.name} and {other_path.name} have the same DISCORD_TOKEN. "
                "Each environment needs its own Discord bot."
            )
        if _database_path(values, environment) == _database_path(other_values, other):
            raise SystemExit(
                f"{path.name} and {other_path.name} use the same DATABASE_PATH. "
                "Remove DATABASE_PATH from both to use the defaults "
                f"({DEFAULT_DATABASE_PATHS['prod']} and {DEFAULT_DATABASE_PATHS['test']})."
            )

    load_dotenv(path)
    os.environ["BOT_ENV"] = environment


def _path(value: str) -> Path:
    p = Path(value).expanduser()
    return p if p.is_absolute() else PROJECT_ROOT / p


def _roster_contact(raw: str) -> str:
    """ROSTER_CONTACT_ID as shown to players. A Discord user ID (just the digits) becomes a clickable
    mention; a mention already written as <@ID> is kept; anything else is a plain name."""
    value = raw.strip()
    if value.isdigit():
        return f"<@{value}>"
    return value or "a host"


def _optional_int(name: str) -> int | None:
    raw = os.getenv(name, "").strip()
    return int(raw) if raw else None


@dataclass(frozen=True)
class Config:
    environment: str  # "prod" or "test"
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
    roster_contact: str  # who players message to add characters, ready to display (see _roster_contact)
    log_level: str

    @classmethod
    def from_env(cls) -> "Config":
        environment = os.getenv("BOT_ENV", "prod")
        return cls(
            environment=environment,
            discord_token=os.getenv("DISCORD_TOKEN", "").strip(),
            guild_id=_optional_int("GUILD_ID"),
            host_role_id=_optional_int("HOST_ROLE_ID"),
            player_role_id=_optional_int("PLAYER_ROLE_ID"),
            cq_channel_id=_optional_int("CQ_CHANNEL_ID"),
            queen_logs_channel_id=_optional_int("QUEEN_LOGS_CHANNEL_ID"),
            timezone=ZoneInfo(os.getenv("BOT_TIMEZONE", "America/New_York")),
            log_timezone=ZoneInfo(os.getenv("LOG_TIMEZONE", "Australia/Sydney")),
            database_path=_path(os.getenv("DATABASE_PATH") or DEFAULT_DATABASE_PATHS.get(environment, "data/monkey_inc.db")),
            seed_roster_csv=_path(os.getenv("SEED_ROSTER_CSV", "bot_data/Monkey, Inc.  - CQ Roster.csv")),
            seed_squad_timings=_path(os.getenv("SEED_SQUAD_TIMINGS", "bot_data/squad_timings.txt")),
            class_icons_dir=CLASS_ICONS_DIR,
            # ROSTER_CONTACT was the variable's earlier name; still read so older .env files work
            roster_contact=_roster_contact(os.getenv("ROSTER_CONTACT_ID") or os.getenv("ROSTER_CONTACT", "")),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        )
