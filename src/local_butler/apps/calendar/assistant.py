"""The chat assistant behind the calendar's AI panel.

One turn is a small tool loop against the local model. The model is offered
four kinds of tool:

* read-only mail tools proxied to the ``bigus_dickus`` MCP server,
* a read-only view of the calendar,
* analysis of the calendar (clashes, free time, suggested moves) and a web
  search for facts it cannot know,
* calendar *writes*, which are never applied here.

Writes are the important design point. ``create_event``, ``update_event`` and
``delete_event`` return a :class:`PendingChange` describing what *would*
change, and the caller shows that to the user. Only after the user says yes
does :meth:`Assistant.apply` actually touch CalDAV. The model therefore cannot
modify the calendar on its own judgement, no matter what it decides it wants
to do.

Overlapping events are a normal state of this calendar -- two meetings in the
same hour are fine -- so ``create_event`` writes with conflict detection off
and the analysis tools report clashes as information, not as errors.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field, replace
from typing import Any, Dict, List, Optional, Sequence

from local_butler.apps.calendar.caldav import CalDAVError
from local_butler.apps.calendar.events import Event, EventError, describe
from local_butler.apps.calendar.mcpclient import MCPClient, MCPError
from local_butler.apps.calendar.optimize import analyse, find_free_time as _free_time_report
from local_butler.apps.calendar.schedule import (
    LLMUnavailable,
    LocalLLM,
    ScheduleService,
    local_now,
)

MAX_TURNS = 6

SYSTEM = (
    "You are the assistant built into a private calendar app. You run entirely "
    "on the user's own machine and you can read their mail and their calendar.\n\n"
    "How to behave:\n"
    "- Answer in a few short sentences. Plain text, no markdown, no bullet "
    "symbols, no headings.\n"
    "- Use the mail tools when the user refers to something in their email, and "
    "say what you found and when it arrived.\n"
    "- Use list_events to check what is already scheduled before proposing "
    "anything, so you do not double-book them.\n"
    "- Two events may share the same hour; overlapping is allowed on this "
    "calendar, so never refuse a request just because something else is "
    "already on.\n"
    "- Use research when your answer needs something current -- opening hours, "
    "prices, timetables, news. Quote the source with the fact.\n"
    "- Use optimize_schedule when the user asks whether their week is "
    "realistic, too full, or where the clashes are, and find_free_time when "
    "they ask when they could fit something in. Both only read the calendar "
    "and report; nothing is moved until they approve a proposal.\n"
    "- To change the calendar you must CALL create_event, update_event or "
    "delete_event. Calling the tool is what shows the user an Apply button; the "
    "change is never written any other way.\n"
    "- Do not ask the user whether they want the change in conversation. If they "
    "asked for it, call the tool and let the Apply button do the asking. Only "
    "ask a question when what they want is genuinely unclear.\n"
    "- Never claim a change is already saved. If you called a write tool, say it "
    "is waiting for their approval.\n"
    "- Today is {today}. Times are in the user's local timezone.\n"
)


CREATE_TOOL = {
    "type": "function",
    "function": {
        "name": "create_event",
        "description": (
            "Propose a new calendar event. The user confirms before it is saved. "
            "Pass the scheduling note through 'note' in the user's own words, "
            "for example 'dentist appointment tuesday at 3pm'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "note": {
                    "type": "string",
                    "description": "The event in plain language, including the day and time.",
                },
                "summary": {
                    "type": "string",
                    "description": "Short title of at most 8 words, with no date in it.",
                },
            },
            "required": ["note"],
        },
    },
}

DELETE_TOOL = {
    "type": "function",
    "function": {
        "name": "delete_event",
        "description": (
            "Propose deleting an upcoming event that matches a name. The user "
            "confirms before anything is deleted."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "The event's title, or part of it.",
                }
            },
            "required": ["summary"],
        },
    },
}

UPDATE_TOOL = {
    "type": "function",
    "function": {
        "name": "update_event",
        "description": (
            "Propose moving an existing event to a new start time, keeping its "
            "length unless 'end' is given too. The user confirms before it is "
            "saved."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "The event's title, or part of it.",
                },
                "start": {
                    "type": "string",
                    "description": "New start as 'YYYY-MM-DD HH:MM'. A bare date keeps the current time.",
                },
                "end": {
                    "type": "string",
                    "description": "Optional new end as 'YYYY-MM-DD HH:MM'. Omit it to keep the length.",
                },
            },
            "required": ["summary", "start"],
        },
    },
}

RESEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "research",
        "description": (
            "Search the web for something current -- opening hours, prices, "
            "timetables, news. Returns short excerpts with their sources."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What to look up."},
                "limit": {"type": "integer", "description": "Results to fetch, at most 10."},
            },
            "required": ["query"],
        },
    },
}

OPTIMIZE_TOOL = {
    "type": "function",
    "function": {
        "name": "optimize_schedule",
        "description": (
            "Report how the next days look: clashes, back-to-back stretches, "
            "how full each day is, where there is free time, and which event "
            "could be moved to relieve it. Read-only; nothing is changed."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "days": {"type": "integer", "description": "How many days ahead to analyse. Default 7."},
            },
        },
    },
}

FREE_TIME_TOOL = {
    "type": "function",
    "function": {
        "name": "find_free_time",
        "description": (
            "Find stretches of at least N free minutes in working hours, for "
            "one date or across the coming days. Read-only."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": "A single day as 'YYYY-MM-DD'. Omit to search the coming days."},
                "minutes": {"type": "integer", "description": "How long the free stretch must be. Default 60."},
                "days": {"type": "integer", "description": "How many days ahead to search when no date is given. Default 7."},
            },
        },
    },
}



@dataclass
class PendingChange:
    """A proposed calendar write, awaiting the user's approval."""

    kind: str  # "create", "update" or "delete"
    summary: str
    detail: str
    event: Optional[Event] = None
    target: Optional[Event] = None
    extra: Dict[str, Any] = field(default_factory=dict)

    def describe_change(self) -> str:
        """One-line summary of what applying this would do."""
        if self.kind == "create":
            return "Add: " + self.detail
        if self.kind == "update":
            return "Update: " + self.detail
        return "Delete: " + self.detail


