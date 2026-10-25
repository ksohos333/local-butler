"""SQLAlchemy models for Geonews."""

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from local_butler.core.database import Base


class User(Base):
    """A single local user. Phase 1 is single-user; this table supports future multi-user."""

    __tablename__ = "geonews_users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )


class Wallet(Base):
    """Simulated credit balance per user."""

    __tablename__ = "geonews_wallets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    balance: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )


class Article(Base):
    """A single news item ingested from any source."""

    __tablename__ = "geonews_articles"
    __table_args__ = (UniqueConstraint("content_hash", name="uq_geonews_articles_hash"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # Source metadata
    source_key: Mapped[str] = mapped_column(String(64), index=True)
    source_type: Mapped[str] = mapped_column(String(32), index=True)
    source_name: Mapped[str] = mapped_column(String(128))
    source_category: Mapped[str | None] = mapped_column(String(32))
    source_region: Mapped[str | None] = mapped_column(String(64))
    source_language: Mapped[str | None] = mapped_column(String(8))
    source_is_primary: Mapped[bool] = mapped_column(Boolean, default=True)
    source_bias: Mapped[str | None] = mapped_column(String(32))

    # Content
    title: Mapped[str] = mapped_column(String(512))
    summary: Mapped[str | None] = mapped_column(Text)
    body: Mapped[str | None] = mapped_column(Text)
    url: Mapped[str | None] = mapped_column(String(2048), index=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )

    # Deduplication
    content_hash: Mapped[str] = mapped_column(String(64))

    # Geo / classification (filled by pipeline)
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)
    location_name: Mapped[str | None] = mapped_column(String(256))
    event_type: Mapped[str | None] = mapped_column(String(32), index=True)
    severity: Mapped[int | None] = mapped_column(Integer, default=5)

    # Processing flags
    processed: Mapped[bool] = mapped_column(Boolean, default=False)
    metadata_json: Mapped[dict | None] = mapped_column(JSON)

    # Phase 4 fields (pre-emptive, nullable)
    fni_score: Mapped[float | None] = mapped_column(Float)
    fni_explanation: Mapped[str | None] = mapped_column(Text)
    bias_score: Mapped[float | None] = mapped_column(Float)
    corroboration_count: Mapped[int] = mapped_column(Integer, default=0)

    entities: Mapped[list["Entity"]] = relationship(
        "Entity", back_populates="article", lazy="selectin", cascade="all, delete-orphan"
    )


class Entity(Base):
    """Named entity extracted from an article by the NLP pipeline."""

    __tablename__ = "geonews_entities"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    article_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("geonews_articles.id"), index=True
    )
    entity_type: Mapped[str] = mapped_column(String(16))  # PERSON, ORG, GPE, LOC
    entity_text: Mapped[str] = mapped_column(String(256), index=True)

    article: Mapped["Article"] = relationship("Article", back_populates="entities")


class Location(Base):
    """Cache of geocoding results to avoid Nominatim rate limits."""

    __tablename__ = "geonews_locations"
    __table_args__ = (UniqueConstraint("query_text", name="uq_geonews_locations_query"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    query_text: Mapped[str] = mapped_column(String(256), index=True)
    latitude: Mapped[float] = mapped_column(Float)
    longitude: Mapped[float] = mapped_column(Float)
    display_name: Mapped[str] = mapped_column(String(512))
    geocoded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )
class IngestionLog(Base):
    """One row per ingestion run, per source."""

    __tablename__ = "geonews_ingestion_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_key: Mapped[str] = mapped_column(String(64), index=True)
    source_type: Mapped[str] = mapped_column(String(32))
    items_fetched: Mapped[int] = mapped_column(Integer, default=0)
    items_new: Mapped[int] = mapped_column(Integer, default=0)
    errors: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str | None] = mapped_column(Text)
    run_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )


class Market(Base):
    """A prediction market on a future event."""

    __tablename__ = "geonews_markets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(512))
    description: Mapped[str | None] = mapped_column(Text)
    market_type: Mapped[str] = mapped_column(String(32), default="binary")
    close_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    oracle_source: Mapped[str | None] = mapped_column(String(256))
    oracle_query: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="open")  # open/closed/settled
    outcome: Mapped[str | None] = mapped_column(String(32))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    liquidity: Mapped[float] = mapped_column(Float, default=100.0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )
    metadata_json: Mapped[dict | None] = mapped_column(JSON)

    bets: Mapped[list["Bet"]] = relationship(
        "Bet", back_populates="market", lazy="selectin"
    )
    transactions: Mapped[list["MarketTransaction"]] = relationship(
        "MarketTransaction", back_populates="market"
    )
    external_predictions: Mapped[list["ExternalPrediction"]] = relationship(
        "ExternalPrediction", back_populates="market"
    )


class Bet(Base):
    """A user's position in a market."""

    __tablename__ = "geonews_bets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    market_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("geonews_markets.id"), index=True
    )
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    side: Mapped[str] = mapped_column(String(32))  # yes/no/choice label
    shares: Mapped[float] = mapped_column(Float)
    avg_price: Mapped[float] = mapped_column(Float)
    cost_basis: Mapped[float] = mapped_column(Float)
    placed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )

    market: Mapped["Market"] = relationship("Market", back_populates="bets")


class MarketTransaction(Base):
    """Ledger entry for simulated credits."""

    __tablename__ = "geonews_transactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    amount: Mapped[int] = mapped_column(Integer)
    type: Mapped[str] = mapped_column(String(32))  # deposit/withdraw/bet/payout
    market_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("geonews_markets.id"), nullable=True
    )
    bet_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )

    market: Mapped["Market | None"] = relationship(
        "Market", back_populates="transactions"
    )


class ExternalPrediction(Base):
    """Snapshot of external prediction market prices."""

    __tablename__ = "geonews_external_predictions"
    __table_args__ = (
        UniqueConstraint(
            "market_id", "platform", name="uq_geonews_external_predictions"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    market_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("geonews_markets.id"), nullable=True
    )
    platform: Mapped[str] = mapped_column(String(64), index=True)
    platform_market_id: Mapped[str | None] = mapped_column(String(256))
    question: Mapped[str] = mapped_column(String(512))
    yes_price: Mapped[float | None] = mapped_column(Float)
    no_price: Mapped[float | None] = mapped_column(Float)
    probability: Mapped[float | None] = mapped_column(Float)
    volume: Mapped[float | None] = mapped_column(Float)
    url: Mapped[str | None] = mapped_column(String(2048))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )

    market: Mapped["Market | None"] = relationship(
        "Market", back_populates="external_predictions"
    )
