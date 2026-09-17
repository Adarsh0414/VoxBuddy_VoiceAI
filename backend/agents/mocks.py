"""
Mock implementations of the agent interfaces, used for the Phase 1 PoC and
for unit tests. These simulate realistic latency and confidence behavior
without calling any external vendor — swap these out for real ASR/MT/TTS
providers in Phase 2 once one is benchmarked and chosen (PRD §21).
"""

from __future__ import annotations

import hashlib
import random
import time

from .base import ASRResult, TranslationResult, TTSResult


class LabelDerivedSpeakerIdentityAgent:
    """Speaker identity derived directly from an ASR/diarization speaker
    label, rather than a separate acoustic embedding model.

    This is a deliberate implementation of the recommendation in
    docs/vendor_decision.md §4: real streaming ASR vendors (AssemblyAI
    included, via agents/asr_assemblyai.py) already return an inline
    diarization label per turn at no extra cost — standing up a separate
    x-vector/d-vector acoustic embedding model in v1 to re-derive
    something the ASR vendor already gives you is redundant. So this
    turns whatever label the upstream ASR agent supplies (real or mock)
    into a stable pseudo-embedding: same label -> identical vector ->
    the CIE's cosine-similarity speaker matcher (agents/engine.py's
    _match_or_create_speaker) always resolves it to the same Speaker.

    Honest limitation: this is genuinely NOT an acoustic voice-similarity
    embedding — it can't recognize "the same physical voice" if the
    upstream label for that voice changes (e.g. AssemblyAI relabeling a
    speaker mid-session), and it can't merge two different labels the
    upstream vendor mistakenly assigned to one real speaker. That's a
    real acoustic-embedding model's job, still not built (tracked as an
    open item in PROGRESS.md). What this class does guarantee: distinct
    labels resolve to distinct speakers with very low collision risk —
    the previous 8-dimensional version of this class (then named
    MockSpeakerEmbeddingAgent) used only the first 8 bytes of the label's
    SHA-256 hash, which left a non-negligible chance that two unrelated
    labels' random 8-dim vectors would exceed the CIE's 0.80
    cosine-similarity match threshold by pure chance and get incorrectly
    merged into one Speaker. Using the full 32-byte digest instead cuts
    that collision probability by many orders of magnitude, at zero
    extra cost."""

    def embed(self, speaker_label: str) -> list[float]:
        h = hashlib.sha256(speaker_label.encode()).digest()
        # Full 32-dim pseudo-embedding from the whole hash, normalized to
        # [-1, 1] — see the class docstring for why 32 dims instead of 8.
        return [(b / 127.5) - 1.0 for b in h]


# Backward-compatible alias — agents/factory.py and any external callers
# that still import the old name keep working unchanged.
MockSpeakerEmbeddingAgent = LabelDerivedSpeakerIdentityAgent


class MockLanguageIDAgent:
    def detect(self, text: str) -> tuple[str, float]:
        # PoC heuristic: tag known sample phrases; real system uses a
        # streaming LID model on audio.
        if any(w in text.lower() for w in ["namaste", "kya", "aap"]):
            return "hi", 0.95
        if any(w in text.lower() for w in ["bonjour", "merci", "comment"]):
            return "fr", 0.95
        return "en", 0.9


class MockASRAgent:
    def transcribe(self, utterance_text: str) -> ASRResult:
        # In the PoC we pass "ground truth" text straight through with a
        # simulated confidence + latency, standing in for a real streaming
        # ASR call.
        time.sleep(0.01)
        return ASRResult(text=utterance_text, language="auto", confidence=round(random.uniform(0.85, 0.99), 2))


class MockTranslationAgent:
    def translate(self, text: str, source_lang: str, target_lang: str,
                   context: list[str]) -> TranslationResult:
        time.sleep(0.01)
        # Stand-in "translation": tag the text with the target language so
        # the pipeline's behavior is observable in the simulation output.
        return TranslationResult(text=f"[{target_lang}] {text}", confidence=round(random.uniform(0.8, 0.97), 2))


class MockTTSAgent:
    def synthesize(self, text: str, target_lang: str) -> TTSResult:
        time.sleep(0.01)
        return TTSResult(audio_bytes=text.encode(), duration_ms=len(text) * 40)
