"""Squad ordering in the /cq availability summary and the availability editor (no network)."""

import asyncio
import dataclasses
import re
from datetime import date
from types import SimpleNamespace

import discord
import pytest

from bot import db
from bot.app import MonkeyBot
from bot.config import Config
from bot.views import AvailabilityEditor, SortToggleButton, get_squad_order, player_summary_embed

WEEK = date(2026, 9, 27)
# Squad numbers deliberately out of time order: Sunday 12:00, Monday 11:00, Monday 22:00
SQUADS = {1: (0, "12:00"), 14: (1, "11:05"), 4: (1, "22:10")}


class FakeResponse:
    def __init__(self):
        self.edits = []

    async def edit_message(self, **kwargs):
        self.edits.append(kwargs)


@pytest.fixture
def bot(tmp_path):
    bot = MonkeyBot(dataclasses.replace(Config.from_env(), database_path=tmp_path / "views.db"))
    for number, (weekday, hhmm) in SQUADS.items():
        db.set_squad_template(bot.conn, number, weekday, hhmm)
    db.create_player(bot.conn, name="Tester", discord_handle="@tester", discord_id=42)
    yield bot
    bot.conn.close()


def squad_numbers_in(text: str) -> list[int]:
    return [int(n) for n in re.findall(r"Squad (\d+)\*\*", text)]


def test_summary_defaults_to_time_order(bot):
    player = db.get_player_by_discord_id(bot.conn, 42)
    embed = player_summary_embed(bot, player, WEEK)
    assert squad_numbers_in(embed.description) == [1, 14, 4]
    assert "Sorted by time" in embed.footer.text


def test_editor_defaults_to_time_order_and_toggles(bot):
    player = db.get_player_by_discord_id(bot.conn, 42)
    editor = AvailabilityEditor(bot, mode="weekly", player=player, week=WEEK, initial={4: "Preferred"})
    assert [s.number for s in editor.page_squads()] == [1, 14, 4]
    assert [c.placeholder for c in editor.children if hasattr(c, "placeholder")][0] == "Squad 1: not set"

    interaction = SimpleNamespace(response=FakeResponse())
    asyncio.run(editor._toggle_order(interaction))
    assert [s.number for s in editor.page_squads()] == [1, 4, 14]
    assert editor.draft == {4: "Preferred"}  # choices survive the toggle
    assert get_squad_order(bot.conn, player.discord_id) == "number"  # remembered

    # the next editor and summary open in the remembered order
    again = AvailabilityEditor(bot, mode="default", player=player, week=WEEK, initial={})
    assert [s.number for s in again.page_squads()] == [1, 4, 14]
    assert squad_numbers_in(player_summary_embed(bot, player, WEEK).description) == [1, 4, 14]


def test_summary_toggle_button(bot):
    player = db.get_player_by_discord_id(bot.conn, 42)
    interaction = SimpleNamespace(
        client=bot, user=SimpleNamespace(id=42, name="tester"), response=FakeResponse()
    )
    asyncio.run(SortToggleButton("number", WEEK.isoformat()).callback(interaction))
    [edit] = interaction.response.edits
    assert squad_numbers_in(edit["embed"].description) == [1, 4, 14]
    # the button in the refreshed view now offers switching back to time order
    labels = [item.item.label for item in edit["view"].children if isinstance(item, SortToggleButton)]
    assert labels == ["Sort by time"]
    assert get_squad_order(bot.conn, player.discord_id) == "number"


