"""The reminder post (role ping) and the personal availability view behind its button (no network)."""

import asyncio
import dataclasses
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
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
        self.sent, self.edited, self.modal = None, None, None

    async def send_message(self, content=None, **kwargs):
        self.sent = {"content": content, **kwargs}

    async def send_modal(self, modal):
        self.modal = modal

    async def edit_message(self, **kwargs):
        self.edited = kwargs


class Followup:
    def __init__(self):
        self.sent = []

    async def send(self, content=None, **kwargs):
        self.sent.append({"content": content, **kwargs})


def click(bot, *, ephemeral_message=None, values=None):
    """A fake interaction from user 42; `ephemeral_message` = pressed on a private (True) or public message;
    `values` = the choice in a dropdown."""
    message = None if ephemeral_message is None else SimpleNamespace(flags=SimpleNamespace(ephemeral=ephemeral_message))
    return SimpleNamespace(
        client=bot, user=SimpleNamespace(id=42, name="tester"), message=message, response=Response(),
        followup=Followup(), data={"values": values or []},
    )


def tick(modal, preferred, available):
    """Tick these squad numbers in the availability pop-up (whichever group each squad is in)."""
    for level, numbers in (("Preferred", preferred), ("Available", available)):
        for group in modal.groups[level]:
            group._values = [o.value for o in group.options if int(o.value) in numbers]


def ticked_now(modal):
    """The squads ticked when the pop-up opens, per section."""
    return {
        level: {int(o.value) for g in groups for o in g.options if o.default} for level, groups in modal.groups.items()
    }


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


def test_change_default_button_opens_default_popup(setup):
    from bot.views import AvailabilityModal, ReminderButton, set_user_timezone

    bot, player = setup  # default: 1 Preferred, 2 Not Available, 3 Available, 4 Preferred
    set_user_timezone(bot.conn, 42, "America/New_York")
    interaction = click(bot, ephemeral_message=True)
    asyncio.run(ReminderButton("default", WEEK.isoformat()).callback(interaction))
    modal = interaction.response.modal
    assert isinstance(modal, AvailabilityModal) and modal.mode == "default"
    assert modal.title == "Your default availability"
    assert ticked_now(modal) == {"Preferred": {1, 4}, "Available": {3}}

    tick(modal, preferred={1}, available={2, 3})
    saved = click(bot, ephemeral_message=True)
    asyncio.run(modal.on_submit(saved))
    assert db.get_default_availability(bot.conn, player.id) == {
        1: "Preferred", 2: "Available", 3: "Available", 4: "Not Available"
    }
    assert db.get_weekly_availability(bot.conn, player.id, WEEK) == {}  # the week itself isn't changed
    assert saved.response.edited is not None  # /cq availability refreshes in place
    assert saved.followup.sent[0]["content"] == "✅ Saved your default availability."


def test_update_a_future_week_button_opens_week_picker(setup):
    from bot.views import ReminderButton, WeekPicker

    bot, _ = setup
    interaction = click(bot)
    asyncio.run(ReminderButton("plan").callback(interaction))
    assert isinstance(interaction.response.sent["view"], WeekPicker)


