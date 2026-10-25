"""Tests for free/busy analysis and the move proposals it makes.

Pure date arithmetic over events: the assistant's ``optimize_schedule`` and
``find_free_time`` tools quote these functions, so the answers the model gives
and the answers the tests expect are the same answers.
"""

from __future__ import annotations

import datetime as dt

from local_butler.apps.calendar.events import Event
from local_butler.apps.calendar.optimize import (
    DEFAULT_WORK_END,
    DEFAULT_WORK_START,
    Busy,
    analyse,
    busy_on,
    day_load,
    find_clashes,
    find_free_time,
    free_slots,
    propose_moves,
    tight_pairs,
)

ZONE = dt.timezone(dt.timedelta(hours=3))
FIRST = dt.date(2026, 10, 6)
LAST = dt.date(2026, 10, 12)  # a Tuesday to the following Monday


def at(day, hour, minute=0):
    return dt.datetime.combine(day, dt.time(hour, minute))


def make_event(summary, start, minutes=60, **kwargs):
    defaults = dict(tz=ZONE)
    defaults.update(kwargs)
    return Event(
        summary=summary,
        start=start,
        end=start + dt.timedelta(minutes=minutes),
        **defaults,
    )


# -- busy intervals --------------------------------------------------------


def test_busy_on_expands_the_window_day_by_day():
    events = [
        make_event("A", at(FIRST, 9)),
        make_event("B", at(FIRST + dt.timedelta(days=3), 14)),
        make_event("Old", at(FIRST - dt.timedelta(days=5), 9)),
    ]

    busy = busy_on(events, FIRST, LAST)

    assert [item.event.summary for item in busy] == ["A", "B"]
    assert busy[0].start == at(FIRST, 9)
    assert busy[0].end == at(FIRST, 10)
    assert busy[0].minutes == 60
    assert busy[0].label.startswith("09:00-10:00")


def test_busy_on_includes_every_occurrence_of_a_recurring_event():
    event = make_event(
        "Standup",
        at(FIRST - dt.timedelta(days=1), 9),
        minutes=15,
        rrule="FREQ=DAILY;COUNT=4",
    )

    busy = busy_on([event], FIRST, LAST)

    assert len(busy) == 3  # the 6th, 7th and 8th of October
    assert {item.start.time() for item in busy} == {dt.time(9, 0)}


# -- clashes ---------------------------------------------------------------


def test_overlapping_events_are_reported_with_their_overlap_length():
    first = make_event("Dentist", at(FIRST, 9), minutes=60)
    second = make_event("Team sync", at(FIRST, 9, 30), minutes=60)

    clashes = find_clashes(busy_on([first, second], FIRST, LAST))

    assert len(clashes) == 1
    assert clashes[0].minutes == 30
    assert "overlaps" in clashes[0].label


def test_events_that_do_not_touch_are_not_a_clash():
    events = [
        make_event("A", at(FIRST, 9), minutes=60),
        make_event("B", at(FIRST, 10), minutes=60),
    ]
    assert find_clashes(busy_on(events, FIRST, LAST)) == []


def test_the_same_event_never_clashes_with_itself():
    event = make_event("A", at(FIRST, 9), minutes=60, rrule="FREQ=DAILY;COUNT=3")
    busy = busy_on([event], FIRST, LAST)
    assert len(busy) == 3
    assert find_clashes(busy) == []


def test_clashes_are_sorted_longest_first():
    short = make_event("Short", at(FIRST, 9, 50), minutes=30)  # 09:50-10:20
    long_a = make_event("Long A", at(FIRST, 9), minutes=60)  # 09:00-10:00
    long_b = make_event("Long B", at(FIRST, 9, 15), minutes=60)  # 09:15-10:15

    clashes = find_clashes(busy_on([short, long_a, long_b], FIRST, LAST))

    assert [clash.minutes for clash in clashes] == [45, 25, 10]
    assert clashes[0].minutes >= clashes[-1].minutes


