"""/host status lists the players who submitted a schedule change (no network)."""

import asyncio
import dataclasses
from datetime import date
from types import SimpleNamespace

from bot import db
from bot.app import MonkeyBot
from bot.cogs.host import HostCog
from bot.config import Config

WEEK = date(2026, 9, 27)


class FakeResponse:
    def __init__(self):
        self.sent = []

    async def send_message(self, content=None, **kwargs):
        self.sent.append({"content": content, **kwargs})


def run_status(cog, week_choice=None):
    interaction = SimpleNamespace(response=FakeResponse())
    cog._week = lambda _choice: WEEK
    asyncio.run(HostCog.status.callback(cog, interaction, week_choice))
    return interaction.response.sent[0]["embed"]


def test_status_lists_only_players_who_changed(tmp_path):
    bot = MonkeyBot(dataclasses.replace(Config.from_env(), database_path=tmp_path / "status.db"))
    conn = bot.conn
    changed = db.create_player(conn, name="Changer", discord_handle="@changer")
    kept = db.create_player(conn, name="Keeper", discord_handle="@keeper")
    db.create_player(conn, name="Silent", discord_handle="@silent")
    benched = db.create_player(conn, name="Benched", discord_handle="@benched", status="inactive")
    db.set_weekly_availability(conn, changed.id, WEEK, {1: "Not Available"})
    db.confirm_no_change(conn, kept.id, WEEK)
    db.set_weekly_availability(conn, benched.id, WEEK, {1: "Preferred"})  # inactive: not counted or listed

    embed = run_status(HostCog(bot))
    assert embed.description.split("\n")[:4] == [
        "**2 / 3** active players have confirmed",
        "• ✅ No change: 1",
        "• ✏️ Updated: 1",
        "• ⏳ Not confirmed yet: 1",
    ]
    assert "substitute" not in embed.description.lower()
    field = embed.fields[0]
    assert field.name == "✏️ Submitted a schedule change"
    assert field.value.strip().split("\n") == ["• Changer (@changer)"]
    conn.close()


def test_status_when_nobody_changed(tmp_path):
    bot = MonkeyBot(dataclasses.replace(Config.from_env(), database_path=tmp_path / "status2.db"))
    db.create_player(bot.conn, name="Silent", discord_handle="@silent")
    embed = run_status(HostCog(bot))
    assert "No one has submitted a schedule change yet" in embed.fields[0].value
    bot.conn.close()


def test_status_lists_character_status_changes(tmp_path):
    bot = MonkeyBot(dataclasses.replace(Config.from_env(), database_path=tmp_path / "changes.db"))
    conn = bot.conn
    alice = db.create_player(conn, name="Alice", discord_handle="@alice", discord_id=1)
    bob = db.create_player(conn, name="Bob", discord_handle="@bob", discord_id=2)
    for player, ign in [(alice, "Ace"), (alice, "Ally"), (bob, "Bolt"), (bob, "Back")]:
        db.add_character(conn, player.id, ign, "NL", "DPS", 3.0, "static")
    ace, ally, bolt, back = (db.get_character(conn, n)["id"] for n in ("Ace", "Ally", "Bolt", "Back"))

    db.set_character_statuses(conn, alice.id, {ace: "sub"}, changed_by=1)          # by the player
    db.set_character_statuses(conn, alice.id, {ace: "inactive"}, changed_by=1)     # changed again
    db.set_character_statuses(conn, alice.id, {ally: "static"}, changed_by=1)      # no actual change
    db.update_character(conn, bolt, changed_by=999, status="sub")                  # by a host
    db.set_character_statuses(conn, bob.id, {back: "sub"}, changed_by=2)
    db.set_character_statuses(conn, bob.id, {back: "static"}, changed_by=2)        # changed back: not shown

    embed = run_status(HostCog(bot))
    field = next(f for f in embed.fields if f.name.startswith("🔄 Character status changes"))
    assert field.value.strip().split("\n") == [
        "• **Alice**: Ace (NL) ⭐ Static → 💤 Inactive",      # before the first change, after the last
        "• **Bob**: Bolt (NL) ⭐ Static → ⏳ Flex *(by host)*",
    ]
    assert conn.execute("SELECT COUNT(*) FROM character_status_log").fetchone()[0] == 5  # every change is logged

    conn.execute("UPDATE character_status_log SET changed_at = changed_at - 8 * 86400")  # older than 7 days
    embed = run_status(HostCog(bot))
    field = next(f for f in embed.fields if f.name.startswith("🔄 Character status changes"))
    assert field.value == "*No character status changes.*"
    conn.close()


