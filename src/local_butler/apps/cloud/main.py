#!/usr/bin/env python3
"""Local Butler Cloud — multi-backend local cloud hub.

Supports Syncthing, MinIO, rclone, Nextcloud/ownCloud/Seafile detection,
and shows Samba/NFS status. Designed to manage local-first cloud/sync
options without relying on external proprietary servers.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import urllib.request
import xml.etree.ElementTree as ET
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk, Gdk

HOME = Path.home()
DATA_ROOT = HOME / ".local" / "share" / "local-butler-cloud"
SERVICE_DIR = HOME / ".config" / "systemd" / "user"


def open_terminal(command: str) -> None:
    """Open a terminal emulator and run the given command."""
    for term in ["gnome-terminal", "kgx", "xfce4-terminal", "lxterminal", "konsole", "alacritty", "xterm"]:
        if shutil.which(term):
            if term == "gnome-terminal":
                subprocess.Popen([term, "--", "bash", "-c", f"{command}; read -n1 -r -p 'Press any key...'"])
            elif term == "konsole":
                subprocess.Popen([term, "-e", "bash", "-c", f"{command}; read -n1 -r -p 'Press any key...'"])
            elif term in ("xfce4-terminal", "lxterminal"):
                subprocess.Popen([term, "-e", "bash", "-c", f"{command}; read -n1 -r -p 'Press any key...'"])
            else:
                subprocess.Popen([term, "-e", "bash", "-c", f"{command}; read -n1 -r -p 'Press any key...'"])
            return
    subprocess.Popen(["x-terminal-emulator", "-e", "bash", "-c", f"{command}; read -n1 -r -p 'Press any key...'"])


def run(cmd, timeout=10, **kwargs):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, **kwargs)


def systemctl_user(*args):
    return run(["systemctl", "--user", *args])


class CloudBackend(ABC):
    """Abstract base for a local cloud/sync backend."""

    name: str = "Backend"
    description: str = ""
    icon: str = "folder-remote"
    service_name: str | None = None
    binary: str | None = None

    @abstractmethod
    def detect(self) -> bool:
        """Return True if this backend is installed/available."""
        ...

    @abstractmethod
    def is_running(self) -> bool:
        ...

    def start(self) -> tuple[bool, str]:
        return False, "Not implemented"

    def stop(self) -> tuple[bool, str]:
        return False, "Not implemented"

    def status_lines(self) -> list[str]:
        return []

    def setup_hint(self) -> str:
        return ""

    def actions(self) -> list[tuple[str, Callable[[Any], None]]]:
        return []


Callable = Any  # noqa


# ---------------------------------------------------------------------------
# Syncthing backend
# ---------------------------------------------------------------------------

class SyncthingBackend(CloudBackend):
    name = "Syncthing"
    description = "Peer-to-peer continuous file synchronization between your own devices."
    icon = "folder-remote"
    binary = "syncthing"
    service_name = "local-butler-syncthing.service"

    def __init__(self):
        self.config_home = HOME / ".config" / "syncthing-local-butler"
        self.gui_url = "http://127.0.0.1:8384"
        self.bin_path = HOME / "bin" / "syncthing"

    def detect(self) -> bool:
        return self.bin_path.exists() or shutil.which("syncthing") is not None

    def _bin(self) -> str:
        return str(self.bin_path) if self.bin_path.exists() else shutil.which("syncthing")

    def is_running(self) -> bool:
        r = systemctl_user("is-active", self.service_name)
        return r.stdout.strip() == "active"

    def _legacy_running(self) -> bool:
        r = systemctl_user("is-active", "syncthing.service")
        return r.stdout.strip() == "active"

    def _ensure_config(self):
        self.config_home.mkdir(parents=True, exist_ok=True)
        config_xml = self.config_home / "config.xml"
        if not config_xml.exists():
            bin_ = self._bin()
            if bin_:
                run([bin_, "generate", "--home", str(self.config_home)])
        if config_xml.exists():
            try:
                tree = ET.parse(config_xml)
                root = tree.getroot()
                gui = root.find("gui")
                if gui is not None:
                    addr = gui.find("address")
                    if addr is None:
                        addr = ET.SubElement(gui, "address")
                    addr.text = "127.0.0.1:8384"
                    tls = gui.find("tls")
                    if tls is None:
                        tls = ET.SubElement(gui, "tls")
                    tls.text = "false"
                    tree.write(config_xml, encoding="utf-8", xml_declaration=True)
            except Exception:
                pass
        (HOME / "Sync").mkdir(parents=True, exist_ok=True)

    def _ensure_service(self):
        service_file = SERVICE_DIR / self.service_name
        if service_file.exists():
            return
        SERVICE_DIR.mkdir(parents=True, exist_ok=True)
        bin_ = self._bin() or "%h/bin/syncthing"
        service_file.write_text(
            f"""[Unit]