def test_back_to_back_pair_is_tight_but_adjacent_hour_is_not():
    tight = [
        make_event("A", at(FIRST, 9), minutes=60),
        make_event("B", at(FIRST, 10, 5), minutes=30),
    ]
    spaced = [
        make_event("A", at(FIRST, 9), minutes=60),
        make_event("B", at(FIRST, 10, 30), minutes=30),
    ]
    assert len(tight_pairs(busy_on(tight, FIRST, LAST))) == 1
    assert tight_pairs(busy_on(spaced, FIRST, LAST)) == []


# -- free time -------------------------------------------------------------


def test_free_slots_are_inside_working_hours_only():
    events = [make_event("Busy", at(FIRST, 10), minutes=60)]

    slots = free_slots(busy_on(events, FIRST, FIRST), FIRST, FIRST)

    assert slots[0].start == at(FIRST, 9)  # window opens at 09:00
    assert slots[0].end == at(FIRST, 10)
    assert slots[1].start == at(FIRST, 11)
    assert slots[1].end == at(FIRST, 17)  # window closes at 17:00
    assert all(slot.start.time() >= DEFAULT_WORK_START for slot in slots)
    assert all(slot.end.time() <= DEFAULT_WORK_END for slot in slots)


def test_a_slot_shorter_than_the_minimum_is_not_offered():
    events = [
        make_event("Busy", at(FIRST, 9, 20), minutes=60),
        make_event("Rest of the day", at(FIRST, 10, 20), minutes=7 * 60),
    ]  # 09:00-09:20 is the only gap left

    assert free_slots(busy_on(events, FIRST, FIRST), FIRST, FIRST, min_minutes=30) == []
    slots = free_slots(
        busy_on(events, FIRST, FIRST), FIRST, FIRST, min_minutes=15
    )
    assert len(slots) == 1
    assert slots[0].minutes == 20


def test_reserved_time_is_held_back_from_the_free_slots():
    hold = Busy(
        event=make_event("Held", at(FIRST, 13)),
        start=at(FIRST, 13),
        end=at(FIRST, 14),
    )

    slots = free_slots([], FIRST, FIRST, reserved=[hold])

    assert not any(slot.start <= at(FIRST, 13) < slot.end for slot in slots)
    assert any(slot.start == at(FIRST, 14) for slot in slots)


def test_day_load_adds_up_minutes_per_day():
    events = [
        make_event("A", at(FIRST, 9), minutes=90),
        make_event("B", at(FIRST, 13), minutes=30),
        make_event("C", at(FIRST + dt.timedelta(days=1), 10), minutes=60),
    ]

    load = dict(day_load(busy_on(events, FIRST, LAST), FIRST, LAST))

    assert load[FIRST] == 120
    assert load[FIRST + dt.timedelta(days=1)] == 60
    assert load[LAST] == 0


# -- proposals -------------------------------------------------------------


def test_the_later_event_is_the_one_offered_for_a_move():
    early = make_event("Dentist", at(FIRST, 9), minutes=60)
    late = make_event("Team sync", at(FIRST, 9, 30), minutes=60)

    moves = propose_moves([early, late], FIRST, LAST)

    assert len(moves) == 1
    assert moves[0].event.summary == "Team sync"
    assert moves[0].new_start == at(FIRST, 10)  # straight after the dentist
    assert moves[0].new_end == at(FIRST, 11)
    assert "clashing with Dentist" in moves[0].reason
    assert moves[0].label.startswith('Move "Team sync"')


def test_a_proposed_move_never_lands_on_another_event():
    first = make_event("A", at(FIRST, 9), minutes=60)
    clash_a = make_event("B", at(FIRST, 9, 30), minutes=60)
    other = make_event("C", at(FIRST, 11), minutes=60)

    moves = propose_moves([first, clash_a, other], FIRST, LAST)

    moved = moves[0].proposed
    for busy_event in (first, other):
        assert moved.end <= busy_event.start or busy_event.end <= moved.start


def test_overlapping_all_day_events_are_never_proposed_for_a_move():
    events = [
        make_event("Holiday", at(FIRST, 0), all_day=True),
        make_event("Offsite", at(FIRST, 0), all_day=True),
    ]

    # They do clash -- but the event offered for a move is an all-day one, and
    # "move this to another day" is a decision the tool is not allowed to make.
    assert find_clashes(busy_on(events, FIRST, LAST)), "these must actually clash"
    assert propose_moves(events, FIRST, LAST) == []


