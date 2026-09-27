"""Entry point: `python -m bot`."""

from __future__ import annotations

import logging
import sys

from .app import MonkeyBot
from .config import Config


def main() -> None:
    config = Config.from_env()
    logging.basicConfig(
        level=config.log_level,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    if not config.discord_token:
        sys.exit("DISCORD_TOKEN is not set. Copy .env.example to .env and fill it in.")
    bot = MonkeyBot(config)
    bot.run(config.discord_token, log_handler=None)


if __name__ == "__main__":
    main()
