"""
OTP delivery providers — how a generated code actually reaches the user.

Same pattern as agents/factory.py's translation provider selection:
zero-config by default (ConsoleOTPProvider, no external account needed,
fully testable in any environment including this one), real Brevo
providers available behind an env var once credentials exist.

Why Brevo for both channels: one account, one API key
(VOXBUDDY_BREVO_API_KEY), covers both transactional email
(/v3/smtp/email — same endpoint the reference Cloudflare Worker example
used) and transactional SMS (/v3/transactionalSMS/sms). Simpler than
running two separate vendor relationships for what's conceptually one
feature (OTP delivery).

Note on architecture vs. the reference example: that project (a static
GitHub Pages site) needed a Cloudflare Worker specifically because a
static site has no server of its own to hold a secret API key. VoxBuddy's
backend IS a real server — the Brevo API key lives in this process's
environment variables exactly like ANTHROPIC_API_KEY already does, with
no separate proxy needed.
"""

from __future__ import annotations

import os
from typing import Protocol

APP_NAME = "VoxBuddy"


class OTPProvider(Protocol):
    def send(self, identifier: str, code: str) -> None:
        """Delivers `code` to `identifier`. Raises OTPSendError on failure."""
        ...


class OTPSendError(Exception):
    pass


class ConsoleOTPProvider:
    """Zero-config default: prints the code to the server console instead
    of sending anything. This is what makes the whole auth flow testable
    without a Brevo account — exactly the same role MockTranslationAgent
    plays for translation."""

    def __init__(self):
        self.last_sent: dict[str, str] = {}

    def send(self, identifier: str, code: str) -> None:
        self.last_sent[identifier] = code
        print(f"\n{'=' * 50}\n  [DEV OTP] {identifier} -> {code}\n{'=' * 50}\n")


class BrevoEmailOTPProvider:
    def __init__(self, api_key: str | None = None, sender_email: str | None = None,
                 sender_name: str = APP_NAME):
        self.api_key = api_key or os.environ.get("VOXBUDDY_BREVO_API_KEY")
        self.sender_email = sender_email or os.environ.get("VOXBUDDY_BREVO_SENDER_EMAIL")
        self.sender_name = sender_name
        # Optional: only needed to embed the real logo image (email clients
        # can't load images from localhost — they need a real public HTTPS
        # URL). Without it, the email still looks intentional, just with a
        # text wordmark instead of the waveform PNG. Set this once the
        # backend is deployed (e.g. https://voxbuddy-backend.onrender.com).
        self.public_url = os.environ.get("VOXBUDDY_PUBLIC_URL", "").rstrip("/")

    def _html(self, code: str) -> str:
        # Table-based layout, all styles inline — deliberate choice for
        # email specifically (not how the rest of the app is built):
        # email clients (Outlook especially) strip <style> blocks and
        # don't reliably support flexbox/grid, so this uses the
        # lowest-common-denominator patterns that actually render
        # consistently across Gmail/Outlook/Apple Mail.
        logo_html = (
            f'<img src="{self.public_url}/static/icons/icon-512.png" width="56" height="56" '
            f'alt="{APP_NAME}" style="display:block;border-radius:50%;" />'
            if self.public_url else
            f'<div style="font-family:Georgia,serif;font-size:26px;font-weight:600;'
            f'background:linear-gradient(120deg,#E8A94C,#4FD1C5);-webkit-background-clip:text;'
            f'-webkit-text-fill-color:transparent;">{APP_NAME}</div>'
        )
        return f"""<div style="background-color:#0B0D14;padding:32px 16px;font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;">
  <table role="presentation" width="100%" style="max-width:420px;margin:0 auto;border-collapse:collapse;">
    <tr><td style="padding-bottom:20px;">{logo_html}</td></tr>
    <tr><td>
      <table role="presentation" width="100%" style="background-color:#151822;border:1px solid rgba(255,255,255,0.08);border-radius:18px;border-collapse:collapse;">
        <tr><td style="padding:28px 26px 8px;">
          <p style="margin:0;color:#EDEFF4;font-size:15px;font-weight:600;">Your verification code</p>
          <p style="margin:6px 0 0;color:#8B90A3;font-size:13px;line-height:1.5;">Enter this in {APP_NAME} to finish signing in.</p>
        </td></tr>
        <tr><td style="padding:18px 26px;">
          <div style="background:linear-gradient(120deg,rgba(232,169,76,0.12),rgba(79,209,197,0.12));border-radius:12px;padding:18px;text-align:center;">
            <span style="font-family:'Courier New',monospace;font-size:32px;font-weight:700;letter-spacing:0.22em;color:#EDEFF4;">{code}</span>
          </div>
        </td></tr>
        <tr><td style="padding:4px 26px 26px;">
          <p style="margin:0;color:#6B7086;font-size:12.5px;line-height:1.6;">
            This code expires in <strong style="color:#8B90A3;">5 minutes</strong>. If you didn't request this, you can safely ignore this email &mdash; {APP_NAME} will never ask for this code by phone or chat.
          </p>
        </td></tr>
      </table>
    </td></tr>
    <tr><td style="padding-top:22px;text-align:center;">
      <p style="margin:0;color:#4A4E5C;font-size:11.5px;">{APP_NAME} &mdash; voice to voice, heart to heart.</p>
    </td></tr>
  </table>
</div>"""

    def send(self, identifier: str, code: str) -> None:
        if not self.api_key or not self.sender_email:
            raise OTPSendError(
                "Brevo email provider selected but VOXBUDDY_BREVO_API_KEY / "
                "VOXBUDDY_BREVO_SENDER_EMAIL are not set."
            )
        import httpx

        try:
            resp = httpx.post(
                "https://api.brevo.com/v3/smtp/email",
                headers={
                    "accept": "application/json",
                    "content-type": "application/json",
                    "api-key": self.api_key,
                },
                json={
                    "sender": {"name": self.sender_name, "email": self.sender_email},
                    "to": [{"email": identifier}],
                    "subject": f"Your {APP_NAME} verification code",
                    "htmlContent": self._html(code),
                },
                timeout=10.0,
            )
        except httpx.HTTPError as e:
            raise OTPSendError(f"Could not reach Brevo: {e}") from e

        if resp.status_code >= 300:
            raise OTPSendError(f"Brevo rejected the send ({resp.status_code}): {resp.text[:200]}")


