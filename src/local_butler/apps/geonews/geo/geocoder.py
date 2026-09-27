"""Geocoding with Nominatim and local DB cache."""

import logging
import threading
import time
from datetime import datetime, timezone

from geopy.exc import GeocoderServiceError
from geopy.geocoders import Nominatim

from local_butler.apps.geonews.models import Location
from local_butler.core.database import SessionLocal

logger = logging.getLogger(__name__)

# Nominatim rate limit: max 1 req/sec for free tier.
_last_request_time = 0.0
_rate_limit_lock = threading.Lock()


def _get_geolocator() -> Nominatim:
    return Nominatim(
        user_agent="GeonewsBot/1.0 (local development; contact@example.com)",
        timeout=10,
    )


def _rate_limit() -> None:
    global _last_request_time  # noqa: PLW0603
    with _rate_limit_lock:
        elapsed = time.monotonic() - _last_request_time
        if elapsed < 1.0:
            time.sleep(1.0 - elapsed)
        _last_request_time = time.monotonic()


def geocode(query: str) -> tuple[float, float, str] | None:
    """Geocode a location query, using the DB cache first.

    Returns (lat, lon, display_name) or None.
    """
    normalized = query.strip().lower()
    if not normalized:
        return None

    db = SessionLocal()
    try:
        cached = (
            db.query(Location)
            .filter(Location.query_text == normalized)
            .first()
        )
        if cached:
            return cached.latitude, cached.longitude, cached.display_name
    finally:
        db.close()

    _rate_limit()
    try:
        locator = _get_geolocator()
        result = locator.geocode(normalized)
        if not result:
            return None
        lat, lon = result.latitude, result.longitude
        display_name = result.address or normalized

        db = SessionLocal()
        try:
            cached = Location(
                query_text=normalized,
                latitude=lat,
                longitude=lon,
                display_name=display_name,
                geocoded_at=datetime.now(timezone.utc),
            )
            db.add(cached)
            db.commit()
        finally:
            db.close()

        return lat, lon, display_name
    except GeocoderServiceError:
        logger.exception("Nominatim service error for %r", query)
        return None
    except Exception:  # noqa: BLE001
        logger.exception("Geocoding failed for %r", query)
        return None