def test_recurring_events_are_never_proposed_for_a_move():
    base = make_event("Dentist", at(FIRST, 9), minutes=60)
    standup = make_event(
        "Standup", at(FIRST, 9, 30), minutes=30, rrule="FREQ=DAILY;COUNT=3"
    )

    moves = propose_moves([base, standup], FIRST, LAST)

    assert moves == []


def test_max_moves_caps_the_suggestions():
    events = []
    for index in range(4):
        day = FIRST + dt.timedelta(days=index)
        events.append(make_event("Base {0}".format(index), at(day, 10), minutes=60))
        events.append(
            make_event("Clash {0}".format(index), at(day, 10, 30), minutes=60)
        )

    uncapped = propose_moves(events, FIRST, LAST, max_moves=10)
    assert len(uncapped) == 4

    moves = propose_moves(events, FIRST, LAST, max_moves=2)
    assert len(moves) == 2
    assert [move.event.summary for move in moves] == ["Clash 0", "Clash 1"]


# -- the reports the model quotes ------------------------------------------


def test_analyse_states_clashes_load_gaps_and_moves():
    events = [
        make_event("Dentist", at(FIRST, 9), minutes=60),
        make_event("Team sync", at(FIRST, 9, 30), minutes=60),
    ]

    report = analyse(events, FIRST, LAST)

    assert "Clashes (1):" in report
    assert "overlaps" in report
    assert "Busiest days:" in report
    assert "Free for 60+ min" in report
    assert "Suggested moves" in report
    assert "Team sync" in report


def test_analyse_says_plainly_when_there_is_nothing_wrong():
    report = analyse([], FIRST, LAST)

    assert "0 occurrence(s), 0 min booked" in report
    assert "Clashes: none." in report
    assert "Free for 60+ min" in report
    assert "Suggested moves: none" in report


def test_analyse_reports_a_full_week_without_inventing_free_time():
    events = [
        make_event("Full {0}".format(day), at(day, 9), minutes=8 * 60)
        for day in (FIRST + dt.timedelta(days=i) for i in range(5))
    ]

    report = analyse(events, FIRST, FIRST + dt.timedelta(days=4))

    assert "No 60+ minute gap in working hours" in report
    assert "Suggested moves: none" in report


def test_analyse_clash_list_is_capped_for_a_long_report():
    events = [make_event("All hands", at(FIRST, 9), minutes=8 * 60)]
    events += [
        make_event(
            "Visitor {0}".format(i),
            at(FIRST, 9, 30) + dt.timedelta(minutes=30 * i),
            minutes=45,
        )
        for i in range(10)
    ]

    report = analyse(events, FIRST, LAST)

    # 10 with the all-hands meeting, plus 9 where the visitors overlap each
    # other -- the report shows eight and says how many it left out.
    assert "Clashes (19):" in report
    assert "...and 11 more" in report


def test_find_free_time_lists_the_windows():
    events = [make_event("Busy", at(FIRST, 11), minutes=60)]

    answer = find_free_time(events, FIRST, FIRST, minutes=60)

    assert answer.startswith("- ")
    assert "free 60-minute window(s)" in answer
    assert "06 Oct" in answer
    assert "09:00-11:00" in answer


def test_find_free_time_says_no_when_the_day_is_full():
    events = [make_event("Busy", at(FIRST, 9), minutes=8 * 60)]

    answer = find_free_time(events, FIRST, FIRST, minutes=60)

    assert answer.startswith("No free 60-minute window")
    assert "1 occurrence(s) booked" in answer


def test_find_free_time_honours_a_custom_minutes_value():
    packed = [make_event("Busy", at(FIRST, 9), minutes=8 * 60)]
    assert "No free 120-minute window" in find_free_time(
        packed, FIRST, FIRST, minutes=120
    )
    assert "No free 15-minute window" in find_free_time(
        packed, FIRST, FIRST, minutes=15
    )

    gappy = [make_event("Busy", at(FIRST, 9, 45), minutes=60)]  # 09:00-09:45 free
    answer = find_free_time(gappy, FIRST, FIRST, minutes=15)
    assert "free 15-minute window" in answer
    assert "No free 120-minute window" not in answer
