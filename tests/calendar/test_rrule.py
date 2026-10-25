"""Tests for RRULE validation and UNTIL handling.

``events`` and ``recurrence`` import no GTK, so these run on the project's own
interpreter even where PyGObject is unavailable.
"""

import datetime as dt

import pytest

from local_butler.apps.calendar.events import Event, EventError, parse_until, validate_rrule
from local_butler.apps.calendar.recurrence import (
    instance_span,
    occurrences_in_range,
    range_for,
    recurring_occurrences,
)

ZONE = dt.timezone(dt.timedelta(hours=-7))


def make_event(summary="Dentist", **kwargs):
    defaults = dict(
        start=dt.datetime(2026, 10, 6, 15, 0),
        end=dt.datetime(2026, 10, 6, 16, 0),
        tz=ZONE,
    )
    defaults.update(kwargs)
    return Event(summary=summary, **defaults)


# -- validate_rrule -------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("FREQ=WEEKLY", "FREQ=WEEKLY"),
        ("RRULE:FREQ=WEEKLY;UNTIL=20261026", "FREQ=WEEKLY;UNTIL=20261026"),
        ("FREQ=DAILY;UNTIL=20261010T150000Z", "FREQ=DAILY;UNTIL=20261010T150000Z"),
        ("freq=weekly;until=20261026", "FREQ=WEEKLY;UNTIL=20261026"),
    ],
)
def test_validate_rrule_accepts_and_normalizes_until(raw, expected):
    assert validate_rrule(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "FREQ=WEEKLY;UNTIL=tomorrow",
        "FREQ=WEEKLY;UNTIL=2026-10-26",
        "FREQ=WEEKLY;UNTIL=202610",
    ],
)
def test_validate_rrule_rejects_a_malformed_until(raw):
    with pytest.raises(EventError):
        validate_rrule(raw)


def test_an_event_cannot_be_built_with_a_malformed_until():
    with pytest.raises(EventError):
        make_event(rrule="FREQ=WEEKLY;UNTIL=nonsense")


# -- parse_until ----------------------------------------------------------


def test_parse_until_treats_a_date_value_as_the_whole_day():
    parsed = parse_until("20261026", ZONE)

    assert parsed == dt.datetime(2026, 10, 26, 23, 59, 59, 999999)


def test_parse_until_converts_a_utc_value_into_the_event_zone():
    # 22:00Z is 15:00 in this fixed-offset zone.
    assert parse_until("20261006T220000Z", ZONE) == dt.datetime(2026, 10, 6, 15, 0)


@pytest.mark.parametrize("raw", ["", "   ", "not-a-date", "202610", None])
def test_parse_until_rejects_nonsense_by_returning_none(raw):
    assert parse_until(raw, ZONE) is None


def test_parse_until_cannot_convert_a_utc_value_without_a_zone():
    assert parse_until("20261006T220000Z", None) is None


# -- expansion -------------------------------------------------------------


def test_expansion_is_unchanged_when_no_until_is_given():
    assert recurring_occurrences(make_event(rrule="FREQ=DAILY"), horizon_days=3) == [
        dt.date(2026, 10, 6),
        dt.date(2026, 10, 7),
        dt.date(2026, 10, 8),
        dt.date(2026, 10, 9),
    ]


@pytest.mark.parametrize(
    "rrule,expected",
    [
        ("FREQ=DAILY;UNTIL=20261008", ["2026-10-06", "2026-10-07", "2026-10-08"]),
        ("FREQ=DAILY;UNTIL=20261005", []),
        ("FREQ=WEEKLY;UNTIL=20261020", ["2026-10-06", "2026-10-13", "2026-10-20"]),
        ("FREQ=WEEKLY;UNTIL=20261019", ["2026-10-06", "2026-10-13"]),
        ("FREQ=MONTHLY;UNTIL=20261106", ["2026-10-06", "2026-11-06"]),
        # The bound is inclusive, so the 2027 mark on the 6th is still kept.
        ("FREQ=YEARLY;UNTIL=20271006", ["2027-10-06"]),
        ("FREQ=YEARLY;UNTIL=20261006", []),
    ],
)
def test_until_caps_every_supported_frequency(rrule, expected):
    results = recurring_occurrences(make_event(rrule=rrule), horizon_days=400)

    assert [d.isoformat() for d in results] == expected


def test_a_yearly_rule_keeps_a_mark_falling_exactly_on_its_bound():
    # YEARLY never marks DTSTART itself, so the bound decides the only mark.
    results = recurring_occurrences(make_event(rrule="FREQ=YEARLY;UNTIL=20271006"), horizon_days=400)

    assert [d.isoformat() for d in results] == ["2027-10-06"]


