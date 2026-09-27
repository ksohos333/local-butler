#!/usr/bin/env python3
"""Local Butler Calendar — controls the local Radicale CalDAV server and opens the system calendar client."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk

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
    def __init__(self, app):
        super().__init__(application=app, title="Local Butler Calendar", default_width=500, default_height=400)
        self.set_icon_name("x-office-calendar")

        header = Gtk.HeaderBar(title="Local Butler Calendar", show_close_button=True)
        self.set_titlebar(header)

        self.status_label = Gtk.Label(label="Checking...")
        self.status_label.set_xalign(0)

        self.info_label = Gtk.Label(label="")
        self.info_label.set_xalign(0)
        self.info_label.set_line_wrap(True)

        self.start_btn = Gtk.Button(label="Start Radicale")
        self.start_btn.connect("clicked", self._on_start)
        self.stop_btn = Gtk.Button(label="Stop Radicale")
        self.stop_btn.connect("clicked", self._on_stop)
        self.open_client_btn = Gtk.Button(label="Open Calendar Client")
        self.open_client_btn.connect("clicked", self._on_open_client)

        btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        btn_box.pack_start(self.start_btn, False, False, 0)
        btn_box.pack_start(self.stop_btn, False, False, 0)
        btn_box.pack_start(self.open_client_btn, False, False, 0)

        self.log_view = Gtk.TextView(editable=False, monospace=True)
        self.log_view.set_wrap_mode(Gtk.WrapMode.WORD)
        self.log_buffer = self.log_view.get_buffer()
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_vexpand(True)
        scrolled.add(self.log_view)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin=12)
        box.pack_start(self.status_label, False, False, 0)
        box.pack_start(self.info_label, False, False, 0)
        box.pack_start(btn_box, False, False, 0)
        box.pack_start(Gtk.Separator(), False, False, 0)
        box.pack_start(scrolled, True, True, 0)
        self.add(box)

        self.refresh()
        GLib.timeout_add_seconds(3, self.refresh)

    def log(self, text):
        GLib.idle_add(self._append_log, text)

    def _append_log(self, text):
        end = self.log_buffer.get_end_iter()
        self.log_buffer.insert(end, text + "\n")
        self.log_view.scroll_to_iter(self.log_buffer.get_end_iter(), 0.0, False, 0.0, 0.0)
        return False

    def refresh(self):
        state = active_state()
        self.status_label.set_markup(f"<b>Radicale:</b> {state}")
        self.start_btn.set_sensitive(state != "active")
        self.stop_btn.set_sensitive(state == "active")

        info = get_calendar_info()
        lines = []
        if info.get("running"):
            lines.append(f"<b>CalDAV URL:</b> {URL}/{DEFAULT_USER}/{DEFAULT_CALENDAR}/")
            lines.append("<b>User:</b> local  |  <b>Password:</b> local")
            if info.get("collections"):
                lines.append("<b>Calendars:</b> " + ", ".join(c["name"] for c in info["collections"]))
            if info.get("error"):
                lines.append(f"<b>Error:</b> {info['error']}")
        else:
            lines.append("Service is stopped.")
        client = find_calendar_client()
        if client:
            lines.append(f"<b>Client found:</b> {client}")
        else:
            lines.append("<b>No calendar client found.</b>")
        self.info_label.set_markup("\n".join(lines))
        self.open_client_btn.set_sensitive(state == "active" and client is not None)
        return True

    def _on_start(self, _btn):
        self.log("Starting Radicale...")
        ok, err = start_service()
        if not ok:
            self.log(f"Failed to start: {err}")
            return
        self.log("Radicale started.")
        ok, url = create_default_calendar()
        if ok:
            self.log(f"Default calendar ready: {url}")
        else:
            self.log(f"Could not create default calendar: {url}")
        self.refresh()

    def _on_stop(self, _btn):
        self.log("Stopping Radicale...")
        ok, err = stop_service()
        self.log("Stopped." if ok else f"Failed: {err}")
        self.refresh()

    def _on_open_client(self, _btn):
        client = find_calendar_client()
        if not client:
            self.log(install_client_hint())
            return
        self.log(f"Opening {client}...")
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
