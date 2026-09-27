"""Host-only slash commands, in four groups:

  /host       availability, roster import, squad times, damage history, reminders
  /config     settings: reminders, damage logs, squad schedule
  /player     add, edit, link
  /character  add, edit, remove

A host is anyone with the role in HOST_ROLE_ID, or anyone with the Manage Server permission.
Each group is hidden from everyone without Manage Server; a server admin shows them to the
host role under Server Settings > Integrations (see host_guide.txt). HostOnly.interaction_check
enforces the host role on every command regardless of visibility.
"""

from __future__ import annotations

from datetime import date

import discord
from discord import app_commands
from discord.ext import commands

from .. import damage, db, importer, reminders, timeutil
from ..app import MonkeyBot
from ..squad_breakdown import SquadBreakdownView
from ..views import (
    EMBED_COLOR,
    LEVEL_EMOJI,
    OrderToggleView,
    availability_lines,
    character_status_label,
    confirmation_text,
    damage_history_embed,
    get_squad_order,
    order_squads,
    send_reminder,
)
from .damage_logs import DEFAULT_CHANNEL_NAME, logs_channel_id
from .reminder import mark_latest_due_as_sent

WEEK_CHOICES = [
    app_commands.Choice(name="Next week (upcoming)", value="upcoming"),
    app_commands.Choice(name="This week (current)", value="current"),
]
STATUS_CHOICES = [
    app_commands.Choice(name="Active", value="active"),
    app_commands.Choice(name="Inactive", value="inactive"),
]
BUFF_CHOICES = [app_commands.Choice(name=b, value=b) for b in db.BUFFS]
CHARACTER_STATUS_CHOICES = [
    app_commands.Choice(name="Static (prioritize)", value="static"),
    app_commands.Choice(name="Flex (only if needed)", value="sub"),
    app_commands.Choice(name="Inactive (don't slot)", value="inactive"),
]
STATUS_CHANGE_DAYS = 7  # how far back /host status lists character status changes
DAY_CHOICES = [app_commands.Choice(name=d, value=i) for i, d in enumerate(timeutil.WEEKDAYS)]


def _chunks(lines: list[str], limit: int = 1024) -> list[str]:
    """Split lines into embed-field-sized chunks."""
    chunks, current = [], ""
    for line in lines:
        if len(current) + len(line) + 1 > limit:
            chunks.append(current)
            current = ""
        current += line + "\n"
    if current:
        chunks.append(current)
    return chunks


def _valid_hhmm(value: str) -> str:
    t = timeutil.parse_hhmm(value)
    return t.strftime("%H:%M")


# --------------------------------------------------------------------------- shared


class HostOnly:
    """Shared by every host group: the host check and common helpers."""

    def __init__(self, bot: MonkeyBot):
        self.bot = bot
        super().__init__()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not self.bot.is_host(interaction):
            raise app_commands.CheckFailure("host only")
        return True

    def _week(self, which: app_commands.Choice[str] | None) -> date:
        now = timeutil.now_utc()
        if which and which.value == "current":
            return timeutil.current_week_start(now, self.bot.tz)
        return timeutil.upcoming_week_start(now, self.bot.tz)

    async def _get_player(self, interaction: discord.Interaction, value: str) -> db.Player | None:
        player = db.get_player(self.bot.conn, int(value)) if value.isdigit() else None
        if player is None:
            matches = db.search_players(self.bot.conn, value, limit=2)
            player = matches[0] if len(matches) == 1 else None
        if player is None:
            await interaction.response.send_message(
                f"Couldn't find a single player matching **{value}**. Pick one from the suggestions.", ephemeral=True
            )
        return player


async def player_autocomplete(interaction: discord.Interaction, current: str):
    conn = interaction.client.conn
    players = db.search_players(conn, current) if current else db.list_players(conn)[:25]
    return [app_commands.Choice(name=f"{p.display} [{p.status}]"[:100], value=str(p.id)) for p in players]


async def character_autocomplete(interaction: discord.Interaction, current: str):
    rows = interaction.client.conn.execute(
        "SELECT ign, job FROM characters WHERE ign LIKE ? ORDER BY ign COLLATE NOCASE LIMIT 25", (f"%{current}%",)
    ).fetchall()
    return [app_commands.Choice(name=f"{r['ign']} ({r['job'] or '?'})", value=r["ign"]) for r in rows]


