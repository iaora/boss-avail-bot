"""Keeps the bot's class icons in step with the bot_data/class_icons/ folder, in the background."""

from __future__ import annotations

import logging

import discord
from discord.ext import commands, tasks

from ..app import MonkeyBot

log = logging.getLogger(__name__)


class ClassIconCog(commands.Cog):
    def __init__(self, bot: MonkeyBot):
        self.bot = bot
        self.sync_icons.start()

    async def cog_unload(self) -> None:
        self.sync_icons.cancel()

    @tasks.loop(minutes=10)
    async def sync_icons(self) -> None:
        await self.bot.class_icons.sync(self.bot, self.bot.config.class_icons_dir)

    @sync_icons.before_loop
    async def before_sync(self) -> None:
        await self.bot.wait_until_ready()

    @sync_icons.error
    async def on_error(self, error: BaseException) -> None:
        if isinstance(error, discord.HTTPException):
            log.warning("Class icon sync failed: %s; will retry", error)
        else:
            log.exception("Class icon sync failed; will retry", exc_info=error)


async def setup(bot: MonkeyBot) -> None:
    await bot.add_cog(ClassIconCog(bot))
