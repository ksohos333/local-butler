"""Tests for the agent memory store.

Uses a throwaway SQLite file rather than the real butler database: these tests
write and delete rows, and that has no business touching production data.
"""

from __future__ import annotations

import pytest

from local_butler.apps.geonews.memory import AgentEvent, AgentNote, MemoryStore, utcnow


@pytest.fixture
def store(tmp_path):
    return MemoryStore(db_url=f"sqlite:///{tmp_path/'agent.db'}")


def test_tables_are_created(store):
    from sqlalchemy import inspect

    tables = set(inspect(store.engine).get_table_names())
    assert {"geonews_agent_notes", "geonews_agent_events"} <= tables


def test_note_roundtrip(store):
    saved = store.add_note("Watching the EU migration package", topic="Migration")

    assert saved["id"]
    assert saved["topic"] == "migration"  # normalised
    assert saved["created_at"]

    found = store.search_notes("migration")
    assert [n["note"] for n in found] == ["Watching the EU migration package"]


def test_search_matches_note_body_not_just_topic(store):
    store.add_note("Interest rate decision matters", topic="rates")

    assert store.search_notes("interest rate")
    # Only topic matches, so the body search must still find it.
    assert store.search_notes("rates")[0]["topic"] == "rates"


def test_search_returns_empty_on_no_match(store):
    store.add_note("Watching the EU migration package")

    assert store.search_notes("cryptocurrency") == []


def test_empty_query_returns_most_recent(store):
    first = store.add_note("first")
    store.add_note("second")

    recent = store.list_notes()
    assert recent[0]["note"] == "second"
    assert first["id"] != recent[0]["id"]


def test_delete_note(store):
    saved = store.add_note("temporary")

    assert store.delete_note(saved["id"]) is True
    assert store.search_notes("temporary") == []
    assert store.delete_note(saved["id"]) is False


def test_event_is_recorded_and_filtered(store):
    store.add_event(summary="Ceasefire talks stalled", topic="lebanon")
    store.add_event(summary="Rate hold announced", topic="rates")

    lebanon = store.recent_events("lebanon")
    assert [e["summary"] for e in lebanon] == ["Ceasefire talks stalled"]

    assert len(store.recent_events()) == 2


def test_events_outside_window_are_excluded(store):
    """Backdated rows must fall out of a narrow window but survive a wide one."""
    import datetime as dt

    session = store.Session()
    try:
        session.add(
            AgentEvent(
                summary="old news",
                topic="lebanon",
                kind="chat",
                # Four months back: outside an hour, inside a year.
                created_at=utcnow() - dt.timedelta(days=120),
            )
        )
        session.commit()
    finally:
        session.close()

    assert store.recent_events(hours=1) == []
    assert len(store.recent_events(hours=24 * 365)) == 1
    # Keyword filtering is applied on top of the window.
    assert store.recent_events("lebanon", hours=24 * 365)[0]["summary"] == "old news"


def test_store_is_shared_by_default(store, monkeypatch):
    from local_butler.apps.geonews import memory as memory_module

    monkeypatch.setattr(memory_module, "_STORE", store)
    monkeypatch.setattr(memory_module, "get_store", lambda: store)

    assert memory_module.get_store() is store