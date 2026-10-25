"""The Geonews agent: a tool-calling loop over the local model.

The model this runs on (Qwen3-14B) has a training cutoff and no network. Left to
itself it answers "what happened this week?" with "I don't have access to
real-time news updates" -- true of the weights, useless to the user. The fix is
not a bigger model; it is giving it tools and requiring it to use them.

The loop is the standard shape: call the model, if it asks for tools run them and
call again, stop when it answers. A cap on rounds stops a confused model from
looping forever, and the guard rails below exist because a capable model will
happily invent citations if left alone:

* The system prompt states plainly that its knowledge has a cutoff and that
  current events require a tool call.
* If the model refuses *and* made no tool call, one retry forces the issue before
  giving up.
* If it still will not search, the fallback runs a search anyway and asks it to
  answer from those results. The user gets a sourced answer or an honest
  statement of what is unavailable, never a confident fabrication.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from typing import Any, Callable, Dict, List, Optional, Tuple

import logging

from sqlalchemy import text as sa_text
from sqlalchemy.orm import Session

from local_butler.apps.geonews import models
from local_butler.apps.geonews.llm import (
    AGENT_TOOLS,
    LLMUnavailable,
    LocalLLM,
    build_news_context,
)
from local_butler.apps.geonews.memory import MemoryStore, get_store
from local_butler.apps.geonews.websearch import search_everything

logger = logging.getLogger(__name__)

# The model reaches for these when it decides it cannot answer. Left in place
# they are exactly the failure this feature exists to remove.
# The model reaches for these as a "search query" when the user asked something
# broad. Searching for the literal phrase finds nothing useful, so the real
# question is substituted instead.
_GENERIC_QUERIES = {
    "current events", "current events?", "current news", "latest news",
    "today news", "today's news", "recent news", "news", "breaking news",
    "world news", "top stories", "headlines", "latest", "today",
}

_REFUSAL_MARKERS = (
    "real-time",
    "real time",
    "my knowledge",
    "training data",
    "knowledge cutoff",
    "cannot access",
    "can't access",
    "no access to",
    "up to date",
    "current events",
    "latest news",
    "don't have access",
    "do not have access",
    "as of my",
    "i'm unable to browse",
    "unable to browse",
)


def _is_refusal(text: str) -> bool:
    """True when the answer is the model disclaiming its own currency."""
    lowered = (text or "").lower()
    if not lowered:
        return False
    # Only judge short answers. A long reply that happens to mention "current
    # events" while actually synthesising is fine.
    if len(lowered) > 400:
        return False
    return any(marker in lowered for marker in _REFUSAL_MARKERS)


def _memory_or_none() -> Optional[MemoryStore]:
    """Shared memory store, or ``None`` if the database is unreachable."""
    try:
        return get_store()
    except Exception as exc:  # noqa: BLE001 - memory is optional
        logger.warning("Agent memory unavailable: %s", exc)
        return None

MAX_TOOL_ROUNDS = 4
# How much of the chat to replay. A deep-research answer runs to several thousand
# characters, so replaying every turn in full would crowd out the actual question.
HISTORY_TURNS = 6
HISTORY_CHAR_BUDGET = 1200

SYSTEM_PROMPT = """You are the assistant inside Geonews, a local news reader.

Your training data has a cutoff, so you have no built-in knowledge of anything \
that happened recently. You do, however, have tools: `search_news` for current \
events and facts, `read_article` for a full article, `recall_notes` for \
background saved earlier, and `remember` to save something worth keeping. You \
also have `deep_research` for a full multi-angle investigation when one search \
is not enough.

