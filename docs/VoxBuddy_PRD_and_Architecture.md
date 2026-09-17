# VoxBuddy
### Product Requirements Document & System Architecture — v1.0
*"Voice to Voice, Heart to Heart, Bringing Worlds Together."*

Owner: Founding Engineering (this document)
Status: Living spec — Phases 1–4 substantially implemented; see `PROGRESS.md` for the up-to-date,
honestly-tracked status of every item below (what's real, tested, mocked, or not started).
Scope: End-to-end product, AI, and infrastructure specification for a production-ready v1

---

## 1. Executive Summary

VoxBuddy is an AI communication platform, not a translation app. Two people who don't share a language wear their own Bluetooth earbuds, speak naturally to each other, and the phone — sitting untouched in a pocket — does everything necessary to make the conversation feel like it's happening in one shared language.

The product succeeds or fails on one axis: **does the technology disappear?** Every requirement in this document is subordinate to that goal. Where a technically simpler path would require the user to look at, hold, or touch the phone, that path is rejected regardless of accuracy or cost benefit.

The hard engineering problem is not machine translation — MT/ASR/TTS are commodity-adjacent in 2026. The hard problem is **automatic conversation understanding**: knowing who is talking to whom, in a crowded, noisy, multi-speaker real world, continuously, with no user confirmation.

---

## 2. Product Philosophy (Non-Negotiable Constraints)

These are architectural constraints, not preferences. Every phase must be checked against them.

1. No manual conversation start/stop.
2. No push-to-talk, no tap-to-talk.
3. No manual speaker/partner selection.
4. No requirement to hold phone near mouth or partner.
5. No requirement to read translated text during a live conversation (text is a fallback/accessibility surface only).
6. No repeated manual language selection — language is inferred and re-inferred continuously.
7. Phone remains in pocket/bag; earbuds are the only interface.
8. If an engineering shortcut requires breaking 1–7, it is rejected — a fallback UX is designed instead (see §6.5).

---

## 3. Target Users & Core Use Cases

| User type | Use case | Environment |
|---|---|---|
| Traveler | Ordering food, asking directions, negotiating price | Airports, markets, restaurants, hotels |
| Local shopkeeper/vendor | Serving international customers | Markets, retail, tourist zones |
| Healthcare worker | Patient communication | Hospitals, clinics |
| Business traveler | Meetings, small talk | Offices, conferences |
| Student/teacher | Cross-language classroom support | Classrooms |
| Two friends/family, mixed language household | Everyday conversation | Homes |

Common thread: **spontaneous, unplanned, two-party spoken conversation** in acoustically imperfect, socially normal environments.

---

## 4. Core User Experience Flow

1. Both users install VoxBuddy, grant mic + Bluetooth permissions once, set their spoken language once (auto-detected thereafter, this is just a bootstrap default).
2. Both connect their own Bluetooth earbuds (standard OS pairing — no proprietary hardware in v1).
3. Users open the app once and put the phone away. The app runs a foreground session (Android foreground service / iOS background audio mode).
4. User A speaks. Their earbud mic captures audio → phone → Conversation Intelligence Engine (CIE) determines this is "primary speaker, session owner."
5. When a second, consistent voice is detected addressing the primary speaker (proximity + turn-taking pattern + acoustic cues), the CIE promotes it to "active conversation partner."
6. Speech from the partner is captured (via the *primary* user's phone mic acting as ambient pickup, since the partner is not required to install anything for a one-sided flow — see §4.1), translated, and played into the primary user's earbuds. If the partner also runs VoxBuddy on their own phone, the pipeline runs symmetrically and both sides get earbud playback of the other's translated speech in their own voice's target language.
7. Conversation continues with zero taps. If a third-party voice is detected nearby (not the established partner), the CIE ignores it (see §7 — Conversation Intelligence).
8. If the partner changes (A turns to a new person), the CIE detects the discontinuity and re-establishes a new partner profile automatically, with brief graceful degradation (see §7.6 Recovery).

### 4.1 Symmetric vs. Asymmetric Mode
This is a product decision with architectural consequences, so it's stated explicitly:

- **Symmetric mode (ideal):** both parties have VoxBuddy + earbuds. Each phone runs its own capture/CIE/playback pipeline for its own user, and the two sessions synchronize over a shared **Conversation Session** on the backend (so context, turn state, and partner identity are shared, not independently re-derived twice).
- **Asymmetric mode (v1 must support this — it's the realistic first-contact scenario):** only the initiating user has the app. Their phone's mic captures *both* voices (own, via earbud mic which has better SNR/isolation, and the partner's, via phone's ambient mic). The phone plays back translated partner speech into the initiating user's earbuds only; the partner just hears the initiating user's natural voice (untranslated) or, if a speaker/phone-out playback fallback is enabled, a synthesized translation is played aloud from the phone speaker for the partner (this is the one permitted moment the phone may need to be visible — treated as a deliberate, minimal-friction fallback, not the core path).

Asymmetric mode is what makes viral, zero-coordination adoption possible (the shopkeeper doesn't need to have installed anything). It's listed here because it changes the audio architecture (§8) and the CIE's job (§7) significantly, and I'm flagging it now rather than discovering it mid-build.

---

## 5. Functional Requirements

**FR-1** Continuous ambient audio capture from paired Bluetooth earbud mic and/or phone mic, with user-controlled always-on vs. push-to-start-session (session start is a one-time app action, not per-utterance).
**FR-2** Real-time noise suppression and voice activity detection (VAD).
**FR-3** Real-time speaker diarization (who is speaking, is it a known voice).
**FR-4** Automatic spoken language identification per utterance.
**FR-5** Streaming ASR (speech-to-text) with partial + final hypotheses.
**FR-6** Context-aware machine translation (uses conversation history, not just sentence-level MT).
**FR-7** Streaming, low-latency TTS in a natural voice, target language.
**FR-8** Conversation partner identification and tracking across turns.
**FR-9** Automatic conversation partner switching detection.
**FR-10** Confidence-scored decision making at every stage, with automatic fallback behavior (never a user-facing error dead-end).
**FR-11** Session persistence: conversation can survive brief connectivity loss, earbud disconnect/reconnect.
**FR-12** Accessibility surface: optional live transcript view for deaf/hard-of-hearing users, screen-reader compatible UI, haptic session-state feedback.
**FR-13** User profile: preferred language, voice preference for TTS, privacy settings.
**FR-14** Conversation history (opt-in, encrypted, user-deletable) for context continuity across a multi-day interaction (e.g., recurring business meetings).
**FR-15** Offline degraded mode: on-device small ASR+MT model pair for common phrase pairs when no connectivity (quality-degraded, clearly a fallback, still zero-tap).

---

## 6. Non-Functional Requirements

| Category | Requirement |
|---|---|
| Latency | ≤ 2.0s perceived end-to-end (speech-end to translated-speech-start) target for v1; ≤ 1.2s target for v2. This is the single most important NFR — above ~2.5s the "invisible" illusion breaks and it feels like a walkie-talkie. |
| Availability | 99.9% for core translation path |
| Scalability | Horizontally scalable per-session; must support regional edge deployment to hit latency targets globally |
| Privacy | Audio not retained beyond session unless user opts into history; on-device VAD/wake-detection where feasible to minimize raw audio egress |
| Security | End-to-end encrypted audio transport; no third-party ad SDKs; least-privilege cloud IAM |
| Accessibility | WCAG 2.2 AA for all visual surfaces |
| Battery | Target < 10%/hour drain in active conversation mode on a mid-range 2024+ device |
| Cost | Cloud AI inference cost per conversation-minute must be tracked and optimized as a first-class metric, not an afterthought |

### 6.5 Fallback UX (still zero/near-zero friction)
- Wrong-partner lock-on → CIE self-corrects within ~1–2 turns using acoustic + semantic mismatch signals; no user action.
- No connectivity → offline on-device model, small vocabulary, clear (audio-cue, not visual-only) indication of degraded mode.
- Earbud disconnect → automatic fallback to phone speaker/mic with a spoken (not text) notification: *"Switched to phone speaker."*

---

## 7. Conversation Intelligence Engine (CIE) — The Core IP

The CIE is the orchestration brain. It does not do ASR/MT/TTS itself — it consumes structured signals from every agent (§8) and maintains the authoritative state of "what is this conversation, right now."

### 7.1 State Model
The CIE maintains a per-session **Conversation State Graph**:
- `Speakers[]` — each with a rolling voice-embedding fingerprint, role (self / partner / bystander), confidence, first-seen/last-seen timestamps.
- `ActivePartners` — a small, capped set of `Speakers[]` entries treated as "people I'm actively talking to" (not a single pointer — see the v1.1 note below), each with its own confidence score.
- `TurnHistory` — ring buffer of last N turns (speaker, text, language, timestamp) for context-aware MT and for partner-switch detection.
- `ConversationTopic/Context` — a rolling semantic summary (embedding + short natural-language gist) used to bias ASR/MT disambiguation (e.g., proper nouns, domain terms).
- `EnvironmentProfile` — noise class, estimated number of distinct voices nearby, venue type if inferable.

### 7.2 Partner Identification Signals (fused, not any single one authoritative)
- **Acoustic proximity**: signal energy / SNR pattern consistent with "facing the mic."
- **Turn-taking pattern**: alternation matching a two-party dialogue rhythm vs. multiple overlapping voices.
- **Voice consistency**: same speaker embedding recurring across turns.
- **Semantic coherence**: replies are topically/contextually responsive to the prior turn (cheap check via the context-memory agent, not a full dialogue model).
- **Directionality** (where hardware supports stereo/beamforming earbuds): angle-of-arrival as a soft signal, never a hard requirement (must degrade gracefully on mono earbuds).

Each signal contributes a weighted vote; CIE outputs a per-speaker confidence score every turn. Below a threshold, CIE holds its current hypothesis rather than flapping (hysteresis is deliberate — instability is worse than a brief wrong guess).

**v1.1 update — multi-partner conversation groups (implemented and tested in the Phase 1 codebase, not just specified here):** the original single-`ActivePartner` model was product-incorrect for a large share of real scenarios — a couple at a market stall, two colleagues in a meeting — where more than one person legitimately talks to the user. The CIE now tracks a small capped group (2 concurrent partners in v1) instead of one slot:
- A confident new voice joins a free slot directly (additive — it does not displace anyone).
- Once the group is full, a new voice can only take a slot whose occupant has been silent past the absence timeout, using the same fast-track/confirmation hysteresis described above.
- A member silent for much longer (a "prune" timeout, longer than the replace-eligibility timeout) is dropped outright, freeing their slot rather than blocking it indefinitely for someone who has genuinely left.
- The group cap of 2 is a deliberate v1 scope limit, not a hard architectural ceiling — supporting larger ad hoc groups (e.g. a family, a meeting table) is flagged as future work once field data shows it's needed (see the open research questions).

### 7.3 Ignoring Bystanders
Any voice that fails the turn-taking + semantic-coherence pattern is tagged `bystander` and excluded from the translation pipeline entirely — not translated, not shown, not stored beyond the rolling diarization buffer needed to keep *not* re-evaluating it every frame.

### 7.4 Partner Switching
Detected when: a new voice embedding appears, old partner voice absent for > N seconds, AND new voice passes the same partner-identification bar. CIE closes the old partner context (soft, not deleted — resumable if they return within a session-timeout window) and opens a new one. A very brief (sub-100ms, non-blocking) audio earcon may signal the switch to the user — deliberately minimal, not an interruption.

### 7.5 Confidence & Escalation
Every output (ASR hypothesis, translation, TTS) carries a confidence score. Low-confidence translations are *never* silently suppressed (that breaks the "conversation flows" promise) — instead the system:
1. Prefers a safe, slightly more literal translation over a risky idiomatic one when confidence is low.
2. Logs the low-confidence event for model improvement.
3. Never asks the user to confirm mid-conversation (violates philosophy) — it only surfaces confidence in the optional accessibility transcript.

### 7.6 Recovery from Misattribution
If CIE later determines (via semantic incoherence across several turns) that it locked onto the wrong partner, it re-opens partner search *without announcing an "error"* — it simply starts weighting the correct voice higher and transitions within 1–2 turns. This is a probabilistic system that self-heals, not a state machine that "fails."

---

## 8. Multi-Agent AI Architecture

All agents are services (not literal chat "agents" in the LLM-persona sense) communicating over a low-latency internal bus (gRPC streaming + a lightweight pub/sub for state events). The CIE is the consumer/orchestrator of all of them.

| Agent | Responsibility | Key techniques |
|---|---|---|
| **Environmental Intelligence Agent** | Classify venue/acoustic environment (street, restaurant, airport, quiet room) | Lightweight on-device audio scene classifier |
| **Noise Intelligence Agent** | Real-time denoising, echo cancellation, wind/handling-noise suppression | On-device DSP + learned denoiser (e.g. RNNoise-class model, on-device); cloud-side heavier denoiser as fallback for asymmetric-mode phone-mic capture |
| **Audio Processing Agent** | VAD, endpointing, audio chunking/framing, resampling, buffering for streaming | On-device VAD (WebRTC VAD / Silero-class), streaming framer |
| **Speaker Identification Agent** | Voice embeddings, diarization, per-speaker fingerprint tracking within a session | Streaming speaker-embedding model (d-vector/x-vector class), session-scoped only (no persistent biometric storage by default — privacy) |
| **Conversation Intelligence Agent (CIE)** | Central orchestration — see §7 | Rule+ML hybrid fusion, hysteresis state machine |
| **Context Memory Agent** | Maintains rolling semantic summary, resolves pronouns/ellipsis, supplies domain-term bias to ASR/MT | Lightweight rolling summarizer, short-context embedding store |
| **Language Detection Agent** | Per-utterance spoken-language ID, confidence-scored, updates continuously (users can code-switch) | Streaming LID model |
| **Translation Agent** | Context-aware MT, using TurnHistory + ConversationTopic as conditioning, not sentence-isolated MT | Streaming/incremental NMT (foundation MT API augmented with context injection) |
| **Speech Synthesis Agent** | Low-latency, natural-sounding streaming TTS in target language, optionally voice-matched | Streaming neural TTS |
| **Accessibility Agent** | Drives transcript view, haptics, screen-reader hooks, caption timing | Consumes CIE + Translation Agent outputs |
| **Privacy Agent** | Enforces retention policy, redaction, consent state, on-device vs. cloud routing decisions | Policy engine, not ML |
| **Analytics Agent** | Latency, confidence, error, and cost telemetry — product + engineering metrics | Event pipeline |
| **Synchronization Agent** | Keeps symmetric-mode dual sessions (both users' phones) in a shared conversation state, handles clock skew, reconnect/resume | Session state sync over the backend Conversation Session service |

Data flow (simplified):
```
Earbud/Phone Mic
   → Audio Processing Agent (VAD/framing)
   → Noise Intelligence Agent (denoise)
   → [parallel] Speaker ID Agent + Language Detection Agent
   → Context Memory Agent (bias signals)
   → Translation pipeline: ASR → Translation Agent → Speech Synthesis Agent
   → Earbud playback

   All agents also emit structured events → CIE (state fusion) → Conversation State Graph
   CIE decisions (who is partner, ignore/include) gate what actually reaches the Translation pipeline
```

---

## 9. System Architecture — High Level

```
┌─────────────────────────────┐        ┌─────────────────────────────┐
│   Mobile App (Android/iOS)  │        │   Mobile App (Android/iOS)  │
│   User A                    │        │   User B (symmetric mode)   │
│  - Bluetooth audio I/O      │        │  - Bluetooth audio I/O      │
│  - On-device VAD/denoise    │        │  - On-device VAD/denoise    │
│  - Local CIE-lite (fast     │        │  - Local CIE-lite           │
│    partner tracking cache)  │        │                              │
└──────────────┬───────────────┘        └──────────────┬───────────────┘
               │  gRPC bidi-stream (audio + control)     │
               ▼                                          ▼
        ┌───────────────────────────────────────────────────────┐
        │             Edge/Regional Gateway (per-region)         │
        │   - session auth, routing, TLS termination             │
        └───────────────────────────┬─────────────────────────────┘
                                     ▼
        ┌───────────────────────────────────────────────────────┐
        │                  Conversation Session Service           │
        │   - Conversation State Graph (authoritative)            │
        │   - Synchronization Agent                                │
        └───────────────────────────┬─────────────────────────────┘
                                     ▼
        ┌───────────────────────────────────────────────────────┐
        │                  AI Orchestration Layer (CIE)            │
        │   fan-out to: Speaker ID / LID / Context Memory /         │
        │   Noise Intelligence → gated → Translation pipeline       │
        └───────────────────────────┬─────────────────────────────┘
                                     ▼
        ┌───────────────────────────────────────────────────────┐
        │        Streaming ASR → Translation → TTS pipeline        │
        │        (managed AI inference, autoscaled, GPU pool)      │
        └───────────────────────────┬─────────────────────────────┘
                                     ▼
                     Translated audio stream → back to
                     recipient's phone → earbuds

  Supporting platform services (not in the hot audio path):
  Auth Service | User/Profile Service | Privacy/Consent Service |
  Analytics/Telemetry Pipeline | Billing (future) | Admin/Ops Console
```

Design principle: **the hot path (audio in → translated audio out) is a pure streaming pipeline with no synchronous calls to "cold" services** (auth, billing, profile) after session establishment — those are resolved once at session start and cached.

---

## 10. Audio & Bluetooth Architecture

- **Capture:** Bluetooth HFP/HSP mic input (mono, 16kHz typical) from earbuds; fallback to phone mic. Android: `AudioRecord` via a foreground `Service` with `MICROPHONE` + `bluetooth` runtime permissions, `AudioManager` routed to `MODE_IN_COMMUNICATION` with SCO audio for earbud mic access. iOS: `AVAudioSession` category `.playAndRecord` with `.allowBluetooth`, background mode `audio`.
- **Framing:** 20–30ms frames, streamed continuously to the on-device VAD; only speech-active segments are forwarded upstream (bandwidth + privacy + cost).
- **Transport:** Opus-encoded audio over a bidirectional gRPC stream (low overhead, good compression at speech bitrates, wide platform support), TLS 1.3.
- **Playback:** Translated audio streamed back and played through the same Bluetooth route with jitter-buffer smoothing (~150–300ms adaptive buffer) to protect against network variance without materially harming the latency budget.
- **Earbud disconnect handling:** OS-level route-change notifications trigger the fallback path in §6.5.
- **Asymmetric-mode dual-voice capture:** phone's built-in mic array (when available) used with beamforming/AGC tuned differently than the earbud path (further-field, more reverberant) — this is a materially different acoustic problem from near-field earbud capture and is treated as a separate tuning profile in the Noise Intelligence Agent, not the same model.

---

## 11. Mobile App Architecture

- **Platforms:** Native Android (Kotlin) and iOS (Swift) for v1 — audio/Bluetooth reliability at this latency budget is not something to risk on a cross-platform audio stack in v1. (A shared design system and API contract keep the two in lockstep; code is not shared, intent is.)
- **Architecture pattern:** MVVM + a dedicated low-level `AudioEngine` module isolated from UI, since audio threading requirements (real-time, no GC pauses/jank) are stricter than typical app code.
- **Local CIE-lite:** a thin on-device cache of "current partner voice embedding + confidence" so the app can make instant local gating decisions (ignore obvious bystanders) without a round trip, while the authoritative CIE state lives server-side and reconciles asynchronously.
- **Offline model:** bundled small ASR+MT model (e.g., quantized on-device model pair) for the top N language pairs, used only in FR-15 degraded mode.
- **UI:** a near-empty "conversation active" screen (waveform/ambient visual only, no controls needed mid-conversation), a one-time setup flow, and an optional transcript/accessibility view reachable but never required.

---

## 12. Backend Architecture

- **Language/runtime:** Go for the low-latency streaming gateway and Conversation Session Service (predictable GC, excellent gRPC/streaming support, strong concurrency model for many long-lived bidi streams). Python for the AI orchestration layer and any custom model-serving glue (ecosystem fit), behind the same gRPC contracts.
- **Orchestration layer:** CIE and the agent fan-out implemented as a set of independently scalable microservices communicating over gRPC streaming + an event bus (e.g., a Kafka-class log) for state events that multiple consumers (Analytics, Accessibility, Privacy) need without coupling to the hot path.
- **AI inference:** managed/hosted foundation models for ASR, MT, TTS (build vs. buy: buy first for v1 to hit quality/latency bar fast; invest in custom fine-tuning only where the CIE's conversation-specific signals — speaker ID, partner tracking — aren't well served by any off-the-shelf API, which is precisely the multi-agent architecture in §8).
- **Session service:** owns the Conversation State Graph, backed by an in-memory store (Redis) for hot session state + async persistence to a durable store for opted-in history.
- **API Gateway:** regional edge deployment (multiple regions) purely to keep RTT within the latency budget — this is a latency-driven infra requirement, not a nice-to-have.

---

## 13. Data Model (Core Entities)

- `User` — id, preferred language(s), TTS voice preference, consent flags, created_at
- `Device` — id, user_id, platform, push token, last_seen
- `ConversationSession` — id, participants[] (1 or 2 known users + anonymous partner slots), mode (symmetric/asymmetric), started_at, ended_at, region
- `Speaker` — session-scoped, embedding_ref (session-lifetime only by default), role, confidence
- `Turn` — session_id, speaker_ref, source_lang, source_text (opt-in retention only), target_lang, target_text (opt-in retention only), timestamps, confidence scores
- `ConsentRecord` — user_id, what's retained (audio/text/none), retention window
- `Telemetry Event` — latency breakdown per turn, error/fallback events, cost per turn

Default posture: **Turn-level text/audio is NOT retained** unless the user opts into "conversation history." Speaker embeddings are session-scoped and discarded at session end unless the user has an explicit saved-contact feature (future).

---

## 14. Security & Privacy

- TLS 1.3 everywhere; mutual TLS between internal services.
- Audio is processed in-memory in the hot path; not written to disk unless a user has opted into history, and even then encrypted at rest (AES-256) with per-user keys.
- On-device VAD/denoise ensures only actual speech segments (not ambient room audio generally) leave the device.
- No biometric voiceprint persisted beyond session lifetime by default — this is both a privacy commitment and a regulatory-risk reducer (biometric data laws vary sharply by jurisdiction).
- Consent flows are explicit for: history retention, offline model download (may use device storage/bandwidth), any future "voice matching" TTS feature that clones a user's own voice characteristics.
- Least-privilege IAM per microservice; no service has blanket data access.
- No third-party ad/tracking SDKs in the mobile app.

---

## 15. Accessibility

- Full live transcript view (opt-in access) with real-time captioning, WCAG 2.2 AA contrast/typography.
- Screen-reader labeling on 100% of UI surfaces (which, by design, is a small surface area — the UI is intentionally minimal).
- Haptic feedback for session state changes (session started, partner switched, disconnected) for users who benefit from non-audio cues.
- Text-to-speech rate/voice customization for users with auditory processing differences.

---

## 16. Cloud Infrastructure & DevOps

- **Cloud provider:** multi-region deployment on a major cloud (GCP or AWS — final pick deferred to Phase 2 cost/latency benchmarking) with GPU-backed autoscaling pools for the inference layer.
- **Containerization:** all services containerized (Docker), orchestrated via Kubernetes, with the streaming gateway and CIE services running as low-latency-tuned deployments (dedicated node pools, no noisy-neighbor CPU throttling).
- **CI/CD:** GitHub Actions — build/test/lint on PR, staged rollout (canary → regional → global) for backend services; mobile app releases via standard store pipelines with phased rollout.
- **IaC:** Terraform for all cloud resources — no manual console changes.
- **Monitoring:** Prometheus + Grafana for service metrics, distributed tracing (OpenTelemetry) across the hot path specifically to keep the latency budget (§6) honest and debuggable per-stage.
- **Logging:** structured logs, PII-redacted by default, correlated by session_id/trace_id.
- **Alerting:** latency SLO burn-rate alerts (this product's core promise is latency — alerting must be tuned tighter here than a typical app).

---

## 17. Testing Strategy

- **Unit/integration:** standard coverage for all services, especially CIE decision logic (this is the most bug-prone, highest-value-to-test component — many small unit tests around partner-switch/hysteresis/recovery logic).
- **Audio-specific test harness:** a corpus of recorded multi-speaker, multi-noise-environment audio (airport, restaurant, market, quiet room) used as regression tests for the whole pipeline — noise robustness is a product-defining metric, not a nice-to-have.
- **Latency testing:** automated per-stage latency benchmarking in CI against the §6 budget; regressions block release.
- **Load/scale testing:** simulated concurrent sessions to validate autoscaling and regional routing.
- **Field testing:** structured real-world testing in each target environment (§4) before each major release — lab testing alone will not validate the CIE's partner-tracking behavior.
- **Accessibility testing:** screen reader + WCAG audits each release.
- **Failure-injection testing:** forced earbud disconnects, network drops, mid-conversation partner switches — verifying graceful, non-visual recovery per §6.5/§7.6.

---

## 18. Technology Stack Summary

| Layer | Choice | Rationale |
|---|---|---|
| Mobile | Native Kotlin (Android) + Swift (iOS) | Real-time audio reliability, Bluetooth API depth |
| Transport | gRPC streaming + Opus codec, TLS 1.3 | Low overhead, bidirectional streaming, wide support |
| Backend gateway/session | Go | Concurrency, predictable latency, streaming-friendly |
| AI orchestration | Python services | ML ecosystem fit, fast iteration on CIE logic |
| ASR/MT/TTS | Managed foundation model APIs (v1), selective custom fine-tuning (v2+) | Fastest path to quality/latency bar; invest custom effort only where differentiated (CIE, speaker ID) |
| Session/state store | Redis (hot), durable DB (opt-in history) | Low-latency session state, durable only where user consents |
| Event/telemetry bus | Kafka-class log | Decouples Analytics/Privacy/Accessibility consumers from hot path |
| Infra | Kubernetes + Terraform, multi-region | Latency-driven regional deployment, reproducible infra |
| CI/CD | GitHub Actions | Matches existing workflow conventions, strong ecosystem |
| Observability | Prometheus/Grafana + OpenTelemetry tracing | Per-stage latency visibility is a product requirement, not just ops nicety |

---

## 19. Roadmap / Phased Delivery

Each phase ends in a working, demoable system. No phase blocks on "everything" — each is a vertical slice.

**Current status (see `PROGRESS.md` for the full item-by-item log):** Phases 1–3 are
substantially built and tested, including items originally scoped for later phases
(real auth, Google Sign-In, native Android app with a foreground service and Bluetooth
device detection, PWA installability, WebSocket reconnection). Phase 4's Redis-backed
session storage is also implemented. What remains genuinely open: live-testing the real
ASR/TTS vendor integrations against real hardware end-to-end, real-hardware Bluetooth
pairing verification, and Phase 5 in full.

**Phase 1 — Foundation (this doc + next steps)**
- This PRD/architecture (done)
- Thin-slice proof of concept: two-phone, quiet-room, symmetric-mode conversation using off-the-shelf ASR/MT/TTS APIs and a *simplified* CIE (single fixed partner, no bystander handling yet) — proves the core loop and measures baseline latency.

**Phase 2 — Conversation Intelligence v1**
- Real speaker diarization + partner tracking + bystander rejection
- Noise Intelligence Agent (real environments, not quiet rooms)
- Hysteresis/recovery logic (§7.5–7.6)

**Phase 3 — Asymmetric Mode + Mobile Polish**
- Single-app-holder flow (§4.1)
- Full mobile UI (minimal, accessible)
- Bluetooth edge-case hardening (disconnects, route changes)

**Phase 4 — Production Hardening**
- Multi-region infra, autoscaling, observability, SLO alerting
- Security/privacy review, consent flows, opt-in history
- Offline degraded mode (FR-15)

**Phase 5 — Scale & Expansion**
- Additional languages, accessibility surface completion
- Cost optimization pass on inference spend
- Groundwork for future proprietary earbuds (not built in v1, architecture must not preclude it)

---

## 20. Open Research Questions (tracked, not blocking)

1. Optimal fusion weighting for partner-identification signals (§7.2) — needs real-world data to tune, not guessable from first principles.
2. Where the on-device vs. cloud line should sit for denoising/diarization as edge hardware capability improves (battery/latency/privacy tradeoff).
3. Voice-matched TTS (translated speech in the *user's own* vocal timbre) — desirable, but consent/deepfake-risk implications need explicit product review before building.
4. Beamforming reliance for phone-mic asymmetric mode — device fragmentation across Android OEMs makes this inconsistent; needs a robustness fallback strategy.
5. Whether the v1.1 partner-group cap of 2 is right, or whether real usage (a family ordering together, a meeting table) needs a larger or dynamic cap. (Self-voice enrollment — excluding the user's own voice from ever being evaluated as a partner candidate — was flagged here as a prerequisite and has since shipped: `cie.enroll_self()` / `POST /api/session/{id}/enroll_self`, tested in `tests/test_cie.py`.)

---

## 21. What This Document Deliberately Does Not Do

- It does not specify exact model vendors/API providers — that's a Phase 1 build-time decision based on live latency/cost/quality benchmarking, not something to lock in on paper.
- It does not introduce any manual-interaction UX pattern anywhere, per §2.
- It does not design proprietary hardware — v1 is software-only per the brief.
