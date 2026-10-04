"""The reminder post (role ping) and the personal availability view behind its button (no network)."""

import asyncio
import dataclasses
import re
from datetime import timedelta
from types import SimpleNamespace

import discord
import pytest

from bot import db, timeutil
from bot.app import MonkeyBot
from bot.config import Config
from bot.views import LayoutToggleButton, get_layout, player_summary_embed, send_reminder

# The current week: the buttons refuse to change weeks that are already over.
WEEK = timeutil.current_week_start(timeutil.now_utc(), Config.from_env().timezone)


class FakeChannel:
    def __init__(self):
        self.sent = []

    async def send(self, **kwargs):
        self.sent.append(kwargs)
        return SimpleNamespace(jump_url="https://discord.test/msg")


class FakeResponse:
    def __init__(self):
        self.edits = []

    async def edit_message(self, **kwargs):
        self.edits.append(kwargs)


def make_bot(tmp_path, **config_changes):
    config = dataclasses.replace(Config.from_env(), database_path=tmp_path / "r.db", **config_changes)
    bot = MonkeyBot(config)
    for number, weekday, hhmm in [(1, 0, "12:00"), (2, 1, "11:00"), (3, 4, "21:00"), (4, 6, "11:00")]:
        db.set_squad_template(bot.conn, number, weekday, hhmm)
    player = db.create_player(bot.conn, name="Tester", discord_handle="@tester", discord_id=42)
    db.set_default_availability(
        bot.conn, player.id, {1: "Preferred", 2: "Not Available", 3: "Available", 4: "Preferred"}
    )
    return bot, player


@pytest.fixture
def setup(tmp_path):
    bot, player = make_bot(tmp_path, player_role_id=555)
    yield bot, player
    bot.conn.close()


def test_reminder_pings_player_role(setup):
    bot, _ = setup
    channel = FakeChannel()
    asyncio.run(send_reminder(bot, channel, WEEK))
    [sent] = channel.sent
    assert sent["content"] == "<@&555>"
    mentions = sent["allowed_mentions"]
    assert [r.id for r in mentions.roles] == [555] and mentions.everyone is False and mentions.users is False
    labels = [child.item.label for child in sent["view"].children]
    assert labels == ["View my availability", "No change", "Change weekly availability"]


def test_reminder_without_player_role_has_no_ping(tmp_path):
    bot, _ = make_bot(tmp_path, player_role_id=None)
    channel = FakeChannel()
    asyncio.run(send_reminder(bot, channel, WEEK))
    assert channel.sent[0]["content"] is None
    bot.conn.close()


class Response:
    def __init__(self):
        self.sent, self.edited = None, None

    async def send_message(self, content=None, **kwargs):
        self.sent = {"content": content, **kwargs}

    async def edit_message(self, **kwargs):
        self.edited = kwargs


def click(bot, *, ephemeral_message=None):
    """A fake interaction from user 42; `ephemeral_message` = pressed on a private (True) or public message."""
    message = None if ephemeral_message is None else SimpleNamespace(flags=SimpleNamespace(ephemeral=ephemeral_message))
    return SimpleNamespace(
        client=bot, user=SimpleNamespace(id=42, name="tester"), message=message, response=Response()
    )


def rows(view):
    return [[c["label"] for c in r["components"]] for r in view.to_components()]


def test_summary_is_always_grouped_by_availability(setup):
    bot, player = setup
    text = player_summary_embed(bot, player, WEEK).description
    preferred, available, not_available = text.split("\n\n")[2:5]  # after the check-in and note lines
    assert preferred.startswith("🟢 **Preferred (2)**") and "Squad 1**" in preferred and "Squad 4**" in preferred
    assert available.startswith("🟡 **Available (1)**") and "Squad 3**" in available
    assert not_available.startswith("🔴 **Not Available (1)**") and "Squad 2**" in not_available
    assert "Your default schedule" in text