def test_change_a_characters_week(setup):
    from bot.squad_breakdown import build_breakdown
    from bot.views import AvailabilityModal, CharacterPicker, ReminderButton

    bot, player = setup  # default: 1 Preferred, 2 Not Available, 3 Available, 4 Preferred
    from bot.views import set_user_timezone

    set_user_timezone(bot.conn, 42, "America/New_York")
    db.add_character(bot.conn, player.id, "Main", "NL", 4.0, "static")
    db.add_character(bot.conn, player.id, "Alt", "DRK", 3.0, "sub")
    alt = db.get_character(bot.conn, "Alt")
    db.set_character_overrides(bot.conn, alt["id"], {2: "Preferred"})  # ongoing exception

    interaction = click(bot)
    asyncio.run(ReminderButton("charweek", WEEK.isoformat()).callback(interaction))
    picker = interaction.response.sent["view"]
    assert isinstance(picker, CharacterPicker) and picker.this_week_only
    assert {o.label: o.description for o in picker.children[0].options} == {
        "Main (NL)": "Static · No changes this week", "Alt (DRK)": "Flex · No changes this week"
    }

    pick = click(bot, ephemeral_message=True, values=[str(alt["id"])])
    asyncio.run(picker._pick(pick))
    modal = pick.response.modal
    assert isinstance(modal, AvailabilityModal) and modal.mode == "character_week"
    assert ticked_now(modal) == {"Preferred": {1, 2, 4}, "Available": {3}}  # its usual week
    assert modal.title.startswith("Alt: ") and modal.title.endswith("week only")
    squad_2 = next(o for o in modal.groups["Preferred"][0].options if o.value == "2")
    assert squad_2.description == "Usual this week: 🟢 Preferred"  # its ongoing exception

    tick(modal, preferred={1, 2}, available={3})  # can't bring Alt to squad 4 this week
    saved = click(bot, ephemeral_message=True)
    asyncio.run(modal.on_submit(saved))
    assert saved.response.edited["content"].startswith("✅ Changed 1 squad(s) for Alt")
    assert isinstance(saved.response.edited["view"], CharacterPicker)  # the picker refreshes
    alt_option = next(o for o in saved.response.edited["view"].children[0].options if o.label == "Alt (DRK)")
    assert alt_option.description == "Flex · 1 squad(s) changed this week"

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
    from bot.views import CharacterPicker, ReminderButton, set_user_timezone

    bot, player = setup
    set_user_timezone(bot.conn, 42, "America/New_York")
    db.add_character(bot.conn, player.id, "Alt", "DRK", 3.0, "sub")
    alt = db.get_character(bot.conn, "Alt")
    db.set_character_overrides(bot.conn, alt["id"], {2: "Preferred"})
    interaction = click(bot)
    asyncio.run(ReminderButton("chars", WEEK.isoformat()).callback(interaction))
    picker = interaction.response.sent["view"]
    assert isinstance(picker, CharacterPicker) and not picker.this_week_only
    assert picker.children[0].options[0].description == "Flex · 1 squad(s) differ from your default"
    pick = click(bot, ephemeral_message=True, values=[str(alt["id"])])
    asyncio.run(picker._pick(pick))
    modal = pick.response.modal
    assert modal.mode == "character" and modal.title == "Alt: ongoing schedule"
    squad_2 = next(o for o in modal.groups["Preferred"][0].options if o.value == "2")
    assert squad_2.description == "Your default: 🔴 Not Available" and squad_2.default  # Alt's exception

    tick(modal, preferred={1, 4}, available={3})  # Alt now follows the default again
    saved = click(bot, ephemeral_message=True)
    asyncio.run(modal.on_submit(saved))
    assert db.get_character_overrides(bot.conn, alt["id"]) == {}
    assert saved.response.edited["content"] == "✅ Alt now follows your default schedule."


def test_cq_characters_has_character_availability_button(setup):
    from bot.cogs.player import CqCog
    from bot.views import CharacterPicker, ReminderButton

    bot, player = setup
    db.add_character(bot.conn, player.id, "Main", "NL", 4.0, "static")

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
    db.add_character(bot.conn, player.id, "Main", "NL", 4.0, "static")
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


def pick(select, value, bot):
    """Choose `value` in a dropdown, as user 42."""
    select._values = [value]
    interaction = click(bot, ephemeral_message=True)
    asyncio.run(select.callback(interaction))
    return interaction


def press_update(bot, week=WEEK):
    """Press ✏️ Change weekly availability."""
    from bot.views import ReminderButton

    interaction = click(bot, ephemeral_message=True)
    asyncio.run(ReminderButton("update", week.isoformat()).callback(interaction))
    return interaction


def weekly_popup(bot, zone="America/New_York"):
    from bot.views import set_user_timezone

    set_user_timezone(bot.conn, 42, zone)
    return press_update(bot).response.modal


