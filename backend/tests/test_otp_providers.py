import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

import otp_providers


def test_console_provider_is_default(monkeypatch):
    monkeypatch.delenv("VOXBUDDY_OTP_PROVIDER", raising=False)
    provider = otp_providers.get_provider("email")
    assert isinstance(provider, otp_providers.ConsoleOTPProvider)


def test_console_provider_records_last_sent_code(capsys):
    provider = otp_providers.ConsoleOTPProvider()
    provider.send("a@b.com", "123456")
    assert provider.last_sent["a@b.com"] == "123456"
    captured = capsys.readouterr()
    assert "123456" in captured.out
    assert "a@b.com" in captured.out


def test_brevo_email_provider_selected_when_configured(monkeypatch):
    monkeypatch.setenv("VOXBUDDY_OTP_PROVIDER", "brevo")
    provider = otp_providers.get_provider("email")
    assert isinstance(provider, otp_providers.BrevoEmailOTPProvider)


def test_brevo_sms_provider_selected_when_configured(monkeypatch):
    monkeypatch.setenv("VOXBUDDY_OTP_PROVIDER", "brevo")
    provider = otp_providers.get_provider("sms")
    assert isinstance(provider, otp_providers.BrevoSMSOTPProvider)


def test_brevo_email_send_fails_clearly_without_credentials(monkeypatch):
    monkeypatch.delenv("VOXBUDDY_BREVO_API_KEY", raising=False)
    monkeypatch.delenv("VOXBUDDY_BREVO_SENDER_EMAIL", raising=False)
    provider = otp_providers.BrevoEmailOTPProvider()
    with pytest.raises(otp_providers.OTPSendError, match="VOXBUDDY_BREVO_API_KEY"):
        provider.send("a@b.com", "123456")


def test_brevo_sms_send_fails_clearly_without_credentials(monkeypatch):
    monkeypatch.delenv("VOXBUDDY_BREVO_API_KEY", raising=False)
    provider = otp_providers.BrevoSMSOTPProvider()
    with pytest.raises(otp_providers.OTPSendError, match="VOXBUDDY_BREVO_API_KEY"):
        provider.send("+14155551234", "123456")


def test_unknown_provider_name_raises_clear_error(monkeypatch):
    monkeypatch.setenv("VOXBUDDY_OTP_PROVIDER", "carrier_pigeon")
    with pytest.raises(otp_providers.OTPSendError, match="Unknown OTP provider 'carrier_pigeon'"):
        otp_providers.get_provider("email")


def test_brevo_provider_unknown_channel_raises(monkeypatch):
    monkeypatch.setenv("VOXBUDDY_OTP_PROVIDER", "brevo")
    with pytest.raises(otp_providers.OTPSendError, match="Unknown channel"):
        otp_providers.get_provider("carrier_pigeon")


def test_email_html_uses_html_entities_not_raw_unicode_dashes(monkeypatch):
    # Raw em-dash characters caused mojibake in some email client/charset
    # combinations — HTML entities render safely everywhere regardless of
    # what charset an email client assumes.
    monkeypatch.delenv("VOXBUDDY_PUBLIC_URL", raising=False)
    provider = otp_providers.BrevoEmailOTPProvider(api_key="x", sender_email="a@b.com")
    html = provider._html("123456")
    assert "—" not in html
    assert "&mdash;" in html


def test_email_html_falls_back_to_text_wordmark_without_public_url(monkeypatch):
    monkeypatch.delenv("VOXBUDDY_PUBLIC_URL", raising=False)
    provider = otp_providers.BrevoEmailOTPProvider(api_key="x", sender_email="a@b.com")
    html = provider._html("123456")
    assert "<img" not in html
    assert "VoxBuddy" in html


def test_email_html_embeds_real_logo_when_public_url_set(monkeypatch):
    monkeypatch.setenv("VOXBUDDY_PUBLIC_URL", "https://voxbuddy-backend.onrender.com")
    provider = otp_providers.BrevoEmailOTPProvider(api_key="x", sender_email="a@b.com")
    html = provider._html("123456")
    assert '<img src="https://voxbuddy-backend.onrender.com/static/icons/icon-512.png"' in html


