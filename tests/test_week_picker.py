"""Submitting availability for future weeks through the week picker (no network)."""

import asyncio
import dataclasses
from datetime import timedelta
from types import SimpleNamespace

import pytest

from bot import db, timeutil
from bot.app import MonkeyBot
from bot.config import Config
from bot.views import ADVANCE_WEEKS, WeekPicker, player_summary_embed


class FakeResponse:
    def __init__(self):
        self.edits, self.modal = [], None

    async def edit_message(self, **kwargs):
        self.edits.append(kwargs)

    async def send_modal(self, modal):
        self.modal = modal


def choose(value: str, bot=None):
    """A dropdown choice from user 42."""
    return SimpleNamespace(
        data={"values": [value]}, response=FakeResponse(), client=bot, user=SimpleNamespace(id=42, name="tester")
    )


@pytest.fixture
def setup(tmp_path):
    bot = MonkeyBot(dataclasses.replace(Config.from_env(), database_path=tmp_path / "picker.db"))
    for number, weekday, hhmm in [(1, 0, "12:00"), (2, 3, "21:00")]:
        db.set_squad_template(bot.conn, number, weekday, hhmm)
    player = db.create_player(bot.conn, name="Tester", discord_handle="@tester", discord_id=42)
    db.set_default_availability(bot.conn, player.id, {1: "Preferred", 2: "Available"})
    next_week = timeutil.upcoming_week_start(timeutil.now_utc(), bot.tz)
    yield bot, player, next_week
    bot.conn.close()


def test_picker_offers_next_week_and_later_weeks(setup):
    bot, player, next_week = setup
    picker = WeekPicker(bot, player)
    [select] = picker.children  # no reset dropdown until a week has changes
    values = [o.value for o in select.options]
    assert len(values) == ADVANCE_WEEKS
    assert values[0] == next_week.isoformat()
    assert values[3] == (next_week + timedelta(weeks=3)).isoformat()
    assert select.options[0].description == "Next week · Default schedule"
    assert select.options[2].description == "In 3 weeks · Default schedule"


def test_pick_future_week_opens_popup_and_saves_that_week(setup):
    from bot.views import AvailabilityModal, set_user_timezone

    bot, player, next_week = setup
    set_user_timezone(bot.conn, 42, "America/New_York")
    target = next_week + timedelta(weeks=3)
    picker = WeekPicker(bot, player)
    interaction = choose(target.isoformat(), bot)
    asyncio.run(picker._pick(interaction))

    modal = interaction.response.modal
    assert isinstance(modal, AvailabilityModal) and modal.week == target
    assert modal.title == timeutil.week_label(target, capital=True)
    defaults = {lvl: {o.value for g in gs for o in g.options if o.default} for lvl, gs in modal.groups.items()}
    assert defaults == {"Preferred": {"1"}, "Available": {"2"}}  # starts from the default

    modal.groups["Preferred"][0]._values = ["1"]  # squad 2 unticked: Not Available
    saved = choose(target.isoformat(), bot)
    asyncio.run(modal.on_submit(saved))
    assert db.get_weekly_availability(bot.conn, player.id, target) == {1: "Preferred", 2: "Not Available"}
    assert db.get_weekly_availability(bot.conn, player.id, next_week) == {}  # other weeks untouched
    assert db.get_confirmation(bot.conn, player.id, target) == "updated"
    [edit] = saved.response.edits  # the week picker refreshes, with the week now marked changed
    assert edit["view"] is picker and edit["content"].startswith("✅ Saved your availability")
    assert target in picker.changed_weeks()


def test_changed_weeks_are_marked_and_can_be_reset(setup):
    bot, player, next_week = setup
    later = next_week + timedelta(weeks=2)
    db.set_weekly_availability(bot.conn, player.id, later, {1: "Not Available", 2: "Not Available"})

    picker = WeekPicker(bot, player)
    pick, reset = picker.children
    assert pick.options[2].description == "In 3 weeks · Changed"
    assert [o.value for o in reset.options] == [later.isoformat()]

    summary = player_summary_embed(bot, player, next_week)
    assert any(f.name == "Changes submitted for later weeks" for f in summary.fields)

    interaction = choose(later.isoformat())
    asyncio.run(picker._reset(interaction))
    assert db.get_weekly_availability(bot.conn, player.id, later) == {}
    assert len(picker.children) == 1  # reset dropdown disappears once nothing is changed
    assert "now uses your default schedule" in interaction.response.edits[0]["content"]
