"""Tests for the tool-calling agent loop.

The model is faked throughout. What is under test is the loop's behaviour under
awkward model output: refusing to search, emitting malformed tool arguments,
looping forever, or calling tools that do not exist. Those are the failure modes
that produce confidently wrong answers in production.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import pytest

from local_butler.apps.geonews.agent import GeonewsAgent, MAX_TOOL_ROUNDS


class FakeLLM:
    """Replays a scripted list of assistant messages.

    Each entry is a dict shaped like an OpenAI completion message. When the
    script runs out the last entry repeats, which is how the loop-forever guard
    gets tested.
    """

    def __init__(self, script: List[Dict[str, Any]]) -> None:
        self.script = script
        self.calls: List[List[Dict[str, Any]]] = []
        self.tools: List[Any] = []

    def complete_with_tools(self, messages, tools=None, **kwargs):
        self.calls.append(list(messages))
        self.tools.append(tools)
        index = min(len(self.calls) - 1, len(self.script) - 1)
        return self.script[index]

    def complete(self, messages, **kwargs):
        self.calls.append(list(messages))
        if any("results above" in str(m.get("content", "")) for m in messages):
            return "Answer built from the retrieved results."
        return "I don't have access to real-time news."


def tool_call(name: str, arguments: Dict[str, Any], call_id: str = "call_1"):
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


@pytest.fixture
def agent(monkeypatch):
    from local_butler.apps.geonews import agent as agent_module

    class Session:
        def query(self, *args, **kwargs):  # pragma: no cover - article lookups unused
            class Result:
                def filter(self, *a, **k):
                    return self

                def first(self):
                    return None

            return Result()

    monkeypatch.setattr(agent_module, "search_everything", lambda *a, **k: [
        ("Live news wires", "Live news wires: no results."),
        ("Web search", "Web search unavailable: stubbed out"),
    ])

    def make(llm, memory=None):
        return GeonewsAgent(Session(), llm=llm, memory=memory)

    return make


def test_search_tool_then_answer(agent):
    llm = FakeLLM([
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [tool_call("search_news", {"query": "lebanon"})],
        },
        {"role": "assistant", "content": "Here is what happened."},
    ])

    answer, meta = agent(llm).run("What happened in Lebanon?")

    assert "Here is what happened" in answer
    assert meta["tools"] == ["search_news"]
    assert meta["searches"] == ["lebanon"]


def test_refusal_triggers_forced_search(agent):
    """A model that refuses without searching must still produce an answer.

    This is the exact failure the feature exists to fix: the old chat answered
    "I don't have access to real-time news" because that was all the weights
    knew.
    """
    llm = FakeLLM([
        {"role": "assistant", "content": "I don't have access to real-time news."},
    ])

    answer, meta = agent(llm).run("What happened today?")

    assert meta["forced_search"] is True
    assert "results above" in json.dumps(
        [str(c) for m in llm.calls for c in m], default=str
    )
    assert answer == "Answer built from the retrieved results."


def test_empty_completion_retries_once(agent):
    llm = FakeLLM([
        {"role": "assistant", "content": ""},
        {"role": "assistant", "content": "Sorry, here is the answer."},
    ])

    answer, _ = agent(llm).run("Anything recent?")

    assert "here is the answer" in answer
    assert len(llm.calls) == 2
    assert any("Please answer now." == m.get("content") for m in llm.calls[1])


def test_loop_guard_stops_at_max_rounds(agent):
    """A model stuck in a tool-call loop must not hang the request."""
    llm = FakeLLM([
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [tool_call("search_news", {"query": "loop"})],
        }
    ])

    answer, meta = agent(llm).run("loop forever")

    # MAX_TOOL_ROUNDS tool rounds, plus the forced-search completion.
    assert len(llm.calls) == MAX_TOOL_ROUNDS + 1
    assert meta["searches"]


def test_unknown_tool_is_reported_not_fatal(agent):
    llm = FakeLLM([
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [tool_call("delete_everything", {})],
        },
        {"role": "assistant", "content": "Understood, I cannot do that."},
    ])

    answer, meta = agent(llm).run("delete everything")

    assert "Understood" in answer
    assert meta["tools"] == ["delete_everything"]  # attempted, and recorded


def test_malformed_arguments_do_not_crash(agent):
    llm = FakeLLM([
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "bad",
                    "type": "function",
                    "function": {"name": "read_article", "arguments": "not json"},
                }
            ],
        },
        {"role": "assistant", "content": "I could not read that article."},
    ])

    answer, _ = agent(llm).run("summarise article")

    assert "could not read" in answer


def test_remember_and_recall_roundtrip(agent):
    class Memory:
        def __init__(self):
            self.notes = []

        def add_note(self, note, topic=""):
            saved = {"id": len(self.notes) + 1, "topic": topic, "note": note}
            self.notes.append(saved)
            return saved

        def search_notes(self, query, limit=6):
            return [n for n in self.notes if query.lower() in n["note"].lower()]

        def add_event(self, *args, **kwargs):
            return {}

    memory = Memory()
    llm = FakeLLM([
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                tool_call("remember", {"note": "I follow the EU migration file"})
            ],
        },
        {"role": "assistant", "content": "Noted, I'll keep that in mind."},
    ])

    answer, meta = agent(llm, memory=memory).run("Remember I follow the EU migration file")

    assert "Noted" in answer
    assert memory.notes[0]["note"] == "I follow the EU migration file"
    assert meta["notes"][0]["id"] == 1


def test_article_context_is_prepended(agent):
    class Article:
        id = 42
        title = "EU tightens migration rules"
        body = "The European Commission approved new return rules."
        summary = "New return rules approved."
        source_name = "Eu Commission"
        url = "https://example.org/a"

    class ArticleSession:
        def query(self, *args, **kwargs):
            class Result:
                def filter(self, *a, **k):
                    return self

                def first(self):
                    return Article()

            return Result()

    from local_butler.apps.geonews.agent import GeonewsAgent

    agent_obj = GeonewsAgent(
        ArticleSession(), llm=FakeLLM([{"role": "assistant", "content": "ok"}])
    )
    messages = agent_obj._base_messages("what does it say?", article_id=42)

    assert messages[0]["role"] == "system"
    assert "EU tightens migration rules" in messages[1]["content"]
    assert messages[-1]["content"] == "what does it say?"


def test_generic_search_query_is_replaced_by_the_question(agent):
    """Searching "current events" matches nothing; the user's words are better."""
    captured = {}

    def fake_search_everything(query, corpus=None, limit=6):
        captured["query"] = query
        return [("Live news wires", "Live news wires: no results.")]

    agent_obj = agent(FakeLLM([
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [tool_call("search_news", {"query": "current events"})],
        },
        {"role": "assistant", "content": "Here is the roundup."},
    ]))
    monkey = pytest.MonkeyPatch()
    monkey.setattr("local_butler.apps.geonews.agent.search_everything", fake_search_everything)
    try:
        answer, meta = agent_obj.run("What are the biggest world stories right now?")
    finally:
        monkey.undo()

    assert captured["query"] == "What are the biggest world stories right now?"
    assert meta["searches"] == ["What are the biggest world stories right now?"]


