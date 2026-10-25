"""Month grid, day timeline, clock, and the right-hand AI panel for the calendar.

The window is a pane: the calendar on the left, the assistant on the right. The
month grid is built here rather than with :class:`Gtk.Calendar` because the grid
needs per-day event markers and a click target per day, which the stock widget
does not expose.

Below the grid sits the day timeline: one horizontal strip per day, hour by
hour, with concurrent events side by side in their own lanes and a marker that
walks with the clock. The clock itself is an :class:`AtomicClock`, so the
second-tick and the marker come from NTP-corrected time rather than whatever
the local clock happens to say.

The model can take tens of seconds to answer, so every assistant call runs on a
worker thread and comes back through :func:`GLib.idle_add`; the GTK main loop is
never blocked and the grid stays responsive while the model thinks.
"""

from __future__ import annotations

import datetime as dt
import re
import shutil
import subprocess
import threading
from dataclasses import replace
from typing import Callable, Dict, List, Optional, Tuple

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gdk, GLib, Gtk, Pango  # noqa: E402

from local_butler.apps.calendar.assistant import (  # noqa: E402
    Assistant,
    AssistantTurn,
    PendingChange,
)
from local_butler.apps.calendar.caldav import CalDAVError  # noqa: E402
from local_butler.apps.calendar.clock import shared_clock  # noqa: E402
from local_butler.apps.calendar.events import Event, EventError  # noqa: E402
from local_butler.apps.calendar.recurrence import (  # noqa: E402
    SCOPES,
    instance_span,
    occurrences_in_range,
    range_for,
    recurring_occurrences,
)
from local_butler.apps.calendar.schedule import (  # noqa: E402
    LLMUnavailable,
    ScheduleService,
    local_now,
)
from local_butler.apps.calendar.timeline import (  # noqa: E402
    SECONDS_PER_DAY,
    Block,
    day_blocks,
    layout_day,
    time_at,
)

WEEK_HEADERS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

# Greedy enough to catch a URL in prose, narrow enough to leave the sentence's
# trailing full stop out of the link.
URL_RE = re.compile(r"https?://[^\s<>\"'()\[\]{}]+", re.IGNORECASE)

_TIMELINE_CSS = b"""
.cal-block { background: #1c71d8; color: #ffffff; border-radius: 4px; }
.cal-block:hover { background: #3584e4; }
.cal-block-allday { background: #613583; color: #ffffff; border-radius: 4px; }
.cal-nowline { background: #c01c28; }
.cal-ruler { color: #8b8e99; }
"""
_css_done = False


def _ensure_css() -> None:
    """Register the timeline's colours once per process."""
    global _css_done
    if _css_done:
        return
    try:
        provider = Gtk.CssProvider()
        provider.load_from_data(_TIMELINE_CSS)
        screen = Gdk.Screen.get_default()
        if screen is not None:
            Gtk.StyleContext.add_provider_for_screen(
                screen, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            )
        _css_done = True
    except Exception:  # noqa: BLE001 -- styling must never stop the window opening
        pass


# -- clickable links -------------------------------------------------------
def link_urls(text: str) -> List[str]:
    """Every URL in ``text``, with sentence punctuation trimmed off the end."""
    urls = []
    for match in URL_RE.finditer(text or ""):
        urls.append(match.group(0).rstrip(".,;:!?"))
    return urls


