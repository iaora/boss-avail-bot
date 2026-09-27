"""Embeds, buttons and the availability editor shared by player and host commands."""

from __future__ import annotations

import math
import re
from datetime import date, timedelta
from typing import TYPE_CHECKING

import discord

from . import damage, db, reminders, timeutil

if TYPE_CHECKING:
    from .app import MonkeyBot

LEVEL_EMOJI = {"Preferred": "🟢", "Available": "🟡", "Not Available": "🔴"}
UNSET_EMOJI = "⚪"
EMBED_COLOR = discord.Color.from_rgb(201, 142, 76)

NOT_REGISTERED = (
    "I couldn't find you on the CQ roster. Ask a host to add you (or, if you're already on the "
    "roster under a different Discord account, to link you)."
)


# --------------------------------------------------------------------------- squad order

# Every list of squads shows in time order ("time") until the user presses 🔀 to switch to squad
# number order ("number"). The choice is remembered per Discord user and used by every view,
# for players and hosts alike.
SQUAD_ORDERS = ("time", "number")


def get_squad_order(conn, user_id: int | None) -> str:
    """The user's remembered squad order ("time" unless they switched it)."""
    order = db.get_setting(conn, f"squad_order_user:{user_id}", "time") if user_id else "time"
    return order if order in SQUAD_ORDERS else "time"


def set_squad_order(conn, user_id: int | None, order: str) -> None:
    if user_id:
        db.set_setting(conn, f"squad_order_user:{user_id}", order)


LAYOUTS = ("list", "grouped")  # "list" = every squad in order (default); "grouped" = by availability level


def get_layout(conn, player_id: int) -> str:
    """How the player likes their availability shown, remembered from their last toggle."""
    layout = db.get_setting(conn, f"avail_layout:{player_id}", "list")
    return layout if layout in LAYOUTS else "list"


def set_layout(conn, player_id: int, layout: str) -> None:
    db.set_setting(conn, f"avail_layout:{player_id}", layout)


def other_layout(layout: str) -> str:
    return "grouped" if layout == "list" else "list"


def layout_button_label(layout: str) -> str:
    """Label for a button that switches TO `layout`."""
    return "Group by availability" if layout == "grouped" else "Show full list"


def order_squads(squads: list[db.SquadTime], order: str) -> list[db.SquadTime]:
    if order == "number":
        return sorted(squads, key=lambda s: s.number)
    return sorted(squads, key=lambda s: (s.starts_at, s.number))


def other_order(order: str) -> str:
    return "number" if order == "time" else "time"


def order_button_label(order: str) -> str:
    """Label for a button that switches TO `order`."""
    return "Sort by squad #" if order == "number" else "Sort by time"


def squad_time_lines(squads: list[db.SquadTime]) -> str:
    return "\n".join(f"**Squad {s.number}** · {timeutil.discord_ts(s.starts_at)}" for s in squads) or "*None*"


class OrderToggleView(discord.ui.View):
    """A 🔀 button that re-renders a private message with its squads in the other order and
    remembers the choice for the user. `render(order)` builds the embed for "time" or "number"."""

    def __init__(self, render, conn, user_id: int, timeout: float = 900):
        super().__init__(timeout=timeout)
        self.render = render
        self.conn = conn
        self.user_id = user_id
        self.order = get_squad_order(conn, user_id)
        self._build()

    def _build(self) -> None:
        self.clear_items()
        button = discord.ui.Button(
            label=order_button_label(other_order(self.order)), emoji="🔀", style=discord.ButtonStyle.secondary
        )
        button.callback = self._toggle
        self.add_item(button)

    def embed(self) -> discord.Embed:
        return self.render(self.order)

    async def _toggle(self, interaction: discord.Interaction) -> None:
        self.order = other_order(self.order)
        set_squad_order(self.conn, self.user_id, self.order)
        self._build()
        await interaction.response.edit_message(embed=self.embed(), view=self)


# --------------------------------------------------------------------------- rendering

def availability_lines(squads: list[db.SquadTime], availability: db.Availability) -> str:
    lines = []
    for s in squads:
        level = availability.get(s.number)
        emoji = LEVEL_EMOJI.get(level, UNSET_EMOJI)
        lines.append(f"{emoji} **Squad {s.number}** · {timeutil.discord_ts(s.starts_at, 'f')} · {level or 'Not set'}")
    return "\n".join(lines) or "*No squads are configured.*"


def grouped_lines(squads: list[db.SquadTime], availability: db.Availability) -> str:
    """Squads sectioned by level (Preferred, Available, Not Available, then any not set)."""
    sections = []
    for level, emoji in [*LEVEL_EMOJI.items(), (None, UNSET_EMOJI)]:
        members = [s for s in squads if availability.get(s.number) == level]
        if not members and level is None:
            continue
        name = level or "Not set"
        body = "\n".join(f"**Squad {s.number}** · {timeutil.discord_ts(s.starts_at, 'f')}" for s in members) or "*None*"
        sections.append(f"{emoji} **{name} ({len(members)})**\n{body}")
    return "\n\n".join(sections) if squads else "*No squads are configured.*"


