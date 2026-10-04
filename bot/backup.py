"""Make a consistent copy of an environment's database: `python -m bot.backup prod [keep]`.

Uses SQLite's online backup API, so it is safe to run while the bot is running.
Backups go to data/backups/ next to the database; only the newest `keep` (default 14) are kept.
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime

from .config import ENVIRONMENTS, Config, load_environment


def backup(keep: int = 14) -> str:
    config = Config.from_env()
    source_path = config.database_path
    if not source_path.exists():
        raise SystemExit(f"No database at {source_path}")
    folder = source_path.parent / "backups"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{source_path.stem}-{datetime.now():%Y%m%d-%H%M%S}.db"

    with sqlite3.connect(source_path) as src, sqlite3.connect(target) as dst:
        src.backup(dst)

    for old in sorted(folder.glob(f"{source_path.stem}-*.db"))[:-keep]:
        old.unlink()
    return str(target)


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in ENVIRONMENTS:
        raise SystemExit(f"Usage: python -m bot.backup {{{'|'.join(ENVIRONMENTS)}}} [keep]")
    load_environment(sys.argv[1])
    print(backup(int(sys.argv[2]) if len(sys.argv) > 2 else 14))
