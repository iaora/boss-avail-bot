"""Class icons (application emojis) and how the squad detail shows them (no network)."""

import asyncio
import dataclasses
from datetime import date

import discord
import pytest

from bot import db
from bot.app import MonkeyBot
from bot.class_icons import ClassIcons, emoji_name, normalize_job
from bot.config import Config
from bot.squad_breakdown import PrepRosterView, SquadBreakdownView

WEEK = date(2026, 9, 27)


class FakeEmoji:
    _next_id = 1_000_000_000_000_000_000

    def __init__(self, name: str):
        FakeEmoji._next_id += 1
        self.id, self.name, self.deleted = FakeEmoji._next_id, name, False

    def __str__(self):
        return f"<:{self.name}:{self.id}>"

    async def delete(self):
        self.deleted = True


class FakeDiscord:
    """Stands in for the bot's application-emoji API calls."""

    def __init__(self, existing=()):
        self.emojis = [FakeEmoji(n) for n in existing]

    async def fetch_application_emojis(self):
        return list(self.emojis)

    async def create_application_emoji(self, *, name, image):
        emoji = FakeEmoji(name)
        self.emojis.append(emoji)
        return emoji


def class_view_lines(embed) -> list[str]:
    """The class view's lines: everything in the description after the time/week line."""
    return embed.description.split("\n\n", 1)[1].split("\n")


def test_names():
    assert normalize_job(" drk ") == "DRK"
    assert emoji_name("Sair") == "class_SAIR"


def test_load_set_replace_remove():
    api = FakeDiscord(existing=["class_DRK", "some_other_emoji"])
    icons = ClassIcons()
    asyncio.run(icons.load(api))
    assert icons.jobs() == ["DRK"] and icons.get("drk").startswith("<:class_DRK:")
    assert icons.get("NL") is None

    old = api.emojis[0]
    asyncio.run(icons.set(api, "drk", b"png-bytes"))
    assert old.deleted and icons.get("DRK") != str(old)  # replaced, not duplicated
    with pytest.raises(ValueError):
        asyncio.run(icons.set(api, "NL", b"x" * (256 * 1024 + 1)))

    assert asyncio.run(icons.remove("DRK")) and icons.get("DRK") is None
    assert not asyncio.run(icons.remove("DRK"))


def sync(icons, api, folder):
    asyncio.run(icons.sync(api, folder))


def test_folder_sync_adds_replaces_and_removes(tmp_path):
    folder = tmp_path / "class_icons"
    folder.mkdir()
    (folder / "NL.png").write_bytes(b"nl-v1")
    (folder / "drk.PNG").write_bytes(b"drk-v1")
    (folder / "notes.txt").write_text("ignored")
    api = FakeDiscord(existing=["class_BM"])  # an icon whose file isn't in the folder
    api.conn = db.connect(tmp_path / "sync.db")
    icons = ClassIcons()
    asyncio.run(icons.load(api))

    sync(icons, api, folder)
    assert icons.jobs() == ["DRK", "NL"]  # BM removed, DRK and NL uploaded
    nl = icons.get("NL")

    sync(icons, api, folder)  # nothing changed: nothing re-uploaded
    assert icons.get("NL") == nl and len([e for e in api.emojis if not e.deleted]) == 2  # DRK + NL

    (folder / "NL.png").write_bytes(b"nl-v2")  # file changed: icon replaced
    sync(icons, api, folder)
    assert icons.get("NL") != nl

    (folder / "drk.PNG").unlink()
    sync(icons, api, folder)
    assert icons.jobs() == ["NL"]
    api.conn.close()


def test_missing_folder_leaves_icons_alone(tmp_path):
    api = FakeDiscord(existing=["class_DRK"])
    api.conn = db.connect(tmp_path / "sync.db")
    icons = ClassIcons()
    asyncio.run(icons.load(api))
    sync(icons, api, tmp_path / "does-not-exist")
    assert icons.jobs() == ["DRK"]
    api.conn.close()


