# Local Butler Diagnostic — Code Review

> File: `src/local_butler/apps/diagnostic/main.py`
> Purpose: GTK system troubleshooter with audio, camera, USB, privacy, process, antivirus, app-health, terminal, and AI chat tabs.

---

## 1. Imports and environment dependencies

### Python standard library
```python
import argparse          # CLI arguments (--cli, --fix)
import datetime          # currently unused, reserved for timestamps
import glob              # list /dev/video* devices
import inspect           # inspect fix_callback signatures
import json              # parse pw-dump and package metadata
import os                # process IDs, environment scanning, permissions
import re                # regex for wpctl output, risk patterns
import shutil            # `which()`, file copies, disk checks
import signal            # SIGTERM / SIGKILL
import subprocess        # run commands, start background processes
import sys               # sys.argv, SystemExit
import tempfile          # cross-platform temp dir
import threading         # background checks, real-time logs/scans
import time              # small delays in fix-all loop
from dataclasses import dataclass, field  # state/value objects
from pathlib import Path # modern path handling
from typing import Callable  # type hints
```

### External GTK/PipeWire binding
```python
import gi
gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk
```
- `PyGObject` is the bridge to GTK 3.
- `GLib` is used to schedule UI updates from worker threads via `GLib.idle_add`.
- `Gtk.ListBox`, `Gtk.Notebook`, `Gtk.ApplicationWindow`, etc. build the interface.

### Data globals
```python
HOME = Path.home()
TMP_DIR = Path(tempfile.gettempdir())
```

---

## 2. Helper functions

### `run(...)`
Wraps `subprocess.run` but guarantees a `CompletedProcess` is returned even on timeout or missing binary. This prevents crashes when the target tool is not installed.

### `has_command(name)`
Returns `True` if a command exists on `$PATH`.

### `active_state(unit, user=True)`
Returns `active`/`inactive`/etc. for a systemd unit using `systemctl --user is-active`. Used to check `privacy-dashboard` and `xdg-desktop-portal`.

### `pretty_bytes(num)`
Converts bytes to human-readable units.

### `dir_size(path)`
Recursively calculates directory size, skipping symlinks and ignoring permission errors.

### `get_camera_users()`
Uses `lsof /dev/video*` to find processes currently holding the V4L2 camera.

### `get_mic_users()`
Parses `pw-dump Node` JSON and finds `Stream/Input/Audio` nodes. Returns unique `(name, pid)` pairs.

### `kill_process(pid, sig)`
Uses `ps -p <pid> -o uid=` to decide whether `pkexec` is needed (i.e. killing processes owned by other users). Falls back to `kill -SIG <pid>` for the user’s own processes.

### `get_running_user_services()`
Runs `systemctl --user list-units --type=service --state=running` and parses lines into dicts.

### `get_running_processes(limit)`
Runs `ps -eo pid,pcpu,pmem,comm,args --sort=-%cpu` and returns top-N processes by CPU.

---

## 3. Data models

### `CheckResult` dataclass
Holds the result of one diagnostic check:
- `name` / `category`
- `status`: ok, warn, error, info
- `summary`: one-line user-visible message
- `detail`: multi-line explanation/debug output
- `fix_command`: list of strings to run as root/user
- `fix_callback`: Python function to call when Fix is clicked
- `fix_label`: text shown on the Fix button
- `icon()`: returns a Unicode check/warn/error/info symbol

### `Check` dataclass
Bundles a `name`, `category`, and the callable that performs the check (`func`).

---

## 4. Diagnostic checks

### Audio checks
- `check_default_source()`
  - Parses `wpctl status` to find the configured default source.
  - Uses `_node_exists()` and `_find_onboard_source_id()` from `pw-dump Node` JSON.
  - Marks USB mic as ok when present and error if missing.
- `check_mic_capture()`
  - Records one second from the default ALSA source using `arecord`.
- `check_audio_routing()`
  - Verifies output sink is onboard PCI and that the K66 USB mic is selected when present.
  - Falls back to onboard if K66 is not selected.

### Camera checks
- `check_camera_present()`
  - Uses `v4l2-ctl --list-devices` to verify the BRIO exists.
  - Shows a Fix button that runs `local-butler-fix-camera`.
- `check_camera_capture()`
  - Captures one JPEG frame with `ffmpeg` from `/dev/video0` at 640×480.
- `check_cheese_config()`
  - Reads GNOME Cheese settings via `gsettings` to confirm BRIO is selected and resolution is conservative.

### USB check
- `check_usb_errors()`
  - Reads `dmesg` and only reports errors that happened after the Logitech BRIO last enumerated.

### Privacy checks
- `check_privacy_dashboard()`
  - Checks if the custom `privacy-dashboard` service is active; offers a stop button.
- `check_portal_service()`
  - Confirms `xdg-desktop-portal` is running for Flatpak/browser portal prompts.

### App-permission checks
- `_get_installed_flatpaks()` / `_is_installed()` / `_get_installed_app_checks()`
  - Builds a registry (Brave, Firefox, Jitsi, Viber, Telegram, WhatsApp Desktop, Signal, Teams, Skype, Discord, Zoom, etc.).
  - Only adds a check for apps actually present on the machine.
- `_flatpak_perm()` / `_make_flatpak_check()`
  - Reads Flatpak portal permissions from `flatpak permission-show`.
  - Returns OK if both camera and microphone are granted; otherwise shows a Fix button to grant them.

