"""Tests for the ICS serializer/parser and the scheduling service.

The CalDAV server and the language model are both replaced by fakes, so these
run offline and deterministically.
"""

import datetime as dt
import json

import pytest

from local_butler.apps.calendar.caldav import CalDAVClient, CalDAVError
from local_butler.apps.calendar.events import Event, EventError, describe, parse_ics
from local_butler.apps.calendar.schedule import (
    ScheduleService,
    build_event,
    extend_event,
    extract_json,
    shift_event,
)

NOW = dt.datetime(2026, 9, 30, 23, 33)
ZONE = dt.timezone(dt.timedelta(hours=3))  # Europe/Athens in September


def make_event(**kwargs):
    defaults = dict(
        summary="Dentist Appointment",
        start=dt.datetime(2026, 10, 6, 15, 0),
        end=dt.datetime(2026, 10, 6, 16, 0),
        tz=ZONE,
    )
    defaults.update(kwargs)
    return Event(**defaults)


# -- events ---------------------------------------------------------------


def test_ics_roundtrip_preserves_fields():
    original = make_event(
        location="corner cafe, 5th",
        description="Bring insurance card;\nbring x,y list",
        reminder_minutes=60,
    )
    [restored] = parse_ics(original.to_ics())
    assert restored.summary == original.summary
    assert restored.start == original.start
    assert restored.end == original.end
    assert restored.location == original.location
    assert restored.description == original.description
    assert restored.reminder_minutes == original.reminder_minutes
    assert restored.uid == original.uid


def test_ics_roundtrip_preserves_unicode_and_recurrence():
    original = make_event(summary="Café µ-back — πουλι", rrule="BYDAY=MO,WE;FREQ=WEEKLY")
    [restored] = parse_ics(original.to_ics())
    assert restored.summary == original.summary
    assert restored.rrule == original.rrule


def test_ics_escapes_commas_semicolons_and_newlines():
    original = make_event(summary="a, b; c\nd")
    ics = original.to_ics()
    assert "\\," in ics and "\\;" in ics and "\\n" in ics
    [restored] = parse_ics(ics)
    assert restored.summary == "a, b; c\nd"


def test_folded_lines_are_under_75_octets():
    original = make_event(description="x" * 400)
    ics = original.to_ics()
    for line in ics.split("\r\n"):
        assert len(line.encode("utf-8")) <= 75
    [restored] = parse_ics(ics)
    assert restored.description == "x" * 400


def test_all_day_event_is_exactly_one_day():
    event = make_event(
        start=dt.datetime(2026, 12, 24, 0, 0),
        end=dt.datetime(2026, 12, 25, 0, 0),
        all_day=True,
    )
    assert event.to_ics().count("DTSTART;VALUE=DATE") == 1
    [restored] = parse_ics(event.to_ics())
    assert restored.all_day is True
    assert (restored.end - restored.start) == dt.timedelta(days=1)


def test_describe_mentions_summary_and_day():
    text = describe(make_event())
    assert "Dentist Appointment" in text
    assert "06 Oct 2026" in text


# -- json extraction ------------------------------------------------------


@pytest.mark.parametrize(
    "reply",
    [
        '{"summary": "a", "all_day": false}',
        'Sure! {"summary": "a", "all_day": false}',
        '```json\n{"summary": "a", "all_day": false}\n```',
        '{"summary": "a", "all_day": false} Hope that helps!',
    ],
)
def test_extract_json_tolerates_chatter(reply):
    assert extract_json(reply)["summary"] == "a"


def test_extract_json_rejects_prose_without_an_object():
    with pytest.raises(EventError):
        extract_json("I cannot help with that.")


# -- build_event ----------------------------------------------------------


def test_build_event_requires_a_summary():
    with pytest.raises(EventError):
        build_event({"summary": "  "}, now=NOW)


def test_build_event_rejects_a_past_start():
    with pytest.raises(EventError):
        build_event({"summary": "x", "start": "2020-01-01T10:00"}, now=NOW)


def test_build_event_all_day_ignores_a_zero_duration():
    event = build_event(
        {
            "summary": "Flight",
            "start": "2026-12-24T09:00",
            "all_day": True,
            "duration_minutes": 0,
        },
        now=NOW,
    )
    assert event.all_day is True
    assert event.start.hour == 0
    assert (event.end - event.start) == dt.timedelta(days=1)


def test_build_event_rejects_a_nonpositive_duration():
    with pytest.raises(EventError):
        build_event(
            {"summary": "x", "start": "2026-10-06T15:00", "duration_minutes": 0},
            now=NOW,
        )


