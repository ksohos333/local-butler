# Calendar scheduling — how it actually works

The calendar window schedules events by typing a plain-English note. Everything
runs locally: Radicale stores the events, the local llama model turns the note
into structured fields, and the GUI is the existing Local Butler calendar app.

    Local Butler Calendar (GTK)
        │
        ├─ panel.py          entry, confirm-before-save, upcoming list
        ├─ schedule.py       asks the model for wording; validates the result
        ├─ resolve.py        decides *when* the note means  ← deterministic
        ├─ events.py         Event model, RFC 5545 read/write
        └─ caldav.py         Radicale client  →  http://127.0.0.1:5232

## The model does wording, not arithmetic

The local model is `Qwen3-8B-Q4_K_M`. It is good at reading "this is a dentist
appointment" and unreliable at "the Tuesday after the 30th is the 6th". Measured
on this machine, asked for a Tuesday, it variously returned the 5th (a Monday),
the 13th, and ignored "all day" and the word "weekday".

So the split is deliberate:

- **The model decides** — `summary`, `location`, `description`, `duration_minutes`.
- **The code decides** — the date, the clock time, `all_day`, the recurrence
  rule, and the reminder, all parsed straight out of the note in `resolve.py`.

The model is never asked for a date at all; it is asked for `time_of_day` only.
If it does return a `start`, it is ignored whenever the note's own text
determines the date. The weekday word, "tomorrow", "December 24th" and "all day"
in the note always win.

Examples, with the reference time Wednesday 30 September 2026, 23:33:

| Note | Scheduled |
|---|---|
| `dentist appointment tuesday at 3pm, remind me 1 hour before` | Tue 6 Oct 15:00, 60m reminder |
| `standup every weekday at 9:15am starting tomorrow` | Thu 1 Oct 09:15, `BYDAY=MO,TU,WE,TH,FR;FREQ=WEEKLY` |
| `flight to Athens on December 24th, all day` | Thu 24 Dec, all day |
| `coffee with Maria friday 10am at the corner cafe` | Fri 2 Oct 10:00, location kept |
| `release party on 2026-12-31 22:00 with the team` | Thu 31 Dec 22:00 |

A named weekday means its next upcoming occurrence. Said at 23:33 on a Wednesday,
"wednesday 9am" means the *coming* Wednesday, because today's 9am has passed.

## Flow

1. `preview()` sends the note to the model and builds an `Event` without writing.
2. The GUI shows the result and any clash with an existing event.
3. `create()` re-checks conflicts and writes the `.ics` with a conditional
   `If-Match` ETag, so two clients editing at once cannot silently clobber.
4. New events are blocked if they overlap something already scheduled; the
   caller has to pass `allow_conflict=True` to override.

The model call runs on a worker thread, so the window stays responsive; it
commonly takes 10–40 seconds.

## Why Qwen needed `/no_think`

Qwen3 defaults to emitting a reasoning trace. With the default settings the
response often arrived with an empty `content` and the whole token budget spent
on `reasoning_content`, which read as "the model refused". Setting
`chat_template_kwargs.enable_thinking=false` — or the `/no_think` prefix used
here — returns the answer in `content`. The same prefix already existed in
`local-agent/agent/llm.py`, so this is a known-good trick on this box, not a
new one.

## Files

- `resolve.py` — pure functions: weekday, relative day, explicit date, clock
  time, recurrence, reminder. No I/O, fully covered by tests.
- `events.py` — `Event`, ICS serialise/parse, 75-octet folding, escaping.
- `caldav.py` — Radicale client. Note `_event_url()` distinguishes an absolute
  href (`/local/calendar/x.ics`), a bare file name, and a UID; conflating them
  silently targets a URL that does not exist.
- `schedule.py` — `ScheduleService`: `parse`, `preview`, `create`, `upcoming`,
  `conflicts`, `delete`, `delete_by_summary`.
- `panel.py` — the GTK scheduling widget.

## Tests

`tests/calendar/` runs offline: the model and the CalDAV server are both faked.

    cd /home/bpoppa12/local-butler
    .venv/bin/python -m pytest tests/calendar/ -q

`test_panel.py` needs PyGObject, which the 3.12 venv does not have, so it skips
there. The launcher runs system Python 3.9, which has GTK but no pytest; the
panel tests were verified by running their assertions under that interpreter
instead.

## Two interpreters

- The venv is Python 3.12 (what `pyproject.toml` requires); it has pytest but no
  `gi`.
- The launcher `~/.local/bin/local-butler-calendar` runs `/usr/bin/python3`,
  which is 3.9.25 and has GTK.

The new modules are written to run on 3.9, and are checked with
`/usr/bin/python3 -m py_compile`. Unifying the two is a separate piece of work.
