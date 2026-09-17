import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cie.engine import ConversationIntelligenceEngine, SignalBundle
from cie.state import ConversationState, SpeakerRole


def embedding_for(label: str) -> list[float]:
    import hashlib
    h = hashlib.sha256(label.encode()).digest()
    return [(b / 127.5) - 1.0 for b in h[:8]]


def make_signals(label: str, turn_taking=0.8, coherence=0.8) -> SignalBundle:
    return SignalBundle(
        speaker_embedding=embedding_for(label),
        turn_taking_score=turn_taking,
        semantic_coherence_score=coherence,
    )


# ---------------------------------------------------------------------------
# Bootstrap / bystander basics
# ---------------------------------------------------------------------------

def test_bootstraps_partner_on_strong_first_signal():
    cie = ConversationIntelligenceEngine(ConversationState())
    decision = cie.process_utterance(make_signals("partner_A", turn_taking=0.9, coherence=0.9))
    assert decision.role == SpeakerRole.PARTNER
    assert decision.speaker_id in cie.state.active_partner_ids


def test_weak_first_signal_does_not_lock_a_partner():
    cie = ConversationIntelligenceEngine(ConversationState())
    decision = cie.process_utterance(make_signals("stranger", turn_taking=0.1, coherence=0.1))
    assert decision.role in (SpeakerRole.BYSTANDER, SpeakerRole.UNKNOWN)
    assert not cie.state.active_partner_ids


def test_reinforces_same_partner_across_turns():
    cie = ConversationIntelligenceEngine(ConversationState())
    first = cie.process_utterance(make_signals("partner_A", turn_taking=0.9, coherence=0.9))
    second = cie.process_utterance(make_signals("partner_A", turn_taking=0.85, coherence=0.85))
    assert first.speaker_id == second.speaker_id
    assert second.role == SpeakerRole.PARTNER
    assert second.notes == "reinforced existing group member"


def test_sustained_incoherence_drops_member_and_reopens_slot():
    cie = ConversationIntelligenceEngine(ConversationState())
    cie.process_utterance(make_signals("partner_A", turn_taking=0.9, coherence=0.9))

    last_decision = None
    for _ in range(3):
        last_decision = cie.process_utterance(make_signals("partner_A", turn_taking=0.9, coherence=0.1))

    assert "dropping from partner group" in last_decision.notes
    assert not cie.state.active_partner_ids


# ---------------------------------------------------------------------------
# Group join (the multi-partner extension)
# ---------------------------------------------------------------------------

def test_second_voice_joins_group_directly_when_room_available():
    """A confident second voice while there's a free slot should join the
    conversation group outright — this is additive, not a switch, and
    should NOT displace the first partner."""
    cie = ConversationIntelligenceEngine(ConversationState())
    first = cie.process_utterance(make_signals("partner_A", turn_taking=0.9, coherence=0.9))

    second = cie.process_utterance(make_signals("partner_B", turn_taking=0.9, coherence=0.9))
    assert second.role == SpeakerRole.PARTNER
    assert second.partner_joined is True
    assert second.partner_switched is False
    assert cie.state.active_partner_ids == {first.speaker_id, second.speaker_id}


def test_both_group_members_get_reinforced_independently():
    cie = ConversationIntelligenceEngine(ConversationState())
    a = cie.process_utterance(make_signals("partner_A", turn_taking=0.9, coherence=0.9))
    b = cie.process_utterance(make_signals("partner_B", turn_taking=0.9, coherence=0.9))

    # Alternate between them several times — both should stay PARTNER.
    for _ in range(3):
        ra = cie.process_utterance(make_signals("partner_A", turn_taking=0.85, coherence=0.85))
        rb = cie.process_utterance(make_signals("partner_B", turn_taking=0.85, coherence=0.85))
        assert ra.role == SpeakerRole.PARTNER
        assert rb.role == SpeakerRole.PARTNER

    assert cie.state.active_partner_ids == {a.speaker_id, b.speaker_id}


def test_third_voice_rejected_when_group_full_and_nobody_absent():
    cie = ConversationIntelligenceEngine(ConversationState())
    cie.process_utterance(make_signals("partner_A", turn_taking=0.9, coherence=0.9))
    cie.process_utterance(make_signals("partner_B", turn_taking=0.9, coherence=0.9))

    decision = cie.process_utterance(make_signals("partner_C", turn_taking=0.9, coherence=0.9))
    assert decision.role == SpeakerRole.BYSTANDER
    assert "group full" in decision.notes
    assert len(cie.state.active_partner_ids) == 2


