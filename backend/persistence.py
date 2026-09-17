"""
Lightweight persistence for completed conversations — closes the "History
is mock data" gap flagged in PROGRESS.md.

Deliberately simple: stdlib sqlite3, no ORM, one file on disk. This is a
Phase 1/2-appropriate choice — a real production deployment would move this
to a proper database (and, per the PRD, a real user/auth system), but a
single-file SQLite store is exactly right for "runs locally in VS Code with
zero setup," which is this project's whole design constraint.

Schema:
  conversations(id, session_id, user_id, started_at, ended_at,
                target_lang, turn_count, duration_seconds)
  turns(id, conversation_id, role, source_lang, source_text, target_lang,
        target_text, timestamp)

Only PARTNER and SELF turns are persisted — bystanders/unresolved speakers
never entered the translation pipeline in the first place (see
session/manager.py), so there's nothing of theirs to save.

user_id is nullable: conversations saved without a logged-in user (the dev
dashboard at "/", simulate.py) are stored as "anonymous" (user_id IS NULL)
rather than rejected — auth is additive scoping on top of an already-working
system, not a hard requirement to use the CIE at all.
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from cie.state import SpeakerRole
from session.manager import SessionManager

DB_PATH = Path(__file__).resolve().parent / "voxbuddy.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    user_id INTEGER,
    started_at REAL NOT NULL,
    ended_at REAL NOT NULL,
    target_lang TEXT NOT NULL,
    turn_count INTEGER NOT NULL,
    duration_seconds REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS turns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id),
    role TEXT NOT NULL,
    source_lang TEXT NOT NULL,
    source_text TEXT NOT NULL,
    target_lang TEXT NOT NULL,
    target_text TEXT NOT NULL,
    timestamp REAL NOT NULL
);
"""


@contextmanager
def _connect(db_path: Path | None = None):
    # Resolved here, at call time, via the module global — NOT as a
    # function-signature default. Defaults are bound once at definition
    # time, so `def f(db_path=DB_PATH)` would freeze in the DB_PATH that
    # existed at import time and silently ignore any later
    # `persistence.DB_PATH = ...` monkeypatch (exactly what the test suite
    # needs to do to use a throwaway DB per test).
    resolved_path = db_path if db_path is not None else DB_PATH
    conn = sqlite3.connect(str(resolved_path))
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        # Commit in `finally`, not after `yield` — see auth_store.py's
        # _connect for the real bug this pattern avoids (a caller raising
        # inside the `with` block would otherwise silently discard any
        # writes already made before the raise).
        conn.commit()
        conn.close()


def init_db(db_path: Path | None = None) -> None:
    with _connect(db_path) as conn:
        conn.executescript(SCHEMA)
        # Lightweight migration: a conversations table created before user
        # accounts existed won't have this column, and CREATE TABLE IF NOT
        # EXISTS above is a no-op against an already-existing table.
        existing_columns = {row["name"] for row in conn.execute("PRAGMA table_info(conversations)")}
        if "user_id" not in existing_columns:
            conn.execute("ALTER TABLE conversations ADD COLUMN user_id INTEGER")


@dataclass
class TurnRecord:
    role: str
    source_lang: str
    source_text: str
    target_lang: str
    target_text: str
    timestamp: float


@dataclass
class ConversationSummary:
    id: int
    session_id: str
    started_at: float
    ended_at: float
    target_lang: str
    turn_count: int
    duration_seconds: float
    first_line: str | None = None


@dataclass
class ConversationDetail:
    summary: ConversationSummary
    turns: list[TurnRecord]


def save_conversation(session: SessionManager, session_id: str,
                       user_id: int | None = None,
                       db_path: Path | None = None) -> int | None:
    """Persists a completed session's turn history. Returns the new
    conversation id, or None if there was nothing worth saving (e.g. the
    conversation never actually got past bystander/unknown speakers).

    user_id is optional — a conversation saved without a logged-in user
    (the dev dashboard, simulate.py) is stored as anonymous (NULL) rather
    than rejected."""
    turns = session.state.turn_history
    if not turns:
        return None

    started_at = turns[0].timestamp
    ended_at = turns[-1].timestamp
    duration = max(ended_at - started_at, 0.0)

    with _connect(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO conversations (session_id, user_id, started_at, ended_at, "
            "target_lang, turn_count, duration_seconds) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (session_id, user_id, started_at, ended_at, turns[-1].target_lang, len(turns), duration),
        )
        conversation_id = cur.lastrowid

        for turn in turns:
            speaker = session.state.speakers.get(turn.speaker_id)
            role = speaker.role.value if speaker else SpeakerRole.UNKNOWN.value
            conn.execute(
                "INSERT INTO turns (conversation_id, role, source_lang, source_text, "
                "target_lang, target_text, timestamp) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (conversation_id, role, turn.source_lang, turn.source_text,
                 turn.target_lang, turn.target_text, turn.timestamp),
            )

    return conversation_id