def test_source_text_overrides_a_wrong_model_date():
    # The model is handed no date at all, and even a wrong one is overruled.
    event = build_event(
        {
            "summary": "Dentist Appointment",
            "time_of_day": "15:00",
            "start": "2026-10-13T15:00",
            "duration_minutes": 60,
        },
        now=NOW,
        source_text="dentist appointment tuesday at 3pm",
    )
    assert event.start == dt.datetime(2026, 10, 6, 15, 0)


def test_source_text_overrides_wrong_reminder_and_recurrence():
    event = build_event(
        {"summary": "Standup", "time_of_day": "09:15", "reminder_minutes": 5,
         "rrule": "FREQ=WEEKLY;BYDAY=SA,SU"},
        now=NOW,
        source_text="standup every weekday at 9:15am, remind me 1 hour before",
    )
    assert event.rrule == "BYDAY=MO,TU,WE,TH,FR;FREQ=WEEKLY"
    assert event.reminder_minutes == 60


def test_summary_is_capitalised_when_fully_lowercase():
    event = build_event({"summary": "coffee with maria", "start": "2026-10-06T10:00"},
                        now=NOW)
    assert event.summary == "Coffee with maria"


def test_summary_capitals_the_model_chose_are_kept():
    event = build_event({"summary": "iOS release", "start": "2026-10-06T10:00"}, now=NOW)
    assert event.summary == "iOS release"


# -- service --------------------------------------------------------------


class FakeCalDAV:
    """In-memory stand-in for Radicale, exercising the real service logic."""

    def __init__(self):
        self.store = {}
        self._seq = 0

    def list_calendars(self):
        return ["local/calendar"]

    def list_events(self, start=None, end=None):
        return [
            {"href": href, "etag": "etag-1", "ics": ics}
            for href, ics in sorted(self.store.items())
        ]

    def put_event(self, ics, uid=None, href=None, if_match=None):
        self._seq += 1
        target = "/local/calendar/{0}.ics".format(uid or "gen-{0}".format(self._seq))
        self.store[target] = ics
        return target

    def delete_event(self, href, if_match=None):
        if href not in self.store:
            raise CalDAVError("404 not found")
        del self.store[href]


class FakeLLM:
    def __init__(self, reply):
        self.reply = reply
        self.prompts = []

    def chat(self, system, prompt):
        self.prompts.append(prompt)
        return self.reply


def service_with(reply):
    llm = FakeLLM(reply)
    return ScheduleService(client=FakeCalDAV(), llm=llm), llm


def test_from_text_writes_the_event():
    reply = json.dumps(
        {
            "summary": "Dentist Appointment",
            "time_of_day": "15:00",
            "all_day": False,
            "duration_minutes": 60,
            "location": "",
            "description": "",
            "reminder_minutes": 60,
        }
    )
    service, _ = service_with(reply)
    event = service.from_text("dentist tuesday at 3pm, remind me 1 hour before", now=NOW)
    assert event.start == dt.datetime(2026, 10, 6, 15, 0)
    assert event.href  # the server returned an href
    assert len(service.upcoming(days=30, now=NOW)) == 1


def test_conflicting_create_is_refused_then_allowed():
    # Two genuinely different events at the same time, so the clash is real
    # rather than a same-UID update.
    reply = json.dumps(
        {"summary": "Dentist Appointment", "time_of_day": "15:00", "duration_minutes": 60}
    )
    service, _ = service_with(reply)
    service.from_text("dentist tuesday at 3pm", now=NOW)
    other = build_event(
        {"summary": "Team Sync", "time_of_day": "15:30", "duration_minutes": 60},
        now=NOW,
        source_text="team sync tuesday at 3:30pm",
    )

    with pytest.raises(EventError):
        service.create(other)

    service.create(other, allow_conflict=True)
    assert len(service.upcoming(days=30, now=NOW)) == 2


def test_recreating_the_same_event_updates_rather_than_duplicates():
    reply = json.dumps(
        {"summary": "Dentist Appointment", "time_of_day": "15:00", "duration_minutes": 60}
    )
    service, _ = service_with(reply)
    first = service.from_text("dentist tuesday at 3pm", now=NOW)
    service.create(first, allow_conflict=True)
    assert len(service.upcoming(days=30, now=NOW)) == 1


def test_delete_removes_the_event():
    reply = json.dumps({"summary": "Coffee", "time_of_day": "10:00", "duration_minutes": 30})
    service, _ = service_with(reply)
    event = service.from_text("coffee friday 10am", now=NOW)
    service.delete(event)
    assert service.upcoming(days=30, now=NOW) == []


