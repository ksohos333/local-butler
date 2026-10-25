# Calendar scheduling — how it actually works

The calendar window schedules events by typing a plain-English note. Everything
runs locally: Radicale stores the events, the local llama model turns the note
into structured fields, and the GUI is a Local Butler calendar app.

    Local Butler Calendar (GTK, dark)
        │
        ├─ main.py             entry, month grid + day timeline + chat, Radicale autostart
        ├─ panels.py           MonthView, DayTimeline, AgendaPanel, ClockBar, ChatPanel,
        │                      EventDialog, AddEventDialog, link helpers
        ├─ timeline.py         pure block/lane maths for the day strip  ← no GTK
        ├─ clock.py            AtomicClock: SNTP + chrony, one shared instance
        ├─ assistant.py        tool loop over calendar + mail, staged changes
        ├─ mcpclient.py        bigus_dickus MCP client  →  http://127.0.0.1:8780/mcp
        ├─ schedule.py         asks the model for wording; validates the result
        ├─ resolve.py          decides *when* the note means  ← deterministic
        ├─ optimize.py         clashes, load, free time, move proposals  ← pure
        ├─ events.py           Event model, RFC 5545 read/write
        └─ caldav.py           Radicale client  →  http://127.0.0.1:5232

## The window

- **Left, top** is the month grid: click a day to see its events. Today and the
  selected day are highlighted, and recurring events are expanded so they appear
  on every date they actually fall on.
- **Below it** is the *day timeline*: the selected day drawn as a horizontal
  strip, 00:00 to 24:00, with every event as a bar. When the selected day is
  today, a red "now" line sits at the current time and moves once a second.
- **Below that** is the agenda for the day/week/month, with a **−15m / +15m /
  +30m** nudge button per event, a Delete button, and **Add**.
- **Right** is the assistant. It can read the calendar and read mail, and it can
  stage a change for you to approve.
- The header's **clock** is the shared atomic clock (see below); the sidebar's
  **Calendar:** line is a live summary of what is coming up.

Recurrence expansion lives in `panels.py`: `DAILY`, `WEEKLY`, `MONTHLY` and
`YEARLY`, with optional `BYDAY` and `COUNT`, over a 400-day horizon capped at 800
occurrences. A stored rule it cannot interpret falls back to the original start
date rather than hiding the event. Multi-day events are listed on their start day.

## Overlapping events are normal

Two meetings in the same hour are a legitimate state of this calendar, not an
error. The policy is:

- `ScheduleService.create(event, allow_conflict=False)` still refuses a clash by
  default — that default protects the CLI/tests from silently double-booking.
  Every app and assistant call site passes `allow_conflict=True`.
- `Service.update()` never checks conflicts at all: an edit is saved regardless
  of what it now overlaps, because the user just approved it.
- The clash *analysis* tools (`optimize_schedule`) report overlaps as
  information, with a suggested move the user can decline.
- In the GUI, a new event that overlaps shows the clash text but the write still
  goes through.

The day timeline is what makes the overlap visible: same-hour events are laid
out in side-by-side lanes (`timeline.py: layout_day`), never stacked on top of
each other. All-day events get their own lane pool above the timed lanes.

## Nothing is written without your approval

`assistant.py` runs a tool loop against the local model. The model gets read
tools (`list_events`, `search_emails`, `list_emails`, `get_email`,
`research`, `optimize_schedule`, `find_free_time`) and three write tools
(`create_event`, `update_event`, `delete_event`). Calling a write tool does *not*
write: it returns a `PendingChange`, which the GUI shows with **Apply** /
**Discard**. Apply is the only route to CalDAV:

- create → `service.create(change.event, allow_conflict=True)`
- update → `service.update(change.event)` (PUT over the event's own href with
  `If-Match`)
- delete → `service.delete(change.target)`

