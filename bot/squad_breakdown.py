"""Host views behind /cq_host: Availability (who is Preferred / Available for each squad, by name)
and Prep Roster (one squad's characters at a level, for slotting).

Availability is worked out per character (the player's weekly change or default, plus that
character's own overrides). Characters the player marked inactive are left out; static
characters are listed before subs so hosts can see who to slot first.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_DOWN, Decimal
from datetime import date
from typing import TYPE_CHECKING

import discord

from . import db, timeutil
from .class_icons import ClassIcons
from .views import EMBED_COLOR, LEVEL_EMOJI, get_squad_order, order_button_label, order_squads, other_order, set_squad_order

if TYPE_CHECKING:
    from .app import MonkeyBot

SHOWN_LEVELS = ("Preferred", "Available")
STATUS_ORDER = {"static": 0, "sub": 1}
EMBED_BUDGET = 5500  # Discord allows 6000 characters per embed; leave room for titles
# Per page of a squad's detail. Kept well under Discord's documented limits (4096 for the
# description, 6000 per embed): long embeds were seen cut off at ~3000 characters / 140
# lines, so pages stay small enough to always show in full.
PAGE_MAX_CHARS = 2000
PAGE_MAX_LINES = 80


def _lines(text: str) -> int:
    return text.count("\n") + 1 if text else 0


def _fits(text: str, extra: str) -> bool:
    return len(text) + len(extra) <= PAGE_MAX_CHARS and _lines(text + extra) <= PAGE_MAX_LINES


@dataclass
class Entry:
    player: db.Player
    characters: list = field(default_factory=list)  # sqlite rows, static first then by dmg

    @property
    def has_static(self) -> bool:
        return any(c["status"] == "static" for c in self.characters)


def build_breakdown(
    conn, players: list[db.Player], squads: list[db.SquadTime], week: date
) -> dict[int, dict[str, list[Entry]]]:
    """{squad: {level: [Entry]}} for Preferred and Available, using each player's usable characters."""
    result = {s.number: {lvl: [] for lvl in SHOWN_LEVELS} for s in squads}
    for player in players:
        characters = [c for c in db.list_characters(conn, player.id) if c["status"] != "inactive"]
        levels = {c["id"]: db.character_week_availability(conn, c, week) for c in characters}
        for s in squads:
            for level in SHOWN_LEVELS:
                matching = [c for c in characters if levels[c["id"]].get(s.number) == level]
                if matching:
                    matching.sort(key=lambda c: (STATUS_ORDER[c["status"]], -(c["dmg"] or 0)))
                    result[s.number][level].append(Entry(player, matching))
    for by_level in result.values():
        for entries in by_level.values():
            # players with a static character first, then by name
            entries.sort(key=lambda e: (not e.has_static, e.player.name.lower()))
    return result


def _player_name(entry: Entry) -> str:
    return entry.player.name


INDENT = "\u2003"  # em space: Discord strips ordinary leading spaces


def _character_label(c, icons: ClassIcons) -> str:
    """'<class icon> ragu' when the class has an icon, otherwise 'ragu/DRK'."""
    icon = icons.get(c["job"])
    return f"{icon} {c['ign']}" if icon else f"{c['ign']}/{c['job'] or '?'}"


# Class groups, in the game's usual order; used to order each player's characters
JOB_GROUPS = {
    "Warrior": ("HERO", "PAL", "DRK", "DW", "ARAN"),
    "Magician": ("FP", "IL", "BSP", "BW", "EVAN"),
    "Archer": ("BM", "MM", "WA"),
    "Thief": ("NL", "SHAD", "DB", "NW"),
    "Pirate": ("BUCC", "SAIR", "TB"),
}
GROUP_RANK = {job: rank for rank, jobs in enumerate(JOB_GROUPS.values()) for job in jobs}
VIEWS = ("class", "player")  # squad detail: grouped by class (default) or by player

