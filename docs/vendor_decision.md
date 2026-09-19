# VoxBuddy — Phase 2 Vendor Research & Decision (ASR / Translation / TTS / Diarization)
*Research date: September 2026. Prices and latency figures are vendor-published or third-party benchmark figures as of mid-2026 and should be re-verified at contract time — this market moves fast.*

## Current Hackathon Architecture

This document is primarily a **historical vendor research/decision log**
from earlier in the project (see sections 1–5 below). The pipeline it
evaluates and the pipeline actually deployed for the hackathon have since
diverged in places. What the **current hackathon deployment** actually
runs:

- **ASR:** AWS Transcribe (`VOXBUDDY_ASR_PROVIDER=aws_transcribe`)
- **Conversation Intelligence Engine (CIE):** original logic, unchanged since this doc was written
- **Translation:** Google Gemini (`VOXBUDDY_TRANSLATION_PROVIDER=gemini`)
- **TTS:** Amazon Polly
- **Persistence:** Amazon DynamoDB
- **Deployment platform:** AWS Elastic Beanstalk / EC2
- **IAM:** a scoped policy covering Polly, Transcribe, and DynamoDB (see `README.md` §6.5)

See `README.md` §6 and §19, and `docs/AWS_INTEGRATION.md` /
`docs/AWS_DEPLOYMENT.md`, for the full, current write-up of this pipeline.

## Alternative / Historical Providers

The vendors and platforms researched or previously used below —
**AssemblyAI**, **ElevenLabs**, **Anthropic** (direct API), **Amazon
Bedrock** (translation), and **Render** (deployment) — are retained in
the codebase as alternative/optional providers or documented here as
historical development choices; they are not part of the current
hackathon deployment listed above. Bedrock specifically is fully
implemented and was the code's intended default, but is currently
blocked at the AWS account level rather than by choice — see `README.md`
§6.7 for the full story.

---

## 0. The decision that shapes everything else: cascaded vs. end-to-end

2026's speech-translation market splits into two architectures:

- **End-to-end speech-to-speech** (OpenAI's `gpt-realtime-translate`, Palabra.ai, Soniox's speech-translation endpoint): one API call, audio in, translated audio out. Lowest integration effort, competitive latency.
- **Cascaded** (separate ASR → Translation → TTS stages): more integration work, but each stage is independently inspectable.

**This matters enormously for VoxBuddy specifically, and it's not a close call:** the CIE's entire value proposition (§7 of the PRD) is *gating what gets translated* — rejecting bystanders, holding a partner lock, deciding per-utterance whether this speaker even enters the pipeline. An end-to-end black box that turns audio into translated audio in one hop gives the CIE nothing to gate *on* — no diarized speaker labels, no per-speaker confidence, no hook to say "transcribe but don't synthesize." Adopting an end-to-end vendor would mean either translating every voice in range indiscriminately (violates §7.3, breaks the bystander-rejection promise) or bolting a separate diarization pass in front of it anyway — at which point you've paid for an end-to-end product and rebuilt the cascade around it regardless.

**Decision: cascaded architecture, ASR+diarization → Translation → TTS as separate stages.** This was actually implied by the PRD's multi-agent design already; the research confirms it's still the right call rather than something Phase 2 should "simplify away."

---

## 1. ASR + Speaker Diarization

