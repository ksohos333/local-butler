"""Tests for the day-timeline geometry: which events land where.

Pure arithmetic over events -- no GTK, no CalDAV -- so these run on an
interpreter with no display server, the same rule the recurrence tests follow.
"""

from __future__ import annotations

import datetime as dt

import pytest

from local_butler.apps.calendar.events import Event
from local_butler.apps.calendar.timeline import (
    SECONDS_PER_DAY,
    Block,
    day_blocks,
    day_seconds,
    layout_day,
    time_at,
)

ZONE = dt.timezone(dt.timedelta(hours=3))
DAY = dt.date(2026, 10, 6)
NEXT_DAY = dt.date(2026, 10, 7)


def make_event(summary, start, end=None, **kwargs):
    defaults = dict(tz=ZONE)
    defaults.update(kwargs)
    return Event(summary=summary, start=start, end=end, **defaults)


def at(hour, minute=0, day=DAY):
    return dt.datetime.combine(day, dt.time(hour, minute))


# -- seconds and fractions -------------------------------------------------


def test_day_seconds_is_a_clamped_fraction():
    assert day_seconds(0.0) == 0
    assert day_seconds(0.5) == 12 * 3600
    assert day_seconds(1.0) == SECONDS_PER_DAY
    assert day_seconds(-3.0) == 0
    assert day_seconds(42.0) == SECONDS_PER_DAY


def test_time_at_is_clamped_inside_the_day():
    assert time_at(DAY, 0) == dt.datetime(2026, 10, 6, 0, 0)
    assert time_at(DAY, 9 * 3600) == dt.datetime(2026, 10, 6, 9, 0)
    assert time_at(DAY, SECONDS_PER_DAY).time() == dt.time(23, 59, 59)
    assert time_at(DAY, -60) == dt.datetime(2026, 10, 6, 0, 0)


def test_block_fractions_are_half_the_day_for_noon():
    block = Block(
        event=make_event("Noon", at(12), at(13)),
        day=DAY,
        start=at(12),
        end=at(13),
    )
    assert block.fraction_start() == 0.5
    assert block.fraction_end() == pytest.approx(0.5 + 1 / 24)
    assert block.seconds_from_midnight == 12 * 3600
    assert block.seconds_long == 3600
    assert block.duration == dt.timedelta(hours=1)


# -- which events belong to a day -----------------------------------------


def test_a_single_day_event_appears_on_its_own_day():
    event = make_event("Dentist", at(15), at(16))
    blocks = day_blocks([event], DAY)
    assert [(b.event.summary, b.start, b.end) for b in blocks] == [
        ("Dentist", at(15), at(16))
    ]
    assert day_blocks([event], NEXT_DAY) == []


def test_a_multi_day_event_is_clipped_to_each_day_it_touches():
    event = make_event("Conference", at(10, day=DAY), at(10, day=DAY + dt.timedelta(days=2)))

    middle = day_blocks([event], NEXT_DAY)
    assert len(middle) == 1
    assert middle[0].start == dt.datetime.combine(NEXT_DAY, dt.time(0, 0))
    assert middle[0].end == dt.datetime.combine(NEXT_DAY + dt.timedelta(days=1), dt.time(0, 0))
    assert middle[0].seconds_long == SECONDS_PER_DAY


def test_an_event_ending_at_midnight_is_not_carried_into_the_next_day():
    event = make_event("Late", at(22), at(0, day=NEXT_DAY))
    assert [b.event.summary for b in day_blocks([event], NEXT_DAY)] == []


def test_recurring_events_expand_per_day():
    event = make_event(
        "Standup",
        at(9, day=DAY - dt.timedelta(days=2)),
        at(9, day=DAY - dt.timedelta(days=2)) + dt.timedelta(minutes=30),
        rrule="FREQ=DAILY;COUNT=5",
    )
    blocks = day_blocks([event], DAY)
    assert len(blocks) == 1
    assert blocks[0].start == at(9)
    assert blocks[0].end == at(9, 30)
    assert blocks[0].event is event


