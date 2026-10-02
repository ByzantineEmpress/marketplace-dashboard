"""Session tokens are stored hashed, never verbatim.

The cookie is the credential, and the database is copied into backups, so a
stored token is a live session handed over by any leak. These check the hash is
what lands in the table, that an older row still works and is upgraded, and that
logging out revokes either form.
"""

import re
import secrets
import unittest
from datetime import datetime, timedelta

from tests import _env  # noqa: E402,F401  isort:skip  (must precede src imports)

from src import auth_sessions
from src.database import SessionLocal, init_db
from src.models import AuthSession, User


class SessionTokenStorageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()

    def setUp(self):
        self.db = SessionLocal()
        self.user = User(email=f"sess.{secrets.token_hex(4)}@example.com",
                         name="Session Tester", provider="google", is_admin=False)
        self.db.add(self.user)
        self.db.commit()

    def tearDown(self):
        uid = self.user.id
        self.db.rollback()
        self.db.query(AuthSession).filter(AuthSession.user_id == uid).delete(synchronize_session=False)
        self.db.query(User).filter(User.id == uid).delete(synchronize_session=False)
        self.db.commit()
        self.db.close()

    def _stored(self):
        return [r.token for r in self.db.query(AuthSession)
                .filter(AuthSession.user_id == self.user.id).all()]

    def test_the_token_itself_is_never_written(self):
        token = auth_sessions.create(self.db, self.user.id)
        stored = self._stored()

        self.assertEqual(len(stored), 1)
        self.assertNotIn(token, stored, "the bearer token was stored verbatim")
        self.assertEqual(stored[0], auth_sessions.hash_token(token))
        self.assertTrue(stored[0].startswith("sha256:"))

    def test_the_stored_form_is_not_reversible_by_looking_at_it(self):
        """A digest, not an encoding: the token must not be recoverable."""
        token = auth_sessions.create(self.db, self.user.id)
        digest = self._stored()[0][len(auth_sessions.HASH_PREFIX):]
        self.assertTrue(re.fullmatch(r"[0-9a-f]{64}", digest))
        self.assertNotIn(token, digest)

    def test_a_session_is_found_by_its_token(self):
        token = auth_sessions.create(self.db, self.user.id)
        row = auth_sessions.find(self.db, token)
        self.assertIsNotNone(row)
        self.assertEqual(row.user_id, self.user.id)

    def test_a_wrong_token_finds_nothing(self):
        auth_sessions.create(self.db, self.user.id)
        self.assertIsNone(auth_sessions.find(self.db, secrets.token_urlsafe(48)))
        self.assertIsNone(auth_sessions.find(self.db, ""))
        self.assertIsNone(auth_sessions.find(self.db, None))

    def test_a_row_written_before_hashing_still_logs_in_and_is_upgraded(self):
        """Nobody should be logged out by this change, and the plaintext must not
        survive the next use either."""
        legacy = secrets.token_urlsafe(48)
        self.db.add(AuthSession(token=legacy, user_id=self.user.id,
                                expires_at=datetime.utcnow() + timedelta(days=1)))
        self.db.commit()

        row = auth_sessions.find(self.db, legacy)
        self.assertIsNotNone(row, "a pre-existing session was logged out")
        self.assertEqual(row.user_id, self.user.id)

        # And it is a hash now, so the plaintext is gone.
        self.assertEqual(self._stored(), [auth_sessions.hash_token(legacy)])

    def test_logout_revokes_a_hashed_session(self):
        token = auth_sessions.create(self.db, self.user.id)
        self.assertEqual(auth_sessions.destroy(self.db, token), 1)
        self.assertIsNone(auth_sessions.find(self.db, token))
        self.assertEqual(self._stored(), [])

    def test_logout_revokes_a_legacy_session_too(self):
        legacy = secrets.token_urlsafe(48)
        self.db.add(AuthSession(token=legacy, user_id=self.user.id,
                                expires_at=datetime.utcnow() + timedelta(days=1)))
        self.db.commit()

        auth_sessions.destroy(self.db, legacy)
        self.assertEqual(self._stored(), [], "a legacy session survived logout")

    def test_two_sessions_do_not_collide(self):
        first = auth_sessions.create(self.db, self.user.id)
        second = auth_sessions.create(self.db, self.user.id)
        self.assertNotEqual(first, second)
        self.assertEqual(len(set(self._stored())), 2)
        self.assertIsNotNone(auth_sessions.find(self.db, first))
        self.assertIsNotNone(auth_sessions.find(self.db, second))


if __name__ == "__main__":
    unittest.main(verbosity=2)
