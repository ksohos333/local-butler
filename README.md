# Local Butler

A local-first monorepo for personal automation:

- **Geonews** — primary-source news aggregator with live map and prediction markets.
- **Money Tracker** — simple income/expense web app.
- **LLM Brain** — (scaffold) extend local-brain / TrueForge agent memory.
- **Voice MCP** — (scaffold) push-to-record Whisper transcription for OpenCode.

## Setup

```bash
cd ~/local-butler
uv sync
```

## Run Geonews

From inside the repo:

```bash
cd ~/local-butler
uv run local-butler geonews
```

Or use the global launcher added to your machine:

```bash
geonews
```

Then open http://127.0.0.1:8788/

### Desktop app icon

A `.desktop` entry was created at `~/.local/share/applications/geonews.desktop`
and an icon at `~/.local/share/icons/geonews.svg`. You should now be able to:

- Type **“Geonews”** in the apps menu search.
- Click the map-pin icon to launch Geonews.

If it doesn't appear immediately, refresh the desktop database:

```bash
update-desktop-database ~/.local/share/applications
```

## Run Money Tracker

```bash
local-butler money
# or
uv run python -m local_butler money
```

Then open http://127.0.0.1:8787/

## Run tests

```bash
uv run pytest
```

## Geonews Phase 4 roadmap (saved for later)

- LLM summarization, Fake News Index, bias tagging, entity extraction.
- Replace the hardcoded geo gazetteer with a local NER/geocoding model.
- Social-media viewers and scrapers that do not require a personal login:
  Twitter/X, VK, Weibo, Telegram public channels, and other hard-to-reach
  regions where direct internet access is unreliable.

## Database migrations

Geonews uses Alembic for schema migrations. After pulling updates, run:

```bash
cd ~/local-butler
uv run alembic upgrade head
```

The desktop launcher (`~/.local/bin/geonews`) runs this automatically before starting the server.

## Project structure

```
local-butler/
├── pyproject.toml
├── src/local_butler/
│   ├── __init__.py
│   ├── core/
│   │   ├── database.py
│   │   └── schemas.py
│   └── apps/
│       ├── geonews/
│       │   ├── main.py
│       │   ├── models.py
│       │   ├── schemas.py
│       │   ├── ingestion/
│       │   ├── processing/
│       │   ├── geo/
│       │   ├── market/
│       │   ├── templates/
│       │   └── static/
│       ├── money_tracker/
│       │   ├── main.py
│       │   ├── crud.py
│       │   ├── models.py
│       │   └── templates/index.html
│       ├── llm_brain/
│       └── voice_mcp/
└── tests/
```
