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

    async def send_message(self, **kwargs):
        self.sent.append(kwargs)


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
    sub = db.create_player(conn, name="Bench", discord_handle="@bench", status="sub")
    db.set_weekly_availability(conn, changed.id, WEEK, {1: "Not Available"})
    db.confirm_no_change(conn, kept.id, WEEK)
    db.set_weekly_availability(conn, sub.id, WEEK, {1: "Preferred"})

    embed = run_status(HostCog(bot))
    assert "**2 / 3** active players have confirmed" in embed.description
    [field] = embed.fields
    assert field.name == "✏️ Submitted a schedule change"
    assert field.value.split("\n")[:2] == ["Changer (@changer)", "Bench (@bench) (sub)"]
    assert "Keeper" not in field.value and "Silent" not in field.value
    conn.close()


def test_status_when_nobody_changed(tmp_path):
    bot = MonkeyBot(dataclasses.replace(Config.from_env(), database_path=tmp_path / "status2.db"))
    db.create_player(bot.conn, name="Silent", discord_handle="@silent")
    embed = run_status(HostCog(bot))
    assert "No one has submitted a schedule change yet" in embed.fields[0].value
    bot.conn.close()