def test_preview_never_writes():
    reply = json.dumps({"summary": "Coffee", "time_of_day": "10:00", "duration_minutes": 30})
    service, _ = service_with(reply)
    service.preview("coffee friday 10am", now=NOW)
    assert service.client.store == {}


class StrictCalDAV(FakeCalDAV):
    """Adds ETag preconditions and href-addressed updates."""

    def __init__(self):
        super().__init__()
        self.etags = {}
        self.rev = 0

    def _bump(self):
        self.rev += 1
        return "etag-{0}".format(self.rev)

    def list_events(self, start=None, end=None):
        return [
            {"href": href, "etag": self.etags.get(href, "etag-0"), "ics": ics}
            for href, ics in sorted(self.store.items())
        ]

    def put_event(self, ics, uid=None, href=None, if_match=None):
        target = href or "/local/calendar/{0}.ics".format(uid or "gen")
        if if_match is not None and self.etags.get(target) != if_match:
            raise CalDAVError("412 precondition failed")
        self.store[target] = ics
        self.etags[target] = self._bump()
        return target


def test_url_resolution_distinguishes_href_from_uid():
    client = CalDAVClient()
    # A path already ends in .ics, and must not be quoted as if it were a UID.
    assert client._event_url("/local/calendar/a%40b.ics") == (
        "http://127.0.0.1:5232/local/calendar/a%40b.ics"
    )
    assert client._event_url("a.ics") == (
        "http://127.0.0.1:5232/local/calendar/a.ics"
    )
    # A bare UID gets the .ics suffix and percent-escaping.
    assert client._event_url("x@y") == "http://127.0.0.1:5232/local/calendar/x%40y.ics"
    assert client._event_url("http://elsewhere/e.ics") == "http://elsewhere/e.ics"


def test_updating_by_href_replaces_rather_than_duplicates():
    service = ScheduleService(
        client=StrictCalDAV(),
        llm=FakeLLM("{}"),
    )
    now = NOW
    event = build_event({"summary": "Team review", "time_of_day": "15:00",
                         "duration_minutes": 60}, now=now,
                        source_text="team review tuesday at 3pm")
    service.create(event)

    stored = service.upcoming(days=30, now=now)[0]
    stored.reminder_minutes = 30
    stored.description = "agenda in the shared doc"
    service.client.put_event(stored.to_ics(), href=stored.href, if_match=stored.etag)

    live = service.upcoming(days=30, now=now)
    assert len(live) == 1
    assert live[0].reminder_minutes == 30
    assert live[0].description == "agenda in the shared doc"


def test_stale_etag_is_rejected():
    service = ScheduleService(client=StrictCalDAV(), llm=FakeLLM("{}"))
    event = build_event({"summary": "Team review", "time_of_day": "15:00"}, now=NOW,
                        source_text="team review tuesday at 3pm")
    service.create(event)
    stored = service.upcoming(days=30, now=NOW)[0]

    with pytest.raises(CalDAVError):
        service.client.put_event(
            stored.to_ics(), href=stored.href, if_match='"not-the-current-etag"'
        )


def test_delete_by_summary_removes_the_next_match():
    reply = json.dumps({"summary": "Coffee", "time_of_day": "10:00", "duration_minutes": 30})
    service, _ = service_with(reply)
    service.from_text("coffee friday 10am", now=NOW)
    assert service.delete_by_summary("coffee", now=NOW) == 1
    assert service.upcoming(days=30, now=NOW) == []


def test_delete_by_summary_reports_a_miss():
    service, _ = service_with("{}")
    with pytest.raises(EventError):
        service.delete_by_summary("nothing like this", now=NOW)


def test_parse_asks_the_model_for_wording_only():
    reply = json.dumps({"summary": "Coffee", "time_of_day": "10:00", "duration_minutes": 30})
    service, llm = service_with(reply)
    service.parse("coffee friday 10am", now=NOW)
    prompt = llm.prompts[0]
    assert "coffee friday 10am" in prompt
    assert "time_of_day" in prompt


# -- update / shift / extend ----------------------------------------------


def test_update_writes_back_over_the_same_href():
    service, _ = service_with(
        json.dumps({"summary": "Coffee", "time_of_day": "10:00", "duration_minutes": 30})
    )
    event = service.from_text("coffee friday 10am", now=NOW)
    href_before = event.href
    assert href_before

    event.start = event.start + dt.timedelta(minutes=15)
    event.end = event.end + dt.timedelta(minutes=15)
    event.description = "bring the beans"
    service.update(event)

    live = service.upcoming(days=30, now=NOW)
    assert len(live) == 1  # replaced in place, not duplicated
    assert live[0].href == href_before
    assert live[0].description == "bring the beans"
    assert live[0].start == event.start


