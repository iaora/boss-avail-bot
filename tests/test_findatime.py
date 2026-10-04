"""/findatime: reading availability, the hour grid, best times, and the poll's buttons (no network)."""

import asyncio
import dataclasses
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import discord
import pytest

from bot import db, findatime
from bot.app import MonkeyBot
from bot.cogs.findatime import FindATimeButton, FindATimeCog, JoinModal, JoinPreview, ScheduleView, poll_message
from bot.config import Config
from bot.views import set_user_timezone

FRIDAY = date(2026, 10, 9)
DATES = findatime.poll_dates(FRIDAY)  # Fri 10/9 .. Thu 10/15
ET = ZoneInfo("America/New_York")


def read(text, today=FRIDAY):
    """{day label: [hours]} for what the parser reads from `text`."""
    parsed = findatime.parse(text, DATES, today)
    out = {}
    for d, h in sorted(parsed.slots):
        out.setdefault(findatime.day_label(d), []).append(h)
    return out, parsed


# --------------------------------------------------------------------------- parsing


@pytest.mark.parametrize("text, expected", [
    ("Friday 5-7PM", {"Fri 10/9": [17, 18]}),
    ("fri 5pm-7pm", {"Fri 10/9": [17, 18]}),
    ("Fri 17:00-19:00", {"Fri 10/9": [17, 18]}),
    ("Fri 5 to 7 pm", {"Fri 10/9": [17, 18]}),
    ("Sat 11-1pm", {"Sat 10/10": [11, 12]}),  # 11 AM to 1 PM
    ("Sat 10am-noon", {"Sat 10/10": [10, 11]}),
    ("Sat after 9pm", {"Sat 10/10": [21, 22, 23]}),
    ("Sat before 2am", {"Sat 10/10": [0, 1]}),
    ("Sun all day", {"Sun 10/11": list(range(24))}),
    ("Sun", {"Sun 10/11": list(range(24))}),  # a day with no time: the whole day
    ("tomorrow morning", {"Sat 10/10": list(range(6, 12))}),
    ("Oct 12 8-9pm", {"Mon 10/12": [20]}),
    ("10/12 8-9pm", {"Mon 10/12": [20]}),
    ("Sat and Sun 2-4pm", {"Sat 10/10": [14, 15], "Sun 10/11": [14, 15]}),
    ("Fri 5-7pm and Sat 1-3pm", {"Fri 10/9": [17, 18], "Sat 10/10": [13, 14]}),  # each day its own time
    ("Fri 1-3pm, 6-8pm", {"Fri 10/9": [13, 14, 18, 19]}),  # the time carries the previous day
    ("Mon-Wed 9-10pm", {"Mon 10/12": [21], "Tue 10/13": [21], "Wed 10/14": [21]}),
    ("weekends 1-2pm", {"Sat 10/10": [13], "Sun 10/11": [13]}),
    ("Thu 11pm-1am", {"Thu 10/15": [23]}),  # past the poll's last date is dropped
    ("Wed 11pm-1am", {"Wed 10/14": [23], "Thu 10/15": [0]}),  # overnight spills into the next day
])
def test_parse(text, expected):
    got, parsed = read(text)
    assert got == expected and not parsed.unread


def test_parse_flags_what_it_cant_read_or_assumes():
    got, parsed = read("Fri 5-7pm, sometime maybe, 6-9pm")
    assert got == {"Fri 10/9": [17, 18, 19, 20]}  # 6-9pm carries Friday from the part before
    assert parsed.unread == ["sometime maybe"]

    got, parsed = read("8-10")  # a time with no day at all
    assert got == {} and parsed.unread == ["8-10"]

    got, parsed = read("Fri 8-10")  # no am/pm: read as PM, and the user is told
    assert got == {"Fri 10/9": [20, 21]} and "as PM" in parsed.notes[0]

    got, parsed = read("Oct 30 5-6pm")  # outside the poll
    assert got == {} and "isn't in this poll's dates" in parsed.notes[0]


