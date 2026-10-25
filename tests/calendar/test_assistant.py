"""Tests for the assistant's tool loop and its confirmation gate.

The model is replaced with a scripted stub, so these exercise the part that
matters -- that a proposed write never reaches CalDAV until the user applies it
-- without waiting on a real inference call or a network.
"""

import datetime as dt

import pytest

from local_butler.apps.calendar.assistant import (
    Assistant,
    AssistantTurn,
    PendingChange,
    _parse_arguments,
    _render_events,
    _render_mail,
)
from local_butler.apps.calendar.events import Event, EventError
from local_butler.apps.calendar.schedule import LLMUnavailable

ZONE = dt.timezone(dt.timedelta(hours=3))
NOW = dt.datetime(2026, 10, 6, 12, 0)


def make_event(summary="Dentist", **kwargs):
    defaults = dict(start=NOW + dt.timedelta(days=1), end=NOW + dt.timedelta(days=1, hours=1), tz=ZONE)
    defaults.update(kwargs)
    return Event(summary=summary, **defaults)


class StubService:
    """A ScheduleService stand-in that records writes instead of doing them."""

    def __init__(self, events=None):
        self._events = list(events or [])
        self.created = []
        self.deleted = []
        self.updated = []
        self.conflict_flags = []

    def upcoming(self, days=14, now=None):
        return list(self._events)

    def events(self, start=None, end=None):
        return list(self._events)

    def parse(self, text, now=None):
        return make_event(summary="Parsed:" + text[:20], start=NOW + dt.timedelta(hours=2))

    def create(self, event, allow_conflict=False):
        self.created.append(event)
        self.conflict_flags.append(allow_conflict)
        return event

    def update(self, event):
        self.updated.append(event)
        return event

    def delete(self, event):
        self.deleted.append(event)


