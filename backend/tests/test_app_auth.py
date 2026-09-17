import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from fastapi.testclient import TestClient

import auth_store
import persistence
import token_store


@pytest.fixture
def client(tmp_path, monkeypatch):
    test_db = tmp_path / "test_app_auth.db"
    monkeypatch.setattr(persistence, "DB_PATH", test_db)
    monkeypatch.setattr(auth_store, "DB_PATH", test_db)
    persistence.init_db(test_db)
    auth_store.init_db(test_db)
    token_store.reset_store()

    # Force dev mode + console OTP provider so tests never need real
    # credentials and can read the code straight from the response.
    monkeypatch.setenv("VOXBUDDY_AUTH_DEV_MODE", "1")
    monkeypatch.setenv("VOXBUDDY_OTP_PROVIDER", "console")
    monkeypatch.setenv("VOXBUDDY_SESSION_STORE", "sqlite")

    import app as app_module
    monkeypatch.setattr(app_module.persistence, "DB_PATH", test_db)
    monkeypatch.setattr(app_module.auth_store, "DB_PATH", test_db)

    app_module.reset_otp_rate_limit()
    yield TestClient(app_module.app)
    token_store.reset_store()


def login(client, identifier="a@b.com", channel="email"):
    resp = client.post("/api/auth/request-otp", json={"identifier": identifier, "channel": channel})
    assert resp.status_code == 200
    code = resp.json()["dev_otp"]
    assert code is not None

    verify = client.post("/api/auth/verify-otp", json={
        "identifier": identifier, "channel": channel, "code": code,
    })
    assert verify.status_code == 200
    return verify.json()["token"], verify.json()["user"]


# --- request-otp --------------------------------------------------------------

