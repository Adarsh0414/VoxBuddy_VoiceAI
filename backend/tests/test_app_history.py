import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from fastapi.testclient import TestClient

import persistence


@pytest.fixture
def client(tmp_path, monkeypatch):
    # Point persistence at a throwaway DB file so these tests never touch
    # (or depend on) the real backend/voxbuddy.db.
    test_db = tmp_path / "test_app_history.db"
    monkeypatch.setattr(persistence, "DB_PATH", test_db)
    persistence.init_db(test_db)

    import app as app_module
    monkeypatch.setattr(app_module.persistence, "DB_PATH", test_db)

    app_module.reset_otp_rate_limit()
    return TestClient(app_module.app)


def _run_conversation(client, session_id):
    client.post(f"/api/session/{session_id}/enroll_self", json={"speaker_label": "me"})
    client.post(f"/api/session/{session_id}/utterance", json={
        "speaker_label": "shopkeeper", "text": "namaste", "target_lang": "en",
        "turn_taking_score": 0.9, "semantic_coherence_score": 0.9,
    })


def test_end_session_persists_and_appears_in_history(client):
    session_id = client.post("/api/session").json()["session_id"]
    _run_conversation(client, session_id)

    end_resp = client.post(f"/api/session/{session_id}/end").json()
    assert end_resp["saved"] is True
    conversation_id = end_resp["conversation_id"]

    history = client.get("/api/history").json()
    assert len(history) == 1
    assert history[0]["id"] == conversation_id
    assert history[0]["turn_count"] == 1
    assert history[0]["first_line"] == "namaste"


def test_history_detail_returns_full_transcript(client):
    session_id = client.post("/api/session").json()["session_id"]
    _run_conversation(client, session_id)
    conversation_id = client.post(f"/api/session/{session_id}/end").json()["conversation_id"]

    detail = client.get(f"/api/history/{conversation_id}").json()
    assert detail["turns"][0]["source_text"] == "namaste"
    assert detail["turns"][0]["role"] == "partner"


def test_ending_session_with_no_turns_is_not_saved(client):
    session_id = client.post("/api/session").json()["session_id"]
    end_resp = client.post(f"/api/session/{session_id}/end").json()
    assert end_resp["saved"] is False
    assert client.get("/api/history").json() == []


def test_ended_session_is_removed_from_active_sessions(client):
    session_id = client.post("/api/session").json()["session_id"]
    _run_conversation(client, session_id)
    client.post(f"/api/session/{session_id}/end")

    # The session should no longer exist server-side.
    state = client.get(f"/api/session/{session_id}/state").json()
    assert state == {"error": "session not found"}


def test_clear_history_removes_everything(client):
    session_id = client.post("/api/session").json()["session_id"]
    _run_conversation(client, session_id)
    client.post(f"/api/session/{session_id}/end")
    assert len(client.get("/api/history").json()) == 1

    resp = client.delete("/api/history")
    assert resp.json()["cleared"] is True
    assert client.get("/api/history").json() == []


def test_history_detail_for_missing_conversation(client):
    resp = client.get("/api/history/99999").json()
    assert resp == {"error": "conversation not found"}


def test_stats_endpoint_reflects_real_conversations(client):
    assert client.get("/api/stats").json() == {
        "total_conversations": 0, "total_languages": 0, "total_seconds": 0.0,
        "day_streak": 0,
    }

    session_id = client.post("/api/session").json()["session_id"]
    _run_conversation(client, session_id)
    client.post(f"/api/session/{session_id}/end")

    stats = client.get("/api/stats").json()
    assert stats["total_conversations"] == 1
    assert stats["total_languages"] == 1  # the shopkeeper turn is Hindi
