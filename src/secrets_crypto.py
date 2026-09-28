"""Encryption for secrets held at rest.

Marketplace OAuth tokens and API credentials let someone read and act on a
seller's real accounts. They are stored in the database, which in this
deployment is a SQLite file that gets copied into backups and nightly
snapshots — so plaintext columns would spread working credentials across every
backup.

Design notes
------------
* The key is derived from ``SECRET_KEY`` with a domain-separation label, so the
  variable that already protects sessions also protects stored secrets without
  being used raw as a key.
* Values are stored as ``enc:v1:<fernet token>``. The prefix makes the format
  self-describing and lets :func:`decrypt` pass through anything that is not
  ours, which is what makes migrating from the previous plaintext columns safe:
  old rows keep working until they are next written.
* Decryption never raises. A wrong or rotated ``SECRET_KEY`` returns the stored
  text rather than taking down login or a sync. Callers that need to tell
  "unreadable" from "readable" can check :func:`is_encrypted`.

SECRET_KEY is effectively immutable once secrets exist
------------------------------------------------------
Changing SECRET_KEY makes every encrypted value unreadable, and signatures
would be the only way to detect it — so this module always stamps the version
prefix and, on a failed decrypt, says so loudly instead of quietly returning an
empty credential that looks like "not connected".

WARNING to keep in mind before ever changing SECRET_KEY in production: users
would have to re-link their marketplace accounts. Session tokens (which carry a
signature) are likewise invalidated. The application regenerates SECRET_KEY
only when it is unset, and startup warns about that case.
"""

from __future__ import annotations

import base64
import hashlib
import logging
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

log = logging.getLogger(__name__)

PREFIX = "enc:v1:"

# Domain separation: this label is what stops the derived key from being the
# session key itself.
_KDF_LABEL = b"marketplace-dashboard:secrets:v1"

_fernet: Optional[Fernet] = None
_fernet_key_source: Optional[str] = None


def _secret_key() -> str:
    """The application secret, read lazily so tests can patch config."""
    from src.config import config

    return getattr(config, "SECRET_KEY", "") or ""


def _build_fernet() -> Optional[Fernet]:
    secret = _secret_key()
    if not secret:
        return None
    digest = hashlib.sha256(_KDF_LABEL + secret.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def _get_fernet() -> Optional[Fernet]:
    """Cached Fernet instance, rebuilt if SECRET_KEY changes."""
    global _fernet, _fernet_key_source
    current = _secret_key()
    if _fernet is None or _fernet_key_source != current:
        _fernet = _build_fernet()
        _fernet_key_source = current
    return _fernet


def is_encrypted(value: Optional[str]) -> bool:
    return isinstance(value, str) and value.startswith(PREFIX)


def encrypt(plaintext: Optional[str]) -> Optional[str]:
    """Encrypt a secret for storage.

    Returns the input unchanged when there is nothing to do (None or empty) or
    no key is configured, so a misconfigured instance degrades to the previous
    behaviour rather than losing credentials outright.
    """
    if plaintext is None or plaintext == "":
        return plaintext
    if is_encrypted(plaintext):
        return plaintext  # already encrypted; do not double-wrap

    fernet = _get_fernet()
    if fernet is None:
        log.warning(
            "SECRET_KEY is not set; marketplace credentials are being stored "
            "unencrypted. Set SECRET_KEY to enable encryption at rest."
        )
        return plaintext

    token = fernet.encrypt(plaintext.encode("utf-8"))
    return PREFIX + token.decode("ascii")


def decrypt(stored: Optional[str]) -> Optional[str]:
    """Decrypt a stored secret. Never raises.

    A value that is not in our format — including rows written before
    encryption existed — is returned as-is.
    """
    if stored is None or stored == "":
        return stored
    if not is_encrypted(stored):
        return stored

    fernet = _get_fernet()
    if fernet is None:
        log.warning("Encrypted value found but SECRET_KEY is not set")
        return stored

    token = stored[len(PREFIX):]
    try:
        return fernet.decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError, TypeError):
        log.error(
            "Could not decrypt a stored marketplace credential. This normally "
            "means SECRET_KEY changed after the value was written, in which "
            "case the account must be re-linked."
        )
        return stored


def mask(value: Optional[str], keep: int = 4) -> str:
    """A display-safe form of a secret: ``••••abcd``.

    Used by the API so a settings page can show that something is configured
    without ever sending the value to the browser.
    """
    if not value:
        return ""
    plain = decrypt(value) or ""
    if len(plain) <= keep:
        return "•" * len(plain)
    return "•" * 8 + plain[-keep:]
