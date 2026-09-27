"""Pydantic schemas for Geonews API and UI."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class ArticleOut(BaseModel):
    """Article as returned to the frontend."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    source_key: str
    source_type: str
    source_name: str
    source_category: str | None
    source_region: str | None
    source_language: str | None
    source_is_primary: bool

    title: str
    summary: str | None
    body: str | None
    url: str | None
    published_at: datetime | None
    fetched_at: datetime

    latitude: float | None
    longitude: float | None
    location_name: str | None
    event_type: str | None
    severity: int | None

    fni_score: float | None
    bias_score: float | None


class FeedResponse(BaseModel):
    """Paginated feed response."""

    items: list[ArticleOut]
    total: int
    page: int
    page_size: int


class GeoFeature(BaseModel):
    """A single GeoJSON feature for the map."""

    type: str = "Feature"
    geometry: dict
    properties: dict


class GeoResponse(BaseModel):
    """GeoJSON FeatureCollection for Leaflet."""

    type: str = "FeatureCollection"
    features: list[GeoFeature]


class StatsResponse(BaseModel):
    """Dashboard stats."""

    total_articles: int
    primary_sources: int
    last_ingestion: datetime | None
    event_types: dict[str, int]


class IngestResponse(BaseModel):
    """Response from a manual ingestion trigger."""

    started: bool
    message: str


class ProcessResponse(BaseModel):
    """Response from a manual processing trigger."""

    processed: int
    message: str


class TrendPoint(BaseModel):
    """Articles per day."""

    date: str
    count: int


class EventTypeTrend(BaseModel):
    """Trend for a single event type over two windows."""

    event_type: str
    current: int
    previous: int
    delta: int
    delta_pct: float


class RegionTrend(BaseModel):
    """Trend for a single source region over two windows."""

    region: str
    current: int
    previous: int
    delta: int
    delta_pct: float


class Hotspot(BaseModel):
    """Recent high-impact mapped article."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    event_type: str | None
    severity: int | None
    latitude: float | None
    longitude: float | None
    location_name: str | None
    source_name: str
    published_at: datetime | None


class TrendsResponse(BaseModel):
    """Trend analysis payload."""

    days: int
    total: int
    previous_total: int
    time_series: list[TrendPoint]
    type_distribution: list[EventTypeTrend]
    region_trends: list[RegionTrend]
    hotspots: list[Hotspot]
