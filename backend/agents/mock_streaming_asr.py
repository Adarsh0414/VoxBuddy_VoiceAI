"""
Mock implementation of StreamingASRAgent (see agents/base.py).

This is what lets the streaming session pipeline (session/streaming_manager.py)
be built and fully tested in this environment, where there's no network
access to a real streaming ASR vendor. It simulates the exact
partial-then-final event pattern a real vendor emits, driven by a simple
stand-in for audio: callers push "chunk" strings via send_audio() instead of
real PCM bytes, and each chunk is treated as one word/token of the utterance.
A chunk equal to the sentinel END_OF_TURN flushes a final result.

Swapping this for agents/asr_assemblyai.py's real adapter requires no
changes to anything downstream — both implement the same
StreamingASRAgent protocol.
"""

from __future__ import annotations

from typing import Callable

from .base import StreamingASRResult

END_OF_TURN = "<END_OF_TURN>"


class MockStreamingASRAgent:
    def __init__(self, speaker_label: str = "mock_speaker", language: str = "en"):
        self.speaker_label = speaker_label
        self.language = language
        self._on_result: Callable[[StreamingASRResult], None] | None = None
        self._buffer: list[str] = []
        self._started = False

    def start(self, on_result: Callable[[StreamingASRResult], None],
               sample_rate: int = 16000) -> None:
        self._on_result = on_result
        self._buffer = []
        self._started = True

    def send_audio(self, pcm_chunk) -> None:
        if not self._started:
            raise RuntimeError("call start() before send_audio()")

        # pcm_chunk stands in for a chunk of real audio; in this mock it's a
        # plain string token or the END_OF_TURN sentinel.
        token = pcm_chunk if isinstance(pcm_chunk, str) else pcm_chunk.decode("utf-8")

        if token == END_OF_TURN:
            final_text = " ".join(self._buffer).strip()
            self._buffer = []
            self._on_result(StreamingASRResult(
                text=final_text, is_final=True, confidence=0.93,
                speaker_label=self.speaker_label, language=self.language,
            ))
            return

        self._buffer.append(token)
        partial_text = " ".join(self._buffer).strip()
        self._on_result(StreamingASRResult(
            text=partial_text, is_final=False, confidence=0.75,
            speaker_label=self.speaker_label, language=self.language,
        ))

    def stop(self) -> None:
        self._started = False
        self._on_result = None
        self._buffer = []