def list_conversations(db_path: Path | None = None, limit: int = 50,
                        user_id: int | None = None) -> list[ConversationSummary]:
    """If user_id is given, only that user's conversations are returned.
    If user_id is None, ALL conversations (including other users' and
    anonymous ones) are returned — this is the existing dev-dashboard
    behavior, unchanged for backward compatibility. The app.py endpoint is
    what decides which mode to use based on whether the caller is
    authenticated."""
    with _connect(db_path) as conn:
        if user_id is not None:
            rows = conn.execute(
                "SELECT * FROM conversations WHERE user_id = ? "
                "ORDER BY started_at DESC LIMIT ?", (user_id, limit)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM conversations ORDER BY started_at DESC LIMIT ?", (limit,)
            ).fetchall()
        summaries = []
        for row in rows:
            first_turn = conn.execute(
                "SELECT source_text FROM turns WHERE conversation_id = ? "
                "ORDER BY timestamp ASC LIMIT 1", (row["id"],)
            ).fetchone()
            summaries.append(ConversationSummary(
                id=row["id"], session_id=row["session_id"],
                started_at=row["started_at"], ended_at=row["ended_at"],
                target_lang=row["target_lang"], turn_count=row["turn_count"],
                duration_seconds=row["duration_seconds"],
                first_line=first_turn["source_text"] if first_turn else None,
            ))
        return summaries


def get_conversation_owner(conversation_id: int, db_path: Path | None = None) -> int | None:
    """Returns the user_id a conversation was saved under, or None if it
    was saved anonymously (or doesn't exist — callers should already know
    it exists via get_conversation before checking ownership)."""
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT user_id FROM conversations WHERE id = ?", (conversation_id,)
        ).fetchone()
        return row["user_id"] if row else None


def get_conversation(conversation_id: int, db_path: Path | None = None) -> ConversationDetail | None:
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM conversations WHERE id = ?", (conversation_id,)
        ).fetchone()
        if not row:
            return None

        turn_rows = conn.execute(
            "SELECT * FROM turns WHERE conversation_id = ? ORDER BY timestamp ASC",
            (conversation_id,),
        ).fetchall()

        summary = ConversationSummary(
            id=row["id"], session_id=row["session_id"],
            started_at=row["started_at"], ended_at=row["ended_at"],
            target_lang=row["target_lang"], turn_count=row["turn_count"],
            duration_seconds=row["duration_seconds"],
        )
        turns = [
            TurnRecord(role=r["role"], source_lang=r["source_lang"], source_text=r["source_text"],
                       target_lang=r["target_lang"], target_text=r["target_text"],
                       timestamp=r["timestamp"])
            for r in turn_rows
        ]
        return ConversationDetail(summary=summary, turns=turns)


def delete_all(db_path: Path | None = None, user_id: int | None = None) -> None:
    """Used by tests and by the 'clear my history' privacy control (PRD
    §14 — users can delete conversation history). If user_id is given,
    only that user's conversations/turns are deleted; if None, EVERYTHING
    is deleted (existing behavior, used by the anonymous dev-dashboard
    privacy control and by tests)."""
    with _connect(db_path) as conn:
        if user_id is not None:
            conv_ids = [r["id"] for r in conn.execute(
                "SELECT id FROM conversations WHERE user_id = ?", (user_id,)
            ).fetchall()]
            for cid in conv_ids:
                conn.execute("DELETE FROM turns WHERE conversation_id = ?", (cid,))
            conn.execute("DELETE FROM conversations WHERE user_id = ?", (user_id,))
        else:
            conn.execute("DELETE FROM turns")
            conn.execute("DELETE FROM conversations")


