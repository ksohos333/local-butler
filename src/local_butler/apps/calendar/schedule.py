"""Scheduling service: turns plain language into real events on the CalDAV server.

This is the piece the old calendar app was missing. Radicale can store events and
GNOME Calendar can display them, but nothing could actually *create* one, so the
calendar was read-only in practice.

Two ways in:

* :meth:`ScheduleService.from_text` -- "dentist tuesday 3pm" becomes an event,
  resolved against the current local time by the local llama.cpp model.
* :func:`build_event` / :meth:`ScheduleService.create` -- structured input from
  code or another interface.

Writes go through CalDAV with ETags, so a change made in GNOME Calendar is never
silently clobbered.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Sequence

from local_butler.apps.calendar.caldav import CalDAVClient, CalDAVError
from local_butler.apps.calendar.events import (
    Event,
    EventError,
    describe,
    overlaps_any,
    parse_ics,
    validate_rrule,
)

DEFAULT_LLM_URL = "http://127.0.0.1:8080"
DEFAULT_CALDAV_URL = "http://127.0.0.1:5232"

EXTRACTION_SYSTEM = (
    "You convert a person's scheduling note into a single JSON object. "
    "Never explain, never add prose, never wrap the JSON in prose. "
    "Reply with one JSON object and nothing else."
)

EXTRACTION_TEMPLATE = """Convert this scheduling note into one JSON object:
"{text}"

Reply with exactly these keys and nothing else:
{{
  "summary": "short title, at most 8 words",
  "time_of_day": "HH:MM if the note states a clock time, otherwise null",
  "all_day": true if the note is for a whole day, otherwise false,
  "duration_minutes": integer length in minutes,
  "location": "string or empty string",
  "description": "string or empty string",
  "reminder_minutes": integer minutes of warning the note asks for, or null
}}

Rules:
- summary describes WHAT it is, never the date: "Dentist Appointment", not
  "Dentist Appointment on Tuesday".
- Copy names, places and titles exactly as the note writes them, keeping their
  capital letters: "Coffee with Maria", not "Coffee with maria".
- Leave time_of_day null if no clock time is mentioned. The date is worked out
  separately, so never output a date field.
- Use 60 for duration_minutes unless the note implies something else; use 1440
  for a whole day.
