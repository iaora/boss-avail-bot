"""Player-facing slash commands, all under /cq:

  /cq availability   your CQ availability; No change, Update a week, Update my default and
                     Character availability are buttons on it
  /cq characters     your characters; Set character status, Character availability and
                     Damage history are buttons on it

Adding or changing players and characters is host-only (see /player and /character).
"""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from .. import db, timeutil
from ..app import MonkeyBot
from ..views import (
    EMBED_COLOR,
    NOT_REGISTERED,
    CharacterStatusEditor,
    DamageHistoryPicker,
    ReminderButton,
    character_status_label,
    send_player_summary,
)


class PlayerCommands:
    """Player command helpers: finding the caller on the roster."""

    def __init__(self, bot: MonkeyBot):
        self.bot = bot
        super().__init__()

    def _week(self):
        return timeutil.upcoming_week_start(timeutil.now_utc(), self.bot.tz)

    async def _player(self, interaction: discord.Interaction) -> db.Player | None:
        player = self.bot.resolve_player(interaction.user)
        if player is None:
            await interaction.response.send_message(NOT_REGISTERED, ephemeral=True)
        return player


# --------------------------------------------------------------------------- /cq


class CqCog(PlayerCommands, commands.GroupCog, group_name="cq", group_description="Your CQ availability and characters"):
    @app_commands.command(name="availability", description="Your availability for next week and your check-in status")
    async def availability(self, interaction: discord.Interaction):
        if player := await self._player(interaction):
            await send_player_summary(interaction, self.bot, player, self._week())

    @app_commands.command(name="characters", description="List your characters and set their status")
    async def characters(self, interaction: discord.Interaction):
        if not (player := await self._player(interaction)):
            return
        chars = db.list_characters(self.bot.conn, player.id)
        lines = []
        for c in chars:
            squad = f"Squad {c['squad']}" if c["squad"] else (c["slot_status"] or "Unslotted")
            overrides = db.get_character_overrides(self.bot.conn, c["id"])
            extra = f" · {len(overrides)} availability override(s)" if overrides else ""
            dmg = f"{c['dmg']:g}" if c["dmg"] is not None else "?"
            lines.append(
                f"{character_status_label(c['status'])} · **{c['ign']}** · {c['job'] or '?'} · {c['buff'] or '?'} "
                f"· dmg {dmg} · {squad}{extra}"
            )
        embed = discord.Embed(
            title=f"{player.name}'s characters ({len(chars)})",
            description="\n".join(lines)[:4000] or "*No characters yet. Ask a host to add them.*",
            color=EMBED_COLOR,
        )
        embed.set_footer(text="⭐ Static: prioritize · ⏳ Flex: only if needed · 💤 Inactive: set by a host, not slotted")
        view = discord.ui.View(timeout=900)
        if chars:
            button = discord.ui.Button(label="Set character status", style=discord.ButtonStyle.primary, emoji="⭐")

            async def open_editor(button_interaction: discord.Interaction) -> None:
                await CharacterStatusEditor(self.bot, player).send(button_interaction)

            button.callback = open_editor
            view.add_item(button)
            # same button as in /cq availability: pick a character, then edit its own availability
            view.add_item(ReminderButton("chars", self._week().isoformat()))
            history = discord.ui.Button(label="Damage history", style=discord.ButtonStyle.secondary, emoji="📈")

            async def open_history(button_interaction: discord.Interaction) -> None:
                await DamageHistoryPicker(self.bot, player).send(button_interaction)

            history.callback = open_history
            view.add_item(history)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


async def setup(bot: MonkeyBot) -> None:
    await bot.add_cog(CqCog(bot))
