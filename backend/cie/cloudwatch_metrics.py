"""
Amazon CloudWatch custom metrics for the Conversation Intelligence Engine.

This is VoxBuddy's third AWS integration point (alongside Amazon Polly TTS
and DynamoDB persistence — see docs/AWS_INTEGRATION.md). It doesn't add any
new metrics: cie/eval.py already computes exactly the numbers a judge or a
PM would ask for (confidence, bystander-rejection rate, speaker-switch
accuracy, latency) — this module just gives them somewhere to go besides
stdout, so "we monitor our AI decision engine in production" is something
you can actually open a CloudWatch dashboard and show, not just a claim.

Two publish functions, matching the two kinds of number the CIE produces:

  publish_decision_metrics(...)
      Per-utterance metrics (Confidence, LatencyMs, BystanderRejected,
      PartnerSwitch) — one CIE decision, one data point. Called once per
      row from cie/eval.py's loop today. Nothing about the call site is
      eval-specific: session/manager.py could call the exact same function
      from the live pipeline once real audio is flowing, and the same
      CloudWatch dashboard built against eval runs would then be showing
      production traffic instead — that's the "we monitor this in
      production" story, not a separate thing that needs building later.

  publish_eval_summary(...)
      The once-per-run aggregate metrics from cie/eval.py's
      _compute_metrics() (speaker_id_accuracy, false_speaker_rate,
      bystander_rejection_rate, speaker_switch_accuracy, avg_confidence,
      latency p50/p95, sample count). These only mean anything against a
      labeled dataset, so unlike publish_decision_metrics this one stays
      eval-only.

Design notes (same philosophy as agents/tts_polly.py and
persistence_dynamodb.py):
  - boto3 is imported lazily, inside _get_client(), so it's only ever
    touched when metrics are actually enabled — nothing here adds a hard
    dependency or a network call to a normal `python -m cie.eval` run.
  - Off by default. Set VOXBUDDY_CLOUDWATCH_METRICS=true to turn this on;
    every publish_* call is a cheap no-op otherwise (an environment lookup,
    nothing more) so it's safe to leave the call sites in place
    unconditionally, including in the test suite.
  - Publish failures (missing credentials, no network, throttling) are
    logged and swallowed, never raised. A judge watching a live demo
    should never see a metrics call take down the eval harness or the
    pipeline — this is observability, not a critical path.
  - AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY / AWS_REGION are the same
    three environment variables Polly and DynamoDB already read (see
    backend/.env.example) — no separate credentials needed.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

ENABLED_ENV_VAR = "VOXBUDDY_CLOUDWATCH_METRICS"
NAMESPACE = os.environ.get("VOXBUDDY_CLOUDWATCH_NAMESPACE", "VoxBuddy/CIE")

# CloudWatch's PutMetricData hard limit — batch anything bigger than this.
_MAX_METRICS_PER_CALL = 20

_client = None


def enabled() -> bool:
    """Whether metric publishing is turned on. Checked live (not cached)
    so tests can flip the env var with monkeypatch mid-run."""
    return os.environ.get(ENABLED_ENV_VAR, "").strip().lower() in ("1", "true", "yes")


def _get_client():
    global _client
    if _client is None:
        import boto3  # lazy import — optional unless metrics are enabled

        _client = boto3.client("cloudwatch", region_name=os.environ.get("AWS_REGION"))
    return _client


def _datum(name: str, value: float | None, unit: str = "None",
           dimensions: list[dict] | None = None) -> dict | None:
    if value is None:
        return None
    datum: dict = {"MetricName": name, "Value": float(value), "Unit": unit}
    if dimensions:
        datum["Dimensions"] = dimensions
    return datum


def _put(metric_data: list[dict]) -> bool:
    """Send already-built MetricDatum dicts to CloudWatch. No-op (and no
    boto3 import) unless publishing is enabled. Never raises."""
    if not metric_data or not enabled():
        return False
    try:
        client = _get_client()
        for i in range(0, len(metric_data), _MAX_METRICS_PER_CALL):
            client.put_metric_data(
                Namespace=NAMESPACE,
                MetricData=metric_data[i:i + _MAX_METRICS_PER_CALL],
            )
        return True
    except Exception as exc:  # pragma: no cover - network/credentials failures
        logger.warning("CloudWatch metric publish failed (non-fatal): %s", exc)
        return False


def publish_decision_metrics(*, confidence: float, latency_ms: float, role: str,
                              partner_switched: bool = False, source: str = "eval") -> bool:
    """One CIE decision -> one set of CloudWatch data points.

    `source` becomes a CloudWatch dimension ("eval" vs "live") so eval-run
    traffic and real production traffic land in the same namespace/metrics
    but can still be filtered/graphed separately.
    """
    dims = [{"Name": "Source", "Value": source}]
    data = [
        _datum("Confidence", confidence, dimensions=dims),
        _datum("LatencyMs", latency_ms, unit="Milliseconds", dimensions=dims),
        _datum("BystanderRejected", 0.0 if role == "partner" else 1.0, dimensions=dims),
        _datum("PartnerSwitch", 1.0 if partner_switched else 0.0, dimensions=dims),
    ]
    return _put([d for d in data if d is not None])


# metrics-dict key -> (CloudWatch metric name, unit). Ratios (0-1) get
# rescaled to CloudWatch's 0-100 "Percent" unit; everything else passes
# through as-is. Matches the keys _compute_metrics() in cie/eval.py returns.
_SUMMARY_METRIC_MAP: dict[str, tuple[str, str]] = {
    "speaker_id_accuracy": ("SpeakerIdAccuracy", "Percent"),
    "false_speaker_rate": ("FalseSpeakerRate", "Percent"),
    "bystander_rejection_rate": ("BystanderRejectionRate", "Percent"),
    "speaker_switch_accuracy": ("SpeakerSwitchAccuracy", "Percent"),
    "avg_confidence": ("AvgConfidence", "None"),
    "latency_ms_avg": ("LatencyMsAvg", "Milliseconds"),
    "latency_ms_p95": ("LatencyMsP95", "Milliseconds"),
    "n_samples": ("SampleCount", "Count"),
}


def publish_eval_summary(metrics: dict, source: str = "eval") -> bool:
    """Push the once-per-run aggregate dict cie/eval.py's _compute_metrics()
    produces. Ratios (None-able, since e.g. bystander_rejection_rate is
    None when a dataset has no bystander rows) are skipped rather than
    sent as zero."""
    dims = [{"Name": "Source", "Value": source}]
    data = []
    for key, (metric_name, unit) in _SUMMARY_METRIC_MAP.items():
        value = metrics.get(key)
        if value is None:
            continue
        if unit == "Percent":
            value = value * 100
        data.append(_datum(metric_name, value, unit=unit, dimensions=dims))
    return _put(data)
