"""
DynamoDB-backed conversation persistence — the AWS alternative to
persistence.py's SQLite implementation.

This is VoxBuddy's second AWS integration point (alongside Polly TTS —
see agents/tts_polly.py). It implements the exact same public function
signatures and dataclasses as persistence.py (re-exported from there, not
redefined, so the two are always structurally identical), so app.py can
point at either backend by changing one import line — see
persistence_store.py, which is the actual thing app.py imports and which
picks between this module and persistence.py based on
VOXBUDDY_PERSISTENCE_PROVIDER.

Design notes (deliberately simple, matching persistence.py's own "Phase
1/2-appropriate" philosophy — see its module docstring):
  - Three tables: <prefix>_conversations, <prefix>_turns, <prefix>_counters.
    Prefix defaults to "voxbuddy" and is configurable via
    DYNAMODB_TABLE_PREFIX, so multiple deployments/branches don't collide
    in the same AWS account.
  - Conversation ids are still plain incrementing integers (not UUIDs) so
    that URLs like /history/{conversation_id} and the rest of app.py don't
    need to change at all when switching providers. DynamoDB has no native
    autoincrement, so <prefix>_counters holds a single atomic-counter item
    that update_item's ADD expression increments — this is the standard
    DynamoDB pattern for sequential ids.
  - list_conversations / get_language_breakdown / get_summary_stats scan
    the relevant table and aggregate in Python rather than using a GSI.
    Correct and simple at hackathon/demo data volumes (dozens to low
    hundreds of conversations); a real production scale-up would add a
    user_id GSI on <prefix>_conversations instead of scanning.
"""

from __future__ import annotations

import os
import time
from decimal import Decimal

from cie.state import SpeakerRole
from session.manager import SessionManager

# Reuse persistence.py's dataclasses directly so both backends return
# structurally identical objects — app.py doesn't need to know which
# backend produced a ConversationSummary/ConversationDetail/SummaryStats.
from persistence import ConversationDetail, ConversationSummary, SummaryStats, TurnRecord

TABLE_PREFIX = os.environ.get("DYNAMODB_TABLE_PREFIX", "voxbuddy")
CONVERSATIONS_TABLE = f"{TABLE_PREFIX}_conversations"
TURNS_TABLE = f"{TABLE_PREFIX}_turns"
COUNTERS_TABLE = f"{TABLE_PREFIX}_counters"

_resource = None


def _get_resource():
    global _resource
    if _resource is None:
        import boto3  # lazy import — keeps boto3 optional unless this backend is selected

        _resource = boto3.resource("dynamodb", region_name=os.environ.get("AWS_REGION"))
    return _resource


def _table(name: str):
    return _get_resource().Table(name)


def _to_decimal(value: float) -> Decimal:
    # DynamoDB's Python SDK requires Decimal for numeric attributes, not
    # native float (which loses precision in DynamoDB's own number type).
    return Decimal(str(value))