Need: streaming, low end-of-speech latency, diarization *on the same connection* (not a separate batch pass — CIE decisions have to happen per-utterance, in real time), reasonable multilingual/code-switching support (Hindi/English and other code-switched speech is a realistic VoxBuddy scenario per the PRD's environments list).

| Vendor | Streaming latency | Diarization | Multilingual | Pricing (streaming) | Notes |
|---|---|---|---|---|---|
| **AssemblyAI (Universal-Streaming / Universal-3.5 Pro Realtime)** | ~300ms | Yes, inline on the same WebSocket, up to 10 speakers, revises labels as context arrives | Strong, with mid-sentence code-switching on the multilingual streaming model | ~$0.15/hr (Universal-Streaming) up to $0.45/hr (Pro Realtime); diarization is an inline feature, not a separate paid add-on | Best fit for VoxBuddy: diarization is *inline*, which is exactly the signal the CIE needs without a second network hop |
| **Deepgram (Flux / Nova-3)** | Sub-300ms, some of the lowest published streaming latency in the category | Add-on (~$0.12/hr extra) | Flux Multilingual covers 36+ languages with code-switching | ~$0.0077/min streaming (~$0.46/hr) plus diarization add-on | Excellent latency; diarization costs extra and is a separate feature flag, slightly less clean fit than AssemblyAI's inline model |
| **Speechmatics (Ursa 2)** | Sub-1s | Yes | 55+ languages, notably strong on Indian English and other accents the shopkeeper/tourist scenario will actually encounter | ~$0.24/hr Pro tier | Worth a real accuracy bake-off given VoxBuddy's target environments (markets, non-native English) — accent robustness is a differentiator here specifically |
| **Soniox** | Sub-200ms | Yes, plus native speech translation in the same API | 60+ languages | ~$0.12/hr streaming | Interesting middle ground — bundles translation but still exposes diarized text, so the CIE could still gate before the translation step fires |

**Recommendation for the Phase 2 build-out: benchmark AssemblyAI Universal-Streaming first** (inline diarization is the cleanest match for the CIE's needs, mid-tier pricing), **with Speechmatics Ursa 2 as the accuracy/accent-robustness challenger** given VoxBuddy's actual target environments skew toward non-native and accented speech. Don't commit past a benchmark — run both against recorded market/airport/restaurant audio (PRD §17's audio test harness) before picking.

**AWS-native option, added post-recommendation:** Amazon Transcribe streaming also supports diarization (`ShowSpeakerLabel`), but its labels are assigned per-word rather than inline per-turn like AssemblyAI's — see `backend/agents/asr_transcribe.py`'s module docstring for what that means for a result that spans a turn boundary. Not included in the table/benchmark above because it wasn't evaluated against real audio at the time this doc was written; it's implemented (`VOXBUDDY_ASR_PROVIDER=aws_transcribe`) as the AWS-native alternative the README's "Future improvements" section called for, not as a revision to the recommendation above. Worth adding to a real bake-off alongside AssemblyAI/Speechmatics before Phase 2 commits to one vendor, especially if judged AWS-native integration depth matters as much as raw ASR accuracy for the target use case.

---

## 2. Translation

Need: **context-aware** translation — the CIE's `ConversationTopic`/`TurnHistory` (PRD §7.1) needs to actually bias the translation, not just receive isolated sentences. This is explicitly called out as a functional requirement (FR-6) and is one of the areas where a fixed NMT engine (Google Translate API, DeepL) is architecturally awkward — those APIs translate strings, they don't take "here's the last 3 turns of context" as a first-class input.

Two real paths:

1. **Dedicated speech-translation vendors** (Soniox speech translation, Palabra.ai) — fast, purpose-built, but same limitation as any fixed NMT: limited-to-no support for injecting rolling conversation context or domain terms discovered mid-conversation.
2. **LLM-based translation** (Claude, GPT-class models) called with the turn history and topic embedding as explicit context — this directly implements what the PRD's Context Memory Agent is for (§8: "supplies domain-term bias to ASR/MT"), at the cost of higher per-call latency than a dedicated NMT engine and a per-provider prompt-engineering effort to keep it terse/low-latency.

**Recommendation: LLM-based translation for v1**, specifically because context-awareness is a named functional requirement, not a nice-to-have, and no dedicated NMT vendor found in this research exposes a comparable context-injection mechanism. This is the one place in the stack where "buy the fastest dedicated vendor" is second-best to "build a thin, well-prompted LLM call" — flagging that explicitly since it cuts against the PRD §21 default of buying commodity AI. Latency cost of this choice needs to be measured directly against the ≤2.0s budget once real numbers exist; if it blows the budget, fall back to a dedicated NMT engine for the hot path and reserve LLM translation for a lower-priority "context correction" pass.

I've implemented a working adapter against this recommendation — see `backend/agents/translation_anthropic.py`.

**Update:** Amazon Bedrock (`backend/agents/translation_bedrock.py`) is the *code's default* translation provider (`VOXBUDDY_TRANSLATION_PROVIDER=bedrock`), using Bedrock's Converse API against the same LLM-based, context-injection design recommended above — the recommendation itself hasn't changed, just which vendor runs it by default, since Bedrock reuses the AWS credentials this app's other AWS-native stages (Polly, Transcribe, DynamoDB) already require, with no separate account/billing relationship. **However, the currently deployed hackathon build overrides this default and runs on Gemini** (`agents/translation_gemini.py`, `VOXBUDDY_TRANSLATION_PROVIDER=gemini`) instead, because Bedrock model access is currently blocked at the AWS account level — see `README.md` §6.7 for the full story. Flipping back to `bedrock` once that's resolved needs no code change.

---

## 3. Text-to-Speech

Need: streaming (audio must start playing before the full sentence is synthesized, to hit the latency budget), natural-sounding, broad language coverage.

| Vendor | Time-to-first-audio | Notes |
|---|---|---|
| **ElevenLabs (Flash v2.5 + streaming WebSocket)** | Low, WebSocket-based, designed for exactly this "stream partial text as an LLM produces it" pattern | Mature SDK, multi-context WebSocket support for handling interruptions/barge-in, which matters once the CIE supports true partner-switch mid-utterance |
| **Deepgram Aura-2** | Sub-200ms published | Attractive if already on Deepgram for ASR — one vendor relationship, one bill |
| **Inworld Realtime TTS-2** | Sub-200ms published, described as a current latency leader | Newer entrant; worth a bake-off but less production track record than ElevenLabs |

**Recommendation: ElevenLabs for the Phase 2 build-out** — the multi-context WebSocket API's interruption handling maps directly onto the CIE's partner-switch behavior (§7.4), and it's the most production-proven option of the three. Adapter implemented at `backend/agents/tts_elevenlabs.py`.

---

## 4. What's implemented vs. what's still a decision doc

*Real Anthropic and Gemini adapters exist alongside the code-default Bedrock one — `agents/translation_anthropic.py` / `agents/translation_gemini.py` — same context-injection design, picked via `VOXBUDDY_TRANSLATION_PROVIDER=anthropic` / `=gemini`.*

**Note:** the "Status" column below describes each adapter's own implementation state at the time it was written (real vendor integration vs. mock), not which one the current hackathon deployment actually runs — see "Current Hackathon Architecture" at the top of this document for that.

| Agent | Status |
|---|---|
| Translation (Amazon Bedrock — code default; Anthropic Claude or Google Gemini — alternative, context-aware) | **Implemented, real, and (Bedrock/Anthropic) live-tested** — `agents/translation_bedrock.py` (code default) / `agents/translation_anthropic.py` / `agents/translation_gemini.py`. Selected via `VOXBUDDY_TRANSLATION_PROVIDER`; the deployed hackathon build sets this explicitly to `gemini` (Bedrock is blocked at the AWS account level — README.md §6.7). |
| ASR + diarization (AssemblyAI — historical/alternative; current deployment uses AWS Transcribe) | **Implemented against the real v3 streaming SDK.** The async/streaming interface gap noted below is closed — `StreamingASRAgent` in `agents/base.py`, bridged into the pipeline by `session/streaming_manager.py`. First live-device test surfaced a real integration bug: the adapter called the SDK's blocking `client.stream()` once per audio frame instead of once with a generator for the whole session (the SDK's actual documented contract), so no final transcript was ever produced. Fixed with a queue + generator + background-thread pattern that preserves the non-blocking `send_audio()` shape the rest of the pipeline depends on — see `agents/asr_assemblyai.py`. Needs `ASSEMBLYAI_API_KEY` set (locally in `.env`, and separately in Render's dashboard if deploying there, since Render doesn't inherit local `.env` values). |
| TTS (ElevenLabs — historical/alternative; current deployment uses Amazon Polly) | **Implemented against the real API**, with real (non-placeholder) default voice IDs now set in `agents/tts_elevenlabs.py` — though currently the same voice ID is reused for every target language, so distinct per-language voices are still a to-do. Needs `ELEVENLABS_API_KEY` set the same way as AssemblyAI above (local `.env` + Render dashboard separately, if deploying there). |
| Speaker embeddings | **Still mocked.** Recommendation unchanged: derive an initial speaker identity directly from inline diarization labels (cheap, already-paid-for) rather than standing up a separate embedding model in v1; revisit a dedicated x-vector/d-vector model only if diarization-label stability across turns proves insufficient for the CIE's cross-turn `Speaker` tracking |

