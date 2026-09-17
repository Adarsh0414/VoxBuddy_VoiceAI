"""
VoxBuddy Phase 1 backend — FastAPI app.

Runs the real CIE + mocked ASR/MT/TTS pipeline behind a small HTTP + WebSocket
API, and serves the frontend demo as static files from the same process so
the whole thing is a single `uvicorn app:app` command locally, and a single
deployable service later (e.g. on Render, matching how CampusVibe/CallBeacon
are hosted).

Run:
    cd backend
    pip install -r requirements.txt
    uvicorn app:app --reload

Then open http://127.0.0.1:8000/
"""

from __future__ import annotations

import asyncio
import os
import secrets
import uuid
from pathlib import Path
from urllib.parse import quote, urlencode

try:
    from dotenv import load_dotenv
    load_dotenv()  # loads .env from the working directory if present; no-op otherwise
except ImportError:
    pass  # python-dotenv is optional — env vars can be exported directly instead

from fastapi import Depends, FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from agents.factory import get_streaming_asr_agent
from cie.state import SpeakerRole
from cie.demo_fixtures import DEMO_SCENARIOS, DEMO_SCENARIO_SUMMARY
from session.manager import IncomingUtterance, SessionManager
from session.streaming_manager import StreamingSessionAdapter
import auth_store
import otp_providers
import persistence_store as persistence  # backend chosen by VOXBUDDY_PERSISTENCE_PROVIDER (sqlite/dynamodb)
import token_store

app = FastAPI(title="VoxBuddy Phase 1 PoC")
persistence.init_db()
auth_store.init_db()

# --- in-memory session store (fine for a local PoC; Phase 2+ moves this to
# the real Conversation Session Service backed by Redis, per the PRD) -------
sessions: dict[str, SessionManager] = {}

# Maps the frontend's bare language codes (same codes used everywhere else
# in the app — see LANGUAGES in frontend/app-preview.html, and target_lang
# below) to the region-qualified BCP-47 codes AWS Transcribe's streaming
# API requires as language_code (e.g. "en" -> "en-US"). This is the ASR
# *source* language — the language actually spoken into the mic — which is
# a separate concept from target_lang (the translation destination); see
# websocket_audio_session() below for how the two are kept independent.
# Extend this as more source languages are supported.
ASR_SOURCE_LANGUAGE_CODES: dict[str, str] = {
    "en": "en-US",
    "hi": "hi-IN",
}
DEFAULT_ASR_SOURCE_LANGUAGE_CODE = "en-US"

# --- /ws/{session_id}/audio connection-lifecycle guard ----------------------
# See websocket_audio_session() below. Real-time ASR vendors (AWS
# Transcribe, AssemblyAI) bill for however long audio is streamed to them,
# not just for detected speech - and the frontend deliberately streams
# silence too (a client-side noise gate used to exist and was removed
# because it broke AssemblyAI's turn detection; see app-preview.html). A
# forgotten browser tab, a phone backgrounded overnight (the Android build
# runs a foreground service specifically to keep this connection alive),
# or a client reconnect loop that never really cleans up would otherwise
# keep billing indefinitely - there is no other timeout anywhere in this
# path (adapter, session manager, or agent). These two env vars bound that
# risk independent of VOXBUDDY_ASR_PROVIDER, so behavior is identical (and
# testable) under mock, assemblyai, and aws_transcribe:
#   - VOXBUDDY_WS_IDLE_TIMEOUT_SECONDS: no final transcribed turn for this
#     long -> close. Default 100s: long enough that a normal conversational
#     pause (someone thinking, taking a sip of water) doesn't trip it, short
#     enough that "forgot to hang up" gets caught well before real cost
#     accrues.
#   - VOXBUDDY_WS_MAX_SESSION_SECONDS: hard cap on total connection
#     duration regardless of activity - belt-and-suspenders against a
#     session that keeps producing occasional real turns but is clearly no
#     longer a real conversation (left open all day). Default 1800s (30
#     min): comfortably longer than any real back-and-forth this product
#     supports, short enough to bound worst-case cost.
#
# Read fresh per-connection (below), not cached at import time, matching
# how every other runtime-configurable env var in this file (e.g.
# GOOGLE_CLIENT_ID, VOXBUDDY_PUBLIC_URL) is read inside its handler rather
# than into a module-level constant - and it's what lets tests exercise
# both timeouts with short, deterministic values via monkeypatch.setenv()
# without needing to reimport this module.
DEFAULT_VOXBUDDY_WS_IDLE_TIMEOUT_SECONDS = 100.0
DEFAULT_VOXBUDDY_WS_MAX_SESSION_SECONDS = 1800.0

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"


# --- per-IP rate limiting for OTP requests ----------------------------------
# auth_store.create_otp() already enforces a per-IDENTIFIER cooldown (one
# phone/email can't spam itself), but nothing previously stopped one client
# from requesting OTPs for many DIFFERENT identifiers rapidly — real abuse
# surface, since every request costs a real SMS/email send once a real
# OTP_EMAIL/SMS_PROVIDER is configured (Brevo/Fast2SMS/SMTP all charge or
# rate-limit per-account, not just per-recipient). Deliberately a small
# fixed-window in-memory limiter, not a new dependency (slowapi/Redis) —
# proportionate for a single-instance deployment; revisit if this ever runs
# behind a real load balancer with multiple instances, since counts here
# don't share across processes.
import time as _time
from collections import defaultdict as _defaultdict

