import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

import persistence
from session.manager import IncomingUtterance, SessionManager


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "test_voxbuddy.db"
    persistence.init_db(path)
    return path


def make_session_with_turns():
    session = SessionManager()
    session.enroll_self("me")
    session.handle_utterance(IncomingUtterance(
        speaker_label="shopkeeper", text="namaste", target_lang="en",
        turn_taking_score=0.9, semantic_coherence_score=0.9,
    ))
    session.handle_utterance(IncomingUtterance(
        speaker_label="me", text="hello", target_lang="hi",
        turn_taking_score=0.9, semantic_coherence_score=0.9,
    ))
    return session


def test_save_and_retrieve_conversation(db_path):
    session = make_session_with_turns()
    conversation_id = persistence.save_conversation(session, "sess-1", db_path=db_path)
    assert conversation_id is not None

    detail = persistence.get_conversation(conversation_id, db_path=db_path)
    assert detail is not None
    assert detail.summary.turn_count == 2
    assert len(detail.turns) == 2
    roles = {t.role for t in detail.turns}
    assert roles == {"partner", "self"}


def test_saving_empty_session_returns_none(db_path):
    session = SessionManager()  # no utterances processed
    result = persistence.save_conversation(session, "sess-empty", db_path=db_path)
    assert result is None


def test_list_conversations_orders_most_recent_first(db_path):
    s1 = make_session_with_turns()
    persistence.save_conversation(s1, "sess-1", db_path=db_path)
    time.sleep(0.01)
    s2 = make_session_with_turns()
    persistence.save_conversation(s2, "sess-2", db_path=db_path)

    summaries = persistence.list_conversations(db_path=db_path)
    assert len(summaries) == 2
    assert summaries[0].session_id == "sess-2"  # most recent first
    assert summaries[0].first_line == "namaste"


def test_get_nonexistent_conversation_returns_none(db_path):
    assert persistence.get_conversation(9999, db_path=db_path) is None


def test_delete_all_clears_history(db_path):
    session = make_session_with_turns()
    persistence.save_conversation(session, "sess-1", db_path=db_path)
    assert len(persistence.list_conversations(db_path=db_path)) == 1

    persistence.delete_all(db_path=db_path)
    assert len(persistence.list_conversations(db_path=db_path)) == 0


def test_bystander_turns_are_never_persisted(db_path):
    """Bystanders never enter the translation pipeline (CIE gates them out
    before ASR even runs — see session/manager.py), so there should be
    nothing of theirs in turn_history to persist in the first place."""
    session = SessionManager()
    session.enroll_self("me")
    session.handle_utterance(IncomingUtterance(
        speaker_label="shopkeeper", text="namaste", target_lang="en",
        turn_taking_score=0.9, semantic_coherence_score=0.9,
    ))
    session.handle_utterance(IncomingUtterance(
        speaker_label="random_vendor", text="aloo le lo", target_lang="en",
        turn_taking_score=0.1, semantic_coherence_score=0.05,
    ))
    conversation_id = persistence.save_conversation(session, "sess-1", db_path=db_path)
    detail = persistence.get_conversation(conversation_id, db_path=db_path)
    assert detail.summary.turn_count == 1
    assert detail.turns[0].source_text == "namaste"


def test_summary_stats_on_empty_history(db_path):
    stats = persistence.get_summary_stats(db_path=db_path)
    assert stats.total_conversations == 0
    assert stats.total_languages == 0
    assert stats.total_seconds == 0


def test_summary_stats_reflect_real_conversations(db_path):
    # First conversation: shopkeeper (Hindi, per MockLanguageIDAgent)
    s1 = make_session_with_turns()
    persistence.save_conversation(s1, "sess-1", db_path=db_path)

    # Second conversation: a French partner this time
    s2 = SessionManager()
    s2.enroll_self("me")
    s2.handle_utterance(IncomingUtterance(
        speaker_label="hotel_desk", text="bonjour, comment allez-vous?", target_lang="en",
        turn_taking_score=0.9, semantic_coherence_score=0.9,
    ))
    persistence.save_conversation(s2, "sess-2", db_path=db_path)

    stats = persistence.get_summary_stats(db_path=db_path)
    assert stats.total_conversations == 2
    assert stats.total_languages == 2  # Hindi + French, counted from partner turns only


def test_summary_stats_language_count_ignores_duplicate_languages(db_path):
    # Two separate conversations, both with a Hindi-speaking partner —
    # should count as ONE distinct language, not two.
    for i in range(2):
        s = make_session_with_turns()
        persistence.save_conversation(s, f"sess-{i}", db_path=db_path)

    stats = persistence.get_summary_stats(db_path=db_path)
    assert stats.total_conversations == 2
    assert stats.total_languages == 1


def test_conversations_scoped_by_user_id(db_path):
    s1 = make_session_with_turns()
    persistence.save_conversation(s1, "sess-user1", user_id=1, db_path=db_path)
    s2 = make_session_with_turns()
    persistence.save_conversation(s2, "sess-user2", user_id=2, db_path=db_path)
    s3 = make_session_with_turns()
    persistence.save_conversation(s3, "sess-anon", user_id=None, db_path=db_path)

    user1_history = persistence.list_conversations(db_path=db_path, user_id=1)
    assert len(user1_history) == 1
    assert user1_history[0].session_id == "sess-user1"

    # No user_id filter -> everyone's conversations, existing dashboard behavior
    everyone = persistence.list_conversations(db_path=db_path)
    assert len(everyone) == 3


