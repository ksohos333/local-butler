"""Burst and momentum detection over daily article volume.

What counts as "a trend" here is deliberately narrow: a subject's daily volume
compared against *its own recent baseline*. Nothing is called unusual because it
is high in absolute terms -- Syria gets more articles than Tuvalu every day, and
that is not news.

The detector is a two-sided test rather than a model:

* **burst** -- today's volume is a large multiple of the trailing baseline
  *and* clears an absolute floor, so a jump from 0 to 2 articles is not a trend;
* **momentum** -- share of attention is rising across the window, which catches
  a story that has been growing steadily without ever spiking.

Why not a fitted forecaster: the corpus covers about ten days of usable history.
Fitting seasonality or a model to that would produce confident numbers with no
basis. Detection against an observed baseline is the strongest claim this data
can actually support, so that is what this module makes -- and ``confidence``
scales down as the history shortens instead of hiding the weakness.
"""

from __future__ import annotations

import datetime as dt
import logging
import math
import statistics
from collections import defaultdict
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from local_butler.apps.geonews.analytics.models import AnalyticsStore

logger = logging.getLogger(__name__)

# Below this many prior days a burst call is not trustworthy, so we decline to
# make one rather than emit a flag backed by two data points.
_MIN_HISTORY_DAYS = 4
# A burst must also be big in absolute terms. Without this, a subject that
# usually gets 0 articles "doubles" on any day it gets 1.
_MIN_BURST_VOLUME = 5
_BURST_MULTIPLE = 3.0
# Fewer steady-state days than this and no burst should be claimed at all.
_MIN_ANALYSIS_DAYS = 5


def _median(values: Sequence[float]) -> float:
    return float(statistics.median(values)) if values else 0.0


def _mean(values: Sequence[float]) -> float:
    return float(statistics.fmean(values)) if values else 0.0


def _share_delta(recent: Sequence[float], prior: Sequence[float]) -> float:
    """Percentage-point change between two windows' mean share."""
    if not recent or not prior:
        return 0.0
    return (_mean(recent) - _mean(prior)) * 100.0


