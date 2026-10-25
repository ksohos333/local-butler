#!/usr/bin/env python3
"""Local Butler Calendar — controls the local Radicale CalDAV server and opens the system calendar client."""

from __future__ import annotations

import datetime as dt
import shutil
import subprocess
import sys
import threading
import urllib.request
from pathlib import Path

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk

from local_butler.apps.calendar.clock import shared_clock
from local_butler.apps.calendar.events import EventError, describe
from local_butler.apps.calendar.panels import (
    AddEventDialog,
    AgendaPanel,
    ChatPanel,
    ClockBar,
    DayTimeline,
    EventDialog,
    MonthView,
)
from local_butler.apps.calendar.schedule import ScheduleService

HOME = Path.home()
CONFIG_DIR = HOME / ".config" / "radicale"
DATA_DIR = HOME / ".local" / "share" / "radicale" / "collections"
USERS_FILE = CONFIG_DIR / "users"
CONFIG_FILE = CONFIG_DIR / "config"
SERVICE_NAME = "radicale.service"
URL = "http://127.0.0.1:5232"
DEFAULT_USER = "local"
DEFAULT_CALENDAR = "calendar"


def run(cmd, **kwargs):
    return subprocess.run(cmd, capture_output=True, text=True, **kwargs)


def active_state():
    r = run(["systemctl", "--user", "is-active", SERVICE_NAME])
    return r.stdout.strip() or "unknown"


def service_exists():
    r = run(["systemctl", "--user", "list-unit-files", SERVICE_NAME])
    return SERVICE_NAME in r.stdout


def ensure_config():
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    if not USERS_FILE.exists():
        USERS_FILE.write_text("local:local\n")
        USERS_FILE.chmod(0o600)

    if not CONFIG_FILE.exists():
        CONFIG_FILE.write_text(
            """[server]
hosts = 127.0.0.1:5232

[auth]
type = htpasswd
htpasswd_filename = /home/bpoppa12/.config/radicale/users
htpasswd_encryption = plain

[rights]
type = authenticated

[storage]
type = multifilesystem
filesystem_folder = /home/bpoppa12/.local/share/radicale/collections

[logging]
level = info
"""
        )


def ensure_service():
    service_dir = HOME / ".config" / "systemd" / "user"
    service_file = service_dir / SERVICE_NAME
    if service_file.exists():
        return
    service_dir.mkdir(parents=True, exist_ok=True)
    service_file.write_text(
        """[Unit]
Description=Radicale CalDAV/CardDAV server (user)
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/radicale --config %h/.config/radicale/config
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
"""
    )
    run(["systemctl", "--user", "daemon-reload"])


def start_service():
    ensure_config()
    ensure_service()
    run(["systemctl", "--user", "enable", SERVICE_NAME])
    r = run(["systemctl", "--user", "start", SERVICE_NAME])
    if r.returncode != 0:
        return False, r.stderr.strip()
    # Wait briefly for server to come up
    for _ in range(20):
        if active_state() == "active":
            return True, ""
        GLib.usleep(100_000)
    return False, "service did not become active"


def stop_service():
    r = run(["systemctl", "--user", "stop", SERVICE_NAME])
    return r.returncode == 0, r.stderr.strip()


def create_default_calendar():
    cal_url = f"{URL}/{DEFAULT_USER}/{DEFAULT_CALENDAR}/"
    req = urllib.request.Request(cal_url, method="MKCALENDAR")
    req.add_header("Content-Type", "text/calendar; charset=utf-8")
    # basic auth local:local
    import base64
    creds = base64.b64encode(b"local:local").decode()
    req.add_header("Authorization", f"Basic {creds}")
    try:
        urllib.request.urlopen(req, timeout=5)
        return True, cal_url
    except urllib.error.HTTPError as e:
        if e.code == 405 or e.code == 409:
            # Already exists or method not allowed; likely present
            return True, cal_url
        return False, str(e)
    except Exception as e:
        return False, str(e)


