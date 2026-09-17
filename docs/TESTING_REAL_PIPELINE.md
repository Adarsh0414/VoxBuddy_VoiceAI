# Testing the real listen → translate → speak pipeline

This is the checklist for turning the demo into the real thing — locally,
and on your deployed Render backend. The mic capture path is now always
real (there's no "demo script" toggle anymore); what varies is whether
each AI stage is running its zero-config mock or a real vendor.

## 1. Get three API keys

| Piece | Vendor | Where to get it | Cost |
|---|---|---|---|
| Listening (speech-to-text) | AssemblyAI | assemblyai.com → dashboard → API key | Free tier available |
| Translation | Anthropic or Google | console.anthropic.com or Google AI Studio | Pay-as-you-go / free tier |
| Speaking | ElevenLabs | elevenlabs.io → profile → API key | Free tier available |

## 2. Confirm ElevenLabs voice IDs

`agents/tts_elevenlabs.py`'s `DEFAULT_VOICE_IDS` now ships with a real
(non-placeholder) voice ID rather than a `REPLACE_WITH...` stub — but the
same ID is currently reused for every target language, so every language
will sound identical until you swap in distinct voice IDs per language
from your ElevenLabs dashboard's Voice Library (an ID looks like
`21m00Tcm4TlvDq8ikWAM`).

## 3. Fill in `.env` (local) — and separately, your host's env vars

```
cd backend
cp .env.example .env
# then edit .env and paste in your three keys + confirm voice IDs
```

**If you're testing against a deployed backend (Render), setting local
`.env` is not enough** — Render does not read your local `.env` file.
Every vendor key in `render.yaml` is marked `sync: false`, meaning it has
to be pasted into Render's own dashboard (Environment tab) manually,
separately from local dev. A backend that works locally but stays silent
after deploy is the classic symptom of this step being skipped.

## 4. Run it for real

```
cd backend
pip install -r requirements.txt
uvicorn app:app --reload
```

Open `http://localhost:8000/app` in a real browser (Chrome/Edge — mic
capture needs a secure context; `localhost` counts as one), or the
installed Android app pointed at your deployed backend.

## 5. Test the actual loop

1. Log in (OTP prints to the terminal unless you've set up email/SMS
   delivery).
2. Tap **Start Conversation** on Home and allow the mic permission
   prompt — this always uses the real microphone now.
3. Speak a sentence.
4. Watch the status line — it should show the translated text, then you
   should **hear it spoken back** through your device's current audio
   output (earbuds, if connected).

## About "speaking in earbuds" specifically

No special earbud code is needed for basic playback. The device plays
audio through whatever its **current default audio output** is — if
Bluetooth earbuds are already connected at the OS level, translated
speech comes out of them automatically, same as any other app's audio.
On the native Android build specifically, `AudioDevicePlugin` additionally
detects an already-connected Classic Bluetooth (A2DP) device so the app
can reflect that in its own UI, but it's not required for audio to
actually route there.

## A real bug that caused exactly this symptom before — now fixed

If you're troubleshooting "the app listens but never speaks," the most
likely historical cause has been fixed but is worth knowing about: the
AssemblyAI adapter (`backend/agents/asr_assemblyai.py`) was calling the
SDK's `client.stream()` once per incoming audio frame instead of once
per session with a generator — the SDK's actual documented usage. That
meant no final transcript was ever produced, so nothing ever reached
translation or TTS, with no error shown anywhere. It's now implemented
with a queue + generator + background thread, matching the SDK's real
contract. If you pull an older copy of this repo and hit the same silent
symptom, this is the first thing to check.

## If something doesn't work now

Error surfacing was hardened alongside the fix above — most failures now
show up directly instead of silently:

- **Session won't even start / `/api/session` fails**: a provider is set
  to a real vendor but its API key is missing — the error message now
  names exactly which one.
- **No transcription happens**: check the browser console and the
  `uvicorn` terminal; confirm `VOXBUDDY_ASR_PROVIDER=assemblyai` is set
  correctly (a typo raises `ValueError` on session creation, it doesn't
  silently fall back to mock).
- **Translation works but no audio plays**: the status line now shows
  `(playback failed: ...)` inline with the translated text when
  `tts_error` is set — usually a missing key or an invalid/placeholder
  voice ID.
- **Still stuck**: open browser dev tools → Console, and share the first
  real error. An exact error message is the fastest way to actually fix
  it versus guessing.