def test_rejected_file_is_not_retried_until_it_changes(tmp_path):
    folder = tmp_path / "icons"
    folder.mkdir()
    (folder / "NL.png").write_bytes(b"x" * (256 * 1024 + 1))  # too big
    api = FakeDiscord()
    api.conn = db.connect(tmp_path / "sync.db")
    calls = []
    original = api.create_application_emoji

    async def counting(**kwargs):
        calls.append(kwargs["name"])
        return await original(**kwargs)

    api.create_application_emoji = counting
    icons = ClassIcons()
    sync(icons, api, folder)
    sync(icons, api, folder)
    assert icons.jobs() == [] and db.get_setting(api.conn, "class_icon_failed:NL")
    (folder / "NL.png").write_bytes(b"small")
    sync(icons, api, folder)
    assert icons.jobs() == ["NL"] and calls == ["class_NL"]
    api.conn.close()


@pytest.fixture
def bot(tmp_path):
    bot = MonkeyBot(dataclasses.replace(Config.from_env(), database_path=tmp_path / "icons.db"))
    db.set_squad_template(bot.conn, 1, 0, "12:00")
    for name, chars in [("zed", [("ZedMain", "DRK", "static")]), ("Amy", [("AmyMain", "NL", "static"), ("AmyAlt", "DRK", "sub")]),
                        ("bob", [("BobSub", "BM", "sub")])]:
        p = db.create_player(bot.conn, name=name, discord_handle=f"@{name.lower()}")
        for ign, job, status in chars:
            db.add_character(bot.conn, p.id, ign, job, 3.0, status)
        db.set_default_availability(bot.conn, p.id, {1: "Preferred"})
    yield bot
    bot.conn.close()


def test_squad_detail_is_alphabetical_with_icons(bot):
    api = FakeDiscord(existing=["class_DRK"])
    asyncio.run(bot.class_icons.load(api))
    drk = bot.class_icons.get("DRK")
    view = SquadBreakdownView(bot, WEEK, db.list_players(bot.conn, ("active",)), discord.Embed(title="o"))
    view.view_mode = "player"
    lines = view.squad_embed(1, "Preferred").fields[0].value.strip().split("\n")
    assert lines == [
        "**Amy**", "\u2003• AmyMain/NL", f"\u2003• ⏳ {drk} AmyAlt",  # static before sub
        "**bob**", "\u2003• ⏳ BobSub/BM",
        "**zed**", f"\u2003• {drk} ZedMain",
    ]


def test_squad_detail_pages_instead_of_truncating(bot, monkeypatch):
    import bot.squad_breakdown as breakdown

    monkeypatch.setattr(breakdown, "PAGE_MAX_CHARS", 40)  # force one player per page
    view = PrepRosterView(bot, WEEK, db.list_players(bot.conn, ("active",)))
    view.squad, view.level, view.view_mode = 1, "Preferred", "player"
    view._build()
    labels = [c.label for c in view.children if isinstance(c, discord.ui.Button)]
    assert labels[-3:] == ["◀", "▶", "Group by class"]

    class Response:
        async def edit_message(self, **kwargs):
            self.embed = kwargs["embed"]

    seen = []
    for _ in range(3):
        seen += [l for f in view.current_embed().fields for l in f.value.split("\n") if l.startswith("**")]
        asyncio.run(view._page_callback(1)(type("I", (), {"response": Response()})()))
    assert seen == ["**Amy**", "**bob**", "**zed**"]
    assert "Page 3/3" in view.current_embed().footer.text