### Maintenance checks
- `check_system_updates()`
  - Runs `dnf check-update -q`.
  - Exit code 100 means updates are available.
  - Fix callback opens the grouped update manager.
- `check_temp_size()`
  - Recursively sizes the temp directory and warns if > 5 GB.

### `get_all_checks()`
Combines `ALL_CHECKS` with dynamically discovered installed-app checks.

---

## 5. GTK UI classes

### `ResultRow`
A single row in any diagnostic category list:
- Left: colored status icon.
- Middle: bold check name + summary.
- Right: optional Fix button and Details button.
- `_on_fix` detects whether the fix is a command or callback and dispatches accordingly.
- `_on_details` opens a `Gtk.MessageDialog` with up to 2000 characters of detail.

### `PrivacyScanner`
- Header with Refresh and Kill all.
- `get_camera_users()` + `get_mic_users()` populate a `ListBox`.
- Each row has Open (terminal), Stop, Kill.
- `_open_pid` launches a terminal showing `ps`, `lsof`, environment, and journal for the chosen PID.

### `ProcessManager`
Three subtabs:
1. **Processes** — top-100 CPU processes from `ps`, with Open/Stop/Kill.
2. **Services** — running user systemd services, with Stop service.
3. **Risk scan** — heuristic scan for duplicates, suspicious keywords, remote-access tools, cloud-sync agents, and temp-dir execution. Each finding has a Stop all button.

### `MaintenanceDialog`
Modal with quick actions:
- Update manager
- Reset USB camera
- Fix audio routing
- Stop privacy dashboard

### `UpdateManagerDialog`
A grouped update tree:
- TreeView with checkbox column.
- Groups updates by repository.
- Buttons: Refresh, Select all, Deselect all, Update selected, Update all, Versions/downgrade.
- Per-package version dialog shows all installable versions via `dnf repoquery`.

### `AntivirusManager`
- ClamAV version/definition status.
- `Update defs`, `Quick scan home`, `Full scan home`, custom path scan.
- Uses `clamdscan --multiscan` if available, otherwise `clamscan -i -r`.
- Quarantine directory created at `/tmp/local_butler_quarantine`.
- Real-time log output in a `Gtk.TextView` via a background thread.

### Steam/app game health checks
- `_run_steam_game_check(app_name, app_id)` reads `<steamlib>/appmanifest_<id>.acf`, resolves the install directory, and counts files to detect corruption.
- `_get_app_health_checks()` adds a check for Hearts of Iron Iron IV and dynamically for every installed app.

### `LogTerminal`
- Follow PID / Follow app / Run command modes.
- Spawns background reader threads and updates the UI via `GLib.idle_add`.

### `LocalAIChat`
- Detects Ollama, llama-server, or local-ai.
- If a server is present, queries `localhost:11434/api/generate` or `localhost:8080/completion`.
- If absent, uses a hard-coded keyword fallback so the chat can still control the app conceptually.

### `DiagnosticWindow`
- `Gtk.ApplicationWindow` with headerbar Diagnose / Fix all / Maintain buttons.
- Builds one tab per category, plus Antivirus, App Health, Terminal, and AI.
- `_on_run_all` and `_on_fix_all` run checks in threads.
- `_update_row` replaces rows in the correct category list.

---

## 6. Known areas for improvement / better alternatives

| Current choice | What it does | Possible better alternative (next phase) |
|---------------|--------------|------------------------------------------|
| `subprocess.run` everywhere | Blocking shell calls | Replace hot paths with native D-Bus (systemd, PipeWire portals) via `dbus-python` or `Jeepney` |
| `ps`, `lsof`, `dmesg` | OS introspection | Use `/proc` parsing + `psutil` for cross-distro process info |
| `pw-dump` JSON | PipeWire state | Direct D-Bus calls to PipeWire/WirePlumber with `dbus-python` |
| `ffmpeg` for camera test | Capture single frame | `v4l2py` or `opencv-python` would avoid a process spawn |
| `arecord` for mic test | Record 1 s audio | Use Python `sounddevice` or PyAudio for in-process capture |
| ClamAV (`clamscan`/`clamdscan`) | Antivirus engine | Keep ClamAV; optionally add `Lynis` for system auditing or `Chkrootkit`/`Rkhunter` for rootkit checks |
| `dnf` | Package updates | Add `flatpak update` and `snap refresh` support |
| Flatpak portal permission DB | Read with `flatpak permission-show` | Query `~/.local/share/flatpak/db/devices` directly or use D-Bus `xdp-documents-portal` |
| GTK 3 | UI toolkit | GTK 4 + `libadwaita` for modern GNOME look |
| No persistence | Results are transient | SQLite store (`sqlite3`) for check history/trends |
| No notification integration | User must open app | Add `notify2` / `libnotify` alerts when a check turns red |
| AI chat fallback | Keyword-based | Integrate a bundled/local LLM like `llamafile` or `ollama` with function calling |

---

## 7. Security/reliability notes

- `kill_process` uses `pkexec` only when UID differs from the current user; otherwise it trusts the user.
- Update manager uses `pkexec dnf` for privilege escalation.
- Update dialog parses `dnf check-update` output; malformed lines could break parsing.
- Risk scanner is heuristic only; false positives are expected for common browsers.
- ClamAV scans run with the user’s permissions; system dirs are skipped unless run as root.
- AI chat performs HTTP requests to `localhost` only and has a sandboxed fallback.
