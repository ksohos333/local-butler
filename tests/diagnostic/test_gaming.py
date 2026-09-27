"""Tests for the game compatibility checker and ProtonDB seed."""

import sys
import types
from pathlib import Path


def _stub_gi():
    """Provide a minimal gi/Gtk stub so gaming.py can be imported headless."""

    class DummyModule(types.ModuleType):
        def __getattr__(self, name):
            return object

    gi = types.ModuleType("gi")
    gi.require_version = lambda *_a, **_k: None
    repo = DummyModule("gi.repository")
    repo.Gtk = DummyModule("gi.repository.Gtk")
    repo.GLib = DummyModule("gi.repository.GLib")
    gi.repository = repo

    sys.modules["gi"] = gi
    sys.modules["gi.repository"] = repo
    sys.modules["gi.repository.Gtk"] = repo.Gtk
    sys.modules["gi.repository.GLib"] = repo.GLib


_stub_gi()

from local_butler.apps.diagnostic import gaming, protondb


def _checker(data, app_id=123):
    c = gaming.GameCompatibilityChecker(app_id)
    c.data = data
    return c


def test_protondb_lookup_hoi4():
    data = protondb.lookup(394360)
    assert data is not None
    assert data["tier"] == "gold"
    assert data["reports"] > 0


def test_protondb_lookup_unknown():
    assert protondb.lookup(999999999) is None


def test_windows_game_with_protondb_gold_and_proton(monkeypatch):
    monkeypatch.setattr(gaming, "_find_proton", lambda: "/fake/steamapps/common/Proton 9.0")
    monkeypatch.setattr(
        gaming.protondb,
        "lookup",
        lambda _app_id: {"tier": "gold", "reports": 100},
    )
    data = {
        "name": "Fake Windows Game",
        "pc_requirements": {
            "minimum": (
                "<strong>Minimum:</strong><ul>"
                "<li><strong>OS:</strong> Windows® 10<br></li>"
                "<li><strong>Memory:</strong> 4 GB RAM<br></li>"
                "</ul>"
            ),
        },
    }
    result = _checker(data).check()
    assert result["verdict"] == "ok"
    assert "GOLD" in result["detail"]
    assert result["proton_version"] == "Proton 9.0"


def test_windows_game_with_protondb_borked(monkeypatch):
    monkeypatch.setattr(gaming, "_find_proton", lambda: "/fake/steamapps/common/Proton 9.0")
    monkeypatch.setattr(
        gaming.protondb,
        "lookup",
        lambda _app_id: {"tier": "borked", "reports": 50},
    )
    data = {
        "name": "Anti-Cheat Game",
        "pc_requirements": {
            "minimum": (
                "<strong>Minimum:</strong><ul>"
                "<li><strong>OS:</strong> Windows® 10<br></li>"
                "<li><strong>Memory:</strong> 4 GB RAM<br></li>"
                "</ul>"
            ),
        },
    }
    result = _checker(data).check()
    assert result["verdict"] == "fail"
    assert "BORKED" in result["detail"]


def test_native_linux_game(monkeypatch):
    monkeypatch.setattr(gaming, "_find_proton", lambda: None)
    data = {
        "name": "Native Linux Game",
        "pc_requirements": {
            "minimum": (
                "<strong>Minimum:</strong><ul>"
                "<li><strong>OS:</strong> SteamOS / Linux<br></li>"
                "<li><strong>Memory:</strong> 4 GB RAM<br></li>"
                "</ul>"
            ),
        },
    }
    result = _checker(data).check()
    assert result["verdict"] == "ok"
    assert "Native Linux" in result["detail"]
    assert result["minimum"]["os_required"] == "linux"


def test_parse_req_keeps_os_windows_for_backwards_compat():
    checker = gaming.GameCompatibilityChecker(1)
    parsed = checker._parse_req(
        "<strong>Minimum:</strong><ul>"
        "<li><strong>OS:</strong> Windows® 10<br></li>"
        "<li><strong>Memory:</strong> 4 GB RAM<br></li>"
        "</ul>"
    )
    assert parsed["os_windows"] is True
    assert parsed["os_required"] == "windows"