def test_low_confidence_voice_ignored_even_with_room_available():
    cie = ConversationIntelligenceEngine(ConversationState())
    cie.process_utterance(make_signals("partner_A", turn_taking=0.9, coherence=0.9))

    decision = cie.process_utterance(make_signals("bystander_B", turn_taking=0.1, coherence=0.1))
    assert decision.role == SpeakerRole.BYSTANDER
    assert len(cie.state.active_partner_ids) == 1


# ---------------------------------------------------------------------------
# Replacing an absent group member (switch, generalized to N-member groups)
# ---------------------------------------------------------------------------

def test_moderate_confidence_replacement_requires_confirmation():
    cie = ConversationIntelligenceEngine(ConversationState())
    cie.process_utterance(make_signals("partner_A", turn_taking=0.9, coherence=0.9))
    cie.process_utterance(make_signals("partner_B", turn_taking=0.9, coherence=0.9))

    b_id = [sid for sid in cie.state.active_partner_ids
            if sid != cie._match_or_create_speaker(embedding_for("partner_A"), cie._profile()).id][0]
    cie.state.speakers[b_id].last_seen = time.time() - 10

    first = cie.process_utterance(make_signals("partner_C", turn_taking=0.9, coherence=0.9))
    assert first.role == SpeakerRole.UNKNOWN
    assert first.partner_switched is False
    assert "awaiting confirmation" in first.notes
    assert b_id in cie.state.active_partner_ids  # not yet replaced

    second = cie.process_utterance(make_signals("partner_C", turn_taking=0.9, coherence=0.9))
    assert second.role == SpeakerRole.PARTNER
    assert second.partner_switched is True
    assert b_id not in cie.state.active_partner_ids
    assert second.speaker_id in cie.state.active_partner_ids


def test_fast_track_replacement_on_overwhelming_single_turn():
    cie = ConversationIntelligenceEngine(ConversationState())
    a = cie.process_utterance(make_signals("partner_A", turn_taking=0.95, coherence=0.95))
    cie.process_utterance(make_signals("partner_B", turn_taking=0.95, coherence=0.95))  # fill the group
    cie.state.speakers[a.speaker_id].last_seen = time.time() - 10

    decision = cie.process_utterance(make_signals("partner_C", turn_taking=1.0, coherence=1.0))
    assert decision.role == SpeakerRole.PARTNER
    assert decision.partner_switched is True
    assert "fast-tracked" in decision.notes


def test_replacement_candidacy_cancelled_if_absent_member_speaks_again():
    cie = ConversationIntelligenceEngine(ConversationState())
    a = cie.process_utterance(make_signals("partner_A", turn_taking=0.9, coherence=0.9))
    cie.process_utterance(make_signals("partner_B", turn_taking=0.9, coherence=0.9))  # fill the group
    cie.state.speakers[a.speaker_id].last_seen = time.time() - 10

    first = cie.process_utterance(make_signals("candidate_C", turn_taking=0.9, coherence=0.9))
    assert "1/2" in first.notes

    # partner_A reasserts itself before candidate C confirms
    cie.state.speakers[a.speaker_id].last_seen = time.time()
    reassert = cie.process_utterance(make_signals("partner_A", turn_taking=0.9, coherence=0.9))
    assert reassert.role == SpeakerRole.PARTNER
    assert reassert.speaker_id == a.speaker_id

    cie.state.speakers[a.speaker_id].last_seen = time.time() - 10
    second = cie.process_utterance(make_signals("candidate_C", turn_taking=0.9, coherence=0.9))
    assert "1/2" in second.notes  # restarted, not resumed at 2/2


# ---------------------------------------------------------------------------
# Housekeeping: prune, crowded environments, oscillation
# ---------------------------------------------------------------------------

def test_prune_drops_member_after_long_silence_freeing_the_slot():
    cie = ConversationIntelligenceEngine(ConversationState())
    a = cie.process_utterance(make_signals("partner_A", turn_taking=0.9, coherence=0.9))
    b = cie.process_utterance(make_signals("partner_B", turn_taking=0.9, coherence=0.9))
    assert len(cie.state.active_partner_ids) == 2

    # partner_B has been gone well past the prune timeout (not just the
    # shorter absence/replace timeout) -> should be dropped outright.
    cie.state.speakers[b.speaker_id].last_seen = time.time() - 31

    decision = cie.process_utterance(make_signals("partner_C", turn_taking=0.9, coherence=0.9))
    # Room should have been freed by the prune, so this is a JOIN not a
    # confirmation-gated replacement.
    assert decision.role == SpeakerRole.PARTNER
    assert decision.partner_joined is True
    assert b.speaker_id not in cie.state.active_partner_ids


