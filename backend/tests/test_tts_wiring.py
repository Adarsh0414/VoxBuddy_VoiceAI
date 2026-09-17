import base64
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from agents.mocks import MockTTSAgent
from session.manager import IncomingUtterance, SessionManager


def make_established_session(tts_agent=None, tts_audio_format=None):
    """A SessionManager with a partner already established, so the next
    utterance goes all the way through translate + TTS instead of being
    filtered as a bystander/unknown speaker."""
    session = SessionManager(tts_agent=tts_agent, tts_audio_format=tts_audio_format)
    session.handle_utterance(IncomingUtterance(
        speaker_label="shopkeeper", text="namaste", target_lang="en",
        turn_taking_score=0.9, semantic_coherence_score=0.9,
    ))
    return session


def test_mock_tts_audio_is_attached_to_pipeline_result():
    session = make_established_session()
    result = session.handle_utterance(IncomingUtterance(
        speaker_label="shopkeeper", text="kitne ka hai", target_lang="en",
        turn_taking_score=0.9, semantic_coherence_score=0.9,
    ))
    assert result.tts_audio_b64 is not None
    assert result.tts_audio_format == "mock-text"
    assert result.tts_error is None
    # Round-trips back to the exact bytes MockTTSAgent produced (the
    # translated text, UTF-8 encoded — not real audio, but real wiring).
    decoded = base64.b64decode(result.tts_audio_b64)
    assert decoded == result.translated_text.encode()


def test_bystander_utterances_have_no_tts_audio():
    session = SessionManager()
    result = session.handle_utterance(IncomingUtterance(
        speaker_label="random_stranger", text="hello", target_lang="en",
        turn_taking_score=0.1, semantic_coherence_score=0.1,
    ))
    assert result.turn is None
    assert result.tts_audio_b64 is None


def test_tts_vendor_failure_is_caught_and_reported_not_raised():
    class BrokenTTSAgent:
        def synthesize(self, text, target_lang):
            raise RuntimeError("No real ElevenLabs voice_id configured for target_lang='en'")

    session = make_established_session(tts_agent=BrokenTTSAgent(), tts_audio_format="mp3")
    result = session.handle_utterance(IncomingUtterance(
        speaker_label="shopkeeper", text="kitne ka hai", target_lang="en",
        turn_taking_score=0.9, semantic_coherence_score=0.9,
    ))
    # Translation still succeeded even though TTS blew up.
    assert result.translated_text is not None
    assert result.turn is not None
    assert result.tts_audio_b64 is None
    assert "voice_id" in result.tts_error


def test_app_ws_response_includes_tts_fields(monkeypatch):
    monkeypatch.setenv("VOXBUDDY_ASR_PROVIDER", "mock")
    import app as app_module
    app_module.sessions.clear()
    from fastapi.testclient import TestClient

    client = TestClient(app_module.app)
    res = client.post("/api/session")
    session_id = res.json()["session_id"]
    client.post(f"/api/session/{session_id}/enroll_self", json={"speaker_label": "self"})

    with client.websocket_connect(f"/ws/{session_id}") as ws:
        ws.send_json({
            "speaker_label": "shopkeeper", "text": "namaste", "target_lang": "en",
            "turn_taking_score": 0.9, "semantic_coherence_score": 0.9,
        })
        ws.receive_json()
        ws.send_json({
            "speaker_label": "shopkeeper", "text": "kitne ka hai", "target_lang": "en",
            "turn_taking_score": 0.9, "semantic_coherence_score": 0.9,
        })
        result = ws.receive_json()

    assert result["tts_audio_b64"] is not None
    assert result["tts_audio_format"] == "mock-text"


def test_app_ws_response_includes_signal_breakdown(monkeypatch):
    """The v1.2 signal-breakdown fields (voice/turn/semantic readings,
    weights, thresholds, noise profile) need to survive the full round trip
    from CIEDecision -> PipelineResult -> UtteranceOut -> WS JSON, since
    that's what the CIE Live Cockpit frontend renders live."""
    monkeypatch.setenv("VOXBUDDY_ASR_PROVIDER", "mock")
    import app as app_module
    app_module.sessions.clear()
    from fastapi.testclient import TestClient

    client = TestClient(app_module.app)
    res = client.post("/api/session")
    session_id = res.json()["session_id"]

    with client.websocket_connect(f"/ws/{session_id}") as ws:
        ws.send_json({
            "speaker_label": "shopkeeper", "text": "namaste", "target_lang": "en",
            "turn_taking_score": 0.9, "semantic_coherence_score": 0.85,
        })
        result = ws.receive_json()

    assert result["turn_taking_score"] == 0.9
    assert result["semantic_coherence_score"] == 0.85
    assert result["voice_similarity"] == 0.5  # neutral prior for a brand-new candidate
    assert result["weight_voice"] == 0.45
    assert result["weight_turn"] == 0.25
    assert result["weight_semantic"] == 0.30
    assert result["bystander_threshold"] == 0.35
    assert result["lock_threshold"] == 0.62
    assert result["noise_profile"] == "clean"
