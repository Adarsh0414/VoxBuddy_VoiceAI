"""
Test-suite-wide safety net.

backend/.env holds REAL development credentials (Gemini, AssemblyAI,
ElevenLabs, Brevo, fast2sms, Google OAuth, etc.) so that `app.py`'s
`load_dotenv()` gives a working local dev server with zero setup.

That is exactly the problem for automated tests: `load_dotenv()` (via
python-dotenv, `override=False` by default) only *fills in* environment
variables that are not already set. Whichever test file happens to
`import app` first in a pytest run triggers that `load_dotenv()` call,
which then pushes every real provider/key from backend/.env into
`os.environ` for the rest of the *entire test process* — including
test files that never touch `app` directly (e.g. otp_providers tests)
and test files that only override a subset of the provider knobs.
Depending on test order, that can make tests silently exercise real
paid vendor APIs (or fail with 502s when the sandboxed/CI network can't
reach them), rather than the mocks they were written against.

Fix: set every provider-selection variable to a safe mock/local value
*here*, in conftest.py, which pytest always imports before collecting
any test module. Because these are set before `app` is ever imported,
`load_dotenv()`'s no-override default means the real values from
backend/.env can never take effect during a test run — without editing
or removing backend/.env itself (dev credentials rotation is explicitly
out of scope for this pass).

Individual tests remain free to `monkeypatch.setenv(...)` a specific
provider (real-looking fake key included) to exercise that provider's
code path in isolation; monkeypatch restores these safe defaults again
afterwards, so there is never any leakage between tests either.
"""
import os

_SAFE_DEFAULTS = {
    # Core pipeline providers: keep every test on the deterministic mocks
    # unless a test explicitly opts into a specific real-looking vendor.
    "VOXBUDDY_ASR_PROVIDER": "mock",
    "VOXBUDDY_TRANSLATION_PROVIDER": "mock",
    "VOXBUDDY_TTS_PROVIDER": "mock",
    "VOXBUDDY_PERSISTENCE_PROVIDER": "sqlite",
    "VOXBUDDY_SESSION_STORE": "sqlite",

    # OTP: force console delivery (no email/SMS vendor calls) and dev mode
    # so codes are readable straight from the API response.
    # Channel-specific overrides are intentionally left "" rather than
    # "console": otp_providers.get_provider() treats an empty string as
    # "no override" and falls back to VOXBUDDY_OTP_PROVIDER (see below),
    # which is exactly the "no channel override configured" state most
    # tests expect. Setting them to a non-empty value here would itself
    # shadow tests that set VOXBUDDY_OTP_PROVIDER directly and expect it
    # to take effect for both channels.
    "VOXBUDDY_OTP_PROVIDER": "console",
    "VOXBUDDY_OTP_EMAIL_PROVIDER": "",
    "VOXBUDDY_OTP_SMS_PROVIDER": "",
    "VOXBUDDY_AUTH_DEV_MODE": "1",

    # Never let a real vendor key that happens to be sitting in backend/.env
    # get picked up implicitly.
    "ANTHROPIC_API_KEY": "",
    "GEMINI_API_KEY": "",
    "ASSEMBLYAI_API_KEY": "",
    "ELEVENLABS_API_KEY": "",
    "VOXBUDDY_BREVO_API_KEY": "",
    "VOXBUDDY_FAST2SMS_API_KEY": "",
    "GOOGLE_CLIENT_ID": "",
    "GOOGLE_CLIENT_SECRET": "",

    # AWS: harmless placeholder credentials. Real AWS-touching tests
    # (moto-based) set these themselves already, but this keeps any test
    # that lazily constructs a boto3 client from ever reaching for real
    # credentials/region from the environment.
    "AWS_ACCESS_KEY_ID": "testing",
    "AWS_SECRET_ACCESS_KEY": "testing",
    "AWS_REGION": "us-east-1",
    "VOXBUDDY_CLOUDWATCH_METRICS": "false",
    "VOXBUDDY_CALIBRATION_LOGGING": "false",
}

for _key, _value in _SAFE_DEFAULTS.items():
    os.environ[_key] = _value
