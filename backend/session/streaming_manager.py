"""
Bridges a StreamingASRAgent (real-time, event-callback ASR — see
agents/base.py) into the existing SessionManager pipeline (CIE gating +
Translation + TTS).

This is the piece flagged as missing in agents/asr_assemblyai.py's module
docstring: the old ASRAgent protocol was request/response, which doesn't
match how real streaming vendors work. StreamingASRAgent does, and this
adapter turns its partial/final event stream into calls against the same
pipeline handle_utterance already uses — so swapping
agents/mock_streaming_asr.py for the real AssemblyAI adapter requires no
changes here or anywhere downstream.

NOTE on turn-taking/semantic-coherence signals: in the real architecture
these come from elsewhere (speaker timing, the Context Memory Agent), not
from ASR — this mirrors how the rest of the system already treats them as
externally-supplied signals (see cie.SignalBundle). This adapter accepts
them via a `signal_provider` callback so callers can supply real or
reasonable-default values per turn.
"""

from __future__ import annotations

from typing import Callable

from agents.base import StreamingASRAgent, StreamingASRResult
from session.manager import PipelineResult, SessionManager

DEFAULT_SIGNAL = (0.8, 0.8)  # (turn_taking_score, semantic_coherence_score) fallback


class StreamingSessionAdapter:
    def __init__(self, session: SessionManager, asr_agent: StreamingASRAgent,
                 target_lang: str = "en",
                 signal_provider: Callable[[StreamingASRResult], tuple[float, float]] | None = None,
                 on_pipeline_result: Callable[[PipelineResult], None] | None = None,
                 on_final_asr_result: Callable[[StreamingASRResult], None] | None = None):
        self.session = session
        self.asr_agent = asr_agent
        self.target_lang = target_lang
        self.signal_provider = signal_provider or (lambda _result: DEFAULT_SIGNAL)
        self.on_pipeline_result = on_pipeline_result
        # Purely a connection-lifecycle hook (see app.py's
        # websocket_audio_session idle-timeout guard) - fires on every
        # final ASR result, even an empty silence-flush one that never
        # reaches the pipeline below. Deliberately separate from
        # on_pipeline_result: that one only fires for a real, non-empty
        # transcribed turn, which is exactly the signal the idle timer
        # needs, but callers who only care about "did the ASR agent
        # produce ANY final event" (this one) shouldn't have to filter
        # pipeline results themselves. Does not affect CIE/translation/TTS
        # gating in any way - purely observational.
        self.on_final_asr_result = on_final_asr_result
        self.results: list[PipelineResult] = []

    def start(self, sample_rate: int = 16000) -> None:
        self.asr_agent.start(on_result=self._handle_asr_result, sample_rate=sample_rate)

    def push_audio(self, chunk) -> None:
        self.asr_agent.send_audio(chunk)

    def stop(self) -> None:
        self.asr_agent.stop()

    def _handle_asr_result(self, result: StreamingASRResult) -> None:
        if not result.is_final:
            return  # partial hypothesis — nothing to gate/translate yet

        if self.on_final_asr_result:
            self.on_final_asr_result(result)

        if not result.text.strip():
            return  # empty final turn (e.g. a pure-silence flush)

        turn_taking_score, coherence_score = self.signal_provider(result)
        speaker_label = result.speaker_label or "unknown_streaming_speaker"

        pipeline_result = self.session.handle_transcribed_utterance(
            speaker_label=speaker_label,
            text=result.text,
            asr_confidence=result.confidence,
            target_lang=self.target_lang,
            turn_taking_score=turn_taking_score,
            semantic_coherence_score=coherence_score,
        )
        self.results.append(pipeline_result)
        if self.on_pipeline_result:
            self.on_pipeline_result(pipeline_result)
