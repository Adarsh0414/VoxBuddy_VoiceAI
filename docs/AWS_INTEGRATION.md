# VoxBuddy — AWS Integration Write-Up

## The problem

Traveling in a country where you don't speak the language is stressful in
exactly the moments you can least afford friction: haggling at a market
stall, asking a stranger for directions, checking into a guesthouse. Phone
translation apps require you to physically hand your phone back and forth,
which is slow, awkward, and signals distrust in a lot of cultures. What
people actually want is to have a normal, hands-free conversation while a
translator works invisibly in their ear.

The hard technical problem hiding inside that is not translation quality —
it's **figuring out who is talking to you in the first place**. A live
mic in a crowded market picks up the shopkeeper, the shopkeeper's kid, a
nearby vendor, and the user's own voice, all mixed together. Naively
translating everything produces a stream of garbage and gets it wrong the
moment a third party interrupts.

## What we built

VoxBuddy is a real-time, hands-free speech-translation app built around a
**Conversation Intelligence Engine (CIE)** — signal-fusion logic that
identifies the actual conversation partner from turn-taking patterns,
semantic coherence, and voice similarity, with hysteresis so it doesn't
flip-flop mid-conversation, explicit bystander rejection, and support for
multi-partner group conversations. This is the original, hand-built part
of the system (`backend/cie/`), fully unit-tested independent of any
vendor.

**Pipeline:** microphone audio → WebSocket → FastAPI backend → streaming
ASR → CIE (decides: partner / bystander / self) → context-aware
translation → text-to-speech → played back into the user's earbuds, with
every completed conversation persisted for a history/stats screen.

Every stage sits behind a small `Protocol` interface (`agents/base.py`),
selected at runtime by an environment variable via `agents/factory.py` —
so adding a new vendor for any stage is a new adapter file plus one
`if provider == "..."` branch, never a change to the orchestration logic
in `session/manager.py`.

## Exactly where AWS is used

Six AWS integrations exist. Five are pipeline stages backed by real AWS
services, each implementing the exact same interfaces the rest of the
system already depends on — genuine drop-in alternatives, not a
bolted-on demo path. The sixth (S3 calibration logging) isn't a
pipeline stage at all — it's an opt-in observability side-channel
hanging off the same CIE call site, covered last below.

### 0. Amazon Bedrock — translation (implemented, blocked at the AWS account level — Gemini is the active provider)

**File:** `backend/agents/translation_bedrock.py` (`BedrockTranslationAgent`)

Implements `TranslationAgent.translate(text, source_lang, target_lang,
context) -> TranslationResult` (same protocol as the existing
`AnthropicTranslationAgent` and `GeminiTranslationAgent`) using the
Bedrock Runtime **Converse API** —
`boto3.client("bedrock-runtime").converse(...)` — rather than
`invoke_model`, since Converse's request/response shape is
model-family-agnostic: pointing `BEDROCK_MODEL_ID` at a different model
family (Anthropic, Amazon Nova, Meta, etc.) needs no code change here.
Same context-injection prompt design as the Anthropic/Gemini adapters
(recent turn history resolves pronouns/ellipsis consistently, per FR-6 —
see docs/vendor_decision.md §2).

The code defaults `VOXBUDDY_TRANSLATION_PROVIDER` to `bedrock`, and
Bedrock reuses the same `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` /
`AWS_REGION` credentials already required for Polly, so it's genuinely
zero-extra-config on an AWS-backed deployment — **in principle.** In
practice, the deployed app overrides this default and runs with
`VOXBUDDY_TRANSLATION_PROVIDER=gemini`, because subscribing to the
underlying Bedrock model is blocked at the AWS account level, not by
anything in this codebase. Full account-side troubleshooting log:

1. `AccessDeniedException` — IAM user/role not authorized for the
   AWS Marketplace actions (`aws-marketplace:ViewSubscriptions`,
   `aws-marketplace:Subscribe`) Bedrock needs to complete a third-party
   model subscription. Fixed by adding those actions to
   `voxbuddy-backend-policy`.
