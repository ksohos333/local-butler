"""Tests for the scheduling panel's widget logic.

Widgets are built directly rather than through a running window, so these need
no display and no network.
"""

import datetime as dt
import time

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk  # noqa: E402

from local_butler.apps.calendar.events import Event  # noqa: E402
from local_butler.apps.calendar.panel import SchedulePanel, _escape  # noqa: E402
from local_butler.apps.calendar.schedule import LLMUnavailable  # noqa: E402

ZONE = dt.timezone(dt.timedelta(hours=3))


@pytest.fixture
def panel():
    return SchedulePanel()


def make_event(summary="Dentist", **kwargs):
    defaults = dict(
        start=dt.datetime(2026, 10, 6, 15, 0),
        end=dt.datetime(2026, 10, 6, 16, 0),
        tz=ZONE,
    )
    defaults.update(kwargs)
    return Event(summary=summary, **defaults)


def row_labels(panel):
    """The text of each event row, in order, ignoring the per-row Delete button.

    The empty state inserts a bare Label while a populated row is
    ListBoxRow > Box > [Label, Button > Label], so this walks the tree rather
    than assuming one label per row -- and stops at Button so the button's own
    caption doesn't shift the row indices.
    """
    found = []

    def walk(widget):
        if isinstance(widget, Gtk.Label):
            found.append(widget.get_text())
            return
        if isinstance(widget, Gtk.Button):
            return
        if hasattr(widget, "get_children"):
            for child in widget.get_children():
                walk(child)

    for child in panel.list_box.get_children():
        walk(child)
    return found


def test_escape_neutralises_markup():
    assert _escape("a & b < c > d") == "a &amp; b &lt; c &gt; d"


def test_empty_list_shows_a_placeholder(panel):
    panel._rebuild_list([])
    assert row_labels(panel) == ["Nothing scheduled."]


def test_each_event_becomes_one_row(panel):
    panel._rebuild_list([make_event("Dentist"), make_event("Coffee")])
    assert len(panel.list_box.get_children()) == 2
    assert "Dentist" in row_labels(panel)[0]
    assert "Coffee" in row_labels(panel)[1]


def test_rebuilding_does_not_duplicate_rows(panel):
    events = [make_event("Dentist"), make_event("Coffee")]
    panel._rebuild_list(events)
    panel._rebuild_list(events)
    assert len(panel.list_box.get_children()) == 2


def test_recurrence_marker_is_shown_once(panel):
    panel._rebuild_list([make_event(rrule="BYDAY=MO;FREQ=WEEKLY")])
    text = row_labels(panel)[0]
    assert text.count("repeats") == 1


def test_markup_in_an_event_summary_is_escaped_when_shown(panel):
    # A summary is displayed as a plain label, so "<" must not become markup.
    panel._rebuild_list([make_event("Tea & <cake>")])
    assert "Tea & <cake>" in row_labels(panel)[0]


# -- the button handlers -------------------------------------------------
#
# These drive the real click handlers. The panel posts work to a thread and
# comes back through GLib.idle_add, so the main context has to be pumped until
# the callback has landed.


class FakeService:
    """Stands in for ScheduleService so the handlers can run offline."""

    def __init__(self, preview_error=None):
        self.events = []
        self.preview_error = preview_error
        self.conflicting = []
        self.previewed = []
        self.created = []
        self.deleted = []

    def preview(self, text):
        self.previewed.append(text)
        if self.preview_error is not None:
            raise self.preview_error
        return make_event()

    def conflicts(self, candidate):
        return self.conflicting

    def create(self, event, allow_conflict=False):
        self.created.append(event)
        self.events.append(event)
        return event

    def upcoming(self, days=30, now=None):
        return list(self.events)

    def delete(self, event):
        self.deleted.append(event)
        self.events = [e for e in self.events if e is not event]
        return True


def panel_with(service):
    panel = SchedulePanel()
    panel.service = service
    return panel


def pump(until, timeout=5.0):
    """Spin the GLib main loop until `until()` holds, so idle_add fires."""
    context = GLib.MainContext.default()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if until():
            return True
        while context.pending():
            context.iteration(False)
        time.sleep(0.01)
    return until()


def test_check_reads_the_note_without_saving_it():
    service = FakeService()
    panel = panel_with(service)
    panel.entry.set_text("dentist appointment tuesday at 3pm")

    panel._on_preview(None)

    assert pump(lambda: panel.add_btn.get_sensitive()), "preview never finished"
    assert service.previewed == ["dentist appointment tuesday at 3pm"]
    assert service.created == [], "Check must not save anything"
    assert "Confirm:" in panel.detail_label.get_text()
    assert "Dentist" in panel.detail_label.get_text()


def test_add_saves_the_confirmed_event_and_clears_the_entry():
    service = FakeService()
    panel = panel_with(service)
    panel.entry.set_text("dentist tuesday 3pm")
    panel._on_preview(None)
    assert pump(lambda: panel.add_btn.get_sensitive())

    panel._on_add(None)

    # Pump on the UI state, not on service.created: the worker appends that
    # first, and the entry is only cleared once the idle callback has run.
    assert pump(lambda: "Added:" in panel.detail_label.get_text()), "add never finished"
    assert service.created[0].summary == "Dentist"
    assert panel.entry.get_text() == ""


def test_discard_saves_nothing():
    service = FakeService()
    panel = panel_with(service)
    panel.entry.set_text("dentist tuesday 3pm")
    panel._on_preview(None)
    assert pump(lambda: panel.add_btn.get_sensitive())

    panel._on_cancel(None)

    assert service.created == []
    assert not panel.add_btn.get_sensitive()
    assert panel.detail_label.get_text() == ""


def test_a_clashing_event_is_reported_before_saving():
    service = FakeService()
    service.conflicting = [make_event("Standup")]
    panel = panel_with(service)
    panel.entry.set_text("dentist tuesday 3pm")

    panel._on_preview(None)

    assert pump(lambda: "clashes with" in panel.detail_label.get_text())
    assert "clashes with: Standup" in panel.detail_label.get_text()


def test_a_failed_read_is_reported_and_leaves_add_disabled():
    service = FakeService(preview_error=LLMUnavailable("llama is not running"))
    panel = panel_with(service)
    panel.entry.set_text("dentist tuesday 3pm")

    panel._on_preview(None)

    assert pump(lambda: "llama is not running" in panel.detail_label.get_text())
    assert not panel.add_btn.get_sensitive()
    assert not panel._busy, "a failure must release the buttons"


def test_an_empty_note_is_rejected_without_calling_the_service():
    service = FakeService()
    panel = panel_with(service)
    panel.entry.set_text("   ")

    panel._on_preview(None)

    assert service.previewed == []
    assert not panel._busy
    assert panel.detail_label.get_text() != ""


def test_the_delete_button_removes_that_event():
    service = FakeService()
    panel = panel_with(service)
    event = make_event("Dentist")
    panel._rebuild_list([event])

    button = next(
        widget
        for widget in panel.list_box.get_children()[0].get_children()[0].get_children()
        if isinstance(widget, Gtk.Button)
    )
    button.clicked()

    assert pump(lambda: "Deleted:" in panel.detail_label.get_text()), "delete never finished"
    assert service.deleted[0] is event
    assert service.events == [], "the deleted event should be gone from the list"
