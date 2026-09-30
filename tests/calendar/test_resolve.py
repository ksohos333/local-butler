"""Tests for deterministic reading of scheduling language.

These cover the arithmetic a small model cannot be trusted with, so they are
pure functions of the note plus an explicit reference ``now``.
"""

import datetime as dt

import pytest

from local_butler.apps.calendar import resolve

# Wednesday 23:33 - late enough that "today at <earlier hour>" is in the past.
NOW = dt.datetime(2026, 9, 30, 23, 33)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("dentist tuesday at 3pm", dt.datetime(2026, 10, 6, 15, 0)),
        ("coffee friday 10am", dt.datetime(2026, 10, 2, 10, 0)),
        ("party saturday 8pm", dt.datetime(2026, 10, 3, 20, 0)),
        ("retro next tuesday 11am", dt.datetime(2026, 10, 6, 11, 0)),
        ("call mom tomorrow morning 9am", dt.datetime(2026, 10, 1, 9, 0)),
        ("gym tonight 7pm", dt.datetime(2026, 9, 30, 19, 0)),
        ("lunch today at 12:30", dt.datetime(2026, 9, 30, 12, 30)),
        ("release on 2026-12-31 22:00", dt.datetime(2026, 12, 31, 22, 0)),
        ("flight on December 24th", dt.datetime(2026, 12, 24, 9, 0)),
    ],
)
def test_resolve_start(text, expected):
    assert resolve.resolve_start(text, NOW) == expected


def test_weekday_never_lands_in_the_past():
    # It is Wednesday 23:33; "wednesday at 9am" must mean the coming Wednesday.
    assert resolve.resolve_start("wednesday 9am", NOW) == dt.datetime(2026, 10, 7, 9, 0)


def test_same_weekday_later_today_stays_today():
    now = dt.datetime(2026, 9, 30, 8, 0)  # Wednesday morning
    assert resolve.resolve_start("standup wednesday 9am", now) == dt.datetime(
        2026, 9, 30, 9, 0
    )


def test_no_time_defaults_to_nine_am_tomorrow():
    assert resolve.resolve_start("renew passport", NOW) == dt.datetime(2026, 10, 1, 9, 0)


def test_explicit_date_wins_over_weekday():
    # The 24th is named, so the weekday word must not move it.
    assert resolve.resolve_start("appointment on December 24th at 10am", NOW) == dt.datetime(
        2026, 12, 24, 10, 0
    )


def test_explicit_date_rolls_to_next_year_when_already_past():
    assert resolve.explicit_date("on January 5th", NOW) == dt.date(2027, 1, 5)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("standup every weekday at 9am", "BYDAY=MO,TU,WE,TH,FR;FREQ=WEEKLY"),
        ("gym every monday and thursday", "BYDAY=MO,TH;FREQ=WEEKLY"),
        ("brunch every weekend", "BYDAY=SA,SU;FREQ=WEEKLY"),
        ("daily standup", "FREQ=DAILY"),
        ("rent monthly", "FREQ=MONTHLY"),
        ("dentist appointment", None),
        ("review the deck", None),
    ],
)
def test_repeat_rule(text, expected):
    assert resolve.repeat_rule(text) == expected


@pytest.mark.parametrize(
    "text,expected",
    [
        ("remind me 1 hour before", 60),
        ("remind me 15 minutes ahead", 15),
        ("remind me two days before", None),  # spelled out numbers are not parsed
        ("remind me 2 days before", 2880),
        ("no reminder needed", None),
    ],
)
def test_reminder_minutes(text, expected):
    assert resolve.reminder_minutes(text) == expected


def test_all_day_detection():
    assert resolve.all_day_requested("conference all day") is True
    assert resolve.all_day_requested("offsite for the whole day") is True
    assert resolve.all_day_requested("lunch at 1pm") is None


@pytest.mark.parametrize(
    "text,expected",
    [
        ("at 3pm", (15, 0)),
        ("at 3 pm", (15, 0)),
        ("at 09:30", (9, 30)),
        ("at 12am", (0, 0)),
        ("at 12pm", (12, 0)),
        ("at 7", None),
        ("top 5 things", None),
    ],
)
def test_time_of_day(text, expected):
    assert resolve.time_of_day(text) == expected