2. Next error, reproduced directly in the Bedrock console Playground
   (so it's not specific to our IAM user): `AccessDeniedException:
   INVALID_PAYMENT_INSTRUMENT — a valid payment instrument must be
   provided`. The AWS account had zero payment methods on file. Added
   UPI AutoPay — didn't clear it, since AWS separately flagged the
   account as unverified for Marketplace purposes ("Add a valid credit
   card to verify the account"; UPI alone doesn't satisfy this). Added
   a RuPay debit card specifically to clear that verification step,
   and the "not verified" warning did go away.
3. Even with IAM fixed, UPI AutoPay and a verified RuPay card both on
   file, and account verification cleared, resubscribing to "Claude
   Sonnet 4.6 (Amazon Bedrock Edition)" (Product ID
   `prod-ffvjxvh4ltq64`) from **AWS Marketplace → Discover products**
   still auto-terminates the agreement instantly — service start and
   service end land on the identical timestamp, across five separate
   attempts (`agmt-26ke1lcobnbghjv2ax3qnnulj`,
   `agmt-2faci7i6mh8m4uu86kuv4iuvh`, `agmt-36zc6irds3x60xq0h5sb8qfwr`,
   and two more) — with no reason surfaced anywhere in the console:
   not on the subscribe confirmation, not on the agreement detail
   page, not in AWS's own follow-up email.

The account carries a live, AWS-confirmed $100 hackathon credit
(WeMakeDevs, credit ID `10067457025`) whose service list explicitly
includes Amazon Bedrock / Amazon Bedrock Service /
AmazonBedrockFoundationModels — so this isn't a spend-limit or
billing-eligibility problem either. Everything fixable from the console
has been fixed; what's left is only visible from AWS's side, so it's
filed as an AWS Support case (Billing/Marketplace category).

**Gemini is the provider actually driving the deployed app** — set via
`VOXBUDDY_TRANSLATION_PROVIDER=gemini` plus a `GEMINI_API_KEY`. It's the
same integration that was VoxBuddy's original, real-device-tested
translation path before the Bedrock adapter was added, so nothing about
the CIE, ASR, TTS, or persistence layers had to change to fall back to
it — swapping providers was a one-line env var change, not a rewrite,
because of the shared `TranslationAgent` protocol both adapters
implement. Provider selection is explicit and never falls back silently
mid-run: a misconfigured provider (missing credentials, model not
enabled) raises a clear error rather than quietly retrying against a
different one.

If the AWS Support case resolves, switching back is just
`VOXBUDDY_TRANSLATION_PROVIDER=bedrock` — the adapter,
IAM policy (`bedrock:InvokeModel` scoped to `BEDROCK_MODEL_ID`'s
resource ARN, plus the `aws-marketplace:*` actions from step 1 above),
and credentials are already in place and untouched. The chosen model
also needs to be enabled for the account/region under the Bedrock
console's "Model access" page; an un-enabled model returns an
`AccessDeniedException` at call time, not at startup.

### 1. Amazon Polly — text-to-speech

**File:** `backend/agents/tts_polly.py` (`PollyTTSAgent`)

Implements `TTSAgent.synthesize(text, target_lang) -> TTSResult` using
`boto3.client("polly").synthesize_speech(...)`. Selected by setting
`VOXBUDDY_TTS_PROVIDER=polly` (see `backend/agents/factory.py`), as a
direct alternative to the existing ElevenLabs adapter — same call sites
in `session/manager.py`, zero orchestration changes. Ships with a
language→voice map covering 15 languages using Polly's neural voices,
with an automatic fallback to the `standard` engine if a neural voice
isn't available in the configured region/account (mirroring the
self-healing pattern already used by the ElevenLabs adapter for its own
voice-catalog issue — see `PROGRESS.md`).

### 2. Amazon DynamoDB — conversation history

**File:** `backend/persistence_dynamodb.py`

Implements the exact same public function signatures and dataclasses as
the existing SQLite `persistence.py` (`save_conversation`,
`list_conversations`, `get_conversation`, `get_summary_stats`,
`get_language_breakdown`, `get_day_streak`, `delete_all`) against three
DynamoDB tables (conversations, turns, an atomic-counter table for
sequential conversation ids). Selected by setting
`VOXBUDDY_PERSISTENCE_PROVIDER=dynamodb`, routed through
`backend/persistence_store.py`, a one-file backend selector that `app.py`
imports instead of `persistence.py` directly — so the entire
history/profile/stats feature set (every screen backed by conversation
data) runs on AWS with a single environment variable change and no code
changes to `app.py`'s route handlers.

### 3. Amazon Transcribe — real-time speech-to-text

**File:** `backend/agents/asr_transcribe.py` (`AmazonTranscribeStreamingASRAgent`)

Implements the same `StreamingASRAgent` protocol as
`agents/mock_streaming_asr.py` and the existing AssemblyAI adapter, using
the `amazon-transcribe` (awslabs) async streaming SDK. Selected by setting
`VOXBUDDY_ASR_PROVIDER=aws_transcribe`, as a direct alternative to
AssemblyAI — same call sites in `session/streaming_manager.py`, zero
orchestration changes. Needs the `transcribe:StartStreamTranscription`
IAM permission (see the "Currently applied" policy under Setup below)
in addition to the credentials already required for Polly/DynamoDB.

Per-word diarization (Transcribe's `Item.speaker`, one per word) is
resolved down to one speaker label per final result via majority vote
across that result's items (`_majority_speaker`), matching the
per-utterance granularity `StreamingASRResult` expects elsewhere in the
pipeline.

**Testing caveat, stated plainly:** unlike Polly and DynamoDB, `moto`
does not mock Transcribe's real-time streaming API, so this adapter has
no automated test coverage against a fake AWS backend the way the other
three integrations do — `backend/tests/test_app_audio_ws.py` and
`test_streaming_session.py` exercise the shared `StreamingASRAgent`
protocol via `MockStreamingASRAgent` instead, which proves the pipeline
wiring but not Transcribe's actual wire behavior. Validate this against
a real AWS account (real speech, real network conditions, real
diarization output) before relying on it in production — treat it the
same as the AssemblyAI adapter's own "scaffolded, not fully vendor
battle-tested" caveat.

### 4. Amazon CloudWatch — custom metrics for the CIE

**File:** `backend/cie/cloudwatch_metrics.py`

`backend/cie/eval.py` already computes exactly the numbers a judge or a
PM would ask about the decision engine — per-utterance confidence and
latency, plus once-per-run speaker-id accuracy, false-speaker rate,
bystander-rejection rate, and speaker-switch accuracy — and used to only
print them. `cloudwatch_metrics.py` gives those same numbers a real
destination: `publish_decision_metrics(...)` sends one CloudWatch data
point per CIE decision (`Confidence`, `LatencyMs`, `BystanderRejected`,
`PartnerSwitch`), and `publish_eval_summary(...)` sends the aggregate
report once per run, both to the `VoxBuddy/CIE` namespace. `eval.py`
calls both automatically; nothing else changes. Off by default — set
`VOXBUDDY_CLOUDWATCH_METRICS=true` to turn it on, using the same AWS
credentials as Polly/DynamoDB above.

The publish functions take a `source` dimension (`"eval"` vs `"live"`) by
design: `session/manager.py` can call `publish_decision_metrics(...)`
with `source="live"` from the real audio pipeline with zero changes to
this module, so the same CloudWatch dashboard built against eval runs
becomes the production dashboard once real traffic is flowing — this is
the "we monitor our AI decision engine in production" story, not a
separate thing that needs building later.

### 5. Amazon S3 — opt-in calibration data flywheel

**Files:** `backend/cie/calibration_log.py` (logging), `backend/cie/calibration.py`
(download/summarize/export)

This directly targets a gap the project has documented against itself
since Phase 2 (see `PROGRESS.md`, "What's still genuinely open"): the
CIE's signal-fusion weights (`WEIGHT_VOICE_SIMILARITY` /
`WEIGHT_TURN_TAKING` / `WEIGHT_SEMANTIC_COHERENCE` in `cie/engine.py`)
are hand-set, not tuned against real recorded multi-speaker audio —
because there was never any real data to tune against. This doesn't fix
that (there's still no labeled data, and nothing here fabricates any),
but it builds the missing pipe: `session/manager.py`'s single CIE call
site now optionally logs the CIE's already-computed signal breakdown
(voice similarity, turn-taking score, semantic coherence score, the
weights/thresholds actually applied, the resulting role and confidence)
to S3 as one small JSON object per decision. Anonymized by construction,
not by redaction — the logger's input type doesn't carry the raw voice
embedding, speaker id, session id, or any transcript text in the first
place, so there's nothing sensitive to accidentally forget to strip.

Off by default; set `VOXBUDDY_CALIBRATION_LOGGING=true` and
`VOXBUDDY_CALIBRATION_BUCKET=<bucket>` to turn it on. `cie/calibration.py`
is the offline half: `python -m cie.calibration --summary` pulls logged
tuples back and prints per-role signal statistics (a sanity check on
real-world distributions against the hand-set thresholds, useful the
moment any data exists at all); `--export out.csv` writes a CSV with one
blank `decision_correct` column for a human reviewer to fill in. That
column — "was this call right?" — is the realistic ground truth here:
anonymized signal tuples can't be reverse-mapped to who actually spoke,
but a reviewer (or, eventually, an in-app "that was wrong" tap) can
judge the outcome. Fitting new weights against enough labeled rows is a
genuinely separate follow-up this PR intentionally doesn't attempt —
there's no labeled data yet to build that script against.

Consent model: this is a single operator-wide opt-in appropriate for a
PoC with no real production user base, not a per-user toggle. The PRD's
existing Privacy Agent / `ConsentRecord` data model
(`docs/VoxBuddy_PRD_and_Architecture.md`) already has the right shape for
a real per-user consent flag later; `calibration_log.enabled()` is
exactly where that check would go once that field exists.

### Why these five integrations

All five are genuinely useful, not just "AWS somewhere in the codebase":
Polly replaces a paid third-party vendor (ElevenLabs) with an AWS-native
option that has no separate account/billing relationship if you're
already on AWS; Transcribe does the same for AssemblyAI on the ASR side;
DynamoDB replaces a single-file SQLite database with a managed,
horizontally-scalable store appropriate for a real multi-user
deployment; CloudWatch turns metrics the CIE was already computing into
something an ops dashboard or an alarm can actually watch; S3 gives the
CIE's own documented weakness (hand-set fusion weights) a real path to
getting fixed instead of staying a permanent bullet point. All five are
the natural production upgrade path this project was already going to
need.

## Testing

Bedrock, Polly, DynamoDB, CloudWatch, and S3 have real test coverage
using mocked clients / `moto` (AWS's official mocking library — no real
AWS calls, no cost, no credentials needed to run the suite). Bedrock's
`converse` API isn't covered by `moto`, so
`backend/tests/test_translation_bedrock.py` injects a `MagicMock` client
directly (the same pattern `test_translation_gemini.py` and
`test_translation_anthropic.py` already use for their respective SDKs)
rather than relying on `moto`. Transcribe does not have automated
AWS-backend coverage at all — see the
testing caveat under "Amazon Transcribe" above; its automated coverage
is limited to the shared `StreamingASRAgent` protocol via
`MockStreamingASRAgent`, exercised in `test_app_audio_ws.py` and
`test_streaming_session.py`.

- `backend/tests/test_translation_bedrock.py` — clean-text/confidence
  parsing from a mocked Converse response, context injection into the
  prompt, configurable model id, region defaulting, and factory-level
  provider-selection tests (Bedrock is the default, missing AWS
  credentials raise clearly, explicit Gemini selection still works, and a
  misconfigured Bedrock never silently falls back to Gemini).
- `backend/tests/test_tts_polly.py` — 4 tests: real audio bytes returned,
  every mapped language synthesizes, unmapped languages fall back
  correctly, and the neural→standard engine fallback actually triggers.
- `backend/tests/test_persistence_dynamodb.py` — 7 tests: save/retrieve,
  empty-session handling, user-scoped listing, id sequencing, delete-all,
  ownership lookup, and stats/language-breakdown aggregation — mirroring
  every scenario the existing SQLite test suite (`test_persistence.py`)
  already covers, so both backends are proven to satisfy the same
  contract `app.py` depends on.
- `backend/tests/test_cie_cloudwatch_metrics.py` — verifies the feature
  is a true no-op (no boto3 import, no error) when
  `VOXBUDDY_CLOUDWATCH_METRICS` is unset, and that enabling it actually
  delivers per-decision and summary data points into a moto-mocked
  CloudWatch, readable back via `list_metrics`/`get_metric_statistics`.
- `backend/tests/test_cie_calibration_log.py` — verifies logging is a
  true no-op when disabled (including that `SessionManager` never
  touches S3 during a normal run), that enabling it writes real,
  anonymized JSON objects into a moto-mocked bucket, and — directly
  checking the anonymization claim — that no logged record ever contains
  a speaker embedding, speaker id, or transcript text.
- `backend/tests/test_cie_calibration.py` — `download_signal_tuples`,
  `summarize_signal_distribution`, and `export_for_labeling` each
  exercised against a moto-mocked bucket seeded with known records.

Running the Polly test suite against the real AWS API for the first time
caught a real bug before it ever reached production: the French voice was
initially mapped to `"Lea"` instead of Polly's actual voice id `"Léa"` —
exactly the kind of vendor-catalog mismatch this project's own
`PROGRESS.md` has already hit twice with other vendors.

## Setup

See `backend/.env.example` for the exact environment variables
(`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_REGION`,
`BEDROCK_MODEL_ID`, `DYNAMODB_TABLE_PREFIX`, `VOXBUDDY_CLOUDWATCH_METRICS`,
`VOXBUDDY_CLOUDWATCH_NAMESPACE`, `VOXBUDDY_CALIBRATION_LOGGING`,
`VOXBUDDY_CALIBRATION_BUCKET`, `VOXBUDDY_CALIBRATION_PREFIX`).

**Currently applied** — the actual `voxbuddy-backend-policy` attached to
the deployed IAM user *does* include the full Bedrock statement set
below, alongside Polly, Transcribe, and DynamoDB (see `README.md` §6.5
for the single authoritative copy with `Sid`s). Having this policy
correct turned out to be necessary but not sufficient — per §0 above,
the actual block on `VOXBUDDY_TRANSLATION_PROVIDER=bedrock` sits on
AWS's Marketplace/billing side, not in IAM, which this policy alone
doesn't fix.

The default model, `global.anthropic.claude-sonnet-4-6`, is only
invocable through a Bedrock **global cross-region inference profile**,
not a bare foundation-model id (the previous default,
`anthropic.claude-3-5-haiku-20241022-v1:0`, was retired by AWS —
Converse now returns `ResourceNotFoundException` for it). Unlike a
foundation-model ARN, an inference-profile ARN **includes your AWS
account id**, and because this model needs cross-region inference,
`bedrock:InvokeModel` needs to be granted against *both* the
inference-profile ARN and the foundation-model ARN (regional and
global/unspecified), the latter two gated by a matching
`bedrock:InferenceProfileArn` condition — a single statement against
just one of these ARNs is not enough:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "VoxBuddyBedrockMarketplace",
      "Effect": "Allow",
      "Action": ["aws-marketplace:ViewSubscriptions", "aws-marketplace:Subscribe", "aws-marketplace:Unsubscribe"],
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
      "Condition": { "StringEquals": { "aws:RequestedRegion": "us-east-1" } }
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
    { "Sid": "VoxBuddyPolly", "Effect": "Allow", "Action": ["polly:SynthesizeSpeech", "polly:DescribeVoices"], "Resource": "*" },
    { "Sid": "VoxBuddyTranscribe", "Effect": "Allow", "Action": ["transcribe:StartStreamTranscription", "transcribe:StartStreamTranscriptionWebSocket"], "Resource": "*" },
    { "Sid": "VoxBuddyDynamoDBList", "Effect": "Allow", "Action": ["dynamodb:ListTables"], "Resource": "*" },
    {
      "Sid": "VoxBuddyDynamoDBTables",
      "Effect": "Allow",
      "Action": ["dynamodb:CreateTable", "dynamodb:DescribeTable", "dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem", "dynamodb:Query", "dynamodb:Scan"],
      "Resource": "arn:aws:dynamodb:us-east-1:<YOUR_ACCOUNT_ID>:table/voxbuddy_*"
    }
  ]
}
```

The `bedrock:InvokeModel` resource ARNs must match `BEDROCK_MODEL_ID`
exactly — changing `BEDROCK_MODEL_ID` or `AWS_REGION` means updating
all three Bedrock statements' ARNs, not just one. Note that a *global*
inference profile still uses a real AWS region in its ARN (the region
you call the Bedrock Runtime endpoint in, e.g. `us-east-1`) —
"global" describes where the request may be routed for inference, not
the ARN's region segment. Some model families
use a bare foundation-model ARN (`.../foundation-model/<id>`, no account
id) instead of an inference-profile ARN — check the specific model's
page in the Bedrock console if `InvokeModel` returns a
`ValidationException` mentioning on-demand throughput, or an
`AccessDeniedException` naming a different ARN shape than the one
granted.

Only add the `transcribe:...` statement if you're actually setting
`VOXBUDDY_ASR_PROVIDER=aws_transcribe` — without it, that provider's
`start()` call fails with an `AccessDeniedException` the first time real
audio is streamed to it, not at startup. The DynamoDB resource ARN needs
your real account ID between the two colons — a blank segment
(`us-east-1::table/...`) won't match any real table ARN and silently
breaks every DynamoDB call.

**Not yet granted — only needed if you turn on CloudWatch metrics or S3
calibration logging** (both off by default; enabling either without
these will fail with `AccessDenied`):

```json
{
  "Version": "2012-10-17",
  "Statement": [
    { "Effect": "Allow", "Action": "cloudwatch:PutMetricData", "Resource": "*" },
    {
      "Effect": "Allow",
      "Action": ["s3:PutObject", "s3:GetObject", "s3:ListBucket"],
      "Resource": [
        "arn:aws:s3:::your-calibration-bucket",
        "arn:aws:s3:::your-calibration-bucket/*"
      ]
    }
  ]
}
```

(CloudWatch's `PutMetricData` API doesn't support resource-level
restriction — `"Resource": "*"` is correct/required, not an over-broad
grant. The S3 statement should be scoped to whichever real bucket name
you set as `VOXBUDDY_CALIBRATION_BUCKET`.)


## License / attribution

`boto3` and `moto` are used under the Apache 2.0 license (AWS SDK for
Python; AWS mocking library). No other third-party code was copied into
the AWS integration files — `translation_bedrock.py`, `tts_polly.py`,
`persistence_dynamodb.py`, `cie/cloudwatch_metrics.py`,
`cie/calibration_log.py`, and `cie/calibration.py` are all original code
written for this project, following the interface contracts already
defined in `agents/base.py` and `persistence.py`.
