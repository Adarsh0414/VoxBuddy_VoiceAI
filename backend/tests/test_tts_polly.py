"""
Tests for agents/tts_polly.py using moto (mocks the AWS Polly API — no
real network calls, no AWS costs, no credentials needed to run this
suite). Mirrors the existing test_tts_wiring.py pattern: verify the
TTSAgent contract is honored (TTSResult with real bytes + duration) and
that the fallback/error-handling logic actually triggers correctly.
"""

from __future__ import annotations

import os

import pytest
from moto import mock_aws

os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_REGION", "us-east-1")

from agents.tts_polly import PollyTTSAgent, VOICE_MAP  # noqa: E402


@mock_aws
def test_synthesize_returns_real_audio_bytes():
    agent = PollyTTSAgent(region_name="us-east-1")
    result = agent.synthesize("Hello there", target_lang="en")

    assert isinstance(result.audio_bytes, bytes)
    assert len(result.audio_bytes) > 0
    assert result.duration_ms > 0


@mock_aws
def test_unknown_language_falls_back_to_default_voice():
    agent = PollyTTSAgent(region_name="us-east-1")
    # "xx" isn't in VOICE_MAP — should still succeed using DEFAULT_VOICE
    # rather than raising a KeyError.
    result = agent.synthesize("Bonjour", target_lang="xx")
    assert len(result.audio_bytes) > 0


@mock_aws
def test_every_mapped_language_synthesizes_successfully():
    agent = PollyTTSAgent(region_name="us-east-1")
    for lang in VOICE_MAP:
        result = agent.synthesize("test", target_lang=lang)
        assert len(result.audio_bytes) > 0


def test_neural_failure_falls_back_to_standard_engine(monkeypatch):
    """Simulates a region/account rejecting the neural engine for a given
    voice (a real, documented Polly failure mode) and verifies the agent
    retries on 'standard' instead of failing the whole request."""
    agent = PollyTTSAgent(region_name="us-east-1")

    calls = []

    class FakeClient:
        def synthesize_speech(self, Text, OutputFormat, VoiceId, Engine):
            calls.append(Engine)
            if Engine == "neural":
                raise Exception("ValidationException: engine not supported in this region")
            return {"AudioStream": _FakeStream(b"standard-engine-audio")}

    class _FakeStream:
        def __init__(self, data):
            self._data = data

        def read(self):
            return self._data

    agent._client = FakeClient()
    result = agent.synthesize("hi", target_lang="en")

    assert calls == ["neural", "standard"]
    assert result.audio_bytes == b"standard-engine-audio"
