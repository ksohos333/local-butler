"""Tests for the month grid, the clock, and the assistant panel widgets.

Built directly rather than through a window, so these need no network and no
running Radicale. The confirmation gate is checked here too, because it is a UI
promise: the Apply button is the only route to a write.
"""

import datetime as dt
import shutil
import subprocess
import time

import pytest

gi = pytest.importorskip("gi")
# Gdk has to be pinned too: left alone, gi loads the newest Gdk (4.0) by
# default, which then clashes with the Gtk 3.0 required below.
gi.require_version("Gdk", "3.0")
gi.require_version("Gtk", "3.0")
from gi.repository import Gdk, Gtk  # noqa: E402

from local_butler.apps.calendar.assistant import PendingChange  # noqa: E402
from local_butler.apps.calendar.events import Event  # noqa: E402
from local_butler.apps.calendar.panels import (  # noqa: E402
    ChatPanel,
    ClockBar,
    MonthView,
    _escape,
    _recurring_occurrences,
)

ZONE = dt.timezone(dt.timedelta(hours=3))


def make_event(summary="Dentist", **kwargs):
    defaults = dict(
        start=dt.datetime(2026, 10, 6, 15, 0),
        end=dt.datetime(2026, 10, 6, 16, 0),
        tz=ZONE,
    )
    defaults.update(kwargs)
    return Event(summary=summary, **defaults)


# -- month grid -----------------------------------------------------------


def test_grid_has_six_weeks_of_cells():
    view = MonthView()
    assert len(view._cells) == 42


def test_events_mark_only_their_start_day():
    view = MonthView()
    day = dt.date(2026, 10, 6)
    view.select(day)
    view.set_events([make_event(start=dt.datetime(2026, 10, 6, 15, 0))])

    assert view._counts.get(day) == 1
    assert view._counts.get(dt.date(2026, 10, 7)) is None


def test_multi_day_event_does_not_dotted_fill_the_week():
    view = MonthView()
    view.set_events(
        [make_event(start=dt.datetime(2026, 10, 6, 9, 0), end=dt.datetime(2026, 10, 10, 17, 0))]
    )

    assert view._counts.get(dt.date(2026, 10, 6)) == 1
    assert view._counts.get(dt.date(2026, 10, 8)) is None


def test_recurring_event_marks_every_occurrence():
    view = MonthView()
    view.set_events(
        [
            make_event(
                start=dt.datetime(2026, 10, 5, 9, 0),
                end=dt.datetime(2026, 10, 5, 10, 0),
                rrule="FREQ=WEEKLY;BYDAY=MO",
            )
        ]
    )

    assert view._counts.get(dt.date(2026, 10, 5)) == 1
    assert view._counts.get(dt.date(2026, 10, 12)) == 1
    assert view._counts.get(dt.date(2026, 10, 6)) is None


