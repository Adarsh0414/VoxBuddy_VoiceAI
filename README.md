<div align="center">

# VoxBuddy

**Real-time, hands-free speech translation for travelers — talk, and let your earbuds do the rest.**

*Powered by Amazon Polly and Amazon DynamoDB.*

</div>

---

## 1. One-line pitch

VoxBuddy listens through your Bluetooth earbuds, figures out who you're actually talking to, translates the conversation live, and speaks the translation back into your ear — no phone screen, no typing, no manual language selection.

---

## 2. Demo links

| Resource | Link |
|---|---|
| 🎥 Demo video | `[ADD LINK]` |
| 📱 Android APK | [VoxBuddy_VoiceAi v1.0.0](https://github.com/Adarsh0414/VoxBuddy_VoiceAI/releases/download/v1.0.0/VoxBuddy_VoiceAi.apk) |
| 🖥️ Backend API (deployed) | [VoxBuddy-CIE_Live_Cockpit](http://voxbuddy-env.eba-ixnm7pmc.us-east-1.elasticbeanstalk.com/?utm_source=chatgpt.com) |

---

## 3. Problem

Language barriers become especially difficult in real-world conversations, where people need to communicate naturally without constantly looking at a phone or manually selecting speakers and languages.

Existing translation apps make you stop the conversation, pull out your phone, hold it up like a walkie-talkie, and hand it back and forth. That's slow, awkward, and breaks eye contact exactly when you need it most — asking for directions, haggling at a market, checking into a guesthouse, or just having a real conversation with someone who doesn't share your language.

The hard part hiding inside that problem isn't translation quality — machine translation is commodity-adjacent in 2026. It's **figuring out who is actually talking to you in the first place.** A live mic in a crowded market picks up the shopkeeper, the shopkeeper's kid, a nearby vendor, and the user's own voice, all mixed together. Naively translating everything produces a stream of garbage and gets it wrong the moment a third party interrupts.

---

## 4. Solution

You start a conversation once, then put your phone away. VoxBuddy:

1. **Listens continuously** through your connected earbuds (Bluetooth Classic or BLE)
2. **Figures out who's actually talking to you** — not background noise, not a stranger walking past — using a real signal-fusion engine, not just "loudest voice wins"
3. **Translates what they say in real time**, with rolling conversation context so translations stay coherent turn-to-turn, not isolated sentence-by-sentence guesses
4. **Speaks the translation back into your earbuds** through **Amazon Polly**, in a natural voice, so you never have to look at a screen mid-conversation

---

## 5. ⭐ What makes VoxBuddy different

Most "live translate" demos work great in a silent room with one other speaker. Real conversations aren't like that — there's background noise, other people talking nearby, and the person you're talking to might pause, step away, or hand you off to someone else (a shopkeeper's colleague, a second family member joining in). Most competitors solve none of this; they're a push-to-talk phone screen with a translate button.

VoxBuddy's answer is the **Conversation Intelligence Engine (CIE)** — the one part of the system that is **not** a vendor API call, but original, fully unit-tested logic (`backend/cie/`):

- **Partner identification via signal fusion** — combines voice similarity, turn-taking fit, and semantic coherence rather than trusting any single signal alone
- **Hysteresis** — won't flip who it thinks your "partner" is on one noisy read
- **Bystander rejection** — a stranger's voice nearby doesn't hijack the conversation
- **Multi-partner conversation groups** — tracks up to two active partners at once (a couple, two colleagues), with fast-track/confirmation logic for replacing a member who's gone quiet
- **Self-voice enrollment** — your own voice is registered once at session start and permanently excluded from partner candidacy, so you can never accidentally become "the partner" in your own conversation
- **Incoherence recovery** — self-heals if the conversation state drifts, instead of getting permanently stuck or throwing a visible error

Every other stage (ASR, translation, TTS, storage) sits behind a clean interface, which is what let us build directly on AWS without touching the CIE at all.

---

## 6. ☁️ Built on AWS

VoxBuddy's deployed pipeline runs on AWS:

- **Amazon Bedrock is VoxBuddy's intended default translation provider, fully implemented in code** — see §6.7 for why the *deployed* build currently runs on Gemini instead, and why that's an AWS account-provisioning issue, not a code or architecture gap.
- **Amazon Polly powers VoxBuddy's translated voice output.** Every translated line the user hears is synthesized by Polly, not a third-party TTS vendor.
- **Amazon DynamoDB stores VoxBuddy's conversation history and usage data.** Every saved conversation, day-streak, per-language count, and stats screen reads from and writes to DynamoDB.
- **Amazon Transcribe powers VoxBuddy's real-time speech-to-text.** When `VOXBUDDY_ASR_PROVIDER=aws_transcribe` is set, live microphone audio is streamed straight to Transcribe instead of a third-party ASR vendor.

`[ADD PLACEHOLDER — confirm/adjust this line once deployed, e.g. "Deployed on Render, backend calls out to Polly and DynamoDB in us-east-1."]`

### 6.1 Amazon Polly — text-to-speech

**File:** `backend/agents/tts_polly.py`

Calls `boto3.client("polly").synthesize_speech(...)` to turn translated text into spoken audio, streamed back into the user's earbuds. Ships with a language → neural-voice map covering all 15 supported languages, with an automatic fallback to Polly's `standard` engine if a neural voice isn't available in the configured region/account.

**Why Polly:** no separate account or billing relationship beyond AWS, and Polly's voice catalog is fixed and documented rather than account-specific — a stable foundation for a static language→voice map.

### 6.2 Amazon DynamoDB — conversation history

**File:** `backend/persistence_dynamodb.py`

Every conversation, turn, and stats query (`save_conversation`, `list_conversations`, `get_conversation`, `get_summary_stats`, `get_language_breakdown`, `get_day_streak`, `delete_all`) is backed by three DynamoDB tables (conversations, turns, and an atomic-counter table for sequential conversation ids), on-demand billed.

**Why DynamoDB:** a managed, horizontally-scalable store appropriate for a real multi-user deployment, with no server to patch or single-file database to outgrow.

### 6.3 Amazon Transcribe — streaming speech-to-text

**File:** `backend/agents/asr_transcribe.py`

Implements the same `StreamingASRAgent` protocol as the mock and AssemblyAI adapters, using the `amazon-transcribe` (awslabs) async streaming SDK's `start_stream_transcription(...)` call. Selected by setting `VOXBUDDY_ASR_PROVIDER=aws_transcribe`, as a direct alternative to AssemblyAI — same call sites in `session/streaming_manager.py`, zero orchestration changes. Per-word diarization (`Item.speaker`) is resolved to one speaker label per final result via majority vote (`_majority_speaker`).

**Why Transcribe:** no separate account/billing relationship beyond AWS, same as Polly and DynamoDB. **Testing caveat:** unlike Polly and DynamoDB, `moto` does not mock Transcribe's real-time streaming API, so this adapter's automated coverage is limited to the shared `StreamingASRAgent` protocol via `MockStreamingASRAgent` — see `docs/AWS_INTEGRATION.md` for the full caveat before relying on this in production.

### 6.4 Dev-mode fallback

Because every AI/storage stage sits behind a clean interface (`agents/base.py`, `persistence_store.py`), the same codebase can also run entirely offline for local development with **no AWS account and no API keys** — ElevenLabs and SQLite are the zero-config mocks/fallbacks used while developing, selectable with `VOXBUDDY_TTS_PROVIDER` / `VOXBUDDY_PERSISTENCE_PROVIDER`. The **submitted, deployed application runs on Polly, DynamoDB, and (when `VOXBUDDY_ASR_PROVIDER=aws_transcribe`) Transcribe** — see §14 for exactly what's live-tested.

### 6.5 IAM policy

This is the policy actually attached to the deployed IAM user (`voxbuddy-backend-policy`) — Bedrock (see §6.7 for why it's not currently unblocked despite being granted here), Polly, Transcribe, and DynamoDB permissions:

```json
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Sid": "VoxBuddyBedrockMarketplace",
            "Effect": "Allow",
            "Action": [
                "aws-marketplace:ViewSubscriptions",
                "aws-marketplace:Subscribe",
                "aws-marketplace:Unsubscribe"
            ],
            "Resource": "*"
        },
        {
            "Sid": "VoxBuddyBedrockAvailability",
            "Effect": "Allow",
            "Action": "bedrock:GetFoundationModelAvailability",
            "Resource": "*"
        },
        {
            "Sid": "VoxBuddyBedrockRegionalProfile",
            "Effect": "Allow",
            "Action": "bedrock:InvokeModel",
            "Resource": "arn:aws:bedrock:us-east-1:<YOUR_ACCOUNT_ID>:inference-profile/global.anthropic.claude-sonnet-4-6",
            "Condition": {
                "StringEquals": {
                    "aws:RequestedRegion": "us-east-1"
                }
            }
        },
        {
            "Sid": "VoxBuddyBedrockRegionalModelAccess",
            "Effect": "Allow",
            "Action": "bedrock:InvokeModel",
            "Resource": "arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-sonnet-4-6",
            "Condition": {
                "StringEquals": {
                    "aws:RequestedRegion": "us-east-1",
                    "bedrock:InferenceProfileArn": "arn:aws:bedrock:us-east-1:<YOUR_ACCOUNT_ID>:inference-profile/global.anthropic.claude-sonnet-4-6"
                }
            }
        },
        {
            "Sid": "VoxBuddyBedrockGlobalModelAccess",
            "Effect": "Allow",
            "Action": "bedrock:InvokeModel",
            "Resource": "arn:aws:bedrock:::foundation-model/anthropic.claude-sonnet-4-6",
            "Condition": {
                "StringEquals": {
                    "aws:RequestedRegion": "unspecified",
                    "bedrock:InferenceProfileArn": "arn:aws:bedrock:us-east-1:<YOUR_ACCOUNT_ID>:inference-profile/global.anthropic.claude-sonnet-4-6"
                }
            }
        },
        {
            "Sid": "VoxBuddyPolly",
            "Effect": "Allow",
            "Action": [
                "polly:SynthesizeSpeech",
                "polly:DescribeVoices"
            ],
            "Resource": "*"
        },
        {
            "Sid": "VoxBuddyTranscribe",
            "Effect": "Allow",
            "Action": [
                "transcribe:StartStreamTranscription",
                "transcribe:StartStreamTranscriptionWebSocket"
            ],
            "Resource": "*"
        },
        {
            "Sid": "VoxBuddyDynamoDBList",
            "Effect": "Allow",
            "Action": [
                "dynamodb:ListTables"
            ],
            "Resource": "*"
        },
        {
            "Sid": "VoxBuddyDynamoDBTables",
            "Effect": "Allow",
            "Action": [
                "dynamodb:CreateTable",
                "dynamodb:DescribeTable",
                "dynamodb:GetItem",
                "dynamodb:PutItem",
                "dynamodb:UpdateItem",
                "dynamodb:DeleteItem",
                "dynamodb:Query",
                "dynamodb:Scan"
            ],
            "Resource": "arn:aws:dynamodb:us-east-1:<YOUR_ACCOUNT_ID>:table/voxbuddy_*"
        }
    ]
}
```

`polly:SynthesizeSpeech`/`DescribeVoices` and `transcribe:StartStreamTranscription*` have no resource-level restriction in AWS (neither service supports resource ARNs for these actions, so `Resource: "*"` is the tightest scope available). `dynamodb:ListTables` is also account/region-scoped by AWS, not per-table — it's used once at startup (`persistence_dynamodb.init_db()`) to check which tables already exist. Every other DynamoDB action is scoped to `voxbuddy_*`-prefixed tables and limited to exactly the operations the code calls (no `dynamodb:*` wildcard) — this policy grants nothing beyond what `backend/persistence_dynamodb.py` actually uses.

The Bedrock statements target Claude Sonnet 4.6 specifically via its **inference profile ARN** (`global.anthropic.claude-sonnet-4-6`), not the bare foundation-model ARN — this model requires cross-region inference, so `bedrock:InvokeModel` needs both the profile-scoped statement and the two foundation-model statements with a matching `bedrock:InferenceProfileArn` condition (one for `us-east-1`, one for AWS's "unspecified"/global routing) to actually authorize a call. `VoxBuddyBedrockAvailability` and `VoxBuddyBedrockMarketplace` are what's needed to check and subscribe to the model in the first place — per §6.7, having these correct in IAM turned out to be necessary but not sufficient; the block that's actually stopping calls sits on AWS's Marketplace/billing side, not here.

**Important:** every `<YOUR_ACCOUNT_ID>` placeholder above (four of them — three in the Bedrock inference-profile ARNs, one in the DynamoDB table ARN) must be your actual AWS account ID — a blank account-ID segment (`us-east-1::...`) does not default to "your account" the way it does for a few AWS-owned global resources, and won't match any real resource ARN. Double-check the **JSON** tab on the live policy in the IAM console and fix any empty segment there — the repo doesn't store the live policy, only this reference copy.

This policy does **not** grant `cloudwatch:PutMetricData` or any `s3:*` actions — if you turn on `VOXBUDDY_CLOUDWATCH_METRICS` or `VOXBUDDY_CALIBRATION_LOGGING` (both off by default; see `docs/AWS_INTEGRATION.md`), add the extra statements shown there first, or those calls will fail with `AccessDenied`.

### 6.6 AWS reference

| Resource | Value |
|---|---|
| AWS Region | `us-east-1` |
| AWS Services used | Amazon Bedrock, Amazon Polly, Amazon DynamoDB, Amazon Transcribe |
| DynamoDB Tables | `voxbuddy_conversations`, `voxbuddy_turns`, `voxbuddy_counters` (prefix via `DYNAMODB_TABLE_PREFIX`) |
| Architecture diagram (AWS-focused) | `[ADD LINK]` |

See `docs/AWS_INTEGRATION.md` for the full write-up, including how a real `moto`-backed test run against Polly's voice catalog caught a real bug before production (the French voice was mapped to `"Lea"` instead of Polly's actual voice id `"Léa"`).

### 6.7 Amazon Bedrock — translation (implemented, blocked at the AWS account level; Gemini is the active provider in the deployed build)

**File:** `backend/agents/translation_bedrock.py`

Fully implements the same `TranslationAgent` protocol (`agents/base.py`) as the Anthropic and Gemini adapters, using the Bedrock Runtime **Converse API** (`boto3.client("bedrock-runtime").converse(...)`) rather than `invoke_model`, so switching `BEDROCK_MODEL_ID` to a different model family needs no code change here. Selectable via `VOXBUDDY_TRANSLATION_PROVIDER=bedrock`, using the same `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` / `AWS_REGION` credentials as Polly and DynamoDB above — no separate key required, and the IAM policy for it (`bedrock:InvokeModel` scoped to the `BEDROCK_MODEL_ID` resource ARN, plus the `aws-marketplace:ViewSubscriptions` / `Subscribe` / `Unsubscribe` actions AWS requires for third-party Bedrock models) is written and ready in `docs/AWS_INTEGRATION.md`.

**Why the deployed app doesn't actually call Bedrock right now:** every call fails with `AccessDeniedException`, tracing back to AWS Marketplace, not to this codebase or its IAM policy. Subscribing to "Claude Sonnet 4.6 (Amazon Bedrock Edition)" (Product ID `prod-ffvjxvh4ltq64`) on our AWS account repeatedly auto-terminates the subscription agreement — service start and service end land on the *same timestamp*, every time, across five separate attempts (agreement IDs `agmt-26ke1lcobnbghjv2ax3qnnulj`, `agmt-2faci7i6mh8m4uu86kuv4iuvh`, `agmt-36zc6irds3x60xq0h5sb8qfwr`, and others) — after fixing every documented, console-visible cause in order:

1. Initial error: `AccessDeniedException ... IAM user or service role is not authorized to perform the required AWS Marketplace actions (aws-marketplace:ViewSubscriptions, aws-marketplace:Subscribe)` → added those actions to the IAM policy. No change.
2. Next error, testing directly in the Bedrock console Playground (ruling out our own IAM user entirely): `AccessDeniedException: ... INVALID_PAYMENT_INSTRUMENT: A valid payment instrument must be provided` → the AWS account had **zero payment methods on file** (confirmed on the Billing → Payment methods page). Added UPI AutoPay first — didn't resolve it, since AWS's own UI separately flagged *"Your AWS account is not verified. Add a valid credit card to verify the account"*, i.e. UPI alone doesn't satisfy Marketplace's account-verification requirement. Added a RuPay debit card on top of UPI specifically to clear that verification step; the "not verified" warning did go away once the card was added.
3. After all of the above — IAM permissions fixed, UPI AutoPay *and* a verified RuPay card both on file, account verification cleared — resubscribing via **AWS Marketplace → Discover products → Claude Sonnet 4.6 (Amazon Bedrock Edition) → Subscribe** still produces the identical same-timestamp terminate, with no error reason surfaced anywhere in the console — not on the subscription confirmation, not on the agreement detail page, not in the follow-up email. So the account-verification fix cleared its own warning but didn't unblock the actual subscription, which points to something on AWS's backend beyond what any console page surfaces.

This account also has a live, AWS-confirmed $100 hackathon credit (WeMakeDevs, credit ID `10067457025`, explicitly covering Amazon Bedrock / Amazon Bedrock Service / AmazonBedrockFoundationModels per its service list) — so this isn't a spend-limit or billing-eligibility problem either. At this point the cause is only visible from AWS's side, and it's filed as an **AWS Support case** (Billing/Marketplace category) rather than something fixable from the console. If it resolves, flipping `VOXBUDDY_TRANSLATION_PROVIDER` from `gemini` back to `bedrock` is the only change needed — the adapter, IAM policy, and credentials are already in place and untouched.

**What the deployed app actually runs on instead:** `VOXBUDDY_TRANSLATION_PROVIDER=gemini` with a personal `GEMINI_API_KEY` — the same Gemini integration that was VoxBuddy's original, real-device-tested translation path before the Bedrock adapter was added. Nothing about the CIE, ASR, TTS, or persistence layers changed to make this work — the provider abstraction in `agents/base.py` is exactly what made this a one-line config swap instead of a rewrite.

**IAM:** the `bedrock:InvokeModel` + `aws-marketplace:*` statements are included in the live `voxbuddy-backend-policy`, ahead of §6.5's policy actually working end-to-end — they're necessary but, per the above, not sufficient on their own.

---

## 7. 🏗️ Architecture

```
┌─────────────┐        WebSocket (audio)         ┌──────────────────────────┐
│   Mobile /   │ ───────────────────────────────▶ │        FastAPI            │
│  Web client  │                                   │                            │
│  (earbuds    │ ◀─────────────────────────────── │  /ws/{session}/audio      │
│   mic + TTS  │        translated audio           │        │                   │
│   playback)  │                                   │        ▼                   │
└─────────────┘                                   │  Streaming ASR Agent       │
                                                    │  (AssemblyAI)              │
                                                    │        │ final transcript  │
                                                    │        ▼                   │
                                                    │  Conversation              │
                                                    │  Intelligence Engine (CIE) │
                                                    │  — partner? bystander?     │
                                                    │        │ accepted turn     │
                                                    │        ▼                   │
                                                    │  Translation Agent         │
                                                    │  (Gemini, active; Bedrock  │
                                                    │   implemented, blocked —   │
                                                    │   see §6.7)                │
                                                    │        │ translated text   │
                                                    │        ▼                   │
                                                    │  Amazon Polly              │
                                                    │  (text-to-speech)          │
                                                    │        │ audio bytes       │
                                                    │        ▼                   │
                                                    │  Amazon DynamoDB           │
                                                    │  (conversation history)    │
                                                    └──────────────────────────┘
```

Every stage in the middle column sits behind a small interface (`agents/base.py`), which is what makes the AWS integration a real architectural choice rather than a hardcoded dependency — Polly and DynamoDB are what the deployed app runs on; a local, offline mock/fallback path exists purely for development (see §6.4). The ASR stage itself is swappable via `VOXBUDDY_ASR_PROVIDER` — `mock` (default, no network), `assemblyai`, or `aws_transcribe` (the AWS-native option, `backend/agents/asr_transcribe.py`) — all three implement the same `StreamingASRAgent` protocol, so this diagram's ASR box is whichever one is configured, not a fixed dependency.

### 7.1 Supported languages

VoxBuddy's translation stage is LLM-based (Amazon Bedrock by default, with Anthropic Claude or Google Gemini as optional alternatives) and not limited to a fixed list — but the Polly voice map currently covers **15 languages** out of the box:

`English` · `Spanish` · `French` · `German` · `Italian` · `Portuguese` · `Hindi` · `Japanese` · `Korean` · `Chinese (Mandarin)` · `Arabic` · `Russian` · `Dutch` · `Polish` · `Turkish`

Adding another language is a one-line addition to `VOICE_MAP` in `backend/agents/tts_polly.py`; any language outside the map still gets a translation, just with a default-voice TTS fallback.

| Architecture diagram (full-size / editable) | `[ADD LINK]` |
|---|---|

---

## 8. Key features

**Real-time pipeline**
- Live microphone capture → streaming ASR → context-aware translation → Polly TTS playback, end to end
- Adaptive voice-activity gating (skips forwarding silence/ambient noise, not full noise cancellation)
- WebSocket auto-reconnect with exponential backoff — a dropped connection resumes the *same* conversation state instead of restarting

**Authentication**
- Passwordless OTP login via email or phone (hashed codes, single-use, rate-limited, auto-expiring)
- Google Sign-In as an alternative, verified server-side against Google's own public keys
- Bearer-token sessions, optionally backed by Redis for horizontal scaling

**Conversation history & stats (DynamoDB)**
- Every conversation is persisted to DynamoDB and scoped to the logged-in user
- Real day-streak, per-language conversation counts, and total talk time — computed from actual history, not placeholder numbers

**Mobile app**
- Installable PWA (manifest + service worker, real home-screen icon, full-screen launch)
- Native Android app (Capacitor) with a foreground service to keep listening alive when backgrounded, native Bluetooth audio device detection, and adaptive launcher icons
- iOS project scaffolded (Xcode project generated; full native build requires macOS)

**Interface-driven pipeline**
- Every AI stage (ASR, translation, TTS, persistence) sits behind a clean interface with a real vendor adapter and a working local mock, swappable purely via environment variables — this is *why* plugging in Polly and DynamoDB required no orchestration changes

---

## 9. 🚀 First Commit contribution

`[ADD PLACEHOLDER — link to the first commit / initial scaffold, and a short note on what shipped in it, e.g.:]`

| | |
|---|---|
| First commit | `[ADD LINK]` |
| Date | `[ADD DATE]` |
| What it established | `[ADD PLACEHOLDER — e.g. "FastAPI skeleton, mock agent interfaces, PRD"]` |
| Contributor | `[ADD NAME / GitHub handle]` |

---

## 10. Cost considerations

Cloud AI inference cost is treated as a first-class product metric, not an afterthought:

- **Amazon Polly** bills per character synthesized, with no separate account/subscription needed beyond AWS — no dedicated TTS vendor bill to manage.
- **Amazon DynamoDB** runs on-demand (`PAY_PER_REQUEST`) billing, so cost scales with actual usage — near-zero at hackathon/demo traffic, with no fixed server cost and no single-instance ceiling to hit as usage grows.
- **ASR and translation are the more expensive stages** (streaming ASR is billed per audio-hour; LLM-based translation is billed per token) — the vendor research in `docs/vendor_decision.md` tracks cost-per-minute for every ASR/TTS vendor considered, and the PRD (`docs/VoxBuddy_PRD_and_Architecture.md`, §6) lists "cloud AI inference cost per conversation-minute" as a tracked non-functional requirement.
- **Testing costs nothing:** both AWS integrations are tested against `moto`, so CI and local test runs never touch billed AWS endpoints.

| Estimated monthly cost (demo scale) | `[ADD PLACEHOLDER]` |
|---|---|
| Estimated monthly cost (production scale) | `[ADD PLACEHOLDER]` |

---

## 11. Testing

```bash
cd backend
pytest tests/ -v
```

**203 tests** covering the CIE's partner-identification logic, streaming pipeline wiring, auth (OTP + Google Sign-In), persistence, TTS/ASR error handling, and session token storage. Notably:

- `test_tts_polly.py` — 4 tests: real audio bytes returned, every mapped language synthesizes, unmapped languages fall back correctly, and the neural→standard engine fallback actually triggers.
- `test_persistence_dynamodb.py` — 7 tests: save/retrieve, empty-session handling, user-scoped listing, id sequencing, delete-all, ownership lookup, and stats/language-breakdown aggregation — mirroring every scenario the local-dev test suite already covers, so the DynamoDB backend is proven to satisfy the same contract `app.py` depends on.

Both AWS test files use [`moto`](https://github.com/getmoto/moto) (AWS's official mocking library) — no real AWS calls, no cost, and no credentials needed to run the suite.

| CI / coverage badge | `[ADD LINK / BADGE]` |
|---|---|

---
---

*Everything below this line goes deeper: exact user flow, full tech stack, AWS-specific design decisions, what's real vs. mocked, implementation notes, project layout, and how to run it locally.*

---

## 12. 🔄 User flow

1. Open the app once, sign in (OTP or Google), set a default spoken language, and connect Bluetooth earbuds — this is the *only* manual step in the entire experience.
2. Put the phone away and start talking. VoxBuddy's earbud mic captures your speech and streams it over a WebSocket to the backend.
3. Streaming ASR turns speech into text in near real time.
4. The **CIE** decides, per utterance: is this you, your conversation partner, or a bystander who should be ignored? It holds that decision with hysteresis so it doesn't flip on a single noisy read.
5. Accepted turns go to the context-aware translation agent, which uses the rolling conversation history so translations stay coherent turn-to-turn.
6. The translated text is synthesized into speech by **Amazon Polly** and streamed back into your earbuds — no screen, no typing, no manual language picker.
7. If your conversation partner changes (you turn to a new person, or a second person joins), the CIE detects it and re-establishes the partner automatically.
8. The finished conversation is persisted to **Amazon DynamoDB** (opt-in history) so your streaks, per-language counts, and total talk time are computed from real usage.

| User-flow diagram / storyboard | `[ADD LINK]` |
|---|---|

---

## 13. Tech stack

| Layer | Technology |
|---|---|
| Backend | Python, FastAPI, WebSockets |
| Speech-to-text | [AssemblyAI](https://www.assemblyai.com/) streaming API (real-time ASR + inline diarization) |
| Translation | [Amazon Bedrock](https://aws.amazon.com/bedrock/) (default) — context-aware; [Anthropic Claude](https://www.anthropic.com/) or [Google Gemini](https://ai.google.dev/) API available as optional alternatives |
| **Text-to-speech (deployed)** | **Amazon Polly** |
| **Conversation history (deployed)** | **Amazon DynamoDB** |
| Local dev fallback | ElevenLabs (TTS) and SQLite (history) — used only for offline/no-AWS-account development, see §6.4 |
| Auth | Custom OTP (SMTP or Brevo for email, Fast2SMS or Brevo for SMS) + Google Sign-In |
| Session storage | SQLite (default) or Redis (optional, for scaling auth tokens) |
| Frontend | Vanilla JS single-page app, PWA (manifest + service worker) |
| Mobile shell | [Capacitor](https://capacitorjs.com/) — real Android Studio/Gradle project + Xcode project |
| Deployment | [Render](https://render.com/) (`render.yaml` blueprint included) |
| Testing | pytest — 203 backend tests |

---

## 14. AWS design decisions

Design choices made specifically because AWS is the deployed platform, not just "which vendor is cheapest":

- **Interface-first, AWS by default.** Polly and DynamoDB sit behind interfaces (`TTSAgent`, `persistence_store`) that also support a local mock/fallback — but production configuration (`VOXBUDDY_TTS_PROVIDER=polly`, `VOXBUDDY_PERSISTENCE_PROVIDER=dynamodb`) requires zero changes to `session/manager.py` or `app.py`'s route handlers.
- **One shared credential set.** Both integrations read the same `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` / `AWS_REGION` environment variables via `boto3`'s default credential chain — no VoxBuddy-specific AWS config layer to maintain.
- **Neural-first, standard-fallback for Polly.** Neural voices sound materially better but aren't guaranteed available for every language/region/account combination, so `tts_polly.py` always attempts `neural` first and falls back to `standard` on rejection.
- **Three-table DynamoDB schema, not one.** Conversations, turns, and an atomic-counter table are kept separate so conversation ids can increment atomically (`dynamodb:UpdateItem` with `ADD`) without a read-modify-write race, and so per-turn data doesn't bloat the conversation-list query.
- **`persistence_store.py` swaps the whole module, not individual functions.** A partial re-export would break tests that monkeypatch module-level globals like `persistence.DB_PATH`; replacing the entry in `sys.modules` keeps the DynamoDB and local-dev backends behaviorally identical from every caller's point of view.
- **Scoped IAM, not a wildcard.** The policy in §6.5 grants exactly the DynamoDB operations `persistence_dynamodb.py` calls, scoped to `voxbuddy_*`-prefixed tables — no `dynamodb:*`.
- **`moto` over live AWS calls in tests.** Every AWS code path is exercised in CI without real credentials, real cost, or real network calls.

---

## 15. What's real vs. what's a mock

The **deployed application uses real Amazon Polly and real Amazon DynamoDB** — both are complete implementations against AWS's documented APIs, not stubs. For local development without an AWS account, the same interfaces fall back to ElevenLabs/SQLite or a zero-config mock, so the app is easy to run and demo offline too. See `PROGRESS.md` for the full, honestly-tracked breakdown of what's been live-tested against real vendor traffic versus what's built-and-correct-but-unverified.

---

## 16. Technical implementation

- **Interface-driven, not vendor-driven.** Every AI stage is a small `Protocol`/interface (`agents/base.py` for ASR/translation/TTS, `persistence_store.py` for storage) with one method contract, a mock implementation, and one or more real adapters. Adding a new vendor for any stage is a new adapter file plus one `if provider == "..."` branch in `agents/factory.py` — never a change to the orchestration logic in `session/manager.py`.
- **The CIE is a pure state machine, not a wrapper around an LLM.** It consumes structured signals (voice similarity, turn-taking pattern, semantic coherence) and maintains a per-session `Conversation State Graph` — speakers, active partners, turn history, rolling topic context — independently of which ASR/translation/TTS vendor is plugged in underneath it.
- **Streaming end to end.** Audio flows over a WebSocket in real time; a `StreamingASRAgent` interface (event-callback shaped, matching how real vendor SDKs actually work) bridges into the same CIE + translation + TTS pipeline used by the batch/mock path, so streaming and non-streaming code share one contract, verified in `test_streaming_session.py`.
- **Self-healing over hard failure.** The Polly adapter retries with a safe fallback (the `standard` engine) rather than raising to the caller when a vendor rejects a specific voice/engine combination — the same self-healing philosophy the CIE applies to misattributed conversation partners.
- **Persistence is swappable without touching route handlers.** `app.py` imports `persistence_store`, which replaces its own entry in `sys.modules` with whichever backend is selected — so `VOXBUDDY_PERSISTENCE_PROVIDER=dynamodb` changes the entire storage layer with no changes to any FastAPI route.

---

## 17. Project structure

```
voxbuddy/
├── backend/
│   ├── app.py                    FastAPI app — REST + WebSocket API
│   ├── cie/                      Conversation Intelligence Engine (real, original logic)
│   ├── agents/                   ASR / Translation / TTS interfaces, mocks, and real vendor adapters
│   │   └── tts_polly.py          Text-to-speech — Amazon Polly adapter (deployed default)
│   ├── session/                  Per-session pipeline orchestration + streaming bridge
│   ├── auth_store.py             OTP auth, users, sessions
│   ├── otp_providers.py          SMTP / Brevo / Fast2SMS OTP delivery
│   ├── persistence.py            Conversation history (SQLite, local-dev fallback)
│   ├── persistence_dynamodb.py   Conversation history — Amazon DynamoDB adapter (deployed default)
│   ├── persistence_store.py      Persistence backend selector (env-var driven)
│   ├── token_store.py            Session tokens (SQLite or Redis)
│   └── tests/                    203 pytest tests, incl. moto-based AWS tests
├── frontend/
│   ├── app-preview.html          ⭐ Current product UI, served at /app — Login, Home, Conversation, History, Settings, plus a pre-login Interactive CIE Demo off the login screen...
│   ├── product-preview.html      (Early UI prototype/wireframe — historical reference only, superseded by app-preview.html; not linked from the running app)
│   ├── index.html                CIE developer/testing dashboard
│   ├── manifest.json / sw.js     PWA install + service worker
│   └── privacy.html, terms.html  Legal pages
├── mobile/
│   ├── android/                  Real Android Studio/Gradle project (Capacitor)
│   │   └── .../AudioDevicePlugin.java, ConversationForegroundService.java   Native plugins
│   └── ios/                      Xcode project (Capacitor) — build requires macOS
├── docs/
│   ├── VoxBuddy_PRD_and_Architecture.md
│   ├── AWS_INTEGRATION.md        Full write-up of the Polly + DynamoDB integrations
│   ├── vendor_decision.md
│   ├── MOBILE_BUILD.md
│   ├── PLAY_STORE_PUBLISHING.md
│   ├── SECURITY_PRIVACY_REVIEW.md
│   └── TESTING_REAL_PIPELINE.md
├── render.yaml                   Render deployment blueprint
└── PROGRESS.md                   Full build log — what's done, tested, and what's next
```

---

## 18. Getting started

### Backend

```bash
cd backend
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
uvicorn app:app --reload
```

Open **http://127.0.0.1:8000/app** — this is the current VoxBuddy product UI (`frontend/app-preview.html`). Without AWS credentials set, every stage runs in local mock/fallback mode (no keys needed) so the whole flow — login, conversation, history — still works out of the box for development.

> Note: the repo also contains `frontend/product-preview.html` (served at `/preview`). This is an early UI prototype/wireframe kept for historical reference only — it is not the product and should be ignored when evaluating VoxBuddy. `/app` is the one and only current product experience.

> 🎬 **Judges in a hurry:** on the `/app` login screen, tap **"See How VoxBuddy Works"** for a pre-login Interactive Demo — no account, OTP, or microphone permission needed. It walks through 5 scripted scenarios (conversation partner, bystander filtering, self-voice exclusion, multiple partners, noisy-environment adaptation) using real output from the Conversation Intelligence Engine (`backend/cie/engine.py`), computed offline so it works with zero network/vendor dependencies. It's a fixed, deterministic walkthrough for a fast "what makes this different" pitch — not a substitute for the real logged-in conversation flow above.

### Running on AWS (Polly + DynamoDB + Transcribe)

Copy `.env.example` to `.env` in `backend/` and set:

```bash
VOXBUDDY_TTS_PROVIDER=polly
VOXBUDDY_PERSISTENCE_PROVIDER=dynamodb
VOXBUDDY_ASR_PROVIDER=aws_transcribe
AWS_ACCESS_KEY_ID=...
AWS_SECRET_ACCESS_KEY=...
AWS_REGION=us-east-1
DYNAMODB_TABLE_PREFIX=voxbuddy
```

This is the configuration the deployed app runs with. The IAM user needs the policy in §6.5 — note that policy must include `transcribe:StartStreamTranscription` for the ASR leg, in addition to the Polly/DynamoDB permissions. DynamoDB's three tables are created automatically on startup; there's no separate table-creation step.

### Other providers

| Stage | Env vars | Notes |
|---|---|---|
| Translation (Gemini is what the deployed app actually runs — see §6.7 for why) | `VOXBUDDY_TRANSLATION_PROVIDER=gemini\|anthropic\|bedrock` + matching API key/credentials | `gemini` is the active provider; `bedrock` is implemented and ready but blocked at the AWS account level (§6.7); `anthropic` is a third option, bring your own API key |
| Speech-to-text (alternative to AWS Transcribe above) | `VOXBUDDY_ASR_PROVIDER=assemblyai` + `ASSEMBLYAI_API_KEY` | Real mic transcription |
| Text-to-speech (local dev only) | `VOXBUDDY_TTS_PROVIDER=elevenlabs` + `ELEVENLABS_API_KEY` | Needs real voice IDs in `agents/tts_elevenlabs.py` |
| Conversation history (local dev only) | `VOXBUDDY_PERSISTENCE_PROVIDER=sqlite` | Zero-config, no AWS account needed |
| OTP email | `VOXBUDDY_OTP_EMAIL_PROVIDER=smtp\|brevo` + credentials | Falls back to console-printed codes if unset |
| OTP SMS | `VOXBUDDY_OTP_SMS_PROVIDER=fast2sms\|brevo` + credentials | Optional |
| Google Sign-In | `GOOGLE_CLIENT_ID` (+ `GOOGLE_CLIENT_SECRET` for native) | Sign-in button hides itself if unset |
| Redis sessions | `VOXBUDDY_SESSION_STORE=redis` + `REDIS_URL` | Optional |
| Audio WebSocket idle/session timeout | `VOXBUDDY_WS_IDLE_TIMEOUT_SECONDS` (default 100) / `VOXBUDDY_WS_MAX_SESSION_SECONDS` (default 1800) | Auto-closes `/ws/{session_id}/audio` if left open idle or too long, so a forgotten/backgrounded connection can't bill a real-time ASR vendor indefinitely — see `websocket_audio_session()` in `app.py` |

### Testing on your phone over WiFi

```bash
uvicorn app:app --host 0.0.0.0 --reload
```

Then open `http://<your-pc-local-ip>:8000/app` from your phone on the same network. Install it to your home screen (Chrome: menu → "Install app"; Safari: Share → "Add to Home Screen") for a full-screen, native-feeling launch.

### Native Android / iOS

`mobile/` is a real Capacitor project — an actual Android Studio/Gradle project and an actual Xcode project, both pointed at your deployed backend URL (`mobile/capacitor.config.ts`). See `docs/MOBILE_BUILD.md` for the full build guide. iOS specifically requires building on macOS with Xcode — there's no way around that platform requirement.

### Deploying

`render.yaml` is a ready-to-use [Render](https://render.com/) blueprint — connect the repo, set your real AWS (and other vendor) API keys in Render's dashboard (they're intentionally *not* committed to the blueprint), and it deploys as one FastAPI service serving both the API and the frontend, calling out to Polly and DynamoDB.

---

## 19. Future improvements

- Real-hardware Bluetooth pairing verification (code is written against `@capacitor-community/bluetooth-le`, untested on physical BLE hardware)
- Full offline degraded mode (bundled on-device ASR+MT model pair for common phrase pairs when there's no connectivity)
- Speaker diarization / voice embeddings with a real vendor (currently mocked; AssemblyAI's inline diarization is the planned first real source)
- Additional AWS options: Amazon Translate/Bedrock as further AWS-native pipeline stages, extending the same adapter pattern already used for Polly, DynamoDB, and Transcribe (`backend/agents/asr_transcribe.py`, `VOXBUDDY_ASR_PROVIDER=aws_transcribe`). Now configured and in active use against a real AWS account — but still has no automated test coverage against a mocked AWS backend the way Polly/DynamoDB do (`moto` doesn't support Transcribe's real-time streaming API; see §6.3/docs/AWS_INTEGRATION.md), so validate its actual transcription/diarization output against real speech before fully trusting it in production, the same caveat as the AssemblyAI adapter always carried.
- Expand the Polly voice map beyond the current 15 languages
- Cost-optimization pass on inference spend once real usage data exists (per the PRD's tracked NFR)
- Google Play Store publishing (guide ready in `docs/PLAY_STORE_PUBLISHING.md`)

---

## 20. Team

| Name | Role | GitHub | LinkedIn |
|---|---|---|---|
| Adarsh Kumar Singh | Frontend, Mobile & Application | [@Adarsh0414](https://github.com/Adarsh0414) | [@Adarsh_Singh](https://www.linkedin.com/in/adarsh-ks-tech14/) |
| Janvi Jaiswal | AI & Backend Systems | [@Janvi99852003](https://github.com/Janvi99852003) | [@Janvi_Jaiswal](https://www.linkedin.com/in/janvi-jaiswal-72415b307/) |

**Affiliation:** B.Tech CSE, VIT Bhopal
**Repo:** [github.com/Adarsh0414/VoxBuddy](https://github.com/Adarsh0414/VoxBuddy)
**Contact:** adarsh.ks.tech@gmail.com

---

## 21. AI tools used in this build

Disclosed per the hackathon's rules on AI tool use.

| Tool | Used for |
|---|---|
| **Claude** | Implementation support — scaffolding boilerplate (FastAPI routes, the DynamoDB adapter, Capacitor plugin wiring), debugging error traces, and drafting/iterating on this README and supporting docs |
| **ChatGPT** | Debugging support and a second opinion on trickier issues — streaming ASR edge cases, IAM policy scoping |
| **Manual debugging** | The majority of hands-on testing — anything touching real hardware (Bluetooth pairing, live mic input) and the CIE's signal-fusion behavior — was verified by hand against real audio and test scenarios, since this is the part no AI tool has context on |

**What AI tools did *not* do:** design the Conversation Intelligence Engine's partner-identification approach, choose the AWS services in §6, or write the CIE's test scenarios (`backend/cie/`). Those are our own decisions, implemented with AI tools speeding up the typing, not the thinking.
