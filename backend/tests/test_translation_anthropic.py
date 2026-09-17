import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agents.translation_anthropic import AnthropicTranslationAgent


def make_fake_response(text: str, stop_reason: str = "end_turn"):
    text_block = SimpleNamespace(type="text", text=text)
    return SimpleNamespace(content=[text_block], stop_reason=stop_reason)


def test_translate_returns_clean_text_and_high_confidence_on_end_turn():
    agent = AnthropicTranslationAgent(api_key="fake-key-not-used")
    fake_client = MagicMock()
    fake_client.messages.create.return_value = make_fake_response("bonjour, comment allez-vous?")
    agent._client = fake_client  # inject mock, bypassing real network call

    result = agent.translate(
        text="hello, how are you?",
        source_lang="en",
        target_lang="fr",
        context=["previous turn one", "previous turn two"],
    )

    assert result.text == "bonjour, comment allez-vous?"
    assert result.confidence == 0.9
    fake_client.messages.create.assert_called_once()


def test_translate_lowers_confidence_on_truncated_response():
    agent = AnthropicTranslationAgent(api_key="fake-key-not-used")
    fake_client = MagicMock()
    fake_client.messages.create.return_value = make_fake_response("partial tra", stop_reason="max_tokens")
    agent._client = fake_client

    result = agent.translate("some long utterance", "en", "hi", context=[])
    assert result.confidence == 0.6


def test_context_is_included_in_prompt():
    agent = AnthropicTranslationAgent(api_key="fake-key-not-used")
    fake_client = MagicMock()
    fake_client.messages.create.return_value = make_fake_response("ok")
    agent._client = fake_client

    agent.translate("it is 200 rupees", "en", "hi", context=["how much is this?"])

    _, kwargs = fake_client.messages.create.call_args
    sent_prompt = kwargs["messages"][0]["content"]
    assert "how much is this?" in sent_prompt
    assert "it is 200 rupees" in sent_prompt


def test_raises_clear_error_without_api_key():
    agent = AnthropicTranslationAgent(api_key=None)
    agent.api_key = None  # ensure no env var leaked in during test run
    try:
        agent.translate("hi", "en", "fr", [])
        assert False, "expected RuntimeError"
    except RuntimeError as e:
        assert "ANTHROPIC_API_KEY" in str(e)
