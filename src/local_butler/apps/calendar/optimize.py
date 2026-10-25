"""Free/busy analysis for a stretch of calendar, and proposals to fix it.

Pure date arithmetic over events: no GTK, no CalDAV, no model. The assistant's
``optimize_schedule`` tool and the tests both read this, so "tidy up my week"
means the same thing whether the user asked the chat or the test suite.

Nothing here rewrites the calendar. :func:`propose_moves` only returns
suggestions, and events that repeat or are all-day are left alone, because
"move this occurrence" and "move this series" are different questions.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, replace
from typing import Iterable, List, Optional, Sequence, Tuple

from local_butler.apps.calendar.events import Event
from local_butler.apps.calendar.recurrence import instance_span, occurrences_in_range

DEFAULT_WORK_START = dt.time(9, 0)
DEFAULT_WORK_END = dt.time(17, 0)
TIGHT_GAP = dt.timedelta(minutes=10)


@dataclass(frozen=True)
class Busy:
    """One occurrence occupying ``[start, end)``."""

    event: Event
    start: dt.datetime
    end: dt.datetime

    @property
    def minutes(self) -> int:
        return int(max(0.0, (self.end - self.start).total_seconds()) // 60)

    @property
    def label(self) -> str:
        return "{0}-{1} {2}".format(
            self.start.strftime("%H:%M"),
            self.end.strftime("%H:%M"),
            self.event.summary,
        )


@dataclass(frozen=True)
class Clash:
    """Two events sharing time; ``minutes`` is the length of the overlap."""

    first: Busy
    second: Busy
    minutes: int

    @property
    def label(self) -> str:
        return "{0} overlaps {1} by {2} min".format(
            self.first.label, self.second.label, self.minutes
        )


@dataclass(frozen=True)
class FreeSlot:
    start: dt.datetime
    end: dt.datetime

    @property
    def minutes(self) -> int:
        return int((self.end - self.start).total_seconds() // 60)

    @property
    def label(self) -> str:
        return "{0} {1}-{2}".format(
            self.start.strftime("%a %d %b"),
            self.start.strftime("%H:%M"),
            self.end.strftime("%H:%M"),
        )


@dataclass(frozen=True)
class Move:
    """A suggestion: put ``event`` at ``new_start`` instead of where it is."""

    event: Event
    new_start: dt.datetime
    new_end: dt.datetime
    reason: str = ""

    @property
    def proposed(self) -> Event:
        return replace(self.event, start=self.new_start, end=self.new_end)

    @property
    def label(self) -> str:
        return 'Move "{0}" to {1} {2} ({3})'.format(
            self.event.summary,
            self.new_start.strftime("%a %d %b"),
            self.new_start.strftime("%H:%M"),
            self.reason or "clears a clash",
        )


def busy_on(events: Iterable[Event], first: dt.date, last: dt.date) -> List[Busy]:
    """Every occurrence in ``[first, last]``, as absolute start/end pairs."""
    rows: List[Busy] = []
    for event, day in occurrences_in_range(events, first, last):
        if event.rrule:
            start, end = instance_span(event, day)
        else:
            start, end = event.start, event.end
        rows.append(Busy(event=event, start=start, end=end))
    rows.sort(key=lambda item: (item.start, item.end, item.event.summary))
    return rows


def find_clashes(busy: Sequence[Busy]) -> List[Clash]:
    """Every pair sharing time, longest overlap first."""
    clashes: List[Clash] = []
    for index, first in enumerate(busy):
        for second in busy[index + 1 :]:
            if first.event.uid == second.event.uid:
                continue
            overlap = min(first.end, second.end) - max(first.start, second.start)
            if overlap > dt.timedelta(0):
                minutes = int(overlap.total_seconds() // 60)
                clashes.append(Clash(first=first, second=second, minutes=minutes))
    clashes.sort(key=lambda clash: (-clash.minutes, clash.first.start))
    return clashes


def tight_pairs(busy: Sequence[Busy]) -> List[Tuple[Busy, Busy]]:
    """Consecutive events with barely any gap: the day is a corridor walk."""
    ordered = sorted(busy, key=lambda item: item.start)
    pairs: List[Tuple[Busy, Busy]] = []
    for previous, following in zip(ordered, ordered[1:]):
        if previous.event.uid == following.event.uid:
            continue
        gap = following.start - previous.end
        if dt.timedelta(0) < gap < TIGHT_GAP:
            pairs.append((previous, following))
    return pairs


def _gaps(
    occupied: Sequence[Busy], window_start: dt.datetime, window_end: dt.datetime
) -> List[Tuple[dt.datetime, dt.datetime]]:
    """Free stretches inside ``[window_start, window_end)``."""
    hits = sorted(
        (item for item in occupied
         if item.end > window_start and item.start < window_end),
        key=lambda item: item.start,
    )
    gaps: List[Tuple[dt.datetime, dt.datetime]] = []
    cursor = window_start
    for item in hits:
        if item.start > cursor:
            gaps.append((cursor, min(item.start, window_end)))
        cursor = max(cursor, item.end)
        if cursor >= window_end:
            break
    if cursor < window_end:
        gaps.append((cursor, window_end))
    return gaps


def free_slots(
    busy: Sequence[Busy],
    first: dt.date,
    last: dt.date,
    work_start: dt.time = DEFAULT_WORK_START,
    work_end: dt.time = DEFAULT_WORK_END,
    min_minutes: int = 30,
    reserved: Sequence[Busy] = (),
) -> List[FreeSlot]:
    """Working-hours gaps of at least ``min_minutes`` between ``first`` and ``last``."""
    occupied = list(busy) + list(reserved)
    slots: List[FreeSlot] = []
    need = dt.timedelta(minutes=min_minutes)
    day = first
    while day <= last:
        window_start = dt.datetime.combine(day, work_start)
        window_end = dt.datetime.combine(day, work_end)
        for gap_start, gap_end in _gaps(occupied, window_start, window_end):
            if gap_end - gap_start >= need:
                slots.append(FreeSlot(start=gap_start, end=gap_end))
        day += dt.timedelta(days=1)
    return slots


def day_load(busy: Sequence[Busy], first: dt.date, last: dt.date) -> List[Tuple[dt.date, int]]:
    """Minutes booked per day across ``[first, last]``."""
    rows: List[Tuple[dt.date, int]] = []
    day = first
    while day <= last:
        day_start = dt.datetime.combine(day, dt.time(0, 0))
        day_end = day_start + dt.timedelta(days=1)
        total = 0
        for item in busy:
            overlap = min(item.end, day_end) - max(item.start, day_start)
            if overlap > dt.timedelta(0):
                total += int(overlap.total_seconds() // 60)
        rows.append((day, total))
        day += dt.timedelta(days=1)
    return rows


def _first_fit(
    occupied: Sequence[Busy],
    not_before: dt.datetime,
    duration: dt.timedelta,
    first: dt.date,
    last: dt.date,
    work_start: dt.time,
    work_end: dt.time,
) -> Optional[Tuple[dt.datetime, dt.datetime]]:
    """Earliest working-hours gap of ``duration`` at or after ``not_before``."""
    day = max(not_before.date(), first)
    while day <= last:
        window_start = dt.datetime.combine(day, work_start)
        window_end = dt.datetime.combine(day, work_end)
        search = max(not_before, window_start)
        if search < window_end:
            for gap_start, gap_end in _gaps(occupied, window_start, window_end):
                start = max(gap_start, search)
                if gap_end - start >= duration:
                    return start, start + duration
        day += dt.timedelta(days=1)
    return None


def propose_moves(
    events: Iterable[Event],
    first: dt.date,
    last: dt.date,
    max_moves: int = 5,
    work_start: dt.time = DEFAULT_WORK_START,
    work_end: dt.time = DEFAULT_WORK_END,
) -> List[Move]:
    """Suggest where each clashing event could go instead.

    The event that started later is the one offered for a move; the earlier one
    stays put, which keeps the answers stable when the same week is analysed
    twice. Recurring and all-day events are never moved: the suggestion would
    have to rewrite the whole series or invent a detached instance, and this
    tool is not allowed to write to the calendar anyway.
    """
    busy = busy_on(events, first, last)
    moves: List[Move] = []
    moved = set()
    for clash in find_clashes(busy):
        victim = clash.second
        other = clash.first
        if victim.event.uid in moved:
            continue
        if victim.event.all_day or victim.event.rrule:
            continue
        others = [item for item in busy if item.event.uid != victim.event.uid]
        for move in moves:
            others.append(Busy(event=move.event, start=move.new_start, end=move.new_end))
        slot = _first_fit(
            others,
            victim.start,
            victim.end - victim.start,
            first,
            last,
            work_start,
            work_end,
        )
        if slot is None:
            continue
        moves.append(
            Move(
                event=victim.event,
                new_start=slot[0],
                new_end=slot[1],
                reason="was clashing with {0}".format(other.event.summary),
            )
        )
        moved.add(victim.event.uid)
        if len(moves) >= max_moves:
            break
    return moves


def analyse(
    events: Iterable[Event],
    first: dt.date,
    last: dt.date,
    work_start: dt.time = DEFAULT_WORK_START,
    work_end: dt.time = DEFAULT_WORK_END,
    min_free_minutes: int = 60,
    max_moves: int = 5,
) -> str:
    """A plain-text read of ``[first, last]``: clashes, load, gaps, moves.

    Written for the assistant to quote back at the user, so it states what is
    wrong, how heavy each day is, where the time is free, and what could be
    moved -- and says so plainly when a section is empty, because "no clashes"
    and "analysis failed" must not read the same.
    """
    rows = list(events)
    busy = busy_on(rows, first, last)
    clashes = find_clashes(busy)
    lines: List[str] = [
        "{0} to {1}: {2} occurrence(s), {3} min booked".format(
            first.isoformat(), last.isoformat(), len(busy),
            sum(item.minutes for item in busy),
        )
    ]

    if clashes:
        lines.append("")
        lines.append("Clashes ({0}):".format(len(clashes)))
        lines.extend("- " + clash.label for clash in clashes[:8])
        if len(clashes) > 8:
            lines.append("- ...and {0} more".format(len(clashes) - 8))
    else:
        lines.append("")
        lines.append("Clashes: none.")

    tight = tight_pairs(busy)
    if tight:
        lines.append("")
        lines.append("Back to back ({0}):".format(len(tight)))
        lines.extend(
            "- {0} then {1}".format(early.label, late.label)
            for early, late in tight[:5]
        )

    loads = sorted(day_load(busy, first, last), key=lambda row: -row[1])
    busy_days = [row for row in loads if row[1] > 0]
    if busy_days:
        lines.append("")
        lines.append("Busiest days:")
        for day, minutes in busy_days[:4]:
            lines.append(
                "- {0}: {1} min ({2} h)".format(
                    day.strftime("%a %d %b"), minutes, round(minutes / 60.0, 1)
                )
            )

    slots = free_slots(
        busy, first, last, work_start=work_start, work_end=work_end,
        min_minutes=min_free_minutes,
    )
    lines.append("")
    if slots:
        lines.append(
            "Free for {0}+ min, {1}-{2}:".format(
                min_free_minutes,
                work_start.strftime("%H:%M"),
                work_end.strftime("%H:%M"),
            )
        )
        lines.extend("- " + slot.label for slot in slots[:6])
        if len(slots) > 6:
            lines.append("- ...and {0} more".format(len(slots) - 6))
    else:
        lines.append(
            "No {0}+ minute gap in working hours ({1}-{2}).".format(
                min_free_minutes,
                work_start.strftime("%H:%M"),
                work_end.strftime("%H:%M"),
            )
        )

    moves = propose_moves(
        rows, first, last, max_moves=max_moves,
        work_start=work_start, work_end=work_end,
    )
    lines.append("")
    if moves:
        lines.append("Suggested moves (nothing is changed until you apply them):")
        lines.extend("- " + move.label for move in moves)
    else:
        lines.append(
            "Suggested moves: none -- nothing is safe to move automatically "
            "(recurring and all-day events are left alone)."
        )
    return "\n".join(lines)


def find_free_time(
    events: Iterable[Event],
    first: dt.date,
    last: dt.date,
    minutes: int = 60,
    work_start: dt.time = DEFAULT_WORK_START,
    work_end: dt.time = DEFAULT_WORK_END,
) -> str:
    """Text answer to "when am I free for N minutes?"."""
    busy = busy_on(events, first, last)
    slots = free_slots(
        busy, first, last, work_start=work_start, work_end=work_end,
        min_minutes=minutes,
    )
    if not slots:
        return (
            "No free {0}-minute window between {1} and {2} in {3}..{4} "
            "({5} occurrence(s) booked).".format(
                minutes, work_start.strftime("%H:%M"), work_end.strftime("%H:%M"),
                first.isoformat(), last.isoformat(), len(busy),
            )
        )
    return "\n".join(
        "- {0}".format(slot.label) for slot in slots[:10]
    ) + "\n({0} free {1}-minute window(s) shown)".format(len(slots[:10]), minutes)
