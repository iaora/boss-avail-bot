"""Host-only slash commands, each a panel of buttons:

  /cq_host    status, availability, prep roster, damage history, post the reminder
  /cq_config  settings panel: buttons for reminders, damage logs, squad time, remove squads, roster import
  /player     panel: Add, Change, Link buttons
  /character  panel: Add, Change, Remove buttons

A host is anyone with the role in HOST_ROLE_ID, or anyone with the Manage Server permission.
Each is hidden from everyone without Manage Server; a server admin shows them to the
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
from ..squad_breakdown import PrepRosterView, SquadBreakdownView
from ..views import (
    EMBED_COLOR,
    LEVEL_EMOJI,
    OrderToggleView,
    availability_lines,
    character_status_label,
    confirmation_text,
    damage_history_embed,
    get_squad_order,
    order_button_label,
    order_squads,
    other_order,
    popup_chunks,
    send_reminder,
    set_squad_order,
    squad_times_embed,
)
from .damage_logs import DEFAULT_CHANNEL_NAME, logs_channel_id
from .reminder import mark_latest_due_as_sent

STATUS_CHANGE_DAYS = 7  # how far back Status lists character status changes
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


async def player_autocomplete(interaction: discord.Interaction, current: str):
    conn = interaction.client.conn
    players = db.search_players(conn, current) if current else db.list_players(conn)[:25]
    return [app_commands.Choice(name=f"{p.display} [{p.status}]"[:100], value=str(p.id)) for p in players]


async def character_autocomplete(interaction: discord.Interaction, current: str):
    rows = interaction.client.conn.execute(
        "SELECT ign, job FROM characters WHERE ign LIKE ? ORDER BY ign COLLATE NOCASE LIMIT 25", (f"%{current}%",)
    ).fetchall()
    return [app_commands.Choice(name=f"{r['ign']} ({r['job'] or '?'})", value=r["ign"]) for r in rows]


class IndividualAvailabilityView(discord.ui.View):
    """From /cq_host > Status: one private message with a dropdown of the players who changed the week.
    Picking someone (or re-sorting) updates this same message in place."""

    def __init__(self, cog: "HostCog", week: date, players: list[db.Player], user_id: int):
        super().__init__(timeout=900)
        self.cog = cog
        self.week = week
        self.players = {p.id: p for p in players[:25]}  # Discord allows 25 options per dropdown
        self.user_id = user_id
        self.order = get_squad_order(cog.bot.conn, user_id)
        self.selected: int | None = None
        self._build()

    def _build(self) -> None:
        self.clear_items()
        select = discord.ui.Select(
            placeholder="Choose a player…",
            options=[
                discord.SelectOption(
                    label=p.display[:100],
                    value=str(p.id),
                    description=self.cog._changes_summary(p, self.week)[:100],
                    emoji="✏️",
                    default=p.id == self.selected,
                )
                for p in self.players.values()
            ],
            row=0,
        )
        select.callback = self._pick
        self.add_item(select)
        if self.selected is not None:  # sorting only matters once a player's week is shown
            sort = discord.ui.Button(
                label=order_button_label(other_order(self.order)), emoji="🔀",
                style=discord.ButtonStyle.secondary, row=1,
            )
            sort.callback = self._toggle_order
            self.add_item(sort)

    def embed(self) -> discord.Embed:
        if self.selected is None:
            return discord.Embed(
                title=f"Individual availability: {timeutil.week_label(self.week)}",
                description=f"{len(self.players)} player(s) submitted a schedule change. Pick one below to see "
                "what they changed and their full week. Pick another to switch; this message updates.",
                color=EMBED_COLOR,
            )
        return self.cog._changes_embed(self.players[self.selected], self.week, self.order)

    async def _refresh(self, interaction: discord.Interaction) -> None:
        self._build()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    async def _pick(self, interaction: discord.Interaction) -> None:
        if not self.cog.bot.is_host(interaction):
            await interaction.response.send_message("Only hosts can use this.", ephemeral=True)
            return
        self.selected = int(interaction.data["values"][0])
        await self._refresh(interaction)

    async def _toggle_order(self, interaction: discord.Interaction) -> None:
        if not self.cog.bot.is_host(interaction):
            await interaction.response.send_message("Only hosts can use this.", ephemeral=True)
            return
        self.order = other_order(self.order)
        set_squad_order(self.cog.bot.conn, interaction.user.id, self.order)
        await self._refresh(interaction)


# --------------------------------------------------------------------------- /cq_host


class HostCog(HostOnly, commands.Cog):
    """/cq_host: a panel of buttons (see HostPanel). Each button runs one of the methods below."""

    @app_commands.command(name="cq_host", description="Host tools: status, availability, prep roster, damage history, reminder")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def cq_host(self, interaction: discord.Interaction):
        panel = HostPanel(self)
        await interaction.response.send_message(embed=panel.embed(), view=panel, ephemeral=True)

    async def status(self, interaction: discord.Interaction, wk: date) -> None:
        """How many active players have confirmed their availability for the week."""
        confirmations = db.get_confirmations(self.bot.conn, wk)
        active = db.list_players(self.bot.conn, ("active",))
        no_change = sum(1 for p in active if confirmations.get(p.id) == "no_change")
        # Players who submitted a different schedule for this week
        changed_players = [p for p in active if confirmations.get(p.id) == "updated"]
        updated = [p.display for p in changed_players]
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
            embed.set_footer(text="Press See individual availability to see what each person changed")

        view = discord.ui.View(timeout=900)
        if changed_players:
            individual = discord.ui.Button(
                label="See individual availability", emoji="👤", style=discord.ButtonStyle.secondary
            )

            async def open_individual(button_interaction: discord.Interaction) -> None:
                if not self.bot.is_host(button_interaction):
                    await button_interaction.response.send_message("Only hosts can use this.", ephemeral=True)
                    return
                picker = IndividualAvailabilityView(self, wk, changed_players, button_interaction.user.id)
                await button_interaction.response.send_message(embed=picker.embed(), view=picker, ephemeral=True)

            individual.callback = open_individual
            view.add_item(individual)
        button = discord.ui.Button(label="Open availability", emoji="📋", style=discord.ButtonStyle.primary)

        async def open_availability(button_interaction: discord.Interaction) -> None:
            # buttons skip the slash-command host check, so check here too
            if not self.bot.is_host(button_interaction):
                await button_interaction.response.send_message("Only hosts can use this.", ephemeral=True)
                return
            await self.all_availability(button_interaction, wk)  # same view as the Availability button

        button.callback = open_availability
        view.add_item(button)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    async def player_availability(self, interaction: discord.Interaction, player: db.Player, wk: date) -> None:
        """One player's availability for the week (their weekly update, else their default)."""
        view = OrderToggleView(lambda order: self._player_embed(player, wk, order), self.bot.conn, interaction.user.id)
        await interaction.response.send_message(embed=view.embed(), view=view, ephemeral=True)

    def _changes(self, player: db.Player, wk: date, order: str = "time") -> list[tuple[db.SquadTime, str | None, str]]:
        """Squads where the player's availability for the week differs from their default:
        (squad, default level, level this week)."""
        conn = self.bot.conn
        default = db.get_default_availability(conn, player.id)
        weekly = db.get_weekly_availability(conn, player.id, wk)
        return [
            (s, default.get(s.number), weekly[s.number])
            for s in order_squads(db.squads_for_week(conn, wk, self.bot.tz), order)
            if s.number in weekly and weekly[s.number] != default.get(s.number)
        ]

    def _changes_summary(self, player: db.Player, wk: date) -> str:
        """e.g. '2 squad(s) changed from their default · 1 character changed'."""
        text = f"{len(self._changes(player, wk))} squad(s) changed from their default"
        characters = len(db.characters_changed_for_week(self.bot.conn, player.id, wk))
        return text + (f" · {characters} character(s) changed" if characters else "")

    def _changes_embed(self, player: db.Player, wk: date, order: str = "time") -> discord.Embed:
        """A player's availability for the week plus exactly what changed from their default."""
        embed = self._player_embed(player, wk, order)
        changes = [
            f"• **Squad {s.number}** · {timeutil.discord_ts(s.starts_at)}: "
            f"{LEVEL_EMOJI.get(before, '⚪')} {before or 'Not set'} → {LEVEL_EMOJI[after]} {after}"
            for s, before, after in self._changes(player, wk, order)
        ]
        char_changes = db.characters_changed_for_week(self.bot.conn, player.id, wk)
        if not changes and char_changes:
            changes = ["*Their own schedule is the same as their default (see their character changes).*"]
        chunks = _chunks(changes) or ["*Same as their default (they re-saved without changing anything).*"]
        position = 0
        for i, chunk in enumerate(chunks[:3]):
            embed.insert_field_at(
                position, name="✏️ Changed from default" if i == 0 else "\u200b", value=chunk, inline=False
            )
            position += 1
        if char_changes:
            lines = [
                f"• 🧩 **{ign}**: " + ", ".join(f"Squad {n} {LEVEL_EMOJI[lvl]} {lvl}" for n, lvl in sorted(c.items()))
                for ign, c in char_changes.items()
            ]
            for i, chunk in enumerate(_chunks(lines)[:2]):
                embed.insert_field_at(
                    position, name="🧩 Characters changed for this week" if i == 0 else "\u200b", value=chunk,
                    inline=False,
                )
                position += 1
        return embed

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

    async def all_availability(self, interaction: discord.Interaction, wk: date) -> None:
        """Everyone's availability for the week, per squad."""
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
                f"**Squad {s.number}** · {timeutil.discord_ts(s.starts_at)} · "
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
                text="Use the buttons for who's 🟢 Preferred / 🟡 Available per squad. Prep Roster in /cq_host "
                "shows each squad's characters"
            )
            return embed

        view = SquadBreakdownView(self.bot, wk, players, overview, interaction.user.id)
        await interaction.response.send_message(embed=view.current_embed(), view=view, ephemeral=True)

    async def prep_roster(self, interaction: discord.Interaction, wk: date) -> None:
        """One squad at a time: the characters at 🟢 Preferred or 🟡 Available, for slotting."""
        view = PrepRosterView(self.bot, wk, db.list_players(self.bot.conn, ("active",)), interaction.user.id)
        await interaction.response.send_message(embed=view.current_embed(), view=view, ephemeral=True)

    async def damage_history(self, interaction: discord.Interaction, character: str) -> None:
        """A character's recorded runs and current damage (the name is matched like /character's)."""
        try:
            char = find_character(self.bot.conn, character)
        except ValueError as e:
            await interaction.response.send_message(str(e), ephemeral=True)
            return
        await interaction.response.send_message(embed=damage_history_embed(self.bot.conn, char), ephemeral=True)

    def reminder_channel(self, interaction: discord.Interaction):
        """Where the reminder is posted: the configured channel, else the channel this was used in."""
        settings = reminders.load(self.bot.conn, self.bot.config.cq_channel_id)
        return self.bot.get_channel(settings.channel_id) if settings.channel_id else interaction.channel

    async def remind_now(self, interaction: discord.Interaction) -> None:
        """Post next week's availability reminder right now."""
        channel = self.reminder_channel(interaction)
        if not isinstance(channel, discord.abc.Messageable):
            await interaction.response.send_message("I can't post in the configured channel.", ephemeral=True)
            return
        wk = timeutil.upcoming_week_start(timeutil.now_utc(), self.bot.tz)
        message = await send_reminder(self.bot, channel, wk)
        # Covers any scheduled reminder that is due right now, so it isn't posted twice.
        mark_latest_due_as_sent(self.bot)
        await interaction.response.send_message(f"📣 Posted: {message.jump_url}", ephemeral=True)


