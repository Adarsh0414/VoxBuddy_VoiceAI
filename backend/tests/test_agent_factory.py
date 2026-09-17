"""Tests for agents/factory.py's get_streaming_asr_agent() provider
selection — specifically the aws_transcribe option added alongside
agents/asr_transcribe.py. mock/assemblyai selection already worked before
this change; included here for completeness since there was no dedicated
factory test file yet."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from agents.factory import get_streaming_asr_agent


def test_default_provider_is_mock(monkeypatch):
    monkeypatch.delenv("VOXBUDDY_ASR_PROVIDER", raising=False)
    from agents.mock_streaming_asr import MockStreamingASRAgent
    assert isinstance(get_streaming_asr_agent(), MockStreamingASRAgent)


def test_assemblyai_provider_requires_api_key(monkeypatch):
    monkeypatch.setenv("VOXBUDDY_ASR_PROVIDER", "assemblyai")
    monkeypatch.delenv("ASSEMBLYAI_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="ASSEMBLYAI_API_KEY"):
        get_streaming_asr_agent()


def test_assemblyai_provider_selected_when_configured(monkeypatch):
    monkeypatch.setenv("VOXBUDDY_ASR_PROVIDER", "assemblyai")
    monkeypatch.setenv("ASSEMBLYAI_API_KEY", "fake-key")
    from agents.asr_assemblyai import AssemblyAIStreamingASRAgent
    agent = get_streaming_asr_agent()
    assert isinstance(agent, AssemblyAIStreamingASRAgent)
    assert agent.api_key == "fake-key"


def test_aws_transcribe_provider_requires_credentials(monkeypatch):
    monkeypatch.setenv("VOXBUDDY_ASR_PROVIDER", "aws_transcribe")
    monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("AWS_SECRET_ACCESS_KEY", raising=False)
    with pytest.raises(RuntimeError, match="AWS_ACCESS_KEY_ID"):
        get_streaming_asr_agent()


def test_aws_transcribe_provider_selected_when_configured(monkeypatch):
    monkeypatch.setenv("VOXBUDDY_ASR_PROVIDER", "aws_transcribe")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "fake-access-key")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "fake-secret-key")
    monkeypatch.setenv("AWS_REGION", "eu-west-1")
    from agents.asr_transcribe import AmazonTranscribeStreamingASRAgent
    agent = get_streaming_asr_agent()
    assert isinstance(agent, AmazonTranscribeStreamingASRAgent)
    assert agent.region == "eu-west-1"


def test_aws_transcribe_provider_works_with_only_required_env(monkeypatch):
    """AWS_REGION is optional — the agent should still be constructed (and
    fall back to us-east-1 internally) with just the two credential vars."""
    monkeypatch.setenv("VOXBUDDY_ASR_PROVIDER", "aws_transcribe")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "fake-access-key")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "fake-secret-key")
    monkeypatch.delenv("AWS_REGION", raising=False)
    agent = get_streaming_asr_agent()
    assert agent.region == "us-east-1"


def test_aws_transcribe_provider_uses_default_language_when_none_given(monkeypatch):
    """No language_code passed in -> falls back to en-US, same as before
    this parameter existed, so nothing breaks for existing callers."""
    monkeypatch.setenv("VOXBUDDY_ASR_PROVIDER", "aws_transcribe")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "fake-access-key")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "fake-secret-key")
    agent = get_streaming_asr_agent()
    assert agent.language_code == "en-US"


def test_aws_transcribe_provider_uses_given_language_code(monkeypatch):
    """A language_code passed in reaches the agent unchanged -- this is
    what lets the ASR source language actually vary (e.g. Hindi speech),
    independent of whatever the translation target_lang is set to."""
    monkeypatch.setenv("VOXBUDDY_ASR_PROVIDER", "aws_transcribe")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "fake-access-key")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "fake-secret-key")
    agent = get_streaming_asr_agent(language_code="hi-IN")
    assert agent.language_code == "hi-IN"


def test_unknown_asr_provider_raises_clear_error(monkeypatch):
    monkeypatch.setenv("VOXBUDDY_ASR_PROVIDER", "some_made_up_vendor")
    with pytest.raises(ValueError, match="Unknown VOXBUDDY_ASR_PROVIDER"):
        get_streaming_asr_agent()
