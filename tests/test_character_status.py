"""Character status (static / sub / inactive) and the host's per-squad breakdown (no network)."""

import asyncio
import dataclasses
from datetime import date
from types import SimpleNamespace

import discord
import pytest

from bot import db, importer
from bot.app import MonkeyBot
from bot.config import Config
from bot.squad_breakdown import SquadBreakdownView, build_breakdown
from bot.views import CharacterStatusEditor

WEEK = date(2026, 9, 27)


class FakeResponse:
    def __init__(self):
        self.edits = []

    async def edit_message(self, **kwargs):
        self.edits.append(kwargs)


def interaction(values=None, user_id=None):
    return SimpleNamespace(data={"values": values or []}, response=FakeResponse(), user=SimpleNamespace(id=user_id))


@pytest.fixture
def bot(tmp_path):
    bot = MonkeyBot(dataclasses.replace(Config.from_env(), database_path=tmp_path / "status.db"))
    conn = bot.conn
    for number, weekday, hhmm in [(1, 0, "12:00"), (2, 6, "11:00")]:
        db.set_squad_template(conn, number, weekday, hhmm)
    alice = db.create_player(conn, name="Alice", discord_handle="@alice")
    bob = db.create_player(conn, name="Bob", discord_handle="@bob")
    db.add_character(conn, alice.id, "AliceMain", "NL", "DPS", 4.0, "static")
    db.add_character(conn, alice.id, "AliceAlt", "BM", "SE", 3.0, "sub")
    db.add_character(conn, alice.id, "AliceOld", "DRK", "HB", 5.0, "inactive")
    db.add_character(conn, bob.id, "BobSub", "SHAD", "HASTE", 3.5, "sub")
    db.set_default_availability(conn, alice.id, {1: "Preferred", 2: "Available"})
    db.set_default_availability(conn, bob.id, {1: "Preferred", 2: "Not Available"})
    yield bot
    conn.close()


def test_import_sets_status_from_perm_once(tmp_path):
    bot = MonkeyBot(dataclasses.replace(Config.from_env(), database_path=tmp_path / "imp.db"))
    csv_text = "IGN,Name,Discord,Perm\nPermChar,P,@p,TRUE\nFlexChar,P,@p,FALSE\n"
    importer.import_roster(bot.conn, csv_text)
    assert db.get_character(bot.conn, "PermChar")["status"] == "static"
    assert db.get_character(bot.conn, "FlexChar")["status"] == "sub"
    player = db.get_player_by_handle(bot.conn, "@p")
    db.set_character_statuses(bot.conn, player.id, {db.get_character(bot.conn, "PermChar")["id"]: "inactive"})
    importer.import_roster(bot.conn, csv_text)  # re-import keeps the player's choice
    assert db.get_character(bot.conn, "PermChar")["status"] == "inactive"
    bot.conn.close()


def test_players_can_only_change_their_own_characters(bot):
    conn = bot.conn
    alice = db.get_player_by_handle(conn, "@alice")
    bob_sub = db.get_character(conn, "BobSub")
    db.set_character_statuses(conn, alice.id, {bob_sub["id"]: "inactive"})
    assert db.get_character(conn, "BobSub")["status"] == "sub"
    with pytest.raises(ValueError):
        db.set_character_statuses(conn, alice.id, {bob_sub["id"]: "retired"})


def test_status_editor_saves_on_save_only(bot):
    alice = db.get_player_by_handle(bot.conn, "@alice")
    editor = CharacterStatusEditor(bot, alice)
    alt = db.get_character(bot.conn, "AliceAlt")
    editor.draft[alt["id"]] = "static"
    assert db.get_character(bot.conn, "AliceAlt")["status"] == "sub"  # not saved yet
    asyncio.run(editor._save(interaction()))
    assert db.get_character(bot.conn, "AliceAlt")["status"] == "static"


