import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agents.translation_bedrock import BedrockTranslationAgent


def make_fake_response(text: str, stop_reason: str = "end_turn"):
    return {
        "output": {"message": {"content": [{"text": text}]}},
        "stopReason": stop_reason,
    }


def test_translate_returns_clean_text_and_high_confidence_on_end_turn():
    agent = BedrockTranslationAgent(region_name="us-east-1")
    fake_client = MagicMock()
    fake_client.converse.return_value = make_fake_response("bonjour, comment allez-vous?")
    agent._client = fake_client  # inject mock, bypassing real network call

    result = agent.translate(
        text="hello, how are you?",
        source_lang="en",
        target_lang="fr",
        context=["previous turn one", "previous turn two"],
    )

    assert result.text == "bonjour, comment allez-vous?"
    assert result.confidence == 0.9
    fake_client.converse.assert_called_once()


def test_translate_lowers_confidence_on_truncated_response():
    agent = BedrockTranslationAgent(region_name="us-east-1")
    fake_client = MagicMock()
    fake_client.converse.return_value = make_fake_response("partial tra", stop_reason="max_tokens")
    agent._client = fake_client

    result = agent.translate("some long utterance", "en", "hi", context=[])
    assert result.confidence == 0.6


def test_context_is_included_in_prompt():
    agent = BedrockTranslationAgent(region_name="us-east-1")
    fake_client = MagicMock()
    fake_client.converse.return_value = make_fake_response("ok")
    agent._client = fake_client

    agent.translate("it is 200 rupees", "en", "hi", context=["how much is this?"])

    _, kwargs = fake_client.converse.call_args
    sent_prompt = kwargs["messages"][0]["content"][0]["text"]
    assert "how much is this?" in sent_prompt
    assert "it is 200 rupees" in sent_prompt


def test_uses_configured_model_id():
    agent = BedrockTranslationAgent(region_name="us-east-1", model_id="amazon.nova-lite-v1:0")
    fake_client = MagicMock()
    fake_client.converse.return_value = make_fake_response("ok")
    agent._client = fake_client

    agent.translate("hi", "en", "fr", [])

    _, kwargs = fake_client.converse.call_args
    assert kwargs["modelId"] == "amazon.nova-lite-v1:0"


def test_default_model_id_is_current_sonnet_global_inference_profile(monkeypatch):
    """Regression guard: anthropic.claude-3-5-haiku-20241022-v1:0 (the
    previous default) was retired by AWS — Converse now returns
    ResourceNotFoundException for it. The default must be a model that is
    actually invocable, and reads as the global cross-region inference
    profile id (not a bare foundation-model id) that this model family
    requires."""
    monkeypatch.delenv("BEDROCK_MODEL_ID", raising=False)
    agent = BedrockTranslationAgent()
    assert agent.model_id == "global.anthropic.claude-sonnet-4-6"


def test_bedrock_model_id_env_var_overrides_default(monkeypatch):
    monkeypatch.setenv("BEDROCK_MODEL_ID", "global.anthropic.claude-sonnet-4-6")
    agent = BedrockTranslationAgent()
    assert agent.model_id == "global.anthropic.claude-sonnet-4-6"


def test_arbitrary_model_id_passed_through_unmodified_to_converse():
    """The Converse API's modelId accepts a bare foundation-model id, an
    inference-profile id, or a full ARN — this adapter must not
    reformat/validate it, just pass whatever BEDROCK_MODEL_ID resolves to
    straight through, so a global inference profile id works with zero
    code changes here."""
    agent = BedrockTranslationAgent(
        region_name="us-east-1",
        model_id="global.anthropic.claude-sonnet-4-6",
    )
    fake_client = MagicMock()
    fake_client.converse.return_value = make_fake_response("ok")
    agent._client = fake_client

    agent.translate("hi", "en", "fr", [])

    _, kwargs = fake_client.converse.call_args
    assert kwargs["modelId"] == "global.anthropic.claude-sonnet-4-6"


def test_defaults_region_to_us_east_1_when_unset(monkeypatch):
    monkeypatch.delenv("AWS_REGION", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
    agent = BedrockTranslationAgent()
    assert agent.region_name == "us-east-1"


def test_factory_selects_bedrock_provider(monkeypatch):
    monkeypatch.setenv("VOXBUDDY_TRANSLATION_PROVIDER", "bedrock")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "fake-access-key")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "fake-secret-key")
    from agents.factory import get_translation_agent
    agent = get_translation_agent()
    assert isinstance(agent, BedrockTranslationAgent)


def test_factory_defaults_to_bedrock_when_unset(monkeypatch):
    monkeypatch.delenv("VOXBUDDY_TRANSLATION_PROVIDER", raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "fake-access-key")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "fake-secret-key")
    from agents.factory import get_translation_agent
    agent = get_translation_agent()
    assert isinstance(agent, BedrockTranslationAgent)


def test_factory_requires_aws_credentials_for_bedrock(monkeypatch):
    monkeypatch.setenv("VOXBUDDY_TRANSLATION_PROVIDER", "bedrock")
    monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("AWS_SECRET_ACCESS_KEY", raising=False)
    from agents.factory import get_translation_agent
    try:
        get_translation_agent()
        assert False, "expected RuntimeError"
    except RuntimeError as e:
        assert "AWS_ACCESS_KEY_ID" in str(e)


def test_factory_still_selects_gemini_when_explicitly_configured(monkeypatch):
    """Bedrock becoming the default must not break explicit opt-in to
    Gemini for users who want to use their own Gemini API key."""
    monkeypatch.setenv("VOXBUDDY_TRANSLATION_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-factory-test")
    from agents.factory import get_translation_agent
    from agents.translation_gemini import GeminiTranslationAgent
    agent = get_translation_agent()
    assert isinstance(agent, GeminiTranslationAgent)


def test_factory_does_not_fall_back_to_gemini_on_bedrock_misconfiguration(monkeypatch):
    """Provider selection stays explicit: a missing Bedrock credential
    must raise, never silently switch to Gemini."""
    monkeypatch.setenv("VOXBUDDY_TRANSLATION_PROVIDER", "bedrock")
    monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("AWS_SECRET_ACCESS_KEY", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-should-not-be-used")
    from agents.factory import get_translation_agent
    from agents.translation_gemini import GeminiTranslationAgent
    try:
        agent = get_translation_agent()
        assert not isinstance(agent, GeminiTranslationAgent)
        assert False, "expected RuntimeError, got an agent instead"
    except RuntimeError:
        pass
