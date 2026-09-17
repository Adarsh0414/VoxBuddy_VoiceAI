import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch, tmp_path):
    db_path = tmp_path / "test_google_auth.db"
    monkeypatch.setenv("VOXBUDDY_DB_PATH", str(db_path))
    monkeypatch.setenv("VOXBUDDY_ASR_PROVIDER", "mock")
    import app as app_module
    app_module.sessions.clear()
    import auth_store
    auth_store.DB_PATH = db_path
    auth_store.init_db(db_path)
    return TestClient(app_module.app)


def test_auth_config_reports_unconfigured_without_client_id(client, monkeypatch):
    monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
    res = client.get("/api/auth/config")
    assert res.json() == {"google_client_id": ""}


def test_auth_config_reports_client_id_when_set(client, monkeypatch):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "fake-client-id.apps.googleusercontent.com")
    res = client.get("/api/auth/config")
    assert res.json() == {"google_client_id": "fake-client-id.apps.googleusercontent.com"}


def test_google_sign_in_rejected_when_not_configured(client, monkeypatch):
    monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
    res = client.post("/api/auth/google", json={"id_token": "whatever"})
    assert res.status_code == 503


def test_google_sign_in_creates_new_user_from_verified_token(client, monkeypatch):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "fake-client-id.apps.googleusercontent.com")

    from google.oauth2 import id_token as google_id_token

    def fake_verify(token, request, audience):
        assert token == "real-looking-jwt-from-frontend"
        assert audience == "fake-client-id.apps.googleusercontent.com"
        return {"email": "newuser@gmail.com", "email_verified": True, "name": "Priya Sharma"}

    monkeypatch.setattr(google_id_token, "verify_oauth2_token", fake_verify)

    res = client.post("/api/auth/google", json={"id_token": "real-looking-jwt-from-frontend"})
    assert res.status_code == 200
    body = res.json()
    assert body["user"]["email"] == "newuser@gmail.com"
    assert body["user"]["display_name"] == "Priya Sharma"
    assert body["user"]["onboarded"] is False
    assert body["token"]

    # The returned token should actually work for an authenticated call
    me = client.get("/api/auth/me", headers={"Authorization": f"Bearer {body['token']}"})
    assert me.status_code == 200
    assert me.json()["email"] == "newuser@gmail.com"


def test_google_sign_in_rejects_unverified_email(client, monkeypatch):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "fake-client-id.apps.googleusercontent.com")

    from google.oauth2 import id_token as google_id_token

    def fake_verify(token, request, audience):
        return {"email": "sneaky@gmail.com", "email_verified": False, "name": "Someone"}

    monkeypatch.setattr(google_id_token, "verify_oauth2_token", fake_verify)

    res = client.post("/api/auth/google", json={"id_token": "some-token"})
    assert res.status_code == 401
    assert "not verified" in res.json()["detail"].lower()


def test_google_sign_in_rejects_invalid_token(client, monkeypatch):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "fake-client-id.apps.googleusercontent.com")

    from google.oauth2 import id_token as google_id_token

    def fake_verify(token, request, audience):
        raise ValueError("Token used too early")

    monkeypatch.setattr(google_id_token, "verify_oauth2_token", fake_verify)

    res = client.post("/api/auth/google", json={"id_token": "expired-or-forged"})
    assert res.status_code == 401
    assert "invalid google sign-in token" in res.json()["detail"].lower()


def test_google_sign_in_reuses_existing_account_matched_by_email(client, monkeypatch):
    # Someone who already has an account via email OTP, then later uses
    # "Sign in with Google" with that same email — should land in the
    # SAME account, not create a duplicate. Set up the existing account
    # directly via auth_store (not the HTTP layer) to avoid the resend
    # cooldown entirely — this test is about the Google path's
    # find-or-create behavior, not the OTP request flow itself.
    import auth_store
    code, _ = auth_store.create_otp("shared@gmail.com", "email", db_path=auth_store.DB_PATH)
    original_user_id = auth_store.verify_otp("shared@gmail.com", code, db_path=auth_store.DB_PATH)
    auth_store.update_display_name(original_user_id, "Original Name", db_path=auth_store.DB_PATH)

    monkeypatch.setenv("GOOGLE_CLIENT_ID", "fake-client-id.apps.googleusercontent.com")
    from google.oauth2 import id_token as google_id_token

    def fake_verify(token, request, audience):
        return {"email": "shared@gmail.com", "email_verified": True, "name": "Google Profile Name"}

    monkeypatch.setattr(google_id_token, "verify_oauth2_token", fake_verify)

    res = client.post("/api/auth/google", json={"id_token": "some-token"})
    assert res.status_code == 200
    body = res.json()
    assert body["user"]["id"] == original_user_id
    # Existing display name should NOT be overwritten by the Google profile name
    assert body["user"]["display_name"] == "Original Name"