def init_db() -> None:
    """Creates the three tables if they don't already exist. Idempotent —
    safe to call on every app startup, same as persistence.init_db()."""
    client = _get_resource().meta.client
    existing = set(client.list_tables()["TableNames"])

    if CONVERSATIONS_TABLE not in existing:
        client.create_table(
            TableName=CONVERSATIONS_TABLE,
            KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "id", "AttributeType": "N"}],
            BillingMode="PAY_PER_REQUEST",
        )
    if TURNS_TABLE not in existing:
        client.create_table(
            TableName=TURNS_TABLE,
            KeySchema=[
                {"AttributeName": "conversation_id", "KeyType": "HASH"},
                {"AttributeName": "seq", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "conversation_id", "AttributeType": "N"},
                {"AttributeName": "seq", "AttributeType": "N"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
    if COUNTERS_TABLE not in existing:
        client.create_table(
            TableName=COUNTERS_TABLE,
            KeySchema=[{"AttributeName": "name", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "name", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )

    for table_name in (CONVERSATIONS_TABLE, TURNS_TABLE, COUNTERS_TABLE):
        _table(table_name).meta.client.get_waiter("table_exists").wait(TableName=table_name)


def _next_conversation_id() -> int:
    resp = _table(COUNTERS_TABLE).update_item(
        Key={"name": "conversation_id"},
        UpdateExpression="ADD #v :incr",
        ExpressionAttributeNames={"#v": "value"},
        ExpressionAttributeValues={":incr": 1},
        ReturnValues="UPDATED_NEW",
    )
    return int(resp["Attributes"]["value"])


def save_conversation(session: SessionManager, session_id: str,
                       user_id: int | None = None) -> int | None:
    """Same contract as persistence.save_conversation — returns the new
    conversation id, or None if there was nothing worth saving."""
    turns = session.state.turn_history
    if not turns:
        return None

    conversation_id = _next_conversation_id()
    started_at = turns[0].timestamp
    ended_at = turns[-1].timestamp
    duration = max(ended_at - started_at, 0.0)

    item = {
        "id": conversation_id,
        "session_id": session_id,
        "started_at": _to_decimal(started_at),
        "ended_at": _to_decimal(ended_at),
        "target_lang": turns[-1].target_lang,
        "turn_count": len(turns),
        "duration_seconds": _to_decimal(duration),
    }
    if user_id is not None:
        item["user_id"] = user_id
    _table(CONVERSATIONS_TABLE).put_item(Item=item)

    turns_table = _table(TURNS_TABLE)
    for seq, turn in enumerate(turns):
        speaker = session.state.speakers.get(turn.speaker_id)
        role = speaker.role.value if speaker else SpeakerRole.UNKNOWN.value
        turns_table.put_item(Item={
            "conversation_id": conversation_id,
            "seq": seq,
            "role": role,
            "source_lang": turn.source_lang,
            "source_text": turn.source_text,
            "target_lang": turn.target_lang,
            "target_text": turn.target_text,
            "timestamp": _to_decimal(turn.timestamp),
        })

    return conversation_id


def _row_to_summary(row: dict, first_line: str | None = None) -> ConversationSummary:
    return ConversationSummary(
        id=int(row["id"]),
        session_id=row["session_id"],
        started_at=float(row["started_at"]),
        ended_at=float(row["ended_at"]),
        target_lang=row["target_lang"],
        turn_count=int(row["turn_count"]),
        duration_seconds=float(row["duration_seconds"]),
        first_line=first_line,
    )


def _first_turn_text(conversation_id: int) -> str | None:
    resp = _table(TURNS_TABLE).query(
        KeyConditionExpression="conversation_id = :cid",
        ExpressionAttributeValues={":cid": conversation_id},
        Limit=1,
    )
    items = resp.get("Items", [])
    return items[0]["source_text"] if items else None


def list_conversations(limit: int = 50, user_id: int | None = None) -> list[ConversationSummary]:
    rows = _table(CONVERSATIONS_TABLE).scan().get("Items", [])
    if user_id is not None:
        rows = [r for r in rows if r.get("user_id") == user_id]
    rows.sort(key=lambda r: float(r["started_at"]), reverse=True)
    rows = rows[:limit]
    return [_row_to_summary(r, first_line=_first_turn_text(int(r["id"]))) for r in rows]


def get_conversation_owner(conversation_id: int) -> int | None:
    resp = _table(CONVERSATIONS_TABLE).get_item(Key={"id": conversation_id})
    row = resp.get("Item")
    if not row:
        return None
    return row.get("user_id")


def get_conversation(conversation_id: int) -> ConversationDetail | None:
    resp = _table(CONVERSATIONS_TABLE).get_item(Key={"id": conversation_id})
    row = resp.get("Item")
    if not row:
        return None

    turn_rows = _table(TURNS_TABLE).query(
        KeyConditionExpression="conversation_id = :cid",
        ExpressionAttributeValues={":cid": conversation_id},
    ).get("Items", [])
    turn_rows.sort(key=lambda r: int(r["seq"]))

    summary = _row_to_summary(row)
    turns = [
        TurnRecord(
            role=r["role"], source_lang=r["source_lang"], source_text=r["source_text"],
            target_lang=r["target_lang"], target_text=r["target_text"],
            timestamp=float(r["timestamp"]),
        )
        for r in turn_rows
    ]
    return ConversationDetail(summary=summary, turns=turns)


def delete_all(user_id: int | None = None) -> None:
    conv_table = _table(CONVERSATIONS_TABLE)
    turns_table = _table(TURNS_TABLE)

    rows = conv_table.scan().get("Items", [])
    if user_id is not None:
        rows = [r for r in rows if r.get("user_id") == user_id]

    for row in rows:
        cid = int(row["id"])
        turn_rows = turns_table.query(
            KeyConditionExpression="conversation_id = :cid",
            ExpressionAttributeValues={":cid": cid},
        ).get("Items", [])
        for t in turn_rows:
            turns_table.delete_item(Key={"conversation_id": cid, "seq": t["seq"]})
        conv_table.delete_item(Key={"id": cid})


def get_language_breakdown(user_id: int | None = None) -> list[dict]:
    conv_rows = _table(CONVERSATIONS_TABLE).scan().get("Items", [])
    allowed_ids = None
    if user_id is not None:
        allowed_ids = {int(r["id"]) for r in conv_rows if r.get("user_id") == user_id}

    turn_rows = _table(TURNS_TABLE).scan().get("Items", [])
    counts: dict[str, set[int]] = {}
    for t in turn_rows:
        if t.get("role") != SpeakerRole.PARTNER.value:
            continue
        cid = int(t["conversation_id"])
        if allowed_ids is not None and cid not in allowed_ids:
            continue
        counts.setdefault(t["source_lang"], set()).add(cid)

    breakdown = [{"lang": lang, "count": len(cids)} for lang, cids in counts.items()]
    breakdown.sort(key=lambda d: d["count"], reverse=True)
    return breakdown


def get_summary_stats(user_id: int | None = None) -> SummaryStats:
    conv_rows = _table(CONVERSATIONS_TABLE).scan().get("Items", [])
    if user_id is not None:
        conv_rows = [r for r in conv_rows if r.get("user_id") == user_id]

    total_conversations = len(conv_rows)
    total_seconds = sum(float(r["duration_seconds"]) for r in conv_rows)
    allowed_ids = {int(r["id"]) for r in conv_rows}

    turn_rows = _table(TURNS_TABLE).scan().get("Items", [])
    langs = {
        t["source_lang"] for t in turn_rows
        if t.get("role") == SpeakerRole.PARTNER.value and int(t["conversation_id"]) in allowed_ids
    }

    return SummaryStats(
        total_conversations=total_conversations,
        total_languages=len(langs),
        total_seconds=total_seconds,
        day_streak=get_day_streak(user_id=user_id, _conv_rows=conv_rows),
    )


def get_day_streak(user_id: int | None = None, now: float | None = None,
                    _conv_rows: list[dict] | None = None) -> int:
    import datetime as _dt

    rows = _conv_rows if _conv_rows is not None else _table(CONVERSATIONS_TABLE).scan().get("Items", [])
    if user_id is not None and _conv_rows is None:
        rows = [r for r in rows if r.get("user_id") == user_id]

    if not rows:
        return 0

    active_days = sorted({
        _dt.datetime.fromtimestamp(float(r["started_at"]), tz=_dt.timezone.utc).date()
        for r in rows
    }, reverse=True)

    today = _dt.datetime.fromtimestamp(
        now if now is not None else time.time(), tz=_dt.timezone.utc
    ).date()

    most_recent = active_days[0]
    if (today - most_recent).days > 1:
        return 0

    streak = 1
    for i in range(1, len(active_days)):
        if active_days[i] == active_days[i - 1] - _dt.timedelta(days=1):
            streak += 1
        else:
            break
    return streak
