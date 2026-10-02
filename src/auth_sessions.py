"""Login sessions, with the bearer token never stored.

The session cookie IS the credential: whoever holds it is the user until it
expires. Those rows live in the database, which this deployment copies into
nightly snapshots and backups, and a stored token is usable the moment it is
read — there is no cracking step to slow anyone down. Storing it verbatim means
one leaked backup is every live session.

So only a hash is stored. SHA-256 is the right choice here rather than bcrypt:
the token is 48 bytes from ``secrets.token_urlsafe``, so there is no dictionary to
guess and nothing for a slow hash to defend against. That is the same reasoning
OWASP gives for high-entropy session identifiers — a slow KDF buys nothing when
the input is 384 bits of randomness, and it would cost a hash on every request.

Rows written before this change hold the token itself. They are matched once, by
:func:`find`, and rewritten as a hash in passing, so no session is logged out and
the plaintext does not survive the next use.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta
from typing import Optional

# Prefix rather than a bare digest: a token and a digest are both 64 characters,
# so without it a legacy row and a hashed one are indistinguishable, and matching
# a raw token against a digest would silently never succeed.
HASH_PREFIX = "sha256:"

SESSION_DAYS = 7


def hash_token(token: str) -> str:
    return HASH_PREFIX + hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_token() -> str:
    """A fresh cookie value: 48 bytes, 64 URL-safe characters."""
    return secrets.token_urlsafe(48)


def create(db, user_id: int, days: int = SESSION_DAYS) -> str:
    """Start a session and return the token to put in the cookie.

    The token is returned, never stored; only its hash goes to the database.
    """
    from src.models import AuthSession

    token = new_token()
    db.add(AuthSession(
        token=hash_token(token),
        user_id=user_id,
        expires_at=datetime.utcnow() + timedelta(days=days),
    ))
    db.commit()
    return token


def find(db, token: str):
    """The session for a presented token, or None.

    Falls back to a plaintext match for rows written before hashing existed, and
    upgrades such a row to a hash on the way out.
    """
    from src.models import AuthSession

    if not token:
        return None

    hashed = hash_token(token)
    row = db.query(AuthSession).filter(AuthSession.token == hashed).first()
    if row:
        return row

    legacy = db.query(AuthSession).filter(AuthSession.token == token).first()
    if legacy is None:
        return None

    # Rewrite in place. A failure here must not stop the login: the session is
    # valid either way, it just keeps its older form.
    try:
        legacy.token = hashed
        db.commit()
    except Exception:
        db.rollback()
    return legacy


def hash_existing(db) -> int:
    """Rewrite any session token still stored verbatim as a digest.

    Rows upgrade lazily on next use, but "next use" can be days away — and a
    backup taken in the meantime still holds live sessions, which is the whole
    problem this change exists to remove. So they are converted now.

    Safe to run repeatedly and safe to interrupt: a row is either already a
    digest, in which case it is skipped, or it is not, in which case hashing it
    leaves the holder's cookie working, because the cookie is hashed on the way
    in and produces the same digest.
    """
    from src.models import AuthSession

    migrated = 0
    try:
        rows = db.query(AuthSession).all()
    except Exception:
        return 0

    for row in rows:
        token = row.token or ""
        if not token or token.startswith(HASH_PREFIX):
            continue
        row.token = hash_token(token)
        migrated += 1

    if migrated:
        try:
            db.commit()
        except Exception:
            db.rollback()
            return 0
    return migrated


def destroy(db, token: str) -> int:
    """Log out: delete the session for a presented token, either form."""
    from src.models import AuthSession

    if not token:
        return 0
    deleted = (
        db.query(AuthSession)
        .filter(AuthSession.token.in_([hash_token(token), token]))
        .delete(synchronize_session=False)
    )
    db.commit()
    return deleted