def confirmation_text(kind: str | None) -> str:
    return {
        "no_change": "✅ Confirmed: no change (default schedule)",
        "updated": "✅ Confirmed: submitted this week's availability",
    }.get(kind, "⏳ Not confirmed yet")


def player_summary_embed(
    bot: "MonkeyBot", player: db.Player, week_start: date, order: str | None = None, layout: str | None = None
) -> discord.Embed:
    """A player's availability for one week (their change for that week, else their default)."""
    order = order or get_squad_order(bot.conn, player.discord_id)
    layout = layout or get_layout(bot.conn, player.id)
    squads = order_squads(db.squads_for_week(bot.conn, week_start, bot.tz), order)
    default = db.get_default_availability(bot.conn, player.id)
    availability, source = db.effective_week_availability(bot.conn, player.id, week_start)
    kind = db.get_confirmation(bot.conn, player.id, week_start)

    note = (
        "*Changed for this week only. Your default schedule applies to other weeks.*"
        if source == "weekly"
        else "*Your default schedule*"
    )
    body = grouped_lines(squads, availability) if layout == "grouped" else availability_lines(squads, availability)
    embed = discord.Embed(
        title=f"{player.name}: {timeutil.week_label(week_start)}",
        description=f"{note}\n\n{body}",
        color=EMBED_COLOR,
    )
    embed.add_field(name="Check-in", value=confirmation_text(kind), inline=False)
    if source == "weekly":
        diffs = [
            f"Squad {s.number}: {LEVEL_EMOJI.get(default.get(s.number), UNSET_EMOJI)} → "
            f"{LEVEL_EMOJI.get(availability.get(s.number), UNSET_EMOJI)}"
            for s in squads
            if default.get(s.number) != availability.get(s.number)
        ]
        if diffs:
            embed.add_field(name="Different from your default", value="\n".join(diffs)[:1024], inline=False)
    later = [w for w in db.weeks_with_changes(bot.conn, player.id, week_start) if w != week_start]
    if later:
        embed.add_field(
            name="Changes submitted for later weeks",
            value="\n".join(f"✏️ {timeutil.week_label(w, capital=True)}" for w in later),
            inline=False,
        )
    sorted_by = "time" if order == "time" else "squad number"
    shown_as = "full list" if layout == "list" else "grouped by availability"
    embed.set_footer(
        text=f"Status: {player.status} · Sorted by {sorted_by} · Showing {shown_as} · "
        "🟢 Preferred  🟡 Available  🔴 Not Available"
    )
    return embed


def reminder_message(bot: "MonkeyBot", week_start: date) -> tuple[discord.Embed, discord.ui.View]:
    settings = reminders.load(bot.conn, bot.config.cq_channel_id)
    squads = db.squads_for_week(bot.conn, week_start, bot.tz)
    deadline = settings.deadline_at(week_start, bot.tz)
    embed = discord.Embed(
        title=f"📅 CQ availability: {timeutil.week_label(week_start)}",
        description=(
            f"Please confirm your availability for next week by **{timeutil.discord_ts(deadline)}** "
            f"({timeutil.discord_ts(deadline, 'R')}).\n\n"
            "• **View my availability** to see your schedule for that week (only you can see it)\n"
            "• **No change** if your default schedule works this week\n"
            "• **Update this week** to set different availability for just this week"
        ),
        color=EMBED_COLOR,
    )
    embed.add_field(
        name="Squad times (shown in your timezone)",
        value=squad_time_lines(order_squads(squads, "time")),
        inline=False,
    )
    view = discord.ui.View(timeout=None)
    wk = week_start.isoformat()
    for action in ("view", "nochange", "update"):
        view.add_item(ReminderButton(action, wk))
    return embed, view


def squad_times_embed(bot: "MonkeyBot", week_start: date, order: str) -> discord.Embed:
    squads = order_squads(db.squads_for_week(bot.conn, week_start, bot.tz), order)
    return discord.Embed(
        title=f"Squad times: {timeutil.week_label(week_start)}",
        description=squad_time_lines(squads),
        color=EMBED_COLOR,
    ).set_footer(text=f"Shown in your timezone · sorted by {'time' if order == 'time' else 'squad number'}")


async def send_reminder(bot: "MonkeyBot", channel: discord.abc.Messageable, week_start: date) -> discord.Message:
    """Post the reminder, pinging the player role (PLAYER_ROLE_ID) if one is configured."""
    embed, view = reminder_message(bot, week_start)
    role_id = bot.config.player_role_id
    return await channel.send(
        content=f"<@&{role_id}>" if role_id else None,
        embed=embed,
        view=view,
        allowed_mentions=discord.AllowedMentions(
            everyone=False, users=False, roles=[discord.Object(id=role_id)] if role_id else False
        ),
    )


