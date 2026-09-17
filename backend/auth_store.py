"""
Authentication storage: users, OTP codes, and session tokens.

Deliberately mirrors persistence.py's proven pattern (stdlib sqlite3, no
ORM, db_path resolved dynamically at call time via a None-default rather
than bound at function-definition time — see the comment in
persistence.py's _connect for why that matters for test isolation).

Design choices worth knowing about:
  - A user can sign in with EITHER email or phone (or both, over time) —
    both columns are nullable+unique, not a forced single identifier.
  - OTP codes are stored as a salted hash, never plaintext. The plaintext
    is only ever held in memory for the few milliseconds between
    generating it and handing it to the send provider.
  - Sessions are opaque bearer tokens (a random 32-byte urlsafe string),
    not JWTs — simpler, no extra dependency, and trivially revocable by
    just deleting the row (logout).
  - This is a Phase 2/3-appropriate implementation: real, working, tested
    — but a production deployment would want stronger rate limiting than
    the in-DB cooldown check here, and probably a managed auth provider
    instead of hand-rolled OTP storage at real scale.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "voxbuddy.db"

OTP_LENGTH = 6
OTP_EXPIRY_SECONDS = 5 * 60
RESEND_COOLDOWN_SECONDS = 30
MAX_VERIFY_ATTEMPTS = 5
TOKEN_EXPIRY_SECONDS = 30 * 24 * 60 * 60  # 30 days

# Pepper for OTP hashing — a fixed per-deployment secret mixed into every
# hash so a leaked DB alone isn't enough to forge codes. Set
# VOXBUDDY_OTP_PEPPER in production; the fallback here is fine for local
# dev (same spirit as the DEV OTP provider — zero config to get started).
_PEPPER = os.environ.get("VOXBUDDY_OTP_PEPPER", "dev-only-pepper-change-in-prod")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT UNIQUE,
    phone TEXT UNIQUE,
    display_name TEXT,
    preferred_language TEXT,
    tts_voice TEXT,
    onboarded_at REAL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS otp_codes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    identifier TEXT NOT NULL,
    channel TEXT NOT NULL,
    code_hash TEXT NOT NULL,
    purpose TEXT NOT NULL DEFAULT 'login',
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    consumed_at REAL
);

CREATE TABLE IF NOT EXISTS auth_tokens (
    token TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL
);
"""


@contextmanager
def _connect(db_path: Path | None = None):
    # See persistence.py's _connect for why db_path is resolved here rather
    # than as a bound default argument.
    #
    # IMPORTANT: commit happens in `finally`, not after `yield`. If a
    # caller raises inside the `with` block (e.g. verify_otp raising
    # OtpIncorrect after already executing an "attempts += 1" UPDATE), a
    # commit placed after `yield` would never run — the exception resumes
    # at the yield point and skips straight to `finally`, silently
    # discarding that UPDATE. That's a real bug this fixed: the
    # lockout-after-N-attempts protection was being reset on every wrong
    # guess because the attempt counter never actually persisted. Business
    # -logic exceptions (wrong code, expired, etc.) should still commit
    # whatever was legitimately written before they were raised.
    resolved_path = db_path if db_path is not None else DB_PATH
    conn = sqlite3.connect(str(resolved_path))
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.commit()
        conn.close()


def init_db(db_path: Path | None = None) -> None:
    with _connect(db_path) as conn:
        conn.executescript(SCHEMA)
        # Migration for databases created before preferences existed —
        # same pattern as persistence.py's user_id migration.
        existing_columns = {row["name"] for row in conn.execute("PRAGMA table_info(users)")}
        for column, coltype in [("preferred_language", "TEXT"), ("tts_voice", "TEXT"),
                                  ("onboarded_at", "REAL")]:
            if column not in existing_columns:
                conn.execute(f"ALTER TABLE users ADD COLUMN {column} {coltype}")


# --- identifier normalization / validation ---------------------------------

EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
# Deliberately light validation, not full E.164/libphonenumber: a leading
# '+', country code, then 6-14 more digits. Good enough to catch obvious
# typos without adding a phone-number-parsing dependency for a Phase 1/2
# system — revisit if international edge cases (extensions, specific
# national formats) turn out to matter.
PHONE_RE = re.compile(r"^\+[1-9]\d{6,14}$")


class InvalidIdentifier(ValueError):
    pass


def normalize_identifier(identifier: str, channel: str) -> str:
    identifier = (identifier or "").strip()
    if channel == "email":
        identifier = identifier.lower()
        if not EMAIL_RE.match(identifier):
            raise InvalidIdentifier(f"'{identifier}' is not a valid email address")
        return identifier
    if channel == "sms":
        identifier = identifier.replace(" ", "").replace("-", "")
        if not PHONE_RE.match(identifier):
            raise InvalidIdentifier(
                f"'{identifier}' is not a valid phone number — use international "
                f"format starting with + (e.g. +14155551234)"
            )
        return identifier
    raise InvalidIdentifier(f"Unknown channel '{channel}', expected 'email' or 'sms'")


