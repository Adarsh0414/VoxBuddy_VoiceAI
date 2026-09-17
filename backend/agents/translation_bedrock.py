"""
Context-aware translation agent using Amazon Bedrock's Converse API.

Same role as translation_anthropic.py's AnthropicTranslationAgent and
translation_gemini.py's GeminiTranslationAgent — see
docs/vendor_decision.md §2 for why an LLM is used here at all (context
injection from recent turn history, FR-6). This is the AWS-native
translation option: VOXBUDDY_TRANSLATION_PROVIDER=bedrock, the default
provider (see agents/factory.py), so it's what runs with zero extra
config beyond the AWS credentials VoxBuddy's other AWS integrations
(agents/tts_polly.py, persistence_dynamodb.py) already need. Same
TranslationAgent protocol, same context-injection prompt design as the
other two adapters — a genuine drop-in, not a lesser option.

Uses the Bedrock Runtime `converse` API (not `invoke_model`) because it
gives a single, model-family-agnostic request/response shape — swapping
BEDROCK_MODEL_ID to a different model family (Anthropic, Amazon Nova,
Meta, etc.) needs no code change here, unlike invoke_model's
per-provider body schema.

Auth: boto3 picks up AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY / AWS_REGION
(or AWS_DEFAULT_REGION) from the environment automatically, same as
tts_polly.py — nothing VoxBuddy-specific to configure beyond those.

Requires IAM permission to invoke the configured model — see
docs/AWS_INTEGRATION.md for the exact policy statement
(`bedrock:InvokeModel` scoped to BEDROCK_MODEL_ID's resource ARN).
"""

from __future__ import annotations

import os

from .base import TranslationResult

# Anthropic's Claude models on Bedrock are a safe, widely-available
# default across regions/accounts. This app previously defaulted to
# "anthropic.claude-3-5-haiku-20241022-v1:0"; AWS retired that model
# version (Bedrock's Converse API now returns ResourceNotFoundException
# for it — "this model version has reached end of life"). Updated to the
# current model, which Bedrock only exposes through a *global*
# cross-region inference profile rather than a bare foundation-model id —
# that's the "global." prefix below, and it changes the IAM resource ARN
# shape too (see docs/AWS_INTEGRATION.md's IAM section: inference-profile
# ARNs, unlike foundation-model ARNs, include your AWS account id).
# Override via BEDROCK_MODEL_ID if your account prefers a different model
# (Amazon Nova, Meta Llama, etc. all work unchanged through the Converse
# API below) — check the Bedrock console's "Model access" page for what's
# actually enabled in your account/region before relying on this default.
DEFAULT_MODEL_ID = "global.anthropic.claude-sonnet-4-6"

SYSTEM_PROMPT = (
    "You are a real-time conversational speech translator embedded in a "
    "live two-person conversation. Translate the given utterance from the "
    "source language to the target language. Rules:\n"
    "- Output ONLY the translation, nothing else — no preamble, no quotes, "
    "no explanation.\n"
    "- Use the provided recent conversation turns only to resolve pronouns, "
    "ellipsis, and ambiguous terms consistently — do not translate them "
    "again.\n"
    "- Prefer natural spoken phrasing over literal word-for-word translation, "
    "but if you are uncertain about an idiom, prefer a safer, more literal "
    "rendering over a risky idiomatic guess (this is a live conversation, "
    "not a document — a wrong idiom is worse than a flat-but-correct one)."
)


class BedrockTranslationAgent:
    """Implements the TranslationAgent protocol from agents/base.py."""

    def __init__(self, region_name: str | None = None, model_id: str | None = None,
                 max_tokens: int = 200):
        self.region_name = region_name or os.environ.get("AWS_REGION") or os.environ.get(
            "AWS_DEFAULT_REGION", "us-east-1"
        )
        self.model_id = model_id or os.environ.get("BEDROCK_MODEL_ID", DEFAULT_MODEL_ID)
        self.max_tokens = max_tokens
        self._client = None  # lazily constructed, same reasoning as the
        # other translation adapters — importing this module never
        # requires boto3 or AWS credentials to be present (e.g. for tests
        # that inject a fake client).

    def _get_client(self):
        if self._client is None:
            import boto3  # lazy import — keeps boto3 optional unless bedrock is selected

            self._client = boto3.client("bedrock-runtime", region_name=self.region_name)
        return self._client

    def _build_prompt(self, text: str, source_lang: str, target_lang: str,
                       context: list[str]) -> str:
        context_block = ""
        if context:
            recent = "\n".join(f"- {t}" for t in context[-5:])
            context_block = f"\nRecent conversation turns (for context only):\n{recent}\n"
        return (
            f"Source language: {source_lang}\n"
            f"Target language: {target_lang}\n"
            f"{context_block}\n"
            f"Utterance to translate: {text}"
        )

    def translate(self, text: str, source_lang: str, target_lang: str,
                   context: list[str]) -> TranslationResult:
        client = self._get_client()
        prompt = self._build_prompt(text, source_lang, target_lang, context)

        response = client.converse(
            modelId=self.model_id,
            system=[{"text": SYSTEM_PROMPT}],
            messages=[{"role": "user", "content": [{"text": prompt}]}],
            inferenceConfig={"maxTokens": self.max_tokens},
        )

        output_message = response["output"]["message"]
        translated_text = "".join(
            block["text"] for block in output_message["content"] if "text" in block
        ).strip()

        # The Converse API doesn't return a translation-quality confidence
        # score either — same simplification as the Anthropic/Gemini
        # adapters, approximating one from stopReason as a coarse signal.
        # See docs/vendor_decision.md §5 for what a real confidence signal
        # would need instead.
        stop_reason = response.get("stopReason")
        confidence = 0.9 if stop_reason == "end_turn" else 0.6

        return TranslationResult(text=translated_text, confidence=confidence)
