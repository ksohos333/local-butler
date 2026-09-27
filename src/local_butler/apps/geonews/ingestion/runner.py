"""Ingestion orchestrator."""

import asyncio
import logging
from datetime import datetime, timezone

from local_butler.apps.geonews.ingestion.config import enabled_sources
from local_butler.apps.geonews.ingestion.rss import fetch_rss
from local_butler.apps.geonews.ingestion.scrape import fetch_scrape_simple
from local_butler.apps.geonews.models import IngestionLog
from local_butler.core.database import SessionLocal

logger = logging.getLogger(__name__)


async def ingest_all() -> list[dict]:
    """Run ingestion for all enabled sources."""
    sources = enabled_sources()
    results: list[dict] = []

    for source_id, config in sources.items():
        logger.info("Ingesting source: %s", source_id)
        source_type = config["type"]

        if source_type == "rss":
            fetched, new, errors, error_message = await fetch_rss(config)
        elif source_type == "scrape_simple":
            fetched, new, errors, error_message = await fetch_scrape_simple(config)
        elif source_type == "scrape_playwright":
            fetched, new, errors, error_message = 0, 0, 0, "Playwright scrapers enabled in Phase 4"
        elif source_type == "telegram":
            # Telegram-driven ingestion is handled by the bot polling task
            # started in the FastAPI lifespan. This runner loop still touches
            # telegram sources so their health appears in the ingestion logs.
            fetched, new, errors, error_message = 0, 0, 0, "Telegram bot handles this source"
        else:
            fetched, new, errors, error_message = 0, 0, 1, f"Unknown type {source_type}"

        db = SessionLocal()
        try:
            log = IngestionLog(
                source_key=source_id,
                source_type=source_type,
                items_fetched=fetched,
                items_new=new,
                errors=errors,
                error_message=error_message,
                run_at=datetime.now(timezone.utc),
            )
            db.add(log)
            db.commit()
        finally:
            db.close()

        results.append(
            {
                "source": source_id,
                "fetched": fetched,
                "new": new,
                "errors": errors,
                "error": error_message,
            }
        )

    return results


async def background_ingest_loop(interval_seconds: int = 300) -> None:
    """Run ingestion continuously in the background."""
    while True:
        try:
            await ingest_all()
        except Exception:  # noqa: BLE001
            logger.exception("Background ingestion loop failed")
        await asyncio.sleep(interval_seconds)
