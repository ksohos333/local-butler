"""Article processing pipeline.

Phase 1: classify event type, severity, and location for unprocessed articles.
Phase 4: add summarization, FNI, bias tagging, entity extraction.
"""

import asyncio
import logging
from datetime import datetime, timezone

from local_butler.apps.geonews.geo.classifier import classify_article
from local_butler.apps.geonews.models import Article
from local_butler.core.database import SessionLocal

logger = logging.getLogger(__name__)


async def process_unprocessed_articles(batch_size: int = 100) -> int:
    """Classify and geocode unprocessed articles.

    Returns number processed.
    """
    db = SessionLocal()
    try:
        articles = (
            db.query(Article)
            .filter(Article.processed == False)
            .limit(batch_size)
            .all()
        )

        for article in articles:
            try:
                # Classifier may perform synchronous network I/O (geocoding);
                # run it in a thread so it doesn't block the event loop.
                result = await asyncio.to_thread(
                    classify_article,
                    article.title,
                    article.body or article.summary,
                    article.source_region,
                )
                article.event_type = result["event_type"]
                article.severity = result["severity"]
                article.location_name = result["location_name"]
                article.latitude = result["latitude"]
                article.longitude = result["longitude"]
                article.processed = True
                article.metadata_json = article.metadata_json or {}
                article.metadata_json["processed_at"] = datetime.now(timezone.utc).isoformat()
            except Exception:  # noqa: BLE001
                logger.exception("Failed to process article %s", article.id)
                article.processed = True  # don't retry forever
                article.metadata_json = article.metadata_json or {}
                article.metadata_json["processing_error"] = True

        db.commit()
        return len(articles)
    finally:
        db.close()
