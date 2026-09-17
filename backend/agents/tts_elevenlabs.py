"""
ElevenLabs streaming text-to-speech adapter.

STATUS: live and working against real ElevenLabs traffic as of the
voice-discovery fix below — see PROGRESS.md for the full bug history
(a Voice Library voice_id, then a hardcoded "premade" voice_id that
turned out to also be unreliable, both producing the same 402).

Implements the synchronous TTSAgent protocol from agents/base.py using the
SDK's simple `convert` + streaming iterator, which is sufficient to replace
MockTTSAgent directly without any interface changes (unlike the ASR case —
TTS here is naturally request/response: text in, audio stream out, which is
exactly what TTSAgent.synthesize already expects).

For the lowest possible time-to-first-audio in the real hot path (PRD §6
latency budget), Phase 2 should move this to the WebSocket
`/v1/text-to-speech/{voice_id}/stream-input` endpoint so partial text (as
translation tokens arrive) can start audio generation before the full
sentence is translated — this adapter uses the simpler streaming HTTP
method as a correct, lower-risk starting point.
"""

from __future__ import annotations

import os

from .base import TTSResult

# A small default voice map per target language.
#
# Real bug history, in order:
# 1. This originally pointed every language at "VO7pRycLkEn8V7IWzZ0r" — a
#    *Voice Library* voice (pulled from ElevenLabs' shared community
#    library). ElevenLabs restricts Library voices to paid-plan accounts
#    for API access specifically — free to preview in their dashboard,
#    blocked via the API — returning 402 ("Free users cannot use library
#    voices via the API").
# 2. Fixed (seemingly) by switching to "21m00Tcm4TlvDq8ikWAM" ("Rachel"),
#    one of ElevenLabs' classic built-in "Default" voices. Still failed
#    with the exact same 402 on a real account. Root cause: ElevenLabs is
#    actively retiring their entire classic Default voice catalog in
#    2026 (their own docs: "All our Default voices will expire on
#    December 31, 2026... being replaced with new voices") — Rachel and
#    the other well-known hardcoded IDs from years of tutorials/blog
#    posts are no longer reliably present/free-tier-accessible on a
#    given account during this transition. Any ID hardcoded from
#    training data or documentation is fundamentally unreliable right
#    now for exactly this reason.
#
# Real fix: don't hardcode a specific ID at all. _resolve_voice_id()
# below queries the account's own actual voice list at runtime
# (category="premade", which ElevenLabs' docs confirm IS API-accessible
# on Free tier, unlike Library voices) and picks a real, currently-valid
# voice from it — self-healing regardless of which specific IDs
# ElevenLabs' catalog contains on any given day. These are now only a
# same-process cache/override, populated by that resolution, not a
# source of truth to trust blindly.
DEFAULT_VOICE_IDS: dict[str, str] = {}

DEFAULT_MODEL = "eleven_flash_v2_5"  # lowest-latency model per vendor research