def test_open_availability_button(tmp_path):
    from bot.squad_breakdown import SquadBreakdownView

    bot = MonkeyBot(dataclasses.replace(Config.from_env(), database_path=tmp_path / "button.db"))
    db.set_squad_template(bot.conn, 1, 0, "12:00")
    player = db.create_player(bot.conn, name="Alice", discord_handle="@alice")
    db.set_default_availability(bot.conn, player.id, {1: "Preferred"})
    cog = HostCog(bot)
    cog._week = lambda _choice: WEEK

    status = SimpleNamespace(response=FakeResponse())
    asyncio.run(HostCog.status.callback(cog, status, None))
    [button] = status.response.sent[0]["view"].children
    assert button.label == "Open availability"

    bot.is_host = lambda interaction: True
    click = SimpleNamespace(response=FakeResponse(), user=SimpleNamespace(id=1))
    asyncio.run(button.callback(click))
    [sent] = click.response.sent
    assert isinstance(sent["view"], SquadBreakdownView) and sent["view"].week == WEEK  # same week as the status
    assert sent["embed"].title == "Availability: week of Sunday Sep 27, 2026"
    assert "file" not in sent and sent["ephemeral"]  # no CSV export

    bot.is_host = lambda interaction: False
    refused = SimpleNamespace(response=FakeResponse(), user=SimpleNamespace(id=2))
    asyncio.run(button.callback(refused))
    assert refused.response.sent == [{"content": "Only hosts can use this.", "ephemeral": True}]
    bot.conn.close()


class EditResponse(FakeResponse):
    def __init__(self):
        super().__init__()
        self.edits = []

    async def edit_message(self, **kwargs):
        self.edits.append(kwargs)


def press(bot, user_id=1, values=None):
    return SimpleNamespace(response=EditResponse(), user=SimpleNamespace(id=user_id), data={"values": values or []})


