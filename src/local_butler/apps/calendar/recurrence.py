"""RRULE expansion and agenda ranges for the calendar.

Kept apart from :mod:`panels` because the date arithmetic is pure and must stay
testable on an interpreter without PyGObject.

Two entry points share one rule walker:

* :func:`recurring_occurrences` answers "mark this month's grid", walking from
  DTSTART.
* :func:`occurrences_in_range` answers "list what happens in this day, week or
  month", walking over an arbitrary window so a rule that started years ago
  still reaches the window being viewed.

Both respect ``BYDAY``, ``COUNT`` and ``UNTIL`` identically; that is the point of
sharing :func:`_walk`.
"""

from __future__ import annotations

import calendar
import datetime as dt
from typing import Iterable, List, Optional, Sequence, Tuple

from local_butler.apps.calendar.events import Event, parse_until

WEEKDAY_INDEX = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}
EXPANSION_CAP = 800
SCOPES = ("day", "week", "month")


# -- rules -----------------------------------------------------------------
class _Rule:
    """The subset of RRULE this app supports, already parsed."""

    __slots__ = ("freq", "byday", "count", "until")

    def __init__(
        self,
        freq: str = "",
        byday: Sequence[int] = (),
        count: int = 0,
        until: Optional[dt.datetime] = None,
    ) -> None:
        self.freq = freq
        self.byday = list(byday)
        self.count = count
        self.until = until


def _rule_for(event: Event) -> _Rule:
    """Parse ``event.rrule``; returns an empty rule when there is nothing to do."""
    if not event.rrule:
        return _Rule()
    parts = {}
    for chunk in event.rrule.split(";"):
        key, _, value = chunk.partition("=")
        if key.strip():
            parts[key.strip().upper()] = value.strip()
    freq = parts.get("FREQ", "").upper()
    byday = [
        WEEKDAY_INDEX[token]
        for token in parts.get("BYDAY", "").split(",")
        if token.strip().upper() in WEEKDAY_INDEX
    ]
    if freq == "WEEKLY" and not byday:
        # A weekly rule with no BYDAY repeats on DTSTART's weekday. Without this
        # an unfiltered window would match all seven days.
        byday = [event.start.date().weekday()]
    try:
        count = int(parts.get("COUNT", "0"))
    except ValueError:
        count = 0
    return _Rule(freq, byday, count, parse_until(parts.get("UNTIL", ""), event.tz))


def _mark_time(event: Event) -> dt.time:
    """The wall-clock time every occurrence of a rule keeps."""
    clock = event.start.timetz()
    return dt.time(clock.hour, clock.minute, clock.second, clock.microsecond)


def _allowed(rule: _Rule, day: dt.date, mark: dt.time) -> bool:
    """``BYDAY`` filter plus the inclusive ``UNTIL`` bound."""
    if rule.byday and day.weekday() not in rule.byday:
        return False
    if rule.until is None:
        return True
    return dt.datetime.combine(day, mark) <= rule.until


def _monthly_anchor(start: dt.date) -> int:
    """Day-of-month a MONTHLY rule repeats on.

    Clamped to 28 so a 29th/30th/31st start does not skip short months, which
    is how most calendar clients behave rather than dropping a month.
    """
    return min(start.day, 28)


def _candidates(
    rule: _Rule, start: dt.date, last: dt.date, anchor_day: int
) -> Iterable[dt.date]:
    """Every date the frequency could match in ``[start, last]``, BYDAY aside.

    Yields dates in order. The caller applies :func:`_allowed`, which is what
    keeps ``COUNT`` and ``UNTIL`` bookkeeping in one place.
    """
    freq = rule.freq
    if freq == "DAILY":
        current = start
        while current <= last:
            yield current
            current += dt.timedelta(days=1)
    elif freq == "WEEKLY":
        current = start
        while current <= last:
            yield current
            current += dt.timedelta(days=1)
    elif freq == "MONTHLY":
        year, month = start.year, start.month
        while True:
            if dt.date(year, month, 1) > last:
                return
            try:
                candidate = dt.date(year, month, anchor_day)
            except ValueError:
                candidate = None
            if candidate is not None and start <= candidate <= last:
                yield candidate
            month += 1
            if month > 12:
                year, month = year + 1, 1
    elif freq == "YEARLY":
        # YEARLY never matches DTSTART itself, matching the month-grid behaviour.
        try:
            current = start.replace(year=start.year + 1)
        except ValueError:  # 29 February
            current = start.replace(year=start.year + 1, day=28)
        while current <= last:
            yield current
            try:
                current = current.replace(year=current.year + 1)
            except ValueError:
                current = current.replace(year=current.year + 1, day=28)
    else:
        return