def test_crowded_environment_group_remains_locked():
    cie = ConversationIntelligenceEngine(ConversationState())
    cie.process_utterance(make_signals("partner_A", turn_taking=0.9, coherence=0.9))
    cie.process_utterance(make_signals("partner_B", turn_taking=0.9, coherence=0.9))

    for i in range(15):
        decision = cie.process_utterance(
            make_signals(f"crowd_voice_{i}", turn_taking=0.15, coherence=0.1)
        )
        assert decision.role == SpeakerRole.BYSTANDER

    assert len(cie.state.active_partner_ids) == 2


def test_oscillating_candidates_never_confirm_without_repetition():
    cie = ConversationIntelligenceEngine(ConversationState())
    a = cie.process_utterance(make_signals("partner_A", turn_taking=0.9, coherence=0.9))
    cie.state.speakers[a.speaker_id].last_seen = time.time() - 10

    for _ in range(4):
        d1 = cie.process_utterance(make_signals("candidate_X", turn_taking=0.9, coherence=0.9))
        d2 = cie.process_utterance(make_signals("candidate_Y", turn_taking=0.9, coherence=0.9))
        assert d1.partner_switched is False
        assert d2.partner_switched is False

    assert a.speaker_id in cie.state.active_partner_ids


# ---------------------------------------------------------------------------
# Self-voice enrollment
# ---------------------------------------------------------------------------

def test_enrolled_self_speaker_is_never_a_partner_candidate():
    cie = ConversationIntelligenceEngine(ConversationState())
    self_speaker = cie.enroll_self(embedding_for("tourist_self"))
    assert self_speaker.role == SpeakerRole.SELF

    # Even with maximally confident signals, self speech should never be
    # evaluated as a partner and should never appear in active_partner_ids.
    decision = cie.process_utterance(make_signals("tourist_self", turn_taking=1.0, coherence=1.0))
    assert decision.role == SpeakerRole.SELF
    assert decision.speaker_id == self_speaker.id
    assert self_speaker.id not in cie.state.active_partner_ids


def test_self_speaker_does_not_consume_a_group_slot():
    """This is the concrete bug enrollment fixes: without it, the user's own
    speech could fill a conversation-group slot, leaving no room for a real
    second partner."""
    cie = ConversationIntelligenceEngine(ConversationState())
    cie.enroll_self(embedding_for("tourist_self"))

    cie.process_utterance(make_signals("shopkeeper", turn_taking=0.9, coherence=0.9))
    cie.process_utterance(make_signals("tourist_self", turn_taking=0.9, coherence=0.9))
    second_partner = cie.process_utterance(make_signals("shopkeeper_spouse", turn_taking=0.9, coherence=0.9))

    assert second_partner.role == SpeakerRole.PARTNER
    assert second_partner.partner_joined is True
    assert len(cie.state.active_partner_ids) == 2  # shopkeeper + spouse, NOT self


def test_self_enrollment_is_idempotent_and_reuses_the_same_speaker():
    cie = ConversationIntelligenceEngine(ConversationState())
    first = cie.enroll_self(embedding_for("tourist_self"))
    second = cie.enroll_self(embedding_for("tourist_self"))
    assert first.id == second.id
    assert len(cie.state.speakers) == 1


def test_self_enrollment_removes_speaker_from_group_if_already_tracked():
    """Edge case: if the CIE already (mistakenly, pre-enrollment) tracked
    this voice as a partner, enrolling it as self afterward should evict it
    from the group rather than leaving a stale membership."""
    cie = ConversationIntelligenceEngine(ConversationState())
    cie.process_utterance(make_signals("tourist_self", turn_taking=0.9, coherence=0.9))
    assert cie.state.active_partner_ids  # currently (incorrectly) tracked as partner

    self_speaker = cie.enroll_self(embedding_for("tourist_self"))
    assert self_speaker.id not in cie.state.active_partner_ids
    assert self_speaker.role == SpeakerRole.SELF
