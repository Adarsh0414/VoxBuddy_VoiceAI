"""
Tests for persistence_dynamodb.py using moto (mocks the AWS DynamoDB API —
no real network calls, no AWS costs, no real credentials needed). Mirrors
tests/test_persistence.py's scenarios exactly, so both backends are
proven to satisfy the same contract that app.py depends on.
"""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from moto import mock_aws

os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_REGION", "us-east-1")

from session.manager import IncomingUtterance, SessionManager  # noqa: E402


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


@pytest.fixture
def db():
    """Fresh moto-mocked DynamoDB + a freshly-imported persistence_dynamodb
    module per test, so the module-level cached boto3 resource and each
    test's tables never leak across tests."""
    with mock_aws():
        import persistence_dynamodb
        importlib.reload(persistence_dynamodb)
        persistence_dynamodb.init_db()
        yield persistence_dynamodb


def test_save_and_retrieve_conversation(db):
    session = make_session_with_turns()
    conversation_id = db.save_conversation(session, "sess-1")
    assert conversation_id is not None

    detail = db.get_conversation(conversation_id)
    assert detail is not None
    assert len(detail.turns) == 2
    assert detail.summary.session_id == "sess-1"


def test_empty_session_saves_nothing(db):
    session = SessionManager()
    session.enroll_self("me")
    assert db.save_conversation(session, "sess-empty") is None


def test_list_conversations_scoped_by_user(db):
    session = make_session_with_turns()
    cid_a = db.save_conversation(session, "sess-a", user_id=1)
    cid_b = db.save_conversation(session, "sess-b", user_id=2)

    only_user_1 = db.list_conversations(user_id=1)
    assert [c.id for c in only_user_1] == [cid_a]

    everyone = db.list_conversations(user_id=None)
    assert {c.id for c in everyone} == {cid_a, cid_b}


def test_conversation_ids_increment(db):
    session = make_session_with_turns()
    first = db.save_conversation(session, "sess-1")
    second = db.save_conversation(session, "sess-2")
    assert second == first + 1


def test_delete_all_removes_conversations_and_turns(db):
    session = make_session_with_turns()
    conversation_id = db.save_conversation(session, "sess-1")

    db.delete_all()

    assert db.get_conversation(conversation_id) is None
    assert db.list_conversations() == []


def test_get_conversation_owner(db):
    session = make_session_with_turns()
    conversation_id = db.save_conversation(session, "sess-1", user_id=42)
    assert db.get_conversation_owner(conversation_id) == 42


def test_summary_stats_and_language_breakdown(db):
    session = make_session_with_turns()
    db.save_conversation(session, "sess-1")

    stats = db.get_summary_stats()
    assert stats.total_conversations == 1
    assert stats.total_languages >= 1

    breakdown = db.get_language_breakdown()
    assert any(row["lang"] == "hi" for row in breakdown)
