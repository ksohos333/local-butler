"""External prediction market aggregation.

Fetches current prices from Polymarket and Metaculus and stores them in the
geonews_external_predictions table. Phase 3 only reads public data; no API keys
are required.
"""

import logging
from datetime import datetime, timezone

import certifi
import httpx

from local_butler.apps.geonews.models import ExternalPrediction
from local_butler.core.database import SessionLocal

logger = logging.getLogger(__name__)

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36 GeonewsBot/1.0"
    ),
    "Accept": "application/json",
}

POLYMARKET_ENDPOINT = (
    "https://gamma-api.polymarket.com/events"
    "?active=true&closed=false&limit=50&order=volume&sort=desc"
)

METACULUS_ENDPOINT = (
    "https://www.metaculus.com/api2/questions/?status=open&order_by=-activity"
)

# Metaculus now requires an API token for the questions API. Until one is
# configured the endpoint answers 401/403 on every poll, so the integration is
# off by default. Set METACULUS_ENABLED = True once a token is in place.
METACULUS_ENABLED = False


def _to_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


async def fetch_polymarket() -> list[dict]:
    """Fetch active Polymarket events.

    Returns list of normalized prediction dicts.
    """
    try:
        async with httpx.AsyncClient(
            timeout=30, verify=certifi.where()
        ) as client:
            response = await client.get(
                POLYMARKET_ENDPOINT,
                headers=DEFAULT_HEADERS,
            )
            response.raise_for_status()
            data = response.json()
    except httpx.HTTPStatusError as exc:
        logger.warning(
            "Polymarket returned HTTP %s; skipping external sync",
            exc.response.status_code,
        )
        return []
    except httpx.RequestError as exc:
        logger.info(
            "Polymarket unavailable from this host: %s", type(exc).__name__
        )
        return []
    except Exception:  # noqa: BLE001
        logger.warning("Polymarket fetch failed", exc_info=False)
        return []

    results = []
    for event in data.get("events", []):
        markets = event.get("markets", [{}])
        if not markets:
            continue
        market = markets[0]
        yes_ask = _to_float(market.get("outcomePrices", ["0.5", "0.5"])[0])
        yes_bid = _to_float(market.get("outcomePrices", ["0.5", "0.5"])[0])  # best available
        volume = _to_float(market.get("volume"))

        results.append(
            {
                "platform": "polymarket",
                "platform_market_id": market.get("conditionId"),
                "question": event.get("title") or market.get("question"),
                "yes_price": yes_ask,
                "no_price": 1.0 - (yes_bid or 0.5) if yes_bid else None,
                "probability": yes_ask,
                "volume": volume,
                "url": event.get("url") or f"https://polymarket.com/event/{event.get('slug', '')}",
            }
        )
    return results


async def fetch_metaculus() -> list[dict]:
    """Fetch open Metaculus questions."""
    if not METACULUS_ENABLED:
        logger.info("Metaculus disabled (no API token configured); skipping")
        return []
    try:
        async with httpx.AsyncClient(
            timeout=30, verify=certifi.where()
        ) as client:
            response = await client.get(
                METACULUS_ENDPOINT,
                headers=DEFAULT_HEADERS,
            )
            response.raise_for_status()
            data = response.json()
    except httpx.HTTPStatusError as exc:
        logger.warning(
            "Metaculus returned HTTP %s; skipping external sync",
            exc.response.status_code,
        )
        return []
    except httpx.RequestError as exc:
        logger.info(
            "Metaculus unavailable from this host: %s", type(exc).__name__
        )
        return []
    except Exception:  # noqa: BLE001
        logger.warning("Metaculus fetch failed", exc_info=False)
        return []


    results = []
    for question in data.get("results", []):
        probability = _to_float(question.get("community_prediction"))
        results.append(
            {
                "platform": "metaculus",
                "platform_market_id": str(question.get("id")),
                "question": question.get("title"),
                "yes_price": probability,
                "no_price": 1.0 - probability if probability else None,
                "probability": probability,
                "volume": None,
                "url": f"https://www.metaculus.com/questions/{question.get('id')}/",
            }
        )
    return results


async def fetch_all_external_markets() -> list[dict]:
    """Fetch and normalize all external market data."""
    data = []
    data.extend(await fetch_polymarket())
    data.extend(await fetch_metaculus())
    return data


async def sync_external_predictions(db=None) -> tuple[int, int]:
    """Persist external market snapshots.

    Returns (count_updated, count_errors).
    """
    should_close = False
    if db is None:
        db = SessionLocal()
        should_close = True

    updated = 0
    errors = 0
    try:
        rows = await fetch_all_external_markets()
        for row in rows:
            try:
                existing = (
                    db.query(ExternalPrediction)
                    .filter(
                        ExternalPrediction.platform == row["platform"],
                        ExternalPrediction.question == row["question"],
                    )
                    .first()
                )
                if existing:
                    existing.yes_price = row["yes_price"]
                    existing.no_price = row["no_price"]
                    existing.probability = row["probability"]
                    existing.volume = row["volume"]
                    existing.url = row["url"]
                    existing.updated_at = datetime.now(timezone.utc)
                else:
                    snapshot = ExternalPrediction(
                        platform=row["platform"],
                        platform_market_id=row.get("platform_market_id"),
                        question=row["question"],
                        yes_price=row["yes_price"],
                        no_price=row["no_price"],
                        probability=row["probability"],
                        volume=row["volume"],
                        url=row["url"],
                    )
                    db.add(snapshot)
                updated += 1
            except Exception:  # noqa: BLE001
                logger.exception("Failed to persist external prediction")
                errors += 1
        db.commit()
    finally:
        if should_close:
            db.close()

    return updated, errors