class HostPanel(discord.ui.View):
    """The /cq_host message: a week dropdown, then a button per host tool. Status, Availability,
    Prep Roster and Player availability use the chosen week."""

    WEEKS = {"upcoming": "Next week (upcoming)", "current": "This week (current)"}

    def __init__(self, cog: HostCog):
        super().__init__(timeout=900)
        self.cog = cog
        self.week = "upcoming"
        self._rebuild()

    def week_start(self) -> date:
        now = timeutil.now_utc()
        if self.week == "current":
            return timeutil.current_week_start(now, self.cog.bot.tz)
        return timeutil.upcoming_week_start(now, self.cog.bot.tz)

    def embed(self) -> discord.Embed:
        return discord.Embed(
            title="Host tools",
            description=(
                f"Week: **{timeutil.week_label(self.week_start())}** (change it with the dropdown)\n\n"
                "📋 **Status**: who has checked in, and recent character status changes\n"
                "📊 **Availability**: everyone's availability per squad (counts and names)\n"
                "🧾 **Prep Roster**: one squad's characters at each level, for slotting\n"
                "👤 **Player availability**: one player's availability\n"
                "📈 **Damage history**: a character's recorded runs and damage\n"
                "📣 **Post reminder now**: post next week's reminder in the CQ channel"
            ),
            color=EMBED_COLOR,
        )

    def _rebuild(self) -> None:
        self.clear_items()
        week = discord.ui.Select(
            options=[discord.SelectOption(label=label, value=v, default=v == self.week) for v, label in self.WEEKS.items()],
            row=0,
        )
        week.callback = self._pick_week
        self.add_item(week)
        cog = self.cog
        buttons = [
            ("Status", "📋", discord.ButtonStyle.primary, 1, lambda i: cog.status(i, self.week_start())),
            ("Availability", "📊", discord.ButtonStyle.primary, 1, lambda i: cog.all_availability(i, self.week_start())),
            ("Prep Roster", "🧾", discord.ButtonStyle.primary, 1, lambda i: cog.prep_roster(i, self.week_start())),
            ("Player availability", "👤", discord.ButtonStyle.secondary, 1,
             lambda i: i.response.send_modal(PlayerAvailabilityModal(self))),
            ("Damage history", "📈", discord.ButtonStyle.secondary, 2,
             lambda i: i.response.send_modal(DamageHistoryModal(self))),
            ("Post reminder now", "📣", discord.ButtonStyle.danger, 2, self._confirm_reminder),
        ]
        for label, emoji, style, row, action in buttons:
            button = discord.ui.Button(label=label, emoji=emoji, style=style, row=row)
            button.callback = action
            self.add_item(button)

    async def _confirm_reminder(self, interaction: discord.Interaction) -> None:
        """Asks first, since the reminder pings the player role in a public channel."""
        channel = self.cog.reminder_channel(interaction)
        where = getattr(channel, "mention", None) or "this channel"
        wk = timeutil.upcoming_week_start(timeutil.now_utc(), self.cog.bot.tz)
        confirm = discord.ui.View(timeout=300)
        post = discord.ui.Button(label="Post now", emoji="📣", style=discord.ButtonStyle.danger)

        async def post_now(button_interaction: discord.Interaction) -> None:
            confirm.stop()
            await self.cog.remind_now(button_interaction)

        post.callback = post_now
        confirm.add_item(post)
        await interaction.response.send_message(
            f"Post the reminder for the **{timeutil.week_label(wk)}** in {where} now? It pings the player role.",
            view=confirm,
            ephemeral=True,
        )

    async def _pick_week(self, interaction: discord.Interaction) -> None:
        self.week = interaction.data["values"][0]
        self._rebuild()
        await interaction.response.edit_message(embed=self.embed(), view=self)