def test_view_uses_this_weeks_change_and_lists_differences(setup):
    bot, player = setup
    db.set_weekly_availability(
        bot.conn, player.id, WEEK, {1: "Not Available", 2: "Not Available", 3: "Available", 4: "Preferred"}
    )
    embed = player_summary_embed(bot, player, WEEK)
    assert "Changed for this week only" in embed.description
    assert re.search(r"🔴 \*\*Not Available \(2\)\*\*\n\*\*Squad 1\*\*", embed.description)
    diffs = next(f for f in embed.fields if f.name == "Different from your default")
    assert diffs.value == "Squad 1: 🟢 → 🔴"


def test_button_rows_and_check_in_status(setup):
    from bot.views import player_summary_view

    bot, player = setup
    view = player_summary_view(bot, player, WEEK, "time")
    assert rows(view) == [
        ["No change", "Change weekly availability", "Change a character's week", "Update a future week"],
        ["Sort by squad #", "Change default availability"],
    ]
    assert "haven't checked in" in player_summary_embed(bot, player, WEEK).description
    first = view.children[0].item
    assert first.style == discord.ButtonStyle.success  # prompting: green

    db.confirm_no_change(bot.conn, player.id, WEEK)
    first = player_summary_view(bot, player, WEEK, "time").children[0].item
    assert (first.label, first.style) == ("Checked in", discord.ButtonStyle.secondary)
    assert "You're checked in:** using your default" in player_summary_embed(bot, player, WEEK).description

    db.set_weekly_availability(bot.conn, player.id, WEEK, {1: "Not Available"})
    first = player_summary_view(bot, player, WEEK, "time").children[0].item
    assert (first.label, first.style) == ("Use my default", discord.ButtonStyle.secondary)
    assert "You're checked in:** you changed this week" in player_summary_embed(bot, player, WEEK).description


def test_no_change_refreshes_own_view_and_replies_to_reminder(setup):
    from bot.views import ReminderButton

    bot, player = setup
    db.set_weekly_availability(bot.conn, player.id, WEEK, {1: "Not Available"})

    own = click(bot, ephemeral_message=True)  # pressed on their private /cq availability
    asyncio.run(ReminderButton("nochange", WEEK.isoformat()).callback(own))
    assert db.get_confirmation(bot.conn, player.id, WEEK) == "no_change"
    assert db.get_weekly_availability(bot.conn, player.id, WEEK) == {}  # back to the default
    assert own.response.sent is None and own.response.edited  # edited in place...
    assert rows(own.response.edited["view"])[0][0] == "Checked in"  # ...showing they're checked in

    public = click(bot, ephemeral_message=False)  # pressed on the shared reminder
    asyncio.run(ReminderButton("nochange", WEEK.isoformat()).callback(public))
    assert public.response.sent["ephemeral"] and "Thanks" in public.response.sent["content"]


def test_change_default_button_opens_default_editor(setup):
    from bot.views import AvailabilityEditor, ReminderButton

    bot, player = setup
    interaction = click(bot)
    asyncio.run(ReminderButton("default", WEEK.isoformat()).callback(interaction))
    editor = interaction.response.sent["view"]
    assert isinstance(editor, AvailabilityEditor) and editor.mode == "default"
    assert editor.draft == db.get_default_availability(bot.conn, player.id)


def test_update_a_future_week_button_opens_week_picker(setup):
    from bot.views import ReminderButton, WeekPicker

    bot, _ = setup
    interaction = click(bot)
    asyncio.run(ReminderButton("plan").callback(interaction))
    assert isinstance(interaction.response.sent["view"], WeekPicker)