def get_language_breakdown(db_path: Path | None = None, user_id: int | None = None) -> list[dict]:
    """Powers the Profile screen's 'Languages spoken with' list — real
    per-language conversation counts, replacing the hardcoded fake
    percentages that used to sit there. Ordered by how often each
    language actually came up, most first."""
    with _connect(db_path) as conn:
        if user_id is not None:
            rows = conn.execute(
                "SELECT t.source_lang AS lang, COUNT(DISTINCT t.conversation_id) AS n "
                "FROM turns t JOIN conversations c ON c.id = t.conversation_id "
                "WHERE t.role = 'partner' AND c.user_id = ? "
                "GROUP BY t.source_lang ORDER BY n DESC", (user_id,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT source_lang AS lang, COUNT(DISTINCT conversation_id) AS n "
                "FROM turns WHERE role = 'partner' GROUP BY source_lang ORDER BY n DESC"
            ).fetchall()
    return [{"lang": r["lang"], "count": r["n"]} for r in rows]


@dataclass
class SummaryStats:
    total_conversations: int
    total_languages: int
    total_seconds: float
    day_streak: int


def get_summary_stats(db_path: Path | None = None, user_id: int | None = None) -> SummaryStats:
    """Powers the Profile screen's stat cards — real numbers computed from
    persisted history, replacing the hardcoded 27/5/3h40m mock values.

    'Languages' is deliberately counted from partner-role turns' source_lang
    (the language actually spoken BACK to the user), not the conversations
    table's target_lang column — target_lang is always the user's own
    language in this pipeline (everything gets translated TO them), so it
    can't distinguish which partner languages were actually involved.

    If user_id is given, stats are scoped to that user only; if None, stats
    reflect ALL stored conversations (existing anonymous-dashboard behavior).
    """
    with _connect(db_path) as conn:
        if user_id is not None:
            total_conversations = conn.execute(
                "SELECT COUNT(*) AS n FROM conversations WHERE user_id = ?", (user_id,)
            ).fetchone()["n"]
            total_seconds = conn.execute(
                "SELECT COALESCE(SUM(duration_seconds), 0) AS s FROM conversations WHERE user_id = ?",
                (user_id,)
            ).fetchone()["s"]
            total_languages = conn.execute(
                "SELECT COUNT(DISTINCT t.source_lang) AS n FROM turns t "
                "JOIN conversations c ON c.id = t.conversation_id "
                "WHERE t.role = 'partner' AND c.user_id = ?", (user_id,)
            ).fetchone()["n"]
        else:
            total_conversations = conn.execute(
                "SELECT COUNT(*) AS n FROM conversations"
            ).fetchone()["n"]
            total_seconds = conn.execute(
                "SELECT COALESCE(SUM(duration_seconds), 0) AS s FROM conversations"
            ).fetchone()["s"]
            total_languages = conn.execute(
                "SELECT COUNT(DISTINCT source_lang) AS n FROM turns WHERE role = 'partner'"
            ).fetchone()["n"]

    return SummaryStats(
        total_conversations=total_conversations,
        total_languages=total_languages,
        total_seconds=total_seconds,
        day_streak=get_day_streak(db_path=db_path, user_id=user_id),
    )


def get_day_streak(db_path: Path | None = None, user_id: int | None = None,
                    now: float | None = None) -> int:
    """Real consecutive-day streak, replacing the hardcoded "12" that was
    flagged as fake mock data. Counts distinct UTC calendar days on which
    the user completed at least one conversation, walking backward from
    today. Timezone note: uses UTC date boundaries, not the user's local
    day — a conversation at 11pm and one at 1am the next day in the
    user's actual timezone could show as non-consecutive or as the same
    day near midnight. Good enough for a v1 streak; a precise version
    would need the user's timezone stored on their profile, which isn't
    collected anywhere in onboarding right now.

    Streak semantics (the common "don't punish sleep" convention): if the
    most recent active day was today OR yesterday, the streak is live and
    counts consecutive days backward from there. If the most recent
    active day is 2+ days ago, the streak is broken -> 0, even if there
    was a long streak in the past.
    """
    import datetime as _dt

    with _connect(db_path) as conn:
        if user_id is not None:
            rows = conn.execute(
                "SELECT DISTINCT started_at FROM conversations WHERE user_id = ? "
                "ORDER BY started_at DESC", (user_id,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT DISTINCT started_at FROM conversations ORDER BY started_at DESC"
            ).fetchall()

    if not rows:
        return 0

    active_days = sorted({
        _dt.datetime.fromtimestamp(r["started_at"], tz=_dt.timezone.utc).date()
        for r in rows
    }, reverse=True)

    today = _dt.datetime.fromtimestamp(
        now if now is not None else time.time(), tz=_dt.timezone.utc
    ).date()

    most_recent = active_days[0]
    gap_from_today = (today - most_recent).days
    if gap_from_today > 1:
        return 0  # most recent activity was 2+ days ago — streak is broken

    streak = 1
    for i in range(1, len(active_days)):
        expected_prev_day = active_days[i - 1] - _dt.timedelta(days=1)
        if active_days[i] == expected_prev_day:
            streak += 1
        else:
            break
    return streak
