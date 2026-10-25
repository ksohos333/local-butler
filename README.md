# Local Butler

A local-first platform of eight small Python applications that run entirely on
your own machine — a news intelligence engine with a live map and prediction
markets, a natural-language calendar, a money tracker, a Linux troubleshooter, a
camera/microphone monitor, and more. One host process, no accounts, no cloud,
no telemetry.

| | |
|---|---|
| Applications | 8 packages (6 complete, 2 scaffolds) |
| Tests | 393 passing, 2 skipped |
| Code | ~19,000 lines application, ~6,500 lines tests |
| Stack | Python 3.12, FastAPI, SQLAlchemy, Alembic, SQLite, Jinja2, Leaflet |

## The applications

### GeoNews — news intelligence *(flagship)*

Ingests 25 primary-source feeds, geolocates every story, and renders the result
on an interactive Leaflet map with marker clustering. On top of that:

- **Bayesian trend scoring** over article volume, so emerging stories surface
  ahead of the mainstream.
- **An LLM research agent** (`agent.py`, `deep_research.py`) that answers
  open-ended questions across the ingested corpus.
- **Prediction markets** — wallets, bets and a credit ledger, with external
  predictions folded in (`market/`).
- **Telegram and web ingestion**, web search, geocoding, an analytics package,
  and a GTK desktop client alongside the web app.

### Calendar — natural-language scheduling

Controls a local Radicale CalDAV server and turns plain-language requests into
calendar events. Handles recurrence rules, a day timeline, conflict resolution,
and slot optimisation, and can talk to MCP servers.

### Money Tracker

Categories, transactions and summaries over a local SQLite database.

### Diagnostic — Linux troubleshooter

A GTK system troubleshooter: gathers system information and runs the checks that
usually need root, so you can diagnose a problem before you escalate it.

### Privacy Dashboard

Camera and microphone monitor — see exactly what is holding the device open.

### Cloud

A multi-backend local cloud hub.

### LLM Brain · Voice MCP

Scaffolds for a local LLM backend and a push-to-record Whisper pipeline.

## Quick start

```bash
git clone https://github.com/ksohos333/local-butler.git
cd local-butler

uv sync          # or: python3 -m venv .venv && pip install -e '.[dev]'

uv run local-butler geonews
```

Then open <http://127.0.0.1:8788/>. Other apps are started the same way —
`uv run local-butler money`, `uv run local-butler calendar`, and so on.

Run the desktop clients directly for the GTK apps:

```bash
uv run python -m local_butler.apps.geonews.main_gtk
```

## Architecture

```text
src/local_butler/
├── core/
│   ├── database.py      # Shared engine, session and declarative Base
│   └── schemas.py       # Shared Pydantic schemas
├── apps/
│   ├── geonews/         # News intelligence, map, agent, markets
│   ├── calendar/        # CalDAV + natural-language scheduling
│   ├── money_tracker/   # Categories and transactions
│   ├── diagnostic/      # GTK Linux troubleshooter
│   ├── privacy_dashboard/
│   ├── cloud/
│   ├── llm_brain/       # scaffold
│   └── voice_mcp/       # scaffold
└── alembic/versions/    # Schema migrations
```

Every app has its own `models.py` with explicit `__tablename__`s, so several
applications can share one database without colliding in the SQLAlchemy
registry.

## Testing

```bash
uv run pytest
```

393 tests cover the API layer, the data model, the scheduling logic and the
ingestion pipelines:

```text
tests/
├── calendar/       # recurrence, timeline, panels, optimisation, assistant
├── diagnostic/
├── geonews/        # market and chat endpoints
├── money_tracker/  # CRUD and summaries
└── geonews_smoke_test.py
```

## Data

Everything lives under `~/.local/share/local-butler/`. Nothing is sent
anywhere. GeoNews pulls from the feeds listed in
`src/local_butler/apps/geonews/sources.toml`.

## Design notes

`docs/` holds the reasoning behind the structure, including
[ADR-001-layout.md](docs/ADR-001-layout.md), the calendar and cloud design
notes, and a diagnostic code review.

## License

MIT — see [LICENSE](LICENSE).