_otp_request_log: dict[str, list[float]] = _defaultdict(list)
_OTP_RATE_LIMIT_WINDOW_SECONDS = 600      # 10 minutes
_OTP_RATE_LIMIT_MAX_REQUESTS = 8          # per IP, per window


def _check_otp_rate_limit(client_ip: str) -> None:
    now = _time.time()
    window_start = now - _OTP_RATE_LIMIT_WINDOW_SECONDS
    recent = [t for t in _otp_request_log[client_ip] if t > window_start]
    if len(recent) >= _OTP_RATE_LIMIT_MAX_REQUESTS:
        raise HTTPException(
            status_code=429,
            detail="Too many code requests from this network. Try again in a few minutes.",
        )
    recent.append(now)
    _otp_request_log[client_ip] = recent


def reset_otp_rate_limit() -> None:
    """Test-only helper, mirroring token_store.reset_store() — clears the
    in-memory rate-limit counters between tests. Without this, every test
    in a file that calls request-otp shares one bucket (TestClient always
    reports the same fake client IP), so a handful of legitimate test
    calls trips the same limiter real abuse would."""
    _otp_request_log.clear()


class UtteranceIn(BaseModel):
    speaker_label: str
    text: str
    target_lang: str = "en"
    turn_taking_score: float = 0.8
    semantic_coherence_score: float = 0.8
    noise_class: str | None = None  # optional ambient-noise estimate from the
                                     # client (e.g. "quiet"/"moderate"/
                                     # "loud_crowd"); feeds the CIE's
                                     # noise-adaptive thresholds, see
                                     # cie/engine.py


class UtteranceOut(BaseModel):
    speaker_id: str
    role: str
    confidence: float
    partner_switched: bool
    partner_joined: bool
    notes: str
    speaker_language: str | None = None  # last language ID'd for this
                                          # speaker, tracked across turns
    translated_text: str | None
    latency_ms: float
    primary_partner_id: str | None
    active_partner_ids: list[str]
    speakers_tracked: int
    tts_audio_b64: str | None = None
    tts_audio_format: str | None = None
    tts_error: str | None = None
    translation_error: str | None = None

    # -- signal breakdown (v1.2) -- see cie/engine.py CIEDecision. Exposed
    # so a UI can render *why* a decision fired, not just the outcome.
    # All None for the SELF role (nothing meaningful to plot there).
    voice_similarity: float | None = None
    turn_taking_score: float | None = None
    semantic_coherence_score: float | None = None
    weight_voice: float | None = None
    weight_turn: float | None = None
    weight_semantic: float | None = None
    bystander_threshold: float | None = None
    lock_threshold: float | None = None
    noise_profile: str | None = None


def _to_out(session: SessionManager, result) -> UtteranceOut:
    d = result.decision
    return UtteranceOut(
        speaker_id=d.speaker_id,
        role=d.role.value,
        confidence=round(d.confidence, 3),
        partner_switched=d.partner_switched,
        partner_joined=d.partner_joined,
        notes=d.notes,
        speaker_language=d.language,
        translated_text=result.translated_text,
        latency_ms=round(result.latency_ms, 2),
        primary_partner_id=session.state.primary_partner_id,
        active_partner_ids=sorted(session.state.active_partner_ids),
        tts_audio_b64=getattr(result, "tts_audio_b64", None),
        tts_audio_format=getattr(result, "tts_audio_format", None),
        tts_error=getattr(result, "tts_error", None),
        translation_error=getattr(result, "translation_error", None),
        speakers_tracked=len(session.state.speakers),
        voice_similarity=d.voice_similarity,
        turn_taking_score=d.turn_taking_score,
        semantic_coherence_score=d.semantic_coherence_score,
        weight_voice=d.weight_voice,
        weight_turn=d.weight_turn,
        weight_semantic=d.weight_semantic,
        bystander_threshold=d.bystander_threshold,
        lock_threshold=d.lock_threshold,
        noise_profile=d.noise_profile,
    )


# =============================================================================
# Authentication — email OR phone, OTP-based, no passwords.
#
# Either channel works interchangeably; a user is identified by whichever
# email/phone they successfully verify with (see auth_store.py — email and
# phone create distinct user records for now, not automatically linked,
# since there's no verified way to know two different identifiers belong
# to the same person without asking).
# =============================================================================

class RequestOtpIn(BaseModel):
    identifier: str
    channel: str  # "email" | "sms"


class RequestOtpOut(BaseModel):
    ok: bool
    expires_in_seconds: int
    dev_otp: str | None = None  # only populated when VOXBUDDY_AUTH_DEV_MODE=1


class VerifyOtpIn(BaseModel):
    identifier: str
    channel: str
    code: str


class AuthUserOut(BaseModel):
    id: int
    email: str | None
    phone: str | None
    display_name: str | None
    preferred_language: str | None
    tts_voice: str | None
    onboarded: bool


class VerifyOtpOut(BaseModel):
    token: str
    user: AuthUserOut


def _dev_mode() -> bool:
    return os.environ.get("VOXBUDDY_AUTH_DEV_MODE", "").lower() in ("1", "true", "yes")


def _to_user_out(user: auth_store.AuthUser) -> AuthUserOut:
    return AuthUserOut(
        id=user.id, email=user.email, phone=user.phone, display_name=user.display_name,
        preferred_language=user.preferred_language, tts_voice=user.tts_voice,
        onboarded=user.onboarded_at is not None,
    )


