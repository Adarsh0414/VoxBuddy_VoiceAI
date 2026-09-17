import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

import auth_store as store


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "test_auth.db"
    store.init_db(path)
    return path


# --- identifier normalization ------------------------------------------------

def test_normalize_email_lowercases_and_trims():
    assert store.normalize_identifier("  Adarsh@Example.COM ", "email") == "adarsh@example.com"


def test_normalize_email_rejects_invalid():
    with pytest.raises(store.InvalidIdentifier):
        store.normalize_identifier("not-an-email", "email")


def test_normalize_phone_strips_spaces_and_dashes():
    assert store.normalize_identifier("+1 415-555-1234", "sms") == "+14155551234"


def test_normalize_phone_rejects_missing_country_code():
    with pytest.raises(store.InvalidIdentifier):
        store.normalize_identifier("4155551234", "sms")  # no leading +


def test_normalize_phone_rejects_too_short():
    with pytest.raises(store.InvalidIdentifier):
        store.normalize_identifier("+1234", "sms")


def test_normalize_unknown_channel_rejected():
    with pytest.raises(store.InvalidIdentifier):
        store.normalize_identifier("a@b.com", "carrier_pigeon")


# --- OTP create/verify -------------------------------------------------------

def test_create_and_verify_otp_succeeds(db_path):
    code, expires_at = store.create_otp("a@b.com", "email", db_path=db_path)
    assert len(code) == store.OTP_LENGTH
    assert expires_at > time.time()

    user_id = store.verify_otp("a@b.com", code, db_path=db_path)
    assert isinstance(user_id, int)

    user = store.get_user(user_id, db_path=db_path)
    assert user.email == "a@b.com"
    assert user.phone is None


def test_verify_with_wrong_code_raises_incorrect(db_path):
    store.create_otp("a@b.com", "email", db_path=db_path)
    with pytest.raises(store.OtpIncorrect) as exc_info:
        store.verify_otp("a@b.com", "000000", db_path=db_path)
    assert exc_info.value.attempts_remaining == store.MAX_VERIFY_ATTEMPTS - 1


def test_verify_with_no_pending_otp_raises_not_found(db_path):
    with pytest.raises(store.OtpNotFound):
        store.verify_otp("nobody@example.com", "123456", db_path=db_path)


def test_verify_expired_otp_raises_expired(db_path, monkeypatch):
    code, _ = store.create_otp("a@b.com", "email", db_path=db_path)
    # Fast-forward time past expiry by monkeypatching time.time within the
    # module under test.
    real_time = time.time
    monkeypatch.setattr(store.time, "time", lambda: real_time() + store.OTP_EXPIRY_SECONDS + 1)
    with pytest.raises(store.OtpExpired):
        store.verify_otp("a@b.com", code, db_path=db_path)


def test_too_many_wrong_attempts_locks_out(db_path):
    store.create_otp("a@b.com", "email", db_path=db_path)
    for _ in range(store.MAX_VERIFY_ATTEMPTS):
        with pytest.raises(store.OtpIncorrect):
            store.verify_otp("a@b.com", "000000", db_path=db_path)
    with pytest.raises(store.OtpTooManyAttempts):
        store.verify_otp("a@b.com", "000000", db_path=db_path)


def test_otp_is_single_use(db_path):
    code, _ = store.create_otp("a@b.com", "email", db_path=db_path)
    store.verify_otp("a@b.com", code, db_path=db_path)
    with pytest.raises(store.OtpNotFound):
        store.verify_otp("a@b.com", code, db_path=db_path)


def test_resend_cooldown_blocks_rapid_requests(db_path):
    store.create_otp("a@b.com", "email", db_path=db_path)
    with pytest.raises(store.CooldownActive) as exc_info:
        store.create_otp("a@b.com", "email", db_path=db_path)
    assert 0 < exc_info.value.retry_after_seconds <= store.RESEND_COOLDOWN_SECONDS


def test_second_login_reuses_existing_user(db_path, monkeypatch):
    code1, _ = store.create_otp("a@b.com", "email", db_path=db_path)
    user_id_1 = store.verify_otp("a@b.com", code1, db_path=db_path)

    # Advance past the resend cooldown so the second create_otp isn't
    # itself testing CooldownActive (covered separately).
    real_time = time.time
    monkeypatch.setattr(store.time, "time", lambda: real_time() + store.RESEND_COOLDOWN_SECONDS + 1)

    code2, _ = store.create_otp("a@b.com", "email", db_path=db_path)
    user_id_2 = store.verify_otp("a@b.com", code2, db_path=db_path)

    assert user_id_1 == user_id_2