# --------------------------------------------------------------------------- grid and best times


def test_grid_is_drawn_in_each_viewers_timezone():
    hours = findatime.to_epoch_hours({(FRIDAY, 17), (FRIDAY, 18)}, ET)  # Fri 5-7 PM Eastern
    row = findatime.squares(hours, FRIDAY, ET)
    assert row.count(findatime.FREE) == 2 and len(row.split(" ")) == 4  # 4 groups of 6 hours
    assert row.split(" ")[2] == "⬛⬛⬛⬛⬛🟩" and row.split(" ")[3].startswith("🟩")
    assert findatime.day_ranges(hours, FRIDAY, ET) == "5 PM-7 PM"
    sydney = ZoneInfo("Australia/Sydney")  # the same hours are Saturday morning there
    assert findatime.day_ranges(hours, FRIDAY + timedelta(days=1), sydney) == "8 AM-10 AM"
    header, day, row = findatime.grid(hours, DATES[:1], ET).split("\n")
    assert header.startswith("12-6AM") and header.endswith("6-12AM")  # labels over the 4 groups
    assert day == "**Fri 10/9** · 5 PM-7 PM"
    assert row == findatime.squares(hours, FRIDAY, ET)  # squares on their own line, from the left edge
    whole = findatime.to_epoch_hours({(FRIDAY, h) for h in range(24)}, ET)
    assert findatime.day_ranges(whole, FRIDAY, ET) == "all day"
    late = findatime.to_epoch_hours({(FRIDAY, 22), (FRIDAY, 23)}, ET)
    assert findatime.day_ranges(late, FRIDAY, ET) == "10 PM-12 AM"


def test_best_windows_most_people_then_longest_then_soonest():
    h = lambda day, hour: findatime.local_hour(DATES[day], hour, ET)  # noqa: E731
    availability = {
        1: {h(0, 17), h(0, 18), h(0, 19), h(1, 20)},
        2: {h(0, 18), h(0, 19), h(1, 20)},
        3: {h(0, 19), h(1, 20), h(1, 21)},
    }
    windows = findatime.best_windows(availability, from_hour=0)
    assert [(w.start, w.end, set(w.people)) for w in windows[:3]] == [
        (h(0, 19), h(0, 20), {1, 2, 3}),  # all three, soonest of the 1-hour ones
        (h(1, 20), h(1, 21), {1, 2, 3}),
        (h(0, 18), h(0, 19), {1, 2}),
    ]
    assert findatime.best_windows(availability, from_hour=h(1, 21)) == [findatime.Window(h(1, 21), h(1, 22), frozenset({3}))]


# --------------------------------------------------------------------------- the poll


class Response:
    def __init__(self):
        self.sent, self.edited, self.modal = None, None, None

    async def send_message(self, content=None, **kwargs):
        self.sent = {"content": content, **kwargs}

    async def edit_message(self, **kwargs):
        self.edited = kwargs

    async def send_modal(self, modal):
        self.modal = modal


class Followup:
    def __init__(self):
        self.sent = []

    async def send(self, content=None, **kwargs):
        self.sent.append({"content": content, **kwargs})


class PollMessage:
    def __init__(self):
        self.edits = []
        self.channel = SimpleNamespace(id=10)
        self.id = 20

    async def edit(self, **kwargs):
        self.edits.append(kwargs)


NOW = datetime(2026, 10, 9, 12, tzinfo=timezone.utc)  # Friday 10/9, 8 AM Eastern / 8 PM Singapore


@pytest.fixture
def bot(tmp_path, monkeypatch):
    from bot import timeutil

    monkeypatch.setattr(timeutil, "now_utc", lambda: NOW)  # polls start "now"; times are judged against it
    bot = MonkeyBot(dataclasses.replace(Config.from_env(), database_path=tmp_path / "fat.db"))
    message = PollMessage()
    bot.get_channel = lambda _id: SimpleNamespace(get_partial_message=lambda _mid: message)
    bot.poll_message = message
    yield bot
    bot.conn.close()


