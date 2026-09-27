"""Player-facing slash commands, in two groups:

  /cq  availability   (your CQ availability; No change, Update a week and Update my default
       are buttons on it)
  /my  characters   (your characters; per-character availability is the Character availability
       button in /cq availability)

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
    character_status_label,
    send_player_summary,
)


class PlayerCommands:
    """Shared by the player groups: finding the caller on the roster."""

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


class CqCog(PlayerCommands, commands.GroupCog, group_name="cq", group_description="Your CQ availability"):
    @app_commands.command(name="availability", description="Your availability for next week and your check-in status")
    async def availability(self, interaction: discord.Interaction):
        if player := await self._player(interaction):
            await send_player_summary(interaction, self.bot, player, self._week())


# --------------------------------------------------------------------------- /my


class MyCog(PlayerCommands, commands.GroupCog, group_name="my", group_description="Your characters"):
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
        embed.set_footer(text="⭐ Static: prioritize · ⏳ Sub: only if needed · 💤 Inactive: don't slot")
        view = discord.ui.View(timeout=900)
        if chars:
            button = discord.ui.Button(label="Set character status", style=discord.ButtonStyle.primary, emoji="⭐")

            async def open_editor(button_interaction: discord.Interaction) -> None:
                await CharacterStatusEditor(self.bot, player).send(button_interaction)

            button.callback = open_editor
            view.add_item(button)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


async def setup(bot: MonkeyBot) -> None:
    await bot.add_cog(CqCog(bot))
    await bot.add_cog(MyCog(bot))
