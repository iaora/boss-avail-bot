"""The reminder post (role ping) and the personal availability view behind its button (no network)."""

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
from bot.views import LayoutToggleButton, get_layout, player_summary_embed, send_reminder

WEEK = date(2026, 9, 27)


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
    assert labels == ["View my availability", "No change", "Update this week"]


def test_reminder_without_player_role_has_no_ping(tmp_path):
    bot, _ = make_bot(tmp_path, player_role_id=None)
    channel = FakeChannel()
    asyncio.run(send_reminder(bot, channel, WEEK))
    assert channel.sent[0]["content"] is None
    bot.conn.close()


def test_list_layout_shows_every_squad_in_order(setup):
    bot, player = setup
    embed = player_summary_embed(bot, player, WEEK)
    assert re.findall(r"(\S+) \*\*Squad (\d+)\*\*", embed.description) == [
        ("🟢", "1"), ("🔴", "2"), ("🟡", "3"), ("🟢", "4")
    ]
    assert "Your default schedule" in embed.description
    assert "Showing full list" in embed.footer.text


def test_grouped_layout_sections_by_level(setup):
    bot, player = setup
    text = player_summary_embed(bot, player, WEEK, layout="grouped").description
    preferred, available, not_available = text.split("\n\n")[1:4]
    assert preferred.startswith("🟢 **Preferred (2)**") and "Squad 1**" in preferred and "Squad 4**" in preferred
    assert available.startswith("🟡 **Available (1)**") and "Squad 3**" in available
    assert not_available.startswith("🔴 **Not Available (1)**") and "Squad 2**" in not_available


def test_view_uses_this_weeks_change_and_lists_differences(setup):
    bot, player = setup
    db.set_weekly_availability(
        bot.conn, player.id, WEEK, {1: "Not Available", 2: "Not Available", 3: "Available", 4: "Preferred"}
    )
    embed = player_summary_embed(bot, player, WEEK)
    assert "Changed for this week only" in embed.description
    assert re.search(r"🔴 \*\*Squad 1\*\*", embed.description)
    diffs = next(f for f in embed.fields if f.name == "Different from your default")
    assert diffs.value == "Squad 1: 🟢 → 🔴"


def test_layout_toggle_is_remembered(setup):
    bot, player = setup
    interaction = SimpleNamespace(client=bot, user=SimpleNamespace(id=42, name="tester"), response=FakeResponse())
    asyncio.run(LayoutToggleButton("grouped", WEEK.isoformat()).callback(interaction))
    [edit] = interaction.response.edits
    assert "🟢 **Preferred (2)**" in edit["embed"].description
    assert get_layout(bot.conn, player.id) == "grouped"
    toggles = [c for c in edit["view"].children if isinstance(c, LayoutToggleButton)]
    assert [t.item.label for t in toggles] == ["Show full list"]
    rows = edit["view"].to_components()
    assert [len(r["components"]) for r in rows] == [4, 2]  # actions, then view toggles


def test_update_my_default_button_opens_default_editor(setup):
    from bot.views import AvailabilityEditor, ReminderButton, player_summary_view

    bot, player = setup
    labels = [c.item.label for c in player_summary_view(WEEK, "time", "list").children if hasattr(c, "item")]
    assert "Update my default" in labels  # on /cq availability

    class Response:
        async def send_message(self, **kwargs):
            self.kwargs = kwargs

    interaction = SimpleNamespace(client=bot, user=SimpleNamespace(id=42, name="tester"), response=Response())
    asyncio.run(ReminderButton("default", WEEK.isoformat()).callback(interaction))
    editor = interaction.response.kwargs["view"]
    assert isinstance(editor, AvailabilityEditor) and editor.mode == "default"
    assert editor.draft == db.get_default_availability(bot.conn, player.id)


def test_no_change_button_confirms_the_week(setup):
    from bot.views import ReminderButton, player_summary_view

    from bot import timeutil

    bot, player = setup
    week = timeutil.upcoming_week_start(timeutil.now_utc(), bot.tz)  # a week that hasn't finished
    labels = [c.item.label for c in player_summary_view(week, "time", "list").children if hasattr(c, "item")]
    assert "No change" in labels  # on /cq availability
    db.set_weekly_availability(bot.conn, player.id, week, {1: "Not Available"})

    class Response:
        async def send_message(self, *args, **kwargs):
            pass

    interaction = SimpleNamespace(client=bot, user=SimpleNamespace(id=42, name="tester"), response=Response())
    asyncio.run(ReminderButton("nochange", week.isoformat()).callback(interaction))
    assert db.get_confirmation(bot.conn, player.id, week) == "no_change"
    assert db.get_weekly_availability(bot.conn, player.id, week) == {}  # back to the default


def test_update_a_week_button_opens_week_picker(setup):
    from bot.views import ReminderButton, WeekPicker, player_summary_view

    bot, _ = setup
    labels = [c.item.label for c in player_summary_view(WEEK, "time", "list").children if hasattr(c, "item")]
    assert "Update a week" in labels  # on /cq availability

    class Response:
        async def send_message(self, **kwargs):
            self.kwargs = kwargs

    interaction = SimpleNamespace(client=bot, user=SimpleNamespace(id=42, name="tester"), response=Response())
    asyncio.run(ReminderButton("plan").callback(interaction))
    assert isinstance(interaction.response.kwargs["view"], WeekPicker)


def test_character_availability_button_and_picker(setup):
    from bot.views import AvailabilityEditor, CharacterPicker, ReminderButton, player_summary_view

    bot, player = setup
    db.add_character(bot.conn, player.id, "Main", "NL", "DPS", 4.0, "static")
    db.add_character(bot.conn, player.id, "Alt", "DRK", "HB", 3.0, "sub")
    alt = db.get_character(bot.conn, "Alt")
    db.set_character_overrides(bot.conn, alt["id"], {2: "Preferred"})

    view = player_summary_view(WEEK, "time", "list")
    row0 = [c["label"] for c in view.to_components()[0]["components"]]
    assert row0 == ["No change", "Update a week", "Update my default", "Character availability"]

    class Response:
        async def send_message(self, **kwargs):
            self.sent = kwargs

        async def edit_message(self, **kwargs):
            self.edited = kwargs

    interaction = SimpleNamespace(client=bot, user=SimpleNamespace(id=42, name="tester"), response=Response())
    asyncio.run(ReminderButton("chars", WEEK.isoformat()).callback(interaction))
    picker = interaction.response.sent["view"]
    assert isinstance(picker, CharacterPicker)
    options = {o.label: o.description for o in picker.children[0].options}
    assert options == {"Main (NL)": "Static · Follows your default",
                       "Alt (DRK)": "Sub · 1 squad(s) differ from your default"}

    pick = SimpleNamespace(data={"values": [str(alt["id"])]}, response=Response())
    asyncio.run(picker._pick(pick))
    editor = pick.response.edited["view"]
    assert isinstance(editor, AvailabilityEditor) and editor.mode == "character"
    assert editor.character["ign"] == "Alt" and editor.draft[2] == "Preferred"  # starts from default + override


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