Rules:
- Any question about current events, recent figures, or today's news MUST start \
with a `search_news` call. Do not answer those from memory.
- Pass the user's own words as the query. Do not search for a placeholder like \
"current events"; that matches nothing.
- If the user asks what you remember, know about them, or are tracking, call \
`recall_notes` or `recent_events` and answer from what they return. Do not ask \
them to repeat something you were told to keep.
- Use `deep_research` when a question needs background, history, comparison, or \
several linked stories. It takes about a minute; say that it is working rather \
than answering from `search_news` alone.
- Ground every factual claim in tool output. If the tools return nothing, say \
what you could not find instead of filling the gap.
- Synthesise. When several sources cover one story, combine them into a clear \
picture rather than listing them separately, and note where they disagree.
- Never invent headlines, numbers, dates or quotes. There is no penalty for \
saying you could not verify something.
- Earlier turns of this conversation may be included for context. Resolve \
references like "it" or "what about Germany?" against them, but never treat a \
past answer as current fact: news moves on, so re-search for anything current.
- Be concise: a few sentences unless the user asks for depth.
- Answer in the user's language."""


class ToolResult:
    """One executed tool call, shaped for the OpenAI tool-message protocol."""

    def __init__(self, tool_call_id: str, name: str, content: str, ok: bool = True) -> None:
        self.tool_call_id = tool_call_id
        self.name = name
        self.content = content
        self.ok = ok

    def as_message(self) -> Dict[str, Any]:
        return {
            "role": "tool",
            "tool_call_id": self.tool_call_id,
            "content": self.content[:6000],
        }


