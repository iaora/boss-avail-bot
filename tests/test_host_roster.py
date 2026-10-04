"""Only hosts can add or change players and characters (no network)."""

import asyncio
import dataclasses
from types import SimpleNamespace

import pytest

from bot import db
from bot.app import COGS, MonkeyBot
from bot.cogs.host import add_character_entry, add_player_entry, edit_character_entry, edit_player_entry, remove_characters
from bot.config import Config

HOST_ID = 555


@pytest.fixture
def bot(tmp_path):
    bot = MonkeyBot(dataclasses.replace(Config.from_env(), database_path=tmp_path / "roster.db"))
    yield bot
    bot.conn.close()


def test_players_have_no_roster_editing_commands(tmp_path):
    bot = MonkeyBot(dataclasses.replace(Config.from_env(), database_path=tmp_path / "cmds.db"))

    async def load_and_list():
        for ext in COGS:
            await bot.load_extension(ext)
        try:
            groups = {name: {c.name for c in bot.tree.get_command(name).commands} for name in ("cq",)}
            return {c.name for c in bot.tree.get_commands()}, groups
        finally:
            await bot.close()  # stops the reminder loop too

    top_level, groups = asyncio.run(load_and_list())
    # hosts have panels of buttons (/cq_host, /cq_config, /character, /player); players have
    # /cq availability, /cq characters and /settings; anyone can start a /findatime poll
    assert top_level == {"cq_host", "cq_config", "player", "character", "cq", "settings", "findatime"}
    assert groups["cq"] == {"availability", "characters"}  # everything else is buttons


def test_add_player_and_characters(bot):
    conn = bot.conn
    member = SimpleNamespace(id=99, name="newbie", mention="<@99>")
    reply = add_player_entry(bot, name="Newbie", member=member, username=None)
    assert reply.startswith("✅ Added **Newbie (@newbie)** (active)")
    player = db.get_player_by_discord_id(conn, 99)

    with pytest.raises(ValueError, match="already on the roster"):
        add_player_entry(bot, name="Dup", member=member, username=None)
    with pytest.raises(ValueError, match="Enter the player's name"):
        add_player_entry(bot, name="  ", member=None, username="someone")
    with pytest.raises(ValueError, match="isn't a known job"):
        add_character_entry(bot, player="Newbie", ign="NewChar", job="xyz", dmg="2.5")
    add_character_entry(bot, player="Newbie", ign="NewChar", job="nl", dmg="2.5")
    char = db.get_character(conn, "newchar")
    assert (char["player_id"], char["job"], char["buff"], char["dmg"]) == (player.id, "NL", "HASTE", 2.5)
    with pytest.raises(ValueError, match="already exists"):
        add_character_entry(bot, player="Newbie", ign="NEWCHAR", job="NL", dmg=None)

    def edit(**changes):
        char = db.get_character(conn, "NewChar")
        fields = dict(job=char["job"], dmg="", status=char["status"], owner="")
        return edit_character_entry(bot, char, changed_by=HOST_ID, **{**fields, **changes})

    edit(job="bm", dmg="3")
    char = db.get_character(conn, "NewChar")
    assert (char["job"], char["buff"], char["dmg"]) == ("BM", "SE", 3.0)  # the buff follows the job

    other = db.create_player(conn, name="Other", discord_handle="@other")
    reply = edit(owner="other")
    assert db.get_character(conn, "NewChar")["player_id"] == other.id
    assert "Moved from **Newbie (@newbie)** to **Other (@other)**" in reply
    with pytest.raises(ValueError, match="No single player matches"):
        edit(owner="nobody-here")
    assert db.get_character(conn, "NewChar")["player_id"] == other.id

    def edit_player(**changes):
        p = db.get_player(conn, player.id)
        return edit_player_entry(bot, p, **{"name": p.name, "status": p.status, **changes})

    edit_player(name="Renamed")
    assert db.get_player(conn, player.id).name == "Renamed"
    reply = edit_player(status="inactive")
    assert db.get_player(conn, player.id).status == "inactive" and "status **Inactive" in reply
    assert db.get_player(conn, player.id).name == "Renamed"  # name untouched
    edit_player(name="Both", status="active")
    assert (db.get_player(conn, player.id).name, db.get_player(conn, player.id).status) == ("Both", "active")
    assert edit_player().startswith("Nothing changed")

    remove_characters(bot, [db.get_character(conn, "NewChar")["id"]])
    assert db.get_character(conn, "NewChar") is None


def test_add_player_by_username_links_later(bot):
    add_player_entry(bot, name="Later", member=None, username="@futureplayer")
    player = db.get_player_by_handle(bot.conn, "@futureplayer")
    assert player is not None and player.discord_id is None
    # the first time that Discord user interacts, they're linked by username
    linked = bot.resolve_player(SimpleNamespace(id=1234, name="futureplayer"))
    assert linked.id == player.id and linked.discord_id == 1234


def test_squad_time_change_and_reset(tmp_path):
    from bot import timeutil
    from bot.cogs.host import change_squad_time

    bot = MonkeyBot(dataclasses.replace(Config.from_env(), database_path=tmp_path / "squads.db"))
    db.set_squad_template(bot.conn, 1, 0, "12:00")
    week = timeutil.upcoming_week_start(timeutil.now_utc(), bot.tz)

    def squad1():
        return next(s for s in db.squads_for_week(bot.conn, week, bot.tz) if s.number == 1)

    change_squad_time(bot, 1, "week", 1, "21:30")
    assert squad1().overridden

    reply = change_squad_time(bot, 1, "reset", None, None)
    assert not squad1().overridden and "back on the recurring schedule" in reply

    with pytest.raises(ValueError, match="Pick a day and enter a time"):
        change_squad_time(bot, 1, "week", None, "")
    with pytest.raises(ValueError, match="21:30"):
        change_squad_time(bot, 1, "week", 1, "9pm")
    with pytest.raises(ValueError, match="Choose Every week to add it"):
        change_squad_time(bot, 2, "week", 1, "21:30")
    added = change_squad_time(bot, 2, "permanent", 3, "21:30")
    assert "every week" in added and 2 in db.squad_numbers(bot.conn)
    bot.conn.close()
