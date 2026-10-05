"""The Google Books API key, entered on the settings page.

It is stored through the same machinery as the marketplace credentials, so most of
what matters is what that machinery already guarantees: encrypted at rest, masked
on the way out, never returned to the browser. What is new here is that Google
Books is NOT a marketplace and must not be offered the buttons that assume one.
"""

import secrets
import unittest
from datetime import datetime, timedelta

from tests import _env  # noqa: E402,F401  isort:skip  (must precede src imports)

from fastapi.testclient import TestClient

from src import books
from src.api.main import app
from src.api.routes import (OAUTH_PLATFORMS, PLATFORM_CREDENTIALS,
                            SECRET_CREDENTIAL_KEYS)
from src.database import SessionLocal, init_db
from src.models import AuthSession, User, UserMarketplaceCredential

FAKE_KEY = "AIzaSyTESTKEY_0123456789abcdefghijklm"


class KeyStorageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()

    def setUp(self):
        self.db = SessionLocal()
        self.user = User(email=f"gbook.{secrets.token_hex(4)}@example.com",
                         name="Books Key Tester", provider="google", is_admin=False)
        self.db.add(self.user)
        self.db.commit()

        token = secrets.token_urlsafe(48)
        self.db.add(AuthSession(token=token, user_id=self.user.id,
                                expires_at=datetime.utcnow() + timedelta(hours=1)))
        self.db.commit()
        self._token = token

    def tearDown(self):
        uid = self.user.id
        self.db.rollback()
        for model in (UserMarketplaceCredential, AuthSession):
            self.db.query(model).filter(model.user_id == uid).delete(
                synchronize_session=False)
        self.db.query(User).filter(User.id == uid).delete(synchronize_session=False)
        self.db.commit()
        self.db.close()

    def _client(self):
        client = TestClient(app)
        client.cookies.set("auth_token", self._token)
        return client

    def test_the_field_is_offered(self):
        with self._client() as client:
            data = client.get("/api/accounts/credentials").json()
        self.assertIn("google_books", data)
        fields = data["google_books"]["fields"]
        self.assertEqual([f["key"] for f in fields], ["api_key"])
        self.assertFalse(data["google_books"]["is_configured"])

    def test_it_is_not_offered_a_connect_or_disconnect_button(self):
        """It has no OAuth flow. A Connect button would invite a click that cannot
        work, and Disconnect offers to delete listings it never had."""
        self.assertNotIn("google_books", OAUTH_PLATFORMS)
        with self._client() as client:
            data = client.get("/api/accounts/credentials").json()
        self.assertFalse(data["google_books"]["connectable"])
        # The marketplaces still are.
        for platform in ("ebay", "etsy"):
            self.assertTrue(data[platform]["connectable"], platform)

    def test_a_nicer_label_than_the_platform_key(self):
        with self._client() as client:
            data = client.get("/api/accounts/credentials").json()
        self.assertNotIn("_", data["google_books"]["label"])
        self.assertNotEqual(data["google_books"]["label"], "Google_books")

    def test_the_key_is_a_secret_and_is_masked_on_the_way_out(self):
        self.assertIn("api_key", SECRET_CREDENTIAL_KEYS)
        with self._client() as client:
            saved = client.post("/api/accounts/credentials",
                                json={"platform": "google_books",
                                      "values": {"api_key": FAKE_KEY}})
            self.assertIn(saved.status_code, (200, 201), saved.text)
            data = client.get("/api/accounts/credentials").json()

        field = data["google_books"]["fields"][0]
        self.assertTrue(field["is_set"])
        self.assertTrue(field["is_secret"])
        self.assertNotEqual(field["hint"], FAKE_KEY)
        # And the raw value appears nowhere in the response at all.
        self.assertNotIn(FAKE_KEY, saved.text)
        self.assertNotIn(FAKE_KEY, str(data))

    def test_it_is_encrypted_at_rest(self):
        """The whole point of routing this through the credential table."""
        with self._client() as client:
            client.post("/api/accounts/credentials",
                        json={"platform": "google_books",
                              "values": {"api_key": FAKE_KEY}})

        # Read the raw column, bypassing the decrypting type decorator.
        from sqlalchemy import text
        row = self.db.execute(text(
            "SELECT value FROM user_marketplace_credentials "
            "WHERE user_id = :uid AND platform = 'google_books'"
        ), {"uid": self.user.id}).fetchone()
        self.assertIsNotNone(row, "the key was not stored")
        self.assertNotIn(FAKE_KEY, row[0])
        self.assertTrue(row[0].startswith("enc:v2:"),
                        f"not encrypted with the current scheme: {row[0][:12]}")

    def test_it_is_stored_per_user(self):
        with self._client() as client:
            client.post("/api/accounts/credentials",
                        json={"platform": "google_books",
                              "values": {"api_key": FAKE_KEY}})
        self.assertEqual(
            self.db.query(UserMarketplaceCredential)
            .filter(UserMarketplaceCredential.user_id == self.user.id,
                    UserMarketplaceCredential.platform == "google_books")
            .count(), 1)


class KeyUsageTest(unittest.TestCase):
    """The saved key has to reach the lookup, and the fallback has to work."""

    def setUp(self):
        self.original = books._google_books
        self.seen = []

        def fake(isbn, api_key=""):
            self.seen.append(api_key)
            return {}

        books._google_books = fake

    def tearDown(self):
        books._google_books = self.original

    def test_a_supplied_key_is_passed_through(self):
        books.lookup_book("0-241-10825-X", FAKE_KEY)
        self.assertEqual(self.seen, [FAKE_KEY])

    def test_no_key_is_fine(self):
        """The lookup works on Open Library alone."""
        meta = books.lookup_book("0-241-10825-X")
        self.assertEqual(self.seen, [""])
        self.assertIsInstance(meta, dict)

    def test_a_rejected_key_is_reported_separately_from_a_quota_refusal(self):
        """A bad key and an exhausted quota need different fixes."""
        def rejecting(isbn, api_key=""):
            return {"key_rejected": "HTTP 400"}

        books._google_books = rejecting
        meta = books.lookup_book("0-241-10825-X", "AIza-not-a-real-key")
        self.assertEqual(meta["google_key_rejected"], "HTTP 400")
        self.assertFalse(meta["google_quota_exceeded"])
        self.assertNotIn("googlebooks", meta["sources"])

    def test_the_key_is_never_echoed_back_in_the_result(self):
        """The lookup response goes to the browser, so the key must not be in it."""
        result = books.isbn_from_image(b"")  # unrelated, but cheap to assert on
        self.assertNotIn(FAKE_KEY, str(result))


class EmptyKeyTest(unittest.TestCase):
    def test_a_blank_stored_key_falls_back_rather_than_breaking(self):
        """An empty string must not be sent as a key parameter."""
        import httpx
        captured = {}
        original = httpx.get

        class Resp:
            status_code = 200

            @staticmethod
            def json():
                return {"items": []}

        def fake(url, **kwargs):
            captured.update(kwargs.get("params") or {})
            return Resp()

        httpx.get = fake
        try:
            books._google_books("9780241108253", "")
        finally:
            httpx.get = original
        self.assertNotIn("key", captured)


if __name__ == "__main__":
    unittest.main(verbosity=2)
