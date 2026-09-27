"""Local Butler Diagnostic — GTK system troubleshooter for Linux."""

from __future__ import annotations

import argparse
import datetime
import glob
import inspect
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk

import local_butler.apps.diagnostic.gaming as gaming

HOME = Path.home()
TMP_DIR = Path(tempfile.gettempdir())


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def run(cmd: list[str], timeout: float = 15.0, **kwargs) -> subprocess.CompletedProcess:
    """Run a command and return a CompletedProcess with stdout/stderr."""
    try:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            **kwargs,
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, 1, "", "command timed out")
    except FileNotFoundError:
        return subprocess.CompletedProcess(cmd, 127, "", f"{cmd[0]} not found")


def has_command(name: str) -> bool:
    return shutil.which(name) is not None


def active_state(unit: str, user: bool = True) -> str:
    flag = "--user" if user else "--system"
    res = run(["systemctl", flag, "is-active", unit], timeout=5)
    return res.stdout.strip()


def pretty_bytes(num: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if num < 1024:
            return f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} PB"


def dir_size(path: Path) -> int:
    total = 0
    try:
        for entry in os.scandir(path):
            if entry.is_symlink():
                continue
            if entry.is_file(follow_symlinks=False):
                total += entry.stat(follow_symlinks=False).st_size
            elif entry.is_dir(follow_symlinks=False):
                total += dir_size(Path(entry.path))
    except PermissionError:
        pass
    return total


def get_camera_users() -> list[dict]:
    """Return processes currently holding /dev/video* devices."""
    devices = glob.glob("/dev/video*")
    if not devices:
        return []
    res = run(["lsof"] + devices, timeout=5)
    users: list[dict] = []
    for line in res.stdout.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 2:
            continue
        users.append(
            {
                "type": "camera",
                "name": parts[0],
                "pid": parts[1],
                "device": parts[-1] if parts[-1].startswith("/dev/") else "",
            }
        )
    return users


def get_mic_users() -> list[dict]:
    """Return PipeWire clients that are recording from an audio source."""
    res = run(["pw-dump", "Node"], timeout=5)
    if res.returncode != 0:
        return []
    try:
        nodes = json.loads(res.stdout)
    except Exception:
        return []
    users: list[dict] = []
    seen = set()
    for node in nodes:
        props = node.get("info", {}).get("props", {})
        if props.get("media.class") != "Stream/Input/Audio":
            continue
        pid = props.get("application.process.id", "?")
        name = props.get("application.name") or props.get("application.process.binary") or "unknown"
        key = (name, str(pid))
        if key in seen:
            continue
        seen.add(key)
        users.append({"type": "microphone", "name": name, "pid": str(pid)})
    return users


def kill_process(pid: int, sig: int = signal.SIGTERM) -> tuple[bool, str]:
    """Kill a process. Uses pkexec if it is owned by another user."""
    try:
        uid_res = run(["ps", "-p", str(pid), "-o", "uid="], timeout=3)
        proc_uid = uid_res.stdout.strip()
        my_uid = str(os.getuid())
        cmd = ["kill", f"-{sig}", str(pid)]
        if proc_uid and proc_uid != my_uid and shutil.which("pkexec"):
            cmd = ["pkexec"] + cmd
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode == 0:
            return True, ""
        return False, res.stderr.strip()
    except Exception as exc:
        return False, str(exc)


def get_running_user_services() -> list[dict]:
    res = run(
        ["systemctl", "--user", "list-units", "--type=service", "--state=running", "--no-pager", "--plain"],
        timeout=10,
    )
    services: list[dict] = []
    for line in res.stdout.splitlines():
        line = line.strip()
        if not line or line.startswith("UNIT") or line.startswith("LOAD"):
            continue
        parts = line.split(None, 4)
        if len(parts) < 4:
            continue
        services.append(
            {
                "type": "service",
                "name": parts[0],
                "status": parts[3],
                "description": parts[4] if len(parts) > 4 else "",
            }
        )
    return services


def get_running_processes(limit: int = 50) -> list[dict]:
    res = run(
        ["ps", "-eo", "pid,pcpu,pmem,comm,args", "--sort=-%cpu"],
        timeout=10,
    )
    processes: list[dict] = []
    for i, line in enumerate(res.stdout.splitlines()[1:]):
        if i >= limit:
            break
        parts = line.split(None, 4)
        if len(parts) < 5:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        processes.append(
            {
                "type": "process",
                "pid": pid,
                "cpu": parts[1],
                "mem": parts[2],
                "name": parts[3],
                "cmd": parts[4],
            }
        )
    return processes


# ---------------------------------------------------------------------------
# Check data model
# ---------------------------------------------------------------------------

@dataclass
class CheckResult:
    name: str
    status: str  # ok, warn, error, info
    summary: str
    detail: str = ""
    fix_command: list[str] | None = None
    fix_callback: Callable[..., None] | None = None
    fix_label: str = ""
    category: str = ""

    def icon(self) -> str:
        return {
            "ok": "✓",
            "warn": "⚠",
            "error": "✗",
            "info": "ℹ",
        }.get(self.status, "?")


@dataclass
class Check:
    name: str
    category: str
    func: Callable[[], CheckResult]


# ---------------------------------------------------------------------------
# Diagnostic checks
# ---------------------------------------------------------------------------

def _find_onboard_source_id() -> str | None:
    """Return the node.name of the first onboard analog microphone source."""
    res = run(["pw-dump", "Node"], timeout=5)
    if res.returncode != 0:
        return None
    try:
        nodes = json.loads(res.stdout)
    except Exception:
        return None
    for node in nodes:
        props = node.get("info", {}).get("props", {})
        if props.get("media.class") == "Audio/Source":
            name = props.get("node.name", "")
            name_lower = name.lower()
            if "pci" in name_lower and "usb" not in name_lower and "hdmi" not in name_lower and "blue" not in name_lower:
                return name
    return None


def _node_exists(node_name: str) -> bool:
    """Return True if a PipeWire node with the given node.name exists."""
    res = run(["pw-dump", "Node"], timeout=5)
    if res.returncode != 0:
        return False
    try:
        nodes = json.loads(res.stdout)
    except Exception:
        return False
    for node in nodes:
        props = node.get("info", {}).get("props", {})
        if props.get("node.name") == node_name:
            return True
    return False


def check_default_source() -> CheckResult:
    res = run(["wpctl", "status"], timeout=5)
    if res.returncode != 0:
        return CheckResult(
            "Default audio source",
            "error",
            "PipeWire/WirePlumber not responding",
            res.stderr,
            category="Audio",
        )

    configured = None
    in_settings = False
    for line in res.stdout.splitlines():
        if "Default Configured Devices" in line:
            in_settings = True
        if in_settings and "Audio/Source" in line:
            configured = line.strip().split("alsa_")[-1] if "alsa_" in line else line.strip()
            break

    present_names = []
    in_sources = False
    for line in res.stdout.splitlines():
        if line.strip().startswith("Sources:"):
            in_sources = True
            continue
        if in_sources and line.strip().startswith("Streams:"):
            break
        if in_sources and ("Analog Stereo" in line or "USB Audio" in line):
            present_names.append(line.strip())

    if not configured:
        return CheckResult(
            "Default audio source",
            "info",
            "Could not determine default source",
            res.stdout,
            category="Audio",
        )

    full_name = "alsa_" + configured
    node_present = _node_exists(full_name)
    onboard_id = _find_onboard_source_id()
    detail = "\n".join(present_names) or res.stdout

    if "pci" in configured:
        status = "ok" if node_present else "error"
        summary = (
            f"Default source is onboard/PCI: {configured}"
            if node_present
            else f"Default source points to a missing onboard device: {configured}"
        )
        return CheckResult(
            "Default audio source",
            status,
            summary,
            detail,
            fix_command=["wpctl", "set-default", onboard_id] if onboard_id and not node_present else None,
            fix_label="Set default to onboard mic",
            category="Audio",
        )

    if "usb" in configured:
        if node_present:
            friendly = configured.split(".")[-2] if "." in configured else configured
            summary = f"Default source is the {friendly} USB microphone"
            detail_text = "The USB mic is present and selected. Use the fix button to switch to the onboard mic if you unplug it."
            status = "ok"
        else:
            summary = f"Default source points to a missing USB device: {configured}"
            detail_text = "Set the default source to the onboard microphone."
            status = "error"
        return CheckResult(
            "Default audio source",
            status,
            summary,
            detail + "\n" + detail_text,
            fix_command=["wpctl", "set-default", onboard_id] if onboard_id else None,
            fix_label="Set default to onboard mic",
            category="Audio",
        )

    return CheckResult(
        "Default audio source",
        "info",
        "Could not determine default source",
        res.stdout,
        category="Audio",
    )


def check_mic_capture() -> CheckResult:
    if not has_command("arecord"):
        return CheckResult(
            "Microphone capture test",
            "warn",
            "arecord not installed",
            "Install alsa-utils to enable the live capture test.",
            category="Audio",
        )
    test_path = TMP_DIR / "local_butler_mic_test.wav"
    if test_path.exists():
        test_path.unlink()
    res = run(
        ["arecord", "-D", "default", "-d", "1", "-f", "cd", str(test_path)],
        timeout=8,
    )
    if res.returncode != 0 or not test_path.exists() or test_path.stat().st_size < 1000:
        return CheckResult(
            "Microphone capture test",
            "error",
            "Could not record audio from the default mic",
            f"{res.stderr}\n{res.stdout}".strip(),
            category="Audio",
        )
    return CheckResult(
        "Microphone capture test",
        "ok",
        f"Recorded {pretty_bytes(test_path.stat().st_size)} of audio in 1 second",
        category="Audio",
    )


