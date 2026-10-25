"""FastAPI application for Geonews."""

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import certifi
import httpx

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy import func, text as sa_text
from sqlalchemy.orm import Session

from local_butler.apps.geonews import models
from local_butler.apps.geonews.ingestion.config import enabled_sources
from local_butler.apps.geonews.agent import GeonewsAgent
from local_butler.apps.geonews.deep_research import DeepResearcher
from local_butler.apps.geonews.llm import (
    AGENT_TOOLS,
    DEFAULT_LLM_URL,
    DEFAULT_OLLAMA_URL,
    LLMUnavailable,
    LocalLLM,
    build_news_context,
)
from local_butler.apps.geonews.memory import get_store
from local_butler.apps.geonews.ingestion.runner import (
    background_ingest_loop,
    ingest_all,
)
from local_butler.apps.geonews.ingestion.telegram import (
    TelegramIngestor,
    get_bot_token,
)
from local_butler.apps.geonews.market.external import sync_external_predictions
from local_butler.apps.geonews.market.routes import router as market_router
from local_butler.apps.geonews.processing.pipeline import (
    process_unprocessed_articles,
)
from local_butler.apps.geonews.schemas import (
    FeedResponse,
    GeoResponse,
    IngestResponse,
    ProcessResponse,
    StatsResponse,
    TrendsResponse,
)
from local_butler.core.database import engine, get_db
from local_butler.core.database import SessionLocal


class AgentChatRequest(BaseModel):
    message: str
    # Which chat this turn belongs to. Omitted or stale means "start a new one",
    # so the client never has to care whether the id is still valid.
    session_id: int | None = None
    # Optional article context so the answer can be grounded in what the user
    # is currently reading instead of the model guessing from memory.
    article_id: int | None = None


logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)


def LLM() -> LocalLLM:
    """Build a client from GEONEWS_LLM_URL / GEONEWS_LLM_MODEL when set."""
    return LocalLLM(
        base_url=os.environ.get("GEONEWS_LLM_URL", DEFAULT_LLM_URL),
        ollama_url=os.environ.get("GEONEWS_OLLAMA_URL", DEFAULT_OLLAMA_URL),
        model=os.environ.get("GEONEWS_LLM_MODEL", ""),
    )
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)

# Database migrations are managed by Alembic; do not call create_all here.
# Run `uv run alembic upgrade head` after schema changes.

# Background task handles, used to cancel on shutdown
_background_tasks: set[asyncio.Task] = set()
_telegram_ingestor: TelegramIngestor | None = None


async def _background_processing_loop(interval: int = 60) -> None:
    while True:
        try:
            await process_unprocessed_articles(batch_size=100)
        except Exception:  # noqa: BLE001
            logger.exception("Processing loop failed")
        await asyncio.sleep(interval)


