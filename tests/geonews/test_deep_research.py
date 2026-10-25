"""Tests for the deep research pipeline.

The model and every network backend are stubbed. What is under test is the
pipeline's structure: that it plans multiple angles rather than searching once,
that it reads article bodies rather than relying on headlines, that a failed
planner still produces multi-angle coverage, and that disagreement between
sources reaches the report instead of being smoothed away.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

import pytest

from local_butler.apps.geonews.deep_research import (
    DeepResearcher,
    MAX_ANGLES,
    MAX_ARTICLES_READ,
    extract_json_object,
)


class ScriptedLLM:
    """Replies with queued strings; repeats the last one once exhausted."""

    def __init__(self, replies: List[str]) -> None:
        self.replies = replies
        self.calls: List[List[Dict[str, Any]]] = []

    def complete(self, messages, **kwargs):
        self.calls.append(list(messages))
        index = min(len(self.calls) - 1, len(self.replies) - 1)
        return self.replies[index]


class Article:
    def __init__(self, id, title, body, source_name="Wire", url="http://x/1"):
        self.id = id
        self.title = title
        self.body = body
        self.summary = body[:200]
        self.source_name = source_name
        self.url = url


class Session:
    """Enough of a session for corpus search, article reads and nothing else."""

    def __init__(self, rows=None, articles=None, corpus_error=None):
        self.rows = rows or []
        self.articles = articles or {}
        self.corpus_error = corpus_error
        self.queries: List[str] = []

    def execute(self, sql, params):
        self.queries.append(sql)
        if self.corpus_error:
            raise self.corpus_error
        return _Rows(self.rows)

    def query(self, model):
        return _Query(self)


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _Query:
    def __init__(self, session):
        self.session = session

    def filter(self, *a, **k):
        return self

    def first(self):
        for article in self.session.articles.values():
            return article
        return None


class Row:
    def __init__(self, id, title, summary="", body="", source_name="Wire",
                 url="http://x/1", published_at="2026-09-01"):
        self.id = id
        self.title = title
        self.summary = summary
        self.body = body
        self.source_name = source_name
        self.url = url
        self.published_at = published_at


@pytest.fixture(autouse=True)
def stub_network(monkeypatch):
    """No real HTTP in tests, and every backend's result is observable."""
    from local_butler.apps.geonews import deep_research

    calls: List[str] = []

    def fake_search(query, corpus=None, limit=6):
        calls.append(query)
        return [
            ("Live news wires", f"Live news wires: result for {query}"),
            ("Web search", "Web search: blocked by rate limit"),
        ]

    monkeypatch.setattr(deep_research, "search_everything", fake_search)
    return calls


def make_researcher(session, llm, progress=None):
    return DeepResearcher(session, llm=llm, progress=progress)


# -- JSON salvage ----------------------------------------------------------


def test_extract_json_from_prose():
    """The planner wraps JSON in commentary often enough that strict parsing
    would fail more often than it succeeds."""
    reply = 'Sure! Here are the angles:\n```json\n{"angles": ["a", "b"]}\n```\nHope that helps.'
    assert extract_json_object(reply)["angles"] == ["a", "b"]


def test_extract_json_bare_array():
    assert extract_json_object('["one", "two"]')["angles"] == ["one", "two"]


def test_extract_json_gives_up_cleanly():
    assert extract_json_object("I refuse to produce JSON.") == {}


# -- planning --------------------------------------------------------------


def test_plan_uses_model_angles():
    llm = ScriptedLLM([json.dumps({"angles": ["eu migration policy", "who opposes it"]})])
    session = Session()
    researcher = make_researcher(session, llm)

    angles = researcher.plan("What is going on with EU migration returns?")

    assert angles == ["eu migration policy", "who opposes it"]
    assert len(session.queries) == 0, "planning must not hit the database"