def open_url(url: str) -> None:
    """Hand a URL to the desktop; failures are not worth interrupting for."""
    try:
        subprocess.Popen(
            ["xdg-open", url],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        pass


def _link_tag(buffer: Gtk.TextBuffer) -> Gtk.TextTag:
    tag = buffer.get_tag_table().lookup("link")
    if tag is None:
        tag = buffer.create_tag(
            "link",
            foreground="#62a0ea",
            underline=Pango.Underline.SINGLE,
        )
    return tag


def tag_links(buffer: Gtk.TextBuffer, start_offset: int = 0,
              end_offset: Optional[int] = None) -> None:
    """Paint every URL in ``[start_offset, end_offset)`` with the link tag.

    Re-tagging the whole transcript on each append is cheap (the transcript is
    a few kilobytes) and survives :meth:`Gtk.TextBuffer.set_text`, which drops
    every tag it holds -- that is how the "..." placeholder gets replaced.
    """
    if end_offset is None:
        end_offset = buffer.get_char_count()
    if end_offset <= start_offset:
        return
    tag = _link_tag(buffer)
    start = buffer.get_iter_at_offset(start_offset)
    end = buffer.get_iter_at_offset(end_offset)
    text = buffer.get_text(start, end, False)
    buffer.remove_tag(tag, start, end)
    base = start_offset
    for match in URL_RE.finditer(text):
        url = match.group(0).rstrip(".,;:!?")
        buffer.apply_tag(
            tag,
            buffer.get_iter_at_offset(base + match.start()),
            buffer.get_iter_at_offset(base + match.start() + len(url)),
        )


def link_at(view: Gtk.TextView, x: int, y: int) -> Optional[str]:
    """The URL under pixel ``(x, y)``, or ``None`` when the click misses one."""
    buffer = view.get_buffer()
    bx, by = view.window_to_buffer_coords(Gtk.TextWindowType.TEXT, x, y)
    try:
        where = buffer.get_iter_at_location(bx, by)
    except (TypeError, ValueError):
        return None
    line_start = where.copy()
    line_start.set_line_offset(0)
    line_end = where.copy()
    if not line_end.ends_line():
        line_end.forward_to_line_end()
    line = buffer.get_text(line_start, line_end, False)
    column = where.get_line_offset()
    for match in URL_RE.finditer(line):
        url = match.group(0).rstrip(".,;:!?")
        if match.start() <= column <= match.start() + len(url):
            return url
    return None


def on_text_click(
    view: Gtk.TextView, event, require_ctrl: bool = False
) -> bool:
    """Click handler: open the URL under the pointer when it is safe to.

    Read-only views (the transcript) open on a plain click; editable views
    (the event dialogs) wait for Ctrl, so dragging out a text selection still
    selects text instead of launching a browser.
    """
    if event.button != 1:
        return False
    if require_ctrl and not (event.state & Gdk.ModifierType.CONTROL_MASK):
        return False
    url = link_at(view, int(event.x), int(event.y))
    if not url:
        return False
    open_url(url)
    return True


def _description_view(text: str, editable: bool = True) -> Gtk.TextView:
    """A multiline description field with its URLs tagged and clickable."""
    view = Gtk.TextView(
        editable=editable,
        cursor_visible=editable,
        wrap_mode=Gtk.WrapMode.WORD,
    )
    view.set_accepts_tab(False)
    view.set_size_request(-1, 120 if editable else 90)
    buffer = view.get_buffer()
    buffer.set_text(text or "")
    tag_links(buffer)
    view.connect(
        "button-press-event",
        lambda widget, press: on_text_click(widget, press, require_ctrl=editable),
    )
    return view



class MonthView(Gtk.Box):
    """A month grid with event markers and a clickable day."""

    def __init__(self, on_day_clicked: Optional[Callable[[dt.date], None]] = None) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.on_day_clicked = on_day_clicked
        self._month = local_now().date().replace(day=1)
        self._selected = local_now().date()
        self._counts: Dict[dt.date, int] = {}
        self._cells: Dict[dt.date, Gtk.Box] = {}

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.prev_btn = Gtk.Button(label="‹")
        self.prev_btn.set_tooltip_text("Previous month")
        self.prev_btn.connect("clicked", lambda _b: self.shift_month(-1))
        self.title_label = Gtk.Label()
        self.title_label.set_justify(Gtk.Justification.CENTER)
        self.title_label.set_hexpand(True)
        self.title_label.set_markup("<b>September 2026</b>")
        self.next_btn = Gtk.Button(label="›")
        self.next_btn.set_tooltip_text("Next month")
        self.next_btn.connect("clicked", lambda _b: self.shift_month(1))
        self.today_btn = Gtk.Button(label="Today")
        self.today_btn.connect("clicked", lambda _b: self.select(local_now().date()))
        header.pack_start(self.prev_btn, False, False, 0)
        header.pack_start(self.title_label, True, True, 0)
        header.pack_start(self.next_btn, False, False, 0)
        header.pack_start(self.today_btn, False, False, 0)
        self.pack_start(header, False, False, 0)

        weekdays = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        for name in WEEK_HEADERS:
            label = Gtk.Label()
            label.set_markup("<b>{0}</b>".format(name))
            label.set_justify(Gtk.Justification.CENTER)
            label.set_hexpand(True)
            weekdays.pack_start(label, True, True, 0)
        self.pack_start(weekdays, False, False, 0)

        self.grid = Gtk.Grid(column_spacing=4, row_spacing=4)
        self.pack_start(self.grid, True, True, 0)
        self._build_grid()

    # -- grid -------------------------------------------------------------

    def _cells_for(self, month: dt.date) -> List[Tuple[dt.date, bool]]:
        """Every date shown for ``month`` as (date, in_this_month)."""
        first = month
        offset = first.weekday()
        start = first - dt.timedelta(days=offset)
        return [(start + dt.timedelta(days=index), (start + dt.timedelta(days=index)).month == month.month) for index in range(42)]

    def _build_grid(self) -> None:
        for child in self.grid.get_children():
            self.grid.remove(child)
        self._cells = {}
        for index, (day, in_month) in enumerate(self._cells_for(self._month)):
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            box.set_hexpand(True)
            box.set_vexpand(True)

            button = Gtk.Button()
            button.set_relief(Gtk.ReliefStyle.NONE)
            button.set_hexpand(True)
            label = Gtk.Label(label=str(day.day))
            label.set_justify(Gtk.Justification.LEFT)
            label.set_margin_start(4)
            button.add(label)
            button.connect("clicked", lambda _b, d=day: self.select(d))
            box.pack_start(button, False, False, 0)

            dots = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
            dots.set_margin_start(6)
            box.pack_start(dots, False, False, 0)

            if not in_month:
                label.set_markup("<span foreground='#8b8e99'>{0}</span>".format(day.day))
            box._day_label = label  # type: ignore[attr-defined]
            box._dots = dots  # type: ignore[attr-defined]
            self._cells[day] = box
            self.grid.attach(box, index % 7, index // 7, 1, 1)
        self.title_label.set_markup("<b>{0}</b>".format(self._month.strftime("%B %Y")))
        self._apply_styles()
        self._paint_dots()
        self._highlight()
        self.grid.show_all()

    def _apply_styles(self) -> None:
        today = local_now().date()
        for day, box in self._cells.items():
            label = box._day_label  # type: ignore[attr-defined]
            if day == today:
                label.set_markup("<span foreground='#3584e4'><b>{0}</b></span>".format(day.day))
            elif day == self._selected:
                label.set_markup("<span foreground='#1c71d8'><b>{0}</b></span>".format(day.day))
            else:
                label.set_text(str(day.day))

    def _paint_dots(self) -> None:
        for day, box in self._cells.items():
            dots = box._dots  # type: ignore[attr-defined]
            for child in dots.get_children():
                dots.remove(child)
            for index in range(min(self._counts.get(day, 0), 4)):
                dot = Gtk.Label()
                dot.set_markup("<span foreground='#1c71d8'>●</span>")
                dots.pack_start(dot, False, False, 0)

    def _highlight(self) -> None:
        for day, box in self._cells.items():
            for child in box.get_children():
                child.set_sensitive(True)
        selected = self._cells.get(self._selected)
        if selected is not None:
            selected.get_children()[0].set_sensitive(False)

    # -- state ------------------------------------------------------------

    def shift_month(self, delta: int) -> None:
        year = self._month.year + (self._month.month - 1 + delta) // 12
        month = (self._month.month - 1 + delta) % 12 + 1
        self._month = self._month.replace(year=year, month=month, day=1)
        self._build_grid()

    def select(self, day: dt.date) -> None:
        self._selected = day
        if (day.year, day.month) != (self._month.year, self._month.month):
            self._month = day.replace(day=1)
            self._build_grid()
        else:
            self._apply_styles()
            self._highlight()
        if self.on_day_clicked:
            self.on_day_clicked(day)

    @property
    def selected(self) -> dt.date:
        return self._selected

    def set_events(self, events: List[Event]) -> None:
        counts: Dict[dt.date, int] = {}
        for event in events:
            if event.duration > dt.timedelta(days=1):
                # A multi-day event gets one marker on its start day only.
                # Spreading the count across every day it touches would fill the
                # grid with dots that look like separate appointments, which is
                # worse than showing the one real start.
                days = [event.start.date()]
            else:
                # recurring_occurrences already includes the start date, so
                # build a set to avoid counting it twice. An expired UNTIL yields
                # nothing at all, which falls back to marking just the start.
                days = sorted(set(recurring_occurrences(event)) or [event.start.date()])
            for day in days:
                counts[day] = counts.get(day, 0) + 1
        self._counts = counts
        self.title_label.set_markup("<b>{0}</b>".format(self._month.strftime("%B %Y")))
        self._paint_dots()


class AgendaPanel(Gtk.Box):
    """Bottom strip listing what happens over a day, a week, or a month.

    The scope is switchable because the three questions are different. "What am
    I doing today" is a schedule; "this week" is a shape; "this month" is a
    check for clashes. Expanding recurrence is what makes the answer honest --
    without it a weekly standup would appear once instead of five times.
    """

    def __init__(
        self,
        on_scope_changed: Optional[Callable[[str], None]] = None,
        on_delete: Optional[Callable[[Event], None]] = None,
        on_event_clicked: Optional[Callable[[Event, dt.date], None]] = None,
        on_adjust: Optional[Callable[[Event, dt.date, str], None]] = None,
        on_add: Optional[Callable[[dt.date], None]] = None,
        height: int = 210,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.on_scope_changed = on_scope_changed
        self.on_delete = on_delete
        # Clicking the title opens the event; the three nudge buttons move or
        # lengthen it without a dialog. Both are optional so the panel still
        # works for callers that only render.
        self.on_event_clicked = on_event_clicked
        self.on_adjust = on_adjust
        self.on_add = on_add
        self._anchor: dt.date = local_now().date()
        self._scope = "week"
        self._events: List[Event] = []

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.range_label = Gtk.Label()
        self.range_label.set_xalign(0.0)
        self.range_label.set_hexpand(True)
        self.range_label.set_markup("<b>...</b>")
        header.pack_start(self.range_label, True, True, 0)

        if self.on_add is not None:
            add_btn = Gtk.Button(label="＋ Add")
            add_btn.set_tooltip_text("Add an event to the selected day")
            add_btn.connect("clicked", lambda _b: self.on_add(self._anchor))
            header.pack_start(add_btn, False, False, 0)

        self.scope_buttons = {}
        group: Optional[Gtk.RadioButton] = None
        for scope in SCOPES:
            button = Gtk.RadioButton.new_with_label_from_widget(group, scope.title())
            if scope == self._scope:
                button.set_active(True)
            button.connect("toggled", self._on_scope_toggled, scope)
            button.set_tooltip_text("Show the {0} around the selected date".format(scope))
            header.pack_start(button, False, False, 0)
            self.scope_buttons[scope] = button
            group = group or button
        self.pack_start(header, False, False, 0)

        self.list_box = Gtk.ListBox()
        self.list_box.set_selection_mode(Gtk.SelectionMode.NONE)
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_vexpand(True)
        scrolled.set_size_request(-1, height)
        scrolled.set_shadow_type(Gtk.ShadowType.IN)
        scrolled.add(self.list_box)
        self.pack_start(scrolled, True, True, 0)

    # -- state ------------------------------------------------------------

    def set_anchor(self, day: dt.date) -> None:
        """Follow the date the user selected in the grid."""
        self._anchor = day
        self.rebuild()

    def set_events(self, events: List[Event]) -> None:
        self._events = list(events)
        self.rebuild()

    @property
    def scope(self) -> str:
        return self._scope

    def _on_scope_toggled(self, button: Gtk.RadioButton, scope: str) -> None:
        if not button.get_active() or scope == self._scope:
            return
        self._scope = scope
        self.rebuild()
        if self.on_scope_changed:
            self.on_scope_changed(scope)

    # -- rendering --------------------------------------------------------

    def rebuild(self) -> None:
        first, last = range_for(self._anchor, self._scope)
        rows = occurrences_in_range(self._events, first, last)
        self.range_label.set_markup("<b>{0}</b>".format(_range_title(first, last, self._scope)))
        self._rebuild_list(rows)

    def _rebuild_list(self, rows: List[Tuple[Event, dt.date]]) -> None:
        for child in self.list_box.get_children():
            self.list_box.remove(child)

        if not rows:
            empty_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            empty = Gtk.Label(label="Nothing in this {0}.".format(self._scope))
            empty.set_margin_top(8)
            empty.set_margin_bottom(8)
            empty.set_margin_start(6)
            empty.set_xalign(0.0)
            empty_box.pack_start(empty, True, True, 0)
            if self.on_add is not None:
                add_empty = Gtk.Button(label="＋ Add event")
                add_empty.set_tooltip_text(
                    "Add an event on {0}".format(self._anchor.strftime("%a %d %b"))
                )
                add_empty.connect("clicked", lambda _b: self.on_add(self._anchor))
                empty_box.pack_start(add_empty, False, False, 0)
            self.list_box.add(empty_box)

        last_day: Optional[dt.date] = None
        for event, day in rows:
            # In day scope the heading already names the date, so repeating it on
            # every row is noise.
            if self._scope != "day" and day != last_day:
                self.list_box.add(self._day_divider(day, day == local_now().date()))
                last_day = day
            self.list_box.add(self._row(event, day))
        self.list_box.show_all()

    def _day_divider(self, day: dt.date, is_today: bool) -> Gtk.ListBoxRow:
        stamp = day.strftime("%a %d %b")
        if is_today:
            stamp += "  •  today"
        label = Gtk.Label()
        label.set_xalign(0.0)
        label.set_markup("<b>{0}</b>".format(_escape(stamp)))
        label.set_margin_top(8)
        label.set_margin_bottom(2)
        label.set_margin_start(6)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        box.pack_start(label, False, False, 0)
        box.pack_start(
            Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), False, False, 0
        )
        row = Gtk.ListBoxRow()
        row.set_selectable(False)
        row.add(box)
        return row

    def _row(self, event: Event, day: dt.date) -> Gtk.ListBoxRow:
        if event.all_day:
            when = "all day"
        else:
            begins, ends = instance_span(event, day)
            when = begins.strftime("%H:%M")
            if event.duration >= dt.timedelta(minutes=1):
                when += "-{0}".format(ends.strftime("%H:%M"))

        # describe() renders the stored DTSTART, which is the wrong date for
        # every occurrence after the first, so only the summary is reused here.
        summary = Gtk.Label(label=event.summary)
        summary.set_xalign(0.0)
        summary.set_line_wrap(True)
        summary.set_hexpand(True)

        time_label = Gtk.Label(label=when)
        time_label.set_xalign(0.0)
        time_label.set_hexpand(False)
        time_label.set_width_chars(11)
        time_label.set_markup(
            "<span foreground='#1c71d8'>{0}</span>".format(_escape(when))
        )
        if event.rrule:
            time_label.set_tooltip_text("Repeats: {0}".format(event.rrule))

        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        box.pack_start(time_label, False, False, 0)

        if self.on_event_clicked is not None:
            # The title is the click target for "view and edit", so a row needs
            # no separate Edit button competing with Delete.
            clickable = Gtk.EventBox()
            clickable.add(summary)
            clickable.set_tooltip_text("Click to view and edit this event")
            clickable.connect(
                "button-press-event",
                lambda widget, press, ev=event, d=day: self._row_clicked(press, ev, d),
            )
            box.pack_start(clickable, True, True, 0)
        else:
            box.pack_start(summary, True, True, 0)

        if self.on_adjust is not None and not event.all_day:
            for text, kind, tip in (
                ("−15m", "earlier", "Move 15 minutes earlier"),
                ("+15m", "later", "Move 15 minutes later"),
                ("+30m", "longer", "Extend by 30 minutes"),
            ):
                nudge = Gtk.Button(label=text)
                nudge.set_valign(Gtk.Align.CENTER)
                nudge.set_focus_on_click(False)
                nudge.set_tooltip_text(tip)
                nudge.connect(
                    "clicked",
                    lambda _b, ev=event, d=day, which=kind: self.on_adjust(ev, d, which),
                )
                box.pack_start(nudge, False, False, 0)

        if self.on_delete is not None:
            delete = Gtk.Button(label="Delete")
            delete.set_valign(Gtk.Align.CENTER)
            delete.set_tooltip_text("Remove this event from the calendar")
            # A recurring event is deleted whole, so the note prevents the
            # surprise of losing every future occurrence too.
            if event.rrule:
                delete.set_label("Delete all")
            delete.connect("clicked", lambda _b, ev=event: self.on_delete(ev))
            box.pack_start(delete, False, False, 0)
        box.set_margin_start(6)
        box.set_margin_end(6)
        box.set_margin_top(2)
        box.set_margin_bottom(2)

        row = Gtk.ListBoxRow()
        row.add(box)
        return row

    def _row_clicked(self, press, event: Event, day: dt.date) -> bool:
        """Left click on a row's title opens it; anything else is ignored."""
        if press.button != 1:
            return False
        if self.on_event_clicked:
            self.on_event_clicked(event, day)
        return True


def _range_title(first: dt.date, last: dt.date, scope: str) -> str:
    """Human heading for the agenda window."""
    if scope == "day":
        return first.strftime("%A %d %B %Y")
    if first.month == last.month:
        return "{0} – {1}".format(
            first.strftime("%d"), last.strftime("%d %B %Y")
        )
    return "{0} – {1}".format(first.strftime("%d %b"), last.strftime("%d %b %Y"))


def _parse_date(text: str) -> dt.date:
    """Read a ``YYYY-MM-DD`` entry, rejecting anything else."""
    try:
        return dt.datetime.strptime((text or "").strip(), "%Y-%m-%d").date()
    except ValueError:
        raise EventError("date must look like 2026-10-06") from None


class EventDialog(Gtk.Dialog):
    """View and edit one event, with its links clickable.

    The dialog is opened from a row or a timeline block, saved from its Save
    button, and deleted from its Delete button; it never writes by itself. The
    caller validates :meth:`draft`, so a bad time shows up as an error label
    here rather than as a rejected write on the server.
    """

    DELETE_RESPONSE = 101

    def __init__(self, parent, event: Event, day: Optional[dt.date] = None) -> None:
        super().__init__(title="Event", transient_for=parent, modal=True)
        self.set_default_size(540, 520)
        self.event = event
        self.day = day or event.start.date()

        self.add_button("Cancel", Gtk.ResponseType.CANCEL)
        self.add_button("Save", Gtk.ResponseType.OK)
        delete_btn = self.add_button(
            "Delete series" if event.rrule else "Delete", self.DELETE_RESPONSE
        )
        delete_btn.set_tooltip_text(
            "Removes every future occurrence" if event.rrule else "Removes this event"
        )

        grid = Gtk.Grid(column_spacing=8, row_spacing=8)
        grid.set_margin_start(12)
        grid.set_margin_end(12)
        grid.set_margin_top(12)
        grid.set_margin_bottom(12)

        self.summary_entry = Gtk.Entry(text=event.summary)
        self.summary_entry.set_placeholder_text("What is it?")
        self.location_entry = Gtk.Entry(text=event.location)
        self.location_entry.set_placeholder_text("Where? (optional)")
        self._attach(grid, 0, "Title", self.summary_entry)
        self._attach(grid, 1, "Location", self.location_entry)

        if event.all_day:
            self.date_entry = Gtk.Entry(text=self.day.isoformat())
            self.date_entry.set_placeholder_text("YYYY-MM-DD")
            self.duration_spin = Gtk.SpinButton.new_with_range(1, 30, 1)
            self.duration_spin.set_value(max(1, event.duration.days))
            self.duration_spin.set_tooltip_text("Days long")
            when = self.date_entry
            duration_label = "Days"
        else:
            self.date_entry = Gtk.Entry(text=self.day.isoformat())
            self.hour_spin = Gtk.SpinButton.new_with_range(0, 23, 1)
            self.hour_spin.set_value(event.start.hour)
            self.minute_spin = Gtk.SpinButton.new_with_range(0, 59, 5)
            self.minute_spin.set_value(event.start.minute)
            clock = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
            clock.pack_start(self.hour_spin, False, False, 0)
            colon = Gtk.Label(label=":")
            clock.pack_start(colon, False, False, 0)
            clock.pack_start(self.minute_spin, False, False, 0)
            when = clock
            minutes = max(5, int(event.duration.total_seconds() // 60))
            self.duration_spin = Gtk.SpinButton.new_with_range(5, 10080, 5)
            self.duration_spin.set_value(minutes)
            self.duration_spin.set_tooltip_text("Length in minutes")
            duration_label = "Minutes"
        self._attach(grid, 2, "Starts", when)
        self._attach(grid, 3, duration_label, self.duration_spin)

        repeats = Gtk.Label(label=event.rrule or "does not repeat")
        repeats.set_xalign(0.0)
        if event.rrule:
            repeats.set_tooltip_text("Repeats: {0}".format(event.rrule))
        else:
            repeats.set_sensitive(False)
        self._attach(grid, 4, "Repeats", repeats)

        notes_label = Gtk.Label(label="Notes")
        notes_label.set_xalign(0.0)
        notes_label.set_valign(Gtk.Align.START)
        notes_label.set_margin_top(4)
        grid.attach(notes_label, 0, 5, 1, 1)
        notes_scroll = Gtk.ScrolledWindow()
        notes_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self.description_view = _description_view(event.description, editable=True)
        notes_scroll.add(self.description_view)
        notes_scroll.set_vexpand(True)
        grid.attach(notes_scroll, 1, 5, 1, 1)

        hint = Gtk.Label()
        hint.set_xalign(0.0)
        hint.set_line_wrap(True)
        hint.set_markup(
            "<span size='x-small' foreground='#8b8e99'>"
            "Ctrl+click a link in the notes to open it. "
            "Times are local ({0})."
            "</span>".format(_escape(event.start.strftime("%Z") or "local time"))
        )
        grid.attach(hint, 0, 6, 2, 1)

        self.error_label = Gtk.Label()
        self.error_label.set_xalign(0.0)
        self.error_label.set_line_wrap(True)
        self.error_label.set_markup(
            "<span foreground='#c01c28'></span>"
        )
        self.error_label.set_no_show_all(True)
        grid.attach(self.error_label, 0, 7, 2, 1)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.add(grid)
        self.get_content_area().pack_start(scrolled, True, True, 0)
        self.show_all()
        self.error_label.hide()

    @staticmethod
    def _attach(grid: Gtk.Grid, row: int, title: str, widget: Gtk.Widget) -> None:
        label = Gtk.Label(label=title)
        label.set_xalign(0.0)
        label.set_margin_top(4)
        grid.attach(label, 0, row, 1, 1)
        widget.set_hexpand(True)
        grid.attach(widget, 1, row, 1, 1)

    def show_error(self, message: str) -> None:
        self.error_label.set_markup(
            "<span foreground='#c01c28'>{0}</span>".format(_escape(message))
        )
        self.error_label.show()

    def draft(self) -> Event:
        """The edited event; raises :class:`EventError` when it cannot exist."""
        summary = self.summary_entry.get_text().strip()
        if not summary:
            raise EventError("the event needs a title")
        if self.event.all_day:
            day = _parse_date(self.date_entry.get_text())
            start = dt.datetime.combine(day, dt.time(0, 0))
            end = start + dt.timedelta(days=int(self.duration_spin.get_value()))
        else:
            if not (0 <= int(self.hour_spin.get_value()) <= 23):
                raise EventError("hour must be between 0 and 23")
            start = dt.datetime.combine(
                self.day,
                dt.time(int(self.hour_spin.get_value()), int(self.minute_spin.get_value())),
            )
            end = start + dt.timedelta(minutes=int(self.duration_spin.get_value()))
        if end <= start:
            raise EventError("the event has to end after it starts")
        buffer = self.description_view.get_buffer()
        description = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False)
        return replace(
            self.event,
            summary=summary,
            location=self.location_entry.get_text().strip(),
            description=description,
            start=start,
            end=end,
        )


class AddEventDialog(Gtk.Dialog):
    """Compose a new event on a chosen day; the caller decides about clashes."""

    def __init__(
        self,
        parent,
        day: dt.date,
        at: Optional[dt.time] = None,
    ) -> None:
        super().__init__(title="New event", transient_for=parent, modal=True)
        self.set_default_size(480, 440)
        self.day = day
        start_time = at or dt.time(9, 0)

        self.add_button("Cancel", Gtk.ResponseType.CANCEL)
        self.add_button("Add", Gtk.ResponseType.OK)

        grid = Gtk.Grid(column_spacing=8, row_spacing=8)
        grid.set_margin_start(12)
        grid.set_margin_end(12)
        grid.set_margin_top(12)
        grid.set_margin_bottom(12)

        self.summary_entry = Gtk.Entry()
        self.summary_entry.set_placeholder_text("What is it?")
        self.summary_entry.set_activates_default(True)
        EventDialog._attach(grid, 0, "Title", self.summary_entry)

        self.date_entry = Gtk.Entry(text=day.isoformat())
        self.date_entry.set_placeholder_text("YYYY-MM-DD")
        EventDialog._attach(grid, 1, "Date", self.date_entry)

        self.hour_spin = Gtk.SpinButton.new_with_range(0, 23, 1)
        self.hour_spin.set_value(start_time.hour)
        self.minute_spin = Gtk.SpinButton.new_with_range(0, 59, 5)
        self.minute_spin.set_value(start_time.minute)
        clock = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        clock.pack_start(self.hour_spin, False, False, 0)
        clock.pack_start(Gtk.Label(label=":"), False, False, 0)
        clock.pack_start(self.minute_spin, False, False, 0)
        EventDialog._attach(grid, 2, "Starts", clock)

        self.duration_spin = Gtk.SpinButton.new_with_range(5, 10080, 5)
        self.duration_spin.set_value(60)
        self.duration_spin.set_tooltip_text("Length in minutes")
        EventDialog._attach(grid, 3, "Minutes", self.duration_spin)

        self.location_entry = Gtk.Entry()
        self.location_entry.set_placeholder_text("Where? (optional)")
        EventDialog._attach(grid, 4, "Location", self.location_entry)

        notes_label = Gtk.Label(label="Notes")
        notes_label.set_xalign(0.0)
        notes_label.set_valign(Gtk.Align.START)
        grid.attach(notes_label, 0, 5, 1, 1)
        notes_scroll = Gtk.ScrolledWindow()
        notes_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self.description_view = _description_view("", editable=True)
        notes_scroll.add(self.description_view)
        notes_scroll.set_vexpand(True)
        grid.attach(notes_scroll, 1, 5, 1, 1)

        hint = Gtk.Label()
        hint.set_xalign(0.0)
        hint.set_line_wrap(True)
        hint.set_markup(
            "<span size='x-small' foreground='#8b8e99'>"
            "Overlapping events are allowed; you will be told if one clashes."
            "</span>"
        )
        grid.attach(hint, 0, 6, 2, 1)

        self.error_label = Gtk.Label()
        self.error_label.set_xalign(0.0)
        self.error_label.set_line_wrap(True)
        self.error_label.set_no_show_all(True)
        grid.attach(self.error_label, 0, 7, 2, 1)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.add(grid)
        self.get_content_area().pack_start(scrolled, True, True, 0)
        self.show_all()
        self.error_label.hide()

    def show_error(self, message: str) -> None:
        self.error_label.set_markup(
            "<span foreground='#c01c28'>{0}</span>".format(_escape(message))
        )
        self.error_label.show()

    def draft(self) -> Event:
        """Build the new event; raises :class:`EventError` when it is unusable."""
        summary = self.summary_entry.get_text().strip()
        if not summary:
            raise EventError("the event needs a title")
        day = _parse_date(self.date_entry.get_text())
        start = dt.datetime.combine(
            day,
            dt.time(int(self.hour_spin.get_value()), int(self.minute_spin.get_value())),
        )
        end = start + dt.timedelta(minutes=int(self.duration_spin.get_value()))
        if end <= start:
            raise EventError("the event has to end after it starts")
        buffer = self.description_view.get_buffer()
        description = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False)
        return Event(
            summary=summary,
            start=start,
            end=end,
            location=self.location_entry.get_text().strip(),
            description=description,
        )


class DayTimeline(Gtk.Box):
    """One day as an hour-by-hour strip, with concurrent events side by side.

    Nothing is stored in pixels: a position is a fraction of the day, so
    resizing the window re-lays the same blocks out. Events that share an hour
    get their own lane (see :func:`timeline.layout_day`), which is the whole
    point -- three meetings at ten o'clock must be three visible bars.
    """

    ROW_H = 26
    MAX_TIMED_ROWS = 6
    RULER_H = 18

    def __init__(
        self,
        on_event_clicked: Optional[Callable[[Block], None]] = None,
        on_time_clicked: Optional[Callable[[dt.date, dt.time], None]] = None,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        _ensure_css()
        self.on_event_clicked = on_event_clicked
        self.on_time_clicked = on_time_clicked
        self._day = local_now().date()
        self._blocks: List[Block] = []
        self._all_rows = 0
        self._timed_rows = 0
        self._overflow = 0
        self._width = 640
        self._now: Optional[dt.datetime] = None

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.day_label = Gtk.Label()
        self.day_label.set_xalign(0.0)
        header.pack_start(self.day_label, True, True, 0)
        self.now_label = Gtk.Label()
        self.now_label.set_xalign(1.0)
        header.pack_start(self.now_label, False, False, 0)
        self.pack_start(header, False, False, 0)

        self.ruler = Gtk.Fixed()
        self.ruler.set_size_request(-1, self.RULER_H)
        self.pack_start(self.ruler, False, False, 0)

        self.progress = Gtk.ProgressBar()
        self.progress.set_show_text(False)
        self.progress.set_size_request(-1, 4)
        self.pack_start(self.progress, False, False, 0)

        self._frame = Gtk.EventBox()
        self._canvas = Gtk.Fixed()
        self._frame.add(self._canvas)
        self._frame.connect("button-press-event", self._on_background)
        self._frame.set_vexpand(True)
        self.pack_start(self._frame, True, True, 0)
        self._canvas.connect("size-allocate", self._on_size)

        self._ruler_items: List[Tuple[float, Gtk.Widget]] = []
        self._canvas_lines: List[Tuple[float, Gtk.Widget]] = []
        self._block_widgets: List[Tuple[Block, Gtk.EventBox]] = []
        self._now_line: Optional[Gtk.Box] = None
        self._overflow_label: Optional[Gtk.Label] = None
        self._height = self.ROW_H + 4

        self.set_day(self._day, [])

    # -- state ------------------------------------------------------------
    def set_day(self, day: dt.date, events: List[Event]) -> None:
        """Show ``day`` with every occurrence on it, stacked into lanes."""
        self._day = day
        placed, all_rows, timed_rows = layout_day(day_blocks(events, day))
        self._blocks = [block for block in placed if block.all_day or block.lane < self.MAX_TIMED_ROWS]
        self._overflow = len(placed) - len(self._blocks)
        self._all_rows = all_rows
        self._timed_rows = min(timed_rows, self.MAX_TIMED_ROWS)
        self.day_label.set_markup(
            "<b>{0}</b>".format(_escape(day.strftime("%A %d %B")))
        )
        self._rebuild()

    @property
    def day(self) -> dt.date:
        return self._day

    @property
    def blocks(self) -> List[Block]:
        return list(self._blocks)

    # -- building ---------------------------------------------------------
    def _rebuild(self) -> None:
        for container in (self.ruler, self._canvas):
            for child in container.get_children():
                container.remove(child)
                child.destroy()
        self._ruler_items = []
        self._canvas_lines = []
        self._block_widgets = []
        self._now_line = None
        self._overflow_label = None

        # Hour marks: a label every three hours, a tick every hour.
        for hour in range(0, 25, 3):
            label = Gtk.Label(label="{0:02d}".format(hour if hour < 24 else 24))
            label.get_style_context().add_class("cal-ruler")
            label.set_width_chars(5)
            label.set_xalign(0.5)
            self.ruler.put(label, 0, 0)
            self._ruler_items.append((hour / 24.0, label))
        for hour in range(0, 25):
            tick = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
            tick.set_size_request(1, 6 if hour % 3 else 10)
            self.ruler.put(tick, 0, self.RULER_H - 10)
            self._ruler_items.append((hour / 24.0, tick))

        rows = self._all_rows + self._timed_rows + (1 if self._overflow else 0)
        height = max(self.ROW_H, rows * self.ROW_H) + 4
        self._height = height
        self._canvas.set_size_request(-1, height)

        # A hairline behind every hour, so a free stretch reads as free time.
        for hour in range(1, 24):
            rule = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
            rule.set_size_request(1, height)
            rule.set_opacity(0.35)
            self._canvas.put(rule, 0, 0)
            self._canvas_lines.append((hour / 24.0, rule))

        for block in self._blocks:
            self._block_widgets.append((block, self._make_block(block)))

        if self._overflow:
            self._overflow_label = Gtk.Label(
                label="+{0} more event(s) — see the agenda below".format(self._overflow)
            )
            self._overflow_label.set_xalign(0.0)
            row = self._all_rows + self._timed_rows
            self._canvas.put(self._overflow_label, 6, row * self.ROW_H + 4)

        self._now_line = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self._now_line.get_style_context().add_class("cal-nowline")
        self._now_line.set_size_request(2, height)
        self._now_line.set_no_show_all(True)
        self._canvas.put(self._now_line, 0, 0)

        # show_all first: _relayout may hide a block label to keep the Fixed's
        # minimum inside the canvas, and show_all would just bring it back.
        self.show_all()
        self._relayout()
        if self._now is not None:
            self.tick(self._now)
        if self._now_line is not None and not self._is_today():
            self._now_line.hide()

    def _make_block(self, block: Block) -> Gtk.EventBox:
        widget = Gtk.EventBox()
        widget.get_style_context().add_class(
            "cal-block-allday" if block.all_day else "cal-block"
        )
        label = Gtk.Label(label=block.event.summary)
        label.set_xalign(0.0)
        label.set_margin_start(6)
        label.set_ellipsize(Pango.EllipsizeMode.END)
        widget.add(label)
        widget.set_tooltip_text(self._tooltip(block))
        widget.connect(
            "button-press-event",
            lambda widget, press, b=block: self._on_block_clicked(press, b),
        )
        self._canvas.put(widget, 0, 0)
        return widget

    @staticmethod
    def _tooltip(block: Block) -> str:
        event = block.event
        if event.all_day:
            when = block.day.strftime("%a %d %b") + " (all day)"
        else:
            when = "{0}–{1}".format(
                block.start.strftime("%H:%M"), block.end.strftime("%H:%M")
            )
        lines = ["{0}  ·  {1}".format(when, event.summary)]
        if event.location:
            lines.append("At: {0}".format(event.location))
        if event.rrule:
            lines.append("Repeats: {0}".format(event.rrule))
        if event.description:
            lines.append(event.description)
        lines.append("Click to view and edit")
        return "\n".join(lines)

    # -- geometry ---------------------------------------------------------
    def _on_size(self, _widget, allocation) -> None:
        width = max(1, allocation.width)
        if width == self._width:
            return
        self._width = width
        self._relayout()

    def _x(self, fraction: float) -> int:
        return int(round(fraction * self._width))

    def _row_of(self, block: Block) -> int:
        return block.lane if block.all_day else self._all_rows + block.lane

    def _relayout(self) -> None:
        # Nothing may stick out past the right edge: GtkFixed's minimum width
        # is max(child_x + child_min), so a child overshooting the canvas makes
        # the whole window grow on every size-allocate, forever.
        right = self._width
        for fraction, widget in self._ruler_items:
            x = self._x(fraction)
            if isinstance(widget, Gtk.Label):
                low, natural = widget.get_preferred_width()
                natural = max(low, natural)
                x = max(0, min(right - natural, x - 12))
                self.ruler.move(widget, x, 0)
            else:
                x = max(0, min(right - 1, x))
                self.ruler.move(widget, x, self.RULER_H - 10)
        for fraction, widget in self._canvas_lines:
            x = max(0, min(right - 1, self._x(fraction)))
            self._canvas.move(widget, x, 0)
        for block, widget in self._block_widgets:
            start = block.fraction_start()
            end = block.fraction_end()
            x = max(0, min(right - 1, self._x(start)))
            desired = max(20, self._x(end) - self._x(start) - 2)
            width = max(1, min(desired, right - x))
            widget.set_size_request(width, self.ROW_H - 6)
            # An ellipsized Gtk.Label still claims ~15px of minimum width
            # (plus its 6px margin) no matter how narrow the block is, and an
            # EventBox cannot go below its child's minimum.  A sub-21px block
            # at the right edge would therefore push the Fixed's minimum past
            # its allocation and grow the window on every allocate, forever.
            # Below that size the text is unreadable anyway: hide the label so
            # the box follows set_size_request alone; the tooltip still works.
            for child in widget.get_children():
                child.set_visible(width >= 21)
            self._canvas.move(widget, x, self._row_of(block) * self.ROW_H + 3)
        if self._overflow_label is not None:
            row = self._all_rows + self._timed_rows
            self._canvas.move(self._overflow_label, 6, row * self.ROW_H + 4)
        if self._now_line is not None:
            self._now_line.set_size_request(2, self._height)
            self._canvas.move(self._now_line, self._now_x(), 0)
        self._place_now_line()

    def _now_x(self) -> int:
        if self._now is None or not self._is_today():
            return 0
        midnight = dt.datetime.combine(self._day, dt.time(0, 0))
        fraction = (self._now - midnight).total_seconds() / SECONDS_PER_DAY
        # The line is 2px wide, so stop 2px short of the edge or it too would
        # push the Fixed's minimum width past its allocation.
        return max(0, min(self._width - 2, self._x(max(0.0, min(1.0, fraction)))))

    def _is_today(self) -> bool:
        return self._now is not None and self._now.date() == self._day

    def _place_now_line(self) -> None:
        if self._now_line is None:
            return
        if self._is_today():
            self._now_line.set_no_show_all(False)
            self._now_line.show()
            self._canvas.move(self._now_line, self._now_x(), 0)
        else:
            self._now_line.hide()

    # -- interaction ------------------------------------------------------
    def _on_block_clicked(self, press, block: Block) -> bool:
        if press.button != 1:
            return False
        if self.on_event_clicked:
            self.on_event_clicked(block)
        return True

    def _on_background(self, _frame, press) -> bool:
        """Clicking empty space offers an event starting at that time."""
        if press.button != 1 or not self.on_time_clicked:
            return False
        fraction = press.x / float(self._width)
        seconds = max(0, min(SECONDS_PER_DAY - 1, int(fraction * SECONDS_PER_DAY)))
        moment = time_at(self._day, seconds)
        self.on_time_clicked(self._day, moment.time().replace(second=0, microsecond=0))
        return True

    # -- clock ------------------------------------------------------------
    def tick(self, now: dt.datetime) -> bool:
        """Move the marker and the progress bar; returns ``True`` to keep ticking."""
        self._now = now
        if self._is_today():
            midnight = dt.datetime.combine(self._day, dt.time(0, 0))
            fraction = (now - midnight).total_seconds() / SECONDS_PER_DAY
            self.progress.set_fraction(max(0.0, min(1.0, fraction)))
            self.now_label.set_markup("<b>{0}</b>".format(now.strftime("%H:%M:%S")))
        else:
            self.progress.set_fraction(0.0)
            self.now_label.set_markup(
                "<span foreground='#8b8e99'>{0}</span>".format(
                    _escape(self._day.strftime("%d %b (no live marker)"))
                )
            )
        self._place_now_line()
        return True



class ClockBar(Gtk.Box):
    """Live second-by-second time, from the atomic clock rather than the OS.

    The NTP-corrected clock keeps the timeline marker and the agenda honest
    when the machine is asleep or its RTC drifts; the sync status is shown
    next to it so a stale clock is visible rather than silent.
    """

    def __init__(self, clock=None) -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.clock = clock if clock is not None else shared_clock()
        self.now_label = Gtk.Label()
        self.now_label.set_markup("<b>...</b>")
        self.zone_label = Gtk.Label()
        self.zone_label.set_markup("<span foreground='#8b8e99'></span>")
        self.pack_start(self.now_label, False, False, 0)
        self.pack_start(self.zone_label, True, True, 0)
        self.tick()

    @property
    def now(self) -> dt.datetime:
        """The clock's current time, re-read on every call."""
        return self.clock.now()

    def tick(self) -> bool:
        self.clock.maybe_resync()
        now = self.clock.now()
        self.now_label.set_markup(
            "<b>{0}</b> <span foreground='#8b8e99'>{1}</span>".format(
                now.strftime("%H:%M:%S"), now.strftime("%A %d %B %Y")
            )
        )
        zone = now.strftime("%Z") or "local time"
        self.zone_label.set_markup(
            "<span size='small' foreground='#8b8e99'>{0} · {1}</span>".format(
                _escape(zone), _escape(self.clock.status_text())
            )
        )
        return True


class ChatPanel(Gtk.Box):
    """Transcript, input, and the confirmation gate for proposed changes."""

    def __init__(
        self,
        service: ScheduleService,
        width: int = 380,
        on_changed: Optional[Callable[[], None]] = None,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.service = service
        self.assistant = Assistant(service=service)
        self.on_changed = on_changed
        self._busy = False

        self.set_size_request(width, -1)

        heading = Gtk.Label()
        heading.set_markup("<b>Assistant</b>")
        heading.set_xalign(0)
        self.pack_start(heading, False, False, 0)

        self.status_label = Gtk.Label(label="Checking mail access...")
        self.status_label.set_xalign(0)
        self.status_label.set_line_wrap(True)
        self.status_label.set_markup(
            "<span foreground='#8b8e99'>Checking mail access...</span>"
        )
        self.pack_start(self.status_label, False, False, 0)

        self.state_label = Gtk.Label(label="Calendar: checking...")
        self.state_label.set_xalign(0)
        self.state_label.set_line_wrap(True)
        self.pack_start(self.state_label, False, False, 0)

        self.pack_start(self._build_caldav_frame(), False, False, 0)

        self.view = Gtk.TextView(editable=False, cursor_visible=False, wrap_mode=Gtk.WrapMode.WORD)
        self.view.set_margin_start(8)
        self.view.set_margin_end(8)
        self.view.set_margin_top(8)
        self.view.set_margin_bottom(8)
        # Plain click opens: the transcript is read-only, so there is no text
        # selection to protect from an accidental browser launch.
        self.view.connect(
            "button-press-event",
            lambda widget, press: on_text_click(widget, press, require_ctrl=False),
        )
        self.buffer = self.view.get_buffer()
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_vexpand(True)
        scrolled.set_shadow_type(Gtk.ShadowType.IN)
        scrolled.add(self.view)
        self.pack_start(scrolled, True, True, 0)

        self.confirm_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.confirm_box.set_visible(False)
        self.pack_start(self.confirm_box, False, False, 0)

        entry_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.entry = Gtk.Entry()
        self.entry.set_placeholder_text(
            "Ask about your calendar or mail, or say what to schedule..."
        )
        self.entry.connect("activate", self._on_send)
        self.send_btn = Gtk.Button(label="Send")
        self.send_btn.connect("clicked", self._on_send)
        entry_row.pack_start(self.entry, True, True, 0)
        entry_row.pack_start(self.send_btn, False, False, 0)
        self.pack_start(entry_row, False, False, 0)

        self._probe_mail()

    # -- current state ----------------------------------------------------

    def set_state(self, events: List[Event], reachable: bool) -> None:
        """Summarise what is on the calendar right now.

        Without this the sidebar only ever showed whether mail was reachable,
        which left the actual state of the calendar invisible until the user
        happened to ask about it.
        """
        if not reachable:
            self.state_label.set_markup(
                "<span foreground='#c01c28'><b>Calendar:</b> unreachable</span>"
            )
            return
        if not events:
            self.state_label.set_markup(
                "<span foreground='#8b8e99'><b>Calendar:</b> empty</span>"
            )
            return

        now = local_now()
        upcoming = [
            item
            for item in events
            if item.end >= now
        ][:3]
        if not upcoming:
            self.state_label.set_markup(
                "<span foreground='#8b8e99'><b>Calendar:</b> {0} event(s), all past"
                "</span>".format(len(events))
            )
            return

        lines = ["<span foreground='#8b8e99'><b>{0} upcoming</b> of {1}</span>".format(len(upcoming), len(events))]
        for item in upcoming:
            when = item.start
            if when.date() == now.date():
                stamp = "today {0}".format(when.strftime("%H:%M"))
            else:
                stamp = when.strftime("%a %d %b %H:%M")
            suffix = "  ↻" if item.rrule else ""
            lines.append(
                "<span size='small'>{0} — {1}{2}</span>".format(
                    _escape(stamp), _escape(item.summary), suffix
                )
            )
        self.state_label.set_markup("\n".join(lines))

    # -- CalDAV / GNOME Calendar ------------------------------------------

    @property
    def caldav_url(self) -> str:
        """Absolute URL of the collection GNOME Calendar should subscribe to."""
        client = self.service.client
        return "{0}/{1}/{2}/".format(
            client.base_url.rstrip("/"),
            client.user,
            client.calendar.strip("/"),
        )

    def _build_caldav_frame(self) -> Gtk.Frame:
        """A copyable CalDAV block so GNOME Calendar can subscribe.

        Both clients read the same Radicale collection, so anything added here
        shows up in GNOME Calendar and vice versa. The built-in month grid is
        unaffected either way.
        """
        client = self.service.client
        frame = Gtk.Frame(label=" GNOME Calendar (CalDAV) ")
        frame.set_margin_top(4)

        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        inner.set_margin_start(8)
        inner.set_margin_end(8)
        inner.set_margin_top(6)
        inner.set_margin_bottom(6)

        url_label = Gtk.Label()
        url_label.set_selectable(True)
        url_label.set_xalign(0.0)
        url_label.set_markup(
            "<span font_family='monospace' size='small'>{0}</span>".format(
                _escape(self.caldav_url)
            )
        )
        inner.pack_start(url_label, False, False, 0)

        creds = Gtk.Label()
        creds.set_selectable(True)
        creds.set_xalign(0.0)
        creds.set_markup(
            "<span size='small' foreground='#8b8e99'>User: {0}  •  Pass: {1}</span>".format(
                _escape(client.user), _escape(client.password)
            )
        )
        inner.pack_start(creds, False, False, 0)

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        copy_btn = Gtk.Button(label="Copy URL")
        copy_btn.set_tooltip_text("Copy the CalDAV collection URL")
        copy_btn.connect("clicked", self._on_copy_caldav)
        row.pack_start(copy_btn, True, True, 0)

        self.open_gnome_btn = Gtk.Button(label="Open GNOME Calendar")
        self.open_gnome_btn.set_tooltip_text(
            "Launch gnome-calendar and print the connection steps here"
        )
        self.open_gnome_btn.connect("clicked", self._on_open_gnome)
        row.pack_start(self.open_gnome_btn, True, True, 0)
        inner.pack_start(row, False, False, 0)

        note = Gtk.Label()
        note.set_xalign(0.0)
        note.set_line_wrap(True)
        note.set_markup(
            "<span size='x-small' foreground='#8b8e99'>"
            "Subscribes the same Radicale collection, so entries sync both ways."
            "</span>"
        )
        inner.pack_start(note, False, False, 0)

        frame.add(inner)
        return frame

    def _on_copy_caldav(self, _btn) -> None:
        clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
        clipboard.set_text(self.caldav_url, -1)
        clipboard.store()
        self.append("CalDAV URL copied: {0}".format(self.caldav_url))

    def _on_open_gnome(self, _btn) -> None:
        client_name = shutil.which("gnome-calendar")
        if not client_name:
            self.append(
                "gnome-calendar is not installed. Install it with:\n"
                "  sudo dnf install gnome-calendar"
            )
            return
        try:
            subprocess.Popen([client_name])
        except OSError as exc:
            self.append("Could not launch gnome-calendar: {0}".format(exc))
            return
        self.append(
            "Launched gnome-calendar. To connect it to this calendar:\n"
            "1. Menu (☰) → Add Calendar…  (or Ctrl+Shift+A)\n"
            "2. Network server → Other CalDAV server\n"
            "3. Server URL: {0}\n"
            "4. Username: {1}   Password: {2}\n"
            "5. Choose the “{3}” collection, name it, and Add\n\n"
            "Both apps then read the same Radicale store, so events sync.".format(
                self.service.client.base_url.rstrip("/"),
                self.service.client.user,
                self.service.client.password,
                self.service.client.calendar.strip("/"),
            )
        )

    # -- transcript -------------------------------------------------------

    def append(self, text: str, who: str = "assistant") -> None:
        start_offset = self.buffer.get_char_count()
        end = self.buffer.get_end_iter()
        if who == "user":
            markup = "<b>You:</b> {0}\n\n".format(_escape(text))
        else:
            markup = "{0}\n\n".format(_escape(text))
        # PyGObject needs the byte length; -1 means "to the end of the buffer".
        self.buffer.insert_markup(end, markup, -1)
        tag_links(self.buffer, start_offset)
        self.view.scroll_to_iter(self.buffer.get_end_iter(), 0.0, False, 0.0, 0.0)

    # -- threading --------------------------------------------------------

    def _on_ui(self, func, *args) -> None:
        GLib.idle_add(func, *args)

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.send_btn.set_sensitive(not busy)
        self.entry.set_sensitive(not busy)
        if busy:
            self.send_btn.set_label("Thinking...")
        else:
            self.send_btn.set_label("Send")

    def _probe_mail(self) -> bool:
        """One-off background check of whether mail tools are reachable."""

        def worker() -> None:
            try:
                ok = self.assistant.mcp.available()
            except Exception:  # noqa: BLE001
                ok = False
            self._on_ui(self._show_mail_status, ok)

        threading.Thread(target=worker, daemon=True).start()
        return False

    def _show_mail_status(self, ok: bool) -> None:
        self.assistant.mail_available = ok
        if ok:
            self.status_label.set_markup(
                "<span foreground='#2ec27e'>Mail connected</span> "
                "<span foreground='#8b8e99'>(bigus_dickus)</span>"
            )
        else:
            self.status_label.set_markup(
                "<span foreground='#f5c211'>Mail unavailable</span> "
                "<span foreground='#8b8e99'>(calendar only)</span>"
            )

    # -- actions ----------------------------------------------------------

    def _on_send(self, _widget) -> None:
        if self._busy:
            return
        text = self.entry.get_text().strip()
        if not text:
            return
        self.entry.set_text("")
        self.append(text, who="user")
        self._set_busy(True)
        self.append("...")

        def worker() -> None:
            try:
                turn = self.assistant.ask(text)
            except (LLMUnavailable, EventError) as exc:
                self._on_ui(self._failed, str(exc))
                return
            except Exception as exc:  # noqa: BLE001
                self._on_ui(self._failed, "{0}: {1}".format(type(exc).__name__, exc))
                return
            self._on_ui(self._answered, turn)

        threading.Thread(target=worker, daemon=True).start()

    def _failed(self, message: str) -> None:
        self._set_busy(False)
        self.append("I could not answer that: " + message)

    def _answered(self, turn: AssistantTurn) -> None:
        self._set_busy(False)
        # Replace the "..." placeholder line with the real reply.
        start, end = self.buffer.get_bounds()
        text = self.buffer.get_text(start, end, False)
        if text.rstrip().endswith("..."):
            head = text.rstrip()[: -len("...")]
            self.buffer.set_text(head)
            # set_text throws every tag away, so the links already painted on
            # the transcript have to be painted again.
            tag_links(self.buffer)
        self.append(turn.text)
        if turn.changes:
            # One box for the whole batch: showing them one at a time would let a
            # later proposal push an earlier one out of the UI while it was
            # still unapplied.
            self._show_confirmations(turn.changes)

    def _show_confirmations(self, changes: List[PendingChange]) -> None:
        for child in self.confirm_box.get_children():
            self.confirm_box.remove(child)

        frame = Gtk.Frame()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin=8)

        title = Gtk.Label()
        count = len(changes)
        title.set_markup(
            "<b>{0} proposed change{1}</b>".format(count, "" if count == 1 else "s")
        )
        title.set_xalign(0)
        box.pack_start(title, False, False, 0)

        for change in changes:
            row = Gtk.Label()
            row.set_markup(
                "<span size='small'>{0}</span>".format(_escape(change.describe_change()))
            )
            row.set_xalign(0)
            row.set_line_wrap(True)
            box.pack_start(row, False, False, 0)

        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        apply_btn = Gtk.Button(label="Apply all")
        apply_btn.connect("clicked", lambda _b, c=list(changes): self._apply_all(c))
        skip_btn = Gtk.Button(label="Discard")
        skip_btn.connect("clicked", lambda _b: self.confirm_box.set_visible(False))
        buttons.pack_start(apply_btn, True, True, 0)
        buttons.pack_start(skip_btn, True, True, 0)
        box.pack_start(buttons, False, False, 0)

        frame.add(box)
        self.confirm_box.pack_start(frame, False, False, 0)
        self.confirm_box.show_all()
        self.confirm_box.set_visible(True)

    def _show_confirmation(self, change: PendingChange) -> None:
        """Single-change confirmation box."""
        self._show_confirmations([change])

    def _apply_all(self, changes: List[PendingChange]) -> None:
        self.confirm_box.set_visible(False)
        self._set_busy(True)
        self.append("Applying {0} change(s)...".format(len(changes)))

        def worker() -> None:
            done: List[str] = []
            for change in changes:
                try:
                    done.append(self.assistant.apply(change))
                # CalDAVError is a RuntimeError, so an unreachable server has
                # to be caught here too or the panel would stay busy forever.
                except (EventError, CalDAVError) as exc:
                    done.append("Could not apply {0}: {1}".format(change.summary, exc))
                except Exception as exc:  # noqa: BLE001
                    done.append(
                        "Could not apply {0}: {1}".format(change.summary, exc)
                    )
            self._on_ui(self._applied, "; ".join(done))

        threading.Thread(target=worker, daemon=True).start()

    def _apply(self, change: PendingChange) -> None:
        self.confirm_box.set_visible(False)
        self._set_busy(True)
        self.append("Applying: {0}".format(change.describe_change()))

        def worker() -> None:
            try:
                result = self.assistant.apply(change)
            except (EventError, CalDAVError, LLMUnavailable) as exc:
                self._on_ui(self._failed, str(exc))
                return
            except Exception as exc:  # noqa: BLE001
                self._on_ui(self._failed, "{0}: {1}".format(type(exc).__name__, exc))
                return
            self._on_ui(self._applied, result)

        threading.Thread(target=worker, daemon=True).start()

    def _applied(self, message: str) -> None:
        self._set_busy(False)
        self.append(message)
        # The grid, the timeline and the agenda all cache the same events, so
        # an applied change has to reach them too.
        if self.on_changed:
            self.on_changed()


def _escape(text: str) -> str:
    """Escape text for Pango markup."""
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


# Kept for callers that imported the private name before it moved to
# :mod:`recurrence`, which holds the same pure arithmetic.
_recurring_occurrences = recurring_occurrences
