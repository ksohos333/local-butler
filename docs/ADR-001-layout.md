# ADR-001: Monorepo Layout and App Scaffold Pattern

## Status

Accepted

## Context

Local Butler began as a simple money-tracker web app but is intended to grow into a
suite of local-first personal automation tools: LLM brain, voice MCP, calendar,
cloud storage, diagnostics, privacy monitoring, etc. This is **your personal local
cloud** — all data stays on the machine under `~/.local/share/local-butler/`.

As apps were added, two distinct patterns emerged:

1. **FastAPI web apps** (`money_tracker`) — share the monorepo's dependency tree
   and run via `uvicorn` from a single CLI entry point.
2. **Standalone GTK3 desktop / system-tray apps** (`calendar`, `cloud`,
   `diagnostic`, `privacy_dashboard`) — require system-level GTK3 / AppIndicator
   / Pillow / psutil libraries that are not suitable as shared monorepo
   dependencies. They run as independent processes with their own venvs and
   `.desktop` launchers.

The repo needs a clearly documented layout so new apps can be added without
ambiguity about where code lives, how it is launched, and what shared
infrastructure it inherits.

## Decision

We will organize the codebase as follows:

### 1. Monorepo root

`~/local-butler/` is a **uv-managed Python monorepo**. `pyproject.toml` declares
dependencies for all FastAPI web apps. GTK3 desktop apps may declare their own
venvs and system dependencies separately.

### 2. App scaffold

Every app lives under `src/local_butler/apps/<name>/` with an `__init__.py` that
exposes a `main()` entry point. Tests live under `tests/<name>/`.

Example created directory structure:

```
src/local_butler/
├── __init__.py              # CLI entry point + APPS dict
├── __main__.py              # Package runner
├── core/
│   ├── database.py          # Shared SQLAlchemy engine + Base
│   └── schemas.py           # Shared Pydantic models
└── apps/
    ├── calendar/              # GTK3 desktop app
    ├── cloud/                 # GTK3 desktop app
    ├── diagnostic/            # GTK3 desktop app
    ├── llm_brain/             # Planned scaffold
    ├── money_tracker/         # FastAPI web app
    ├── privacy_dashboard/     # GTK3 system-tray daemon
    └── voice_mcp/             # Planned scaffold
```

### 3. Explicit architectural constraint — two app types

This is a **constraint**, not a temporary workaround.

#### Type A: FastAPI web app

- Imported and launched via the `APPS` dict in `src/local_butler/__init__.py`.
- Shares the monorepo `.venv` and dependency tree.
- Uses `core/database.py` for SQLAlchemy access and `core/schemas.py` for
  Pydantic models.
- Run with `local-butler <app>` → `uvicorn.run(...)`.
- Templates are flexible, but the convention is `apps/<name>/templates/`.

#### Type B: Standalone GTK3 desktop app

- NOT imported by the CLI entry point.
- Runs as an independent process with its own shebang / venv.
- May declare system-level dependencies (GTK3, pystray, psutil, etc.) outside
  `pyproject.toml`—documented in a README or `requirements-system.txt`.
- Launched via a `.desktop` file or systemd user service.
- Does NOT import from `core/` unless it explicitly depends on the monorepo venv.

#### Why this split

GTK3 / AppIndicator / Pillow have C extensions that can conflict with the
monorepo's Python version and FastAPI dependency tree. The privacy dashboard, for
example, needs system GTK3 bindings and historically used Python 3.9, while the
monorepo requires Python 3.12. Forcing all apps into a single venv would create
an unmaintainable dependency tangle and could prevent the desktop apps from
running under Rocky Linux's system libraries.

### 4. Shared core

`core/database.py` provides:

- A single SQLite database at `~/.local/share/local-butler/butler.db`.
- The SQLAlchemy engine with `check_same_thread=False` (required for SQLite
  across FastAPI background threads).
- The declarative `Base` class and the `get_db()` session generator.

`core/schemas.py` provides shared Pydantic models. New shared types should be
added here; app-specific types stay in the app's own `models.py`.

### 5. Naming conventions

- App directory: `lowercase_with_underscores` (`privacy_dashboard`,
  `money_tracker`).
- Module files: `models.py` (SQLAlchemy), `crud.py` (operations), `main.py`
  (entry point).
- Templates: **flexible** — each app chooses its own location.

## Consequences

### Positive

- Adding a Type A app requires creating one directory, wiring it into the `APPS`
  dict, and no new venv. Web-app dependency management is centralized in
  `pyproject.toml`.
- Adding a Type B app is fully decoupled — no risk of breaking other apps through
  dependency conflicts.
- All apps that need persistence share one SQLite database, one schema namespace.
- The FastAPI / GTK3 split is now explicit, so future apps are not forced into
  the wrong pattern.

### Negative / known gaps

- The `APPS` dict in `src/local_butler/__init__.py` currently only lists
  `money`. `calendar`, `cloud`, `diagnostic`, and `privacy_dashboard` exist but
  are not discoverable via the `local-butler <app>` CLI. Desktop apps need their
  own discovery mechanism (or a separate launcher list in `__init__.py`).
- The privacy dashboard uses its own venv at
  `~/.local/share/uv/privacy-venv/` with a separately specified Python version.
  This works but is not integrated with the monorepo's `uv.lock`.
- Only `money_tracker` has tests. No test coverage exists for `calendar`, `cloud`,
  `diagnostic`, or `privacy_dashboard`.
- `llm_brain` and `voice_mcp` are empty `__init__.py` scaffolds — listed as
  planned but contain no code.

### Compliance checklist

- [ ] Update the `APPS` dict in `src/local_butler/__init__.py` to reflect all
      built apps, or document why desktop apps are excluded.
- [ ] Document the privacy-dashboard venv path in the root README.
- [ ] Create test stubs for untested apps.
- [ ] Mark `llm_brain` and `voice_mcp` as planned in the root README.

## References

- `pyproject.toml` — monorepo dependency declaration.
- `src/local_butler/core/database.py` — shared DB infrastructure.
- `src/local_butler/core/schemas.py` — shared Pydantic models.
- `FUTURE.md` — upcoming phases and backlog.
- `src/local_butler/apps/privacy_dashboard/README.md` — desktop app venv details.