# --------------------------------------------------------------------------- persistent buttons

BUTTONS = {
    "view": ("View my availability", discord.ButtonStyle.secondary, "📋"),
    "nochange": ("No change", discord.ButtonStyle.success, "✅"),
    "update": ("Update this week", discord.ButtonStyle.primary, "✏️"),
    "default": ("Update my default", discord.ButtonStyle.secondary, "🛠️"),
    "plan": ("Update a week", discord.ButtonStyle.primary, "🗓️"),
    "chars": ("Character availability", discord.ButtonStyle.secondary, "🧩"),
}


class ReminderButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"avail:(?P<action>view|nochange|update|default|plan|chars)(?::(?P<week>\d{4}-\d{2}-\d{2}))?",
):
    """Buttons whose state lives in the custom_id, so they keep working after a bot restart."""

    def __init__(self, action: str, week: str | None = None, row: int | None = None):
        label, style, emoji = BUTTONS[action]
        custom_id = f"avail:{action}" + (f":{week}" if week else "")
        super().__init__(discord.ui.Button(label=label, style=style, emoji=emoji, custom_id=custom_id, row=row))
        self.action = action
        self.week = date.fromisoformat(week) if week else None

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str], /):
        return cls(match["action"], match["week"])

    async def callback(self, interaction: discord.Interaction) -> None:
        bot: MonkeyBot = interaction.client  # type: ignore[assignment]
        player = bot.resolve_player(interaction.user)
        if player is None:
            await interaction.response.send_message(NOT_REGISTERED, ephemeral=True)
            return

        week = self.week or timeutil.upcoming_week_start(timeutil.now_utc(), bot.tz)
        if self.action in ("nochange", "update") and week < timeutil.current_week_start(timeutil.now_utc(), bot.tz):
            await interaction.response.send_message(
                "This reminder is for a week that has already finished.", ephemeral=True
            )
            return

        if self.action == "view":
            await send_player_summary(interaction, bot, player, week)
        elif self.action == "nochange":
            db.confirm_no_change(bot.conn, player.id, week)
            embed = player_summary_embed(bot, player, week, get_squad_order(bot.conn, interaction.user.id))
            await interaction.response.send_message(
                f"Thanks, {player.name}! Your default schedule will be used for the {timeutil.week_label(week)}.",
                embed=embed,
                ephemeral=True,
            )
        elif self.action == "update":
            await open_weekly_editor(interaction, bot, player, week)
        elif self.action == "default":
            await open_default_editor(interaction, bot, player, week)
        elif self.action == "plan":
            await WeekPicker(bot, player).send(interaction)
        elif self.action == "chars":
            await CharacterPicker(bot, player, week).send(interaction)


class SortToggleButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"avail-sort:(?P<order>time|number):(?P<week>\d{4}-\d{2}-\d{2})",
):
    """On the /cq availability summary: switch to `order` and remember it for this player."""

    def __init__(self, order: str, week: str, row: int | None = None):
        super().__init__(
            discord.ui.Button(
                label=order_button_label(order),
                style=discord.ButtonStyle.secondary,
                emoji="🔀",
                custom_id=f"avail-sort:{order}:{week}",
                row=row,
            )
        )
        self.order = order
        self.week = date.fromisoformat(week)

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str], /):
        return cls(match["order"], match["week"])

    async def callback(self, interaction: discord.Interaction) -> None:
        bot: MonkeyBot = interaction.client  # type: ignore[assignment]
        player = bot.resolve_player(interaction.user)
        if player is None:
            await interaction.response.send_message(NOT_REGISTERED, ephemeral=True)
            return
        set_squad_order(bot.conn, interaction.user.id, self.order)
        await rerender_summary(interaction, bot, player, self.week)


class LayoutToggleButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"avail-layout:(?P<layout>list|grouped)(?::(?P<order>time|number))?:(?P<week>\d{4}-\d{2}-\d{2})",
):
    """On the availability summary: switch between the full list and grouped-by-level view.

    (The optional order part of the id is from older messages and is ignored.)"""

    def __init__(self, layout: str, week: str, row: int | None = None):
        super().__init__(
            discord.ui.Button(
                label=layout_button_label(layout),
                style=discord.ButtonStyle.secondary,
                emoji="📊",
                custom_id=f"avail-layout:{layout}:{week}",
                row=row,
            )
        )
        self.layout = layout
        self.week = date.fromisoformat(week)

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str], /):
        return cls(match["layout"], match["week"])

    async def callback(self, interaction: discord.Interaction) -> None:
        bot: MonkeyBot = interaction.client  # type: ignore[assignment]
        player = bot.resolve_player(interaction.user)
        if player is None:
            await interaction.response.send_message(NOT_REGISTERED, ephemeral=True)
            return
        set_layout(bot.conn, player.id, self.layout)
        await rerender_summary(interaction, bot, player, self.week)


class SquadTimesButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"avail-times:(?P<order>time|number):(?P<week>\d{4}-\d{2}-\d{2})",
):
    """Show the squad times privately in `order`.

    No longer added to new reminders (players sort via "View my availability" instead); kept so the
    button on reminders posted before that change still works. Pressed on the reminder it sends a
    private copy; pressed on that copy it re-sorts it."""

    def __init__(self, order: str, week: str):
        super().__init__(
            discord.ui.Button(
                label=order_button_label(order),
                style=discord.ButtonStyle.secondary,
                emoji="🔀",
                custom_id=f"avail-times:{order}:{week}",
            )
        )
        self.order = order
        self.week = date.fromisoformat(week)

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str], /):
        return cls(match["order"], match["week"])

    async def callback(self, interaction: discord.Interaction) -> None:
        bot: MonkeyBot = interaction.client  # type: ignore[assignment]
        set_squad_order(bot.conn, interaction.user.id, self.order)
        embed = squad_times_embed(bot, self.week, self.order)
        view = discord.ui.View(timeout=None)
        view.add_item(SquadTimesButton(other_order(self.order), self.week.isoformat()))
        message = interaction.message
        if message is not None and message.flags.ephemeral:
            await interaction.response.edit_message(embed=embed, view=view)
        else:
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


async def rerender_summary(interaction: discord.Interaction, bot: "MonkeyBot", player: db.Player, week: date) -> None:
    order, layout = get_squad_order(bot.conn, interaction.user.id), get_layout(bot.conn, player.id)
    await interaction.response.edit_message(
        embed=player_summary_embed(bot, player, week, order, layout), view=player_summary_view(week, order, layout)
    )


def player_summary_view(week: date, order: str, layout: str) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    wk = week.isoformat()
    # row 0: actions; row 1: how the list is shown
    view.add_item(ReminderButton("nochange", wk, row=0))
    view.add_item(ReminderButton("plan", row=0))
    view.add_item(ReminderButton("default", wk, row=0))
    view.add_item(ReminderButton("chars", wk, row=0))
    view.add_item(SortToggleButton(other_order(order), wk, row=1))
    view.add_item(LayoutToggleButton(other_layout(layout), wk, row=1))
    return view


async def send_player_summary(
    interaction: discord.Interaction, bot: "MonkeyBot", player: db.Player, week: date
) -> None:
    order, layout = get_squad_order(bot.conn, interaction.user.id), get_layout(bot.conn, player.id)
    await interaction.response.send_message(
        embed=player_summary_embed(bot, player, week, order, layout),
        view=player_summary_view(week, order, layout),
        ephemeral=True,
    )


# --------------------------------------------------------------------------- week picker

ADVANCE_WEEKS = 8  # next week plus the 7 after it


