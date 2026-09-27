#!/home/bpoppa12/.local/share/uv/privacy-venv/bin/python
"""Privacy Dashboard — Camera & Microphone Monitor

A system-tray daemon that watches for processes using the camera or
microphone. Default-deny: any access that is not explicitly allowed triggers
a desktop alert with Block (default), Allow, Ask, and Kill options.

Usage:
    local-butler-privacy-dashboard          # foreground
    local-butler-privacy-dashboard --daemon # starts in background (no tray window)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk

import psutil
import pystray
from PIL import Image, ImageDraw


# -----------------------------------------------------------------------------
# Paths
# -----------------------------------------------------------------------------

HOME = Path.home()
DATA_DIR = HOME / ".local" / "share" / "local-butler"
DATA_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR = DATA_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_DIR = HOME / ".config" / "local-butler"
CONFIG_DIR.mkdir(parents=True, exist_ok=True)

CONFIG_PATH = CONFIG_DIR / "privacy-dashboard.json"
LOG_PATH = LOG_DIR / "privacy-dashboard.log"
ICON_PATH = DATA_DIR / "icons" / "privacy-safe.png"
ICON_DIR = DATA_DIR / "icons"
ICON_DIR.mkdir(parents=True, exist_ok=True)


# -----------------------------------------------------------------------------
# Icon generator
# -----------------------------------------------------------------------------


def make_icon(color: str, letter: str = "P") -> Image.Image:
    """Create a small square tray icon."""
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    # background circle
    draw.ellipse([2, 2, size - 2, size - 2], fill=color)
    # letter
    draw.text(
        (size // 2, size // 2),
        letter,
        fill=(255, 255, 255, 255),
        anchor="mm",
    )
    return img


def ensure_icons() -> None:
    for name, color in [("safe", "#22c55e"), ("alert", "#ef4444"), ("ask", "#eab308")]:
        path = ICON_DIR / f"privacy-{name}.png"
        if not path.exists():
            make_icon(color).save(path)


# -----------------------------------------------------------------------------
# Configuration / Policy
# -----------------------------------------------------------------------------


@dataclass
class Policy:
    allowlist: dict[str, str] = field(default_factory=dict)
    denylist: dict[str, str] = field(default_factory=dict)
    auto_kill: bool = False
    poll_interval: float = 2.0

    def save(self) -> None:
        CONFIG_PATH.write_text(
            json.dumps(
                {
                    "allowlist": self.allowlist,
                    "denylist": self.denylist,
                    "auto_kill": self.auto_kill,
                    "poll_interval": self.poll_interval,
                },
                indent=2,
            )
        )

    @classmethod
    def load(cls) -> "Policy":
        if CONFIG_PATH.exists():
            try:
                data = json.loads(CONFIG_PATH.read_text())
                return cls(
                    allowlist=data.get("allowlist", {}),
                    denylist=data.get("denylist", {}),
                    auto_kill=data.get("auto_kill", False),
                    poll_interval=data.get("poll_interval", 2.0),
                )
            except Exception:
                pass
        policy = cls()
        policy.save()
        return policy


# -----------------------------------------------------------------------------
# Logging
# -----------------------------------------------------------------------------


def log_event(message: str) -> None:
    timestamp = datetime.now().isoformat()
    line = f"{timestamp} {message}"
    LOG_PATH.open("a").write(line + "\n")
    print(line, flush=True)


# -----------------------------------------------------------------------------
# Monitors
# -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Access:
    device: str  # "camera" or "microphone"
    pid: int | None
    process_name: str
    details: str = ""


def _process_name(pid: int | None) -> str:
    if pid is None:
        return "unknown"
    try:
        proc = psutil.Process(pid)
        return proc.name()
    except Exception:
        return str(pid)


def detect_camera_users() -> list[Access]:
    """Detect processes using /dev/video* devices."""
    devices = []
    for dev in Path("/dev").glob("video*"):
        devices.append(str(dev))
    if not devices:
        return []

    accesses: list[Access] = []
    try:
        result = subprocess.run(
            ["fuser", "-v"] + devices,
            capture_output=True,
            text=True,
        )
        # fuser -v writes to stderr
        text = result.stdout + result.stderr
        for line in text.splitlines():
            if line.startswith("/dev/video"):
                continue
            parts = line.split()
            if len(parts) < 3:
                continue
            # fuser -v output: USER PID ACCESS COMMAND  or  /dev/... lines
            if parts[0] in ("USER", "ACCESS"):
                continue
            # try to find pid column
            for token in parts:
                if token.isdigit():
                    pid = int(token)
                    name = _process_name(pid)
                    accesses.append(
                        Access(device="camera", pid=pid, process_name=name)
                    )
                    break
    except Exception as exc:
        log_event(f"camera-monitor error: {exc}")
    return accesses


def _parse_pw_dump_nodes() -> list[dict[str, Any]]:
    """Return PipeWire nodes that look like input streams or sources in active use."""
    try:
        result = subprocess.run(
            ["pw-dump"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        data = json.loads(result.stdout)
    except Exception:
        return []
    nodes = []
    for item in data:
        if item.get("type") != "PipeWire:Interface:Node":
            continue
        info = item.get("info", {})
        props = info.get("props", {})
        media_class = props.get("media.class", "")
        if media_class == "Stream/Input/Audio":
            nodes.append({
                "name": props.get("application.name") or props.get("node.name") or "unknown",
                "binary": props.get("application.process.binary", ""),
                "pid": props.get("application.process.id"),
                "state": info.get("state", "unknown"),
            })
    return nodes


def detect_microphone_users() -> list[Access]:
    """Detect active microphone capture streams via PipeWire."""
    accesses: list[Access] = []
    try:
        nodes = _parse_pw_dump_nodes()
        for node in nodes:
            state = node.get("state", "").lower()
            if state not in ("running", "idle"):
                continue
            name = node.get("binary") or node.get("name") or "unknown"
            pid = node.get("pid")
            pid_int = int(pid) if pid and str(pid).isdigit() else None
            if pid_int is None:
                pid_int = _find_pid_by_name(name)
            accesses.append(
                Access(
                    device="microphone",
                    pid=pid_int,
                    process_name=name,
                    details=node.get("name", ""),
                )
            )
    except Exception as exc:
        log_event(f"mic-monitor error: {exc}")
    return accesses


def _find_pid_by_name(name: str) -> int | None:
    """Best-effort PID lookup from process name or command line."""
    if not name or name == "unknown":
        return None
    try:
        for proc in psutil.process_iter(["pid", "name", "cmdline"]):
            info = proc.info
            if info.get("name") == name:
                return info.get("pid")
        for proc in psutil.process_iter(["pid", "name", "cmdline"]):
            info = proc.info
            cmdline = info.get("cmdline") or []
            if any(name in arg for arg in cmdline):
                return info.get("pid")
    except Exception:
        pass
    return None


# -----------------------------------------------------------------------------
# Prompt / Action
# -----------------------------------------------------------------------------


PROMPT_LOCK = threading.Lock()
PROMPT_ACTIVE: set[tuple[str, str]] = set()  # (device, process_name)


def prompt_user(access: Access) -> str | None:
    """Show a zenity dialog and return the chosen action."""
    if not shutil.which("zenity"):
        log_event("zenity not found; cannot prompt user")
        return None

    title = "Privacy Alert"
    text = (
        f"<big><b>{access.process_name}</b></big> is trying to use your\n"
        f"<big><b>{access.device.upper()}</b></big>.\n\n"
        f"What do you want to do?"
    )

    try:
        result = subprocess.run(
            [
                "zenity",
                "--list",
                f"--title={title}",
                f"--text={text}",
                "--column=Action",
                "Block (default)",
                "Allow",
                "Ask again later",
                "Kill process",
                "--hide-header",
                "--width=400",
                "--height=250",
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        action = result.stdout.strip()
        if not action:
            return None
        # Normalize
        if action.startswith("Block"):
            return "block"
        if action.startswith("Allow"):
            return "allow"
        if action.startswith("Ask"):
            return "ask"
        if action.startswith("Kill"):
            return "kill"
        return action.lower()
    except subprocess.TimeoutExpired:
        log_event(f"prompt timeout for {access.process_name} {access.device}")
        return None
    except Exception as exc:
        log_event(f"prompt error: {exc}")
        return None


def notify(message: str, urgency: str = "normal") -> None:
    try:
        subprocess.run(
            ["notify-send", f"--urgency={urgency}", "Privacy Dashboard", message],
            check=False,
            timeout=10,
        )
    except Exception:
        pass


def kill_process(pid: int | None, name: str) -> bool:
    if pid is None:
        return False
    proc = None
    try:
        proc = psutil.Process(pid)
        proc.terminate()
        proc.wait(timeout=3)
        log_event(f"terminated process {name} (pid {pid})")
        return True
    except Exception:
        if proc is not None:
            try:
                proc.kill()
                log_event(f"killed process {name} (pid {pid})")
                return True
            except Exception as exc:
                log_event(f"failed to kill {name} (pid {pid}): {exc}")
                return False
        log_event(f"failed to access process {name} (pid {pid})")
        return False


# -----------------------------------------------------------------------------
# Controller
# -----------------------------------------------------------------------------


class PrivacyDashboard:
    def __init__(self) -> None:
        self.policy = Policy.load()
        self.last_access_hash: set[tuple[str, str]] = set()
        self.tray_icon: pystray.Icon | None = None
        self.status_lock = threading.Lock()
        self.status = "safe"  # safe | ask | alert

    def run(self, daemon_mode: bool = False) -> None:
        ensure_icons()
        monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
        monitor_thread.start()

        if daemon_mode:
            # Keep main thread alive.
            try:
                while True:
                    time.sleep(60)
            except KeyboardInterrupt:
                pass
            return

        # Run tray icon on main thread.
        self.tray_icon = pystray.Icon(
            "privacy-dashboard",
            icon=Image.open(ICON_DIR / "privacy-safe.png"),
            title="Privacy Dashboard — safe",
            menu=self._build_menu(),
        )
        self.tray_icon.run()

    def _do_nothing(self, icon: pystray.Icon, item: pystray.MenuItem) -> None:
        pass

    def _quit(self, icon: pystray.Icon, item: pystray.MenuItem) -> None:
        icon.stop()

    def _show_logs(self, icon: pystray.Icon, item: pystray.MenuItem) -> None:
        try:
            subprocess.Popen(["xdg-open", str(LOG_PATH)])
        except Exception:
            pass

    def _toggle_auto_kill(self, icon: pystray.Icon, item: pystray.MenuItem) -> None:
        self.policy.auto_kill = not self.policy.auto_kill
        self.policy.save()
        log_event(f"auto_kill set to {self.policy.auto_kill}")
        self._update_menu()

    def _build_menu(self) -> pystray.Menu:
        auto_kill_label = f"Auto-kill {'ON' if self.policy.auto_kill else 'off'}"
        return pystray.Menu(
            pystray.MenuItem(f"Status: {self.status}", self._do_nothing),
            pystray.MenuItem("Show logs", self._show_logs),
            pystray.MenuItem(auto_kill_label, self._toggle_auto_kill),
            pystray.MenuItem("Quit", self._quit),
        )

    def _update_menu(self) -> None:
        if not self.tray_icon:
            return
        self.tray_icon.menu = self._build_menu

    def _set_status(self, status: str, message: str = "") -> None:
        with self.status_lock:
            self.status = status
        if self.tray_icon:
            icon_name = {"safe": "safe", "ask": "ask", "alert": "alert"}.get(status, "safe")
            self.tray_icon.icon = Image.open(ICON_DIR / f"privacy-{icon_name}.png")
            self.tray_icon.title = f"Privacy Dashboard — {status}"
            self.tray_icon.update_menu()
        if message:
            notify(message, urgency="critical" if status == "alert" else "normal")

    def _monitor_loop(self) -> None:
        while True:
            accesses = detect_camera_users() + detect_microphone_users()
            current = set()
            active_alerts: list[Access] = []

            for access in accesses:
                key = (access.device, access.process_name)
                current.add(key)
                proc_key = access.process_name

                allowed_for = self.policy.allowlist.get(proc_key)
                if allowed_for is not None:
                    devices = allowed_for if isinstance(allowed_for, list) else [allowed_for]
                    if access.device in devices:
                        continue
                if proc_key in self.policy.denylist:
                    log_event(f"denied {access.process_name} tried {access.device} (pid:{access.pid})")
                    kill_process(access.pid, access.process_name)
                    active_alerts.append(access)
                    continue

                if self.policy.auto_kill:
                    log_event(f"auto-killed {access.process_name} {access.device} (pid:{access.pid})")
                    kill_process(access.pid, access.process_name)
                    active_alerts.append(access)
                    continue

                # Show prompt if not already showing for this app/device.
                if key not in PROMPT_ACTIVE:
                    PROMPT_ACTIVE.add(key)
                    self._set_status("ask", f"{access.process_name} wants {access.device}")
                    action = prompt_user(access)
                    PROMPT_ACTIVE.discard(key)

                    if action == "allow":
                        self.policy.allowlist[proc_key] = access.device
                        log_event(f"allowed {access.process_name} for {access.device}")
                        self.policy.save()
                    elif action in ("block", "kill"):
                        self.policy.denylist[proc_key] = access.device
                        kill_process(access.pid, access.process_name)
                        log_event(f"blocked {access.process_name} {access.device}")
                        self.policy.save()
                    elif action == "ask":
                        # do nothing now; will ask again next poll
                        pass

            if active_alerts:
                self._set_status("alert", "Unauthorized camera/microphone access detected")
            elif accesses:
                self._set_status("safe")
            else:
                self._set_status("safe")

            self.last_access_hash = current
            time.sleep(self.policy.poll_interval)


def main() -> int:
    parser = argparse.ArgumentParser(description="Privacy Dashboard")
    parser.add_argument("--daemon", action="store_true", help="Run in background without tray window")
    args = parser.parse_args()

    if not args.daemon:
        # Only initialize GTK in GUI/tray mode (needed for dialogs).
        Gtk.init_check()

    dashboard = PrivacyDashboard()
    log_event(f"Privacy Dashboard started (daemon={args.daemon})")
    dashboard.run(daemon_mode=args.daemon)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