def test_plan_caps_angle_count():
    too_many = json.dumps({"angles": [f"angle {i}" for i in range(12)]})
    researcher = make_researcher(Session(), ScriptedLLM([too_many]))

    assert len(researcher.plan("q")) == MAX_ANGLES


def test_plan_falls_back_when_model_returns_one_angle():
    """A single angle is the same as no planning, so the mechanical facets are
    added back in."""
    llm = ScriptedLLM([json.dumps({"angles": ["just one"]})])
    researcher = make_researcher(Session(), llm)

    angles = researcher.plan("What is happening with the Lebanon ceasefire?")

    assert len(angles) > 1
    assert "criticism" in " ".join(angles).lower()


def test_plan_falls_back_when_model_raises():
    class Broken:
        def complete(self, *a, **k):
            raise RuntimeError("model gone")

    researcher = make_researcher(Session(), Broken())

    angles = researcher.plan("Tell me about the semiconductor export controls")

    assert len(angles) > 1
    assert any("semiconductor" in a for a in angles)


def test_plan_dedupes_angles():
    llm = ScriptedLLM([
        json.dumps({"angles": ["same angle", "Same Angle", "other angle", "other angle"]})
    ])
    researcher = make_researcher(Session(), llm)

    angles = researcher.plan("q")

    assert angles == ["same angle", "other angle"]


# -- gathering -------------------------------------------------------------


def test_gather_searches_every_angle(stub_network):
    llm = ScriptedLLM([json.dumps({"angles": ["angle a", "angle b", "angle c"]})])
    session = Session(rows=[Row(1, "Lebanon ceasefire talks")])
    researcher = make_researcher(session, llm)
    researcher.angles = researcher.plan("Lebanon")

    researcher.gather(researcher.angles)

    for angle in ("angle a", "angle b", "angle c"):
        assert angle in stub_network


def test_gather_survives_a_failed_backend(stub_network, monkeypatch):
    """One dead source must not lose the other angles."""
    from local_butler.apps.geonews import deep_research

    def flaky(query, corpus=None, limit=6):
        if "bad" in query:
            raise RuntimeError("connection reset")
        return [("Live news wires", "fine")]

    monkeypatch.setattr(deep_research, "search_everything", flaky)
    researcher = make_researcher(Session(), ScriptedLLM(["x"]))
    researcher.angles = ["bad angle", "good angle"]

    researcher.gather(researcher.angles)

    assert any("failed: connection reset" in text for _, text in researcher.sections)
    assert any("fine" in text for _, text in researcher.sections)


def test_corpus_failure_is_not_fatal():
    session = Session(corpus_error=RuntimeError("no such table"))
    researcher = make_researcher(session, ScriptedLLM(["x"]))
    researcher.angles = ["migration"]

    researcher.gather(researcher.angles)

    # The network half still ran, so the report is thin rather than absent.
    assert any("Live news wires" in label for label, _ in researcher.sections)


# -- reading articles ------------------------------------------------------


def test_reads_bodies_of_cross_angle_articles():
    """An article matching several angles is more central than one matching a
    single angle, so it is the one worth reading in full."""
    rows = [
        Row(1, "Migration returns explained"),
        Row(2, "Migration returns criticism"),
    ]
    articles = {
        1: Article(1, "Migration returns explained", "FULL BODY ONE"),
        2: Article(2, "Migration returns criticism", "FULL BODY TWO"),
    }
    session = Session(rows=rows, articles=articles)
    researcher = make_researcher(session, ScriptedLLM(["x"]))
    # Every angle returns the same rows, so both score 2 and keep corpus order.
    researcher.angles = ["migration returns"]

    researcher.read_articles()

    assert [a["id"] for a in researcher.articles_read] == ["1", "2"]
    assert "FULL BODY ONE" in researcher.articles_read[0]["context"]


