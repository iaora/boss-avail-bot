"""Make a consistent copy of the database: `python -m bot.backup [keep]`.

Uses SQLite's online backup API, so it is safe to run while the bot is running.
Backups go to data/backups/ next to the database; only the newest `keep` (default 14) are kept.
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime

from .config import Config


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
    print(backup(int(sys.argv[1]) if len(sys.argv) > 1 else 14))