@app.post("/api/auth/request-otp", response_model=RequestOtpOut)
def request_otp(body: RequestOtpIn, request: Request):
    # x-forwarded-for first: Render (and most PaaS hosts) sit behind a
    # proxy, so request.client.host would otherwise always be the proxy's
    # own IP — the actual rate limit would then apply to every user at
    # once instead of per real client.
    client_ip = (request.headers.get("x-forwarded-for", "").split(",")[0].strip()
                 or (request.client.host if request.client else "unknown"))
    _check_otp_rate_limit(client_ip)

    try:
        identifier = auth_store.normalize_identifier(body.identifier, body.channel)
    except auth_store.InvalidIdentifier as e:
        raise HTTPException(status_code=400, detail=str(e))

    try:
        code, expires_at = auth_store.create_otp(identifier, body.channel)
    except auth_store.CooldownActive as e:
        raise HTTPException(status_code=429, detail=str(e))

    try:
        provider = otp_providers.get_provider(body.channel)
        provider.send(identifier, code)
    except otp_providers.OTPSendError as e:
        raise HTTPException(status_code=502, detail=f"Could not send code: {e}")

    return RequestOtpOut(
        ok=True,
        expires_in_seconds=auth_store.OTP_EXPIRY_SECONDS,
        dev_otp=code if _dev_mode() else None,
    )


@app.post("/api/auth/verify-otp", response_model=VerifyOtpOut)
def verify_otp(body: VerifyOtpIn):
    try:
        identifier = auth_store.normalize_identifier(body.identifier, body.channel)
    except auth_store.InvalidIdentifier as e:
        raise HTTPException(status_code=400, detail=str(e))

    try:
        user_id = auth_store.verify_otp(identifier, body.code)
    except auth_store.OtpIncorrect as e:
        raise HTTPException(status_code=400, detail=str(e))
    except (auth_store.OtpExpired, auth_store.OtpNotFound) as e:
        raise HTTPException(status_code=400, detail=str(e))
    except auth_store.OtpTooManyAttempts as e:
        raise HTTPException(status_code=429, detail=str(e))

    token, _ = token_store.get_store().create(user_id)
    user = auth_store.get_user(user_id)
    return VerifyOtpOut(token=token, user=_to_user_out(user))


class GoogleSignInIn(BaseModel):
    id_token: str


@app.get("/api/auth/config")
def auth_config():
    """Tells the frontend whether Google Sign-In is actually configured,
    so the button can be hidden entirely rather than rendered broken —
    same "real feature, graceful absence" pattern as every mock-vs-real
    vendor toggle elsewhere in this project. app-preview.html is a
    static file (no server-side templating), so this is how it learns
    the client ID at runtime instead of it being baked into the HTML."""
    return {"google_client_id": os.environ.get("GOOGLE_CLIENT_ID", "")}


def _session_from_google_payload(payload: dict) -> "VerifyOtpOut":
    """Shared by both Google sign-in paths (web JS-credential flow and the
    native authorization-code flow below) — everything past "we have a
    verified Google identity" is identical, so this is the one place that
    turns that into an app session."""
    if not payload.get("email_verified", False):
        raise HTTPException(status_code=401, detail="Google account email is not verified.")
    email = payload["email"].lower()
    display_name = payload.get("name")
    user_id = auth_store.find_or_create_user_by_google(email, display_name)
    token, _ = token_store.get_store().create(user_id)
    user = auth_store.get_user(user_id)
    return VerifyOtpOut(token=token, user=_to_user_out(user))


@app.post("/api/auth/google", response_model=VerifyOtpOut)
def google_sign_in(body: GoogleSignInIn):
    """Web-only path: Google Identity Services' JS SDK renders a button,
    the browser signs the user in itself, and we just verify the ID token
    it hands back. This literally cannot work inside the Android app's
    embedded WebView — Google's own policy refuses to complete OAuth
    inside "disallowed" embedded user agents, which is why the button
    would render (or silently fail) in the native app but work fine in a
    real browser. Native uses /api/auth/google/start + /callback instead.

    Verifies the ID token's signature against Google's own public keys
    (google-auth's verify_oauth2_token does this — fetches Google's
    current signing certs and checks the JWT signature, expiry, and
    audience match our own GOOGLE_CLIENT_ID) rather than trusting
    whatever the frontend claims, since a forged/replayed token would
    otherwise be an account-takeover vector.
    """
    client_id = os.environ.get("GOOGLE_CLIENT_ID")
    if not client_id:
        raise HTTPException(status_code=503, detail="Google Sign-In is not configured on this server.")

    from google.auth.transport import requests as google_requests
    from google.oauth2 import id_token as google_id_token

    try:
        payload = google_id_token.verify_oauth2_token(
            body.id_token, google_requests.Request(), client_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=401, detail=f"Invalid Google sign-in token: {e}")

    return _session_from_google_payload(payload)


_google_oauth_states: set[str] = set()