class WeekPicker(discord.ui.View):
    """Lets a player pick which upcoming week to change, or reset a week back to their default."""

    def __init__(self, bot: "MonkeyBot", player: db.Player):
        super().__init__(timeout=900)
        self.bot = bot
        self.player = player
        first = timeutil.upcoming_week_start(timeutil.now_utc(), bot.tz)
        self.weeks = [first + timedelta(weeks=i) for i in range(ADVANCE_WEEKS)]
        self._rebuild()

    def changed_weeks(self) -> set[date]:
        return set(db.weeks_with_changes(self.bot.conn, self.player.id, self.weeks[0]))

    def _describe(self, week: date, changed: set[date]) -> str:
        when = "Next week" if week == self.weeks[0] else f"In {(week - self.weeks[0]).days // 7 + 1} weeks"
        return f"{when} · " + ("Changed" if week in changed else "Default schedule")

    def embed(self) -> discord.Embed:
        changed = self.changed_weeks()
        lines = [
            f"{'✏️' if w in changed else '▫️'} **{timeutil.week_label(w, capital=True)}** · {self._describe(w, changed)}"
            for w in self.weeks
        ]
        return discord.Embed(
            title="Which week do you want to change?",
            description="\n".join(lines)
            + "\n\nPick a week to set availability for just that week. Weeks you don't change use your "
            "default schedule. You can also reset a changed week back to your default.",
            color=EMBED_COLOR,
        )

    def _rebuild(self) -> None:
        self.clear_items()
        changed = self.changed_weeks()
        pick = discord.ui.Select(
            placeholder="Choose a week to change…",
            options=[
                discord.SelectOption(
                    label=f"Week of {timeutil.short_week_label(w)}",
                    value=w.isoformat(),
                    description=self._describe(w, changed),
                    emoji="✏️" if w in changed else "🗓️",
                )
                for w in self.weeks
            ],
            row=0,
        )
        pick.callback = self._pick
        self.add_item(pick)
        if changed:
            reset = discord.ui.Select(
                placeholder="Reset a week to your default…",
                options=[
                    discord.SelectOption(label=f"Week of {timeutil.short_week_label(w)}", value=w.isoformat(), emoji="↩️")
                    for w in self.weeks
                    if w in changed
                ],
                row=1,
            )
            reset.callback = self._reset
            self.add_item(reset)

    async def send(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(embed=self.embed(), view=self, ephemeral=True)

    async def _pick(self, interaction: discord.Interaction) -> None:
        week = date.fromisoformat(interaction.data["values"][0])
        self.stop()
        initial, _ = db.effective_week_availability(self.bot.conn, self.player.id, week)
        editor = AvailabilityEditor(self.bot, mode="weekly", player=self.player, week=week, initial=initial)
        await editor.send(interaction, replace=True)

    async def _reset(self, interaction: discord.Interaction) -> None:
        week = date.fromisoformat(interaction.data["values"][0])
        db.confirm_no_change(self.bot.conn, self.player.id, week)
        self._rebuild()
        await interaction.response.edit_message(
            content=f"↩️ The {timeutil.week_label(week)} now uses your default schedule.", embed=self.embed(), view=self
        )


# --------------------------------------------------------------------------- availability editor

async def open_weekly_editor(interaction: discord.Interaction, bot: "MonkeyBot", player: db.Player, week: date) -> None:
    initial, _ = db.effective_week_availability(bot.conn, player.id, week)
    editor = AvailabilityEditor(bot, mode="weekly", player=player, week=week, initial=initial)
    await editor.send(interaction)


async def open_default_editor(interaction: discord.Interaction, bot: "MonkeyBot", player: db.Player, week: date) -> None:
    initial = db.get_default_availability(bot.conn, player.id)
    editor = AvailabilityEditor(bot, mode="default", player=player, week=week, initial=initial)
    await editor.send(interaction)


async def open_character_editor(
    interaction: discord.Interaction, bot: "MonkeyBot", player: db.Player, character, week: date, *, replace=False
) -> None:
    base = db.get_default_availability(bot.conn, player.id)
    initial = {**base, **db.get_character_overrides(bot.conn, character["id"])}
    editor = AvailabilityEditor(bot, mode="character", player=player, week=week, initial=initial, character=character)
    await editor.send(interaction, replace=replace)


class CharacterPicker(discord.ui.View):
    """From /cq availability: pick one of your characters to give it its own availability."""

    def __init__(self, bot: "MonkeyBot", player: db.Player, week: date):
        super().__init__(timeout=900)
        self.bot = bot
        self.player = player
        self.week = week
        self.characters = {c["id"]: c for c in db.list_characters(bot.conn, player.id)}
        options = []
        for c in list(self.characters.values())[:25]:  # Discord allows 25 options per dropdown
            overrides = db.get_character_overrides(bot.conn, c["id"])
            emoji, status, _ = CHARACTER_STATUS_INFO[c["status"]]
            detail = f"{len(overrides)} squad(s) differ from your default" if overrides else "Follows your default"
            options.append(
                discord.SelectOption(
                    label=f"{c['ign']} ({c['job'] or '?'})"[:100], value=str(c["id"]),
                    description=f"{status} · {detail}"[:100], emoji=emoji,
                )
            )
        if options:
            select = discord.ui.Select(placeholder="Choose a character…", options=options)
            select.callback = self._pick
            self.add_item(select)

    def embed(self) -> discord.Embed:
        return discord.Embed(
            title="Character availability",
            description="Give one character a schedule that differs from your default, e.g. if you can "
            "only bring your DRK on weekends. Pick a character below. Only the squads you change are "
            "saved for that character; the rest keep following your default.",
            color=EMBED_COLOR,
        )

    async def send(self, interaction: discord.Interaction) -> None:
        if not self.characters:
            await interaction.response.send_message(
                "You don't have any characters yet. Ask a host to add them.", ephemeral=True
            )
            return
        await interaction.response.send_message(embed=self.embed(), view=self, ephemeral=True)

    async def _pick(self, interaction: discord.Interaction) -> None:
        character = self.characters[int(interaction.data["values"][0])]
        self.stop()
        await open_character_editor(interaction, self.bot, self.player, character, self.week, replace=True)


class AvailabilityEditor(discord.ui.View):
    """Paged editor: four squad dropdowns per page plus navigation and Save/Cancel buttons.

    Choices are kept in a draft and only written to the database when the player presses Save.
    """

    PAGE_SIZE = 4

    def __init__(self, bot: "MonkeyBot", *, mode: str, player: db.Player, week: date, initial: db.Availability, character=None):
        super().__init__(timeout=900)
        self.bot = bot
        self.mode = mode
        self.player = player
        self.week = week
        self.character = character
        self.order = get_squad_order(bot.conn, player.discord_id)
        self.squads = order_squads(db.squads_for_week(bot.conn, week, bot.tz), self.order)
        self.draft: db.Availability = {s.number: initial[s.number] for s in self.squads if s.number in initial}
        # character mode: the player's default, to mark squads where this character differs (✏️)
        self.default = db.get_default_availability(bot.conn, player.id) if mode == "character" else {}
        self.page = 0
        self._rebuild()

    @property
    def page_count(self) -> int:
        return max(1, math.ceil(len(self.squads) / self.PAGE_SIZE))

    def page_squads(self) -> list[db.SquadTime]:
        start = self.page * self.PAGE_SIZE
        return self.squads[start : start + self.PAGE_SIZE]

    def title(self) -> str:
        if self.mode == "weekly":
            return f"Your availability for the {timeutil.week_label(self.week)}"
        if self.mode == "default":
            return "Your default availability"
        return f"Availability for {self.character['ign']} (overrides your default)"

    def _differs(self, squad: int, level: str | None) -> bool:
        """Character mode: this squad's level differs from the player's default (unset saves as Not Available)."""
        return self.mode == "character" and (level or "Not Available") != self.default.get(squad)

    def _lines(self, levels: db.Availability, on_page: set[int] = frozenset()) -> str:
        lines = []
        for s in self.squads:
            level = levels.get(s.number)
            marker = "▶ " if s.number in on_page else ""
            changed = ""
            if self._differs(s.number, level):
                default = self.default.get(s.number)
                changed = f" ✏️ (default {LEVEL_EMOJI.get(default, UNSET_EMOJI)})"
            lines.append(
                f"{marker}{LEVEL_EMOJI.get(level, UNSET_EMOJI)} **Squad {s.number}** · "
                f"{timeutil.discord_ts(s.starts_at, 'f')} · {level or 'Not set'}{changed}"
            )
        return "\n".join(lines)

    def embed(self) -> discord.Embed:
        on_page = {s.number for s in self.page_squads()}
        embed = discord.Embed(title=self.title(), description=self._lines(self.draft, on_page), color=EMBED_COLOR)
        legend = " ✏️ = differs from your default (shown in brackets) ·" if self.mode == "character" else ""
        embed.set_footer(
            text=f"Page {self.page + 1}/{self.page_count} · Sorted by "
            f"{'time' if self.order == 'time' else 'squad number'} ·{legend} Pick a level for each squad, use ◀ ▶ to "
            "change page, then press Save. Squads left unset are saved as Not Available."
        )
        return embed

    def _rebuild(self) -> None:
        self.clear_items()
        for row, squad in enumerate(self.page_squads()):
            self.add_item(SquadSelect(squad.number, self.draft.get(squad.number), row))

        prev_btn = discord.ui.Button(label="◀", style=discord.ButtonStyle.secondary, row=4, disabled=self.page == 0)
        next_btn = discord.ui.Button(
            label="▶", style=discord.ButtonStyle.secondary, row=4, disabled=self.page >= self.page_count - 1
        )
        save_btn = discord.ui.Button(label="Save", style=discord.ButtonStyle.success, row=4)
        cancel_btn = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.danger, row=4)
        sort_btn = discord.ui.Button(
            label=order_button_label(other_order(self.order)), style=discord.ButtonStyle.secondary, emoji="🔀", row=4
        )
        prev_btn.callback = self._prev
        next_btn.callback = self._next
        save_btn.callback = self._save
        cancel_btn.callback = self._cancel
        sort_btn.callback = self._toggle_order
        for b in (prev_btn, next_btn, save_btn, cancel_btn, sort_btn):
            self.add_item(b)

    async def send(self, interaction: discord.Interaction, *, replace: bool = False) -> None:
        """Show the editor as a new private message, or in place of the current one (`replace`)."""
        if not self.squads:
            await interaction.response.send_message("No squads are configured yet. Ask a host.", ephemeral=True)
            return
        if replace:
            await interaction.response.edit_message(content=None, embed=self.embed(), view=self)
        else:
            await interaction.response.send_message(embed=self.embed(), view=self, ephemeral=True)

    async def refresh(self, interaction: discord.Interaction) -> None:
        self._rebuild()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    async def _prev(self, interaction: discord.Interaction) -> None:
        self.page = max(0, self.page - 1)
        await self.refresh(interaction)

    async def _next(self, interaction: discord.Interaction) -> None:
        self.page = min(self.page_count - 1, self.page + 1)
        await self.refresh(interaction)

    async def _toggle_order(self, interaction: discord.Interaction) -> None:
        # Choices made so far are kept; only the order of the list and pages changes.
        self.order = other_order(self.order)
        set_squad_order(self.bot.conn, self.player.discord_id, self.order)
        self.squads = order_squads(self.squads, self.order)
        self.page = 0
        await self.refresh(interaction)

    async def _cancel(self, interaction: discord.Interaction) -> None:
        self.stop()
        await interaction.response.edit_message(content="Cancelled. Nothing was changed.", embed=None, view=None)

    async def _save(self, interaction: discord.Interaction) -> None:
        full = {s.number: self.draft.get(s.number, "Not Available") for s in self.squads}
        conn = self.bot.conn
        if self.mode == "weekly":
            db.set_weekly_availability(conn, self.player.id, self.week, full)
            message = f"✅ Saved your availability for the {timeutil.week_label(self.week)}."
        elif self.mode == "default":
            db.set_default_availability(conn, self.player.id, full)
            message = "✅ Saved your default availability."
        else:
            base = db.get_default_availability(conn, self.player.id)
            overrides = {n: lvl for n, lvl in full.items() if base.get(n) != lvl}
            db.set_character_overrides(conn, self.character["id"], overrides)
            message = (
                f"✅ Saved {len(overrides)} override(s) for {self.character['ign']}."
                if overrides
                else f"✅ {self.character['ign']} now follows your default schedule."
            )
        self.stop()
        embed = discord.Embed(title=self.title(), description=self._lines(full), color=EMBED_COLOR)
        if self.mode == "character":
            embed.set_footer(text="✏️ = differs from your default (shown in brackets)")
        await interaction.response.edit_message(content=message, embed=embed, view=None)