def checkbox_groups(modal):
    labels = [
        c for c in modal.children
        if isinstance(c, discord.ui.Label) and isinstance(c.component, discord.ui.CheckboxGroup)
    ]
    return [(label.text, [(o.label, o.default) for o in label.component.options]) for label in labels]


def test_weekly_popup_shows_local_times_and_ticks_current_levels(setup):
    bot, player = setup  # default: 1 Preferred, 2 Not Available, 3 Available, 4 Preferred
    modal = weekly_popup(bot, "Australia/Sydney")
    assert modal.title == timeutil.week_label(WEEK, capital=True)
    first = modal.children[0]  # "can't make any runs" comes first, unticked
    assert isinstance(first.component, discord.ui.Checkbox) and not first.component.default
    preferred_label = next(c for c in modal.children if c.text == "🟢 Preferred")
    assert "Times in NSW / Victoria (Sydney)" in preferred_label.description

    squads = modal.squads
    sydney = ZoneInfo("Australia/Sydney")
    expected = [(f"Squad {s.number} · {timeutil.run_label(s.starts_at, sydney)}", s.number in (1, 4)) for s in squads]
    assert checkbox_groups(modal)[0] == ("🟢 Preferred", expected)  # one group per level with 4 runs
    assert [ticked for _, ticked in checkbox_groups(modal)[1][1]] == [s.number == 3 for s in squads]

    # the same run shows in each player's own timezone
    la = weekly_popup(bot, "America/Los_Angeles")
    assert checkbox_groups(la)[0][1][0][0] != expected[0][0]
    assert checkbox_groups(la)[0][1][0][0].endswith(timeutil.run_label(squads[0].starts_at, ZoneInfo("America/Los_Angeles")))


def test_run_label_format():
    unix = int(datetime(2026, 10, 10, 1, 0, tzinfo=timezone.utc).timestamp())
    assert timeutil.run_label(unix, ZoneInfo("America/New_York")) == "Fri Oct 9, 9:00 PM"
    assert timeutil.run_label(unix, ZoneInfo("Australia/Sydney")) == "Sat Oct 10, 12:00 PM"


def test_popup_splits_16_runs_into_two_groups_per_level(setup):
    bot, _ = setup
    for number in range(5, 17):
        db.set_squad_template(bot.conn, number, number % 7, "10:00")
    modal = weekly_popup(bot)
    assert len(modal.children) == 5  # Discord's limit: the "can't make any" checkbox + 4 checkbox groups
    assert [(text, len(options)) for text, options in checkbox_groups(modal)] == [
        ("🟢 Preferred (1 of 2)", 8), ("🟢 Preferred (2 of 2)", 8),
        ("🟡 Available (1 of 2)", 8), ("🟡 Available (2 of 2)", 8),
    ]


def test_popup_asks_for_timezone_the_first_time(setup):
    from bot.views import AvailabilityModal, TimezoneSelect, get_user_timezone

    bot, _ = setup
    interaction = press_update(bot)
    assert interaction.response.modal is None
    assert "Pick your timezone first" in interaction.response.sent["content"]
    [select] = interaction.response.sent["view"].children
    assert isinstance(select, TimezoneSelect)

    picked = pick(select, "Europe/London", bot)
    assert get_user_timezone(bot.conn, 42) == ZoneInfo("Europe/London")
    assert isinstance(picked.response.modal, AvailabilityModal)  # opens straight away
    assert press_update(bot).response.modal is not None  # and isn't asked again


def test_too_many_runs_falls_back_to_dropdown_editor_and_finished_week_is_refused(setup):
    from bot.views import POPUP_MAX_SQUADS, AvailabilityEditor, set_user_timezone

    bot, _ = setup
    set_user_timezone(bot.conn, 42, "America/New_York")
    for number in range(5, POPUP_MAX_SQUADS + 2):
        db.set_squad_template(bot.conn, number, number % 7, "10:00")
    interaction = press_update(bot)
    assert interaction.response.modal is None
    editor = interaction.response.sent["view"]
    assert isinstance(editor, AvailabilityEditor) and editor.mode == "weekly"

    interaction = press_update(bot, WEEK - timedelta(days=7))
    assert interaction.response.modal is None and "already finished" in interaction.response.sent["content"]


