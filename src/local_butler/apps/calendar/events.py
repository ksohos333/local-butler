"""Calendar event model plus RFC 5545 (iCalendar) serialization and parsing.

Events are stored by Radicale as plain ``.ics`` files, so this module owns the
conversion in both directions. It is deliberately dependency-free: only the
standard library is used, so the calendar keeps working offline.

Wall-clock times are written with an explicit ``TZID`` (IANA zone) rather than
UTC offsets, which is what makes recurring events land at the same local time
across daylight-saving changes.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

PRODID = "-//Local Butler//Calendar//EN"
MAX_LINE_OCTETS = 75

FREQUENCIES = ("DAILY", "WEEKLY", "MONTHLY", "YEARLY")
WEEKDAYS = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")


class EventError(ValueError):
    """Raised when an event cannot be built or understood."""


def local_timezone() -> dt.tzinfo:
    """Resolve the workstation's own timezone, preferring its IANA name."""
    name = _iana_name()
    if name:
        try:
            return ZoneInfo(name)
        except ZoneInfoNotFoundError:
            pass
    return dt.datetime.now().astimezone().tzinfo or dt.timezone.utc


def iana_name(tz: Optional[dt.tzinfo] = None) -> Optional[str]:
    """Best-effort IANA zone name for a tzinfo, or for the system zone.

    Rocky/Fedora symlink ``/etc/localtime`` into ``/usr/share/zoneinfo`` and have
    no ``/etc/timezone``, so that symlink is the reliable source.
    """
    if isinstance(tz, ZoneInfo):
        return tz.key
    import os

    for candidate in (os.environ.get("TZ"), _read_first_line("/etc/timezone")):
        if candidate and _is_known_zone(candidate):
            return candidate
    try:
        target = os.readlink("/etc/localtime")
    except OSError:
        target = ""
    marker = "/usr/share/zoneinfo/"
    if marker in target:
        name = target.split(marker, 1)[1]
        if _is_known_zone(name):
            return name
    current = dt.datetime.now().astimezone().tzinfo
    if isinstance(current, ZoneInfo):
        return current.key
    return None


def _read_first_line(path: str) -> Optional[str]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.readline().strip() or None
    except OSError:
        return None


def _is_known_zone(name: str) -> bool:
    try:
        ZoneInfo(name)
        return True
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return False


def _iana_name() -> Optional[str]:
    return iana_name(None)


# -- text helpers ----------------------------------------------------------
def escape_text(value: str) -> str:
    """Escape a value for an iCalendar TEXT property."""
    out = value.replace("\\", "\\\\")
    out = out.replace(";", "\\;").replace(",", "\\,")
    out = out.replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "\\n")
    return out


def unescape_text(value: str) -> str:
    out = value.replace("\\n", "\n").replace("\\N", "\n")
    return out.replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\")


def fold(line: str) -> List[str]:
    """Fold a content line to 75 octets, per RFC 5545."""
    raw = line.encode("utf-8")
    if len(raw) <= MAX_LINE_OCTETS:
        return [line]
    chunks: List[str] = []
    current = b""
    for char in line:
        encoded = char.encode("utf-8")
        limit = MAX_LINE_OCTETS if not chunks else MAX_LINE_OCTETS - 1
        if len(current) + len(encoded) > limit:
            chunks.append(current.decode("utf-8"))
            current = b""
        current += encoded
    if current:
        chunks.append(current.decode("utf-8"))
    return [chunks[0]] + [" " + chunk for chunk in chunks[1:]]


def unfold(text: str) -> List[str]:
    """Undo line folding, returning logical content lines.

    RFC 5545 folds long values across lines by prefixing each continuation with
    a single space. Those continuations must be rejoined before parsing or a
    long SUMMARY silently loses text.
    """
    text = text.replace("\r\n", "\n")
    lines: List[str] = []
    for raw in text.split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    return lines