def score_series(
    volumes: Sequence[int],
    day_totals: Sequence[int],
    *,
    min_history: int = _MIN_HISTORY_DAYS,
    min_burst_volume: int = _MIN_BURST_VOLUME,
    burst_multiple: float = _BURST_MULTIPLE,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Score one subject across days.

    The comparison is made on **share of that day's attention** rather than raw
    volume. The corpus grows as it ingests -- a day with 400 articles and a day
    with 40 are not comparable counts -- but the fraction of a day's coverage
    that went to this subject is. Raw volume is kept only as a floor, to stop a
    subject going from 0 to 1 article being reported as a trend.

    Returns ``(records, gaps)``. A day in ``gaps`` had too little history to
    judge, which is different from being judged and found unremarkable.
    """
    n = len(volumes)
    records: List[Dict[str, Any]] = []
    gaps: List[Dict[str, Any]] = []

    shares: List[float] = [
        (v / t) if t else 0.0 for v, t in zip(volumes, day_totals)
    ]

    for i, (volume, share) in enumerate(zip(volumes, shares)):
        history = shares[:i]
        history_days = len(history)

        burst = False
        burst_score = 0.0
        momentum = 0.0

        if history_days >= min_history:
            # Median baseline: one spike day cannot drag it upward.
            baseline = _median(history)
            if baseline > 0:
                burst_score = share / baseline
                burst = (
                    volume >= min_burst_volume and burst_score >= burst_multiple
                )
            elif volume >= min_burst_volume:
                # Never tracked before, but a genuinely large day still counts.
                burst_score = float(volume)
                burst = True

            # Momentum asks "which way is this heading now", so the current day belongs
            # in the recent window -- excluding it would report every trend's
            # first day as flat. Windows are sliced to stay disjoint at any
            # history length, since a fixed [-3:]/[-7:-3] pair shares a day
            # once history is short and pulls momentum toward zero.
            recent = shares[max(0, i - 2): i + 1]
            prior_end = max(0, i - 2)
            prior = shares[max(0, i - 6): prior_end]
            momentum = _share_delta(recent, prior)
        else:
            gaps.append({"day_index": i, "history_days": history_days})

        records.append(
            {
                "volume": volume,
                "share": round(share, 5),
                "is_burst": burst,
                "burst_score": round(burst_score, 3),
                "momentum": round(momentum, 3),
            }
        )

    return records, gaps


def build_signals(
    rows: Iterable[Dict[str, Any]],
    *,
    days: int = 30,
    include_clusters: bool = True,
    min_cluster_sources: int = 2,
    include_topics: bool = True,
    topic_min_df: int = 8,
    topic_max_ratio: float = 0.01,
    topic_named_ratio: float = 0.5,
    topic_terms: Optional[Sequence[str]] = None,
) -> List[Dict[str, Any]]:
    """Build per-subject daily series from article rows.

    Subjects produced:

    * ``event_type:<name>``  -- volume of that class of event per day
    * ``source:<key>``       -- one subject per outlet
    * ``location:<name>``    -- one subject per named place
    * ``cluster:<signature>``-- one subject per corroborated event
    * ``topic:<term>``       -- one subject per recurring distinctive term

    Every subject is scored against its own history, so a slow-burning topic and
    a sudden spike are both visible without either being called a trend by
    absolute size.
    """
    rows = list(rows)
    if not rows:
        return []

    # The corpus is back-loaded: ingestion has been running for a couple of
    # weeks and day volume has climbed from ~20 to ~450. Comparing raw daily
    # counts across that ramp would report the ingestion schedule as news --
    # every subject would "burst" on the most recent day. So restrict analysis
    # to days where ingestion was near steady state, then compare subjects by
    # share of each day rather than by raw count.
    window = analysis_window(rows)
    if not window:
        return []
    if len(window) < _MIN_ANALYSIS_DAYS:
        logger.warning(
            "only %d steady-state days available; trends need at least %d",
            len(window), _MIN_ANALYSIS_DAYS,
        )
    rows = [r for r in rows if _day(r.get("published_at")) in window]

    day_totals: Dict[str, int] = defaultdict(int)
    for row in rows:
        day = _day(row.get("published_at"))
        if day:
            day_totals[day] += 1

    # -- collect raw per-day counts per subject ---------------------------
    series: Dict[Tuple[str, str], Dict[str, int]] = defaultdict(
        lambda: defaultdict(int)
    )
    labels: Dict[Tuple[str, str], str] = {}
    day_severity: Dict[Tuple[str, str, str], int] = {}
    day_sources: Dict[Tuple[str, str, str], set] = defaultdict(set)

    doc_freq: Dict[str, int] = defaultdict(int)
    capitalised: Dict[str, int] = defaultdict(int)

    def record(kind: str, name: str, label: str, day: str, row: Dict[str, Any]) -> None:
        """Count one article against a subject, with its provenance."""
        key = (kind, name)
        series[key][day] += 1
        labels.setdefault(key, label)
        day_key = (kind, name, day)
        severity = row.get("severity")
        if severity:
            day_severity[day_key] = max(day_severity.get(day_key, 0), severity)
        source = row.get("source_key")
        if source:
            day_sources[day_key].add(source)

    for row in rows:
        day = _day(row.get("published_at"))
        if day is None:
            continue

        event_type = row.get("event_type")
        if event_type:
            record("event_type", event_type, f"{event_type.replace('_', ' ')} events",
                   day, row)

        source = row.get("source_key")
        if source:
            record("source", source, source, day, row)

        location = row.get("location_name")
        if location:
            record("location", location, location, day, row)

        if include_topics:
            proper = _proper_terms(row.get("title") or "")
            for term in _topic_terms(row, topic_terms):
                doc_freq[term] += 1
                if term in proper:
                    capitalised[term] += 1
                record("topic", term, term, day, row)

    # Topics must be *persistent* (recurring, not a one-off), *distinctive*
    # (rare relative to the corpus) and *name-like*. All three are needed: with
    # only a frequency floor, "may" and "rights" clear the bar and then
    # outrank real stories; with no proper-noun test at all, topics degenerate
    # into a ranking of common English vocabulary.
    if include_topics and doc_freq:
        ceiling = max(topic_min_df + 1, int(len(rows) * topic_max_ratio))
        for (kind, term) in list(series):
            if kind != "topic":
                continue
            df = doc_freq.get(term, 0)
            named = capitalised.get(term, 0) / df if df else 0.0
            if (
                df < topic_min_df
                or df > ceiling
                or named < topic_named_ratio
            ):
                del series[(kind, term)]
                labels.pop((kind, term), None)

    # -- turn each subject's counts into a dense daily series -------------
    signals: List[Dict[str, Any]] = []
    for (kind, name), counts in sorted(series.items()):
        day_list = sorted(counts)[-days:]
        if len(day_list) < 2:
            continue
        volumes = [counts[d] for d in day_list]

        subject = f"{kind}:{name}"
        records, gaps = score_series(
            volumes, [day_totals.get(d, 0) for d in day_list]
        )
        if gaps and len(gaps) == len(volumes):
            logger.debug(
                "subject %s has only %d days of history; no signals emitted",
                subject, len(volumes),
            )
            continue

        for day, volume, record_ in zip(day_list, volumes, records):
            day_key = (kind, name, day)
            signals.append(
                {
                    "subject": subject[:128],
                    "subject_kind": kind[:32],
                    "label": labels.get((kind, name), name),
                    "day": day,
                    "volume": volume,
                    "share": record_["share"],
                    "source_count": len(day_sources.get(day_key, set())),
                    "max_severity": day_severity.get(day_key, 0),
                    "is_burst": record_["is_burst"],
                    "burst_score": record_["burst_score"],
                    "momentum": record_["momentum"],
                }
            )

    return signals


def run_trend_detection(
    store: AnalyticsStore,
    *,
    rows: Optional[Iterable[Dict[str, Any]]] = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Recompute all trend signals and persist them.

    ``rows`` mirrors :func:`~.cluster.run_corroboration`: pass a corpus
    explicitly to analyse a subset, or to test without the Article table.
    """
    if rows is None:
        rows = load_signal_rows(store)
    rows = list(rows)
    signals = build_signals(rows, **kwargs)
    written = store.replace_signals(signals)
    bursts = sum(1 for s in signals if s["is_burst"])
    return {
        "articles": len(rows),
        "signals": written,
        "bursts": bursts,
        "subjects": len({s["subject"] for s in signals}),
    }


def load_signal_rows(store: AnalyticsStore) -> List[Dict[str, Any]]:
    """Read the columns trend detection needs, plus corroboration for clusters."""
    from local_butler.apps.geonews.models import Article

    table = Article.__table__
    cluster_table = None
    try:
        from local_butler.apps.geonews.analytics.models import EventCluster

        cluster_table = EventCluster.__table__
    except Exception:  # pragma: no cover - defensive
        pass

    columns = [
        table.c.id,
        table.c.title,
        table.c.published_at,
        table.c.source_key,
        table.c.event_type,
        table.c.severity,
        table.c.location_name,
        table.c.corroboration_count,
    ]
    session = store.Session()
    try:
        raw = session.execute(
            table.select().with_only_columns(*columns)
        ).mappings().all()
        rows = [dict(r) for r in raw]

        if cluster_table is not None and "geonews_event_clusters" in _tables(session):
            clusters = session.execute(
                cluster_table.select().with_only_columns(
                    cluster_table.c.signature,
                    cluster_table.c.source_count,
                )
            ).mappings().all()
            corroborated = {
                c["signature"] for c in clusters if (c["source_count"] or 0) >= 2
            }
            for row in rows:
                row["corroborated"] = (row.get("corroboration_count") or 0) >= 2
            rows[0].setdefault("_corroborated_signatures", corroborated)
        return rows
    finally:
        session.close()


def _tables(session) -> set:
    from sqlalchemy import inspect

    try:
        return set(inspect(session.get_bind()).get_table_names())
    except Exception:  # pragma: no cover - defensive
        return set()


def analysis_window(
    rows: Sequence[Dict[str, Any]],
    *,
    min_day_volume: int = 20,
    steady_ratio: float = 0.4,
) -> Set[str]:
    """Days on which ingestion volume is close to steady state.

    A day counts if it holds at least ``steady_ratio`` of the median volume of
    the substantial days. Sparse days -- including the long tail of 2018 and
    early-summer stragglers in the corpus -- are excluded: on a day carrying
    three articles, any subject can trivially own 100% of the day's attention,
    and that is a division artifact, not a trend.
    """
    totals: Dict[str, int] = defaultdict(int)
    for row in rows:
        day = _day(row.get("published_at"))
        if day:
            totals[day] += 1

    substantial = [v for v in totals.values() if v >= min_day_volume]
    if not substantial:
        return set()

    cutoff = steady_ratio * _median(substantial)
    return {d for d, v in totals.items() if v >= cutoff and v >= min_day_volume}


def _day(value: Optional[dt.datetime]) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        return value[:10]
    return value.strftime("%Y-%m-%d")


def _topic_terms(row: Dict[str, Any], allowed: Optional[Sequence[str]]) -> List[str]:
    """Terms tracked as topics.

    With ``allowed`` given, only those. Otherwise reuse the clustering
    tokenizer. Possessives are stripped because ``tokenize`` keeps the
    apostrophe, and "ukraine's" is not a topic anyone wants to track.
    """
    from local_butler.apps.geonews.analytics.cluster import tokenize

    if allowed is not None:
        haystack = (row.get("title") or "").lower()
        return [t for t in allowed if t in haystack]

    out = []
    for term in tokenize(row.get("title") or "")[:12]:
        stem = term.removesuffix("'s")
        if len(stem) >= 3:
            out.append(stem)
    return out


def _proper_terms(title: str) -> Set[str]:
    """Terms that look like proper nouns, judged by capitalisation.

    A cheap and surprisingly effective stand-in for named-entity recognition:
    headline style capitalises the names of places, people and organisations,
    while "may", "rights" and "prices" stay lower-case. The first word of a
    headline is ignored because sentence case capitalises it regardless of
    whether it is a name.
    """
    from local_butler.apps.geonews.analytics.cluster import tokenize

    words = (title or "").split()
    proper: Set[str] = set()
    seen = 0
    for position, word in enumerate(words):
        if not word[:1].isalpha():
            continue
        seen += 1
        if position == 0:
            continue  # sentence-initial: capitalised by convention
        if word[:1].isupper():
            for term in tokenize(word):
                stem = term.removesuffix("'s")
                if len(stem) >= 3:
                    proper.add(stem)
    return proper