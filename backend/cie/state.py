"""
Conversation State Graph — the CIE's authoritative model of "what is happening
in this conversation right now."

This is intentionally decoupled from any specific ASR/MT/TTS vendor. Agents
(speaker ID, language ID, translation, etc.) only ever read/write through this
model, never through each other directly — that's what keeps the system a
"multi-agent architecture" instead of a tangle of point-to-point calls.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class SpeakerRole(str, Enum):
    SELF = "self"
    PARTNER = "partner"
    BYSTANDER = "bystander"
    UNKNOWN = "unknown"


@dataclass
class Speaker:
    """A voice the system has observed in this session."""

    id: str
    embedding: list[float]  # mock: in production this is a real d-vector/x-vector
    role: SpeakerRole = SpeakerRole.UNKNOWN
    confidence: float = 0.0
    first_seen: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    turn_count: int = 0
    language: Optional[str] = None       # last language ID'd for this speaker
                                          # (set by the pipeline post-LID, not
                                          # the CIE itself) — lets callers show
                                          # "Speaker A -> English, Speaker B ->
                                          # Hindi" across turns.
    embedding_samples: int = 1           # how many utterances have folded
                                          # into `embedding` via update_embedding

    def update_embedding(self, new_embedding: list[float], alpha: float = 0.2) -> None:
        """Fold a newly-matched utterance's embedding into this speaker's
        voice centroid via an exponential moving average, instead of freezing
        forever on whatever the first utterance happened to sound like.
        Voices drift over a session (distance from mic, background noise,
        vocal fatigue) — a centroid that adapts stays a better comparison
        target than a static first sample, which is what "better voice
        embedding comparison" concretely means here. alpha=0.2 keeps the
        identity stable (no single noisy read can hijack it) while still
        tracking gradual drift."""
        self.embedding = [
            (1 - alpha) * old + alpha * new
            for old, new in zip(self.embedding, new_embedding)
        ]
        self.embedding_samples += 1


@dataclass
class Turn:
    """One utterance, fully processed."""

    id: str
    speaker_id: str
    source_lang: str
    source_text: str
    target_lang: str
    target_text: str
    asr_confidence: float
    translation_confidence: float
    timestamp: float = field(default_factory=time.time)
    latency_ms: Optional[float] = None


@dataclass
class EnvironmentProfile:
    noise_class: str = "unknown"  # e.g. "quiet", "moderate", "loud_crowd"
    estimated_voice_count: int = 0


@dataclass
class ConversationState:
    """The full authoritative state for one session.

    v1 limitation being fixed here: this used to track exactly ONE active
    partner (active_partner_id). Real conversations routinely involve more
    than one person talking to the user (a couple at a market stall, two
    colleagues in a meeting) — see docs/vendor_decision.md / PRD environments
    list. `active_partner_ids` replaces the singular field with a small,
    capped group (see cie/engine.py MAX_ACTIVE_PARTNERS).
    """

    session_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    speakers: dict[str, Speaker] = field(default_factory=dict)
    active_partner_ids: set[str] = field(default_factory=set)
    self_speaker_id: Optional[str] = None
    turn_history: list[Turn] = field(default_factory=list)
    environment: EnvironmentProfile = field(default_factory=EnvironmentProfile)
    max_history: int = 20

    def add_speaker(self, speaker: Speaker) -> None:
        self.speakers[speaker.id] = speaker

    def set_speaker_language(self, speaker_id: str, language: str) -> None:
        """Record the last language ID'd for this speaker so multi-speaker
        sessions can report e.g. Speaker A -> English, Speaker B -> Hindi,
        Speaker C -> Tamil, maintained across turns. Called by the pipeline
        after language ID runs (the CIE itself has no opinion on language)."""
        speaker = self.speakers.get(speaker_id)
        if speaker is not None:
            speaker.language = language

    def record_turn(self, turn: Turn) -> None:
        self.turn_history.append(turn)
        if len(self.turn_history) > self.max_history:
            self.turn_history.pop(0)
        if turn.speaker_id in self.speakers:
            self.speakers[turn.speaker_id].turn_count += 1
            self.speakers[turn.speaker_id].last_seen = turn.timestamp

    def recent_turns(self, n: int = 5) -> list[Turn]:
        return self.turn_history[-n:]

    @property
    def primary_partner_id(self) -> Optional[str]:
        """The most recently active member of the partner group, for UIs/
        APIs that just want a single "who am I talking to" headline value.
        Group membership itself (active_partner_ids) is the source of truth."""
        if not self.active_partner_ids:
            return None
        return max(self.active_partner_ids, key=lambda sid: self.speakers[sid].last_seen)
