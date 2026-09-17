"""
Tests for cie/calibration_log.py using moto (mocks S3 — no real network
calls, no AWS costs, no real credentials needed).

What matters here, in order:
  1. Off by default: no boto3 import, no network call, and SessionManager
     works exactly as before with zero configuration.
  2. When enabled, it actually writes anonymized JSON objects to S3.
  3. The anonymization claim is a real, checked property of every
     logged record — not just a docstring promise.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from moto import mock_aws

os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_REGION", "us-east-1")

import cie.calibration_log as calibration_log  # noqa: E402
from cie.engine import ConversationIntelligenceEngine, SignalBundle  # noqa: E402
from cie.state import ConversationState  # noqa: E402
from session.manager import SessionManager  # noqa: E402


def embedding_for(label: str) -> list[float]:
    import hashlib
    h = hashlib.sha256(label.encode()).digest()
    return [(b / 127.5) - 1.0 for b in h[:8]]


@pytest.fixture(autouse=True)
def _reset_module_state(monkeypatch):
    monkeypatch.delenv(calibration_log.ENABLED_ENV_VAR, raising=False)
    monkeypatch.delenv(calibration_log.BUCKET_ENV_VAR, raising=False)
    importlib.reload(calibration_log)
    yield
    importlib.reload(calibration_log)


def make_decision():
    cie = ConversationIntelligenceEngine(ConversationState())
    signals = SignalBundle(
        speaker_embedding=embedding_for("A"),
        turn_taking_score=0.9,
        semantic_coherence_score=0.9,
        noise_class="quiet",
    )
    decision = cie.process_utterance(signals)
    return signals, decision


def test_disabled_by_default():
    assert calibration_log.enabled() is False


def test_disabled_without_bucket_even_if_flag_set(monkeypatch):
    monkeypatch.setenv(calibration_log.ENABLED_ENV_VAR, "true")
    # No bucket configured — must stay disabled rather than error later.
    assert calibration_log.enabled() is False


def test_log_decision_is_a_true_no_op_when_disabled():
    """No mock_aws context on purpose — if this touched boto3 or the
    network without real credentials it would raise, not just return
    False."""
    signals, decision = make_decision()
    assert calibration_log.log_decision(signals, decision) is False


@mock_aws
def test_log_decision_writes_anonymized_record(monkeypatch):
    monkeypatch.setenv(calibration_log.ENABLED_ENV_VAR, "true")
    monkeypatch.setenv(calibration_log.BUCKET_ENV_VAR, "voxbuddy-calibration")

    client = calibration_log._get_client()
    client.create_bucket(Bucket="voxbuddy-calibration")

    signals, decision = make_decision()
    assert calibration_log.log_decision(signals, decision) is True

    objects = client.list_objects_v2(Bucket="voxbuddy-calibration")["Contents"]
    assert len(objects) == 1
    key = objects[0]["Key"]
    assert key.startswith(f"{calibration_log.PREFIX}/")

    body = client.get_object(Bucket="voxbuddy-calibration", Key=key)["Body"].read()
    record = json.loads(body)

    # The anonymization claim, checked directly rather than trusted:
    serialized = json.dumps(record)
    assert "speaker_embedding" not in serialized
    assert "speaker_id" not in record
    assert "session" not in serialized.lower()
    for forbidden in embedding_for("A"):
        # None of the raw embedding floats leaked into the record either.
        assert forbidden not in record.values()

    # But the actually-useful signal breakdown IS present.
    assert record["role"] == decision.role.value
    assert record["confidence"] == decision.confidence
    assert record["turn_taking_score"] == decision.turn_taking_score
    assert record["semantic_coherence_score"] == decision.semantic_coherence_score
    assert record["noise_class"] == "quiet"
    assert "logged_at" in record


@mock_aws
def test_session_manager_logs_when_enabled(monkeypatch):
    monkeypatch.setenv(calibration_log.ENABLED_ENV_VAR, "true")
    monkeypatch.setenv(calibration_log.BUCKET_ENV_VAR, "voxbuddy-calibration")
    client = calibration_log._get_client()
    client.create_bucket(Bucket="voxbuddy-calibration")

    session = SessionManager()
    session.enroll_self("me")
    session._gate_with_cie("shopkeeper", turn_taking_score=0.9, semantic_coherence_score=0.9)

    objects = client.list_objects_v2(Bucket="voxbuddy-calibration").get("Contents", [])
    assert len(objects) == 1


def test_session_manager_never_touches_s3_when_disabled():
    """No mock_aws context here either — a normal SessionManager run must
    never require AWS credentials or network access at all."""
    session = SessionManager()
    session.enroll_self("me")
    result = session._gate_with_cie("shopkeeper", turn_taking_score=0.9,
                                     semantic_coherence_score=0.9)
    assert result is not None