class BrevoSMSOTPProvider:
    def __init__(self, api_key: str | None = None, sender_name: str | None = None):
        self.api_key = api_key or os.environ.get("VOXBUDDY_BREVO_API_KEY")
        # Brevo SMS sender: an alphanumeric name (up to 11 chars in most
        # countries), not a phone number — set once in the Brevo dashboard.
        self.sender_name = sender_name or os.environ.get("VOXBUDDY_BREVO_SMS_SENDER", APP_NAME[:11])

    def send(self, identifier: str, code: str) -> None:
        if not self.api_key:
            raise OTPSendError("Brevo SMS provider selected but VOXBUDDY_BREVO_API_KEY is not set.")
        import httpx

        try:
            resp = httpx.post(
                "https://api.brevo.com/v3/transactionalSMS/sms",
                headers={
                    "accept": "application/json",
                    "content-type": "application/json",
                    "api-key": self.api_key,
                },
                json={
                    "sender": self.sender_name,
                    "recipient": identifier,
                    "content": f"Your {APP_NAME} code is {code}. Expires in 5 minutes.",
                    "type": "transactional",
                },
                timeout=10.0,
            )
        except httpx.HTTPError as e:
            raise OTPSendError(f"Could not reach Brevo: {e}") from e

        if resp.status_code >= 300:
            if "sms related addons" in resp.text.lower():
                raise OTPSendError(
                    "Brevo SMS isn't enabled on this account yet. Transactional "
                    "SMS is a separate add-on from email — enable it (and add "
                    "SMS credits) at https://app.brevo.com/transactional/sms/"
                    "settings/configurations, or use Email login instead, "
                    "which doesn't need this."
                )
            raise OTPSendError(f"Brevo rejected the send ({resp.status_code}): {resp.text[:200]}")


# Module-level singleton so ConsoleOTPProvider's last_sent dict (used by the
# dev-mode "what code did you just send" test/debug endpoint) persists
# across requests within one process, same lifetime as the in-memory
# `sessions` dict in app.py.
_console_provider = ConsoleOTPProvider()