def test_update_without_an_href_is_refused():
    service, _ = service_with("{}")
    never_written = make_event()  # no href, so there is nothing to write over
    with pytest.raises(CalDAVError):
        service.update(never_written)


def test_update_sends_the_etag_so_a_concurrent_edit_surfaces():
    service = ScheduleService(client=StrictCalDAV(), llm=FakeLLM("{}"))
    event = build_event(
        {"summary": "Team review", "time_of_day": "15:00", "duration_minutes": 60},
        now=NOW,
        source_text="team review tuesday at 3pm",
    )
    service.create(event)
    stored = service.upcoming(days=30, now=NOW)[0]

    stored.etag = '"not-the-current-etag"'  # another client wrote in the meantime
    stored.description = "changed here too"
    with pytest.raises(CalDAVError):
        service.update(stored)
    assert service.upcoming(days=30, now=NOW)[0].description == ""


def test_update_never_refuses_because_of_a_clash():
    # Overlapping events are normal here: update must save a meeting that
    # sits on top of another one without asking anybody.
    reply = json.dumps({"summary": "Coffee", "time_of_day": "10:00", "duration_minutes": 30})
    service, _ = service_with(reply)
    event = service.from_text("coffee friday 10am", now=NOW)
    service.create(
        make_event(
            summary="Team Sync",
            start=event.start + dt.timedelta(minutes=15),
            end=event.start + dt.timedelta(minutes=45),
        ),
        allow_conflict=True,
    )

    event.description = "moved onto the team sync on purpose"
    service.update(event)  # no allow_conflict flag exists on purpose

    live = service.upcoming(days=30, now=NOW)
    assert len(live) == 2
    assert {item.summary for item in live} == {"Coffee", "Team Sync"}


def test_shift_moves_a_timed_event_and_leaves_the_original_alone():
    event = make_event()

    moved = shift_event(event, dt.timedelta(minutes=15))

    assert moved.start == dt.datetime(2026, 10, 6, 15, 15)
    assert moved.end == dt.datetime(2026, 10, 6, 16, 15)
    assert (moved.end - moved.start) == (event.end - event.start)
    assert event.start == dt.datetime(2026, 10, 6, 15, 0)  # untouched


def test_shift_moves_an_all_day_event_in_whole_days():
    holiday = make_event(
        summary="Holiday",
        start=dt.datetime(2026, 12, 24),
        end=None,
        all_day=True,
    )

    nudged = shift_event(holiday, dt.timedelta(minutes=15))
    assert nudged.start == holiday.start  # a nudge must not change the day

    tomorrow = shift_event(holiday, dt.timedelta(days=1, minutes=15))
    assert tomorrow.start == dt.datetime(2026, 12, 25)
    assert (tomorrow.end - tomorrow.start) == dt.timedelta(days=1)

    back = shift_event(holiday, dt.timedelta(days=-1))
    assert back.start == dt.datetime(2026, 12, 23)


def test_extend_lengthens_an_event_and_can_shorten_it():
    event = make_event()

    longer = extend_event(event, dt.timedelta(minutes=30))
    assert longer.end == dt.datetime(2026, 10, 6, 16, 30)
    assert longer.start == event.start

    shorter = extend_event(event, dt.timedelta(minutes=-30))
    assert (shorter.end - shorter.start) == dt.timedelta(minutes=30)
    assert event.end == dt.datetime(2026, 10, 6, 16, 0)  # original untouched


def test_extend_refuses_to_shrink_an_event_to_nothing():
    event = make_event()
    with pytest.raises(EventError):
        extend_event(event, dt.timedelta(minutes=-60))  # ends exactly at start
    with pytest.raises(EventError):
        extend_event(event, dt.timedelta(hours=-2))


def test_shift_then_extend_then_update_saves_the_result():
    service, _ = service_with(
        json.dumps({"summary": "Coffee", "time_of_day": "10:00", "duration_minutes": 30})
    )
    event = service.from_text("coffee friday 10am", now=NOW)

    moved = service.shift(event, dt.timedelta(minutes=15))
    assert moved.href == event.href  # still addressable after the copy
    extended = service.extend(moved, dt.timedelta(minutes=30))
    service.update(extended)

    live = service.upcoming(days=30, now=NOW)
    assert len(live) == 1
    assert live[0].start == event.start + dt.timedelta(minutes=15)
    assert (live[0].end - live[0].start) == dt.timedelta(minutes=60)