`update_event` refuses to move a recurring event (editing one instance of a
series properly means editing the rule, which is the dialog's job), and a bare
`YYYY-MM-DD` argument keeps the event's own time of day so the model can move a
meeting to another day without repeating the clock time.

Several proposals from one reply share a single confirmation box with
**Apply all**, because showing them one at a time would let a later proposal push
an earlier, still-unapplied one out of the UI. The direct **Delete** button on the
day panel is its own confirmation dialog for the same reason.

The mail side is read-only by construction: `mcpclient.py` allow-lists
`list_emails`, `get_email` and `search_emails`, so `send_email`, `shell`,
`python`, `web_search` and the transaction tools are never offered to the model.

## Research, analysis and free time

Three read-only tools exist for questions that are not "what is on my calendar":

- **`research(query, limit)`** — lazy-imports
  `local_butler.apps.geonews.websearch.search_everything_text()` (stdlib-only,
  offline-safe) and hands back up to 6000 characters of excerpts with sources.
  A network or parse failure comes back as `ERROR: research failed: ...` for the
  model to report, never as a crash.
- **`optimize_schedule(days)`** — `optimize.analyse()`: occurrence count and
  booked minutes, clashes longest-overlap-first (capped at 8 lines), busiest
  days, free windows per day, and `propose_moves()` suggestions. All-day and
  recurring events are never proposed for a move — the tool may *suggest*
  moving "Team sync to 11:00", never "move the holiday".
- **`find_free_time(date?, minutes, days)`** — `optimize.find_free_time()`,
  searching inside working hours (09:00–17:00 by default) and ignoring gaps
  shorter than `minutes`. A day that is full says so plainly.

All three only read; nothing is moved until the user approves a
`PendingChange`.

## Clickable links in descriptions

Event descriptions and the chat transcript commonly contain URLs (meeting
links, source citations from `research`). `panels.py` has a small helper set:

- `URL_RE` finds `http(s)://...` runs; `tag_links()` paints them with a
  `"link"` `TextTag`; `link_at()` resolves a pointer position to a URL.
- In the read-only chat transcript a **plain click** opens the link.
- In the dialog's editable `TextView`s you need **Ctrl+click**, so a click that
  was meant to place the caret does not open a browser.

`main.py` escapes everything it puts into the transcript with
`GLib.markup_escape_text()` first, so a description cannot inject Pango markup.

## The day timeline and the atomic clock

`timeline.py` is pure arithmetic with no GTK import: `day_blocks()` turns
events (and their recurrences) into `Block`s clipped to the selected day,
`layout_day()` assigns lanes so overlapping blocks sit side by side, and
`time_at()` maps seconds-since-midnight back to a datetime. `panels.py`'s
`DayTimeline` widget does the drawing from those results on a `Gtk.Fixed`.

`clock.py` keeps one trustworthy wall clock:

- `AtomicClock` resolves NTP (`time.google.com`, `pool.ntp.org`,
  `time.cloudflare.com`, `time.apple.com`) with a strict SNTP reply parser, or
  falls back to `chronyc tracking` when chronyd is what holds the machine's
  time. `sync()`/`sync_async()` record the offset and the source;
  `status_text()` is what the header shows.
- It resyncs every `DEFAULT_RESYNC_SECONDS = 900` on a daemon thread, with a
  cooldown based on `time.monotonic()` so a failing network cannot start a
  thread storm. After a failed sync the last known offset is kept and `now()`
  keeps ticking from it.
- `shared_clock()` returns the process-wide singleton; `main.py` owns it,
  drives `ClockBar.tick()` and `DayTimeline.tick(now)` from one
  `GLib.timeout_add_seconds(1, self._tick)`, and `ClockBar.tick()` always
  returns `True` so GTK keeps rescheduling it.

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
3. `create()` writes the `.ics` with a conditional `If-Match` ETag, so two
   clients editing at once cannot silently clobber. Overlap checking is the
   caller's choice (`allow_conflict`), as described above.
4. Edits go through `update()` (same href + `If-Match`), nudge buttons through
   `shift()`/`extend()` and then `update()`.

Model calls and CalDAV writes both run on worker threads
(`ChatPanel._apply`, `main._run_write`), with results marshalled back through
`GLib.idle_add`, so the window stays responsive; a model call commonly takes
10–40 seconds.

## Editing in the GUI

- Clicking an agenda row's title or a timeline bar opens `EventDialog`: the
  event's fields, its description with links, Save (writes via `update`),
  Delete (its own confirmation; a recurring event offers "Delete series").
- The agenda's **−15m / +15m / +30m** buttons call `service.shift` /
  `service.extend` on a copy and write it back — all-day events shift in whole
  days, and `extend` refuses to shrink an event to zero length.
- **Add**, or clicking empty space on the timeline at a given hour, opens
  `AddEventDialog` pre-filled with that time; the write goes through
  `_create_allowing_overlap` → `create(..., allow_conflict=True)`.
- Every write runs through `main._run_write(work, message)`: a worker thread,
  then `_write_done` (green status + `refresh()`) or `_write_failed` (red
  status) via `GLib.idle_add`.

## GNOME Calendar

The built-in month view is the app's primary interface. GNOME Calendar is
supported as an *external client* on the same Radicale collection, so an event
added in either one shows up in the other.

    Server address   http://127.0.0.1:5232
    Collection       local/calendar
    Username         local
    Password         local

The **GNOME Calendar (CalDAV)** panel in the sidebar shows the exact collection
URL for the configured server, copies it to the clipboard, and launches
`gnome-calendar`. Adding the account is a one-off step in GNOME's own account
dialog. Verified round trip against Radicale: `PUT` create, `PROPFIND` read,
conditional `PUT` update with `If-Match`, `DELETE`.

## Why Qwen needed `/no_think`

Qwen3 defaults to emitting a reasoning trace. With the default settings the
response often arrived with an empty `content` and the whole token budget spent
on `reasoning_content`, which read as "the model refused". Setting
`chat_template_kwargs.enable_thinking=false` — or the `/no_think` prefix used
here — returns the answer in `content`. The same prefix already existed in
`local-agent/agent/llm.py`, so this is a known-good trick on this box, not a
new one.

The system prompt also has to say that staging a change means *calling the tool*,
not asking in conversation. Otherwise the model treats the confirmation as part
of the chat and no Apply button ever appears.

## Files

- `resolve.py` — pure functions: weekday, relative day, explicit date, clock
  time, recurrence, reminder. No I/O, fully covered by tests.
- `events.py` — `Event`, ICS serialise/parse, 75-octet folding, escaping.
- `caldav.py` — Radicale client. Note `_event_url()` distinguishes an absolute
  href (`/local/calendar/x.ics`), a bare file name, and a UID; conflating them
  silently targets a URL that does not exist. `put_event()` takes ICS text, not
  an `Event`.
- `schedule.py` — `ScheduleService`: `parse`, `preview`, `from_text`, `create`,
  `update`, `shift`, `extend`, `upcoming`, `conflicts`, `delete`,
  `delete_by_summary`. Module-level `shift_event`/`extend_event` are the pure
  copies the nudge buttons use.
- `timeline.py` — `Block`, `day_blocks`, `layout_day`, `time_at`. Pure; the
  geometry tests run without GTK.
- `clock.py` — `build_request`/`parse_response` (strict SNTP), `sntp_offset`,
  `chrony_offset`, `AtomicClock`, `shared_clock`.
- `optimize.py` — `free_slots`, `find_clashes`, `day_load`, `propose_moves`,
  `analyse`, `find_free_time`. Pure; quoted verbatim by two assistant tools.
- `assistant.py` — tool loop, read-only mail access, `PendingChange`, `apply()`.
- `panels.py` — `MonthView` (incl. recurrence expansion), `DayTimeline`,
  `AgendaPanel`, `ClockBar`, `ChatPanel`, `EventDialog`, `AddEventDialog`, and
  the link helpers (`URL_RE`, `tag_links`, `link_at`, `on_text_click`).
- `panel.py` — the old GTK scheduling widget; no longer imported by `main.py`.

## Tests

`tests/calendar/` runs offline: the model, the CalDAV server, the NTP socket
and the web search are all faked.

    cd /home/bpoppa12/local-butler
    .venv/bin/python -m pytest tests/calendar/ -q

218 tests pass; the 2 GTK widget tests (`test_panels.py`, `test_panel.py`) skip
because the 3.12 venv has no `gi`. Lint with
`timeout 60 uvx pyflakes src/local_butler/apps/calendar/*.py tests/calendar/*.py`.

## Two interpreters

- The venv is Python 3.12 (what `pyproject.toml` requires); it has pytest but no
  `gi`.
- The launcher `~/.local/bin/local-butler-calendar` runs `/usr/bin/python3`,
  which is 3.9.25 and has GTK.

The GTK tests need both, so they run under a separate environment
(`/tmp/lbtest`) that has PyGObject *and* pytest. The non-GTK modules are written
to run on 3.9 and checked with `/usr/bin/python3 -m py_compile`. Unifying the
interpreters is a separate piece of work.