# "Group by class" order: these roles first, classes in the order listed; every other class
# falls under DPS, A-Z.
CLASS_VIEW_ORDER = {
    "HP": ("DRK", "ARAN"),
    "BSP": ("BSP",),
    "SI": ("BUCC", "TB"),
    "CRIT": ("DB", "BM", "MM", "WA"),
}
# Which roles share a page in the "Group by class" view; each group starts a new page.
CLASS_VIEW_PAGES = (("HP", "BSP", "SI"), ("CRIT",), ("DPS",))
CLASS_VIEW_RANK = {
    job: (group, position)
    for group, jobs in enumerate(CLASS_VIEW_ORDER.values())
    for position, job in enumerate(jobs)
}


def _class_view_key(job: str) -> tuple:
    """Sort key for a class in the "Group by class" view: listed roles first, then DPS A-Z."""
    group, position = CLASS_VIEW_RANK.get(job, (len(CLASS_VIEW_ORDER), 0))
    return (group, position, job)
MAX_BLOCK = 1000  # a block must fit in one embed field (1024)


def _job(c) -> str:
    return (c["job"] or "?").upper()


def _status_rank(c) -> int:
    return 0 if c["status"] == "static" else 1


def _sub_mark(c) -> str:
    return "⏳ " if c["status"] == "sub" else ""


def _player_block(entry: Entry, icons: ClassIcons) -> str:
    """Player name on its own line, then one bullet per character (⏳ = sub).

    Characters: static before sub, then by class group (Warrior, Magician, Archer, Thief,
    Pirate; unknown jobs last), then by name.
    """
    ordered = sorted(
        entry.characters, key=lambda c: (_status_rank(c), GROUP_RANK.get(_job(c), 99), c["ign"].lower())
    )
    lines = [f"**{_player_name(entry)}**"]
    lines += [f"{INDENT}• {_sub_mark(c)}{_character_label(c, icons)}" for c in ordered]
    return "\n".join(lines)


def _role(job: str) -> str:
    return next((role for role, jobs in CLASS_VIEW_ORDER.items() if job in jobs), "DPS")


def _dmg_text(c) -> str:
    """Damage in billions, rounded DOWN to tenths: 3.08 -> '3.0b', 4.39 -> '4.3b'; '?' if unknown."""
    if c["dmg"] is None:
        return "?"
    # Decimal(str(...)) avoids float artifacts (4.3 is stored as 4.2999..., which would floor to 4.2)
    tenths = Decimal(str(c["dmg"])).quantize(Decimal("0.1"), rounding=ROUND_DOWN)
    return f"{tenths}b"


def _class_blocks(entries: list[Entry], icons: ClassIcons) -> list[tuple[str, str]]:
    """(role, block) per class: '<icon> DRK (6)', then '• `4.39b` - name', static before sub, then by damage.

    Classes: HP (DRK, ARAN), BSP, SI (BUCC, TB), CRIT (DB, BM, MM, WA), then DPS (the rest, A-Z).
    A class too long for one page is split, with '(cont.)' on the continued header.
    """
    by_job: dict[str, list] = {}
    for e in entries:
        for c in e.characters:
            by_job.setdefault(_job(c), []).append(c)
    blocks = []
    for job in sorted(by_job, key=_class_view_key):
        # static before sub, then highest damage first (unknown damage last), then name
        rows = sorted(
            by_job[job],
            key=lambda c: (_status_rank(c), c["dmg"] is None, -(c["dmg"] or 0), c["ign"].lower()),
        )
        icon = icons.get(job)
        header = f"{icon} **{job}** ({len(rows)})" if icon else f"**{job}** ({len(rows)})"
        block = header
        for c in rows:
            # damage first, as inline code (shaded monospace) so it stands apart when skimming
            line = f"\n{INDENT}• {_sub_mark(c)}`{_dmg_text(c)}` - {c['ign']}"
            # keep each block small enough for a page, with room for a role heading
            if len(block) + len(line) > PAGE_MAX_CHARS - 60 or _lines(block) >= PAGE_MAX_LINES - 2:
                blocks.append((_role(job), block))
                block = f"{header} (cont.)"
            block += line
        blocks.append((_role(job), block))
    return blocks


