# Publishing VoxBuddy to Google Play (as a TWA)

Honest scope: this covers **Google Play only**. Apple's App Store rejects
PWAs outright ("repackaged websites" are against their review guidelines)
— iOS users can only get VoxBuddy via Safari's "Add to Home Screen," not
a Play-Store-style listing, unless you build a real native/Capacitor app
later.

Everything up through step 2 can be done now. Steps 3+ need tools
(Android SDK, a JDK, Bubblewrap) and a signing key that has to live on
**your** machine, under **your** control — never hand your signing key to
anyone, including an AI assistant. Whoever holds it can push updates to
your app forever.

## Step 1 — Deploy to a real HTTPS URL

TWA requires your app to be live on the public internet, not `localhost`.
Render works and matches how CampusVibe/CallBeacon are already hosted:

1. Push this repo to a GitHub repo.
2. On Render: New → Web Service → connect the repo, root directory
   `backend`, build command `pip install -r requirements.txt`, start
   command `uvicorn app:app --host 0.0.0.0 --port $PORT`.
3. Set environment variables from `.env.example` as needed (at minimum
   `VOXBUDDY_AUTH_DEV_MODE=0` for production — never leave dev mode on
   publicly, it returns OTP codes in the API response).
4. Once deployed you'll have something like `https://voxbuddy.onrender.com`.

## Step 2 — Verify the PWA is actually installable

From your own machine, with the real URL:
1. Open it in Chrome, DevTools → Lighthouse → run a PWA audit. Google's
   TWA guidance wants a score of 80+.
2. Confirm `https://your-domain/manifest.json` and `https://your-domain/sw.js`
   both load.
3. Confirm `https://your-domain/privacy` loads (already built —
   `frontend/privacy.html` — but **edit the placeholder contact email in
   it before going further**, Play Console will ask for this).

## Step 3 — Generate the Android package (on your machine)

You now have two options — use whichever fits:

**Option A (recommended, and the one already in use):** the real Android
Studio project at `mobile/android` — see `docs/MOBILE_BUILD.md`. This is
no longer just scaffolding: it's a built, installed, real-device-tested
app with native plugins (background foreground service so mic capture
survives being backgrounded, Bluetooth audio device detection). Open it
in Android Studio, let Gradle sync, then Build → Generate Signed
Bundle/APK. The same codebase also covers iOS via `mobile/ios` (not yet
built — needs a Mac), so if you want both stores from one setup, start
here.

**Option B (Android-only, lighter-weight, ~200KB app):** wrap the PWA
directly with Bubblewrap instead — produces a smaller app since it's just
a thin TWA wrapper, not a full WebView shell:

This needs a JDK and the Android SDK, which this build environment
couldn't reach (no network path to Google's SDK servers here) — this step
has to happen on your own computer.

```bash
npm install -g @bubblewrap/cli
bubblewrap init --manifest=https://your-domain/manifest.json
```

Bubblewrap will ask a series of questions (package name, e.g.
`com.yourname.voxbuddy`; app name; signing key details) and download the
Android SDK/JDK itself if needed. **Save the signing key file and its
password somewhere safe and backed up** — if you lose it, you cannot
publish updates to the same app listing ever again, only a new one.

```bash
bubblewrap build
```

This produces a signed `.aab` file — the actual file you upload to Play
Console.

**Either way**, whether via Option A or B, don't lose the resulting
signing key — same warning applies to both.

## Step 4 — Digital Asset Links (prove you own both the site and the app)

Bubblewrap prints a fingerprint after building. Create a file at
`https://your-domain/.well-known/assetlinks.json`:

```json
[{
  "relation": ["delegate_permission/common.handle_all_urls"],
  "target": {
    "namespace": "android_app",
    "package_name": "com.yourname.voxbuddy",
    "sha256_cert_fingerprints": ["THE_FINGERPRINT_BUBBLEWRAP_PRINTED"]
  }
}]
```

You'll need a small addition to `app.py` to serve this (a static file
route under `/.well-known/` — ask me to add this once you have the real
fingerprint, it's a two-line change).

## Step 5 — Google Play Console

1. Create a account at [play.google.com/console](https://play.google.com/console) — **$25 one-time fee**.
2. Create a new app, fill in:
   - App name, short/full description
   - Screenshots (take these from your phone — you already have a
     polished UI to screenshot: Home, Conversation, History, Profile)
   - Feature graphic (1024×500 — I can generate this on request, matching
     the app's amber/teal visual identity)
   - Privacy policy URL: `https://your-domain/privacy`
   - Content rating questionnaire
   - Data safety form (be accurate: you collect email/phone and
     conversation content — see `frontend/privacy.html` for the exact
     language to reuse here)
3. Upload the `.aab` from Step 3.
4. Submit for review. Google's review is typically faster than Apple's —
   often a few hours to a couple of days for a first submission, but
   don't assume the current timeline; check Play Console for whatever it
   says at the time.

## What I can help with from here

- Editing `frontend/privacy.html` further
- Adding the `assetlinks.json` route once you have a real fingerprint
- Generating a feature graphic / Play Store screenshots layout
- Reviewing your Play Console data-safety form answers against what the
  code actually does, so you don't over- or under-declare

What I can't do: hold your signing key, run Bubblewrap against a live SDK
download (blocked in this sandbox), or submit the listing on your behalf.