def _reset_camera() -> None:
    """Run the one-click USB camera reset helper."""
    script = HOME / ".local" / "bin" / "local-butler-fix-camera"
    if shutil.which("pkexec"):
        subprocess.Popen(["pkexec", str(script)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        subprocess.Popen([str(script)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def check_camera_present() -> CheckResult:
    if not has_command("v4l2-ctl"):
        return CheckResult(
            "Camera device presence",
            "warn",
            "v4l2-ctl not installed",
            "Install v4l-utils to enable camera diagnostics.",
            category="Camera",
        )
    res = run(["v4l2-ctl", "--list-devices"], timeout=5)
    if res.returncode != 0 or "/dev/video" not in res.stdout:
        return CheckResult(
            "Camera device presence",
            "error",
            "No V4L2 camera detected",
            res.stderr or res.stdout or "Check that the webcam is plugged in.",
            fix_callback=_reset_camera,
            fix_label="Reset USB camera",
            category="Camera",
        )
    return CheckResult(
        "Camera device presence",
        "ok",
        "Camera detected",
        res.stdout,
        category="Camera",
    )


def check_camera_capture() -> CheckResult:
    if not Path("/dev/video0").exists():
        return CheckResult(
            "Camera capture test",
            "error",
            "/dev/video0 not present",
            "No camera to test.",
            category="Camera",
        )
    test_frame = TMP_DIR / "local_butler_camera_frame.jpg"
    test_frame.unlink(missing_ok=True)
    res = run(
        [
            "ffmpeg",
            "-f", "v4l2",
            "-input_format", "mjpeg",
            "-video_size", "640x480",
            "-i", "/dev/video0",
            "-vframes", "1",
            str(test_frame),
            "-y",
        ],
        timeout=10,
    )
    if res.returncode != 0 or not test_frame.exists() or test_frame.stat().st_size < 1000:
        detail = res.stderr.strip().splitlines()[-5:] if res.stderr else ""
        return CheckResult(
            "Camera capture test",
            "error",
            "Cannot capture video from /dev/video0",
            "\n".join(detail) or "The camera enumerates but does not stream. Check dmesg for USB errors.",
            category="Camera",
        )
    return CheckResult(
        "Camera capture test",
        "ok",
        f"Captured a frame ({pretty_bytes(test_frame.stat().st_size)})",
        category="Camera",
    )


def check_usb_errors() -> CheckResult:
    res = run(["dmesg"], timeout=5)
    if res.returncode != 0:
        return CheckResult(
            "USB errors",
            "warn",
            "Could not read kernel log",
            res.stderr,
            category="USB",
        )
    lines = res.stdout.splitlines()
    # Only count errors that happened after the Logitech BRIO last came back online.
    last_brio_idx = -1
    for i, line in enumerate(lines):
        if "Found UVC 1.00 device Logitech BRIO" in line:
            last_brio_idx = i

    if last_brio_idx >= 0:
        candidate = lines[last_brio_idx:]
    else:
        candidate = lines[-200:]

    errors = [
        line
        for line in candidate
        if "error -" in line.lower()
        or "failed" in line.lower()
        or "unable to enumerate" in line.lower()
    ][-20:]

    if not errors:
        return CheckResult(
            "USB errors",
            "ok",
            "No USB errors since the camera last enumerated",
            category="USB",
        )

    status = "error" if any("unable to enumerate" in line.lower() for line in errors) else "warn"
    return CheckResult(
        "USB errors",
        status,
        f"{len(errors)} USB error lines since the camera last enumerated",
        "\n".join(errors),
        category="USB",
    )


def check_privacy_dashboard() -> CheckResult:
    state = active_state("privacy-dashboard")
    if state == "active":
        return CheckResult(
            "Privacy dashboard",
            "warn",
            "Privacy dashboard is running and may block camera/mic access",
            "Apps like WhatsApp, Jitsi and Brave may be killed when they try to use the camera/microphone.",
            fix_command=["systemctl", "--user", "stop", "privacy-dashboard"],
            fix_label="Stop privacy dashboard",
            category="Privacy",
        )
    return CheckResult(
        "Privacy dashboard",
        "ok",
        f"Privacy dashboard is {state}",
        category="Privacy",
    )


def _get_installed_flatpaks() -> set[str]:
    res = run(["flatpak", "list", "--app", "--columns=application"], timeout=5)
    if res.returncode != 0:
        return set()
    return {line.strip() for line in res.stdout.splitlines() if line.strip() and not line.startswith("Application")}


def _get_installed_desktop_apps() -> dict[str, dict]:
    """Return installed desktop apps under /usr/share/applications and ~/.local/share/applications."""
    apps: dict[str, dict] = {}
    for app_dir in ("/usr/share/applications", str(HOME / ".local" / "share" / "applications")):
        if not Path(app_dir).exists():
            continue
        for entry in Path(app_dir).glob("*.desktop"):
            try:
                name = entry.stem
                exec_line = ""
                icon = ""
                comment = ""
                for line in entry.read_text("utf-8", errors="ignore").splitlines():
                    if line.startswith("Name=") and not line.startswith("Name["):
                        name = line.split("=", 1)[1].strip()
                    elif line.startswith("Exec="):
                        exec_line = line.split("=", 1)[1].strip()
                    elif line.startswith("Icon="):
                        icon = line.split("=", 1)[1].strip()
                    elif line.startswith("Comment=") and not comment:
                        comment = line.split("=", 1)[1].strip()
                apps[name] = {
                    "id": name,
                    "file": entry,
                    "exec": exec_line,
                    "icon": icon,
                    "category": comment or "Desktop app",
                }
            except Exception:
                continue
    return apps


# Mapping of known communication/media apps to their detection methods.
_APP_REGISTRY: list[dict] = [
    {"id": "brave", "name": "Brave", "type": "rpm", "binaries": ["brave", "brave-browser"], "cat": "Web browser"},
    {"id": "firefox", "name": "Firefox", "type": "rpm", "binaries": ["firefox"], "cat": "Web browser"},
    {"id": "mullvad", "name": "Mullvad VPN", "type": "rpm", "binaries": ["mullvad-vpn"], "cat": "VPN"},
    {"id": "protonmail-bridge", "name": "Proton Mail Bridge", "type": "rpm", "binaries": ["protonmail-bridge"], "cat": "Mail"},
    {"id": "evolution", "name": "Evolution", "type": "rpm", "binaries": ["evolution"], "cat": "Mail"},
    {"id": "jitsi-flatpak", "name": "Jitsi", "type": "flatpak", "app_id": "org.jitsi.jitsi-meet", "cat": "Video conferencing"},
    {"id": "viber-flatpak", "name": "Viber", "type": "flatpak", "app_id": "com.viber.Viber", "cat": "Messaging"},
    {"id": "telegram-flatpak", "name": "Telegram", "type": "flatpak", "app_id": "org.telegram.desktop", "cat": "Messaging"},
    {"id": "whatsapp-flatpak", "name": "WhatsApp Desktop", "type": "flatpak", "app_id": "io.github.mimbrero.WhatsAppDesktop", "cat": "Messaging"},
    {"id": "signal-flatpak", "name": "Signal", "type": "flatpak", "app_id": "org.signal.Signal", "cat": "Messaging"},
    {"id": "teams-flatpak", "name": "Microsoft Teams", "type": "flatpak", "app_id": "com.microsoft.Teams", "cat": "Video conferencing"},
    {"id": "skype-flatpak", "name": "Skype", "type": "flatpak", "app_id": "com.skype.Client", "cat": "Video conferencing"},
    {"id": "discord-flatpak", "name": "Discord", "type": "flatpak", "app_id": "com.discordapp.Discord", "cat": "Messaging"},
    {"id": "zoom-flatpak", "name": "Zoom", "type": "flatpak", "app_id": "us.zoom.Zoom", "cat": "Video conferencing"},
]


def _is_installed(app: dict) -> bool:
    if app["type"] == "flatpak" and app["app_id"] in _get_installed_flatpaks():
        return True
    if app["type"] == "rpm":
        for binary in app.get("binaries", []):
            if shutil.which(binary):
                return True
    return False


def _check_app_permissions(app: dict) -> CheckResult:
    name = app["name"]
    if app["type"] == "flatpak":
        return _make_flatpak_check(app["app_id"], name)
    if app.get("cat") == "Web browser":
        return CheckResult(
            f"{name}",
            "info",
            f"{name} is installed. Camera/mic permissions are managed per-site in the browser.",
            category="Apps",
        )
    return CheckResult(
        f"{name}",
        "info",
        f"{name} is installed. It is a native app; check its own settings for camera/mic access.",
        category="Apps",
    )


def _get_installed_app_checks() -> list[Check]:
    checks: list[Check] = []
    for app in _APP_REGISTRY:
        if _is_installed(app):
            checks.append(Check(app["name"], "Apps", lambda app=app: _check_app_permissions(app)))
    return checks


def _flatpak_perm(app_id: str) -> tuple[str, str]:
    res = run(["flatpak", "permission-show", app_id], timeout=5)
    mic_ok = any(
        line.startswith("devices") and "microphone" in line and "yes" in line
        for line in res.stdout.splitlines()
    )
    cam_ok = any(
        line.startswith("devices") and "camera" in line and "yes" in line
        for line in res.stdout.splitlines()
    )
    return ("ok" if mic_ok else "error", "ok" if cam_ok else "error")


def _make_flatpak_check(app_id: str, friendly: str) -> CheckResult:
    mic, cam = _flatpak_perm(app_id)
    if mic == "ok" and cam == "ok":
        return CheckResult(
            f"{friendly} permissions",
            "ok",
            f"{friendly} has camera and microphone portal permissions",
            category="Apps",
        )
    summary = f"{friendly} is missing {'camera' if cam != 'ok' else ''}{' and ' if cam != 'ok' and mic != 'ok' else ''}{'microphone' if mic != 'ok' else ''} permission"

    def _grant() -> None:
        subprocess.run(["flatpak", "permission-set", "devices", "camera", app_id, "yes"], check=False)
        subprocess.run(["flatpak", "permission-set", "devices", "microphone", app_id, "yes"], check=False)

    return CheckResult(
        f"{friendly} permissions",
        "warn" if cam == "error" and mic == "error" else "warn",
        summary,
        f"camera={cam}, microphone={mic}",
        fix_callback=_grant,
        fix_label=f"Grant {friendly} camera/mic",
        category="Apps",
    )


def check_jitsi_permissions() -> CheckResult:
    if not shutil.which("flatpak"):
        return CheckResult("Jitsi permissions", "info", "Flatpak not installed", category="Apps")
    return _make_flatpak_check("org.jitsi.jitsi-meet", "Jitsi")


def _check_installed_flatpak_perm(app_id: str, friendly: str) -> CheckResult:
    if not shutil.which("flatpak"):
        return CheckResult(f"{friendly} permissions", "info", "Flatpak not installed", category="Apps")
    installed = run(["flatpak", "list", "--app"], timeout=5)
    if app_id not in installed.stdout:
        return CheckResult(f"{friendly} permissions", "info", f"{friendly} is not installed", category="Apps")
    return _make_flatpak_check(app_id, friendly)


def check_brave_permissions() -> CheckResult:
    if shutil.which("brave") or shutil.which("brave-browser"):
        return CheckResult(
            "Brave (WhatsApp Web)",
            "info",
            "Native Brave is installed; WhatsApp Web camera/mic permissions are managed per-site in the browser",
            category="Apps",
        )
    return _check_installed_flatpak_perm("com.brave.Browser", "Brave (WhatsApp Web)")


def check_telegram_permissions() -> CheckResult:
    return _check_installed_flatpak_perm("org.telegram.desktop", "Telegram")


def check_signal_permissions() -> CheckResult:
    return _check_installed_flatpak_perm("org.signal.Signal", "Signal")


def check_teams_permissions() -> CheckResult:
    return _check_installed_flatpak_perm("com.microsoft.Teams", "Microsoft Teams")


def check_skype_permissions() -> CheckResult:
    return _check_installed_flatpak_perm("com.skype.Client", "Skype")


def check_discord_permissions() -> CheckResult:
    return _check_installed_flatpak_perm("com.discordapp.Discord", "Discord")


def _open_update_manager(parent: Gtk.Window | None = None) -> None:
    """Open the GUI update manager, or upgrade everything when running headless/CLI."""
    if parent is None:
        subprocess.Popen(
            ["pkexec", "dnf", "upgrade", "-y"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    else:
        UpdateManagerDialog(parent)


def check_system_updates() -> CheckResult:
    if not shutil.which("dnf"):
        return CheckResult("System updates", "info", "dnf not available", category="Maintenance")
    res = run(["dnf", "check-update", "-q"], timeout=60)
    # dnf returns 100 when updates are available
    if res.returncode == 0:
        return CheckResult("System updates", "ok", "System is up to date", category="Maintenance")
    if res.returncode == 100:
        lines = [l.strip() for l in res.stdout.splitlines() if l.strip() and not l.startswith("=")]
        return CheckResult(
            "System updates",
            "warn",
            f"{len(lines)} package(s) can be updated",
            "\n".join(lines[:20]),
            fix_callback=_open_update_manager,
            fix_label="Manage updates",
            category="Maintenance",
        )
    return CheckResult(
        "System updates",
        "error",
        "Failed to check for updates",
        res.stderr,
        category="Maintenance",
    )


def check_temp_size() -> CheckResult:
    size = dir_size(TMP_DIR)
    status = "warn" if size > 5 * 1024**3 else "ok"
    return CheckResult(
        "Temporary files",
        status,
        f"{TMP_DIR} is using {pretty_bytes(size)}",
        category="Maintenance",
    )


def check_viber_permissions() -> CheckResult:
    if not shutil.which("flatpak"):
        return CheckResult("Viber permissions", "info", "Flatpak not installed", category="Apps")
    return _make_flatpak_check("com.viber.Viber", "Viber")


def check_zoom_permissions() -> CheckResult:
    if not shutil.which("flatpak"):
        return CheckResult("Zoom permissions", "info", "Flatpak not installed", category="Apps")
    zoom_check = run(["flatpak", "list", "--app"], timeout=5)
    if "us.zoom.Zoom" not in zoom_check.stdout:
        return CheckResult("Zoom permissions", "info", "Zoom is not installed", category="Apps")
    return _make_flatpak_check("us.zoom.Zoom", "Zoom")


def check_cheese_config() -> CheckResult:
    if not shutil.which("gsettings"):
        return CheckResult("Cheese camera config", "info", "gsettings not available", category="Camera")
    camera = run(["gsettings", "get", "org.gnome.Cheese", "camera"], timeout=3).stdout.strip().strip("'\"")
    photo_x = int(run(["gsettings", "get", "org.gnome.Cheese", "photo-x-resolution"], timeout=3).stdout.strip() or "0")
    photo_y = int(run(["gsettings", "get", "org.gnome.Cheese", "photo-y-resolution"], timeout=3).stdout.strip() or "0")
    detail = f"Camera: {camera}\nResolution: {photo_x}x{photo_y}"
    if not camera or "BRIO" not in camera:
        return CheckResult("Cheese camera config", "warn", "Cheese is not set to the BRIO", detail, category="Camera")
    if photo_x > 1280 or photo_y > 720:
        return CheckResult(
            "Cheese camera config",
            "warn",
            f"Cheese resolution ({photo_x}x{photo_y}) may be too high for the flaky USB controller",
            detail + "\n640x480 or 1280x720 is recommended.",
            category="Camera",
        )
    return CheckResult("Cheese camera config", "ok", f"Cheese set to {camera} at {photo_x}x{photo_y}", detail, category="Camera")


def check_portal_service() -> CheckResult:
    state = active_state("xdg-desktop-portal")
    if state == "active":
        return CheckResult("Desktop portal", "ok", "xdg-desktop-portal is running", category="Privacy")
    return CheckResult(
        "Desktop portal",
        "error",
        f"xdg-desktop-portal is {state}",
        "Camera/mic portal prompts for Flatpak apps and browsers will not work.",
        fix_command=["systemctl", "--user", "start", "xdg-desktop-portal"],
        fix_label="Start portal",
        category="Privacy",
    )


def _fix_audio_routing() -> None:
    script = HOME / ".local" / "bin" / "local-butler-fix-audio-routing"
    subprocess.Popen([str(script)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def check_audio_routing() -> CheckResult:
    sink_res = run(["wpctl", "inspect", "@DEFAULT_AUDIO_SINK@"], timeout=5)
    source_res = run(["wpctl", "inspect", "@DEFAULT_AUDIO_SOURCE@"], timeout=5)
    sink_name = re.search(r'node\.name\s*=\s*"([^"]+)"', sink_res.stdout)
    source_name = re.search(r'node\.name\s*=\s*"([^"]+)"', source_res.stdout)
    sink = sink_name.group(1) if sink_name else ""
    source = source_name.group(1) if source_name else ""

    onboard_sink_ok = "pci-0000_07_00.6" in sink and "output" in sink
    k66_present = shutil.which("pw-cli") and "K66" in run(["pw-cli", "ls", "Node"], timeout=5).stdout
    source_ok = "K66" in source if k66_present else True

    detail = f"Default output: {sink}\nDefault input:  {source}"
    if onboard_sink_ok and source_ok:
        return CheckResult("Audio routing", "ok", "Output is onboard and input is set correctly", detail, category="Audio")

    summary_parts = []
    if not onboard_sink_ok:
        summary_parts.append("output is not the onboard headphone/line-out")
    if k66_present and "K66" not in source:
        summary_parts.append("K66 mic is plugged in but not selected as input")
    summary = "; ".join(summary_parts) or "Audio routing is not optimal"
    return CheckResult(
        "Audio routing",
        "warn",
        summary,
        detail + "\nThis commonly happens when a headset/mic is plugged into the front panel.",
        fix_callback=_fix_audio_routing,
        fix_label="Fix audio routing",
        category="Audio",
    )


# ---------------------------------------------------------------------------
# All checks registry
# ---------------------------------------------------------------------------

ALL_CHECKS = [
    Check("Default audio source", "Audio", check_default_source),
    Check("Audio routing", "Audio", check_audio_routing),
    Check("Microphone capture test", "Audio", check_mic_capture),
    Check("Camera device presence", "Camera", check_camera_present),
    Check("Camera capture test", "Camera", check_camera_capture),
    Check("Cheese camera config", "Camera", check_cheese_config),
    Check("USB errors", "USB", check_usb_errors),
    Check("Privacy dashboard", "Privacy", check_privacy_dashboard),
    Check("Desktop portal", "Privacy", check_portal_service),
    Check("System updates", "Maintenance", check_system_updates),
    Check("Temporary files", "Maintenance", check_temp_size),
]


def get_all_checks() -> list[Check]:
    """Return the full list of diagnostic checks, including dynamically detected apps."""
    return ALL_CHECKS + _get_installed_app_checks()


# ---------------------------------------------------------------------------
# GTK UI
# ---------------------------------------------------------------------------

class ResultRow(Gtk.ListBoxRow):
    def __init__(self, result: CheckResult):
        super().__init__()
        self.result = result
        self.fix_button: Gtk.Button | None = None

        grid = Gtk.Grid(margin=10, row_spacing=4, column_spacing=12)
        self.add(grid)

        status_color = {
            "ok": "#22c55e",
            "warn": "#eab308",
            "error": "#ef4444",
            "info": "#3b82f6",
        }.get(result.status, "#888888")

        icon_label = Gtk.Label(label=result.icon())
        icon_label.set_markup(f'<span size="x-large" foreground="{status_color}">{result.icon()}</span>')
        grid.attach(icon_label, 0, 0, 1, 2)

        name_label = Gtk.Label(label=result.name)
        name_label.set_xalign(0)
        name_label.set_markup(f"<b>{GLib.markup_escape_text(result.name, -1)}</b>")
        grid.attach(name_label, 1, 0, 1, 1)

        summary_label = Gtk.Label(label=result.summary)
        summary_label.set_xalign(0)
        summary_label.set_line_wrap(True)
        grid.attach(summary_label, 1, 1, 1, 1)

        if result.fix_callback:
            self.fix_button = Gtk.Button(label=result.fix_label or "Fix")
            self.fix_button.connect("clicked", self._on_fix)
            grid.attach(self.fix_button, 2, 0, 1, 2)
        elif result.fix_command:
            self.fix_button = Gtk.Button(label=result.fix_label or "Fix")
            self.fix_button.connect("clicked", self._on_fix)
            grid.attach(self.fix_button, 2, 0, 1, 2)

        if result.detail:
            detail_button = Gtk.Button(label="Details")
            detail_button.connect("clicked", self._on_details)
            grid.attach(detail_button, 3, 0, 1, 2)

    def _on_fix(self, _button: Gtk.Button) -> None:
        if self.result.fix_callback:
            try:
                sig = inspect.signature(self.result.fix_callback)
                if any(p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY) for p in sig.parameters.values()):
                    self.result.fix_callback(parent=self.get_toplevel())
                else:
                    self.result.fix_callback()
            except TypeError:
                self.result.fix_callback()
            return
        if not self.result.fix_command:
            return
        cmd = self.result.fix_command
        if cmd[0] in ("systemctl", "wpctl"):
            # User-level commands don't need root.
            subprocess.Popen(cmd)
        else:
            subprocess.Popen(["pkexec"] + cmd)

    def _on_details(self, _button: Gtk.Button) -> None:
        dialog = Gtk.MessageDialog(
            parent=self.get_toplevel(),
            flags=Gtk.DialogFlags.MODAL,
            type=Gtk.MessageType.INFO,
            buttons=Gtk.ButtonsType.CLOSE,
            text=self.result.summary,
        )
        dialog.format_secondary_text(self.result.detail[:2000])
        dialog.run()
        dialog.destroy()


class PrivacyScanner(Gtk.Box):
    """Live scanner for apps/processes using the camera or microphone."""

    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.set_margin_top(6)
        self.set_margin_bottom(6)
        self.set_margin_start(6)
        self.set_margin_end(6)

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.pack_start(header, False, False, 0)

        title = Gtk.Label(label="Click Refresh to load.")
        title.set_xalign(0)
        self.pack_start(title, False, False, 0)

        kill_all_btn = Gtk.Button(label="Kill all")
        kill_all_btn.set_tooltip_text("Stop every active camera/mic process")
        kill_all_btn.connect("clicked", self._on_kill_all)
        header.pack_end(kill_all_btn, False, False, 0)

        refresh_btn = Gtk.Button(label="Refresh")
        refresh_btn.connect("clicked", self._on_refresh)
        header.pack_end(refresh_btn, False, False, 0)

        self.status_label = Gtk.Label(label="Click Refresh to scan.")
        self.status_label.set_xalign(0)
        self.pack_start(self.status_label, False, False, 0)

        self.listbox = Gtk.ListBox()
        self.listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroll.set_min_content_height(120)
        scroll.add(self.listbox)
        self.pack_start(scroll, True, True, 0)

    def _on_refresh(self, _button: Gtk.Button | None = None) -> None:
        for child in self.listbox.get_children():
            self.listbox.remove(child)

        camera_users = get_camera_users()
        mic_users = get_mic_users()
        all_users = camera_users + mic_users

        if not all_users:
            row = Gtk.ListBoxRow()
            row.add(Gtk.Label(label="No active camera or microphone users found.", margin=8))
            self.listbox.add(row)
            self.status_label.set_text("No active usage.")
        else:
            for user in all_users:
                self.listbox.add(self._make_user_row(user))
            self.status_label.set_text(f"{len(all_users)} active user(s).")

        self.listbox.show_all()
        self._last_pids = [int(u.get("pid", "0")) for u in all_users if str(u.get("pid", "")).isdigit()]

    def _on_kill_all(self, _button: Gtk.Button | None = None) -> None:
        if not getattr(self, "_last_pids", []):
            self.status_label.set_text("No active users to stop.")
            return
        count = 0
        for pid in self._last_pids:
            ok, _err = kill_process(pid, signal.SIGTERM)
            if ok:
                count += 1
        self.status_label.set_text(f"Sent STOP to {count} process(es).")
        self._on_refresh()

    def _make_user_row(self, user: dict) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow()
        grid = Gtk.Grid(margin=6, column_spacing=12, row_spacing=4)
        row.add(grid)

        icon = "📷" if user.get("type") == "camera" else "🎙"
        icon_lbl = Gtk.Label(label=icon)
        grid.attach(icon_lbl, 0, 0, 1, 2)

        name = user.get("name", "unknown")
        pid = user.get("pid", "?")
        detail = f"PID {pid}"
        if user.get("device"):
            detail += f"  •  {user['device']}"

        name_lbl = Gtk.Label(label=f"<b>{GLib.markup_escape_text(str(name), -1)}</b>")
        name_lbl.set_use_markup(True)
        name_lbl.set_xalign(0)
        grid.attach(name_lbl, 1, 0, 1, 1)

        detail_lbl = Gtk.Label(label=GLib.markup_escape_text(detail, -1))
        detail_lbl.set_xalign(0)
        grid.attach(detail_lbl, 1, 1, 1, 1)

        open_btn = Gtk.Button(label="Open")
        open_btn.set_tooltip_text(f"Open a terminal showing PID {pid}")
        open_btn.connect("clicked", lambda _b, name=name, pid=pid: self._open_pid(name, pid))
        grid.attach(open_btn, 2, 0, 1, 2)

        stop_btn = Gtk.Button(label="Stop")
        stop_btn.connect("clicked", lambda _b, pid=pid: self._stop_pid(pid))
        grid.attach(stop_btn, 3, 0, 1, 2)

        kill_btn = Gtk.Button(label="Kill")
        kill_btn.connect("clicked", lambda _b, pid=pid: self._kill_pid(pid))
        grid.attach(kill_btn, 4, 0, 1, 2)

        return row

    def _open_pid(self, name: str, pid: str) -> None:
        term = shutil.which("gnome-terminal") or shutil.which("konsole") or shutil.which("alacritty") or shutil.which("xterm")
        cmd = f"""echo '--- PID {pid} ({name}) active camera/mic ---'; ps -f -p {pid}; echo; echo '--- open files ---'; lsof -p {pid} 2>/dev/null || echo 'lsof not available'; echo; echo '--- recent journal ---'; journalctl --user -b -n 100 _PID={pid} 2>/dev/null || true; read -n1 -rsp 'Press any key to close...'"""
        if term:
            subprocess.Popen(
                [term, "--"] + ["bash", "-c", cmd],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

    def _stop_pid(self, pid: str) -> None:
        self._signal_pid(pid, signal.SIGTERM)

    def _kill_pid(self, pid: str) -> None:
        self._signal_pid(pid, signal.SIGKILL)

    def _signal_pid(self, pid: str, sig: int) -> None:
        try:
            pid_int = int(pid)
        except ValueError:
            return
        ok, err = kill_process(pid_int, sig)
        if ok:
            self._on_refresh()
        else:
            self.status_label.set_text(f"Could not signal PID {pid}: {err}")


class ProcessManager(Gtk.Box):
    """Simple process and user-service manager with a security-risk scanner."""

    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.set_margin_top(6)
        self.set_margin_bottom(6)
        self.set_margin_start(6)
        self.set_margin_end(6)

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.pack_start(header, False, False, 0)

        title = Gtk.Label(label="<b>Running processes &amp; background services</b>")
        title.set_use_markup(True)
        title.set_xalign(0)
        header.pack_start(title, True, True, 0)

        scan_btn = Gtk.Button(label="Scan risks")
        scan_btn.set_tooltip_text("Find redundant background processes and security risks")
        scan_btn.connect("clicked", self._on_scan_risks)
        header.pack_end(scan_btn, False, False, 0)

        refresh_btn = Gtk.Button(label="Refresh")
        refresh_btn.connect("clicked", self._on_refresh)
        header.pack_end(refresh_btn, False, False, 0)

        self.status_label = Gtk.Label(label="Click Refresh to load processes.")
        self.status_label.set_xalign(0)
        self.pack_start(self.status_label, False, False, 0)

        self.notebook = Gtk.Notebook()
        self.pack_start(self.notebook, True, True, 0)

        self.proc_list = Gtk.ListBox()
        self.proc_list.set_selection_mode(Gtk.SelectionMode.NONE)
        proc_scroll = Gtk.ScrolledWindow()
        proc_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        proc_scroll.add(self.proc_list)
        self.notebook.append_page(proc_scroll, Gtk.Label(label="Processes"))

        self.svc_list = Gtk.ListBox()
        self.svc_list.set_selection_mode(Gtk.SelectionMode.NONE)
        svc_scroll = Gtk.ScrolledWindow()
        svc_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        svc_scroll.add(self.svc_list)
        self.notebook.append_page(svc_scroll, Gtk.Label(label="Services"))

        self.risk_list = Gtk.ListBox()
        self.risk_list.set_selection_mode(Gtk.SelectionMode.NONE)
        risk_scroll = Gtk.ScrolledWindow()
        risk_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        risk_scroll.add(self.risk_list)
        self.notebook.append_page(risk_scroll, Gtk.Label(label="Risk scan"))

        self._last_procs: list[dict] = []

    def _on_refresh(self, _button: Gtk.Button | None = None) -> None:
        for child in self.proc_list.get_children():
            self.proc_list.remove(child)
        for child in self.svc_list.get_children():
            self.svc_list.remove(child)

        self._last_procs = get_running_processes(limit=100)
        for proc in self._last_procs:
            self.proc_list.add(self._make_process_row(proc))
        for svc in get_running_user_services():
            self.svc_list.add(self._make_service_row(svc))

        self.proc_list.show_all()
        self.svc_list.show_all()
        self.status_label.set_text(f"Loaded {len(self._last_procs)} processes.")

    def _on_scan_risks(self, _button: Gtk.Button | None = None) -> None:
        for child in self.risk_list.get_children():
            self.risk_list.remove(child)

        if not self._last_procs:
            self._last_procs = get_running_processes(limit=100)

        findings = _scan_security_risks(self._last_procs)
        if not findings:
            row = Gtk.ListBoxRow()
            row.add(Gtk.Label(label="No obvious redundant or risky processes found.", margin=8))
            self.risk_list.add(row)
        else:
            for finding in findings:
                self.risk_list.add(self._make_risk_row(finding))
        self.risk_list.show_all()
        self.notebook.set_current_page(2)
        self.status_label.set_text(f"Risk scan: {len(findings)} finding(s).")

    def _make_process_row(self, proc: dict) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow()
        grid = Gtk.Grid(margin=6, column_spacing=12, row_spacing=4)
        row.add(grid)

        name_lbl = Gtk.Label(label=f"<b>{GLib.markup_escape_text(proc['name'], -1)}</b>  (PID {proc['pid']})")
        name_lbl.set_use_markup(True)
        name_lbl.set_xalign(0)
        grid.attach(name_lbl, 0, 0, 1, 1)

        detail_lbl = Gtk.Label(label=f"CPU {proc['cpu']}%  •  MEM {proc['mem']}%")
        detail_lbl.set_xalign(0)
        grid.attach(detail_lbl, 0, 1, 1, 1)

        open_btn = Gtk.Button(label="Open")
        open_btn.set_tooltip_text(f"Open a terminal showing PID {proc['pid']} ({proc['name']})")
        open_btn.connect("clicked", lambda _b, p=proc: self._open_process(p))
        grid.attach(open_btn, 1, 0, 1, 2)

        stop_btn = Gtk.Button(label="Stop")
        stop_btn.connect("clicked", lambda _b, pid=proc["pid"]: self._signal_pid(pid, signal.SIGTERM))
        grid.attach(stop_btn, 2, 0, 1, 2)

        kill_btn = Gtk.Button(label="Kill")
        kill_btn.connect("clicked", lambda _b, pid=proc["pid"]: self._signal_pid(pid, signal.SIGKILL))
        grid.attach(kill_btn, 3, 0, 1, 2)

        return row

    def _make_service_row(self, svc: dict) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow()
        grid = Gtk.Grid(margin=6, column_spacing=12, row_spacing=4)
        row.add(grid)

        name_lbl = Gtk.Label(label=f"<b>{GLib.markup_escape_text(svc['name'], -1)}</b>")
        name_lbl.set_use_markup(True)
        name_lbl.set_xalign(0)
        grid.attach(name_lbl, 0, 0, 1, 1)

        detail_lbl = Gtk.Label(label=f"{svc['status']}  •  {GLib.markup_escape_text(svc['description'], -1)}")
        detail_lbl.set_xalign(0)
        grid.attach(detail_lbl, 0, 1, 1, 1)

        stop_btn = Gtk.Button(label="Stop service")
        stop_btn.connect("clicked", lambda _b, name=svc["name"]: self._stop_service(name))
        grid.attach(stop_btn, 1, 0, 1, 2)

        return row

    def _make_risk_row(self, finding: dict) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow()
        grid = Gtk.Grid(margin=6, column_spacing=12, row_spacing=4)
        row.add(grid)

        color = {
            "low": "#3b82f6",
            "medium": "#eab308",
            "high": "#ef4444",
        }.get(finding["level"], "#888888")
        icon_lbl = Gtk.Label(label="⚠")
        icon_lbl.set_markup(f'<span foreground="{color}" size="large">⚠</span>')
        grid.attach(icon_lbl, 0, 0, 1, 2)

        title_lbl = Gtk.Label(label=f"<b>{finding['title']}</b>")
        title_lbl.set_use_markup(True)
        title_lbl.set_xalign(0)
        grid.attach(title_lbl, 1, 0, 1, 1)

        detail_lbl = Gtk.Label(label=GLib.markup_escape_text(finding["detail"], -1))
        detail_lbl.set_xalign(0)
        grid.attach(detail_lbl, 1, 1, 1, 1)

        pids = finding.get("pids", [])
        if pids:
            stop_btn = Gtk.Button(label="Stop all")
            stop_btn.connect("clicked", lambda _b, ps=pids: self._signal_pids(ps, signal.SIGTERM))
            grid.attach(stop_btn, 2, 0, 1, 2)

        return row

    def _signal_pid(self, pid: int, sig: int) -> None:
        kill_process(pid, sig)
        self._on_refresh()

    def _signal_pids(self, pids: list[int], sig: int) -> None:
        for pid in pids:
            kill_process(pid, sig)
        self._on_refresh()

    def _stop_service(self, name: str) -> None:
        subprocess.Popen(["systemctl", "--user", "stop", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self._on_refresh()

    def _open_process(self, proc: dict) -> None:
        """Open a terminal running a diagnostic commands for a process."""
        term = shutil.which("gnome-terminal") or shutil.which("konsole") or shutil.which("alacritty") or shutil.which("xterm")
        pid = proc["pid"]
        name = proc["name"]
        cmd = f"""echo '--- PID {pid} ({name}) ---'; ps -f -p {pid}; echo; echo '--- open files ---'; lsof -p {pid} 2>/dev/null || echo 'lsof not available'; echo; echo '--- environment ---'; cat /proc/{pid}/environ 2>/dev/null | tr '\\0' '\\n' | head -40; echo; echo '--- recent journal ---'; journalctl --user -b -n 100 _PID={pid} 2>/dev/null || true; read -n1 -rsp 'Press any key to close...'"""
        if term:
            subprocess.Popen(
                [term, "--"] + ["bash", "-c", cmd],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        else:
            self.status_label.set_text("No supported terminal emulator found.")


# Known sensitive/suspicious keywords used to detect potential redundant background processes.
_RISK_PATTERNS: list[dict] = [
    {"name": "cloud sync agents", "keywords": ["nextcloud", "syncthing", "onedrive", "dropbox", "mega", "pcloud", "kdeconnect"], "level": "low"},
    {"name": "telemetry/agent services", "keywords": ["telemetry", "crashpad", "analytics", "reporting"], "level": "medium"},
    {"name": "remote access tools", "keywords": ["x11vnc", "tigervnc", "remmina", "anydesk", "teamviewer", "rustdesk", "nomachine"], "level": "high"},
    {"name": "cryptominers / heavy CPU", "pattern": re.compile(r"^.{0,30}(miner|xmr|stak|nanominer|nbminer|trex|gminer|lolminer).*$", re.I), "level": "high"},
    {"name": "redundant browsers (multipleChromes/Brave/Firefox)", "keywords": ["brave", "firefox", "chrome", "chromium"], "level": "low"},
]


def _scan_security_risks(processes: list[dict]) -> list[dict]:
    findings: list[dict] = []

    # Group by command basename to find duplicate/related processes.
    by_name: dict[str, list[dict]] = {}
    seen_keywords: dict[str, list[tuple[str, int]]] = {}

    for proc in processes:
        pn = proc["name"]
        by_name.setdefault(pn, []).append(proc)
        for risk in _RISK_PATTERNS:
            if "keywords" in risk:
                for kw in risk["keywords"]:
                    if kw.lower() in proc["name"].lower() or kw.lower() in proc["cmd"].lower():
                        seen_keywords.setdefault(risk["name"], []).append((proc["name"], proc["pid"]))
            elif "pattern" in risk:
                if risk["pattern"].search(proc["cmd"]):
                    seen_keywords.setdefault(risk["name"], []).append((proc["name"], proc["pid"]))

    # Find duplicates.
    for name, procs in by_name.items():
        if len(procs) > 1 and len(procs) <= 30:
            findings.append(
                {
                    "title": f"Multiple '{name}' processes",
                    "detail": f"Found {len(procs)} running instances of {name}. PIDs: {', '.join(str(p['pid']) for p in procs[:10])}",
                    "level": "low",
                    "pids": [p["pid"] for p in procs],
                }
            )

    for risk_name, matches in seen_keywords.items():
        risk_def = next((r for r in _RISK_PATTERNS if r["name"] == risk_name), {"level": "medium"})
        level = risk_def.get("level", "medium")
        title = risk_name
        if "cryptominer" in risk_name.lower():
            title = "Possible cryptominer / mining tooling"
        unique_pids = list({pid for _, pid in matches})
        findings.append(
            {
                "title": title,
                "detail": f"{len(matches)} process(es) matching: {', '.join(f'{n} ({pid})' for n, pid in matches[:8])}",
                "level": level,
                "pids": unique_pids,
            }
        )

    # Processes running from suspicious locations.
    suspicious_paths = ["/tmp", "/var/tmp", HOME / ".local" / "tmp"]
    suspicious_pids: list[int] = []
    for proc in processes:
        exe = Path(f"/proc/{proc['pid']}/exe")
        try:
            target = exe.readlink()
        except Exception:
            target = ""
        for sp in suspicious_paths:
            if sp in str(target):
                suspicious_pids.append(proc["pid"])
    if suspicious_pids:
        findings.append(
            {
                "title": "Processes running from temporary directory",
                "detail": f"PIDs: {', '.join(str(p) for p in suspicious_pids[:10])}",
                "level": "high",
                "pids": suspicious_pids,
            }
        )

    return findings


class PackageVersionsDialog(Gtk.Dialog):
    """Show all available versions of a single package and allow downgrade/install."""

    def __init__(self, parent: Gtk.Window, pkg_name: str, current_ver: str) -> None:
        super().__init__(
            title=f"Versions of {pkg_name}",
            parent=parent,
            flags=Gtk.DialogFlags.MODAL,
            buttons=("Close", Gtk.ResponseType.CLOSE),
        )
        self.set_default_size(550, 350)
        self.pkg_name = pkg_name
        self.current_ver = current_ver

        content = self.get_content_area()
        content.set_spacing(6)
        content.set_margin_top(12)
        content.set_margin_bottom(12)
        content.set_margin_start(12)
        content.set_margin_end(12)

        info = Gtk.Label(label=f"<b>{pkg_name}</b>\nCurrent: {current_ver}")
        info.set_use_markup(True)
        info.set_xalign(0)
        content.pack_start(info, False, False, 0)

        self.listbox = Gtk.ListBox()
        self.listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroll.add(self.listbox)
        content.pack_start(scroll, True, True, 0)

        self._load_versions()
        self.show_all()

    def _load_versions(self) -> None:
        for child in self.listbox.get_children():
            self.listbox.remove(child)

        res = run(["dnf", "repoquery", "--show-duplicates", "--quiet", self.pkg_name], timeout=60)
        versions = [v.strip() for v in res.stdout.splitlines() if v.strip()]
        if not versions:
            row = Gtk.ListBoxRow()
            row.add(Gtk.Label(label="No versions found.", margin=8))
            self.listbox.add(row)
            return

        for version in versions:
            row = Gtk.ListBoxRow()
            hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            hbox.set_margin_top(6)
            hbox.set_margin_bottom(6)
            hbox.set_margin_start(6)
            hbox.set_margin_end(6)
            row.add(hbox)

            label_text = f"{version}  (current)" if version == self.current_ver else version
            lbl = Gtk.Label(label=label_text)
            lbl.set_xalign(0)
            hbox.pack_start(lbl, True, True, 0)

            if version != self.current_ver:
                btn = Gtk.Button(label="Install")
                btn.connect("clicked", lambda _b, v=version: self._install(v))
                hbox.pack_end(btn, False, False, 0)

            self.listbox.add(row)
        self.listbox.show_all()

    def _install(self, version: str) -> None:
        subprocess.Popen(
            ["pkexec", "dnf", "install", "-y", version],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.destroy()


class UpdateManagerDialog(Gtk.Dialog):
    """Grouped update selector with update-all / selected / downgrade support."""

    COL_SELECTED = 0
    COL_PACKAGE = 1
    COL_CURRENT = 2
    COL_AVAILABLE = 3
    COL_REPO = 4
    COL_FULL = 5

    def __init__(self, parent: Gtk.Window) -> None:
        super().__init__(
            title="Update Manager",
            parent=parent,
            flags=Gtk.DialogFlags.MODAL,
            buttons=("Close", Gtk.ResponseType.CLOSE),
        )
        self.set_default_size(900, 600)
        self._create_ui()
        self._refresh()

    def _create_ui(self) -> None:
        content = self.get_content_area()
        content.set_spacing(6)
        content.set_margin_top(12)
        content.set_margin_bottom(12)
        content.set_margin_start(12)
        content.set_margin_end(12)

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        content.pack_start(header, False, False, 0)

        refresh_btn = Gtk.Button(label="Refresh")
        refresh_btn.connect("clicked", lambda _b: self._refresh())
        header.pack_start(refresh_btn, False, False, 0)

        sel_all_btn = Gtk.Button(label="Select all")
        sel_all_btn.connect("clicked", lambda _b: self._select_all(True))
        header.pack_start(sel_all_btn, False, False, 0)

        desel_all_btn = Gtk.Button(label="Deselect all")
        desel_all_btn.connect("clicked", lambda _b: self._select_all(False))
        header.pack_start(desel_all_btn, False, False, 0)

        update_sel_btn = Gtk.Button(label="Update selected")
        update_sel_btn.connect("clicked", self._on_update_selected)
        header.pack_start(update_sel_btn, False, False, 0)

        update_all_btn = Gtk.Button(label="Update all")
        update_all_btn.connect("clicked", self._on_update_all)
        header.pack_start(update_all_btn, False, False, 0)

        versions_btn = Gtk.Button(label="Versions / downgrade")
        versions_btn.connect("clicked", self._on_versions)
        header.pack_start(versions_btn, False, False, 0)

        self.store = Gtk.TreeStore(bool, str, str, str, str, str)
        self.tree = Gtk.TreeView(model=self.store)
        self.tree.get_selection().set_mode(Gtk.SelectionMode.SINGLE)

        toggle_renderer = Gtk.CellRendererToggle()
        toggle_renderer.connect("toggled", self._on_toggled)
        toggle_col = Gtk.TreeViewColumn("Select", toggle_renderer, active=self.COL_SELECTED)
        self.tree.append_column(toggle_col)

        for i, title in enumerate(["Package / Group", "Current version", "Available version", "Repository"]):
            renderer = Gtk.CellRendererText()
            col = Gtk.TreeViewColumn(title, renderer, text=i + 1)
            col.set_resizable(True)
            self.tree.append_column(col)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroll.add(self.tree)
        content.pack_start(scroll, True, True, 0)

        self.status_label = Gtk.Label(label="Loading...")
        self.status_label.set_xalign(0)
        content.pack_start(self.status_label, False, False, 0)

        self.show_all()

    def _on_toggled(self, _renderer: Gtk.CellRendererToggle, path: str) -> None:
        iter_ = self.store.get_iter_from_string(path)
        new_value = not self.store[iter_][self.COL_SELECTED]
        self.store[iter_][self.COL_SELECTED] = new_value

        if self.store.iter_has_child(iter_):
            child = self.store.iter_children(iter_)
            while child:
                self.store[child][self.COL_SELECTED] = new_value
                child = self.store.iter_next(child)
        else:
            parent = self.store.iter_parent(iter_)
            if parent:
                child = self.store.iter_children(parent)
                all_selected = True
                while child:
                    if not self.store[child][self.COL_SELECTED]:
                        all_selected = False
                        break
                    child = self.store.iter_next(child)
                self.store[parent][self.COL_SELECTED] = all_selected

    def _select_all(self, value: bool) -> None:
        def set_value(model: Gtk.TreeStore, _path: Gtk.TreePath, iter_: Gtk.TreeIter) -> None:
            model[iter_][self.COL_SELECTED] = value
        self.store.foreach(set_value)

    def _refresh(self) -> None:
        thread = threading.Thread(target=self._refresh_thread, daemon=True)
        thread.start()

    def _refresh_thread(self) -> None:
        GLib.idle_add(self.status_label.set_text, "Checking for updates...")
        updates = self._load_updates()
        GLib.idle_add(self._populate, updates)

    def _load_updates(self) -> list[dict]:
        res = run(["dnf", "check-update", "-q"], timeout=120)
        if res.returncode not in (0, 100):
            return []
        lines = [l.strip() for l in res.stdout.splitlines() if l.strip() and not l.startswith("=")]
        updates = []
        for line in lines:
            parts = line.split(None, 2)
            if len(parts) < 3:
                continue
            pkg_arch, available, repo = parts
            name = pkg_arch.rsplit(".", 1)[0]
            current_res = run(["rpm", "-q", pkg_arch], timeout=10)
            current = current_res.stdout.strip()
            if not current or current.startswith("package "):
                current = "?"
            updates.append(
                {
                    "pkg": pkg_arch,
                    "name": name,
                    "current": current,
                    "available": available,
                    "repo": repo,
                }
            )
        return updates

    def _populate(self, updates: list[dict]) -> None:
        self.store.clear()
        by_repo: dict[str, list[dict]] = {}
        for u in updates:
            by_repo.setdefault(u["repo"], []).append(u)

        for repo in sorted(by_repo):
            parent = self.store.append(None, [False, repo, "", "", repo, ""])
            for u in sorted(by_repo[repo], key=lambda x: x["name"]):
                self.store.append(
                    parent,
                    [False, u["name"], u["current"], u["available"], u["repo"], u["pkg"]],
                )
        self.status_label.set_text(f"{len(updates)} update(s) available.")

    def _selected_packages(self) -> list[str]:
        selected: list[str] = []

        def collect(model: Gtk.TreeStore, _path: Gtk.TreePath, iter_: Gtk.TreeIter) -> None:
            if not model.iter_has_child(iter_) and model[iter_][self.COL_SELECTED]:
                full = model[iter_][self.COL_FULL]
                if full:
                    selected.append(full)

        self.store.foreach(collect)
        return selected

    def _on_update_selected(self, _button: Gtk.Button) -> None:
        pkgs = self._selected_packages()
        if not pkgs:
            self.status_label.set_text("No packages selected.")
            return
        subprocess.Popen(
            ["pkexec", "dnf", "install", "-y"] + pkgs,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.status_label.set_text(f"Updating {len(pkgs)} package(s)...")

    def _on_update_all(self, _button: Gtk.Button) -> None:
        subprocess.Popen(
            ["pkexec", "dnf", "upgrade", "-y"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.status_label.set_text("Updating all packages...")

    def _on_versions(self, _button: Gtk.Button) -> None:
        model, iter_ = self.tree.get_selection().get_selected()
        if iter_ is None:
            self.status_label.set_text("Select a package first.")
            return
        if model.iter_has_child(iter_):
            self.status_label.set_text("Select a package, not a group.")
            return
        pkg = model[iter_][self.COL_PACKAGE]
        current = model[iter_][self.COL_CURRENT]
        PackageVersionsDialog(self, pkg, current)


class MaintenanceDialog(Gtk.Dialog):
    """Quick-access maintenance actions."""

    def __init__(self, parent: Gtk.Window) -> None:
        super().__init__(
            title="Maintenance",
            parent=parent,
            flags=Gtk.DialogFlags.MODAL,
            buttons=("Close", Gtk.ResponseType.CLOSE),
        )
        self.set_default_size(300, 250)
        self.parent_window = parent
        content = self.get_content_area()
        content.set_spacing(6)
        content.set_margin_top(12)
        content.set_margin_bottom(12)
        content.set_margin_start(12)
        content.set_margin_end(12)

        actions = [
            ("Update manager", self._on_update),
            ("Reset USB camera", self._on_reset_camera),
            ("Fix audio routing", self._on_fix_audio),
            ("Stop privacy dashboard", self._on_stop_privacy),
        ]
        for label, handler in actions:
            btn = Gtk.Button(label=label)
            btn.connect("clicked", handler)
            content.pack_start(btn, False, False, 0)

        self.show_all()

    def _run(self, cmd: list[str]) -> None:
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _on_update(self, _button: Gtk.Button) -> None:
        UpdateManagerDialog(self.parent_window)

    def _on_reset_camera(self, _button: Gtk.Button) -> None:
        script = HOME / ".local" / "bin" / "local-butler-fix-camera"
        if shutil.which("pkexec"):
            self._run(["pkexec", str(script)])
        else:
            self._run([str(script)])

    def _on_fix_audio(self, _button: Gtk.Button) -> None:
        script = HOME / ".local" / "bin" / "local-butler-fix-audio-routing"
        self._run([str(script)])

    def _on_stop_privacy(self, _button: Gtk.Button) -> None:
        self._run(["systemctl", "--user", "stop", "privacy-dashboard"])


def _apply_fix(result: CheckResult) -> None:
    """Apply the fix attached to a check result."""
    if result.fix_callback:
        try:
            sig = inspect.signature(result.fix_callback)
            if any(p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY) for p in sig.parameters.values()):
                result.fix_callback(parent=None)
            else:
                result.fix_callback()
        except TypeError:
            result.fix_callback()
        return
    if not result.fix_command:
        return
    cmd = result.fix_command
    if cmd[0] in ("systemctl", "wpctl"):
        subprocess.run(cmd, check=False)
    elif shutil.which("pkexec"):
        subprocess.run(["pkexec"] + cmd, check=False)
    else:
        subprocess.run(cmd, check=False)


# ---------------------------------------------------------------------------
# Antivirus helpers (ClamAV)
# ---------------------------------------------------------------------------

def _get_clamav_version() -> str:
    res = run(["clamscan", "--version"], timeout=5)
    return res.stdout.strip() if res.returncode == 0 else ""


def _get_freshclam_status() -> tuple[bool, str]:
    dat = Path("/var/lib/clamav/freshclam.dat")
    if not dat.exists():
        return False, "freshclam.dat not found"
    try:
        text = dat.read_text(errors="ignore")
        for line in text.splitlines():
            if line.startswith("Date:"):
                return True, line.split(":", 1)[1].strip()
    except Exception as exc:
        return False, str(exc)
    return False, "unknown"


def _run_clamscan(paths: list[str] | None = None, recursive: bool = False, extra_flags: list[str] | None = None) -> subprocess.Popen:
    """Start a clamscan process and return the Popen object. Uses clamdscan if daemon is running; otherwise clamscan."""
    paths = paths or [str(HOME)]
    flags = extra_flags or []
    if shutil.which("clamdscan"):
        cmd = ["clamdscan", "--multiscan"] + (["--recursive"] if recursive else []) + flags + paths
    else:
        cmd = ["clamscan", "-i"] + (["-r"] if recursive else []) + flags + paths
    return subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        universal_newlines=True,
    )


def _run_freshclam() -> None:
    """Update ClamAV definitions."""
    subprocess.Popen(
        ["pkexec", "freshclam"] if shutil.which("pkexec") else ["freshclam"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


class AntivirusManager(Gtk.Box):
    """ClamAV scanner UI with realtime log output and quarantine."""

    QUARANTINE_DIR = Path("/tmp/local_butler_quarantine")

    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.set_margin_top(6)
        self.set_margin_bottom(6)
        self.set_margin_start(6)
        self.set_margin_end(6)

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.pack_start(header, False, False, 0)

        title = Gtk.Label(label="<b>Antivirus (ClamAV)</b>")
        title.set_use_markup(True)
        title.set_xalign(0)
        header.pack_start(title, True, True, 0)

        self.status_label = Gtk.Label(label="Loading...")
        self.status_label.set_xalign(0)
        self.pack_start(self.status_label, False, False, 0)

        quick_btn = Gtk.Button(label="Quick scan home")
        quick_btn.connect("clicked", lambda _b: self._start_scan([str(HOME)], False))
        header.pack_end(quick_btn, False, False, 0)

        full_btn = Gtk.Button(label="Full scan home")
        full_btn.connect("clicked", lambda _b: self._start_scan([str(HOME)], True))
        header.pack_end(full_btn, False, False, 0)

        update_btn = Gtk.Button(label="Update defs")
        update_btn.connect("clicked", lambda _b: self._update_defs())
        header.pack_end(update_btn, False, False, 0)

        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.pack_start(toolbar, False, False, 0)

        path_entry = Gtk.Entry()
        path_entry.set_placeholder_text("Enter path to scan")
        path_entry.set_text(str(HOME))
        self.path_entry = path_entry
        toolbar.pack_start(path_entry, True, True, 0)

        custom_btn = Gtk.Button(label="Scan path")
        custom_btn.connect("clicked", lambda _b: self._start_scan([self.path_entry.get_text()], True))
        toolbar.pack_start(custom_btn, False, False, 0)

        self.Quarantine_DIR = Path("/tmp/local_butler_quarantine")
        self.Quarantine_DIR.mkdir(parents=True, exist_ok=True)

        self.textview = Gtk.TextView()
        self.textview.set_editable(False)
        self.textview.set_monospace(True)
        self.textbuffer = self.textview.get_buffer()
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroll.add(self.textview)
        self.pack_start(scroll, True, True, 0)

        self._scan_thread: threading.Thread | None = None
        self._scan_stop_event = threading.Event()
        self._check_status()

    def _check_status(self) -> None:
        version = _get_clamav_version()
        ok, date = _get_freshclam_status()
        self.status_label.set_text(f"{version} | Definitions last updated: {date if ok else 'unknown'}")

    def _update_defs(self) -> None:
        _run_freshclam()
        self._log("Started freshclam definition update. Check back in a moment.")
        GLib.timeout_add_seconds(10, self._check_status_recheck)

    def _check_status_recheck(self) -> bool:
        self._check_status()
        return False

    def _log(self, text: str) -> None:
        end_iter = self.textbuffer.get_end_iter()
        self.textbuffer.insert(end_iter, text + "\n")
        mark = self.textbuffer.create_mark("end", self.textbuffer.get_end_iter(), False)
        self.textview.scroll_to_mark(mark, 0, False, 0, 0)

    def _start_scan(self, paths: list[str], recursive: bool) -> None:
        if self._scan_thread and self._scan_thread.is_alive():
            self._log("A scan is already running. Stop it first.")
            return
        paths = [p for p in paths if p and Path(p).exists()]
        if not paths:
            self._log("No valid path to scan.")
            return
        self._log(f"Starting {'recursive ' if recursive else ''}scan: {paths}")
        self._scan_stop_event.clear()
        self._scan_thread = threading.Thread(target=self._scan_worker, args=(paths, recursive), daemon=True)
        self._scan_thread.start()

    def _scan_worker(self, paths: list[str], recursive: bool) -> None:
        proc = _run_clamscan(paths, recursive)
        self._current_proc = proc
        try:
            for line in iter(proc.stdout.readline, ""):
                if not line:
                    break
                GLib.idle_add(self._log, line.rstrip())
                if self._scan_stop_event.is_set():
                    proc.terminate()
                    break
            proc.wait(timeout=10)
        except Exception as exc:
            GLib.idle_add(self._log, f"Scan error: {exc}")
        finally:
            GLib.idle_add(self._log, "Scan finished.")


# ---------------------------------------------------------------------------
# App / Game health check
# ---------------------------------------------------------------------------

def _run_steam_game_check(app_name: str, app_id: int) -> CheckResult:
    """Check whether a Steam game is installed and validate key files."""
    steam_root = HOME / ".var" / "app" / "com.valvesoftware.Steam" / ".local" / "share" / "Steam" / "steamapps"
    if not steam_root.exists():
        return CheckResult(f"{app_name}", "info", "Steam library not found", category="App Health")

    acf_path = steam_root / f"appmanifest_{app_id}.acf"
    if not acf_path.exists():
        return CheckResult(f"{app_name}", "info", f"{app_name} is not installed", category="App Health")

    # Try to parse basic acf fields without external Steam API.
    try:
        content = acf_path.read_text(errors="ignore")
        install_dir_match = re.search(r'"installdir"\s+"([^"]+)"', content)
        if not install_dir_match:
            return CheckResult(f"{app_name}", "warn", "Could not parse Steam manifest", detail=content[:500], category="App Health")
        install_dir = steam_root / "common" / install_dir_match.group(1)
        detail = f"Install dir: {install_dir}\n"
        # Count and sample some files.
        file_count = sum(1 for _ in install_dir.rglob("*") if _.is_file())
        detail += f"Files: {file_count}\n"
        if file_count == 0:
            return CheckResult(f"{app_name}", "error", f"{app_name} appears corrupted (no files)", detail=detail, category="App Health")
        return CheckResult(f"{app_name}", "ok", f"{app_name} is installed ({file_count} files)", detail=detail, category="App Health")
    except Exception as exc:
        return CheckResult(f"{app_name}", "warn", f"Error checking {app_name}: {exc}", category="App Health")


def _check_flatpak_app_health(app_id: str, friendly: str) -> CheckResult:
    installed = _get_installed_flatpaks()
    if app_id not in installed:
        return CheckResult(friendly, "info", f"{friendly} is not installed", category="App Health")
    res = run(["flatpak", "info", app_id], timeout=10)
    if res.returncode != 0:
        return CheckResult(friendly, "warn", f"Could not read {friendly} Flatpak metadata", detail=res.stderr, category="App Health")
    return CheckResult(friendly, "ok", f"{friendly} Flatpak metadata readable", detail=res.stdout[:500], category="App Health")


def _check_native_app_health(name: str, binaries: list[str]) -> CheckResult:
    for binary in binaries:
        path = shutil.which(binary)
        if path:
            try:
                proc = Path(path)
                resolved = str(proc.resolve()) if proc.is_symlink() else path
                return CheckResult(name, "ok", f"{name} executable found at {resolved}", category="App Health")
            except Exception:
                return CheckResult(name, "ok", f"{name} executable found at {path}", category="App Health")
    return CheckResult(name, "info", f"{name} is not installed", category="App Health")


def _get_app_health_checks() -> list[Check]:
    checks: list[Check] = []
    # Dynamic registry apps.
    for app in _APP_REGISTRY:
        if _is_installed(app):
            if app["type"] == "flatpak":
                checks.append(Check(app["name"], "App Health", lambda a=app: _check_flatpak_app_health(a["app_id"], a["name"])))
            else:
                checks.append(Check(app["name"], "App Health", lambda a=app: _check_native_app_health(a["name"], a["binaries"])))
    # Steam games.
    checks.append(Check("Hearts of Iron IV", "App Health", lambda: _run_steam_game_check("Hearts of Iron IV", 394360)))
    return checks


# ---------------------------------------------------------------------------
# Terminal / log viewer
# ---------------------------------------------------------------------------

class LogTerminal(Gtk.Box):
    """Simple log viewer that can follow a command or journal for a process/app."""

    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.set_margin_top(6)
        self.set_margin_bottom(6)
        self.set_margin_start(6)
        self.set_margin_end(6)

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.pack_start(header, False, False, 0)

        title = Gtk.Label(label="<b>Log / terminal output</b>")
        title.set_use_markup(True)
        title.set_xalign(0)
        header.pack_start(title, True, True, 0)

        self.pid_entry = Gtk.Entry()
        self.pid_entry.set_placeholder_text("PID")
        self.pid_entry.set_width_chars(8)
        header.pack_end(self.pid_entry, False, False, 0)

        follow_pid_btn = Gtk.Button(label="Follow PID")
        follow_pid_btn.connect("clicked", lambda _b: self._follow_pid(self.pid_entry.get_text()))
        header.pack_end(follow_pid_btn, False, False, 0)

        self.app_entry = Gtk.Entry()
        self.app_entry.set_placeholder_text("App name / unit")
        self.app_entry.set_width_chars(20)
        header.pack_end(self.app_entry, False, False, 0)

        follow_app_btn = Gtk.Button(label="Follow app log")
        follow_app_btn.connect("clicked", lambda _b: self._follow_app(self.app_entry.get_text()))
        header.pack_end(follow_app_btn, False, False, 0)

        self.textview = Gtk.TextView()
        self.textview.set_editable(False)
        self.textview.set_monospace(True)
        self.textview.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.buffer = self.textview.get_buffer()
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroll.add(self.textview)
        self.pack_start(scroll, True, True, 0)

        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.pack_start(toolbar, False, False, 0)

        clear_btn = Gtk.Button(label="Clear")
        clear_btn.connect("clicked", lambda _b: self.buffer.set_text(""))
        toolbar.pack_start(clear_btn, False, False, 0)

        run_cmd_entry = Gtk.Entry()
        run_cmd_entry.set_placeholder_text("Run command...")
        run_cmd_entry.set_hexpand(True)
        run_cmd_entry.connect("activate", lambda _e: self._run_command(run_cmd_entry.get_text()))
        toolbar.pack_start(run_cmd_entry, True, True, 0)

        run_btn = Gtk.Button(label="Run")
        run_btn.connect("clicked", lambda _b: self._run_command(run_cmd_entry.get_text()))
        toolbar.pack_start(run_btn, False, False, 0)

        self._reader_thread: threading.Thread | None = None
        self._reader_stop = threading.Event()
        self._reader_proc: subprocess.Popen | None = None

    def _append(self, text: str) -> None:
        end_iter = self.buffer.get_end_iter()
        self.buffer.insert(end_iter, text)
        self.textview.scroll_to_iter(self.buffer.get_end_iter(), 0.0, False, 0, 0)

    def _stop_reader(self) -> None:
        self._reader_stop.set()
        if self._reader_proc:
            try:
                self._reader_proc.terminate()
            except Exception:
                pass

    def _follow_pid(self, pid_text: str) -> None:
        try:
            pid = int(pid_text.strip())
        except ValueError:
            self._append("Invalid PID.\n")
            return
        self._stop_reader()
        self._reader_stop.clear()
        self._append(f"--- Following dmesg-style log for PID {pid} ---\n")
        cmd = [
            "stdbuf", "-o0",
            "bash", "-c",
            f"journalctl --user -b -n 100 _PID={pid}; stdbuf -o0 journalctl --user -f _PID={pid} -n 0 2>/dev/null || tail -f /proc/{pid}/fd/1 2>/dev/null || tail -f /dev/null",
        ]
        self._reader_proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
        self._reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
        self._reader_thread.start()

    def _follow_app(self, app_text: str) -> None:
        name = app_text.strip()
        if not name:
            return
        self._stop_reader()
        self._reader_stop.clear()
        self._append(f"--- Following processes matching '{name}' ---\n")
        self._reader_proc = subprocess.Popen(
            ["stdbuf", "-o0", "bash", "-c", f"journalctl --user -b -f --grep='{name}' --no-pager -n 50 2>/dev/null || tail -f /dev/null"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        self._reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
        self._reader_thread.start()

    def _run_command(self, cmd_text: str) -> None:
        text = cmd_text.strip()
        if not text:
            return
        self._append(f"$ {text}\n")
        self._reader_proc = subprocess.Popen(
            text,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        self._reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
        self._reader_thread.start()

    def _reader_loop(self) -> None:
        if not self._reader_proc or not self._reader_proc.stdout:
            return
        for line in iter(self._reader_proc.stdout.readline, ""):
            if self._reader_stop.is_set():
                break
            if not line:
                break
            GLib.idle_add(self._append, line)
        try:
            self._reader_proc.wait(timeout=2)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Local AI chat panel
# ---------------------------------------------------------------------------

class LocalAIChat(Gtk.Box):
    """A local AI assistant panel backed by bigus_dickus (via local-brain)."""

    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.set_margin_top(6)
        self.set_margin_bottom(6)
        self.set_margin_start(6)
        self.set_margin_end(6)

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.pack_start(header, False, False, 0)

        title = Gtk.Label(label="<b>bigus_dickus — Local AI Agent</b>")
        title.set_use_markup(True)
        title.set_xalign(0)
        header.pack_start(title, True, True, 0)

        self.status_label = Gtk.Label(label="Detecting local agent...")
        self.status_label.set_xalign(0)
        self.pack_start(self.status_label, False, False, 0)

        controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.pack_start(controls, False, False, 0)
        self.backend_label = Gtk.Label(label="Backend: detecting...")
        self.backend_label.set_xalign(0)
        controls.pack_start(self.backend_label, False, False, 0)
        self.model_combo = Gtk.ComboBoxText()
        self.model_combo.set_tooltip_text("Select an AI model")
        controls.pack_end(self.model_combo, False, False, 0)

        self.textview = Gtk.TextView()
        self.textview.set_editable(False)
        self.textview.set_wrap_mode(Gtk.WrapMode.WORD)
        self.buffer = self.textview.get_buffer()
        self.buffer.set_text("Assistant: Hello! I am bigus_dickus, your local AI agent. Ask me to diagnose this machine, run a command, or help fix a problem.\n")
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroll.add(self.textview)
        self.pack_start(scroll, True, True, 0)

        input_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.pack_start(input_box, False, False, 0)

        self.entry = Gtk.Entry()
        self.entry.set_placeholder_text("Ask bigus_dickus a question or give an order...")
        self.entry.connect("activate", lambda _e: self._send())
        input_box.pack_start(self.entry, True, True, 0)

        send_btn = Gtk.Button(label="Send")
        send_btn.connect("clicked", lambda _b: self._send())
        input_box.pack_start(send_btn, False, False, 0)

        self._backends: dict[str, dict[str, Any]] = {}
        self._ollama_models: list[str] = []
        self._tf_models: list[str] = []
        self._session_id: str = uuid.uuid4().hex
        self._selected_model: str | None = None
        self._busy = False
        self._assistant_mark: object | None = None
        self._detect_backends()

    def _detect_backends(self) -> None:
        """Query local-brain for available chat backends and models."""
        try:
            req = urllib.request.Request(
                "http://127.0.0.1:9876/api/chat/health",
                method="GET",
            )
            with urllib.request.urlopen(req, timeout=3) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            self._backends = data.get("backends", {})
        except Exception:
            self._backends = {}
            # Fallback for older local-brain versions without the health endpoint.
            try:
                import socket

                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(1)
                sock.connect(("127.0.0.1", 9876))
                sock.close()
                self._backends = {
                    "trueforge": {"ok": False, "models": []},
                    "ollama": {"ok": False, "models": []},
                }
            except Exception:
                pass

        self._ollama_models = self._backends.get("ollama", {}).get("models", [])
        self._tf_models = self._backends.get("trueforge", {}).get("models", [])

        self.model_combo.remove_all()
        for m in self._ollama_models:
            self.model_combo.append_text(f"ollama/{m}")
        for m in self._tf_models:
            self.model_combo.append_text(f"trueforge/{m}")
        if self.model_combo.get_active() == -1 and self.model_combo.get_model().iter_n_children() > 0:
            self.model_combo.set_active(0)

        if self._backends.get("ollama", {}).get("ok"):
            self.backend_label.set_text(f"Backend: Ollama ({len(self._ollama_models)} models)")
        elif self._backends.get("trueforge", {}).get("ok"):
            self.backend_label.set_text("Backend: TrueForge / bigus_dickus")
        elif self._backends:
            self.backend_label.set_text("Backend: local-brain (no models reachable)")
        elif shutil.which("ollama"):
            self.backend_label.set_text("Backend: Ollama direct fallback")
        else:
            self.backend_label.set_text("Backend: none available")

    def _append(self, text: str, newline: bool = True) -> None:
        end_iter = self.buffer.get_end_iter()
        self.buffer.insert(end_iter, text + ("\n" if newline else ""))
        self.textview.scroll_to_iter(self.buffer.get_end_iter(), 0.0, False, 0, 0)

    def _send(self) -> None:
        if self._busy:
            return
        text = self.entry.get_text().strip()
        if not text:
            return
        self.entry.set_text("")
        self._append(f"You: {text}")
        self._busy = True
        model_text = self.model_combo.get_active_text()
        self._selected_model = model_text

        use_local_brain = bool(self._backends) and (
            self._backends.get("ollama", {}).get("ok")
            or self._backends.get("trueforge", {}).get("ok")
        )
        if use_local_brain:
            threading.Thread(target=self._query_local_brain, args=(text,), daemon=True).start()
        elif shutil.which("ollama"):
            threading.Thread(target=self._query_ollama, args=(text,), daemon=True).start()
        else:
            threading.Thread(target=self._local_fallback, args=(text,), daemon=True).start()

    def _start_assistant_message(self) -> None:
        self._append("Assistant: ", newline=False)
        end = self.buffer.get_end_iter()
        self._assistant_mark = self.buffer.create_mark("assistant_end", end, left_gravity=False)

    def _append_delta(self, chunk: str) -> None:
        if not chunk:
            return
        if not self._assistant_mark:
            self._append(chunk, newline=False)
            return
        mark_iter = self.buffer.get_iter_at_mark(self._assistant_mark)
        self.buffer.insert(mark_iter, chunk)
        new_end = self.buffer.get_end_iter()
        self.buffer.move_mark(self._assistant_mark, new_end)
        self.textview.scroll_to_iter(new_end, 0.0, False, 0, 0)

    def _finish_assistant_message(self) -> None:
        if self._assistant_mark:
            mark_iter = self.buffer.get_iter_at_mark(self._assistant_mark)
            self.buffer.insert(mark_iter, "\n")
            self.buffer.delete_mark(self._assistant_mark)
            self._assistant_mark = None
        self.textview.scroll_to_iter(self.buffer.get_end_iter(), 0.0, False, 0, 0)

    def _query_local_brain(self, text: str) -> None:
        """Stream a response from local-brain, with retry and backend fallback."""
        import http.client

        last_error = None
        for attempt in range(2):
            try:
                payload = {
                    "message": text,
                    "session_id": self._session_id,
                }
                if self._selected_model:
                    payload["model"] = self._selected_model
                body = json.dumps(payload).encode("utf-8")
                conn = http.client.HTTPConnection("127.0.0.1", 9876, timeout=120)
                conn.request(
                    "POST",
                    "/api/chat",
                    body=body,
                    headers={"Content-Type": "application/json"},
                )
                resp = conn.getresponse()
                GLib.idle_add(self._start_assistant_message)
                while True:
                    line = resp.readline()
                    if not line:
                        break
                    try:
                        line_text = line.decode("utf-8").strip()
                    except UnicodeDecodeError:
                        continue
                    if not line_text.startswith("data:"):
                        continue
                    data = line_text[5:].strip()
                    if not data:
                        continue
                    try:
                        event = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    etype = event.get("type", "")
                    if etype == "delta":
                        chunk = event.get("content", "")
                        if chunk:
                            GLib.idle_add(self._append_delta, chunk)
                    elif etype == "tool_calls":
                        calls = event.get("tool_calls", [])
                        if calls:
                            GLib.idle_add(self._append_delta, f"\n[Tool approval required: {len(calls)}]\n")
                    elif etype == "tool_response":
                        resp_text = event.get("content", "")
                        if resp_text:
                            GLib.idle_add(self._append_delta, f"\n[Tool result]\n{resp_text}\n")
                    elif etype == "error":
                        GLib.idle_add(self._append_delta, f"\n[error: {event.get('content', '')}]")
                conn.close()
                GLib.idle_add(self._finish_assistant_message)
                GLib.idle_add(lambda: setattr(self, "_busy", False))
                return
            except Exception as exc:
                last_error = exc
                if attempt == 0:
                    time.sleep(1)
                else:
                    break
        GLib.idle_add(self._append, f"Assistant: [error reaching local-brain] {last_error}")
        if shutil.which("ollama"):
            threading.Thread(target=self._query_ollama, args=(text,), daemon=True).start()
        else:
            GLib.idle_add(lambda: setattr(self, "_busy", False))

    def _query_ollama(self, text: str) -> None:
        """Best-effort direct query to Ollama if local-brain is unavailable."""
        import urllib.request

        model = "llama3.2:3b"
        if self._selected_model:
            if self._selected_model.startswith("ollama/"):
                model = self._selected_model.split("/", 1)[1]
            elif not self._selected_model.startswith("trueforge/"):
                model = self._selected_model
        else:
            try:
                req = urllib.request.Request("http://localhost:11434/api/tags", method="GET")
                with urllib.request.urlopen(req, timeout=3) as resp:
                    tags = json.loads(resp.read().decode("utf-8"))
                available = [m.get("name") for m in tags.get("models", []) if m.get("name")]
                if available:
                    model = available[0]
            except Exception:
                pass
        try:
            payload = json.dumps({"model": model, "prompt": text, "stream": False}).encode()
            req = urllib.request.Request(
                "http://localhost:11434/api/generate",
                data=payload,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                answer = data.get("response", "").strip()
            GLib.idle_add(self._append, f"Assistant: {answer}")
        except Exception as exc:
            GLib.idle_add(self._append, f"Assistant: [error] {exc}")
        GLib.idle_add(lambda: setattr(self, "_busy", False))

    def _local_fallback(self, text: str) -> None:
        lower = text.lower()
        answer = ""
        if any(word in lower for word in ["camera", "mic", "microphone", "permissions", "portal"]):
            answer = "Use the Privacy tab to see real-time camera/mic users and stop them. Also check the Apps tab for Flatpak permissions."
        elif any(word in lower for word in ["process", "background", "service", "kill", "stop"]):
            answer = "Use the Processes tab, click Refresh, and use Stop/Kill on any process. Use Scan risks to find suspicious background processes."
        elif any(word in lower for word in ["update", "upgrade", "dnf", "package"]):
            answer = "Go to Maintenance → Manage updates (or Maintain → Update manager) to see grouped packages and update all or selected ones."
        elif any(word in lower for word in ["virus", "malware", "scan", "clamav", "antivirus"]):
            answer = "Open the Antivirus tab, ensure ClamAV definitions are current, then run Quick scan or Full scan."
        elif any(word in lower for word in ["audio", "sound", "headphone", "mic"]):
            answer = "Run Fix all or click Maintain → Fix audio routing. That will route output to onboard speakers/headphones and input to the K66 mic."
        elif any(word in lower for word in ["camera", "usb", "error"]):
            answer = "The USB errors check is in the Diagnostics list. If the BRIO disappears, use Maintain → Reset USB camera."
        else:
            answer = "I can currently help with this app's built-in diagnostics. No local AI agent is running. Start local-brain (port 9876) or Ollama (port 11434)."
        GLib.idle_add(self._append, f"Assistant: {answer}")
        GLib.idle_add(lambda: setattr(self, "_busy", False))


# ---------------------------------------------------------------------------
# Main diagnostic window additions
# ---------------------------------------------------------------------------


class DiagnosticWindow(Gtk.ApplicationWindow):
    def __init__(self, application: Gtk.Application | None = None) -> None:
        super().__init__(title="Local Butler Diagnostic", application=application)
        self.set_default_size(900, 700)
        self.set_position(Gtk.WindowPosition.CENTER)
        self.set_icon_name("applications-engineering")

        header = Gtk.HeaderBar(title="Local Butler Diagnostic", show_close_button=True)

        run_all_btn = Gtk.Button(label="Diagnose")
        run_all_btn.set_tooltip_text("Run all checks")
        run_all_btn.connect("clicked", self._on_run_all)
        header.pack_start(run_all_btn)

        fix_all_btn = Gtk.Button(label="Fix all")
        fix_all_btn.set_tooltip_text("Diagnose and apply available fixes")
        fix_all_btn.connect("clicked", self._on_fix_all)
        header.pack_start(fix_all_btn)

        maintenance_btn = Gtk.Button(label="Maintain")
        maintenance_btn.set_tooltip_text("Open maintenance actions")
        maintenance_btn.connect("clicked", self._on_maintenance)
        header.pack_start(maintenance_btn)

        self.set_titlebar(header)

        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.add(vbox)

        self.status_label = Gtk.Label(label="Click 'Diagnose' to start.")
        self.status_label.set_margin_top(12)
        self.status_label.set_margin_bottom(6)
        vbox.pack_start(self.status_label, False, False, 0)

        self.notebook = Gtk.Notebook()
        vbox.pack_start(self.notebook, True, True, 0)

        self.pages: dict[str, Gtk.ListBox] = {}
        self.privacy_scanner: PrivacyScanner | None = None
        self.process_manager: ProcessManager | None = None
        for category in ["Audio", "Camera", "USB", "Privacy", "Apps", "Maintenance", "Processes", "Antivirus", "App Health", "Gaming", "Terminal", "AI"]:
            if category == "Privacy":
                vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
                scanner = PrivacyScanner()
                self.privacy_scanner = scanner
                vbox.pack_start(scanner, False, False, 0)
                listbox = Gtk.ListBox()
                listbox.set_selection_mode(Gtk.SelectionMode.NONE)
                scroll = Gtk.ScrolledWindow()
                scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
                scroll.add(vbox)
                label = Gtk.Label(label=category)
                self.notebook.append_page(scroll, label)
                self.pages[category] = listbox
            elif category == "Processes":
                manager = ProcessManager()
                self.process_manager = manager
                label = Gtk.Label(label=category)
                self.notebook.append_page(manager, label)
                self.pages[category] = manager.proc_list
            elif category == "Antivirus":
                av = AntivirusManager()
                label = Gtk.Label(label=category)
                self.notebook.append_page(av, label)
                self.pages[category] = Gtk.ListBox()
            elif category == "App Health":
                app_health = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
                header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
                app_health.pack_start(header, False, False, 0)
                title = Gtk.Label(label="<b>Installed app &amp; game health checks</b>")
                title.set_use_markup(True)
                title.set_xalign(0)
                header.pack_start(title, True, True, 0)
                run_btn = Gtk.Button(label="Run checks")
                run_btn.connect("clicked", lambda _b: self._run_app_health())
                header.pack_end(run_btn, False, False, 0)
                self.app_health_list = Gtk.ListBox()
                self.app_health_list.set_selection_mode(Gtk.SelectionMode.NONE)
                scroll = Gtk.ScrolledWindow()
                scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
                scroll.add(self.app_health_list)
                app_health.pack_start(scroll, True, True, 0)
                label = Gtk.Label(label=category)
                self.notebook.append_page(app_health, label)
                self.pages[category] = self.app_health_list
            elif category == "Terminal":
                log_term = LogTerminal()
                label = Gtk.Label(label=category)
                self.notebook.append_page(log_term, label)
                self.pages[category] = Gtk.ListBox()
            elif category == "Gaming":
                gaming_mgr = gaming.GamingManager()
                label = Gtk.Label(label=category)
                self.notebook.append_page(gaming_mgr, label)
                self.pages[category] = Gtk.ListBox()
            elif category == "AI":
                ai_chat = LocalAIChat()
                label = Gtk.Label(label=category)
                self.notebook.append_page(ai_chat, label)
                self.pages[category] = Gtk.ListBox()
            else:
                listbox = Gtk.ListBox()
                listbox.set_selection_mode(Gtk.SelectionMode.NONE)
                scroll = Gtk.ScrolledWindow()
                scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
                scroll.add(listbox)
                label = Gtk.Label(label=category)
                self.notebook.append_page(scroll, label)
                self.pages[category] = listbox

        self.rows: dict[str, ResultRow] = {}
        for check in get_all_checks():
            row = ResultRow(CheckResult(check.name, "info", "Not checked yet", category=check.category))
            self.pages[check.category].add(row)
            self.rows[check.name] = row

    def _run_app_health(self) -> None:
        def worker() -> None:
            checks = _get_app_health_checks()
            for check in checks:
                result = check.func()
                GLib.idle_add(self._update_row, check.name, result)
            GLib.idle_add(self.status_label.set_text, "App health checks complete.")
        threading.Thread(target=worker, daemon=True).start()

    def _on_run_all(self, _button: Gtk.Button) -> None:
        thread = threading.Thread(target=self._run_checks_thread, daemon=True)
        thread.start()

    def _on_fix_all(self, _button: Gtk.Button) -> None:
        thread = threading.Thread(target=self._fix_all_thread, daemon=True)
        thread.start()

    def _on_maintenance(self, _button: Gtk.Button) -> None:
        MaintenanceDialog(self)

    def _run_checks_thread(self) -> None:
        GLib.idle_add(self.status_label.set_text, "Running diagnostics...")
        for check in get_all_checks():
            result = check.func()
            GLib.idle_add(self._update_row, check.name, result)
        GLib.idle_add(self.status_label.set_text, "Diagnostics complete.")

    def _fix_all_thread(self) -> None:
        GLib.idle_add(self.status_label.set_text, "Diagnosing...")
        results: dict[str, CheckResult] = {}
        for check in get_all_checks():
            results[check.name] = check.func()
            GLib.idle_add(self._update_row, check.name, results[check.name])

        for check in get_all_checks():
            result = results[check.name]
            if result.status in ("warn", "error") and (result.fix_callback or result.fix_command):
                GLib.idle_add(self.status_label.set_text, f"Fixing {check.name}...")
                _apply_fix(result)
                time.sleep(0.3)
                new_result = check.func()
                GLib.idle_add(self._update_row, check.name, new_result)

        GLib.idle_add(self.status_label.set_text, "Fix-all complete.")

    def _update_row(self, name: str, result: CheckResult) -> None:
        old_row = self.rows[name]
        parent = old_row.get_parent()
        if parent:
            parent.remove(old_row)
        new_row = ResultRow(result)
        self.rows[name] = new_row
        self.pages[result.category].add(new_row)
        new_row.show_all()


def _apply_fix(result: CheckResult) -> None:
    """Apply the fix attached to a check result."""
    if result.fix_callback:
        try:
            sig = inspect.signature(result.fix_callback)
            if any(p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY) for p in sig.parameters.values()):
                result.fix_callback(parent=None)
            else:
                result.fix_callback()
        except TypeError:
            result.fix_callback()
        return
    if not result.fix_command:
        return
    cmd = result.fix_command
    if cmd[0] in ("systemctl", "wpctl"):
        subprocess.run(cmd, check=False)
    elif shutil.which("pkexec"):
        subprocess.run(["pkexec"] + cmd, check=False)
    else:
        subprocess.run(cmd, check=False)


def _print_result(result: CheckResult) -> None:
    color_map = {
        "ok": "\033[32m",
        "warn": "\033[33m",
        "error": "\033[31m",
        "info": "\033[34m",
    }
    reset = "\033[0m"
    color = color_map.get(result.status, "")
    print(f"{color}[{result.icon()}] {result.name} — {result.status.upper()}: {result.summary}{reset}")
    if result.detail:
        for line in result.detail.splitlines()[:12]:
            print(f"      {line}")


def _run_cli(fix: bool = False) -> int:
    print("Local Butler Diagnostic (CLI mode)\n")
    checks = get_all_checks()
    for check in checks:
        result = check.func()
        _print_result(result)
        if fix and result.status in ("warn", "error") and (result.fix_callback or result.fix_command):
            print("      → Applying fix...")
            _apply_fix(result)
            # Re-run the check to verify.
            result = check.func()
            _print_result(result)
        print()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Local Butler Diagnostic")
    parser.add_argument("--cli", action="store_true", help="Run diagnostics in the terminal")
    parser.add_argument("--fix", action="store_true", help="Auto-apply fixes in CLI mode")
    args = parser.parse_args(argv)

    if args.cli:
        return _run_cli(fix=args.fix)

    app = Gtk.Application(application_id="com.localbutler.Diagnostic")

    def _activate(app: Gtk.Application) -> None:
        if not getattr(app, "_window", None):
            app._window = DiagnosticWindow(application=app)
        app._window.show_all()
        app._window.present()

    app.connect("activate", _activate)
    return app.run(None)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