# --------------------------------------------------------------------------- character status

CHARACTER_STATUS_INFO = {
    "static": ("⭐", "Static", "Prioritize this character when slotting"),
    "sub": ("⏳", "Sub", "Only slot if needed"),
    "inactive": ("💤", "Inactive", "Don't slot this character"),
}


def character_status_label(status: str) -> str:
    emoji, name, _ = CHARACTER_STATUS_INFO[status]
    return f"{emoji} {name}"


def character_line(c) -> str:
    dmg = f"{c['dmg']:.2f}" if c["dmg"] is not None else "?"
    return f"{c['ign']} · {c['job'] or '?'} · {c['buff'] or '?'} · dmg {dmg}"


class CharacterStatusEditor(discord.ui.View):
    """Paged editor: one static/sub/inactive dropdown per character, saved on Save."""

    PAGE_SIZE = 4

    def __init__(self, bot: "MonkeyBot", player: db.Player):
        super().__init__(timeout=900)
        self.bot = bot
        self.player = player
        self.characters = db.list_characters(bot.conn, player.id)
        self.draft = {c["id"]: c["status"] for c in self.characters}
        self.page = 0
        self._rebuild()

    @property
    def page_count(self) -> int:
        return max(1, math.ceil(len(self.characters) / self.PAGE_SIZE))

    def page_characters(self):
        start = self.page * self.PAGE_SIZE
        return self.characters[start : start + self.PAGE_SIZE]

    def embed(self) -> discord.Embed:
        on_page = {c["id"] for c in self.page_characters()}
        lines = [
            f"{'▶ ' if c['id'] in on_page else ''}{CHARACTER_STATUS_INFO[self.draft[c['id']]][0]} "
            f"**{c['ign']}** · {c['job'] or '?'} · {c['buff'] or '?'} · {CHARACTER_STATUS_INFO[self.draft[c['id']]][1]}"
            for c in self.characters
        ]
        embed = discord.Embed(
            title="Your character status",
            description="Tell the hosts which characters to slot:\n"
            "⭐ **Static**: prioritize · ⏳ **Sub**: only if needed · 💤 **Inactive**: don't slot\n\n"
            + "\n".join(lines),
            color=EMBED_COLOR,
        )
        embed.set_footer(text=f"Page {self.page + 1}/{self.page_count} · Use ◀ ▶ to change page, then press Save.")
        return embed

    def _rebuild(self) -> None:
        self.clear_items()
        for row, c in enumerate(self.page_characters()):
            self.add_item(CharacterStatusSelect(c, self.draft[c["id"]], row))
        buttons = [
            ("◀", discord.ButtonStyle.secondary, self.page == 0, self._prev),
            ("▶", discord.ButtonStyle.secondary, self.page >= self.page_count - 1, self._next),
            ("Save", discord.ButtonStyle.success, False, self._save),
            ("Cancel", discord.ButtonStyle.danger, False, self._cancel),
        ]
        for label, style, disabled, callback in buttons:
            button = discord.ui.Button(label=label, style=style, row=4, disabled=disabled)
            button.callback = callback
            self.add_item(button)

    async def send(self, interaction: discord.Interaction) -> None:
        if not self.characters:
            await interaction.response.send_message(
                "You don't have any characters yet. Ask a host to add them.", ephemeral=True
            )
            return
        await interaction.response.send_message(embed=self.embed(), view=self, ephemeral=True)

    async def refresh(self, interaction: discord.Interaction) -> None:
        self._rebuild()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    async def _prev(self, interaction: discord.Interaction) -> None:
        self.page = max(0, self.page - 1)
        await self.refresh(interaction)

    async def _next(self, interaction: discord.Interaction) -> None:
        self.page = min(self.page_count - 1, self.page + 1)
        await self.refresh(interaction)

    async def _cancel(self, interaction: discord.Interaction) -> None:
        self.stop()
        await interaction.response.edit_message(content="Cancelled. Nothing was changed.", embed=None, view=None)

    async def _save(self, interaction: discord.Interaction) -> None:
        db.set_character_statuses(self.bot.conn, self.player.id, self.draft, changed_by=interaction.user.id)
        self.stop()
        counts = {s: sum(1 for v in self.draft.values() if v == s) for s in db.CHARACTER_STATUSES}
        summary = " · ".join(f"{character_status_label(s)}: {n}" for s, n in counts.items())
        await interaction.response.edit_message(
            content=f"✅ Saved your character status. {summary}", embed=self.embed(), view=None
        )


