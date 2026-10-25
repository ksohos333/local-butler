"""Persistent memory for the Geonews agent.

Two distinct kinds of memory, deliberately kept apart:

* **Knowledge notes** -- things the user asked the agent to remember, or durable
  facts it decided were worth keeping. These survive restarts and are recalled
  by keyword. This is what makes the agent useful across sessions: it can know
  that the user follows the migration file, without being told again.
* **Event log** -- a running record of what the agent learned and when, so it can
  answer "what has happened with X lately?" and synthesise a thread over time
  rather than reacting to a single headline.

* **Chat history** -- conversations the user actually had, stored as a session per
  conversation and one row per turn. Without this the transcript lives only in the
  DOM, so it is lost on reload and cannot be resumed; and because the agent has a
  durable event log, it could recall what it learned while being unable to show
  what the user asked. The session list is also what "look at older chats" reads.

All three live in the main butler database rather than a side file, so they move
with the rest of the app and are covered by the same backup story.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from typing import Any, Dict, List, Optional

from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    create_engine,
    or_,
)
from sqlalchemy.orm import declarative_base, sessionmaker

from local_butler.core.database import DEFAULT_DB_URL

MemoryBase = declarative_base()


def utcnow() -> dt.datetime:
    """Naive UTC.

    SQLite does not persist tzinfo, so a timezone-aware default writes a value
    that reads back naive and silently breaks the recency filters. Keeping both
    sides naive avoids the whole class of bug.
    """
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


class AgentNote(MemoryBase):
    """One durable thing the agent remembers."""

    __tablename__ = "geonews_agent_notes"

    id = Column(Integer, primary_key=True)
    topic = Column(String(128), index=True)
    note = Column(Text, nullable=False)
    created_at = Column(
        DateTime,
        default=utcnow,
    )

    __table_args__ = (Index("ix_agent_notes_topic_created", "topic", "created_at"),)


class AgentEvent(MemoryBase):
    """Something the agent saw, for timeline questions and synthesis."""

    __tablename__ = "geonews_agent_events"

    id = Column(Integer, primary_key=True)
    kind = Column(String(32), default="search")
    topic = Column(String(128), index=True)
    summary = Column(Text)
    detail = Column(Text)
    created_at = Column(
        DateTime,
        default=utcnow,
    )

    __table_args__ = (Index("ix_agent_events_topic_created", "topic", "created_at"),)


class ChatSession(MemoryBase):
    """One conversation with the agent."""

    __tablename__ = "geonews_agent_chats"

    id = Column(Integer, primary_key=True)
    title = Column(String(200), default="")
    # Nullable: a chat is created when the user starts one, before any message
    # exists to summarise it from.
    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(
        DateTime,
        default=utcnow,
        onupdate=utcnow,
    )

    __table_args__ = (Index("ix_agent_chats_updated", "updated_at"),)


class ChatMessage(MemoryBase):
    """One turn: what the user asked and what came back."""

    __tablename__ = "geonews_agent_messages"

    id = Column(Integer, primary_key=True)
    session_id = Column(Integer, ForeignKey("geonews_agent_chats.id"), index=True)
    role = Column(String(16))
    content = Column(Text)
    # JSON blob of the metadata that turn produced: tools consulted, searches
    # run, notes written. Kept so a resumed chat can explain an old answer.
    meta = Column(Text)
    created_at = Column(DateTime, default=utcnow)

    __table_args__ = (Index("ix_agent_messages_session_created", "session_id", "created_at"),)


class MemoryStore:
    """CRUD helpers over the note, event and chat tables."""

    def __init__(self, db_url: str = DEFAULT_DB_URL) -> None:
        self.engine = create_engine(db_url, future=True)
        self._ensure_schema()
        self.Session = sessionmaker(bind=self.engine, future=True)

    def _ensure_schema(self) -> None:
        """Create the memory tables if missing, without disturbing other tables."""
        MemoryBase.metadata.create_all(
            self.engine,
            tables=[
                AgentNote.__table__,
                AgentEvent.__table__,
                ChatSession.__table__,
                ChatMessage.__table__,
            ],
        )

    # -- notes ------------------------------------------------------------

    def add_note(self, note: str, topic: str = "") -> Dict[str, Any]:
        session = self.Session()
        try:
            row = AgentNote(topic=(topic or "").strip().lower()[:128], note=note.strip())
            session.add(row)
            session.commit()
            return {
                "id": row.id,
                "topic": row.topic,
                "note": row.note,
                "created_at": row.created_at.isoformat() if row.created_at else None,
            }
        finally:
            session.close()

    def search_notes(self, query: str, limit: int = 6) -> List[Dict[str, Any]]:
        """Keyword match on note text and topic.

        Plain LIKE rather than FTS: note counts are small, and this keeps the
        table shape simple. Terms are ORed so "eu migration" finds either word.
        """
        terms = [t for t in _terms(query) if len(t) > 2]
        session = self.Session()
        try:
            if not terms:
                rows = (
                    session.query(AgentNote)
                    .order_by(AgentNote.created_at.desc())
                    .limit(limit)
                    .all()
                )
            else:
                clauses = [
                    AgentNote.note.ilike(f"%{t}%") | AgentNote.topic.ilike(f"%{t}%")
                    for t in terms
                ]
                rows = (
                    session.query(AgentNote)
                    .filter(or_(*clauses))
                    .order_by(AgentNote.created_at.desc())
                    .limit(limit)
                    .all()
                )
            return [
                {
                    "id": r.id,
                    "topic": r.topic,
                    "note": r.note,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                }
                for r in rows
            ]
        finally:
            session.close()

    def list_notes(self, limit: int = 50) -> List[Dict[str, Any]]:
        session = self.Session()
        try:
            rows = (
                session.query(AgentNote)
                .order_by(AgentNote.created_at.desc())
                .limit(limit)
                .all()
            )
            return [
                {
                    "id": r.id,
                    "topic": r.topic,
                    "note": r.note,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                }
                for r in rows
            ]
        finally:
            session.close()

    def delete_note(self, note_id: int) -> bool:
        session = self.Session()
        try:
            row = session.get(AgentNote, note_id)
            if row is None:
                return False
            session.delete(row)
            session.commit()
            return True
        finally:
            session.close()

    # -- events -----------------------------------------------------------

    def add_event(
        self,
        summary: str,
        topic: str = "",
        kind: str = "search",
        detail: Optional[str] = None,
    ) -> Dict[str, Any]:
        session = self.Session()
        try:
            row = AgentEvent(
                summary=summary.strip(),
                topic=(topic or "").strip().lower()[:128],
                kind=kind,
                detail=detail,
            )
            session.add(row)
            session.commit()
            return {
                "id": row.id,
                "topic": row.topic,
                "summary": row.summary,
                "created_at": row.created_at.isoformat() if row.created_at else None,
            }
        finally:
            session.close()

    def recent_events(
        self, query: str = "", hours: int = 72, limit: int = 20
    ) -> List[Dict[str, Any]]:
        """Recent events, optionally filtered by keyword."""
        session = self.Session()
        try:
            cutoff = utcnow() - dt.timedelta(hours=hours)
            query_obj = session.query(AgentEvent).filter(AgentEvent.created_at >= cutoff)
            terms = [t for t in _terms(query) if len(t) > 2]
            if terms:
                query_obj = query_obj.filter(
                    or_(
                        *[
                            AgentEvent.summary.ilike(f"%{t}%")
                            | AgentEvent.topic.ilike(f"%{t}%")
                            for t in terms
                        ]
                    )
                )
            rows = (
                query_obj.order_by(AgentEvent.created_at.desc()).limit(limit).all()
            )
            return [
                {
                    "id": r.id,
                    "topic": r.topic,
                    "summary": r.summary,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                }
                for r in rows
            ]
        finally:
            session.close()

    # -- chat history -----------------------------------------------------

    def create_chat(self, title: str = "New chat") -> Dict[str, Any]:
        """Open a chat. Idempotent enough for a button that may double-fire."""
        session = self.Session()
        try:
            row = ChatSession(title=(title or "").strip()[:200] or "New chat")
            session.add(row)
            session.commit()
            return {
                "id": row.id,
                "title": row.title,
                "created_at": row.created_at.isoformat() if row.created_at else None,
                "updated_at": row.updated_at.isoformat() if row.updated_at else None,
            }
        finally:
            session.close()

    def add_message(
        self,
        session_id: int,
        role: str,
        content: str,
        meta: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Append a turn, and name the chat after its first real question.

        Deriving the title here rather than in the frontend means a chat opened
        from any entry point (chat panel, deep research panel, a restored page)
        ends up with the same sensible name.
        """
        session = self.Session()
        try:
            chat = session.get(ChatSession, session_id)
            if chat is None:
                return {}
            row = ChatMessage(
                session_id=session_id,
                role=role,
                content=content,
                meta=json.dumps(meta) if meta else None,
            )
            session.add(row)
            if role == "user" and (not chat.title or chat.title == "New chat"):
                chat.title = _title_from(content)
            chat.updated_at = utcnow()
            session.commit()
            return {
                "id": row.id,
                "session_id": row.session_id,
                "role": row.role,
                "created_at": row.created_at.isoformat() if row.created_at else None,
            }
        finally:
            session.close()

    def list_chats(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Chats, most recently active first, each with a preview.

        One query for the sessions and one for the first user message of each,
        rather than N queries -- with 50 chats the difference is visible on the
        history list's first paint.
        """
        session = self.Session()
        try:
            chats = (
                session.query(ChatSession)
                .order_by(ChatSession.updated_at.desc(), ChatSession.id.desc())
                .limit(limit)
                .all()
            )
            previews: Dict[int, str] = {}
            if chats:
                subquery = (
                    session.query(
                        ChatMessage.session_id.label("sid"),
                        ChatMessage.content.label("content"),
                    )
                    .filter(ChatMessage.session_id.in_([c.id for c in chats]))
                    .order_by(ChatMessage.created_at.asc(), ChatMessage.id.asc())
                )
                for sid, content in _first_per_session(session, subquery):
                    previews.setdefault(sid, content or "")

            out = []
            for c in chats:
                out.append(
                    {
                        "id": c.id,
                        "title": c.title or "New chat",
                        "created_at": c.created_at.isoformat() if c.created_at else None,
                        "updated_at": c.updated_at.isoformat() if c.updated_at else None,
                        "preview": _preview(previews.get(c.id, "")),
                    }
                )
            return out
        finally:
            session.close()

    def get_chat(self, session_id: int) -> Optional[Dict[str, Any]]:
        """A chat and its full transcript, oldest turn first."""
        session = self.Session()
        try:
            chat = session.get(ChatSession, session_id)
            if chat is None:
                return None
            rows = (
                session.query(ChatMessage)
                .filter(ChatMessage.session_id == session_id)
                .order_by(ChatMessage.created_at.asc(), ChatMessage.id.asc())
                .all()
            )
            return {
                "id": chat.id,
                "title": chat.title or "New chat",
                "created_at": chat.created_at.isoformat() if chat.created_at else None,
                "updated_at": chat.updated_at.isoformat() if chat.updated_at else None,
                "messages": [
                    {
                        "id": r.id,
                        "role": r.role,
                        "content": r.content,
                        "meta": _loads(r.meta),
                        "created_at": r.created_at.isoformat() if r.created_at else None,
                    }
                    for r in rows
                ],
            }
        finally:
            session.close()

    def chat_history(self, session_id: int, limit: int = 12) -> List[Dict[str, str]]:
        """The last ``limit`` turns as plain role/content pairs.

        Separate from ``get_chat`` on purpose: the history list for the frontend
        needs timestamps and metadata, the agent needs only what it can replay as
        chat messages. Truncating here keeps a 40k-character deep-research report
        out of the model's context window.
        """
        session = self.Session()
        try:
            rows = (
                session.query(ChatMessage)
                .filter(
                    ChatMessage.session_id == session_id,
                    ChatMessage.role.in_(("user", "assistant")),
                )
                .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
                .limit(limit)
                .all()
            )
            return [
                {"role": r.role, "content": (r.content or "")[:HISTORY_CHAR_LIMIT]}
                for r in reversed(rows)
            ]
        finally:
            session.close()

    def rename_chat(self, session_id: int, title: str) -> bool:
        session = self.Session()
        try:
            chat = session.get(ChatSession, session_id)
            if chat is None:
                return False
            chat.title = (title or "").strip()[:200] or "New chat"
            chat.updated_at = utcnow()
            session.commit()
            return True
        finally:
            session.close()

    def delete_chat(self, session_id: int) -> bool:
        """Remove a chat and every turn in it.

        Messages go first: they hold a foreign key, so deleting the parent first
        would either fail or leave orphans depending on the database.
        """
        session = self.Session()
        try:
            chat = session.get(ChatSession, session_id)
            if chat is None:
                return False
            (
                session.query(ChatMessage)
                .filter(ChatMessage.session_id == session_id)
                .delete(synchronize_session=False)
            )
            session.delete(chat)
            session.commit()
            return True
        finally:
            session.close()


def _first_per_session(session, query) -> List[Any]:
    """First row per session_id from a query already ordered oldest-first.

    SQLAlchemy's `in_` + `order_by` returns every message, not just the first of
    each chat. Grouping in Python keeps this portable across SQLite and Postgres
    instead of relying on window functions.
    """
    seen = set()
    out = []
    for sid, content in query.all():
        if sid in seen:
            continue
        seen.add(sid)
        out.append((sid, content))
    return out


def _title_from(message: str) -> str:
    """Turn a first question into a chat name."""
    text = " ".join((message or "").split())
    if not text:
        return "New chat"
    if len(text) <= 60:
        return text
    return text[:57].rstrip() + "..."


def _preview(text: str) -> str:
    """A one-line hint for the history list.

    Empty stays empty rather than falling back to "New chat": a chat the user
    opened and never used should read as blank, not as though it already had a
    title worth matching.
    """
    return _title_from(text) if (text or "").strip() else ""


def _loads(blob: Optional[str]) -> Optional[Any]:
    if not blob:
        return None
    try:
        return json.loads(blob)
    except (TypeError, ValueError):
        return None


def _terms(query: str) -> List[str]:
    return re.findall(r"[A-Za-z0-9]{2,}", (query or "").lower())


# Per-turn cap for history handed back to the agent. A deep-research answer is
# several thousand characters; replaying it whole crowds out the new question.
HISTORY_CHAR_LIMIT = 1200


_STORE: Optional[MemoryStore] = None


def get_store() -> MemoryStore:
    """Process-wide store, created on first use."""
    global _STORE
    if _STORE is None:
        _STORE = MemoryStore()
    return _STORE