def test_player_view_order_and_class_view(tmp_path):
    bot = MonkeyBot(dataclasses.replace(Config.from_env(), database_path=tmp_path / "sort.db"))
    db.set_squad_template(bot.conn, 1, 0, "12:00")
    pat = db.create_player(bot.conn, name="Pat", discord_handle="@pat")
    kim = db.create_player(bot.conn, name="Kim", discord_handle="@kim")
    for player, ign, job, status in [
        (pat, "zulu", "BM", "static"),     # Archer
        (pat, "Echo", "NL", "static"),     # Thief
        (pat, "delta", "DRK", "static"),   # Warrior
        (pat, "Alpha", "PAL", "static"),   # Warrior
        (pat, "bravo", "HERO", "sub"),     # Warrior, but a sub
        (pat, "Charlie", "FP", "sub"),     # Magician, sub
        (kim, "kilo", "DRK", "sub"),
        (kim, "Lima", "BM", "static"),
    ]:
        db.add_character(bot.conn, player.id, ign, job, 3.0, status)
        db.set_default_availability(bot.conn, player.id, {1: "Preferred"})
    view = PrepRosterView(bot, WEEK, [pat, kim])
    assert view.view_mode == "class"  # the default
    view.squad, view.level, view.view_mode = 1, "Preferred", "player"

    def lines():
        return view.current_embed().fields[0].value.strip().split("\n")

    # per player (A-Z); characters: static first, then Warrior/Magician/Archer/Thief/Pirate, then name
    assert lines() == [
        "**Kim**", "\u2003• Lima/BM", "\u2003• ⏳ kilo/DRK",
        "**Pat**", "\u2003• Alpha/PAL", "\u2003• delta/DRK", "\u2003• zulu/BM", "\u2003• Echo/NL",
        "\u2003• ⏳ bravo/HERO", "\u2003• ⏳ Charlie/FP",
    ]

    class Response:
        async def edit_message(self, **kwargs):
            pass

    asyncio.run(view._toggle_view_mode(type("I", (), {"response": Response()})()))
    # by class with role headings; a blank line only before each new role
    # page 1: HP / BSP / SI, page 2: CRIT, page 3: DPS (empty roles skipped)
    pages = [class_view_lines(view.squad_embed(1, "Preferred", p)) for p in range(3)]
    assert len(view.detail_pages(1, "Preferred")) == 3
    assert pages[0] == ["__**HP**__", "**DRK** (2)", "\u2003• `3.0b` - delta", "\u2003• ⏳ `3.0b` - kilo"]
    assert pages[1] == ["__**CRIT**__", "**BM** (2)", "\u2003• `3.0b` - Lima", "\u2003• `3.0b` - zulu"]
    assert pages[2] == [
        "__**DPS**__", "**FP** (1)", "\u2003• ⏳ `3.0b` - Charlie",
        "**HERO** (1)", "\u2003• ⏳ `3.0b` - bravo",
        "**NL** (1)", "\u2003• `3.0b` - Echo",
        "**PAL** (1)", "\u2003• `3.0b` - Alpha",
    ]
    assert [c.label for c in view.children if isinstance(c, discord.ui.Button)][-1] == "Group by player"
    assert "Grouped by class" in view.current_embed().footer.text
    bot.conn.close()


def test_class_view_shows_icon_and_splits_long_classes(tmp_path):
    bot = MonkeyBot(dataclasses.replace(Config.from_env(), database_path=tmp_path / "long.db"))
    asyncio.run(bot.class_icons.load(FakeDiscord(existing=["class_BSP"])))
    bsp = bot.class_icons.get("BSP")
    db.set_squad_template(bot.conn, 1, 0, "12:00")
    players = []
    for i in range(250):
        p = db.create_player(bot.conn, name=f"Player{i:03d}", discord_handle=f"@p{i}")
        db.add_character(bot.conn, p.id, f"Bishop{i:03d}", "BSP", 2.0, "static")
        db.set_default_availability(bot.conn, p.id, {1: "Preferred"})
        players.append(p)
    view = SquadBreakdownView(bot, WEEK, players, discord.Embed(title="o"))
    view.squad, view.level, view.view_mode = 1, "Preferred", "class"
    view.showing_overview = False
    pages = [view.squad_embed(1, "Preferred", p) for p in range(len(view.detail_pages(1, "Preferred")))]
    text = "\n".join(e.description for e in pages)
    assert class_view_lines(pages[0])[:3] == ["__**BSP**__", f"{bsp} **BSP** (250)", "\u2003• `2.0b` - Bishop000"]
    assert "(cont.)" in text  # the class is split rather than cut off
    assert text.count(" - Bishop") == 250 and len(pages) > 1 and all(len(e.description) <= 4096 and len(e) <= 6000 for e in pages)
    bot.conn.close()


def test_class_view_order_for_all_roster_classes():
    from bot.squad_breakdown import _class_view_key

    jobs = ["ARAN", "BM", "BSP", "BUCC", "BW", "DB", "DRK", "DW", "EVAN", "FP",
            "HERO", "IL", "MM", "NL", "NW", "PAL", "SAIR", "SHAD", "TB", "WA"]
    assert sorted(jobs, key=_class_view_key) == [
        "DRK", "ARAN",                                  # HP
        "BSP",                                          # BSP
        "BUCC", "TB",                                   # SI
        "DB", "BM", "MM", "WA",                         # CRIT
        "BW", "DW", "EVAN", "FP", "HERO", "IL", "NL", "NW", "PAL", "SAIR", "SHAD",  # DPS, A-Z
    ]