def damage_history_embed(conn, char) -> discord.Embed:
    """A character's current damage and last 15 logged runs (★ = counted in the average)."""
    runs = conn.execute(
        """
        SELECT l.started_at, l.finished_at, r.damage, r.normalized FROM damage_runs r
        JOIN damage_logs l ON l.id = r.log_id WHERE r.character_id = ? ORDER BY l.started_at DESC LIMIT 15
        """,
        (char["id"],),
    ).fetchall()
    averaged = damage.average_runs(conn)
    lines = [
        f"{'★ ' if i < averaged else ''}{timeutil.discord_ts(r['started_at'], 'd')} · "
        f"{r['damage'] / 1e9:.2f}B in {(r['finished_at'] - r['started_at']) / 60:.1f} min → **{r['normalized']:.2f}**"
        for i, r in enumerate(runs)
    ]
    dmg = f"{char['dmg']:.2f}" if char["dmg"] is not None else "not set"
    embed = discord.Embed(
        title=f"{char['ign']} ({char['job'] or '?'}): damage {dmg}",
        description="\n".join(lines) or "*No runs recorded yet. The damage comes from the roster sheet.*",
        color=EMBED_COLOR,
    )
    embed.set_footer(text=f"★ = counted in the average (last {averaged} runs) · scaled to {damage.NORMALIZE_MINUTES:g} min")
    return embed


