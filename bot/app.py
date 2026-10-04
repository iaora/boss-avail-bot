"""The bot object: owns the config and database connection, loads cogs, syncs slash commands."""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from . import db, importer
from .class_icons import ClassIcons
from .config import Config

log = logging.getLogger(__name__)

# View Channels, Send Messages, Embed Links, Attach Files, Read Message History, Add Reactions
INVITE_PERMISSIONS = discord.Permissions(
    view_channel=True, send_messages=True, embed_links=True,
    attach_files=True, read_message_history=True, add_reactions=True,
).value

COGS = (
    "bot.cogs.player", "bot.cogs.host", "bot.cogs.reminder", "bot.cogs.damage_logs", "bot.cogs.class_icons",
    "bot.cogs.findatime",
)


class MonkeyBot(commands.Bot):
    def __init__(self, config: Config):
        intents = discord.Intents.default()
        # Needed to read file attachments posted in #queen-logs. Must also be switched on in the
        # Developer Portal (Bot > Privileged Gateway Intents > Message Content Intent).
        intents.message_content = True
        super().__init__(command_prefix=commands.when_mentioned, intents=intents)
        self.config = config
        self.conn = db.connect(config.database_path)
        self.class_icons = ClassIcons()
        self.tree.on_error = self.on_app_command_error

    @property
    def tz(self):
        return self.config.timezone

    async def setup_hook(self) -> None:
        importer.seed_if_empty(self.conn, self.config.seed_roster_csv, self.config.seed_squad_timings, self.tz)

        try:
            await self.class_icons.load(self)  # syncing with the icon folder runs in cogs/class_icons.py
        except discord.HTTPException:
            log.exception("Couldn't load class icons; showing class names instead")

        from .views import LayoutToggleButton, ReminderButton, SortToggleButton, SquadTimesButton

        self.add_dynamic_items(ReminderButton, SortToggleButton, LayoutToggleButton, SquadTimesButton)
        for cog in COGS:
            await self.load_extension(cog)

        if self.config.guild_id:
            guild = discord.Object(id=self.config.guild_id)
            self.tree.copy_global_to(guild=guild)
            try:
                synced = await self.tree.sync(guild=guild)
            except discord.Forbidden:
                app_id = self.application_id
                raise SystemExit(
                    f"Discord refused to register slash commands in server {self.config.guild_id} "
                    "(403 Missing Access).\nEither the bot isn't in that server, it was invited without the "
                    "'applications.commands' scope, or GUILD_ID is wrong.\nInvite it with:\n"
                    f"https://discord.com/oauth2/authorize?client_id={app_id}&scope=bot+applications.commands"
                    f"&permissions={INVITE_PERMISSIONS}&guild_id={self.config.guild_id}"
                ) from None
            log.info("Synced %d commands to guild %s", len(synced), self.config.guild_id)
        else:
            synced = await self.tree.sync()
            log.info("Synced %d global commands (may take up to an hour to appear)", len(synced))

    async def on_ready(self) -> None:
        log.info("Logged in as %s (id %s)", self.user, self.user.id if self.user else "?")

    # ------------------------------------------------------------------ helpers

    def resolve_player(self, user: discord.abc.User) -> db.Player | None:
        """Find the roster entry for a Discord user, linking by @username on first contact."""
        player = db.get_player_by_discord_id(self.conn, user.id)
        handle = f"@{user.name}"
        if player is None:
            player = db.get_player_by_handle(self.conn, handle)
            if player is None or player.discord_id is not None:
                return None
            db.link_discord(self.conn, player.id, user.id, handle)
            log.info("Linked %s to player %s", handle, player.name)
            return db.get_player(self.conn, player.id)
        if player.discord_handle != handle:
            db.link_discord(self.conn, player.id, user.id, handle)
            player = db.get_player(self.conn, player.id)
        return player

    def is_host(self, interaction: discord.Interaction) -> bool:
        return self.is_host_member(interaction.user)

    def is_host_member(self, member: discord.abc.User) -> bool:
        if not isinstance(member, discord.Member):
            return False
        if member.guild_permissions.manage_guild:
            return True
        role_id = self.config.host_role_id
        return role_id is not None and any(r.id == role_id for r in member.roles)

    async def on_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        if isinstance(error, app_commands.CheckFailure):
            message = "Only hosts can use this command."
        else:
            log.exception("Command error", exc_info=error)
            message = "Something went wrong running that command. The error has been logged."
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)

    async def close(self) -> None:
        await super().close()
        self.conn.close()
