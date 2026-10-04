"""Class icons, stored as the bot's own application emojis (named class_<JOB>, e.g. class_DRK).

Application emojis belong to the bot, not a server: they work everywhere the bot posts and
don't use server emoji slots, but each bot (prod and test) has its own copies. The source of truth
is the bot_data/class_icons/ folder, shared by both bots: one image per class, named after the job
(DRK.png, NL.png, ...). Each bot keeps Discord in step with it in the background (see
cogs/class_icons.py): new files are uploaded, changed files replaced, and icons whose file was
deleted are removed.
"""

from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING

import discord

from . import db

if TYPE_CHECKING:
    from .app import MonkeyBot

log = logging.getLogger(__name__)

PREFIX = "class_"
MAX_BYTES = 256 * 1024  # Discord's emoji size limit
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif"}


def normalize_job(job: str) -> str:
    return re.sub(r"[^A-Z0-9_]", "", job.strip().upper())


def emoji_name(job: str) -> str:
    return f"{PREFIX}{normalize_job(job)}"[:32]


class ClassIcons:
    def __init__(self) -> None:
        self._emojis: dict[str, discord.Emoji] = {}  # job -> emoji

    def get(self, job: str | None) -> str | None:
        """The emoji markup for a job's icon, e.g. '<:class_DRK:123>', or None if there isn't one."""
        emoji = self._emojis.get(normalize_job(job or ""))
        return str(emoji) if emoji else None

    def jobs(self) -> list[str]:
        return sorted(self._emojis)

    async def load(self, bot: "MonkeyBot") -> None:
        self._emojis = {
            e.name[len(PREFIX):]: e for e in await bot.fetch_application_emojis() if e.name.startswith(PREFIX)
        }
        log.info("Loaded %d class icons", len(self._emojis))

    async def set(self, bot: "MonkeyBot", job: str, image: bytes) -> discord.Emoji:
        if len(image) > MAX_BYTES:
            raise ValueError("the image must be 256 KB or smaller")
        key = normalize_job(job)
        if not key:
            raise ValueError("the job name must contain letters or numbers")
        if old := self._emojis.get(key):
            await old.delete()
        emoji = await bot.create_application_emoji(name=emoji_name(key), image=image)
        self._emojis[key] = emoji
        return emoji

    async def remove(self, job: str) -> bool:
        emoji = self._emojis.pop(normalize_job(job), None)
        if emoji:
            await emoji.delete()
        return emoji is not None

    async def sync(self, bot: "MonkeyBot", folder: Path) -> None:
        """Make the bot's class icons match the image files in `folder`.

        A missing folder is left alone (no icons are deleted), so a machine without the
        folder doesn't wipe icons uploaded from another one.
        """
        if not folder.is_dir():
            return
        files = {}
        for path in sorted(folder.iterdir()):
            job = normalize_job(path.stem)
            if path.suffix.lower() in IMAGE_SUFFIXES and job:
                files[job] = path

        for job, path in files.items():
            data = path.read_bytes()
            digest = hashlib.sha256(data).hexdigest()
            key, failed_key = f"class_icon_sha256:{job}", f"class_icon_failed:{job}"
            if job in self._emojis and db.get_setting(bot.conn, key) == digest:
                continue  # already uploaded and unchanged
            if db.get_setting(bot.conn, failed_key) == digest:
                continue  # this exact file was rejected before; wait until it changes
            try:
                await self.set(bot, job, data)
                db.set_setting(bot.conn, key, digest)
                log.info("Uploaded class icon for %s from %s", job, path.name)
            except (ValueError, discord.HTTPException) as e:
                db.set_setting(bot.conn, failed_key, digest)
                log.warning("Couldn't upload class icon %s: %s (fix or replace the file to retry)", path.name, e)

        for job in [j for j in self._emojis if j not in files]:
            try:
                await self.remove(job)
                log.info("Removed class icon for %s (its file is gone)", job)
            except discord.HTTPException as e:
                log.warning("Couldn't remove class icon %s: %s", job, e)
