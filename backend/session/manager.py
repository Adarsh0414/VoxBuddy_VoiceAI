"""
Session Manager — the "hot path" orchestrator described in PRD §9. It wires
the CIE together with the ASR/Translation/TTS agents and records per-stage
latency so we can validate against the <=2.0s budget (PRD §6) from day one,
even in this mocked PoC.
"""

from __future__ import annotations

import base64
import time
import uuid
from dataclasses import dataclass

from cie.engine import CIEDecision, ConversationIntelligenceEngine, SignalBundle
from cie.state import ConversationState, SpeakerRole, Turn
from cie import calibration_log
from agents.base import TranslationAgent, TTSAgent
from agents.factory import get_translation_agent, get_tts_agent
from agents.mocks import (
    MockASRAgent,
    MockLanguageIDAgent,
    MockSpeakerEmbeddingAgent,
)


@dataclass
class IncomingUtterance:
    """What arrives from the mobile client for one utterance (mocked audio)."""

    speaker_label: str          # PoC stand-in for a raw audio chunk
    text: str                   # PoC stand-in for what ASR would produce
    target_lang: str
    turn_taking_score: float
    semantic_coherence_score: float
    noise_class: str | None = None  # optional ambient-noise estimate, see
                                     # cie/engine.py's noise-adaptive profiles


@dataclass
class PipelineResult:
    decision: CIEDecision
    turn: Turn | None
    latency_ms: float
    translated_text: str | None
    tts_audio_b64: str | None = None
    tts_audio_format: str | None = None
    tts_error: str | None = None
    translation_error: str | None = None