- reminder_minutes is null when the note says nothing about reminding.
- If the note cannot be scheduled at all, reply {{"error": "reason"}}."""


class LLMUnavailable(RuntimeError):
    """Raised when the local model cannot be reached."""


def local_now(tz: Optional[dt.tzinfo] = None) -> dt.datetime:
    """Current local wall-clock time, naive, to match :class:`Event`."""
    from local_butler.apps.calendar.events import local_timezone

    return dt.datetime.now(tz or local_timezone()).replace(microsecond=0, tzinfo=None)


class LocalLLM:
    """Tiny client for the local llama.cpp OpenAI-compatible endpoint."""

    def __init__(self, base_url: str = DEFAULT_LLM_URL, model: str = "", timeout: float = 120.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout

    def available(self) -> bool:
        try:
            request = urllib.request.Request(self.base_url + "/health")
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status == 200
        except urllib.error.HTTPError:
            return False
        except urllib.error.URLError:
            return False

    def first_model(self) -> str:
        if self.model:
            return self.model
        try:
            request = urllib.request.Request(self.base_url + "/v1/models")
            with urllib.request.urlopen(request, timeout=5) as response:
                data = json.loads(response.read().decode("utf-8"))
            entries = data.get("data") or []
            if entries:
                return str(entries[0].get("id", ""))
        except (urllib.error.URLError, ValueError, OSError):
            pass
        return "local-model"

    def chat(self, system: str, user: str) -> str:
        """Run one completion with Qwen3 thinking disabled."""
        model = self.first_model()
        if not model:
            raise LLMUnavailable("no model served by {0}".format(self.base_url))
        payload = json.dumps(
            {
                "model": model,
                "messages": [
                    {"role": "system", "content": system},
                    # /no_think keeps the reasoning model from spending the whole
                    # token budget on hidden thinking, which otherwise returns
                    # empty content.
                    {"role": "user", "content": "/no_think\n" + user},
                ],
                "temperature": 0.2,
                "max_tokens": 1024,
                "stream": False,
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + "/v1/chat/completions",
            data=payload,
            method="POST",
        )
        request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:200]
            raise LLMUnavailable("llama-server returned {0}: {1}".format(exc.code, detail)) from exc
        except urllib.error.URLError as exc:
            raise LLMUnavailable("cannot reach {0}: {1}".format(self.base_url, exc.reason)) from exc
        choices = data.get("choices") or []
        if not choices:
            raise LLMUnavailable("llama-server returned no choices")
        message = choices[0].get("message") or {}
        text = (message.get("content") or "").strip()
        if not text and message.get("reasoning_content"):
            # Thinking-only reply: keep the tail of the reasoning so the caller
            # can still try to parse it instead of returning nothing.
            text = str(message["reasoning_content"]).strip()
        if not text:
            raise LLMUnavailable("model returned empty content")
        return text


def extract_json(text: str) -> Dict[str, Any]:
    """Pull the first JSON object out of a model reply.

    Small models like to wrap JSON in code fences or add a sentence, so this is
    deliberately forgiving; :func:`build_event` still validates the result.
    """
    cleaned = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", cleaned, re.DOTALL)
    if fence:
        cleaned = fence.group(1).strip()
    start = cleaned.find("{")
    if start == -1:
        raise EventError("model reply contained no JSON: {0!r}".format(text[:200]))
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(cleaned)):
        char = cleaned[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                blob = cleaned[start : index + 1]
                try:
                    parsed = json.loads(blob)
                except ValueError as exc:
                    raise EventError("model reply was not valid JSON: {0}".format(exc)) from exc
                if not isinstance(parsed, dict):
                    raise EventError("model reply was not a JSON object")
                return parsed
    raise EventError("model reply had an unterminated JSON object")


def build_event(
    payload: Dict[str, Any],
    now: Optional[dt.datetime] = None,
    source_text: str = "",
) -> Event:
    """Turn a model payload into a validated :class:`Event`.

    When ``source_text`` is supplied the note itself decides the date, the
    all-day flag, the repeat rule and the reminder, because deterministic
    parsing of those is far more trustworthy than a small model. The model is
    left responsible for wording only: summary, location, description, duration.
    """
    from local_butler.apps.calendar import resolve
    from local_butler.apps.calendar.events import local_timezone

    if payload.get("error"):
        raise EventError("cannot schedule: {0}".format(payload["error"]))
    now = now or local_now()
    zone = local_timezone()

    summary = str(payload.get("summary", "")).strip()
    if not summary:
        raise EventError("missing summary")
    summary = _tidy_summary(summary)

    model_time = _parse_clock(payload.get("time_of_day"))
    if source_text:
        start = resolve.resolve_start(source_text, now, model_time=model_time)
    else:
        raw_start = str(payload.get("start", "")).strip()
        if not raw_start:
            raise EventError("missing start time")
        start = _parse_iso(raw_start, zone)
    if start < now - dt.timedelta(minutes=1):
        raise EventError(
            "start {0} is in the past (now is {1})".format(start, now)
        )

    all_day = bool(payload.get("all_day", False))
    if source_text:
        stated_all_day = resolve.all_day_requested(source_text)
        if stated_all_day is not None:
            all_day = stated_all_day
    if all_day:
        start = start.replace(hour=0, minute=0, second=0, microsecond=0)

    minutes = payload.get("duration_minutes")
    if all_day:
        # An all-day event lasts exactly one day; ignore whatever the model said.
        end = start + dt.timedelta(days=1)
    else:
        if minutes is None:
            minutes = 60
        try:
            minutes = int(minutes)
        except (TypeError, ValueError):
            raise EventError("duration_minutes must be a number") from None
        if minutes <= 0:
            raise EventError("duration must be positive")
        end = start + dt.timedelta(minutes=minutes)

    rrule = str(payload.get("rrule", "") or "").strip()
    if source_text:
        stated_rrule = resolve.repeat_rule(source_text)
        if stated_rrule:
            rrule = stated_rrule
    if rrule:
        rrule = validate_rrule(rrule)

    reminder = payload.get("reminder_minutes")
    if source_text:
        stated_reminder = resolve.reminder_minutes(source_text)
        if stated_reminder is not None:
            reminder = stated_reminder
    if reminder is not None:
        try:
            reminder = int(reminder)
        except (TypeError, ValueError):
            raise EventError("reminder_minutes must be a number or null") from None
        if reminder < 0 or reminder > 10080:
            raise EventError("reminder_minutes out of range")

    return Event(
        summary=summary,
        start=start,
        end=end,
        all_day=all_day,
        description=str(payload.get("description", "") or ""),
        location=str(payload.get("location", "") or ""),
        rrule=rrule,
        reminder_minutes=reminder,
        tz=zone,
    )


def _tidy_summary(summary: str) -> str:
    """Normalise a model-written title without mangling capitals it chose.

    Small models often answer in all lower case; only a wholly lowercase title
    is touched, so "iOS release" and "AI sync" are left exactly as written.
    """
    text = summary.strip()
    if text and text == text.lower():
        text = text[0].upper() + text[1:]
    return text


def _parse_clock(value: Any) -> Optional[tuple]:
    """Read the model's ``time_of_day`` field into an ``(hour, minute)`` pair."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return None
    text = str(value).strip()
    match = re.match(r"^(\d{1,2})[:.]?(\d{2})?\s*(am|pm)?$", text, re.IGNORECASE)
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    suffix = (match.group(3) or "").lower()
    if suffix == "pm" and hour < 12:
        hour += 12
    if suffix == "am" and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        return None
    return (hour, minute)


