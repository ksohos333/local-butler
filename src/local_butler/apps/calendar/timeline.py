"""Geometry for one day of the calendar: which events, in which lane.

The widget that draws this lives in :mod:`panels`; the arithmetic does not, so
it can be tested on an interpreter that has no display server and no PyGObject
-- the same rule the recurrence module follows.

A day is a fixed 86400 seconds wide, so every position is a fraction of the day
and nothing here depends on pixels. :func:`layout_day` decides lanes: events
that share time never share a lane, which is what makes an hour with three
meetings show three bars side by side instead of one bar hiding the others.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Iterable, List, Sequence, Tuple

from local_butler.apps.calendar.events import Event
from local_butler.apps.calendar.recurrence import instance_span, occurrences_in_range

SECONDS_PER_DAY = 86400
DAY_START = dt.time(0, 0)


@dataclass(frozen=True)
class Block:
    """One occurrence of one event, already clipped to a single day."""

    event: Event
    day: dt.date
    start: dt.datetime
    end: dt.datetime
    lane: int = 0
    all_day: bool = False

    @property
    def duration(self) -> dt.timedelta:
        return self.end - self.start

    @property
    def seconds_from_midnight(self) -> float:
        return (self.start - dt.datetime.combine(self.day, DAY_START)).total_seconds()

    @property
    def seconds_long(self) -> float:
        return self.duration.total_seconds()

    def fraction_start(self) -> float:
        return max(0.0, min(1.0, self.seconds_from_midnight / SECONDS_PER_DAY))

    def fraction_end(self) -> float:
        end = self.seconds_from_midnight + self.seconds_long
        return max(0.0, min(1.0, end / SECONDS_PER_DAY))


def day_blocks(events: Iterable[Event], day: dt.date) -> List[Block]:
    """Every occurrence touching ``day``, clipped to that day's midnight edges.

    A three-day event shows up on all three days as a bar that runs off the
    right-hand edge of the last day rather than being silently dropped, and a
    recurring event expands here rather than in the caller.
    """
    day_start = dt.datetime.combine(day, DAY_START)
    day_end = day_start + dt.timedelta(days=1)
    blocks: List[Block] = []
    for event, _on in occurrences_in_range(events, day, day):
        if event.rrule:
            start, end = instance_span(event, day)
        else:
            start, end = event.start, event.end
        start = max(start, day_start)
        end = min(end, day_end)
        if end <= start:
            continue
        blocks.append(
            Block(
                event=event,
                day=day,
                start=start,
                end=end,
                all_day=event.all_day,
            )
        )
    blocks.sort(
        key=lambda block: (
            0 if block.all_day else 1,
            block.start,
            block.end,
            block.event.summary,
        )
    )
    return blocks


def layout_day(blocks: Sequence[Block]) -> Tuple[List[Block], int, int]:
    """Assign lanes and return ``(placed, all_day_rows, timed_rows)``.

    All-day events get their own lane pool above the timed ones, because they
    span the whole width and would otherwise push every meeting into a single
    lane at the bottom. Within a pool the rule is greedy: the first lane whose
    last event has already ended takes the next one, so events that merely
    touch (10:00-11:00 and 11:00-12:00) still stack in one lane.
    """
    placed: List[Block] = []
    all_day_rows = 0
    timed_rows = 0
    for want_all_day in (True, False):
        pool = [block for block in blocks if block.all_day is want_all_day]
        lane_ends: List[dt.datetime] = []
        for block in pool:
            lane = 0
            while lane < len(lane_ends) and lane_ends[lane] > block.start:
                lane += 1
            if lane == len(lane_ends):
                lane_ends.append(block.end)
            else:
                lane_ends[lane] = block.end
            placed.append(_with_lane(block, lane))
        rows = len(lane_ends)
        if want_all_day:
            all_day_rows = rows
        else:
            timed_rows = rows
    return placed, all_day_rows, timed_rows


def _with_lane(block: Block, lane: int) -> Block:
    return Block(
        event=block.event,
        day=block.day,
        start=block.start,
        end=block.end,
        lane=lane,
        all_day=block.all_day,
    )


def day_seconds(fraction: float) -> int:
    """Clamped fraction of a day into seconds past midnight."""
    fraction = max(0.0, min(1.0, fraction))
    return int(round(fraction * SECONDS_PER_DAY))


def time_at(day: dt.date, seconds: int) -> dt.datetime:
    """Clock time for ``seconds`` past midnight on ``day``, clamped to 23:59."""
    seconds = max(0, min(SECONDS_PER_DAY - 1, int(seconds)))
    return dt.datetime.combine(day, DAY_START) + dt.timedelta(seconds=seconds)
