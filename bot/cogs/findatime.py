"""/findatime: a public poll for finding a time when several people are free (for any boss).

Anyone can start one. The poll message lists who has joined, with buttons:
  Join          type your availability in plain English ("Fri 5-7pm, Sat after 2pm"), check the
                hour grid the bot draws from it, then Confirm (or Edit). Once you've joined, Join
                shows your hours privately with Edit and Leave poll.
  See schedule  everyone's hours, one day at a time, in your timezone (only you see it)
  Best time     the hours when the most people overlap (only you see it). The creator or a host
                also gets controls there: propose one of the times (or another time), which pings
                everyone who joined to Confirm, say they can't make it, or propose a different
                time; and Close the poll (no new joins; results stay viewable).
A public message shows the same buttons to everyone, so it only has buttons anyone can use;
actions for some people only (leaving, proposing, closing) are in private messages.
Times are typed and shown in each person's own timezone (/settings), and stored in UTC.
"""

from __future__ import annotations

import logging
import re
from datetime import date
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands

from .. import db, findatime, timeutil
from ..app import MonkeyBot
from ..views import EMBED_COLOR, TimezoneSelect, get_user_timezone, timezone_text

log = logging.getLogger(__name__)

BUTTONS = {
    "join": ("Join/Edit", discord.ButtonStyle.success, "✋"),
    "schedule": ("See schedule", discord.ButtonStyle.primary, "🗓️"),
    "best": ("Best time", discord.ButtonStyle.primary, "⭐"),
}
# Buttons that polls posted before they moved into private messages may still show; they still work.
OLD_BUTTONS = {
    "leave": ("Remove", discord.ButtonStyle.secondary, "➖"),
    "close": ("Close", discord.ButtonStyle.danger, "🔒"),
}


def now_hour() -> int:
    return int(timeutil.now_utc().timestamp()) // 3600


def poll_dates(poll: db.FindATimePoll, tz) -> list[date]:
    """The poll's dates for someone in `tz`: from their today to the poll's end."""
    return findatime.window_dates(poll.start_hour, poll.end_hour, now_hour(), tz)


def open_hours(poll: db.FindATimePoll, tz):
    """Whether an hour (a local date and hour in `tz`) can still be chosen: not past, before the poll ends."""
    now = now_hour()
    return lambda d, h: now <= findatime.local_hour(d, h, tz) < poll.end_hour


def date_span(poll: db.FindATimePoll, tz) -> str:
    dates = poll_dates(poll, tz)
    return f"{findatime.day_label(dates[0])} – {findatime.day_label(dates[-1])}"


def plain_name(name: str) -> str:
    """A display name without Discord formatting characters, so it can't break the bold around it."""
    return re.sub(r"[*_`~|\\]", "", name)


def typed(text: str) -> str:
    """What someone typed, shown as inline code (a backtick in it would end the code early)."""
    return "`" + text[:300].replace("`", "'") + "`"


def local_today(tz) -> date:
    return timeutil.now_utc().astimezone(tz).date()


def viewer_timezone(bot: MonkeyBot, user_id: int):
    """The user's timezone from /settings, else the bot's (with a hint to set theirs)."""
    tz = get_user_timezone(bot.conn, user_id)
    if tz:
        return tz, ""
    return bot.tz, f"Times are in {timezone_text(bot.tz)}. Set your own timezone in /settings."


def poll_message(bot: MonkeyBot, poll: db.FindATimePoll) -> tuple[discord.Embed, discord.ui.View]:
    entries = db.poll_entries(bot.conn, poll.id)
    title = "🗓️ Find a time" + (f": {poll.title}" if poll.title else "")
    if poll.closed:
        intro = "🔒 **Closed.** See schedule and Best time still work."
    else:
        intro = (
            "Press **Join/Edit** and type when you're free, in your own time, e.g. `Fri 5-7pm, Sat after 2pm` "
            "or `weekdays 8-11pm`. Press it again to change your hours or leave. See schedule and Best "
            "time are only shown to you."
        )
    embed = discord.Embed(
        title=title,
        description=(
            f"**Until** {timeutil.discord_ts(poll.end_hour * 3600, 'f')} (7 days from when it started) · "
            f"started by <@{poll.creator_id}>\n{intro}"
        ),
        color=EMBED_COLOR,
    )
    names = ", ".join(e.name for e in entries)
    embed.add_field(name=f"Joined ({len(entries)})", value=names[:1024] or "*No one yet*", inline=False)
    embed.set_footer(text=f"Poll #{poll.id}")
    view = discord.ui.View(timeout=None)
    for action in BUTTONS:
        view.add_item(FindATimeButton(action, poll.id, disabled=poll.closed and action == "join"))
    return embed, view