def user(user_id, name):
    return SimpleNamespace(id=user_id, name=name.lower(), display_name=name)


def run(callback, who=None, **extra):
    i = SimpleNamespace(response=Response(), followup=Followup(), user=who or user(1, "Robin"), **extra)
    asyncio.run(callback(i))
    return i


def start_poll(bot, title="Lotus"):
    async def original_response():
        return bot.poll_message

    i = run(lambda i: FindATimeCog.findatime.callback(FindATimeCog(bot), i, title=title),
            original_response=original_response)
    poll = db.get_poll(bot.conn, 1)
    return i.response.sent, poll


def press(bot, poll, action, who=None):
    return run(FindATimeButton(action, poll.id).callback, who=who, client=bot)


def join(bot, poll, who, zone, text):
    """Join (or, if already in, Edit from your private entry) and confirm `text`."""
    from bot.cogs.findatime import MyEntryView

    set_user_timezone(bot.conn, who.id, zone)
    pressed = press(bot, poll, "join", who)
    if pressed.response.modal is None:  # already joined: your hours, with Edit
        assert isinstance(pressed.response.sent["view"], MyEntryView)
        modal = run(pressed.response.sent["view"].children[0].callback, who=who).response.modal
        modal.text._value = text
        preview = run(modal.on_submit, who=who).response.edited["view"]
    else:
        modal = pressed.response.modal
        modal.text._value = text
        preview = run(modal.on_submit, who=who).response.sent["view"]
    run(preview._confirm, who=who)
    return preview


def test_findatime_posts_a_public_poll(bot):
    sent, poll = start_poll(bot)
    assert "ephemeral" not in sent  # everyone sees it
    assert sent["embed"].title == "🗓️ Find a time: Lotus"
    assert sent["embed"].fields[0].value == "*No one yet*"
    # only buttons anyone can use; leaving, proposing and closing are in private messages
    assert [c.item.label for c in sent["view"].children] == ["Join/Edit", "See schedule", "Best time"]
    assert (poll.channel_id, poll.message_id, poll.title) == (10, 20, "Lotus")


def test_join_asks_for_a_timezone_first(bot):
    from bot.views import TimezoneSelect

    _, poll = start_poll(bot)
    i = press(bot, poll, "join")
    [select] = i.response.sent["view"].children
    assert isinstance(select, TimezoneSelect) and i.response.modal is None
    select._values = ["America/New_York"]
    picked = run(select.callback, client=bot)
    assert isinstance(picked.response.modal, JoinModal)


def test_join_preview_confirm_and_the_poll_updates(bot):
    _, poll = start_poll(bot)
    set_user_timezone(bot.conn, 1, "America/New_York")
    modal = press(bot, poll, "join").response.modal
    modal.text._value = f"{poll.starts_on:%a} 5-7pm, gibberish"
    sent = run(modal.on_submit).response.sent
    preview = sent["view"]
    assert sent["ephemeral"] and isinstance(preview, JoinPreview)
    assert "5 PM-7 PM" in sent["embed"].description  # compact by default: the hours in words
    assert "gibberish" in sent["embed"].fields[0].value  # flagged, not saved
    assert db.poll_entries(bot.conn, poll.id) == []  # nothing saved until Confirm

    edit = run(preview._edit).response.modal  # Edit reopens the box with what was typed
    assert isinstance(edit, JoinModal) and edit.text.default.endswith("gibberish") and edit.from_preview

    confirmed = run(preview._confirm)
    assert confirmed.response.edited["content"].startswith("✅ You're in")
    [entry] = db.poll_entries(bot.conn, poll.id)
    assert entry.name == "Robin" and len(entry.hours) == 2
    assert bot.poll_message.edits[-1]["embed"].fields[0].name == "Joined (1)"  # the public list updated
    assert bot.poll_message.edits[-1]["embed"].fields[0].value == "Robin"


def test_preview_with_nothing_readable_cant_be_confirmed(bot):
    _, poll = start_poll(bot)
    preview = JoinPreview(bot, poll, ET, "whenever")
    assert preview.children[0].label == "Confirm" and preview.children[0].disabled