def test_article_read_is_capped():
    rows = [Row(i, f"Migration story {i}") for i in range(1, 15)]
    articles = {
        i: Article(i, f"Migration story {i}", f"BODY {i}") for i in range(1, 15)
    }
    session = Session(rows=rows, articles=articles)
    researcher = make_researcher(session, ScriptedLLM(["x"]))
    researcher.angles = ["migration"]

    researcher.read_articles()

    assert len(researcher.articles_read) == MAX_ARTICLES_READ


# -- synthesis -------------------------------------------------------------


def test_synthesis_prompt_demands_gaps_and_disagreement():
    llm = ScriptedLLM(["report"])
    researcher = make_researcher(Session(), llm)
    researcher.sections = [("Live news wires", "evidence here")]

    researcher.synthesize("What is going on?")

    prompt = llm.calls[0][0]["content"]
    assert "could not verify" in prompt
    assert "disagree" in prompt
    assert "evidence here" in prompt


def test_synthesis_truncates_and_says_so():
    llm = ScriptedLLM(["report"])
    researcher = make_researcher(Session(), llm)
    researcher.sections = [("Live news wires", "x" * 60000)]

    researcher.synthesize("q")

    prompt = llm.calls[0][0]["content"]
    assert "[Evidence truncated" in prompt
    assert len(prompt) < 30000


def test_synthesis_admits_model_failure():
    class Broken:
        def complete(self, *a, **k):
            raise RuntimeError("model gone")

    researcher = make_researcher(Session(), Broken())

    report = researcher.synthesize("q")

    assert "could not reach the local model" in report


# -- full pipeline ---------------------------------------------------------


def test_research_reports_angles_and_sources():
    llm = ScriptedLLM([
        json.dumps({"angles": ["eu migration policy", "criticism of eu migration policy"]}),
        "REPORT BODY",
    ])
    rows = [Row(7, "EU migration returns", summary="returns explained")]
    session = Session(rows=rows, articles={7: Article(7, "EU migration returns", "BODY")})
    researcher = make_researcher(session, llm)

    report, meta = researcher.research("What is really going on with EU migration returns?")

    assert report == "REPORT BODY"
    assert meta["angles"]
    assert meta["sections"] > 0
    assert "Wire" in meta["sources"]


def test_research_emits_progress_in_order():
    seen: List[str] = []
    llm = ScriptedLLM([json.dumps({"angles": ["a", "b"]}), "REPORT"])
    researcher = make_researcher(Session(), llm, progress=seen.append)

    researcher.research("question")

    assert seen[0] == "Planning research angles"
    assert "Done" == seen[-1]
    assert any("Searching" in step for step in seen)


def test_research_with_empty_question_does_nothing():
    llm = ScriptedLLM(["never used"])
    researcher = make_researcher(Session(), llm)

    report, meta = researcher.research("   ")

    assert llm.calls == []
    assert meta["angles"] == []


def test_blocked_backend_is_not_counted_as_a_source():
    """A rate-limited backend is not a citation. Listing it as one would make a
    thin report look well-sourced."""
    from local_butler.apps.geonews.deep_research import _unique_sources

    sections = [
        ("Live news wires", "result"),
        ("Web search", "Web search unavailable: rate limited"),
        ("Wikipedia", "failed: blocked"),
    ]
    sources = _unique_sources(sections, [{"source": "Anadolu Agency"}])

    assert sources == ["Anadolu Agency", "Live news wires"]

# -- endpoint --------------------------------------------------------------


class TempStoreMixin:
    """Point MemoryStore at a throwaway database.

    Without this the endpoint tests write real chats into the user's butler.db,
    which is both a test side effect on production data and a source of order
    dependence between tests.
    """

    @staticmethod
    def temp_store(monkeypatch, tmp_path):
        from local_butler.apps.geonews import main as main_module
        from local_butler.apps.geonews.memory import MemoryStore

        store = MemoryStore(f"sqlite:///{tmp_path / 'chats.db'}")
        monkeypatch.setattr(main_module, "get_store", lambda: store)
        return store


class FakeSession:
    """Stand-in for SessionLocal: the fake agents never touch the database."""

    def close(self):
        pass