@dataclass
class AssistantTurn:
    """What one exchange with the assistant produced."""

    text: str
    changes: List[PendingChange] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    @property
    def needs_confirmation(self) -> bool:
        return bool(self.changes)


class Assistant:
    """Local-model assistant with mail reads and confirm-gated calendar writes."""

    def __init__(
        self,
        service: Optional[ScheduleService] = None,
        llm: Optional[LocalLLM] = None,
        mcp: Optional[MCPClient] = None,
        days_ahead: int = 30,
    ) -> None:
        self.service = service or ScheduleService()
        self.llm = llm or LocalLLM()
        self.mcp = mcp or MCPClient()
        self.days_ahead = days_ahead
        self.history: List[Dict[str, Any]] = []
        self.mail_available = True
        self.last_tool_output = ""

    # -- tool surfaces ----------------------------------------------------

    def _calendar_tools(self) -> List[Dict[str, Any]]:
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "list_events",
                    "description": "List the events on the calendar for the coming days.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "days": {
                                "type": "integer",
                                "description": "How many days ahead to look. Default 14.",
                            }
                        },
                    },
                },
            }
        ]
        if self.mail_available:
            for tool in self.mcp.allowed_tools():
                tools.append(
                    {
                        "type": "function",
                        "function": {
                            "name": "search_emails",
                            "description": "Search mail by subject, sender or body text.",
                            "parameters": {
                                "type": "object",
                                "properties": {
                                    "query": {"type": "string"},
                                    "limit": {"type": "integer"},
                                },
                                "required": ["query"],
                            },
                        },
                    }
                )
                break
            tools.extend(
                [
                    {
                        "type": "function",
                        "function": {
                            "name": "list_emails",
                            "description": "List the most recent cached emails.",
                            "parameters": {
                                "type": "object",
                                "properties": {
                                    "limit": {"type": "integer"},
                                    "folder": {"type": "string"},
                                },
                            },
                        },
                    },
                    {
                        "type": "function",
                        "function": {
                            "name": "get_email",
                            "description": "Read one email in full by its numeric id.",
                            "parameters": {
                                "type": "object",
                                "properties": {"email_id": {"type": "integer"}},
                                "required": ["email_id"],
                            },
                        },
                    },
                ]
            )
        tools.extend([CREATE_TOOL, UPDATE_TOOL, DELETE_TOOL, RESEARCH_TOOL, OPTIMIZE_TOOL, FREE_TIME_TOOL])
        return tools

    # -- tool execution ---------------------------------------------------

    def _run_tool(
        self, name: str, args: Dict[str, Any]
    ) -> tuple[str, List[PendingChange]]:
        """Execute one read, or record one proposed write.

        Returns the text handed back to the model plus any changes that need
        the user's confirmation.
        """
        changes: List[PendingChange] = []
        try:
            if name == "list_events":
                days = int(args.get("days") or 14)
                events = self.service.upcoming(days=days, now=local_now())
                return _render_events(events), changes
            if name in ("search_emails", "list_emails", "get_email"):
                return _render_mail(self.mcp.call_tool(name, args)), changes
            if name == "create_event":
                note = str(args.get("note") or "").strip()
                if not note:
                    return "ERROR: create_event needs a note describing the event.", changes
                summary = str(args.get("summary") or "").strip()
                text = "{0} {1}".format(note, summary).strip() if summary else note
                event = self.service.parse(text, now=local_now())
                changes.append(
                    PendingChange(
                        kind="create",
                        summary=event.summary,
                        detail=describe(event),
                        event=event,
                    )
                )
                return (
                    "Proposed. Not saved yet; the user has to confirm. Tell them what "
                    "you proposed and that you are waiting for their approval.",
                    changes,
                )
            if name == "delete_event":
                wanted = str(args.get("summary") or "").strip()
                if not wanted:
                    return "ERROR: delete_event needs the event's summary.", changes
                match = self._match_upcoming(wanted)
                if match is None:
                    return (
                        "ERROR: no upcoming event matches {0!r}. Tell the user it was not "
                        "found.".format(wanted),
                        changes,
                    )
                changes.append(
                    PendingChange(
                        kind="delete",
                        summary=match.summary,
                        detail=describe(match),
                        target=match,
                    )
                )
                return (
                    "Proposed deletion. Not deleted yet; the user has to confirm.",
                    changes,
                )
            if name == "update_event":
                wanted = str(args.get("summary") or "").strip()
                if not wanted:
                    return "ERROR: update_event needs the event's summary.", changes
                match = self._match_upcoming(wanted)
                if match is None:
                    return (
                        "ERROR: no upcoming event matches {0!r}. Tell the user it was not "
                        "found.".format(wanted),
                        changes,
                    )
                if match.rrule:
                    return (
                        "ERROR: {0} repeats, so moving it needs the whole series edited "
                        "in the app. Tell the user to open it and change it there.".format(
                            match.summary
                        ),
                        changes,
                    )
                start = _parse_when(str(args.get("start") or ""), keep=match.start)
                end_raw = str(args.get("end") or "").strip()
                end = _parse_when(end_raw, keep=start) if end_raw else start + match.duration
                if end <= start:
                    return "ERROR: the event has to end after it starts.", changes
                updated = replace(match, start=start, end=end)
                changes.append(
                    PendingChange(
                        kind="update",
                        summary=updated.summary,
                        detail="{0}  \u2192  {1}".format(describe(match), describe(updated)),
                        event=updated,
                        target=match,
                    )
                )
                return (
                    "Proposed. Not saved yet; the user has to confirm. Tell them what "
                    "you proposed and that you are waiting for their approval.",
                    changes,
                )
            if name == "research":
                query = str(args.get("query") or "").strip()
                if not query:
                    return "ERROR: research needs a query.", changes
                try:
                    limit = max(1, min(10, int(args.get("limit") or 6)))
                except (TypeError, ValueError):
                    limit = 6
                try:
                    from local_butler.apps.geonews.websearch import search_everything_text

                    text = (search_everything_text(query, limit=limit) or "").strip()
                except Exception as exc:  # noqa: BLE001 -- network and parsing both live here
                    return "ERROR: research failed: {0}".format(exc), changes
                if not text:
                    return "No results found for {0!r}.".format(query), changes
                return text[:6000], changes
            if name == "optimize_schedule":
                days = _as_days(args.get("days"))
                first = local_now().date()
                last = first + dt.timedelta(days=days - 1)
                events = self.service.upcoming(days=days, now=local_now())
                return analyse(events, first, last), changes
            if name == "find_free_time":
                date_text = str(args.get("date") or "").strip()
                today = local_now().date()
                if date_text:
                    first = _parse_day(date_text)
                    last = first
                    days = max(1, (first - today).days + 1)
                else:
                    days = _as_days(args.get("days"))
                    first = today
                    last = first + dt.timedelta(days=days - 1)
                try:
                    minutes = max(5, min(480, int(args.get("minutes") or 60)))
                except (TypeError, ValueError):
                    minutes = 60
                events = self.service.upcoming(days=days, now=local_now())
                return _free_time_report(events, first, last, minutes=minutes), changes
        except (EventError, CalDAVError, LLMUnavailable, MCPError) as exc:
            return "ERROR: {0}".format(exc), changes
        except (TypeError, ValueError) as exc:
            return "ERROR: bad arguments for {0}: {1}".format(name, exc), changes
        return "ERROR: unknown tool {0}".format(name), changes

    def _match_upcoming(self, wanted: str) -> Optional[Event]:
        needle = wanted.strip().lower()
        events = self.service.upcoming(days=self.days_ahead, now=local_now())
        exact = [item for item in events if item.summary.strip().lower() == needle]
        if exact:
            return exact[0]
        loose = [item for item in events if needle in item.summary.strip().lower()]
        return loose[0] if loose else None

    # -- the loop ---------------------------------------------------------

    def ask(self, text: str) -> AssistantTurn:
        """Run one full exchange, returning the reply and any proposed writes."""
        prompt = (text or "").strip()
        if not prompt:
            return AssistantTurn(text="Tell me what you want to do or look up.")

        self.history.append({"role": "user", "content": prompt})
        if len(self.history) > 16:
            self.history = self.history[-16:]

        today = local_now().strftime("%A %d %B %Y")
        system = SYSTEM.format(today=today)
        messages: List[Dict[str, Any]] = [{"role": "system", "content": system}]
        messages.extend(self.history)

        changes: List[PendingChange] = []
        notes: List[str] = []
        reply = ""

        for turn in range(MAX_TURNS):
            message = self.llm.chat_tools(messages, self._calendar_tools(), max_tokens=900)
            calls = message.get("tool_calls") or []
            if not calls:
                reply = (message.get("content") or "").strip()
                if not reply and message.get("reasoning_content"):
                    reply = str(message["reasoning_content"]).strip()
                break
            messages.append(
                {
                    "role": "assistant",
                    "content": message.get("content") or "",
                    "tool_calls": calls,
                }
            )
            for call in calls:
                function = call.get("function") or {}
                name = str(function.get("name") or "")
                arguments = _parse_arguments(function.get("arguments"))
                result, produced = self._run_tool(name, arguments)
                changes.extend(produced)
                notes.append("{0}: {1}".format(name, result[:200]))
                self.last_tool_output = result
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.get("id") or name,
                        "name": name,
                        "content": result,
                    }
                )
            else:
                if turn == MAX_TURNS - 1:
                    reply = "I could not finish that in one go. Try asking me something simpler."

        if not reply:
            reply = "I looked into that but could not produce an answer."

        self.history.append({"role": "assistant", "content": reply})
        if changes:
            reply = "{0}\n\nI have proposed a change for you to confirm on the left.".format(reply)
        return AssistantTurn(text=reply, changes=changes, notes=notes)

    # -- applying approved writes -----------------------------------------

    def apply(self, change: PendingChange) -> str:
        """Carry out one change the user approved.

        This is the only place in the assistant that writes to the calendar, and
        it is reached only from an explicit approval in the UI.
        """
        if change.kind == "create" and change.event is not None:
            # Overlapping events are allowed on this calendar, so a clash is
            # information rather than a refusal; the user has already seen the
            # proposal and pressed Apply.
            saved = self.service.create(change.event, allow_conflict=True)
            return "Added: {0}".format(describe(saved))
        if change.kind == "update" and change.event is not None:
            saved = self.service.update(change.event)
            return "Updated: {0}".format(describe(saved))
        if change.kind == "delete" and change.target is not None:
            self.service.delete(change.target)
            return "Deleted: {0}".format(change.summary)
        raise EventError("nothing to apply for this change")


