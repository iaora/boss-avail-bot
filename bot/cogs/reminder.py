"""Posts the weekly availability reminders on the host-configured schedule."""

from __future__ import annotations

import logging

import discord
from discord.ext import commands, tasks

from .. import db, reminders, timeutil
from ..app import MonkeyBot
from ..views import send_reminder

log = logging.getLogger(__name__)

LAST_SLOT = "last_reminder_slot"


def mark_latest_due_as_sent(bot: MonkeyBot) -> None:
    """Record the most recent reminder slot that has passed as already posted."""
    now = timeutil.now_utc()
    week = timeutil.upcoming_week_start(now, bot.tz)
    settings = reminders.load(bot.conn, bot.config.cq_channel_id)
    due = settings.latest_due(week, now, bot.tz)
    db.set_setting(bot.conn, LAST_SLOT, reminders.slot_key(due) if due else "")


class ReminderCog(commands.Cog):
    def __init__(self, bot: MonkeyBot):
        self.bot = bot
        self.check_reminder.start()

    async def cog_unload(self) -> None:
        self.check_reminder.cancel()

    @tasks.loop(minutes=1)
    async def check_reminder(self) -> None:
        conn = self.bot.conn
        settings = reminders.load(conn, self.bot.config.cq_channel_id)
        if not settings.enabled or not settings.channel_id:
            return

        now = timeutil.now_utc()
        week = timeutil.upcoming_week_start(now, self.bot.tz)
        due = settings.latest_due(week, now, self.bot.tz)
        if due is None or db.get_setting(conn, LAST_SLOT) == reminders.slot_key(due):
            return

        channel = self.bot.get_channel(settings.channel_id)
        if not isinstance(channel, discord.abc.Messageable):
            log.warning("Reminder channel %s not found or not a text channel", settings.channel_id)
            return
        await send_reminder(self.bot, channel, week)
        db.set_setting(conn, LAST_SLOT, reminders.slot_key(due))
        log.info("Posted availability reminder for %s (slot %s)", week, due)

    @check_reminder.before_loop
    async def before_check(self) -> None:
        await self.bot.wait_until_ready()
        # On a fresh install, don't post a reminder whose time passed before the bot existed;
        # start with the next one. After that, if the bot was down when a reminder was due,
        # the most recent missed one is posted as soon as it comes back up.
        if db.get_setting(self.bot.conn, LAST_SLOT) is None:
            mark_latest_due_as_sent(self.bot)

    @check_reminder.error
    async def on_error(self, error: BaseException) -> None:
        log.exception("Reminder loop failed; restarting", exc_info=error)
        self.check_reminder.restart()


async def setup(bot: MonkeyBot) -> None:
    await bot.add_cog(ReminderCog(bot))
