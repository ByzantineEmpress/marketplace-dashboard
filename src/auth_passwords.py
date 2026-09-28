"""Password hashing for local (email + password) accounts.

Uses the ``bcrypt`` package directly rather than ``passlib``: passlib 1.7.4
reads ``bcrypt.__about__.__version__``, which bcrypt 4.1+ removed, so passlib
raises on this environment. Depending on it would break every password login.

Passwords are pre-hashed with SHA-256 and base64-encoded before bcrypt sees
them. bcrypt silently ignores everything past 72 bytes, which would make two
different long passwords interchangeable; the fixed-width pre-hash removes
that truncation entirely.
"""

import base64
import hashlib

import bcrypt

# bcrypt rounds. 12 is a sensible interactive default on modern hardware.
BCRYPT_ROUNDS = 12

# Minimum length we accept for a new password.
MIN_PASSWORD_LENGTH = 8

# A hard upper bound so a huge body cannot be used to burn CPU in bcrypt.
MAX_PASSWORD_LENGTH = 1024


def _prepare(password: str) -> bytes:
    """Reduce any password to a fixed 44-byte, bcrypt-safe token."""
    digest = hashlib.sha256(password.encode("utf-8")).digest()
    return base64.b64encode(digest)


def hash_password(password: str) -> str:
    """Return a bcrypt hash for ``password``."""
    salt = bcrypt.gensalt(rounds=BCRYPT_ROUNDS)
    return bcrypt.hashpw(_prepare(password), salt).decode("ascii")


def verify_password(password: str, password_hash: str) -> bool:
    """Check ``password`` against a stored hash.

    Returns False for malformed or empty hashes instead of raising, so a
    corrupt row cannot turn a login attempt into a 500.
    """
    if not password or not password_hash:
        return False
    try:
        return bcrypt.checkpw(_prepare(password), password_hash.encode("ascii"))
    except (ValueError, TypeError):
        return False


def validate_password_strength(password) -> str:
    """Return an error message for an unacceptable password, else ""."""
    if not isinstance(password, str) or not password:
        return "Choose a password"
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"Password must be at least {MIN_PASSWORD_LENGTH} characters"
    if len(password) > MAX_PASSWORD_LENGTH:
        return "Password is too long"
    return ""
