"""
Agent factory — decides at runtime whether the pipeline uses the mocked
agents (default, zero-config, what Phase 1 has been running) or a real
vendor adapter, controlled by environment variables so nobody has to edit
code to flip this.

This keeps session/manager.py decoupled from any specific vendor, per the
agents/base.py Protocol design — swapping providers is a config change,
not a code change.
"""

from __future__ import annotations

import os

from .base import StreamingASRAgent, TranslationAgent, TTSAgent
from .mocks import MockTranslationAgent


def get_translation_agent() -> TranslationAgent:
    """
    Selects the translation agent based on VOXBUDDY_TRANSLATION_PROVIDER:
      - "bedrock" (default) — BedrockTranslationAgent, the AWS-native
        option. Uses the same AWS credentials (AWS_ACCESS_KEY_ID /
        AWS_SECRET_ACCESS_KEY / AWS_REGION) already required for
        agents/tts_polly.py, so it runs with zero extra config on an
        AWS-backed deployment. Model id configurable via BEDROCK_MODEL_ID.
      - "mock" — MockTranslationAgent, no network, no API key.
      - "anthropic" — AnthropicTranslationAgent, requires ANTHROPIC_API_KEY
      - "gemini" — GeminiTranslationAgent, requires GEMINI_API_KEY. An
        optional/secondary provider for users who'd rather use their own
        Gemini API key — same TranslationAgent protocol, same
        context-injection prompt design as the others, not a lesser
        option, just not the default.

    Provider selection is explicit and never falls back silently: if
    "bedrock" is selected and the call fails (bad credentials, model not
    enabled, etc.), that error propagates rather than quietly retrying
    against Gemini or any other provider.

    Only translation has a real implementation wired up so far (see
    docs/vendor_decision.md §4) — ASR and TTS stay mocked until the async
    streaming interface work described in agents/asr_assemblyai.py is done.
    """
    provider = os.environ.get("VOXBUDDY_TRANSLATION_PROVIDER", "bedrock").lower()

    if provider == "mock":
        return MockTranslationAgent()

    if provider == "bedrock":
        from .translation_bedrock import BedrockTranslationAgent
        if not os.environ.get("AWS_ACCESS_KEY_ID") or not os.environ.get("AWS_SECRET_ACCESS_KEY"):
            raise RuntimeError(
                "VOXBUDDY_TRANSLATION_PROVIDER=bedrock requires "
                "AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY to be set "
                "(AWS_REGION optional, defaults to us-east-1)."
            )
        return BedrockTranslationAgent(
            region_name=os.environ.get("AWS_REGION"),
            model_id=os.environ.get("BEDROCK_MODEL_ID"),
        )

    if provider == "anthropic":
        from .translation_anthropic import AnthropicTranslationAgent
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError(
                "VOXBUDDY_TRANSLATION_PROVIDER=anthropic requires "
                "ANTHROPIC_API_KEY to be set."
            )
        return AnthropicTranslationAgent(api_key=api_key)

    if provider == "gemini":
        from .translation_gemini import GeminiTranslationAgent
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError(
                "VOXBUDDY_TRANSLATION_PROVIDER=gemini requires "
                "GEMINI_API_KEY to be set."
            )
        return GeminiTranslationAgent(api_key=api_key)

    raise ValueError(
        f"Unknown VOXBUDDY_TRANSLATION_PROVIDER='{provider}'. "
        f"Valid options: 'bedrock', 'mock', 'anthropic', 'gemini'."
    )


