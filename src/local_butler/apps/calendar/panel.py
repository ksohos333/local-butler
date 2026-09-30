"""Scheduling UI for the Local Butler calendar window.

Kept apart from ``main`` so the window file stays about service control and this
file stays about scheduling. The language model can take tens of seconds to
answer, so every call runs on a worker thread and comes back through
``GLib.idle_add``; the GTK main loop is never blocked.
"""

from __future__ import annotations

import threading
from typing import List, Optional

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk

from local_butler.apps.calendar.caldav import CalDAVError
from local_butler.apps.calendar.events import Event, EventError, describe
from local_butler.apps.calendar.schedule import LLMUnavailable, ScheduleService, local_now

WEEK_DAYS = 30


class SchedulePanel(Gtk.Box):
    """Natural-language entry, confirmation, and the upcoming-events list."""

    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.service = ScheduleService()
        self._pending: Optional[Event] = None
        self._busy = False

        self.entry = Gtk.Entry()
        self.entry.set_placeholder_text(
            "e.g. dentist appointment tuesday at 3pm, remind me 1 hour before"
        )
        self.entry.set_activates_default(False)
        self.entry.connect("activate", self._on_entry_activate)

        self.preview_btn = Gtk.Button(label="Check")
        self.preview_btn.set_tooltip_text("Read the note without saving anything")
        self.preview_btn.connect("clicked", self._on_preview)

        self.add_btn = Gtk.Button(label="Add")
        self.add_btn.set_sensitive(False)
        self.add_btn.set_tooltip_text("Save the event shown below")
        self.add_btn.connect("clicked", self._on_add)

        self.cancel_btn = Gtk.Button(label="Discard")
        self.cancel_btn.set_sensitive(False)
        self.cancel_btn.connect("clicked", self._on_cancel)

        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        buttons.pack_start(self.entry, True, True, 0)
        buttons.pack_start(self.preview_btn, False, False, 0)
        buttons.pack_start(self.add_btn, False, False, 0)
        buttons.pack_start(self.cancel_btn, False, False, 0)
        self.pack_start(buttons, False, False, 0)

        self.detail_label = Gtk.Label(label="")
        self.detail_label.set_xalign(0)
        self.detail_label.set_line_wrap(True)
        self.pack_start(self.detail_label, False, False, 0)

        self.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), False, False, 0)

        heading = Gtk.Label()
        heading.set_markup("<b>Next {} days</b>".format(WEEK_DAYS))
        heading.set_xalign(0)
        self.pack_start(heading, False, False, 0)

        self.list_box = Gtk.ListBox()
        self.list_box.set_selection_mode(Gtk.SelectionMode.NONE)
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_vexpand(True)
        scrolled.add(self.list_box)
        self.pack_start(scrolled, True, True, 0)

    # -- helpers ----------------------------------------------------------

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.preview_btn.set_sensitive(not busy)
        self.entry.set_sensitive(not busy)
        if busy:
            self.add_btn.set_sensitive(False)
            self.cancel_btn.set_sensitive(False)

    def _on_ui(self, func, *args) -> None:
        """Run ``func`` on the GTK thread, from a worker."""
        GLib.idle_add(func, *args)

    def _run(self, work, on_ok, on_error) -> None:
        """Do ``work`` off-thread and route the outcome back to the UI."""

        def worker() -> None:
            try:
                result = work()
            except (EventError, CalDAVError, LLMUnavailable, OSError) as exc:
                self._on_ui(on_error, str(exc))
                return
            except Exception as exc:  # noqa: BLE001 - never let a thread kill the app
                self._on_ui(on_error, "{0}: {1}".format(type(exc).__name__, exc))
                return
            self._on_ui(on_ok, result)

        self._set_busy(True)
        threading.Thread(target=worker, daemon=True).start()

    def _show_error(self, message: str) -> None:
        self._set_busy(False)
        self.detail_label.set_markup(
            "<span foreground='#c01c28'>{0}</span>".format(_escape(message))
        )

    # -- actions ----------------------------------------------------------

    def _on_entry_activate(self, _entry) -> None:
        if not self._busy:
            self._on_preview(None)

    def _on_preview(self, _button) -> None:
        text = self.entry.get_text().strip()
        if not text:
            self._show_error("Type what you want scheduled first.")
            return
        self.detail_label.set_markup("<i>Reading the note...</i>")

        def work():
            # conflicts() is a CalDAV round-trip, so it belongs on the worker
            # thread with preview() rather than in the idle callback below.
            event = self.service.preview(text)
            return event, self.service.conflicts(event)

        def ok(result) -> None:
            event, clashes = result
            self._set_busy(False)
            self._pending = event
            lines = [describe(event)]
            if event.location:
                lines.append("at {0}".format(event.location))
            if event.rrule:
                lines.append("repeats")
            if clashes:
                lines.append("clashes with: " + ", ".join(c.summary for c in clashes))
            self.detail_label.set_markup(
                "<b>Confirm:</b> " + _escape("\n".join(lines))
            )
            self.add_btn.set_sensitive(True)
            self.cancel_btn.set_sensitive(True)

        self._run(work, ok, self._show_error)

    def _on_cancel(self, _button) -> None:
        self._pending = None
        self.add_btn.set_sensitive(False)
        self.cancel_btn.set_sensitive(False)
        self.detail_label.set_markup("")

    def _on_add(self, _button) -> None:
        event = self._pending
        if event is None:
            return
        self.add_btn.set_sensitive(False)
        self.cancel_btn.set_sensitive(False)
        self.detail_label.set_markup("<i>Saving...</i>")

        def ok(saved: Event) -> None:
            self._set_busy(False)
            self._pending = None
            self.entry.set_text("")
            self.detail_label.set_markup(
                "<span foreground='#2ec27e'>Added: {0}</span>".format(
                    _escape(describe(saved))
                )
            )
            self.refresh()

        self._run(lambda: self.service.create(event), ok, self._show_error)

    def _on_delete(self, event: Event) -> None:
        self.detail_label.set_markup("<i>Deleting...</i>")

        def ok(_result) -> None:
            self._set_busy(False)
            self.detail_label.set_markup(
                "<span foreground='#2ec27e'>Deleted: {0}</span>".format(
                    _escape(event.summary)
                )
            )
            self.refresh()

        self._run(lambda: self.service.delete(event), ok, self._show_error)

    # -- list -------------------------------------------------------------

    def refresh(self) -> bool:
        """Rebuild the upcoming list; returns True for the periodic timer."""
        try:
            events = self.service.upcoming(days=WEEK_DAYS, now=local_now())
        except (EventError, CalDAVError, OSError):
            events = []
        self._rebuild_list(events)
        return True

    def _rebuild_list(self, events: List[Event]) -> None:
        for child in self.list_box.get_children():
            self.list_box.remove(child)

        if not events:
            row = Gtk.Label(label="Nothing scheduled.")
            row.set_margin_top(6)
            row.set_margin_bottom(6)
            row.set_xalign(0)
            self.list_box.add(row)
        for event in events:
            self.list_box.add(self._row(event))
        self.list_box.show_all()

    def _row(self, event: Event) -> Gtk.ListBoxRow:
        # describe() already flags recurrence, so the row must not repeat it.
        text = describe(event)
        label = Gtk.Label(label=text)
        label.set_xalign(0)
        label.set_line_wrap(True)
        label.set_margin_top(4)
        label.set_margin_bottom(4)

        button = Gtk.Button(label="Delete")
        button.set_valign(Gtk.Align.CENTER)
        button.connect("clicked", lambda _b, ev=event: self._on_delete(ev))

        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        box.pack_start(label, True, True, 0)
        box.pack_start(button, False, False, 0)
        box.set_margin_start(6)
        box.set_margin_end(6)
        row = Gtk.ListBoxRow()
        row.add(box)
        return row


def _escape(text: str) -> str:
    """Escape text for Pango markup."""
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
