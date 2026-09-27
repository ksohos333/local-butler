"""Simple HTTP scraping for primary-source listing pages."""

import hashlib
import logging
from urllib.parse import urljoin

import certifi
import httpx
from bs4 import BeautifulSoup

from local_butler.apps.geonews.models import Article
from local_butler.core.database import SessionLocal

logger = logging.getLogger(__name__)


def _resolve_url(base: str, href: str) -> str:
    return urljoin(base, href.split("#")[0])


def _content_hash(config: dict, title: str, url: str | None) -> str:
    payload = f"{config.get('source_id', '')}::{title}::{url or ''}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _extract_paragraphs(soup: BeautifulSoup) -> str:
    """Best-effort article body extraction from generic paragraphs."""
    # Prefer article/main/content divs, fall back to all paragraphs
    for selector in ("article", "main", "[role='main']", "#content", ".content"):
        container = soup.select_one(selector)
        if container:
            paragraphs = container.find_all("p")
            break
    else:
        paragraphs = soup.find_all("p")

    texts = [p.get_text(strip=True) for p in paragraphs if len(p.get_text(strip=True)) > 40]
    return "\n\n".join(texts)


async def _fetch_page(client: httpx.AsyncClient, url: str) -> BeautifulSoup | None:
    try:
        headers = {"User-Agent": "GeonewsBot/1.0 (+https://localhost)"}
        response = await client.get(url, headers=headers, timeout=30)
        response.raise_for_status()
        return BeautifulSoup(response.text, "lxml")
    except Exception:  # noqa: BLE001
        logger.exception("Failed to fetch %s", url)
        return None


async def fetch_scrape_simple(config: dict) -> tuple[int, int, int, str | None]:
    """Scrape a simple HTML listing page."""
    source_id = config["source_id"]
    source_name = source_id.replace("_", " ").title()
    listing_url = config["url"]
    selector = config.get("link_selector", "a")

    async with httpx.AsyncClient(
        timeout=30, verify=certifi.where(), follow_redirects=True
    ) as client:
        listing_soup = await _fetch_page(client, listing_url)
        if listing_soup is None:
            return 0, 0, 1, f"Failed to fetch listing {listing_url}"

        link_tags = listing_soup.select(selector)
        # Limit to first 20 links to keep ingestion bounded
        links: list[tuple[str, str]] = []
        for tag in link_tags:
            href = tag.get("href")
            text = tag.get_text(strip=True)
            if not href or not text or len(text) < 10:
                continue
            url = _resolve_url(listing_url, href)
            if url in (u for u, _ in links):
                continue
            links.append((url, text))
            if len(links) >= 20:
                break

    fetched = len(links)
    new = 0
    errors = 0

    db = SessionLocal()
    try:
        async with httpx.AsyncClient(
            timeout=30, verify=certifi.where(), follow_redirects=True
        ) as client:
            for url, link_text in links:
                try:
                    page_soup = await _fetch_page(client, url)
                    if page_soup is None:
                        errors += 1
                        continue

                    title_tag = page_soup.find("title")
                    title = (title_tag.get_text(strip=True) if title_tag else link_text) or link_text
                    body = _extract_paragraphs(page_soup)

                    content_hash = _content_hash(config, title, url)
                    exists = (
                        db.query(Article.id)
                        .filter(Article.content_hash == content_hash)
                        .first()
                    )
                    if exists:
                        continue

                    article = Article(
                        source_key=source_id,
                        source_type="scrape_simple",
                        source_name=source_name,
                        source_category=config.get("category"),
                        source_region=config.get("region"),
                        source_language=config.get("language"),
                        source_is_primary=True,
                        title=title,
                        summary=body[:1024] if body else link_text[:1024],
                        body=body if body else None,
                        url=url,
                        content_hash=content_hash,
                        event_type="other",
                        severity=5,
                        processed=False,
                        metadata_json={"link_text": link_text},
                    )
                    db.add(article)
                    new += 1
                except Exception:  # noqa: BLE001
                    logger.exception("Failed to scrape %s", url)
                    errors += 1

        db.commit()
    finally:
        db.close()

    return fetched, new, errors, None
