"""Tests for the CIE upgrades: noise-adaptive thresholds, the multi-signal
bystander gate, EMA embedding updates, self-voice adaptivity, per-speaker
language tracking, the should_translate_now helper, and the eval harness.
Complements tests/test_cie.py, which covers the pre-existing hysteresis/
multi-partner behavior untouched by this pass."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cie.engine import (
    ConversationIntelligenceEngine,
    SignalBundle,
    should_translate_now,
)
from cie.state import ConversationState, Speaker, SpeakerRole
from cie.eval import run_eval, load_dataset, DATASET_PATH


def embedding_for(label: str) -> list[float]:
    import hashlib
    h = hashlib.sha256(label.encode()).digest()
    return [(b / 127.5) - 1.0 for b in h[:8]]


def make_signals(label: str, turn_taking=0.8, coherence=0.8, noise_class=None) -> SignalBundle:
    return SignalBundle(
        speaker_embedding=embedding_for(label),
        turn_taking_score=turn_taking,
        semantic_coherence_score=coherence,
        noise_class=noise_class,
    )


# ---------------------------------------------------------------------------
# Noise-adaptive thresholds
# ---------------------------------------------------------------------------

def test_loud_crowd_raises_the_bystander_bar():
    """A read that would sit just above the CLEAN bystander threshold
    (so it'd be UNKNOWN, not written off) should be pushed below the
    NOISY threshold and correctly rejected as a bystander instead."""
    clean_cie = ConversationIntelligenceEngine(ConversationState())
    clean_decision = clean_cie.process_utterance(
        make_signals("borderline", turn_taking=0.30, coherence=0.30, noise_class="quiet")
    )
    assert clean_decision.role != SpeakerRole.BYSTANDER  # UNKNOWN under clean rules

    noisy_cie = ConversationIntelligenceEngine(ConversationState())
    noisy_decision = noisy_cie.process_utterance(
        make_signals("borderline", turn_taking=0.30, coherence=0.30, noise_class="loud_crowd")
    )
    assert noisy_decision.role == SpeakerRole.BYSTANDER


def test_noise_class_persists_on_state_once_set():
    cie = ConversationIntelligenceEngine(ConversationState())
    cie.process_utterance(make_signals("A", noise_class="loud_crowd"))
    assert cie.state.environment.noise_class == "loud_crowd"
    # A later utterance with no override keeps the last-known environment.
    cie.process_utterance(make_signals("A"))
    assert cie.state.environment.noise_class == "loud_crowd"


# ---------------------------------------------------------------------------
# Multi-signal bystander gate
# ---------------------------------------------------------------------------

def test_single_strong_signal_does_not_bootstrap_a_partner():
    """High confidence driven almost entirely by ONE signal (semantic
    coherence) with the other (turn-taking) below the floor should NOT
    be enough to lock a brand-new partner, even if the weighted-sum
    confidence would otherwise clear the lock threshold."""
    cie = ConversationIntelligenceEngine(ConversationState())
    decision = cie.process_utterance(
        make_signals("stranger", turn_taking=0.30, coherence=1.0, noise_class="loud_crowd")
    )
    assert decision.role != SpeakerRole.PARTNER
    assert not cie.state.active_partner_ids


def test_two_corroborating_signals_do_bootstrap_a_partner():
    cie = ConversationIntelligenceEngine(ConversationState())
    decision = cie.process_utterance(make_signals("A", turn_taking=0.9, coherence=0.9))
    assert decision.role == SpeakerRole.PARTNER


# ---------------------------------------------------------------------------
# Embedding EMA (better voice-embedding comparison)
# ---------------------------------------------------------------------------

def test_speaker_embedding_updates_toward_new_samples():
    speaker = Speaker(id="spk_1", embedding=[0.0, 0.0])
    speaker.update_embedding([1.0, 1.0], alpha=0.2)
    assert speaker.embedding == [0.2, 0.2]
    assert speaker.embedding_samples == 2


def test_matched_speaker_embedding_drifts_across_turns():
    cie = ConversationIntelligenceEngine(ConversationState())
    first = cie.process_utterance(make_signals("A", turn_taking=0.9, coherence=0.9))
    original = list(cie.state.speakers[first.speaker_id].embedding)

    # A slightly different but still-matching embedding (blend of A with a
    # touch of noise) should nudge the stored centroid rather than leaving
    # it frozen at the very first sample.
    drifted_embedding = [x * 0.95 for x in embedding_for("A")]
    cie.process_utterance(SignalBundle(
        speaker_embedding=drifted_embedding, turn_taking_score=0.9, semantic_coherence_score=0.9,
    ))
    updated = cie.state.speakers[first.speaker_id].embedding
    assert updated != original
    assert cie.state.speakers[first.speaker_id].embedding_samples == 2


# ---------------------------------------------------------------------------
# Self-voice detection adaptivity
# ---------------------------------------------------------------------------

def test_self_still_recognized_under_noise():
    cie = ConversationIntelligenceEngine(ConversationState())
    self_speaker = cie.enroll_self(embedding_for("me"))
    decision = cie.process_utterance(make_signals("me", noise_class="loud_crowd"))
    assert decision.role == SpeakerRole.SELF
    assert decision.speaker_id == self_speaker.id


# ---------------------------------------------------------------------------
# Language tracking
# ---------------------------------------------------------------------------

def test_speaker_language_is_recorded_and_persists_across_turns():
    state = ConversationState()
    cie = ConversationIntelligenceEngine(state)
    decision = cie.process_utterance(make_signals("A", turn_taking=0.9, coherence=0.9))
    state.set_speaker_language(decision.speaker_id, "hi")

    second = cie.process_utterance(make_signals("A", turn_taking=0.85, coherence=0.85))
    assert second.language == "hi"


def test_two_speakers_track_independent_languages():
    state = ConversationState()
    cie = ConversationIntelligenceEngine(state)
    a = cie.process_utterance(make_signals("A", turn_taking=0.9, coherence=0.9))
    b = cie.process_utterance(make_signals("B", turn_taking=0.9, coherence=0.9))
    state.set_speaker_language(a.speaker_id, "en")
    state.set_speaker_language(b.speaker_id, "hi")

    ra = cie.process_utterance(make_signals("A", turn_taking=0.85, coherence=0.85))
    rb = cie.process_utterance(make_signals("B", turn_taking=0.85, coherence=0.85))
    assert ra.language == "en"
    assert rb.language == "hi"


# ---------------------------------------------------------------------------
# Confidence-gated translation
# ---------------------------------------------------------------------------

def test_should_translate_now_requires_partner_role_and_confidence():
    cie = ConversationIntelligenceEngine(ConversationState())
    decision = cie.process_utterance(make_signals("A", turn_taking=0.9, coherence=0.9))
    assert decision.role == SpeakerRole.PARTNER
    assert should_translate_now(decision) is True
    assert should_translate_now(decision, min_confidence=0.99) is False

    bystander = cie.process_utterance(make_signals("Noise", turn_taking=0.1, coherence=0.1))
    assert should_translate_now(bystander) is False


# ---------------------------------------------------------------------------
# Eval harness
# ---------------------------------------------------------------------------

def test_eval_harness_runs_against_bundled_dataset():
    results, metrics = run_eval(DATASET_PATH)
    assert len(results) == 10
    assert metrics["speaker_id_accuracy"] == 1.0
    assert metrics["false_speaker_rate"] == 0.0
    assert metrics["bystander_rejection_rate"] == 1.0
    assert metrics["speaker_switch_accuracy"] == 1.0
    assert metrics["avg_confidence"] > 0
    assert metrics["latency_ms_avg"] >= 0


def test_eval_harness_age_seconds_drives_switch_rows():
    """The bundled dataset's two expect_switch rows (sample_009, sample_010)
    only mean anything because age_seconds actually ages tracked speakers'
    last_seen before a row is processed. Assert both resolve the way their
    row intends, not just that the aggregate switch-accuracy metric is 1.0
    (which a harness bug could still satisfy vacuously with zero real
    switch attempts)."""
    results, _ = run_eval(DATASET_PATH)
    by_id = {r.row.sample_id: r for r in results}

    no_switch_yet = by_id["sample_009"]
    assert no_switch_yet.row.expect_switch is False
    assert no_switch_yet.partner_switched is False
    assert no_switch_yet.predicted_role == "bystander"

    switch_fires = by_id["sample_010"]
    assert switch_fires.row.expect_switch is True
    assert switch_fires.partner_switched is True
    assert switch_fires.predicted_role == "partner"


def test_eval_harness_age_seconds_defaults_to_zero():
    """Rows without an age_seconds value (or with the column absent
    entirely) must behave exactly as before — no artificial aging."""
    rows = load_dataset(DATASET_PATH)
    aged = [r for r in rows if r.age_seconds]
    unaged = [r for r in rows if not r.age_seconds]
    assert {r.sample_id for r in aged} == {"sample_009", "sample_010"}
    assert len(unaged) == 8


# ---------------------------------------------------------------------------
# Signal breakdown (v1.2) — makes the fusion decision inspectable, not just
# its outcome. This is what the CIE Live Cockpit frontend renders.
# ---------------------------------------------------------------------------

def test_decision_carries_raw_signal_breakdown():
    cie = ConversationIntelligenceEngine(ConversationState())
    decision = cie.process_utterance(make_signals("A", turn_taking=0.9, coherence=0.85))

    assert decision.turn_taking_score == 0.9
    assert decision.semantic_coherence_score == 0.85
    # Brand-new candidate: voice_component is the neutral 0.5 prior, not a
    # measured similarity yet (see _fused_confidence).
    assert decision.voice_similarity == 0.5
    assert decision.weight_voice == 0.45
    assert decision.weight_turn == 0.25
    assert decision.weight_semantic == 0.30
    assert decision.bystander_threshold == 0.35
    assert decision.lock_threshold == 0.62
    assert decision.noise_profile == "clean"


def test_decision_breakdown_reflects_noisy_profile():
    cie = ConversationIntelligenceEngine(ConversationState())
    decision = cie.process_utterance(
        make_signals("A", turn_taking=0.9, coherence=0.9, noise_class="loud_crowd")
    )
    assert decision.noise_profile == "noisy"
    assert decision.weight_voice == 0.30       # voice weight reduced under noise
    assert decision.bystander_threshold == pytest.approx(0.40)  # BYSTANDER_THRESHOLD + 0.05
    assert decision.lock_threshold == pytest.approx(0.67)       # PARTNER_LOCK_THRESHOLD + 0.05


def test_decision_breakdown_reports_measured_similarity_once_established():
    cie = ConversationIntelligenceEngine(ConversationState())
    cie.process_utterance(make_signals("A", turn_taking=0.9, coherence=0.9))
    reinforced = cie.process_utterance(make_signals("A", turn_taking=0.9, coherence=0.9))
    # Now that A is an active partner, voice_similarity is a real measured
    # cosine similarity against A's stored embedding — same voice, so ~1.0 —
    # not the 0.5 neutral prior used for not-yet-partner candidates.
    assert reinforced.voice_similarity > 0.9


def test_self_decision_has_no_signal_breakdown():
    """SELF is excluded from partner candidacy before fusion even runs, so
    there's no meaningful voice/turn/semantic breakdown to report — a UI
    should render SELF distinctly rather than plot empty bars."""
    cie = ConversationIntelligenceEngine(ConversationState())
    cie.enroll_self(embedding_for("me"))
    decision = cie.process_utterance(make_signals("me"))
    assert decision.role == SpeakerRole.SELF
    assert decision.voice_similarity is None
    assert decision.turn_taking_score is None
    assert decision.noise_profile is None
