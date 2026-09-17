# Security & privacy review

A real code-level pass through the backend and frontend, not a generic
checklist — every item below was actually checked against this codebase.
Findings are grouped by whether they were fixed now or are a known,
documented tradeoff.

## Fixed in this pass

**No per-IP rate limit on OTP requests.** `auth_store.create_otp()`
already stops one identifier (one email/phone) from spamming itself — a
30-second resend cooldown, 5 max verify attempts. But nothing stopped one
client from requesting OTPs for many *different* identifiers rapidly,
which matters for real cost: once a real `VOXBUDDY_OTP_EMAIL_PROVIDER` /
`VOXBUDDY_OTP_SMS_PROVIDER` is configured, every request is a real
SMTP/Brevo/Fast2SMS send that costs money or eats a provider-side rate
limit. Added a small in-memory per-IP limiter (`backend/app.py`, 8
requests per 10 minutes) — deliberately not a new dependency, since this
runs as a single instance; if this ever moves behind multiple backend
instances/a real load balancer, this needs to move to Redis (same store
already used for sessions) so counts are shared. Reads
`X-Forwarded-For` first, since Render sits behind a proxy and
`request.client.host` would otherwise always be the proxy's own IP.
Covered by `backend/tests/test_otp_rate_limit.py`.

## Checked and confirmed safe

- **SQL construction** (`auth_store.py`) — one place builds a query with
  an f-string (`f"SELECT id FROM users WHERE {column} = ?"`), which looks
  concerning at a glance, but `column` is only ever one of two
  internally-set literals (`"email"` / `"phone"`), never raw user input —
  not an injection path. Every actual user-supplied value goes through a
  real parameterized `?` placeholder.
- **OTP code comparison** — uses `hmac.compare_digest()`, not `==`, so
  it's not vulnerable to a timing attack that could narrow down a correct
  code digit-by-digit.
- **CORS** — no `CORSMiddleware` is configured, which is correct here
  (frontend and backend are served from the same origin) — the effect is
  that FastAPI's default same-origin policy applies, so a malicious page
  on another domain can't make authenticated requests against this API
  from a browser.
- **`.env` is gitignored and confirmed never committed** (`git log --all`
  shows no history of it) — real vendor keys aren't sitting in the
  GitHub repo's history.
- **Dev-mode OTP echo** (`dev_otp` in the request-otp response,
  showing the code directly instead of requiring real delivery) —
  correctly gated behind `VOXBUDDY_AUTH_DEV_MODE`, which defaults to off
  or won't be set at all.

## Known tradeoffs — not changed now, documented so the decision is deliberate

- **Auth tokens are stored in `localStorage`, not an httpOnly cookie.**
  This means a successful XSS on the frontend could read a token
  directly, whereas an httpOnly cookie would be invisible to page
  JavaScript. Not changed here because it's a real architecture decision,
  not a quick fix: this app is served through both a plain browser tab
  and a Capacitor native WebView pointed at a remote origin, and getting
  cookie-based auth working correctly across both (SameSite/Secure
  attributes, the native shell's origin handling) needs its own focused
  pass rather than a drive-by change. Worth planning as real follow-up
  work, not indefinitely deferred.
- **Real API keys have been visible in this development conversation's
  local `.env` file** during debugging sessions with an AI coding
  assistant. They were never committed to the repo, but as ordinary good
  hygiene, rotating any key that's been pasted into a chat-based tool at
  any point is worth doing — cheap insurance regardless of whether
  anything's actually at risk.

## Real, actionable gaps found (not security bugs, but block a clean launch)

- **`frontend/privacy.html` and `frontend/terms.html` both still contain
  a literal placeholder** — `[Add your contact email here before
  publishing — required by Play Store]` — instead of a real contact
  address. This isn't optional: Google Play's own publishing
  requirements check for a working privacy policy with real contact
  info, not a bracketed placeholder.

## Out of scope for this pass (flagging, not attempting)

- **Dependency vulnerability scanning** (`pip-audit` / `npm audit` /
  Dependabot) — needs to run against your actual installed versions in
  CI, not something to eyeball from source.
- **Formal legal review of the privacy policy's actual content** — what's
  in `privacy.html` reads as reasonable boilerplate, but whether it's
  legally sufficient for your actual data handling (what's stored, for
  how long, whether/how it's used to train anything) isn't something a
  code review substitutes for.