def test_stats_scoped_by_user_id(db_path):
    s1 = make_session_with_turns()
    persistence.save_conversation(s1, "sess-user1", user_id=1, db_path=db_path)
    s2 = make_session_with_turns()
    persistence.save_conversation(s2, "sess-user2", user_id=2, db_path=db_path)

    user1_stats = persistence.get_summary_stats(db_path=db_path, user_id=1)
    assert user1_stats.total_conversations == 1

    all_stats = persistence.get_summary_stats(db_path=db_path)
    assert all_stats.total_conversations == 2


def test_delete_all_scoped_to_user_leaves_others_intact(db_path):
    s1 = make_session_with_turns()
    persistence.save_conversation(s1, "sess-user1", user_id=1, db_path=db_path)
    s2 = make_session_with_turns()
    persistence.save_conversation(s2, "sess-user2", user_id=2, db_path=db_path)

    persistence.delete_all(db_path=db_path, user_id=1)

    assert persistence.list_conversations(db_path=db_path, user_id=1) == []
    assert len(persistence.list_conversations(db_path=db_path, user_id=2)) == 1


def test_migration_adds_user_id_column_to_existing_table(tmp_path):
    """Simulates a database created before user accounts existed (no
    user_id column) — init_db should migrate it in place rather than
    silently no-op-ing against CREATE TABLE IF NOT EXISTS."""
    import sqlite3
    old_db = tmp_path / "old_schema.db"
    conn = sqlite3.connect(str(old_db))
    conn.executescript("""
        CREATE TABLE conversations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            started_at REAL NOT NULL,
            ended_at REAL NOT NULL,
            target_lang TEXT NOT NULL,
            turn_count INTEGER NOT NULL,
            duration_seconds REAL NOT NULL
        );
    """)
    conn.commit()
    conn.close()

    persistence.init_db(old_db)

    conn = sqlite3.connect(str(old_db))
    columns = {row[1] for row in conn.execute("PRAGMA table_info(conversations)")}
    conn.close()
    assert "user_id" in columns


def test_get_conversation_owner(db_path):
    s = make_session_with_turns()
    conv_id = persistence.save_conversation(s, "sess-1", user_id=42, db_path=db_path)
    assert persistence.get_conversation_owner(conv_id, db_path=db_path) == 42


def test_get_conversation_owner_none_for_anonymous(db_path):
    s = make_session_with_turns()
    conv_id = persistence.save_conversation(s, "sess-1", db_path=db_path)  # no user_id
    assert persistence.get_conversation_owner(conv_id, db_path=db_path) is None


def test_get_conversation_owner_none_for_missing_conversation(db_path):
    assert persistence.get_conversation_owner(99999, db_path=db_path) is None


def test_language_breakdown_counts_conversations_not_turns(db_path):
    """A conversation with 3 Hindi turns should count as ONE conversation
    for Hindi, not three — the breakdown answers 'how many conversations
    have I had in this language,' not 'how many sentences.'"""
    session = SessionManager()
    session.enroll_self("me")
    for text in ["namaste", "aap kaise hain", "namaste, kya haal hai"]:
        session.handle_utterance(IncomingUtterance(
            speaker_label="shopkeeper", text=text, target_lang="en",
            turn_taking_score=0.9, semantic_coherence_score=0.9,
        ))
    persistence.save_conversation(session, "sess-1", db_path=db_path)

    breakdown = persistence.get_language_breakdown(db_path=db_path)
    assert breakdown == [{"lang": "hi", "count": 1}]


def test_language_breakdown_orders_by_frequency(db_path):
    # Two Hindi conversations, one French — Hindi should come first.
    for i in range(2):
        s = make_session_with_turns()  # Hindi, via "namaste"
        persistence.save_conversation(s, f"sess-hi-{i}", db_path=db_path)

    s = SessionManager()
    s.enroll_self("me")
    s.handle_utterance(IncomingUtterance(
        speaker_label="hotel_desk", text="bonjour, comment allez-vous?", target_lang="en",
        turn_taking_score=0.9, semantic_coherence_score=0.9,
    ))
    persistence.save_conversation(s, "sess-fr", db_path=db_path)

    breakdown = persistence.get_language_breakdown(db_path=db_path)
    assert breakdown[0] == {"lang": "hi", "count": 2}
    assert breakdown[1] == {"lang": "fr", "count": 1}


def test_language_breakdown_scoped_by_user(db_path):
    s1 = make_session_with_turns()
    persistence.save_conversation(s1, "sess-1", user_id=1, db_path=db_path)
    s2 = SessionManager()
    s2.enroll_self("me")
    s2.handle_utterance(IncomingUtterance(
        speaker_label="hotel_desk", text="bonjour, comment allez-vous?", target_lang="en",
        turn_taking_score=0.9, semantic_coherence_score=0.9,
    ))
    persistence.save_conversation(s2, "sess-2", user_id=2, db_path=db_path)

    user1_breakdown = persistence.get_language_breakdown(db_path=db_path, user_id=1)
    assert user1_breakdown == [{"lang": "hi", "count": 1}]
