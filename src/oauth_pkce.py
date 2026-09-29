"""PKCE (RFC 7636) helpers.

Etsy requires PKCE on every authorization flow: "The Etsy Open API requires a
PKCE on every authorization flow request." Without ``code_challenge`` in the
authorization URL and the matching ``code_verifier`` in the token request, the
flow cannot complete at all.

PKCE also strictly improves the flow for any provider: the authorization code is
useless to anyone who intercepts it, because redeeming it requires the verifier,
which never leaves this server.

The verifier is a secret for the duration of one login. It is held in a
short-lived HttpOnly cookie rather than the database: it is needed exactly once,
a few seconds later, and keeping it out of storage means there is nothing to
leak from a backup.

Only ``S256`` is generated. Etsy requires ``code_challenge_method=S256``
explicitly, and ``plain`` provides no protection.
"""

from __future__ import annotations

import base64
import hashlib
import secrets

# RFC 7636 allows 43-128 characters from an unreserved alphabet. token_urlsafe
# of 64 bytes yields 86 characters, inside that range and comfortably random.
_VERIFIER_BYTES = 64

# Cookie names. The state cookie already exists for Google under
# "google_oauth_state"; marketplace flows get their own so the two can never be
# confused for one another.
STATE_COOKIE = "marketplace_oauth_state"
VERIFIER_COOKIE = "marketplace_oauth_verifier"


def new_verifier() -> str:
    """A fresh code verifier."""
    return secrets.token_urlsafe(_VERIFIER_BYTES)


def challenge_for(verifier: str) -> str:
    """The S256 challenge for a verifier.

    base64url without padding, as RFC 7636 requires.
    """
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def new_state() -> str:
    """A fresh, single-use CSRF state value."""
    return secrets.token_urlsafe(32)