def get_calendar_info():
    info = {"running": active_state() == "active", "collections": []}
    if not info["running"]:
        return info
    collections_url = f"{URL}/{DEFAULT_USER}/"
    req = urllib.request.Request(collections_url, method="PROPFIND")
    req.add_header("Depth", "1")
    import base64
    creds = base64.b64encode(b"local:local").decode()
    req.add_header("Authorization", f"Basic {creds}")
    body = b"""<?xml version="1.0" encoding="utf-8" ?>
<D:propfind xmlns:D="DAV:"><D:prop><D:displayname/><D:resourcetype/></D:prop></D:propfind>"""
    try:
        resp = urllib.request.urlopen(req, data=body, timeout=5)
        text = resp.read().decode("utf-8", errors="replace")
        import xml.etree.ElementTree as ET
        root = ET.fromstring(text)
        ns = {"D": "DAV:", "C": "urn:ietf:params:xml:ns:caldav"}
        for response in root.findall("D:response", ns):
            href = response.findtext("D:href", "", ns)
            if href.rstrip("/").endswith(DEFAULT_USER):
                continue
            props = response.find("D:propstat/D:prop", ns)
            if props is None:
                continue
            name = props.findtext("D:displayname", "", ns)
            resourcetype = props.find("D:resourcetype", ns)
            is_calendar = resourcetype is not None and resourcetype.find("C:calendar", ns) is not None
            info["collections"].append({"href": href, "name": name or Path(href).name, "calendar": is_calendar})
    except Exception as e:
        info["error"] = str(e)
    return info


def find_calendar_client():
    for cmd in ["gnome-calendar", "evolution", "thunderbird"]:
        if shutil.which(cmd):
            return cmd
    return None


def install_client_hint():
    return """No CalDAV calendar client found. Install one of:
  sudo dnf install gnome-calendar evolution-data-server
  sudo dnf install thunderbird
Then add this CalDAV account:
  URL: http://127.0.0.1:5232/local/calendar/
  User: local
  Password: local
"""


