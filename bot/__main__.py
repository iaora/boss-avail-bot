"""Entry point: `python -m bot prod` or `python -m bot test` (see config.py for the environments)."""

from __future__ import annotations

import argparse
import fcntl
import logging
import sys
from typing import IO

from .app import MonkeyBot
from .config import ENVIRONMENTS, Config, load_environment


def lock_environment(config: Config) -> IO:
    """Hold an exclusive lock next to the database for as long as the bot runs, so the same
    environment can't be started twice (two copies on one token both answer every button)."""
    lock_path = config.database_path.with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_path, "w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit(f"The {config.environment} bot is already running (lock: {lock_path}).")
    return handle


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m bot", description="Run the Monkey Inc Discord bot.")
    parser.add_argument("environment", choices=ENVIRONMENTS, help="prod (.env.prod) or test (.env.test)")
    args = parser.parse_args()

    load_environment(args.environment)
    config = Config.from_env()
    logging.basicConfig(
        level=config.log_level,
        format=f"%(asctime)s [{config.environment}] %(levelname)-8s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    if not config.discord_token:
        sys.exit(f"DISCORD_TOKEN is not set in .env.{config.environment}.")
    lock = lock_environment(config)  # noqa: F841 -- held until the process exits

    bot = MonkeyBot(config)
    bot.run(config.discord_token, log_handler=None)


if __name__ == "__main__":
    main()