class PlayerAvailabilityModal(discord.ui.Modal):
    def __init__(self, panel: HostPanel):
        super().__init__(title="Player availability", timeout=900)
        self.panel = panel
        self.player = discord.ui.TextInput(max_length=100, placeholder="Name, Discord username, or one of their characters")
        self.add_item(discord.ui.Label(
            text="Player", component=self.player, description=f"For the {timeutil.week_label(panel.week_start())}"
        ))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            player = find_player(self.panel.cog.bot.conn, self.player.value)
        except ValueError as e:
            await interaction.response.send_message(str(e), ephemeral=True)
            return
        await self.panel.cog.player_availability(interaction, player, self.panel.week_start())


class DamageHistoryModal(discord.ui.Modal):
    def __init__(self, panel: HostPanel):
        super().__init__(title="Damage history", timeout=900)
        self.panel = panel
        self.character = discord.ui.TextInput(max_length=50)
        self.add_item(discord.ui.Label(text="In-game name", component=self.character))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.panel.cog.damage_history(interaction, self.character.value)


# --------------------------------------------------------------------------- /cq_config

# One command, /cq_config, shows the CQ settings with a button per setting; each button opens a
# pop-up with that setting's fields, filled in with the current values. The save functions below
# do the work (and are what the tests call); the pop-ups only collect and check the input.


def reminder_settings_embed(bot: MonkeyBot) -> discord.Embed:
    settings = reminders.load(bot.conn, bot.config.cq_channel_id)
    wk = timeutil.upcoming_week_start(timeutil.now_utc(), bot.tz)
    embed = discord.Embed(title="⏰ Reminders", description=settings.describe(bot.tz), color=EMBED_COLOR)
    embed.add_field(
        name=f"For the {timeutil.week_label(wk)}",
        value=(
            "Reminders: "
            + ", ".join(timeutil.discord_ts(t) for t in settings.reminder_times(wk, bot.tz))
            + f"\nDeadline: {timeutil.discord_ts(settings.deadline_at(wk, bot.tz))}"
        ),
        inline=False,
    )
    return embed


def damage_settings_embed(bot: MonkeyBot) -> discord.Embed:
    conn = bot.conn
    channel_id = logs_channel_id(bot)
    logs = conn.execute("SELECT COUNT(*) FROM damage_logs WHERE boss_id = ?", (db.CQ,)).fetchone()[0]
    return discord.Embed(
        title="📊 Damage logs",
        description=(
            f"**Channel:** {f'<#{channel_id}>' if channel_id else f'any channel named #{DEFAULT_CHANNEL_NAME}'}\n"
            f"**Damage = average of the last {damage.average_runs(conn)} run(s)**, each scaled to "
            f"{damage.NORMALIZE_MINUTES:g} minutes, in billions\n"
            f"**Log timezone:** {bot.config.log_timezone.key}\n"
            f"**Runs recorded:** {logs}"
        ),
        color=EMBED_COLOR,
    )


def save_reminder_settings(
    bot: MonkeyBot, *, channel_id: int | None, weekdays: list[int], remind_time: str, deadline_weekday: int,
    deadline_time: str,
) -> str:
    """Raises ValueError for a time that isn't HH:MM. A channel of None keeps the current one."""
    conn = bot.conn
    before = reminders.load(conn, bot.config.cq_channel_id)
    remind_time, deadline_time = _valid_hhmm(remind_time), _valid_hhmm(deadline_time)
    if not weekdays:
        raise ValueError("pick at least one reminder day")
    if channel_id:
        db.set_boss_setting(conn, "cq_channel_id", str(channel_id))
    db.set_boss_setting(conn, "reminder_weekdays", ",".join(map(str, sorted(set(weekdays)))))
    db.set_boss_setting(conn, "reminder_time", remind_time)
    db.set_boss_setting(conn, "deadline_weekday", str(deadline_weekday))
    db.set_boss_setting(conn, "deadline_time", deadline_time)
    if sorted(set(weekdays)) != before.reminder_weekdays or remind_time != before.reminder_time:
        # A new schedule only affects future reminders; don't post one whose new time already passed.
        mark_latest_due_as_sent(bot)
    return "✅ Saved the reminder settings."


def set_reminders_enabled(bot: MonkeyBot, enabled: bool) -> str:
    db.set_boss_setting(bot.conn, "reminder_enabled", "1" if enabled else "0")
    return "🔔 Automatic reminders are on." if enabled else "🔕 Automatic reminders are off."


def save_damage_settings(bot: MonkeyBot, *, channel_id: int | None, average_runs: int) -> str:
    """A channel of None keeps the current one."""
    conn = bot.conn
    if not 1 <= average_runs <= 50:
        raise ValueError("runs to average must be between 1 and 50")
    if channel_id:
        db.set_boss_setting(conn, "queen_logs_channel_id", str(channel_id))
    if average_runs != damage.average_runs(conn):
        db.set_boss_setting(conn, "damage_average_runs", str(average_runs))
        damage.recompute_all(conn)
    return "✅ Saved the damage log settings."


SQUAD_TIME_ACTIONS = {
    "week": "Next week only",
    "permanent": "Every week (the recurring schedule)",
    "reset": "Reset next week back to the recurring schedule",
}


def change_squad_time(bot: MonkeyBot, squad: int, action: str, day: int | None, time: str | None) -> str:
    """action: 'week' (next week only), 'permanent' (also adds a new squad), or 'reset' (undo next
    week's change). Raises ValueError with a message for the host."""
    wk = timeutil.upcoming_week_start(timeutil.now_utc(), bot.tz)
    conn = bot.conn
    if action == "reset":
        db.clear_squad_override(conn, wk, squad)
        return f"✅ Squad {squad} is back on the recurring schedule for the {timeutil.week_label(wk)}."
    if day is None or not time:
        raise ValueError("Pick a day and enter a time (or choose Reset to undo next week's change).")
    try:
        hhmm = _valid_hhmm(time)
    except ValueError:
        raise ValueError("Time must look like `21:30`.") from None
    if action == "permanent":
        db.set_squad_template(conn, squad, day, hhmm)
        db.clear_squad_override(conn, wk, squad)
        scope = "every week"
    else:
        if squad not in db.squad_numbers(conn):
            raise ValueError(f"Squad {squad} isn't on the schedule. Choose Every week to add it.")
        db.set_squad_override(conn, wk, squad, timeutil.at_weekday(wk, day, hhmm, bot.tz))
        scope = f"the {timeutil.week_label(wk)} only"
    starts = timeutil.at_weekday(wk, day, hhmm, bot.tz)
    return (
        f"✅ Squad {squad} is now {timeutil.WEEKDAYS[day]} {hhmm} ({bot.tz.key}) = {timeutil.discord_ts(starts)} "
        f"for {scope}."
    )


def remove_squads(bot: MonkeyBot, numbers: list[int]) -> str:
    existing = set(db.squad_numbers(bot.conn))
    removed = sorted(n for n in numbers if n in existing)
    for n in removed:
        db.delete_squad(bot.conn, n)
    if not removed:
        return "Nothing removed: those squads aren't on the schedule."
    return f"🗑️ Removed {', '.join(f'Squad {n}' for n in removed)} from the schedule."