class StubLLM:
    """Replays a fixed list of assistant messages, one per turn."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def chat_tools(self, messages, tools, max_tokens=1024):
        self.calls.append({"messages": list(messages), "tools": list(tools)})
        if not self.script:
            return {"content": "nothing left to do"}
        return self.script.pop(0)


def tool_call(name, args, call_id="c1"):
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": args},
    }


class StubMCP:
    def __init__(self, payload=None, available=True):
        self.payload = payload if payload is not None else {"emails": []}
        self._available = available
        self.calls = []

    def available(self):
        return self._available

    def allowed_tools(self):
        return [{"name": "search_emails"}, {"name": "list_emails"}, {"name": "get_email"}]

    def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return self.payload


def build(script, events=None, mcp=None):
    service = StubService(events)
    llm = StubLLM(script)
    assistant = Assistant(service=service, llm=llm, mcp=mcp or StubMCP())
    return assistant, service, llm


# -- the confirmation gate ------------------------------------------------


def test_create_event_is_only_proposed_not_written():
    assistant, service, _ = build(
        [
            {
                "content": "",
                "tool_calls": [tool_call("create_event", '{"note": "dentist tuesday at 3pm"}')],
            },
            {"content": "I proposed the dentist appointment for you to approve."},
        ]
    )

    turn = assistant.ask("book me a dentist appointment")

    assert turn.needs_confirmation
    assert len(turn.changes) == 1
    assert turn.changes[0].kind == "create"
    assert service.created == [], "no event may be written before the user applies it"
    assert "confirm" in turn.text.lower()


def test_apply_is_what_actually_writes():
    assistant, service, _ = build(
        [
            {
                "content": "",
                "tool_calls": [tool_call("create_event", '{"note": "dentist tuesday"}')],
            },
            {"content": "proposed"},
        ]
    )
    turn = assistant.ask("book a dentist")

    message = assistant.apply(turn.changes[0])

    assert len(service.created) == 1
    assert "Added" in message


def test_delete_event_is_only_proposed_then_applied():
    event = make_event(summary="Gym")
    assistant, service, _ = build(
        [
            {
                "content": "",
                "tool_calls": [tool_call("delete_event", '{"summary": "Gym"}')],
            },
            {"content": "proposed deletion"},
        ],
        events=[event],
    )

    turn = assistant.ask("cancel my gym session")

    assert service.deleted == [], "deletion must wait for approval"
    assert turn.changes[0].kind == "delete"

    assistant.apply(turn.changes[0])
    assert service.deleted == [event]


def test_delete_of_unknown_event_reports_not_found():
    assistant, service, _ = build(
        [
            {
                "content": "",
                "tool_calls": [tool_call("delete_event", '{"summary": "Dentist"}')],
            },
            {"content": "I could not find that event."},
        ],
        events=[make_event(summary="Gym")],
    )

    turn = assistant.ask("delete the dentist")

    assert turn.changes == []
    assert "could not find" in turn.text.lower()


def test_delete_matches_partial_summary():
    event = make_event(summary="Team standup")
    assistant, service, _ = build(
        [
            {"content": "", "tool_calls": [tool_call("delete_event", '{"summary": "standup"}')]},
            {"content": "proposed"},
        ],
        events=[event],
    )

    turn = assistant.ask("cancel standup")

    assert len(turn.changes) == 1
    assert turn.changes[0].target.summary == "Team standup"


# -- reads ----------------------------------------------------------------


def test_list_events_is_handled_without_changes():
    assistant, service, _ = build(
        [
            {"content": "", "tool_calls": [tool_call("list_events", '{"days": 7}')]},
            {"content": "You have a dentist appointment on Tuesday."},
        ],
        events=[make_event(summary="Dentist")],
    )

    turn = assistant.ask("what is on my calendar")

    assert turn.changes == []
    tool_messages = [m for m in assistant.history if m.get("role") == "assistant"]
    assert tool_messages
    assert "Dentist" in assistant.last_tool_output


def test_mail_tool_is_proxied_through_mcp():
    mcp = StubMCP(payload={"emails": [{"id": 7, "from": "clinic@example.com", "subject": "Appointment", "date": "2026-10-01"}]})
    assistant, service, _ = build(
        [
            {"content": "", "tool_calls": [tool_call("search_emails", '{"query": "dentist"}')]},
            {"content": "Your clinic confirmed the appointment."},
        ],
        mcp=mcp,
    )

    turn = assistant.ask("did the dentist email me?")

    assert mcp.calls == [("search_emails", {"query": "dentist"})]
    assert turn.changes == []


def test_tool_names_offered_are_write_and_read_only():
    assistant, _, llm = build([{"content": "hello"}])
    assistant.ask("hi")

    offered = [t["function"]["name"] for t in llm.calls[0]["tools"]]
    assert "create_event" in offered
    assert "delete_event" in offered
    assert "list_events" in offered
    for dangerous in ("send_email", "shell", "python", "import_transactions"):
        assert dangerous not in offered


def test_mail_tools_drop_out_when_mcp_is_unavailable():
    assistant, _, llm = build([{"content": "hello"}], mcp=StubMCP(available=False))
    assistant.mail_available = False

    assistant.ask("hi")

    offered = [t["function"]["name"] for t in llm.calls[0]["tools"]]
    assert "search_emails" not in offered
    assert "list_events" in offered


# -- failure handling -----------------------------------------------------


def test_unknown_tool_is_reported_to_the_model_not_raised():
    assistant, _, _ = build(
        [
            {"content": "", "tool_calls": [tool_call("rm_rf", "{}")]},
            {"content": "I cannot do that."},
        ]
    )

    turn = assistant.ask("delete everything")

    assert "unknown tool" in assistant.last_tool_output
    assert turn.changes == []


def test_create_without_note_errors_instead_of_guessing():
    assistant, _, _ = build(
        [
            {"content": "", "tool_calls": [tool_call("create_event", "{}")]},
            {"content": "Tell me what to schedule."},
        ]
    )

    turn = assistant.ask("book something")

    assert turn.changes == []
    assert "needs a note" in assistant.last_tool_output


def test_empty_question_does_not_call_the_model():
    assistant, _, llm = build([{"content": "unused"}])

    turn = assistant.ask("   ")

    assert llm.calls == []
    assert turn.text


def test_thinking_only_reply_falls_back_to_reasoning():
    assistant, _, _ = build([{"content": "", "reasoning_content": "I think it is Tuesday."}])
    turn = assistant.ask("when is my dentist?")
    assert turn.text == "I think it is Tuesday."


def test_apply_rejects_a_change_with_nothing_in_it():
    assistant, _, _ = build([{"content": "hi"}])
    with pytest.raises(EventError):
        assistant.apply(PendingChange(kind="create", summary="x", detail="y"))


def test_empty_calendar_reads_as_empty_not_as_failure():
    assistant, _, _ = build(
        [
            {"content": "", "tool_calls": [tool_call("list_events", "{}")]},
            {"content": "Nothing is scheduled."},
        ],
        events=[],
    )

    turn = assistant.ask("anything coming up?")

    assert "empty" in assistant.last_tool_output
    assert turn.changes == []


# -- helpers --------------------------------------------------------------


def test_parse_arguments_tolerates_json_and_prose():
    assert _parse_arguments('{"a": 1}') == {"a": 1}
    assert _parse_arguments("") == {}
    assert _parse_arguments('{"note": "gym"}') == {"note": "gym"}
    loose = _parse_arguments("here you go {\"note\": \"gym\"}")
    assert loose == {"note": "gym"}


def test_render_events_and_mail():
    assert "empty" in _render_events([])
    assert "Dentist" in _render_events([make_event(summary="Dentist")])
    assert "No results" in _render_mail(None)
    line = _render_mail({"emails": [{"id": 1, "from": "a@b.c", "subject": "Hi", "date": "Oct 1"}]})
    assert "a@b.c" in line and "Hi" in line


def test_assistant_turn_defaults_are_empty():
    turn = AssistantTurn(text="hello")
    assert turn.changes == [] and turn.notes == []
    assert not turn.needs_confirmation


def test_llm_unavailable_is_not_swallowed():
    class Boom:
        def chat_tools(self, *a, **k):
            raise LLMUnavailable("no model")

    assistant = Assistant(service=StubService(), llm=Boom(), mcp=StubMCP())
    with pytest.raises(LLMUnavailable):
        assistant.ask("hello")

# -- update_event ---------------------------------------------------------


def dentist_on(day_offset=1, hour=15):
    start = (NOW + dt.timedelta(days=day_offset)).replace(hour=hour, minute=0)
    return make_event(
        summary="Dentist", start=start, end=start + dt.timedelta(hours=1)
    )


def test_update_event_is_only_proposed_then_applied():
    event = dentist_on()
    assistant, service, _ = build(
        [
            {
                "content": "",
                "tool_calls": [
                    tool_call(
                        "update_event",
                        '{{"summary": "Dentist", "start": "{0}"}}'.format(
                            (event.start + dt.timedelta(hours=2)).strftime("%Y-%m-%d %H:%M")
                        ),
                    )
                ],
            },
            {"content": "proposed the move"},
        ],
        events=[event],
    )

    turn = assistant.ask("move the dentist two hours later")

    assert service.updated == [], "moving an event must wait for approval"
    assert turn.changes[0].kind == "update"
    updated = turn.changes[0].event
    assert updated.start == event.start + dt.timedelta(hours=2)
    assert (updated.end - updated.start) == dt.timedelta(hours=1)  # length kept
    assert turn.changes[0].describe_change().startswith("Update:")
    assert turn.changes[0].target is event

    message = assistant.apply(turn.changes[0])
    assert service.updated == [updated]
    assert message.startswith("Updated:")


def test_update_event_can_change_the_end_as_well():
    event = dentist_on()
    assistant, _, _ = build(
        [
            {
                "content": "",
                "tool_calls": [
                    tool_call(
                        "update_event",
                        '{{"summary": "Dentist", "start": "{0}", "end": "{1}"}}'.format(
                            (event.start + dt.timedelta(hours=1)).strftime("%Y-%m-%d %H:%M"),
                            (event.start + dt.timedelta(hours=3)).strftime("%Y-%m-%d %H:%M"),
                        ),
                    )
                ],
            },
            {"content": "proposed"},
        ],
        events=[event],
    )

    turn = assistant.ask("make the dentist two hours long starting at four")

    updated = turn.changes[0].event
    assert updated.start == event.start + dt.timedelta(hours=1)
    assert updated.end == event.start + dt.timedelta(hours=3)


def test_update_event_with_a_bare_date_keeps_the_time_of_day():
    event = dentist_on()
    new_day = (event.start + dt.timedelta(days=7)).strftime("%Y-%m-%d")
    assistant, _, _ = build(
        [
            {
                "content": "",
                "tool_calls": [
                    tool_call(
                        "update_event",
                        '{{"summary": "Dentist", "start": "{0}"}}'.format(new_day),
                    )
                ],
            },
            {"content": "proposed"},
        ],
        events=[event],
    )

    turn = assistant.ask("push the dentist back a week")

    updated = turn.changes[0].event
    assert updated.start.date() == event.start.date() + dt.timedelta(days=7)
    assert updated.start.time() == event.start.time()
    assert (updated.end - updated.start) == dt.timedelta(hours=1)


def test_update_event_refuses_a_repeating_series():
    assistant, service, _ = build(
        [
            {
                "content": "",
                "tool_calls": [
                    tool_call(
                        "update_event",
                        '{"summary": "Standup", "start": "2026-10-13 10:00"}',
                    )
                ],
            },
            {"content": "That repeats, so it has to be edited in the app."},
        ],
        events=[make_event(summary="Team standup", rrule="FREQ=WEEKLY;BYDAY=TU")],
    )

    turn = assistant.ask("move standup to ten")

    assert turn.changes == []
    assert service.updated == []
    assert "repeats" in assistant.last_tool_output


def test_update_of_unknown_event_reports_not_found():
    assistant, service, _ = build(
        [
            {
                "content": "",
                "tool_calls": [
                    tool_call(
                        "update_event",
                        '{"summary": "Dentist", "start": "2026-10-13 10:00"}',
                    )
                ],
            },
            {"content": "I could not find that event."},
        ],
        events=[make_event(summary="Gym")],
    )

    turn = assistant.ask("move the dentist")

    assert turn.changes == []
    assert service.updated == []
    assert "no upcoming event matches" in assistant.last_tool_output


def test_update_event_rejects_an_end_before_its_start():
    event = dentist_on()
    assistant, _, _ = build(
        [
            {
                "content": "",
                "tool_calls": [
                    tool_call(
                        "update_event",
                        '{{"summary": "Dentist", "start": "{0}", "end": "{1}"}}'.format(
                            event.start.strftime("%Y-%m-%d %H:%M"),
                            (event.start - dt.timedelta(hours=1)).strftime("%Y-%m-%d %H:%M"),
                        ),
                    )
                ],
            },
            {"content": "That does not work."},
        ],
        events=[event],
    )

    turn = assistant.ask("make the dentist end before it starts")

    assert turn.changes == []
    assert "end after it starts" in assistant.last_tool_output


def test_update_event_needs_a_summary():
    assistant, _, _ = build(
        [
            {"content": "", "tool_calls": [tool_call("update_event", '{"start": "2026-10-13 10:00"}')]},
            {"content": "Which event?"},
        ],
        events=[dentist_on()],
    )

    turn = assistant.ask("move something")

    assert turn.changes == []
    assert "needs the event's summary" in assistant.last_tool_output


def test_apply_create_allows_overlapping_events():
    assistant, service, _ = build(
        [
            {"content": "", "tool_calls": [tool_call("create_event", '{"note": "dentist tuesday at 3pm"}')]},
            {"content": "proposed"},
        ]
    )
    turn = assistant.ask("book a dentist")

    assistant.apply(turn.changes[0])

    assert service.conflict_flags == [
        True
    ], "the user already approved, so a clash must not block the write"


# -- research -------------------------------------------------------------


def test_research_returns_excerpts_with_their_sources(monkeypatch):
    from local_butler.apps.geonews import websearch

    calls = []

    def fake_search(query, corpus=None, limit=6):
        calls.append((query, limit))
        return "Piraeus ferry office opens 07:30 (source: portofpireaus.gr)"

    monkeypatch.setattr(websearch, "search_everything_text", fake_search)
    assistant, _, _ = build(
        [
            {"content": "", "tool_calls": [tool_call("research", '{"query": "piraeus opening hours", "limit": 3}')]},
            {"content": "The office opens at 07:30 according to portofpireaus.gr."},
        ]
    )

    turn = assistant.ask("when does the ferry office open?")

    assert turn.changes == []
    assert calls == [("piraeus opening hours", 3)]
    assert "portofpireaus.gr" in assistant.last_tool_output


def test_research_says_plainly_when_nothing_came_back(monkeypatch):
    from local_butler.apps.geonews import websearch

    monkeypatch.setattr(websearch, "search_everything_text", lambda query, corpus=None, limit=6: "")
    assistant, _, _ = build(
        [
            {"content": "", "tool_calls": [tool_call("research", '{"query": "obscure thing"}')]},
            {"content": "Nothing found."},
        ]
    )

    assistant.ask("look up the obscure thing")

    assert "No results found" in assistant.last_tool_output


def test_research_failure_is_reported_to_the_model_not_raised(monkeypatch):
    from local_butler.apps.geonews import websearch

    def boom(query, corpus=None, limit=6):
        raise OSError("network down")

    monkeypatch.setattr(websearch, "search_everything_text", boom)
    assistant, _, _ = build(
        [
            {"content": "", "tool_calls": [tool_call("research", '{"query": "news"}')]},
            {"content": "I could not reach the web."},
        ]
    )

    turn = assistant.ask("what is in the news?")

    assert turn.changes == []
    assert "ERROR: research failed" in assistant.last_tool_output


def test_research_needs_a_query():
    assistant, _, _ = build(
        [
            {"content": "", "tool_calls": [tool_call("research", "{}")]},
            {"content": "What should I look up?"},
        ]
    )

    assistant.ask("research")

    assert "needs a query" in assistant.last_tool_output


# -- optimize_schedule / find_free_time -----------------------------------


def next_day(hour, minute=0, days_ahead=1):
    day = (dt.datetime.now() + dt.timedelta(days=days_ahead)).date()
    start = dt.datetime.combine(day, dt.time(hour, minute))
    return start, start + dt.timedelta(hours=1)


def test_optimize_schedule_reports_clashes_without_writing_anything():
    a_start, a_end = next_day(9)
    b_start, b_end = next_day(9, 30)
    events = [
        make_event(summary="Dentist", start=a_start, end=a_end),
        make_event(summary="Team sync", start=b_start, end=b_end),
    ]
    assistant, service, _ = build(
        [
            {"content": "", "tool_calls": [tool_call("optimize_schedule", '{"days": 7}')]},
            {"content": "Tuesday morning has a clash you could move."},
        ],
        events=events,
    )

    turn = assistant.ask("is my week realistic?")

    assert turn.changes == []
    assert service.created == [] and service.updated == [] and service.deleted == []
    output = assistant.last_tool_output
    assert "Clashes" in output
    assert "Dentist" in output and "Team sync" in output
    assert "Free for" in output
    assert "Suggested moves" in output


def test_optimize_schedule_on_a_free_week_reports_no_clashes():
    assistant, _, _ = build(
        [
            {"content": "", "tool_calls": [tool_call("optimize_schedule", '{"days": 3}')]},
            {"content": "Your week looks calm."},
        ],
        events=[],
    )

    assistant.ask("am i too busy?")

    assert "Clashes: none." in assistant.last_tool_output


def test_find_free_time_reports_the_windows():
    busy_start, busy_end = next_day(11)
    tomorrow = (dt.datetime.now() + dt.timedelta(days=1)).date().isoformat()
    assistant, service, _ = build(
        [
            {
                "content": "",
                "tool_calls": [
                    tool_call(
                        "find_free_time",
                        '{{"date": "{0}", "minutes": 60}}'.format(tomorrow),
                    )
                ],
            },
            {"content": "The morning and afternoon are open."},
        ],
        events=[make_event(summary="Busy", start=busy_start, end=busy_end)],
    )

    turn = assistant.ask("when can I fit a one hour call tomorrow?")

    assert turn.changes == []
    assert service.created == [] and service.updated == []
    output = assistant.last_tool_output
    assert "free 60-minute window(s)" in output
    assert "09:00-11:00" in output
    assert "12:00-17:00" in output


def test_find_free_time_reports_a_day_that_is_full():
    busy_start, busy_end = next_day(9)
    full = make_event(
        summary="Fully booked",
        start=busy_start,
        end=busy_start + dt.timedelta(hours=8),
    )
    tomorrow = (dt.datetime.now() + dt.timedelta(days=1)).date().isoformat()
    assistant, _, _ = build(
        [
            {
                "content": "",
                "tool_calls": [
                    tool_call("find_free_time", '{{"date": "{0}", "minutes": 60}}'.format(tomorrow))
                ],
            },
            {"content": "Tomorrow is full."},
        ],
        events=[full],
    )

    assistant.ask("any room tomorrow?")

    assert assistant.last_tool_output.startswith("No free 60-minute window")


def test_find_free_time_rejects_a_date_it_cannot_read():
    assistant, _, _ = build(
        [
            {"content": "", "tool_calls": [tool_call("find_free_time", '{"date": "next tuesday"}')]},
            {"content": "Which date exactly?"},
        ],
        events=[],
    )

    turn = assistant.ask("any free time next tuesday?")

    assert turn.changes == []
    assert "date must look like" in assistant.last_tool_output


# -- what the model is offered --------------------------------------------


def test_update_research_and_analysis_tools_are_offered():
    assistant, _, llm = build([{"content": "hello"}])
    assistant.ask("hi")

    offered = [tool["function"]["name"] for tool in llm.calls[0]["tools"]]
    for expected in (
        "update_event",
        "research",
        "optimize_schedule",
        "find_free_time",
        "create_event",
        "delete_event",
        "list_events",
    ):
        assert expected in offered
