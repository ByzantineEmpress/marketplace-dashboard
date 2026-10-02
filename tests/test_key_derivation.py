"""The key-derivation change, and the migration that has to come with it.

Changing how the key is derived makes every existing ciphertext unreadable unless
the old key is kept and the stored values are re-written. That is the whole risk
here: a mistake does not produce a wrong answer, it permanently destroys the
marketplace credentials of every connected account, and the only recovery is
asking each seller to link their account again.

So these check that the old key still opens old values, that new values use the
new key, that the migration moves them, and that anything unreadable is left
alone rather than overwritten.
"""

import secrets
import unittest

from tests import _env  # noqa: E402,F401  isort:skip  (must precede src imports)

from src import secrets_crypto as sc
from src.database import SessionLocal, init_db
from src.models import MarketplaceAccount, User, UserMarketplaceCredential


class KeyDerivationTest(unittest.TestCase):
    def setUp(self):
        sc._fernets.clear()

    def test_new_values_use_the_current_key(self):
        stored = sc.encrypt("hunter2")
        self.assertTrue(stored.startswith(sc.PREFIX_V2))
        self.assertEqual(sc.stored_version(stored), "v2")
        self.assertEqual(sc.decrypt(stored), "hunter2")

    def test_a_value_written_under_the_old_key_still_opens(self):
        """The point of the version prefix: the key that wrote a value is the key
        that reads it, so no account has to be re-linked."""
        from src.config import config

        legacy = sc._legacy_key(config.SECRET_KEY)
        import base64

        from cryptography.fernet import Fernet

        token = Fernet(base64.urlsafe_b64encode(legacy)).encrypt(b"old-secret")
        stored = sc.PREFIX_V1 + token.decode("ascii")

        self.assertEqual(sc.stored_version(stored), "v1")
        self.assertEqual(sc.decrypt(stored), "old-secret")

    def test_the_two_keys_are_actually_different(self):
        """If they were not, this change would be a no-op dressed up as a fix."""
        from src.config import config

        self.assertNotEqual(sc._legacy_key(config.SECRET_KEY),
                            sc._current_key(config.SECRET_KEY))

    def test_an_undecryptable_value_is_returned_unchanged_not_emptied(self):
        """A wrong SECRET_KEY must not look like "not connected"."""
        stored = sc.PREFIX_V2 + "not-a-real-fernet-token"
        self.assertEqual(sc.decrypt(stored), stored)

    def test_plain_values_pass_through(self):
        """Rows written before encryption existed still work."""
        self.assertEqual(sc.decrypt("plain-value"), "plain-value")
        self.assertIsNone(sc.stored_version("plain-value"))

    def test_encrypting_twice_does_not_double_wrap(self):
        once = sc.encrypt("x")
        self.assertEqual(sc.encrypt(once), once)

    def test_the_cost_of_the_new_derivation_is_the_point(self):
        """PBKDF2 is deliberately slow; a bare SHA-256 is not. Guards against the
        iterations being quietly dropped back to something cheap."""
        self.assertGreaterEqual(sc.PBKDF2_ITERATIONS, 600_000)

    def test_the_mask_keeps_its_shape(self):
        bullet = "\u2022"
        self.assertEqual(sc.mask(sc.encrypt("abcdefgh")), bullet * 8 + "efgh")
        self.assertEqual(sc.mask(""), "")


class MigrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()

    def setUp(self):
        sc._fernets.clear()
        self.db = SessionLocal()
        self.user = User(email=f"kdf.{secrets.token_hex(4)}@example.com",
                         name="KDF Tester", provider="google", is_admin=False)
        self.db.add(self.user)
        self.db.commit()

    def tearDown(self):
        uid = self.user.id
        self.db.rollback()
        self.db.query(UserMarketplaceCredential).filter(
            UserMarketplaceCredential.user_id == uid).delete(synchronize_session=False)
        self.db.query(MarketplaceAccount).filter(
            MarketplaceAccount.user_id == uid).delete(synchronize_session=False)
        self.db.query(User).filter(User.id == uid).delete(synchronize_session=False)
        self.db.commit()
        self.db.close()

    def _legacy_ciphertext(self, plaintext: str) -> str:
        import base64

        from cryptography.fernet import Fernet

        from src.config import config

        key = sc._legacy_key(config.SECRET_KEY)
        token = Fernet(base64.urlsafe_b64encode(key)).encrypt(plaintext.encode())
        return sc.PREFIX_V1 + token.decode("ascii")

    def _raw(self, table: str, column: str):
        from sqlalchemy import text

        return dict(self.db.execute(text(f"SELECT id, {column} FROM {table}")).fetchall())

    def test_a_legacy_credential_is_moved_to_the_new_key(self):
        self.db.add(UserMarketplaceCredential(
            user_id=self.user.id, platform="ebay", credential_key="client_secret",
            value=self._legacy_ciphertext("super-secret")))
        self.db.commit()

        moved = sc.migrate_legacy_values(self.db)
        self.assertEqual(moved, 1)

        raw = list(self._raw("user_marketplace_credentials", "value").values())[0]
        self.assertTrue(raw.startswith(sc.PREFIX_V2))
        self.assertNotIn(sc.PREFIX_V1, raw)

        # And it still reads back as the same secret.
        row = self.db.query(UserMarketplaceCredential).filter(
            UserMarketplaceCredential.user_id == self.user.id).first()
        self.assertEqual(row.value, "super-secret")

    def test_an_account_token_is_migrated_too(self):
        self.db.add(MarketplaceAccount(
            user_id=self.user.id, platform="etsy",
            access_token=self._legacy_ciphertext("access-abc"),
            refresh_token=self._legacy_ciphertext("refresh-xyz")))
        self.db.commit()

        self.assertEqual(sc.migrate_legacy_values(self.db), 2)

        raws = self._raw("marketplace_accounts", "access_token")
        self.assertTrue(all(v.startswith(sc.PREFIX_V2) for v in raws.values()))

        account = self.db.query(MarketplaceAccount).filter(
            MarketplaceAccount.user_id == self.user.id).first()
        self.assertEqual(account.access_token, "access-abc")
        self.assertEqual(account.refresh_token, "refresh-xyz")

    def test_the_migration_is_idempotent(self):
        self.db.add(UserMarketplaceCredential(
            user_id=self.user.id, platform="ebay", credential_key="client_secret",
            value=self._legacy_ciphertext("s")))
        self.db.commit()

        self.assertEqual(sc.migrate_legacy_values(self.db), 1)
        self.assertEqual(sc.migrate_legacy_values(self.db), 0)

    def test_an_unreadable_value_is_left_alone(self):
        """It is unreadable either way; overwriting it would destroy the only
        copy of whatever the seller's account needs."""
        broken = sc.PREFIX_V1 + "not-a-real-token"
        self.db.add(UserMarketplaceCredential(
            user_id=self.user.id, platform="ebay", credential_key="client_secret",
            value="placeholder"))
        self.db.commit()
        # Written raw, because the column type would encrypt on the way in and
        # the point is to plant a v1 value the migration cannot read.
        from sqlalchemy import text

        self.db.execute(
            text("UPDATE user_marketplace_credentials SET value = :v WHERE user_id = :u"),
            {"v": broken, "u": self.user.id},
        )
        self.db.commit()

        self.assertEqual(sc.migrate_legacy_values(self.db), 0)
        raws = self._raw("user_marketplace_credentials", "value")
        self.assertEqual(list(raws.values()), [broken])


if __name__ == "__main__":
    unittest.main(verbosity=2)