class GeonewsAgent:
    """Runs the tool-calling loop. One instance per chat request."""

    def __init__(self, db: Session, llm: Optional[LocalLLM] = None,
                 memory: Optional[MemoryStore] = None,
                 progress: Optional[Callable[[str], None]] = None,
                 history: Optional[List[Dict[str, str]]] = None) -> None:
        self.db = db
        self.llm = llm or LocalLLM()
        # Called with human-readable stage names, e.g. "Reading 4 articles".
        # The chat endpoint streams these so a minute-long research pass does not
        # look like a hang.
        self.progress = progress
        # Earlier turns of the chat this request belongs to, oldest first. Makes
        # follow-up questions ("what about last week?") answerable.
        self.history = history or []
        # Default to the shared store: `remember` has to work for a plain chat
        # request, not only when a caller injects one.
        self.memory = memory if memory is not None else _memory_or_none()
        self.searches: List[str] = []
        self.tools_used: List[str] = []
        self.notes_written: List[Dict[str, Any]] = []
        self.research_reports: List[Dict[str, Any]] = []
        self.original_question: str = ""

    # -- tool implementations -------------------------------------------

    def _search_corpus(self, query: str, limit: int = 8) -> List[Dict[str, str]]:
        """Keyword search the ingested corpus, newest first.

        Uses LIKE across title/summary/body with per-term scoring: the corpus is
        a few thousand rows, so a full-text index is not worth the write cost.
        """
        terms = [t for t in re.findall(r"[A-Za-z0-9]{3,}", (query or "").lower())]
        if not terms:
            return []
        patterns = [f"%{t}%" for t in terms[:8]]
        # Named parameters: this SQLAlchemy version rejects positional lists on
        # text() execution.
        clause = " OR ".join(
            "(lower(title) LIKE :t{i} OR lower(summary) LIKE :t{i} OR lower(body) LIKE :t{i})".format(
                i=index
            )
            for index in range(len(patterns))
        )
        params = {f"t{index}": pattern for index, pattern in enumerate(patterns)}

        # Raw SQL keeps the row shape predictable and avoids mapping thousands of
        # ORM objects we only score once.
        sql = f"""
            SELECT id, title, summary, body, source_name, url, published_at
            FROM geonews_articles
            WHERE {clause}
            ORDER BY published_at DESC
            LIMIT 400
        """
        try:
            result = self.db.execute(sa_text(sql), params)
        except Exception as exc:  # noqa: BLE001 - search must never break chat
            logger.warning("Corpus search failed: %s", exc)
            return []

        scored: List[Tuple[int, float, Dict[str, str]]] = []
        for row in result.fetchall():
            title = (row.title or "").lower()
            summary = (row.summary or "").lower()
            body = (row.body or "").lower()
            score = 0
            for term in terms:
                if term in title:
                    score += 3
                elif term in summary:
                    score += 2
                elif term in body:
                    score += 1
            if score <= 0:
                continue
            published = row.published_at
            # Raw SQLite returns ISO text, not datetime; parse for sorting.
            if isinstance(published, str):
                try:
                    published = dt.datetime.fromisoformat(published)
                except ValueError:
                    published = None
            epoch = published.timestamp() if published else 0
            scored.append((score, epoch, {
                "title": row.title or "",
                "url": row.url or "",
                "domain": row.source_name or "",
                "published": published.isoformat() if published else (row.published_at or ""),
                "snippet": (row.summary or row.body or "")[:400],
            }))

        # Strongest match first, recency breaking ties.
        scored.sort(key=lambda item: (-item[0], -item[1]))
        return [item[2] for item in scored[:limit]]

    def _effective_query(self, query: str) -> str:
        """Substitute the user's question when the model searches a placeholder.

        Asking "what are the biggest stories?" often produces a ``search_news``
        call for "current events", which matches nothing in any feed. The user's
        own words are the better query.
        """
        cleaned = (query or "").strip().rstrip("?.").lower()
        if not cleaned:
            return self.original_question
        if cleaned in _GENERIC_QUERIES:
            return self.original_question or query
        return query

    def _tool_search_news(self, arguments: Dict[str, Any]) -> str:
        query = self._effective_query(str(arguments.get("query", "")))
        if not query:
            return "No search query was given."
        self.searches.append(query)

        # search_everything keeps every backend's failure visible rather than
        # dropping a source: "blocked" and "no news" must not look alike.
        sections = search_everything(query, corpus=self._search_corpus(query), limit=8)

        if self.memory:
            notes = self.memory.search_notes(query, limit=5)
            if notes:
                sections.append((
                    "Saved notes",
                    "\n".join(f"- [{n['topic'] or 'general'}] {n['note']}" for n in notes),
                ))

        today = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
        body = "\n\n".join(text for _, text in sections)
        return f"Search results for {query!r} (today is {today}).\n\n{body}"

    def _tool_recent_events(self, arguments: Dict[str, Any]) -> str:
        """What the agent has already learned, for "what changed since last time?"."""
        if not self.memory:
            return "Memory is unavailable."
        query = self._effective_query(str(arguments.get("query", "")))
        hours = arguments.get("hours")
        try:
            hours = int(hours) if hours else 168
        except (TypeError, ValueError):
            hours = 168
        events = self.memory.recent_events(query, hours=hours, limit=15)
        if not events:
            return "Nothing recorded in that window yet."
        return "\n".join(f"- {e['created_at']}: {e['summary']}" for e in events)

    def _tool_read_article(self, arguments: Dict[str, Any]) -> str:
        try:
            article_id = int(arguments.get("article_id"))
        except (TypeError, ValueError):
            return "No valid article_id was given."
        article = self.db.query(models.Article).filter(models.Article.id == article_id).first()
        if article is None:
            return f"No article with id {article_id}."
        return build_news_context(
            title=article.title,
            body=article.body or "",
            summary=article.summary or "",
            sources=[{"name": article.source_name, "url": article.url or ""}],
        )

    def _tool_deep_research(self, arguments: Dict[str, Any]) -> str:
        """Delegate a full research pass. The report comes back as tool output."""
        from local_butler.apps.geonews.deep_research import DeepResearcher

        question = str(arguments.get("question") or "").strip()
        if not question:
            question = self.original_question
        if not question:
            return "No research question was given."

        # Imported lazily: the chat path does not need the planner, and this
        # keeps the module boundary between chat and research one-directional.
        progress = getattr(self, "progress", None)
        researcher = DeepResearcher(self.db, self.llm, progress=progress)
        report, meta = researcher.research(question)

        self.research_reports.append(meta)
        # The angles each got their own search, so they belong in `searches`.
        # `tools_used` is already recorded by run_tool for every attempt.
        self.searches.extend(meta.get("angles", []))
        return report[:9000]

    def _tool_recall_notes(self, arguments: Dict[str, Any]) -> str:
        if not self.memory:
            return "Memory is unavailable."
        query = str(arguments.get("query", "")).strip()
        notes = self.memory.search_notes(query, limit=8)
        if not notes:
            return "Nothing saved matches that."
        return "\n".join(
            f"- [{n['topic'] or 'general'}] {n['note']}" for n in notes
        )

    def _tool_remember(self, arguments: Dict[str, Any]) -> str:
        if not self.memory:
            return "Memory is unavailable."
        note = str(arguments.get("note", "")).strip()
        topic = str(arguments.get("topic", "")).strip()
        if not note:
            return "Nothing to remember."
        saved = self.memory.add_note(note, topic)
        self.notes_written.append(saved)
        return f"Saved under topic {saved['topic'] or 'general'}."

    def run_tool(self, call: Dict[str, Any]) -> ToolResult:
        """Execute one requested tool call by name."""
        function = call.get("function") or {}
        name = function.get("name", "")
        raw_args = function.get("arguments") or "{}"
        if isinstance(raw_args, str):
            try:
                arguments = json.loads(raw_args)
            except ValueError:
                arguments = {}
        else:
            arguments = raw_args

        handlers = {
            "search_news": self._tool_search_news,
            "read_article": self._tool_read_article,
            "recall_notes": self._tool_recall_notes,
            "remember": self._tool_remember,
            "recent_events": self._tool_recent_events,
            "deep_research": self._tool_deep_research,
        }
        # Record the attempt either way: provenance should show what the model
        # tried, not only what succeeded.
        self.tools_used.append(name)

        handler = handlers.get(name)
        if handler is None:
            return ToolResult(
                call.get("id", ""), name, f"Unknown tool {name}.", ok=False
            )

        try:
            return ToolResult(call.get("id", ""), name, handler(arguments))
        except LLMUnavailable as exc:
            return ToolResult(call.get("id", ""), name, str(exc), ok=False)
        except Exception as exc:  # noqa: BLE001 - a bad tool must not kill the chat
            logger.warning("Tool %s failed: %s", name, exc)
            return ToolResult(call.get("id", ""), name, f"{name} failed: {exc}", ok=False)

    # -- main loop -------------------------------------------------------

    def _base_messages(self, question: str, article_id: Optional[int]) -> List[Dict[str, Any]]:
        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT}
        ]

        if article_id is not None:
            article = (
                self.db.query(models.Article)
                .filter(models.Article.id == article_id)
                .first()
            )
            if article:
                messages.append({
                    "role": "user",
                    "content": (
                        "I am reading this article in the app:\n\n"
                        + build_news_context(
                            title=article.title,
                            body=article.body or "",
                            summary=article.summary or "",
                            sources=[{"name": article.source_name, "url": article.url or ""}],
                        )
                        + "\n\nAnswer my question about it, and search if it needs "
                        "wider context."
                    ),
                })

        messages.extend(self._history_messages())
        messages.append({"role": "user", "content": question})
        return messages

    def _history_messages(self) -> List[Dict[str, Any]]:
        """Earlier turns in this chat, so a follow-up question has referents.

        Without this, "and what about Germany?" after a question about migration
        policy is unanswerable: the model sees one orphan turn and has to guess
        what "it" is. Older turns are truncated hard -- a deep-research answer can
        be thousands of characters, and feeding all of them back would crowd out
        the question and blow the context window.
        """
        if not self.history:
            return []
        messages: List[Dict[str, Any]] = []
        for turn in self.history[-HISTORY_TURNS:]:
            role = turn.get("role")
            content = (turn.get("content") or "").strip()
            # Only real conversation. Errors are never persisted, so nothing needs
            # filtering here -- and guessing from message text would wrongly drop
            # honest answers that happen to start "I could not".
            if role not in ("user", "assistant") or not content:
                continue
            messages.append({
                "role": role,
                "content": content[:HISTORY_CHAR_BUDGET],
            })
        if messages:
            messages.insert(0, {
                "role": "user",
                "content": (
                    "Earlier turns from this same conversation, for context:\n"
                    "(If any of it is stale, trust the news you search for now.)"
                ),
            })
        return messages

    def _final_text(self, message: Dict[str, Any]) -> str:
        text = (message.get("content") or "").strip()
        if not text and message.get("reasoning_content"):
            text = str(message["reasoning_content"]).strip()
        return text

    def run(
        self, question: str, article_id: Optional[int] = None
    ) -> Tuple[str, Dict[str, Any]]:
        """Answer ``question``, returning (text, metadata about what happened)."""
        self.original_question = (question or "").strip()
        messages = self._base_messages(question, article_id)
        meta: Dict[str, Any] = {"searches": [], "tools": [], "notes": []}

        answer = ""
        for round_index in range(MAX_TOOL_ROUNDS):
            message = self.llm.complete_with_tools(messages, AGENT_TOOLS)
            calls = message.get("tool_calls") or []

            if not calls:
                answer = self._final_text(message)
                if not answer:
                    # Empty completion: nudge once, then give up.
                    if round_index == 0:
                        messages.append(
                            {"role": "user", "content": "Please answer now."}
                        )
                        continue
                    break
                if _is_refusal(answer):
                    # The model claims it cannot know. That is true of its
                    # weights and irrelevant to the user, so keep looping until
                    # the round cap forces the fallback below.
                    answer = ""
                    continue
                break

            messages.append({
                "role": "assistant",
                "content": message.get("content") or "",
                "tool_calls": calls,
            })
            for call in calls:
                result = self.run_tool(call)
                messages.append(result.as_message())

        meta["searches"] = list(self.searches)
        meta["tools"] = list(self.tools_used)
        meta["notes"] = list(self.notes_written)
        meta["research"] = list(self.research_reports)

        if not answer:
            answer = self._forced_search_answer(question, messages)
            meta["forced_search"] = True
            meta["searches"] = list(self.searches)
            meta["tools"] = list(self.tools_used)

        self._log_event(question, answer, meta)
        return answer, meta

    def _forced_search_answer(self, question: str, messages: List[Dict[str, Any]]) -> str:
        """Last resort: search anyway, then answer from whatever came back.

        The model claimed it had nothing. Rather than forwarding that to the
        user, do the search ourselves and give it one explicit instruction.
        """
        try:
            results = self._tool_search_news({"query": question})
        except Exception as exc:  # noqa: BLE001
            logger.warning("Forced search failed: %s", exc)
            return (
                "I could not reach any news source just now, so I can't tell you "
                "about current events. Try again in a moment."
            )

        self.searches.append(question)
        messages.append(
            {"role": "user", "content": results},
        )
        messages.append(
            {
                "role": "user",
                "content": (
                    "Answer the original question using the results above. "
                    "Cite the sources you used. If they genuinely do not cover it, "
                    "say exactly what is missing."
                ),
            }
        )
        try:
            message = self.llm.complete(messages, max_tokens=1400, temperature=0.3)
            return message or "I could not produce an answer from the sources I found."
        except LLMUnavailable as exc:
            return f"I could not complete the search: {exc}"

    def _log_event(self, question: str, answer: str, meta: Dict[str, Any]) -> None:
        """Record what was learned, so the agent has a timeline to synthesise."""
        if not self.memory:
            return
        topic = (self.searches[0] if self.searches else question)[:128]
        try:
            self.memory.add_event(
                summary=f"Q: {question[:200]} | A: {answer[:400]}",
                topic=topic,
                kind="chat",
                detail=json.dumps(meta.get("tools", [])),
            )
        except Exception as exc:  # noqa: BLE001 - memory is best-effort
            logger.warning("Could not log agent event: %s", exc)