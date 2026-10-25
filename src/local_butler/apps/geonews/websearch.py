"""Search backends for the Geonews agent.

The agent needs three different kinds of lookup, and conflating them is what
makes a news chatbot useless:

* **Own corpus** -- Geonews already ingests thousands of articles. Asking about
  "this week's flooding" should hit the articles it just ingested, which is both
  fresher and more relevant than anything on the open web.
* **Live news wires** -- background feeds (UN, EU Commission, central banks,
  wire RSS) fetched on demand, so "what happened today" reflects right now
  rather than whatever was ingested at the last scheduled run.
* **Encyclopedia** -- stable background facts ("what is the EU return order?")
  that no article will state from scratch.

Backend notes, all learned by testing from this machine:

* DuckDuckGo serves an anti-bot ``anomaly.js`` challenge to this IP on both
  ``html.`` and ``lite.``, so web search is best-effort and often unavailable.
  It stays wired up because it works from residential IPs and may recover.
* GDELT answers "limit one request every 5 seconds" and then still refuses, so
  it is not a dependency. The RSS path covers the same need more reliably.
* Wikipedia's action API is fast, stable and free.
* The configured RSS feeds are all reachable without keys or auth.
"""

from __future__ import annotations

import concurrent.futures
import html
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

DDG_LITE = "https://lite.duckduckgo.com/lite/?q="
WIKI_API = "https://en.wikipedia.org/w/api.php"

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)


class WebSearchError(RuntimeError):
    """Raised when a search backend cannot answer.

    Named for the fact that most of these are refusals or blocks rather than
    real failures: the caller should report the gap and carry on with the other
    backends instead of treating it as fatal.
    """


# Kept as an alias because the earlier name reads better at some call sites.
SearchError = WebSearchError


