"""Tests for agent chat history: the store, the endpoints, and the replay.

Chat history exists because two things were true before it: the transcript lived
only in the DOM, so a reload lost it; and the agent had a durable event log but
no transcript, meaning it could recall what it learned while being unable to show
what the user asked. These tests pin both halves.
"""

import asyncio
import json

import pytest

from local_butler.apps.geonews.memory import MemoryStore


@pytest.fixture()
def store(tmp_path):
    return MemoryStore(f"sqlite:///{tmp_path / 'chats.db'}")


# -- store ----------------------------------------------------------------


def test_first_question_names_the_chat(store):
    chat = store.create_chat()
    assert chat["title"] == "New chat"

    store.add_message(chat["id"], "user", "What happened with the EU migration returns?")

    assert store.get_chat(chat["id"])["title"] == (
        "What happened with the EU migration returns?"
    )


def test_title_is_not_overwritten_by_later_questions(store):
    chat = store.create_chat()
    store.add_message(chat["id"], "user", "Tell me about the Belgium election")
    store.add_message(chat["id"], "assistant", "It went badly.")
    store.add_message(chat["id"], "user", "and the EU one?")

    assert store.get_chat(chat["id"])["title"] == "Tell me about the Belgium election"


def test_long_first_question_makes_a_truncated_title(store):
    chat = store.create_chat()
    store.add_message(
        chat["id"],
        "user",
        "Could you please explain in detail everything that has happened "
        "with the European Union migrant return rules over the last year?",
    )

    title = store.get_chat(chat["id"])["title"]
    assert len(title) <= 60
    assert title.endswith("...")
    assert title.startswith("Could you please explain")


def test_transcript_is_returned_oldest_first_with_metadata(store):
    chat = store.create_chat()
    store.add_message(chat["id"], "user", "q1")
    store.add_message(chat["id"], "assistant", "a1", meta={"tools": ["search_news"]})
    store.add_message(chat["id"], "user", "q2")

    messages = store.get_chat(chat["id"])["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant", "user"]
    assert [m["content"] for m in messages] == ["q1", "a1", "q2"]
    assert messages[1]["meta"] == {"tools": ["search_news"]}
    assert messages[0]["meta"] is None


def test_malformed_metadata_does_not_break_the_transcript(store):
    """A hand-edited or half-written meta blob must not make history unreadable."""
    chat = store.create_chat()
    store.add_message(chat["id"], "user", "q")
    store.add_message(chat["id"], "assistant", "a", meta={"tools": ["search_news"]})

    import sqlite3

    conn = sqlite3.connect(store.engine.url.database)
    conn.execute("UPDATE geonews_agent_messages SET meta = ? WHERE role = 'assistant'", ("{not json",))
    conn.commit()
    conn.close()

    messages = store.get_chat(chat["id"])["messages"]
    assert messages[1]["meta"] is None


def test_chats_are_listed_most_recently_active_first(store):
    first = store.create_chat("first")
    second = store.create_chat("second")
    store.add_message(first["id"], "user", "back to the first one")

    titles = [c["title"] for c in store.list_chats()]
    assert titles[0] == "first"
    assert titles[1] == "second"


def test_empty_chats_are_still_listed(store):
    """A chat opened by the New chat button and never used still shows up."""
    store.create_chat("abandoned")
    assert [c["title"] for c in store.list_chats()] == ["abandoned"]
    assert store.list_chats()[0]["preview"] == ""


def test_list_preview_is_the_first_question_not_the_latest(store):
    chat = store.create_chat()
    store.add_message(chat["id"], "user", "the opening question")
    store.add_message(chat["id"], "assistant", "x" * 300)
    store.add_message(chat["id"], "user", "a later question")

    assert store.list_chats()[0]["preview"] == "the opening question"


def test_get_missing_chat_returns_none(store):
    assert store.get_chat(4242) is None


def test_adding_a_message_to_a_missing_chat_is_a_no_op(store):
    """Silently dropping beats an IntegrityError inside a streaming response."""
    assert store.add_message(4242, "user", "q") == {}


def test_history_excludes_errors_and_truncates_long_answers(store):
    chat = store.create_chat()
    store.add_message(chat["id"], "user", "q")
    store.add_message(chat["id"], "assistant", "a" * 9000)
    store.add_message(chat["id"], "assistant", "I could not reach the model.")

    history = store.chat_history(chat["id"])
    assert all(m["role"] in ("user", "assistant") for m in history)
    assert len(history[1]["content"]) <= 1200


