"""/character: the Add / Change / Remove panel and its pop-ups (no network)."""

import asyncio
import dataclasses
from types import SimpleNamespace

import discord
import pytest

from bot import db
from bot.app import MonkeyBot
from bot.cogs.host import (
    AddCharacterModal,
    CharacterAdminCog,
    CharacterEditView,
    EditCharacterModal,
    FindCharacterModal,
    FindPlayerToRemoveModal,
    RemoveCharactersModal,
    RemoveCharactersView,
    find_player,
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


def interaction():
    return SimpleNamespace(response=Response(), user=SimpleNamespace(id=555))


def run(callback):
    i = interaction()
    asyncio.run(callback(i))
    return i


def type_in(field, text):
    field._value = text


@pytest.fixture
def bot(tmp_path):
    bot = MonkeyBot(dataclasses.replace(Config.from_env(), database_path=tmp_path / "chars.db"))
    robin = db.create_player(bot.conn, name="Robin", discord_handle="@robin")
    db.create_player(bot.conn, name="Robinson", discord_handle="@robinson")
    db.add_character(bot.conn, robin.id, "Main", "NL", 4.0, "static")
    db.add_character(bot.conn, robin.id, "Alt", "DRK", 3.0, "sub")
    yield bot
    bot.conn.close()


def test_character_command_shows_add_change_remove(bot):
    i = run(lambda i: CharacterAdminCog.character.callback(CharacterAdminCog(bot), i))
    view = i.response.sent["view"]
    assert [b.label for b in view.children] == ["Add", "Change", "Remove"]
    for button, modal in zip(view.children, (AddCharacterModal, FindCharacterModal, FindPlayerToRemoveModal)):
        assert isinstance(run(button.callback).response.modal, modal)


def test_find_player_prefers_an_exact_name_and_suggests_otherwise(bot):
    assert find_player(bot.conn, "robin").name == "Robin"  # not Robinson
    assert find_player(bot.conn, "@robinson").name == "Robinson"
    assert find_player(bot.conn, "Alt").name == "Robin"  # by one of their characters
    with pytest.raises(ValueError, match="Did you mean: Robin"):
        find_player(bot.conn, "rob")


def test_add_character_popup(bot):
    modal = AddCharacterModal(bot)
    assert [label.text for label in modal.children] == ["Player", "In-game name", "Job", "Damage", "Status"]
    # every known job, each showing the buff it gets
    assert [o.value for o in modal.job.options] == sorted(db.JOB_BUFFS)
    assert next(o.description for o in modal.job.options if o.value == "BSP") == "Buff: HSH"
    assert [o.value for o in modal.status.options if o.default] == ["static"]

    type_in(modal.player, "Robinson")
    type_in(modal.ign, "Newbie")
    modal.job._values = ["DRK"]
    type_in(modal.dmg, "2.5")
    modal.status._values = ["sub"]
    i = run(modal.on_submit)
    assert i.response.sent["content"] == "✅ Added **Newbie** (DRK, HB) to **Robinson (@robinson)**, as ⏳ Flex."
    char = db.get_character(bot.conn, "Newbie")
    assert (char["job"], char["buff"], char["dmg"], char["status"]) == ("DRK", "HB", 2.5, "sub")

    type_in(modal.dmg, "lots")
    type_in(modal.ign, "Another")
    assert run(modal.on_submit).response.sent["content"] == "Nothing saved: Damage must be a number, e.g. `4.2`."
    assert db.get_character(bot.conn, "Another") is None


def test_change_finds_the_character_then_edits_it(bot):
    find = FindCharacterModal(bot)
    type_in(find.ign, "nobody")
    assert "No character named **nobody**" in run(find.on_submit).response.sent["content"]

    type_in(find.ign, "alt")  # matched ignoring capitals
    card = run(find.on_submit).response.sent
    assert card["embed"].title == "Alt (DRK)" and isinstance(card["view"], CharacterEditView)

    modal = run(card["view"].children[0].callback).response.modal
    assert isinstance(modal, EditCharacterModal)
    assert [label.text for label in modal.children] == ["Job", "Damage", "Status", "Move to player"]
    assert [o.value for o in modal.job.options if o.default] == ["DRK"]  # filled in with the current values
    assert [o.value for o in modal.status.options if o.default] == ["sub"]
    assert not hasattr(modal, "buff")  # the buff always comes from the job
    assert modal.dmg.default == "3"

    modal.job._values, modal.status._values = ["NL"], ["inactive"]
    type_in(modal.dmg, "3")
    type_in(modal.owner, "Robinson")
    i = run(modal.on_submit)
    char = db.get_character(bot.conn, "Alt")
    owner = db.get_player(bot.conn, char["player_id"])
    assert (char["job"], char["buff"], char["status"], owner.name) == ("NL", "HASTE", "inactive", "Robinson")
    assert i.response.edited["content"].startswith("✅ Updated **Alt**. Buff: HASTE (from the job). Moved from **Robin")
    assert "Robinson" in i.response.edited["embed"].description  # the card refreshes


def test_change_with_unknown_owner_saves_nothing(bot):
    char = db.get_character(bot.conn, "Main")
    modal = EditCharacterModal(CharacterEditView(bot, char["id"]), char)
    modal.job._values, modal.status._values = ["DRK"], ["static"]
    type_in(modal.dmg, "")
    type_in(modal.owner, "nobody-here")
    assert run(modal.on_submit).response.sent["content"].startswith("Nothing saved: No single player matches")
    assert db.get_character(bot.conn, "Main")["job"] == "NL"


def test_remove_ticks_several_of_a_players_characters(bot):
    find = FindPlayerToRemoveModal(bot)
    type_in(find.player, "Robin")
    listing = run(find.on_submit).response.sent
    assert isinstance(listing["view"], RemoveCharactersView) and "Robin" in listing["embed"].title

    modal = run(listing["view"].children[0].callback).response.modal
    assert isinstance(modal, RemoveCharactersModal)
    [group] = modal.groups
    assert sorted(o.label for o in group.options) == ["Alt (DRK)", "Main (NL)"]

    group._values = []
    assert run(modal.on_submit).response.sent["content"] == "Nothing ticked, so nothing was removed."
    group._values = [o.value for o in group.options]
    i = run(modal.on_submit)
    assert db.list_characters(bot.conn, find_player(bot.conn, "Robin").id) == []
    assert i.response.edited["content"].startswith("🗑️ Removed")

    type_in(find.player, "Robinson")
    assert run(find.on_submit).response.sent["content"] == "**Robinson (@robinson)** has no characters."


def test_character_change_option_opens_the_edit_popup_directly(bot):
    cog = CharacterAdminCog(bot)
    i = run(lambda i: CharacterAdminCog.character.callback(cog, i, change="nobody"))
    assert i.response.modal is None and "No character named **nobody**" in i.response.sent["content"]

    i = run(lambda i: CharacterAdminCog.character.callback(cog, i, change="Alt"))
    modal = i.response.modal
    assert isinstance(modal, EditCharacterModal) and not modal.from_card
    assert [o.value for o in modal.status.options if o.default] == ["sub"]  # filled in with Alt's values

    modal.job._values, modal.status._values = ["DRK"], ["static"]
    type_in(modal.dmg, "3")
    type_in(modal.owner, "")
    saved = run(modal.on_submit)
    assert saved.response.edited is None  # nothing to edit: the result is a new private message
    assert saved.response.sent["content"] == "✅ Updated **Alt**." and saved.response.sent["ephemeral"]
    assert isinstance(saved.response.sent["view"], CharacterEditView)  # Edit again from there
    assert db.get_character(bot.conn, "Alt")["status"] == "static"


def test_buff_comes_from_the_job():
    assert db.buff_for_job("bsp") == "HSH" and db.buff_for_job(" NW ") == "HASTE"
    assert db.buff_for_job("XYZ") is None and db.buff_for_job(None) is None
    assert set(db.JOB_BUFFS.values()) <= set(db.BUFFS)


def test_existing_buffs_are_brought_in_line_with_the_job(tmp_path):
    """Migration 8: characters saved with a buff that doesn't match their job get the job's buff."""
    import sqlite3

    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    for version, script in enumerate(db.MIGRATIONS[:7], start=1):
        conn.executescript(f"BEGIN;\n{script}\nPRAGMA user_version = {version};\nCOMMIT;")
    conn.execute("INSERT INTO players (id, name) VALUES (1, 'Robin')")
    conn.executemany(
        "INSERT INTO characters (player_id, ign, job, buff) VALUES (1, ?, ?, ?)",
        [("Odd", "NW", "DPS"), ("Fine", "BSP", "HSH"), ("New", "XYZ", "SE")],
    )
    conn.commit()
    conn.close()

    conn = db.connect(path)
    buffs = {r["ign"]: r["buff"] for r in conn.execute("SELECT ign, buff FROM characters")}
    assert buffs == {"Odd": "HASTE", "Fine": "HSH", "New": "SE"}  # unknown jobs keep theirs
    conn.close()