class ElevenLabsTTSAgent:
    def __init__(self, api_key: str | None = None,
                 voice_ids: dict[str, str] | None = None,
                 model_id: str = DEFAULT_MODEL):
        self.api_key = api_key or os.environ.get("ELEVENLABS_API_KEY")
        # A shallow copy, not a reference to the module-level dict — this
        # instance mutates it as a runtime cache (see _resolve_voice_id),
        # and sharing the module dict across instances/tests would leak
        # resolved IDs between them.
        self.voice_ids = dict(voice_ids or DEFAULT_VOICE_IDS)
        self.model_id = model_id
        self._client = None
        self._premade_voice_ids_cache: list[str] | None = None

    def _get_client(self):
        if self._client is None:
            if not self.api_key:
                raise RuntimeError(
                    "ELEVENLABS_API_KEY is not set. Set it in the environment "
                    "or pass api_key= explicitly."
                )
            from elevenlabs.client import ElevenLabs  # lazy import
            self._client = ElevenLabs(api_key=self.api_key)
        return self._client

    def _fetch_premade_voice_ids(self, client) -> list[str]:
        """Queries this account's own actual voice list, filtered to
        category="premade" — the one category ElevenLabs' docs confirm
        is accessible via the API on every plan tier, Free included
        (unlike "professional"/Library voices). Cached per-agent-instance
        for the life of the process; a premade voice being added/removed
        from an account mid-process is rare enough not to justify
        re-querying on every single synthesize() call."""
        if self._premade_voice_ids_cache is not None:
            return self._premade_voice_ids_cache
        response = client.voices.search(category="premade")
        ids = [v.voice_id for v in response.voices]
        self._premade_voice_ids_cache = ids
        return ids

    def _resolve_voice_id(self, client, target_lang: str) -> str:
        """Returns a voice_id to actually use for target_lang: whatever's
        already cached/configured (from a prior successful call, or an
        explicit voice_ids= override at construction) if present,
        otherwise resolved fresh from the account's own premade voices —
        see the module docstring above for why this can't just be a
        hardcoded default."""
        cached = self.voice_ids.get(target_lang)
        if cached:
            return cached

        premade = self._fetch_premade_voice_ids(client)
        if not premade:
            raise RuntimeError(
                "No premade voices found on this ElevenLabs account. "
                "Every account should have at least one by default — "
                "check the API key is correct, or add/enable a voice "
                "under the account's Voices tab in the ElevenLabs "
                "dashboard."
            )
        chosen = premade[0]
        self.voice_ids[target_lang] = chosen  # cache for subsequent calls
        return chosen

    def synthesize(self, text: str, target_lang: str) -> TTSResult:
        client = self._get_client()
        voice_id = self._resolve_voice_id(client, target_lang)

        try:
            audio_bytes = self._convert(client, voice_id, text)
        except Exception as exc:  # noqa: BLE001 - vendor SDK error types vary
            msg = str(exc)
            is_library_voice_error = (
                "library voices" in msg.lower() or "payment_required" in msg.lower()
            )
            if is_library_voice_error and target_lang in self.voice_ids:
                # The cached/configured voice_id for this language turned
                # out to be a Library voice after all (e.g. it was
                # something ElevenLabs' catalog moved into that category,
                # or an explicit voice_ids= override at construction time
                # pointed at one by mistake). Drop it from the cache and
                # fall through to a fresh premade lookup instead of
                # failing the same way every single call from now on —
                # this is the actual self-healing behavior, not just a
                # nicer error message.
                self.voice_ids.pop(target_lang, None)
                self._premade_voice_ids_cache = None  # force a fresh fetch too
                try:
                    retry_voice_id = self._resolve_voice_id(client, target_lang)
                    audio_bytes = self._convert(client, retry_voice_id, text)
                except Exception as retry_exc:  # noqa: BLE001
                    raise RuntimeError(
                        f"TTS failed even after retrying with a fresh "
                        f"premade voice: {retry_exc}"
                    ) from retry_exc
            elif is_library_voice_error:
                # ElevenLabs' own error message here is accurate but
                # doesn't say what to actually do about it.
                msg += (
                    " — this voice_id is from ElevenLabs' shared Voice "
                    "Library, which requires a paid plan for API access "
                    "even though it's free to preview in their dashboard."
                )
                raise RuntimeError(msg) from exc
            else:
                raise RuntimeError(msg) from exc

        # The convert() call doesn't return duration directly; estimating
        # from mp3 byte size at the fixed bitrate above is rough but fine
        # for latency-budget bookkeeping (PRD §6). Replace with a real
        # duration probe (e.g. mutagen) if exact timing is needed downstream.
        estimated_duration_ms = (len(audio_bytes) * 8) / 128 * 1000 / 1000

        return TTSResult(audio_bytes=audio_bytes, duration_ms=estimated_duration_ms)

    def _convert(self, client, voice_id: str, text: str) -> bytes:
        audio_chunks = client.text_to_speech.convert(
            voice_id=voice_id,
            model_id=self.model_id,
            text=text,
            output_format="mp3_44100_128",
        )
        return b"".join(audio_chunks)
