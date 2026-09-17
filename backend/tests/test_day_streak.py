import datetime as dt
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

import persistence


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "test_streak.db"
    persistence.init_db(path)
    return path


def _insert_conversation_on(db_path, day: dt.date, user_id: int = 1):
    """Inserts a minimal conversation row timestamped at noon UTC on the
    given date — enough for get_day_streak, which only reads started_at."""
    ts = dt.datetime.combine(day, dt.time(12, 0), tzinfo=dt.timezone.utc).timestamp()
    with persistence._connect(db_path) as conn:
        conn.execute(
            "INSERT INTO conversations (session_id, user_id, started_at, ended_at, "
            "target_lang, turn_count, duration_seconds) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (f"sess-{day}", user_id, ts, ts + 10, "en", 1, 10.0),
        )


def test_no_conversations_gives_zero_streak(db_path):
    assert persistence.get_day_streak(db_path=db_path, user_id=1) == 0


def test_single_conversation_today_gives_streak_one(db_path):
    today = dt.datetime.now(dt.timezone.utc).date()
    _insert_conversation_on(db_path, today)
    now_ts = dt.datetime.combine(today, dt.time(18, 0), tzinfo=dt.timezone.utc).timestamp()
    assert persistence.get_day_streak(db_path=db_path, user_id=1, now=now_ts) == 1


def test_consecutive_days_count_correctly(db_path):
    today = dt.date(2026, 9, 17)
    for offset in range(4):  # today, yesterday, day before, day before that
        _insert_conversation_on(db_path, today - dt.timedelta(days=offset))
    now_ts = dt.datetime.combine(today, dt.time(9, 0), tzinfo=dt.timezone.utc).timestamp()
    assert persistence.get_day_streak(db_path=db_path, user_id=1, now=now_ts) == 4


def test_gap_breaks_streak_at_the_gap(db_path):
    today = dt.date(2026, 9, 17)
    _insert_conversation_on(db_path, today)
    _insert_conversation_on(db_path, today - dt.timedelta(days=1))
    # gap here — no conversation on today-2
    _insert_conversation_on(db_path, today - dt.timedelta(days=3))
    now_ts = dt.datetime.combine(today, dt.time(9, 0), tzinfo=dt.timezone.utc).timestamp()
    assert persistence.get_day_streak(db_path=db_path, user_id=1, now=now_ts) == 2


def test_inactive_for_two_plus_days_resets_to_zero(db_path):
    today = dt.date(2026, 9, 17)
    # last activity was 3 days ago — streak should read as broken, not "3"
    _insert_conversation_on(db_path, today - dt.timedelta(days=3))
    _insert_conversation_on(db_path, today - dt.timedelta(days=4))
    now_ts = dt.datetime.combine(today, dt.time(9, 0), tzinfo=dt.timezone.utc).timestamp()
    assert persistence.get_day_streak(db_path=db_path, user_id=1, now=now_ts) == 0


def test_yesterday_only_still_counts_as_live_streak(db_path):
    # "don't punish sleep": if you were active yesterday but not yet today,
    # the streak is still alive, not reset.
    today = dt.date(2026, 9, 17)
    _insert_conversation_on(db_path, today - dt.timedelta(days=1))
    _insert_conversation_on(db_path, today - dt.timedelta(days=2))
    now_ts = dt.datetime.combine(today, dt.time(9, 0), tzinfo=dt.timezone.utc).timestamp()
    assert persistence.get_day_streak(db_path=db_path, user_id=1, now=now_ts) == 2


def test_multiple_conversations_same_day_count_once(db_path):
    today = dt.date(2026, 9, 17)
    _insert_conversation_on(db_path, today)
    with persistence._connect(db_path) as conn:
        ts2 = dt.datetime.combine(today, dt.time(20, 0), tzinfo=dt.timezone.utc).timestamp()
        conn.execute(
            "INSERT INTO conversations (session_id, user_id, started_at, ended_at, "
            "target_lang, turn_count, duration_seconds) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("sess-2nd", 1, ts2, ts2 + 5, "en", 1, 5.0),
        )
    now_ts = dt.datetime.combine(today, dt.time(21, 0), tzinfo=dt.timezone.utc).timestamp()
    assert persistence.get_day_streak(db_path=db_path, user_id=1, now=now_ts) == 1


def test_streak_scoped_per_user(db_path):
    today = dt.date(2026, 9, 17)
    _insert_conversation_on(db_path, today, user_id=1)
    _insert_conversation_on(db_path, today, user_id=2)
    _insert_conversation_on(db_path, today - dt.timedelta(days=1), user_id=2)
    now_ts = dt.datetime.combine(today, dt.time(9, 0), tzinfo=dt.timezone.utc).timestamp()
    assert persistence.get_day_streak(db_path=db_path, user_id=1, now=now_ts) == 1
    assert persistence.get_day_streak(db_path=db_path, user_id=2, now=now_ts) == 2


def test_summary_stats_includes_day_streak(db_path):
    today = dt.date(2026, 9, 17)
    _insert_conversation_on(db_path, today)
    now_ts = dt.datetime.combine(today, dt.time(9, 0), tzinfo=dt.timezone.utc).timestamp()
    stats = persistence.get_summary_stats(db_path=db_path, user_id=1)
    # get_summary_stats itself doesn't take `now`, so this just checks the
    # field exists and is non-negative — the exact value is time-sensitive
    # and covered precisely by the get_day_streak tests above.
    assert stats.day_streak >= 0