def test_join_again_shows_your_hours_with_edit_and_leave(bot):
    from bot.cogs.findatime import MyEntryView

    _, poll = start_poll(bot)
    join(bot, poll, user(1, "Robin"), "America/New_York", "Sun all day")
    sent = press(bot, poll, "join").response.sent
    view = sent["view"]
    assert sent["ephemeral"] and isinstance(view, MyEntryView)
    assert [b.label for b in view.children] == ["Edit", "Leave poll", "Grid view"]
    assert "Sun all day" in sent["embed"].description

    left = run(view.children[1].callback)
    assert left.response.edited["content"] == "➖ You've left this poll."
    assert db.poll_entries(bot.conn, poll.id) == []
    assert bot.poll_message.edits[-1]["embed"].fields[0].value == "*No one yet*"


def test_remove(bot):
    _, poll = start_poll(bot)
    assert press(bot, poll, "leave").response.sent["content"] == "You haven't joined this poll."
    join(bot, poll, user(1, "Robin"), "America/New_York", "Sun all day")
    i = press(bot, poll, "leave")
    assert i.response.edited["embed"].fields[0].value == "*No one yet*"  # the poll message updated in place
    assert i.followup.sent[0]["content"].startswith("➖ You've been removed")


def test_see_schedule_groups_by_day_in_the_viewers_timezone(bot):
    _, poll = start_poll(bot)
    day = poll.starts_on.strftime("%a")
    join(bot, poll, user(1, "Robin"), "America/New_York", f"{day} 5-7pm")
    join(bot, poll, user(2, "Kim"), "America/Los_Angeles", f"{day} 2-4pm")  # = 5-7 PM Eastern
    set_user_timezone(bot.conn, 3, "America/New_York")
    sent = press(bot, poll, "schedule", user(3, "Viewer")).response.sent
    view = sent["view"]
    assert sent["ephemeral"] and isinstance(view, ScheduleView)
    grid = run(view.children[-1].callback, who=user(3, "Viewer")).response.edited  # 🖥️ Grid view
    header, *rows = grid["embed"].description.split("\n")
    assert "12-6AM" in header and "6-12PM" in header
    assert rows[0::2] == ["**Robin**", "**Kim**"]  # each name on its own line...
    assert rows[1] == rows[3] and rows[1].count(findatime.FREE) == 2  # ...over the same hours, lined up
    assert view.children[0].options[0].description == "2 of 2 free at some point"

    nxt = run(view.children[2].callback)  # ▶ next day
    assert findatime.FREE not in nxt.response.edited["embed"].description


def test_best_time_lists_the_overlap(bot):
    _, poll = start_poll(bot)
    day = poll.starts_on.strftime("%a")
    join(bot, poll, user(1, "Robin"), "America/New_York", f"{day} 5-8pm")
    join(bot, poll, user(2, "Kim"), "America/New_York", f"{day} 6-9pm")
    embed = press(bot, poll, "best", user(3, "Viewer")).response.sent["embed"]
    first = embed.description.split("\n")[0]
    assert "(2h) · **2/2** free: everyone" in first and first.startswith("**1. <t:")


def test_close_is_only_offered_to_the_creator_or_a_host(bot):
    _, poll = start_poll(bot)
    bot.is_host = lambda interaction: False
    assert "view" not in press(bot, poll, "best", user(2, "Kim")).response.sent  # no controls for others
    # an old poll message's Close button still checks
    refused = press(bot, poll, "close", user(2, "Kim"))
    assert refused.response.sent["content"].startswith("Only the poll's creator") and not db.get_poll(bot.conn, 1).closed

    controls = press(bot, poll, "best", user(1, "Robin")).response.sent["view"]  # the creator
    close = next(c for c in controls.children if getattr(c, "label", None) == "Close poll")
    closed = run(close.callback, who=user(1, "Robin"))
    assert db.get_poll(bot.conn, 1).closed and closed.response.edited["content"].startswith("🔒 Closed")
    buttons = {c.item.label: c.item.disabled for c in bot.poll_message.edits[-1]["view"].children}
    assert buttons == {"Join/Edit": True, "See schedule": False, "Best time": False}  # the public message updated
    assert press(bot, poll, "join").response.sent["content"] == "This poll is closed."
    reopened = press(bot, poll, "best", user(1, "Robin")).response.sent["view"]
    assert "Close poll" not in [getattr(c, "label", None) for c in reopened.children]


