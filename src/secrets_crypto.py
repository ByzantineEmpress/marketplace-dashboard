"""Encryption for secrets held at rest.

Marketplace OAuth tokens and API credentials let someone read and act on a
seller's real accounts. They are stored in the database, which in this deployment
is a SQLite file that gets copied into backups and nightly snapshots, so plaintext
columns would spread working credentials across every backup.

Design notes
------------
* The key is derived from ``SECRET_KEY`` with a domain-separation label, so the
  variable that already protects sessions also protects stored secrets without
  being used raw as a key.
* Values are stored as ``enc:v2:<fernet token>``. The prefix makes the format
  self-describing, which is what lets a stored value be read with the key it was
  actually written with after the derivation changed. Anything without a prefix
  is passed through, so rows written before encryption existed keep working.
* Decryption never raises. A wrong or rotated ``SECRET_KEY`` returns the stored
  text rather than taking down login or a sync. Callers that need to tell
  "unreadable" from "readable" can check :func:`is_encrypted`.

The key derivation changed, and old values still read
-----------------------------------------------------
v1 derived the key with a bare SHA-256. That is fast, which is the property you
do not want in a key derivation: if SECRET_KEY is a human-chosen string rather
than 32 random bytes, an attacker holding a backup can test guesses at billions
per second. v2 uses PBKDF2-HMAC-SHA256, OWASP's recommendation for this
construction, which makes each guess cost that much more.

The salt is fixed and domain-separated rather than random per value. A random
salt would have to be stored with each ciphertext, and it buys nothing here: the
input is an application secret, not a password, so the salt's job is separation
between uses rather than defeating a rainbow table. Randomness would also make
the derived key unreproducible, and then every value would need its own key.

v1 values stay readable, and :func:`migrate_legacy_values` re-writes them as v2.
Until that has run, both keys exist in memory and the prefix decides which is
used, so a failure to migrate degrades to "still readable" rather than "lost".
"""

from __future__ import annotations

import base64
import hashlib
import logging
from typing import Optional, Tuple

from cryptography.fernet import Fernet, InvalidToken

log = logging.getLogger(__name__)

PREFIX_V1 = "enc:v1:"
PREFIX_V2 = "enc:v2:"
# Kept as the name callers already use, and as the marker that a value is ours.
PREFIX = PREFIX_V2

# Domain separation. Different labels mean the secrets key is not the key used for
# anything else derived from SECRET_KEY.
_LABEL_V1 = b"marketplace-dashboard:secrets:v1"
_LABEL_V2 = b"marketplace-dashboard:secrets:v2"

# OWASP's current PBKDF2-HMAC-SHA256 figure. Paid once per process: the derived
# key is cached, and the secret is not re-derived per request or per value.
PBKDF2_ITERATIONS = 600_000

_fernets: dict = {}


def _secret_key() -> str:
    """The application secret, read lazily so tests can patch config."""
    from src.config import config

    return getattr(config, "SECRET_KEY", "") or ""


def _legacy_key(secret: str) -> bytes:
    """v1: a bare SHA-256. Kept only so existing values stay readable."""
    return hashlib.sha256(_LABEL_V1 + secret.encode("utf-8")).digest()


def _current_key(secret: str) -> bytes:
    """v2: PBKDF2-HMAC-SHA256 over the application secret."""
    return hashlib.pbkdf2_hmac(
        "sha256", secret.encode("utf-8"), _LABEL_V2,
        PBKDF2_ITERATIONS, dklen=32,
    )


def _fernet_for(version: str) -> Optional[Fernet]:
    """The cached Fernet for a stored-value version, or None without a key."""
    secret = _secret_key()
    if not secret:
        return None

    cache_key = (version, secret)
    if cache_key not in _fernets:
        raw = _legacy_key(secret) if version == "v1" else _current_key(secret)
        _fernets[cache_key] = Fernet(base64.urlsafe_b64encode(raw))
    return _fernets[cache_key]


def is_encrypted(value: Optional[str]) -> bool:
    return isinstance(value, str) and (
        value.startswith(PREFIX_V1) or value.startswith(PREFIX_V2))


def stored_version(value: Optional[str]) -> Optional[str]:
    """``"v1"``, ``"v2"``, or None when the value is not ours."""
    if not isinstance(value, str):
        return None
    if value.startswith(PREFIX_V1):
        return "v1"
    if value.startswith(PREFIX_V2):
        return "v2"
    return None


