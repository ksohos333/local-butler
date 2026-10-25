"""Tests for the search layer.

Every network call is stubbed. These tests care about parsing and scoring, which
is where the bugs live: DuckDuckGo changes its markup, and a silent parser
failure looks identical to "no results", which would let the agent claim there
is no news.
"""

from __future__ import annotations

import pytest

from local_butler.apps.geonews import websearch

DDG_HTML = """
<html><body><div class="results">
  <div class="result results_links results_links_deep web-result">
    <h2 class="result__title">
      <a rel="nofollow" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.org%2Fone"
         class='result-link'>First <b>result</b> title</a>
    </h2>
    <a class="result__snippet" href="https://example.org/one">Snippet about the
      first <b>result</b> &amp; its details</a>
  </div>
  <div class="result results_links results_links_deep web-result">
    <h2 class="result__title">
      <a class='result-link' rel="nofollow"
         href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.org%2Ftwo">Second title</a>
    </h2>
    <a class="result__snippet" href="https://example.org/two">Second snippet</a>
  </div>
</div></body></html>
"""

WIKI_SEARCH_JSON = {
    "query": {
        "search": [
            {
                "title": "Hezbollah–Israel conflict",
                "snippet": "Armed conflict <span>between</span> Israel and Hezbollah.",
                "timestamp": "2026-09-30T00:00:00Z",
            }
        ]
    }
}

WIKI_EXTRACTS_JSON = {
    "query": {
        "pages": {
            "123": {
                "title": "Hezbollah–Israel conflict",
                "extract": "The Hezbollah–Israel conflict is an armed conflict "
                "between Israel and the Lebanese Shia Islamist militant group.",
            }
        }
    }
}

RSS_XML = """<?xml version="1.0"?>
<rss version="2.0"><channel>
  <title>Example Wire</title>
  <item>
    <title>Ceasefire talks stall after weeks of fighting</title>
    <link>https://wire.example/story-1</link>
    <description>The mediation effort broke down on Tuesday.</description>
    <pubDate>Wed, 30 Sep 2026 08:00:00 GMT</pubDate>
  </item>
  <item>
    <title>Unrelated weather report</title>
    <link>https://wire.example/story-2</link>
    <description>Sunny with highs near 20 degrees.</description>
    <pubDate>Tue, 29 Sep 2026 08:00:00 GMT</pubDate>
  </item>
</channel></rss>
"""


@pytest.fixture
def stub_fetch(monkeypatch):
    """Install a fake HTTP layer and hand back the call log."""

    calls = []

    def _fetch(url, timeout=10):
        calls.append(url)
        if "duckduckgo" in url:
            if websearch._is_ddg_challenge(DDG_HTML):
                raise websearch.WebSearchError("DuckDuckGo returned an anomaly page")
            return DDG_HTML
        if "list=search" in url:
            import json as json_module

            return json_module.dumps(WIKI_SEARCH_JSON)
        if "prop=extracts" in url:
            import json as json_module

            return json_module.dumps(WIKI_EXTRACTS_JSON)
        if url.endswith(".xml") or url.endswith("rss"):
            return RSS_XML
        raise websearch.WebSearchError(f"unstubbed url: {url}")

    monkeypatch.setattr(websearch, "_fetch", _fetch)
    return calls


def test_ddg_results_are_extracted(stub_fetch):
    results = websearch.web_search("lebanon")

    assert len(results) == 2
    # Redirect wrapper must be unwrapped to the real destination.
    assert results[0]["url"] == "https://example.org/one"
    assert results[0]["title"] == "First result title"
    assert "snippet" in results[0]["snippet"].lower()


def test_ddg_attribute_order_does_not_matter(stub_fetch):
    """First result puts href before class, second puts class first.

    A parser that assumed one order silently returned nothing before.
    """
    urls = [r["url"] for r in websearch.web_search("lebanon")]
    assert urls == ["https://example.org/one", "https://example.org/two"]


def test_ddg_challenge_raises_rather_than_returning_empty(stub_fetch, monkeypatch):
    monkeypatch.setattr(
        websearch, "_fetch", lambda url, timeout=10: "anomaly.js challenge"
    )

    with pytest.raises(websearch.WebSearchError):
        websearch.web_search("lebanon")


def test_rss_is_parsed_and_scored(stub_fetch):
    entries = websearch.parse_feed(RSS_XML, "https://wire.example/rss")
    assert len(entries) == 2
    assert entries[0]["title"].startswith("Ceasefire talks stall")
    assert entries[0]["url"] == "https://wire.example/story-1"
    assert entries[0]["domain"] == "wire.example"


def test_feed_score_prefers_term_matches(stub_fetch):
    entries = websearch.parse_feed(RSS_XML, "https://wire.example/rss")
    scores = [websearch._feed_score(entry, ["ceasefire"]) for entry in entries]

    assert scores[0] > scores[1]


def test_live_news_drops_unrelated_entries(stub_fetch):
    results = websearch.live_news("ceasefire fighting", limit=5)

    assert results
    assert all("weather" not in r["title"].lower() for r in results)
    assert any("ceasefire" in r["title"].lower() for r in results)


def test_live_news_marks_empty_result_honestly(stub_fetch):
    """No term matches must return nothing, not the first N entries."""
    results = websearch.live_news("cryptocurrency mining halving", limit=5)

    assert results == []


def test_wikipedia_parsing(stub_fetch):
    results = websearch.wikipedia_search("lebanon")

    assert results[0]["title"] == "Hezbollah–Israel conflict"
    assert results[0]["url"].startswith("https://en.wikipedia.org/")


def test_search_everything_keeps_sources_separate(stub_fetch, monkeypatch):
    monkeypatch.setattr(
        websearch, "live_news", lambda q, limit=10: [
            {"title": "Wire story", "url": "https://wire.example/1",
             "domain": "wire.example", "published": "2026-09-30", "snippet": "s"}
        ]
    )

    sections = websearch.search_everything("lebanon")

    labels = [s[0] for s in sections]
    assert any("Live news" in label for label in labels)
    assert any("Web search" in label for label in labels)


def test_search_everything_survives_a_dead_provider(stub_fetch, monkeypatch):
    def boom(*args, **kwargs):
        raise websearch.WebSearchError("blocked")

    monkeypatch.setattr(websearch, "web_search", boom)

    sections = websearch.search_everything("lebanon")

    # The section is present but says why it is empty, instead of vanishing.
    labelled = dict(sections)
    assert "Web search" in labelled
    assert "blocked" in labelled["Web search"].lower()
    # The other backends still contributed.
    assert "Wikipedia" in labelled and "no results" not in labelled["Wikipedia"]


def test_format_results_is_readable_and_numbered(stub_fetch):
    text = websearch.format_results(
        "Live news wires",
        [
            {
                "title": "Headline here",
                "url": "https://example.org/a",
                "domain": "example.org",
                "published": "2026-09-30",
                "snippet": "Body text",
            }
        ],
    )

    assert "Live news wires" in text
    assert "[1]" in text
    assert "Headline here" in text
    assert "https://example.org/a" in text


def test_format_results_handles_empty_list():
    assert websearch.format_results("Empty source", []) == "Empty source: no results."