class DamageHistoryPicker(discord.ui.View):
    """From /cq characters: pick one of your characters to see its damage history (same view as
    /host damage_history). The dropdown stays, so you can switch characters."""

    def __init__(self, bot: "MonkeyBot", player: db.Player):
        super().__init__(timeout=900)
        self.bot = bot
        self.characters = {c["id"]: c for c in db.list_characters(bot.conn, player.id)}
        self.selected: int | None = None
        self._build()

    def _build(self) -> None:
        self.clear_items()
        options = [
            discord.SelectOption(
                label=f"{c['ign']} ({c['job'] or '?'})"[:100],
                value=str(c["id"]),
                description=f"Damage {c['dmg']:.2f}" if c["dmg"] is not None else "No damage recorded",
                default=c["id"] == self.selected,
            )
            for c in list(self.characters.values())[:25]  # Discord allows 25 options per dropdown
        ]
        select = discord.ui.Select(placeholder="Choose a character…", options=options)
        select.callback = self._pick
        self.add_item(select)

    def embed(self) -> discord.Embed:
        if self.selected is None:
            return discord.Embed(
                title="Damage history",
                description="Pick a character to see its damage from the hosts' damage logs.",
                color=EMBED_COLOR,
            )
        return damage_history_embed(self.bot.conn, self.characters[self.selected])

    async def send(self, interaction: discord.Interaction) -> None:
        if not self.characters:
            await interaction.response.send_message(
                "You don't have any characters yet. Ask a host to add them.", ephemeral=True
            )
            return
        await interaction.response.send_message(embed=self.embed(), view=self, ephemeral=True)

    async def _pick(self, interaction: discord.Interaction) -> None:
        self.selected = int(interaction.data["values"][0])
        self._build()
        await interaction.response.edit_message(embed=self.embed(), view=self)


class CharacterStatusSelect(discord.ui.Select):
    def __init__(self, character, current: str, row: int):
        options = [
            discord.SelectOption(
                label=f"{character['ign']}: {name}",
                value=status,
                emoji=emoji,
                description=help_text,
                default=status == current,
            )
            for status, (emoji, name, help_text) in CHARACTER_STATUS_INFO.items()
        ]
        super().__init__(options=options, row=row)
        self.character_id = character["id"]

    async def callback(self, interaction: discord.Interaction) -> None:
        view: CharacterStatusEditor = self.view  # type: ignore[assignment]
        view.draft[self.character_id] = self.values[0]
        await view.refresh(interaction)


class SquadSelect(discord.ui.Select):
    def __init__(self, squad: int, current: str | None, row: int):
        options = [
            discord.SelectOption(
                label=f"Squad {squad}: {level}", value=level, emoji=LEVEL_EMOJI[level], default=level == current
            )
            for level in db.LEVELS
        ]
        super().__init__(placeholder=f"Squad {squad}: not set", options=options, row=row)
        self.squad = squad

    async def callback(self, interaction: discord.Interaction) -> None:
        view: AvailabilityEditor = self.view  # type: ignore[assignment]
        view.draft[self.squad] = self.values[0]
        await view.refresh(interaction)
