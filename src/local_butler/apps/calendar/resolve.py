"""Deterministic reading of scheduling language.

An 8B model is good at "this note is a dentist appointment" and bad at "the
Tuesday after the 30th is the 6th". Everything that involves counting or
calendar arithmetic is therefore done here in plain Python; the model is only
asked for wording, and only used as a fallback for the parts it can be trusted
with.

Every function is pure and takes the reference ``now`` explicitly, so behaviour
is reproducible in tests and at any time of day.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Optional, Tuple

MONTHS = {
    "january": 1, "jan": 1,
    "february": 2, "feb": 2,
    "march": 3, "mar": 3,
    "april": 4, "apr": 4,
    "may": 5,
    "june": 6, "jun": 6,
    "july": 7, "jul": 7,
    "august": 8, "aug": 8,
    "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10,
    "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}

WEEKDAYS = {
    "monday": 0, "mon": 0,
    "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2, "wednesday": 2,
    "thursday": 3, "thu": 3, "thur": 3, "thurs": 3,
    "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}

WEEKDAY_CODES = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")

_ALL_DAY_WORDS = re.compile(
    r"\b(all[- ]day|whole day|full day|day off|public holiday|bank holiday|"
    r"vacation all day)\b",
    re.IGNORECASE,
)


def all_day_requested(text: str) -> Optional[bool]:
    """True/False when the note explicitly says all-day, else None."""
    if _ALL_DAY_WORDS.search(text or ""):
        return True
    if re.search(r"\bnot all[- ]day\b", text or "", re.IGNORECASE):
        return False
    return None


def relative_day(text: str) -> Optional[int]:
    """Day offset for "today"/"tomorrow"/"day after tomorrow", else None."""
    lowered = (text or "").lower()
    if re.search(r"\bday after tomorrow\b", lowered):
        return 2
    if re.search(r"\b(tomorrow|tmrw|tmr)\b", lowered):
        return 1
    if re.search(r"\b(today|tonight|this afternoon|this morning|this evening)\b", lowered):
        return 0
    if re.search(r"\bnext week\b", lowered):
        return 7
    return None


def weekday(text: str) -> Optional[int]:
    """Weekday index (Mon=0) named in the note, preferring the longest match."""
    lowered = (text or "").lower()
    found: Optional[Tuple[int, int]] = None
    for word, index in WEEKDAYS.items():
        if re.search(r"\b{0}\b".format(re.escape(word)), lowered):
            if found is None or len(word) > found[0]:
                found = (len(word), index)
    return found[1] if found else None


_TIME_RE = re.compile(
    r"\b(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<ampm>am|pm|a\.m\.|p\.m\.|o'clock)?\b",
    re.IGNORECASE,
)


def time_of_day(text: str) -> Optional[Tuple[int, int]]:
    """Clock time in the note, e.g. "3pm" or "09:30" -> (15, 0)."""
    best: Optional[Tuple[int, int]] = None
    for match in _TIME_RE.finditer(text or ""):
        hour_text = match.group("hour")
        minute_text = match.group("minute")
        suffix = (match.group("ampm") or "").replace(".", "").lower()
        # Ignore things that are plainly not a clock time: dates, years, "top 5".
        hour = int(hour_text)
        if hour > 23 and not suffix:
            continue
        if minute_text is None and not suffix:
            if not re.match(r"^\s*at\s*$", (text or "")[max(0, match.start() - 3) : match.start()]):
                continue
        minute = int(minute_text) if minute_text else 0
        if minute > 59:
            continue
        if suffix in ("pm", "p"):
            if hour < 12:
                hour += 12
        elif suffix in ("am", "a"):
            if hour == 12:
                hour = 0
        if suffix or minute_text:
            best = (hour, minute)
    return best


def explicit_date(text: str, now: dt.datetime) -> Optional[dt.date]:
    """A calendar date stated outright, e.g. "December 24th" or "2026-12-24"."""
    if not text:
        return None
    lowered = text.lower()

    iso = re.search(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", text)
    if iso:
        try:
            return dt.date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))
        except ValueError:
            return None

    slashed = re.search(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b", text)
    if slashed:
        day, month = int(slashed.group(1)), int(slashed.group(2))
        year = int(slashed.group(3)) if slashed.group(3) else now.year
        if year < 100:
            year += 2000
        if 1 <= month <= 12 and 1 <= day <= 31:
            try:
                return dt.date(year, month, day)
            except ValueError:
                pass

    month_names = "|".join(sorted(MONTHS, key=len, reverse=True))
    named = re.search(
        r"\b(?P<month>{0})\.?\s+(?P<day>\d{{1,2}})(?:st|nd|rd|th)?\b".format(month_names),
        lowered,
    )
    if named:
        month = MONTHS[named.group("month")]
        day = int(named.group("day"))
        year = now.year
        try:
            candidate = dt.date(year, month, day)
        except ValueError:
            return None
        if candidate < now.date() - dt.timedelta(days=1):
            candidate = dt.date(year + 1, month, day)
        return candidate

    day_first = re.search(
        r"\b(?P<day>\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?(?P<month>{0})\b".format(month_names),
        lowered,
    )
    if day_first:
        month = MONTHS[day_first.group("month")]
        day = int(day_first.group("day"))
        year = now.year
        try:
            candidate = dt.date(year, month, day)
        except ValueError:
            return None
        if candidate < now.date() - dt.timedelta(days=1):
            candidate = dt.date(year + 1, month, day)
        return candidate
    return None


def next_weekday(now: dt.datetime, target: int, allow_today: bool = True) -> dt.date:
    """The next date falling on ``target`` (Mon=0), relative to ``now``."""
    today = now.date()
    if allow_today and today.weekday() == target:
        return today
    for offset in range(1, 8):
        candidate = today + dt.timedelta(days=offset)
        if candidate.weekday() == target:
            return candidate
    return today + dt.timedelta(days=(target - today.weekday()) % 7 or 7)


def repeat_rule(text: str) -> Optional[str]:
    """An RRULE when the note clearly asks for repetition, else None."""
    lowered = (text or "").lower()
    if not re.search(r"\b(every|each|weekly|daily|monthly|yearly|recurring|repeats?)\b", lowered):
        return None

    days = set()
    for word, index in WEEKDAYS.items():
        if re.search(r"\b{0}\b".format(re.escape(word)), lowered):
            days.add(index)
    if re.search(r"\b(weekday|weekdays|working days|workdays)\b", lowered):
        days |= {0, 1, 2, 3, 4}
    if re.search(r"\bweekend\b", lowered):
        days |= {5, 6}

    if days and re.search(r"\b(every|each|weekly)\b", lowered):
        codes = ",".join(WEEKDAY_CODES[index] for index in sorted(days))
        return "BYDAY={0};FREQ=WEEKLY".format(codes)
    if re.search(r"\b(monthly|every month)\b", lowered):
        return "FREQ=MONTHLY"
    if re.search(r"\b(yearly|annually|every year)\b", lowered):
        return "FREQ=YEARLY"
    if re.search(r"\b(daily|every day|each day)\b", lowered):
        return "FREQ=DAILY"
    if re.search(r"\bweekly\b", lowered):
        return "FREQ=WEEKLY"
    return None


def reminder_minutes(text: str) -> Optional[int]:
    """Lead time asked for in the note ("remind me 1 hour before")."""
    lowered = (text or "").lower()
    match = re.search(
        r"remind(?: me)?[^0-9]{0,20}(\d+)\s*(minute|min|hour|hr|day|week)", lowered
    )
    if not match:
        return None
    amount = int(match.group(1))
    unit = match.group(2)
    if unit.startswith("hour") or unit == "hr":
        return amount * 60
    if unit.startswith("day") or unit.startswith("week"):
        return amount * 1440
    return amount


def resolve_start(
    text: str,
    now: dt.datetime,
    model_time: Optional[Tuple[int, int]] = None,
    default_hour: int = 9,
) -> dt.datetime:
    """Work out when the note refers to, without trusting the model's arithmetic.

    Precedence: an explicit date, then a relative day word, then a named
    weekday, then "later today if the time still fits, otherwise tomorrow".
    """
    clock = time_of_day(text) or model_time

    day: Optional[dt.date] = explicit_date(text, now)
    if day is not None:
        hour, minute = clock if clock else (default_hour, 0)
        return dt.datetime.combine(day, dt.time(hour, minute))

    offset = relative_day(text)
    if offset is not None:
        day = (now + dt.timedelta(days=offset)).date()
        hour, minute = clock if clock else (default_hour, 0)
        return dt.datetime.combine(day, dt.time(hour, minute))

    named = weekday(text)
    if named is not None:
        # "tuesday at 3pm" said at 23:30 means this coming Tuesday, not next week's.
        today_is_target = now.date().weekday() == named
        day = next_weekday(now, named, allow_today=today_is_target and clock is not None)
        candidate = dt.datetime.combine(day, dt.time(clock[0], clock[1]))
        if candidate < now:
            candidate += dt.timedelta(days=7)
        return candidate

    if clock is not None:
        today = now.replace(hour=clock[0], minute=clock[1], second=0, microsecond=0)
        if today >= now:
            return today
        return (now + dt.timedelta(days=1)).replace(
            hour=clock[0], minute=clock[1], second=0, microsecond=0
        )
    return (now + dt.timedelta(days=1)).replace(
        hour=default_hour, minute=0, second=0, microsecond=0
    )