def test_email_html_contains_the_actual_code(monkeypatch):
    monkeypatch.delenv("VOXBUDDY_PUBLIC_URL", raising=False)
    provider = otp_providers.BrevoEmailOTPProvider(api_key="x", sender_email="a@b.com")
    html = provider._html("987654")
    assert "987654" in html


def test_sms_send_gives_actionable_error_for_missing_sms_addon(monkeypatch):
    # Real Brevo response shape when SMS isn't enabled on the account
    # (transactional email and SMS are separate add-ons on Brevo) — this
    # is exactly what was returned live: {"code":"invalid_parameter",
    # "message":"No sms related addons are found for the given organization"}
    import httpx

    def fake_post(*args, **kwargs):
        request = httpx.Request("POST", "https://api.brevo.com/v3/transactionalSMS/sms")
        return httpx.Response(
            400,
            json={"code": "invalid_parameter",
                  "message": "No sms related addons are found for the given organization"},
            request=request,
        )

    monkeypatch.setattr(httpx, "post", fake_post)
    provider = otp_providers.BrevoSMSOTPProvider(api_key="fake-key")
    with pytest.raises(otp_providers.OTPSendError) as exc_info:
        provider.send("+919128945293", "123456")

    message = str(exc_info.value)
    assert "add-on" in message
    assert "app.brevo.com/transactional/sms/settings/configurations" in message
    assert "Email login" in message


def test_sms_send_other_errors_still_show_raw_brevo_response(monkeypatch):
    # Any other Brevo failure (bad number format, insufficient credits,
    # etc.) should keep surfacing the real response rather than being
    # swallowed into the addon-specific message.
    import httpx

    def fake_post(*args, **kwargs):
        request = httpx.Request("POST", "https://api.brevo.com/v3/transactionalSMS/sms")
        return httpx.Response(
            400,
            json={"code": "invalid_parameter", "message": "Invalid recipient number"},
            request=request,
        )

    monkeypatch.setattr(httpx, "post", fake_post)
    provider = otp_providers.BrevoSMSOTPProvider(api_key="fake-key")
    with pytest.raises(otp_providers.OTPSendError, match="Invalid recipient number"):
        provider.send("not-a-number", "123456")


# ---- SMTPEmailOTPProvider ---------------------------------------------

def test_smtp_provider_requires_credentials(monkeypatch):
    monkeypatch.delenv("VOXBUDDY_SMTP_USERNAME", raising=False)
    monkeypatch.delenv("VOXBUDDY_SMTP_PASSWORD", raising=False)
    provider = otp_providers.SMTPEmailOTPProvider()
    with pytest.raises(otp_providers.OTPSendError, match="VOXBUDDY_SMTP_USERNAME"):
        provider.send("someone@example.com", "123456")


def test_smtp_provider_sends_via_real_smtplib_call(monkeypatch):
    # Mocks smtplib.SMTP_SSL itself (the stdlib boundary) rather than any
    # VoxBuddy code — proves send() actually drives smtplib correctly
    # (right host/port, login called, sendmail called with the right
    # recipient) without needing a real mailbox.
    import smtplib

    calls = {}

    class FakeSMTP:
        def __init__(self, host, port, context=None, timeout=None):
            calls["host"] = host
            calls["port"] = port

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def login(self, username, password):
            calls["login"] = (username, password)

        def sendmail(self, from_addr, to_addrs, msg):
            calls["sendmail"] = (from_addr, to_addrs)
            calls["msg_contains_code"] = "654321" in msg

    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTP)
    provider = otp_providers.SMTPEmailOTPProvider(
        host="smtp.gmail.com", port=465, username="me@gmail.com",
        password="fake-app-password", sender_email="me@gmail.com",
    )
    provider.send("someone@example.com", "654321")

    assert calls["host"] == "smtp.gmail.com"
    assert calls["login"] == ("me@gmail.com", "fake-app-password")
    assert calls["sendmail"][1] == ["someone@example.com"]
    assert calls["msg_contains_code"] is True