def test_poll_message_lists_people_in_join_order(bot):
    _, poll = start_poll(bot)
    for uid, name in [(1, "Robin"), (2, "Kim"), (3, "Pat")]:
        join(bot, poll, user(uid, name), "America/New_York", "Sun all day")
    join(bot, poll, user(1, "Robin"), "America/New_York", "Sat all day")  # re-joining keeps your place
    embed, _ = poll_message(bot, db.get_poll(bot.conn, poll.id))
    assert embed.fields[0].name == "Joined (3)" and embed.fields[0].value == "Robin, Kim, Pat"


# --------------------------------------------------------------------------- proposing a time


class Channel:
    def __init__(self):
        self.posts = []

    async def send(self, **kwargs):
        self.posts.append(kwargs)
        return SimpleNamespace(channel=SimpleNamespace(id=10), id=30, jump_url="https://discord.test/proposal")


@pytest.fixture
def proposing(bot):
    channel = Channel()
    message = bot.poll_message
    bot.get_channel = lambda _id: SimpleNamespace(get_partial_message=lambda _mid: message, send=channel.send)
    bot.is_host = lambda interaction: False
    _, poll = start_poll(bot)
    day = poll.starts_on.strftime("%a")
    join(bot, poll, user(1, "Robin"), "America/New_York", f"{day} 5-8pm")
    join(bot, poll, user(2, "Kim"), "America/New_York", f"{day} 6-9pm")
    return poll, channel


def test_only_the_creator_or_a_host_can_propose(bot, proposing):
    from bot.cogs.findatime import ProposeView

    poll, _ = proposing
    assert "view" not in press(bot, poll, "best", user(2, "Kim")).response.sent  # just the list
    sent = press(bot, poll, "best", user(1, "Robin")).response.sent  # the creator
    view = sent["view"]
    assert isinstance(view, ProposeView)
    assert [b.label for b in view.children] == ["Propose a time", "Close poll"]


def test_proposal_pings_everyone_who_joined_and_tracks_answers(bot, proposing):
    from bot.cogs.findatime import ProposalButton

    poll, channel = proposing
    robin = user(1, "Robin")
    view = press(bot, poll, "best", robin).response.sent["view"]
    modal = run(view.children[0].callback, who=robin).response.modal
    # the best times are listed at the top, in the host's time
    assert modal.children[0].content.split("\n")[1] == "1. Fri 10/9 6 PM-8 PM · 2/2 free"
    modal.text._value = f"{poll.starts_on:%a} 6pm"
    check = run(modal.on_submit, who=robin).response.sent
    assert check["ephemeral"] and check["content"].startswith("📣 Propose **Fri 10/9 6 PM** (your time) = <t:")
    assert channel.posts == []  # nobody is pinged until Send
    assert ":R>) to the 2 people in the poll?" in check["content"]  # also "in N days"
    sent = run(check["view"].children[0].callback, who=robin).response.edited
    assert sent["content"].startswith("📣 Proposed <t:") and ":R>) and pinged 2 people" in sent["content"]
    assert sent["view"] is None

    [post] = channel.posts
    assert post["content"] == "<@1> <@2>"  # everyone who joined is pinged...
    assert [u.id for u in post["allowed_mentions"].users] == [1, 2] and post["allowed_mentions"].everyone is False
    embed = post["embed"]
    assert embed.title == "📣 Proposed time: Lotus" and "Proposed by <@1>" in embed.description
    assert [c.item.label for c in post["view"].children] == ["Confirm", "Can't make it", "Propose different time"]
    proposal = db.get_proposal(bot.conn, 1)
    assert (proposal.message_id, proposal.end_hour - proposal.start_hour) == (30, 1)  # a start time
    assert ":F>**" in embed.description and " – " not in embed.description.split("\n")[0]

    def answer(action, who, **extra):
        return run(ProposalButton(action, proposal.id).callback, who=who, client=bot, **extra)

    yes = answer("yes", user(1, "Robin"))
    fields = {f.name: f.value for f in yes.response.edited["embed"].fields}
    assert fields["✅ Confirmed (1)"] == "Robin" and fields["⏳ Waiting on (1)"] == "Kim"

    other = answer("other", user(2, "Kim"))  # Kim suggests a different time
    modal = other.response.modal
    modal.text._value = f"{poll.starts_on:%a} 9-10pm"
    submitted = run(modal.on_submit, who=user(2, "Kim"))
    fields = {f.name: f.value for f in submitted.response.edited["embed"].fields}
    assert fields["🔁 Suggested another time (1)"].startswith("Kim: <t:")
    assert fields["⏳ Waiting on (0)"] == "*No one*"

    no = answer("no", user(2, "Kim"))  # changing your answer replaces it
    fields = {f.name: f.value for f in no.response.edited["embed"].fields}
    assert fields["❌ Can't make it (1)"] == "Kim" and "🔁 Suggested another time (1)" not in fields


