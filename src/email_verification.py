"""Single-use email verification tokens.

The raw token goes in the emailed link; only its SHA-256 digest is stored. So
a database dump cannot be replayed to confirm somebody else's address, and a
token is worthless after one successful use.
"""

import hashlib
import secrets

# Byte length of the generated token (256 bits of entropy).
TOKEN_BYTES = 32

# How long a confirmation link stays valid.
VERIFICATION_TTL_MINUTES = 60

# How many times we will (re)send a confirmation mail for one signup.
MAX_VERIFICATION_SENDS = 5


def new_token() -> str:
    """Return a fresh URL-safe token."""
    return secrets.token_urlsafe(TOKEN_BYTES)


def token_fingerprint(token: str) -> str:
    """Return the hex SHA-256 of a token, for storage and lookup."""
    if not token:
        return ""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def tokens_match(candidate: str, stored_fingerprint: str) -> bool:
    """Constant-time comparison of a presented token against a stored digest."""
    return secrets.compare_digest(token_fingerprint(candidate), stored_fingerprint or "")