# --- OTP lifecycle -----------------------------------------------------------

class CooldownActive(Exception):
    def __init__(self, retry_after_seconds: float):
        self.retry_after_seconds = retry_after_seconds
        super().__init__(f"Please wait {retry_after_seconds:.0f}s before requesting another code.")


class OtpError(Exception):
    """Base for verify-time failures — see the specific subclasses below,
    which app.py maps to distinct, honest error messages rather than a
    single generic 'invalid' for every failure mode."""


class OtpNotFound(OtpError):
    pass


class OtpExpired(OtpError):
    pass


class OtpTooManyAttempts(OtpError):
    pass


class OtpIncorrect(OtpError):
    def __init__(self, attempts_remaining: int):
        self.attempts_remaining = attempts_remaining
        super().__init__(f"Incorrect code, {attempts_remaining} attempt(s) remaining")


def _hash_code(identifier: str, code: str) -> str:
    payload = f"{identifier}:{code}:{_PEPPER}".encode()
    return hashlib.sha256(payload).hexdigest()


def _generate_code() -> str:
    return "".join(str(secrets.randbelow(10)) for _ in range(OTP_LENGTH))


def create_otp(identifier: str, channel: str, purpose: str = "login",
                db_path: Path | None = None) -> tuple[str, float]:
    """Generates and stores a new OTP for this identifier. Returns
    (plaintext_code, expires_at) — the caller is responsible for actually
    sending the code (see otp_providers.py) and MUST NOT persist or log the
    plaintext anywhere beyond that.

    Raises CooldownActive if the identifier requested a code too recently.
    """
    now = time.time()
    with _connect(db_path) as conn:
        recent = conn.execute(
            "SELECT created_at FROM otp_codes WHERE identifier = ? "
            "ORDER BY created_at DESC LIMIT 1", (identifier,)
        ).fetchone()
        if recent and (now - recent["created_at"]) < RESEND_COOLDOWN_SECONDS:
            retry_after = RESEND_COOLDOWN_SECONDS - (now - recent["created_at"])
            raise CooldownActive(retry_after)

        code = _generate_code()
        expires_at = now + OTP_EXPIRY_SECONDS
        conn.execute(
            "INSERT INTO otp_codes (identifier, channel, code_hash, purpose, "
            "attempts, created_at, expires_at) VALUES (?, ?, ?, ?, 0, ?, ?)",
            (identifier, channel, _hash_code(identifier, code), purpose, now, expires_at),
        )
    return code, expires_at


def verify_otp(identifier: str, code: str, db_path: Path | None = None) -> int:
    """Verifies a code against the most recent unconsumed OTP for this
    identifier, creating the user if this is their first successful
    verification. Returns the user_id on success.

    Raises OtpNotFound / OtpExpired / OtpTooManyAttempts / OtpIncorrect on
    failure — deliberately distinct exceptions so the API layer can return
    honest, specific errors instead of a single opaque 'invalid code'.
    """
    now = time.time()
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM otp_codes WHERE identifier = ? AND consumed_at IS NULL "
            "ORDER BY created_at DESC LIMIT 1", (identifier,)
        ).fetchone()
        if not row:
            raise OtpNotFound(f"No pending code for {identifier} — request a new one")

        if now > row["expires_at"]:
            raise OtpExpired("That code has expired — request a new one")

        if row["attempts"] >= MAX_VERIFY_ATTEMPTS:
            raise OtpTooManyAttempts("Too many incorrect attempts — request a new code")

        if not hmac.compare_digest(row["code_hash"], _hash_code(identifier, code)):
            conn.execute("UPDATE otp_codes SET attempts = attempts + 1 WHERE id = ?", (row["id"],))
            remaining = MAX_VERIFY_ATTEMPTS - (row["attempts"] + 1)
            raise OtpIncorrect(max(remaining, 0))

        conn.execute("UPDATE otp_codes SET consumed_at = ? WHERE id = ?", (now, row["id"]))

        channel = row["channel"]
        column = "email" if channel == "email" else "phone"
        user_row = conn.execute(f"SELECT id FROM users WHERE {column} = ?", (identifier,)).fetchone()
        if user_row:
            return user_row["id"]

        cur = conn.execute(
            f"INSERT INTO users ({column}, created_at) VALUES (?, ?)",
            (identifier, now),
        )
        return cur.lastrowid


# --- sessions ----------------------------------------------------------------

@dataclass
class AuthUser:
    id: int
    email: str | None
    phone: str | None
    display_name: str | None
    preferred_language: str | None
    tts_voice: str | None
    onboarded_at: float | None
    created_at: float


def _row_to_user(row) -> AuthUser:
    return AuthUser(
        id=row["id"], email=row["email"], phone=row["phone"],
        display_name=row["display_name"], preferred_language=row["preferred_language"],
        tts_voice=row["tts_voice"], onboarded_at=row["onboarded_at"],
        created_at=row["created_at"],
    )


