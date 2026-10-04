"""/cq_config: the settings panel and its pop-ups (no network)."""

import asyncio
import dataclasses
from types import SimpleNamespace

import discord
import pytest

from bot import db, reminders
from bot.app import MonkeyBot
from bot.cogs.host import (
    ConfigCog,
    ConfigPanel,
    DamageLogsModal,
    ImportRosterModal,
    RemindersModal,
    RemoveSquadsModal,
    SquadTimeModal,
)
from bot.config import Config


class Response:
    def __init__(self):
        self.sent, self.edited, self.modal, self.deferred = None, None, None, False

    async def defer(self, **kwargs):
        self.deferred = True

    async def send_message(self, content=None, **kwargs):
        self.sent = {"content": content, **kwargs}

    async def edit_message(self, **kwargs):
        self.edited = kwargs

    async def send_modal(self, modal):
        self.modal = modal


class Followup:
    def __init__(self):
        self.sent = []

    async def send(self, content=None, **kwargs):
        self.sent.append({"content": content, **kwargs})


def interaction():
    return SimpleNamespace(response=Response(), followup=Followup(), user=SimpleNamespace(id=555))


@pytest.fixture
def bot(tmp_path):
    bot = MonkeyBot(dataclasses.replace(Config.from_env(), database_path=tmp_path / "config.db", cq_channel_id=None))
    for number, weekday, hhmm in [(1, 0, "12:00"), (2, 5, "21:00")]:
        db.set_squad_template(bot.conn, number, weekday, hhmm)
    yield bot
    bot.conn.close()


def press(panel, label):
    button = next(b for b in panel.children if isinstance(b, discord.ui.Button) and b.label == label)
    i = interaction()
    asyncio.run(button.callback(i))
    return i


def submit(modal):
    i = interaction()
    asyncio.run(modal.on_submit(i))
    return i


def test_cq_config_shows_settings_and_a_button_per_setting(bot):
    i = interaction()
    asyncio.run(ConfigCog.cq_config.callback(ConfigCog(bot), i))
    sent = i.response.sent
    assert sent["ephemeral"]
    assert [e.title for e in sent["embeds"]][:2] == ["⏰ Reminders", "📊 Damage logs"]
    assert sent["embeds"][2].title.startswith("Squad times")
    assert [c.label for c in sent["view"].children] == [
        "Reminders", "Damage logs", "Squad time", "Remove squads", "Turn automatic reminders off", "Import roster"
    ]
    for label, modal in [("Reminders", RemindersModal), ("Damage logs", DamageLogsModal),
                         ("Squad time", SquadTimeModal), ("Remove squads", RemoveSquadsModal),
                         ("Import roster", ImportRosterModal)]:
        assert isinstance(press(sent["view"], label).response.modal, modal)


def test_reminders_modal_starts_from_current_settings_and_saves(bot):
    panel = ConfigPanel(bot, 555)
    modal = press(panel, "Reminders").response.modal
    assert {o.value for o in modal.days.options if o.default} == {"3", "5"}  # Wed, Fri
    assert modal.remind_time.default == "18:00" and modal.deadline_time.default == "12:00"
    assert [o.value for o in modal.deadline_day.options if o.default] == ["6"]  # Saturday
    assert len(modal.children) == 5  # Discord's limit for a pop-up

    modal.channel._values = [SimpleNamespace(id=777)]
    modal.days._values = ["1", "4"]
    modal.remind_time._value = "19:30"
    modal.deadline_day._values = ["5"]
    modal.deadline_time._value = "9:00"
    i = submit(modal)
    settings = reminders.load(bot.conn, None)
    assert (settings.channel_id, settings.reminder_weekdays, settings.reminder_time) == (777, [1, 4], "19:30")
    assert (settings.deadline_weekday, settings.deadline_time) == (5, "09:00")
    assert i.response.edited["content"] == "✅ Saved the reminder settings."  # the panel re-renders
    assert "Monday and Thursday at 19:30" in i.response.edited["embeds"][0].description