@app.get("/api/auth/google/start")
def google_sign_in_start():
    """Native app's entry point — opened in the SYSTEM browser (Chrome
    Custom Tabs via @capacitor/browser), never the embedded WebView, which
    is what makes this allowed under Google's policy where the JS-SDK
    button isn't. Redirects straight into Google's real OAuth consent
    screen; /callback below picks up the redirect back."""
    client_id = os.environ.get("GOOGLE_CLIENT_ID")
    if not client_id:
        raise HTTPException(status_code=503, detail="Google Sign-In is not configured on this server.")
    public_url = os.environ.get("VOXBUDDY_PUBLIC_URL", "").rstrip("/")
    if not public_url:
        raise HTTPException(status_code=503, detail="VOXBUDDY_PUBLIC_URL is not set on this server.")

    state = secrets.token_urlsafe(24)
    _google_oauth_states.add(state)

    params = {
        "client_id": client_id,
        "redirect_uri": f"{public_url}/api/auth/google/callback",
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "prompt": "select_account",
    }
    return RedirectResponse(f"https://accounts.google.com/o/oauth2/v2/auth?{urlencode(params)}")


@app.get("/api/auth/google/callback")
async def google_sign_in_callback(code: str | None = None, state: str | None = None, error: str | None = None):
    """Google redirects here (a real HTTPS URL, required for a "Web
    application" OAuth client) after the user finishes signing in in the
    system browser. Exchanges the one-time code for tokens server-side
    (needs GOOGLE_CLIENT_SECRET, which only the backend ever holds),
    verifies the ID token exactly like the web path does, then hands the
    resulting app session back to the *native app* — not the browser —
    via a voxbuddy:// deep link, which Android is registered to intercept
    and reopen the app with (see AndroidManifest.xml's intent-filter)."""
    deep_link_base = "voxbuddy://auth"

    if error:
        return HTMLResponse(_google_callback_page(f"{deep_link_base}?error={error}", ok=False))
    if not code or not state or state not in _google_oauth_states:
        return HTMLResponse(_google_callback_page(f"{deep_link_base}?error=invalid_state", ok=False))
    _google_oauth_states.discard(state)

    client_id = os.environ.get("GOOGLE_CLIENT_ID")
    client_secret = os.environ.get("GOOGLE_CLIENT_SECRET")
    public_url = os.environ.get("VOXBUDDY_PUBLIC_URL", "").rstrip("/")
    if not client_id or not client_secret or not public_url:
        return HTMLResponse(_google_callback_page(f"{deep_link_base}?error=not_configured", ok=False))

    import httpx
    from google.auth.transport import requests as google_requests
    from google.oauth2 import id_token as google_id_token

    async with httpx.AsyncClient(timeout=10) as client:
        token_res = await client.post("https://oauth2.googleapis.com/token", data={
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": f"{public_url}/api/auth/google/callback",
            "grant_type": "authorization_code",
        })
    if token_res.status_code != 200:
        return HTMLResponse(_google_callback_page(f"{deep_link_base}?error=token_exchange_failed", ok=False))

    id_tok = token_res.json().get("id_token")
    if not id_tok:
        return HTMLResponse(_google_callback_page(f"{deep_link_base}?error=no_id_token", ok=False))

    try:
        payload = google_id_token.verify_oauth2_token(id_tok, google_requests.Request(), client_id)
    except ValueError:
        return HTMLResponse(_google_callback_page(f"{deep_link_base}?error=invalid_token", ok=False))

    try:
        session = _session_from_google_payload(payload)
    except HTTPException as e:
        return HTMLResponse(_google_callback_page(f"{deep_link_base}?error={quote(str(e.detail))}", ok=False))

    onboarded = "1" if session.user.onboarded else "0"
    redirect_url = f"{deep_link_base}?{urlencode({'token': session.token, 'onboarded': onboarded})}"
    return HTMLResponse(_google_callback_page(redirect_url, ok=True))


