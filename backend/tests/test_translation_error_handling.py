"""
Regression test for a real bug: SessionManager._translate_and_record()
called self.translation_agent.translate() with no try/except, unlike the
TTS call right below it (which was already guarded). A real vendor
failure there — this exact scenario played out with a deprecated Gemini
model ID returning a 404 in production, visible in the Gemini API
dashboard's error graph — propagated straight out of the pipeline with
nothing to catch it. For the real AssemblyAI-driven audio path, that
exception surfaces on the ASR vendor SDK's own background thread (see
asr_assemblyai.py), which typically swallows it inside its own internal
callback dispatch rather than letting it reach anywhere visible. The
practical symptom: ASR genuinely transcribes, then the whole turn just
vanishes — indistinguishable from "still listening" on the client.

Fixed by wrapping the translate() call the same way TTS already was,
attaching a `translation_error` field to PipelineResult instead of
raising.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from session.manager import IncomingUtterance, SessionManager


class FailingTranslationAgent:
    """Stands in for a real vendor call that raises — e.g. Gemini
    returning a 404 for a deprecated/invalid model ID, an expired key, or
    a network error. This is the exact shape of failure that used to
    silently kill a turn with nothing downstream ever finding out."""

    def translate(self, text, source_lang, target_lang, context):
        raise RuntimeError(
            "404 NOT_FOUND: This model models/gemini-2.5-flash is no "
            "longer available to new users."
        )


def make_established_session_with_failing_translation():
    session = SessionManager(translation_agent=FailingTranslationAgent())
    # First turn also goes through the failing agent — this just
    # establishes the speaker as a partner via CIE bootstrap so the
    # SECOND turn (the one under test) reaches _translate_and_record
    # rather than being filtered as bystander/unknown.
    session.handle_utterance(IncomingUtterance(
        speaker_label="shopkeeper", text="namaste", target_lang="en",
        turn_taking_score=0.9, semantic_coherence_score=0.9,
    ))
    return session


def test_translation_failure_is_caught_and_reported_not_raised():
    session = make_established_session_with_failing_translation()
    # Before the fix, this line itself would raise RuntimeError straight
    # out of handle_utterance() — this test's mere existence (not
    # crashing) is half the regression guard.
    result = session.handle_utterance(IncomingUtterance(
        speaker_label="shopkeeper", text="kitne ka hai", target_lang="en",
        turn_taking_score=0.9, semantic_coherence_score=0.9,
    ))

    assert result.translated_text is None
    assert result.translation_error is not None
    assert "404" in result.translation_error
    # A failed translation has nothing to synthesize speech from — TTS
    # should never even be attempted, let alone report its own error.
    assert result.tts_audio_b64 is None
    assert result.tts_error is None


def test_translation_failure_does_not_record_a_turn():
    """A failed turn shouldn't pollute conversation history/context used
    by later turns' translations — only genuinely successful turns
    should ever reach ConversationState.record_turn()."""
    session = make_established_session_with_failing_translation()
    before = len(session.state.recent_turns(10))

    session.handle_utterance(IncomingUtterance(
        speaker_label="shopkeeper", text="kitne ka hai", target_lang="en",
        turn_taking_score=0.9, semantic_coherence_score=0.9,
    ))

    after = len(session.state.recent_turns(10))
    assert after == before