async def refresh_poll_message(bot: MonkeyBot, poll: db.FindATimePoll) -> None:
    """Update the public poll message (after a change made from a private message)."""
    channel = bot.get_channel(poll.channel_id) if poll.channel_id else None
    if channel is None or poll.message_id is None:
        return
    embed, view = poll_message(bot, db.get_poll(bot.conn, poll.id))
    try:
        await channel.get_partial_message(poll.message_id).edit(embed=embed, view=view)
    except discord.HTTPException as e:
        log.warning("Couldn't update findatime poll %s: %s", poll.id, e)


class FindATimeButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"fat:(?P<action>join|leave|schedule|best|close):(?P<poll>\d+)",
):
    """The poll's buttons. The poll id is in the custom_id, so they keep working after a restart."""

    def __init__(self, action: str, poll_id: int, disabled: bool = False):
        label, style, emoji = {**BUTTONS, **OLD_BUTTONS}[action]
        super().__init__(
            discord.ui.Button(label=label, style=style, emoji=emoji, custom_id=f"fat:{action}:{poll_id}", disabled=disabled)
        )
        self.action = action
        self.poll_id = poll_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str], /):
        return cls(match["action"], int(match["poll"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        bot: MonkeyBot = interaction.client  # type: ignore[assignment]
        poll = db.get_poll(bot.conn, self.poll_id)
        if poll is None:
            await interaction.response.send_message("This poll no longer exists.", ephemeral=True)
            return
        if poll.closed and self.action in ("join", "leave", "close"):
            await interaction.response.send_message("This poll is closed.", ephemeral=True)
            return
        await getattr(self, f"_{self.action}")(interaction, bot, poll)

    async def _join(self, interaction: discord.Interaction, bot: MonkeyBot, poll: db.FindATimePoll) -> None:
        mine = next((e for e in db.poll_entries(bot.conn, poll.id) if e.user_id == interaction.user.id), None)
        if mine is not None:  # already in: show their hours privately, with Edit and Leave poll
            view = MyEntryView(bot, poll, mine, interaction.user.id)
            await interaction.response.send_message(embed=view.embed(), view=view, ephemeral=True)
            return
        previous = None
        tz = get_user_timezone(bot.conn, interaction.user.id)
        if tz is not None:
            await interaction.response.send_modal(JoinModal(bot, poll, tz, previous))
            return

        async def open_modal(pick: discord.Interaction, zone: str) -> None:
            await pick.response.send_modal(JoinModal(bot, poll, ZoneInfo(zone), previous))

        view = discord.ui.View(timeout=900)
        view.add_item(TimezoneSelect(interaction.user.id, None, open_modal))
        await interaction.response.send_message(
            "🌐 Pick your timezone first, so the hours you type are read in your local time. "
            "You can change it later in /settings.",
            view=view,
            ephemeral=True,
        )

    async def _leave(self, interaction: discord.Interaction, bot: MonkeyBot, poll: db.FindATimePoll) -> None:
        if not db.delete_poll_entry(bot.conn, poll.id, interaction.user.id):
            await interaction.response.send_message("You haven't joined this poll.", ephemeral=True)
            return
        embed, view = poll_message(bot, poll)
        await interaction.response.edit_message(embed=embed, view=view)  # pressed on the poll message
        await interaction.followup.send("➖ You've been removed from this poll.", ephemeral=True)

    async def _schedule(self, interaction: discord.Interaction, bot: MonkeyBot, poll: db.FindATimePoll) -> None:
        view = ScheduleView(bot, poll, interaction.user.id)
        await interaction.response.send_message(embed=view.embed(), view=view, ephemeral=True)

    async def _best(self, interaction: discord.Interaction, bot: MonkeyBot, poll: db.FindATimePoll) -> None:
        embed = best_time_embed(bot, poll)
        if can_manage(bot, interaction, poll):  # the creator or a host: propose a time, close the poll
            view = ProposeView(bot, poll, interaction.user.id)
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True)

    async def _close(self, interaction: discord.Interaction, bot: MonkeyBot, poll: db.FindATimePoll) -> None:
        if not can_manage(bot, interaction, poll):
            await interaction.response.send_message("Only the poll's creator or a host can close it.", ephemeral=True)
            return
        db.close_poll(bot.conn, poll.id)
        embed, view = poll_message(bot, db.get_poll(bot.conn, poll.id))
        await interaction.response.edit_message(embed=embed, view=view)


# --------------------------------------------------------------------------- Join


class JoinModal(discord.ui.Modal):
    """Type your availability. `from_preview`: opened with Edit on the preview, which is then updated."""

    def __init__(self, bot: MonkeyBot, poll: db.FindATimePoll, tz, previous: str | None, *, from_preview: bool = False):
        super().__init__(title=("Join: " + (poll.title or "find a time"))[:45], timeout=900)
        self.bot, self.poll, self.tz, self.from_preview = bot, poll, tz, from_preview
        self.text = discord.ui.TextInput(
            style=discord.TextStyle.paragraph, max_length=500, default=previous,
            placeholder="e.g. Fri 5-7pm, Sat after 2pm, weekdays 8-11pm, Sun all day",
        )
        self.add_item(discord.ui.Label(
            text="When are you free?", component=self.text,
            description=f"In your time ({timezone_text(tz)}), {date_span(poll, tz)}"[:100],
        ))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        preview = JoinPreview(self.bot, self.poll, self.tz, self.text.value)
        if self.from_preview:
            await interaction.response.edit_message(content=None, embed=preview.embed(), view=preview)
        else:
            await interaction.response.send_message(embed=preview.embed(), view=preview, ephemeral=True)


class LayoutToggle:
    """A 🖥️ Grid view / 📱 Compact view button that switches how this one message shows hours: free
    hours in words (the default; easier on phones), or the emoji grid. Not remembered: each message
    starts compact. Views using it define _build() (adding self._layout_button()) and embed()."""

    compact = True

    def _layout_button(self, row: int | None = None) -> discord.ui.Button:
        button = discord.ui.Button(
            label="Grid view" if self.compact else "Compact view", emoji="🖥️" if self.compact else "📱",
            style=discord.ButtonStyle.secondary, row=row,
        )
        button.callback = self._toggle_layout
        return button

    async def _toggle_layout(self, interaction: discord.Interaction) -> None:
        self.compact = not self.compact
        self._build()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    def _hours_text(self, hours: set[int], dates: list[date], tz) -> str:
        if self.compact:
            return findatime.compact(hours, dates, tz, now_hour())
        return findatime.grid(hours, dates, tz, now_hour())

    def _legend(self) -> str:
        return findatime.COMPACT_LEGEND if self.compact else findatime.LEGEND


class JoinPreview(LayoutToggle, discord.ui.View):
    """The hour grid read from what was typed, to Confirm or Edit before it's saved."""

    def __init__(self, bot: MonkeyBot, poll: db.FindATimePoll, tz, text: str):
        super().__init__(timeout=900)
        self.bot, self.poll, self.tz, self.text = bot, poll, tz, text
        self.parsed = findatime.parse(text, poll_dates(poll, tz), local_today(tz), open_hours(poll, tz))
        self.hours = findatime.to_epoch_hours(self.parsed.slots, tz)
        self._build()

    def _build(self) -> None:
        self.clear_items()
        confirm = discord.ui.Button(label="Confirm", emoji="✅", style=discord.ButtonStyle.success, disabled=not self.hours)
        confirm.callback = self._confirm
        edit = discord.ui.Button(label="Edit", emoji="✏️", style=discord.ButtonStyle.secondary)
        edit.callback = self._edit
        self.add_item(confirm)
        self.add_item(edit)
        self.add_item(self._layout_button())

    def embed(self) -> discord.Embed:
        embed = discord.Embed(
            title="Your availability" + (f": {self.poll.title}" if self.poll.title else ""),
            description=f"You typed: {typed(self.text)}\n\n"
            + self._hours_text(self.hours, poll_dates(self.poll, self.tz), self.tz),
            color=EMBED_COLOR,
        )
        if self.parsed.unread:
            embed.add_field(
                name="⚠️ Couldn't read",
                value="\n".join(f"• {u}" for u in self.parsed.unread)[:1000]
                + "\nGive each part a day and a time, e.g. `Fri 5-7pm`.",
                inline=False,
            )
        if self.parsed.notes:
            embed.add_field(name="Check", value="\n".join(f"• {n}" for n in self.parsed.notes)[:1024], inline=False)
        footer = f"{self._legend()} · your time: {timezone_text(self.tz)}"
        embed.set_footer(text=footer + (" · Press Confirm to save, or Edit to change it" if self.hours else
                                        " · Nothing to save yet: press Edit"))
        return embed

    async def _confirm(self, interaction: discord.Interaction) -> None:
        poll = db.get_poll(self.bot.conn, self.poll.id)
        if poll is None or poll.closed:
            await interaction.response.edit_message(content="This poll is closed, so nothing was saved.", view=None)
            return
        name = getattr(interaction.user, "display_name", None) or interaction.user.name
        db.set_poll_entry(self.bot.conn, poll.id, interaction.user.id, name, self.text, self.hours)
        self.stop()
        await interaction.response.edit_message(
            content="✅ You're in! Press Join/Edit any time to change your hours.", embed=self.embed(), view=None
        )
        await refresh_poll_message(self.bot, poll)

    async def _edit(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(JoinModal(self.bot, self.poll, self.tz, self.text, from_preview=True))


class MyEntryView(LayoutToggle, discord.ui.View):
    """Join, for someone already in the poll: their hours (privately), with Edit and Leave poll."""

    def __init__(self, bot: MonkeyBot, poll: db.FindATimePoll, entry: db.FindATimeEntry, user_id: int):
        super().__init__(timeout=900)
        self.bot, self.poll, self.entry = bot, poll, entry
        self.tz, self.tz_hint = viewer_timezone(bot, user_id)
        self._build()

    def _build(self) -> None:
        self.clear_items()
        edit = discord.ui.Button(label="Edit", emoji="✏️", style=discord.ButtonStyle.primary)
        edit.callback = self._edit
        leave = discord.ui.Button(label="Leave poll", emoji="➖", style=discord.ButtonStyle.danger)
        leave.callback = self._leave
        self.add_item(edit)
        self.add_item(leave)
        self.add_item(self._layout_button())

    def embed(self) -> discord.Embed:
        embed = discord.Embed(
            title="You're in" + (f": {self.poll.title}" if self.poll.title else ""),
            description=f"You typed: {typed(self.entry.text)}\n\n"
            + self._hours_text(self.entry.hours, poll_dates(self.poll, self.tz), self.tz),
            color=EMBED_COLOR,
        )
        embed.set_footer(text=f"{self._legend()} · your time: {timezone_text(self.tz)}"
                         + (f" · {self.tz_hint}" if self.tz_hint else ""))
        return embed

    async def _edit(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(
            JoinModal(self.bot, self.poll, self.tz, self.entry.text, from_preview=True)
        )

    async def _leave(self, interaction: discord.Interaction) -> None:
        db.delete_poll_entry(self.bot.conn, self.poll.id, interaction.user.id)
        self.stop()
        await interaction.response.edit_message(content="➖ You've left this poll.", embed=None, view=None)
        await refresh_poll_message(self.bot, self.poll)


# --------------------------------------------------------------------------- See schedule


class ScheduleView(LayoutToggle, discord.ui.View):
    """Everyone's hours for one day at a time (a day dropdown and ◀ ▶), in the viewer's timezone."""

    def __init__(self, bot: MonkeyBot, poll: db.FindATimePoll, user_id: int):
        super().__init__(timeout=900)
        self.bot, self.poll = bot, poll
        self.tz, self.tz_hint = viewer_timezone(bot, user_id)
        self.entries = db.poll_entries(bot.conn, poll.id)
        self.dates = poll_dates(poll, self.tz)
        self.day = 0
        self._build()

    def free_count(self, d: date) -> int:
        day_hours = {findatime.local_hour(d, h, self.tz) for h in range(24)}
        return sum(1 for e in self.entries if e.hours & day_hours)

    def embed(self) -> discord.Embed:
        d = self.dates[self.day]
        if self.compact:  # free hours in words, plus when 2+ people overlap
            lines = []
            counts: dict[int, int] = {}
            for e in self.entries:
                ranges = findatime.day_ranges(e.hours, d, self.tz, now_hour())
                lines.append(f"**{plain_name(e.name)}** · {ranges or '*not free*'}")
                for h in e.hours:
                    counts[h] = counts.get(h, 0) + 1
            overlap = findatime.day_ranges({h for h, n in counts.items() if n >= 2}, d, self.tz, now_hour())
            lines.append(f"\n🤝 **2+ free:** {overlap or '*no overlap this day*'}")
        else:
            lines = [findatime.header()]
            for e in self.entries:  # name on its own line, so the 24 squares fit on one line below it
                lines += [f"**{plain_name(e.name)}**", findatime.squares(e.hours, d, self.tz, now_hour())]
        embed = discord.Embed(
            title=f"{d:%A} {d.month}/{d.day}" + (f" · {self.poll.title}" if self.poll.title else ""),
            description="\n".join(lines)[:4000] if self.entries else "*No one has joined yet.*",
            color=EMBED_COLOR,
        )
        embed.set_footer(text=f"Day {self.day + 1}/{len(self.dates)} · {self._legend()} · your time: "
                         f"{timezone_text(self.tz)}" + (f" · {self.tz_hint}" if self.tz_hint else ""))
        return embed

    def _build(self) -> None:
        self.clear_items()
        select = discord.ui.Select(
            options=[
                discord.SelectOption(
                    label=f"{d:%A} {d.month}/{d.day}", value=str(i), default=i == self.day,
                    description=f"{self.free_count(d)} of {len(self.entries)} free at some point",
                )
                for i, d in enumerate(self.dates)
            ],
            row=0,
        )
        select.callback = self._pick
        self.add_item(select)
        for label, step, disabled in [("◀", -1, self.day == 0), ("▶", 1, self.day == len(self.dates) - 1)]:
            button = discord.ui.Button(label=label, style=discord.ButtonStyle.secondary, row=1, disabled=disabled)
            button.callback = self._step(step)
            self.add_item(button)
        self.add_item(self._layout_button(row=1))

    async def _show(self, interaction: discord.Interaction) -> None:
        self._build()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    async def _pick(self, interaction: discord.Interaction) -> None:
        self.day = int(interaction.data["values"][0])
        await self._show(interaction)

    def _step(self, step: int):
        async def callback(interaction: discord.Interaction) -> None:
            self.day = max(0, min(len(self.dates) - 1, self.day + step))
            await self._show(interaction)

        return callback


# --------------------------------------------------------------------------- Best time


def can_manage(bot: MonkeyBot, interaction: discord.Interaction, poll: db.FindATimePoll) -> bool:
    """The poll's creator or a host can close it and propose times."""
    return interaction.user.id == poll.creator_id or bot.is_host(interaction)


def poll_windows(bot: MonkeyBot, poll: db.FindATimePoll) -> list[findatime.Window]:
    entries = db.poll_entries(bot.conn, poll.id)
    now_hour = int(timeutil.now_utc().timestamp()) // 3600
    return findatime.best_windows({e.user_id: e.hours for e in entries}, from_hour=now_hour)


def best_time_embed(bot: MonkeyBot, poll: db.FindATimePoll) -> discord.Embed:
    entries = db.poll_entries(bot.conn, poll.id)
    embed = discord.Embed(
        title="⭐ Best times" + (f": {poll.title}" if poll.title else ""), color=EMBED_COLOR
    )
    if not entries:
        embed.description = "No one has joined yet."
        return embed
    names = {e.user_id: e.name for e in entries}
    windows = poll_windows(bot, poll)
    # an overlap people may have seen in See schedule that's already over (shown greyed out there)
    now = now_hour()
    counts: dict[int, int] = {}
    for e in entries:
        for h in e.hours:
            counts[h] = counts.get(h, 0) + 1
    past = sorted(h for h, n in counts.items() if n >= 2 and h < now)
    passed_note = (
        f"\n\n⬜ {len(past)} hour(s) when 2+ people were free have already passed "
        f"(the last was {timeutil.discord_ts(past[-1] * 3600, 'F')}), so they're not listed."
        if past else ""
    )
    if not windows:
        embed.description = "None of the hours people chose are still ahead." + passed_note
        return embed
    lines = []
    if len(entries) >= 2 and len(windows[0].people) < 2:
        lines.append("**No time ahead works for 2 or more people yet.** Here's when each person is free:")
    for i, w in enumerate(windows, start=1):
        free = sorted(names[u] for u in w.people)
        missing = sorted(names[u] for u in names if u not in w.people)
        who = "everyone" if not missing else ", ".join(free)
        lines.append(
            f"**{i}. {timeutil.discord_ts(w.start * 3600, 'F')} – {timeutil.discord_ts(w.end * 3600, 't')}** "
            f"({w.hours}h) · **{len(w.people)}/{len(entries)}** free: {who}"
            + (f"\n Missing: {', '.join(missing)}" if missing else "")
        )
    embed.description = ("\n".join(lines) + passed_note)[:4000]
    embed.set_footer(text="Most people first, then the longest stretch, then the soonest · times are in your timezone")
    return embed


# --------------------------------------------------------------------------- proposing a time


class ProposeView(discord.ui.View):
    """Under Best time, only for the poll's creator or a host: propose a time to everyone who joined,
    or close the poll."""

    def __init__(self, bot: MonkeyBot, poll: db.FindATimePoll, user_id: int):
        super().__init__(timeout=900)
        self.bot, self.poll = bot, poll
        self.tz, _ = viewer_timezone(bot, user_id)
        propose_button = discord.ui.Button(label="Propose a time", emoji="📣", style=discord.ButtonStyle.primary)
        propose_button.callback = self._propose
        self.add_item(propose_button)
        if not poll.closed:
            close = discord.ui.Button(label="Close poll", emoji="🔒", style=discord.ButtonStyle.danger)
            close.callback = self._close
            self.add_item(close)

    async def _propose(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(ProposeModal(self.bot, self.poll, self.tz))

    async def _close(self, interaction: discord.Interaction) -> None:
        db.close_poll(self.bot.conn, self.poll.id)
        self.stop()
        await interaction.response.edit_message(
            content="🔒 Closed: no new joins. See schedule and Best time still work.", view=None
        )
        await refresh_poll_message(self.bot, self.poll)


def best_times_text(bot: MonkeyBot, poll: db.FindATimePoll, tz) -> str:
    """The best times in plain words, in `tz` (for pop-ups, which may not show Discord timestamps)."""
    total = len(db.poll_entries(bot.conn, poll.id))
    lines = [
        f"{i}. {findatime.window_label(w.start, w.end, tz)} · {len(w.people)}/{total} free"
        for i, w in enumerate(poll_windows(bot, poll), start=1)
    ]
    return "\n".join(lines) or "No times ahead yet."


def when_text(start: int, end: int) -> str:
    """A proposed time as Discord timestamps: just the start for a single hour (the run's start time),
    else the range."""
    if end - start <= 1:
        return timeutil.discord_ts(start * 3600, "F")
    return f"{timeutil.discord_ts(start * 3600, 'F')} – {timeutil.discord_ts(end * 3600, 't')}"


class ProposeModal(discord.ui.Modal):
    """Propose a time: the best times at the top, and a box to type the time ("tue 1pm"). What's typed
    is read by the built-in parser and shown back for a final check before anyone is pinged."""

    def __init__(self, bot: MonkeyBot, poll: db.FindATimePoll, tz, previous: str | None = None,
                 *, from_check: bool = False):
        super().__init__(title="Propose a time", timeout=900)
        self.bot, self.poll, self.tz, self.from_check = bot, poll, tz, from_check
        self.add_item(discord.ui.TextDisplay(
            f"**Best times** (your time: {timezone_text(tz)})\n{best_times_text(bot, poll, tz)}"[:4000]
        ))
        self.text = discord.ui.TextInput(max_length=100, default=previous, placeholder="e.g. tue 1pm, or tue 1-3pm")
        self.add_item(discord.ui.Label(
            text="When should the run start?", component=self.text,
            description=f"In your time, {date_span(poll, tz)}"[:100],
        ))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        check = ProposalCheck(self.bot, self.poll, self.tz, self.text.value)
        content = check.content()
        if self.from_check:
            await interaction.response.edit_message(content=content, view=check)
        else:
            await interaction.response.send_message(content, view=check, ephemeral=True)


class ProposalCheck(discord.ui.View):
    """The proposed time as the parser read it, to Send (ping everyone) or Edit."""

    def __init__(self, bot: MonkeyBot, poll: db.FindATimePoll, tz, text: str):
        super().__init__(timeout=900)
        self.bot, self.poll, self.tz, self.text = bot, poll, tz, text
        self.parsed = findatime.parse(text, poll_dates(poll, tz), local_today(tz), open_hours(poll, tz))
        self.block = findatime.first_block(findatime.to_epoch_hours(self.parsed.slots, tz))
        send = discord.ui.Button(label="Send", emoji="📣", style=discord.ButtonStyle.success, disabled=self.block is None)
        send.callback = self._send
        edit = discord.ui.Button(label="Edit", emoji="✏️", style=discord.ButtonStyle.secondary)
        edit.callback = self._edit
        self.add_item(send)
        self.add_item(edit)

    def content(self) -> str:
        if self.block is None:
            return (f"I couldn't read a day and time from {typed(self.text)}. Try e.g. `tue 1pm`."
                    + "".join(f"\n• {n}" for n in self.parsed.notes))
        start, end = self.block
        people = len(db.poll_entries(self.bot.conn, self.poll.id))
        local = findatime.window_label(start, end, self.tz) if end - start > 1 else findatime.start_label(start, self.tz)
        text = (f"📣 Propose **{local}** (your time) = {when_text(start, end)} "
                f"({timeutil.discord_ts(start * 3600, 'R')}) to the {people} people in the poll? They'll be pinged.")
        notes = self.parsed.notes + (["Only the first block of time you typed is used."]
                                     if len(findatime.to_epoch_hours(self.parsed.slots, self.tz)) > end - start else [])
        return text + "".join(f"\n• {n}" for n in notes)

    async def _send(self, interaction: discord.Interaction) -> None:
        self.stop()
        await propose(interaction, self.bot, self.poll, *self.block, edit=True)

    async def _edit(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(ProposeModal(self.bot, self.poll, self.tz, self.text, from_check=True))


class TimeModal(discord.ui.Modal):
    """Type one time in plain English ("Sat 8-10pm"); the first block of hours it reads is used."""

    def __init__(self, bot: MonkeyBot, poll: db.FindATimePoll, tz, title: str, on_time):
        super().__init__(title=title, timeout=900)
        self.bot, self.poll, self.tz, self.on_time = bot, poll, tz, on_time
        self.text = discord.ui.TextInput(max_length=100, placeholder="e.g. Sat 8-10pm")
        self.add_item(discord.ui.Label(
            text="When?", component=self.text,
            description=f"In your time ({timezone_text(tz)}), {date_span(poll, tz)}"[:100],
        ))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        parsed = findatime.parse(
            self.text.value, poll_dates(self.poll, self.tz), local_today(self.tz), open_hours(self.poll, self.tz)
        )
        block = findatime.first_block(findatime.to_epoch_hours(parsed.slots, self.tz))
        if block is None:
            await interaction.response.send_message(
                f"I couldn't read a day and time from `{self.text.value}`. Try e.g. `Sat 8-10pm`.", ephemeral=True
            )
            return
        await self.on_time(interaction, *block)


async def propose(
    interaction: discord.Interaction, bot: MonkeyBot, poll: db.FindATimePoll, start: int, end: int,
    *, edit: bool = False,
) -> None:
    """Post a proposal in the poll's channel, pinging everyone who joined the poll. `edit`: reply by
    updating the message the button was on (the private check) instead of sending a new one."""
    reply = interaction.response.edit_message if edit else (
        lambda **kw: interaction.response.send_message(ephemeral=True, **kw)
    )
    channel = bot.get_channel(poll.channel_id) if poll.channel_id else interaction.channel
    if channel is None:
        await reply(content="I can't find the poll's channel.", view=None)
        return
    proposal = db.create_proposal(bot.conn, poll.id, interaction.user.id, start, end)
    ids = [e.user_id for e in db.poll_entries(bot.conn, poll.id)]
    embed, view = proposal_message(bot, proposal)
    try:
        message = await channel.send(
            content=" ".join(f"<@{i}>" for i in ids) or None,
            embed=embed,
            view=view,
            allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=[discord.Object(i) for i in ids]),
        )
    except discord.HTTPException:
        await reply(content="I couldn't post in the poll's channel.", view=None)
        return
    db.set_proposal_message(bot.conn, proposal.id, message.channel.id, message.id)
    await reply(
        content=f"📣 Proposed {when_text(start, end)} ({timeutil.discord_ts(start * 3600, 'R')}) and pinged "
        f"{len(ids)} people: {message.jump_url}",
        view=None,
    )


def proposal_message(bot: MonkeyBot, proposal: db.FindATimeProposal) -> tuple[discord.Embed, discord.ui.View]:
    poll = db.get_poll(bot.conn, proposal.poll_id)
    entries = db.poll_entries(bot.conn, poll.id)
    responses = db.proposal_responses(bot.conn, proposal.id)
    start, end = proposal.start_hour * 3600, proposal.end_hour * 3600
    embed = discord.Embed(
        title="📣 Proposed time" + (f": {poll.title}" if poll.title else ""),
        description=(
            f"**{when_text(proposal.start_hour, proposal.end_hour)}** ({timeutil.discord_ts(start, 'R')})\n"
            f"Proposed by <@{proposal.proposer_id}>. Can you make it?"
        ),
        color=EMBED_COLOR,
    )
    yes = [r.name for r in responses if r.answer == "yes"]
    no = [r.name for r in responses if r.answer == "no"]
    other = [
        f"{r.name}: {timeutil.discord_ts(r.alt_start * 3600, 'F')} – {timeutil.discord_ts(r.alt_end * 3600, 't')}"
        for r in responses if r.answer == "other"
    ]
    answered = {r.user_id for r in responses}
    waiting = [e.name for e in entries if e.user_id not in answered]
    embed.add_field(name=f"✅ Confirmed ({len(yes)})", value=", ".join(yes)[:1024] or "*No one yet*", inline=False)
    embed.add_field(name=f"❌ Can't make it ({len(no)})", value=", ".join(no)[:1024] or "*No one*", inline=False)
    if other:
        embed.add_field(name=f"🔁 Suggested another time ({len(other)})", value="\n".join(other)[:1024], inline=False)
    embed.add_field(name=f"⏳ Waiting on ({len(waiting)})", value=", ".join(waiting)[:1024] or "*No one*", inline=False)
    embed.set_footer(text="Times are in your timezone · you can change your answer any time")
    view = discord.ui.View(timeout=None)
    for action in PROPOSAL_BUTTONS:
        view.add_item(ProposalButton(action, proposal.id))
    return embed, view


PROPOSAL_BUTTONS = {
    "yes": ("Confirm", discord.ButtonStyle.success, "✅"),
    "no": ("Can't make it", discord.ButtonStyle.danger, "❌"),
    "other": ("Propose different time", discord.ButtonStyle.secondary, "🔁"),
}


class ProposalButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"fatp:(?P<action>yes|no|other):(?P<proposal>\d+)",
):
    """Answer a proposal. The proposal id is in the custom_id, so they keep working after a restart."""

    def __init__(self, action: str, proposal_id: int):
        label, style, emoji = PROPOSAL_BUTTONS[action]
        super().__init__(
            discord.ui.Button(label=label, style=style, emoji=emoji, custom_id=f"fatp:{action}:{proposal_id}")
        )
        self.action = action
        self.proposal_id = proposal_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str], /):
        return cls(match["action"], int(match["proposal"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        bot: MonkeyBot = interaction.client  # type: ignore[assignment]
        proposal = db.get_proposal(bot.conn, self.proposal_id)
        if proposal is None:
            await interaction.response.send_message("This proposal no longer exists.", ephemeral=True)
            return
        name = getattr(interaction.user, "display_name", None) or interaction.user.name
        if interaction.user.id not in {e.user_id for e in db.poll_entries(bot.conn, proposal.poll_id)}:
            await interaction.response.send_message(
                "Only people who joined the poll can answer this. Join the poll to take part.", ephemeral=True
            )
            return
        if self.action in ("yes", "no"):
            db.set_proposal_response(bot.conn, proposal.id, interaction.user.id, name, self.action)
            embed, view = proposal_message(bot, proposal)
            await interaction.response.edit_message(embed=embed, view=view)  # pressed on the proposal
            return

        async def chosen(submit: discord.Interaction, start: int, end: int) -> None:
            db.set_proposal_response(bot.conn, proposal.id, submit.user.id, name, "other", (start, end))
            embed, view = proposal_message(bot, proposal)
            await submit.response.edit_message(embed=embed, view=view)  # submitted from the proposal

        tz, _ = viewer_timezone(bot, interaction.user.id)
        poll = db.get_poll(bot.conn, proposal.poll_id)
        await interaction.response.send_modal(TimeModal(bot, poll, tz, "Propose a different time", chosen))


# --------------------------------------------------------------------------- command


class FindATimeCog(commands.Cog):
    def __init__(self, bot: MonkeyBot):
        self.bot = bot

    @app_commands.command(name="findatime", description="Start a poll to find a time when several people are free")
    @app_commands.describe(title="What it's for, e.g. a boss name (optional)")
    @app_commands.guild_only()
    async def findatime(self, interaction: discord.Interaction, title: app_commands.Range[str, 1, 80] | None = None):
        tz, _ = viewer_timezone(self.bot, interaction.user.id)
        now = timeutil.now_utc()
        poll = db.create_poll(
            self.bot.conn, interaction.user.id, title.strip() if title else None, now.astimezone(tz).date(),
            int(now.timestamp()),
        )
        embed, view = poll_message(self.bot, poll)
        await interaction.response.send_message(embed=embed, view=view)
        message = await interaction.original_response()
        db.set_poll_message(self.bot.conn, poll.id, message.channel.id, message.id)


async def setup(bot: MonkeyBot) -> None:
    bot.add_dynamic_items(FindATimeButton, ProposalButton)
    await bot.add_cog(FindATimeCog(bot))
