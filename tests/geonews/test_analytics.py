"""Tests for event clustering and corroboration scoring.

The claims these tests protect are the ones the agent will make out loud: "N
independent outlets reported this." So the assertions are about *source
diversity*, not just "these rows got grouped somehow".
"""

from __future__ import annotations

import datetime as dt

import pytest

from local_butler.apps.geonews.analytics.cluster import (
    cluster_articles,
    run_corroboration,
    tokenize,
)
from local_butler.apps.geonews.analytics.models import AnalyticsStore


def make_row(i, source, title, summary="", day="2026-10-01",
             event_type="political", severity=5, location=None):
    return {
        "id": i,
        "source_key": source,
        "title": title,
        "summary": summary,
        "published_at": dt.datetime.fromisoformat(f"{day} 12:00:00"),
        "event_type": event_type,
        "severity": severity,
        "location_name": location,
    }


DRONE_TITLE = "Russia launches massive drone attack on Kyiv"


def drone_event_row(i, source, summary):
    return make_row(
        i, source, DRONE_TITLE, summary,
        event_type="conflict", severity=7,
    )


@pytest.fixture
def store(tmp_path):
    return AnalyticsStore(db_url=f"sqlite:///{tmp_path/'analytics.db'}")


def test_tokenize_drops_stopwords_and_boilerplate():
    tokens = tokenize("BREAKING: Report says the drone attack hit Kyiv - latest update")
    assert "drone" in tokens and "attack" in tokens and "kyiv" in tokens
    assert "breaking" not in tokens
    assert "latest" not in tokens
    assert "says" not in tokens


def test_tokenize_empty():
    assert tokenize("") == []
    assert tokenize("!!! ???") == []


def test_same_event_from_many_outlets_forms_one_corroborated_cluster():
    rows = [
        drone_event_row(1, "reuters", "Kyiv hit by overnight drone strikes"),
        drone_event_row(2, "bbc", "Hundreds of drones targeted Ukraine overnight"),
        drone_event_row(3, "npr", "Overnight drone attack on Kyiv"),
        drone_event_row(4, "aljazeera", "Ukraine reports casualties in drone strike"),
    ]

    clusters, corroboration = cluster_articles(rows, threshold=0.28)

    assert len(clusters) == 1
    cluster = clusters[0]
    assert cluster["source_count"] == 4
    assert sorted(cluster["sources"]) == ["aljazeera", "bbc", "npr", "reuters"]
    # Every member of a corroborated event is scored 4, not 1.
    assert set(corroboration.values()) == {4}


def test_repetition_by_one_outlet_is_not_corroboration():
    """The whole point: one outlet repeating itself is not confirmation."""
    rows = [
        drone_event_row(1, "sputnik", "Kyiv under drone attack"),
        drone_event_row(2, "sputnik", "Drone attack on Kyiv overnight"),
        drone_event_row(3, "sputnik", "More drones strike Kyiv"),
    ]

    clusters, corroboration = cluster_articles(rows, threshold=0.28)

    assert len(clusters) == 1
    assert clusters[0]["article_count"] == 3
    assert clusters[0]["source_count"] == 1
    assert set(corroboration.values()) == {1}


def test_unrelated_events_are_not_merged():
    rows = [
        drone_event_row(1, "reuters", "Kyiv hit by overnight drone strikes"),
        make_row(2, "reuters", "Federal Reserve holds interest rates steady",
                 "Rates unchanged at 4.25%", event_type="economic"),
        make_row(3, "bbc", "Japan unveils new defence budget",
                 "Tokyo raises military spending", event_type="political"),
    ]

    clusters, corroboration = cluster_articles(rows)

    assert len(clusters) == 3
    assert all(c["source_count"] == 1 for c in clusters)
    assert set(corroboration.values()) == {1}


def test_events_outside_the_window_are_not_linked():
    rows = [
        drone_event_row(1, "reuters", "Overnight drone attack on Kyiv"),
        make_row(2, "bbc", DRONE_TITLE, "Later wave of drones over Ukraine",
                 day="2026-10-20", event_type="conflict", severity=7),
    ]

    clusters, _ = cluster_articles(rows, threshold=0.28, window_days=3)

    assert len(clusters) == 2
    assert all(c["source_count"] == 1 for c in clusters)


def test_cluster_carries_provenance_fields():
    rows = [
        drone_event_row(1, "reuters", "Kyiv hit by overnight drone strikes"),
        drone_event_row(2, "bbc", "Hundreds of drones targeted Ukraine overnight"),
    ]

    clusters, _ = cluster_articles(rows, threshold=0.28)
    cluster = clusters[0]

    assert len(cluster["signature"]) == 32
    assert cluster["label"]
    assert cluster["first_seen"] == dt.datetime(2026, 10, 1, 12, 0)
    assert cluster["last_seen"] == dt.datetime(2026, 10, 1, 12, 0)
    assert cluster["max_severity"] == 7
    assert cluster["event_type"] == "conflict"


def test_peak_day_tracks_the_busiest_day():
    rows = [
        drone_event_row(1, "reuters", "Overnight drone attack on Kyiv"),
        drone_event_row(2, "bbc", "Overnight drone attack on Kyiv", ),
        make_row(3, "npr", DRONE_TITLE, "Overnight drone attack on Kyiv",
                 day="2026-10-02", event_type="conflict", severity=7),
    ]

    clusters, _ = cluster_articles(rows, threshold=0.28)
    cluster = clusters[0]

    assert cluster["peak_volume"] == 2
    assert cluster["peak_day"] in {"2026-10-01", "2026-10-02"}


def test_signatures_are_unique_within_a_batch():
    """Disjoint clusters keyed on the same top terms must not collide."""
    rows = [
        make_row(1, "reuters", "Japan unveils new defence budget", "Tokyo"),
        make_row(2, "bbc", "Japan unveils new defence budget", "Tokyo again"),
        make_row(3, "npr", "Japan unveils new defence budget", "Tokyo once more"),
        make_row(4, "aljazeera", "Japan unveils new defence budget", "Tokyo again"),
    ]

    clusters, _ = cluster_articles(rows, threshold=0.28)

    signatures = [c["signature"] for c in clusters]
    assert len(signatures) == len(set(signatures))


def test_empty_input_is_handled():
    assert cluster_articles([]) == ([], {})


def test_untokenisable_rows_are_skipped():
    rows = [make_row(1, "reuters", "!!!", ""), make_row(2, "bbc", "???", "...")]

    assert cluster_articles(rows) == ([], {})


def test_run_corroboration_persists_and_is_idempotent(store):
    rows = [
        drone_event_row(1, "reuters", "Kyiv hit by overnight drone strikes"),
        drone_event_row(2, "bbc", "Overnight drone attack on Kyiv"),
    ]

    first = run_corroboration(store, threshold=0.28, rows=rows)
    second = run_corroboration(store, threshold=0.28, rows=rows)

    assert first["articles"] == 2
    assert first["clusters"] == 1
    assert first["corroborated_clusters"] == 1
    assert first == second
    assert store.stats()["corroborated_clusters"] == 1

    # Rebuilding replaces, never duplicates.
    assert store.stats()["clusters"] == 1


def test_run_corroboration_with_no_rows_is_a_noop(store):
    assert run_corroboration(store, rows=[]) == {
        "articles": 0,
        "clusters": 0,
        "corroborated_clusters": 0,
        "articles_scored": 0,
    }
    assert store.stats()["clusters"] == 0