async def import_roster_file(bot: MonkeyBot, interaction: discord.Interaction, file: discord.Attachment) -> None:
    """Refresh characters, squads and slots from the roster CSV."""
    if not file.filename.lower().endswith(".csv") or file.size > 5_000_000:
        await interaction.response.send_message("Please upload the roster as a .csv file (under 5 MB).", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        text = (await file.read()).decode("utf-8-sig")
        result = importer.import_roster(bot.conn, text)
    except (ValueError, UnicodeDecodeError) as e:
        await interaction.followup.send(f"❌ Import failed: {e}", ephemeral=True)
        return
    await interaction.followup.send(
        "✅ Import complete. Player statuses and availability that players set in the bot were not changed.\n"
        f"```\n{result.summary()[:1800]}\n```",
        ephemeral=True,
    )


def _day_options(selected: set[int] = frozenset()) -> list[discord.SelectOption]:
    return [discord.SelectOption(label=d, value=str(i), default=i in selected) for i, d in enumerate(timeutil.WEEKDAYS)]


def _labelled(text: str, component, description: str | None = None) -> discord.ui.Label:
    return discord.ui.Label(text=text, component=component, description=description)


def _chosen_channel_id(select: discord.ui.ChannelSelect) -> int | None:
    return select.values[0].id if select.values else None


class ConfigPanel(discord.ui.View):
    """The /cq_config message: current settings, and a button per setting. Every change re-renders it."""

    def __init__(self, bot: MonkeyBot, user_id: int):
        super().__init__(timeout=900)
        self.bot = bot
        self.user_id = user_id
        self._rebuild()

    def embeds(self) -> list[discord.Embed]:
        wk = timeutil.upcoming_week_start(timeutil.now_utc(), self.bot.tz)
        return [
            reminder_settings_embed(self.bot),
            damage_settings_embed(self.bot),
            squad_times_embed(self.bot, wk, get_squad_order(self.bot.conn, self.user_id), mark_moved=True),
        ]

    def _rebuild(self) -> None:
        self.clear_items()
        enabled = reminders.load(self.bot.conn, self.bot.config.cq_channel_id).enabled
        buttons = [
            ("Reminders", "⏰", discord.ButtonStyle.primary, lambda: RemindersModal(self)),
            ("Damage logs", "📊", discord.ButtonStyle.primary, lambda: DamageLogsModal(self)),
            ("Squad time", "🕒", discord.ButtonStyle.primary, lambda: SquadTimeModal(self)),
            (
                "Remove squads", "🗑️", discord.ButtonStyle.danger,
                lambda: RemoveSquadsModal(self) if db.squad_numbers(self.bot.conn) else "There are no squads to remove.",
            ),
        ]
        for label, emoji, style, make in buttons:
            button = discord.ui.Button(label=label, emoji=emoji, style=style, row=0)
            button.callback = self._opener(make)
            self.add_item(button)
        toggle = discord.ui.Button(
            label="Turn automatic reminders off" if enabled else "Turn automatic reminders on",
            emoji="🔕" if enabled else "🔔",
            style=discord.ButtonStyle.secondary,
            row=1,
        )
        toggle.callback = self._toggle_reminders
        self.add_item(toggle)
        import_button = discord.ui.Button(label="Import roster", emoji="📥", style=discord.ButtonStyle.secondary, row=1)
        import_button.callback = self._opener(lambda: ImportRosterModal(self.bot))
        self.add_item(import_button)

    def _opener(self, make):
        async def open_modal(interaction: discord.Interaction) -> None:
            if isinstance(modal := make(), str):  # nothing to edit
                await interaction.response.send_message(modal, ephemeral=True)
            else:
                await interaction.response.send_modal(modal)

        return open_modal

    async def show(self, interaction: discord.Interaction, note: str | None = None) -> None:
        """Re-render this message in place (after a pop-up or button on it)."""
        self._rebuild()
        await interaction.response.edit_message(content=note, embeds=self.embeds(), view=self)

    async def _toggle_reminders(self, interaction: discord.Interaction) -> None:
        enabled = reminders.load(self.bot.conn, self.bot.config.cq_channel_id).enabled
        await self.show(interaction, set_reminders_enabled(self.bot, not enabled))


class ImportRosterModal(discord.ui.Modal):
    def __init__(self, bot: MonkeyBot):
        super().__init__(title="Import the roster", timeout=900)
        self.bot = bot
        self.file = discord.ui.FileUpload(max_values=1)
        self.add_item(discord.ui.TextDisplay(
            "Upload the CSV export of the CQ Roster sheet. It refreshes characters, squads and slots; "
            "player statuses and the availability players set in the bot aren't changed."
        ))
        self.add_item(discord.ui.Label(text="Roster CSV", component=self.file, description=".csv, under 5 MB"))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await import_roster_file(self.bot, interaction, self.file.values[0])


class RemindersModal(discord.ui.Modal):
    def __init__(self, panel: ConfigPanel):
        super().__init__(title="Reminder settings", timeout=900)
        self.panel = panel
        bot = panel.bot
        current = reminders.load(bot.conn, bot.config.cq_channel_id)
        tz = bot.tz.key
        self.channel = discord.ui.ChannelSelect(
            channel_types=[discord.ChannelType.text], required=False, min_values=0,
            default_values=[discord.Object(id=current.channel_id)] if current.channel_id else [],
        )
        self.days = discord.ui.Select(options=_day_options(set(current.reminder_weekdays)), min_values=1, max_values=7)
        self.remind_time = discord.ui.TextInput(default=current.reminder_time, max_length=5, placeholder="18:00")
        self.deadline_day = discord.ui.Select(options=_day_options({current.deadline_weekday}))
        self.deadline_time = discord.ui.TextInput(default=current.deadline_time, max_length=5, placeholder="12:00")
        self.add_item(_labelled("Channel", self.channel, "Where the weekly reminder is posted (the CQ channel)"))
        self.add_item(_labelled("Reminder days", self.days, "Pick one or more days"))
        self.add_item(_labelled("Reminder time", self.remind_time, f"24h, {tz}, e.g. 18:00"))
        self.add_item(_labelled("Deadline day", self.deadline_day, "In the week before the squads run"))
        self.add_item(_labelled("Deadline time", self.deadline_time, f"24h, {tz}, e.g. 12:00"))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            note = save_reminder_settings(
                self.panel.bot,
                channel_id=_chosen_channel_id(self.channel),
                weekdays=[int(v) for v in self.days.values],
                remind_time=self.remind_time.value,
                deadline_weekday=int(self.deadline_day.values[0]),
                deadline_time=self.deadline_time.value,
            )
        except ValueError:
            await interaction.response.send_message(
                "Nothing saved: times must look like `18:00`, with at least one reminder day.", ephemeral=True
            )
            return
        await self.panel.show(interaction, note)


class DamageLogsModal(discord.ui.Modal):
    def __init__(self, panel: ConfigPanel):
        super().__init__(title="Damage log settings", timeout=900)
        self.panel = panel
        bot = panel.bot
        channel_id = logs_channel_id(bot)
        self.channel = discord.ui.ChannelSelect(
            channel_types=[discord.ChannelType.text], required=False, min_values=0,
            default_values=[discord.Object(id=channel_id)] if channel_id else [],
        )
        self.runs = discord.ui.TextInput(default=str(damage.average_runs(bot.conn)), max_length=2, placeholder="3")
        self.add_item(_labelled(
            "Channel", self.channel, f"Where hosts post damage logs. Empty: any channel named #{DEFAULT_CHANNEL_NAME}"
        ))
        self.add_item(_labelled("Runs to average", self.runs, "How many of a character's most recent runs (1–50)"))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            note = save_damage_settings(
                self.panel.bot, channel_id=_chosen_channel_id(self.channel), average_runs=int(self.runs.value)
            )
        except ValueError:
            await interaction.response.send_message(
                "Nothing saved: runs to average must be a number from 1 to 50.", ephemeral=True
            )
            return
        await self.panel.show(interaction, note)


class SquadTimeModal(discord.ui.Modal):
    MAX_LISTED = 24  # a dropdown holds 25 options: the squads plus "New squad"

    def __init__(self, panel: ConfigPanel):
        super().__init__(title="Change a squad's time", timeout=900)
        self.panel = panel
        bot = panel.bot
        template = db.list_squad_template(bot.conn)
        new_number = max((r["number"] for r in template), default=0) + 1
        if len(template) <= self.MAX_LISTED:
            options = [
                discord.SelectOption(
                    label=f"Squad {r['number']}", value=str(r["number"]),
                    description=f"Usually {timeutil.WEEKDAYS[r['weekday']]} {r['time']} ({bot.tz.key})",
                )
                for r in sorted(template, key=lambda r: r["number"])
            ]
            options.append(discord.SelectOption(
                label=f"New squad {new_number}", value=str(new_number), emoji="➕",
                description="Choose Every week below to add it",
            ))
            self.squad = discord.ui.Select(options=options)
        else:  # too many squads for a dropdown
            self.squad = discord.ui.TextInput(max_length=2, placeholder=f"e.g. 3 (or {new_number} for a new squad)")
        self.day = discord.ui.Select(options=_day_options(), required=False, min_values=0)
        self.time = discord.ui.TextInput(required=False, max_length=5, placeholder="21:30")
        self.action = discord.ui.RadioGroup(options=[
            discord.RadioGroupOption(label=label, value=value, default=value == "week")
            for value, label in SQUAD_TIME_ACTIONS.items()
        ])
        self.add_item(_labelled("Squad", self.squad))
        self.add_item(_labelled("Day", self.day, "Not needed for Reset"))
        self.add_item(_labelled("Time", self.time, f"24h, {bot.tz.key}, e.g. 21:30. Not needed for Reset"))
        self.add_item(_labelled("Change", self.action))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw = self.squad.values[0] if isinstance(self.squad, discord.ui.Select) else self.squad.value.strip()
        try:
            if not raw.isdigit() or not 1 <= int(raw) <= 99:
                raise ValueError("The squad must be a number from 1 to 99.")
            note = change_squad_time(
                self.panel.bot, int(raw), self.action.value or "week",
                int(self.day.values[0]) if self.day.values else None, self.time.value,
            )
        except ValueError as e:
            await interaction.response.send_message(f"Nothing saved: {e}", ephemeral=True)
            return
        await self.panel.show(interaction, note)


class RemoveSquadsModal(discord.ui.Modal):
    def __init__(self, panel: ConfigPanel):
        super().__init__(title="Remove squads", timeout=900)
        self.panel = panel
        bot = panel.bot
        template = sorted(db.list_squad_template(bot.conn), key=lambda r: r["number"])[:25]
        self.squads = discord.ui.Select(
            options=[
                discord.SelectOption(
                    label=f"Squad {r['number']}", value=str(r["number"]),
                    description=f"{timeutil.WEEKDAYS[r['weekday']]} {r['time']} ({bot.tz.key})",
                )
                for r in template
            ],
            min_values=1,
            max_values=max(1, len(template)),
        )
        self.add_item(discord.ui.TextDisplay(
            "Removes the squads from the schedule permanently, for every week. This can't be undone; to add a "
            "squad back, use Squad time with Every week."
        ))
        self.add_item(_labelled("Squads to remove", self.squads, "Pick one or more"))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.panel.show(interaction, remove_squads(self.panel.bot, [int(v) for v in self.squads.values]))


class ConfigCog(HostOnly, commands.Cog):
    @app_commands.command(name="cq_config", description="CQ settings: reminders, damage logs, squad schedule, roster import")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def cq_config(self, interaction: discord.Interaction):
        panel = ConfigPanel(self.bot, interaction.user.id)
        await interaction.response.send_message(embeds=panel.embeds(), view=panel, ephemeral=True)


# --------------------------------------------------------------------------- /player

# Like /character: one command, /player, shows Add / Change / Link buttons, each opening a pop-up.
# Players are typed by name and matched on submit (find_player, below in the /character section);
# Discord accounts use Discord's own member picker, which can search. `/player change:` (search as
# you type) opens a player's edit pop-up straight away.

PLAYER_STATUS_OPTIONS = [("active", "Active"), ("inactive", "Inactive")]


def add_player_entry(bot: MonkeyBot, *, name: str, member, username: str | None, status: str = "active") -> str:
    """`member`: their Discord account (from the member picker), or None with `username` for someone
    not in the server yet (linked when they first use the bot). Raises ValueError (nothing saved)."""
    conn = bot.conn
    name = name.strip()
    if not name:
        raise ValueError("Enter the player's name.")
    if member and (existing := db.get_player_by_discord_id(conn, member.id)):
        raise ValueError(f"{member.mention} is already on the roster as **{existing.display}**.")
    username = (username or "").strip().lstrip("@")
    handle = f"@{member.name}" if member else (f"@{username}" if username else None)
    if handle and (existing := db.get_player_by_handle(conn, handle)):
        raise ValueError(f"**{handle}** is already on the roster as **{existing.display}**.")
    player = db.create_player(
        conn, name=name, discord_handle=handle, discord_id=member.id if member else None, status=status
    )
    return (
        f"✅ Added **{player.display}** ({player.status}). Next: `/character` > Add for each of their "
        "characters. They set their usual availability with Change default availability in `/cq availability`."
    )


def edit_player_entry(bot: MonkeyBot, player: db.Player, *, name: str, status: str) -> str:
    """Saves only what changed."""
    changes = []
    name = name.strip()
    if name and name != player.name:
        db.set_player_name(bot.conn, player.id, name)
        changes.append(f"renamed to **{name}**")
    if status != player.status:
        db.set_player_status(bot.conn, player.id, status)
        changes.append(f"status **{dict(PLAYER_STATUS_OPTIONS)[status]}**")
    if not changes:
        return f"Nothing changed for **{player.display}**."
    return f"✅ **{player.display}**: {', '.join(changes)}."


def link_player_entry(bot: MonkeyBot, *, player: str, member) -> str:
    p = find_player(bot.conn, player)
    db.link_discord(bot.conn, p.id, member.id, f"@{member.name}")
    return f"🔗 Linked **{p.name}** to {member.mention}."


def player_card(bot: MonkeyBot, player: db.Player) -> discord.Embed:
    account = player.mention if player.discord_id else (
        f"{player.discord_handle} (not linked yet: links when they first use the bot)" if player.discord_handle
        else "none (use Link)"
    )
    characters = db.list_characters(bot.conn, player.id)
    return discord.Embed(
        title=player.name,
        description=(
            f"**Discord:** {account}\n**Status:** {dict(PLAYER_STATUS_OPTIONS)[player.status]}\n"
            f"**Characters:** {', '.join(c['ign'] for c in characters) or 'none'}"
        )[:4000],
        color=EMBED_COLOR,
    )


def _player_status_select(current: str) -> discord.ui.Select:
    return discord.ui.Select(options=[
        discord.SelectOption(label=label, value=value, default=value == current) for value, label in PLAYER_STATUS_OPTIONS
    ])


class PlayerPanel(discord.ui.View):
    """The /player message: Add, Change and Link."""

    def __init__(self, bot: MonkeyBot):
        super().__init__(timeout=900)
        self.bot = bot
        for label, emoji, style, make in [
            ("Add", "➕", discord.ButtonStyle.success, lambda: AddPlayerModal(self.bot)),
            ("Change", "✏️", discord.ButtonStyle.primary, lambda: FindPlayerModal(self.bot)),
            ("Link", "🔗", discord.ButtonStyle.secondary, lambda: LinkPlayerModal(self.bot)),
        ]:
            button = discord.ui.Button(label=label, emoji=emoji, style=style)
            button.callback = CharacterPanel._opener(make)
            self.add_item(button)

    def embed(self) -> discord.Embed:
        return discord.Embed(
            title="Players",
            description=(
                "➕ **Add** a player to the roster.\n"
                "✏️ **Change** a player's name or status.\n"
                "🔗 **Link** a roster player to their Discord account.\n\n"
                "Type player names; they're matched against the roster when you submit.\n"
                "Tip: `/player change:` plus a name (search as you type) opens a player's edit pop-up straight away."
            ),
            color=EMBED_COLOR,
        )


class AddPlayerModal(discord.ui.Modal):
    def __init__(self, bot: MonkeyBot):
        super().__init__(title="Add a player", timeout=900)
        self.bot = bot
        self.name = discord.ui.TextInput(max_length=100, placeholder="The name the player goes by")
        self.member = discord.ui.UserSelect(required=False, min_values=0)
        self.username = discord.ui.TextInput(required=False, max_length=40, placeholder="e.g. futureplayer")
        self.status = _player_status_select("active")
        self.add_item(discord.ui.Label(text="Name", component=self.name))
        self.add_item(discord.ui.Label(text="Discord account", component=self.member, description="Recommended"))
        self.add_item(discord.ui.Label(
            text="Discord username", component=self.username,
            description="Only if they're not in the server yet: links when they first use the bot",
        ))
        self.add_item(discord.ui.Label(text="Status", component=self.status))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            note = add_player_entry(
                self.bot, name=self.name.value, member=self.member.values[0] if self.member.values else None,
                username=self.username.value, status=_value(self.status),
            )
        except ValueError as e:
            await _refuse(interaction, e)
            return
        await interaction.response.send_message(note, ephemeral=True)


class FindPlayerModal(discord.ui.Modal):
    """Change, step 1: which player? Then their card with an Edit button."""

    def __init__(self, bot: MonkeyBot):
        super().__init__(title="Change a player", timeout=900)
        self.bot = bot
        self.player = discord.ui.TextInput(max_length=100, placeholder="Name, Discord username, or one of their characters")
        self.add_item(discord.ui.Label(text="Player", component=self.player))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            player = find_player(self.bot.conn, self.player.value)
        except ValueError as e:
            await interaction.response.send_message(str(e), ephemeral=True)
            return
        view = PlayerEditView(self.bot, player.id)
        await interaction.response.send_message(embed=view.embed(), view=view, ephemeral=True)


class PlayerEditView(discord.ui.View):
    """Change, step 2: the player's details and an Edit button that opens the filled-in pop-up."""

    def __init__(self, bot: MonkeyBot, player_id: int):
        super().__init__(timeout=900)
        self.bot = bot
        self.player_id = player_id
        button = discord.ui.Button(label="Edit", emoji="✏️", style=discord.ButtonStyle.primary)
        button.callback = self._edit
        self.add_item(button)

    def player(self) -> db.Player | None:
        return db.get_player(self.bot.conn, self.player_id)

    def embed(self) -> discord.Embed:
        return player_card(self.bot, self.player())

    async def _edit(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(EditPlayerModal(self, self.player()))


class EditPlayerModal(discord.ui.Modal):
    """`from_card`: opened from the player's card (refreshed on save); otherwise (from /player change:)
    the result is sent as a new message."""

    def __init__(self, view: PlayerEditView, player: db.Player, *, from_card: bool = True):
        super().__init__(title=f"Change {player.name}"[:45], timeout=900)
        self.view = view
        self.player = player
        self.from_card = from_card
        self.name = discord.ui.TextInput(default=player.name, max_length=100)
        self.status = _player_status_select(player.status)
        self.add_item(discord.ui.Label(text="Name", component=self.name))
        self.add_item(discord.ui.Label(
            text="Status", component=self.status, description="Only active players are counted in check-ins"
        ))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        note = edit_player_entry(self.view.bot, self.player, name=self.name.value, status=_value(self.status))
        if self.from_card:
            await interaction.response.edit_message(content=note, embed=self.view.embed(), view=self.view)
        else:
            await interaction.response.send_message(note, embed=self.view.embed(), view=self.view, ephemeral=True)


class LinkPlayerModal(discord.ui.Modal):
    def __init__(self, bot: MonkeyBot):
        super().__init__(title="Link a player to Discord", timeout=900)
        self.bot = bot
        self.player = discord.ui.TextInput(max_length=100, placeholder="Name, Discord username, or one of their characters")
        self.member = discord.ui.UserSelect()
        self.add_item(discord.ui.Label(text="Player", component=self.player))
        self.add_item(discord.ui.Label(text="Discord account", component=self.member))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            note = link_player_entry(self.bot, player=self.player.value, member=self.member.values[0])
        except ValueError as e:
            await _refuse(interaction, e)
            return
        await interaction.response.send_message(note, ephemeral=True)


class PlayerAdminCog(HostOnly, commands.Cog):
    @app_commands.command(name="player", description="Add, change or link players on the roster")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    @app_commands.describe(change="A player to change: opens their edit pop-up straight away")
    @app_commands.autocomplete(change=player_autocomplete)
    async def player(self, interaction: discord.Interaction, change: str | None = None):
        if change is None:
            panel = PlayerPanel(self.bot)
            await interaction.response.send_message(embed=panel.embed(), view=panel, ephemeral=True)
            return
        # the suggestions' values are player ids; anything typed by hand is matched like the pop-ups
        player = db.get_player(self.bot.conn, int(change)) if change.isdigit() else None
        if player is None:
            try:
                player = find_player(self.bot.conn, change)
            except ValueError as e:
                await interaction.response.send_message(str(e), ephemeral=True)
                return
        view = PlayerEditView(self.bot, player.id)
        await interaction.response.send_modal(EditPlayerModal(view, player, from_card=False))


# --------------------------------------------------------------------------- /character

# One command, /character, shows Add / Change / Remove buttons. Pop-ups can't search and a dropdown
# holds only 25 options, so hosts type the player or character name and it's matched on submit. The
# functions below do the work (and are what the tests call); the pop-ups collect the input.

CHARACTER_STATUS_OPTIONS = [("static", "Static (prioritize)"), ("sub", "Flex (only if needed)"), ("inactive", "Inactive (don't slot)")]


def find_player(conn, text: str) -> db.Player:
    """The one roster player matching `text` (name, Discord username, or one of their characters).
    Raises ValueError with suggestions when there's no single match."""
    text = text.strip()
    if not text:
        raise ValueError("Enter a player name.")
    matches = db.search_players(conn, text, limit=25)
    wanted = text.lower().lstrip("@")
    exact = [p for p in matches if wanted in (p.name.lower(), (p.discord_handle or "").lower().lstrip("@"))]
    if len(exact) == 1:
        return exact[0]
    if len(matches) == 1:
        return matches[0]
    hint = f" Did you mean: {', '.join(p.display for p in matches[:5])}?" if matches else ""
    raise ValueError(f"No single player matches **{text}**.{hint}")


def find_character(conn, text: str):
    text = text.strip()
    if not text:
        raise ValueError("Enter a character's in-game name.")
    if char := db.get_character(conn, text):
        return char
    rows = conn.execute(
        "SELECT ign FROM characters WHERE ign LIKE ? ORDER BY ign COLLATE NOCASE LIMIT 6", (f"%{text}%",)
    ).fetchall()
    if len(rows) == 1:
        return db.get_character(conn, rows[0]["ign"])
    hint = f" Did you mean: {', '.join(r['ign'] for r in rows[:5])}?" if rows else ""
    raise ValueError(f"No character named **{text}**.{hint}")


def parse_dmg(text: str | None) -> float | None:
    text = (text or "").strip()
    if not text:
        return None
    try:
        value = float(text)
    except ValueError:
        raise ValueError("Damage must be a number, e.g. `4.2`.") from None
    if value < 0:
        raise ValueError("Damage can't be negative.")
    return value


def add_character_entry(
    bot: MonkeyBot, *, player: str, ign: str, job: str, dmg: str | None, status: str = "static"
) -> str:
    """The buff comes from the job (db.JOB_BUFFS)."""
    conn = bot.conn
    owner = find_player(conn, player)
    ign, job = ign.strip(), job.strip().upper()
    if not ign:
        raise ValueError("Enter the character's in-game name.")
    if existing := db.get_character(conn, ign):
        raise ValueError(
            f"**{existing['ign']}** already exists (owned by {db.get_player(conn, existing['player_id']).display})."
        )
    if (buff := db.buff_for_job(job)) is None:
        raise ValueError(f"**{job}** isn't a known job, so its buff isn't known. Pick a job from the list.")
    db.add_character(conn, owner.id, ign, job, parse_dmg(dmg), status)
    return f"✅ Added **{ign}** ({job}, {buff}) to **{owner.display}**, as {character_status_label(status)}."


def edit_character_entry(
    bot: MonkeyBot, char, *, job: str, dmg: str | None, status: str, owner: str | None, changed_by: int
) -> str:
    """Saves only what changed; a new job also sets the buff. Raises ValueError (nothing saved) for a
    bad damage or owner."""
    conn = bot.conn
    new_dmg = parse_dmg(dmg)
    new_job = (job or "").strip().upper()
    job_changed = bool(new_job) and new_job != (char["job"] or "")
    note = f" Buff: {db.buff_for_job(new_job) or '?'} (from the job)." if job_changed else ""
    new_owner = find_player(conn, owner) if (owner or "").strip() else None
    if new_owner and new_owner.id == char["player_id"]:
        new_owner = None  # already theirs
    old_owner = db.get_player(conn, char["player_id"])
    db.update_character(
        conn,
        char["id"],
        changed_by=changed_by,
        job=new_job if job_changed else None,
        dmg=new_dmg if new_dmg is not None and new_dmg != char["base_dmg"] else None,
        status=status if status != char["status"] else None,
        player_id=new_owner.id if new_owner else None,
    )
    moved = f" Moved from **{old_owner.display}** to **{new_owner.display}**." if new_owner else ""
    return f"✅ Updated **{char['ign']}**.{note}{moved}"


def remove_characters(bot: MonkeyBot, character_ids: list[int]) -> str:
    names = []
    for cid in character_ids:
        row = bot.conn.execute("SELECT ign FROM characters WHERE id = ?", (cid,)).fetchone()
        if row:
            db.delete_character(bot.conn, cid)
            names.append(row["ign"])
    if not names:
        return "Nothing removed."
    return f"🗑️ Removed {', '.join(f'**{n}**' for n in names)} and their damage history."


def character_card(bot: MonkeyBot, char) -> discord.Embed:
    owner = db.get_player(bot.conn, char["player_id"])
    dmg = f"{char['dmg']:g}" if char["dmg"] is not None else "?"
    base = f" (entered: {char['base_dmg']:g})" if char["base_dmg"] is not None and char["base_dmg"] != char["dmg"] else ""
    return discord.Embed(
        title=f"{char['ign']} ({char['job'] or '?'})",
        description=(
            f"**Owner:** {owner.display}\n**Buff:** {char['buff'] or '?'}\n**Damage:** {dmg}{base}\n"
            f"**Status:** {character_status_label(char['status'])}"
        ),
        color=EMBED_COLOR,
    )


def _job_input(bot: MonkeyBot, current: str | None = None):
    """A dropdown of the jobs (with class icons and their buff); a text box if there are more than fit."""
    jobs = sorted(set(db.JOB_BUFFS) | set(db.known_jobs(bot.conn)) | ({current.upper()} if current else set()))
    if not jobs or len(jobs) > 25:
        return discord.ui.TextInput(default=current, max_length=10, placeholder="e.g. NL, DRK, BSP")
    options = []
    for job in jobs:
        icon = bot.class_icons.get(job)
        buff = db.buff_for_job(job)
        options.append(discord.SelectOption(
            label=job, value=job, default=job == (current or "").upper(),
            description=f"Buff: {buff}" if buff else None,
            emoji=discord.PartialEmoji.from_str(icon) if icon else None,
        ))
    return discord.ui.Select(options=options)


def _value(component) -> str:
    """The text typed, or the option picked, in a pop-up field."""
    if isinstance(component, discord.ui.TextInput):
        return component.value
    return component.values[0] if component.values else ""


def _status_select(current: str) -> discord.ui.Select:
    return discord.ui.Select(options=[
        discord.SelectOption(label=label, value=value, default=value == current)
        for value, label in CHARACTER_STATUS_OPTIONS
    ])


class CharacterPanel(discord.ui.View):
    """The /character message: Add, Change and Remove."""

    def __init__(self, bot: MonkeyBot):
        super().__init__(timeout=900)
        self.bot = bot
        for label, emoji, style, make in [
            ("Add", "➕", discord.ButtonStyle.success, lambda: AddCharacterModal(self.bot)),
            ("Change", "✏️", discord.ButtonStyle.primary, lambda: FindCharacterModal(self.bot)),
            ("Remove", "🗑️", discord.ButtonStyle.danger, lambda: FindPlayerToRemoveModal(self.bot)),
        ]:
            button = discord.ui.Button(label=label, emoji=emoji, style=style)
            button.callback = self._opener(make)
            self.add_item(button)

    @staticmethod
    def _opener(make):
        async def open_modal(interaction: discord.Interaction) -> None:
            await interaction.response.send_modal(make())

        return open_modal

    def embed(self) -> discord.Embed:
        return discord.Embed(
            title="Characters",
            description=(
                "➕ **Add** a character to a player.\n"
                "✏️ **Change** a character's job, damage, status or owner.\n"
                "🗑️ **Remove** one or more of a player's characters.\n\n"
                "Type player and character names; they're matched against the roster when you submit.\n"
                "Tip: `/character change:` plus a name (search as you type) opens a character's edit pop-up "
                "straight away."
            ),
            color=EMBED_COLOR,
        )


async def _refuse(interaction: discord.Interaction, error: ValueError) -> None:
    await interaction.response.send_message(f"Nothing saved: {error}", ephemeral=True)


class AddCharacterModal(discord.ui.Modal):
    def __init__(self, bot: MonkeyBot):
        super().__init__(title="Add a character", timeout=900)
        self.bot = bot
        self.player = discord.ui.TextInput(max_length=100, placeholder="Name, Discord username, or one of their characters")
        self.ign = discord.ui.TextInput(max_length=50)
        self.job = _job_input(bot)
        self.dmg = discord.ui.TextInput(required=False, max_length=10, placeholder="e.g. 4.2 (optional)")
        self.status = _status_select("static")
        self.add_item(discord.ui.Label(text="Player", component=self.player))
        self.add_item(discord.ui.Label(text="In-game name", component=self.ign))
        self.add_item(discord.ui.Label(
            text="Job", component=self.job, description="The buff is set from the job"
        ))
        self.add_item(discord.ui.Label(text="Damage", component=self.dmg, description="Fallback until damage logs come in"))
        self.add_item(discord.ui.Label(text="Status", component=self.status))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            note = add_character_entry(
                self.bot, player=self.player.value, ign=self.ign.value, job=_value(self.job),
                dmg=self.dmg.value, status=_value(self.status),
            )
        except ValueError as e:
            await _refuse(interaction, e)
            return
        await interaction.response.send_message(note, ephemeral=True)


class FindCharacterModal(discord.ui.Modal):
    """Change, step 1: which character? Then its card with an Edit button."""

    def __init__(self, bot: MonkeyBot):
        super().__init__(title="Change a character", timeout=900)
        self.bot = bot
        self.ign = discord.ui.TextInput(max_length=50)
        self.add_item(discord.ui.Label(text="In-game name", component=self.ign, description="The character to change"))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            char = find_character(self.bot.conn, self.ign.value)
        except ValueError as e:
            await interaction.response.send_message(str(e), ephemeral=True)
            return
        view = CharacterEditView(self.bot, char["id"])
        await interaction.response.send_message(embed=view.embed(), view=view, ephemeral=True)


class CharacterEditView(discord.ui.View):
    """Change, step 2: the character's details and an Edit button that opens the filled-in pop-up."""

    def __init__(self, bot: MonkeyBot, character_id: int):
        super().__init__(timeout=900)
        self.bot = bot
        self.character_id = character_id
        button = discord.ui.Button(label="Edit", emoji="✏️", style=discord.ButtonStyle.primary)
        button.callback = self._edit
        self.add_item(button)

    def character(self):
        return db.get_character_by_id(self.bot.conn, self.character_id)

    def embed(self) -> discord.Embed:
        return character_card(self.bot, self.character())

    async def _edit(self, interaction: discord.Interaction) -> None:
        if (char := self.character()) is None:
            await interaction.response.edit_message(content="That character was removed.", embed=None, view=None)
            return
        await interaction.response.send_modal(EditCharacterModal(self, char))


class EditCharacterModal(discord.ui.Modal):
    """The filled-in edit pop-up. `from_card`: opened from the character's card (Edit button), which is
    refreshed on save; otherwise (from /character change:) the result is sent as a new message."""

    def __init__(self, view: CharacterEditView, char, *, from_card: bool = True):
        super().__init__(title=f"Change {char['ign']}"[:45], timeout=900)
        self.view = view
        self.char = char
        self.from_card = from_card
        owner = db.get_player(view.bot.conn, char["player_id"])
        self.job = _job_input(view.bot, char["job"])
        self.dmg = discord.ui.TextInput(
            required=False, max_length=10,
            default=f"{char['base_dmg']:g}" if char["base_dmg"] is not None else None,
        )
        self.status = _status_select(char["status"])
        self.owner = discord.ui.TextInput(required=False, max_length=100, placeholder=f"Leave empty to keep {owner.name}"[:100])
        self.add_item(discord.ui.Label(text="Job", component=self.job, description="The buff is set from the job"))
        self.add_item(discord.ui.Label(
            text="Damage", component=self.dmg, description="Fallback value; damage from logs takes priority"
        ))
        self.add_item(discord.ui.Label(text="Status", component=self.status))
        self.add_item(discord.ui.Label(text="Move to player", component=self.owner, description=f"Owner now: {owner.display}"[:100]))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            note = edit_character_entry(
                self.view.bot, self.char, job=_value(self.job), dmg=self.dmg.value,
                status=_value(self.status), owner=self.owner.value, changed_by=interaction.user.id,
            )
        except ValueError as e:
            await _refuse(interaction, e)
            return
        if self.from_card:
            await interaction.response.edit_message(content=note, embed=self.view.embed(), view=self.view)
        else:
            await interaction.response.send_message(note, embed=self.view.embed(), view=self.view, ephemeral=True)


class FindPlayerToRemoveModal(discord.ui.Modal):
    """Remove, step 1: whose characters? Then a list with a button to pick which to remove."""

    def __init__(self, bot: MonkeyBot):
        super().__init__(title="Remove characters", timeout=900)
        self.bot = bot
        self.player = discord.ui.TextInput(max_length=100, placeholder="Name, Discord username, or one of their characters")
        self.add_item(discord.ui.Label(text="Player", component=self.player))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            player = find_player(self.bot.conn, self.player.value)
        except ValueError as e:
            await interaction.response.send_message(str(e), ephemeral=True)
            return
        view = RemoveCharactersView(self.bot, player)
        if not view.characters:
            await interaction.response.send_message(f"**{player.display}** has no characters.", ephemeral=True)
            return
        await interaction.response.send_message(embed=view.embed(), view=view, ephemeral=True)


class RemoveCharactersView(discord.ui.View):
    MAX = 4 * 10  # a pop-up fits 4 checkbox groups of 10 below its warning

    def __init__(self, bot: MonkeyBot, player: db.Player):
        super().__init__(timeout=900)
        self.bot = bot
        self.player = player
        self.characters = db.list_characters(bot.conn, player.id)
        button = discord.ui.Button(label="Choose characters to remove", emoji="🗑️", style=discord.ButtonStyle.danger)
        button.callback = self._choose
        self.add_item(button)

    def embed(self) -> discord.Embed:
        lines = [f"{character_status_label(c['status'])} · **{c['ign']}** ({c['job'] or '?'})" for c in self.characters]
        return discord.Embed(
            title=f"{self.player.display}'s characters ({len(self.characters)})",
            description="\n".join(lines)[:4000],
            color=EMBED_COLOR,
        )

    async def _choose(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(RemoveCharactersModal(self))


class RemoveCharactersModal(discord.ui.Modal):
    def __init__(self, view: RemoveCharactersView):
        super().__init__(title=f"Remove {view.player.name}'s characters"[:45], timeout=900)
        self.view = view
        self.add_item(discord.ui.TextDisplay(
            "Tick the characters to remove. Their damage history is deleted too, and this can't be undone."
        ))
        self.groups = []
        chunks = popup_chunks(view.characters[: RemoveCharactersView.MAX])
        for index, chunk in enumerate(chunks):
            group = discord.ui.CheckboxGroup(
                required=False, min_values=0, max_values=len(chunk),
                options=[discord.CheckboxGroupOption(label=f"{c['ign']} ({c['job'] or '?'})"[:100], value=str(c["id"])) for c in chunk],
            )
            self.groups.append(group)
            text = "Characters" + (f" ({index + 1} of {len(chunks)})" if len(chunks) > 1 else "")
            self.add_item(discord.ui.Label(text=text, component=group))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        ids = [int(v) for g in self.groups for v in g.values]
        if not ids:
            await interaction.response.send_message("Nothing ticked, so nothing was removed.", ephemeral=True)
            return
        note = remove_characters(self.view.bot, ids)
        await interaction.response.edit_message(content=note, embed=None, view=None)


class CharacterAdminCog(HostOnly, commands.Cog):
    @app_commands.command(name="character", description="Add, change or remove characters on the roster")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    @app_commands.describe(change="A character to change: opens its edit pop-up straight away")
    @app_commands.autocomplete(change=character_autocomplete)
    async def character(self, interaction: discord.Interaction, change: str | None = None):
        if change is None:
            panel = CharacterPanel(self.bot)
            await interaction.response.send_message(embed=panel.embed(), view=panel, ephemeral=True)
            return
        try:
            char = find_character(self.bot.conn, change)
        except ValueError as e:
            await interaction.response.send_message(str(e), ephemeral=True)
            return
        view = CharacterEditView(self.bot, char["id"])
        await interaction.response.send_modal(EditCharacterModal(view, char, from_card=False))


async def setup(bot: MonkeyBot) -> None:
    for cog in (HostCog, ConfigCog, PlayerAdminCog, CharacterAdminCog):
        await bot.add_cog(cog(bot))
