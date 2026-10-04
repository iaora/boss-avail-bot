"""Watches #queen-logs for damage log .txt files posted by hosts and updates character damage."""

from __future__ import annotations

import logging

import discord
from discord.ext import commands

from .. import damage, db
from ..app import MonkeyBot

log = logging.getLogger(__name__)

DEFAULT_CHANNEL_NAME = "queen-logs"
MAX_LOG_BYTES = 1_000_000


def logs_channel_id(bot: MonkeyBot) -> int | None:
    configured = db.get_boss_setting(bot.conn, "queen_logs_channel_id")  # Crimson Queen's log channel
    return int(configured) if configured else bot.config.queen_logs_channel_id


def is_logs_channel(bot: MonkeyBot, channel: discord.abc.GuildChannel | discord.Thread) -> bool:
    channel_id = logs_channel_id(bot)
    if channel_id:
        return channel.id == channel_id
    return getattr(channel, "name", None) == DEFAULT_CHANNEL_NAME


class DamageLogCog(commands.Cog):
    def __init__(self, bot: MonkeyBot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.guild is None or not is_logs_channel(self.bot, message.channel):
            return
        attachments = [a for a in message.attachments if a.filename.lower().endswith(".txt")]
        if not attachments or not self.bot.is_host_member(message.author):
            return

        problems: list[str] = []
        unknown: list[str] = []
        processed = 0
        for attachment in attachments:
            if attachment.size > MAX_LOG_BYTES:
                problems.append(f"**{attachment.filename}**: file is too large to be a damage log.")
                continue
            try:
                text = (await attachment.read()).decode("utf-8", errors="replace")
                parsed = damage.parse_log(text, self.bot.config.log_timezone)
            except ValueError as e:
                problems.append(f"**{attachment.filename}**: not processed, {e}.")
                continue

            result = damage.record_log(
                self.bot.conn,
                parsed,
                message_id=message.id,
                filename=attachment.filename,
                uploaded_by=message.author.id,
            )
            log.info(
                "Damage log %s: %d characters updated, unknown: %s, duplicate: %s",
                attachment.filename, len(result.updated), result.unknown, result.duplicate,
            )
            if result.duplicate:
                problems.append(f"**{attachment.filename}**: this run was already uploaded, nothing new to save.")
            else:
                processed += 1
            if result.unknown:
                unknown.append(
                    f"**{attachment.filename}**: {', '.join(f'`{n}`' for n in result.unknown)}"
                )

        if processed:
            await message.add_reaction("✅")
        if unknown:
            # Everyone else in the log was saved; flag the names that couldn't be matched to the host.
            await message.add_reaction("⚠️")
            problems.append(
                "⚠️ These names aren't on the roster, so their damage was **not** saved "
                "(everyone else in the log was):\n"
                + "\n".join(unknown)
                + "\nCheck the spelling, or add the character (`/character` > Add or `/cq_config` > Import roster), "
                "then post the same log again. Only the missing characters will be added."
            )
        if problems:
            await message.reply("\n".join(problems)[:2000], mention_author=bool(unknown))

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        removed = damage.delete_logs_for_message(self.bot.conn, payload.message_id)
        if removed is None:
            return
        log.info("Damage log in message %s deleted; recomputed %d characters", payload.message_id, len(removed))
        channel = self.bot.get_channel(payload.channel_id)
        if isinstance(channel, discord.abc.Messageable):
            await channel.send(
                f"🗑️ A damage log upload was deleted, so its run was removed and damage was recalculated for "
                f"{len(removed)} character(s)."
            )


async def setup(bot: MonkeyBot) -> None:
    await bot.add_cog(DamageLogCog(bot))