def _class_pages(blocks: list[tuple[str, str]]) -> list[str]:
    """Lay class blocks out as pages: HP + BSP + SI, then CRIT, then DPS (CLASS_VIEW_PAGES), skipping
    empty ones. Each role gets a heading, with a blank line between roles on the same page. A page
    too long to show in full continues on the next page, repeating the role heading with '(cont.)'."""
    by_role: dict[str, list[str]] = {}
    for role, block in blocks:
        by_role.setdefault(role, []).append(block)

    pages: list[str] = []
    for page_roles in CLASS_VIEW_PAGES:
        text = ""
        for role in (r for r in page_roles if r in by_role):
            for i, block in enumerate(by_role[role]):
                piece = f"__**{role}**__\n{block}" if i == 0 else block
                sep = ("\n\n" if i == 0 else "\n") if text else ""
                if text and not _fits(text, sep + piece):  # page full: continue on a new page
                    pages.append(text)
                    text, sep = "", ""
                    if i > 0:
                        piece = f"__**{role}**__ (cont.)\n{block}"
                text += sep + piece
        if text:
            pages.append(text)
    return pages


def _fit(items: list[str], limit: int) -> str:
    """Join items with ', ' within `limit` characters, ending with '… +N more' if some don't fit."""
    reserve = len(f" … +{len(items)} more")
    shown: list[str] = []
    length = 0
    for i, item in enumerate(items):
        add = len(item) + (2 if shown else 0)
        last = i == len(items) - 1
        if length + add + (0 if last else reserve) > limit:
            return ", ".join(shown) + f" … +{len(items) - len(shown)} more"
        shown.append(item)
        length += add
    return ", ".join(shown)