def _occurrences_before(rule: _Rule, start: dt.date, before: dt.date, mark: dt.time) -> int:
    """How many occurrences the rule yields strictly before ``before``.

    Needed because ``COUNT`` counts from DTSTART: a window that begins after the
    start must know how many of the count it already spent. Stops at ``COUNT``,
    so this stays cheap for the small counts that actually appear in practice.
    """
    if not rule.freq or before <= start:
        return 0
    seen = 0
    for candidate in _candidates(rule, start, before - dt.timedelta(days=1), _monthly_anchor(start)):
        if not _allowed(rule, candidate, mark):
            continue
        seen += 1
        if rule.count and seen >= rule.count:
            return seen
        if seen >= EXPANSION_CAP:
            break
    return seen


def _walk(event: Event, first: dt.date, last: dt.date) -> List[dt.date]:
    """Occurrences of ``event`` inside ``[first, last]``, in order."""
    rule = _rule_for(event)
    start = event.start.date()
    if not rule.freq or last < start:
        return []
    if not rule.freq in ("DAILY", "WEEKLY", "MONTHLY", "YEARLY"):
        # An unrecognised FREQ cannot be expanded safely. validate_rrule rejects
        # these at write time, so this only guards something already stored.
        return [start] if first <= start <= last else []

    mark = _mark_time(event)
    window = max(first, start)
    results: List[dt.date] = []
    spent = _occurrences_before(rule, start, window, mark) if rule.count else 0
    for candidate in _candidates(rule, window, last, _monthly_anchor(start)):
        if rule.count and spent + len(results) >= rule.count:
            break
        if not _allowed(rule, candidate, mark):
            continue
        results.append(candidate)
        if len(results) >= EXPANSION_CAP:
            break
    return results


# -- grid marks ------------------------------------------------------------
def recurring_occurrences(event: Event, horizon_days: int = 400) -> List[dt.date]:
    """Start dates of a repeating event, for marking the month grid.

    Walks from DTSTART for ``horizon_days``, capped by ``EXPANSION_CAP``, which
    covers roughly a year of marks without letting an open-ended daily rule
    spin.
    """
    if not event.rrule:
        return []
    start = event.start.date()
    return _walk(event, start, start + dt.timedelta(days=horizon_days))


# -- agenda ----------------------------------------------------------------
def range_for(anchor: dt.date, scope: str) -> Tuple[dt.date, dt.date]:
    """The inclusive ``(first, last)`` day window for ``scope`` around ``anchor``.

    Week means Monday through Sunday, matching the grid's column order.
    """
    if scope == "day":
        return anchor, anchor
    if scope == "week":
        first = anchor - dt.timedelta(days=anchor.weekday())
        return first, first + dt.timedelta(days=6)
    if scope == "month":
        last_day = calendar.monthrange(anchor.year, anchor.month)[1]
        return anchor.replace(day=1), anchor.replace(day=last_day)
    raise ValueError("unknown scope: {0!r}".format(scope))


def _touched_days(event: Event, first: dt.date, last: dt.date) -> List[dt.date]:
    """Days a non-recurring event occupies inside the window.

    A multi-day event appears once, on the first day of the window it touches,
    rather than repeating on every day it spans. Listing a three-day trip three
    times in a week view reads as three separate plans.
    """
    if event.duration > dt.timedelta(days=1):
        span_start = event.start.date()
        span_end = (event.end - dt.timedelta(microseconds=1)).date()
        if span_end < first or span_start > last:
            return []
        return [max(span_start, first)]
    start = event.start.date()
    return [start] if first <= start <= last else []


def occurrences_in_range(
    events: Iterable[Event], first: dt.date, last: dt.date
) -> List[Tuple[Event, dt.date]]:
    """Every ``(event, day)`` landing in ``[first, last]``, sorted by day.

    Recurring events contribute one entry per occurrence, so the caller can
    show "Wed 09:00 Team sync" rather than a single row that pretends to be the
    only time it happens.
    """
    if last < first:
        return []
    rows: List[Tuple[Event, dt.date]] = []
    for event in events:
        if event.rrule:
            for day in _walk(event, first, last):
                rows.append((event, day))
        else:
            for day in _touched_days(event, first, last):
                rows.append((event, day))
    rows.sort(key=lambda row: (row[1], row[0].start.timetz(), row[0].summary))
    return rows


def instance_span(event: Event, day: dt.date) -> Tuple[dt.datetime, dt.datetime]:
    """Wall-clock start/end for the occurrence of ``event`` that falls on ``day``.

    Every occurrence keeps DTSTART's time of day, so shifting the date across a
    month or week boundary cannot drag an event off its usual hour.
    """
    start = dt.datetime.combine(day, _mark_time(event))
    return start, start + event.duration