def test_until_never_yields_a_mark_past_its_bound():
    results = recurring_occurrences(make_event(rrule="FREQ=DAILY;UNTIL=20261010"), horizon_days=400)

    assert results
    assert max(results) == dt.date(2026, 10, 10)


def test_count_and_until_apply_whichever_binds_first():
    bounded_by_until = recurring_occurrences(
        make_event(rrule="FREQ=WEEKLY;COUNT=10;UNTIL=20261020"),
        horizon_days=400,
    )
    bounded_by_count = recurring_occurrences(
        make_event(rrule="FREQ=WEEKLY;COUNT=2;UNTIL=20261231"),
        horizon_days=400,
    )

    assert len(bounded_by_until) == 3
    assert len(bounded_by_count) == 2


def test_an_expired_until_leaves_no_marks_on_the_grid():
    assert recurring_occurrences(make_event(rrule="FREQ=DAILY;UNTIL=20260101")) == []
    assert recurring_occurrences(make_event(rrule="FREQ=WEEKLY;UNTIL=20260101")) == []
    assert recurring_occurrences(make_event(rrule="FREQ=MONTHLY;UNTIL=20260101")) == []


def test_an_event_with_no_rule_has_no_occurrences():
    assert recurring_occurrences(make_event()) == []


def test_an_unexpandable_frequency_falls_back_to_the_start_date():
    broken = make_event()
    object.__setattr__(broken, "rrule", "FREQ=NONSENSE")

    assert recurring_occurrences(broken) == [broken.start.date()]


def test_an_all_day_event_is_compared_by_date_not_by_clock_time():
    all_day = make_event(
        all_day=True,
        start=dt.datetime(2026, 10, 6, 0, 0),
        end=dt.datetime(2026, 10, 7, 0, 0),
        rrule="FREQ=DAILY;UNTIL=20261008",
    )

    assert [d.isoformat() for d in recurring_occurrences(all_day)] == [
        "2026-10-06",
        "2026-10-07",
        "2026-10-08",
    ]

# -- agenda ranges ---------------------------------------------------------


def agenda_event(start="2026-10-06T15:00", summary="Standup", **kwargs):
    """Takes the start as its first argument; one-hour long unless told otherwise.

    ``make_event`` pins ``end`` to a fixed clock, which would silently turn every
    moved start into a multi-day event once the date changed.
    """
    begins = dt.datetime.fromisoformat(start)
    kwargs.setdefault("end", begins + dt.timedelta(hours=1))
    return make_event(summary=summary, start=begins, **kwargs)


class TestRangeFor:
    def test_day_is_the_anchor_alone(self):
        assert range_for(dt.date(2026, 10, 6), "day") == (
            dt.date(2026, 10, 6),
            dt.date(2026, 10, 6),
        )

    def test_week_runs_monday_to_sunday(self):
        first, last = range_for(dt.date(2026, 10, 6), "week")
        assert first == dt.date(2026, 10, 5)  # a Tuesday
        assert last == dt.date(2026, 10, 11)
        assert first.weekday() == 0 and last.weekday() == 6

    def test_month_spans_its_full_length(self):
        assert range_for(dt.date(2026, 10, 6), "month") == (
            dt.date(2026, 10, 1),
            dt.date(2026, 10, 31),
        )

    def test_february_in_a_leap_year(self):
        assert range_for(dt.date(2028, 2, 10), "month") == (
            dt.date(2028, 2, 1),
            dt.date(2028, 2, 29),
        )

    def test_unknown_scope_is_rejected(self):
        with pytest.raises(ValueError):
            range_for(dt.date(2026, 10, 6), "decade")


