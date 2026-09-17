"""
CIE Evaluation Harness

The single biggest thing missing from "we improved the CIE" is proof. This
module runs the engine against a labeled test set (backend/cie/eval_dataset.csv)
and reports the numbers a judge or a PM would actually want:

    Audio         Actual Speaker   CIE Prediction   Confidence
    sample_001    A                A                0.94
    sample_002    B                B                0.87
    sample_003    A                B                0.61
    sample_004    Noise            B                0.32

    -> Speaker identification accuracy
    -> False speaker detection rate
    -> Bystander rejection rate
    -> Speaker-switch accuracy
    -> Average confidence
    -> Latency (per-utterance, p50/p95)

Usage:
    python -m cie.eval                      # uses the bundled sample dataset
    python -m cie.eval path/to/dataset.csv

Set VOXBUDDY_CLOUDWATCH_METRICS=true (AWS credentials already configured,
see backend/.env.example) to also stream every metric below live to
CloudWatch as it's computed, instead of only printing the report at the
end — see cie/cloudwatch_metrics.py. Off by default; nothing changes for
a plain local/CI run.

Dataset format (CSV, one row per synthetic "audio" sample):
    sample_id, actual_speaker, turn_taking, coherence, noise_class, expect_role, expect_switch, age_seconds

  - actual_speaker: a label such as "A", "B", "Noise". Two labels joined
    with "+" (e.g. "A+B") produce a 50/50 blended embedding — a stand-in for
    a hard, ambiguous, or overlapping-voices sample, since this PoC has no
    real audio to draw a genuinely confusable sample from.
  - expect_role: "partner" or "bystander" — the ground truth this sample
    is meant to exercise. "bystander" means "should NOT be accepted as an
    active partner this turn" — it's satisfied by either a BYSTANDER or an
    UNKNOWN decision, since both correctly withhold translation.
  - expect_switch: "true"/"false", optional (blank = not evaluated) — set
    on rows meant to test whether a partner-group switch fires when it
    should (or, just as often, correctly DOESN'T fire yet).
  - age_seconds: optional, default 0 — simulates elapsed wall-clock time
    since the previous row by pushing every currently-tracked speaker's
    `last_seen` back by this many seconds *before* the row is processed.
    This is what makes expect_switch rows meaningful in a flat, otherwise-
    instantaneous CSV: a row with age_seconds >= PARTNER_ABSENCE_TIMEOUT_S
    makes the group's longest-silent member eligible for replacement,
    exactly the mechanism tests/test_cie.py's dedicated switch tests
    exercise directly (`speaker.last_seen = time.time() - N`), just
    driven from the dataset instead of hardcoded in a test body.

IMPORTANT CAVEAT: speaker embeddings here are synthetic (sha256-hash based,
same helper the test suite uses) rather than real d-vectors from an audio
sample, so "speaker identification accuracy" measures whether the engine's
matching/hysteresis/gating LOGIC resolves each row to the identity a human
labeler intended — not real-world voice-recognition accuracy. Swap
`embedding_for` for a real embedding-agent call once real audio is
available; every metric below stays valid against that column shape.
"""

from __future__ import annotations

import csv
import hashlib
import statistics
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from . import cloudwatch_metrics
from .engine import ConversationIntelligenceEngine, SignalBundle
from .state import ConversationState, SpeakerRole

DATASET_PATH = Path(__file__).parent / "eval_dataset.csv"


def embedding_for(label: str) -> list[float]:
    h = hashlib.sha256(label.encode()).digest()
    return [(b / 127.5) - 1.0 for b in h[:8]]


def _resolve_embedding(actual_speaker: str) -> list[float]:
    if "+" in actual_speaker:
        a, b = actual_speaker.split("+", 1)
        ea, eb = embedding_for(a), embedding_for(b)
        return [(x + y) / 2 for x, y in zip(ea, eb)]
    return embedding_for(actual_speaker)


@dataclass
class EvalRow:
    sample_id: str
    actual_speaker: str
    turn_taking: float
    coherence: float
    noise_class: str
    expect_role: str
    expect_switch: bool | None
    age_seconds: float = 0.0


@dataclass
class EvalResult:
    row: EvalRow
    predicted_label: str
    predicted_role: str
    confidence: float
    partner_switched: bool
    latency_ms: float
    notes: str


def load_dataset(path: Path = DATASET_PATH) -> list[EvalRow]:
    rows = []
    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            switch_raw = (r.get("expect_switch") or "").strip().lower()
            age_raw = (r.get("age_seconds") or "").strip()
            rows.append(EvalRow(
                sample_id=r["sample_id"],
                actual_speaker=r["actual_speaker"].strip(),
                turn_taking=float(r["turn_taking"]),
                coherence=float(r["coherence"]),
                noise_class=(r.get("noise_class") or "unknown").strip(),
                expect_role=r["expect_role"].strip().lower(),
                expect_switch=None if switch_raw == "" else switch_raw == "true",
                age_seconds=float(age_raw) if age_raw else 0.0,
            ))
    return rows


