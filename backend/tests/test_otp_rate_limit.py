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
    test_db = tmp_path / "test_otp_rate_limit.db"
    monkeypatch.setattr(persistence, "DB_PATH", test_db)
    monkeypatch.setattr(auth_store, "DB_PATH", test_db)
    persistence.init_db(test_db)
    auth_store.init_db(test_db)
    token_store.reset_store()
    monkeypatch.setenv("VOXBUDDY_AUTH_DEV_MODE", "1")
    monkeypatch.setenv("VOXBUDDY_OTP_PROVIDER", "console")
    # Channel-specific env vars take priority over the generic one above
    # (see otp_providers.get_provider()) — if a real provider is set in
    # a local .env for either channel, it would otherwise still win and
    # attempt a real network send during this test.
    monkeypatch.setenv("VOXBUDDY_OTP_EMAIL_PROVIDER", "console")
    monkeypatch.setenv("VOXBUDDY_OTP_SMS_PROVIDER", "console")
    monkeypatch.setenv("VOXBUDDY_SESSION_STORE", "sqlite")

    import app as app_module
    monkeypatch.setattr(app_module.persistence, "DB_PATH", test_db)
    monkeypatch.setattr(app_module.auth_store, "DB_PATH", test_db)
    app_module.reset_otp_rate_limit()

    yield TestClient(app_module.app)
    token_store.reset_store()


def test_otp_requests_within_limit_all_succeed(client):
    # Each call uses a different identifier — this is exactly the
    # abuse pattern the per-IP limiter (not auth_store's per-identifier
    # cooldown) is meant to catch, so it must still trip even though no
    # single identifier is reused.
    for i in range(8):
        resp = client.post("/api/auth/request-otp", json={
            "identifier": f"user{i}@example.com", "channel": "email",
        })
        assert resp.status_code == 200, resp.json()


def test_otp_requests_beyond_limit_get_429(client):
    for i in range(8):
        client.post("/api/auth/request-otp", json={
            "identifier": f"user{i}@example.com", "channel": "email",
        })
    resp = client.post("/api/auth/request-otp", json={
        "identifier": "one-too-many@example.com", "channel": "email",
    })
    assert resp.status_code == 429


def test_rate_limit_is_per_ip_via_x_forwarded_for(client):
    # Two distinct "clients" (simulated via X-Forwarded-For, the header
    # Render's proxy sets) each get their own independent budget.
    for i in range(8):
        resp = client.post(
            "/api/auth/request-otp",
            json={"identifier": f"a{i}@example.com", "channel": "email"},
            headers={"X-Forwarded-For": "1.1.1.1"},
        )
        assert resp.status_code == 200
    resp = client.post(
        "/api/auth/request-otp",
        json={"identifier": "fresh@example.com", "channel": "email"},
        headers={"X-Forwarded-For": "2.2.2.2"},
    )
    assert resp.status_code == 200