# -- the event -------------------------------------------------------------
@dataclass
class Event:
    """A single calendar event.

    ``start``/``end`` are naive local wall-clock values when ``all_day`` is
    unset; ``tz`` decides how they are serialized.
    """

    summary: str
    start: dt.datetime
    end: Optional[dt.datetime] = None
    uid: Optional[str] = None
    description: str = ""
    location: str = ""
    all_day: bool = False
    tz: dt.tzinfo = field(default_factory=local_timezone)
    rrule: str = ""
    reminder_minutes: Optional[int] = None
    created_at: Optional[dt.datetime] = None
    etag: Optional[str] = None
    href: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.summary or not self.summary.strip():
            raise EventError("event needs a summary")
        if self.end is None:
            if self.all_day:
                self.end = self.start + dt.timedelta(days=1)
            else:
                self.end = self.start + dt.timedelta(hours=1)
        if self.end < self.start:
            raise EventError("event ends before it starts")
        if not self.uid:
            self.uid = "{0}@local-butler".format(uuid4())
        if not self.created_at:
            self.created_at = dt.datetime.now(dt.timezone.utc)
        if self.reminder_minutes is not None and self.reminder_minutes < 0:
            raise EventError("reminder cannot be negative")
        if self.rrule:
            validate_rrule(self.rrule)

    # -- convenience ------------------------------------------------------
    @property
    def duration(self) -> dt.timedelta:
        return self.end - self.start

    def overlaps(self, other: "Event") -> bool:
        """True when this event shares any time with ``other``.

        Recurring events are approximated by their first occurrence; that is
        enough for the single-machine, single-calendar case this app targets.
        """
        return self.start < other.end and other.start < self.end

    def with_href(self, href: str, etag: Optional[str] = None) -> "Event":
        self.href = href
        self.etag = etag
        return self

    # -- serialization ----------------------------------------------------
    def _stamp(self, value: dt.datetime, *, all_day: bool = False) -> str:
        if all_day:
            return value.strftime("%Y%m%d")
        return value.strftime("%Y%m%dT%H%M%S")

    def to_ics(self) -> str:
        """Render this event as a single-VEVENT iCalendar document (CRLF)."""
        zone = iana_name(self.tz) or "UTC"
        lines: List[str] = [
            "BEGIN:VCALENDAR",
            "VERSION:2.0",
            "PRODID:{0}".format(PRODID),
            "CALSCALE:GREGORIAN",
            "BEGIN:VEVENT",
            "UID:{0}".format(self.uid),
            "DTSTAMP:{0}".format(
                self.created_at.astimezone(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            ),
        ]
        if self.all_day:
            lines.append("DTSTART;VALUE=DATE:{0}".format(self._stamp(self.start, all_day=True)))
            lines.append("DTEND;VALUE=DATE:{0}".format(self._stamp(self.end, all_day=True)))
        else:
            lines.append("DTSTART;TZID={0}:{1}".format(zone, self._stamp(self.start)))
            lines.append("DTEND;TZID={0}:{1}".format(zone, self._stamp(self.end)))
        lines.append("SUMMARY:{0}".format(escape_text(self.summary)))
        if self.description:
            lines.append("DESCRIPTION:{0}".format(escape_text(self.description)))
        if self.location:
            lines.append("LOCATION:{0}".format(escape_text(self.location)))
        if self.rrule:
            lines.append("RRULE:{0}".format(self.rrule))
        if self.reminder_minutes is not None:
            lines.extend(self._alarm_lines())
        lines.extend(["END:VEVENT", "END:VCALENDAR"])

        folded: List[str] = []
        for line in lines:
            folded.extend(fold(line))
        return "\r\n".join(folded) + "\r\n"

    def _alarm_lines(self) -> List[str]:
        minutes = int(self.reminder_minutes or 0)
        trigger = "-PT{0}M".format(minutes) if minutes else "PT0M"
        return [
            "BEGIN:VALARM",
            "ACTION:DISPLAY",
            "DESCRIPTION:{0}".format(escape_text(self.summary)),
            "TRIGGER:{0}".format(trigger),
            "END:VALARM",
        ]


def parse_until(raw: str, tz: Optional[dt.tzinfo] = None) -> Optional[dt.datetime]:
    """Parse an RRULE ``UNTIL`` value into a naive local datetime bound.

    RFC 5545 makes ``UNTIL`` inclusive, so a DATE-only value covers the whole
    of that day. A value ending in ``Z`` is UTC and is converted into ``tz``
    before comparison, since event start times are naive local wall clock.
    Returns ``None`` when the value is absent or malformed.
    """
    text = (raw or "").strip().upper()
    if not text:
        return None
    is_utc = text.endswith("Z")
    if is_utc:
        text = text[:-1]
    if "T" in text:
        fmt = "%Y%m%dT%H%M%S"
    else:
        fmt = "%Y%m%d"
    try:
        moment = dt.datetime.strptime(text, fmt)
    except ValueError:
        return None
    if "T" not in text:
        moment = moment.replace(hour=23, minute=59, second=59, microsecond=999999)
    if is_utc:
        if tz is None:
            return None
        moment = moment.replace(tzinfo=dt.timezone.utc).astimezone(tz).replace(tzinfo=None)
    return moment


def validate_rrule(rrule: str) -> str:
    """Validate the supported subset of RRULE, returning it normalized."""
    text = (rrule or "").strip()
    if text.upper().startswith("RRULE:"):
        text = text[6:]
    if not text:
        raise EventError("empty recurrence rule")
    parts = {}
    for chunk in text.split(";"):
        if not chunk:
            continue
        if "=" not in chunk:
            raise EventError("bad RRULE part: {0}".format(chunk))
        key, _, value = chunk.partition("=")
        parts[key.strip().upper()] = value.strip()
    if "FREQ" not in parts:
        raise EventError("RRULE needs FREQ")
    freq = parts["FREQ"].upper()
    if freq not in FREQUENCIES:
        raise EventError(
            "unsupported FREQ {0}; use one of {1}".format(
                freq, ", ".join(FREQUENCIES)
            )
        )
    parts["FREQ"] = freq
    if "BYDAY" in parts:
        days = [d.strip().upper() for d in parts["BYDAY"].split(",") if d.strip()]
        for day in days:
            prefix = day.rstrip("0123456789")
            if prefix not in WEEKDAYS:
                raise EventError("bad BYDAY value: {0}".format(day))
        parts["BYDAY"] = ",".join(days)
    if "COUNT" in parts:
        try:
            count = int(parts["COUNT"])
        except ValueError:
            raise EventError("COUNT must be a number") from None
        if count < 1:
            raise EventError("COUNT must be positive")
        parts["COUNT"] = str(count)
    if "UNTIL" in parts:
        if parse_until(parts["UNTIL"], dt.timezone.utc) is None:
            raise EventError("UNTIL must be YYYYMMDD or YYYYMMDDTHHMMSS[Z]")
        parts["UNTIL"] = parts["UNTIL"].strip().upper()
    return ";".join(
        "{0}={1}".format(key, parts[key]) for key in sorted(parts)
    )


# -- parsing ---------------------------------------------------------------
_PROP_RE = re.compile(r"^(?P<name>[A-Za-z-]+)(?P<params>(?:;[^:]*)?):(?P<value>.*)$")


def parse_ics(ics: str, default_tz: Optional[dt.tzinfo] = None) -> List[Event]:
    """Parse every VEVENT in an iCalendar document into :class:`Event` objects."""
    zone = default_tz or local_timezone()
    events: List[Event] = []
    current: Optional[Dict[str, object]] = None
    in_alarm = False
    reminder: Optional[int] = None
    for raw in unfold(ics):
        if not raw.strip():
            continue
        line = raw.strip()
        if line == "BEGIN:VEVENT":
            current = {}
            in_alarm = False
            reminder = None
            continue
        if line == "END:VEVENT":
            if current is not None:
                event = _event_from_props(current, zone, reminder)
                if event is not None:
                    events.append(event)
            current = None
            in_alarm = False
            reminder = None
            continue
        if line == "BEGIN:VALARM":
            in_alarm = True
            continue
        if line == "END:VALARM":
            in_alarm = False
            continue
        if current is None:
            continue
        match = _PROP_RE.match(line)
        if not match:
            continue
        name = match.group("name").upper()
        params = match.group("params") or ""
        value = match.group("value").strip()
        if in_alarm:
            if name == "TRIGGER":
                reminder = _parse_trigger(value)
            continue
        if name == "SUMMARY":
            current["summary"] = unescape_text(value)
        elif name == "DESCRIPTION":
            current["description"] = unescape_text(value)
        elif name == "LOCATION":
            current["location"] = unescape_text(value)
        elif name == "UID":
            current["uid"] = value
        elif name == "RRULE":
            current["rrule"] = value
        elif name == "DTSTAMP":
            current["dtstamp"] = value
        elif name == "DTSTART":
            current["dtstart"] = (value, "VALUE=DATE" in params.upper(), _tz_param(params, zone))
        elif name == "DTEND":
            current["dtend"] = (value, "VALUE=DATE" in params.upper(), _tz_param(params, zone))
    return events


def _tz_param(params: str, fallback: dt.tzinfo) -> dt.tzinfo:
    match = re.search(r"TZID=([^;:]+)", params or "")
    if match:
        try:
            return ZoneInfo(match.group(1).strip())
        except ZoneInfoNotFoundError:
            return fallback
    return fallback


def _parse_trigger(value: str) -> Optional[int]:
    """VALARM TRIGGER -> whole minutes of lead time before the start.

    A negative (``-PT30M``) or positive (``PT15M``) trigger both mean "remind me
    ahead of the event", so the sign is dropped and only the lead time is kept.
    Absolute triggers (``20261001T090000Z``) are not supported and yield None.
    """
    text = value.strip().upper()
    match = re.match(r"^(-?)PT(?:(\d+)H)?(?:(\d+)M)?$", text)
    if not match:
        return None
    _, hours, minutes = match.groups()
    return int(hours or 0) * 60 + int(minutes or 0)


def _parse_stamp(value: str, all_day: bool, tz: dt.tzinfo) -> dt.datetime:
    """Parse an iCalendar date-time into a *naive local wall clock* value.

    Event start/end are always naive local times and ``Event.tz`` decides how
    they are serialized. Keeping every datetime naive means two events can be
    compared for overlap without any awareness juggling.
    """
    if all_day:
        return dt.datetime.strptime(value, "%Y%m%d")
    if value.endswith("Z"):
        utc = dt.datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(
            tzinfo=dt.timezone.utc
        )
        return utc.astimezone(tz).replace(tzinfo=None)
    return dt.datetime.strptime(value, "%Y%m%dT%H%M%S")


def _event_from_props(
    props: Dict[str, object], zone: dt.tzinfo, reminder: Optional[int]
) -> Optional[Event]:
    if "dtstart" not in props:
        return None
    start_value, all_day, start_tz = props["dtstart"]  # type: ignore[misc]
    start = _parse_stamp(start_value, all_day, start_tz)
    if "dtend" in props:
        end_value, end_all_day, end_tz = props["dtend"]  # type: ignore[misc]
        end = _parse_stamp(end_value, end_all_day, end_tz)
    else:
        end = start + dt.timedelta(days=1 if all_day else 1)
    stamp = props.get("dtstamp")
    created = None
    if stamp:
        try:
            created = _parse_stamp(str(stamp), False, dt.timezone.utc)
        except ValueError:
            created = None
    return Event(
        summary=str(props.get("summary", "(untitled)")),
        start=start,
        end=end,
        uid=props.get("uid"),  # type: ignore[arg-type]
        description=str(props.get("description", "")),
        location=str(props.get("location", "")),
        all_day=all_day,
        tz=start_tz,
        rrule=str(props.get("rrule", "")),
        reminder_minutes=reminder,
        created_at=created,
    )


def overlaps_any(events: Sequence[Event], candidate: Event) -> List[Event]:
    """Return the events that clash with ``candidate``."""
    return [event for event in events if event.overlaps(candidate)]


def describe(event: Event) -> str:
    """A short human line, used by the GTK app and CLI output."""
    if event.all_day:
        when = event.start.strftime("%a %d %b %Y (all day)")
    else:
        when = event.start.strftime("%a %d %b %Y %H:%M")
        if event.duration >= dt.timedelta(minutes=1):
            when += "-{0}".format(event.end.strftime("%H:%M"))
    repeat = " [repeats]" if event.rrule else ""
    alarm = ""
    if event.reminder_minutes is not None:
        alarm = " (remind {0}m)".format(event.reminder_minutes)
    return "{0}  {1}{2}{3}".format(when, event.summary, repeat, alarm)