class TestOccurrencesInRange:
    def test_a_one_off_event_lands_only_on_its_own_day(self):
        event = agenda_event("2026-10-06T15:00")
        rows = occurrences_in_range([event], dt.date(2026, 10, 1), dt.date(2026, 10, 31))
        assert [(day, item.summary) for item, day in rows] == [(dt.date(2026, 10, 6), "Standup")]

    def test_events_outside_the_window_are_dropped(self):
        event = agenda_event("2026-09-01T15:00")
        assert occurrences_in_range([event], dt.date(2026, 10, 1), dt.date(2026, 10, 31)) == []

    def test_a_weekly_rule_repeats_inside_the_window(self):
        event = agenda_event("2026-10-06T15:00", rrule="FREQ=WEEKLY")
        first, last = dt.date(2026, 10, 1), dt.date(2026, 10, 31)
        days = [day for _item, day in occurrences_in_range([event], first, last)]
        assert days == [dt.date(2026, 10, 6), dt.date(2026, 10, 13), dt.date(2026, 10, 20), dt.date(2026, 10, 27)]

    def test_a_rule_that_started_long_ago_still_reaches_the_window(self):
        # A daily rule from years back must not be capped out before the window
        # the user is actually looking at.
        event = agenda_event("2019-01-01T09:00", rrule="FREQ=DAILY")
        rows = occurrences_in_range([event], dt.date(2026, 10, 6), dt.date(2026, 10, 8))
        assert [day for _i, day in rows] == [
            dt.date(2026, 10, 6),
            dt.date(2026, 10, 7),
            dt.date(2026, 10, 8),
        ]

    def test_count_is_spent_from_the_start_not_the_window(self):
        # COUNT=3 from a 2026-10-01 daily start means the three days in early
        # October; a window starting on the 6th must show nothing.
        event = agenda_event("2026-10-01T09:00", rrule="FREQ=DAILY;COUNT=3")
        assert occurrences_in_range([event], dt.date(2026, 10, 6), dt.date(2026, 10, 31)) == []

    def test_count_still_shows_its_last_occurrence(self):
        event = agenda_event("2026-10-01T09:00", rrule="FREQ=DAILY;COUNT=3")
        rows = occurrences_in_range([event], dt.date(2026, 10, 1), dt.date(2026, 10, 31))
        assert [day for _i, day in rows] == [
            dt.date(2026, 10, 1),
            dt.date(2026, 10, 2),
            dt.date(2026, 10, 3),
        ]

    def test_until_is_inclusive_at_the_boundary(self):
        event = agenda_event("2026-10-06T15:00", rrule="FREQ=DAILY;UNTIL=20261008")
        rows = occurrences_in_range([event], dt.date(2026, 10, 6), dt.date(2026, 10, 31))
        assert [day for _i, day in rows] == [
            dt.date(2026, 10, 6),
            dt.date(2026, 10, 7),
            dt.date(2026, 10, 8),
        ]

    def test_a_multi_day_event_appears_once_not_once_per_day(self):
        event = agenda_event("2026-10-06T09:00", end=dt.datetime(2026, 10, 9, 17, 0))
        rows = occurrences_in_range([event], dt.date(2026, 10, 1), dt.date(2026, 10, 31))
        assert [day for _i, day in rows] == [dt.date(2026, 10, 6)]

    def test_a_multi_day_event_that_started_earlier_clamps_to_the_window_start(self):
        # A trip begun on the 2nd and still running on the 5th should be visible
        # in a window that opens on the 5th.
        event = agenda_event("2026-10-02T09:00", end=dt.datetime(2026, 10, 9, 17, 0))
        rows = occurrences_in_range([event], dt.date(2026, 10, 5), dt.date(2026, 10, 11))
        assert [day for _i, day in rows] == [dt.date(2026, 10, 5)]

    def test_a_multi_day_event_that_finished_earlier_is_not_shown(self):
        event = agenda_event("2026-10-02T09:00", end=dt.datetime(2026, 10, 4, 17, 0))
        assert occurrences_in_range([event], dt.date(2026, 10, 5), dt.date(2026, 10, 11)) == []

    def test_rows_are_sorted_by_day_then_time(self):
        late = agenda_event("2026-10-06T17:00", summary="Gym")
        early = agenda_event("2026-10-06T09:00", summary="Standup")
        other = agenda_event("2026-10-05T09:00", summary="Review")
        rows = occurrences_in_range(
            [late, early, other], dt.date(2026, 10, 1), dt.date(2026, 10, 31)
        )
        assert [item.summary for item, _day in rows] == ["Review", "Standup", "Gym"]

    def test_all_day_events_take_the_whole_span(self):
        event = agenda_event("2026-10-06", all_day=True)
        rows = occurrences_in_range([event], dt.date(2026, 10, 1), dt.date(2026, 10, 31))
        assert [day for _i, day in rows] == [dt.date(2026, 10, 6)]

    def test_an_inverted_window_returns_nothing(self):
        event = agenda_event("2026-10-06T15:00")
        assert occurrences_in_range([event], dt.date(2026, 10, 31), dt.date(2026, 10, 1)) == []


class TestInstanceSpan:
    def test_an_occurrence_keeps_the_original_time_of_day(self):
        event = agenda_event("2026-10-06T15:00", rrule="FREQ=WEEKLY")
        start, end = instance_span(event, dt.date(2026, 11, 17))
        assert start == dt.datetime(2026, 11, 17, 15, 0)
        assert end == dt.datetime(2026, 11, 17, 16, 0)

    def test_a_multi_hour_event_keeps_its_duration(self):
        event = agenda_event("2026-10-06T09:00", end=dt.datetime(2026, 10, 6, 11, 30))
        start, end = instance_span(event, dt.date(2026, 10, 20))
        assert (start.hour, start.minute) == (9, 0)
        assert (end.hour, end.minute) == (11, 30)