def test_request_otp_returns_dev_code_in_dev_mode(client):
    resp = client.post("/api/auth/request-otp", json={"identifier": "a@b.com", "channel": "email"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["dev_otp"] is not None
    assert len(body["dev_otp"]) == 6


def test_request_otp_rejects_invalid_email(client):
    resp = client.post("/api/auth/request-otp", json={"identifier": "not-an-email", "channel": "email"})
    assert resp.status_code == 400


def test_request_otp_rejects_invalid_phone(client):
    resp = client.post("/api/auth/request-otp", json={"identifier": "12345", "channel": "sms"})
    assert resp.status_code == 400


def test_request_otp_accepts_valid_phone(client):
    resp = client.post("/api/auth/request-otp", json={"identifier": "+14155551234", "channel": "sms"})
    assert resp.status_code == 200


def test_request_otp_cooldown_returns_429(client):
    client.post("/api/auth/request-otp", json={"identifier": "a@b.com", "channel": "email"})
    resp = client.post("/api/auth/request-otp", json={"identifier": "a@b.com", "channel": "email"})
    assert resp.status_code == 429


def test_dev_otp_not_returned_when_dev_mode_off(client, monkeypatch):
    monkeypatch.setenv("VOXBUDDY_AUTH_DEV_MODE", "0")
    resp = client.post("/api/auth/request-otp", json={"identifier": "a@b.com", "channel": "email"})
    assert resp.status_code == 200
    assert resp.json()["dev_otp"] is None


# --- verify-otp ----------------------------------------------------------------

def test_verify_otp_success_issues_token(client):
    token, user = login(client)
    assert token is not None
    assert user["email"] == "a@b.com"
    assert user["phone"] is None


def test_verify_otp_wrong_code_returns_400(client):
    client.post("/api/auth/request-otp", json={"identifier": "a@b.com", "channel": "email"})
    resp = client.post("/api/auth/verify-otp", json={
        "identifier": "a@b.com", "channel": "email", "code": "000000",
    })
    assert resp.status_code == 400


def test_verify_otp_without_request_returns_400(client):
    resp = client.post("/api/auth/verify-otp", json={
        "identifier": "nobody@example.com", "channel": "email", "code": "123456",
    })
    assert resp.status_code == 400


def test_login_via_phone_works_too(client):
    token, user = login(client, identifier="+14155551234", channel="sms")
    assert user["phone"] == "+14155551234"
    assert user["email"] is None


# --- me / logout -----------------------------------------------------------------

def test_me_requires_auth(client):
    resp = client.get("/api/auth/me")
    assert resp.status_code == 401


def test_me_returns_user_with_valid_token(client):
    token, user = login(client)
    resp = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert resp.json()["id"] == user["id"]


def test_me_rejects_garbage_token(client):
    resp = client.get("/api/auth/me", headers={"Authorization": "Bearer not-a-real-token"})
    assert resp.status_code == 401


def test_logout_invalidates_token(client):
    token, _ = login(client)
    client.post("/api/auth/logout", headers={"Authorization": f"Bearer {token}"})
    resp = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401


# --- history/stats scoping with real auth ---------------------------------------

def _run_conversation(client, session_id, headers=None):
    client.post(f"/api/session/{session_id}/enroll_self",
                json={"speaker_label": "me"}, headers=headers or {})
    client.post(f"/api/session/{session_id}/utterance", json={
        "speaker_label": "shopkeeper", "text": "namaste", "target_lang": "en",
        "turn_taking_score": 0.9, "semantic_coherence_score": 0.9,
    }, headers=headers or {})


def test_authenticated_users_see_only_their_own_history(client):
    token_a, _ = login(client, identifier="alice@example.com")
    token_b, _ = login(client, identifier="bob@example.com")
    headers_a = {"Authorization": f"Bearer {token_a}"}
    headers_b = {"Authorization": f"Bearer {token_b}"}

    session_a = client.post("/api/session").json()["session_id"]
    _run_conversation(client, session_a)
    client.post(f"/api/session/{session_a}/end", headers=headers_a)

    session_b = client.post("/api/session").json()["session_id"]
    _run_conversation(client, session_b)
    client.post(f"/api/session/{session_b}/end", headers=headers_b)

    history_a = client.get("/api/history", headers=headers_a).json()
    history_b = client.get("/api/history", headers=headers_b).json()
    assert len(history_a) == 1
    assert len(history_b) == 1
    assert history_a[0]["id"] != history_b[0]["id"]


def test_anonymous_history_unaffected_by_authenticated_users(client):
    """The existing dev dashboard / simulate.py flow (no Authorization
    header at all) should keep working exactly as before — auth is
    additive scoping, not a hard requirement."""
    session_id = client.post("/api/session").json()["session_id"]
    _run_conversation(client, session_id)
    client.post(f"/api/session/{session_id}/end")  # no auth header

    history = client.get("/api/history").json()  # no auth header
    assert len(history) == 1


def test_authenticated_stats_scoped_correctly(client):
    token, _ = login(client)
    headers = {"Authorization": f"Bearer {token}"}

    stats_before = client.get("/api/stats", headers=headers).json()
    assert stats_before["total_conversations"] == 0

    session_id = client.post("/api/session").json()["session_id"]
    _run_conversation(client, session_id)
    client.post(f"/api/session/{session_id}/end", headers=headers)

    stats_after = client.get("/api/stats", headers=headers).json()
    assert stats_after["total_conversations"] == 1


# --- history detail ownership (a real gap: any user could otherwise read
# any other user's transcript just by guessing a conversation id) ------------

def test_user_cannot_read_another_users_conversation_detail(client):
    token_a, _ = login(client, identifier="alice@example.com")
    token_b, _ = login(client, identifier="bob@example.com")
    headers_a = {"Authorization": f"Bearer {token_a}"}
    headers_b = {"Authorization": f"Bearer {token_b}"}

    session_a = client.post("/api/session").json()["session_id"]
    _run_conversation(client, session_a)
    conv_id = client.post(f"/api/session/{session_a}/end", headers=headers_a).json()["conversation_id"]

    # Alice can read her own conversation.
    assert client.get(f"/api/history/{conv_id}", headers=headers_a).status_code == 200

    # Bob cannot — even though he's a valid authenticated user.
    resp = client.get(f"/api/history/{conv_id}", headers=headers_b)
    assert resp.json() == {"error": "conversation not found"}


def test_unauthenticated_user_cannot_read_someone_elses_conversation(client):
    token_a, _ = login(client, identifier="alice@example.com")
    headers_a = {"Authorization": f"Bearer {token_a}"}

    session_a = client.post("/api/session").json()["session_id"]
    _run_conversation(client, session_a)
    conv_id = client.post(f"/api/session/{session_a}/end", headers=headers_a).json()["conversation_id"]

    # No Authorization header at all -> should NOT be able to read Alice's
    # owned conversation just because no token was supplied.
    resp = client.get(f"/api/history/{conv_id}")
    assert resp.json() == {"error": "conversation not found"}


def test_anonymous_conversation_detail_still_readable_by_anyone(client):
    """Preserves existing dev-dashboard behavior: a conversation saved
    with no logged-in user (user_id IS NULL) stays openly readable."""
    session_id = client.post("/api/session").json()["session_id"]
    _run_conversation(client, session_id)
    conv_id = client.post(f"/api/session/{session_id}/end").json()["conversation_id"]  # no auth

    assert client.get(f"/api/history/{conv_id}").status_code == 200


# --- onboarding / profile / preferences (real Setup + Settings wiring) ------

def test_new_user_is_not_onboarded_yet(client):
    token, user = login(client)
    assert user["onboarded"] is False
    assert user["display_name"] is None
    assert user["preferred_language"] is None


def test_onboard_sets_name_language_and_voice(client):
    token, _ = login(client)
    headers = {"Authorization": f"Bearer {token}"}

    resp = client.post("/api/auth/onboard", json={
        "display_name": "Adarsh", "preferred_language": "en", "tts_voice": "warm",
    }, headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["display_name"] == "Adarsh"
    assert body["preferred_language"] == "en"
    assert body["tts_voice"] == "warm"
    assert body["onboarded"] is True


def test_onboard_requires_authentication(client):
    resp = client.post("/api/auth/onboard", json={
        "display_name": "Adarsh", "preferred_language": "en",
    })
    assert resp.status_code == 401


def test_onboard_rejects_empty_name(client):
    token, _ = login(client)
    headers = {"Authorization": f"Bearer {token}"}
    resp = client.post("/api/auth/onboard", json={
        "display_name": "   ", "preferred_language": "en",
    }, headers=headers)
    assert resp.status_code == 400


def test_me_reflects_onboarding_after_it_happens(client):
    token, _ = login(client)
    headers = {"Authorization": f"Bearer {token}"}
    client.post("/api/auth/onboard", json={
        "display_name": "Adarsh", "preferred_language": "hi",
    }, headers=headers)

    me = client.get("/api/auth/me", headers=headers).json()
    assert me["onboarded"] is True
    assert me["preferred_language"] == "hi"


def test_update_profile_changes_display_name(client):
    token, _ = login(client)
    headers = {"Authorization": f"Bearer {token}"}
    client.post("/api/auth/onboard", json={"display_name": "Adarsh", "preferred_language": "en"}, headers=headers)

    resp = client.patch("/api/auth/profile", json={"display_name": "New Name"}, headers=headers)
    assert resp.status_code == 200
    assert resp.json()["display_name"] == "New Name"


def test_update_profile_rejects_empty_name(client):
    token, _ = login(client)
    headers = {"Authorization": f"Bearer {token}"}
    resp = client.patch("/api/auth/profile", json={"display_name": "  "}, headers=headers)
    assert resp.status_code == 400


def test_update_preferences_changes_language(client):
    token, _ = login(client)
    headers = {"Authorization": f"Bearer {token}"}
    client.post("/api/auth/onboard", json={"display_name": "Adarsh", "preferred_language": "en"}, headers=headers)

    resp = client.patch("/api/auth/preferences", json={"preferred_language": "fr"}, headers=headers)
    assert resp.status_code == 200
    assert resp.json()["preferred_language"] == "fr"


def test_update_preferences_changes_tts_voice_independently(client):
    token, _ = login(client)
    headers = {"Authorization": f"Bearer {token}"}
    client.post("/api/auth/onboard", json={
        "display_name": "Adarsh", "preferred_language": "en", "tts_voice": "warm",
    }, headers=headers)

    resp = client.patch("/api/auth/preferences", json={"tts_voice": "bright"}, headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["tts_voice"] == "bright"
    assert body["preferred_language"] == "en"  # untouched


def test_update_preferences_requires_authentication(client):
    resp = client.patch("/api/auth/preferences", json={"preferred_language": "fr"})
    assert resp.status_code == 401
