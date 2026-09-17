"""
Amazon Transcribe streaming ASR + inline diarization adapter.

STATUS: scaffolded against the real `amazon-transcribe` (awslabs) async
Python SDK's documented streaming shape, NOT live-tested — this build
environment has no AWS credentials and Transcribe's streaming endpoint
isn't in this sandbox's network allowlist. Treat this exactly like
agents/asr_assemblyai.py's own module docstring: a correct starting point
to validate against a real AWS account, not a finished integration.

This implements the StreamingASRAgent protocol from agents/base.py — the
same interface agents/mock_streaming_asr.py and agents/asr_assemblyai.py
implement — so swapping any of the three in via VOXBUDDY_ASR_PROVIDER (see
agents/factory.py) requires no changes anywhere downstream (see
session/streaming_manager.py).

Why this exists alongside the AssemblyAI adapter rather than replacing it:
docs/vendor_decision.md §1 already picked AssemblyAI's inline, per-turn
diarization as the Phase 2 recommendation after a real vendor comparison.
This adapter doesn't reopen that call — it adds the AWS-native option the
README's "Future improvements" section already named as the next
integration to build, following the same adapter pattern already used for
Polly (agents/tts_polly.py) and DynamoDB (persistence_dynamodb.py). Worth
a real bake-off against AssemblyAI on VoxBuddy's actual target audio
(markets, accented/code-switched speech — see vendor_decision.md §1)
before picking one for production; nothing here forces that choice.

One real architectural difference from AssemblyAI worth flagging, since it
shapes the code below: Transcribe's streaming diarization labels are
per-WORD (each `Item` in a result carries its own `.speaker`), not
per-result like AssemblyAI's top-level `speaker` field on a Turn event — a
single final Result can in principle contain words from more than one
speaker if a turn boundary lands mid-utterance. This adapter resolves that
down to one label per final result via majority vote across that result's
items (see `_majority_speaker`), matching the per-utterance granularity
StreamingASRResult expects. A more careful integration might instead split
a mixed-speaker result into two utterances before it reaches the CIE, but
that's a real design decision for whoever validates this against live
audio, not something to guess at here.

A dependency note worth flagging up front: this scaffold targets the
`amazon-transcribe` PyPI package (awslabs' original async streaming SDK),
which AWS has since marked deprecated in favor of a new official
replacement, `aws_sdk_transcribe_streaming` — but that replacement is
still pre-1.0 and explicitly documented as subject to breaking changes.
`amazon-transcribe` is what's implemented below because it's the one with
a stable, documented API surface to scaffold against correctly; whoever
picks this up for real should check whether the new SDK has stabilized by
then and port if so — the shape of this adapter (start/send_audio/stop
bridging into an async client) should translate directly either way.
"""

from __future__ import annotations

import asyncio
import os
import queue
import threading
from collections import Counter
from typing import Callable

from .base import StreamingASRResult

# Sentinel pushed onto the audio queue to signal "no more audio" — stop()
# uses this to end the writer coroutine below (see _session).
_STOP = object()


