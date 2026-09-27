"""Only hosts can add or change players and characters (no network)."""

import asyncio
import dataclasses
from types import SimpleNamespace

import pytest
from discord import app_commands

from bot import db
from bot.app import COGS, MonkeyBot
from bot.cogs.host import CharacterAdminCog, ConfigCog, PlayerAdminCog
from bot.config import Config


class FakeResponse:
    def __init__(self):
        self.messages = []

    async def send_message(self, content=None, **kwargs):
        self.messages.append(content)


HOST_ID = 555


def call(cog, command, **kwargs):
    interaction = SimpleNamespace(response=FakeResponse(), user=SimpleNamespace(id=HOST_ID))
    asyncio.run(command.callback(cog, interaction, **kwargs))
    return interaction.response.messages[-1]


@pytest.fixture
def cogs(tmp_path):
    bot = MonkeyBot(dataclasses.replace(Config.from_env(), database_path=tmp_path / "roster.db"))
    yield PlayerAdminCog(bot), CharacterAdminCog(bot)
    bot.conn.close()


def test_players_have_no_roster_editing_commands(tmp_path):
    bot = MonkeyBot(dataclasses.replace(Config.from_env(), database_path=tmp_path / "cmds.db"))

    async def load_and_list():
        for ext in COGS:
            await bot.load_extension(ext)
        try:
            groups = {name: {c.name for c in bot.tree.get_command(name).commands}
                      for name in ("host", "config", "player", "character", "cq")}
            return {c.name for c in bot.tree.get_commands()}, groups
        finally:
            await bot.close()  # stops the reminder loop too

    top_level, groups = asyncio.run(load_and_list())
    # every command lives in a group; players have just /cq availability and /cq characters
    assert top_level == {"host", "config", "player", "character", "cq"}
    assert groups["cq"] == {"availability", "characters"}  # everything else is buttons
    assert groups["player"] == {"add", "edit", "link"}
    assert groups["character"] == {"add", "edit", "remove"}
    assert groups["config"] == {"reminders", "damage_logs", "squad_time", "squad_remove"}
    assert groups["host"] == {"status", "availability", "import", "squads", "damage_history", "remind_now"}


def test_add_player_and_characters(cogs):
    players, characters = cogs
    conn = players.bot.conn
    member = SimpleNamespace(id=99, name="newbie", mention="<@99>")
    reply = call(players, PlayerAdminCog.add_player, name="Newbie", member=member, username=None, status=None)
    assert reply.startswith("✅ Added **Newbie (@newbie)** (active)")
    player = db.get_player_by_discord_id(conn, 99)

    again = call(players, PlayerAdminCog.add_player, name="Dup", member=member, username=None, status=None)
    assert "already on the roster" in again

    buff = app_commands.Choice(name="DPS", value="DPS")
    call(characters, CharacterAdminCog.add_character, player=str(player.id), ign="NewChar", job="nl", buff=buff, dmg=2.5)
    char = db.get_character(conn, "newchar")
    assert (char["player_id"], char["job"], char["buff"], char["dmg"]) == (player.id, "NL", "DPS", 2.5)
    dup = call(characters, CharacterAdminCog.add_character, player=str(player.id), ign="NEWCHAR", job="NL", buff=buff, dmg=None)
    assert "already exists" in dup

    call(characters, CharacterAdminCog.edit_character, character="NewChar", job="bm", buff=None, dmg=3.0)
    char = db.get_character(conn, "NewChar")
    assert (char["job"], char["dmg"]) == ("BM", 3.0)

    other = db.create_player(conn, name="Other", discord_handle="@other")
    reply = call(characters, CharacterAdminCog.edit_character, character="NewChar", job=None, buff=None, dmg=None, owner=str(other.id))
    assert db.get_character(conn, "NewChar")["player_id"] == other.id
    assert "Moved from **Newbie (@newbie)** to **Other (@other)**" in reply
    bad = call(characters, CharacterAdminCog.edit_character, character="NewChar", job=None, buff=None, dmg=None, owner="nobody-here")
    assert "Couldn't find" in bad and db.get_character(conn, "NewChar")["player_id"] == other.id

    call(players, PlayerAdminCog.edit_player, player=str(player.id), name="Renamed")
    assert db.get_player(conn, player.id).name == "Renamed"
    inactive = app_commands.Choice(name="Inactive", value="inactive")
    reply = call(players, PlayerAdminCog.edit_player, player=str(player.id), status=inactive)
    assert db.get_player(conn, player.id).status == "inactive" and "status **Inactive" in reply
    assert db.get_player(conn, player.id).name == "Renamed"  # name untouched
    active = app_commands.Choice(name="Active", value="active")
    call(players, PlayerAdminCog.edit_player, player=str(player.id), name="Both", status=active)
    assert (db.get_player(conn, player.id).name, db.get_player(conn, player.id).status) == ("Both", "active")
    nothing = call(players, PlayerAdminCog.edit_player, player=str(player.id))
    assert "Give a new `name`, a `status`, or both" in nothing

    call(characters, CharacterAdminCog.remove_character, character="NewChar")
    assert db.get_character(conn, "NewChar") is None


def test_add_player_by_username_links_later(cogs):
    players, _ = cogs
    call(players, PlayerAdminCog.add_player, name="Later", member=None, username="@futureplayer", status=None)
    player = db.get_player_by_handle(players.bot.conn, "@futureplayer")
    assert player is not None and player.discord_id is None
    # the first time that Discord user interacts, they're linked by username
    linked = players.bot.resolve_player(SimpleNamespace(id=1234, name="futureplayer"))
    assert linked.id == player.id and linked.discord_id == 1234


def test_squad_time_change_and_reset(tmp_path):
    from datetime import date

    from bot import timeutil

    bot = MonkeyBot(dataclasses.replace(Config.from_env(), database_path=tmp_path / "squads.db"))
    db.set_squad_template(bot.conn, 1, 0, "12:00")
    config = ConfigCog(bot)
    week = timeutil.upcoming_week_start(timeutil.now_utc(), bot.tz)
    monday = app_commands.Choice(name="Monday", value=1)

    def squad1():
        return next(s for s in db.squads_for_week(bot.conn, week, bot.tz) if s.number == 1)

    call(config, ConfigCog.squad_time, squad=1, day=monday, time="21:30")
    assert squad1().overridden

    reply = call(config, ConfigCog.squad_time, squad=1, reset=True)
    assert not squad1().overridden and "back on the recurring schedule" in reply

    mixed = call(config, ConfigCog.squad_time, squad=1, day=monday, time="21:30", reset=True)
    assert "leave out day, time and permanent" in mixed and not squad1().overridden
    missing = call(config, ConfigCog.squad_time, squad=1)
    assert "Give a `day` and `time`" in missing
    bot.conn.close()