class SquadBreakdownView(discord.ui.View):
    """/cq_host > Availability: 📋 Overview (counts per squad), then 🟢 / 🟡 for every squad's players
    at that level (names). One squad's characters are in PrepRosterView, which shares the data and
    the squad detail embeds below.

    State: `level` (Preferred/Available) and `squad` (a squad number, or None for all squads; only
    PrepRosterView picks one).
    """

    def __init__(
        self,
        bot: "MonkeyBot",
        week: date,
        players: list[db.Player],
        overview,  # an Embed, or a function(order) -> Embed
        user_id: int | None = None,
    ):
        super().__init__(timeout=900)
        self.week = week
        self.overview = overview
        self.conn = bot.conn
        self.user_id = user_id
        self.order = get_squad_order(bot.conn, user_id)  # the host's remembered squad order
        self.squads = order_squads(db.squads_for_week(bot.conn, week, bot.tz), self.order)
        self.data = build_breakdown(bot.conn, players, self.squads, week)
        self.tz = bot.tz
        self.icons = bot.class_icons
        self.level = "Preferred"
        self.squad: int | None = None
        self.page = 0  # page within a squad's detail
        self.showing_overview = True  # the message opens on the overview (counts per squad)
        self.view_mode = "class"  # squad detail grouped by "class" (default) or by "player"
        self._build()

    @staticmethod
    def _style(selected: bool) -> discord.ButtonStyle:
        # The button for what's on screen is filled blue so it reads as the selected tab; the others are grey.
        return discord.ButtonStyle.primary if selected else discord.ButtonStyle.secondary

    def _add_level_buttons(self, row: int) -> None:
        for level in ("Preferred", "Available"):
            selected = not self.showing_overview and self.level == level
            button = discord.ui.Button(label=level, emoji=LEVEL_EMOJI[level], style=self._style(selected), row=row)
            button.callback = self._level_callback(level)
            self.add_item(button)

    def _build(self) -> None:
        """Overview, Preferred, Available, and the 🔀 squad order toggle."""
        self.clear_items()
        overview = discord.ui.Button(label="Overview", emoji="📋", style=self._style(self.showing_overview), row=0)
        overview.callback = self._show_overview
        self.add_item(overview)
        self._add_level_buttons(row=0)
        sort = discord.ui.Button(
            label=order_button_label(other_order(self.order)), emoji="🔀", style=discord.ButtonStyle.secondary, row=1
        )
        sort.callback = self._toggle_order
        self.add_item(sort)

    def current_embed(self) -> discord.Embed:
        if self.showing_overview:
            return self.overview(self.order) if callable(self.overview) else self.overview
        if self.squad is None:
            return self.level_embed(self.level)
        return self.squad_embed(self.squad, self.level, self.page)

    # ------------------------------------------------------------------ embeds

    def level_embed(self, level: str) -> discord.Embed:
        """Every squad with the players at `level` (names only; pick a squad for characters)."""
        embed = discord.Embed(
            title=f"{LEVEL_EMOJI[level]} {level} players per squad: {timeutil.week_label(self.week)}",
            description="⏳ = the player has no static character at this level, only ⏳ Flex ones; "
            "they're listed after players with a static character. Prep Roster in /cq_host shows their characters.",
            color=EMBED_COLOR,
        )
        per_field = min(1024, EMBED_BUDGET // max(1, len(self.squads)))
        for s in self.squads:
            entries = self.data[s.number][level]
            names = [("" if e.has_static else "⏳ ") + _player_name(e) for e in entries]
            ts = timeutil.discord_ts(s.starts_at)
            value = ts + "\n" + (_fit(names, per_field - len(ts) - 1) if names else "*No one*")
            embed.add_field(name=f"Squad {s.number} ({len(entries)})", value=value, inline=False)
        embed.set_footer(text="⏳ = only ⏳ Flex characters")
        return embed

    def detail_pages(self, number: int, level: str) -> list[list[str]]:
        """A squad's detail as pages. Class view: one text per page (shown in the description, so
        the only gaps are the blank lines between roles). Player view: embed fields per page."""
        entries = sorted(self.data[number][level], key=lambda e: e.player.name.lower())  # alphabetical
        if self.view_mode == "class":
            return [[text] for text in _class_pages(_class_blocks(entries, self.icons))] or [[]]
        pages: list[list[str]] = []
        fields: list[str] = []
        field = ""
        for block in (_player_block(e, self.icons)[:MAX_BLOCK] + "\n" for e in entries):
            if len(field) + len(block) > 1024:  # field full: start a new field
                fields.append(field)
                field = ""
            page_text = "".join(fields) + field
            if page_text and not _fits(page_text, block):  # page full: start a new page
                if field:
                    fields.append(field)
                pages.append(fields)
                fields, field = [], ""
            field += block
        if field:
            fields.append(field)
        if fields:
            pages.append(fields)
        return pages or [[]]

    def squad_embed(self, number: int, level: str, page: int = 0) -> discord.Embed:
        squad = next(s for s in self.squads if s.number == number)
        count = len(self.data[number][level])
        pages = self.detail_pages(number, level)
        page = min(page, len(pages) - 1)
        embed = discord.Embed(
            title=f"Squad {number}: {LEVEL_EMOJI[level]} {level} ({count} players)",
            description=f"{timeutil.discord_ts(squad.starts_at)} · {timeutil.week_label(self.week)}",
            color=EMBED_COLOR,
        )
        paging = f"Page {page + 1}/{len(pages)} (◀ ▶) · " if len(pages) > 1 else ""
        grouping = (
            "Grouped by player (A-Z) · characters: static, then class group, then name"
            if self.view_mode == "player"
            else "Grouped by class: HP, BSP, SI | CRIT | DPS A-Z · damage (billions) - character, "
            "static first, then highest damage"
        )
        embed.set_footer(text=f"{paging}{grouping} · ⏳ = Flex character · Inactive characters hidden")
        if self.view_mode == "class":
            embed.description += "\n\n" + (pages[page][0] if pages[page] else "*No one*")
            return embed
        for field in pages[page] or ["*No one*"]:
            embed.add_field(name="\u200b", value=field, inline=False)
        return embed

    # ------------------------------------------------------------------ callbacks

    async def _refresh(self, interaction: discord.Interaction, embed: discord.Embed | None = None) -> None:
        self._build()
        await interaction.response.edit_message(embed=embed or self.current_embed(), view=self)

    def _level_callback(self, level: str):
        async def callback(interaction: discord.Interaction) -> None:
            self.level = level
            self.page = 0
            self.showing_overview = False
            await self._refresh(interaction)

        return callback

    async def _toggle_order(self, interaction: discord.Interaction) -> None:
        self.order = other_order(self.order)
        set_squad_order(self.conn, self.user_id, self.order)  # remembered for this host
        self.squads = order_squads(self.squads, self.order)
        await self._refresh(interaction)

    async def _show_overview(self, interaction: discord.Interaction) -> None:
        self.squad = None
        self.page = 0
        self.showing_overview = True
        await self._refresh(interaction)


class PrepRosterView(SquadBreakdownView):
    """/cq_host > Prep Roster: one squad's characters at a level, for slotting. The dropdown picks the
    squad, 🟢 / 🟡 the level; ◀ ▶ page through long squads and 🔀 groups by class or by player.
    Opens on the first squad (in the host's squad order)."""

    def __init__(self, bot: "MonkeyBot", week: date, players: list[db.Player], user_id: int | None = None):
        super().__init__(bot, week, players, overview=None, user_id=user_id)
        self.showing_overview = False
        self.squad = self.squads[0].number if self.squads else None
        self._build()

    def current_embed(self) -> discord.Embed:
        if self.squad is None:
            return discord.Embed(
                title=f"Prep roster: {timeutil.week_label(self.week)}", description="*No squads configured.*",
                color=EMBED_COLOR,
            )
        return self.squad_embed(self.squad, self.level, self.page)

    def _build(self) -> None:
        self.clear_items()
        if self.squad is None:
            return
        select = discord.ui.Select(
            placeholder="Pick a squad…",
            options=[
                discord.SelectOption(
                    label=f"Squad {s.number} · "
                    f"{timeutil.local_label(s.starts_at, self.tz, with_zone=True, twelve_hour=True)}",
                    value=str(s.number),
                    description=(
                        f"🟢 {len(self.data[s.number]['Preferred'])} preferred · "
                        f"🟡 {len(self.data[s.number]['Available'])} available"
                    ),
                    default=self.squad == s.number,
                )
                for s in self.squads[:25]
            ],
            row=0,
        )
        select.callback = self._pick_squad
        self.add_item(select)
        self._add_level_buttons(row=1)
        page_count = len(self.detail_pages(self.squad, self.level))
        if page_count > 1:
            for label, step, disabled in [("◀", -1, self.page == 0), ("▶", 1, self.page >= page_count - 1)]:
                button = discord.ui.Button(label=label, style=discord.ButtonStyle.secondary, row=1, disabled=disabled)
                button.callback = self._page_callback(step)
                self.add_item(button)
        other = "class" if self.view_mode == "player" else "player"
        toggle = discord.ui.Button(label=f"Group by {other}", emoji="🔀", style=discord.ButtonStyle.secondary, row=2)
        toggle.callback = self._toggle_view_mode
        self.add_item(toggle)

    # ------------------------------------------------------------------ callbacks

    async def _pick_squad(self, interaction: discord.Interaction) -> None:
        self.squad = int(interaction.data["values"][0])
        self.page = 0
        await self._refresh(interaction)

    async def _toggle_view_mode(self, interaction: discord.Interaction) -> None:
        self.view_mode = "class" if self.view_mode == "player" else "player"
        self.page = 0
        await self._refresh(interaction)

    def _page_callback(self, step: int):
        async def callback(interaction: discord.Interaction) -> None:
            self.page = max(0, self.page + step)
            await self._refresh(interaction)

        return callback
