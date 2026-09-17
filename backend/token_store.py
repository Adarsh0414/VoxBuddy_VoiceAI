"""
Token storage abstraction for bearer session tokens.

Two implementations:
  - SqliteTokenStore (default) — delegates to auth_store.py's existing,
    tested SQLite functions. Zero config, works everywhere, what every
    test and the local dev flow uses.
  - RedisTokenStore — real Redis-backed storage using native TTL
    (SETEX), selected via VOXBUDDY_SESSION_STORE=redis. This is what lets
    auth tokens be shared across multiple backend processes/machines
    without every instance needing to hit the same SQLite file.

IMPORTANT ARCHITECTURAL NOTE — what this deliberately does NOT cover:
The PRD's Conversation Session Service (§9) envisioned Redis for the LIVE
CONVERSATION state (CIE decisions, speaker embeddings, turn history
mid-call) to support horizontal scaling. That's a materially different
problem from token storage: a live conversation is bound to one open
WebSocket connection on one specific server process, and the CIE's
ConversationState holds live Python objects (sets, custom dataclasses),
not simple key-value data that serializes cleanly. The standard, correct
production fix for THAT is sticky sessions at the load balancer (route a
given session_id's WebSocket traffic to the same backend instance for the
connection's lifetime) — not moving conversation state into Redis, which
would mean serializing/deserializing the entire CIE state on every single
utterance for no real benefit. This module intentionally scopes
"Redis-backed session state" to what Redis actually fits well here: bearer
auth tokens (ephemeral, fast-lookup, naturally TTL'd). The `sessions` dict
in app.py (live SessionManager objects, one per open WebSocket) is
unaffected by this — that's the sticky-session-appropriate part, and
stays in-process by design, not as an oversight.

User PROFILE data (email/phone/display_name) always stays in SQLite
regardless of which token store is active — that's persistent business
data, a different concern from ephemeral session tokens. A RedisTokenStore
only ever maps token -> user_id; app.py hydrates the full user via
auth_store.get_user() afterward either way.
"""

from __future__ import annotations

import os
import secrets
import time
from typing import Protocol

import auth_store


class TokenStore(Protocol):
    def create(self, user_id: int) -> tuple[str, float]: ...
    def get_user_id(self, token: str) -> int | None: ...
    def delete(self, token: str) -> None: ...


class SqliteTokenStore:
    """Default, zero-config. Delegates to auth_store.py's existing,
    tested SQLite implementation rather than duplicating it."""

    def create(self, user_id: int) -> tuple[str, float]:
        return auth_store.create_token(user_id)

    def get_user_id(self, token: str) -> int | None:
        user = auth_store.get_user_by_token(token)
        return user.id if user else None

    def delete(self, token: str) -> None:
        auth_store.delete_token(token)


class RedisTokenStore:
    """Real Redis-backed token storage using native key expiry (SETEX) —
    no separate cleanup job needed for expired tokens, Redis handles it.
    Requires a reachable Redis instance (VOXBUDDY_REDIS_URL, default
    redis://localhost:6379/0)."""

    def __init__(self, redis_url: str | None = None):
        import redis as redis_lib
        self.redis_url = redis_url or os.environ.get("VOXBUDDY_REDIS_URL", "redis://localhost:6379/0")
        self._client = redis_lib.from_url(self.redis_url, decode_responses=True)

    @staticmethod
    def _key(token: str) -> str:
        return f"voxbuddy:token:{token}"

    def create(self, user_id: int) -> tuple[str, float]:
        token = secrets.token_urlsafe(32)
        ttl = auth_store.TOKEN_EXPIRY_SECONDS
        self._client.set(self._key(token), str(user_id), ex=ttl)
        return token, time.time() + ttl

    def get_user_id(self, token: str) -> int | None:
        value = self._client.get(self._key(token))
        return int(value) if value is not None else None

    def delete(self, token: str) -> None:
        self._client.delete(self._key(token))


_singleton_store: TokenStore | None = None


def get_store() -> TokenStore:
    global _singleton_store
    if _singleton_store is not None:
        return _singleton_store

    backend = os.environ.get("VOXBUDDY_SESSION_STORE", "sqlite").lower()
    if backend == "redis":
        _singleton_store = RedisTokenStore()
    elif backend == "sqlite":
        _singleton_store = SqliteTokenStore()
    else:
        raise ValueError(
            f"Unknown VOXBUDDY_SESSION_STORE='{backend}'. Valid options: 'sqlite', 'redis'."
        )
    return _singleton_store


def reset_store() -> None:
    """Test-only: clears the cached singleton so a subsequent get_store()
    call re-reads VOXBUDDY_SESSION_STORE (e.g. tests switching between
    sqlite/redis backends need this — otherwise the first backend picked
    in a test run would stick for the rest of the process)."""
    global _singleton_store
    _singleton_store = None