def _sse_events(body: bytes) -> List[Dict[str, Any]]:
    """Parse an SSE body into a list of event dicts."""
    events = []
    for line in body.decode("utf-8").splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        events.append(json.loads(line[5:]))
    return events


def test_research_endpoint_streams_progress_then_report(monkeypatch, tmp_path):
    """A minute-long pass has to be visible, or the user assumes it hung."""
    from local_butler.apps.geonews import main as main_module

    class FakeResearcher:
        def __init__(self, db, progress=None):
            self.progress = progress

        def research(self, question):
            self.progress("Planning research angles")
            self.progress("Searching 3 angles")
            self.progress("Reading 4 articles")
            self.progress("Writing report")
            self.progress("Done")
            return "THE REPORT", {
                "angles": ["a", "b", "c"],
                "articles_read": 4,
                "sources": ["Anadolu"],
            }

    monkeypatch.setattr(main_module, "DeepResearcher", FakeResearcher)
    monkeypatch.setattr(main_module, "SessionLocal", FakeSession)
    TempStoreMixin.temp_store(monkeypatch, tmp_path)

    import httpx
    from httpx import ASGITransport

    async def call():
        transport = ASGITransport(app=main_module.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post(
                "/api/agent/research", json={"question": "what is going on?"}
            )

    import asyncio

    response = asyncio.run(call())
    events = _sse_events(response.content)
    kinds = [event["type"] for event in events]

    assert kinds[0] == "thinking"
    assert kinds[-1] == "done"
    progress = [e["content"] for e in events if e["type"] == "progress"]
    assert progress == [
        "Planning research angles",
        "Searching 3 angles",
        "Reading 4 articles",
        "Writing report",
        "Done",
    ]
    report = "".join(e["content"] for e in events if e["type"] == "delta")
    assert report == "THE REPORT"
    source_line = next(e for e in events if e["type"] == "sources")["content"]
    assert "3 angles" in source_line
    assert "4 articles read" in source_line


def test_research_endpoint_surfaces_error(monkeypatch, tmp_path):
    from local_butler.apps.geonews import main as main_module

    class BrokenResearcher:
        def __init__(self, db, progress=None):
            pass

        def research(self, question):
            raise RuntimeError("everything is on fire")

    monkeypatch.setattr(main_module, "DeepResearcher", BrokenResearcher)
    monkeypatch.setattr(main_module, "SessionLocal", FakeSession)
    TempStoreMixin.temp_store(monkeypatch, tmp_path)

    import asyncio
    import httpx
    from httpx import ASGITransport

    async def call():
        transport = ASGITransport(app=main_module.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post("/api/agent/research", json={"question": "q"})

    response = asyncio.run(call())
    events = _sse_events(response.content)

    assert any(
        e["type"] == "error" and "on fire" in e["content"] for e in events
    )
    assert events[-1]["type"] == "done"


def test_chat_endpoint_survives_agent_failure(monkeypatch, tmp_path):
    """A crash inside the worker thread must become an SSE error, not a hung
    stream or a 500 with no explanation."""
    from local_butler.apps.geonews import main as main_module

    class BrokenAgent:
        def __init__(self, db, progress=None, history=None):
            pass

        def run(self, message, article_id=None):
            raise RuntimeError("agent exploded")

    monkeypatch.setattr(main_module, "GeonewsAgent", BrokenAgent)
    monkeypatch.setattr(main_module, "SessionLocal", FakeSession)
    TempStoreMixin.temp_store(monkeypatch, tmp_path)

    import asyncio
    import httpx
    from httpx import ASGITransport

    async def call():
        transport = ASGITransport(app=main_module.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post("/api/agent/chat", json={"message": "hi"})

    response = asyncio.run(call())
    events = _sse_events(response.content)

    assert any(
        e["type"] == "error" and "exploded" in e["content"] for e in events
    )
    assert events[-1]["type"] == "done"