def _as_days(value: Any, default: int = 7, low: int = 1, high: int = 90) -> int:
    """Clamp a model-supplied day count to something the tools can afford."""
    try:
        days = int(value or default)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, days))


def _parse_day(text: str) -> dt.date:
    """Read a ``YYYY-MM-DD`` argument."""
    try:
        return dt.datetime.strptime((text or "").strip(), "%Y-%m-%d").date()
    except ValueError:
        raise EventError("date must look like 2026-10-06") from None


def _parse_when(text: str, keep: dt.datetime) -> dt.datetime:
    """Read ``'YYYY-MM-DD HH:MM'``; a bare date keeps the event's own time.

    ``keep`` is the time-of-day to fall back on, so the model can move an event
    to another day without repeating when on that day it happens.
    """
    stamp = (text or "").strip()
    if not stamp:
        raise EventError("start must look like 2026-10-06 14:30")
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return dt.datetime.strptime(stamp, fmt)
        except ValueError:
            continue
    try:
        day = dt.datetime.strptime(stamp, "%Y-%m-%d").date()
    except ValueError:
        raise EventError("start must look like 2026-10-06 14:30") from None
    return dt.datetime.combine(day, keep.time())



def _parse_arguments(raw: Any) -> Dict[str, Any]:
    """Read a tool call's arguments, tolerating a model that sends prose."""
    if isinstance(raw, dict):
        return raw
    text = str(raw or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except ValueError:
        from local_butler.apps.calendar.schedule import extract_json

        try:
            parsed = extract_json(text)
        except EventError:
            return {"note": text}
    return parsed if isinstance(parsed, dict) else {}


def _render_events(events: Sequence[Event]) -> str:
    if not events:
        return "The calendar is empty."
    return "\n".join(describe(event) for event in events)


def _render_mail(payload: Any) -> str:
    """Flatten a mail tool's result into lines the model can read cheaply."""
    if payload is None:
        return "No results."
    if isinstance(payload, str):
        return payload[:4000]
    if isinstance(payload, dict):
        emails = payload.get("emails")
        if isinstance(emails, list):
            lines = []
            for item in emails[:10]:
                if not isinstance(item, dict):
                    lines.append(str(item)[:200])
                    continue
                sender = item.get("from") or item.get("sender") or "unknown sender"
                subject = item.get("subject") or "(no subject)"
                date = item.get("date") or item.get("received_at") or ""
                ident = item.get("id") or item.get("email_id")
                lines.append(
                    "id {0} | {1} | {2} | {3}".format(ident, date, sender, subject)
                )
            return "\n".join(lines) or "No matching emails."
        body = json.dumps(payload)[:4000]
        return body
    if isinstance(payload, list):
        lines = []
        for item in payload[:10]:
            if isinstance(item, dict):
                lines.append(
                    "id {0} | {1} | {2} | {3}".format(
                        item.get("id"),
                        item.get("date") or "",
                        item.get("from") or item.get("sender") or "",
                        item.get("subject") or "(no subject)",
                    )
                )
            else:
                lines.append(str(item)[:200])
        return "\n".join(lines) or "No results."
    return str(payload)[:4000]