"""eBay Marketplace Account Deletion endpoint.

eBay disables a keyset until this endpoint is registered and verified, so the
verification hash must be exactly right: SHA-256 of

    challengeCode + verificationToken + endpoint

in that order (eBay is explicit that the order matters). This suite pins that
contract and the notification handling.
"""

import hashlib
import json
import secrets
import unittest

from tests import _env  # noqa: E402,F401  isort:skip  (must precede src imports)

from fastapi.testclient import TestClient

from src import ebay_notifications
from src.api.main import app
from src.config import config
from src.database import SessionLocal, init_db
from src.models import MarketplaceAccount, User


class ChallengeHashTest(unittest.TestCase):
    def test_hash_is_sha256_hex_in_the_documented_order(self):
        challenge = "challenge-abc"
        token = "verification-token"
        endpoint = "https://example.com/api/ebay/account-deletion"
        expected = hashlib.sha256(
            f"{challenge}{token}{endpoint}".encode("utf-8")).hexdigest()
        self.assertEqual(
            ebay_notifications.challenge_response(challenge, token, endpoint),
            expected,
        )

    def test_order_matters(self):
        """A subtle reorder would make every verification fail silently."""
        challenge, token, endpoint = "a", "b", "c"
        correct = ebay_notifications.challenge_response(challenge, token, endpoint)
        wrong_order = hashlib.sha256(f"{token}{challenge}{endpoint}".encode()).hexdigest()
        self.assertNotEqual(correct, wrong_order)

    def test_deletion_endpoint_defaults_to_app_base_url(self):
        original_base = config.APP_BASE_URL
        original_override = config.EBAY_DELETION_ENDPOINT
        config.APP_BASE_URL = "https://duckduckdeals.ca"
        config.EBAY_DELETION_ENDPOINT = ""
        try:
            self.assertEqual(
                ebay_notifications.deletion_endpoint(),
                "https://duckduckdeals.ca/api/ebay/account-deletion",
            )
        finally:
            config.APP_BASE_URL = original_base
            config.EBAY_DELETION_ENDPOINT = original_override

    def test_parse_notification_is_defensive(self):
        info = ebay_notifications.parse_notification({
            "metadata": {"topic": "MARKETPLACE_ACCOUNT_DELETION"},
            "notification": {
                "notificationId": "n-1",
                "eventDate": "2025-01-01T00:00:00Z",
                "data": {"username": "seller123", "userId": "u-123"},
            },
        })
        self.assertEqual(info["username"], "seller123")
        self.assertEqual(info["user_id"], "u-123")

        # Not a dict at all must not raise.
        info2 = ebay_notifications.parse_notification("not-json")
        self.assertEqual(info2["username"], "")


class DeletionEndpointTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()
        cls._cm = TestClient(app)
        cls.client = cls._cm.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls._cm.__exit__(None, None, None)

    def setUp(self):
        self.original_token = config.EBAY_VERIFICATION_TOKEN
        config.EBAY_VERIFICATION_TOKEN = "test-token-123"

    def tearDown(self):
        config.EBAY_VERIFICATION_TOKEN = self.original_token

    def test_get_returns_the_challenge_response(self):
        challenge = "abc123"
        res = self.client.get("/api/ebay/account-deletion",
                              params={"challenge_code": challenge})
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertEqual(
            body["challengeResponse"],
            ebay_notifications.challenge_response(challenge, "test-token-123"),
        )

    def test_get_requires_a_challenge_code(self):
        res = self.client.get("/api/ebay/account-deletion")
        self.assertEqual(res.status_code, 400)

    def test_get_without_a_token_is_503_not_a_wrong_hash(self):
        config.EBAY_VERIFICATION_TOKEN = ""
        res = self.client.get("/api/ebay/account-deletion",
                              params={"challenge_code": "x"})
        self.assertEqual(res.status_code, 503)

    def test_post_records_and_acknowledges(self):
        res = self.client.post("/api/ebay/account-deletion", json={
            "metadata": {"topic": "MARKETPLACE_ACCOUNT_DELETION", "schemaVersion": "1.0"},
            "notification": {
                "notificationId": "n-test",
                "eventDate": "2025-01-01T00:00:00Z",
                "data": {"username": "deleted_user", "userId": "u-deleted"},
            },
        })
        self.assertEqual(res.status_code, 200)

        from src.models import EbayAccountDeletion
        db = SessionLocal()
        try:
            row = db.query(EbayAccountDeletion).filter_by(
                notification_id="n-test").first()
            self.assertIsNotNone(row)
            self.assertEqual(row.username, "deleted_user")
        finally:
            db.query(EbayAccountDeletion).filter_by(
                notification_id="n-test").delete(synchronize_session=False)
            db.commit()
            db.close()

    def test_post_malformed_body_still_acknowledges(self):
        """eBay retries non-2xx; a bad body must not cause a retry storm."""
        res = self.client.post(
            "/api/ebay/account-deletion",
            content=b"this is not json",
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(res.status_code, 200)

    def test_post_disconnects_a_matching_ebay_account(self):
        db = SessionLocal()
        user = User(email=f"del.{secrets.token_hex(4)}@example.com",
                    name="Del Tester", provider="google", is_admin=False)
        db.add(user)
        db.flush()
        uid = user.id
        db.add(MarketplaceAccount(
            platform="ebay", user_id=uid, shop_name="deleted_user",
            is_connected=True, access_token="secret-access",
            refresh_token="secret-refresh",
        ))
        db.commit()
        db.close()

        res = self.client.post("/api/ebay/account-deletion", json={
            "metadata": {"topic": "MARKETPLACE_ACCOUNT_DELETION"},
            "notification": {
                "notificationId": "n-match",
                "data": {"username": "deleted_user", "userId": "u-deleted"},
            },
        })
        self.assertEqual(res.status_code, 200)

        db = SessionLocal()
        try:
            # Deletion now ERASES the connection, not just clears its flag.
            acct = db.query(MarketplaceAccount).filter(
                MarketplaceAccount.user_id == uid,
                MarketplaceAccount.platform == "ebay").first()
            self.assertIsNone(acct)
        finally:
            from src.models import EbayAccountDeletion
            db.query(EbayAccountDeletion).filter_by(
                notification_id="n-match").delete(synchronize_session=False)
            db.query(MarketplaceAccount).filter(
                MarketplaceAccount.user_id == uid).delete(synchronize_session=False)
            db.query(User).filter(User.id == uid).delete(synchronize_session=False)
            db.commit()
            db.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