class SMTPEmailOTPProvider:
    """Direct SMTP send — no Brevo (or any third-party email API) needed
    at all. Works with Gmail (with an app password, not your normal
    password — Google requires that for SMTP), Outlook, or literally any
    SMTP server. This is genuinely simpler for email specifically than
    Brevo, since Gmail SMTP needs no separate account/API key beyond a
    Gmail address you already have — just Settings -> Security -> App
    Passwords in your Google account.

    Uses only Python's stdlib (smtplib + ssl + email.mime) — no extra
    pip dependency, unlike every other real vendor adapter in this
    project.
    """

    def __init__(self, host: str | None = None, port: int | None = None,
                 username: str | None = None, password: str | None = None,
                 sender_email: str | None = None):
        self.host = host or os.environ.get("VOXBUDDY_SMTP_HOST", "smtp.gmail.com")
        self.port = port or int(os.environ.get("VOXBUDDY_SMTP_PORT", "465"))
        self.username = username or os.environ.get("VOXBUDDY_SMTP_USERNAME")
        self.password = password or os.environ.get("VOXBUDDY_SMTP_PASSWORD")
        self.sender_email = sender_email or os.environ.get("VOXBUDDY_SMTP_SENDER_EMAIL") or self.username

    def _html(self, code: str) -> str:
        # Same template as BrevoEmailOTPProvider — duplicated rather than
        # shared via inheritance, since these are two independent,
        # swappable implementations of one Protocol (agents/base.py-style
        # pattern used throughout this project), not a shared-base-class
        # relationship.
        return BrevoEmailOTPProvider()._html(code)

    def send(self, identifier: str, code: str) -> None:
        if not self.username or not self.password:
            raise OTPSendError(
                "SMTP email provider selected but VOXBUDDY_SMTP_USERNAME / "
                "VOXBUDDY_SMTP_PASSWORD are not set. For Gmail: use an App "
                "Password (Google Account -> Security -> App Passwords), "
                "not your normal login password — Gmail rejects normal "
                "passwords for SMTP."
            )
        import smtplib
        import ssl
        from email.mime.multipart import MIMEMultipart
        from email.mime.text import MIMEText

        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"Your {APP_NAME} verification code"
        msg["From"] = self.sender_email
        msg["To"] = identifier
        msg.attach(MIMEText(f"Your {APP_NAME} verification code is: {code}\n"
                             f"This code expires in 5 minutes.", "plain"))
        msg.attach(MIMEText(self._html(code), "html"))

        try:
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(self.host, self.port, context=context, timeout=10) as server:
                server.login(self.username, self.password)
                server.sendmail(self.sender_email, [identifier], msg.as_string())
        except smtplib.SMTPAuthenticationError as e:
            raise OTPSendError(
                f"SMTP login failed ({e}). For Gmail specifically, make sure "
                f"you're using an App Password, not your regular password, "
                f"and that 2-Step Verification is enabled on the account."
            ) from e
        except (smtplib.SMTPException, OSError) as e:
            raise OTPSendError(f"Could not send via SMTP: {e}") from e