Description=Syncthing - Local Butler Cloud Sync
Documentation=man:syncthing(1)
After=network.target

[Service]
Type=simple
ExecStart={bin_} serve --no-browser --no-restart --home=%h/.config/syncthing-local-butler --log-level=INFO
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
"""
        )
        systemctl_user("daemon-reload")

    def start(self) -> tuple[bool, str]:
        self._ensure_config()
        self._ensure_service()
        systemctl_user("enable", self.service_name)
        r = systemctl_user("start", self.service_name)
        if r.returncode != 0:
            return False, r.stderr.strip()
        for _ in range(20):
            if self.is_running():
                return True, ""
            GLib.usleep(100_000)
        return False, "service did not become active"

    def stop(self) -> tuple[bool, str]:
        r = systemctl_user("stop", self.service_name)
        return r.returncode == 0, r.stderr.strip()

    def _api_key(self) -> str | None:
        config_xml = self.config_home / "config.xml"
        if not config_xml.exists():
            return None
        try:
            tree = ET.parse(config_xml)
            return tree.findtext("gui/apikey")
        except Exception:
            return None

    def _api_get(self, path: str):
        key = self._api_key()
        if not key:
            return None, "no api key"
        req = urllib.request.Request(f"{self.gui_url}/rest{path}")
        req.add_header("X-API-Key", key)
        try:
            with urllib.request.urlopen(req, timeout=3) as resp:
                return json.loads(resp.read().decode("utf-8")), None
        except Exception as e:
            return None, str(e)

    def status_lines(self) -> list[str]:
        lines = []
        if self._legacy_running():
            lines.append("⚠ Older syncthing.service is also running; stop it to free port 8384.")
        if not self.is_running():
            return lines
        data, err = self._api_get("/system/status")
        if err:
            lines.append(f"API error: {err}")
            return lines
        lines.append(f"Web UI: {self.gui_url}")
        my_id = data.get("myID")
        if my_id:
            lines.append(f"This device ID: {my_id}")
        folders, _ = self._api_get("/config/folders")
        if folders:
            lines.append(f"Folders: {len(folders)}")
        return lines

    def setup_hint(self) -> str:
        if self.detect():
            return "Install a Syncthing client on your phone/another PC, then share this device's ID."
        return "Download Syncthing and place the binary at ~/bin/syncthing."

    def actions(self):
        actions = []
        if self.is_running():
            actions.append(("Open Web UI", lambda _: subprocess.Popen(["xdg-open", self.gui_url])))
            actions.append(("Open ~/Sync", lambda _: subprocess.Popen(["xdg-open", str(HOME / "Sync")])))
        return actions


# ---------------------------------------------------------------------------
# MinIO backend
# ---------------------------------------------------------------------------

class MinIOBackend(CloudBackend):
    name = "MinIO"
    description = "Self-hosted S3-compatible object storage server."
    icon = "drive-harddisk"
    binary = "minio"
    service_name = "local-butler-minio.service"

    def __init__(self):
        self.data_dir = DATA_ROOT / "minio"
        self.console_url = "http://127.0.0.1:9001"
        self.api_url = "http://127.0.0.1:9000"

    def detect(self) -> bool:
        return shutil.which("minio") is not None

    def is_running(self) -> bool:
        r = systemctl_user("is-active", self.service_name)
        return r.stdout.strip() == "active"

    def _ensure_service(self):
        service_file = SERVICE_DIR / self.service_name
        if service_file.exists():
            return
        SERVICE_DIR.mkdir(parents=True, exist_ok=True)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        service_file.write_text(
            f"""[Unit]
Description=MinIO - Local Butler S3-compatible object storage
After=network.target

