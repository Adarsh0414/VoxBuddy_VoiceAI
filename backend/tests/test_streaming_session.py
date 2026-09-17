import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agents.mock_streaming_asr import END_OF_TURN, MockStreamingASRAgent
from session.manager import SessionManager
from session.streaming_manager import StreamingSessionAdapter


def make_adapter(signal_provider=None, speaker_label="shopkeeper"):
    session = SessionManager()
    asr = MockStreamingASRAgent(speaker_label=speaker_label)
    adapter = StreamingSessionAdapter(session, asr, target_lang="en",
                                       signal_provider=signal_provider)
    adapter.start()
    return adapter, session


def test_partial_results_do_not_trigger_the_pipeline():
    adapter, session = make_adapter()
    adapter.push_audio("namaste")
    adapter.push_audio("kitne")
    adapter.push_audio("ka")
    # No END_OF_TURN sent yet — all partials, nothing should have reached
    # the CIE/translation pipeline.
    assert adapter.results == []
    assert len(session.state.turn_history) == 0


def test_final_result_triggers_full_pipeline_and_translates():
    # High turn-taking/coherence -> establishes as partner and translates.
    adapter, session = make_adapter(signal_provider=lambda r: (0.9, 0.9))
    for token in ["namaste", "kitne", "ka", "hai", "yeh"]:
        adapter.push_audio(token)
    adapter.push_audio(END_OF_TURN)

    assert len(adapter.results) == 1
    result = adapter.results[0]
    assert result.decision.role.value == "partner"
    assert result.translated_text == "[en] namaste kitne ka hai yeh"
    assert len(session.state.turn_history) == 1
    assert session.state.turn_history[0].source_text == "namaste kitne ka hai yeh"


def test_bystander_signal_is_filtered_even_after_a_final_result():
    # Low turn-taking/coherence -> CIE should reject as bystander, and the
    # streaming adapter should record a result with no translation.
    adapter, session = make_adapter(signal_provider=lambda r: (0.1, 0.05))
    adapter.push_audio("aloo")
    adapter.push_audio("le")
    adapter.push_audio("lo")
    adapter.push_audio(END_OF_TURN)

    assert len(adapter.results) == 1
    result = adapter.results[0]
    assert result.decision.role.value == "bystander"
    assert result.translated_text is None
    assert len(session.state.turn_history) == 0


def test_multiple_turns_accumulate_correctly():
    adapter, session = make_adapter(signal_provider=lambda r: (0.9, 0.9))

    adapter.push_audio("namaste")
    adapter.push_audio(END_OF_TURN)
    adapter.push_audio("kaise")
    adapter.push_audio("ho")
    adapter.push_audio(END_OF_TURN)

    assert len(adapter.results) == 2
    assert adapter.results[0].turn.source_text == "namaste"
    assert adapter.results[1].turn.source_text == "kaise ho"
    assert len(session.state.turn_history) == 2


def test_on_pipeline_result_callback_is_invoked_per_final_turn():
    seen = []
    session = SessionManager()
    asr = MockStreamingASRAgent(speaker_label="shopkeeper")
    adapter = StreamingSessionAdapter(
        session, asr, target_lang="en",
        signal_provider=lambda r: (0.9, 0.9),
        on_pipeline_result=lambda result: seen.append(result),
    )
    adapter.start()
    adapter.push_audio("namaste")
    adapter.push_audio(END_OF_TURN)

    assert len(seen) == 1
    assert seen[0] is adapter.results[0]


def test_on_final_asr_result_fires_even_for_empty_final_turn():
    """Distinct from on_pipeline_result: this hook is for connection-
    lifecycle purposes (see app.py's idle-timeout guard) and must fire on
    every final ASR event -- including an empty silence-flush one that
    never reaches the pipeline -- since "the ASR agent is still alive and
    reporting" is the signal being tracked, not "a real turn happened"."""
    seen = []
    session = SessionManager()
    asr = MockStreamingASRAgent(speaker_label="shopkeeper")
    adapter = StreamingSessionAdapter(
        session, asr, target_lang="en",
        on_final_asr_result=lambda result: seen.append(result),
    )
    adapter.start()

    adapter.push_audio(END_OF_TURN)  # nothing buffered -> empty final text
    assert len(seen) == 1
    assert seen[0].is_final is True
    assert adapter.results == []  # confirms it never reached the pipeline

    adapter.push_audio("namaste")
    assert len(seen) == 1  # partials must NOT trigger this hook
    adapter.push_audio(END_OF_TURN)
    assert len(seen) == 2


def test_empty_final_turn_is_ignored():
    """A pure-silence flush (empty text) shouldn't reach the pipeline at
    all — not even as a bystander decision."""
    adapter, session = make_adapter(signal_provider=lambda r: (0.9, 0.9))
    adapter.push_audio(END_OF_TURN)  # nothing buffered -> empty final text
    assert adapter.results == []


def test_streaming_and_batch_paths_share_the_same_cie_state():
    """A real deployment might mix a streaming ASR turn with, e.g., a
    fallback batch utterance in the same session — both paths write into
    the same ConversationState/CIE, so partner tracking stays consistent
    regardless of which ASR path produced the text."""
    from session.manager import IncomingUtterance

    session = SessionManager()
    asr = MockStreamingASRAgent(speaker_label="shopkeeper")
    adapter = StreamingSessionAdapter(session, asr, target_lang="en",
                                       signal_provider=lambda r: (0.9, 0.9))
    adapter.start()
    adapter.push_audio("namaste")
    adapter.push_audio(END_OF_TURN)
    assert len(session.state.active_partner_ids) == 1

    # Same session, same speaker label, now via the batch/mocked-ASR path.
    batch_result = session.handle_utterance(IncomingUtterance(
        speaker_label="shopkeeper", text="aap kahan se ho?", target_lang="en",
        turn_taking_score=0.9, semantic_coherence_score=0.9,
    ))
    assert batch_result.decision.notes == "reinforced existing group member"
    assert len(session.state.turn_history) == 2
