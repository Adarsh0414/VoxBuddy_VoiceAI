"""
Turning logged calibration data (cie/calibration_log.py) back into
something a human — or, eventually, a real weight-fitting script — can
use. This is deliberately the *second half* of the flywheel, kept
separate from the live-logging path so nothing here needs to run in the
hot path or in production at all.

Three things, in the order a real calibration effort would actually use
them:

  1. download_signal_tuples()   — pull the logged JSON objects back out
     of S3 into plain dicts.
  2. summarize_signal_distribution() — quick descriptive stats (by
     predicted role) so you can eyeball, even before any labeling exists,
     whether real-world signal distributions look like what the
     hand-set thresholds in cie/engine.py assume.
  3. export_for_labeling()      — writes a CSV a human reviewer fills in
     by hand, one row per logged decision, with a single blank
     `decision_correct` column (was this PARTNER/BYSTANDER/UNKNOWN call
     right?). That column, not a recovered "true speaker identity", is
     the realistic ground truth here: anonymized signal tuples can't be
     reverse-mapped to who actually spoke (see calibration_log.py's
     module docstring for why that's intentional), but a person who was
     in the conversation — or a user tapping "that was wrong" in the
     app — absolutely can judge whether the engine's call was correct.
     Once enough labeled rows exist, fitting new WEIGHT_VOICE_SIMILARITY
     / WEIGHT_TURN_TAKING / WEIGHT_SEMANTIC_COHERENCE values against them
     (e.g. a simple grid search or logistic regression predicting
     decision_correct from the three raw signal columns) is a genuinely
     separate follow-up script this file intentionally does not attempt
     — there's no labeled data yet to fit against, so writing that script
     now would just be guessing at its own interface.

Usage:
    python -m cie.calibration --export calibration_for_labeling.csv
    python -m cie.calibration --summary
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import statistics
from pathlib import Path

from . import calibration_log

logger = logging.getLogger(__name__)

# Columns written by export_for_labeling(), in order. Everything up to
# (not including) decision_correct comes straight from a logged record;
# decision_correct is the blank column a human fills in afterward.
_LABELING_COLUMNS = (
    "sample_id", "logged_at", "role", "confidence",
    "voice_similarity", "turn_taking_score", "semantic_coherence_score",
    "weight_voice", "weight_turn", "weight_semantic",
    "bystander_threshold", "lock_threshold", "noise_profile", "noise_class",
    "partner_switched", "partner_joined",
    "decision_correct",  # blank — filled in by a human reviewer afterward
)


def download_signal_tuples(bucket: str | None = None, prefix: str | None = None,
                            limit: int | None = None) -> list[dict]:
    """Pull logged signal-tuple JSON objects back from S3. Skips (and
    logs) any individual object that fails to fetch or parse, rather than
    letting one bad object abort the whole batch — this is an offline
    analysis tool run against a real bucket, not a hot-path call, so
    partial results are far more useful than an all-or-nothing failure.
    """
    import boto3  # lazy import — this module is only ever run offline/on demand

    bucket = bucket or calibration_log_bucket()
    prefix = prefix or calibration_log.PREFIX
    client = boto3.client("s3")

    records: list[dict] = []
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            if limit is not None and len(records) >= limit:
                return records
            try:
                body = client.get_object(Bucket=bucket, Key=obj["Key"])["Body"].read()
                record = json.loads(body)
                record["_key"] = obj["Key"]
                records.append(record)
            except Exception as exc:  # noqa: BLE001 - one bad object shouldn't abort the batch
                logger.warning("Skipping unreadable calibration object %s: %s", obj.get("Key"), exc)
    return records


def calibration_log_bucket() -> str:
    """The bucket configured for logging, for callers that want the same
    default download_signal_tuples() uses without importing os directly."""
    import os

    return os.environ.get(calibration_log.BUCKET_ENV_VAR, "")


def summarize_signal_distribution(records: list[dict]) -> dict:
    """Mean/min/max of each raw signal, grouped by the role the CIE
    actually assigned — a sanity check on real-world distributions
    against the hand-set thresholds in cie/engine.py, usable the moment
    any data exists at all, with no labeling required."""
    by_role: dict[str, list[dict]] = {}
    for r in records:
        by_role.setdefault(r.get("role", "unknown"), []).append(r)

    summary = {}
    for role, rows in by_role.items():
        role_summary = {"n": len(rows)}
        for field in ("confidence", "voice_similarity", "turn_taking_score",
                      "semantic_coherence_score"):
            values = [r[field] for r in rows if r.get(field) is not None]
            if values:
                role_summary[field] = {
                    "mean": statistics.fmean(values),
                    "min": min(values),
                    "max": max(values),
                }
        summary[role] = role_summary
    return summary


def export_for_labeling(records: list[dict], out_path: Path | str) -> Path:
    """Write logged records to a CSV shaped for a human reviewer: every
    signal the CIE used plus one blank column (decision_correct) for them
    to fill in. Deliberately not the same schema as cie/eval_dataset.csv
    — that file drives *synthetic* replayed scenarios through the engine
    from scratch; this one records what the engine already decided on
    real signals, for a human to grade after the fact."""
    out_path = Path(out_path)
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=_LABELING_COLUMNS)
        writer.writeheader()
        for i, record in enumerate(records):
            row = {col: record.get(col, "") for col in _LABELING_COLUMNS}
            row["sample_id"] = record.get("_key") or f"sample_{i:04d}"
            row["decision_correct"] = ""  # always blank — the human fills this in
            writer.writerow(row)
    return out_path


def _main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bucket", default=None, help="Override VOXBUDDY_CALIBRATION_BUCKET")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--export", metavar="PATH", help="Write a labeling CSV to PATH")
    parser.add_argument("--summary", action="store_true", help="Print per-role signal stats")
    args = parser.parse_args()

    bucket = args.bucket or calibration_log_bucket()
    if not bucket:
        parser.error("No bucket configured — pass --bucket or set VOXBUDDY_CALIBRATION_BUCKET")

    records = download_signal_tuples(bucket=bucket, limit=args.limit)
    print(f"Downloaded {len(records)} logged signal tuples from s3://{bucket}/{calibration_log.PREFIX}")

    if args.summary or not args.export:
        for role, stats in summarize_signal_distribution(records).items():
            print(f"\n{role} (n={stats['n']})")
            for field, agg in stats.items():
                if field == "n":
                    continue
                print(f"  {field:<26} mean={agg['mean']:.3f}  min={agg['min']:.3f}  max={agg['max']:.3f}")

    if args.export:
        path = export_for_labeling(records, args.export)
        print(f"\nWrote {len(records)} rows to {path} — fill in 'decision_correct' by hand.")


if __name__ == "__main__":
    _main()
