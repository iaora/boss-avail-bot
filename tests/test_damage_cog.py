"""Drives the #queen-logs listener with fake Discord messages (no network)."""

import asyncio
import dataclasses
from types import SimpleNamespace

import pytest

from bot import db
from bot.app import MonkeyBot
from bot.cogs.damage_logs import DamageLogCog
from bot.config import Config

from .test_damage import make_log


class FakeAttachment:
    def __init__(self, filename: str, text: str):
        self.filename, self._data = filename, text.encode()
        self.size = len(self._data)

    async def read(self) -> bytes:
        return self._data


class FakeMessage:
    def __init__(self, message_id: int, *attachments: FakeAttachment):
        self.id = message_id
        self.author = SimpleNamespace(bot=False, id=42)
        self.guild = object()
        self.channel = SimpleNamespace(id=777, name="queen-logs")
        self.attachments = list(attachments)
        self.reactions: list[str] = []
        self.replies: list[tuple[str, bool]] = []

    async def add_reaction(self, emoji: str) -> None:
        self.reactions.append(emoji)

    async def reply(self, text: str, mention_author: bool = False) -> None:
        self.replies.append((text, mention_author))


@pytest.fixture
def cog(tmp_path):
    config = dataclasses.replace(
        Config.from_env(), database_path=tmp_path / "cog.db", queen_logs_channel_id=None
    )
    bot = MonkeyBot(config)
    bot.is_host_member = lambda member: True
    player = db.create_player(bot.conn, name="Tester", discord_handle="@tester")
    for ign in ("Alpha", "Bravo"):
        db.add_character(bot.conn, player.id, ign, "NL", 1.0)
    yield DamageLogCog(bot)
    bot.conn.close()


LOG = make_log(
    "27-09-2026 01:00:00", "27-09-2026 01:27:30",
    {"Alpha": 3_000_000_000, "Ghost": 9_000_000_000, "bravo": 2_000_000_000},
)


def test_unknown_names_are_flagged_and_others_saved(cog):
    message = FakeMessage(1, FakeAttachment("run.txt", LOG))
    asyncio.run(cog.on_message(message))

    assert db.get_character(cog.bot.conn, "Alpha")["dmg"] == 3.0
    assert db.get_character(cog.bot.conn, "Bravo")["dmg"] == 2.0
    assert message.reactions == ["✅", "⚠️"]
    [(text, pinged)] = message.replies
    assert "`Ghost`" in text and "Alpha" not in text
    assert pinged  # the uploading host is notified


def test_reupload_still_flags_unknown_names(cog):
    asyncio.run(cog.on_message(FakeMessage(1, FakeAttachment("run.txt", LOG))))
    again = FakeMessage(2, FakeAttachment("run.txt", LOG))
    asyncio.run(cog.on_message(again))
    [(text, pinged)] = again.replies
    assert "already uploaded" in text and "`Ghost`" in text and pinged
    assert again.reactions == ["⚠️"]


def test_clean_log_only_reacts(cog):
    clean = make_log("28-09-2026 01:00:00", "28-09-2026 01:27:30", {"Alpha": 4_000_000_000})
    message = FakeMessage(3, FakeAttachment("clean.txt", clean))
    asyncio.run(cog.on_message(message))
    assert message.reactions == ["✅"] and message.replies == []


def test_other_channels_and_non_hosts_ignored(cog):
    message = FakeMessage(4, FakeAttachment("run.txt", LOG))
    message.channel = SimpleNamespace(id=1, name="general")
    asyncio.run(cog.on_message(message))
    cog.bot.is_host_member = lambda member: False
    message2 = FakeMessage(5, FakeAttachment("run.txt", LOG))
    asyncio.run(cog.on_message(message2))
    assert message.reactions == message.replies == message2.reactions == message2.replies == []
    assert db.get_character(cog.bot.conn, "Alpha")["dmg"] == 1.0
