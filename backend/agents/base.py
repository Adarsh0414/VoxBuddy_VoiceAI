"""
Agent interfaces. Each real vendor integration (Google/Azure/OpenAI-class
ASR, MT, TTS, embedding models, etc.) implements one of these Protocols.
The rest of the system never depends on a concrete vendor — only on this
contract — so swapping providers during Phase 1 benchmarking (see PRD §21)
doesn't touch orchestration code.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol


@dataclass
class ASRResult:
    text: str
    language: str
    confidence: float


@dataclass
class TranslationResult:
    text: str
    confidence: float


@dataclass
class TTSResult:
    audio_bytes: bytes
    duration_ms: float


class SpeakerEmbeddingAgent(Protocol):
    def embed(self, audio_chunk: bytes) -> list[float]: ...


class LanguageIDAgent(Protocol):
    def detect(self, audio_or_text: str) -> tuple[str, float]: ...


class ASRAgent(Protocol):
    """Request/response ASR: whole utterance in, one result out. Fine for
    the mocked PoC and for offline/batch transcription, but does NOT match
    how any real streaming vendor's SDK actually works — see
    StreamingASRAgent below, which is what a real integration should target
    for the live conversation hot path."""

    def transcribe(self, audio_chunk: bytes) -> ASRResult: ...


@dataclass
class StreamingASRResult:
    """What a real streaming ASR vendor actually hands you: a running
    sequence of partial results per turn, ending in one final result.
    Mirrors AssemblyAI's turn-event shape (see agents/asr_assemblyai.py) —
    other streaming vendors (Deepgram, Speechmatics) differ in field names
    but follow the same partial-then-final pattern, so this is the
    vendor-neutral contract the rest of the system should depend on."""

    text: str
    is_final: bool            # True once this turn is done (AssemblyAI: end_of_turn)
    confidence: float
    speaker_label: str | None = None   # inline diarization label, if the vendor provides one
    language: str | None = None


class StreamingASRAgent(Protocol):
    """
    Real-time streaming ASR, event-callback shaped to match how actual
    vendor SDKs work (a persistent connection, audio pushed in, results
    delivered via callback — not a single call-in/result-out like ASRAgent).

    Usage:
        agent.start(on_result=my_handler, sample_rate=16000)
        agent.send_audio(pcm_chunk)   # called repeatedly as audio arrives
        ...
        agent.stop()

    `on_result` is invoked for every result (partial AND final) — callers
    that only care about complete utterances should filter on
    `result.is_final`, which is exactly what session/streaming_manager.py
    does to bridge this into the existing CIE + Translation + TTS pipeline.
    """

    def start(self, on_result: "Callable[[StreamingASRResult], None]",
               sample_rate: int = 16000) -> None: ...

    def send_audio(self, pcm_chunk: bytes) -> None: ...

    def stop(self) -> None: ...


class TranslationAgent(Protocol):
    def translate(self, text: str, source_lang: str, target_lang: str,
                   context: list[str]) -> TranslationResult: ...


class TTSAgent(Protocol):
    def synthesize(self, text: str, target_lang: str) -> TTSResult: ...