def test_see_individual_availability(tmp_path):
    import discord

    from bot.cogs.host import IndividualAvailabilityView

    bot = MonkeyBot(dataclasses.replace(Config.from_env(), database_path=tmp_path / "changes_view.db"))
    conn = bot.conn
    for number, weekday, hhmm in [(1, 0, "12:00"), (2, 1, "11:00"), (3, 4, "21:00")]:
        db.set_squad_template(conn, number, weekday, hhmm)
    changer = db.create_player(conn, name="Changer", discord_handle="@changer")
    same = db.create_player(conn, name="Same", discord_handle="@same")
    db.create_player(conn, name="Keeper", discord_handle="@keeper")
    db.set_default_availability(conn, changer.id, {1: "Preferred", 2: "Available", 3: "Preferred"})
    db.set_weekly_availability(conn, changer.id, WEEK, {1: "Not Available", 2: "Available", 3: "Available"})
    db.set_default_availability(conn, same.id, {1: "Preferred"})
    db.set_weekly_availability(conn, same.id, WEEK, {1: "Preferred"})  # re-saved without changing anything
    cog = HostCog(bot)
    cog._week = lambda _choice: WEEK
    bot.is_host = lambda interaction: True

    # the status message has buttons only: no dropdown
    status = SimpleNamespace(response=FakeResponse())
    asyncio.run(HostCog.status.callback(cog, status, None))
    view = status.response.sent[0]["view"]
    assert not [c for c in view.children if isinstance(c, discord.ui.Select)]
    assert [c.label for c in view.children] == ["See individual availability", "Open availability"]

    # the button opens ONE message with the player dropdown
    opened = press(bot)
    asyncio.run(view.children[0].callback(opened))
    [sent] = opened.response.sent
    picker = sent["view"]
    # (compared by name: other tests reload the cog module, which makes a second copy of the class)
    assert type(picker).__name__ == IndividualAvailabilityView.__name__ and sent["ephemeral"]
    assert "Pick one below" in sent["embed"].description
    [select] = picker.children  # no sort button until someone is picked
    assert {o.label: o.description for o in select.options} == {
        "Changer (@changer)": "2 squad(s) changed from their default",
        "Same (@same)": "0 squad(s) changed from their default",
    }

    # picking a player EDITS that message in place (no new message)
    pick = press(bot, values=[str(changer.id)])
    asyncio.run(picker._pick(pick))
    assert pick.response.sent == []
    [edit] = pick.response.edits
    changes = edit["embed"].fields[0]
    assert changes.name == "✏️ Changed from default"
    lines = changes.value.strip().split("\n")
    assert len(lines) == 2
    assert lines[0].startswith("• **Squad 1**") and lines[0].endswith("🟢 Preferred → 🔴 Not Available")
    assert lines[1].startswith("• **Squad 3**") and lines[1].endswith("🟢 Preferred → 🟡 Available")
    assert "Changer (@changer)" in edit["embed"].title
    options = {o.label: o.default for o in edit["view"].children[0].options}
    assert options == {"Changer (@changer)": True, "Same (@same)": False}  # dropdown stays, shows who's picked
    assert [c.label for c in edit["view"].children[1:]] == ["Sort by squad #"]

    # picking someone else edits it again
    pick_same = press(bot, values=[str(same.id)])
    asyncio.run(picker._pick(pick_same))
    assert pick_same.response.sent == []
    assert "Same as their default" in pick_same.response.edits[0]["embed"].fields[0].value

    # 🔀 re-sorts in place
    sort = press(bot)
    asyncio.run(picker._toggle_order(sort))
    assert sort.response.sent == [] and picker.order == "number"
    assert [c.label for c in sort.response.edits[0]["view"].children[1:]] == ["Sort by time"]

    # hosts only
    bot.is_host = lambda interaction: False
    refused = press(bot, user_id=2, values=[str(changer.id)])
    asyncio.run(picker._pick(refused))
    assert refused.response.sent == [{"content": "Only hosts can use this.", "ephemeral": True}]
    conn.close()


def test_no_individual_button_when_no_one_changed(tmp_path):
    bot = MonkeyBot(dataclasses.replace(Config.from_env(), database_path=tmp_path / "none.db"))
    db.create_player(bot.conn, name="Keeper", discord_handle="@keeper")
    cog = HostCog(bot)
    cog._week = lambda _choice: WEEK
    status = SimpleNamespace(response=FakeResponse())
    asyncio.run(HostCog.status.callback(cog, status, None))
    assert [c.label for c in status.response.sent[0]["view"].children] == ["Open availability"]
    bot.conn.close()


def test_changes_view_includes_character_week_changes(tmp_path):
    import discord

    bot = MonkeyBot(dataclasses.replace(Config.from_env(), database_path=tmp_path / "charweek.db"))
    conn = bot.conn
    db.set_squad_template(conn, 1, 0, "12:00")
    db.set_squad_template(conn, 2, 1, "11:00")
    player = db.create_player(conn, name="Pat", discord_handle="@pat")
    db.add_character(conn, player.id, "Ace", "NL", "DPS", 3.0)
    db.set_default_availability(conn, player.id, {1: "Preferred", 2: "Preferred"})
    ace = db.get_character(conn, "Ace")
    db.set_character_weekly(conn, player.id, ace["id"], WEEK, {2: "Not Available"})  # checks them in too
    cog = HostCog(bot)
    cog._week = lambda _choice: WEEK

    from bot.cogs.host import IndividualAvailabilityView

    picker = IndividualAvailabilityView(cog, WEEK, [player], user_id=1)
    [select] = [c for c in picker.children if isinstance(c, discord.ui.Select)]
    assert select.options[0].description == "0 squad(s) changed from their default · 1 character(s) changed"

    embed = cog._changes_embed(player, WEEK)
    assert embed.fields[0].value.startswith("*Their own schedule is the same as their default")
    assert embed.fields[1].name == "🧩 Characters changed for this week"
    assert embed.fields[1].value.strip() == "• 🧩 **Ace**: Squad 2 🔴 Not Available"
    conn.close()