def test_squad_order_is_remembered_across_every_view(bot):
    """Switching order anywhere switches it everywhere, for that Discord user only."""
    from bot.squad_breakdown import PrepRosterView, SquadBreakdownView
    from bot.views import OrderToggleView, SquadTimesButton, get_squad_order, squad_times_embed

    player = db.get_player_by_discord_id(bot.conn, 42)
    assert get_squad_order(bot.conn, 42) == "time"  # everyone starts in time order

    # a host-style private list (e.g. /cq_host > Availability) toggled by user 42...
    render = lambda order: squad_times_embed(bot, WEEK, order)  # noqa: E731
    view = OrderToggleView(render, bot.conn, 42)
    assert squad_numbers_in(view.embed().description) == [1, 14, 4]
    asyncio.run(view._toggle(SimpleNamespace(response=FakeResponse())))

    # ...now applies to the player summary, the editors and the host breakdown for user 42
    assert squad_numbers_in(player_summary_embed(bot, player, WEEK).description) == [1, 4, 14]
    editor = AvailabilityEditor(bot, mode="weekly", player=player, week=WEEK, initial={})
    assert [s.number for s in editor.squads] == [1, 4, 14]
    breakdown = SquadBreakdownView(bot, WEEK, [player], discord.Embed(title="o"), user_id=42)
    assert [s.number for s in breakdown.squads] == [1, 4, 14]
    prep = PrepRosterView(bot, WEEK, [player], user_id=42)
    assert [o.value for o in prep.children[0].options] == ["1", "4", "14"]  # Prep Roster's squad dropdown too
    assert prep.squad == 1  # opens on the first squad
    assert OrderToggleView(render, bot.conn, 42).order == "number"

    # other users are unaffected
    assert get_squad_order(bot.conn, 99) == "time"
    assert [s.number for s in SquadBreakdownView(bot, WEEK, [player], discord.Embed(title="o"), user_id=99).squads] == [1, 14, 4]


def test_old_reminder_sort_button_still_works(bot):
    """New reminders don't have this button; ones posted earlier do, and it must keep working."""
    from bot.views import SquadTimesButton, get_squad_order

    class Response(FakeResponse):
        def __init__(self):
            super().__init__()
            self.sent = []

        async def send_message(self, **kwargs):
            self.sent.append(kwargs)

    public = SimpleNamespace(flags=SimpleNamespace(ephemeral=False))
    interaction = SimpleNamespace(client=bot, user=SimpleNamespace(id=7), message=public, response=Response())
    asyncio.run(SquadTimesButton("number", WEEK.isoformat()).callback(interaction))
    [sent] = interaction.response.sent
    assert sent["ephemeral"] and squad_numbers_in(sent["embed"].description) == [1, 4, 14]
    assert get_squad_order(bot.conn, 7) == "number"

    private = SimpleNamespace(flags=SimpleNamespace(ephemeral=True))
    interaction = SimpleNamespace(client=bot, user=SimpleNamespace(id=7), message=private, response=Response())
    asyncio.run(SquadTimesButton("time", WEEK.isoformat()).callback(interaction))
    [edit] = interaction.response.edits  # the private copy is re-sorted in place
    assert squad_numbers_in(edit["embed"].description) == [1, 14, 4]
    assert get_squad_order(bot.conn, 7) == "time"


def test_character_editor_marks_rows_that_differ_from_default(bot):
    player = db.get_player_by_discord_id(bot.conn, 42)
    db.set_default_availability(bot.conn, player.id, {1: "Preferred", 14: "Available", 4: "Not Available"})
    db.add_character(bot.conn, player.id, "Alt", "DRK", 3.0, "sub")
    alt = db.get_character(bot.conn, "Alt")
    db.set_character_overrides(bot.conn, alt["id"], {14: "Not Available"})

    initial = {**db.get_default_availability(bot.conn, player.id), **db.get_character_overrides(bot.conn, alt["id"])}
    editor = AvailabilityEditor(bot, mode="character", player=player, week=WEEK, initial=initial, character=alt)

    def marked():
        return [int(n) for n in re.findall(r"Squad (\d+)\*\*.*✏️", editor.embed().description)]

    assert marked() == [14]  # the saved override
    assert "Not Available ✏️ (default 🟡)" in editor.embed().description  # shows what the default is
    assert "✏️ = differs from your default" in editor.embed().footer.text
    editor.draft[1] = "Available"  # live: marked as soon as it's picked, before Save
    assert marked() == [1, 14]
    editor.draft[14] = "Available"  # back to the default: mark goes away
    assert marked() == [1]

    response = FakeResponse()
    asyncio.run(editor._save(SimpleNamespace(response=response)))
    saved = response.edits[-1]["embed"]
    assert [int(n) for n in re.findall(r"Squad (\d+)\*\*.*✏️", saved.description)] == [1]
    assert "Available ✏️ (default 🟢)" in saved.description

    weekly = AvailabilityEditor(bot, mode="weekly", player=player, week=WEEK, initial={1: "Not Available"})
    assert "✏️" not in weekly.embed().description  # only the character editor marks rows
