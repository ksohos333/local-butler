"""Resolve place names mentioned in article text to coordinates.

GeoNews articles name places in their headline and summary ("troops enter
Taiz"), not in a structured location field, and the location field that does
exist holds only 28 country-level values. Mapping events to provinces means
reading the place out of the text first.

This module does that against the GeoNames ``cities15000`` dump, which is a
flat file, needs no network at runtime, and covers ~400 Ukrainian and ~178
Myanmar cities -- the theatres that actually produce the articles. It sits
beside :mod:`local_butler.apps.geonews.geo.geocoder` rather than replacing it:
Nominatim still answers the long tail of one-off queries, but at one request
per second it cannot geocode a corpus.

Nothing here is learned or inferred. Every answer is either a gazetteer row
or nothing at all, and ambiguous names stay ambiguous unless a country hint or
a population ordering breaks the tie.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

# GeoNames column positions in cities15000.txt.
_COL_ID, _COL_NAME, _COL_ASCII, _COL_ALT = 0, 1, 2, 3
_COL_LAT, _COL_LON, _COL_FCLASS, _COL_FCODE = 4, 5, 6, 7
_COL_CC, _COL_CC2, _COL_ADMIN1, _COL_ADMIN2 = 8, 9, 10, 11
_COL_POPULATION = 14

# A single lower-case word that happens to be a town name ("Price, Utah",
# "Progress, Amur", "Alert, Nunavut") is almost always a coincidence in running
# text. Requiring the town to be reasonably large is what separates real
# mentions from these, and it is far cheaper to defend than a stopword list.
DEFAULT_MIN_POPULATION = 25_000

# Every city is feature class "P" and a "PPL*" feature code. Checking the class
# alone would also admit parks and platforms; checking the code is what keeps
# this to populated places.
_CITY_CLASS = "P"
_CITY_CODE_PREFIX = "PPL"

# Keys shorter than this are dropped from the index. GeoNames alternatenames
# mix in organisational abbreviations, and three-letter keys collide with
# articles constantly: a single junk alias of "The" reached Teresina and
# mislocated 300 articles before this floor existed.
_MIN_KEY_LENGTH = 4

# The world is full of towns named "Police", "Price" or "Progress". A
# single capitalised token is only trusted when the place is big enough to
# plausibly generate national news coverage on its own.
DEFAULT_MIN_POPULATION = 25_000

# Used as a second opinion, never as a veto: a gazetteer name that is also an
# ordinary English word is only accepted when the place is large. The floor is
# 50k because that is where the two populations separate -- above it sit
# Sumy (256k), Mariupol (230k), Bakhmut (80k), Kherson (66k) and Goma (78k),
# all places that generate conflict coverage; below it sit Police (34k) and
# Bank (18k), which are just words. Raising this floor silently deletes real
# front-line towns, which is the failure mode that matters most here.
_WORDLIST_PATH = Path("/usr/share/dict/words")
_DICTIONARY_WORD_MIN_POPULATION = 50_000

# "the Bank", "in Sudan", "of Jordan" are not place names. No gazetteer entry
# legitimately begins with one of these, so a key that does is an artefact of
# greedy n-gram matching rather than a place.
_LEADING_FUNCTION_WORDS = frozenset(
    {"the", "a", "an", "of", "in", "on", "at", "to", "for", "and", "or", "but",
     "from", "by", "with", "as", "is", "are", "was", "were", "be", "that"}
)

_MAX_NGRAM = 4
_WORD_RE = re.compile(r"[0-9A-Za-z\u00c0-\u024f][0-9A-Za-z\u00c0-\u024f'\u2019.-]*")

_PROJECT_ROOT = Path(__file__).resolve().parents[5]
DEFAULT_CITIES_PATH = _PROJECT_ROOT / "data" / "geocoding" / "cities15000.txt"
DEFAULT_ADMIN1_PATH = _PROJECT_ROOT / "data" / "geocoding" / "admin1CodesASCII.txt"


def normalise(text: str) -> str:
    """Fold a place name to its matching key.

    Diacritics are stripped and punctuation dropped so that "Saarbrücken",
    "Saarbrucken" and "SAAR-BRÜCKEN" all collapse together. GeoNames carries
    the ASCII and native forms separately, so folding both sides is what makes
    an article written in English match a database written in the local
    orthography.
    """
    folded = unicodedata.normalize("NFKD", text)
    ascii_only = "".join(c for c in folded if not unicodedata.combining(c))
    lowered = ascii_only.lower()
    cleaned = re.sub(r"[^a-z0-9]+", " ", lowered)
    return " ".join(cleaned.split())


@dataclass(frozen=True)
class Place:
    """One gazetteer row, reduced to what a map needs."""

    geoname_id: int
    name: str
    latitude: float
    longitude: float
    country_code: str
    admin1_code: str
    population: int
    alternate_names: Tuple[str, ...] = field(default=())

    @property
    def admin1(self) -> str:
        return f"{self.country_code}.{self.admin1_code}"

    @property
    def is_city(self) -> bool:
        return True

    def display(self, admin1_names: Optional[Dict[str, str]] = None) -> str:
        province = admin1_names.get(self.admin1, self.admin1_code) if admin1_names else self.admin1_code
        return f"{self.name}, {province}, {self.country_code}"


def load_places(path: Path = DEFAULT_CITIES_PATH) -> List[Place]:
    """Load populated places, keeping only settlement feature codes."""
    places: List[Place] = []
    seen: set[int] = set()
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            fields = line.rstrip("\n").split("\t")
            if len(fields) <= _COL_POPULATION:
                continue
            if fields[_COL_FCLASS] != _CITY_CLASS:
                continue
            if not fields[_COL_FCODE].startswith(_CITY_CODE_PREFIX):
                continue
            gid = int(fields[_COL_ID])
            if gid in seen:
                continue
            seen.add(gid)
            alts = tuple(
                a.strip()
                for a in fields[_COL_ALT].split(",")
                if a.strip() and 2 < len(a.strip()) < 60
            )
            try:
                places.append(
                    Place(
                        geoname_id=gid,
                        name=fields[_COL_NAME],
                        latitude=float(fields[_COL_LAT]),
                        longitude=float(fields[_COL_LON]),
                        country_code=fields[_COL_CC],
                        admin1_code=fields[_COL_ADMIN1],
                        population=int(fields[_COL_POPULATION] or 0),
                        alternate_names=alts,
                    )
                )
            except ValueError:
                continue
    logger.info("loaded %d populated places from %s", len(places), path)
    return places


def load_admin1_names(path: Path = DEFAULT_ADMIN1_PATH) -> Dict[str, str]:
    """Map ``CC.ADMIN1`` codes to province names."""
    names: Dict[str, str] = {}
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            if line.startswith("#"):
                continue
            parts = line.rstrip("\n").split("\t")
            if len(parts) < 2:
                continue
            names[parts[0]] = parts[1]
    return names


def build_index(places: Iterable[Place]) -> Dict[str, List[Place]]:
    """Index every name a place answers to.

    Both the primary name and each alternate name are indexed, so "Yerevan" and
    transliterations of it all reach the same row.
    """
    index: Dict[str, List[Place]] = {}
    for place in places:
        keys = {normalise(place.name)}
        keys.update(normalise(a) for a in place.alternate_names)
        for key in keys:
            if len(key) < _MIN_KEY_LENGTH:
                continue
            index.setdefault(key, []).append(place)
    return index


def _load_dictionary_words() -> frozenset[str]:
    """Load the system wordlist if present, so results degrade rather than fail."""
    if not _WORDLIST_PATH.exists():
        logger.info("no wordlist at %s; skipping dictionary gate", _WORDLIST_PATH)
        return frozenset()
    words: set[str] = set()
    with open(_WORDLIST_PATH, encoding="utf-8", errors="ignore") as handle:
        for line in handle:
            word = line.strip().lower()
            if word:
                words.add(word)
    logger.info("loaded %d dictionary words", len(words))
    return frozenset(words)


_DICTIONARY_WORDS = _load_dictionary_words()


def _name_is_placeworthy(place: Place, key: str) -> bool:
    """Reject a single-token match where the name is just an ordinary word.

    Plenty of real towns are called "Police", "Bend" or "Hamm", and plenty of
    running text contains those words capitalised for other reasons. If the
    matched key is also an English word, the town has to be large enough that
    the two explanations compete. Names absent from the wordlist -- Kharkiv,
    Taiz, Pokrovsk -- are never penalised.
    """
    if place.population >= _DICTIONARY_WORD_MIN_POPULATION:
        return True
    return key not in _DICTIONARY_WORDS


@dataclass(frozen=True)
class Mention:
    """A place name found in text, with where it was found."""

    text: str
    start: int
    end: int
    candidates: Tuple[Place, ...]

    @property
    def is_ambiguous(self) -> bool:
        return len({p.geoname_id for p in self.candidates}) > 1


def extract_mentions(
    text: str,
    index: Dict[str, List[Place]],
    *,
    min_population: int = DEFAULT_MIN_POPULATION,
    max_ngram: int = _MAX_NGRAM,
) -> List[Mention]:
    """Find gazetteer place names in ``text``.

    Matching is longest-n-gram-first so that "New York" wins over "York" and
    "Kinshasa" over a shorter incidental match. A single-word match must be
    capitalised in the source *and* name a place of at least ``min_population``;
    a multi-word match is trusted on its shape alone, because running text
    lowercases "new york" while "New York" as a database key still identifies
    exactly one place.
    """
    words = [(m.group(0), m.start(), m.end()) for m in _WORD_RE.finditer(text)]
    n_words = len(words)
    taken: List[bool] = [False] * n_words
    mentions: List[Mention] = []

    for i in range(n_words):
        if taken[i]:
            continue
        for span in range(min(max_ngram, n_words - i), 0, -1):
            chunk = words[i : i + span]
            if any(taken[j] for j in range(i, i + span)):
                continue
            key = normalise(" ".join(w for w, _, _ in chunk))
            if not key or key not in index:
                continue
            if key.split(" ", 1)[0] in _LEADING_FUNCTION_WORDS:
                continue
            candidates = index[key]
            if span == 1:
                word, _, _ = chunk[0]
                if not word[:1].isupper():
                    continue
                eligible = [
                    p for p in candidates
                    if p.population >= min_population and _name_is_placeworthy(p, key)
                ]
                if not eligible:
                    continue
                candidates = eligible
            start, end = chunk[0][1], chunk[-1][2]
            mentions.append(
                Mention(
                    text=text[start:end],
                    start=start,
                    end=end,
                    candidates=tuple(candidates),
                )
            )
            for j in range(i, i + span):
                taken[j] = True
            break

    return mentions


def pick_candidate(
    candidates: Sequence[Place],
    *,
    hint_country: Optional[str] = None,
) -> Tuple[Optional[Place], str]:
    """Choose one place from an ambiguous mention.

    ``hint_country`` is the article's existing coarse location, which is often
    good enough to settle "Springfield" or "Georgia". Without a hint the most
    populous place wins, which is right far more often than it is wrong for
    news text -- and when it is not, the caller sees ``ambiguous`` and can send
    the case for review rather than shipping a confident wrong answer.
    """
    if not candidates:
        return None, "none"
    unique = {p.geoname_id: p for p in candidates}
    if len(unique) == 1:
        return next(iter(unique.values())), "unique"

    if hint_country:
        hinted = [
            p for p in unique.values()
            if p.country_code.upper() == hint_country.upper()
        ]
        if len(hinted) == 1:
            return hinted[0], "country_hint"
        if hinted:
            unique = {p.geoname_id: p for p in hinted}

    best = max(unique.values(), key=lambda p: (p.population, p.name))
    return best, "ambiguous"


def resolve_text(
    text: str,
    index: Dict[str, List[Place]],
    *,
    hint_country: Optional[str] = None,
    min_population: int = DEFAULT_MIN_POPULATION,
) -> List[Dict[str, object]]:
    """Resolve every place mention in one string of article text."""
    out: List[Dict[str, object]] = []
    for mention in extract_mentions(text, index, min_population=min_population):
        place, how = pick_candidate(mention.candidates, hint_country=hint_country)
        if place is None:
            continue
        out.append(
            {
                "mention": mention.text,
                "place": place,
                "match": how,
                "ambiguous": how == "ambiguous",
            }
        )
    return out


@dataclass
class ResolvedArticle:
    """One article's best place, and whether we are confident about it."""

    article_id: int
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    city: Optional[str] = None
    admin1: Optional[str] = None
    country: Optional[str] = None
    country_code: Optional[str] = None
    mentions: List[str] = field(default_factory=list)
    ambiguous: bool = False

    @property
    def confidence(self) -> str:
        if self.latitude is None:
            return "none"
        return "ambiguous" if self.ambiguous else "resolved"


