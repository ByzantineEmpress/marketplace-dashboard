"""Test-environment defaults, applied before the app is imported.

Import this FIRST, before anything from ``src``:

    from tests import _env  # noqa: F401  (must precede the src imports)
    from src.api.main import app

Why this exists
---------------
``src.config`` reads the environment at import time, and ``load_dotenv`` does
not override variables that are already set. So whatever is in ``.env`` decides
how the app behaves inside the tests — and that made the suite's result depend
on whose machine it ran on.

The failure this fixes
----------------------
With ``REQUIRE_HTTPS=true`` — the production setting — the login route marks the
auth cookie ``Secure``. The Starlette TestClient does not carry a ``Secure``
cookie back over its ``http://testserver`` transport, so every authenticated
request arrived unauthenticated: the dashboard returned the login page with
``auth_required``, and roughly twenty tests failed with diffs like
"'listing-grid' not found in ..." that pointed nowhere near the real cause.

The suite had only ever passed on a development machine whose ``.env`` set
``REQUIRE_HTTPS=false``. Against a production-configured environment it failed,
so it could never have been green in CI. Adding ``tests/_env.py`` makes that
assumption explicit instead of accidental.

This is deliberately assigned rather than ``setdefault``: the point is that the
suite's behaviour does not depend on ambient configuration, and an inherited
``REQUIRE_HTTPS=true`` (from a production ``.env``) must not silently change
what is being tested.

HTTPS and plain-HTTP behaviour is still covered — ``tests/test_transport_security.py``
sets ``config.REQUIRE_HTTPS`` itself and asserts the cookie flags, HSTS header
and bypass warning directly.
"""

import os

# The TestClient speaks http://, so the suite must run with Secure cookies off.
os.environ["REQUIRE_HTTPS"] = "false"

# The dev-mode Google path lets tests sign in without contacting Google.
os.environ.setdefault("GOOGLE_DEV_MODE", "true")

# A client ID must be present for the OAuth flow to start at all: with it empty,
# /auth/google refuses before issuing the state cookie, so every callback test
# fails with "security state mismatch" instead of the outcome it asserts. A
# fresh checkout in CI has no .env, which is what made a green local run red
# there.
#
# Checked for emptiness rather than with setdefault: CI exports this as an empty
# string, and setdefault treats a set-but-empty variable as present. A real
# value is still respected. Nothing is contacted — the tests stub the HTTP layer.
if not os.environ.get("GOOGLE_CLIENT_ID"):
    os.environ["GOOGLE_CLIENT_ID"] = "test-client-id.apps.googleusercontent.com"

# Keep tests off whatever database the developer has open. A caller that wants a
# specific path still wins; CI sets a fresh temp file per run.
os.environ.setdefault("DATABASE_URL", "sqlite:///./test-marketplace.db")