def test_only_people_in_the_poll_can_answer_a_proposal(bot, proposing):
    from bot.cogs.findatime import ProposalButton

    poll, _ = proposing
    proposal = db.create_proposal(bot.conn, poll.id, 1, 100, 102)
    outsider = run(ProposalButton("yes", proposal.id).callback, who=user(9, "Stranger"), client=bot)
    assert outsider.response.sent["content"].startswith("Only people who joined the poll can answer")
    assert db.proposal_responses(bot.conn, proposal.id) == []


def test_proposed_time_is_parsed_and_checked_before_sending(bot, proposing):
    poll, channel = proposing
    robin = user(1, "Robin")
    view = press(bot, poll, "best", robin).response.sent["view"]
    modal = run(view.children[0].callback, who=robin).response.modal
    modal.text._value = "sometime"
    unreadable = run(modal.on_submit, who=robin).response.sent
    assert "couldn't read a day and time" in unreadable["content"]
    assert unreadable["view"].children[0].label == "Send" and unreadable["view"].children[0].disabled

    edit = run(unreadable["view"].children[1].callback, who=robin).response.modal  # ✏️ Edit
    assert edit.text.default == "sometime" and edit.from_check
    edit.text._value = f"{poll.starts_on:%a} 9-11pm"
    check = run(edit.on_submit, who=robin).response.edited  # the same message updates
    assert "**Fri 10/9 9 PM-11 PM** (your time)" in check["content"]
    run(check["view"].children[0].callback, who=robin)
    proposal = db.get_proposal(bot.conn, 1)
    assert len(channel.posts) == 1 and proposal.end_hour - proposal.start_hour == 2


def test_first_block_and_window_label():
    assert findatime.first_block(set()) is None
    assert findatime.first_block({10, 11, 12, 20, 21}) == (10, 13)
    start = findatime.local_hour(FRIDAY, 17, ET)
    assert findatime.window_label(start, start + 2, ET) == "Fri 10/9 5 PM-7 PM"



# --------------------------------------------------------------------------- times that have passed


def test_a_day_already_over_for_you_means_next_week(bot):
    """At NOW it's Friday 8 AM in New York but Friday 8 PM in Singapore: "fri 3-6pm" is over there."""
    _, poll = start_poll(bot)
    sg = ZoneInfo("Asia/Singapore")
    preview = JoinPreview(bot, poll, sg, "fri 3-6pm")
    [(day, _)] = {(d, 0) for d, h in preview.parsed.slots}
    assert day == date(2026, 10, 16)  # next Friday, still inside the poll's 7 days
    assert len(preview.hours) == 3 and not preview.parsed.notes

    ny = JoinPreview(bot, poll, ET, "fri 3-6pm")  # still ahead in New York: today
    assert {d for d, h in ny.parsed.slots} == {FRIDAY}