def test_all_day_blocks_come_before_timed_blocks():
    all_day = make_event("Holiday", at(0), all_day=True)
    timed = make_event("Dentist", at(9), at(10))
    blocks = day_blocks([timed, all_day], DAY)
    assert [block.all_day for block in blocks] == [True, False]


# -- lanes -----------------------------------------------------------------


def test_overlapping_events_get_separate_lanes():
    first = Block(
        event=make_event("A", at(9), at(10)),
        day=DAY, start=at(9), end=at(10),
    )
    second = Block(
        event=make_event("B", at(9, 30), at(10, 30)),
        day=DAY, start=at(9, 30), end=at(10, 30),
    )
    third = Block(
        event=make_event("C", at(9, 15), at(9, 45)),
        day=DAY, start=at(9, 15), end=at(9, 45),
    )

    placed, all_day_rows, timed_rows = layout_day([first, second, third])

    assert timed_rows == 3
    assert all_day_rows == 0
    assert [block.lane for block in placed] == [0, 1, 2]


def test_events_that_merely_touch_share_a_lane():
    early = Block(
        event=make_event("A", at(10), at(11)),
        day=DAY, start=at(10), end=at(11),
    )
    late = Block(
        event=make_event("B", at(11), at(12)),
        day=DAY, start=at(11), end=at(12),
    )

    placed, _, timed_rows = layout_day([early, late])

    assert timed_rows == 1
    assert {block.lane for block in placed} == {0}


def test_the_lane_pool_is_reused_by_a_later_event():
    blocks = [
        Block(event=make_event("A", at(9), at(10)), day=DAY, start=at(9), end=at(10)),
        Block(event=make_event("B", at(9), at(10)), day=DAY, start=at(9), end=at(10)),
        Block(event=make_event("C", at(11), at(12)), day=DAY, start=at(11), end=at(12)),
    ]

    placed, _, timed_rows = layout_day(blocks)

    assert timed_rows == 2  # C fits back into the lane A vacated
    by_summary = {block.event.summary: block.lane for block in placed}
    assert by_summary["C"] == 0


def test_all_day_events_have_their_own_lane_pool():
    all_day = Block(
        event=make_event("Holiday", at(0), all_day=True),
        day=DAY, start=at(0), end=at(0) + dt.timedelta(days=1), all_day=True,
    )
    first = Block(
        event=make_event("A", at(9), at(17)), day=DAY, start=at(9), end=at(17),
    )
    second = Block(
        event=make_event("B", at(10), at(11)), day=DAY, start=at(10), end=at(11),
    )

    placed, all_day_rows, timed_rows = layout_day([first, all_day, second])

    # One all-day row above; beneath it the two overlapping meetings stack in
    # their own pool, so a whole-width bar never shoves them into one lane.
    assert all_day_rows == 1
    assert timed_rows == 2
    lanes = {block.event.summary: block.lane for block in placed}
    assert lanes == {"Holiday": 0, "A": 0, "B": 1}


def test_three_meetings_in_one_hour_are_three_bars():
    """The whole point of the widget: same hour, side by side, none hidden."""
    hour = at(14)
    blocks = [
        Block(
            event=make_event(
                name,
                hour + dt.timedelta(minutes=offset),
                hour + dt.timedelta(minutes=offset + 30),
            ),
            day=DAY,
            start=hour + dt.timedelta(minutes=offset),
            end=hour + dt.timedelta(minutes=offset + 30),
        )
        for name, offset in (("Sync", 0), ("Review", 10), ("1:1", 20))
    ]

    placed, _, timed_rows = layout_day(blocks)

    assert timed_rows == 3
    assert len({block.lane for block in placed}) == 3


def test_layout_leaves_the_original_blocks_untouched():
    block = Block(
        event=make_event("A", at(9), at(10)), day=DAY, start=at(9), end=at(10),
    )
    layout_day([block])
    assert block.lane == 0  # a default lane, not somebody else's 1