# Country names as they appear in geonews_articles.location_name, so a coarse
# hint can be trusted to break ambiguous city matches.
_HINT_COUNTRY = {
    "israel": "IL", "palestine": "PS", "united states": "US", "russia": "RU",
    "iran": "IR", "ukraine": "UA", "japan": "JP", "united kingdom": "GB",
    "france": "FR", "germany": "DE", "india": "IN", "china": "CN", "syria": "SY",
    "yemen": "YE", "afghanistan": "AF", "sudan": "SD", "iraq": "IQ", "egypt": "EG",
    "turkey": "TR", "belgium": "BE", "canada": "CA", "brazil": "BR",
    "pakistan": "PK", "saudi arabia": "SA", "united arab emirates": "AE",
    "poland": "PL", "spain": "ES", "italy": "IT", "greece": "GR",
    "nigeria": "NG", "ethiopia": "ET", "somalia": "SO", "vietnam": "VN",
    "indonesia": "ID", "philippines": "PH", "mexico": "MX", "argentina": "AR",
    "mali": "ML", "burkina faso": "BF", "niger": "NE", "myanmar": "MM",
    "congo": "CD", "democratic republic of the congo": "CD",
}


def hint_for(location_name: Optional[str]) -> Optional[str]:
    """Derive a country hint from an article's coarse location string."""
    if not location_name:
        return None
    return _HINT_COUNTRY.get(location_name.strip().lower())


def resolve_article(
    row: Dict[str, object],
    index: Dict[str, List[Place]],
    *,
    min_population: int = DEFAULT_MIN_POPULATION,
) -> ResolvedArticle:
    """Resolve the primary place for one article.

    The first mention in the title is preferred over anything in the summary:
    headlines name where the event happened, summaries often wander.
    """
    title = str(row.get("title") or "")
    summary = str(row.get("summary") or "")
    hint = hint_for(row.get("location_name"))

    resolved = ResolvedArticle(
        article_id=int(row["id"]),
        ambiguous=bool(row.get("ambiguous", False)),
    )

    for text in (title, summary):
        hits = resolve_text(
            text, index, hint_country=hint, min_population=min_population
        )
        if not hits:
            continue
        resolved.mentions = [str(h["mention"]) for h in hits]
        resolved.ambiguous = any(bool(h["ambiguous"]) for h in hits)
        place: Place = hits[0]["place"]  # type: ignore[assignment]
        resolved.latitude = place.latitude
        resolved.longitude = place.longitude
        resolved.city = place.name
        resolved.admin1 = place.admin1
        resolved.country = place.country_code
        resolved.country_code = place.country_code
        break

    return resolved