class AmazonTranscribeStreamingASRAgent:
    """
    Real-time ASR + diarization over Amazon Transcribe's streaming API.

    Usage (matches the StreamingASRAgent protocol exactly):

        agent = AmazonTranscribeStreamingASRAgent(region="us-east-1")
        agent.start(on_result=my_handler, sample_rate=16000)
        agent.send_audio(pcm_bytes)   # called repeatedly as audio arrives
        ...
        agent.stop()

    `on_result` is called with a StreamingASRResult for every result event
    (partial and final) — session/streaming_manager.py only acts on final
    (`is_final=True`) results, same as every other StreamingASRAgent.

    Requires AWS credentials resolvable the usual way (environment,
    ~/.aws/credentials, an instance/task role — boto3-style resolution,
    same as agents/tts_polly.py) and the `amazon-transcribe` package
    installed. Imported lazily inside _session() so the mocked PoC path
    never needs it installed.
    """

    def __init__(self, region: str | None = None, language_code: str = "en-US",
                 show_speaker_label: bool = True):
        self.region = region or os.environ.get("AWS_REGION", "us-east-1")
        self.language_code = language_code
        self.show_speaker_label = show_speaker_label

        # start_stream_transcription()/send_audio_event() are async calls on
        # the awslabs SDK, but this class's own start()/send_audio()/stop()
        # are synchronous to match the StreamingASRAgent protocol every
        # other agent (including the mock and the AssemblyAI adapter)
        # follows. A dedicated background thread runs its own asyncio event
        # loop for the life of the session; send_audio() just enqueues bytes
        # onto a plain thread-safe queue.Queue, and a coroutine inside that
        # loop pulls from it via run_in_executor so the blocking queue.get()
        # never stalls the loop (same bridging idea as the AssemblyAI
        # adapter's queue+generator, adapted for asyncio instead of a
        # blocking client.stream() call).
        self._audio_queue: "queue.Queue[bytes | object]" = queue.Queue()
        self._thread: threading.Thread | None = None
        self._stream_error: Exception | None = None

    def start(self, on_result: Callable[[StreamingASRResult], None],
              sample_rate: int = 16000) -> None:
        ready = threading.Event()

        def _run():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(self._session(on_result, sample_rate, ready))
            except Exception as exc:  # noqa: BLE001 - surfaced via _stream_error
                self._stream_error = exc
                ready.set()
            finally:
                loop.close()

        self._thread = threading.Thread(target=_run, daemon=True)
        self._thread.start()
        # Block until the stream handshake with Transcribe has actually
        # completed (or failed) before returning, so a bad-credentials /
        # bad-region failure surfaces from start() itself rather than
        # silently on the first send_audio() call.
        ready.wait(timeout=10)
        if self._stream_error is not None:
            err, self._stream_error = self._stream_error, None
            raise err

    async def _session(self, on_result: Callable[[StreamingASRResult], None],
                        sample_rate: int, ready: threading.Event) -> None:
        from amazon_transcribe.client import TranscribeStreamingClient
        from amazon_transcribe.handlers import TranscriptResultStreamHandler
        from amazon_transcribe.model import TranscriptEvent

        client = TranscribeStreamingClient(region=self.region)
        stream = await client.start_stream_transcription(
            language_code=self.language_code,
            media_sample_rate_hz=sample_rate,
            media_encoding="pcm",
            show_speaker_label=self.show_speaker_label,
        )

        agent = self

        class _Handler(TranscriptResultStreamHandler):
            async def handle_transcript_event(self, transcript_event: TranscriptEvent):
                for result in transcript_event.transcript.results:
                    if not result.alternatives:
                        continue
                    alt = result.alternatives[0]
                    text = (alt.transcript or "").strip()
                    if not text:
                        continue
                    on_result(StreamingASRResult(
                        text=text,
                        is_final=not result.is_partial,
                        confidence=agent._alt_confidence(alt),
                        speaker_label=agent._majority_speaker(alt),
                        language=getattr(result, "language_code", None),
                    ))

        async def _writer():
            loop = asyncio.get_event_loop()
            while True:
                chunk = await loop.run_in_executor(None, self._audio_queue.get)
                if chunk is _STOP:
                    await stream.input_stream.end_stream()
                    return
                await stream.input_stream.send_audio_event(audio_chunk=chunk)

        # Handshake succeeded — let start() return to the caller. Everything
        # from here runs until stop() drains the queue and ends the stream.
        ready.set()
        handler = _Handler(stream.output_stream)
        await asyncio.gather(_writer(), handler.handle_events())

    @staticmethod
    def _alt_confidence(alt) -> float:
        # Transcribe reports confidence per-item (per word/punctuation
        # mark), not per-alternative — this averages across items that
        # actually carry a confidence score (punctuation marks don't)
        # rather than e.g. taking the min, since a single mis-heard filler
        # word shouldn't tank the whole utterance's reported confidence.
        # Falls back to a neutral 0.9 if Transcribe returns no per-item
        # scores at all (seen for very short/empty alternatives).
        items = getattr(alt, "items", None) or []
        scores = [i.confidence for i in items if getattr(i, "confidence", None) is not None]
        return sum(scores) / len(scores) if scores else 0.9

    @staticmethod
    def _majority_speaker(alt) -> str | None:
        # See the module docstring: diarization labels are per-word here,
        # not per-result, so this collapses a result down to the one
        # speaker label most of its words agree on.
        items = getattr(alt, "items", None) or []
        labels = [i.speaker for i in items if getattr(i, "speaker", None)]
        if not labels:
            return None
        return Counter(labels).most_common(1)[0][0]

    def send_audio(self, pcm_chunk: bytes) -> None:
        if self._thread is None:
            raise RuntimeError("call start() before send_audio()")
        if self._stream_error is not None:
            err, self._stream_error = self._stream_error, None
            raise err
        self._audio_queue.put(pcm_chunk)

    def stop(self) -> None:
        if self._thread is not None:
            self._audio_queue.put(_STOP)
            self._thread.join(timeout=5)
            self._thread = None
