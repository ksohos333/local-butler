"""Lightweight event classification and location extraction.

Phase 1 uses keyword-based classification and a hardcoded location gazetteer.
Phase 4 will replace location extraction with spaCy/LLM + a local geonames gazetteer.
"""

from local_butler.apps.geonews.geo.geocoder import geocode

_EVENT_KEYWORDS = {
    "conflict": [
        "attack", "strike", "missile", "drone", "bomb", "war", "invasion",
        "offensive", "casualties", "killed", "military", "defense", "idf",
        "mod", "army", "forces", "shelling", "ceasefire", "airstrike",
    ],
    "economic": [
        "fed", "rate", "rates", "inflation", "gdp", "economy", "economic",
        "bank", "central bank", "reserve", "monetary", "interest", "cut",
        "hike", "recession", "currency", "trade", "sanctions", "boj", "ecb",
        "rbi", "pboc", "boe",
    ],
    "political": [
        "election", "vote", "president", "minister", "government", "parliament",
        "sanction", "diplomatic", "treaty", "summit", "meeting", "statement",
        "kremlin", "white house", "state department", "commission", "nato",
    ],
    "environmental": [
        "earthquake", "flood", "hurricane", "wildfire", "climate", "storm",
        "drought", "tsunami", "disaster", "who", "disease", "outbreak",
    ],
}

# Hardcoded gazetteer for Phase 1 — avoids Nominatim rate limits/blocks.
# Format: normalized keyword -> (display_name, lat, lon)
_LOCATION_GAZETTEER = {
    "united states": ("United States", 37.0902, -95.7129),
    "usa": ("United States", 37.0902, -95.7129),
    "washington": ("Washington, D.C.", 38.9072, -77.0369),
    "european union": ("European Union", 50.8503, 4.3517),
    "brussels": ("Brussels, Belgium", 50.8503, 4.3517),
    "germany": ("Germany", 51.1657, 10.4515),
    "france": ("France", 46.2276, 2.2137),
    "united kingdom": ("United Kingdom", 55.3781, -3.4360),
    "uk": ("United Kingdom", 55.3781, -3.4360),
    "london": ("London, UK", 51.5074, -0.1278),
    "japan": ("Japan", 36.2048, 138.2529),
    "tokyo": ("Tokyo, Japan", 35.6762, 139.6503),
    "china": ("China", 35.8617, 104.1954),
    "beijing": ("Beijing, China", 39.9042, 116.4074),
    "shanghai": ("Shanghai, China", 31.2304, 121.4737),
    "russia": ("Russia", 61.5240, 105.3188),
    "moscow": ("Moscow, Russia", 55.7558, 37.6173),
    "ukraine": ("Ukraine", 48.3794, 31.1656),
    "kyiv": ("Kyiv, Ukraine", 50.4501, 30.5234),
    "israel": ("Israel", 31.0461, 34.8516),
    "gaza": ("Gaza", 31.5017, 34.4668),
    "palestine": ("Palestine", 31.9522, 35.2332),
    "iran": ("Iran", 32.4279, 53.6880),
    "tehran": ("Tehran, Iran", 35.6892, 51.3890),
    "india": ("India", 20.5937, 78.9629),
    "new delhi": ("New Delhi, India", 28.6139, 77.2090),
    "africa": ("Africa", -8.7832, 34.5085),
    "asia": ("Asia", 34.0479, 100.6197),
    "europe": ("Europe", 54.5260, 15.2551),
    "middle east": ("Middle East", 29.2985, 42.5510),
    "global": ("World", 20.0, 0.0),
    "world": ("World", 20.0, 0.0),
}

# Map source regions/countries to gazetteer entries when text extraction fails.
_SOURCE_REGION_FALLBACK = {
    "United States": ("United States", 37.0902, -95.7129),
    "European Union": ("European Union", 50.8503, 4.3517),
    "United Kingdom": ("United Kingdom", 55.3781, -3.4360),
    "Japan": ("Japan", 36.2048, 138.2529),
    "India": ("India", 20.5937, 78.9629),
    "China": ("China", 35.8617, 104.1954),
    "Russia": ("Russia", 61.5240, 105.3188),
    "Israel": ("Israel", 31.0461, 34.8516),
    "Iran": ("Iran", 32.4279, 53.6880),
    "Global": ("World", 20.0, 0.0),
}


def classify_event(text: str) -> str:
    """Return the most likely event type based on keyword matches."""
    text_lower = text.lower()
    scores: dict[str, int] = {}
    for event_type, keywords in _EVENT_KEYWORDS.items():
        scores[event_type] = sum(1 for kw in keywords if kw in text_lower)

    if not scores or max(scores.values()) == 0:
        return "other"
    return max(scores, key=scores.get)


def severity_for_event(text: str, event_type: str) -> int:
    """Very simple severity score 1-10."""
    text_lower = text.lower()
    base = {"conflict": 7, "economic": 5, "political": 4, "environmental": 6, "other": 3}.get(event_type, 3)

    intensifiers = ["killed", "death", "casualties", "emergency", "crisis", "war", "invasion"]
    if any(word in text_lower for word in intensifiers):
        base += 2
    return min(10, base)


def extract_location(text: str) -> tuple[str, float, float] | None:
    """Extract a location from text using the hardcoded Phase 1 gazetteer.

    Returns (display_name, lat, lon) or None.
    """
    text_lower = text.lower()
    # Prefer longer matches first
    candidates = sorted(_LOCATION_GAZETTEER.items(), key=lambda kv: len(kv[0]), reverse=True)
    for key, (display_name, lat, lon) in candidates:
        if key in text_lower:
            return display_name, lat, lon
    return None


def classify_article(title: str, body: str | None, source_region: str | None = None) -> dict:
    """Classify an article and extract location.

    Returns dict with event_type, severity, location_name, lat, lon.
    """
    text = title if not body else f"{title} {body}"
    event_type = classify_event(text)
    severity = severity_for_event(text, event_type)

    location = extract_location(text)
    if not location and source_region:
        fallback = _SOURCE_REGION_FALLBACK.get(source_region)
        if fallback:
            location = fallback

    if location:
        location_name, lat, lon = location
    else:
        location_name, lat, lon = None, None, None

    return {
        "event_type": event_type,
        "severity": severity,
        "location_name": location_name,
        "latitude": lat,
        "longitude": lon,
    }
