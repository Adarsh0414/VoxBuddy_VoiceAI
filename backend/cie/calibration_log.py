"""
Opt-in S3 logging of anonymized CIE signal tuples — the calibration data
flywheel.

The gap this closes (see PROGRESS.md, "What's still genuinely open"):
the CIE's fusion weights (WEIGHT_VOICE_SIMILARITY / WEIGHT_TURN_TAKING /
WEIGHT_SEMANTIC_COHERENCE in cie/engine.py) are hand-set, not tuned
against real recorded multi-speaker audio — and there was no mechanism to
even collect that data. That's still true after this file: nothing here
runs a calibration, and nothing here fabricates ground-truth labels for
data that doesn't exist yet. What this adds is the missing plumbing —
somewhere for real signal tuples to accumulate in production so that
whenever a labeling process exists (see cie/calibration.py's
export_for_labeling for the intended shape of that follow-up), there's
real data waiting instead of nothing.

What gets logged (anonymized by construction, not by scrubbing):
  - The CIE's already-computed signal breakdown (CIEDecision's
    voice_similarity / turn_taking_score / semantic_coherence_score /
    weight_* / *_threshold / noise_profile fields — see cie/engine.py) —
    plain floats and short enum-like strings.
  - The decision outcome (role, confidence, partner_switched/joined).
  - A coarse UTC timestamp.

What never gets logged, deliberately:
  - `speaker_embedding` / any raw voice-similarity vector. This would be
    the one field capable of re-identifying someone (a biometric voice
    signature), so it's excluded at the source rather than redacted after
    the fact — the logger's input type (CIEDecision's signal breakdown)
    doesn't carry it at all, so there's no vector to accidentally forget
    to strip.
  - speaker_id, session/user id, ASR text, translated text, or target
    language. Calibrating fusion *weights* only needs the numeric signal
    values and whether the fused decision was right — not who said what.

Consent model for this PoC: this is a single operator-wide opt-in
(VOXBUDDY_CALIBRATION_LOGGING=true), not a per-user toggle — appropriate
for a PoC/hackathon deployment with one operator and no real production
user base yet. The PRD (docs/VoxBuddy_PRD_and_Architecture.md's Privacy
Agent / ConsentRecord data model) already has the right hook for a real
per-user consent flag later (e.g. "share anonymized decision signals to
improve accuracy"); this file's `enabled()` check is exactly where that
would plug in once that consent field exists.

Design notes (same philosophy as agents/tts_polly.py,
persistence_dynamodb.py, cie/cloudwatch_metrics.py): boto3 imported
lazily, off by default, publish failures logged and swallowed rather than
raised — a labeling pipeline being unreachable should never take down a
real conversation.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - import cycle avoidance only
    from .engine import CIEDecision, SignalBundle

logger = logging.getLogger(__name__)

ENABLED_ENV_VAR = "VOXBUDDY_CALIBRATION_LOGGING"
BUCKET_ENV_VAR = "VOXBUDDY_CALIBRATION_BUCKET"
PREFIX = os.environ.get("VOXBUDDY_CALIBRATION_PREFIX", "signal-tuples")

# Fields copied out of CIEDecision's signal breakdown. Anything not in
# this list (speaker_id, language, notes, ...) is never touched, let
# alone logged — see module docstring.
_DECISION_FIELDS = (
    "role", "confidence", "partner_switched", "partner_joined",
    "voice_similarity", "turn_taking_score", "semantic_coherence_score",
    "weight_voice", "weight_turn", "weight_semantic",
    "bystander_threshold", "lock_threshold", "noise_profile",
)

_client = None


def enabled() -> bool:
    """Whether calibration logging is turned on. Checked live (not
    cached) so tests can flip the env var with monkeypatch mid-run."""
    if os.environ.get(ENABLED_ENV_VAR, "").strip().lower() not in ("1", "true", "yes"):
        return False
    return bool(os.environ.get(BUCKET_ENV_VAR, "").strip())


def _get_client():
    global _client
    if _client is None:
        import boto3  # lazy import — optional unless logging is enabled

        _client = boto3.client("s3", region_name=os.environ.get("AWS_REGION"))
    return _client


def _build_record(signals: "SignalBundle", decision: "CIEDecision") -> dict:
    record = {field: getattr(decision, field) for field in _DECISION_FIELDS}
    record["role"] = getattr(record["role"], "value", record["role"])
    # noise_class is the raw per-utterance ambient estimate the caller
    # supplied (e.g. "quiet"/"loud_crowd") — distinct from, and useful
    # alongside, decision.noise_profile ("clean"/"noisy", the *bucketed*
    # profile the engine actually applied thresholds/weights for).
    record["noise_class"] = signals.noise_class
    record["logged_at"] = time.time()
    return record


def log_decision(signals: "SignalBundle", decision: "CIEDecision") -> bool:
    """Log one anonymized signal tuple for one CIE decision. No-op (and no
    boto3 import) unless VOXBUDDY_CALIBRATION_LOGGING=true and
    VOXBUDDY_CALIBRATION_BUCKET is set. Never raises."""
    if not enabled():
        return False
    try:
        record = _build_record(signals, decision)
        bucket = os.environ["VOXBUDDY_CALIBRATION_BUCKET"]
        day = time.strftime("%Y-%m-%d", time.gmtime(record["logged_at"]))
        key = f"{PREFIX}/{day}/{uuid.uuid4().hex}.json"
        _get_client().put_object(
            Bucket=bucket,
            Key=key,
            Body=json.dumps(record).encode("utf-8"),
            ContentType="application/json",
        )
        return True
    except Exception as exc:  # pragma: no cover - network/credentials failures
        logger.warning("Calibration data log to S3 failed (non-fatal): %s", exc)
        return False
