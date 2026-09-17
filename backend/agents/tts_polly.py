"""
Amazon Polly text-to-speech adapter.

This is VoxBuddy's AWS integration for the speech-output stage of the
pipeline. It implements the same synchronous TTSAgent protocol from
agents/base.py that agents/tts_elevenlabs.py implements — so it's a
drop-in alternative selected purely by VOXBUDDY_TTS_PROVIDER=polly (see
agents/factory.py), with zero changes anywhere else in the pipeline
(session/manager.py, app.py).

Unlike ElevenLabs' voice catalog (which is account-specific and changes
over time — see the bug history in tts_elevenlabs.py), Polly's standard
and neural voices are a fixed, documented AWS catalog that's stable across
every AWS account, so a static language->voice map here is the *correct*
approach rather than the workaround ElevenLabs needed.

Auth: boto3 picks up AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY / AWS_REGION
(or AWS_DEFAULT_REGION) from the environment automatically — nothing
VoxBuddy-specific to configure beyond those three variables in .env.
"""

from __future__ import annotations

import os

from .base import TTSResult

# Neural-engine voice per language. Neural voices sound materially better
# than 'standard' but aren't available for every language/region — engine
# selection happens per-voice below, with a 'standard' fallback in
# synthesize() if a neural call is rejected (e.g. because the account's
# region doesn't support neural for that voice).
VOICE_MAP: dict[str, dict[str, str]] = {
    "en": {"VoiceId": "Joanna", "Engine": "neural", "LanguageCode": "en-US"},
    "es": {"VoiceId": "Lupe", "Engine": "neural", "LanguageCode": "es-US"},
    "fr": {"VoiceId": "L\u00e9a", "Engine": "neural", "LanguageCode": "fr-FR"},
    "de": {"VoiceId": "Vicki", "Engine": "neural", "LanguageCode": "de-DE"},
    "it": {"VoiceId": "Bianca", "Engine": "neural", "LanguageCode": "it-IT"},
    "pt": {"VoiceId": "Camila", "Engine": "neural", "LanguageCode": "pt-BR"},
    "hi": {"VoiceId": "Kajal", "Engine": "neural", "LanguageCode": "en-IN"},
    "ja": {"VoiceId": "Takumi", "Engine": "neural", "LanguageCode": "ja-JP"},
    "ko": {"VoiceId": "Seoyeon", "Engine": "neural", "LanguageCode": "ko-KR"},
    "zh": {"VoiceId": "Zhiyu", "Engine": "neural", "LanguageCode": "cmn-CN"},
    "ar": {"VoiceId": "Hala", "Engine": "neural", "LanguageCode": "ar-AE"},
    "ru": {"VoiceId": "Tatyana", "Engine": "standard", "LanguageCode": "ru-RU"},
    "nl": {"VoiceId": "Laura", "Engine": "neural", "LanguageCode": "nl-NL"},
    "pl": {"VoiceId": "Ola", "Engine": "neural", "LanguageCode": "pl-PL"},
    "tr": {"VoiceId": "Filiz", "Engine": "standard", "LanguageCode": "tr-TR"},
}

# Used for any target_lang not in VOICE_MAP above.
DEFAULT_VOICE = VOICE_MAP["en"]


class PollyTTSAgent:
    def __init__(self, region_name: str | None = None):
        self.region_name = region_name or os.environ.get("AWS_REGION") or os.environ.get(
            "AWS_DEFAULT_REGION", "us-east-1"
        )
        self._client = None

    def _get_client(self):
        if self._client is None:
            import boto3  # lazy import — keeps boto3 optional unless polly is selected

            self._client = boto3.client("polly", region_name=self.region_name)
        return self._client

    def synthesize(self, text: str, target_lang: str) -> TTSResult:
        client = self._get_client()
        voice = VOICE_MAP.get(target_lang, DEFAULT_VOICE)

        try:
            audio_bytes = self._call_polly(client, text, voice["VoiceId"], voice["Engine"])
        except Exception as exc:  # noqa: BLE001 - boto3 raises botocore.ClientError
            msg = str(exc)
            # Neural engine unsupported in this region/account for this
            # voice -> retry once on 'standard', which every Polly voice
            # supports everywhere. Mirrors the self-healing pattern in
            # tts_elevenlabs.py rather than failing the whole request.
            if voice["Engine"] == "neural" and (
                "engine" in msg.lower() or "validationexception" in msg.lower()
            ):
                try:
                    audio_bytes = self._call_polly(client, text, voice["VoiceId"], "standard")
                except Exception as retry_exc:  # noqa: BLE001
                    raise RuntimeError(
                        f"Polly synthesis failed even after falling back to the "
                        f"standard engine: {retry_exc}"
                    ) from retry_exc
            else:
                raise RuntimeError(f"Polly synthesis failed: {msg}") from exc

        # Polly returns raw mp3 bytes with no duration header; same rough
        # bitrate-based estimate used by the ElevenLabs adapter (mp3 at a
        # nominal 64kbps default for Polly's standard output).
        estimated_duration_ms = (len(audio_bytes) * 8) / 64 * 1000 / 1000

        return TTSResult(audio_bytes=audio_bytes, duration_ms=estimated_duration_ms)

    def _call_polly(self, client, text: str, voice_id: str, engine: str) -> bytes:
        response = client.synthesize_speech(
            Text=text,
            OutputFormat="mp3",
            VoiceId=voice_id,
            Engine=engine,
        )
        return response["AudioStream"].read()