def test_specific_search_query_is_kept(agent):
    captured = {}

    def fake_search_everything(query, corpus=None, limit=6):
        captured["query"] = query
        return []

    agent_obj = agent(FakeLLM([
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [tool_call("search_news", {"query": "Lebanon ceasefire"})],
        },
        {"role": "assistant", "content": "Answered."},
    ]))
    monkey = pytest.MonkeyPatch()
    monkey.setattr("local_butler.apps.geonews.agent.search_everything", fake_search_everything)
    try:
        agent_obj.run("What about Lebanon?")
    finally:
        monkey.undo()

    assert captured["query"] == "Lebanon ceasefire"


def test_recent_events_tool_reads_memory(agent):
    class Memory:
        def add_note(self, note, topic=""):
            return {"id": 1, "topic": topic, "note": note}

        def search_notes(self, query, limit=6):
            return []

        def add_event(self, *args, **kwargs):
            return {}

        def recent_events(self, query="", hours=72, limit=20):
            return [{"summary": "Ceasefire stalled", "created_at": "2026-09-30T10:00:00"}]

    result = agent(FakeLLM([{"role": "assistant", "content": "x"}]), memory=Memory()).run_tool(
        {"id": "1", "function": {"name": "recent_events", "arguments": "{}"}}
    )

    assert result.ok
    assert "Ceasefire stalled" in result.content


