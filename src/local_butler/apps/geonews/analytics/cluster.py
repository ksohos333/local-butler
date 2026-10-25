"""Cluster articles into real-world events.

The idea, in Palantir terms, is the ontology layer: a headline on its own is a
string, but a headline linked to "this same event, corroborated by these other
outlets, escalating since this date" is a fact with provenance. Aggregation
across independent sources is what separates reporting from repetition.

Approach, in order of cost:

1. Build a sparse term-document matrix over titles + summaries.
2. Candidate pairs come from an inverted index on *rare* terms, so we never
   compare all n^2 pairs. Common terms carry no pairing information anyway.
3. Score candidates with cosine similarity on TF-IDF weights.
4. Link pairs above a threshold into clusters with union-find, then greedily
   merge clusters that are still similar enough.

Deliberately simple and inspectable: every cluster can be explained by the terms
that produced it. A black-box embedding store would be more "Palantir", but
with 182-character article bodies it would buy no accuracy, and it would make
the corroboration claim unauditable -- which defeats the point.
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from local_butler.apps.geonews.analytics.models import AnalyticsStore

logger = logging.getLogger(__name__)

# Words that appear in almost every headline and so cannot identify an event.
_STOPWORDS: Set[str] = {
    "a", "об", "about", "after", "again", "against", "all", "also", "am", "an",
    "and", "any", "are", "as", "at", "be", "because", "been", "before", "being",
    "below", "between", "both", "but", "by", "can", "did", "do", "does",
    "doing", "don", "down", "during", "each", "few", "for", "from", "further",
    "had", "has", "have", "having", "he", "her", "here", "hers", "him", "his",
    "how", "i", "if", "in", "into", "is", "it", "its", "itself", "just", "me",
    "more", "most", "my", "no", "nor", "not", "now", "of", "off", "on", "once",
    "only", "or", "other", "ought", "our", "ours", "out", "over", "own",
    "same", "she", "should", "so", "some", "such", "than", "that", "the",
    "their", "theirs", "them", "then", "there", "these", "they", "this",
    "those", "through", "to", "too", "under", "until", "up", "very", "was",
    "we", "were", "what", "when", "where", "which", "while", "who", "whom",
    "why", "with", "would", "you", "your", "yours",
    # newsroom boilerplate
    "says", "said", "new", "report", "reports", "reported", "update",
    "updates", "updated", "latest", "breaking", "live", "video", "watch",
    "photo", "photos", "analysis", "exclusive", "opinion", "editorial",
    "full", "story", "read", "more", "here", "first", "last", "week", "year",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday",
    "sunday", "today", "yesterday", "tomorrow",
}

_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z'\-]{2,}")
_MIN_TOKEN_LEN = 3

# How many of a document's rarest terms index it for candidate blocking. Small
# and fixed: it only has to be distinctive enough to bring true matches into
# the same candidate set, and the cosine score does the actual deciding.
_BLOCK_TERMS = 8


def tokenize(text: str) -> List[str]:
    """Lowercase content tokens with stopwords and 1-2 char noise removed."""
    if not text:
        return []
    out = []
    for raw in _TOKEN_RE.findall(text):
        tok = raw.lower().strip("'-")
        if len(tok) < _MIN_TOKEN_LEN or tok in _STOPWORDS or tok.isdigit():
            continue
        out.append(tok)
    return out


def _day_key(dt_value: Optional[datetime]) -> Optional[str]:
    if dt_value is None:
        return None
    return dt_value.strftime("%Y-%m-%d")


class _Doc:
    """One article, reduced to what clustering needs."""

    __slots__ = ("id", "terms", "tf", "norm", "day", "source_key",
                 "event_type", "severity", "title", "location_name",
                 "published_at", "blocking_terms", "key_terms")

    def __init__(
        self,
        article_id: int,
        text: str,
        day: Optional[str],
        source_key: str,
        event_type: Optional[str],
        severity: Optional[int],
        title: str,
        location_name: Optional[str] = None,
        published_at: Optional[datetime] = None,
    ) -> None:
        self.id = article_id
        self.terms = tokenize(text)
        self.tf: Dict[str, int] = defaultdict(int)
        for tok in self.terms:
            self.tf[tok] += 1
        self.norm = 0.0  # filled by _apply_idf
        self.day = day
        self.source_key = source_key
        self.event_type = event_type
        self.severity = severity
        self.title = title
        self.location_name = location_name
        self.published_at = published_at
        self.blocking_terms: List[str] = []
        self.key_terms: List[str] = []


def _apply_idf(docs: Sequence[_Doc], df: Dict[str, int]) -> None:
    """Convert term frequencies into TF-IDF weights and precompute norms.

    Smoothed IDF so a term seen in every document contributes ~0 rather than a
    negative weight.
    """
    n = len(docs)
    for doc in docs:
        total = sum(doc.tf.values())
        if total == 0:
            continue
        acc = 0.0
        for term, count in doc.tf.items():
            idf = math.log((n + 1) / (df[term] + 1)) + 1.0
            w = (count / total) * idf
            doc.tf[term] = w
            acc += w * w
        doc.norm = math.sqrt(acc)


def _cosine(a: _Doc, b: _Doc) -> float:
    if a.norm == 0.0 or b.norm == 0.0:
        return 0.0
    # Iterate the smaller dict; documents are short so this stays cheap.
    if len(a.tf) > len(b.tf):
        a, b = b, a
    total = 0.0
    for term, weight in a.tf.items():
        other = b.tf.get(term)
        if other:
            total += weight * other
    return total / (a.norm * b.norm)


class _UnionFind:
    def __init__(self) -> None:
        self.parent: Dict[int, int] = {}

    def find(self, x: int) -> int:
        self.parent.setdefault(x, x)
        root = x
        while self.parent[root] != root:
            root = self.parent[root]
        # path compression
        while self.parent[x] != root:
            self.parent[x], x = root, self.parent[x]
        return root

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def cluster_articles(
    rows: Iterable[Dict[str, Any]],
    threshold: float = 0.34,
    window_days: int = 3,
    min_shared_terms: int = 2,
) -> Tuple[List[Dict[str, Any]], Dict[int, int]]:
    """Group article rows into event clusters.

    Returns ``(cluster_rows, corroboration_by_article_id)`` where the latter
    maps each article to the number of distinct outlets in its cluster.
    """
    docs: List[_Doc] = []
    df: Dict[str, int] = defaultdict(int)

    for row in rows:
        text = f"{row.get('title') or ''} {row.get('summary') or ''}"
        doc = _Doc(
            article_id=row["id"],
            text=text,
            day=_day_key(row.get("published_at")),
            source_key=row.get("source_key") or "unknown",
            event_type=row.get("event_type"),
            severity=row.get("severity"),
            title=row.get("title") or "",
            location_name=row.get("location_name"),
            published_at=row.get("published_at"),
        )
        if not doc.terms:
            continue
        docs.append(doc)
        for term in set(doc.terms):
            df[term] += 1

    if not docs:
        return [], {}

    n = len(docs)
    # Blocking decides which pairs are even compared; scoring uses the full
    # weighted vector, because IDF already handles common terms correctly and
    # throwing them away here would lose the vocabulary that identifies an
    # event ("drone attack" is frequent but that is exactly what matters).
    block_max_df = max(3, int(n * 0.02))

    _apply_idf(docs, df)

    blocking: Dict[str, List[_Doc]] = defaultdict(list)
    for doc in docs:
        # A doc's rarest terms are its most distinctive fingerprints, and are
        # what gets it into candidate pairs. Overlap for the *threshold* test is
        # counted over the full vocabulary, so a document is not punished for
        # having long, distinctive headlines.
        terms = sorted(doc.tf, key=lambda t: (df[t], t))[:_BLOCK_TERMS]
        blocking_terms = [t for t in terms if df[t] <= block_max_df]
        doc.blocking_terms = blocking_terms
        doc.key_terms = terms
        for term in blocking_terms:
            blocking[term].append(doc)

    uf = _UnionFind()

    for _term, posting in blocking.items():
        for i in range(len(posting)):
            a = posting[i]
            for j in range(i + 1, len(posting)):
                b = posting[j]
                if a.id == b.id:
                    continue
                shared = len(set(a.tf) & set(b.tf))
                if shared < min_shared_terms:
                    continue
                # Only compare articles published close together.
                if a.day and b.day:
                    gap = _day_gap(a.day, b.day)
                    if gap is not None and gap > window_days:
                        continue
                if _cosine(a, b) >= threshold:
                    uf.union(a.id, b.id)

    by_id = {d.id: d for d in docs}
    groups: Dict[int, List[_Doc]] = defaultdict(list)
    for doc in docs:
        groups[uf.find(doc.id)].append(doc)

    clusters: List[Dict[str, Any]] = []
    seen_signatures: Set[str] = set()
    corroboration: Dict[int, int] = {}

    for members in groups.values():
        sources = sorted({m.source_key for m in members})
        source_count = len(sources)
        for m in members:
            corroboration[m.id] = source_count

        # Key the cluster on its most distinctive shared terms so the same event
        # re-creates the same signature across rebuilds.
        term_counts: Dict[str, int] = defaultdict(int)
        for m in members:
            for term in set(m.tf):
                term_counts[term] += 1
        shared_terms = {t for t, c in term_counts.items() if c >= 2}
        if shared_terms:
            key_terms = sorted(
                shared_terms,
                key=lambda t: (-term_counts[t], df[t], t),
            )[:6]
        else:
            key_terms = sorted(
                term_counts, key=lambda t: (-term_counts[t], df[t], t)
            )[:4]

        base_sig = "|".join(sorted(key_terms))
        signature = hashlib.sha256(base_sig.encode("utf-8")).hexdigest()[:32]
        # Two unrelated clusters can legitimately share their top terms (a
        # singleton and a larger event about the same subject). Disambiguate on
        # the label so the batch satisfies the unique constraint, keeping the
        # signature stable across rebuilds whenever there is no collision.
        bump = 0
        while signature in seen_signatures:
            bump += 1
            signature = hashlib.sha256(
                f"{base_sig}#{bump}:{_label_for(members, key_terms)}".encode("utf-8")
            ).hexdigest()[:32]
        seen_signatures.add(signature)

        days = defaultdict(int)
        for m in members:
            if m.day:
                days[m.day] += 1
        peak_day, peak_volume = ("", 0)
        if days:
            peak_day, peak_volume = max(days.items(), key=lambda kv: kv[1])

        severities = [m.severity for m in members if m.severity is not None]
        event_types = [m.event_type for m in members if m.event_type]
        stamps = [m.published_at for m in members if m.published_at is not None]
        locations = [m.location_name for m in members if m.location_name]

        clusters.append(
            {
                "signature": signature,
                "label": _label_for(members, key_terms),
                "event_type": (
                    max(set(event_types), key=event_types.count)
                    if event_types
                    else None
                ),
                "location_name": _mode(locations),
                "article_count": len(members),
                "source_count": source_count,
                "sources": sources,
                "max_severity": max(severities) if severities else 0,
                "avg_severity": (
                    round(sum(severities) / len(severities), 2)
                    if severities
                    else 0.0
                ),
                "first_seen": min(stamps) if stamps else None,
                "last_seen": max(stamps) if stamps else None,
                "peak_day": peak_day,
                "peak_volume": peak_volume,
            }
        )

    clusters.sort(key=lambda c: (-c["source_count"], -c["article_count"]))
    return clusters, corroboration


def _day_gap(a: str, b: str) -> Optional[int]:
    try:
        da = datetime.strptime(a, "%Y-%m-%d")
        db_ = datetime.strptime(b, "%Y-%m-%d")
    except ValueError:
        return None
    return abs((da - db_).days)


def _label_for(members: List[_Doc], key_terms: List[str]) -> str:
    """Human-readable cluster label: the most representative title."""
    titles = [m.title for m in members if m.title]
    if not titles:
        return ", ".join(key_terms) or "uncategorised event"
    # The title sharing the most key terms best describes the cluster.
    term_set = set(key_terms)

    def score(title: str) -> int:
        return len(term_set & set(tokenize(title)))

    best = max(titles, key=score)
    return best[:400]


def _mode(values: Sequence[Any]) -> Optional[Any]:
    """Most common value, first-seen wins ties. None if empty."""
    if not values:
        return None
    counts: Dict[Any, int] = defaultdict(int)
    for value in values:
        counts[value] += 1
    best = max(counts.items(), key=lambda kv: kv[1])
    return best[0]


def load_cluster_rows(store: AnalyticsStore) -> List[Dict[str, Any]]:
    """Read the columns clustering needs straight out of SQL.

    Deliberately not the ORM session: a full-corpus rebuild should not hold a
    transaction open against the same database the ingestion loop writes to.
    """
    from local_butler.apps.geonews.models import Article

    table = Article.__table__
    session = store.Session()
    try:
        raw = session.execute(
            table.select().with_only_columns(
                table.c.id,
                table.c.title,
                table.c.summary,
                table.c.published_at,
                table.c.source_key,
                table.c.event_type,
                table.c.severity,
                table.c.location_name,
            )
        ).mappings().all()
    finally:
        session.close()
    return [dict(r) for r in raw]


def run_corroboration(
    store: AnalyticsStore,
    *,
    threshold: float = 0.34,
    rows: Optional[Iterable[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Cluster the corpus and persist the result.

    ``rows`` exists so callers can cluster a subset (or a test can pass a
    fixture) without this function needing to know about the Article table.
    """
    if rows is None:
        rows = load_cluster_rows(store)
    rows = list(rows)

    if not rows:
        return {"articles": 0, "clusters": 0, "corroborated_clusters": 0,
                "articles_scored": 0}

    clusters, corroboration = cluster_articles(rows, threshold=threshold)

    written = store.replace_clusters(clusters)
    applied = store.apply_corroboration(corroboration)
    corroborated = sum(1 for c in clusters if c["source_count"] >= 2)

    return {
        "articles": len(rows),
        "clusters": written,
        "corroborated_clusters": corroborated,
        "articles_scored": applied,
    }