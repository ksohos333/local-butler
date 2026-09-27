#!/usr/bin/env python3
"""Open-source game compatibility checker for Local Butler Diagnostic.

Fetches system requirements from the public Steam store API and compares
them to local hardware. No proprietary web services are used.
"""

import html
import json
import re
import shutil
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import protondb

try:
    import gi

    gi.require_version("Gtk", "3.0")
    from gi.repository import GLib, Gtk
except Exception:  # pragma: no cover - headless environments without PyGObject
    import types

    class _NoGtk(types.ModuleType):
        def __getattr__(self, name):
            return object

    Gtk = _NoGtk("gi.repository.Gtk")
    GLib = _NoGtk("gi.repository.GLib")


HOME = Path.home()
STEAM_API_URL = "https://store.steampowered.com/api/appdetails?appids={app_id}"


class SystemSpecs:
    """Lightweight local hardware spec collector."""

    def __init__(self):
        self.cpu = self._cpu()
        self.cpu_cores = self._cpu_cores()
        self.ram_gb = self._ram_gb()
        self.gpu = self._gpu()
        self.disk_free_gb = self._disk_free_gb()
        self.vram_gb = self._vram_gb()
        self.os = self._os()

    def _cpu(self) -> str:
        try:
            for line in Path("/proc/cpuinfo").read_text(errors="ignore").splitlines():
                if line.startswith("model name"):
                    return line.split(":", 1)[1].strip()
        except Exception:
            pass
        return "Unknown"

    def _cpu_cores(self) -> int:
        try:
            return int(subprocess.run(["nproc"], capture_output=True, text=True, timeout=5).stdout.strip())
        except Exception:
            return 0

    def _ram_gb(self) -> float:
        try:
            with Path("/proc/meminfo").open() as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        kb = int(line.split()[1])
                        return round(kb / 1024 / 1024, 2)
        except Exception:
            pass
        return 0.0

    def _gpu(self) -> str:
        try:
            r = subprocess.run(["lspci", "-nn"], capture_output=True, text=True, timeout=5)
            for line in r.stdout.splitlines():
                if "VGA" in line or "3D controller" in line:
                    return line.split(":", 2)[-1].strip()
        except Exception:
            pass
        return "Unknown"

    def _disk_free_gb(self) -> float:
        try:
            st = shutil.disk_usage(str(HOME))
            return round(st.free / (1024 ** 3), 2)
        except Exception:
            return 0.0

    def _vram_gb(self) -> float:
        """Detect dedicated VRAM in GB. Prefers nvidia-smi, then amdgpu sysfs."""
        try:
            r = subprocess.run(
                ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if r.returncode == 0:
                line = r.stdout.strip().splitlines()[0]
                m = re.search(r"(\d+(?:\.\d+)?)\s*(MiB|MB|GiB|GB|TiB|TB)", line, re.I)
                if m:
                    value = float(m.group(1))
                    unit = m.group(2).lower()
                    if unit in ("mib", "mb"):
                        return round(value / 1024, 2)
                    if unit in ("gib", "gb"):
                        return round(value, 2)
                    if unit in ("tib", "tb"):
                        return round(value * 1024, 2)
        except Exception:
            pass
        try:
            total = 0
            for p in Path("/sys/class/drm").glob("card*/device/mem_info_vram_total"):
                total += int(p.read_text().strip())
            if total:
                return round(total / (1024 ** 3), 2)
        except Exception:
            pass
        return 0.0

    def _os(self) -> str:
        try:
            with Path("/etc/os-release").open() as f:
                for line in f:
                    if line.startswith("PRETTY_NAME="):
                        return line.split("=", 1)[1].strip().strip('"')
        except Exception:
            pass
        return "Linux"

    def summary(self) -> str:
        return (
            f"CPU: {self.cpu} ({self.cpu_cores} cores)\n"
            f"RAM: {self.ram_gb} GB\n"
            f"GPU: {self.gpu}\n"
            f"VRAM: {self.vram_gb} GB\n"
            f"Free disk: {self.disk_free_gb} GB\n"
            f"OS: {self.os}"
        )


class GameCompatibilityChecker:
    """Fetch a Steam game's PC requirements and compare to local specs."""

    def __init__(self, app_id: int):
        self.app_id = app_id
        self.specs = SystemSpecs()
        self.data: dict[str, Any] | None = None
        self.error: str | None = None

    def fetch(self) -> dict[str, Any]:
        try:
            url = STEAM_API_URL.format(app_id=self.app_id)
            req = urllib.request.Request(url, headers={"User-Agent": "LocalButler/1.0"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            app_data = payload.get(str(self.app_id), {})
            if not isinstance(app_data, dict) or not app_data.get("success"):
                self.error = "Steam returned no data for this app ID"
                return {}
            raw_data = app_data.get("data", {})
            self.data = raw_data if isinstance(raw_data, dict) else {}
            return self.data
        except urllib.error.HTTPError as e:
            self.error = f"HTTP {e.code}"
            return {}
        except Exception as e:
            self.error = str(e)
            return {}

    @staticmethod
    def _extract_number(text: str, patterns: List[str]) -> Optional[float]:
        for pat in patterns:
            m = re.search(pat, text, re.IGNORECASE)
            if m:
                try:
                    return float(m.group(1))
                except ValueError:
                    continue
        return None

    def _extract_line(self, text: str, label: str) -> str:
        m = re.search(rf"{label}:\s*(.+?)(?:<br\s*/?>|</li>|$)", text, re.IGNORECASE)
        if not m:
            m = re.search(rf"{label}\s+(.+?)(?:<br\s*/?>|</li>|$)", text, re.IGNORECASE)
        return re.sub(r"<[^>]+>", "", m.group(1)).strip() if m else ""

    def _parse_req(self, text: str) -> Dict[str, Any]:
        if not text:
            return {}
        cpu_req = self._extract_line(text, "Processor")
        gpu_req = self._extract_line(text, "Graphics")
        dx_req = self._extract_line(text, "DirectX")
        text_plain = re.sub(r"<[^>]+>", " ", text)
        text_plain = text_plain.replace("&nbsp;", " ")
        vram_gb = self._extract_number(gpu_req, [r"(\d+(?:\.\d+)?)\s*GB"])
        windows_match = bool(re.search(r"Windows[^0-9]*\b(7|8|10|11)", text_plain, re.I))
        linux_match = bool(re.search(r"\b(Linux|SteamOS|Steam Deck)\b", text_plain, re.I))
        os_required = "linux" if linux_match else ("windows" if windows_match else None)
        return {
            "ram_gb": self._extract_number(text_plain, [r"(\d+(?:\.\d+)?)\s*GB\s*RAM", r"(\d+)\s*GB\s*Memory"]),
            "disk_gb": self._extract_number(text_plain, [r"(\d+(?:\.\d+)?)\s*GB\s*(?:available|free|storage|space|HDD|SSD)"]),
            "os_windows": windows_match,
            "os_required": os_required,
            "cpu_req": cpu_req,
            "gpu_req": gpu_req,
            "vram_gb": vram_gb,
            "dx_version": dx_req,
        }

    def check(self) -> Dict[str, Any]:
        if self.data is None:
            self.fetch()
        result = {
            "name": self.data.get("name", f"App {self.app_id}"),
            "specs": self.specs,
            "minimum": {},
            "recommended": {},
            "verdict": "unknown",
            "detail": "",
            "error": self.error,
            "proton": None,
            "proton_version": None,
            "wine": shutil.which("wine") is not None,
            "protondb": None,
        }
        if self.error or not self.data:
            result["verdict"] = "error"
            return result

        reqs = self.data.get("pc_requirements", {}) or {}
        if isinstance(reqs, list):
            reqs = {}
        min_text = reqs.get("minimum", "")
        rec_text = reqs.get("recommended", "")
        result["minimum"] = self._parse_req(min_text)
        result["recommended"] = self._parse_req(rec_text)
        result["raw_minimum"] = min_text
        result["raw_recommended"] = rec_text

        proton_path = _find_proton()
        result["proton"] = proton_path
        result["proton_version"] = Path(proton_path).name if proton_path else None
        wine = shutil.which("wine") is not None
        result["wine"] = wine
        result["protondb"] = protondb.lookup(self.app_id)

        min_ram = result["minimum"].get("ram_gb")
        min_disk = result["minimum"].get("disk_gb")
        rec_ram = result["recommended"].get("ram_gb")
        rec_disk = result["recommended"].get("disk_gb")

        issues = []
        if min_ram and self.specs.ram_gb < min_ram:
            issues.append(f"RAM below minimum ({self.specs.ram_gb} GB < {min_ram} GB)")
        if min_disk and self.specs.disk_free_gb < min_disk:
            issues.append(f"Free disk below minimum ({self.specs.disk_free_gb} GB < {min_disk} GB)")
        min_vram = result["minimum"].get("vram_gb")
        if min_vram and self.specs.vram_gb and self.specs.vram_gb < min_vram:
            issues.append(f"VRAM below minimum ({self.specs.vram_gb} GB < {min_vram} GB)")

        os_required = result["minimum"].get("os_required") or result["recommended"].get("os_required")
        windows_only = os_required == "windows" or (os_required is None and result["minimum"].get("os_windows"))
        native_linux = os_required == "linux"

        if issues:
            result["verdict"] = "fail"
            result["detail"] = "; ".join(issues)
            if windows_only:
                result["detail"] += "; Windows-only title"
            elif native_linux:
                result["detail"] += "; Native Linux title"
        elif native_linux:
            result["verdict"] = "ok"
            result["detail"] = "Native Linux game — should run without compatibility layers."
        elif windows_only:
            rating = result.get("protondb") or {}
            tier = rating.get("tier")
            if proton_path:
                proton_name = Path(proton_path).name
                if tier in ("platinum", "gold"):
                    result["verdict"] = "ok"
                    result["detail"] = (
                        f"Windows-only, but ProtonDB rates it {tier.upper()} "
                        f"({rating.get('reports', 0)} reports) and {proton_name} is installed."
                    )
                elif tier in ("silver", "bronze"):
                    result["verdict"] = "warn"
                    result["detail"] = (
                        f"Windows-only. ProtonDB rates it {tier.upper()} — "
                        f"expect minor issues; {proton_name} is installed."
                    )
                elif tier == "borked":
                    result["verdict"] = "fail"
                    result["detail"] = (
                        "Windows-only. ProtonDB reports BORKED — "
                        "unlikely to run well even with Proton."
                    )
                else:
                    result["verdict"] = "warn"
                    result["detail"] = (
                        f"Windows-only, but {proton_name} is installed. "
                        "ProtonDB data unavailable."
                    )
            elif wine:
                result["verdict"] = "warn"
                result["detail"] = (
                    "Windows-only game; Wine is installed but Proton is recommended for Steam titles."
                )
            else:
                result["verdict"] = "fail"
                result["detail"] = "Windows-only game; install Proton (Steam) or Wine to run on Linux."
        elif rec_ram and self.specs.ram_gb < rec_ram:
            result["verdict"] = "warn"
            result["detail"] = f"RAM below recommended ({self.specs.ram_gb} GB < {rec_ram} GB)"
        elif rec_disk and self.specs.disk_free_gb < rec_disk:
            result["verdict"] = "warn"
            result["detail"] = f"Free disk below recommended ({self.specs.disk_free_gb} GB < {rec_disk} GB)"
        else:
            result["verdict"] = "ok"
            result["detail"] = "Local specs meet or exceed recommended requirements."
        return result


def _find_proton() -> Optional[str]:
    """Return path to the newest Proton installation in the Steam library."""
    lib = find_steam_library()
    if not lib:
        return None
    common = lib / "common"
    if not common.exists():
        return None
    for path in sorted(common.glob("Proton*"), key=lambda p: p.name.lower(), reverse=True):
        return str(path)
    return None


def find_steam_library() -> Optional[Path]:
    """Return the Flatpak Steam steamapps path if present."""
    candidates = [
        HOME / ".var" / "app" / "com.valvesoftware.Steam" / ".local" / "share" / "Steam" / "steamapps",
        HOME / ".local" / "share" / "Steam" / "steamapps",
    ]
    for c in candidates:
        if c.exists():
            return c
    return None


def installed_steam_games() -> List[Dict[str, Any]]:
    lib = find_steam_library()
    if not lib:
        return []
    games = []
    for acf in lib.glob("appmanifest_*.acf"):
        m = re.search(r"appmanifest_(\d+)\.acf", acf.name)
        if not m:
            continue
        app_id = int(m.group(1))
        text = acf.read_text(errors="ignore")
        name_match = re.search(r'"name"\s+"([^"]+)"', text)
        if name_match:
            games.append({"app_id": app_id, "name": name_match.group(1)})
    return sorted(games, key=lambda x: x["name"].lower())


class GamingManager(Gtk.Box):
    """GTK panel for game compatibility checks with AI auto-fix."""

    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.set_margin_top(12)
        self.set_margin_bottom(12)
        self.set_margin_start(12)
        self.set_margin_end(12)

        self._last_result: Dict[str, Any] | None = None

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.pack_start(header, False, False, 0)

        title = Gtk.Label(label="<b>Gaming compatibility</b>")
        title.set_use_markup(True)
        title.set_xalign(0)
        header.pack_start(title, True, True, 0)

        self.game_combo = Gtk.ComboBoxText()
        self.game_combo.set_hexpand(True)
        self._populate_games()
        header.pack_start(self.game_combo, True, True, 0)

        check_btn = Gtk.Button(label="Check selected game")
        check_btn.connect("clicked", self._on_check_game)
        header.pack_start(check_btn, False, False, 0)

        self.fix_btn = Gtk.Button(label="Auto-fix with bigus_dickus")
        self.fix_btn.set_sensitive(False)
        self.fix_btn.set_tooltip_text("Ask the local AI agent to suggest or apply a fix")
        self.fix_btn.connect("clicked", self._on_auto_fix)
        header.pack_start(self.fix_btn, False, False, 0)

        custom_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.pack_start(custom_box, False, False, 0)
        self.custom_entry = Gtk.Entry()
        self.custom_entry.set_placeholder_text("Or enter a Steam app ID manually...")
        self.custom_entry.set_hexpand(True)
        self.custom_entry.connect("activate", lambda _e: self._on_check_custom())
        custom_box.pack_start(self.custom_entry, True, True, 0)
        custom_btn = Gtk.Button(label="Check ID")
        custom_btn.connect("clicked", lambda _b: self._on_check_custom())
        custom_box.pack_start(custom_btn, False, False, 0)

        specs = SystemSpecs()
        specs_markup = html.escape(
            f"CPU: {specs.cpu} ({specs.cpu_cores} cores), {specs.ram_gb} GB RAM, {specs.gpu}, {specs.disk_free_gb} GB free"
        )
        specs_label = Gtk.Label()
        specs_label.set_markup(f"<span size='small'><b>Your machine:</b> {specs_markup}</span>")
        specs_label.set_xalign(0)
        specs_label.set_line_wrap(True)
        self.pack_start(specs_label, False, False, 0)

        self.result_label = Gtk.Label(label="Select a game and click Check.")
        self.result_label.set_xalign(0)
        self.result_label.set_line_wrap(True)
        self.pack_start(self.result_label, False, False, 0)

        self.textview = Gtk.TextView()
        self.textview.set_editable(False)
        self.textview.set_monospace(True)
        self.textview.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.buffer = self.textview.get_buffer()
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroll.set_vexpand(True)
        scroll.add(self.textview)
        self.pack_start(scroll, True, True, 0)

        self.fix_label = Gtk.Label()
        self.fix_label.set_xalign(0)
        self.fix_label.set_line_wrap(True)
        self.pack_start(self.fix_label, False, False, 0)

        self.fix_textview = Gtk.TextView()
        self.fix_textview.set_editable(False)
        self.fix_textview.set_monospace(True)
        self.fix_textview.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.fix_buffer = self.fix_textview.get_buffer()
        fix_scroll = Gtk.ScrolledWindow()
        fix_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        fix_scroll.set_vexpand(True)
        fix_scroll.add(self.fix_textview)
        self.pack_start(fix_scroll, True, True, 0)

    def _populate_games(self):
        self.games = installed_steam_games()
        self.game_combo.append_text("-- choose an installed Steam game --")
        for g in self.games:
            self.game_combo.append_text(f"{g['name']} ({g['app_id']})")
        if self.games:
            self.game_combo.set_active(0)

    def _on_check_game(self, _btn):
        idx = self.game_combo.get_active()
        if idx <= 0 or idx - 1 >= len(self.games):
            self.result_label.set_text("Select a game from the list.")
            return
        game = self.games[idx - 1]
        self._run_check(game["app_id"], game["name"])

    def _on_check_custom(self):
        text = self.custom_entry.get_text().strip()
        if not text.isdigit():
            self.result_label.set_text("Enter a numeric Steam app ID.")
            return
        self._run_check(int(text), f"App {text}")

    def _run_check(self, app_id: int, name: str):
        self.result_label.set_text(f"Checking {name}...")
        self.buffer.set_text("")
        self.fix_buffer.set_text("")
        self.fix_label.set_text("")
        self.fix_btn.set_sensitive(False)
        self._last_result = None

        def worker():
            checker = GameCompatibilityChecker(app_id)
            result = checker.check()
            GLib.idle_add(self._show_result, result)

        threading.Thread(target=worker, daemon=True).start()

    def _on_auto_fix(self, _btn):
        if not self._last_result:
            return
        self.fix_btn.set_sensitive(False)
        self.fix_label.set_markup("<b>bigus_dickus is thinking...</b>")
        self.fix_buffer.set_text("")
        threading.Thread(target=self._run_agent_fix, daemon=True).start()

    def _run_agent_fix(self) -> None:
        import http.client

        result = self._last_result
        if not result:
            return
        prompt = (
            "You are bigus_dickus, a local Linux gaming expert. The user ran a Steam game "
            "compatibility check and it did not pass cleanly. Decide the safest fix or workaround "
            "and apply it if you can using your available tools. If you cannot apply it automatically, "
            "list the exact commands the user should run. Prefer non-destructive changes and ask for "
            "approval before anything risky.\n\n"
            f"Game: {result.get('name', 'Unknown')}\n"
            f"Local specs:\n{result['specs'].summary()}\n\n"
            f"Verdict: {result.get('verdict', 'unknown')}\n"
            f"Issues: {result.get('detail', 'No detail')}\n\n"
            f"Parsed minimum requirements:\n{json.dumps(result.get('minimum', {}), indent=2)}\n\n"
            f"Parsed recommended requirements:\n{json.dumps(result.get('recommended', {}), indent=2)}\n\n"
            f"Proton installed: {result.get('proton') is not None}\n"
            f"Wine installed: {result.get('wine', False)}\n"
        )

        try:
            body = json.dumps({"message": prompt}).encode("utf-8")
            conn = http.client.HTTPConnection("127.0.0.1", 9876, timeout=120)
            conn.request(
                "POST",
                "/api/chat",
                body=body,
                headers={"Content-Type": "application/json"},
            )
            resp = conn.getresponse()
            parts: list[str] = []
            tool_responses: list[str] = []
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
                    parts.append(event.get("content", ""))
                elif etype == "tool_response":
                    tool_responses.append(event.get("content", ""))
                elif etype == "error":
                    parts.append(f"\n[error: {event.get('content', '')}]")
            conn.close()
            answer = "".join(parts).strip()
            if tool_responses:
                answer += "\n\n[Tool results]\n" + "\n".join(tool_responses)
            GLib.idle_add(self._show_fix_result, answer)
        except Exception as exc:
            GLib.idle_add(self._show_fix_result, f"[error reaching bigus_dickus] {exc}")

    def _show_fix_result(self, text: str) -> None:
        self.fix_buffer.set_text(text)
        self.fix_label.set_markup("<b>bigus_dickus fix suggestion:</b>")
        self.fix_btn.set_sensitive(True)

    def _show_result(self, result: Dict[str, Any]):
        self._last_result = result
        verdict = result.get("verdict", "unknown")
        name = html.escape(str(result.get("name", "Game")))
        detail = html.escape(str(result.get("detail", "")))
        if verdict == "error":
            self.result_label.set_markup(f"<b>{name}:</b> Could not check — {detail}")
            self.fix_btn.set_sensitive(False)
            return
        colors = {"ok": "#2ecc71", "warn": "#f39c12", "fail": "#e74c3c", "unknown": "#95a5a6"}
        color = colors.get(verdict, "#95a5a6")
        self.result_label.set_markup(
            f"<b>{name}:</b> <span foreground='{color}'><b>{verdict.upper()}</b></span> — {detail}"
        )
        text = "Your machine:\n"
        text += result["specs"].summary() + "\n\n"
        text += "Minimum requirements (parsed):\n"
        text += json.dumps(result.get("minimum", {}), indent=2) + "\n\n"
        text += "Recommended requirements (parsed):\n"
        text += json.dumps(result.get("recommended", {}), indent=2) + "\n\n"
        text += "Raw minimum:\n" + result.get("raw_minimum", "N/A") + "\n\n"
        text += "Raw recommended:\n" + result.get("raw_recommended", "N/A") + "\n"
        protondb = result.get("protondb")
        if protondb:
            text += "\nProtonDB compatibility:\n"
            text += json.dumps(protondb, indent=2) + "\n"
        self.buffer.set_text(text)
        self.fix_btn.set_sensitive(verdict in ("warn", "fail"))
        self.fix_buffer.set_text("")
        self.fix_label.set_text("")


import threading  # noqa: E402