def test_past_hours_are_left_out_with_a_note(bot):
    _, poll = start_poll(bot)
    preview = JoinPreview(bot, poll, ET, "today all day")  # it's 8 AM: 12-8 AM has passed
    assert sorted(h for d, h in preview.parsed.slots) == list(range(8, 24))
    assert "8 hour(s) you typed have already passed" in preview.parsed.notes[0]


def test_past_hours_are_greyed_out(bot):
    _, poll = start_poll(bot)
    join(bot, poll, user(1, "Robin"), "America/New_York", "Sat all day")
    set_user_timezone(bot.conn, 3, "America/New_York")
    view = press(bot, poll, "schedule", user(3, "Viewer")).response.sent["view"]
    view.compact = False  # the grid
    today = view.embed().description.split("\n")[2]  # Friday: header, name, squares
    assert today.startswith(findatime.PAST * 6 + " " + findatime.PAST * 2 + findatime.BUSY)  # 12-8 AM passed


def test_best_time_explains_overlaps_that_already_passed(bot):
    _, poll = start_poll(bot)
    now = int(NOW.timestamp()) // 3600
    db.set_poll_entry(bot.conn, poll.id, 1, "Robin", "x", {now - 3, now - 2, now + 5})
    db.set_poll_entry(bot.conn, poll.id, 2, "Kim", "x", {now - 3, now - 2, now + 9})
    text = press(bot, poll, "best", user(3, "Viewer")).response.sent["embed"].description
    assert text.startswith("**No time ahead works for 2 or more people yet.**")
    assert "2 hour(s) when 2+ people were free have already passed" in text



# --------------------------------------------------------------------------- compact view (phones)


def test_schedule_starts_compact_with_a_grid_toggle(bot):
    _, poll = start_poll(bot)
    join(bot, poll, user(1, "Robin"), "America/New_York", "Fri 5-7pm")
    join(bot, poll, user(2, "Kim"), "America/New_York", "Fri 6-9pm")
    set_user_timezone(bot.conn, 3, "America/New_York")
    sent = press(bot, poll, "schedule", user(3, "Viewer")).response.sent
    view = sent["view"]
    assert view.compact  # every schedule starts compact
    assert sent["embed"].description.split("\n") == [
        "**Robin** · 5 PM-7 PM", "**Kim** · 6 PM-9 PM", "", "🤝 **2+ free:** 6 PM-7 PM"
    ]
    assert findatime.FREE not in sent["embed"].description
    assert sent["embed"].footer.text.startswith("Day 1/8 · Free hours still ahead")
    toggle = view.children[-1]
    assert toggle.label == "Grid view"

    shown = run(toggle.callback, who=user(3, "Viewer")).response.edited
    assert shown["embed"].description.count(findatime.FREE) == 5  # Robin 2 + Kim 3 squares
    assert shown["view"].children[-1].label == "Compact view"

    again = press(bot, poll, "schedule", user(3, "Viewer")).response.sent["view"]
    assert again.compact  # not remembered: back to compact


def test_join_preview_starts_compact(bot):
    _, poll = start_poll(bot)
    preview = JoinPreview(bot, poll, ET, "Fri 5-7pm, Sun all day")
    assert [b.label for b in preview.children] == ["Confirm", "Edit", "Grid view"]
    lines = preview.embed().description.split("\n\n", 1)[1].split("\n")
    assert lines == ["**Fri 10/9** · 5 PM-7 PM", "**Sun 10/11** · all day"]
    grid = run(preview.children[2].callback).response.edited  # 🖥️ Grid view
    assert grid["embed"].description.count(findatime.FREE) == 2 + 24
    assert findatime.compact(set(), DATES, ET) == "*No free hours still ahead.*"