def get_streaming_asr_agent(language_code: str | None = None) -> StreamingASRAgent:
    """
    Selects the streaming ASR agent based on VOXBUDDY_ASR_PROVIDER:
      - "mock" (default) — MockStreamingASRAgent. Takes plain text tokens,
        not real audio, so it CANNOT transcribe a real microphone — it only
        proves the transport/pipeline wiring works end to end.
      - "assemblyai" — AssemblyAIStreamingASRAgent, requires
        ASSEMBLYAI_API_KEY. This is the one that turns real PCM audio from
        a microphone into text. See agents/asr_assemblyai.py's module
        docstring: scaffolded against the real SDK shape but not
        live-tested against real AssemblyAI traffic yet.
      - "aws_transcribe" — AmazonTranscribeStreamingASRAgent, the AWS-native
        alternative (see agents/asr_transcribe.py's module docstring for
        how it compares to the AssemblyAI adapter). Credentials are
        resolved via boto3's normal provider chain — environment
        variables, ~/.aws/credentials, or an EC2/Elastic Beanstalk
        instance profile — same as agents/tts_polly.py; AWS_REGION
        optional, defaults to us-east-1. Also scaffolded but not
        live-tested.

    Added alongside the new /ws/{session_id}/audio endpoint in app.py,
    which is the first caller that pushes real audio bytes through this
    agent rather than mock text tokens.

    :param language_code:
        The ASR/source language — the language actually being SPOKEN into
        the microphone (e.g. "hi-IN", "en-US"), as opposed to the
        translation *target* language (see session/streaming_manager.py's
        target_lang, which is a completely separate concept — the language
        the translated text/speech should come OUT in). Only
        AmazonTranscribeStreamingASRAgent currently accepts a language at
        construction time, so this is a no-op for "mock"/"assemblyai".
        Falls back to "en-US" if omitted, matching
        AmazonTranscribeStreamingASRAgent's own default — existing callers
        that don't pass this keep working exactly as before.
    """
    provider = os.environ.get("VOXBUDDY_ASR_PROVIDER", "mock").lower()

    if provider == "mock":
        from .mock_streaming_asr import MockStreamingASRAgent
        return MockStreamingASRAgent()

    if provider == "assemblyai":
        from .asr_assemblyai import AssemblyAIStreamingASRAgent
        api_key = os.environ.get("ASSEMBLYAI_API_KEY")
        if not api_key:
            raise RuntimeError(
                "VOXBUDDY_ASR_PROVIDER=assemblyai requires "
                "ASSEMBLYAI_API_KEY to be set."
            )
        return AssemblyAIStreamingASRAgent(api_key=api_key)

    if provider == "aws_transcribe":
        from .asr_transcribe import AmazonTranscribeStreamingASRAgent
        # No explicit static-credential check: boto3's normal provider
        # chain resolves credentials on its own (env vars,
        # ~/.aws/credentials, or an EC2/Elastic Beanstalk instance
        # profile). If nothing is resolvable, boto3 raises
        # NoCredentialsError on the first real Transcribe call.
        return AmazonTranscribeStreamingASRAgent(
            region=os.environ.get("AWS_REGION"),
            language_code=language_code or "en-US",
        )

    raise ValueError(
        f"Unknown VOXBUDDY_ASR_PROVIDER='{provider}'. "
        f"Valid options: 'mock', 'assemblyai', 'aws_transcribe'."
    )


def get_tts_agent() -> tuple[TTSAgent, str]:
    """
    Selects the TTS agent based on VOXBUDDY_TTS_PROVIDER, and returns the
    audio format it produces alongside it — callers (session/manager.py,
    then app.py's _to_out) need the format to tell the frontend whether
    audio_b64 is actually playable audio or the mock's placeholder bytes:
      - "mock" (default) — MockTTSAgent. audio_bytes is just the input
        text UTF-8 encoded, NOT audio. Format returned: "mock-text" — the
        frontend must not try to play this through an <audio> element.
      - "elevenlabs" — ElevenLabsTTSAgent, requires ELEVENLABS_API_KEY.
        Produces real mp3 bytes. Format returned: "mp3".
      - "polly" — PollyTTSAgent (Amazon Polly, see agents/tts_polly.py),
        the AWS-native TTS option. Credentials are resolved via boto3's
        normal provider chain — environment variables,
        ~/.aws/credentials, or an EC2/Elastic Beanstalk instance profile
        — so no separate VoxBuddy-specific key is needed here.
        AWS_REGION optional. Produces real mp3 bytes. Format returned:
        "mp3".
    """
    provider = os.environ.get("VOXBUDDY_TTS_PROVIDER", "mock").lower()

    if provider == "mock":
        from .mocks import MockTTSAgent
        return MockTTSAgent(), "mock-text"

    if provider == "elevenlabs":
        from .tts_elevenlabs import ElevenLabsTTSAgent
        api_key = os.environ.get("ELEVENLABS_API_KEY")
        if not api_key:
            raise RuntimeError(
                "VOXBUDDY_TTS_PROVIDER=elevenlabs requires "
                "ELEVENLABS_API_KEY to be set."
            )
        return ElevenLabsTTSAgent(api_key=api_key), "mp3"

    if provider == "polly":
        from .tts_polly import PollyTTSAgent
        # No explicit static-credential check: boto3's normal provider
        # chain resolves credentials on its own (env vars,
        # ~/.aws/credentials, or an EC2/Elastic Beanstalk instance
        # profile). If nothing is resolvable, boto3 raises
        # NoCredentialsError on the first real Polly call.
        return PollyTTSAgent(region_name=os.environ.get("AWS_REGION")), "mp3"

    raise ValueError(
        f"Unknown VOXBUDDY_TTS_PROVIDER='{provider}'. "
        f"Valid options: 'mock', 'elevenlabs', 'polly'."
    )
