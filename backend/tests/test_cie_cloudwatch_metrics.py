"""
Tests for cie/cloudwatch_metrics.py using moto (mocks the AWS CloudWatch
API — no real network calls, no AWS costs, no real credentials needed).

Two things matter here:
  1. With the feature off (the default), nothing touches boto3 or the
     network at all — a plain `python -m cie.eval` / test run must never
     require AWS credentials.
  2. With it on, publishing actually delivers real data points to
     CloudWatch (verified by reading them back), and cie/eval.py's
     run_eval() streams both per-decision and summary metrics without
     any extra wiring at the call site.
"""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from moto import mock_aws

os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_REGION", "us-east-1")

import cie.cloudwatch_metrics as cloudwatch_metrics  # noqa: E402
from cie.eval import DATASET_PATH, run_eval  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_module_state(monkeypatch):
    """Every test gets a clean slate: metrics disabled unless a test opts
    in, and the module-level cached boto3 client re-created so it never
    leaks a real (or differently-mocked) client across tests."""
    monkeypatch.delenv("VOXBUDDY_CLOUDWATCH_METRICS", raising=False)
    importlib.reload(cloudwatch_metrics)
    yield
    importlib.reload(cloudwatch_metrics)


def test_disabled_by_default():
    assert cloudwatch_metrics.enabled() is False


def test_publish_is_a_true_no_op_when_disabled():
    """No moto/mock_aws context at all here on purpose — if this call
    tried to touch boto3 or the network without real credentials, it
    would raise, not just return False."""
    sent = cloudwatch_metrics.publish_decision_metrics(
        confidence=0.9, latency_ms=1.2, role="partner", partner_switched=False,
    )
    assert sent is False

    sent = cloudwatch_metrics.publish_eval_summary({"avg_confidence": 0.9})
    assert sent is False


@mock_aws
def test_publish_decision_metrics_delivers_real_data_points(monkeypatch):
    monkeypatch.setenv("VOXBUDDY_CLOUDWATCH_METRICS", "true")

    sent = cloudwatch_metrics.publish_decision_metrics(
        confidence=0.87, latency_ms=3.4, role="bystander", partner_switched=True,
        source="eval",
    )
    assert sent is True

    client = cloudwatch_metrics._get_client()
    metrics = client.list_metrics(Namespace=cloudwatch_metrics.NAMESPACE)["Metrics"]
    names = {m["MetricName"] for m in metrics}
    assert {"Confidence", "LatencyMs", "BystanderRejected", "PartnerSwitch"} <= names


@mock_aws
def test_publish_eval_summary_skips_none_valued_metrics(monkeypatch):
    monkeypatch.setenv("VOXBUDDY_CLOUDWATCH_METRICS", "true")

    # bystander_rejection_rate is None here, as _compute_metrics() returns
    # for a dataset with zero bystander rows — must be skipped, not sent
    # as a bogus zero.
    sent = cloudwatch_metrics.publish_eval_summary({
        "n_samples": 10,
        "speaker_id_accuracy": 1.0,
        "false_speaker_rate": 0.0,
        "bystander_rejection_rate": None,
        "speaker_switch_accuracy": 1.0,
        "avg_confidence": 0.82,
        "latency_ms_avg": 0.5,
        "latency_ms_p95": 1.1,
    })
    assert sent is True

    client = cloudwatch_metrics._get_client()
    metrics = client.list_metrics(Namespace=cloudwatch_metrics.NAMESPACE)["Metrics"]
    names = {m["MetricName"] for m in metrics}
    assert "BystanderRejectionRate" not in names
    assert {"SpeakerIdAccuracy", "AvgConfidence", "LatencyMsP95", "SampleCount"} <= names


@mock_aws
def test_run_eval_streams_metrics_when_enabled(monkeypatch):
    monkeypatch.setenv("VOXBUDDY_CLOUDWATCH_METRICS", "true")

    results, metrics = run_eval(DATASET_PATH)
    assert len(results) == 10  # unchanged eval behavior — see test_cie_upgrades.py

    client = cloudwatch_metrics._get_client()
    namespaces = [n["Namespace"] for n in client.list_metrics()["Metrics"]]
    assert cloudwatch_metrics.NAMESPACE in namespaces

    stats = client.get_metric_statistics(
        Namespace=cloudwatch_metrics.NAMESPACE,
        MetricName="Confidence",
        Dimensions=[{"Name": "Source", "Value": "eval"}],
        StartTime=__import__("datetime").datetime(1970, 1, 1),
        EndTime=__import__("datetime").datetime(2100, 1, 1),
        Period=3600,
        Statistics=["SampleCount"],
    )
    # One Confidence data point per sample in the bundled dataset.
    total_samples = sum(p["SampleCount"] for p in stats["Datapoints"])
    assert total_samples == len(results)


def test_run_eval_unaffected_when_disabled():
    """No mock_aws context here either — proves run_eval() never imports
    boto3 or touches the network by default."""
    results, metrics = run_eval(DATASET_PATH)
    assert len(results) == 10
    assert metrics["speaker_id_accuracy"] == 1.0
