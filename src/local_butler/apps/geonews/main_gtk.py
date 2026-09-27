#!/usr/bin/env python3
"""Geonews native GTK3 desktop shell.

Loads the Geonews web UI inside a WebKit2 view and keeps the FastAPI backend
running as a local subprocess. This follows the Local Butler native desktop
pattern while reusing the existing HTML/CSS/JS frontend.

System-level GTK/WebKit bindings are required and are intentionally *not*
listed in the monorepo pyproject.toml (see docs/ADR-001-layout.md). Install
with:

    sudo dnf install python3-gobject gtk3 webkit2gtk3

If system Python (``python3``) is missing PyGObject, create a small dedicated
venv for this launcher and point the Exec line in the .desktop file to it.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

try:
    import gi
except ImportError as exc:
    print(
        "Geonews GTK shell requires PyGObject. Install it with:\n"
        "  sudo dnf install python3-gobject gtk3 webkit2gtk3"
    )
    raise SystemExit(1) from exc

gi.require_version("Gtk", "3.0")

# WebKit2 versions vary by distribution. Try 4.0 first, then 4.1.
for _webkit_version in ("4.0", "4.1"):
    try:
        gi.require_version("WebKit2", _webkit_version)
        break
    except ValueError:
        continue
else:
    print("WebKit2 GTK bindings not found. Install webkit2gtk3.")
    raise SystemExit(1)

from gi.repository import GLib, Gtk, WebKit2  # noqa: E402

logger = logging.getLogger("geonews-gtk")
logging.basicConfig(level=logging.INFO)

REPO_ROOT = Path(__file__).resolve().parents[4]
HOST = "127.0.0.1"
PORT = 8788
BASE_URL = f"http://{HOST}:{PORT}"
HOME_URL = BASE_URL + "/"
STARTUP_TIMEOUT_SECONDS = 60


def uv_bin() -> str:
    uv = shutil.which("uv")
    if not uv:
        print("uv is not on PATH. Geonews cannot start its backend.")
        raise SystemExit(1)
    return uv


class GeonewsApplication(Gtk.Application):
    def __init__(self) -> None:
        super().__init__(application_id="com.localbutler.Geonews")
        self._backend_proc: subprocess.Popen | None = None
        self._owns_backend = False
        self._window: GeonewsWindow | None = None
        self._startup_attempts = 0

    def do_activate(self) -> None:
        if self._window:
            self._window.present()
            return

        self._ensure_backend()
        self._window = GeonewsWindow(application=self)
        self._window.show_all()
        self._window.present()

    def do_shutdown(self) -> None:
        # Gtk.Application exposes do_shutdown as a GIO vfunc and has no
        # Python-level parent to chain up to; super().do_shutdown() raised
        # TypeError and aborted the vfunc.
        self._stop_backend()

    def _ensure_backend(self) -> None:
        """Attach to the running backend, starting one only if none answers.

        geonews.service owns port 8788 and the database migrations. Spawning a
        second uvicorn here only raced it for the port, so prefer the service
        and treat a local spawn as an explicit fallback.
        """
        if self._backend_is_up():
            logger.info("Attaching to the backend already serving %s", BASE_URL)
            return

        logger.warning("No backend on %s:%s; starting a local one", HOST, PORT)
        self._backend_proc = subprocess.Popen(
            [
                uv_bin(),
                "run",
                "uvicorn",
                "local_butler.apps.geonews.main:app",
                "--host",
                HOST,
                "--port",
                str(PORT),
                "--no-access-log",
            ],
            cwd=REPO_ROOT,
            env=os.environ.copy(),
        )
        self._owns_backend = True
        logger.info("Local backend PID %s", self._backend_proc.pid)
        self._wait_for_backend()

    @staticmethod
    def _backend_is_up() -> bool:
        try:
            import urllib.request

            with urllib.request.urlopen(BASE_URL, timeout=2):
                return True
        except Exception:
            return False

    def _wait_for_backend(self) -> None:
        self._startup_attempts += 1
        proc = self._backend_proc

        if proc is None:
            return

        if proc.poll() is not None:
            print("Geonews backend exited unexpectedly. Check the logs.")
            self.quit()
            return

        if self._startup_attempts >= STARTUP_TIMEOUT_SECONDS:
            print(f"Geonews backend did not respond within {STARTUP_TIMEOUT_SECONDS}s.")
            self.quit()
            return

        if self._backend_is_up():
            logger.info("Backend is ready")
            return

        # Slow-starting first import can take a few seconds.
        GLib.timeout_add_seconds(1, self._wait_for_backend)

    def _stop_backend(self) -> None:
        # Never stop the systemd-owned backend; only a local fallback we started.
        if not self._owns_backend:
            return
        if self._backend_proc and self._backend_proc.poll() is None:
            logger.info("Stopping locally started backend")
            self._backend_proc.terminate()
            try:
                self._backend_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._backend_proc.kill()
        self._owns_backend = False


class GeonewsWindow(Gtk.ApplicationWindow):
    def __init__(self, application: GeonewsApplication) -> None:
        super().__init__(title="Geonews", application=application)
        self.set_default_size(1400, 900)
        self.set_position(Gtk.WindowPosition.CENTER)
        self.set_icon_name("applications-internet")

        header = Gtk.HeaderBar(title="Geonews", show_close_button=True)
        self.set_titlebar(header)

        reload_btn = Gtk.Button.new_from_icon_name(
            "view-refresh-symbolic", Gtk.IconSize.BUTTON
        )
        reload_btn.set_tooltip_text("Reload Geonews")
        reload_btn.connect("clicked", self._on_reload)
        header.pack_end(reload_btn)

        self.webview = WebKit2.WebView()
        try:
            settings = self.webview.get_settings()
            settings.set_enable_developer_extras(True)
        except Exception:
            pass

        self.add(self.webview)
        # Load unconditionally so the attach and fallback paths both render.
        self.webview.load_uri(HOME_URL)

    def load_uri(self, uri: str) -> None:
        self.webview.load_uri(uri)

    def _on_reload(self, _button: Gtk.Button) -> None:
        self.webview.reload()


def main(argv: list[str] | None = None) -> int:
    app = GeonewsApplication()
    return app.run(argv)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
