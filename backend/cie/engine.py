"""
Conversation Intelligence Engine (CIE)

This is VoxBuddy's core differentiator. It does not run ASR/MT/TTS itself —
it fuses signals from other agents (voice similarity, turn-taking pattern,
semantic coherence with prior turns) into one decision per incoming
utterance: is this speaker part of the active conversation (PARTNER), or
someone to ignore (BYSTANDER).

Design principles applied (see PRD §7):
  - No single signal is authoritative; each is a weighted vote.
  - Hysteresis: a low-confidence read never immediately overrides an
    established partner — instability is worse than a brief wrong guess.
  - Self-healing: sustained semantic incoherence gradually re-opens
    partner search without ever surfacing an "error" to the user.

v1.1 addition: multi-partner conversation groups. Real conversations often
involve more than one person talking to the user (a couple at a market
stall, two colleagues in a meeting) — the engine now tracks a small capped
group of concurrent partners rather than exactly one. See MAX_ACTIVE_PARTNERS
below and docs/vendor_decision.md for the product motivation.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

from .state import ConversationState, Speaker, SpeakerRole

# --- Tunable weights -------------------------------------------------------
# In production these would be learned/calibrated against real field data
# (see PRD §20, open research question #1). Hand-set here for the PoC so the
# fusion logic is inspectable and testable.

WEIGHT_VOICE_SIMILARITY = 0.45
WEIGHT_TURN_TAKING = 0.25
WEIGHT_SEMANTIC_COHERENCE = 0.30

PARTNER_LOCK_THRESHOLD = 0.62      # confidence needed to (re)lock a partner
BYSTANDER_THRESHOLD = 0.35         # below this, treat as bystander and ignore

# --- Noise-adaptive tuning --------------------------------------------------
# Raw voice-embedding similarity degrades under noise (crowd, wind, traffic)
# more than turn-taking/semantic signals do, since those are derived from ASR
# text and timing rather than waveform quality. Rather than one fixed set of
# thresholds, the engine picks a profile from EnvironmentProfile.noise_class:
#   - same-speaker match bar is LOWERED (a real match will score lower under
#     noise; holding the clean-audio bar would keep spawning "new" speakers
#     for the same person)
#   - voice-similarity's weight in the fused score is REDUCED, and
#     turn-taking/semantic weight raised to compensate
#   - the bar to (re)lock a brand-new partner is RAISED slightly, and the
#     bystander cutoff raised too — under noise it's cheaper to sit at
#     UNKNOWN for one more turn than to lock onto (or write off) the wrong
#     voice on a single noisy read
NOISY_NOISE_CLASSES = {"loud_crowd", "noisy"}

_CLEAN_PROFILE = {
    "match_threshold": 0.80,
    "self_match_threshold": 0.85,
    "w_voice": WEIGHT_VOICE_SIMILARITY,
    "w_turn": WEIGHT_TURN_TAKING,
    "w_semantic": WEIGHT_SEMANTIC_COHERENCE,
    "lock": PARTNER_LOCK_THRESHOLD,
    "bystander": BYSTANDER_THRESHOLD,
}
_NOISY_PROFILE = {
    "match_threshold": 0.70,
    "self_match_threshold": 0.78,
    "w_voice": 0.30,
    "w_turn": 0.35,
    "w_semantic": 0.35,
    "lock": PARTNER_LOCK_THRESHOLD + 0.05,
    "bystander": BYSTANDER_THRESHOLD + 0.05,
}

SIGNAL_FLOOR = 0.50                # each independent signal must clear this
                                    # before a brand-new partner can be
                                    # accepted (see _clears_multi_signal_gate)
CANDIDATE_CONFIRM_TURNS = 2        # consecutive reads from the same new voice
                                    # needed to confirm a join/switch at
                                    # moderate confidence (avoids a single
                                    # noisy read changing group membership)
FAST_TRACK_THRESHOLD = 0.75        # confidence high enough to join/switch on
                                    # one turn, no confirmation wait (near the
                                    # practical ceiling given the neutral 0.5
                                    # voice-similarity baseline used for
                                    # brand-new candidates — see
                                    # _fused_confidence)
GROUP_JOIN_THRESHOLD = 0.70        # confidence needed to join the partner
                                    # group directly (when there's a free
                                    # slot) without displacing anyone —
                                    # deliberately higher than
                                    # PARTNER_LOCK_THRESHOLD since adding a
                                    # member is a strictly additive, harder-
                                    # to-undo decision than the ordinary
                                    # single-partner bootstrap case
PARTNER_ABSENCE_TIMEOUT_S = 8.0    # a group member counts as "gone" (and
                                    # therefore replaceable) after this long
PARTNER_PRUNE_TIMEOUT_S = 30.0     # a group member is dropped outright
                                    # (freeing their slot for anyone) after
                                    # this much longer silence, so a slot
                                    # can't be held hostage indefinitely by
                                    # someone who has genuinely left
INCOHERENCE_RECOVERY_TURNS = 3     # sustained mismatch before dropping a
                                    # member and re-opening their slot
MAX_ACTIVE_PARTNERS = 2            # v1 cap. Real group conversations can be
                                    # larger, but 2 covers the dominant
                                    # realistic case (a pair) without the
                                    # combinatorial complexity of tracking
                                    # many concurrent, independently-timed
                                    # candidacies — revisit with field data
                                    # per PRD §20.


def cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a)) or 1e-9
    norm_b = math.sqrt(sum(y * y for y in b)) or 1e-9
    return dot / (norm_a * norm_b)


@dataclass
class SignalBundle:
    """Per-utterance signals supplied by the upstream agents (mocked in PoC)."""

    speaker_embedding: list[float]
    turn_taking_score: float       # 0-1, alternation-with-partner pattern fit
    semantic_coherence_score: float  # 0-1, responsiveness to prior turn
    noise_class: str | None = None   # optional per-utterance noise estimate
                                      # ("quiet"/"moderate"/"loud_crowd"/etc)
                                      # from the ASR/mic front-end; when set,
                                      # updates state.environment.noise_class
                                      # so the adaptive thresholds react to
                                      # the environment in real time.


@dataclass
class CIEDecision:
    speaker_id: str
    role: SpeakerRole
    confidence: float
    partner_switched: bool = False   # an existing member was displaced
    partner_joined: bool = False     # a new member was added to a free slot
    notes: str = ""
    language: str | None = None      # this speaker's last ID'd language, if
                                      # the pipeline has recorded one (see
                                      # ConversationState.set_speaker_language)

    # -- signal breakdown (v1.2) --------------------------------------
    # Everything below is the *why* behind `confidence`/`role`, added so a
    # UI can render the fusion decision instead of just its outcome. All
    # None for the SELF role (voice similarity to the enrolled self-voice
    # isn't a partner-fusion signal, so there's nothing meaningful to plot).
    voice_similarity: float | None = None       # measured cosine similarity
                                                 # to the matched speaker's
                                                 # stored embedding; the 0.5
                                                 # neutral prior for a
                                                 # not-yet-partner candidate
                                                 # is reported as-is, same as
                                                 # _fused_confidence uses it
    turn_taking_score: float | None = None      # pass-through of the input
                                                 # signal actually used
    semantic_coherence_score: float | None = None
    weight_voice: float | None = None           # fusion weights actually
    weight_turn: float | None = None            # applied this turn (differ
    weight_semantic: float | None = None        # between clean/noisy)
    bystander_threshold: float | None = None    # thresholds actually applied
    lock_threshold: float | None = None
    noise_profile: str | None = None            # "clean" or "noisy" — which
                                                 # profile picked these


def should_translate_now(decision: CIEDecision, min_confidence: float = 0.55) -> bool:
    """Confidence-gated translation decision: fire translation immediately
    only for a PARTNER decision that clears min_confidence. A PARTNER call
    below that bar (or any non-PARTNER role) should wait for the next turn's
    corroborating evidence rather than risk translating a bystander or a
    shaky, borderline read. Downstream callers (session manager) can use
    this instead of just checking `role == PARTNER`."""
    return decision.role == SpeakerRole.PARTNER and decision.confidence >= min_confidence


class ConversationIntelligenceEngine:
    def __init__(self, state: ConversationState):
        self.state = state
        self._incoherence_streaks: dict[str, int] = {}
        self._pending_candidate_id: str | None = None
        self._pending_candidate_streak: int = 0

    # -- environment-adaptive tuning ---------------------------------------
    def _profile(self) -> dict:
        """Pick clean-vs-noisy threshold/weight profile from current
        environment noise_class. See NOISY_NOISE_CLASSES / _CLEAN_PROFILE /
        _NOISY_PROFILE above for the rationale."""
        if self.state.environment.noise_class in NOISY_NOISE_CLASSES:
            return _NOISY_PROFILE
        return _CLEAN_PROFILE

    # -- speaker identity resolution -----------------------------------
    def _match_or_create_speaker(self, embedding: list[float], profile: dict) -> Speaker:
        # Self gets first look, with its OWN (higher) bar. Mistaking a real
        # conversation partner for "self" is the costlier failure — it
        # silently skips their translation entirely — so self requires
        # stronger evidence than an ordinary partner match, and that bar
        # only gets stricter under noise (see _NOISY_PROFILE).
        self_speaker = None
        if self.state.self_speaker_id:
            self_speaker = self.state.speakers.get(self.state.self_speaker_id)
            if self_speaker is not None:
                self_sim = cosine_similarity(embedding, self_speaker.embedding)
                if self_sim >= profile["self_match_threshold"]:
                    self_speaker.update_embedding(embedding)
                    return self_speaker

        best_speaker = None
        best_sim = -1.0
        for speaker in self.state.speakers.values():
            if self_speaker is not None and speaker.id == self_speaker.id:
                continue  # already evaluated above against its own bar
            sim = cosine_similarity(embedding, speaker.embedding)
            if sim > best_sim:
                best_sim = sim
                best_speaker = speaker

        if best_speaker is not None and best_sim >= profile["match_threshold"]:
            best_speaker.update_embedding(embedding)
            return best_speaker

        new_speaker = Speaker(id=f"spk_{len(self.state.speakers) + 1}", embedding=embedding)
        self.state.add_speaker(new_speaker)
        return new_speaker

    # -- fused partner-confidence score ----------------------------------
    def _fused_confidence(self, speaker: Speaker, signals: SignalBundle,
                           profile: dict) -> tuple[float, float]:
        if speaker.id in self.state.active_partner_ids:
            voice_component = cosine_similarity(signals.speaker_embedding, speaker.embedding)
        else:
            voice_component = 0.5

        fused = (
            profile["w_voice"] * voice_component
            + profile["w_turn"] * signals.turn_taking_score
            + profile["w_semantic"] * signals.semantic_coherence_score
        )
        return fused, voice_component

    def _signal_breakdown(self, voice_component: float, signals: SignalBundle,
                           profile: dict) -> dict:
        """Everything a UI needs to render *why* a decision came out the way
        it did — the raw per-signal readings, the weights and thresholds
        actually in force this turn (they shift under noise, see
        _CLEAN_PROFILE/_NOISY_PROFILE), and which profile was picked.
        Spread into a CIEDecision(**extras) at every real decision point."""
        return dict(
            voice_similarity=round(voice_component, 3),
            turn_taking_score=round(signals.turn_taking_score, 3),
            semantic_coherence_score=round(signals.semantic_coherence_score, 3),
            weight_voice=profile["w_voice"],
            weight_turn=profile["w_turn"],
            weight_semantic=profile["w_semantic"],
            bystander_threshold=profile["bystander"],
            lock_threshold=profile["lock"],
            noise_profile="noisy" if profile is _NOISY_PROFILE else "clean",
        )

    # -- multi-signal bystander rejection -----------------------------------
    def _clears_multi_signal_gate(self, voice_component: float, signals: SignalBundle) -> bool:
        """Require multiple INDEPENDENT signals to individually corroborate
        before a brand-new voice can be accepted as a partner (bootstrap,
        join, or switch) — not just a high weighted-sum confidence, which
        one freak strong signal can produce on its own (e.g. a bystander who
        happens to say something contextually plausible scores high semantic
        coherence even with weak turn-taking and no real voice match yet).

        For a speaker who isn't yet an active partner, voice_component is
        the neutral 0.5 prior (see _fused_confidence) rather than a measured
        similarity — it isn't evidence, so it doesn't count as a signal
        here. Both turn-taking AND semantic coherence must each clear
        SIGNAL_FLOOR on their own. Once a speaker IS an active partner and
        voice_component is a real measured similarity, that counts as a
        third independent signal and only two of the three need to clear."""
        real_signals = [signals.turn_taking_score, signals.semantic_coherence_score]
        if voice_component != 0.5:
            real_signals.append(voice_component)
        needed = 2
        return sum(1 for c in real_signals if c >= SIGNAL_FLOOR) >= needed

    # -- housekeeping ------------------------------------------------------
    def _prune_stale_partners(self, now: float) -> None:
        """Drop group members who've been silent past the (long) prune
        timeout, so a slot can't be held hostage by someone who has
        genuinely left the conversation entirely."""
        stale = [
            sid for sid in self.state.active_partner_ids
            if now - self.state.speakers[sid].last_seen >= PARTNER_PRUNE_TIMEOUT_S
        ]
        for sid in stale:
            self.state.active_partner_ids.discard(sid)
            self.state.speakers[sid].role = SpeakerRole.BYSTANDER
            self._incoherence_streaks.pop(sid, None)

    def _most_absent_replaceable_member(self, now: float) -> Speaker | None:
        """The current group member silent the longest, IF that silence
        clears the absence timeout (i.e. actually eligible to be replaced).
        Returns None if the group is empty or nobody has been gone long
        enough."""
        if not self.state.active_partner_ids:
            return None
        candidates = [self.state.speakers[sid] for sid in self.state.active_partner_ids]
        most_absent = min(candidates, key=lambda s: s.last_seen)
        if now - most_absent.last_seen >= PARTNER_ABSENCE_TIMEOUT_S:
            return most_absent
        return None

    def _commit(self, speaker: Speaker, confidence: float,
                displacing: Speaker | None, note: str, extras: dict | None = None) -> CIEDecision:
        if displacing is not None:
            self.state.active_partner_ids.discard(displacing.id)
            displacing.role = SpeakerRole.BYSTANDER
            self._incoherence_streaks.pop(displacing.id, None)

        self.state.active_partner_ids.add(speaker.id)
        speaker.role = SpeakerRole.PARTNER
        speaker.confidence = confidence
        self._incoherence_streaks[speaker.id] = 0
        self._pending_candidate_id = None
        self._pending_candidate_streak = 0

        return CIEDecision(
            speaker.id, SpeakerRole.PARTNER, confidence,
            partner_switched=displacing is not None,
            partner_joined=displacing is None,
            notes=note,
            **(extras or {}),
        )

    # -- self-voice enrollment -----------------------------------------
    def enroll_self(self, embedding: list[float]) -> Speaker:
        """Registers the user's own voice so it's permanently excluded from
        partner candidacy — see the module docstring and README's known-gap
        note this fixes. Call once per session (e.g. from a few seconds of
        the user's own speech captured during setup), before any other
        utterances are processed. Safe to call again with a consistent
        embedding (e.g. re-enrollment) — it will re-resolve to the same
        Speaker via the normal matching logic rather than creating a
        duplicate."""
        speaker = self._match_or_create_speaker(embedding, self._profile())
        speaker.role = SpeakerRole.SELF
        self.state.self_speaker_id = speaker.id
        # A self speaker can never simultaneously be a group member — if
        # somehow already in the group (e.g. enrollment happens mid-session
        # after mistakenly being tracked as a partner), remove it.
        self.state.active_partner_ids.discard(speaker.id)
        self._incoherence_streaks.pop(speaker.id, None)
        return speaker

    # -- main entry point --------------------------------------------------
    def process_utterance(self, signals: SignalBundle) -> CIEDecision:
        """Public entry point. Thin wrapper around _process_utterance_inner
        that attaches the resolved speaker's last-known language to the
        decision (see ConversationState.set_speaker_language) — kept as a
        wrapper rather than inlined everywhere so the many early-return
        branches below don't each need to remember to set it."""
        decision = self._process_utterance_inner(signals)
        speaker = self.state.speakers.get(decision.speaker_id)
        if speaker is not None:
            decision.language = speaker.language
        return decision

    def _process_utterance_inner(self, signals: SignalBundle) -> CIEDecision:
        if signals.noise_class:
            self.state.environment.noise_class = signals.noise_class
        profile = self._profile()

        now = time.time()
        self._prune_stale_partners(now)

        speaker = self._match_or_create_speaker(signals.speaker_embedding, profile)
        speaker.last_seen = now

        # Self speech is never a partner candidate — short-circuit before
        # any group logic even looks at it. This is the fix for a real bug:
        # without it, the user's own speech could consume a conversation-
        # group slot (see docs/vendor_decision.md and PROGRESS.md).
        if self.state.self_speaker_id and speaker.id == self.state.self_speaker_id:
            return CIEDecision(speaker.id, SpeakerRole.SELF, confidence=1.0,
                                notes="self speaker (enrolled) — excluded from partner candidacy")

        confidence, voice_component = self._fused_confidence(speaker, signals, profile)
        extras = self._signal_breakdown(voice_component, signals, profile)

        # -- Case A: speaker is already an active group member -> reinforce
        if speaker.id in self.state.active_partner_ids:
            # Any in-progress candidacy from a DIFFERENT voice is stale once
            # an existing member reasserts themselves — see module note in
            # docs/vendor_decision.md on why this is a deliberately
            # conservative (if occasionally slow) simplification for v1.
            self._pending_candidate_id = None
            self._pending_candidate_streak = 0

            streak = self._incoherence_streaks.get(speaker.id, 0)
            if signals.semantic_coherence_score < 0.3:
                streak += 1
            else:
                streak = 0
            self._incoherence_streaks[speaker.id] = streak

            if streak >= INCOHERENCE_RECOVERY_TURNS:
                self.state.active_partner_ids.discard(speaker.id)
                self._incoherence_streaks.pop(speaker.id, None)
                speaker.role = SpeakerRole.UNKNOWN
                return CIEDecision(speaker.id, SpeakerRole.UNKNOWN, confidence,
                                    notes="sustained incoherence, dropping from partner group",
                                    **extras)

            speaker.confidence = confidence
            return CIEDecision(speaker.id, SpeakerRole.PARTNER, confidence,
                                notes="reinforced existing group member", **extras)

        # -- Case B: empty group -> bootstrap
        if not self.state.active_partner_ids:
            if confidence >= profile["lock"]:
                if self._clears_multi_signal_gate(voice_component, signals):
                    return self._commit(speaker, confidence, displacing=None,
                                         note="partner established", extras=extras)
                speaker.role = SpeakerRole.UNKNOWN
                return CIEDecision(
                    speaker.id, SpeakerRole.UNKNOWN, confidence,
                    notes="confidence sufficient but only one corroborating "
                          "signal — need stronger evidence before bootstrapping",
                    **extras,
                )
            if confidence < profile["bystander"]:
                speaker.role = SpeakerRole.BYSTANDER
                return CIEDecision(speaker.id, SpeakerRole.BYSTANDER, confidence,
                                    notes="below bystander threshold, no partner yet", **extras)
            speaker.role = SpeakerRole.UNKNOWN
            return CIEDecision(speaker.id, SpeakerRole.UNKNOWN, confidence,
                                notes="insufficient confidence to bootstrap partner", **extras)

        # -- Case C: speaker is a candidate against a non-empty group
        room_available = len(self.state.active_partner_ids) < MAX_ACTIVE_PARTNERS
        replace_target = None if room_available else self._most_absent_replaceable_member(now)
        # Even with room available, still prefer to replace a long-absent
        # member over silently growing the group past what's actually
        # happening (e.g. the original partner genuinely left and won't be
        # coming back) — but only once they've cleared the absence timeout.
        if replace_target is None:
            replace_target = self._most_absent_replaceable_member(now) if not room_available else None

        eligible = room_available or replace_target is not None
        if not eligible:
            speaker.role = SpeakerRole.BYSTANDER
            return CIEDecision(speaker.id, SpeakerRole.BYSTANDER, confidence,
                                notes="partner group full, no absent member to replace", **extras)

        if confidence < profile["bystander"]:
            speaker.role = SpeakerRole.BYSTANDER
            return CIEDecision(speaker.id, SpeakerRole.BYSTANDER, confidence,
                                notes="below bystander threshold", **extras)

        if confidence < profile["lock"]:
            speaker.role = SpeakerRole.UNKNOWN
            return CIEDecision(speaker.id, SpeakerRole.UNKNOWN, confidence,
                                notes="insufficient confidence to join or replace", **extras)

        gate_clears = self._clears_multi_signal_gate(voice_component, signals)
        join_bar = GROUP_JOIN_THRESHOLD if room_available and replace_target is None else FAST_TRACK_THRESHOLD

        if confidence >= join_bar and gate_clears:
            action = "joined conversation group" if replace_target is None else "partner switch: old partner absent"
            return self._commit(speaker, confidence, displacing=replace_target,
                                 note=f"{action} (fast-tracked)", extras=extras)

        # Moderate confidence (or a high-confidence read that didn't clear
        # the multi-signal gate): require the SAME candidate to clear the
        # bar again before committing (hysteresis) — applies whether they'd
        # be joining a free slot or replacing an absent member.
        if speaker.id == self._pending_candidate_id:
            self._pending_candidate_streak += 1
        else:
            self._pending_candidate_id = speaker.id
            self._pending_candidate_streak = 1

        if self._pending_candidate_streak >= CANDIDATE_CONFIRM_TURNS and gate_clears:
            action = "joined conversation group" if replace_target is None else "partner switch: old partner absent"
            return self._commit(speaker, confidence, displacing=replace_target,
                                 note=f"{action} (confirmed)", extras=extras)

        speaker.role = SpeakerRole.UNKNOWN
        note = (f"candidate awaiting confirmation ({self._pending_candidate_streak}/{CANDIDATE_CONFIRM_TURNS})"
                if gate_clears else
                "candidate confidence sufficient but lacks a second corroborating signal")
        return CIEDecision(speaker.id, SpeakerRole.UNKNOWN, confidence, notes=note, **extras)
