"""Deep research: decompose a question, investigate every angle, then synthesise.

The chat agent in :mod:`agent` answers a question in a handful of tool calls.
That is the right shape for "what happened today?". It is the wrong shape for
"what is actually going on with X?", where the interesting answer lives in the
part of the story that no single search returns.

Deep research is four stages, and each one exists because skipping it produces a
specific failure:

1. **Plan.** The model breaks the question into independent angles. Searching
   once for the whole question returns the most-covered angle and nothing else,
   so a report written from it silently inherits the news cycle's blind spots.
2. **Gather.** Every angle is searched independently, and the full bodies of the
   strongest articles are pulled in. Headlines alone leave the synthesis guessing
   at causation and numbers.
3. **Check.** Contradictions between sources are surfaced explicitly. Two outlets
   disagreeing is information; averaging them into one smooth paragraph throws
   that away.
4. **Synthesise.** One call over all the evidence writes the report, and is told
   what it could *not* find so the gaps become part of the answer.

Concurrency note: corpus search runs on the calling thread because SQLAlchemy
sessions are not thread-safe and SQLite would serialise anyway. Only the network
backends are fanned out, which is where the latency actually is.
"""

from __future__ import annotations

import concurrent.futures
import json
import logging
import re
from typing import Any, Callable, Dict, List, Optional, Tuple

from sqlalchemy import text as sa_text
from sqlalchemy.orm import Session

from local_butler.apps.geonews import models
from local_butler.apps.geonews.llm import LocalLLM, build_news_context
from local_butler.apps.geonews.websearch import format_results, search_everything

logger = logging.getLogger(__name__)

# Bounds. Deep research is slow on a 14B model on one GPU, so each stage has a
# ceiling: more angles stops adding coverage and starts adding latency, and more
# articles blow the context window before they add evidence.
MAX_ANGLES = 5
MAX_ARTICLES_READ = 6
MAX_NETWORK_WORKERS = 4

PLAN_PROMPT = """Break a research question into independent angles worth \
searching separately.

Rules:
- Each angle must be a short search query a news site would actually match.
- Angles must be independent of each other, not restatements of one question.
- Cover different facets: what happened, who is involved, the numbers, the \
history or precedent, and the competing view or criticism.
- Return 3 to {max_angles} angles, most important first.
- Return JSON only: {{"angles": ["...", "..."]}}
- No commentary before or after the JSON.

Question: {question}"""

SYNTHESIS_PROMPT = """You are writing a research report from gathered evidence.

Rules:
- Every factual claim must be supported by the evidence below. If the evidence \
does not support it, leave it out.
- Where sources disagree, say so and give both positions. Do not average them.
- Distinguish what is confirmed from what is only claimed by one source.
- Only use evidence that bears on the question. Search angles deliberately \
return loosely related items; an article about an unrelated directive is not \
context for a migration question, so leave it out rather than padding a section \
with it.
- Finish with a section "What I could not verify" listing what the evidence did \
not cover. An honest gap beats a confident fill.
- Use Markdown. Cite the source inline as (source name).
- Be thorough but not padded. No filler transitions.

Question: {question}

Evidence:
{evidence}"""


def extract_json_object(text: str) -> Dict[str, Any]:
    """Pull the first JSON object out of a model reply.

    Qwen3 wraps JSON in prose or fences often enough that a bare ``json.loads``
    on the response fails more than it succeeds. Fall back to the last resort of
    treating the whole reply as a list of angles.
    """
    candidate = (text or "").strip()
    if candidate.startswith("```"):
        candidate = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", candidate)

    match = re.search(r"\{.*\}", candidate, re.DOTALL)
    if match:
        try:
            parsed = json.loads(match.group(0))
            if isinstance(parsed, dict):
                return parsed
        except ValueError:
            pass

    array = re.search(r"\[.*\]", candidate, re.DOTALL)
    if array:
        try:
            parsed = json.loads(array.group(0))
            if isinstance(parsed, list):
                return {"angles": parsed}
        except ValueError:
            pass
    return {}


def _dedupe(values: List[str]) -> List[str]:
    """Order-preserving dedupe, ignoring case."""
    seen = set()
    out: List[str] = []
    for value in values:
        cleaned = (value or "").strip()
        if not cleaned:
            continue
        key = cleaned.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(cleaned)
    return out