def test_character_availability_combines_weekly_and_overrides(bot):
    conn = bot.conn
    alice = db.get_player_by_handle(conn, "@alice")
    alt = db.get_character(conn, "AliceAlt")
    db.set_character_overrides(conn, alt["id"], {1: "Not Available"})
    assert db.character_week_availability(conn, alt, WEEK) == {1: "Not Available", 2: "Available"}
    # weekly change says Preferred everywhere, but the character's own limit still applies
    db.set_weekly_availability(conn, alice.id, WEEK, {1: "Preferred", 2: "Preferred"})
    assert db.character_week_availability(conn, alt, WEEK) == {1: "Not Available", 2: "Preferred"}


def test_breakdown_hides_inactive_and_lists_static_first(bot):
    conn = bot.conn
    players = db.list_players(conn, ("active",))
    squads = db.squads_for_week(conn, WEEK, bot.tz)
    data = build_breakdown(conn, players, squads, WEEK)

    preferred_1 = data[1]["Preferred"]
    assert [e.player.name for e in preferred_1] == ["Alice", "Bob"]  # Alice has a static char
    assert [c["ign"] for c in preferred_1[0].characters] == ["AliceMain", "AliceAlt"]  # no AliceOld
    assert [e.player.name for e in data[2]["Available"]] == ["Alice"]
    assert data[2]["Preferred"] == []


def test_breakdown_view_buttons_and_squad_picker(bot):
    players = db.list_players(bot.conn, ("active",))
    view = SquadBreakdownView(bot, WEEK, players, discord.Embed(title="Overview"))
    view.view_mode = "player"  # this test checks the per-player layout

    i = interaction()
    asyncio.run(view._level_callback("Preferred")(i))
    embed = i.response.edits[-1]["embed"]
    assert embed.title.startswith("🟢 Preferred players per squad")
    assert "Alice, ⏳ Bob" in embed.fields[0].value  # Bob only has a sub character

    i = interaction(["1"])
    asyncio.run(view._pick_squad(i))
    embed = i.response.edits[-1]["embed"]
    assert embed.title == "Squad 1: 🟢 Preferred (2 players)"
    assert embed.fields[0].value.split("\n")[:4] == [
        "**Alice**", "\u2003• AliceMain/NL", "\u2003• ⏳ AliceAlt/BM", "**Bob**"  # static before sub
    ]
    assert "AliceOld" not in embed.fields[0].value

    i = interaction()
    asyncio.run(view._level_callback("Available")(i))  # stays on squad 1, switches level
    assert i.response.edits[-1]["embed"].title == "Squad 1: 🟡 Available (0 players)"

    i = interaction()
    asyncio.run(view._show_overview(i))
    assert i.response.edits[-1]["embed"].title == "Overview" and view.squad is None


def test_players_choose_only_static_or_sub(bot):
    alice = db.get_player_by_handle(bot.conn, "@alice")  # AliceMain static, AliceAlt sub, AliceOld inactive
    editor = CharacterStatusEditor(bot, alice)
    options = [[o.value for o in c.options] for c in editor.children if isinstance(c, discord.ui.Select)]
    assert options and all(values == ["static", "sub"] for values in options)  # no Inactive choice

    # the host-set inactive character is shown but locked
    assert db.get_character(bot.conn, "AliceOld")["id"] not in editor.draft
    assert "💤 **AliceOld** · DRK · HB · Inactive (set by a host)" in editor.embed().description
    asyncio.run(editor._save(interaction(user_id=1)))
    assert db.get_character(bot.conn, "AliceOld")["status"] == "inactive"  # untouched by the player


def test_only_hosts_set_inactive_and_all_inactive_player_is_told(bot):
    bob = db.get_player_by_handle(bot.conn, "@bob")  # BobSub is his only character
    bob_sub = db.get_character(bot.conn, "BobSub")
    db.update_character(bot.conn, bob_sub["id"], changed_by=999, status="inactive")  # what /character edit does
    assert db.get_character(bot.conn, "BobSub")["status"] == "inactive"

    class Response:
        async def send_message(self, content=None, **kwargs):
            self.content = content

    i = SimpleNamespace(response=Response())
    asyncio.run(CharacterStatusEditor(bot, bob).send(i))
    assert "marked 💤 Inactive by a host" in i.response.content
