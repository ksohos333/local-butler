"""Tests for the Geonews local-model client.

These do not hit a real model: ``LocalLLM`` talks plain HTTP, so the transport
is stubbed and we assert on the payloads and error handling instead.

The regression this guards is real. Geonews used to reach the model through
local-brain and TrueForge, which spawns a *second* llama-server. With a model
already resident on this GPU, that second process died with
``cudaMalloc failed: out of memory`` and every chat request failed. Pointing at
the resident server is the fix, and ``/no_think`` is what stops Qwen3 from
spending its whole token budget on hidden thinking and returning nothing.
"""

from __future__ import annotations

import json

import pytest

from local_butler.apps.geonews import llm as llm_module
from local_butler.apps.geonews.llm import (
    AGENT_TOOLS,
    LLMUnavailable,
    LocalLLM,
    build_news_context,
)


@pytest.fixture
def resident_only(monkeypatch):
    """Pretend llama-server is up (Qwen3-14B) and Ollama is not."""

    def fake_get_json(url, timeout):
        if url.endswith("/v1/models"):
            return {"data": [{"id": "Qwen3-14B-Q4_K_M"}]}
        raise llm_module.urllib.error.URLError("connection refused")

    monkeypatch.setattr(llm_module, "_get_json", fake_get_json)
    return LocalLLM()


# -- discovery -------------------------------------------------------------


def test_prefers_llama_server_over_ollama(resident_only):
    assert resident_only.backend() == "llama-server"
    assert resident_only.first_model() == "Qwen3-14B-Q4_K_M"


def test_falls_back_to_ollama_when_llama_down(monkeypatch):
    def fake_get_json(url, timeout):
        if url.endswith("/api/tags"):
            return {"models": [{"name": "llama3.2:3b"}]}
        raise llm_module.urllib.error.URLError("connection refused")

    monkeypatch.setattr(llm_module, "_get_json", fake_get_json)
    client = LocalLLM()
    assert client.backend() == "ollama"
    assert client.ollama_models() == ["llama3.2:3b"]


def test_backend_is_none_when_nothing_is_listening(monkeypatch):
    def refuse(url, timeout):
        raise llm_module.urllib.error.URLError("connection refused")

    monkeypatch.setattr(llm_module, "_get_json", refuse)
    client = LocalLLM()
    assert client.backend() == "none"
    with pytest.raises(LLMUnavailable, match="No local model is reachable"):
        client.complete([{"role": "user", "content": "hi"}])


# -- /no_think -------------------------------------------------------------


def test_user_turn_gets_no_think_prefix(resident_only):
    messages = resident_only._with_no_think([{"role": "user", "content": "hello"}])
    assert messages[0]["content"].startswith("/no_think")


def test_no_think_prefix_is_not_doubled(resident_only):
    messages = resident_only._with_no_think(
        [{"role": "user", "content": "/no_think\nhello"}]
    )
    assert messages[0]["content"].count("/no_think") == 1


def test_system_turn_is_left_alone(resident_only):
    messages = resident_only._with_no_think(
        [{"role": "system", "content": "be terse"}, {"role": "user", "content": "hi"}]
    )
    assert messages[0]["content"] == "be terse"
    assert messages[1]["content"].startswith("/no_think")


# -- completions -----------------------------------------------------------


def test_complete_sends_expected_payload(monkeypatch):
    captured = {}

    def fake_post(url, body, timeout):
        captured["url"] = url
        captured["body"] = body
        return {"choices": [{"message": {"content": "Paris."}}]}

    monkeypatch.setattr(llm_module, "_get_json", lambda url, t: {"data": [{"id": "m"}]})
    monkeypatch.setattr(llm_module, "_post_json", fake_post)

    client = LocalLLM()
    assert client.complete([{"role": "user", "content": "capital?"}]) == "Paris."
    assert captured["url"].endswith("/v1/chat/completions")
    assert captured["body"]["stream"] is False
    assert captured["body"]["messages"][0]["content"].startswith("/no_think")


def test_reasoning_only_reply_is_not_lost(monkeypatch):
    """A thinking-only response must still produce text, not an error."""
    monkeypatch.setattr(llm_module, "_get_json", lambda url, t: {"data": [{"id": "m"}]})
    monkeypatch.setattr(
        llm_module,
        "_post_json",
        lambda url, body, t: {
            "choices": [{"message": {"content": "", "reasoning_content": "thinking out loud"}}]
        },
    )
    client = LocalLLM()
    assert client.complete([{"role": "user", "content": "hi"}]) == "thinking out loud"


