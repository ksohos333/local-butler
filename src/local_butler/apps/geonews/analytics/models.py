"""Storage for the trend/corroboration engine.

Two tables, deliberately separate:

* **Event clusters** -- groups of articles that appear to describe the *same*
  underlying real-world event. The number of *distinct outlets* in a cluster is
  the corroboration signal: one state outlet repeating itself is not evidence,
  six independent outlets converging is. This is what gives a headline its
  provenance rather than leaving it an isolated string.
* **Trend signals** -- time-series observations over those clusters and topics:
  article volume per day, share of total attention, burst flags, and momentum.

The tables are created with a scoped ``create_all`` on their own declarative
base, matching how ``memory.py`` bootstraps itself. Nothing here touches the
Article table's schema, and no migration is required to start using it.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any, Dict, List, Optional

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
    create_engine,
    inspect,
    text,
)
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import declarative_base, sessionmaker

from local_butler.core.database import DEFAULT_DB_URL

logger = logging.getLogger(__name__)

AnalyticsBase = declarative_base()


def utcnow() -> dt.datetime:
    """Naive UTC.

    Matches ``memory.utcnow`` on purpose: SQLite drops tzinfo, so an aware
    default would read back naive and quietly break recency comparisons.
    """
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


class EventCluster(AnalyticsBase):
    """One real-world event, as reconstructed from article text.

    ``signature`` is the set of distinctive terms the cluster was keyed on, so a
    cluster can be explained to a human rather than existing as an opaque id.
    """

    __tablename__ = "geonews_event_clusters"

    id = Column(Integer, primary_key=True)
    # Content hash of the cluster key. Stable across rebuilds of the same event.
    signature = Column(String(64), unique=True, index=True)

    label = Column(Text)
    event_type = Column(String(32), index=True)
    location_name = Column(String(256), index=True)

    article_count = Column(Integer, default=0)
    # Distinct outlets. This is the number that carries meaning.
    source_count = Column(Integer, default=0, index=True)
    sources = Column(JSON)

    max_severity = Column(Integer, default=0)
    avg_severity = Column(Float, default=0.0)

    first_seen = Column(DateTime, index=True)
    last_seen = Column(DateTime, index=True)
    # Peak article volume on any single day, used for burst context.
    peak_day = Column(String(16))
    peak_volume = Column(Integer, default=0)

    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)

    # No __table_args__: every index above comes from ``index=True``. Declaring
    # a named Index here too would make create_all emit it twice and SQLite
    # would reject the duplicate name.


class TrendSignal(AnalyticsBase):
    """A single day's measurement for one trend subject.

    ``subject`` is the tracking key (``"cluster:<signature>"``,
    ``"event_type:conflict"``, ``"topic:ceasefire"``). Keeping every dimension
    in one table means the UI and the agent can query all of them uniformly.
    """

    __tablename__ = "geonews_trend_signals"

    id = Column(Integer, primary_key=True)
    subject = Column(String(128), index=True)
    subject_kind = Column(String(32), index=True)  # cluster/event_type/topic/region/source
    label = Column(Text)
    day = Column(String(16), index=True)

    volume = Column(Integer, default=0)
    # Fraction of all articles that day, 0..1.
    share = Column(Float, default=0.0)
    source_count = Column(Integer, default=0)
    max_severity = Column(Integer, default=0)

    # Burst flag + how anomalous the day was against the subject's own history.
    is_burst = Column(Boolean, default=False, index=True)
    burst_score = Column(Float, default=0.0)
    # Change in share vs the trailing window, in percentage points.
    momentum = Column(Float, default=0.0)

    computed_at = Column(DateTime, default=utcnow)

    __table_args__ = (
        Index("ix_geonews_trend_signals_subject_day", "subject", "day", unique=True),
        Index("ix_geonews_trend_signals_kind_day", "subject_kind", "day"),
    )


class AnalyticsStore:
    """CRUD over the analytics tables."""

    def __init__(self, db_url: str = DEFAULT_DB_URL) -> None:
        self.engine = create_engine(db_url, future=True)
        self._ensure_schema()
        self.Session = sessionmaker(bind=self.engine, future=True)

    def _ensure_schema(self) -> None:
        AnalyticsBase.metadata.create_all(
            self.engine,
            tables=[
                EventCluster.__table__,
                TrendSignal.__table__,
            ],
        )
        # Article.corroboration_count already exists in the app schema, but the
        # column is not indexed, and every analytics query filters on it. Adding
        # the index here keeps this module independent of Alembic.
        self._index_articles()

    def _index_articles(self) -> None:
        """Best-effort index creation.

        Wrapped because the Article table is owned by ``models.py`` and may not
        exist yet on a fresh database. Index creation is an optimisation, never
        a correctness requirement, so a failure here must not break startup.
        """
        try:
            with self.engine.begin() as conn:
                conn.execute(
                    text(
                        "CREATE INDEX IF NOT EXISTS ix_geonews_articles_event_type_day "
                        "ON geonews_articles (event_type, published_at)"
                    )
                )
        except Exception:  # noqa: BLE001
            pass

    # -- clusters ---------------------------------------------------------

    def replace_clusters(self, rows: List[Dict[str, Any]]) -> int:
        """Rewrite the cluster table from a fresh computation.

        Clusters are fully derived data, so a rebuild is a delete-and-insert.
        Skipped when ``rows`` is empty, which would otherwise wipe good state
        after a failed or empty run.
        """
        if not rows:
            return 0
        session = self.Session()
        try:
            session.query(EventCluster).delete()
            for row in rows:
                session.add(EventCluster(**row))
            session.commit()
            return len(rows)
        finally:
            session.close()

    def apply_corroboration(self, counts: Dict[int, int]) -> int:
        """Write per-article ``corroboration_count`` back onto Article rows.

        Best-effort by design: the analytics tables are scoped to this module,
        but ``geonews_articles`` is owned by ``models.py`` and may not exist in
        the same database (it does not in an analytics-only test database).
        Clusters are still persisted in that case -- losing the denormalised
        score is preferable to failing the whole rebuild.
        """
        if not counts:
            return 0
        session = self.Session()
        try:
            conn = session.connection()
            if "geonews_articles" not in inspect(conn.engine).get_table_names():
                logger.debug(
                    "geonews_articles absent from %s; skipping corroboration "
                    "writeback", self.engine.url
                )
                return 0
            conn.execute(
                text("UPDATE geonews_articles SET corroboration_count = 0")
            )
            session.execute(
                text(
                    "UPDATE geonews_articles SET corroboration_count = :c "
                    "WHERE id = :i"
                ),
                [{"c": c, "i": i} for i, c in counts.items()],
            )
            session.commit()
            return len(counts)
        except SQLAlchemyError:
            logger.warning("corroboration writeback failed", exc_info=True)
            session.rollback()
            return 0
        finally:
            session.close()

    def list_clusters(
        self, limit: int = 50, min_sources: int = 2
    ) -> List[Dict[str, Any]]:
        session = self.Session()
        try:
            rows = (
                session.query(EventCluster)
                .filter(EventCluster.source_count >= min_sources)
                .order_by(EventCluster.last_seen.desc())
                .limit(limit)
                .all()
            )
            return [_cluster_to_dict(r) for r in rows]
        finally:
            session.close()

    def get_cluster(self, signature: str) -> Optional[Dict[str, Any]]:
        session = self.Session()
        try:
            row = (
                session.query(EventCluster)
                .filter(EventCluster.signature == signature)
                .one_or_none()
            )
            return _cluster_to_dict(row) if row else None
        finally:
            session.close()

    # -- signals ----------------------------------------------------------

    def replace_signals(self, rows: List[Dict[str, Any]]) -> int:
        if not rows:
            return 0
        session = self.Session()
        try:
            session.query(TrendSignal).delete()
            for row in rows:
                session.add(TrendSignal(**row))
            session.commit()
            return len(rows)
        finally:
            session.close()

    def top_signals(
        self,
        subject_kind: str | None = None,
        since_day: str | None = None,
        bursts_only: bool = False,
        limit: int = 40,
    ) -> List[Dict[str, Any]]:
        session = self.Session()
        try:
            q = session.query(TrendSignal)
            if subject_kind:
                q = q.filter(TrendSignal.subject_kind == subject_kind)
            if since_day:
                q = q.filter(TrendSignal.day >= since_day)
            if bursts_only:
                q = q.filter(TrendSignal.is_burst.is_(True))
            q = q.order_by(
                TrendSignal.day.desc(), TrendSignal.burst_score.desc()
            ).limit(limit)
            return [
                {
                    "subject": r.subject,
                    "subject_kind": r.subject_kind,
                    "label": r.label,
                    "day": r.day,
                    "volume": r.volume,
                    "share": r.share,
                    "source_count": r.source_count,
                    "max_severity": r.max_severity,
                    "is_burst": r.is_burst,
                    "burst_score": r.burst_score,
                    "momentum": r.momentum,
                }
                for r in q.all()
            ]
        finally:
            session.close()

    def series(self, subject: str, limit: int = 60) -> List[Dict[str, Any]]:
        """Full history for one subject, oldest first."""
        session = self.Session()
        try:
            rows = (
                session.query(TrendSignal)
                .filter(TrendSignal.subject == subject)
                .order_by(TrendSignal.day.asc())
                .limit(limit)
                .all()
            )
            return [
                {
                    "day": r.day,
                    "volume": r.volume,
                    "share": r.share,
                    "source_count": r.source_count,
                    "is_burst": r.is_burst,
                    "burst_score": r.burst_score,
                    "momentum": r.momentum,
                }
                for r in rows
            ]
        finally:
            session.close()

    def latest_day(self) -> Optional[str]:
        session = self.Session()
        try:
            row = (
                session.query(TrendSignal.day)
                .order_by(TrendSignal.day.desc())
                .first()
            )
            return row[0] if row else None
        finally:
            session.close()

    def stats(self) -> Dict[str, Any]:
        session = self.Session()
        try:
            clusters = session.query(EventCluster).count()
            signals = session.query(TrendSignal).count()
            bursts = (
                session.query(TrendSignal).filter(TrendSignal.is_burst.is_(True)).count()
            )
            corroborated = (
                session.query(EventCluster)
                .filter(EventCluster.source_count >= 2)
                .count()
            )
            latest = (
                session.query(TrendSignal.day)
                .order_by(TrendSignal.day.desc())
                .first()
            )
            return {
                "clusters": clusters,
                "corroborated_clusters": corroborated,
                "signals": signals,
                "burst_days": bursts,
                "latest_day": latest[0] if latest else None,
            }
        finally:
            session.close()


def _cluster_to_dict(row: EventCluster) -> Dict[str, Any]:
    return {
        "signature": row.signature,
        "label": row.label,
        "event_type": row.event_type,
        "location_name": row.location_name,
        "article_count": row.article_count,
        "source_count": row.source_count,
        "sources": row.sources or [],
        "max_severity": row.max_severity,
        "avg_severity": row.avg_severity,
        "first_seen": row.first_seen.isoformat() if row.first_seen else None,
        "last_seen": row.last_seen.isoformat() if row.last_seen else None,
        "peak_day": row.peak_day,
        "peak_volume": row.peak_volume,
    }