def _google_callback_page(redirect_url: str, ok: bool) -> str:
    """Tiny bridge page: real HTTPS pages are all Google's redirect_uri
    can point at, but the app itself needs to receive the result, so this
    immediately hands off to the voxbuddy:// deep link. The visible
    message is only a fallback for the rare case the OS doesn't auto-open
    the app (deep link handling disabled, no matching app installed)."""
    message = "Signed in — returning to VoxBuddy…" if ok else "Sign-in didn't complete — returning to VoxBuddy…"
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>VoxBuddy</title>
<style>body{{background:#0B0D14;color:#fff;font-family:sans-serif;
display:flex;align-items:center;justify-content:center;height:100vh;margin:0;text-align:center;padding:24px}}
a{{color:#4FD1C5}}</style></head>
<body><div><p>{message}</p><p><a href="{redirect_url}">Tap here if you're not redirected automatically</a></p></div>
<script>window.location.href = {redirect_url!r};</script>
</body></html>"""


def get_current_user(authorization: str | None = Header(default=None)) -> auth_store.AuthUser:
    """FastAPI dependency for endpoints that require a logged-in user.
    Expects `Authorization: Bearer <token>`."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or malformed Authorization header")
    token = authorization.removeprefix("Bearer ").strip()
    user_id = token_store.get_store().get_user_id(token)
    user = auth_store.get_user(user_id) if user_id is not None else None
    if not user:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    return user


def get_optional_user(authorization: str | None = Header(default=None)) -> auth_store.AuthUser | None:
    """Like get_current_user, but returns None instead of raising when
    there's no/invalid token — for endpoints (like history/stats) that
    should still work anonymously for the existing dev dashboard and
    simulate.py, but scope to a real user when one is logged in."""
    if not authorization or not authorization.startswith("Bearer "):
        return None
    token = authorization.removeprefix("Bearer ").strip()
    user_id = token_store.get_store().get_user_id(token)
    return auth_store.get_user(user_id) if user_id is not None else None


@app.get("/api/auth/me", response_model=AuthUserOut)
def get_me(user: auth_store.AuthUser = Depends(get_current_user)):
    return _to_user_out(user)


class OnboardIn(BaseModel):
    display_name: str
    preferred_language: str
    tts_voice: str = "warm"


@app.post("/api/auth/onboard", response_model=AuthUserOut)
def onboard(body: OnboardIn, user: auth_store.AuthUser = Depends(get_current_user)):
    """Called once, at the end of Setup — this is the endpoint that makes
    Setup collect and USE the name/language/voice it asks for, instead of
    those inputs going nowhere."""
    name = body.display_name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Name cannot be empty")
    auth_store.complete_onboarding(user.id, name, body.preferred_language, body.tts_voice)
    return _to_user_out(auth_store.get_user(user.id))


class UpdateProfileIn(BaseModel):
    display_name: str | None = None


@app.patch("/api/auth/profile", response_model=AuthUserOut)
def update_profile(body: UpdateProfileIn, user: auth_store.AuthUser = Depends(get_current_user)):
    if body.display_name is not None:
        name = body.display_name.strip()
        if not name:
            raise HTTPException(status_code=400, detail="Name cannot be empty")
        auth_store.update_display_name(user.id, name)
    return _to_user_out(auth_store.get_user(user.id))


class UpdatePreferencesIn(BaseModel):
    preferred_language: str | None = None
    tts_voice: str | None = None


@app.patch("/api/auth/preferences", response_model=AuthUserOut)
def update_preferences(body: UpdatePreferencesIn, user: auth_store.AuthUser = Depends(get_current_user)):
    auth_store.update_preferences(user.id, preferred_language=body.preferred_language,
                                    tts_voice=body.tts_voice)
    return _to_user_out(auth_store.get_user(user.id))


@app.post("/api/auth/logout")
def logout(authorization: str | None = Header(default=None)):
    if authorization and authorization.startswith("Bearer "):
        token_store.get_store().delete(authorization.removeprefix("Bearer ").strip())
    return {"ok": True}


@app.delete("/api/auth/account")
def delete_account(authorization: str | None = Header(default=None),
                    user: auth_store.AuthUser = Depends(get_current_user)):
    """Permanent account deletion (Profile > Delete account). Removes the
    user's conversations/turns, their user row, and every session token
    tied to them — signing back in afterward creates a brand-new account
    from scratch, nothing is recovered or reactivated.

    This alone is enough to invalidate every existing session everywhere,
    including under VOXBUDDY_SESSION_STORE=redis: get_current_user() (and
    every other authenticated endpoint) always re-resolves a token to a
    live user via auth_store.get_user() on each request, so a token that
    still technically exists in Redis stops working the instant the
    user row it points to is gone, with no separate "revoke all Redis
    tokens for this user" step needed.
    """
    persistence.delete_all(user_id=user.id)
    auth_store.delete_user(user.id)
    if authorization and authorization.startswith("Bearer "):
        token_store.get_store().delete(authorization.removeprefix("Bearer ").strip())
    return {"ok": True}


@app.get("/api/demo/scenarios")
def get_demo_scenarios():
    """Pre-login Interactive Demo fixtures — deterministic, unauthenticated,
    read-only. Does not create a session, user, or history record, and does
    not call any ASR/translation/TTS vendor. See cie/demo_fixtures.py: the
    signal weights returned here are the real cie.engine configuration, not
    a second/duplicated set of thresholds."""
    return {"scenarios": DEMO_SCENARIOS, "summary": DEMO_SCENARIO_SUMMARY}


@app.post("/api/session")
def create_session():
    session_id = str(uuid.uuid4())
    sessions[session_id] = SessionManager()
    return {"session_id": session_id}


class EnrollSelfIn(BaseModel):
    speaker_label: str


@app.post("/api/session/{session_id}/enroll_self")
def enroll_self(session_id: str, body: EnrollSelfIn):
    """Registers the user's own voice so the CIE never evaluates it as a
    partner candidate — call this once at session start, before any other
    utterances (see cie/engine.py's enroll_self and PROGRESS.md)."""
    session = sessions.get(session_id)
    if not session:
        return {"error": "session not found"}
    session.enroll_self(body.speaker_label)
    return {"self_speaker_id": session.state.self_speaker_id}


@app.get("/api/session/{session_id}/state")
def get_state(session_id: str):
    session = sessions.get(session_id)
    if not session:
        return {"error": "session not found"}
    return {
        "primary_partner_id": session.state.primary_partner_id,
        "active_partner_ids": sorted(session.state.active_partner_ids),
        "speakers": {
            sid: {"role": sp.role.value, "turn_count": sp.turn_count, "confidence": round(sp.confidence, 3)}
            for sid, sp in session.state.speakers.items()
        },
        "turns": len(session.state.turn_history),
    }


@app.post("/api/session/{session_id}/utterance", response_model=UtteranceOut)
def post_utterance(session_id: str, utt: UtteranceIn):
    session = sessions.get(session_id)
    if not session:
        return {"error": "session not found"}
    result = session.handle_utterance(IncomingUtterance(**utt.model_dump()))
    return _to_out(session, result)


@app.websocket("/ws/{session_id}")
async def websocket_session(websocket: WebSocket, session_id: str):
    await websocket.accept()
    if session_id not in sessions:
        sessions[session_id] = SessionManager()
    session = sessions[session_id]

    try:
        while True:
            payload = await websocket.receive_json()
            utt = IncomingUtterance(**UtteranceIn(**payload).model_dump())
            result = session.handle_utterance(utt)
            await websocket.send_json(_to_out(session, result).model_dump())
    except WebSocketDisconnect:
        pass


@app.websocket("/ws/{session_id}/audio")
async def websocket_audio_session(websocket: WebSocket, session_id: str,
                                   target_lang: str = "en", source_lang: str = "en"):
    """
    Real microphone audio in, pipeline results out — the counterpart to
    /ws/{session_id} above, which only ever accepted pre-scripted text
    (used by the frontend's DEMO_EXCHANGE). This endpoint is what a real
    "listen to the user talking" feature streams to.

    Protocol: client sends raw binary frames (16-bit PCM, mono, 16kHz —
    see startMicCapture() in app-preview.html for the encoder). Each frame
    is pushed straight into a StreamingSessionAdapter wrapping whichever
    ASR agent VOXBUDDY_ASR_PROVIDER selects (see agents/factory.py).
    Final transcribed turns come back as JSON, same shape as the text
    websocket's responses, so the frontend can reuse one result handler
    for both.

    With the default mock ASR provider this proves the transport end to
    end but can't actually transcribe speech — MockStreamingASRAgent reads
    text tokens, not audio. Set VOXBUDDY_ASR_PROVIDER=assemblyai (or
    aws_transcribe) with the matching API key/credentials to get real
    transcription.

    target_lang vs. source_lang — these are NOT the same thing and must
    not be conflated (a real bug this used to have, see the target_lang
    comment further down): target_lang is the translation *destination*
    (what language the pipeline's output should come out in).
    source_lang is the ASR *source* language — what language is actually
    being spoken into the microphone right now, which is what the ASR
    vendor needs to correctly transcribe the raw audio in the first place,
    before translation is even in the picture. Mapped through
    ASR_SOURCE_LANGUAGE_CODES to the region-qualified code AWS Transcribe's
    streaming API requires (e.g. "hi" -> "hi-IN"); falls back to
    DEFAULT_ASR_SOURCE_LANGUAGE_CODE ("en-US") for a missing or unmapped
    code, matching AmazonTranscribeStreamingASRAgent's own default so
    existing behavior doesn't change if the frontend doesn't send this.
    Only AmazonTranscribeStreamingASRAgent (VOXBUDDY_ASR_PROVIDER=
    aws_transcribe) currently uses this — it's a no-op for the mock/
    assemblyai providers.

    Known limitation: this selects ONE ASR language for the whole session.
    It does not implement automatic language detection or true Hindi/
    English code-switching mid-conversation — see agents/asr_transcribe.py
    and CHANGES for why that's a separate, not-yet-implemented piece of
    work, not something silently faked here.
    """
    await websocket.accept()
    if session_id not in sessions:
        sessions[session_id] = SessionManager()
    session = sessions[session_id]

    asr_language_code = ASR_SOURCE_LANGUAGE_CODES.get(source_lang, DEFAULT_ASR_SOURCE_LANGUAGE_CODE)
    try:
        asr_agent = get_streaming_asr_agent(language_code=asr_language_code)
    except RuntimeError as exc:
        await websocket.send_json({"error": str(exc)})
        await websocket.close()
        return

    async def send_result(result) -> None:
        await websocket.send_json(_to_out(session, result).model_dump())

    # Captured here, inside the running event loop's own coroutine — safe.
    # Needed below because on_pipeline_result can fire from a background
    # thread (see the comment on the lambda a few lines down for why).
    loop = asyncio.get_running_loop()

    # Idle/hard-cap timeout bookkeeping (see the VOXBUDDY_WS_IDLE_TIMEOUT_
    # SECONDS / VOXBUDDY_WS_MAX_SESSION_SECONDS comment near the top of
    # this file). A plain dict, not a local variable, because
    # on_final_asr_result below needs to mutate it from whatever thread
    # the ASR agent calls back on (see that lambda's comment).
    session_start_at = _time.monotonic()
    ws_clock = {"last_final_at": session_start_at}
    idle_timeout_seconds = float(os.environ.get(
        "VOXBUDDY_WS_IDLE_TIMEOUT_SECONDS", DEFAULT_VOXBUDDY_WS_IDLE_TIMEOUT_SECONDS,
    ))
    max_session_seconds = float(os.environ.get(
        "VOXBUDDY_WS_MAX_SESSION_SECONDS", DEFAULT_VOXBUDDY_WS_MAX_SESSION_SECONDS,
    ))

    adapter = StreamingSessionAdapter(
        session=session,
        asr_agent=asr_agent,
        # Real bug caught in production: this used to be hardcoded to
        # "en" regardless of what the user actually selected as "You
        # speak" during setup — every partner utterance got translated
        # into English no matter what, confirmed live: a user with
        # Hindi selected heard English spoken back. Now takes the real
        # value from the query string, which the frontend populates from
        # the logged-in user's own preferred_language (see startMicCapture()
        # in app-preview.html) — falls back to "en" only if that's
        # somehow missing, not as the silent default it used to be.
        target_lang=target_lang,
        # MockStreamingASRAgent (used by every existing test) calls
        # on_result synchronously, inline, from inside push_audio() —
        # which itself runs inside this coroutine, on the event loop's
        # own thread. asyncio.create_task() only works when called from
        # that thread, so it worked for the mock by pure coincidence.
        # AssemblyAIStreamingASRAgent is different: client.stream() runs
        # in its own background thread (see asr_assemblyai.py's start()),
        # and the SDK invokes _on_turn — and therefore this callback —
        # from THAT thread, not the event loop's. asyncio.create_task()
        # called from a foreign thread doesn't schedule anything; it
        # either raises "no running event loop" (usually swallowed
        # silently by the SDK's own internal event-dispatch try/except,
        # never surfacing anywhere) or is simply undefined behavior. The
        # practical effect was exactly the "just keeps listening forever"
        # symptom: the pipeline genuinely finishes (ASR, CIE, translation,
        # TTS all really run), the result just never makes it back over
        # the WebSocket. asyncio.run_coroutine_threadsafe() is the actual
        # correct primitive for scheduling a coroutine onto a specific
        # event loop from any other thread — it works from both the loop's
        # own thread (the mock's case) and a foreign one (AssemblyAI's).
        on_pipeline_result=lambda result: asyncio.run_coroutine_threadsafe(send_result(result), loop),
        # Idle-timeout clock (see the VOXBUDDY_WS_IDLE_TIMEOUT_SECONDS
        # comment above). Deliberately NOT routed through
        # run_coroutine_threadsafe like on_pipeline_result above: this
        # callback only records a timestamp, it doesn't need to run *on*
        # the event loop, just needs to be safe to call from any thread
        # (AssemblyAI's SDK thread included) - a single dict-item
        # assignment is, under the GIL.
        on_final_asr_result=lambda _result: ws_clock.__setitem__(
            "last_final_at", _time.monotonic()
        ),
    )
    adapter.start(sample_rate=16000)

    try:
        while True:
            now = _time.monotonic()
            idle_deadline = ws_clock["last_final_at"] + idle_timeout_seconds
            hard_deadline = session_start_at + max_session_seconds
            remaining = min(idle_deadline, hard_deadline) - now

            timed_out = False
            if remaining <= 0:
                # A deadline already passed before we even got back around
                # to checking (e.g. this loop iteration itself took a
                # while) - treat it exactly like asyncio.wait_for timing
                # out below, without calling it with a non-positive timeout.
                timed_out = True
            else:
                try:
                    message = await asyncio.wait_for(websocket.receive(), timeout=remaining)
                except asyncio.TimeoutError:
                    timed_out = True

            if timed_out:
                now = _time.monotonic()
                if now >= hard_deadline:
                    reason = (
                        f"Session exceeded the maximum duration of "
                        f"{int(max_session_seconds)}s and was "
                        f"closed automatically."
                    )
                else:
                    reason = (
                        f"No activity for {int(idle_timeout_seconds)}s; "
                        f"session closed automatically."
                    )
                # Distinguishable from both a normal pipeline result and
                # the existing {"error": ...} auth/provider-setup errors,
                # so the frontend can tell "this connection is intentionally
                # done, don't reconnect" apart from "that dropped, retry".
                await websocket.send_json({"idle_timeout": True, "reason": reason})
                await websocket.close()
                break

            if message.get("type") == "websocket.disconnect":
                break
            frame = message.get("bytes")
            if frame is not None:
                try:
                    adapter.push_audio(frame)
                except UnicodeDecodeError:
                    # MockStreamingASRAgent expects text tokens, not real
                    # PCM bytes (see mock_streaming_asr.py) — this is the
                    # expected failure mode when VOXBUDDY_ASR_PROVIDER=mock
                    # is fed real microphone audio. It proves the audio
                    # transport works; it just can't fake-transcribe it.
                    await websocket.send_json({
                        "error": "Mock ASR provider can't transcribe real "
                                 "audio (it expects text tokens, not PCM). "
                                 "Set VOXBUDDY_ASR_PROVIDER=assemblyai and "
                                 "ASSEMBLYAI_API_KEY for real transcription. "
                                 "Audio is streaming correctly otherwise.",
                    })
                    break
    except WebSocketDisconnect:
        pass
    finally:
        adapter.stop()


@app.post("/api/session/{session_id}/end")
def end_session(session_id: str, user: auth_store.AuthUser | None = Depends(get_optional_user)):
    """Persists the session's turn history (if any) and removes it from the
    in-memory session store. This is what makes /api/history real instead
    of mock data — see persistence.py. Scoped to the logged-in user if one
    is present (Authorization header); saved as anonymous otherwise."""
    session = sessions.get(session_id)
    if not session:
        return {"error": "session not found"}
    conversation_id = persistence.save_conversation(
        session, session_id, user_id=user.id if user else None
    )
    del sessions[session_id]
    return {"conversation_id": conversation_id, "saved": conversation_id is not None}


@app.get("/api/history")
def get_history(user: auth_store.AuthUser | None = Depends(get_optional_user)):
    """Returns the logged-in user's conversations if authenticated,
    otherwise ALL stored conversations (existing anonymous dev-dashboard
    behavior, unchanged)."""
    summaries = persistence.list_conversations(user_id=user.id if user else None)
    return [
        {
            "id": s.id,
            "started_at": s.started_at,
            "ended_at": s.ended_at,
            "target_lang": s.target_lang,
            "turn_count": s.turn_count,
            "duration_seconds": round(s.duration_seconds, 1),
            "first_line": s.first_line,
        }
        for s in summaries
    ]


@app.get("/api/history/{conversation_id}")
def get_history_detail(conversation_id: int,
                        user: auth_store.AuthUser | None = Depends(get_optional_user)):
    detail = persistence.get_conversation(conversation_id)
    if not detail:
        return {"error": "conversation not found"}

    # Ownership check: a conversation saved under a specific user_id may
    # only be read back by that same user. Anonymous conversations
    # (user_id IS NULL — the dev dashboard / simulate.py) remain readable
    # by anyone, matching existing behavior for that flow. Without this,
    # any authenticated user could read any other user's transcript just
    # by guessing a conversation id.
    owner_id = persistence.get_conversation_owner(conversation_id)
    if owner_id is not None and (user is None or user.id != owner_id):
        return {"error": "conversation not found"}

    return {
        "id": detail.summary.id,
        "started_at": detail.summary.started_at,
        "duration_seconds": round(detail.summary.duration_seconds, 1),
        "target_lang": detail.summary.target_lang,
        "turns": [
            {
                "role": t.role, "source_lang": t.source_lang, "source_text": t.source_text,
                "target_lang": t.target_lang, "target_text": t.target_text, "timestamp": t.timestamp,
            }
            for t in detail.turns
        ],
    }


@app.get("/api/stats")
def get_stats(user: auth_store.AuthUser | None = Depends(get_optional_user)):
    """Powers the Profile screen's stat cards with real numbers computed
    from persisted history — see persistence.get_summary_stats. Scoped to
    the logged-in user if authenticated, otherwise reflects everyone
    (existing anonymous dev-dashboard behavior)."""
    stats = persistence.get_summary_stats(user_id=user.id if user else None)
    return {
        "total_conversations": stats.total_conversations,
        "total_languages": stats.total_languages,
        "total_seconds": round(stats.total_seconds, 1),
        "day_streak": stats.day_streak,
    }


@app.get("/api/stats/languages")
def get_language_breakdown(user: auth_store.AuthUser | None = Depends(get_optional_user)):
    """Powers Profile's 'Languages spoken with' list — real per-language
    conversation counts, replacing what used to be hardcoded fake bars."""
    return persistence.get_language_breakdown(user_id=user.id if user else None)


@app.delete("/api/history")
def clear_history(user: auth_store.AuthUser | None = Depends(get_optional_user)):
    """Privacy control per PRD §14 — users can delete their conversation
    history. Scoped to the logged-in user if authenticated; clears
    everything if not (existing anonymous dev-dashboard behavior)."""
    persistence.delete_all(user_id=user.id if user else None)
    return {"cleared": True}


# --- serve the frontend demo from the same process --------------------------
app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


@app.api_route("/", methods=["GET", "HEAD"])
def index():
    # GET **and** HEAD — uptime monitors (UptimeRobot, etc.) ping the root
    # URL with HEAD requests to check the service is alive without pulling
    # the full page body. FastAPI's @app.get() only registers GET, so a
    # bare HEAD request used to 405 here (Allow: GET) and UptimeRobot
    # reported the service as down even though it was up.
    return FileResponse(str(FRONTEND_DIR / "index.html"))


@app.get("/preview")
def product_preview():
    return FileResponse(str(FRONTEND_DIR / "product-preview.html"))


@app.get("/app")
def app_preview():
    # no-cache (not no-store) — the browser can still keep a copy, but
    # MUST revalidate with the server (a fast conditional GET using the
    # Last-Modified/ETag FileResponse already sends) before ever using
    # it, rather than silently serving a stale disk-cached copy for some
    # heuristic freshness window. Without this, browsers apply their own
    # caching heuristic to HTML with no explicit Cache-Control — which is
    # exactly what caused a real bug: the service worker's fetch() calls
    # were already "network-first" by design, but still got served a
    # stale cached response by the browser's HTTP cache layer underneath,
    # since "network-first" in a service worker still goes through fetch(),
    # which is itself cache-aware unless told otherwise.
    return FileResponse(str(FRONTEND_DIR / "app-preview.html"),
                         headers={"Cache-Control": "no-cache"})


@app.get("/manifest.json")
def manifest():
    return FileResponse(str(FRONTEND_DIR / "manifest.json"), media_type="application/manifest+json")


@app.get("/sw.js")
def service_worker():
    # Served from the root path (not /static/) deliberately — a service
    # worker's scope is whatever path it's served from, and root gives it
    # scope "/" so it can control the whole app. Serving it from /static/
    # would only let it control /static/ URLs.
    return FileResponse(str(FRONTEND_DIR / "sw.js"), media_type="application/javascript")


@app.get("/privacy")
def privacy_policy():
    # Required for Play Store submission (Play Console asks for a privacy
    # policy URL) — also just the right thing to have given the app
    # collects email/phone and conversation history.
    return FileResponse(str(FRONTEND_DIR / "privacy.html"))


@app.get("/terms")
def terms_of_service():
    # Not always a hard Play Store requirement the way the privacy policy
    # is, but expected by Apple review for an app with accounts, and just
    # good practice given the accuracy disclaimers a translation app
    # specifically needs (see terms.html's "Accuracy — no guarantee").
    return FileResponse(str(FRONTEND_DIR / "terms.html"))