[Service]
Type=simple
Environment="MINIO_ROOT_USER=localbutler"
Environment="MINIO_ROOT_PASSWORD=localbutler"
ExecStart=%h/.local/bin/minio server --console-address 127.0.0.1:9001 %h/.local/share/local-butler-cloud/minio
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
"""
        )
        # Try to symlink system minio to ~/.local/bin if needed
        minio_bin = shutil.which("minio")
        local_bin = HOME / ".local" / "bin" / "minio"
        if minio_bin and not local_bin.exists():
            local_bin.parent.mkdir(parents=True, exist_ok=True)
            local_bin.symlink_to(minio_bin)
        systemctl_user("daemon-reload")

    def start(self) -> tuple[bool, str]:
        if not self.detect():
            return False, "minio binary not found; install minio first"
        self._ensure_service()
        systemctl_user("enable", self.service_name)
        r = systemctl_user("start", self.service_name)
        if r.returncode != 0:
            return False, r.stderr.strip()
        for _ in range(20):
            if self.is_running():
                return True, ""
            GLib.usleep(100_000)
        return False, "service did not become active"

    def stop(self) -> tuple[bool, str]:
        r = systemctl_user("stop", self.service_name)
        return r.returncode == 0, r.stderr.strip()

    def status_lines(self) -> list[str]:
        if not self.is_running():
            return []
        return [f"S3 API: {self.api_url}", f"Console: {self.console_url}", "Root user/pass: localbutler"]

    def setup_hint(self) -> str:
        return "Install minio (e.g. download the static binary) to get a local S3 API."

    def actions(self):
        actions = []
        if self.is_running():
            actions.append(("Open Console", lambda _: subprocess.Popen(["xdg-open", self.console_url])))
        return actions


# ---------------------------------------------------------------------------
# rclone backend (cloud fetch / mount / webdav)
# ---------------------------------------------------------------------------

class RcloneBackend(CloudBackend):
    name = "rclone"
    description = "Sync, mount, or serve files from local disk or remote cloud backends (OneDrive, Google Drive, S3, WebDAV, SFTP, etc.)."
    icon = "network-workgroup"
    binary = "rclone"

    def detect(self) -> bool:
        return shutil.which("rclone") is not None

    def is_running(self) -> bool:
        return False  # rclone is command-line; no persistent service here

    def status_lines(self) -> list[str]:
        if not self.detect():
            return []
        remotes, err = self._list_remotes()
        if err:
            return [f"Could not list remotes: {err}"]
        if remotes:
            return [f"Configured remotes: {', '.join(remotes)}"]
        return ["No remotes configured yet. Run 'rclone config' in a terminal."]

    def _list_remotes(self):
        try:
            r = run(["rclone", "listremotes"], timeout=5)
            if r.returncode != 0:
                return [], r.stderr.strip()
            return [line.strip().rstrip(":") for line in r.stdout.splitlines() if line.strip()], None
        except Exception as e:
            return [], str(e)

    def setup_hint(self) -> str:
        return "Run 'rclone config' to add cloud remotes, then use this machine to fetch/sync data locally."

    def actions(self):
        return [
            ("Configure rclone", lambda _: open_terminal("rclone config")),
            ("rclone docs", lambda _: subprocess.Popen(["xdg-open", "https://rclone.org/"])),
        ]


# ---------------------------------------------------------------------------
# Nextcloud / ownCloud / Seafile detection backends
# ---------------------------------------------------------------------------

class ContainerBackend(CloudBackend):
    """Base for backends usually deployed via container."""

    package_name: str = ""
    container_image: str = ""
    web_port: int = 8080

    def detect(self) -> bool:
        return shutil.which("podman") is not None or shutil.which("docker") is not None

    def is_running(self) -> bool:
        return False

    def status_lines(self) -> list[str]:
        engine = "podman" if shutil.which("podman") else "docker" if shutil.which("docker") else None
        if not engine:
            return ["Install podman or docker to deploy this backend."]
        return [f"{self.name} not running. Deploy with {engine}."]

    def setup_hint(self) -> str:
        return f"Example: podman run -d --name {self.name.lower()}-local -p {self.web_port}:{self.web_port} {self.container_image}"

    def actions(self):
        return [
            ("Copy setup command", lambda btn: self._copy_setup_command(btn)),
        ]

    def _copy_setup_command(self, _btn):
        cmd = self.setup_hint().replace("Example: ", "")
        clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
        clipboard.set_text(cmd, -1)


class NextcloudBackend(ContainerBackend):
    name = "Nextcloud"
    description = "Full self-hosted cloud: files, calendar, contacts, notes, apps."
    icon = "cloud"
    container_image = "docker.io/nextcloud"
    web_port = 8080


class OwnCloudBackend(ContainerBackend):
    name = "ownCloud"
    description = "Lighter self-hosted files/calendar/contacts server."
    icon = "cloud"
    container_image = "docker.io/owncloud/server"
    web_port = 8080


class SeafileBackend(ContainerBackend):
    name = "Seafile"
    description = "Fast file sync and library server."
    icon = "folder-download"
    container_image = "docker.io/seafileltd/seafile-mc"
    web_port = 8082


# ---------------------------------------------------------------------------
# Samba / NFS status backends
# ---------------------------------------------------------------------------

class SambaBackend(CloudBackend):
    name = "Samba"
    description = "Windows-compatible file sharing over the local network."
    icon = "network-server"

    def detect(self) -> bool:
        return shutil.which("smbd") is not None

    def is_running(self) -> bool:
        r = run(["systemctl", "is-active", "smb"])
        return r.stdout.strip() == "active"

    def status_lines(self) -> list[str]:
        if not self.detect():
            return ["smbd not installed."]
        return ["Active" if self.is_running() else "Inactive (system service; use systemctl start smb)"]

    def setup_hint(self) -> str:
        return "Install samba and edit /etc/samba/smb.conf to share folders on your LAN."


class NFSBackend(CloudBackend):
    name = "NFS"
    description = "Unix-style network file sharing over the local network."
    icon = "network-server"

    def detect(self) -> bool:
        return shutil.which("exportfs") is not None

    def is_running(self) -> bool:
        r = run(["systemctl", "is-active", "nfs-server"])
        return r.stdout.strip() == "active"

    def status_lines(self) -> list[str]:
        if not self.detect():
            return ["NFS server not installed."]
        return ["Active" if self.is_running() else "Inactive (system service; use systemctl start nfs-server)"]

    def setup_hint(self) -> str:
        return "Edit /etc/exports to share folders, then run systemctl start nfs-server."


# ---------------------------------------------------------------------------
# Backend registry
# ---------------------------------------------------------------------------

BACKENDS: list[CloudBackend] = [
    SyncthingBackend(),
    MinIOBackend(),
    RcloneBackend(),
    NextcloudBackend(),
    OwnCloudBackend(),
    SeafileBackend(),
    SambaBackend(),
    NFSBackend(),
]


# ---------------------------------------------------------------------------
# GTK UI
# ---------------------------------------------------------------------------

class BackendCard(Gtk.ListBoxRow):
    def __init__(self, backend: CloudBackend, app: "CloudApp"):
        super().__init__()
        self.backend = backend
        self.app = app

        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12, margin=12)
        self.add(box)

        icon = Gtk.Image.new_from_icon_name(backend.icon, Gtk.IconSize.DIALOG)
        box.pack_start(icon, False, False, 0)

        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        vbox.set_hexpand(True)
        box.pack_start(vbox, True, True, 0)

        title = Gtk.Label()
        title.set_markup(f"<b>{backend.name}</b>")
        title.set_xalign(0)
        vbox.pack_start(title, False, False, 0)

        desc = Gtk.Label(label=backend.description)
        desc.set_xalign(0)
        desc.set_line_wrap(True)
        vbox.pack_start(desc, False, False, 0)

        self.status_label = Gtk.Label()
        self.status_label.set_xalign(0)
        self.status_label.set_line_wrap(True)
        self.status_label.set_selectable(True)
        vbox.pack_start(self.status_label, False, False, 0)

        self.hint_label = Gtk.Label()
        self.hint_label.set_xalign(0)
        self.hint_label.set_line_wrap(True)
        self.hint_label.set_selectable(True)
        vbox.pack_start(self.hint_label, False, False, 0)

        btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        vbox.pack_start(btn_box, False, False, 0)

        self.start_btn = Gtk.Button(label="Start")
        self.start_btn.connect("clicked", self._on_start)
        btn_box.pack_start(self.start_btn, False, False, 0)

        self.stop_btn = Gtk.Button(label="Stop")
        self.stop_btn.connect("clicked", self._on_stop)
        btn_box.pack_start(self.stop_btn, False, False, 0)

        self.action_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        btn_box.pack_start(self.action_box, False, False, 0)

        self.refresh()

    def refresh(self):
        detected = self.backend.detect()
        running = self.backend.is_running()
        status_color = "#2ecc71" if running else ("#f39c12" if detected else "#95a5a6")
        status_text = "Running" if running else ("Installed" if detected else "Not installed")
        lines = [f"<span foreground='{status_color}'><b>{status_text}</b></span>"]
        lines.extend(self.backend.status_lines())
        self.status_label.set_markup("\n".join(lines))

        self.start_btn.set_sensitive(detected and not running)
        self.stop_btn.set_sensitive(running)

        # rebuild action buttons
        for child in self.action_box.get_children():
            self.action_box.remove(child)
        for label, callback in self.backend.actions():
            btn = Gtk.Button(label=label)
            btn.connect("clicked", callback)
            self.action_box.pack_start(btn, False, False, 0)
        self.action_box.show_all()

        hint = self.backend.setup_hint()
        if hint and not running:
            self.hint_label.set_markup(f"<span size='small'>{hint}</span>")
            self.hint_label.show()
        else:
            self.hint_label.hide()

    def _on_start(self, _btn):
        self.app.log(f"Starting {self.backend.name}...")
        ok, err = self.backend.start()
        self.app.log(f"{self.backend.name} started." if ok else f"Failed: {err}")
        self.app.refresh_all()

    def _on_stop(self, _btn):
        self.app.log(f"Stopping {self.backend.name}...")
        ok, err = self.backend.stop()
        self.app.log(f"{self.backend.name} stopped." if ok else f"Failed: {err}")
        self.app.refresh_all()


class CloudApp(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Local Butler Cloud", default_width=700, default_height=650)
        self.set_icon_name("folder-remote")

        header = Gtk.HeaderBar(title="Local Butler Cloud", show_close_button=True)
        self.set_titlebar(header)

        refresh_btn = Gtk.Button(label="Refresh")
        refresh_btn.connect("clicked", lambda _b: self.refresh_all())
        header.pack_start(refresh_btn)

        self.status_label = Gtk.Label(label="Local-first cloud and sync hub")
        self.status_label.set_xalign(0)

        self.listbox = Gtk.ListBox()
        self.listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        self.cards: list[BackendCard] = []
        for backend in BACKENDS:
            card = BackendCard(backend, self)
            self.listbox.add(card)
            self.cards.append(card)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroll.add(self.listbox)

        self.log_view = Gtk.TextView(editable=False, monospace=True)
        self.log_view.set_wrap_mode(Gtk.WrapMode.WORD)
        self.log_buffer = self.log_view.get_buffer()
        log_sw = Gtk.ScrolledWindow()
        log_sw.set_vexpand(True)
        log_sw.set_max_content_height(180)
        log_sw.add(self.log_view)

        notebook = Gtk.Notebook()
        notebook.append_page(scroll, Gtk.Label(label="Backends"))
        notebook.append_page(log_sw, Gtk.Label(label="Log"))

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin=12)
        box.pack_start(self.status_label, False, False, 0)
        box.pack_start(notebook, True, True, 0)
        self.add(box)

        self.refresh_all()
        GLib.timeout_add_seconds(5, self.refresh_all)

    def log(self, text):
        GLib.idle_add(self._append_log, text)

    def _append_log(self, text):
        end = self.log_buffer.get_end_iter()
        self.log_buffer.insert(end, text + "\n")
        self.log_view.scroll_to_iter(self.log_buffer.get_end_iter(), 0.0, False, 0.0, 0.0)
        return False

    def refresh_all(self):
        for card in self.cards:
            card.refresh()
        running = sum(1 for c in self.cards if c.backend.is_running())
        installed = sum(1 for c in self.cards if c.backend.detect())
        self.status_label.set_markup(
            f"<b>Status:</b> {running} running / {installed} installed / {len(self.cards)} options"
        )
        return True


class App(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="com.localbutler.Cloud", flags=0)

    def do_activate(self):
        win = CloudApp(self)
        win.show_all()


def main():
    App().run(sys.argv)


if __name__ == "__main__":
    main()