def test_corpus_search_rejects_empty_query(agent):
    agent_obj = agent(FakeLLM([{"role": "assistant", "content": "x"}]))
    assert agent_obj._search_corpus("") == []
    assert agent_obj._search_corpus("a") == []

# -- deep research tool ----------------------------------------------------


def test_deep_research_tool_is_dispatched(monkeypatch):
    """The model must be able to escalate to a full research pass on its own."""
    from local_butler.apps.geonews import deep_research as dr_module

    calls = {}

    class FakeResearcher:
        def __init__(self, db, llm=None, progress=None):
            calls["db"] = db
            calls["llm"] = llm
            calls["progress"] = progress

        def research(self, question):
            calls["question"] = question
            return "REPORT", {
                "angles": ["angle a", "angle b"],
                "articles_read": 3,
                "sources": ["Wire"],
            }

    monkeypatch.setattr(dr_module, "DeepResearcher", FakeResearcher)

    class Session:
        def query(self, *args, **kwargs):  # pragma: no cover
            class Result:
                def filter(self, *a, **k):
                    return self

                def first(self):
                    return None

            return Result()

    seen = []
    llm = FakeLLM([
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [tool_call("deep_research", {"question": "EU migration returns"})],
        },
        {"role": "assistant", "content": "Here is the full report."},
    ])
    agent = GeonewsAgent(Session(), llm=llm, memory=None)
    agent.progress = seen.append
    agent.original_question = "what is going on with EU migration returns"

    answer, meta = agent.run("what is going on with EU migration returns")

    assert calls["question"] == "EU migration returns"
    assert "REPORT" in json.dumps(
        [m for m in llm.calls[-1] if m.get("role") == "tool"]
    )
    assert answer == "Here is the full report."
    assert meta["tools"] == ["deep_research"]
    assert meta["research"][0]["articles_read"] == 3
    # The angles researched are the searches that happened.
    assert "angle a" in meta["searches"]


def test_deep_research_tool_falls_back_to_user_question(monkeypatch):
    """A model that calls deep_research with no question must still be usable."""
    from local_butler.apps.geonews import deep_research as dr_module

    captured = {}

    class FakeResearcher:
        def __init__(self, db, llm=None, progress=None):
            pass

        def research(self, question):
            captured["question"] = question
            return "REPORT", {"angles": [], "articles_read": 0, "sources": []}

    monkeypatch.setattr(dr_module, "DeepResearcher", FakeResearcher)

    agent = GeonewsAgent(object(), llm=FakeLLM([{"role": "assistant", "content": "x"}]),
                         memory=None)
    agent.original_question = "the original thing I asked"
    result = agent.run_tool(tool_call("deep_research", {}))

    assert captured["question"] == "the original thing I asked"
    assert result.ok is True
    assert result.content == "REPORT"


def test_deep_research_tool_survives_failure(monkeypatch):
    """A crashing researcher must return a failed tool result, not kill the
    chat."""
    from local_butler.apps.geonews import deep_research as dr_module

    class BrokenResearcher:
        def __init__(self, db, llm=None, progress=None):
            raise RuntimeError("no database")

    monkeypatch.setattr(dr_module, "DeepResearcher", BrokenResearcher)

    agent = GeonewsAgent(object(), llm=FakeLLM([{"role": "assistant", "content": "x"}]),
                         memory=None)
    agent.original_question = "q"
    result = agent.run_tool(tool_call("deep_research", {"question": "q"}))

    assert result.ok is False
    assert "deep_research failed" in result.content