def encrypt(plaintext: Optional[str]) -> Optional[str]:
    """Encrypt a secret for storage, always under the current key.

    Returns the input unchanged when there is nothing to do (None or empty) or no
    key is configured, so a misconfigured instance degrades to the previous
    behaviour rather than losing credentials outright.
    """
    if plaintext is None or plaintext == "":
        return plaintext
    if is_encrypted(plaintext):
        return plaintext  # already ours; do not double-wrap

    fernet = _fernet_for("v2")
    if fernet is None:
        log.warning(
            "SECRET_KEY is not set; marketplace credentials are being stored "
            "unencrypted. Set SECRET_KEY to enable encryption at rest."
        )
        return plaintext

    token = fernet.encrypt(plaintext.encode("utf-8"))
    return PREFIX_V2 + token.decode("ascii")


def _decrypt_with(version: str, stored: str) -> Optional[str]:
    """Decrypt with one specific key. None means it did not work."""
    fernet = _fernet_for(version)
    if fernet is None:
        return None
    body = stored[len(PREFIX_V1):] if version == "v1" else stored[len(PREFIX_V2):]
    try:
        return fernet.decrypt(body.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError, TypeError):
        return None


def decrypt(stored: Optional[str]) -> Optional[str]:
    """Decrypt a stored secret. Never raises.

    A value that is not in our format, including rows written before encryption
    existed, is returned as-is. A value that cannot be decrypted is also returned
    as-is, and logged, because the alternative is handing back an empty credential
    that reads as "not connected" and hides the real problem.
    """
    if stored is None or stored == "":
        return stored

    version = stored_version(stored)
    if version is None:
        return stored

    if not _secret_key():
        log.warning("Encrypted value found but SECRET_KEY is not set")
        return stored

    plain = _decrypt_with(version, stored)
    if plain is not None:
        return plain

    log.error(
        "Could not decrypt a stored marketplace credential with the %s key. This "
        "normally means SECRET_KEY changed after the value was written, in which "
        "case the account must be re-linked.", version,
    )
    return stored


def mask(value: Optional[str], keep: int = 4) -> str:
    """A display-safe form of a secret: ``\u2022\u2022\u2022\u2022abcd``.

    Used by the API so a settings page can show that something is configured
    without ever sending the value to the browser.

    The bullet is written as an escape rather than as the character itself, so
    this file stays ASCII. A plain-text editor rewriting the file with the wrong
    encoding once turned every non-ASCII character in a sibling module into
    mojibake, and a mask is not worth that risk.
    """
    if not value:
        return ""
    bullet = "\u2022"
    plain = decrypt(value) or ""
    if len(plain) <= keep:
        return bullet * len(plain)
    return bullet * 8 + plain[-keep:]


# -- Migration ------------------------------------------------------------
#
# The columns holding encrypted values, with the statement that reads each one.
#
# Written out in full rather than assembled from the table and column names. Two
# reasons, and the second is the one that matters: reading through the ORM would
# apply the decrypting column type and hand back plaintext, when what has to be
# inspected is the stored ciphertext — and building the SQL from variables means
# the statement text comes from data rather than from source. Naming each
# statement makes the set of columns being rewritten auditable at a glance.
def _encrypted_columns():
    from sqlalchemy import text

    from src.models import MarketplaceAccount, UserMarketplaceCredential

    return (
        (MarketplaceAccount, "access_token",
         text("SELECT id, access_token FROM marketplace_accounts")),
        (MarketplaceAccount, "refresh_token",
         text("SELECT id, refresh_token FROM marketplace_accounts")),
        (UserMarketplaceCredential, "value",
         text("SELECT id, value FROM user_marketplace_credentials")),
    )


def migrate_legacy_values(db) -> int:
    """Re-write v1 values under the v2 key. Returns how many were moved.

    A value that will not decrypt is left exactly as it is. It is unreadable
    either way, and overwriting it would destroy the only copy of whatever the
    seller's account needs.
    """
    from sqlalchemy import update

    migrated = 0
    for model, column, statement in _encrypted_columns():
        try:
            rows = db.execute(statement).fetchall()
        except Exception:
            # A table that is not there yet is not a failure worth stopping for.
            db.rollback()
            continue

        for row_id, stored in rows:
            if not isinstance(stored, str) or not stored.startswith(PREFIX_V1):
                continue
            plain = decrypt(stored)
            if plain is None or plain == stored:
                log.warning(
                    "Leaving an unreadable v1 value in place rather than "
                    "overwriting the only copy of it.",
                )
                continue
            # Written through the model, so the column type does the encrypting
            # under the current key rather than this function assembling
            # ciphertext by hand.
            db.execute(
                update(model).where(model.id == row_id).values({column: plain})
            )
            migrated += 1

        try:
            db.commit()
        except Exception:
            db.rollback()

    return migrated
