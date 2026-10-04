"""/cq_host: the host tools panel and its pop-ups (no network)."""

import asyncio
import dataclasses
from types import SimpleNamespace

import discord
import pytest

from bot import db, timeutil
from bot.app import MonkeyBot
from bot.cogs.host import DamageHistoryModal, HostCog, HostPanel, PlayerAvailabilityModal
from bot.config import Config


class Response:
    def __init__(self):
        self.sent, self.edited, self.modal, self.deferred = None, None, None, False

    async def send_message(self, content=None, **kwargs):
        self.sent = {"content": content, **kwargs}

    async def edit_message(self, **kwargs):
        self.edited = kwargs

    async def send_modal(self, modal):
        self.modal = modal

    async def defer(self, **kwargs):
        self.deferred = True


class Followup:
    def __init__(self):
        self.sent = []

    async def send(self, content=None, **kwargs):
        self.sent.append({"content": content, **kwargs})


class Channel(discord.abc.Messageable):
    mention = "<#123>"

    def __init__(self):
        self.posts = []

    async def send(self, **kwargs):
        self.posts.append(kwargs)
        return SimpleNamespace(jump_url="https://discord.test/reminder")


def interaction(**extra):
    return SimpleNamespace(
        response=Response(), followup=Followup(), user=SimpleNamespace(id=555), channel=Channel(), **extra
    )


def run(callback, **extra):
    i = interaction(**extra)
    asyncio.run(callback(i))
    return i


def type_in(field, text):
    field._value = text


@pytest.fixture
def bot(tmp_path):
    bot = MonkeyBot(dataclasses.replace(Config.from_env(), database_path=tmp_path / "host.db", cq_channel_id=None))
    db.set_squad_template(bot.conn, 1, 0, "12:00")
    robin = db.create_player(bot.conn, name="Robin", discord_handle="@robin")
    db.add_character(bot.conn, robin.id, "Main", "NL", 4.0, "static")
    yield bot
    bot.conn.close()


@pytest.fixture
def panel(bot):
    return HostPanel(HostCog(bot))


def button(panel, label):
    return next(c for c in panel.children if isinstance(c, discord.ui.Button) and c.label == label)


def test_cq_host_shows_week_dropdown_and_a_button_per_tool(bot):
    sent = run(lambda i: HostCog.cq_host.callback(HostCog(bot), i)).response.sent
    view = sent["view"]
    assert isinstance(view.children[0], discord.ui.Select)
    assert [c.label for c in view.children[1:]] == [
        "Status", "Availability", "Prep Roster", "Player availability", "Damage history", "Post reminder now",
    ]
    upcoming = timeutil.upcoming_week_start(timeutil.now_utc(), bot.tz)
    assert sent["embed"].description.startswith(f"Week: **{timeutil.week_label(upcoming)}**")


def test_week_dropdown_changes_the_week_used(bot, panel):
    tz = bot.tz
    assert panel.week_start() == timeutil.upcoming_week_start(timeutil.now_utc(), tz)
    i = run(panel.children[0].callback, data={"values": ["current"]})
    assert panel.week_start() == timeutil.current_week_start(timeutil.now_utc(), tz)
    assert [o.default for o in i.response.edited["view"].children[0].options] == [False, True]

    status = run(button(panel, "Status").callback).response.sent
    assert status["embed"].title == f"Availability status: {timeutil.week_label(panel.week_start())}"


def test_availability_button(panel):
    assert run(button(panel, "Availability").callback).response.sent["embed"].title.startswith("Availability:")


def test_player_availability_popup(panel):
    modal = run(button(panel, "Player availability").callback).response.modal
    assert isinstance(modal, PlayerAvailabilityModal)
    type_in(modal.player, "nobody")
    assert "No single player matches" in run(modal.on_submit).response.sent["content"]
    type_in(modal.player, "robin")
    assert run(modal.on_submit).response.sent["embed"].title.startswith("Robin (@robin):")


def test_damage_history_popup(panel):
    modal = run(button(panel, "Damage history").callback).response.modal
    assert isinstance(modal, DamageHistoryModal)
    type_in(modal.character, "main")
    assert run(modal.on_submit).response.sent["embed"].title.startswith("Main (NL)")
    type_in(modal.character, "nobody")
    assert "No character named **nobody**" in run(modal.on_submit).response.sent["content"]


def test_post_reminder_asks_first(panel):
    ask = run(button(panel, "Post reminder now").callback)
    assert "Post the reminder for the" in ask.response.sent["content"]
    [post] = ask.response.sent["view"].children
    assert post.label == "Post now"

    i = run(post.callback)
    assert len(i.channel.posts) == 1  # posted only after confirming
    assert i.response.sent["content"] == "📣 Posted: https://discord.test/reminder"


def test_prep_roster_button(bot, panel):
    from bot.squad_breakdown import PrepRosterView

    sent = run(button(panel, "Prep Roster").callback).response.sent
    view = sent["view"]
    assert isinstance(view, PrepRosterView) and view.week == panel.week_start()
    assert sent["embed"].title.startswith("Squad 1: 🟢 Preferred")  # opens on the first squad
    assert [o.value for o in view.children[0].options] == ["1"]
    assert [c.label for c in view.children[1:]] == ["Preferred", "Available", "Group by player"]

    db.delete_squad(bot.conn, 1)
    empty = run(button(panel, "Prep Roster").callback).response.sent
    assert empty["embed"].description == "*No squads configured.*" and not empty["view"].children