def test_smtp_provider_gives_clear_gmail_guidance_on_auth_failure(monkeypatch):
    import smtplib

    class FakeSMTPAuthFail:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            raise smtplib.SMTPAuthenticationError(535, b"Authentication failed")

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTPAuthFail)
    provider = otp_providers.SMTPEmailOTPProvider(
        host="smtp.gmail.com", port=465, username="me@gmail.com",
        password="wrong-password", sender_email="me@gmail.com",
    )
    with pytest.raises(otp_providers.OTPSendError, match="App Password"):
        provider.send("someone@example.com", "111111")


# ---- Fast2SMSOTPProvider ------------------------------------------------

def test_fast2sms_provider_requires_api_key(monkeypatch):
    monkeypatch.delenv("VOXBUDDY_FAST2SMS_API_KEY", raising=False)
    provider = otp_providers.Fast2SMSOTPProvider()
    with pytest.raises(otp_providers.OTPSendError, match="VOXBUDDY_FAST2SMS_API_KEY"):
        provider.send("+919128945293", "123456")


def test_fast2sms_provider_strips_country_code(monkeypatch):
    import httpx

    captured = {}

    def fake_get(url, params=None, timeout=None):
        captured["params"] = params
        request = httpx.Request("GET", url)
        return httpx.Response(200, json={"return": True}, request=request)

    monkeypatch.setattr(httpx, "get", fake_get)
    provider = otp_providers.Fast2SMSOTPProvider(api_key="fake-key")
    provider.send("+919128945293", "435343")

    assert captured["params"]["numbers"] == "9128945293"
    assert captured["params"]["variables_values"] == "435343"
    assert captured["params"]["route"] == "otp"


def test_fast2sms_provider_raises_on_return_false(monkeypatch):
    # Fast2SMS's real failure mode: HTTP 200 with {"return": false, ...}
    # rather than a non-2xx status — must be checked separately from the
    # status code, or a failed send would look like it succeeded.
    import httpx

    def fake_get(url, params=None, timeout=None):
        request = httpx.Request("GET", url)
        return httpx.Response(200, json={"return": False, "message": "Invalid Number"}, request=request)

    monkeypatch.setattr(httpx, "get", fake_get)
    provider = otp_providers.Fast2SMSOTPProvider(api_key="fake-key")
    with pytest.raises(otp_providers.OTPSendError, match="Invalid Number"):
        provider.send("+91123", "123456")


# ---- Per-channel independent provider selection -------------------------

def test_email_and_sms_can_use_different_providers_independently(monkeypatch):
    monkeypatch.delenv("VOXBUDDY_OTP_PROVIDER", raising=False)
    monkeypatch.setenv("VOXBUDDY_OTP_EMAIL_PROVIDER", "smtp")
    monkeypatch.setenv("VOXBUDDY_OTP_SMS_PROVIDER", "fast2sms")

    assert isinstance(otp_providers.get_provider("email"), otp_providers.SMTPEmailOTPProvider)
    assert isinstance(otp_providers.get_provider("sms"), otp_providers.Fast2SMSOTPProvider)


def test_channel_specific_env_overrides_shared_env(monkeypatch):
    monkeypatch.setenv("VOXBUDDY_OTP_PROVIDER", "brevo")
    monkeypatch.setenv("VOXBUDDY_OTP_EMAIL_PROVIDER", "smtp")
    # email should use the more specific override (smtp), sms should fall
    # back to the shared setting (brevo) since no SMS-specific override exists
    assert isinstance(otp_providers.get_provider("email"), otp_providers.SMTPEmailOTPProvider)
    assert isinstance(otp_providers.get_provider("sms"), otp_providers.BrevoSMSOTPProvider)


def test_smtp_rejected_for_sms_channel_with_helpful_message(monkeypatch):
    monkeypatch.delenv("VOXBUDDY_OTP_PROVIDER", raising=False)
    monkeypatch.setenv("VOXBUDDY_OTP_SMS_PROVIDER", "smtp")
    with pytest.raises(otp_providers.OTPSendError, match="email-only"):
        otp_providers.get_provider("sms")


def test_fast2sms_rejected_for_email_channel_with_helpful_message(monkeypatch):
    monkeypatch.delenv("VOXBUDDY_OTP_PROVIDER", raising=False)
    monkeypatch.setenv("VOXBUDDY_OTP_EMAIL_PROVIDER", "fast2sms")
    with pytest.raises(otp_providers.OTPSendError, match="SMS-only"):
        otp_providers.get_provider("email")