def run_eval(path: Path = DATASET_PATH) -> tuple[list[EvalResult], dict]:
    rows = load_dataset(path)
    cie = ConversationIntelligenceEngine(ConversationState())

    # First time a given ground-truth label is resolved to a speaker_id, that
    # id becomes "the" identity for that label for the rest of the run — lets
    # us score identification without the dataset knowing spk_N ids up front.
    label_to_id: dict[str, str] = {}
    results: list[EvalResult] = []
    latencies: list[float] = []

    for row in rows:
        if row.age_seconds:
            # Simulate elapsed time since the previous row: push every
            # currently-tracked speaker's last_seen back so absence-timeout
            # logic (partner switch, stale pruning) has something real to
            # measure against, instead of everything happening at t=~0.
            for spk in cie.state.speakers.values():
                spk.last_seen -= row.age_seconds

        embedding = _resolve_embedding(row.actual_speaker)
        signals = SignalBundle(
            speaker_embedding=embedding,
            turn_taking_score=row.turn_taking,
            semantic_coherence_score=row.coherence,
            noise_class=row.noise_class,
        )

        t0 = time.perf_counter()
        decision = cie.process_utterance(signals)
        latency_ms = (time.perf_counter() - t0) * 1000
        latencies.append(latency_ms)

        label_to_id.setdefault(row.actual_speaker, decision.speaker_id)
        predicted_label = next(
            (label for label, sid in label_to_id.items() if sid == decision.speaker_id),
            decision.speaker_id,
        )

        results.append(EvalResult(
            row=row,
            predicted_label=predicted_label,
            predicted_role=decision.role.value,
            confidence=decision.confidence,
            partner_switched=decision.partner_switched,
            latency_ms=latency_ms,
            notes=decision.notes,
        ))

        # Streams live to CloudWatch when VOXBUDDY_CLOUDWATCH_METRICS=true;
        # a cheap no-op (env lookup only, no boto3 import) otherwise — see
        # cie/cloudwatch_metrics.py.
        cloudwatch_metrics.publish_decision_metrics(
            confidence=decision.confidence,
            latency_ms=latency_ms,
            role=decision.role.value,
            partner_switched=decision.partner_switched,
            source="eval",
        )

    metrics = _compute_metrics(results, latencies)

    # Once-per-run aggregate (accuracy/rejection/switch rates, p95 latency)
    # — same enable switch, same no-op-when-disabled guarantee as above.
    cloudwatch_metrics.publish_eval_summary(metrics, source="eval")

    return results, metrics


def _compute_metrics(results: list[EvalResult], latencies: list[float]) -> dict:
    partner_rows = [r for r in results if r.row.expect_role == "partner"]
    bystander_rows = [r for r in results if r.row.expect_role == "bystander"]
    switch_rows = [r for r in results if r.row.expect_switch is not None]

    def accepted(r: EvalResult) -> bool:
        return r.predicted_role == SpeakerRole.PARTNER.value

    correct_id = sum(1 for r in partner_rows if accepted(r) and r.predicted_label == r.row.actual_speaker)
    wrong_id = sum(1 for r in partner_rows if accepted(r) and r.predicted_label != r.row.actual_speaker)
    rejected_bystanders = sum(1 for r in bystander_rows if not accepted(r))
    switch_correct = sum(1 for r in switch_rows if r.partner_switched == r.row.expect_switch)

    sorted_lat = sorted(latencies)

    def ratio(numerator: int, rows: list) -> float | None:
        return numerator / len(rows) if rows else None

    return {
        "n_samples": len(results),
        "speaker_id_accuracy": ratio(correct_id, partner_rows),
        "false_speaker_rate": ratio(wrong_id, partner_rows),
        "bystander_rejection_rate": ratio(rejected_bystanders, bystander_rows),
        "speaker_switch_accuracy": ratio(switch_correct, switch_rows),
        "avg_confidence": statistics.fmean(r.confidence for r in results),
        "latency_ms_avg": statistics.fmean(latencies),
        "latency_ms_p95": sorted_lat[int(0.95 * (len(sorted_lat) - 1))],
    }


def _fmt_pct(value: float | None) -> str:
    return "n/a (no rows)" if value is None else f"{value:.1%}"


def print_report(results: list[EvalResult], metrics: dict) -> None:
    print(f"{'Audio':<12} {'Actual':<8} {'Predicted':<10} {'Role':<10} {'Conf':<6} {'Notes'}")
    for r in results:
        print(f"{r.row.sample_id:<12} {r.row.actual_speaker:<8} {r.predicted_label:<10} "
              f"{r.predicted_role:<10} {r.confidence:<6.2f} {r.notes}")

    print()
    print(f"Speaker identification accuracy : {_fmt_pct(metrics['speaker_id_accuracy'])}")
    print(f"False speaker detection rate     : {_fmt_pct(metrics['false_speaker_rate'])}")
    print(f"Bystander rejection rate         : {_fmt_pct(metrics['bystander_rejection_rate'])}")
    print(f"Speaker-switch accuracy          : {_fmt_pct(metrics['speaker_switch_accuracy'])}")
    print(f"Average confidence               : {metrics['avg_confidence']:.2f}")
    print(f"Latency (avg / p95)              : {metrics['latency_ms_avg']:.3f}ms / "
          f"{metrics['latency_ms_p95']:.3f}ms")
    print(f"Samples evaluated                : {metrics['n_samples']}")
    if cloudwatch_metrics.enabled():
        print(f"CloudWatch                        : streamed live to "
              f"'{cloudwatch_metrics.NAMESPACE}' (VOXBUDDY_CLOUDWATCH_METRICS=true)")


if __name__ == "__main__":
    dataset = Path(sys.argv[1]) if len(sys.argv) > 1 else DATASET_PATH
    results, metrics = run_eval(dataset)
    print_report(results, metrics)