def test_change_a_characters_week(setup):
    from bot.squad_breakdown import build_breakdown
    from bot.views import AvailabilityEditor, CharacterPicker, ReminderButton

    bot, player = setup  # default: 1 Preferred, 2 Not Available, 3 Available, 4 Preferred
    db.add_character(bot.conn, player.id, "Main", "NL", "DPS", 4.0, "static")
    db.add_character(bot.conn, player.id, "Alt", "DRK", "HB", 3.0, "sub")
    alt = db.get_character(bot.conn, "Alt")
    db.set_character_overrides(bot.conn, alt["id"], {2: "Preferred"})  # ongoing exception

    interaction = click(bot)
    asyncio.run(ReminderButton("charweek", WEEK.isoformat()).callback(interaction))
    picker = interaction.response.sent["view"]
    assert isinstance(picker, CharacterPicker) and picker.this_week_only
    assert {o.label: o.description for o in picker.children[0].options} == {
        "Main (NL)": "Static · No changes this week", "Alt (DRK)": "Flex · No changes this week"
    }

    pick = SimpleNamespace(data={"values": [str(alt["id"])]}, response=Response())
    asyncio.run(picker._pick(pick))
    editor = pick.response.edited["view"]
    assert isinstance(editor, AvailabilityEditor) and editor.mode == "character_week"
    assert editor.draft == {1: "Preferred", 2: "Preferred", 3: "Available", 4: "Preferred"}  # its usual week
    assert "this week only" in editor.embed().title or "only" in editor.embed().title

    editor.draft[4] = "Not Available"  # can't bring Alt to squad 4 this week
    assert "Not Available ✏️ (usual 🟢)" in editor.embed().description
    asyncio.run(editor._save(SimpleNamespace(response=Response())))

    assert db.get_character_weekly(bot.conn, alt["id"], WEEK) == {4: "Not Available"}  # only the change
    assert db.get_character_overrides(bot.conn, alt["id"]) == {2: "Preferred"}  # ongoing exception untouched
    assert db.get_confirmation(bot.conn, player.id, WEEK) == "updated"  # counts as checking in
    # hosts see it: Alt isn't available for squad 4 this week, but still is next week
    squads = db.squads_for_week(bot.conn, WEEK, bot.tz)
    this_week = build_breakdown(bot.conn, [player], squads, WEEK)
    assert [c["ign"] for e in this_week[4]["Preferred"] for c in e.characters] == ["Main"]
    next_week = build_breakdown(bot.conn, [player], squads, WEEK + timedelta(days=7))
    assert sorted(c["ign"] for e in next_week[4]["Preferred"] for c in e.characters) == ["Alt", "Main"]
    # the player sees it on /cq availability
    field = next(f for f in player_summary_embed(bot, player, WEEK).fields if f.name == "Characters changed for this week")
    assert field.value == "🧩 **Alt**: Squad 4 🔴"

    # "Use my default" puts the whole week back, character changes included
    db.confirm_no_change(bot.conn, player.id, WEEK)
    assert db.get_character_weekly(bot.conn, alt["id"], WEEK) == {}


def test_ongoing_character_availability_still_in_cq_characters(setup):
    from bot.views import AvailabilityEditor, CharacterPicker, ReminderButton

    bot, player = setup
    db.add_character(bot.conn, player.id, "Alt", "DRK", "HB", 3.0, "sub")
    alt = db.get_character(bot.conn, "Alt")
    db.set_character_overrides(bot.conn, alt["id"], {2: "Preferred"})
    interaction = click(bot)
    asyncio.run(ReminderButton("chars", WEEK.isoformat()).callback(interaction))
    picker = interaction.response.sent["view"]
    assert isinstance(picker, CharacterPicker) and not picker.this_week_only
    assert picker.children[0].options[0].description == "Flex · 1 squad(s) differ from your default"
    pick = SimpleNamespace(data={"values": [str(alt["id"])]}, response=Response())
    asyncio.run(picker._pick(pick))
    assert pick.response.edited["view"].mode == "character"