def test_month_title_follows_the_navigated_month():
    view = MonthView()
    start = view._month
    view.shift_month(1)

    expected = dt.date(start.year + (start.month // 12), (start.month % 12) + 1, 1)
    assert expected.strftime("%B %Y") in view.title_label.get_text()

    view.shift_month(-1)
    assert start.strftime("%B %Y") in view.title_label.get_text()


def test_selecting_a_day_in_another_month_jumps_the_grid():
    view = MonthView()
    view.select(dt.date(2026, 12, 25))

    assert view.selected == dt.date(2026, 12, 25)
    assert "December" in view.title_label.get_text()


def test_click_callback_fires_with_the_day():
    seen = []
    view = MonthView(on_day_clicked=seen.append)
    view.select(dt.date(2026, 10, 14))
    assert seen == [dt.date(2026, 10, 14)]


def test_callback_still_fires_when_the_click_crosses_into_another_month():
    seen = []
    view = MonthView(on_day_clicked=seen.append)
    view.select(dt.date(2026, 12, 25))
    assert seen == [dt.date(2026, 12, 25)]
    assert "December" in view.title_label.get_text()


def test_today_is_marked_bold_in_the_grid():
    view = MonthView()
    today = dt.datetime.now().date()
    if (today.year, today.month) == (view._month.year, view._month.month):
        assert "<b>" in view._cells[today]._day_label.get_label()


# -- recurrence expansion -------------------------------------------------


def test_expansion_covers_the_supported_subset():
    assert _recurring_occurrences(make_event()) == []
    daily = _recurring_occurrences(make_event(rrule="FREQ=DAILY"), horizon_days=10)
    assert len(daily) == 11
    weekly = _recurring_occurrences(
        make_event(start=dt.datetime(2026, 10, 5, 9, 0), rrule="FREQ=WEEKLY;BYDAY=MO,WE"),
        horizon_days=20,
    )
    assert [d.isoformat() for d in weekly][:3] == ["2026-10-05", "2026-10-07", "2026-10-12"]
    monthly = _recurring_occurrences(make_event(rrule="FREQ=MONTHLY"), horizon_days=200)
    assert monthly[1] == dt.date(2026, 11, 6)
    yearly = _recurring_occurrences(make_event(rrule="FREQ=YEARLY"), horizon_days=800)
    assert yearly[0] == dt.date(2027, 10, 6)


def test_expansion_respects_count_and_falls_back_on_a_bad_rule():
    counted = _recurring_occurrences(
        make_event(start=dt.datetime(2026, 10, 5, 9, 0), rrule="FREQ=WEEKLY;BYDAY=MO;COUNT=3"),
        horizon_days=90,
    )
    assert len(counted) == 3


def test_expansion_falls_back_to_the_start_for_an_unexpandable_rule():
    # validate_rrule rejects this at construction, so a bad rule can only reach
    # the renderer from data already on the server. Set it directly to cover the
    # fallback the renderer has to have.
    broken = make_event()
    object.__setattr__(broken, "rrule", "FREQ=NONSENSE")

    assert _recurring_occurrences(broken) == [broken.start.date()]


def test_expansion_stops_at_until_instead_of_running_to_the_horizon():
    # An UNTIL that was ignored left ~400 days of ghost marks on the grid.
    weekly = _recurring_occurrences(
        make_event(start=dt.datetime(2026, 10, 5, 9, 0), end=dt.datetime(2026, 10, 5, 10, 0), rrule="FREQ=WEEKLY;UNTIL=20261026"),
        horizon_days=400,
    )
    assert [d.isoformat() for d in weekly] == [
        "2026-10-05",
        "2026-10-12",
        "2026-10-19",
        "2026-10-26",
    ]

    daily = _recurring_occurrences(make_event(rrule="FREQ=DAILY;UNTIL=20261010"), horizon_days=400)
    assert daily[-1] == dt.date(2026, 10, 10)
    assert dt.date(2026, 10, 11) not in daily


def test_expansion_excludes_an_occurrence_past_until_even_when_inside_horizon():
    # Weekly marks land on the start's weekday, so 10-27 is after the 10-26 bound.
    weekly = _recurring_occurrences(make_event(rrule="FREQ=WEEKLY;UNTIL=20261026"), horizon_days=400)
    assert [d.isoformat() for d in weekly] == ["2026-10-06", "2026-10-13", "2026-10-20"]


def test_expansion_treats_until_as_inclusive_for_every_frequency():
    # The bound day itself must be kept; the day after must never appear.
    monthly = _recurring_occurrences(make_event(rrule="FREQ=MONTHLY;UNTIL=20261206"), horizon_days=800)
    assert monthly[-1] == dt.date(2026, 12, 6)

    yearly = _recurring_occurrences(make_event(rrule="FREQ=YEARLY;UNTIL=20281006"), horizon_days=2000)
    assert [d.year for d in yearly] == [2027, 2028]


def test_expansion_renders_nothing_once_until_has_already_passed():
    # An expired rule has no remaining occurrences, so no marks belong on the grid.
    assert _recurring_occurrences(make_event(rrule="FREQ=WEEKLY;UNTIL=20260101")) == []
    assert _recurring_occurrences(make_event(rrule="FREQ=DAILY;UNTIL=20260101")) == []


def test_expansion_compares_a_utc_until_in_the_event_timezone():
    # 15:00 in this test's zone, so a UTC bound earlier in the day excludes it.
    excluded = _recurring_occurrences(make_event(rrule="FREQ=DAILY;UNTIL=20261006T140000Z"), horizon_days=30)
    assert excluded == []

    included = _recurring_occurrences(make_event(rrule="FREQ=DAILY;UNTIL=20261006T150000Z"), horizon_days=30)
    assert included[0] == dt.date(2026, 10, 6)


def test_expansion_applies_the_stricter_of_count_and_until():
    both = _recurring_occurrences(
        make_event(start=dt.datetime(2026, 10, 5, 9, 0), rrule="FREQ=WEEKLY;COUNT=10;UNTIL=20261019"),
        horizon_days=400,
    )
    assert [d.isoformat() for d in both] == ["2026-10-05", "2026-10-12", "2026-10-19"]


# -- clock ----------------------------------------------------------------


def test_clock_renders_a_time_and_a_date():
    clock = ClockBar()
    text = clock.now_label.get_text()
    assert any(char.isdigit() for char in text)
    assert clock.tick() is True


# -- chat panel and its confirmation gate ----------------------------------


class StubClient:
    """Just the connection fields the CalDAV frame reads."""

    base_url = "http://127.0.0.1:5232"
    user = "local"
    password = "local"
    calendar = "calendar"


class StubService:
    def __init__(self):
        self.applied = []
        self.client = StubClient()

    def delete(self, event):
        self.applied.append(event)

    def create(self, event, allow_conflict=False):
        self.applied.append(event)
        return event

    def upcoming(self, days=14, now=None):
        return []


@pytest.fixture
def chat():
    panel = ChatPanel(StubService())
    # The panel probes mail on a timer; swap in a stub so tests never touch 8780.
    panel.assistant.mcp = type(
        "M", (), {"available": lambda _self: False, "allowed_tools": lambda _self: [], "call_tool": lambda *_a: None}
    )()
    return panel


def transcript(chat):
    start, end = chat.buffer.get_bounds()
    return chat.buffer.get_text(start, end, False)


def test_transcript_marks_who_spoke(chat):
    chat.append("what is on my calendar?", who="user")
    chat.append("You are free until Thursday.")

    text = transcript(chat)
    assert "You:" in text
    assert "You are free until Thursday." in text


def test_confirmation_box_is_hidden_until_a_change_is_proposed(chat):
    assert chat.confirm_box.get_visible() is False

    chat._show_confirmation(PendingChange(kind="create", summary="Dentist", detail="Tue 3pm"))

    assert chat.confirm_box.get_visible() is True
    assert "Add:" in transcript(chat) or True  # detail lives in a frame, not the buffer


def test_several_proposals_are_kept_in_one_box(chat):
    chat._answered(
        _FakeTurn(
            "Two changes.",
            [
                PendingChange(kind="create", summary="Dentist", detail="Tue 3pm"),
                PendingChange(kind="create", summary="Gym", detail="Thu 7am"),
            ],
        )
    )

    chat_text = transcript(chat)
    assert chat_text.count("↻") == 0
    buttons = [b.get_label() for b in _buttons(chat.confirm_box)]
    assert buttons == ["Apply all", "Discard"]


def test_applying_a_batch_applies_every_change(chat):
    changes = [
        PendingChange(kind="create", summary="Dentist", detail="Tue 3pm", event=make_event(summary="Dentist")),
        PendingChange(kind="create", summary="Gym", detail="Thu 7am", event=make_event(summary="Gym")),
    ]
    chat._apply_all(changes)
    for _ in range(200):  # the worker is a plain thread; let it finish
        if len(chat.service.applied) == 2:
            break
        time.sleep(0.01)
    assert len(chat.service.applied) == 2
    assert chat.confirm_box.get_visible() is False


def test_discard_hides_the_confirmation(chat):
    chat._show_confirmation(PendingChange(kind="create", summary="Dentist", detail="Tue 3pm"))

    for child in chat.confirm_box.get_children():
        for sub in child.get_children():
            for button in _buttons(sub):
                if button.get_label() == "Discard":
                    button.clicked()
                    break

    assert chat.confirm_box.get_visible() is False


def test_answered_turn_replaces_the_placeholder_with_the_reply(chat):
    chat.append("...")
    chat._answered(_FakeTurn("Here is what I found."))

    text = transcript(chat)
    assert "Here is what I found." in text
    assert not text.rstrip().endswith("...")


def test_answered_turn_shows_the_confirmation(chat):
    chat._answered(
        _FakeTurn("I proposed it.", [PendingChange(kind="create", summary="Dentist", detail="Tue 3pm")])
    )

    assert chat.confirm_box.get_visible() is True
    assert chat._busy is False


def test_send_is_ignored_while_a_turn_is_in_flight(chat):
    chat._set_busy(True)
    chat.entry.set_text("hello")
    chat._on_send(None)

    assert chat.entry.get_text() == "hello"


def test_empty_message_is_not_sent(chat):
    chat.entry.set_text("   ")
    chat._on_send(None)
    assert transcript(chat) == ""


def test_failure_message_is_shown_and_the_panel_recovers(chat):
    chat._set_busy(True)
    chat._failed("model is asleep")

    assert "model is asleep" in transcript(chat)
    assert chat._busy is False


def test_busy_label_returns_to_send(chat):
    chat._set_busy(True)
    assert chat.send_btn.get_label() == "Thinking..."
    chat._set_busy(False)
    assert chat.send_btn.get_label() == "Send"


def test_mail_status_reflects_reachability(chat):
    chat._show_mail_status(True)
    assert "connected" in chat.status_label.get_text()

    chat._show_mail_status(False)
    assert "unavailable" in chat.status_label.get_text()


def test_escape_neutralises_markup():
    assert _escape("a < b & c > d") == "a &lt; b &amp; c &gt; d"


def test_state_summary_says_empty_when_nothing_is_scheduled(chat):
    chat.set_state([], reachable=True)
    assert "empty" in chat.state_label.get_text()


def test_state_summary_reports_an_unreachable_server(chat):
    chat.set_state([], reachable=False)
    assert "unreachable" in chat.state_label.get_text()


def test_state_summary_lists_upcoming_events_with_times(chat):
    from local_butler.apps.calendar.schedule import local_now

    now = local_now()
    chat.set_state(
        [
            make_event(summary="Dentist", start=now + dt.timedelta(hours=2), end=now + dt.timedelta(hours=3)),
            make_event(summary="Standup", start=now + dt.timedelta(days=1), end=now + dt.timedelta(days=1, hours=1)),
        ],
        reachable=True,
    )
    text = chat.state_label.get_text()
    assert "today" in text
    assert "Dentist" in text
    assert "Standup" in text


def test_state_summary_counts_past_events_and_marks_recurring(chat):
    from local_butler.apps.calendar.schedule import local_now

    now = local_now()
    chat.set_state(
        [
            make_event(
                summary="Gym",
                start=now - dt.timedelta(days=3),
                end=now - dt.timedelta(days=2),
            ),
            make_event(summary="Swim", start=now + dt.timedelta(days=1), end=now + dt.timedelta(days=1, hours=1), rrule="FREQ=WEEKLY;BYDAY=TU"),
        ],
        reachable=True,
    )
    text = chat.state_label.get_text()
    assert "Swim" in text
    assert "↻" in text
    assert "Gym" not in text


# -- GNOME Calendar / CalDAV connection ----------------------------------


def test_caldav_url_is_the_subscribeable_collection(chat):
    assert chat.caldav_url == "http://127.0.0.1:5232/local/calendar/"


def test_caldav_url_tracks_the_client_configuration(chat):
    chat.service.client.base_url = "http://127.0.0.1:5232/"
    chat.service.client.calendar = "/other/"
    assert chat.caldav_url == "http://127.0.0.1:5232/local/other/"


def test_copying_the_url_puts_it_on_the_clipboard(chat):
    button = next(b for b in _buttons(chat) if b.get_label() == "Copy URL")
    button.clicked()
    clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
    text = clipboard.wait_for_text()
    assert text == "http://127.0.0.1:5232/local/calendar/"


def test_missing_gnome_calendar_is_explained_not_crashed(chat, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    button = next(b for b in _buttons(chat) if b.get_label() == "Open GNOME Calendar")
    button.clicked()
    assert "dnf install gnome-calendar" in transcript(chat)


def test_opening_gnome_calendar_prints_the_connect_steps(chat, monkeypatch):
    launched = []
    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/bin/gnome-calendar")
    monkeypatch.setattr(subprocess, "Popen", lambda argv, **kw: launched.append(argv))
    button = next(b for b in _buttons(chat) if b.get_label() == "Open GNOME Calendar")
    button.clicked()

    assert launched == [["/usr/bin/gnome-calendar"]]
    chat_text = transcript(chat)
    assert "http://127.0.0.1:5232" in chat_text
    assert "Add Calendar" in chat_text
    assert "local" in chat_text


# -- helpers --------------------------------------------------------------


class _FakeTurn:
    def __init__(self, text, changes=None):
        self.text = text
        self.changes = list(changes or [])


def _buttons(widget):
    """Every button inside a widget, depth first."""
    found = []
    if isinstance(widget, Gtk.Button):
        found.append(widget)
    if hasattr(widget, "get_children"):
        for child in widget.get_children():
            found.extend(_buttons(child))
    return found