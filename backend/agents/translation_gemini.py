"""
Context-aware translation agent using the Gemini API (google-genai SDK).

Same role as translation_anthropic.py's AnthropicTranslationAgent — see
that module's docstring and docs/vendor_decision.md §2 for why an LLM is
used here at all (context injection from recent turn history, FR-6).
This is a straight swap-in alternative selected via
VOXBUDDY_TRANSLATION_PROVIDER=gemini instead of =anthropic; nothing else
in the pipeline (session/manager.py, app.py) needs to know which one is
running, since both implement the same TranslationAgent protocol.

Requires the GEMINI_API_KEY environment variable. Not live-tested in this
build environment (no key available here) — the request/response shape
matches the documented google-genai SDK, and the parsing logic is covered
by tests/test_translation_gemini.py using a mocked client.
"""

from __future__ import annotations

import os

from .base import TranslationResult

# Gemini's model lineup/naming moves fast — override via GEMINI_MODEL if
# this default has been superseded by the time you're reading this. Check
# ai.google.dev/gemini-api/docs/models for the current recommended
# fast/cheap model for a latency-sensitive task like this one.
#
# Real bug caught in production: this used to default to
# "gemini-2.5-flash", which Google deprecated for newly-created API keys
# ahead of its official Oct 16, 2026 shutdown — every call returned a 404
# ("This model models/gemini-2.5-flash is no longer available to new
# users"), visible directly in the Gemini API dashboard's error graph.
# Because the translation call wasn't wrapped in a try/except at the time
# (see session/manager.py's _translate_and_record, since fixed), that 404
# silently killed every single turn with no error shown anywhere — the
# app just looked like it was permanently stuck listening. Updated to
# gemini-3.5-flash-lite: current-generation, GA, and explicitly Google's
# fastest/cheapest tier — the same "fast/cheap model for a
# latency-sensitive task" this default was always meant to be, just on a
# model ID that still exists. Bump to gemini-3.6-flash or newer via
# GEMINI_MODEL if translation quality ever matters more than raw latency.
DEFAULT_MODEL = "gemini-3.5-flash-lite"

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


class GeminiTranslationAgent:
    """Implements the TranslationAgent protocol from agents/base.py."""

    def __init__(self, api_key: str | None = None, model: str | None = None,
                 max_output_tokens: int = 200):
        self.api_key = api_key or os.environ.get("GEMINI_API_KEY")
        self.model = model or os.environ.get("GEMINI_MODEL", DEFAULT_MODEL)
        self.max_output_tokens = max_output_tokens
        self._client = None  # lazily constructed, same reasoning as the
        # Anthropic adapter — importing this module never requires the
        # google-genai package or a key to be present (e.g. for tests that
        # inject a fake client).

    def _get_client(self):
        if self._client is None:
            if not self.api_key:
                raise RuntimeError(
                    "GEMINI_API_KEY is not set. Set it in the environment "
                    "or pass api_key= explicitly."
                )
            from google import genai  # imported lazily, same reasoning as above
            self._client = genai.Client(api_key=self.api_key)
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

        from google.genai import types

        response = client.models.generate_content(
            model=self.model,
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                max_output_tokens=self.max_output_tokens,
            ),
        )

        translated_text = (response.text or "").strip()

        # Same simplification as the Anthropic adapter: Gemini doesn't
        # return a translation-quality confidence score either, so this
        # approximates one from finish_reason as a coarse signal. See
        # docs/vendor_decision.md §5 for what a real confidence signal
        # would need instead.
        finish_reason = None
        if getattr(response, "candidates", None):
            finish_reason = getattr(response.candidates[0], "finish_reason", None)
        finish_reason_str = str(finish_reason) if finish_reason is not None else ""
        confidence = 0.9 if "STOP" in finish_reason_str else 0.6

        return TranslationResult(text=translated_text, confidence=confidence)