class Fast2SMSOTPProvider:
    """Fast2SMS — an alternative to Brevo for SMS specifically, not a
    Brevo feature. Chosen as the alternative here because signup gives
    free trial credit immediately with no separate "enable this add-on"
    purchase step (the exact friction Brevo's SMS product has), and it's
    India-focused (matches VoxBuddy's actual dev/test phone numbers).

    Uses Fast2SMS's dedicated OTP route ("route=otp"), which sends
    through Fast2SMS's own pre-approved OTP template — this matters in
    India specifically: TRAI (the telecom regulator) requires SMS sender
    templates to be DLT-registered, which is real
    paperwork/business-verification, not just an API key. Using the
    OTP-specific route sends through Fast2SMS's already-approved
    template instead of requiring you to register your own.

    Real code against Fast2SMS's documented API — like every other real
    vendor adapter in this project, not live-tested here (no account,
    same honesty as AssemblyAI/ElevenLabs before you added real keys).
    """

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or os.environ.get("VOXBUDDY_FAST2SMS_API_KEY")

    def send(self, identifier: str, code: str) -> None:
        if not self.api_key:
            raise OTPSendError("Fast2SMS provider selected but VOXBUDDY_FAST2SMS_API_KEY is not set.")
        import httpx

        # Fast2SMS expects a bare 10-digit Indian number for this route,
        # not E.164 (+91...) — strip a leading country code if present so
        # the same "+91XXXXXXXXXX" identifiers used elsewhere still work.
        number = identifier.strip().replace(" ", "")
        if number.startswith("+91"):
            number = number[3:]
        elif number.startswith("91") and len(number) == 12:
            number = number[2:]

        try:
            resp = httpx.get(
                "https://www.fast2sms.com/dev/bulkV2",
                params={
                    "authorization": self.api_key,
                    "route": "otp",
                    "variables_values": code,
                    "flash": "0",
                    "numbers": number,
                },
                timeout=10.0,
            )
        except httpx.HTTPError as e:
            raise OTPSendError(f"Could not reach Fast2SMS: {e}") from e

        if resp.status_code >= 300:
            raise OTPSendError(f"Fast2SMS rejected the send ({resp.status_code}): {resp.text[:200]}")

        # Fast2SMS returns 200 with {"return": false, ...} on some
        # failures (invalid number, insufficient balance) rather than a
        # non-2xx status — has to be checked separately.
        try:
            data = resp.json()
        except ValueError:
            return  # non-JSON 2xx response — treat as success, nothing more to check
        if isinstance(data, dict) and data.get("return") is False:
            raise OTPSendError(f"Fast2SMS rejected the send: {data.get('message', resp.text[:200])}")


def get_provider(channel: str) -> OTPProvider:
    """
    Provider selection is per-channel and independent — email and SMS
    don't have to use the same vendor. This is the direct answer to "why
    not separate this from Brevo": you can run SMTP for email and
    Fast2SMS for SMS simultaneously, or any other combination, without
    touching each other.

    Resolution order for a given channel:
      1. VOXBUDDY_OTP_EMAIL_PROVIDER / VOXBUDDY_OTP_SMS_PROVIDER (channel-specific)
      2. VOXBUDDY_OTP_PROVIDER (old single shared setting, still works
         for backward compatibility — e.g. VOXBUDDY_OTP_PROVIDER=brevo
         still uses Brevo for both, same as before this existed)
      3. "console" (default — prints to stdout, zero config)

    Valid values:
      - "console" — zero config, works for both channels
      - "brevo" — Brevo (email or SMS)
      - "smtp" — direct SMTP send, email only (Gmail/Outlook/any SMTP server)
      - "fast2sms" — Fast2SMS, SMS only (India-focused, easier signup than Brevo SMS)
    """
    channel_key = f"VOXBUDDY_OTP_{channel.upper()}_PROVIDER"
    provider_name = os.environ.get(channel_key) or os.environ.get("VOXBUDDY_OTP_PROVIDER", "console")
    provider_name = provider_name.lower()

    if provider_name == "console":
        return _console_provider

    if provider_name == "brevo":
        if channel == "email":
            return BrevoEmailOTPProvider()
        if channel == "sms":
            return BrevoSMSOTPProvider()
        raise OTPSendError(f"Unknown channel '{channel}' for Brevo provider")

    if provider_name == "smtp":
        if channel != "email":
            raise OTPSendError(
                f"'smtp' is an email-only provider, not valid for channel='{channel}'. "
                f"Use VOXBUDDY_OTP_SMS_PROVIDER=fast2sms (or brevo) for SMS instead."
            )
        return SMTPEmailOTPProvider()

    if provider_name == "fast2sms":
        if channel != "sms":
            raise OTPSendError(
                f"'fast2sms' is an SMS-only provider, not valid for channel='{channel}'. "
                f"Use VOXBUDDY_OTP_EMAIL_PROVIDER=smtp (or brevo) for email instead."
            )
        return Fast2SMSOTPProvider()

    raise OTPSendError(
        f"Unknown OTP provider '{provider_name}' for channel '{channel}'. "
        f"Valid options: 'console', 'brevo', plus 'smtp' (email-only) or "
        f"'fast2sms' (sms-only)."
    )