def _fetch(url: str, timeout: float = 20.0) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            return response.read().decode(charset, errors="replace")
    except urllib.error.HTTPError as exc:
        raise WebSearchError(f"{url} returned {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise WebSearchError(f"cannot reach {url}: {exc.reason}") from exc


def _fetch_json(url: str, timeout: float = 20.0) -> Dict[str, Any]:
    raw = _fetch(url, timeout=timeout)
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise WebSearchError(f"{url} did not return JSON") from exc


def _strip_tags(fragment: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", fragment)).strip()


def _unwrap_ddg(href: str) -> str:
    """DDG wraps results in /l/?uddg=<urlencoded>."""
    if href.startswith("//"):
        href = "https:" + href
    parsed = urllib.parse.urlparse(href)
    if "duckduckgo.com" in parsed.netloc:
        target = urllib.parse.parse_qs(parsed.query).get("uddg")
        if target:
            return urllib.parse.unquote(target[0])
    return href


# -- web search ------------------------------------------------------------


def _is_ddg_challenge(page: str) -> bool:
    """True when DuckDuckGo served an anti-bot page instead of results.

    Returning an empty list here would be indistinguishable from a genuinely
    empty search, so callers get an explicit signal instead.
    """
    if not page:
        return True
    if "anomaly.js" in page or "anomaly-modal" in page:
        return True
    return "result-link" not in page and "result__title" not in page


# Each DDG result is one anchor carrying class='result-link' and href. The
# attribute order varies between the lite and html layouts, so parse the anchor
# as a whole and read attributes out of it rather than matching a fixed order.
_ANCHOR_RE = re.compile(r"<a\b([^>]*\bclass=['\"][^'\"]*result-link[^'\"]*['\"][^>]*)>(.*?)</a>", re.S | re.I)
_HREF_RE = re.compile(r"\bhref=['\"]([^'\"]+)['\"]", re.I)


def web_search(query: str, limit: int = 6) -> List[Dict[str, str]]:
    """DuckDuckGo lite search. Returns title/url/snippet dicts.

    Raises ``WebSearchError`` when blocked, so a caller can say "web search was
    unavailable" rather than implying there is no news.
    """
    query = (query or "").strip()
    if not query:
        return []

    page = _fetch(DDG_LITE + urllib.parse.quote_plus(query))

    if _is_ddg_challenge(page):
        raise WebSearchError(
            "DuckDuckGo returned an anti-bot page; no web search result available"
        )

    results: List[Dict[str, str]] = []
    seen = set()
    for attrs, title_html in _ANCHOR_RE.findall(page):
        href_match = _HREF_RE.search(attrs)
        if not href_match:
            continue
        url = _unwrap_ddg(html.unescape(href_match.group(1)))
        if not url or url in seen or "duckduckgo.com" in url:
            continue
        seen.add(url)
        results.append(
            {"title": _strip_tags(title_html)[:200], "url": url, "snippet": ""}
        )
        if len(results) >= limit:
            break

    _attach_snippets(page, results)
    return results


def _attach_snippets(page: str, results: List[Dict[str, str]]) -> None:
    """Fill in snippets by matching each snippet anchor's own href.

    Snippets sit in a separate table row from the title, so pairing them by
    position breaks whenever a result has no snippet.
    """
    snippet_re = re.compile(
        r"<a\b([^>]*class=['\"][^'\"]*result[-_]{1,2}snippet[^'\"]*['\"][^>]*)>(.*?)</a>",
        re.S | re.I,
    )
    by_url: Dict[str, str] = {}
    for attrs, body in snippet_re.findall(page):
        href_match = _HREF_RE.search(attrs)
        if not href_match:
            continue
        by_url[_unwrap_ddg(html.unescape(href_match.group(1)))] = _strip_tags(body)

    for result in results:
        result["snippet"] = by_url.get(result["url"], "")[:400]


# -- wikipedia -------------------------------------------------------------


def wikipedia_search(query: str, limit: int = 3) -> List[Dict[str, str]]:
    """Search Wikipedia and return a short extract for each hit."""
    query = (query or "").strip()
    if not query:
        return []

    params = urllib.parse.urlencode(
        {
            "action": "query",
            "list": "search",
            "srsearch": query,
            "format": "json",
            "srlimit": str(limit),
        }
    )
    data = _fetch_json(WIKI_API + "?" + params)
    hits = data.get("query", {}).get("search", [])
    if not hits:
        return []

    titles = [h["title"] for h in hits]
    extract_params = urllib.parse.urlencode(
        {
            "action": "query",
            "prop": "extracts",
            "exintro": "1",
            "explaintext": "1",
            "format": "json",
            "titles": "|".join(titles),
        }
    )
    extracts = _fetch_json(WIKI_API + "?" + extract_params)
    pages = extracts.get("query", {}).get("pages", {})

    by_title = {}
    for page in pages.values():
        by_title[page.get("title", "")] = page.get("extract", "") or ""

    out = []
    for hit in hits:
        title = hit["title"]
        extract = by_title.get(title, "") or re.sub(r"<[^>]+>", "", hit.get("snippet", ""))
        out.append(
            {
                "title": title,
                "url": "https://en.wikipedia.org/wiki/"
                + urllib.parse.quote(title.replace(" ", "_")),
                "snippet": extract.strip()[:600],
            }
        )
    return out


# -- live RSS wires --------------------------------------------------------

_ITEM_RE = re.compile(r"<(item|entry)\b.*?</\1>", re.S | re.I)


def _tag_text(fragment: str, *names: str) -> str:
    """Pull the first matching tag's text out of an RSS item fragment."""
    for name in names:
        match = re.search(
            rf"<{name}\b[^>]*>(.*?)</{name}>", fragment, re.S | re.I
        )
        if match:
            return _strip_tags(match.group(1)).strip()
    return ""


def _tag_attr(fragment: str, name: str, attr: str = "href") -> str:
    match = re.search(rf"<{name}\b[^>]*\b{attr}=[\"']([^\"']+)[\"']", fragment, re.I)
    return html.unescape(match.group(1)) if match else ""


def parse_feed(raw: str, source: str = "") -> List[Dict[str, str]]:
    """Extract (title, link, published, summary) from an RSS 2.0 or Atom feed."""
    out: List[Dict[str, str]] = []
    for match in _ITEM_RE.finditer(raw or ""):
        fragment = match.group(0)
        title = _tag_text(fragment, "title")
        link = _tag_text(fragment, "link") or _tag_attr(fragment, "link")
        published = _tag_text(fragment, "pubDate", "published", "updated", "dc:date")
        summary = _tag_text(fragment, "description", "summary", "content")
        if not title:
            continue
        out.append(
            {
                "title": title[:250],
                "url": link,
                "published": published,
                "snippet": summary[:400],
                "domain": _domain_of(source),
            }
        )
    return out


def _domain_of(source: str) -> str:
    """A feed URL or a config key both work as a source label."""
    if not source:
        return ""
    if "://" not in source:
        return source
    return urllib.parse.urlparse(source).netloc or source


def _feed_score(entry: Dict[str, str], terms: List[str]) -> int:
    """Relevance for one feed item.

    Title matches count triple: a headline mentioning "inflation" is about
    inflation, while a body mention is often incidental.
    """
    title = entry.get("title", "").lower()
    body = entry.get("snippet", "").lower()
    score = 0
    for term in terms:
        if term in title:
            score += 3
        elif term in body:
            score += 1
    return score


def live_news(query: str = "", limit: int = 12, sources: Optional[List[str]] = None) -> List[Dict[str, str]]:
    """Fetch configured RSS feeds now and rank items against ``query``.

    Empty query means "give me the latest", which is the useful default for a
    user asking what is happening.
    """
    from local_butler.apps.geonews.ingestion.config import enabled_sources

    config = enabled_sources()
    terms = [t.lower() for t in re.findall(r"[A-Za-z0-9]{3,}", query or "")]

    feeds: List[tuple] = []
    for key, value in config.items():
        if sources and key not in sources:
            continue
        url = value.get("url") or value.get("feed_url")
        if not url:
            continue
        if not value.get("enabled", True):
            continue
        feeds.append((key, str(url)))
    if not feeds:
        return []

    collected: List[Dict[str, str]] = []
    errors = 0

    def pull(item: tuple) -> List[Dict[str, str]]:
        key, url = item
        try:
            return parse_feed(_fetch(url, timeout=15), source=key)
        except WebSearchError:
            return []

    # Fetch concurrently: 26 sequential feeds with a 15s timeout would make the
    # chat feel broken.
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        for entries in pool.map(pull, feeds):
            collected.extend(entries)

    if not collected:
        return []

    if terms:
        # Only keep genuine matches. Returning unrelated headlines under a
        # specific query is how a model ends up confidently wrong: it reads the
        # text as if it were about the topic asked for. An honest empty result
        # lets the agent say the topic is not in today's wires.
        scored = [
            (_feed_score(entry, terms), order, entry)
            for order, entry in enumerate(collected)
        ]
        scored.sort(key=lambda row: (-row[0], row[1]))
        collected = [entry for score, _, entry in scored if score > 0]

    seen = set()
    unique = []
    for entry in collected:
        key = entry["title"].lower()[:80]
        if key in seen:
            continue
        seen.add(key)
        unique.append(entry)

    return unique[:limit]


def format_results(label: str, results: List[Dict[str, str]]) -> str:
    """Render tool output compactly so it fits the model's context."""
    if not results:
        return f"{label}: no results."
    lines = [f"{label} ({len(results)} results):"]
    for index, item in enumerate(results, 1):
        bits = [f"[{index}] {item.get('title', '').strip()}"]
        if item.get("published"):
            bits.append(item["published"])
        if item.get("domain"):
            bits.append(item["domain"])
        lines.append(" ".join(bits))
        if item.get("url"):
            lines.append(f"    url: {item['url']}")
        if item.get("snippet"):
            lines.append(f"    {item['snippet']}")
    return "\n".join(lines)


def search_everything(
    query: str,
    corpus: Optional[List[Dict[str, str]]] = None,
    limit: int = 6,
) -> List[tuple]:
    """Query every available source and return ``(label, text)`` sections.

    Each backend is independent: one being rate-limited or blocked must not take
    the whole search down. A failed backend still gets a section, saying why it
    is empty, so the model can tell "not found" apart from "not reachable" --
    which matters, because only one of those is a real absence of news.
    """
    sections: List[tuple] = []

    if corpus is not None:
        sections.append(("Geonews corpus", format_results("Geonews corpus", corpus)))

    for label, call in (
        ("Live news wires", lambda: live_news(query, limit=max(limit, 10))),
        ("Web search", lambda: web_search(query, limit=limit)),
        ("Wikipedia", lambda: wikipedia_search(query, limit=3)),
    ):
        try:
            sections.append((label, format_results(label, call())))
        except WebSearchError as exc:
            sections.append((label, f"{label} unavailable: {exc}"))

    return sections


def search_everything_text(
    query: str,
    corpus: Optional[List[Dict[str, str]]] = None,
    limit: int = 6,
) -> str:
    """Flat text form of :func:`search_everything`, for tool output."""
    return "\n\n".join(text for _, text in search_everything(query, corpus, limit))