async def _background_external_markets_loop(interval: int = 300) -> None:
    """Periodically fetch external prediction market prices."""
    # Defer the first sync so startup stays quiet and FastAPI responds quickly.
    await asyncio.sleep(60)
    while True:
        try:
            updated, errors = await sync_external_predictions()
            logger.info(
                "External markets synced: %s updated, %s errors", updated, errors
            )
        except Exception:  # noqa: BLE001
            logger.warning("External markets sync failed", exc_info=False)
        await asyncio.sleep(interval)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start background ingestion, processing, and Telegram bot loops."""
    ingest_task = asyncio.create_task(background_ingest_loop(interval_seconds=300))
    process_task = asyncio.create_task(_background_processing_loop(interval=10))
    external_task = asyncio.create_task(_background_external_markets_loop(interval=300))
    _background_tasks.update({ingest_task, process_task, external_task})

    # Start Telegram bot if a token is configured
    global _telegram_ingestor
    bot_token = get_bot_token()
    telegram_sources = enabled_sources()
    telegram_sources = {
        k: v for k, v in telegram_sources.items() if v.get("type") == "telegram"
    }
    if bot_token and telegram_sources:
        try:
            _telegram_ingestor = TelegramIngestor(bot_token)
            _telegram_ingestor.build_app(telegram_sources)
            asyncio.create_task(_telegram_ingestor.start_polling())
            logger.info("Telegram bot initialized with token")
        except Exception:  # noqa: BLE001
            logger.exception("Failed to start Telegram bot")
    elif telegram_sources:
        logger.warning(
            "Telegram sources are configured but TELEGRAM_BOT_TOKEN is not set. "
            "Create ~/.config/geonews/telegram.env with your token."
        )

    yield

    for task in _background_tasks:
        task.cancel()

    if _telegram_ingestor is not None:
        await _telegram_ingestor.stop()


app = FastAPI(title="Geonews", lifespan=lifespan)
app.include_router(market_router)

# Static files and templates
BASE_DIR = Path(__file__).parent
app.mount(
    "/static",
    StaticFiles(directory=str(BASE_DIR / "static")),
    name="static",
)
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request, "index.html", {})


@app.get("/api/events", response_model=FeedResponse)
async def list_events(
    db: Session = Depends(get_db),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    event_type: str | None = None,
    region: str | None = None,
    source: str | None = None,
    search: str | None = None,
):
    query = db.query(models.Article)
    if event_type:
        query = query.filter(models.Article.event_type == event_type)
    if region:
        query = query.filter(models.Article.source_region.ilike(f"%{region}%"))
    if source:
        query = query.filter(models.Article.source_key == source)
    if search:
        pattern = f"%{search}%"
        query = query.filter(
            models.Article.title.ilike(pattern)
            | models.Article.summary.ilike(pattern)
            | models.Article.body.ilike(pattern)
            | models.Article.source_name.ilike(pattern)
            | models.Article.location_name.ilike(pattern)
        )

    total = query.count()
    items = (
        query.order_by(models.Article.fetched_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )

    return FeedResponse(
        items=items,
        total=total,
        page=page,
        page_size=page_size,
    )


@app.get("/api/events/geo", response_model=GeoResponse)
async def events_geo(
    db: Session = Depends(get_db),
    bbox: str | None = None,
    event_type: str | None = None,
    search: str | None = None,
):
    """Return GeoJSON of articles with coordinates."""
    query = db.query(models.Article).filter(
        models.Article.latitude.isnot(None),
        models.Article.longitude.isnot(None),
    )
    if event_type:
        query = query.filter(models.Article.event_type == event_type)

    if search:
        pattern = f"%{search}%"
        query = query.filter(
            models.Article.title.ilike(pattern)
            | models.Article.summary.ilike(pattern)
            | models.Article.body.ilike(pattern)
            | models.Article.source_name.ilike(pattern)
            | models.Article.location_name.ilike(pattern)
        )

    if bbox:
        try:
            west, south, east, north = [float(x) for x in bbox.split(",")]
            query = query.filter(
                models.Article.longitude >= west,
                models.Article.longitude <= east,
                models.Article.latitude >= south,
                models.Article.latitude <= north,
            )
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid bbox format")  # noqa: B904

    items = query.order_by(models.Article.fetched_at.desc()).limit(500).all()

    features = []
    for item in items:
        features.append(
            {
                "type": "Feature",
                "geometry": {
                    "type": "Point",
                    "coordinates": [item.longitude, item.latitude],
                },
                "properties": {
                    "id": item.id,
                    "title": item.title,
                    "event_type": item.event_type,
                    "severity": item.severity,
                    "source_key": item.source_key,
                    "source_name": item.source_name,
                    "location_name": item.location_name,
                    "published_at": (
                        item.published_at.isoformat() if item.published_at else None
                    ),
                },
            }
        )

    return GeoResponse(type="FeatureCollection", features=features)


@app.get("/api/events/{article_id}")
async def get_event(article_id: int, db: Session = Depends(get_db)):
    article = db.query(models.Article).filter(models.Article.id == article_id).first()
    if not article:
        raise HTTPException(status_code=404, detail="Article not found")
    return article


@app.post("/api/summarize/{article_id}")
async def summarize_article(article_id: int, db: Session = Depends(get_db)):
    """Return a 3-paragraph summary of an article using a local Ollama model."""
    article = db.query(models.Article).filter(models.Article.id == article_id).first()
    if not article:
        raise HTTPException(status_code=404, detail="Article not found")

    context = build_news_context(
        title=article.title,
        body=article.body or "",
        summary=article.summary or "",
        sources=[{"name": article.source_name, "url": article.url or ""}]
        if article.source_name
        else None,
    )

    prompt = (
        "Summarize the following news article in at most three paragraphs. "
        "Paragraph 1: what happened. Paragraph 2: where and who is involved. "
        "Paragraph 3: why it matters globally. Keep the tone neutral, factual, and concise.\n\n"
        + context
    )

    # Blocking urllib call inside a thread: llama-server takes many seconds to
    # answer and would otherwise stall the event loop.
    try:
        summary = await asyncio.to_thread(
            LLM().complete,
            [
                {"role": "system", "content": "You are a precise news summarizer."},
                {"role": "user", "content": prompt},
            ],
            max_tokens=900,
            temperature=0.3,
        )
    except LLMUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    return {"summary": summary}


@app.get("/api/trends", response_model=TrendsResponse)
async def get_trends(
    db: Session = Depends(get_db),
    days: int = Query(7, ge=1, le=90),
    region: str | None = None,
    event_type: str | None = None,
):
    """Return trend analysis: time-series, type deltas, region deltas, hotspots."""
    now = datetime.now(timezone.utc)
    current_start = now - timedelta(days=days)
    previous_start = now - timedelta(days=days * 2)

    def _apply_filters(query):
        if region:
            query = query.filter(models.Article.source_region.ilike(f"%{region}%"))
        if event_type:
            query = query.filter(models.Article.event_type == event_type)
        return query

    current_query = _apply_filters(
        db.query(models.Article).filter(models.Article.fetched_at >= current_start)
    )
    previous_query = _apply_filters(
        db.query(models.Article).filter(
            models.Article.fetched_at >= previous_start,
            models.Article.fetched_at < current_start,
        )
    )

    total = current_query.count()
    previous_total = previous_query.count()

    time_rows = (
        current_query.with_entities(
            func.date(models.Article.fetched_at).label("d"),
            func.count().label("c"),
        )
        .group_by(func.date(models.Article.fetched_at))
        .order_by(func.date(models.Article.fetched_at))
        .all()
    )
    time_series = [{"date": str(row.d), "count": row.c} for row in time_rows]

    def _delta_pct(cur: int, prev: int) -> float:
        if prev > 0:
            return round(((cur - prev) / prev) * 100.0, 1)
        return 100.0 if cur > 0 else 0.0

    type_current = {
        (row[0] or "unknown"): row[1]
        for row in current_query.with_entities(
            models.Article.event_type, func.count()
        ).group_by(models.Article.event_type).all()
    }
    type_previous = {
        (row[0] or "unknown"): row[1]
        for row in previous_query.with_entities(
            models.Article.event_type, func.count()
        ).group_by(models.Article.event_type).all()
    }
    type_distribution = []
    for typ in sorted(set(type_current) | set(type_previous)):
        cur = type_current.get(typ, 0)
        prev = type_previous.get(typ, 0)
        type_distribution.append(
            {
                "event_type": typ,
                "current": cur,
                "previous": prev,
                "delta": cur - prev,
                "delta_pct": _delta_pct(cur, prev),
            }
        )

    region_current = {
        (row[0] or "Unknown"): row[1]
        for row in current_query.with_entities(
            models.Article.source_region, func.count()
        ).group_by(models.Article.source_region).all()
    }
    region_previous = {
        (row[0] or "Unknown"): row[1]
        for row in previous_query.with_entities(
            models.Article.source_region, func.count()
        ).group_by(models.Article.source_region).all()
    }
    region_trends = []
    for reg in sorted(set(region_current) | set(region_previous)):
        if not reg:
            continue
        cur = region_current.get(reg, 0)
        prev = region_previous.get(reg, 0)
        region_trends.append(
            {
                "region": reg,
                "current": cur,
                "previous": prev,
                "delta": cur - prev,
                "delta_pct": _delta_pct(cur, prev),
            }
        )
    region_trends.sort(key=lambda r: r["current"], reverse=True)

    hotspot_query = _apply_filters(
        db.query(models.Article)
        .filter(
            models.Article.latitude.isnot(None),
            models.Article.longitude.isnot(None),
        )
        .filter(models.Article.fetched_at >= current_start)
        .order_by(models.Article.fetched_at.desc())
        .limit(50)
    )
    hotspots = hotspot_query.all()

    return TrendsResponse(
        days=days,
        total=total,
        previous_total=previous_total,
        time_series=time_series,
        type_distribution=type_distribution,
        region_trends=region_trends[:20],
        hotspots=hotspots,
    )


@app.get("/api/agent/notes")
async def agent_notes(limit: int = 100):
    """What the agent currently remembers, so the user can inspect and correct it.

    A memory the user cannot see or delete is a memory they cannot trust.
    """
    return {"notes": get_store().list_notes(limit=limit)}


@app.delete("/api/agent/notes/{note_id}")
async def agent_delete_note(note_id: int):
    deleted = get_store().delete_note(note_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="No such note")
    return {"deleted": note_id}


@app.get("/api/agent/health")
async def agent_health():
    """Report which local model backend the agent uses, and what it can reach."""
    llm = LLM()
    model = llm.first_model() if llm.llama_available() else ""
    store = get_store()
    return {
        "backend": llm.backend(),
        "model": model,
        "tools": [t["function"]["name"] for t in AGENT_TOOLS],
        "memory": {
            "notes": len(store.list_notes(limit=1000)),
            "events": len(store.recent_events(hours=24 * 365, limit=1000)),
        },
        "capabilities": {
            "corpus_articles": _article_count(),
            "live_news_sources": _feed_count(),
            "wikipedia": True,
            "web_search": "best-effort (DuckDuckGo rate-limits this host)",
        },
        "ollama_models": llm.ollama_models() if not model else [],
    }


def _article_count() -> int:
    try:
        with engine.connect() as conn:
            return conn.execute(sa_text("SELECT COUNT(*) FROM geonews_articles")).scalar() or 0
    except Exception:  # noqa: BLE001
        return 0


def _feed_count() -> int:
    try:
        return len(enabled_sources())
    except Exception:  # noqa: BLE001
        return 0


@app.get("/api/agent/chats")
async def agent_chats(limit: int = 50):
    """Past conversations, newest first.

    Chat history used to live only in the DOM: a reload lost it, and there was no
    way to go back to a question asked yesterday.
    """
    return {"chats": get_store().list_chats(limit=limit)}


@app.post("/api/agent/chats")
async def agent_new_chat():
    """Open an empty chat. This is what the New chat button calls."""
    return {"chat": get_store().create_chat()}


@app.get("/api/agent/chats/{chat_id}")
async def agent_chat_history(chat_id: int):
    """One chat with its full transcript, oldest turn first."""
    chat = get_store().get_chat(chat_id)
    if chat is None:
        raise HTTPException(status_code=404, detail="No such chat")
    return chat


class AgentChatRename(BaseModel):
    title: str


@app.patch("/api/agent/chats/{chat_id}")
async def agent_rename_chat(chat_id: int, req: AgentChatRename):
    """Retitle a chat. Auto-titles come from the first question, which is often
    a long one the user would rather name differently."""
    if not get_store().rename_chat(chat_id, req.title):
        raise HTTPException(status_code=404, detail="No such chat")
    return {"chat": get_store().get_chat(chat_id)}


@app.delete("/api/agent/chats/{chat_id}")
async def agent_delete_chat(chat_id: int):
    """Remove a chat and every turn in it."""
    if not get_store().delete_chat(chat_id):
        raise HTTPException(status_code=404, detail="No such chat")
    return {"deleted": chat_id}


def _resolve_chat(chat_id: int | None) -> tuple[int, list[dict]]:
    """Find or start the chat this request belongs to, and load its history.

    Returns (chat_id, history). Falls back to opening a fresh chat whenever the id
    is missing or stale, so a deleted chat in another tab degrades to a new
    conversation instead of a 404 in the middle of a question.
    """
    store = get_store()
    if chat_id is not None:
        try:
            history = store.chat_history(chat_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not load chat %s: %s", chat_id, exc)
            history = []
        if history or store.get_chat(chat_id):
            return chat_id, history
        chat_id = None
    return store.create_chat()["id"], []


@app.post("/api/agent/chat")
async def agent_chat(req: AgentChatRequest, db: Session = Depends(get_db)):
    """Answer a question using tools: own corpus, live wires, web, memory.

    This used to proxy through local-brain to TrueForge, which spawns its own
    llama-server and could not fit alongside the resident one (CUDA OOM). It then
    answered current-events questions from weights alone, producing "I don't have
    access to real-time news". Now it calls the resident model with tools and
    requires a search for anything recent.

    Every turn is persisted to the chat the ``session_id`` names, so the history
    survives a reload and a resumed chat can refer back to earlier questions.
    """

    async def _stream():
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()
        DONE = object()

        def produce() -> None:
            def emit(kind: str, payload) -> None:
                # The producer runs on a worker thread; the queue belongs to the
                # event loop, so every put is hopped across.
                loop.call_soon_threadsafe(queue.put_nowait, (kind, payload))

            try:
                chat_id, history = _resolve_chat(req.session_id)
                emit("chat", {"session_id": chat_id})
                store = get_store()
                store.add_message(chat_id, "user", req.message)
                # Fresh SQLAlchemy session: the request-scoped one belongs to the
                # event loop thread and this runs on a worker thread.
                session = SessionLocal()
                try:
                    agent = GeonewsAgent(
                        session,
                        progress=lambda message: emit("progress", message),
                        history=history,
                    )
                    answer, meta = agent.run(req.message, req.article_id)
                    store.add_message(
                        chat_id, "assistant", answer, meta=_turn_meta(meta)
                    )
                    emit("result", (answer, meta))
                finally:
                    session.close()
            except LLMUnavailable as exc:
                emit("error", str(exc))
            except Exception as exc:  # noqa: BLE001 - surface, never crash the stream
                logger.exception("Agent chat failed")
                emit("error", str(exc))
            finally:
                emit("done", None)

        task = loop.run_in_executor(None, produce)

        yield _sse({"type": "thinking"})

        result = None
        error = None
        chat_event = None
        while True:
            kind, payload = await queue.get()
            if kind == "progress":
                yield _sse({"type": "progress", "content": payload})
            elif kind == "chat":
                chat_event = payload
            elif kind == "result":
                result = payload
            elif kind == "error":
                error = payload
            elif kind == "done":
                break

        await task

        # Sent even on error: the user's question was persisted, so the id has to
        # reach the browser for the turn to be findable afterwards.
        if chat_event:
            yield _sse({"type": "chat", **chat_event})

        if error:
            yield _sse({"type": "error", "content": error})
            yield _sse({"type": "done"})
            return

        answer, meta = result
        if meta.get("tools"):
            # Tell the user what was actually consulted; silent browsing makes a
            # grounded answer indistinguishable from a guess.
            names = ", ".join(sorted(set(meta["tools"])))
            yield _sse({"type": "sources", "content": f"Consulted: {names}"})
        for start in range(0, len(answer), 90):
            yield _sse({"type": "delta", "content": answer[start : start + 90]})
        yield _sse({"type": "done"})

    return StreamingResponse(_stream(), media_type="text/event-stream")


def _turn_meta(meta: dict) -> dict:
    """The parts of a turn's metadata worth keeping in the transcript.

    The full meta carries search strings and research angles that are noise in a
    history view; the tool names are what later let you see why an old answer was
    trustworthy.
    """
    return {
        "tools": list(meta.get("tools", [])),
        "notes": list(meta.get("notes", [])),
        "forced_search": bool(meta.get("forced_search")),
    }


class AgentResearchRequest(BaseModel):
    question: str
    # Reports are long enough that losing them on reload would waste the pass, so
    # they land in a chat like any other turn.
    session_id: int | None = None


@app.post("/api/agent/research")
async def agent_research(req: AgentResearchRequest):
    """Run a full deep-research pass and stream progress while it works.

    Separate from ``/api/agent/chat`` because this takes a minute or more: the
    research stages are pushed over SSE as they complete, so the user watches the
    plan and gather steps instead of staring at a spinner. Progress crosses a
    thread boundary, so it goes through the loop's queue like the final result.
    """
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()
    DONE = object()

    async def _stream():
        def produce() -> None:
            def emit(kind: str, payload) -> None:
                loop.call_soon_threadsafe(queue.put_nowait, (kind, payload))

            try:
                chat_id, _ = _resolve_chat(req.session_id)
                emit("chat", {"session_id": chat_id})
                store = get_store()
                store.add_message(chat_id, "user", req.question)
                session = SessionLocal()
                try:
                    researcher = DeepResearcher(
                        session, progress=lambda message: emit("progress", message)
                    )
                    result, meta = researcher.research(req.question)
                    store.add_message(
                        chat_id,
                        "assistant",
                        result,
                        meta={
                            "deep_research": True,
                            "angles": len(meta.get("angles", [])),
                            "articles_read": meta.get("articles_read", 0),
                            "sources": meta.get("sources", [])[:12],
                        },
                    )
                    emit("result", (result, meta))
                finally:
                    session.close()
            except LLMUnavailable as exc:
                emit("error", str(exc))
            except Exception as exc:  # noqa: BLE001 - surface, never crash
                logger.exception("Deep research failed")
                emit("error", str(exc))
            finally:
                emit("done", None)

        task = loop.run_in_executor(None, produce)
        yield _sse({"type": "thinking", "content": "Researching"})

        result = None
        error = None
        chat_event = None
        while True:
            kind, payload = await queue.get()
            if kind == "progress":
                yield _sse({"type": "progress", "content": payload})
            elif kind == "chat":
                chat_event = payload
            elif kind == "result":
                result = payload
            elif kind == "error":
                error = payload
            elif kind == "done":
                break

        await task

        if chat_event:
            yield _sse({"type": "chat", **chat_event})

        if error:
            yield _sse({"type": "error", "content": error})
            yield _sse({"type": "done"})
            return

        report_text, meta = result
        yield _sse({
            "type": "sources",
            "content": (
                f"Deep research: {len(meta.get('angles', []))} angles, "
                f"{meta.get('articles_read', 0)} articles read"
            ),
        })
        for start in range(0, len(report_text), 90):
            yield _sse({"type": "delta", "content": report_text[start : start + 90]})
        yield _sse({"type": "done"})

    return StreamingResponse(_stream(), media_type="text/event-stream")


def _sse(payload: dict) -> bytes:
    return f"data: {json.dumps(payload)}\n\n".encode("utf-8")


@app.get("/api/stats", response_model=StatsResponse)
async def get_stats(db: Session = Depends(get_db)):
    total = db.query(func.count(models.Article.id)).scalar() or 0
    primary = (
        db.query(func.count(models.Article.id))
        .filter(models.Article.source_is_primary == True)
        .scalar()
        or 0
    )
    last = (
        db.query(models.IngestionLog.run_at)
        .order_by(models.IngestionLog.run_at.desc())
        .first()
    )
    last_dt = last[0] if last else None

    event_types = {
        row[0] or "unknown": row[1]
        for row in db.query(
            models.Article.event_type, func.count(models.Article.id)
        ).group_by(models.Article.event_type)
    }

    return StatsResponse(
        total_articles=total,
        primary_sources=primary,
        last_ingestion=last_dt,
        event_types=event_types,
    )


@app.post("/api/ingest", response_model=IngestResponse)
async def trigger_ingest():
    """Trigger an immediate ingestion run (non-blocking)."""
    asyncio.create_task(ingest_all())
    return IngestResponse(started=True, message="Ingestion started in background")


@app.post("/api/process", response_model=ProcessResponse)
async def trigger_process():
    """Trigger immediate article processing (classification + geocoding)."""
    processed = await process_unprocessed_articles(batch_size=1000)
    return ProcessResponse(
        processed=processed,
        message=f"Processed {processed} unclassified articles",
    )


@app.get("/api/source-health")
async def source_health(db: Session = Depends(get_db)):
    """Return last ingestion run per source."""
    from sqlalchemy import func

    subq = (
        db.query(
            models.IngestionLog.source_key,
            func.max(models.IngestionLog.run_at).label("last_run"),
            func.sum(models.IngestionLog.items_new).label("total_new"),
            func.sum(models.IngestionLog.errors).label("total_errors"),
        )
        .group_by(models.IngestionLog.source_key)
        .subquery()
    )

    rows = (
        db.query(
            models.IngestionLog.source_key,
            models.IngestionLog.source_type,
            subq.c.last_run,
            subq.c.total_new,
            subq.c.total_errors,
        )
        .join(subq, models.IngestionLog.source_key == subq.c.source_key)
        .filter(models.IngestionLog.run_at == subq.c.last_run)
        .order_by(subq.c.last_run.desc())
        .all()
    )

    return {
        "sources": [
            {
                "source_key": r.source_key,
                "source_type": r.source_type,
                "last_run": r.last_run.isoformat() if r.last_run else None,
                "items_new": r.total_new or 0,
                "errors": r.total_errors or 0,
            }
            for r in rows
        ]
    }