class CalendarApp(Gtk.ApplicationWindow):
    """Calendar on the left, assistant on the right.

    The month grid and the assistant are separate panes: the assistant spends
    seconds per turn on the local model, and it must not be able to freeze the
    grid while it thinks.
    """

    def __init__(self, app):
        super().__init__(
            application=app,
            title="Local Butler Calendar",
            default_width=1180,
            default_height=720,
        )
        try:
            settings = Gtk.Settings.get_default()
            settings.set_property("gtk-application-prefer-dark-theme", True)
        except Exception:
            pass
        self.set_icon_name("x-office-calendar")

        header = Gtk.HeaderBar(title="Local Butler Calendar", show_close_button=True)
        self.set_titlebar(header)
        # One clock for the whole window: NTP-corrected, so the second tick and
        # the timeline marker agree with each other and with reality.
        self.atomic_clock = shared_clock()
        self.clock = ClockBar(self.atomic_clock)
        header.pack_start(self.clock)

        self.service = ScheduleService()

        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin=12)
        self.month = MonthView(on_day_clicked=self._on_day_selected)
        left.pack_start(self.month, True, True, 0)

        self.timeline = DayTimeline(
            on_event_clicked=self._on_block_clicked,
            on_time_clicked=self._on_add_at,
        )
        left.pack_start(self.timeline, False, False, 0)

        self.agenda = AgendaPanel(
            on_delete=self._delete,
            on_event_clicked=self._on_event_clicked,
            on_adjust=self._on_adjust,
            on_add=self._on_add,
        )
        left.pack_start(self.agenda, False, False, 0)

        self.status_label = Gtk.Label()
        self.status_label.set_xalign(0)
        left.pack_start(self.status_label, False, False, 0)

        self.open_client_btn = Gtk.Button(label="Open GNOME Calendar")
        self.open_client_btn.connect("clicked", self._on_open_client)
        self.open_client_btn.set_tooltip_text(
            "GNOME Calendar reads this same Radicale calendar. Note that "
            "gnome-online-accounts is not installed on this machine, which is "
            "what GNOME Calendar uses to add a CalDAV account."
        )
        left.pack_start(self.open_client_btn, False, False, 0)

        self.chat = ChatPanel(self.service, on_changed=self.refresh)

        pane = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        pane.set_position(760)
        pane.pack1(left, True, False)
        pane.pack2(self.chat, False, False)
        self.add(pane)
        self.pane = pane

        self.refresh()
        GLib.timeout_add_seconds(1, self._tick)
        GLib.timeout_add_seconds(30, self.refresh)

    def _tick(self) -> bool:
        """One second: move the clock, then the timeline marker with it."""
        self.clock.tick()
        self.timeline.tick(self.atomic_clock.now())
        return True

    def _on_day_selected(self, day):
        self.refresh()

    def refresh(self):
        """Reload the service state, the month grid, and the selected day."""
        state = active_state()
        running = state == "active"
        if not running:
            start_service()
            create_default_calendar()
            running = active_state() == "active"
        self.status_label.set_markup(
            "<span foreground='{0}'><b>Calendar:</b> {1}</span>".format(
                "#2ec27e" if running else "#c01c28", state
            )
        )
        client = find_calendar_client()
        self.open_client_btn.set_sensitive(running and client is not None)

        if not running:
            self.month.set_events([])
            self.agenda.set_events([])
            self.timeline.set_day(self.month.selected, [])
            self.chat.set_state([], reachable=False)
            return True

        try:
            events = self.service.events()
        except Exception:  # noqa: BLE001 - an empty grid beats a crash
            events = []
        self.month.set_events(events)
        self.agenda.set_events(events)
        self.agenda.set_anchor(self.month.selected)
        self.timeline.set_day(self.month.selected, events)
        self.chat.set_state(events, reachable=True)
        return True

    # -- opening events ---------------------------------------------------
    def _on_event_clicked(self, event, day):
        """A row in the agenda: open the dialog for that occurrence."""
        self._open_event(event, day)

    def _on_block_clicked(self, block):
        """A bar in the timeline: same dialog, same day."""
        self._open_event(block.event, block.day)

    def _open_event(self, event, day=None):
        dialog = EventDialog(self, event, day=day)
        while True:
            response = dialog.run()
            if response == EventDialog.DELETE_RESPONSE:
                dialog.destroy()
                self._delete(event)
                return
            if response != Gtk.ResponseType.OK:
                dialog.destroy()
                return
            try:
                draft = dialog.draft()
            except EventError as exc:
                dialog.show_error(str(exc))
                continue
            dialog.destroy()
            self._run_write(
                lambda: self.service.update(draft),
                "Saved {0}".format(draft.summary),
            )
            return

    # -- adding -----------------------------------------------------------
    def _on_add(self, day):
        """The agenda's Add button: a new event on the selected day."""
        self._open_add(day)

    def _on_add_at(self, day, time):
        """A click on empty timeline space: an event starting at that time."""
        self._open_add(day, at=time)

    def _open_add(self, day, at=None):
        dialog = AddEventDialog(self, day, at=at)
        while True:
            response = dialog.run()
            if response != Gtk.ResponseType.OK:
                dialog.destroy()
                return
            try:
                draft = dialog.draft()
            except EventError as exc:
                dialog.show_error(str(exc))
                continue
            dialog.destroy()
            self._run_write(lambda: self._create_allowing_overlap(draft),
                            "Added {0}".format(draft.summary))
            return

    def _create_allowing_overlap(self, draft):
        """Create without refusing a clash, but report who it overlaps.

        Overlap is allowed -- two meetings in the same hour is a normal day --
        so the clash list is passed back as part of the status message instead
        of being raised as an error.
        """
        clashes = [item.summary for item in self.service.conflicts(draft)]
        saved = self.service.create(draft, allow_conflict=True)
        if clashes:
            return "{0} (overlaps: {1})".format(saved.summary, ", ".join(clashes))
        return saved.summary

    # -- nudging times ----------------------------------------------------
    def _on_adjust(self, event, day, kind):
        """One of the agenda's nudge buttons: earlier, later, or longer."""
        if kind == "earlier":
            shifted = self.service.shift(event, dt.timedelta(minutes=-15))
            message = "Moved {0} 15 minutes earlier".format(event.summary)
        elif kind == "later":
            shifted = self.service.shift(event, dt.timedelta(minutes=15))
            message = "Moved {0} 15 minutes later".format(event.summary)
        elif kind == "longer":
            shifted = self.service.extend(event, dt.timedelta(minutes=30))
            message = "Extended {0} by 30 minutes".format(event.summary)
        else:
            return
        self._run_write(lambda: self.service.update(shifted), message)

    # -- writing ----------------------------------------------------------
    def _run_write(self, work, message):
        """Run a calendar write off the GTK thread, then refresh the panels.

        ``work`` returns either nothing or a better status line than
        ``message`` (the create path appends who it overlaps).
        """

        def worker():
            try:
                result = work()
            except Exception as exc:  # noqa: BLE001 - CalDAV errors are not EventError
                GLib.idle_add(self._write_failed, str(exc))
                return
            GLib.idle_add(
                self._write_done, result if isinstance(result, str) else message
            )

        threading.Thread(target=worker, daemon=True).start()

    def _write_done(self, message):
        self.status_label.set_markup(
            "<span foreground='#2ec27e'>{0}</span>".format(
                GLib.markup_escape_text(str(message))
            )
        )
        self.refresh()

    def _write_failed(self, error):
        self.status_label.set_markup(
            "<span foreground='#c01c28'>Could not save: {0}</span>".format(
                GLib.markup_escape_text(str(error))
            )
        )

    def _delete(self, event):
        """Ask before deleting, then delete off the GTK thread.

        This path bypasses the assistant, so it has to carry its own
        confirmation; a single misclick here would drop a real event.
        """
        dialog = Gtk.MessageDialog(
            transient_for=self,
            modal=True,
            message_type=Gtk.MessageType.WARNING,
            buttons=Gtk.ButtonsType.OK_CANCEL,
            text="Delete every occurrence of this event?",
        )
        note = "{0}\n\nThis cannot be undone.".format(describe(event))
        if event.rrule:
            note = "{0}\n\nThis repeating event is deleted whole, so every past\nand future occurrence goes too.".format(
                describe(event)
            )
        dialog.format_secondary_text(note)
        response = dialog.run()
        dialog.destroy()
        if response != Gtk.ResponseType.OK:
            return

        def worker():
            try:
                self.service.delete(event)
            except Exception as exc:  # noqa: BLE001
                GLib.idle_add(self._deleted, None, str(exc))
                return
            GLib.idle_add(self._deleted, event, "")

        threading.Thread(target=worker, daemon=True).start()

    def _deleted(self, event, error):
        if error:
            self.status_label.set_markup(
                "<span foreground='#c01c28'>Delete failed: {0}</span>".format(error)
            )
        else:
            self.status_label.set_markup(
                "<span foreground='#2ec27e'>Deleted {0}</span>".format(event.summary)
            )
        self.refresh()

    def _on_open_client(self, _btn):
        client = find_calendar_client()
        if not client:
            self.chat.append(install_client_hint())
            return
        subprocess.Popen([client])


class App(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="com.localbutler.Calendar", flags=0)

    def do_activate(self):
        win = CalendarApp(self)
        win.show_all()


def main():
    ensure_config()
    ensure_service()
    App().run(sys.argv)


if __name__ == "__main__":
    main()