def _parse_iso(value: str, zone: dt.tzinfo) -> dt.datetime:
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        for pattern in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
            try:
                parsed = dt.datetime.strptime(value.strip(), pattern)
                break
            except ValueError:
                continue
        else:
            raise EventError("cannot understand start time: {0!r}".format(value)) from None
    if parsed.tzinfo is not None:
        return parsed.astimezone(zone).replace(tzinfo=None, second=parsed.second)
    return parsed.replace(microsecond=0)


class ScheduleService:
    """Create, list, and delete events on the local CalDAV server."""

    def __init__(
        self,
        client: Optional[CalDAVClient] = None,
        llm: Optional[LocalLLM] = None,
    ) -> None:
        self.client = client or CalDAVClient()
        self.llm = llm or LocalLLM()

    # -- reading ----------------------------------------------------------
    def events(self, start: Optional[str] = None, end: Optional[str] = None) -> List[Event]:
        """All events, sorted by start time."""
        collected: List[Event] = []
        for record in self.client.list_events(start, end):
            for event in parse_ics(record["ics"]):
                event.href = record["href"]
                event.etag = record["etag"]
                collected.append(event)
        collected.sort(key=lambda item: (item.start, item.summary))
        return collected

    def upcoming(self, days: int = 14, now: Optional[dt.datetime] = None) -> List[Event]:
        now = now or local_now()
        horizon = now + dt.timedelta(days=days)
        return [
            event
            for event in self.events()
            if event.end >= now and event.start <= horizon
        ]

    def conflicts(self, candidate: Event) -> List[Event]:
        return overlaps_any(self.events(), candidate)

    # -- writing ----------------------------------------------------------
    def create(self, event: Event, allow_conflict: bool = False) -> Event:
        """Write an event to the calendar.

        Refuses to create a double-booking unless ``allow_conflict`` is set, so a
        mis-heard time does not quietly overwrite real plans.
        """
        if not allow_conflict:
            clashes = self.conflicts(event)
            if clashes:
                names = ", ".join(item.summary for item in clashes[:3])
                raise EventError(
                    "conflicts with: {0}. Pass allow_conflict=True to add anyway.".format(names)
                )
        href = self.client.put_event(event.to_ics(), uid=event.uid)
        event.href = href
        event.etag = self._etag_for(event.uid)
        return event

    def _etag_for(self, uid: str) -> Optional[str]:
        """Look up the ETag Radicale assigned to a freshly written event."""
        try:
            records = self.client.list_events()
        except CalDAVError:
            return None
        for record in records:
            for parsed in parse_ics(record["ics"]):
                if parsed.uid == uid:
                    return record["etag"]
        return None

    def delete(self, event: Event) -> None:
        if not event.href:
            raise CalDAVError("event has no href; cannot delete")
        self.client.delete_event(event.href, if_match=event.etag)

    def delete_by_summary(self, summary: str, now: Optional[dt.datetime] = None) -> int:
        """Delete the next matching upcoming event; returns how many were removed."""
        target = summary.strip().lower()
        matches = [
            event
            for event in self.upcoming(days=3650, now=now)
            if event.summary.strip().lower() == target
        ]
        if not matches:
            raise EventError("no upcoming event named {0!r}".format(summary))
        self.delete(matches[0])
        return 1

    # -- natural language -------------------------------------------------
    def _prompt(self, text: str) -> str:
        return EXTRACTION_TEMPLATE.format(text=text.strip())

    def parse(self, text: str, now: Optional[dt.datetime] = None) -> Event:
        """Parse a note into an :class:`Event` without touching the calendar."""
        if not text or not text.strip():
            raise EventError("say what you want scheduled")
        now = now or local_now()
        reply = self.llm.chat(EXTRACTION_SYSTEM, self._prompt(text))
        return build_event(extract_json(reply), now=now, source_text=text)

    def preview(self, text: str, now: Optional[dt.datetime] = None) -> Event:
        """Alias for :meth:`parse`, so the UI can confirm before committing."""
        return self.parse(text, now=now)

    def from_text(
        self,
        text: str,
        now: Optional[dt.datetime] = None,
        allow_conflict: bool = False,
    ) -> Event:
        """Parse a scheduling note with the local model and create the event."""
        return self.create(self.parse(text, now=now), allow_conflict=allow_conflict)


def format_schedule(events: Sequence[Event]) -> str:
    """Render events as plain-text lines for CLI and GTK use."""
    if not events:
        return "No events scheduled."
    return "\n".join(describe(event) for event in events)