class SessionManager:
    def __init__(self, target_lang_self: str = "en",
                 translation_agent: TranslationAgent | None = None,
                 tts_agent: TTSAgent | None = None,
                 tts_audio_format: str | None = None):
        self.state = ConversationState()
        self.cie = ConversationIntelligenceEngine(self.state)
        self.embedding_agent = MockSpeakerEmbeddingAgent()
        self.lid_agent = MockLanguageIDAgent()
        self.asr_agent = MockASRAgent()
        # Translation is the one stage with a real, tested vendor adapter
        # so far (see docs/vendor_decision.md). Which implementation runs
        # here is controlled by VOXBUDDY_TRANSLATION_PROVIDER, or can be
        # injected directly (used by tests to keep them hermetic/offline).
        self.translation_agent = translation_agent or get_translation_agent()
        # Same pattern for TTS, controlled by VOXBUDDY_TTS_PROVIDER — see
        # agents/factory.py's get_tts_agent() for what "mock" vs
        # "elevenlabs" actually produce. tts_audio_format tells callers
        # (app.py's _to_out) whether tts_audio_b64 below is real playable
        # audio or the mock's placeholder text bytes.
        if tts_agent is not None:
            self.tts_agent = tts_agent
            self.tts_audio_format = tts_audio_format or "mock-text"
        else:
            self.tts_agent, self.tts_audio_format = get_tts_agent()
        self.target_lang_self = target_lang_self

    def enroll_self(self, speaker_label: str) -> None:
        """Registers the user's own voice at session start so the CIE never
        evaluates it as a partner candidate. speaker_label is the same kind
        of stand-in used everywhere else in this mocked PoC (a real
        integration would pass a real embedding captured from a few seconds
        of the user's own speech during setup)."""
        embedding = self.embedding_agent.embed(speaker_label)
        self.cie.enroll_self(embedding)

    def handle_utterance(self, utt: IncomingUtterance) -> PipelineResult:
        """Entry point for the mocked per-utterance PoC flow: text stands in
        for raw audio, and self.asr_agent (a request/response mock) stands
        in for a real ASR call. utt.noise_class is an optional per-utterance
        ambient-noise estimate (e.g. from mic RMS on the client) that feeds
        the CIE's noise-adaptive thresholds — see cie/engine.py."""
        start = time.perf_counter()
        decision = self._gate_with_cie(utt.speaker_label, utt.turn_taking_score,
                                        utt.semantic_coherence_score, utt.noise_class)
        if decision.role in (SpeakerRole.BYSTANDER, SpeakerRole.UNKNOWN):
            return self._filtered_result(decision, start)

        asr_result = self.asr_agent.transcribe(utt.text)
        return self._translate_and_record(decision, asr_result.text, asr_result.confidence,
                                           utt.target_lang, start)

    def handle_transcribed_utterance(self, speaker_label: str, text: str,
                                      asr_confidence: float, target_lang: str,
                                      turn_taking_score: float,
                                      semantic_coherence_score: float,
                                      noise_class: str | None = None) -> PipelineResult:
        """Entry point for a pipeline stage where ASR has ALREADY produced
        text — e.g. a real streaming vendor's final-turn event, bridged in
        by session/streaming_manager.py. Same CIE gating as handle_utterance,
        just skipping the (mocked) transcription step since there's nothing
        left to transcribe."""
        start = time.perf_counter()
        decision = self._gate_with_cie(speaker_label, turn_taking_score,
                                        semantic_coherence_score, noise_class)
        if decision.role in (SpeakerRole.BYSTANDER, SpeakerRole.UNKNOWN):
            return self._filtered_result(decision, start)

        return self._translate_and_record(decision, text, asr_confidence, target_lang, start)

    # -- shared internals ---------------------------------------------------
    def _gate_with_cie(self, speaker_label: str, turn_taking_score: float,
                        semantic_coherence_score: float,
                        noise_class: str | None = None) -> CIEDecision:
        embedding = self.embedding_agent.embed(speaker_label)
        signals = SignalBundle(
            speaker_embedding=embedding,
            turn_taking_score=turn_taking_score,
            semantic_coherence_score=semantic_coherence_score,
            noise_class=noise_class,
        )
        decision = self.cie.process_utterance(signals)
        # Opt-in calibration data flywheel (see cie/calibration_log.py) — a
        # no-op (no boto3 import, no network call) unless
        # VOXBUDDY_CALIBRATION_LOGGING=true and a bucket is configured.
        # Logs only the anonymized signal breakdown already on `decision`,
        # never the raw embedding or speaker_label above.
        calibration_log.log_decision(signals, decision)
        return decision

    def _filtered_result(self, decision: CIEDecision, start: float) -> PipelineResult:
        # Bystanders and unresolved speakers never enter the translation
        # pipeline — this is the CIE actively gating cost + noise, not just
        # a downstream filter (PRD §7.3).
        latency_ms = (time.perf_counter() - start) * 1000
        return PipelineResult(decision=decision, turn=None, latency_ms=latency_ms,
                               translated_text=None)

    def _translate_and_record(self, decision: CIEDecision, text: str, asr_confidence: float,
                               target_lang: str, start: float) -> PipelineResult:
        source_lang, _ = self.lid_agent.detect(text)
        # Multi-speaker language tracking: remember each speaker's last ID'd
        # language on their Speaker record (Speaker A -> English, Speaker B
        # -> Hindi, ...), maintained across turns for as long as the session
        # keeps re-matching them to the same voice.
        self.state.set_speaker_language(decision.speaker_id, source_lang)
        context = [t.target_text for t in self.state.recent_turns(3)]

        # This call used to be unguarded — a real vendor failure (bad
        # model name, missing/expired key, quota, a 404 from a
        # deprecated model ID) raised straight out of here. Nothing
        # downstream ever caught it: it propagated out of
        # session/streaming_manager.py's _handle_asr_result, which for
        # AssemblyAIStreamingASRAgent runs on the vendor SDK's own
        # background thread (see asr_assemblyai.py) — an exception raised
        # there, inside a foreign SDK's callback dispatch, has nowhere to
        # go and is typically swallowed by the SDK's own internal
        # try/except rather than surfacing anywhere. The practical
        # symptom: ASR genuinely transcribes, then the whole turn just
        # vanishes — indistinguishable from "still listening" on the
        # client, since nothing ever reaches app.py's WebSocket send.
        # Mirrors the TTS error-handling immediately below, which already
        # got this right.
        try:
            translation = self.translation_agent.translate(text, source_lang, target_lang, context)
        except Exception as exc:  # noqa: BLE001 - vendor errors vary by SDK
            latency_ms = (time.perf_counter() - start) * 1000
            return PipelineResult(decision=decision, turn=None, latency_ms=latency_ms,
                                   translated_text=None, translation_error=str(exc))

        # TTS output used to be synthesized and immediately discarded here
        # (the return value was never used) — nothing downstream ever
        # actually heard the translated speech. Now it's captured and
        # attached to the PipelineResult so app.py can send it to the
        # frontend. A real vendor failure (e.g. ElevenLabs's placeholder
        # voice_ids, or a bad API key) shouldn't break the pipeline the
        # translation itself still succeeded — so it's caught and reported
        # as tts_error rather than raised.
        tts_audio_b64 = None
        tts_error = None
        try:
            tts_result = self.tts_agent.synthesize(translation.text, target_lang)
            tts_audio_b64 = base64.b64encode(tts_result.audio_bytes).decode("ascii")
        except Exception as exc:  # noqa: BLE001 - vendor errors vary by SDK
            tts_error = str(exc)

        latency_ms = (time.perf_counter() - start) * 1000

        turn = Turn(
            id=str(uuid.uuid4()),
            speaker_id=decision.speaker_id,
            source_lang=source_lang,
            source_text=text,
            target_lang=target_lang,
            target_text=translation.text,
            asr_confidence=asr_confidence,
            translation_confidence=translation.confidence,
            latency_ms=latency_ms,
        )
        self.state.record_turn(turn)

        return PipelineResult(decision=decision, turn=turn, latency_ms=latency_ms,
                               translated_text=translation.text,
                               tts_audio_b64=tts_audio_b64,
                               tts_audio_format=self.tts_audio_format,
                               tts_error=tts_error)
