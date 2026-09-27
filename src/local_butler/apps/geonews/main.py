"""FastAPI application for Geonews."""

import asyncio
import json
import logging
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
from sqlalchemy import func
from sqlalchemy.orm import Session

from local_butler.apps.geonews import models
from local_butler.apps.geonews.ingestion.config import enabled_sources
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


class AgentChatRequest(BaseModel):
    message: str
    session_id: str | None = None


logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)
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

    text = article.body or article.summary or article.title
    prompt = (
        "Summarize the following news article in at most three paragraphs. "
        "Paragraph 1: what happened. Paragraph 2: where and who is involved. "
        "Paragraph 3: why it matters globally. Keep the tone neutral, factual, and concise.\n\n"
        f"Title: {article.title}\n\n{text[:8000]}"
    )

    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            resp = await client.post(
                "http://127.0.0.1:11434/api/generate",
                json={"model": "llama3.2:3b", "prompt": prompt, "stream": False},
            )
            resp.raise_for_status()
            data = resp.json()
            return {"summary": data.get("response", "").strip()}
    except httpx.ConnectError as exc:
        raise HTTPException(
            status_code=503,
            detail="Ollama is not running. Install and start it, then try again.",
        ) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Ollama request failed: {exc}",
        ) from exc


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


@app.post("/api/agent/chat")
async def agent_chat(req: AgentChatRequest):
    """Proxy a chat message to the local bigus_dickus agent (local-brain)."""

    async def _stream():
        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                async with client.stream(
                    "POST",
                    "http://127.0.0.1:9876/api/chat",
                    json={"message": req.message, "session_id": req.session_id},
                    headers={"Content-Type": "application/json"},
                ) as resp:
                    async for chunk in resp.aiter_raw():
                        yield chunk
        except Exception as exc:
            logger.warning("Agent chat proxy error: %s", exc)
            payload = json.dumps({"type": "error", "content": f"Agent proxy error: {exc}"})
            yield f"data: {payload}\n\n".encode("utf-8")

    return StreamingResponse(_stream(), media_type="text/event-stream")


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
