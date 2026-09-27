"""RSS ingestion for primary sources."""

import hashlib
import logging
from datetime import datetime, timezone
from typing import Any

import certifi
import feedparser
import httpx

from local_butler.apps.geonews.models import Article, IngestionLog
from local_butler.core.database import SessionLocal

logger = logging.getLogger(__name__)


def _parse_dt(entry: feedparser.FeedParserDict) -> datetime | None:
    """Best-effort parse a feed entry date."""
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        if key in entry:
            struct = entry[key]
            if struct:
                return datetime(*struct[:6], tzinfo=timezone.utc)
    return None


def _content_hash(config: dict[str, Any], title: str, url: str | None) -> str:
    """Stable hash for deduplication."""
    payload = f"{config.get('source_id', '')}::{title}::{url or ''}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


async def fetch_rss(config: dict[str, Any]) -> tuple[int, int, int, str | None]:
    """Fetch one RSS source and persist new articles.

    Returns (fetched, new, errors, error_message).
    """
    source_id = config["source_id"]
    source_name = source_id.replace("_", " ").title()

    try:
        async with httpx.AsyncClient(
            timeout=30, verify=certifi.where(), follow_redirects=True
        ) as client:
            headers = {
                "User-Agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
                ),
                # Several publishers (arabnews.com) answer 403 to a plain
                # rss-oriented Accept but serve fine to browser-like headers,
                # so send browser headers rather than a feed-specific Accept.
                "Accept": (
                    "text/html,application/xhtml+xml,application/xml;q=0.9,"
                    "image/avif,image/webp,*/*;q=0.8"
                ),
                "Accept-Language": "en-US,en;q=0.9",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none",
                "Upgrade-Insecure-Requests": "1",
            }
            response = await client.get(config["url"], headers=headers)
            response.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        logger.exception("RSS fetch failed for %s", source_id)
        return 0, 0, 1, str(exc)

    parsed = feedparser.parse(response.text)
    fetched = len(parsed.entries)
    new = 0
    errors = 0

    db = SessionLocal()
    try:
        for entry in parsed.entries:
            try:
                title = entry.get("title", "").strip()
                link = entry.get("link", "").strip() or None
                body = entry.get("summary", entry.get("description", "")).strip()
                if not title:
                    continue

                content_hash = _content_hash(config, title, link)

                exists = (
                    db.query(Article.id)
                    .filter(Article.content_hash == content_hash)
                    .first()
                )
                if exists:
                    continue

                article = Article(
                    source_key=source_id,
                    source_type="rss",
                    source_name=source_name,
                    source_category=config.get("category"),
                    source_region=config.get("region"),
                    source_language=config.get("language"),
                    source_is_primary=True,
                    title=title,
                    summary=body[:1024] if body else None,
                    body=body if body else None,
                    url=link,
                    published_at=_parse_dt(entry),
                    content_hash=content_hash,
                    event_type="other",
                    severity=5,
                    processed=False,
                    metadata_json={
                        "feed_title": parsed.feed.get("title"),
                        "feed_link": parsed.feed.get("link"),
                        "entry_id": entry.get("id"),
                    },
                )
                db.add(article)
                new += 1
            except Exception:  # noqa: BLE001
                logger.exception("Failed to process RSS entry in %s", source_id)
                errors += 1

        db.commit()
    finally:
        db.close()

    return fetched, new, errors, None
