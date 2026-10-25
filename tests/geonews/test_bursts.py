"""Tests for burst and momentum detection.

The failure modes these guard against are the ones that would make the agent
say something false: flagging a two-article day as a trend, and reporting the
ingestion schedule as though it were news.
"""

from __future__ import annotations

import pytest

from local_butler.apps.geonews.analytics.bursts import (
    _proper_terms,
    analysis_window,
    build_signals,
    run_trend_detection,
    score_series,
)
from local_butler.apps.geonews.analytics.models import AnalyticsStore


@pytest.fixture
def store(tmp_path):
    return AnalyticsStore(db_url=f"sqlite:///{tmp_path/'analytics.db'}")


def make_row(i, source, day, *, event_type="political", severity=5,
             location=None, title="Headline about a thing"):
    import datetime as dt

    return {
        "id": i,
        "source_key": source,
        "title": title,
        "published_at": dt.datetime.fromisoformat(f"{day} 09:00:00"),
        "event_type": event_type,
        "severity": severity,
        "location_name": location,
        "corroboration_count": 1,
    }


# -- score_series --------------------------------------------------------


def test_small_absolute_jumps_are_not_bursts():
    """0 -> 2 articles doubles, it does not trend."""
    totals = [100] * 6
    records, _ = score_series([0, 0, 0, 0, 0, 2], totals)

    assert records[-1]["is_burst"] is False


def test_a_large_jump_on_a_large_day_is_a_burst():
    totals = [100] * 6
    records, _ = score_series([10, 10, 10, 10, 10, 60], totals)

    assert records[-1]["is_burst"] is True
    assert records[-1]["burst_score"] > 3


def test_scoring_is_on_share_not_raw_volume():
    """Same proportion of attention, wildly different day sizes, same verdict.

    Day 6 carries five times as many articles, but the subject owns the same
    share of each day -- so it is not trending. Scoring on raw counts would
    call this a 5x burst, which is really just a bigger news day.
    """
    small_day = [10, 10, 10, 10, 10, 20]
    big_day = [50, 50, 50, 50, 50, 100]

    small_totals = [100] * 6
    big_totals = [500] * 6

    small, _ = score_series(small_day, small_totals)
    big, _ = score_series(big_day, big_totals)

    assert small[-1]["is_burst"] is False
    assert big[-1]["is_burst"] is False


def test_share_burst_is_detected_even_when_volume_dips():
    """The point of using share: volume can fall while attention concentrates."""
    # Subject holds 10% of the day for a week, then 60% of a smaller day.
    volumes = [10, 10, 10, 10, 10, 6]
    totals = [100, 100, 100, 100, 100, 10]

    records, _ = score_series(volumes, totals)

    assert volumes[-1] < volumes[0]  # fewer articles than before...
    assert records[-1]["share"] > 0.5  # ...but far more of the day's attention
    assert records[-1]["is_burst"] is True


def test_no_burst_claims_before_enough_history():
    records, gaps = score_series([5, 50, 5, 5, 5], [100] * 5)

    assert all(r["is_burst"] is False for r in records)
    assert len(gaps) == 4


def test_empty_history_leaves_no_baseline_and_no_false_claim():
    records, gaps = score_series([], [])

    assert records == []
    assert gaps == []


def test_momentum_windows_do_not_overlap():
    """A subject that doubled must still show momentum on a short history.

    Regression guard: fixed slices over a 5-day history shared a day between
    the recent and prior windows, halving the measured change and reporting a
    strong trend as flat. Also pins that the current day counts -- excluding it
    would report every trend's first day as 0.
    """
    volumes = [5, 5, 5, 5, 5, 20]
    totals = [100] * 6

    records, _ = score_series(volumes, totals)

    # Recent shares .05,.05,.20 (mean .10) vs prior .05,.05,.05 (mean .05).
    assert records[-1]["momentum"] == pytest.approx(5.0)


def test_momentum_captures_a_rising_trend_without_a_spike():
    volumes = [5, 5, 5, 5, 5, 10, 20]
    totals = [100] * 7

    records, _ = score_series(volumes, totals)

    assert records[-1]["momentum"] > 0


# -- analysis_window -----------------------------------------------------


def test_analysis_window_drops_the_ingestion_ramp():
    """Sparse early days must not become the baseline for everything else."""
    rows = []
    i = 0
    # A ramp: 5 articles/day for a month, then 400/day for ten days.
    for day in range(1, 31):
        for n in range(5):
            i += 1
            rows.append(make_row(i, "reuters", f"2026-08-{day:02d}"))
    for day in range(1, 11):
        for n in range(400):
            i += 1
            rows.append(make_row(i, "reuters", f"2026-09-{day:02d}"))

    window = analysis_window(rows)

    assert window, "a steady window should be found"
    assert min(window).startswith("2026-09")
    assert not any(d.startswith("2026-08") for d in window)


def test_analysis_window_needs_noisy_data_to_exist():
    assert analysis_window([]) == set()


# -- topic quality -------------------------------------------------------


def test_proper_terms_prefers_capitalised_names():
    title = "Russia launches drone attack on Kyiv amid talks in Geneva"

    proper = _proper_terms(title)

    assert "kyiv" in proper
    assert "geneva" in proper
    assert "amid" not in proper
    assert "talks" not in proper


def test_proper_terms_ignores_the_sentence_initial_word():
    """'Russia' is first here, so it is not evidence of a proper noun."""
    assert "russia" not in _proper_terms("Russia launches drone attack on Kyiv")


def test_generic_words_do_not_become_topics():
    """Topic terms must be rare *and* name-like, not just frequent.

    The proportions matter: a term in a large share of articles is part of the
    vocabulary, not a topic. Matches the real corpus, where a place name sits
    around 0.3% of articles.
    """
    rows = []
    i = 0
    for day in range(1, 9):
        # A genuine but rare entity.
        i += 1
        rows.append(
            make_row(i, "bbc", f"2026-09-{day:02d}",
                     title="Talks continue in Taiz between the two sides")
        )
        # Ubiquitous newsroom vocabulary, present in nearly every article.
        for _ in range(44):
            i += 1
            rows.append(
                make_row(i, "reuters", f"2026-09-{day:02d}",
                         title="Government may issue new prices for the region")
            )

    subjects = {s["subject"] for s in build_signals(rows)}

    assert "topic:taiz" in subjects, sorted(s for s in subjects if "topic" in s)
    assert "topic:prices" not in subjects
    assert "topic:region" not in subjects
    assert "topic:government" not in subjects
    assert "topic:talks" not in subjects


def test_build_signals_handles_empty_input():
    assert build_signals([]) == []


# -- persistence ---------------------------------------------------------


def test_run_trend_detection_persists(store):
    rows = []
    i = 0
    for day in range(1, 8):
        for _ in range(50):
            i += 1
            rows.append(make_row(i, "reuters", f"2026-09-{day:02d}",
                                 event_type="conflict"))

    result = run_trend_detection(store, rows=rows)

    assert result["signals"] > 0
    assert store.stats()["signals"] == result["signals"]


def test_run_trend_detection_is_idempotent(store):
    rows = []
    i = 0
    for day in range(1, 8):
        for _ in range(50):
            i += 1
            rows.append(make_row(i, "reuters", f"2026-09-{day:02d}"))

    run_trend_detection(store, rows=rows)
    first = store.stats()["signals"]
    run_trend_detection(store, rows=rows)

    assert store.stats()["signals"] == first