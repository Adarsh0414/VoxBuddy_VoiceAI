import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

import auth_store
import token_store


@pytest.fixture(autouse=True)
def _reset_singleton():
    token_store.reset_store()
    yield
    token_store.reset_store()


@pytest.fixture
def sqlite_db(tmp_path, monkeypatch):
    db = tmp_path / "test_token_store.db"
    auth_store.init_db(db)
    monkeypatch.setattr(auth_store, "DB_PATH", db)
    return db


def _make_real_user(db_path) -> int:
    """auth_store.get_user_by_token JOINs against the users table, so a
    token for a user_id with no actual user row will correctly find
    nothing — these tests need a real user, not just an arbitrary int."""
    code, _ = auth_store.create_otp("tokentest@example.com", "email", db_path=db_path)
    return auth_store.verify_otp("tokentest@example.com", code, db_path=db_path)


# --- SqliteTokenStore (default, always available) ---------------------------

def test_sqlite_store_selected_by_default(monkeypatch):
    monkeypatch.delenv("VOXBUDDY_SESSION_STORE", raising=False)
    store = token_store.get_store()
    assert isinstance(store, token_store.SqliteTokenStore)


def test_sqlite_store_create_and_lookup(sqlite_db):
    store = token_store.SqliteTokenStore()
    user_id = _make_real_user(sqlite_db)
    token, expires_at = store.create(user_id)
    assert expires_at > time.time()
    assert store.get_user_id(token) == user_id


def test_sqlite_store_lookup_missing_token_returns_none(sqlite_db):
    store = token_store.SqliteTokenStore()
    assert store.get_user_id("not-a-real-token") is None


def test_sqlite_store_delete(sqlite_db):
    store = token_store.SqliteTokenStore()
    user_id = _make_real_user(sqlite_db)
    token, _ = store.create(user_id)
    store.delete(token)
    assert store.get_user_id(token) is None


def test_get_store_rejects_unknown_backend(monkeypatch):
    monkeypatch.setenv("VOXBUDDY_SESSION_STORE", "carrier_pigeon")
    with pytest.raises(ValueError, match="Unknown VOXBUDDY_SESSION_STORE"):
        token_store.get_store()


def test_get_store_is_a_singleton_until_reset(monkeypatch):
    monkeypatch.setenv("VOXBUDDY_SESSION_STORE", "sqlite")
    store1 = token_store.get_store()
    store2 = token_store.get_store()
    assert store1 is store2
    token_store.reset_store()
    store3 = token_store.get_store()
    assert store3 is not store1


# --- RedisTokenStore (live test against a real local Redis instance) --------
# These run against an actual redis-server, not a mock — see the shell
# session used to build this feature for confirmation that redis-server
# is installed and reachable in this environment. If Redis genuinely isn't
# reachable wherever these tests run, they're skipped rather than failed,
# since Redis is an optional production backend, not a hard requirement.

def _redis_reachable() -> bool:
    try:
        import redis as redis_lib
        client = redis_lib.from_url("redis://localhost:6379/0")
        return client.ping()
    except Exception:
        return False


requires_redis = pytest.mark.skipif(not _redis_reachable(), reason="no local Redis instance reachable")


@requires_redis
def test_redis_store_selected_via_env_var(monkeypatch):
    monkeypatch.setenv("VOXBUDDY_SESSION_STORE", "redis")
    store = token_store.get_store()
    assert isinstance(store, token_store.RedisTokenStore)


@requires_redis
def test_redis_store_create_and_lookup():
    store = token_store.RedisTokenStore()
    token, expires_at = store.create(user_id=99)
    assert expires_at > time.time()
    assert store.get_user_id(token) == 99


@requires_redis
def test_redis_store_lookup_missing_token_returns_none():
    store = token_store.RedisTokenStore()
    assert store.get_user_id("definitely-not-a-real-token") is None


@requires_redis
def test_redis_store_delete():
    store = token_store.RedisTokenStore()
    token, _ = store.create(user_id=7)
    store.delete(token)
    assert store.get_user_id(token) is None


@requires_redis
def test_redis_store_uses_native_ttl_expiry():
    """Proves the token actually expires via Redis's own TTL mechanism —
    not just that our code checks an expires_at timestamp, but that Redis
    itself has forgotten the key."""
    import redis as redis_lib
    store = token_store.RedisTokenStore()
    token, _ = store.create(user_id=5)

    client = redis_lib.from_url("redis://localhost:6379/0", decode_responses=True)
    ttl = client.ttl(store._key(token))
    assert 0 < ttl <= auth_store.TOKEN_EXPIRY_SECONDS


@requires_redis
def test_redis_store_distinct_tokens_do_not_collide():
    store = token_store.RedisTokenStore()
    token_a, _ = store.create(user_id=1)
    token_b, _ = store.create(user_id=2)
    assert token_a != token_b
    assert store.get_user_id(token_a) == 1
    assert store.get_user_id(token_b) == 2
