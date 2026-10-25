"""Local model client for Geonews.

Geonews used to reach the model two ways, both of which broke on this machine:

* ``/api/summarize`` talked to Ollama and pinned ``llama3.2:3b``.
* ``/api/agent/chat`` proxied to local-brain, which forwards to TrueForge,
  which starts its *own* llama-server for ``qwen3-8b-local``.

The second path is the problem. llama-server is already running on this box
holding ~11 GB of a 12 GB GPU, so when TrueForge tries to allocate a second
copy the process dies with ``cudaMalloc failed: out of memory``. The agent chat
therefore failed even though a perfectly good model was answering requests a few
milliseconds earlier.

So Geonews talks directly to the llama-server that is already resident, using
the same ``LocalLLM`` shape the calendar app uses. One model, one GPU slot.

If llama-server is not running we fall back to Ollama, which will happily run a
small model on CPU -- slow, but not broken.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Dict, Iterator, List, Optional

DEFAULT_LLM_URL = "http://127.0.0.1:8080"
DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"

# Qwen3 is a reasoning model. Without /no_think it burns the entire token budget
# on hidden thinking and returns empty content, which is what made summaries
# come back blank.
NO_THINK = "/no_think\n"


class LLMUnavailable(RuntimeError):
    """Raised when no local model backend can serve a request."""


def _get_json(url: str, timeout: float) -> Dict[str, Any]:
    request = urllib.request.Request(url)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _post_json(url: str, body: Dict[str, Any], timeout: float) -> Dict[str, Any]:
    payload = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(url, data=payload, method="POST")
    request.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _message_text(message: Dict[str, Any]) -> str:
    """Prefer content; fall back to reasoning so a reply is never silently lost."""
    text = (message.get("content") or "").strip()
    if not text and message.get("reasoning_content"):
        text = str(message["reasoning_content"]).strip()
    return text


class LocalLLM:
    """Client for the resident llama-server, with an Ollama fallback."""

    def __init__(
        self,
        base_url: str = DEFAULT_LLM_URL,
        ollama_url: str = DEFAULT_OLLAMA_URL,
        model: str = "",
        timeout: float = 120.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.ollama_url = ollama_url.rstrip("/")
        self.model = model
        self.timeout = timeout

    # -- discovery -------------------------------------------------------

    def llama_available(self) -> bool:
        try:
            _get_json(self.base_url + "/v1/models", 5)
            return True
        except (urllib.error.URLError, ValueError, OSError):
            return False

    def first_model(self) -> str:
        if self.model:
            return self.model
        try:
            data = _get_json(self.base_url + "/v1/models", 5)
            entries = data.get("data") or []
            if entries:
                return str(entries[0].get("id", ""))
        except (urllib.error.URLError, ValueError, OSError):
            pass
        return ""

    def ollama_available(self) -> bool:
        try:
            _get_json(self.ollama_url + "/api/tags", 5)
            return True
        except (urllib.error.URLError, ValueError, OSError):
            return False

    def ollama_models(self) -> List[str]:
        try:
            data = _get_json(self.ollama_url + "/api/tags", 5)
            return [str(m.get("name", "")) for m in data.get("models", []) if m.get("name")]
        except (urllib.error.URLError, ValueError, OSError):
            return []

    def backend(self) -> str:
        """Name the backend a request would use right now."""
        if self.llama_available():
            return "llama-server"
        if self.ollama_available():
            return "ollama"
        return "none"

    # -- tool calling ----------------------------------------------------

    def complete_with_tools(
        self,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        max_tokens: int = 1400,
        temperature: float = 0.4,
    ) -> Dict[str, Any]:
        """One completion with tools attached; returns the raw assistant message.

        Tool names are looked up by name at call time -- llama-server emits
        ``function.name`` and ``function.arguments`` as a JSON string, and the
        arguments have to be parsed before they can be dispatched.
        """
        model = self.first_model()
        if not model:
            raise LLMUnavailable("llama-server is serving no model")
        try:
            data = _post_json(
                self.base_url + "/v1/chat/completions",
                {
                    "model": model,
                    "messages": messages,
                    "tools": tools,
                    "tool_choice": "auto",
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                    "stream": False,
                },
                self.timeout,
            )
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:200]
            raise LLMUnavailable(f"llama-server returned {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise LLMUnavailable(f"cannot reach {self.base_url}: {exc.reason}") from exc

        choices = data.get("choices") or []
        if not choices:
            raise LLMUnavailable("llama-server returned no choices")
        return choices[0].get("message") or {}

    # -- completions -----------------------------------------------------

    def complete(
        self,
        messages: List[Dict[str, str]],
        max_tokens: int = 1024,
        temperature: float = 0.3,
    ) -> str:
        """One non-streaming completion, from llama-server or Ollama."""
        messages = self._with_no_think(messages)

        if self.llama_available():
            return self._llama_complete(messages, max_tokens, temperature)
        if self.ollama_available():
            return self._ollama_complete(messages, max_tokens, temperature)
        raise LLMUnavailable(
            "No local model is reachable. Start llama-server "
            "(systemctl --user start llama-server) or Ollama."
        )

    @staticmethod
    def _with_no_think(messages: List[Dict[str, str]]) -> List[Dict[str, str]]:
        """Prefix the user turn with /no_think so Qwen3 skips hidden thinking."""
        out: List[Dict[str, str]] = []
        for message in messages:
            content = message.get("content", "")
            if message.get("role") == "user" and not content.startswith(NO_THINK):
                content = NO_THINK + content
            out.append({"role": message["role"], "content": content})
        return out

    def _llama_complete(
        self,
        messages: List[Dict[str, str]],
        max_tokens: int,
        temperature: float,
    ) -> str:
        model = self.first_model()
        if not model:
            raise LLMUnavailable("llama-server is serving no model")
        try:
            data = _post_json(
                self.base_url + "/v1/chat/completions",
                {
                    "model": model,
                    "messages": messages,
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                    "stream": False,
                },
                self.timeout,
            )
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:200]
            raise LLMUnavailable(f"llama-server returned {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise LLMUnavailable(f"cannot reach {self.base_url}: {exc.reason}") from exc

        choices = data.get("choices") or []
        if not choices:
            raise LLMUnavailable("llama-server returned no choices")
        text = _message_text(choices[0].get("message") or {})
        if not text:
            raise LLMUnavailable("model returned empty content")
        return text

    def _ollama_complete(
        self,
        messages: List[Dict[str, str]],
        max_tokens: int,
        temperature: float,
    ) -> str:
        models = self.ollama_models()
        if not models:
            raise LLMUnavailable("Ollama has no models pulled")
        prompt = "\n\n".join(
            f"{m['role']}: {m['content']}" for m in messages
        )
        try:
            data = _post_json(
                self.ollama_url + "/api/generate",
                {
                    "model": models[0],
                    "prompt": prompt,
                    "stream": False,
                    "options": {"temperature": temperature, "num_predict": max_tokens},
                },
                self.timeout,
            )
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:200]
            raise LLMUnavailable(f"Ollama returned {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise LLMUnavailable(f"cannot reach {self.ollama_url}: {exc.reason}") from exc

        text = (data.get("response") or "").strip()
        if not text:
            raise LLMUnavailable("model returned empty content")
        return text

    def stream(
        self,
        messages: List[Dict[str, Any]],
        temperature: float = 0.4,
        max_tokens: int = 1400,
    ) -> Iterator[str]:
        """Yield text deltas from llama-server's streaming endpoint.

        Works with or without tool schemas attached; when tools are present the
        final ``finish_reason`` may be ``tool_calls``, in which case no content
        deltas arrive and the caller should fall back to the non-streaming
        tool-aware path.
        """
        model = self.first_model()
        if not model:
            raise LLMUnavailable("llama-server is serving no model")

        payload = json.dumps(
            {
                "model": model,
                "messages": self._with_no_think(messages),
                "temperature": temperature,
                "stream": True,
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + "/v1/chat/completions", data=payload, method="POST"
        )
        request.add_header("Content-Type", "application/json")

        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                for raw in response:
                    line = raw.decode("utf-8", errors="replace").strip()
                    if not line.startswith("data:"):
                        continue
                    chunk = line[5:].strip()
                    if not chunk or chunk == "[DONE]":
                        continue
                    try:
                        data = json.loads(chunk)
                    except ValueError:
                        continue
                    for choice in data.get("choices") or []:
                        delta = (choice.get("delta") or {}).get("content")
                        if delta:
                            yield delta
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:200]
            raise LLMUnavailable(f"llama-server returned {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise LLMUnavailable(f"cannot reach {self.base_url}: {exc.reason}") from exc


SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "search_news",
        "description": (
            "Search for current information: the Geonews article corpus, live news "
            "wires (UN, EU Commission, central banks, world wires), the open web and "
            "Wikipedia. Call this for anything about recent or real-world events, "
            "current figures, or background facts. Never answer from memory alone."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search terms. Use an empty string for the latest headlines.",
                }
            },
            "required": ["query"],
        },
    },
}

ARTICLE_TOOL = {
    "type": "function",
    "function": {
        "name": "read_article",
        "description": "Read the full text of one Geonews article by its numeric id.",
        "parameters": {
            "type": "object",
            "properties": {
                "article_id": {"type": "integer", "description": "Article id"}
            },
            "required": ["article_id"],
        },
    },
}

RECALL_TOOL = {
    "type": "function",
    "function": {
        "name": "recall_notes",
        "description": (
            "Look up previously saved notes on a topic. Use this for background the "
            "user asked you to remember, or context established earlier."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What to look up"}
            },
            "required": ["query"],
        },
    },
}

REMEMBER_TOOL = {
    "type": "function",
    "function": {
        "name": "remember",
        "description": (
            "Save a durable note the user asked you to keep: preferences, ongoing "
            "threads, ongoing stories worth tracking. Do not save transient chatter."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "note": {"type": "string", "description": "The fact to remember"},
                "topic": {
                    "type": "string",
                    "description": "Short topic label, e.g. 'migration-policy'",
                },
            },
            "required": ["note"],
        },
    },
}

RECENT_EVENTS_TOOL = {
    "type": "function",
    "function": {
        "name": "recent_events",
        "description": (
            "List what this agent already found earlier: past searches and answers "
            "from recent sessions. Use this to answer 'what has changed since "
            "last time' or to build a running thread on a story."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Optional topic filter; empty for everything",
                },
                "hours": {
                    "type": "integer",
                    "description": "How far back to look, in hours (default 168)",
                },
            },
            "required": [],
        },
    },
}

DEEP_RESEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "deep_research",
        "description": (
            "Run a full research pass on a question: break it into angles, search "
            "every angle across the corpus, live wires, web and Wikipedia, read the "
            "full articles, and write a cited report that names contradictions and "
            "gaps. Slow (often a minute) but far more thorough than search_news. "
            "Use it for 'what is really going on with X', background, history, "
            "comparisons, or when a quick search returned too little to answer."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "The question to research, in the user's own words",
                },
            },
            "required": ["question"],
        },
    },
}

AGENT_TOOLS = [
    SEARCH_TOOL,
    ARTICLE_TOOL,
    RECALL_TOOL,
    REMEMBER_TOOL,
    RECENT_EVENTS_TOOL,
    DEEP_RESEARCH_TOOL,
]


def build_news_context(
    title: str,
    body: str,
    summary: str = "",
    sources: Optional[List[Dict[str, str]]] = None,
) -> str:
    """Assemble the article text handed to the model, trimmed to a sane size."""
    parts = [f"Title: {title}"]
    if summary:
        parts.append(f"Existing summary: {summary}")
    if sources:
        lines = [f"- {s.get('name', '')}: {s.get('url', '')}" for s in sources if s]
        if lines:
            parts.append("Sources:\n" + "\n".join(lines))
    text = body or summary or title
    parts.append(f"Article text:\n{text[:8000]}")
    return "\n\n".join(parts)