def test_empty_completion_raises(monkeypatch):
    monkeypatch.setattr(llm_module, "_get_json", lambda url, t: {"data": [{"id": "m"}]})
    monkeypatch.setattr(
        llm_module, "_post_json", lambda url, body, t: {"choices": [{"message": {"content": ""}}]}
    )
    with pytest.raises(LLMUnavailable, match="empty content"):
        LocalLLM().complete([{"role": "user", "content": "hi"}])


def test_ollama_path_used_when_llama_absent(monkeypatch):
    def fake_get_json(url, timeout):
        if url.endswith("/api/tags"):
            return {"models": [{"name": "llama3.2:3b"}]}
        raise llm_module.urllib.error.URLError("connection refused")

    captured = {}
    monkeypatch.setattr(llm_module, "_get_json", fake_get_json)
    monkeypatch.setattr(
        llm_module,
        "_post_json",
        lambda url, body, t: captured.update(url=url) or {"response": "ok"},
    )
    client = LocalLLM()
    assert client.complete([{"role": "user", "content": "hi"}]) == "ok"
    assert captured["url"].endswith("/api/generate")


# -- article context -------------------------------------------------------


def test_context_includes_title_and_body():
    context = build_news_context("Headline", "Body text here")
    assert "Headline" in context
    assert "Body text here" in context


def test_context_truncates_long_bodies():
    context = build_news_context("T", "x" * 20000)
    assert len(context) < 9000


def test_context_includes_sources():
    context = build_news_context(
        "T", "B", sources=[{"name": "Anadolu", "url": "https://example.com"}]
    )
    assert "Anadolu" in context
    assert "https://example.com" in context

# -- tool calling ----------------------------------------------------------


def _tool_reply(name, arguments, call_id="call_1"):
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": name,
                                "arguments": arguments,
                            },
                        }
                    ],
                }
            }
        ]
    }


def test_complete_with_tools_parses_tool_call(monkeypatch):
    monkeypatch.setattr(llm_module, "_post_json", lambda *a, **k: _tool_reply(
        "search_news", '{"query": "lebanon"}'
    ))
    monkeypatch.setattr(LocalLLM, "first_model", lambda self: "Qwen3-14B-Q4_K_M")

    message = LocalLLM().complete_with_tools([{"role": "user", "content": "hi"}], [])

    call = message["tool_calls"][0]
    assert call["function"]["name"] == "search_news"
    # Arguments arrive as a JSON string, not a dict.
    assert json.loads(call["function"]["arguments"]) == {"query": "lebanon"}


def test_complete_with_tools_sends_tool_schemas(monkeypatch):
    captured = {}

    def fake_post(url, payload, timeout):
        captured.update(payload)
        return {"choices": [{"message": {"role": "assistant", "content": "hi"}}]}

    monkeypatch.setattr(llm_module, "_post_json", fake_post)
    monkeypatch.setattr(LocalLLM, "first_model", lambda self: "Qwen3-14B-Q4_K_M")

    LocalLLM().complete_with_tools([{"role": "user", "content": "hi"}], AGENT_TOOLS)

    names = [t["function"]["name"] for t in captured["tools"]]
    assert names == [t["function"]["name"] for t in AGENT_TOOLS]
    assert captured["tool_choice"] == "auto"
    assert captured["stream"] is False


def test_complete_with_tools_without_tools_still_works(monkeypatch):
    monkeypatch.setattr(llm_module, "_post_json", lambda *a, **k: {
        "choices": [{"message": {"role": "assistant", "content": "plain answer"}}]
    })
    monkeypatch.setattr(LocalLLM, "first_model", lambda self: "Qwen3-14B-Q4_K_M")

    message = LocalLLM().complete_with_tools([{"role": "user", "content": "hi"}], [])

    assert message["content"] == "plain answer"
    assert "tool_calls" not in message


def test_complete_with_tools_raises_when_no_model(monkeypatch):
    monkeypatch.setattr(LocalLLM, "first_model", lambda self: "")

    with pytest.raises(LLMUnavailable):
        LocalLLM().complete_with_tools([{"role": "user", "content": "hi"}], [])


def test_all_tool_schemas_are_well_formed():
    for tool in AGENT_TOOLS:
        spec = tool["function"]
        assert spec["name"].isidentifier()
        assert spec["description"]
        assert spec["parameters"]["type"] == "object"
        assert isinstance(spec["parameters"]["properties"], dict)


def test_tool_names_are_unique():
    names = [t["function"]["name"] for t in AGENT_TOOLS]
    assert len(names) == len(set(names))