def test_email_and_phone_create_distinct_users(db_path):
    code1, _ = store.create_otp("a@b.com", "email", db_path=db_path)
    user_id_email = store.verify_otp("a@b.com", code1, db_path=db_path)

    code2, _ = store.create_otp("+14155551234", "sms", db_path=db_path)
    user_id_phone = store.verify_otp("+14155551234", code2, db_path=db_path)

    assert user_id_email != user_id_phone


# --- sessions -----------------------------------------------------------------

def test_create_and_resolve_token(db_path):
    code, _ = store.create_otp("a@b.com", "email", db_path=db_path)
    user_id = store.verify_otp("a@b.com", code, db_path=db_path)

    token, expires_at = store.create_token(user_id, db_path=db_path)
    assert expires_at > time.time()

    user = store.get_user_by_token(token, db_path=db_path)
    assert user is not None
    assert user.id == user_id


def test_invalid_token_resolves_to_none(db_path):
    assert store.get_user_by_token("not-a-real-token", db_path=db_path) is None


def test_expired_token_resolves_to_none(db_path, monkeypatch):
    code, _ = store.create_otp("a@b.com", "email", db_path=db_path)
    user_id = store.verify_otp("a@b.com", code, db_path=db_path)
    token, _ = store.create_token(user_id, db_path=db_path)

    real_time = time.time
    monkeypatch.setattr(store.time, "time", lambda: real_time() + store.TOKEN_EXPIRY_SECONDS + 1)
    assert store.get_user_by_token(token, db_path=db_path) is None


def test_logout_deletes_token(db_path):
    code, _ = store.create_otp("a@b.com", "email", db_path=db_path)
    user_id = store.verify_otp("a@b.com", code, db_path=db_path)
    token, _ = store.create_token(user_id, db_path=db_path)

    store.delete_token(token, db_path=db_path)
    assert store.get_user_by_token(token, db_path=db_path) is None


def test_update_display_name(db_path):
    code, _ = store.create_otp("a@b.com", "email", db_path=db_path)
    user_id = store.verify_otp("a@b.com", code, db_path=db_path)

    store.update_display_name(user_id, "Adarsh", db_path=db_path)
    user = store.get_user(user_id, db_path=db_path)
    assert user.display_name == "Adarsh"


def test_new_user_has_no_preferences_by_default(db_path):
    code, _ = store.create_otp("a@b.com", "email", db_path=db_path)
    user_id = store.verify_otp("a@b.com", code, db_path=db_path)
    user = store.get_user(user_id, db_path=db_path)
    assert user.preferred_language is None
    assert user.tts_voice is None
    assert user.onboarded_at is None


def test_complete_onboarding_sets_all_fields_at_once(db_path):
    code, _ = store.create_otp("a@b.com", "email", db_path=db_path)
    user_id = store.verify_otp("a@b.com", code, db_path=db_path)

    store.complete_onboarding(user_id, "Adarsh", "hi", "bright", db_path=db_path)
    user = store.get_user(user_id, db_path=db_path)
    assert user.display_name == "Adarsh"
    assert user.preferred_language == "hi"
    assert user.tts_voice == "bright"
    assert user.onboarded_at is not None


def test_update_preferences_only_touches_passed_fields(db_path):
    code, _ = store.create_otp("a@b.com", "email", db_path=db_path)
    user_id = store.verify_otp("a@b.com", code, db_path=db_path)
    store.complete_onboarding(user_id, "Adarsh", "hi", "warm", db_path=db_path)

    # Change only tts_voice — language must survive untouched.
    store.update_preferences(user_id, tts_voice="bright", db_path=db_path)
    user = store.get_user(user_id, db_path=db_path)
    assert user.tts_voice == "bright"
    assert user.preferred_language == "hi"

    # Change only language — tts_voice must survive untouched.
    store.update_preferences(user_id, preferred_language="fr", db_path=db_path)
    user = store.get_user(user_id, db_path=db_path)
    assert user.preferred_language == "fr"
    assert user.tts_voice == "bright"


def test_migration_adds_preference_columns_to_existing_users_table(tmp_path):
    """Simulates a database created before preferences existed."""
    import sqlite3
    old_db = tmp_path / "old_users_schema.db"
    conn = sqlite3.connect(str(old_db))
    conn.executescript("""
        CREATE TABLE users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE,
            phone TEXT UNIQUE,
            display_name TEXT,
            created_at REAL NOT NULL
        );
    """)
    conn.commit()
    conn.close()

    store.init_db(old_db)

    conn = sqlite3.connect(str(old_db))
    columns = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
    conn.close()
    assert {"preferred_language", "tts_voice", "onboarded_at"} <= columns