def test_history_returns_the_most_recent_turns_first_bound(store):
    chat = store.create_chat()
    for i in range(10):
        store.add_message(chat["id"], "user", f"q{i}")

    history = store.chat_history(chat["id"], limit=3)
    assert [h["content"] for h in history] == ["q7", "q8", "q9"]


def test_rename_and_delete(store):
    chat = store.create_chat()
    store.add_message(chat["id"], "user", "q")

    assert store.rename_chat(chat["id"], "  Belgium election  ") is True
    assert store.get_chat(chat["id"])["title"] == "Belgium election"

    assert store.delete_chat(chat["id"]) is True
    assert store.get_chat(chat["id"]) is None
    assert store.delete_chat(chat["id"]) is False


def test_delete_removes_the_messages_too(store):
    """Otherwise a re-used id would resurrect an orphaned transcript."""
    chat = store.create_chat()
    store.add_message(chat["id"], "user", "q")
    store.add_message(chat["id"], "assistant", "a")
    store.delete_chat(chat["id"])

    import sqlite3

    conn = sqlite3.connect(store.engine.url.database)
    orphans = conn.execute(
        "SELECT COUNT(*) FROM geonews_agent_messages WHERE session_id = ?", (chat["id"],)
    ).fetchone()[0]
    conn.close()
    assert orphans == 0


# -- endpoints ------------------------------------------------------------


class FakeSession:
    def close(self):
        pass


@pytest.fixture()
def client_env(monkeypatch, tmp_path):
    """Main module wired to a throwaway store and a fake SQLAlchemy session.

    Every endpoint test goes through this rather than the real butler.db, so a
    failing or re-run test cannot leave junk chats in the user's database.
    """
    from local_butler.apps.geonews import main as main_module

    store = MemoryStore(f"sqlite:///{tmp_path / 'api.db'}")
    monkeypatch.setattr(main_module, "get_store", lambda: store)
    monkeypatch.setattr(main_module, "SessionLocal", FakeSession)
    return main_module, store


