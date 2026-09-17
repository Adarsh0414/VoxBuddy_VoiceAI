"""
Tests for cie/calibration.py using moto (mocks S3). Seeds a mock bucket
with known JSON records (as calibration_log.py would have written them)
and exercises the three offline utilities against it.
"""

from __future__ import annotations

import csv
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import boto3
import pytest
from moto import mock_aws

os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_REGION", "us-east-1")

from cie import calibration, calibration_log  # noqa: E402

BUCKET = "voxbuddy-calibration"

SAMPLE_RECORDS = [
    {"role": "partner", "confidence": 0.9, "voice_similarity": 0.8,
     "turn_taking_score": 0.85, "semantic_coherence_score": 0.9,
     "weight_voice": 0.45, "weight_turn": 0.25, "weight_semantic": 0.3,
     "bystander_threshold": 0.35, "lock_threshold": 0.6,
     "noise_profile": "clean", "noise_class": "quiet",
     "partner_switched": False, "partner_joined": True, "logged_at": 1000.0},
    {"role": "bystander", "confidence": 0.2, "voice_similarity": 0.1,
     "turn_taking_score": 0.1, "semantic_coherence_score": 0.15,
     "weight_voice": 0.45, "weight_turn": 0.25, "weight_semantic": 0.3,
     "bystander_threshold": 0.35, "lock_threshold": 0.6,
     "noise_profile": "clean", "noise_class": "quiet",
     "partner_switched": False, "partner_joined": False, "logged_at": 1001.0},
]


@pytest.fixture
def seeded_bucket():
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=BUCKET)
        for i, record in enumerate(SAMPLE_RECORDS):
            client.put_object(
                Bucket=BUCKET,
                Key=f"{calibration_log.PREFIX}/2026-01-01/{i}.json",
                Body=json.dumps(record).encode("utf-8"),
                ContentType="application/json",
            )
        # One deliberately corrupt object — must be skipped, not fatal.
        client.put_object(Bucket=BUCKET, Key=f"{calibration_log.PREFIX}/2026-01-01/broken.json",
                           Body=b"{not valid json")
        yield client


def test_download_signal_tuples_skips_corrupt_objects(seeded_bucket):
    records = calibration.download_signal_tuples(bucket=BUCKET)
    assert len(records) == 2
    assert {r["role"] for r in records} == {"partner", "bystander"}


def test_download_signal_tuples_respects_limit(seeded_bucket):
    records = calibration.download_signal_tuples(bucket=BUCKET, limit=1)
    assert len(records) == 1


def test_summarize_signal_distribution(seeded_bucket):
    records = calibration.download_signal_tuples(bucket=BUCKET)
    summary = calibration.summarize_signal_distribution(records)

    assert summary["partner"]["n"] == 1
    assert summary["bystander"]["n"] == 1
    assert summary["partner"]["confidence"]["mean"] == 0.9
    assert summary["bystander"]["confidence"]["mean"] == 0.2


def test_export_for_labeling_writes_blank_column_for_humans(seeded_bucket, tmp_path):
    records = calibration.download_signal_tuples(bucket=BUCKET)
    out = calibration.export_for_labeling(records, tmp_path / "for_labeling.csv")

    with open(out, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    assert len(rows) == 2
    for row in rows:
        assert row["decision_correct"] == ""
        assert row["role"] in ("partner", "bystander")
        assert row["sample_id"]  # never blank — falls back to the S3 key


def test_calibration_log_bucket_reads_env(monkeypatch):
    monkeypatch.setenv(calibration_log.BUCKET_ENV_VAR, "some-bucket")
    assert calibration.calibration_log_bucket() == "some-bucket"