def test_damage_text():
    from bot.squad_breakdown import _dmg_text

    cases = {3.08: "3.0b", 4.39: "4.3b", 2.7: "2.7b", 4.0: "4.0b", 4.3: "4.3b", 3.999: "3.9b", 0.05: "0.0b", None: "?"}
    assert {d: _dmg_text({"dmg": d}) for d in cases} == cases  # always rounded down, one decimal


def test_class_view_orders_by_status_then_damage(tmp_path):
    bot = MonkeyBot(dataclasses.replace(Config.from_env(), database_path=tmp_path / "dmg.db"))
    db.set_squad_template(bot.conn, 1, 0, "12:00")
    p = db.create_player(bot.conn, name="Pat", discord_handle="@pat")
    for ign, dmg, status in [("low", 2.1, "static"), ("high", 4.39, "static"), ("none", None, "static"),
                             ("subtop", 6.0, "sub"), ("mid", 3.5, "static"), ("sublow", 1.0, "sub")]:
        db.add_character(bot.conn, p.id, ign, "DRK", dmg, status)
    db.set_default_availability(bot.conn, p.id, {1: "Preferred"})
    view = SquadBreakdownView(bot, WEEK, [p], discord.Embed(title="o"))
    view.squad, view.level = 1, "Preferred"
    view.showing_overview = False
    assert class_view_lines(view.current_embed())[2:] == [
        "\u2003• `4.3b` - high",
        "\u2003• `3.5b` - mid",
        "\u2003• `2.1b` - low",
        "\u2003• `?` - none",          # no damage: last among statics
        "\u2003• ⏳ `6.0b` - subtop",     # subs after all statics, even with higher damage
        "\u2003• ⏳ `1.0b` - sublow",
    ]
    bot.conn.close()


def test_buttons_order_and_selected_tab(bot):
    view = SquadBreakdownView(bot, WEEK, db.list_players(bot.conn, ("active",)), discord.Embed(title="Overview"))

    def tabs():
        return [(b.label, b.style) for b in view.children if isinstance(b, discord.ui.Button)][:3]

    blue, grey = discord.ButtonStyle.primary, discord.ButtonStyle.secondary
    assert tabs() == [("Overview", blue), ("Preferred", grey), ("Available", grey)]  # opens on the overview
    assert view.current_embed().title == "Overview"

    class Response:
        async def edit_message(self, **kwargs):
            self.embed = kwargs["embed"]

    interaction = type("I", (), {"response": Response()})()
    asyncio.run(view._level_callback("Available")(interaction))
    assert tabs() == [("Overview", grey), ("Preferred", grey), ("Available", blue)]
    asyncio.run(view._show_overview(interaction))
    assert tabs() == [("Overview", blue), ("Preferred", grey), ("Available", grey)]
    assert interaction.response.embed.title == "Overview"


def test_class_pages_follow_fixed_groups():
    from bot.squad_breakdown import PAGE_MAX_CHARS, PAGE_MAX_LINES, _class_pages

    def block(job, n):
        return f"**{job}** ({n})" + "".join(f"\n\u2003• `3.0b` - {job}{i:02d}" for i in range(n))

    small = [("HP", block("DRK", 3)), ("BSP", block("BSP", 2)), ("SI", block("BUCC", 2)),
             ("CRIT", block("BM", 3)), ("DPS", block("NL", 3))]
    pages = _class_pages(small)
    assert [p.split("\n")[0] for p in pages] == ["__**HP**__", "__**CRIT**__", "__**DPS**__"]
    assert "__**BSP**__" in pages[0] and "__**SI**__" in pages[0] and "\n\n__**SI**__" in pages[0]

    no_crit = _class_pages([("HP", block("DRK", 3)), ("DPS", block("NL", 3))])
    assert [p.split("\n")[0] for p in no_crit] == ["__**HP**__", "__**DPS**__"]  # empty page skipped

    big = [("HP", block("DRK", 60)), ("SI", block("TB", 40)), ("CRIT", block("BM", 5)),
           ("DPS", block("FP", 60)), ("DPS", block("NL", 60))]
    pages = _class_pages(big)
    assert all(len(p) <= PAGE_MAX_CHARS and p.count("\n") + 1 <= PAGE_MAX_LINES for p in pages)
    assert any(p.startswith("__**SI**__") or "\n__**SI**__" in p for p in pages)
    assert any(p.startswith("__**DPS**__ (cont.)") for p in pages)  # overflow continues, nothing lost
    assert "\n".join(pages).count("• ") == 225