def _call(client_env, method, path, json_body=None):
    import httpx
    from httpx import ASGITransport

    main_module, _ = client_env

    async def run():
        transport = ASGITransport(app=main_module.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.request(method, path, json=json_body)

    return asyncio.run(run())


def test_new_chat_endpoint_returns_an_empty_chat(client_env):
    response = _call(client_env, "POST", "/api/agent/chats")

    assert response.status_code == 200
    chat = response.json()["chat"]
    assert chat["id"] > 0
    assert chat["title"] == "New chat"


def test_list_endpoint_returns_previews(client_env):
    _, store = client_env
    chat = store.create_chat()
    store.add_message(chat["id"], "user", "what is going on with the EU?")

    response = _call(client_env, "GET", "/api/agent/chats")

    chats = response.json()["chats"]
    assert chats[0]["id"] == chat["id"]
    assert chats[0]["preview"] == "what is going on with the EU?"


def test_get_chat_endpoint_returns_the_transcript(client_env):
    _, store = client_env
    chat = store.create_chat()
    store.add_message(chat["id"], "user", "q")
    store.add_message(chat["id"], "assistant", "a")

    response = _call(client_env, "GET", f"/api/agent/chats/{chat['id']}")

    body = response.json()
    assert body["id"] == chat["id"]
    assert [m["content"] for m in body["messages"]] == ["q", "a"]


def test_get_missing_chat_is_404(client_env):
    assert _call(client_env, "GET", "/api/agent/chats/999").status_code == 404


def test_rename_endpoint(client_env):
    _, store = client_env
    chat = store.create_chat()

    response = _call(
        client_env, "PATCH", f"/api/agent/chats/{chat['id']}", {"title": "Chip controls"}
    )

    assert response.status_code == 200
    assert response.json()["chat"]["title"] == "Chip controls"
    assert _call(client_env, "PATCH", "/api/agent/chats/999", {"title": "x"}).status_code == 404


def test_delete_endpoint(client_env):
    _, store = client_env
    chat = store.create_chat()
    store.add_message(chat["id"], "user", "q")

    assert _call(client_env, "DELETE", f"/api/agent/chats/{chat['id']}").status_code == 200
    assert store.get_chat(chat["id"]) is None
    assert _call(client_env, "DELETE", f"/api/agent/chats/{chat['id']}").status_code == 404


def _sse_types(content):
    types = []
    for line in content.decode("utf-8").splitlines():
        line = line.strip()
        if line.startswith("data:"):
            types.append(json.loads(line[5:]))
    return types


def test_chat_persists_both_turns_and_announces_the_chat_id(client_env, monkeypatch):
    main_module, store = client_env

    class FakeAgent:
        def __init__(self, db, progress=None, history=None):
            self.history = history

        def run(self, message, article_id=None):
            return "the answer", {"tools": ["search_news"], "searches": ["q"]}

    monkeypatch.setattr(main_module, "GeonewsAgent", FakeAgent)

    response = _call(client_env, "POST", "/api/agent/chat", {"message": "hello there"})
    events = _sse_types(response.content)

    chat_events = [e for e in events if e["type"] == "chat"]
    assert chat_events, "the client must learn which chat the turn landed in"
    chat_id = chat_events[0]["session_id"]

    chat = store.get_chat(chat_id)
    assert [m["role"] for m in chat["messages"]] == ["user", "assistant"]
    assert chat["messages"][0]["content"] == "hello there"
    assert chat["messages"][1]["content"] == "the answer"
    assert chat["title"] == "hello there"
    # Only the interesting metadata is kept; raw search strings are noise.
    assert chat["messages"][1]["meta"] == {"tools": ["search_news"], "notes": [], "forced_search": False}


def test_chat_reuses_the_given_session_and_replays_its_history(client_env, monkeypatch):
    """This is the point of storing transcripts: the follow-up has referents."""
    main_module, store = client_env

    seen = []

    class FakeAgent:
        def __init__(self, db, progress=None, history=None):
            seen.append(history)

        def run(self, message, article_id=None):
            return "answer", {"tools": []}

    monkeypatch.setattr(main_module, "GeonewsAgent", FakeAgent)

    first = store.create_chat()
    store.add_message(first["id"], "user", "what about Belgium?")
    store.add_message(first["id"], "assistant", "the election went badly")

    _call(
        client_env,
        "POST",
        "/api/agent/chat",
        {"message": "and Germany?", "session_id": first["id"]},
    )

    assert seen[-1] == [
        {"role": "user", "content": "what about Belgium?"},
        {"role": "assistant", "content": "the election went badly"},
    ]
    assert len(store.get_chat(first["id"])["messages"]) == 4


def test_chat_starts_a_new_chat_when_the_session_id_is_stale(client_env, monkeypatch):
    """A chat deleted in another tab must not turn a question into a 404."""
    main_module, store = client_env

    class FakeAgent:
        def __init__(self, db, progress=None, history=None):
            pass

        def run(self, message, article_id=None):
            return "answer", {"tools": []}

    monkeypatch.setattr(main_module, "GeonewsAgent", FakeAgent)

    response = _call(
        client_env, "POST", "/api/agent/chat", {"message": "q", "session_id": 4242}
    )

    events = _sse_types(response.content)
    chat_id = next(e for e in events if e["type"] == "chat")["session_id"]
    assert chat_id != 4242
    assert store.get_chat(4242) is None


def test_deep_research_is_persisted_into_a_chat(client_env, monkeypatch):
    main_module, store = client_env

    class FakeResearcher:
        def __init__(self, db, progress=None):
            pass

        def research(self, question):
            return "THE REPORT", {"angles": ["a", "b"], "articles_read": 4, "sources": ["Wire"]}

    monkeypatch.setattr(main_module, "DeepResearcher", FakeResearcher)

    response = _call(
        client_env, "POST", "/api/agent/research", {"question": "explain the rules"}
    )
    events = _sse_types(response.content)
    chat_id = next(e for e in events if e["type"] == "chat")["session_id"]

    messages = store.get_chat(chat_id)["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert messages[1]["content"] == "THE REPORT"
    assert messages[1]["meta"]["deep_research"] is True
    assert messages[1]["meta"]["articles_read"] == 4
    assert store.get_chat(chat_id)["title"] == "explain the rules"


def test_research_joins_the_chat_the_user_was_already_in(client_env, monkeypatch):
    main_module, store = client_env

    class FakeResearcher:
        def __init__(self, db, progress=None):
            pass

        def research(self, question):
            return "report", {"angles": [], "articles_read": 0, "sources": []}

    monkeypatch.setattr(main_module, "DeepResearcher", FakeResearcher)

    existing = store.create_chat()
    store.add_message(existing["id"], "user", "quick question")

    _call(
        client_env,
        "POST",
        "/api/agent/research",
        {"question": "now go deep", "session_id": existing["id"]},
    )

    assert len(store.list_chats()) == 1
    assert len(store.get_chat(existing["id"])["messages"]) == 3


# -- agent replay ---------------------------------------------------------


def test_agent_includes_history_in_the_prompt():
    """A follow-up question is unanswerable without the earlier turn present."""
    from local_butler.apps.geonews.agent import GeonewsAgent

    captured = {}

    class FakeLLM:
        def complete_with_tools(self, messages, tools):
            captured["messages"] = messages
            return {"role": "assistant", "content": "Germany's coalition collapsed."}

        def complete(self, *a, **k):  # pragma: no cover
            return "unused"

    agent = GeonewsAgent(
        object(),
        llm=FakeLLM(),
        memory=None,
        history=[
            {"role": "user", "content": "what about Belgium?"},
            {"role": "assistant", "content": "the election went badly"},
        ],
    )
    answer, _ = agent.run("and Germany?")

    roles = [m.get("role") for m in captured["messages"]]
    assert "system" in roles
    assert roles[-1] == "user"
    assert captured["messages"][-1]["content"] == "and Germany?"
    joined = "\n".join(str(m.get("content", "")) for m in captured["messages"])
    assert "what about Belgium?" in joined
    assert "the election went badly" in joined
    assert answer == "Germany's coalition collapsed."


def test_agent_replays_an_honest_failure_answer():
    """A failed-search answer is real information and must survive the replay.

    An earlier version of this filtered any turn starting "I could not" on the
    assumption it was an error bubble. It was not: that is what the forced-search
    fallback says when it cannot reach a source, and dropping it would lose the
    one thing the user needs to know about that earlier answer.
    """
    from local_butler.apps.geonews.agent import GeonewsAgent

    captured = {}

    class FakeLLM:
        def complete_with_tools(self, messages, tools):
            captured["messages"] = messages
            return {"role": "assistant", "content": "ok"}

        def complete(self, *a, **k):  # pragma: no cover
            return "unused"

    agent = GeonewsAgent(
        object(),
        llm=FakeLLM(),
        memory=None,
        history=[
            {"role": "user", "content": "real question"},
            {
                "role": "assistant",
                "content": "I could not reach any news source just now.",
            },
        ],
    )
    agent.run("next question")

    joined = "\n".join(str(m.get("content", "")) for m in captured["messages"])
    assert "I could not reach any news source just now." in joined


def test_agent_without_history_sends_only_the_question():
    from local_butler.apps.geonews.agent import GeonewsAgent

    captured = {}

    class FakeLLM:
        def complete_with_tools(self, messages, tools):
            captured["messages"] = messages
            return {"role": "assistant", "content": "ok"}

        def complete(self, *a, **k):  # pragma: no cover
            return "unused"

    agent = GeonewsAgent(object(), llm=FakeLLM(), memory=None)
    agent.run("what happened?")

    assert len(captured["messages"]) == 2
    assert captured["messages"][0]["role"] == "system"
    assert captured["messages"][1]["content"] == "what happened?"


def test_agent_caps_how_much_history_it_replays():
    from local_butler.apps.geonews.agent import HISTORY_CHAR_BUDGET, GeonewsAgent

    captured = {}

    class FakeLLM:
        def complete_with_tools(self, messages, tools):
            captured["messages"] = messages
            return {"role": "assistant", "content": "ok"}

        def complete(self, *a, **k):  # pragma: no cover
            return "unused"

    agent = GeonewsAgent(
        object(),
        llm=FakeLLM(),
        memory=None,
        history=[{"role": "user", "content": "x" * 5000}],
    )
    agent.run("new question")

    replayed = captured["messages"][-2]
    assert len(replayed["content"]) <= HISTORY_CHAR_BUDGET