import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agents.translation_gemini import GeminiTranslationAgent


def make_fake_response(text: str, finish_reason: str = "STOP"):
    candidate = SimpleNamespace(finish_reason=finish_reason)
    return SimpleNamespace(text=text, candidates=[candidate])


def test_translate_returns_clean_text_and_high_confidence_on_stop():
    agent = GeminiTranslationAgent(api_key="fake-key-not-used")
    fake_client = MagicMock()
    fake_client.models.generate_content.return_value = make_fake_response("bonjour, comment allez-vous?")
    agent._client = fake_client  # inject mock, bypassing real network call

    result = agent.translate(
        text="hello, how are you?",
        source_lang="en",
        target_lang="fr",
        context=["previous turn one", "previous turn two"],
    )

    assert result.text == "bonjour, comment allez-vous?"
    assert result.confidence == 0.9
    fake_client.models.generate_content.assert_called_once()


def test_translate_lowers_confidence_on_truncated_response():
    agent = GeminiTranslationAgent(api_key="fake-key-not-used")
    fake_client = MagicMock()
    fake_client.models.generate_content.return_value = make_fake_response("partial tra", finish_reason="MAX_TOKENS")
    agent._client = fake_client

    result = agent.translate("some long utterance", "en", "hi", context=[])
    assert result.confidence == 0.6


def test_context_is_included_in_prompt():
    agent = GeminiTranslationAgent(api_key="fake-key-not-used")
    fake_client = MagicMock()
    fake_client.models.generate_content.return_value = make_fake_response("ok")
    agent._client = fake_client

    agent.translate("it is 200 rupees", "en", "hi", context=["how much is this?"])

    _, kwargs = fake_client.models.generate_content.call_args
    sent_prompt = kwargs["contents"]
    assert "how much is this?" in sent_prompt
    assert "it is 200 rupees" in sent_prompt


def test_raises_clear_error_without_api_key():
    agent = GeminiTranslationAgent(api_key=None)
    agent.api_key = None  # ensure no env var leaked in during test run
    try:
        agent.translate("hi", "en", "fr", [])
        assert False, "expected RuntimeError"
    except RuntimeError as e:
        assert "GEMINI_API_KEY" in str(e)


def test_factory_selects_gemini_provider(monkeypatch):
    monkeypatch.setenv("VOXBUDDY_TRANSLATION_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-factory-test")
    from agents.factory import get_translation_agent
    agent = get_translation_agent()
    assert isinstance(agent, GeminiTranslationAgent)


def test_factory_requires_gemini_key(monkeypatch):
    monkeypatch.setenv("VOXBUDDY_TRANSLATION_PROVIDER", "gemini")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    from agents.factory import get_translation_agent
    try:
        get_translation_agent()
        assert False, "expected RuntimeError"
    except RuntimeError as e:
        assert "GEMINI_API_KEY" in str(e)
