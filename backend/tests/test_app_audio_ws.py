import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("VOXBUDDY_ASR_PROVIDER", "mock")
    import app as app_module
    app_module.sessions.clear()
    return TestClient(app_module.app)


def test_audio_endpoint_accepts_connection_and_creates_session(client):
    with client.websocket_connect("/ws/some-audio-session/audio") as ws:
        # A single frame that happens to be valid UTF-8 text (this is what
        # MockStreamingASRAgent can actually handle — real PCM bytes are
        # covered by the decode-error test below).
        ws.send_bytes(b"namaste")
    import app as app_module
    assert "some-audio-session" in app_module.sessions


def test_audio_endpoint_final_turn_reaches_pipeline(client):
    from agents.mock_streaming_asr import END_OF_TURN

    with client.websocket_connect("/ws/turn-session/audio") as ws:
        # Partial results (adapter._handle_asr_result) don't reach the
        # pipeline or send anything back — only a final (END_OF_TURN) does.
        ws.send_bytes(b"namaste")
        ws.send_bytes(END_OF_TURN.encode("utf-8"))
        final_result = ws.receive_json()
        assert final_result["confidence"] is not None

    import app as app_module
    session = app_module.sessions["turn-session"]
    assert len(session.state.turn_history) == 1


def test_audio_endpoint_reports_real_pcm_incompatible_with_mock(client):
    # Real 16-bit PCM audio is not valid UTF-8 in general — this specific
    # 2-byte frame (0xFF 0xFE) is guaranteed to fail decode(), which is
    # exactly what happens if you point real microphone audio at the mock
    # provider instead of VOXBUDDY_ASR_PROVIDER=assemblyai.
    with client.websocket_connect("/ws/real-audio-session/audio") as ws:
        ws.send_bytes(bytes([0xFF, 0xFE, 0x00, 0x01]))
        result = ws.receive_json()
        assert "error" in result
        assert "assemblyai" in result["error"].lower()


def test_audio_endpoint_without_assemblyai_key_returns_clear_error(client, monkeypatch):
    monkeypatch.setenv("VOXBUDDY_ASR_PROVIDER", "assemblyai")
    monkeypatch.delenv("ASSEMBLYAI_API_KEY", raising=False)
    with client.websocket_connect("/ws/no-key-session/audio") as ws:
        result = ws.receive_json()
        assert "ASSEMBLYAI_API_KEY" in result["error"]


def test_audio_endpoint_translates_into_the_query_param_target_lang(client, monkeypatch):
    """Real bug caught in production: target_lang was hardcoded to "en" in
    this endpoint regardless of what the user actually selected as "You
    speak" during setup — a user with Hindi selected heard English spoken
    back, confirmed on a real device. Fixed by accepting target_lang as a
    query param (populated by the frontend from the logged-in user's own
    preferred_language) instead of a hardcoded default. MockTranslationAgent
    conveniently tags its output with the target_lang it was given
    (`f"[{target_lang}] {text}"`), which is what makes this directly
    assertable without a real translation vendor."""
    monkeypatch.setenv("VOXBUDDY_TRANSLATION_PROVIDER", "mock")
    monkeypatch.setenv("VOXBUDDY_TTS_PROVIDER", "mock")
    from agents.mock_streaming_asr import END_OF_TURN

    with client.websocket_connect("/ws/hindi-session/audio?target_lang=hi") as ws:
        ws.send_bytes(b"namaste")
        ws.send_bytes(END_OF_TURN.encode("utf-8"))
        result = ws.receive_json()

    assert result["translated_text"].startswith("[hi]")


def test_audio_endpoint_maps_source_lang_to_aws_language_code(client, monkeypatch):
    """source_lang (the ASR/spoken language) is a separate query param from
    target_lang (the translation destination) and gets mapped through
    app.ASR_SOURCE_LANGUAGE_CODES to the region-qualified code AWS
    Transcribe expects, then handed to get_streaming_asr_agent -- NOT
    derived from target_lang. Captures what app.py actually passed instead
    of exercising a real AWS connection."""
    import app as app_module
    from agents.mock_streaming_asr import MockStreamingASRAgent, END_OF_TURN

    captured = {}

    def fake_get_streaming_asr_agent(language_code=None):
        captured["language_code"] = language_code
        return MockStreamingASRAgent()

    monkeypatch.setattr(app_module, "get_streaming_asr_agent", fake_get_streaming_asr_agent)
    monkeypatch.setenv("VOXBUDDY_TRANSLATION_PROVIDER", "mock")
    monkeypatch.setenv("VOXBUDDY_TTS_PROVIDER", "mock")

    # source = Hindi, target = English -- must not collapse into ASR=en.
    with client.websocket_connect(
        "/ws/source-lang-session/audio?source_lang=hi&target_lang=en"
    ) as ws:
        ws.send_bytes(b"namaste")
        ws.send_bytes(END_OF_TURN.encode("utf-8"))
        ws.receive_json()
    assert captured["language_code"] == "hi-IN"

    # source = English, target = Hindi -- must not collapse into ASR=hi-IN.
    with client.websocket_connect(
        "/ws/source-lang-session-2/audio?source_lang=en&target_lang=hi"
    ) as ws:
        ws.send_bytes(b"namaste")
        ws.send_bytes(END_OF_TURN.encode("utf-8"))
        ws.receive_json()
    assert captured["language_code"] == "en-US"


