# VoxBuddy — AWS Deployment (Elastic Beanstalk)

This document explains exactly how and where VoxBuddy's backend is deployed
on AWS. It complements `docs/AWS_INTEGRATION.md`, which explains what each
AWS service does *inside* the application (Polly, DynamoDB, Transcribe,
Bedrock, CloudWatch, S3); this document covers the deployment platform
itself — where the process runs, how it starts, how HTTPS is terminated,
and how permissions are granted. See `README.md` 19 for the condensed
version of this same content with an architecture diagram.

No AWS account IDs, access keys, secret values, or private endpoints are
included anywhere in this document.

## 1. Platform: AWS Elastic Beanstalk

The FastAPI backend is deployed as a **single-instance Elastic Beanstalk
environment** (no load balancer) on EB's Python platform, running on one
EC2 instance. This repository contains the exact configuration EB reads at
deploy time:

| File | Role |
|---|---|
| `Procfile` | `web: uvicorn --app-dir backend app:app --host 0.0.0.0 --port 8000` — tells EB's Python platform how to start the app process. |
| `requirements.txt` (repo root) | A one-line pointer (`-r backend/requirements.txt`) so EB installs the same dependency set used for local development, from a single source of truth rather than two lists to keep in sync. |
| `.ebextensions/00_install_certbot.config` | Installs `certbot` and `certbot-nginx` into an isolated Python virtualenv on the instance (`/opt/certbot`), run again on every deploy (idempotent, errors ignored on re-runs). |
| `.ebextensions/10_open_https_port.config` | Adds an inbound rule for port 443 to the environment's EB-managed security group. Port 80 is already open by EB's own default config. |
| `.platform/hooks/postdeploy/00_get_certificate.sh` | Runs after every deploy. Provisions/renews a free Let's Encrypt TLS certificate and writes the nginx config needed to terminate HTTPS on the instance itself. |

## 2. Why HTTPS is handled manually

A single-instance Elastic Beanstalk environment has **no load balancer**
(no ELB/ALB/CloudFront) in front of it. AWS Certificate Manager (ACM)
certificates only attach to those, so they aren't usable here. TLS has to
terminate on the instance's own nginx reverse proxy instead, which means a
real, non-self-signed certificate is needed — self-signed certificates are
rejected by Android and by Google's OAuth flow, both of which VoxBuddy
depends on.

The deploy hook (`00_get_certificate.sh`) handles this without depending on
EB's managed nginx config staying in a particular shape:

1. It reads two environment properties, `VOXBUDDY_HTTPS_DOMAIN` and
   `VOXBUDDY_HTTPS_EMAIL`. If either is unset, the hook exits early and
   HTTPS setup is skipped for that deploy — it never fails the deploy.
2. It adds an nginx `location` block for the Let's Encrypt HTTP-01
   challenge path inside EB's existing default server block, using the
   `webroot` authenticator so it never needs to edit or guess at EB's own
   managed config file.
3. It requests/renews the certificate via `certbot certonly --webroot`.
   Certbot skips re-issuing a still-valid certificate, so re-running this
   hook on every deploy is safe and doubles as the renewal mechanism, as
   long as the environment is redeployed at least once before the
   certificate's ~90-day expiry.
4. It writes its own `server { listen 443 ssl; ... }` nginx block by hand,
   proxying to the same upstream EB's own HTTP block uses (extracted with
   `grep` rather than hard-coded, so it can't silently drift from whatever
   internal port a given platform version uses) — including the
   `Upgrade`/`Connection` headers required for the app's `/ws/*` WebSocket
   endpoints to work over `wss://`.

## 3. How traffic reaches the application

```
Client (browser / PWA / Android app)
        │  HTTPS (443) or HTTP (80)
        ▼
EC2 instance — nginx (EB-managed HTTP block + hand-written HTTPS block)
        │  proxy_pass, with WebSocket Upgrade/Connection headers
        ▼
uvicorn — FastAPI app (backend/app.py), port 8000
        │
        ├── /api/*         REST endpoints (auth, history, stats, ...)
        ├── /ws/{id}         non-streaming utterance WebSocket
        ├── /ws/{id}/audio   streaming audio WebSocket (ASR → CIE → translation → TTS)
        └── /app, /preview, /static/*   frontend static files
```

## 4. IAM

The deployed EC2 instance authenticates to AWS services using credentials
supplied as environment variables (`AWS_ACCESS_KEY_ID`,
`AWS_SECRET_ACCESS_KEY`, `AWS_REGION`), read automatically by `boto3`'s
default credential chain. The IAM policy attached to that identity grants
exactly the actions the application's code calls — see `README.md` 6.5
for the full JSON (Polly, Transcribe, scoped DynamoDB table access, and
the Bedrock/AWS Marketplace actions needed for the not-yet-unblocked
Bedrock translation path, 6.7). It does **not** grant S3 or CloudWatch
actions unless those optional features are explicitly turned on — see
`docs/AWS_INTEGRATION.md` for the additional statements those need.

No real account IDs, key values, or the live policy's ARNs are recorded in
this repository — only a reference copy with `<YOUR_ACCOUNT_ID>`
placeholders.

## 5. Environment configuration

Environment variables are set as EB environment properties (not committed
to the repo). The variable **names** used by the deployed configuration
are documented in `backend/.env.example` and `README.md` 18 — set
`VOXBUDDY_TTS_PROVIDER=polly`, `VOXBUDDY_PERSISTENCE_PROVIDER=dynamodb`,
and `VOXBUDDY_ASR_PROVIDER=aws_transcribe` alongside the shared AWS
credentials to reproduce the deployed configuration. Translation currently
runs on `VOXBUDDY_TRANSLATION_PROVIDER=gemini` rather than the code's
`bedrock` default — see README 6.7 for the (AWS-account-side, not
code-side) reason.

## 6. Health checks and monitoring

Elastic Beanstalk performs its own instance/environment health checks
(CPU, process health, environment status) visible in the EB console,
independent of anything in the application code. The application itself
does not currently expose a dedicated `/health` endpoint beyond its root
route. Amazon CloudWatch integration for the CIE's own decision metrics
exists in the codebase (`backend/cie/cloudwatch_metrics.py`) but is off by
default and not confirmed enabled on the live environment — see
`docs/AWS_INTEGRATION.md` for what it does when turned on.

## 7. Alternative deployment path

`render.yaml` is a ready-to-use [Render](https://render.com/) blueprint
included in the repository as an alternative deployment platform. It is
**not** what the currently live deployment runs on — the live backend runs
on Elastic Beanstalk as described above.