**Deployment note learned the hard way (historical, applies if deploying to Render):** `render.yaml` marks every vendor API key as
`sync: false`, meaning Render never auto-populates them from this repo or from a local
`.env` — they have to be pasted into the Render dashboard manually, separately from local
dev setup. A backend that works perfectly locally but stays silent (or mock-only) once
deployed is the signature symptom of this step being skipped. The current hackathon
deployment runs on AWS Elastic Beanstalk instead, where the equivalent step is setting
environment properties on the EB environment — see `docs/AWS_DEPLOYMENT.md` §5.

## 5. Immediate next actions (need real accounts / credentials, can't be done further in this sandbox)

1. Get trial API keys for AssemblyAI, Speechmatics, ElevenLabs.
2. Run the same 5-minute multi-speaker, market-noise recording through AssemblyAI and Speechmatics; compare WER and diarization stability — this determines the ASR pick.
3. Wire `translation_anthropic.py`'s real key in and measure actual latency per call against the ≤2.0s budget; this determines whether LLM-based translation survives contact with the latency requirement or needs a dedicated-NMT fallback.
4. ~~Extend `agents/base.py`'s `ASRAgent` protocol to an async streaming shape once a vendor is chosen~~ **Done.** `StreamingASRAgent` (event-callback shaped, matching how real vendor SDKs actually work) now lives in `agents/base.py`, implemented by both `agents/mock_streaming_asr.py` (for testing) and `agents/asr_assemblyai.py` (real vendor scaffold). `session/streaming_manager.py` bridges either implementation into the existing CIE + Translation + TTS pipeline — 7 tests cover partial-vs-final gating, multi-turn accumulation, bystander rejection through the streaming path, and that streaming/batch paths share CIE state correctly. What's still open: live-testing against AssemblyAI's actual WebSocket (needs real credentials, not available in this build environment) and deciding how turn-taking/semantic-coherence signals get supplied per turn in production — `signal_provider` in `streaming_manager.py` is currently a caller-supplied placeholder, not connected to a real signal source.