def test_reminders_modal_rejects_bad_times(bot):
    modal = RemindersModal(ConfigPanel(bot, 555))
    modal.days._values = ["3"]
    modal.remind_time._value = "6pm"
    modal.deadline_day._values = ["6"]
    modal.deadline_time._value = "12:00"
    i = submit(modal)
    assert i.response.sent["content"].startswith("Nothing saved") and i.response.edited is None
    assert reminders.load(bot.conn, None).reminder_time == "18:00"


def test_toggle_automatic_reminders(bot):
    panel = ConfigPanel(bot, 555)
    i = press(panel, "Turn automatic reminders off")
    assert not reminders.load(bot.conn, None).enabled
    assert i.response.edited["content"] == "🔕 Automatic reminders are off."
    i = press(panel, "Turn automatic reminders on")
    assert reminders.load(bot.conn, None).enabled


def test_damage_logs_modal(bot):
    from bot import damage

    modal = DamageLogsModal(ConfigPanel(bot, 555))
    assert modal.runs.default == "3"
    modal.runs._value = "5"
    modal.channel._values = []  # left empty: keep the current channel
    i = submit(modal)
    assert damage.average_runs(bot.conn) == 5 and db.get_boss_setting(bot.conn, "queen_logs_channel_id") is None
    assert i.response.edited["content"] == "✅ Saved the damage log settings."

    modal = DamageLogsModal(ConfigPanel(bot, 555))
    modal.runs._value = "99"
    assert submit(modal).response.sent["content"].startswith("Nothing saved")


def test_squad_time_modal_lists_squads_and_adds_a_new_one(bot):
    modal = SquadTimeModal(ConfigPanel(bot, 555))
    labels = [o.label for o in modal.squad.options]
    assert labels == ["Squad 1", "Squad 2", "New squad 3"]
    assert modal.action.value is None and [o.default for o in modal.action.options] == [True, False, False]

    modal.squad._values = ["3"]
    modal.day._values = ["2"]
    modal.time._value = "20:00"
    modal.action._value = "permanent"
    i = submit(modal)
    assert 3 in db.squad_numbers(bot.conn)
    assert i.response.edited["content"].startswith("✅ Squad 3 is now Tuesday 20:00")

    modal = SquadTimeModal(ConfigPanel(bot, 555))
    modal.squad._values = ["4"]  # a new squad, but only for next week: refused
    modal.day._values = ["2"]
    modal.time._value = "20:00"
    modal.action._value = "week"
    assert "Choose Every week to add it" in submit(modal).response.sent["content"]


def test_remove_squads_modal_removes_several(bot):
    modal = RemoveSquadsModal(ConfigPanel(bot, 555))
    assert [o.label for o in modal.squads.options] == ["Squad 1", "Squad 2"] and modal.squads.max_values == 2
    modal.squads._values = ["1", "2"]
    i = submit(modal)
    assert db.squad_numbers(bot.conn) == []
    assert i.response.edited["content"] == "🗑️ Removed Squad 1, Squad 2 from the schedule."
    # with no squads left, the button says so instead of opening an empty pop-up
    assert press(ConfigPanel(bot, 555), "Remove squads").response.sent["content"] == "There are no squads to remove."


def test_import_roster_popup(bot):
    modal = press(ConfigPanel(bot, 555), "Import roster").response.modal
    assert isinstance(modal, ImportRosterModal)

    csv = "IGN,Name,Discord,Job\nFresh,Newbie,@newbie,BSP\n"

    async def read():
        return csv.encode()

    modal.file._values = [SimpleNamespace(filename="roster.csv", size=len(csv), read=read)]
    i = submit(modal)
    assert i.response.deferred and i.followup.sent[0]["content"].startswith("✅ Import complete")
    assert db.get_character(bot.conn, "Fresh")["buff"] == "HSH"  # from the job

    modal.file._values = [SimpleNamespace(filename="roster.txt", size=10, read=read)]
    assert "upload the roster as a .csv" in submit(modal).response.sent["content"]


def test_squad_times_mark_squads_moved_for_next_week(bot):
    from bot.cogs.host import change_squad_time

    change_squad_time(bot, 2, "week", 3, "20:00")
    squad_times = ConfigPanel(bot, 555).embeds()[2].description
    assert "**Squad 2**" in squad_times and "✏️ moved for this week" in squad_times
    assert squad_times.count("✏️") == 1  # squad 1 wasn't moved