class DeepResearcher:
    """Runs one deep-research pass. One instance per request."""

    def __init__(
        self,
        db: Session,
        llm: Optional[LocalLLM] = None,
        progress: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.db = db
        self.llm = llm or LocalLLM()
        self._progress = progress or (lambda message: None)
        self.angles: List[str] = []
        self.sections: List[Tuple[str, str]] = []
        self.articles_read: List[Dict[str, str]] = []

    # -- stage 1: plan ---------------------------------------------------

    def _fallback_angles(self, question: str) -> List[str]:
        """Angles derived mechanically, used when the model will not plan.

        Better than a single search: the facets below are the ones that get
        asked about any news story, so even a planner-free run covers more than
        the chat agent would.
        """
        terms = re.findall(r"[A-Za-z0-9]{3,}", question)
        subject = " ".join(terms[:6]) if terms else question
        return [
            question,
            f"{subject} latest developments",
            f"{subject} criticism OR opposition",
            f"{subject} numbers OR figures",
        ][:MAX_ANGLES]

    def plan(self, question: str) -> List[str]:
        """Decompose the question into search angles."""
        self._progress("Planning research angles")
        try:
            reply = self.llm.complete(
                [
                    {
                        "role": "user",
                        "content": PLAN_PROMPT.format(
                            max_angles=MAX_ANGLES, question=question
                        ),
                    }
                ],
                max_tokens=400,
                temperature=0.2,
            )
        except Exception as exc:  # noqa: BLE001 - a failed planner is not a failure
            # Planning is an optimisation, not the work. Any model trouble falls
            # back to the mechanical angles rather than aborting the research.
            logger.warning("Deep research planning failed: %s", exc)
            self.angles = self._fallback_angles(question)
            return self.angles

        angles = extract_json_object(reply).get("angles") or []
        if isinstance(angles, str):
            angles = [angles]
        angles = _dedupe([str(a) for a in angles if isinstance(a, (str, int))])

        # The planner sometimes returns the whole question as one angle; add the
        # mechanical facets anyway so there is more than a single search.
        if len(angles) < 2:
            angles = _dedupe(angles + self._fallback_angles(question))
        self.angles = angles[:MAX_ANGLES]
        return self.angles

    # -- stage 2: gather -------------------------------------------------

    def _search_corpus(self, query: str, limit: int = 6) -> List[Dict[str, str]]:
        """Same keyword scoring the chat agent uses, so both agree on ranking."""
        terms = re.findall(r"[A-Za-z0-9]{3,}", (query or "").lower())
        if not terms:
            return []
        patterns = [f"%{term}%" for term in terms[:8]]
        clause = " OR ".join(
            "(lower(title) LIKE :t{i} OR lower(summary) LIKE :t{i} "
            "OR lower(body) LIKE :t{i})".format(i=index)
            for index in range(len(patterns))
        )
        params = {f"t{index}": pattern for index, pattern in enumerate(patterns)}
        sql = f"""
            SELECT id, title, summary, body, source_name, url, published_at
            FROM geonews_articles
            WHERE {clause}
            ORDER BY published_at DESC
            LIMIT 400
        """
        try:
            rows = self.db.execute(sa_text(sql), params).fetchall()
        except Exception as exc:  # noqa: BLE001 - one dead backend is not fatal
            logger.warning("Deep research corpus search failed: %s", exc)
            return []

        scored = []
        for row in rows:
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
            scored.append((score, {
                "id": str(row.id),
                "title": row.title or "",
                "url": row.url or "",
                "domain": row.source_name or "",
                "published": row.published_at or "",
                "snippet": (row.summary or row.body or "")[:400],
            }))
        scored.sort(key=lambda item: -item[0])
        return [item[1] for item in scored[:limit]]

    def _network_sections(self, angle: str) -> List[Tuple[str, str]]:
        """Search the network backends for one angle.

        No corpus here on purpose: the session is not thread-safe, so the corpus
        half of each angle is searched serially in :meth:`gather`.
        """
        return search_everything(angle, corpus=None, limit=8)

    def gather(self, angles: List[str]) -> None:
        """Search every angle, in parallel for the network half."""
        self._progress(f"Searching {len(angles)} angles")

        for angle in angles:
            # Serial: touching the SQLAlchemy session from a worker thread.
            corpus_text = self._corpus_text(self._search_corpus(angle))
            if corpus_text:
                self.sections.append((f"Geonews corpus — {angle}", corpus_text))

        with concurrent.futures.ThreadPoolExecutor(
            max_workers=MAX_NETWORK_WORKERS
        ) as pool:
            futures = {
                pool.submit(self._network_sections, angle): angle for angle in angles
            }
            for future in concurrent.futures.as_completed(futures):
                angle = futures[future]
                try:
                    for label, text in future.result():
                        self.sections.append((f"{label} — {angle}", text))
                except Exception as exc:  # noqa: BLE001 - keep the other angles
                    logger.warning("Angle %r network search failed: %s", angle, exc)
                    self.sections.append((f"Search — {angle}", f"failed: {exc}"))

    @staticmethod
    def _corpus_text(results: List[Dict[str, str]]) -> str:
        if not results:
            return ""
        return format_results("Geonews corpus", results)

    # -- stage 2b: read the bodies ---------------------------------------

    def read_articles(self) -> None:
        """Pull full text for the strongest corpus articles.

        This is what separates deep research from a headline roundup: the
        synthesis needs bodies to attribute a number or a motive.
        """
        seen: set = set()
        candidates: List[Tuple[int, Dict[str, str]]] = []
        for score, row in self._ranked_corpus_rows():
            if row["id"] in seen:
                continue
            seen.add(row["id"])
            candidates.append((score, row))

        if not candidates:
            return

        self._progress(f"Reading {min(len(candidates), MAX_ARTICLES_READ)} articles")
        for _, row in candidates[:MAX_ARTICLES_READ]:
            article = (
                self.db.query(models.Article)
                .filter(models.Article.id == int(row["id"]))
                .first()
            )
            if article is None:
                continue
            self.articles_read.append({
                "id": row["id"],
                "title": article.title or "",
                "source": article.source_name or "unknown",
                "url": article.url or "",
                "context": build_news_context(
                    title=article.title,
                    body=article.body or "",
                    summary=article.summary or "",
                    sources=[{
                        "name": article.source_name,
                        "url": article.url or "",
                    }],
                ),
            })

    def _ranked_corpus_rows(self) -> List[Tuple[int, Dict[str, str]]]:
        """Re-score stored corpus hits by cross-angle agreement.

        An article that shows up for several independent angles is far more likely
        to be central to the question than one that matched a single angle, and
        that is the strongest available signal for what to read in full.
        """
        counts: Dict[str, int] = {}
        rows: Dict[str, Dict[str, str]] = {}
        for angle in self.angles:
            for hit in self._search_corpus(angle, limit=6):
                counts[hit["id"]] = counts.get(hit["id"], 0) + 1
                rows.setdefault(hit["id"], hit)
        return sorted(
            ((count, rows[article_id]) for article_id, count in counts.items()),
            key=lambda item: -item[0],
        )

    # -- stage 4: synthesise ---------------------------------------------

    def _evidence(self) -> str:
        parts: List[str] = []
        for article in self.articles_read:
            parts.append(
                f"[Article {article['id']} | {article['source']}]\n"
                f"{article['context']}"
            )
        for label, text in self.sections:
            parts.append(f"[{label}]\n{text}")
        body = "\n\n".join(parts)
        # The synthesis has to fit the context window; truncation is stated in the
        # prompt rather than hidden, so the model knows evidence was cut.
        limit = 24000
        if len(body) > limit:
            body = body[:limit] + "\n\n[Evidence truncated: earlier sections were cut for length.]"
        return body or "[No evidence was gathered from any source.]"

    def synthesize(self, question: str) -> str:
        """Write the report from the gathered evidence."""
        self._progress("Writing report")
        try:
            report = self.llm.complete(
                [
                    {
                        "role": "user",
                        "content": SYNTHESIS_PROMPT.format(
                            question=question, evidence=self._evidence()
                        ),
                    }
                ],
                max_tokens=2200,
                temperature=0.3,
            )
        except Exception as exc:  # noqa: BLE001 - report what broke, lose nothing
            # The evidence was gathered and is worth keeping: hand it back with
            # the failure stated, instead of discarding a minute of searching.
            logger.warning("Deep research synthesis failed: %s", exc)
            return (
                "I gathered sources but could not reach the local model to write "
                f"the report: {exc}\n\nHere is the raw evidence instead:\n\n"
                f"{self._evidence()}"
            )
        return report or "I gathered sources but could not write a report from them."

    # -- entry point -----------------------------------------------------

    def research(self, question: str) -> Tuple[str, Dict[str, Any]]:
        """Run every stage, returning (report, metadata)."""
        question = (question or "").strip()
        if not question:
            return "Ask a question and I will research it.", {
                "angles": [], "sources": 0, "articles_read": 0
            }

        angles = self.plan(question)
        self.gather(angles)
        self.read_articles()
        report = self.synthesize(question)

        # Section count is the honest measure of what a report is built on; a
        # report over three sections should not look like one over forty.
        meta = {
            "angles": list(self.angles),
            "sections": len(self.sections),
            "articles_read": len(self.articles_read),
            "sources": _unique_sources(self.sections, self.articles_read),
        }
        self._progress("Done")
        return report, meta


def _unique_sources(
    sections: List[Tuple[str, str]], articles: List[Dict[str, str]]
) -> List[str]:
    """Outlets that actually returned something.

    A backend that was rate-limited is not a source, and listing it alongside a
    real citation would let a thin report look well-sourced. Error sections are
    detected by the markers :func:`search_everything` writes on failure.
    """
    names: set = set()
    for article in articles:
        if article.get("source"):
            names.add(article["source"])
    for label, text in sections:
        lowered = text.lower()
        if "unavailable" in lowered or lowered.startswith("failed:"):
            continue
        names.add(label.split("—")[0].strip())
    return sorted(names)