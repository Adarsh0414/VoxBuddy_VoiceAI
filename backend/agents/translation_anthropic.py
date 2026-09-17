"""
Context-aware translation agent using the Anthropic Messages API.

Why an LLM here and not a dedicated NMT engine: see docs/vendor_decision.md
§2. In short — the CIE's ConversationTopic/TurnHistory (PRD §7.1) needs to
actually influence translation (FR-6), and no dedicated speech-translation
vendor found in the Phase 2 research exposes a comparable context-injection
mechanism. This adapter takes recent turn history as explicit context on
every call.

Requires the ANTHROPIC_API_KEY environment variable. Not live-tested in
this build environment (no key available here) — the request/response
shape matches the documented Messages API, and the parsing logic is
covered by tests/test_translation_anthropic.py using a mocked client.
"""

from __future__ import annotations

import os

from .base import TranslationResult

DEFAULT_MODEL = "claude-sonnet-4-6"

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


class AnthropicTranslationAgent:
    """Implements the TranslationAgent protocol from agents/base.py."""

    def __init__(self, api_key: str | None = None, model: str = DEFAULT_MODEL,
                 max_tokens: int = 200):
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        self.model = model
        self.max_tokens = max_tokens
        self._client = None  # lazily constructed so importing this module

        # never requires anthropic + a key to be present (e.g. for tests
        # that inject a fake client).

    def _get_client(self):
        if self._client is None:
            if not self.api_key:
                raise RuntimeError(
                    "ANTHROPIC_API_KEY is not set. Set it in the environment "
                    "or pass api_key= explicitly."
                )
            import anthropic  # imported lazily so the mock agents path
            # never needs this dependency installed.
            self._client = anthropic.Anthropic(api_key=self.api_key)
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

        response = client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )

        translated_text = "".join(
            block.text for block in response.content if getattr(block, "type", None) == "text"
        ).strip()

        # The Messages API doesn't return a confidence score; we approximate
        # one from the stop_reason as a coarse signal (a clean "end_turn"
        # stop is a mild positive signal vs. a truncated "max_tokens" stop).
        # This is a known simplification — see docs/vendor_decision.md §5
        # for what a production confidence signal would need instead
        # (e.g. a second self-consistency call, or a dedicated NMT engine's
        # native confidence score as a cross-check).
        confidence = 0.9 if getattr(response, "stop_reason", None) == "end_turn" else 0.6

        return TranslationResult(text=translated_text, confidence=confidence)