async def job_autocomplete(interaction: discord.Interaction, current: str):
    jobs = db.known_jobs(interaction.client.conn)
    return [app_commands.Choice(name=j, value=j) for j in jobs if current.upper() in j][:25]


# --------------------------------------------------------------------------- /host


@app_commands.default_permissions(manage_guild=True)
@app_commands.guild_only()
class HostCog(HostOnly, commands.GroupCog, group_name="host", group_description="Host tools: availability, roster import, squads, damage, reminders"):
    @app_commands.command(name="status", description="How many active players have confirmed their availability")
    @app_commands.choices(week=WEEK_CHOICES)
    async def status(self, interaction: discord.Interaction, week: app_commands.Choice[str] | None = None):
        wk = self._week(week)
        confirmations = db.get_confirmations(self.bot.conn, wk)
        active = db.list_players(self.bot.conn, ("active",))
        no_change = sum(1 for p in active if confirmations.get(p.id) == "no_change")
        # Players who submitted a different schedule for this week
        updated = [p.display for p in active if confirmations.get(p.id) == "updated"]
        waiting = len(active) - no_change - len(updated)

        embed = discord.Embed(
            title=f"Availability status: {timeutil.week_label(wk)}",
            description=(
                f"**{no_change + len(updated)} / {len(active)}** active players have confirmed\n"
                f"• ✅ No change: {no_change}\n"
                f"• ✏️ Updated: {len(updated)}\n"
                f"• ⏳ Not confirmed yet: {waiting}"
            ),
            color=EMBED_COLOR,
        )
        chunks = _chunks([f"• {name}" for name in updated]) or ["*No one has submitted a schedule change yet.*"]
        for i, chunk in enumerate(chunks[:5]):
            embed.add_field(name="✏️ Submitted a schedule change" if i == 0 else "\u200b", value=chunk, inline=False)

        # Character status changes (static / sub / inactive) in the last STATUS_CHANGE_DAYS days
        since = int(timeutil.now_utc().timestamp()) - STATUS_CHANGE_DAYS * 86400
        status_lines = [
            f"• **{c['player']}**: {c['ign']} ({c['job'] or '?'}) "
            f"{character_status_label(c['before'])} → {character_status_label(c['after'])}"
            + (" *(by host)*" if c["by_host"] else "")
            for c in db.status_changes_since(self.bot.conn, since)
        ]
        chunks = _chunks(status_lines) or ["*No character status changes.*"]
        for i, chunk in enumerate(chunks[:5]):
            name = f"🔄 Character status changes (last {STATUS_CHANGE_DAYS} days)" if i == 0 else "\u200b"
            embed.add_field(name=name, value=chunk, inline=False)

        if updated:
            embed.set_footer(text="Use /host availability player:<name> to see someone's changed schedule")

        view = discord.ui.View(timeout=900)
        button = discord.ui.Button(label="Open availability", emoji="📋", style=discord.ButtonStyle.primary)

        async def open_availability(button_interaction: discord.Interaction) -> None:
            # buttons skip the slash-command host check, so check here too
            if not self.bot.is_host(button_interaction):
                await button_interaction.response.send_message("Only hosts can use this.", ephemeral=True)
                return
            await self._all_availability(button_interaction, wk)  # same view as /host availability

        button.callback = open_availability
        view.add_item(button)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @app_commands.command(
        name="availability",
        description="Everyone's availability for a week, or one player's (weekly update, else their default)",
    )
    @app_commands.describe(player="Leave empty for all active players")
    @app_commands.choices(week=WEEK_CHOICES)
    @app_commands.autocomplete(player=player_autocomplete)
    async def availability(
        self,
        interaction: discord.Interaction,
        player: str | None = None,
        week: app_commands.Choice[str] | None = None,
    ):
        wk = self._week(week)
        if player:
            if p := await self._get_player(interaction, player):
                view = OrderToggleView(lambda order: self._player_embed(p, wk, order), self.bot.conn, interaction.user.id)
                await interaction.response.send_message(embed=view.embed(), view=view, ephemeral=True)
            return
        await self._all_availability(interaction, wk)

    def _player_embed(self, player: db.Player, wk: date, order: str = "time") -> discord.Embed:
        conn = self.bot.conn
        squads = order_squads(db.squads_for_week(conn, wk, self.bot.tz), order)
        avail, source = db.effective_week_availability(conn, player.id, wk)
        source_text = "submitted for this week" if source == "weekly" else "default schedule (no weekly update)"
        embed = discord.Embed(
            title=f"{player.display}: {timeutil.week_label(wk)}",
            description=f"*Source: {source_text}*\n{availability_lines(squads, avail)}",
            color=EMBED_COLOR,
        )
        embed.add_field(name="Confirmation", value=confirmation_text(db.get_confirmation(conn, player.id, wk)))
        embed.add_field(name="Status", value=player.status)
        char_lines = []
        for c in db.list_characters(conn, player.id):
            overrides = db.get_character_overrides(conn, c["id"])
            line = (
                f"{character_status_label(c['status'])} · **{c['ign']}** {c['job'] or ''} {c['buff'] or ''} "
                f"· Squad {c['squad'] or '-'}"
            )
            if overrides:
                line += " · overrides: " + ", ".join(
                    f"S{n} {LEVEL_EMOJI[lvl]}" for n, lvl in sorted(overrides.items())
                )
            char_lines.append(line)
        for i, chunk in enumerate(_chunks(char_lines)[:3]):
            embed.add_field(name="Characters" if i == 0 else "​", value=chunk, inline=False)
        return embed

    async def _all_availability(self, interaction: discord.Interaction, wk: date) -> None:
        conn = self.bot.conn
        players = db.list_players(conn, ("active",))
        squads = db.squads_for_week(conn, wk, self.bot.tz)
        confirmations = db.get_confirmations(conn, wk)

        counts = {s.number: {lvl: 0 for lvl in db.LEVELS} for s in squads}
        for p in players:
            avail, _ = db.effective_week_availability(conn, p.id, wk)
            for n, lvl in avail.items():
                if n in counts:
                    counts[n][lvl] += 1

        def overview(order: str) -> discord.Embed:
            lines = [
                f"**Squad {s.number}** · {timeutil.discord_ts(s.starts_at, 'f')} · "
                f"🟢 {counts[s.number]['Preferred']}  🟡 {counts[s.number]['Available']}  "
                f"🔴 {counts[s.number]['Not Available']}"
                for s in order_squads(squads, order)
            ]
            embed = discord.Embed(
                title=f"Availability: {timeutil.week_label(wk)}",
                description=(
                    f"{len(players)} active players · "
                    f"{sum(1 for p in players if p.id in confirmations)} confirmed. "
                    "Players without a weekly update use their default.\n\n" + "\n".join(lines)
                ),
                color=EMBED_COLOR,
            )
            embed.set_footer(
                text="Use the buttons for who's 🟢 Preferred / 🟡 Available per squad, or pick a squad for their "
                "characters"
            )
            return embed

        view = SquadBreakdownView(self.bot, wk, players, overview, interaction.user.id)
        await interaction.response.send_message(embed=view.current_embed(), view=view, ephemeral=True)

    @app_commands.command(name="import", description="Upload the roster CSV to refresh characters, squads and slots")
    @app_commands.describe(file="CSV export of the CQ Roster sheet")
    async def import_csv(self, interaction: discord.Interaction, file: discord.Attachment):
        if not file.filename.lower().endswith(".csv") or file.size > 5_000_000:
            await interaction.response.send_message("Please attach the roster as a .csv file (under 5 MB).", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            text = (await file.read()).decode("utf-8-sig")
            result = importer.import_roster(self.bot.conn, text)
        except (ValueError, UnicodeDecodeError) as e:
            await interaction.followup.send(f"❌ Import failed: {e}", ephemeral=True)
            return
        await interaction.followup.send(
            "✅ Import complete. Player statuses and availability that players set in the bot were not changed.\n"
            f"```\n{result.summary()[:1800]}\n```",
            ephemeral=True,
        )

    @app_commands.command(name="squads", description="Show squad times for a week")
    @app_commands.choices(week=WEEK_CHOICES)
    async def squads(self, interaction: discord.Interaction, week: app_commands.Choice[str] | None = None):
        wk = self._week(week)
        squads = db.squads_for_week(self.bot.conn, wk, self.bot.tz)

        def render(order: str) -> discord.Embed:
            lines = [
                f"**Squad {s.number}** · {timeutil.discord_ts(s.starts_at)}"
                + (" · ✏️ edited for this week" if s.overridden else "")
                for s in order_squads(squads, order)
            ]
            embed = discord.Embed(
                title=f"Squad times: {timeutil.week_label(wk)}",
                description="\n".join(lines) or "*No squads configured.*",
                color=EMBED_COLOR,
            )
            embed.set_footer(text="Times are shown in your timezone")
            return embed

        view = OrderToggleView(render, self.bot.conn, interaction.user.id)
        await interaction.response.send_message(embed=view.embed(), view=view, ephemeral=True)

    @app_commands.command(name="damage_history", description="A character's recorded runs and current damage")
    @app_commands.autocomplete(character=character_autocomplete)
    async def damage_history(self, interaction: discord.Interaction, character: str):
        conn = self.bot.conn
        char = db.get_character(conn, character)
        if char is None:
            await interaction.response.send_message(f"No character named **{character}**.", ephemeral=True)
            return
        await interaction.response.send_message(embed=damage_history_embed(conn, char), ephemeral=True)

    @app_commands.command(name="remind_now", description="Post next week's availability reminder right now")
    async def remind_now(self, interaction: discord.Interaction):
        settings = reminders.load(self.bot.conn, self.bot.config.cq_channel_id)
        channel = self.bot.get_channel(settings.channel_id) if settings.channel_id else interaction.channel
        if not isinstance(channel, discord.abc.Messageable):
            await interaction.response.send_message("I can't post in the configured channel.", ephemeral=True)
            return
        wk = timeutil.upcoming_week_start(timeutil.now_utc(), self.bot.tz)
        message = await send_reminder(self.bot, channel, wk)
        # Covers any scheduled reminder that is due right now, so it isn't posted twice.
        mark_latest_due_as_sent(self.bot)
        await interaction.response.send_message(f"📣 Posted: {message.jump_url}", ephemeral=True)


# --------------------------------------------------------------------------- /config


@app_commands.default_permissions(manage_guild=True)
@app_commands.guild_only()
class ConfigCog(HostOnly, commands.GroupCog, group_name="config", group_description="Bot settings: reminders, damage logs, squad schedule"):
    @app_commands.command(name="reminders", description="View or change the weekly reminder schedule")
    @app_commands.describe(
        channel="Channel to post the reminder in (the CQ channel)",
        remind_days="Days the reminder is posted, comma-separated, e.g. Wed, Fri",
        remind_time="24h time the reminders are posted, e.g. 18:00",
        deadline_day="Deadline day (in the week before the squads run)",
        deadline_time="24h deadline time, e.g. 12:00",
        enabled="Turn the automatic weekly reminders on or off",
    )
    @app_commands.choices(deadline_day=DAY_CHOICES)
    async def reminder_config(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel | None = None,
        remind_days: str | None = None,
        remind_time: str | None = None,
        deadline_day: app_commands.Choice[int] | None = None,
        deadline_time: str | None = None,
        enabled: bool | None = None,
    ):
        conn = self.bot.conn
        try:
            updates = {
                "cq_channel_id": str(channel.id) if channel else None,
                "reminder_weekdays": ",".join(map(str, reminders.parse_weekdays(remind_days))) if remind_days else None,
                "reminder_time": _valid_hhmm(remind_time) if remind_time else None,
                "deadline_weekday": str(deadline_day.value) if deadline_day else None,
                "deadline_time": _valid_hhmm(deadline_time) if deadline_time else None,
                "reminder_enabled": ("1" if enabled else "0") if enabled is not None else None,
            }
        except ValueError:
            await interaction.response.send_message(
                "Days must look like `Wed, Fri` and times like `18:00`.", ephemeral=True
            )
            return
        for key, value in updates.items():
            if value is not None:
                db.set_setting(conn, key, value)
        if remind_days or remind_time:
            # A new schedule only affects future reminders; don't post one whose new time already passed.
            mark_latest_due_as_sent(self.bot)

        settings = reminders.load(conn, self.bot.config.cq_channel_id)
        wk = timeutil.upcoming_week_start(timeutil.now_utc(), self.bot.tz)
        embed = discord.Embed(title="Reminder settings", description=settings.describe(self.bot.tz), color=EMBED_COLOR)
        embed.add_field(
            name=f"For the {timeutil.week_label(wk)}",
            value=(
                "Reminders: "
                + ", ".join(timeutil.discord_ts(t) for t in settings.reminder_times(wk, self.bot.tz))
                + f"\nDeadline: {timeutil.discord_ts(settings.deadline_at(wk, self.bot.tz))}"
            ),
            inline=False,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="damage_logs", description="View or change damage log settings")
    @app_commands.describe(
        channel="Channel where hosts post damage logs (default: #queen-logs)",
        average_runs="How many of a character's most recent runs to average",
    )
    async def damage_config(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel | None = None,
        average_runs: app_commands.Range[int, 1, 50] | None = None,
    ):
        conn = self.bot.conn
        if channel:
            db.set_setting(conn, "queen_logs_channel_id", str(channel.id))
        if average_runs:
            db.set_setting(conn, "damage_average_runs", str(average_runs))
            damage.recompute_all(conn)
        channel_id = logs_channel_id(self.bot)
        runs = damage.average_runs(conn)
        logs = conn.execute("SELECT COUNT(*) FROM damage_logs").fetchone()[0]
        embed = discord.Embed(
            title="Damage log settings",
            description=(
                f"**Channel:** {f'<#{channel_id}>' if channel_id else f'any channel named #{DEFAULT_CHANNEL_NAME}'}\n"
                f"**Damage = average of the last {runs} run(s)**, each scaled to "
                f"{damage.NORMALIZE_MINUTES:g} minutes, in billions\n"
                f"**Log timezone:** {self.bot.config.log_timezone.key}\n"
                f"**Runs recorded:** {logs}"
            ),
            color=EMBED_COLOR,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(
        name="squad_time", description="Change a squad's time for next week or permanently, or reset next week's change"
    )
    @app_commands.describe(
        squad="Squad number",
        day="Day of the week",
        time="24h time in the host timezone, e.g. 21:30",
        permanent="Also change the recurring schedule for all future weeks",
        reset="Undo next week's change: back to the recurring schedule (day and time not needed)",
    )
    @app_commands.choices(day=DAY_CHOICES)
    async def squad_time(
        self,
        interaction: discord.Interaction,
        squad: app_commands.Range[int, 1, 99],
        day: app_commands.Choice[int] | None = None,
        time: str | None = None,
        permanent: bool = False,
        reset: bool = False,
    ):
        wk = timeutil.upcoming_week_start(timeutil.now_utc(), self.bot.tz)
        conn = self.bot.conn
        if reset:
            if day or time or permanent:
                await interaction.response.send_message(
                    "`reset` puts the squad back on its recurring schedule; leave out day, time and permanent.",
                    ephemeral=True,
                )
                return
            db.clear_squad_override(conn, wk, squad)
            await interaction.response.send_message(
                f"✅ Squad {squad} is back on the recurring schedule for the {timeutil.week_label(wk)}.",
                ephemeral=True,
            )
            return
        if day is None or not time:
            await interaction.response.send_message(
                "Give a `day` and `time` (or `reset: True` to undo next week's change).", ephemeral=True
            )
            return
        try:
            hhmm = _valid_hhmm(time)
        except ValueError:
            await interaction.response.send_message("Time must look like `21:30`.", ephemeral=True)
            return
        if permanent:
            db.set_squad_template(conn, squad, day.value, hhmm)
            db.clear_squad_override(conn, wk, squad)
            scope = "every week"
        else:
            if squad not in db.squad_numbers(conn):
                await interaction.response.send_message(
                    f"Squad {squad} isn't on the schedule. Use `permanent: True` to add it.", ephemeral=True
                )
                return
            db.set_squad_override(conn, wk, squad, timeutil.at_weekday(wk, day.value, hhmm, self.bot.tz))
            scope = f"the {timeutil.week_label(wk)} only"
        starts = timeutil.at_weekday(wk, day.value, hhmm, self.bot.tz)
        await interaction.response.send_message(
            f"✅ Squad {squad} is now {day.name} {hhmm} ({self.bot.tz.key}) = {timeutil.discord_ts(starts)} for {scope}.",
            ephemeral=True,
        )

    @app_commands.command(name="squad_remove", description="Permanently remove a squad from the schedule")
    async def squad_remove(self, interaction: discord.Interaction, squad: int):
        if squad not in db.squad_numbers(self.bot.conn):
            await interaction.response.send_message(f"Squad {squad} isn't on the schedule.", ephemeral=True)
            return
        db.delete_squad(self.bot.conn, squad)
        await interaction.response.send_message(f"🗑️ Removed Squad {squad} from the schedule.", ephemeral=True)


# --------------------------------------------------------------------------- /player


@app_commands.default_permissions(manage_guild=True)
@app_commands.guild_only()
class PlayerAdminCog(HostOnly, commands.GroupCog, group_name="player", group_description="Add and change players on the roster"):
    @app_commands.command(name="add", description="Add a new player to the roster")
    @app_commands.describe(
        name="The name the player goes by",
        member="Their Discord account (recommended)",
        username="Their Discord username, if they're not in the server yet (links when they first use the bot)",
        status="Defaults to Active",
    )
    @app_commands.choices(status=STATUS_CHOICES)
    async def add_player(
        self,
        interaction: discord.Interaction,
        name: str,
        member: discord.Member | None = None,
        username: str | None = None,
        status: app_commands.Choice[str] | None = None,
    ):
        conn = self.bot.conn
        if member and (existing := db.get_player_by_discord_id(conn, member.id)):
            await interaction.response.send_message(
                f"{member.mention} is already on the roster as **{existing.display}**.", ephemeral=True
            )
            return
        handle = f"@{member.name}" if member else (f"@{username.strip().lstrip('@')}" if username else None)
        if handle and (existing := db.get_player_by_handle(conn, handle)):
            await interaction.response.send_message(
                f"**{handle}** is already on the roster as **{existing.display}**.", ephemeral=True
            )
            return
        player = db.create_player(
            conn,
            name=name.strip(),
            discord_handle=handle,
            discord_id=member.id if member else None,
            status=status.value if status else "active",
        )
        await interaction.response.send_message(
            f"✅ Added **{player.display}** ({player.status}). Next: `/character add` for each of their "
            "characters. They set their usual availability with Update my default in `/cq availability`.",
            ephemeral=True,
        )

    @app_commands.command(name="edit", description="Rename a player or change their status")
    @app_commands.describe(name="New name", status="Active or inactive")
    @app_commands.choices(status=STATUS_CHOICES)
    @app_commands.autocomplete(player=player_autocomplete)
    async def edit_player(
        self,
        interaction: discord.Interaction,
        player: str,
        name: str | None = None,
        status: app_commands.Choice[str] | None = None,
    ):
        if not name and not status:
            await interaction.response.send_message("Give a new `name`, a `status`, or both.", ephemeral=True)
            return
        if not (p := await self._get_player(interaction, player)):
            return
        changes = []
        if name and name.strip():
            db.set_player_name(self.bot.conn, p.id, name.strip())
            changes.append(f"renamed to **{name.strip()}**")
        if status:
            db.set_player_status(self.bot.conn, p.id, status.value)
            changes.append(f"status **{status.name}**")
        await interaction.response.send_message(f"✅ **{p.display}**: {', '.join(changes)}.", ephemeral=True)

    @app_commands.command(name="link", description="Link a roster player to a Discord member")
    @app_commands.autocomplete(player=player_autocomplete)
    async def link(self, interaction: discord.Interaction, player: str, member: discord.Member):
        if p := await self._get_player(interaction, player):
            db.link_discord(self.bot.conn, p.id, member.id, f"@{member.name}")
            await interaction.response.send_message(f"🔗 Linked **{p.name}** to {member.mention}.", ephemeral=True)


# --------------------------------------------------------------------------- /character


@app_commands.default_permissions(manage_guild=True)
@app_commands.guild_only()
class CharacterAdminCog(HostOnly, commands.GroupCog, group_name="character", group_description="Add, change and remove characters on the roster"):
    @app_commands.command(name="add", description="Add a character to a player")
    @app_commands.describe(
        ign="In-game name", job="Job, e.g. BSP, NL, DRK", buff="Role in the squad", dmg="Damage",
        status="Static (default), sub or inactive. Players can change it in /cq characters",
    )
    @app_commands.choices(buff=BUFF_CHOICES, status=CHARACTER_STATUS_CHOICES)
    @app_commands.autocomplete(player=player_autocomplete, job=job_autocomplete)
    async def add_character(
        self,
        interaction: discord.Interaction,
        player: str,
        ign: str,
        job: str,
        buff: app_commands.Choice[str],
        dmg: float | None = None,
        status: app_commands.Choice[str] | None = None,
    ):
        if not (p := await self._get_player(interaction, player)):
            return
        if existing := db.get_character(self.bot.conn, ign):
            owner = db.get_player(self.bot.conn, existing["player_id"])
            await interaction.response.send_message(
                f"**{existing['ign']}** already exists (owned by {owner.display}).", ephemeral=True
            )
            return
        db.add_character(self.bot.conn, p.id, ign, job, buff.value, dmg, status.value if status else "static")
        await interaction.response.send_message(
            f"✅ Added **{ign.strip()}** ({job.upper()}, {buff.value}) to **{p.display}**.", ephemeral=True
        )

    @app_commands.command(
        name="edit", description="Change a character's job, role, damage, status or owner"
    )
    @app_commands.describe(
        dmg="Fallback damage; damage from uploaded logs takes priority",
        status="Static, sub or inactive (players can also set this themselves)",
        owner="Move the character to another player",
    )
    @app_commands.choices(buff=BUFF_CHOICES, status=CHARACTER_STATUS_CHOICES)
    @app_commands.autocomplete(character=character_autocomplete, job=job_autocomplete, owner=player_autocomplete)
    async def edit_character(
        self,
        interaction: discord.Interaction,
        character: str,
        job: str | None = None,
        buff: app_commands.Choice[str] | None = None,
        dmg: float | None = None,
        status: app_commands.Choice[str] | None = None,
        owner: str | None = None,
    ):
        char = db.get_character(self.bot.conn, character)
        if char is None:
            await interaction.response.send_message(f"No character named **{character}**.", ephemeral=True)
            return
        new_owner = None
        if owner:
            if not (new_owner := await self._get_player(interaction, owner)):
                return
            if new_owner.id == char["player_id"]:
                new_owner = None  # already theirs; nothing to move
        old_owner = db.get_player(self.bot.conn, char["player_id"])
        db.update_character(
            self.bot.conn,
            char["id"],
            changed_by=interaction.user.id,
            job=job.upper() if job else None,
            buff=buff.value if buff else None,
            dmg=dmg,
            status=status.value if status else None,
            player_id=new_owner.id if new_owner else None,
        )
        moved = f" Moved from **{old_owner.display}** to **{new_owner.display}**." if new_owner else ""
        await interaction.response.send_message(f"✅ Updated **{char['ign']}**.{moved}", ephemeral=True)

    @app_commands.command(name="remove", description="Remove a character from the roster")
    @app_commands.autocomplete(character=character_autocomplete)
    async def remove_character(self, interaction: discord.Interaction, character: str):
        char = db.get_character(self.bot.conn, character)
        if char is None:
            await interaction.response.send_message(f"No character named **{character}**.", ephemeral=True)
            return
        db.delete_character(self.bot.conn, char["id"])
        await interaction.response.send_message(
            f"🗑️ Removed **{char['ign']}** and its damage history.", ephemeral=True
        )


async def setup(bot: MonkeyBot) -> None:
    for cog in (HostCog, ConfigCog, PlayerAdminCog, CharacterAdminCog):
        await bot.add_cog(cog(bot))