def create_token(user_id: int, db_path: Path | None = None) -> tuple[str, float]:
    token = secrets.token_urlsafe(32)
    now = time.time()
    expires_at = now + TOKEN_EXPIRY_SECONDS
    with _connect(db_path) as conn:
        conn.execute(
            "INSERT INTO auth_tokens (token, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
            (token, user_id, now, expires_at),
        )
    return token, expires_at


def get_user_by_token(token: str, db_path: Path | None = None) -> AuthUser | None:
    now = time.time()
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT u.* FROM auth_tokens t JOIN users u ON u.id = t.user_id "
            "WHERE t.token = ? AND t.expires_at > ?", (token, now)
        ).fetchone()
        return _row_to_user(row) if row else None


def delete_token(token: str, db_path: Path | None = None) -> None:
    with _connect(db_path) as conn:
        conn.execute("DELETE FROM auth_tokens WHERE token = ?", (token,))


def delete_user(user_id: int, db_path: Path | None = None) -> None:
    """Full account deletion (Profile > Delete account). Removes the user
    row itself plus every auth-related trace of them: all their session
    tokens (so any other signed-in device is immediately logged out too,
    not just this one) and any pending OTP codes under their email/phone.
    Does NOT touch conversations/turns — that's persistence.delete_all(),
    called separately by the /api/auth/account DELETE endpoint, since
    that lives in a different module with its own db connection.
    Signing back in afterwards creates a brand-new user row (new id, no
    memory of the old one) rather than reactivating anything — this is a
    real, one-way deletion, not a soft-delete/deactivation flag.
    """
    with _connect(db_path) as conn:
        row = conn.execute("SELECT email, phone FROM users WHERE id = ?", (user_id,)).fetchone()
        conn.execute("DELETE FROM auth_tokens WHERE user_id = ?", (user_id,))
        if row:
            for identifier in (row["email"], row["phone"]):
                if identifier:
                    conn.execute("DELETE FROM otp_codes WHERE identifier = ?", (identifier,))
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))


def get_user(user_id: int, db_path: Path | None = None) -> AuthUser | None:
    with _connect(db_path) as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return _row_to_user(row) if row else None


def update_display_name(user_id: int, display_name: str, db_path: Path | None = None) -> None:
    with _connect(db_path) as conn:
        conn.execute("UPDATE users SET display_name = ? WHERE id = ?", (display_name, user_id))


def update_preferences(user_id: int, preferred_language: str | None = None,
                        tts_voice: str | None = None, db_path: Path | None = None) -> None:
    """Partial update — only touches the fields actually passed, so a
    settings screen changing just TTS voice doesn't clobber the language
    preference (or vice versa)."""
    with _connect(db_path) as conn:
        if preferred_language is not None:
            conn.execute("UPDATE users SET preferred_language = ? WHERE id = ?",
                         (preferred_language, user_id))
        if tts_voice is not None:
            conn.execute("UPDATE users SET tts_voice = ? WHERE id = ?", (tts_voice, user_id))


def complete_onboarding(user_id: int, display_name: str, preferred_language: str,
                         tts_voice: str = "warm", db_path: Path | None = None) -> None:
    """Called once, at the end of the Setup flow — this is what makes
    Setup a real data-collection step instead of three slides with a
    button at the end that does nothing with what was shown."""
    now = time.time()
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE users SET display_name = ?, preferred_language = ?, tts_voice = ?, "
            "onboarded_at = ? WHERE id = ?",
            (display_name, preferred_language, tts_voice, now, user_id),
        )


def find_or_create_user_by_google(email: str, display_name: str | None,
                                   db_path: Path | None = None) -> int:
    """Google Sign-In's equivalent of verify_otp's find-or-create tail —
    no OTP involved (Google already proved the email ownership via its
    own signed token), so this is a separate, simpler path rather than
    routing through the OTP tables.

    Deliberately keyed on the SAME `email` column OTP login uses, not a
    separate google_id/sub column — so someone who signed up via email
    OTP and later clicks "Sign in with Google" with that same email
    address lands in the same account, not a confusing duplicate one.
    This does mean: if a Google account's email were somehow not
    actually controlled by the person signing in (not possible in
    practice — Google only issues verified-email tokens for accounts it
    controls), it would collide with an existing OTP account. Accepted
    tradeoff given Google itself guarantees email ownership.

    display_name is only set if the user doesn't already have one —
    someone who already picked a name during OTP-based Setup shouldn't
    have it silently overwritten by whatever their Google profile name
    happens to be.
    """
    now = time.time()
    with _connect(db_path) as conn:
        row = conn.execute("SELECT id, display_name FROM users WHERE email = ?", (email,)).fetchone()
        if row:
            if display_name and not row["display_name"]:
                conn.execute("UPDATE users SET display_name = ? WHERE id = ?", (display_name, row["id"]))
            return row["id"]

        cur = conn.execute(
            "INSERT INTO users (email, display_name, created_at) VALUES (?, ?, ?)",
            (email, display_name, now),
        )
        return cur.lastrowid
