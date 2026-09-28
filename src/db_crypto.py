"""SQLAlchemy column type that encrypts on write and decrypts on read.

Using a column type rather than calling encrypt/decrypt by hand means a newly
added credential column cannot accidentally be stored in plaintext: the default
is safe, and forgetting to encrypt requires actively choosing a different type.
"""

from __future__ import annotations

from sqlalchemy import Text
from sqlalchemy.types import TypeDecorator

from src import secrets_crypto


class EncryptedText(TypeDecorator):
    """A Text column whose value is encrypted at rest.

    Reads pass through :func:`secrets_crypto.decrypt`, which tolerates values
    that were written before this type existed, so introducing it requires no
    data migration.
    """

    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        """Python -> database."""
        return secrets_crypto.encrypt(value)

    def process_result_value(self, value, dialect):
        """Database -> Python."""
        return secrets_crypto.decrypt(value)