def test_audio_endpoint_defaults_source_lang_to_en_US_when_omitted(client, monkeypatch):
    """No source_lang supplied -> sensible en-US fallback, matching
    AmazonTranscribeStreamingASRAgent's own default, so existing
    functionality doesn't unexpectedly break."""
    import app as app_module
    from agents.mock_streaming_asr import MockStreamingASRAgent, END_OF_TURN

    captured = {}

    def fake_get_streaming_asr_agent(language_code=None):
        captured["language_code"] = language_code
        return MockStreamingASRAgent()

    monkeypatch.setattr(app_module, "get_streaming_asr_agent", fake_get_streaming_asr_agent)
    monkeypatch.setenv("VOXBUDDY_TRANSLATION_PROVIDER", "mock")
    monkeypatch.setenv("VOXBUDDY_TTS_PROVIDER", "mock")

    with client.websocket_connect("/ws/no-source-lang-session/audio") as ws:
        ws.send_bytes(b"namaste")
        ws.send_bytes(END_OF_TURN.encode("utf-8"))
        ws.receive_json()
    assert captured["language_code"] == "en-US"


def test_audio_endpoint_idle_timeout_closes_session_after_inactivity(client, monkeypatch):
    """Core safety net: with no final turns at all, the connection must be
    closed automatically once VOXBUDDY_WS_IDLE_TIMEOUT_SECONDS elapses,
    with a message the frontend can tell apart from a normal pipeline
    result or the {"error": ...} branch."""
    monkeypatch.setenv("VOXBUDDY_WS_IDLE_TIMEOUT_SECONDS", "0.2")
    monkeypatch.setenv("VOXBUDDY_WS_MAX_SESSION_SECONDS", "60")

    with client.websocket_connect("/ws/idle-timeout-session/audio") as ws:
        # No frames sent at all -- just silence from the client's POV.
        result = ws.receive_json()
        assert result["idle_timeout"] is True
        assert "reason" in result
        assert "error" not in result
        # The server closes its end after sending this -- confirm the
        # client sees the socket actually go away rather than staying open.
        with pytest.raises(Exception):
            ws.receive_json()


def test_audio_endpoint_idle_timeout_does_not_fire_during_active_conversation(client, monkeypatch):
    """Periodic final turns spaced closer together than the idle timeout
    must keep resetting the clock -- a real, ongoing conversation with
    normal pauses between turns must not get closed early."""
    monkeypatch.setenv("VOXBUDDY_WS_IDLE_TIMEOUT_SECONDS", "0.3")
    monkeypatch.setenv("VOXBUDDY_WS_MAX_SESSION_SECONDS", "60")
    from agents.mock_streaming_asr import END_OF_TURN

    with client.websocket_connect("/ws/active-conversation-session/audio") as ws:
        for _ in range(4):
            time.sleep(0.15)  # comfortably less than the 0.3s idle timeout
            ws.send_bytes(b"namaste")
            ws.send_bytes(END_OF_TURN.encode("utf-8"))
            result = ws.receive_json()
            # A real pipeline result each time, never the idle-timeout message.
            assert "idle_timeout" not in result
            assert result["confidence"] is not None


def test_audio_endpoint_hard_session_cap_fires_even_with_ongoing_activity(client, monkeypatch):
    """Belt-and-suspenders bound: even a session that keeps producing real
    turns (so the idle timer never trips) must still be closed once total
    connection duration exceeds VOXBUDDY_WS_MAX_SESSION_SECONDS."""
    monkeypatch.setenv("VOXBUDDY_WS_IDLE_TIMEOUT_SECONDS", "60")
    monkeypatch.setenv("VOXBUDDY_WS_MAX_SESSION_SECONDS", "0.3")
    from agents.mock_streaming_asr import END_OF_TURN

    with client.websocket_connect("/ws/hard-cap-session/audio") as ws:
        got_idle_timeout = False
        for _ in range(10):
            time.sleep(0.05)  # well under the 60s idle timeout each turn
            ws.send_bytes(b"namaste")
            ws.send_bytes(END_OF_TURN.encode("utf-8"))
            result = ws.receive_json()
            if result.get("idle_timeout"):
                got_idle_timeout = True
                assert "maximum duration" in result["reason"]
                break
        assert got_idle_timeout, "hard session cap never fired despite ongoing activity"


def test_audio_endpoint_short_normal_session_unaffected_by_timeouts(client):
    """Sanity check with the real default env values (no monkeypatch): a
    normal, short test session must behave exactly as before -- this is
    effectively test_audio_endpoint_final_turn_reaches_pipeline again,
    guarding against the new guard changing existing behavior."""
    from agents.mock_streaming_asr import END_OF_TURN

    with client.websocket_connect("/ws/short-session/audio") as ws:
        ws.send_bytes(b"namaste")
        ws.send_bytes(END_OF_TURN.encode("utf-8"))
        result = ws.receive_json()
        assert "idle_timeout" not in result
        assert result["confidence"] is not None


def test_audio_endpoint_defaults_to_english_when_target_lang_omitted(client, monkeypatch):
    """The fallback still exists (the query param is optional) — this just
    confirms omitting it doesn't error, and defaults sanely rather than
    breaking the connection."""
    monkeypatch.setenv("VOXBUDDY_TRANSLATION_PROVIDER", "mock")
    monkeypatch.setenv("VOXBUDDY_TTS_PROVIDER", "mock")
    from agents.mock_streaming_asr import END_OF_TURN

    with client.websocket_connect("/ws/default-lang-session/audio") as ws:
        ws.send_bytes(b"namaste")
        ws.send_bytes(END_OF_TURN.encode("utf-8"))
        result = ws.receive_json()

    assert result["translated_text"].startswith("[en]")
