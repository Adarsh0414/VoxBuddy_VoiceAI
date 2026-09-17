"""
AssemblyAI streaming ASR + inline diarization adapter.

STATUS: scaffolded against the real assemblyai-python-sdk v3 streaming
shape (per docs/vendor_decision.md §1), NOT live-tested — this build
environment has no network access to AssemblyAI and no API key. Treat this
as a correct starting point for Phase 2, not a finished integration.

This implements the StreamingASRAgent protocol from agents/base.py — the
same interface agents/mock_streaming_asr.py implements, which is what lets
the whole streaming pipeline (session/streaming_manager.py) be built and
tested without live AssemblyAI access. Swapping the mock for this adapter
at integration time should require no changes anywhere downstream.
"""

from __future__ import annotations

import os
import queue
import threading
from typing import Callable

from .base import ASRResult, StreamingASRResult

# Sentinel pushed onto the audio queue to signal "no more audio" — stop()
# uses this to end the generator handed to client.stream() (see below for
# why a queue+generator is required at all).
_STOP = object()


class AssemblyAIStreamingASRAgent:
    """
    Real-time ASR + diarization over AssemblyAI's v3 streaming WebSocket.

    Usage (matches the StreamingASRAgent protocol exactly):

        agent = AssemblyAIStreamingASRAgent()
        agent.start(on_result=my_handler, sample_rate=16000)
        agent.send_audio(pcm_bytes)   # called repeatedly as audio arrives
        ...
        agent.stop()

    `on_result` is called with a StreamingASRResult for every turn event
    (partial and final) — session/streaming_manager.py only acts on final
    (`is_final=True`) results, matching the utterance-level granularity the
    rest of the pipeline (Translation, TTS) already expects.
    """

    def __init__(self, api_key: str | None = None, speech_model: str = "universal-3-5-pro"):
        self.api_key = api_key or os.environ.get("ASSEMBLYAI_API_KEY")
        self.speech_model = speech_model
        self._client = None
        self._streaming_params_cls = None
        # client.stream() (below) is a BLOCKING call that consumes an
        # iterable/generator of chunks for the life of the session — it is
        # meant to be called ONCE (see AssemblyAI's own examples, e.g.
        # `client.stream(aai.extras.MicrophoneStream(...))`), not called
        # repeatedly with one chunk at a time. But this class's own
        # send_audio() is called repeatedly, once per ~256ms browser audio
        # frame, matching the StreamingASRAgent protocol every other
        # agent (including the mock) follows. A bounded queue bridges the
        # two shapes: send_audio() just enqueues bytes (fast, non-blocking,
        # safe to call from the asyncio event loop thread), while a
        # generator pulls from that queue and is handed to client.stream()
        # exactly once, inside its own background thread so the blocking
        # call never blocks the event loop.
        self._audio_queue: "queue.Queue[bytes | object]" = queue.Queue()
        self._stream_thread: threading.Thread | None = None
        self._stream_error: Exception | None = None

    def _build_client(self, on_result: Callable[[StreamingASRResult], None]):
        if not self.api_key:
            raise RuntimeError(
                "ASSEMBLYAI_API_KEY is not set. Set it in the environment "
                "or pass api_key= explicitly."
            )
        # Imported lazily so the mocked PoC path never needs this SDK
        # installed.
        from assemblyai.streaming.v3 import (
            StreamingClient,
            StreamingClientOptions,
            StreamingParameters,
            StreamingEvents,
            TurnEvent,
            StreamingError,
        )

        client = StreamingClient(StreamingClientOptions(api_key=self.api_key))

        def _on_turn(_client, event: TurnEvent):
            if not event.transcript:
                return
            result = StreamingASRResult(
                text=event.transcript,
                is_final=bool(event.end_of_turn),
                confidence=getattr(event, "confidence", 0.9),
                # NOTE: speaker label extraction depends on the exact field
                # name in the SDK version pinned at integration time —
                # verify against current docs, this is the one part of this
                # adapter most likely to have drifted by the time it's wired
                # up for real.
                speaker_label=getattr(event, "speaker", None),
                language=getattr(event, "language", None),
            )
            on_result(result)

        def _on_error(_client, error: StreamingError):
            raise RuntimeError(f"AssemblyAI streaming error: {error}")

        client.on(StreamingEvents.Turn, _on_turn)
        client.on(StreamingEvents.Error, _on_error)
        self._streaming_params_cls = StreamingParameters
        return client

    def _audio_generator(self):
        """Yields chunks pushed via send_audio() until stop() enqueues the
        _STOP sentinel. This is what actually gets handed to
        client.stream() — see the __init__ comment for why."""
        while True:
            chunk = self._audio_queue.get()
            if chunk is _STOP:
                return
            yield chunk

    def _run_stream(self) -> None:
        try:
            self._client.stream(self._audio_generator())
        except Exception as exc:  # noqa: BLE001 - surfaced via _stream_error
            # client.stream() runs in its own thread (see start()), so an
            # exception here would otherwise vanish silently instead of
            # reaching the websocket handler in app.py. Stash it so
            # send_audio()/stop() can re-raise it on the calling thread.
            self._stream_error = exc

    def start(self, on_result: Callable[[StreamingASRResult], None],
               sample_rate: int = 16000) -> None:
        self._client = self._build_client(on_result)
        self._client.connect(self._streaming_params_cls(
            sample_rate=sample_rate,
            speech_model=self.speech_model,
        ))
        # client.stream() blocks for the whole session consuming the
        # generator above, so it has to run off the asyncio event-loop
        # thread — otherwise every subsequent send_audio()/websocket
        # message in app.py would hang behind it.
        self._stream_thread = threading.Thread(target=self._run_stream, daemon=True)
        self._stream_thread.start()

    def send_audio(self, pcm_chunk: bytes) -> None:
        if self._client is None:
            raise RuntimeError("call start() before send_audio()")
        if self._stream_error is not None:
            err, self._stream_error = self._stream_error, None
            raise err
        self._audio_queue.put(pcm_chunk)

    def stop(self) -> None:
        if self._client is not None:
            self._audio_queue.put(_STOP)
            if self._stream_thread is not None:
                self._stream_thread.join(timeout=5)
                self._stream_thread = None
            self._client.disconnect()
            self._client = None

    # -- separate batch mode, NOT part of the StreamingASRAgent protocol --
    # Provided only so this file has something runnable today against
    # short pre-recorded clips via AssemblyAI's batch endpoint, for offline
    # accuracy testing (e.g. the ASR/diarization bake-off in
    # docs/vendor_decision.md §5) — not suitable for the live conversation
    # hot path.
    def transcribe_batch(self, audio_url_or_path: str) -> ASRResult:
        if not self.api_key:
            raise RuntimeError("ASSEMBLYAI_API_KEY is not set.")
        import assemblyai as aai

        aai.settings.api_key = self.api_key
        config = aai.TranscriptionConfig(speaker_labels=True, language_detection=True)
        transcript = aai.Transcriber().transcribe(audio_url_or_path, config=config)
        return ASRResult(
            text=transcript.text or "",
            language=getattr(transcript, "language_code", "auto"),
            confidence=getattr(transcript, "confidence", 0.0) or 0.0,
        )
