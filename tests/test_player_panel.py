"""/player: the Add / Change / Link panel and its pop-ups (no network)."""

import asyncio
import dataclasses
from types import SimpleNamespace

import pytest

from bot import db
from bot.app import MonkeyBot
from bot.cogs.host import (
    AddPlayerModal,
    EditPlayerModal,
    FindPlayerModal,
    LinkPlayerModal,
    PlayerAdminCog,
    PlayerEditView,
)
from bot.config import Config


class Response:
    def __init__(self):
        self.sent, self.edited, self.modal = None, None, None

    async def send_message(self, content=None, **kwargs):
        self.sent = {"content": content, **kwargs}

    async def edit_message(self, **kwargs):
        self.edited = kwargs

    async def send_modal(self, modal):
        self.modal = modal


def run(callback):
    i = SimpleNamespace(response=Response(), user=SimpleNamespace(id=555))
    asyncio.run(callback(i))
    return i


def type_in(field, text):
    field._value = text


def member(id_, name):
    return SimpleNamespace(id=id_, name=name, mention=f"<@{id_}>")


@pytest.fixture
def bot(tmp_path):
    bot = MonkeyBot(dataclasses.replace(Config.from_env(), database_path=tmp_path / "players.db"))
    robin = db.create_player(bot.conn, name="Robin", discord_handle="@robin")
    db.add_character(bot.conn, robin.id, "Main", "NL", 4.0, "static")
    yield bot
    bot.conn.close()


def command(bot, change=None):
    cog = PlayerAdminCog(bot)
    return run(lambda i: PlayerAdminCog.player.callback(cog, i, change=change))


def test_player_command_shows_add_change_link(bot):
    view = command(bot).response.sent["view"]
    assert [b.label for b in view.children] == ["Add", "Change", "Link"]
    for button, modal in zip(view.children, (AddPlayerModal, FindPlayerModal, LinkPlayerModal)):
        assert isinstance(run(button.callback).response.modal, modal)


def test_add_player_popup(bot):
    modal = AddPlayerModal(bot)
    assert [label.text for label in modal.children] == ["Name", "Discord account", "Discord username", "Status"]
    assert [o.value for o in modal.status.options if o.default] == ["active"]

    type_in(modal.name, "Newbie")
    modal.member._values = [member(99, "newbie")]
    type_in(modal.username, "")
    modal.status._values = ["inactive"]
    i = run(modal.on_submit)
    assert i.response.sent["content"].startswith("✅ Added **Newbie (@newbie)** (inactive)")
    assert db.get_player_by_discord_id(bot.conn, 99).name == "Newbie"

    type_in(modal.name, "Again")  # same Discord account: refused
    assert "already on the roster" in run(modal.on_submit).response.sent["content"]


def test_add_player_not_in_the_server_yet(bot):
    modal = AddPlayerModal(bot)
    type_in(modal.name, "Later")
    modal.member._values = []
    type_in(modal.username, "@futureplayer")
    modal.status._values = ["active"]
    run(modal.on_submit)
    later = db.get_player_by_handle(bot.conn, "@futureplayer")
    assert later.name == "Later" and later.discord_id is None


def test_change_finds_the_player_then_edits_them(bot):
    find = FindPlayerModal(bot)
    type_in(find.player, "Main")  # by one of their characters
    card = run(find.on_submit).response.sent
    assert card["embed"].title == "Robin" and "Main" in card["embed"].description
    assert isinstance(card["view"], PlayerEditView)

    modal = run(card["view"].children[0].callback).response.modal
    assert isinstance(modal, EditPlayerModal) and modal.name.default == "Robin"
    assert [o.value for o in modal.status.options if o.default] == ["active"]

    type_in(modal.name, "Robin B")
    modal.status._values = ["inactive"]
    i = run(modal.on_submit)
    robin = db.search_players(bot.conn, "Robin B")[0]
    assert (robin.name, robin.status) == ("Robin B", "inactive")
    assert i.response.edited["content"] == "✅ **Robin (@robin)**: renamed to **Robin B**, status **Inactive**."
    assert i.response.edited["embed"].title == "Robin B"  # the card refreshes


def test_player_change_option_opens_the_edit_popup_directly(bot):
    assert "No single player matches **nobody**" in command(bot, "nobody").response.sent["content"]

    robin = db.search_players(bot.conn, "Robin")[0]
    i = command(bot, str(robin.id))  # a picked suggestion's value is the player id
    modal = i.response.modal
    assert isinstance(modal, EditPlayerModal) and not modal.from_card

    type_in(modal.name, "Robin")
    modal.status._values = ["active"]
    saved = run(modal.on_submit)
    assert saved.response.sent["content"] == "Nothing changed for **Robin (@robin)**."
    assert saved.response.edited is None and saved.response.sent["ephemeral"]


def test_link_popup(bot):
    modal = LinkPlayerModal(bot)
    type_in(modal.player, "robin")
    modal.member._values = [member(42, "robin_discord")]
    i = run(modal.on_submit)
    assert i.response.sent["content"] == "🔗 Linked **Robin** to <@42>."
    assert db.get_player_by_discord_id(bot.conn, 42).name == "Robin"

    type_in(modal.player, "nobody")
    assert run(modal.on_submit).response.sent["content"].startswith("Nothing saved: No single player matches")
