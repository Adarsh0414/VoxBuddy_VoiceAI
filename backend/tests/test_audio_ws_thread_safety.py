"""
Regression test for a real bug: on_pipeline_result used
asyncio.create_task(), which only works when called from the event
loop's own thread. MockStreamingASRAgent (used by every other websocket
test in this suite) calls its results back synchronously, inline, from
the same coroutine/thread as the WebSocket handler — so those tests
passed even with the broken version of the code. AssemblyAIStreamingASRAgent
delivers results from its own background thread instead (a requirement
of the separate client.stream() fix — see asr_assemblyai.py), and
asyncio.create_task() silently fails to schedule anything when called
from a foreign thread. The practical symptom: the ASR/CIE/translation/TTS
pipeline genuinely completes, but the result never reaches the client —
exactly the "just keeps listening forever" bug reported against a real
device.

This test uses a minimal fake StreamingASRAgent that — like the real
AssemblyAI adapter, unlike the mock — delivers its final result from an
actual background thread, so it can only pass if the fix
(asyncio.run_coroutine_threadsafe instead of asyncio.create_task) is
actually in place.
"""
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from fastapi.testclient import TestClient

from agents.base import StreamingASRResult


class ThreadedFakeASRAgent:
    """Delivers its final result from a real background thread — the
    same shape as AssemblyAIStreamingASRAgent, unlike MockStreamingASRAgent
    (which calls back synchronously, inline, from the caller's own
    thread)."""

    def start(self, on_result, sample_rate=16000):
        self._on_result = on_result

    def send_audio(self, pcm_chunk):
        if pcm_chunk == b"__TRIGGER_FINAL__":
            def fire():
                time.sleep(0.05)  # ensure this really runs after send_audio() returns
                self._on_result(StreamingASRResult(
                    text="hello from a background thread",
                    is_final=True,
                    confidence=0.95,
                    speaker_label="threaded_speaker",
                    language="en",
                ))
            threading.Thread(target=fire, daemon=True).start()

    def stop(self):
        pass


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("VOXBUDDY_ASR_PROVIDER", "mock")  # irrelevant, overridden below
    # Force mock translation/TTS regardless of any real .env keys present —
    # this test is only about the threading bug, a real network call
    # (blocked or slow in some environments) would make it hang/fail for
    # an unrelated reason.
    monkeypatch.setenv("VOXBUDDY_TRANSLATION_PROVIDER", "mock")
    monkeypatch.setenv("VOXBUDDY_TTS_PROVIDER", "mock")
    import app as app_module
    app_module.sessions.clear()
    # Accepts language_code (now passed by websocket_audio_session, see
    # app.py) and ignores it — this test is about the threading bug, not
    # ASR language selection.
    monkeypatch.setattr(app_module, "get_streaming_asr_agent",
                         lambda language_code=None: ThreadedFakeASRAgent())
    return TestClient(app_module.app)


def test_result_from_a_background_thread_reaches_the_client(client):
    with client.websocket_connect("/ws/threaded-session/audio") as ws:
        ws.send_bytes(b"__TRIGGER_FINAL__")
        # If on_pipeline_result still used asyncio.create_task(), this
        # would hang until the test's own receive timeout and fail —
        # nothing would ever actually be sent, because create_task()
        # silently does nothing useful when invoked from
        # ThreadedFakeASRAgent's background thread above.
        result = ws.receive_json()
        assert result["translated_text"] or result.get("original_text")