def test_cq_characters_has_character_availability_button(setup):
    from bot.cogs.player import CqCog
    from bot.views import CharacterPicker, ReminderButton

    bot, player = setup
    db.add_character(bot.conn, player.id, "Main", "NL", "DPS", 4.0, "static")

    class Response:
        async def send_message(self, **kwargs):
            self.sent = kwargs

    interaction = SimpleNamespace(client=bot, user=SimpleNamespace(id=42, name="tester"), response=Response())
    asyncio.run(CqCog.characters.callback(CqCog(bot), interaction))
    labels = [c["label"] for row in interaction.response.sent["view"].to_components() for c in row["components"]]
    assert labels == ["Set character status", "Character availability", "Damage history"]

    button = next(c for c in interaction.response.sent["view"].children if isinstance(c, ReminderButton))
    click = SimpleNamespace(client=bot, user=SimpleNamespace(id=42, name="tester"), response=Response())
    asyncio.run(button.callback(click))
    assert isinstance(click.response.sent["view"], CharacterPicker)


def test_cq_characters_damage_history_button(setup):
    from bot import damage
    from bot.cogs.player import CqCog
    from bot.views import DamageHistoryPicker

    bot, player = setup
    db.add_character(bot.conn, player.id, "Main", "NL", "DPS", 4.0, "static")
    log = damage.parse_log(
        "[Start Time] 27-09-2026 01:00:00\n[Finish Time] 27-09-2026 01:27:30\n>>Main: 5,000,000,000\n",
        bot.config.log_timezone,
    )
    damage.record_log(bot.conn, log, message_id=1)

    class Response:
        async def send_message(self, **kwargs):
            self.sent = kwargs

        async def edit_message(self, **kwargs):
            self.edited = kwargs

    interaction = SimpleNamespace(client=bot, user=SimpleNamespace(id=42, name="tester"), response=Response())
    asyncio.run(CqCog.characters.callback(CqCog(bot), interaction))
    history_button = next(
        c for c in interaction.response.sent["view"].children
        if isinstance(c, discord.ui.Button) and c.label == "Damage history"
    )
    click = SimpleNamespace(response=Response())
    asyncio.run(history_button.callback(click))
    picker = click.response.sent["view"]
    assert isinstance(picker, DamageHistoryPicker)
    assert [o.label for o in picker.children[0].options] == ["Main (NL)"]  # only your own characters

    main = db.get_character(bot.conn, "Main")
    pick = SimpleNamespace(data={"values": [str(main["id"])]}, response=Response())
    asyncio.run(picker._pick(pick))
    embed = pick.response.edited["embed"]
    assert embed.title == "Main (NL): damage 5.00"
    assert "5.00B in 27.5 min → **5.00**" in embed.description and embed.description.startswith("★")
    assert pick.response.edited["view"].children[0].options[0].default  # dropdown stays, to switch characters


def test_cq_characters_prompts_who_to_contact(tmp_path):
    from bot.cogs.player import CqCog

    for contact, expected in [("Robin", "Message Robin."), ("", "Message a host.")]:
        bot, player = make_bot(tmp_path / (contact or "default"), player_role_id=None)
        bot.config = dataclasses.replace(bot.config, roster_contact=contact or "a host")  # as Config.from_env does
        interaction = click(bot)
        asyncio.run(CqCog.characters.callback(CqCog(bot), interaction))
        description = interaction.response.sent["embed"].description
        assert description.endswith(f"➕ Want to add new characters? {expected}")
        bot.conn.close()


def test_timestamps_include_the_weekday(setup):
    from bot.squad_breakdown import SquadBreakdownView
    from bot.views import AvailabilityEditor

    bot, player = setup
    texts = [
        player_summary_embed(bot, player, WEEK).description,
        AvailabilityEditor(bot, mode="weekly", player=player, week=WEEK, initial={}).embed().description,
    ]
    view = SquadBreakdownView(bot, WEEK, [player], discord.Embed(title="o"))
    texts.append("\n".join(f.value for f in view.level_embed("Preferred").fields))
    for text in texts:
        assert ":F>" in text  # Discord's long style: "Sunday, September 27, 2026 12:00 PM"
        assert ":f>" not in text and ":d>" not in text
