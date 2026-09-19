"""
Persistence backend selector.

app.py does `import persistence_store as persistence` instead of
`import persistence` directly, so the storage backend is a one-variable
config choice - VOXBUDDY_PERSISTENCE_PROVIDER - rather than a code change,
matching the same provider-swap pattern agents/factory.py already uses
for ASR/translation/TTS.

  - "sqlite" (default) - persistence.py. Zero-config, runs locally with no
    AWS account needed, exactly as this project has always worked.
  - "dynamodb" - persistence_dynamodb.py, the AWS-native backend (see its
    module docstring). Credentials are resolved via boto3's normal
    provider chain (env vars, ~/.aws/credentials, or an EC2/Elastic
    Beanstalk instance profile / IAM role); AWS_REGION optional (boto3
    default region otherwise).

Implementation note: this module replaces its own entry in sys.modules
with the chosen backend module, rather than re-exporting individual
names. That means `import persistence_store as persistence` behaves
*identically* to `import persistence` when the sqlite backend is active -
every attribute is the real thing, including module globals like
persistence.DB_PATH that the existing test suite monkeypatches directly
(see tests/test_app_history.py, test_app_auth.py, test_otp_rate_limit.py).
A partial re-export (`from persistence import save_conversation, ...`)
would silently break exactly that monkeypatching, since the copied
function objects would still close over the *original* module's DB_PATH,
not a `persistence_store.DB_PATH` attribute set by a test.
"""

from __future__ import annotations

import os
import sys

_PROVIDER = os.environ.get("VOXBUDDY_PERSISTENCE_PROVIDER", "sqlite").lower()

if _PROVIDER == "sqlite":
    import persistence as _backend
elif _PROVIDER == "dynamodb":
    # No explicit static-credential check here: boto3's normal provider
    # chain resolves credentials on its own (env vars, ~/.aws/credentials,
    # or — as on an Elastic Beanstalk EC2 instance — an attached IAM
    # instance profile). If no credentials are resolvable at all, boto3
    # raises NoCredentialsError the first time a real DynamoDB call is
    # made, which surfaces just as clearly as this upfront check did.
    import persistence_dynamodb as _backend
else:
    raise ValueError(
        f"Unknown VOXBUDDY_PERSISTENCE_PROVIDER='{_PROVIDER}'. "
        f"Valid options: 'sqlite', 'dynamodb'."
    )

sys.modules[__name__] = _backend