def test_weekly_popup_submit_saves_the_week(setup):
    from bot.views import ticked_levels

    bot, player = setup
    modal = weekly_popup(bot)
    preferred, available = modal.groups["Preferred"][0], modal.groups["Available"][0]
    preferred._values, available._values = ["2", "3"], ["3", "4"]  # 3 ticked in both: Preferred wins

    interaction = click(bot, ephemeral_message=True)
    asyncio.run(modal.on_submit(interaction))
    assert db.get_weekly_availability(bot.conn, player.id, WEEK) == {
        1: "Not Available", 2: "Preferred", 3: "Preferred", 4: "Available"
    }
    assert db.get_confirmation(bot.conn, player.id, WEEK) == "updated"  # counts as checking in
    assert interaction.response.edited is not None  # the /cq availability message refreshes in place
    [note] = interaction.followup.sent
    assert note["ephemeral"] and note["content"].startswith("✅ Saved your availability for the week of")
    assert "Squad(s) 3 were ticked in both" in note["content"]

    squads = modal.squads
    assert ticked_levels(squads, set(), set()) == {s.number: "Not Available" for s in squads}


def test_settings_timezone_and_squad_order(setup):
    from bot.views import SettingsView, get_squad_order, get_user_timezone

    bot, _ = setup
    view = SettingsView(bot, 42)
    timezone_field, order_field = view.embed().fields
    assert timezone_field.value.startswith("Not set") and order_field.value.startswith("**By time**")

    select, sort_btn = view.children
    interaction = pick(select, "Asia/Tokyo", bot)
    assert get_user_timezone(bot.conn, 42) == ZoneInfo("Asia/Tokyo")
    shown = interaction.response.edited["embed"].fields[0].value
    assert shown.startswith("**Japan / Korea (JST)**")
    assert [o.default for o in view.children[0].options].count(True) == 1  # the new choice is selected

    interaction = click(bot, ephemeral_message=True)
    asyncio.run(view.children[1].callback(interaction))
    assert get_squad_order(bot.conn, 42) == "number"  # the same choice every squad list uses
    assert interaction.response.edited["embed"].fields[1].value.startswith("**By squad number**")
    assert view.children[1].label == "Sort by time"


def test_timezone_choices_fit_a_dropdown():
    from bot.views import TIMEZONE_CHOICES

    assert len(TIMEZONE_CHOICES) <= 25
    for label, zone in TIMEZONE_CHOICES:
        ZoneInfo(zone)
        assert len(label) <= 100


def test_popup_titles_fit_discords_limit():
    from bot.views import TITLE_MAX, AvailabilityModal

    long_name = {"ign": "AVeryLongCharacterNameThatGoesOnAndOn"}
    for mode in ("weekly", "default", "character", "character_week"):
        title = AvailabilityModal._title(mode, WEEK, long_name)
        assert len(title) <= TITLE_MAX
    assert AvailabilityModal._title("character", WEEK, long_name).endswith("…")


def test_cant_make_any_runs_checkbox_saves_everything_as_not_available(setup):
    bot, player = setup
    modal = weekly_popup(bot)
    tick(modal, preferred={1, 4}, available={3})  # whatever is ticked...
    modal.none._value = True  # ...is ignored when "can't make any" is ticked
    saved = click(bot, ephemeral_message=True)
    asyncio.run(modal.on_submit(saved))
    assert db.get_weekly_availability(bot.conn, player.id, WEEK) == {n: "Not Available" for n in (1, 2, 3, 4)}
    assert db.get_confirmation(bot.conn, player.id, WEEK) == "updated"  # still counts as checking in
    assert saved.followup.sent[0]["content"].endswith("Every run is 🔴 Not Available.")
