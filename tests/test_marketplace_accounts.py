"""Per-user marketplace accounts: ownership, isolation and encryption.

These cover the bug being fixed and, more importantly, the class of bug it
belonged to. Before this change ``marketplace_accounts.platform`` was globally
UNIQUE and there was no owner, so:

  * only one user in the whole instance could hold an eBay connection, and
  * ``store_tokens`` looked the row up by platform alone, so a second user
    connecting the same marketplace overwrote the first user's tokens.

The tests below assert the properties that were impossible then: two users hold
the same platform independently, neither can read or delete the other's, and
nothing sensitive is stored in or returned as plaintext.
"""

import os
import secrets
import sys
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Must precede the src imports: it fixes the ambient configuration the app
# reads at import time. See tests/_env.py for why that matters.
try:
    from tests import _env  # noqa: E402,F401  isort:skip
except ImportError:  # `tests` resolved to the directory, not the package
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import _env  # noqa: E402,F401  isort:skip


from starlette.testclient import TestClient
from src.api.main import app
from src.database import init_db, SessionLocal
from src.models import (
    AuthSession,
    Listing,
    MarketplaceAccount,
    User,
    UserMarketplaceCredential,
)
from src import secrets_crypto


class MarketplaceAccountTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()
        cls._client_cm = TestClient(app)
        cls.client = cls._client_cm.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls._client_cm.__exit__(None, None, None)

    # ---------- helpers ----------

    def _make_user(self, db, label):
        user = User(
            email=f"{label}.{secrets.token_hex(4)}@example.com",
            name=label.capitalize(),
            provider="google",
            is_admin=False,
        )
        db.add(user)
        db.flush()
        return user

    def _token_for(self, db, user):
        token = secrets.token_urlsafe(48)
        db.add(
            AuthSession(
                token=token,
                user_id=user.id,
                expires_at=datetime.utcnow() + timedelta(hours=1),
            )
        )
        db.commit()
        return token

    def _as(self, token):
        self.client.cookies.clear()
        self.client.cookies.set("auth_token", token)

    def setUp(self):
        self.db = SessionLocal()
        self.alice = self._make_user(self.db, "alice")
        self.bob = self._make_user(self.db, "bob")
        self.alice_token = self._token_for(self.db, self.alice)
        self.bob_token = self._token_for(self.db, self.bob)

    def tearDown(self):
        self.db.rollback()
        # Remove anything this test created, so runs do not accumulate rows.
        self.db.query(UserMarketplaceCredential).filter(
            UserMarketplaceCredential.user_id.in_([self.alice.id, self.bob.id])
        ).delete(synchronize_session=False)
        self.db.query(MarketplaceAccount).filter(
            MarketplaceAccount.user_id.in_([self.alice.id, self.bob.id])
        ).delete(synchronize_session=False)
        self.db.query(AuthSession).filter(
            AuthSession.user_id.in_([self.alice.id, self.bob.id])
        ).delete(synchronize_session=False)
        self.db.query(User).filter(
            User.id.in_([self.alice.id, self.bob.id])
        ).delete(synchronize_session=False)
        self.db.commit()
        self.db.close()

    # ---------- the bug that was reported ----------

    def test_a_normal_user_can_reach_the_accounts_page(self):
        """The reported bug: users were sent to an admin-only page."""
        self._as(self.alice_token)
        res = self.client.get("/marketplace-settings")
        self.assertEqual(res.status_code, 200)
        self.assertIn("My Marketplace Accounts", res.text)

    def test_a_normal_user_is_not_required_to_be_an_admin(self):
        """And the API behind it must not demand admin either."""
        self._as(self.alice_token)
        res = self.client.get("/api/accounts")
        self.assertEqual(res.status_code, 200)

        res = self.client.get("/api/accounts/credentials")
        self.assertEqual(res.status_code, 200)

        # An anonymous caller is still refused.
        self.client.cookies.clear()
        self.assertEqual(self.client.get("/api/accounts").status_code, 401)

    # ---------- isolation ----------

    def test_two_users_can_hold_the_same_platform_independently(self):
        """Impossible before: platform was globally UNIQUE."""
        self.db.add(MarketplaceAccount(
            user_id=self.alice.id, platform="ebay", is_connected=True,
            access_token="alice-token", shop_name="Alice's Shop",
        ))
        self.db.add(MarketplaceAccount(
            user_id=self.bob.id, platform="ebay", is_connected=True,
            access_token="bob-token", shop_name="Bob's Shop",
        ))
        self.db.commit()

        rows = self.db.query(MarketplaceAccount).filter(
            MarketplaceAccount.platform == "ebay",
            MarketplaceAccount.user_id.in_([self.alice.id, self.bob.id]),
        ).all()
        self.assertEqual(len(rows), 2, "both users should be able to connect eBay")

    def test_the_same_user_cannot_duplicate_a_platform(self):
        self.db.add(MarketplaceAccount(
            user_id=self.alice.id, platform="ebay", is_connected=True,
        ))
        self.db.commit()

        self.db.add(MarketplaceAccount(
            user_id=self.alice.id, platform="ebay", is_connected=True,
        ))
        with self.assertRaises(Exception):
            self.db.commit()
        self.db.rollback()

    def test_accounts_endpoint_only_returns_your_own(self):
        self.db.add(MarketplaceAccount(
            user_id=self.alice.id, platform="ebay", is_connected=True,
            shop_name="Alice's Shop",
        ))
        self.db.add(MarketplaceAccount(
            user_id=self.bob.id, platform="etsy", is_connected=True,
            shop_name="Bob's Shop",
        ))
        self.db.commit()

        self._as(self.alice_token)
        data = self.client.get("/api/accounts").json()
        platforms = {row["platform"] for row in data}
        self.assertIn("ebay", platforms)
        self.assertNotIn("etsy", platforms, "Alice must not see Bob's connection")
        for row in data:
            self.assertNotEqual(row.get("shop_name"), "Bob's Shop")

        self._as(self.bob_token)
        data = self.client.get("/api/accounts").json()
        platforms = {row["platform"] for row in data}
        self.assertIn("etsy", platforms)
        self.assertNotIn("ebay", platforms, "Bob must not see Alice's connection")

    def test_credentials_are_stored_per_user_and_never_cross_over(self):
        self._as(self.alice_token)
        res = self.client.post(
            "/api/accounts/credentials",
            json={"platform": "ebay", "values": {
                "client_id": "alice-client-id",
                "client_secret": "alice-client-secret",
            }},
        )
        self.assertEqual(res.status_code, 200, res.text)
        self.assertTrue(res.json()["ok"])

        # Bob sees nothing of Alice's.
        self._as(self.bob_token)
        status = self.client.get("/api/accounts/credentials").json()
        ebay_fields = {f["key"]: f for f in status["ebay"]["fields"]}
        self.assertFalse(ebay_fields["client_id"]["is_set"])
        self.assertFalse(ebay_fields["client_secret"]["is_set"])

        # Alice still sees her own.
        self._as(self.alice_token)
        status = self.client.get("/api/accounts/credentials").json()
        ebay_fields = {f["key"]: f for f in status["ebay"]["fields"]}
        self.assertTrue(ebay_fields["client_id"]["is_set"])
        self.assertTrue(ebay_fields["client_secret"]["is_set"])

    def test_one_users_save_does_not_overwrite_anothers(self):
        self._as(self.alice_token)
        self.client.post("/api/accounts/credentials", json={
            "platform": "ebay", "values": {"client_id": "alice-id"}})
        self._as(self.bob_token)
        self.client.post("/api/accounts/credentials", json={
            "platform": "ebay", "values": {"client_id": "bob-id"}})

        rows = {
            (r.user_id, r.credential_key): r.value
            for r in self.db.query(UserMarketplaceCredential).filter(
                UserMarketplaceCredential.platform == "ebay",
                UserMarketplaceCredential.user_id.in_([self.alice.id, self.bob.id]),
            ).all()
        }
        self.assertEqual(rows[(self.alice.id, "client_id")], "alice-id")
        self.assertEqual(rows[(self.bob.id, "client_id")], "bob-id")

    def test_unknown_credential_keys_are_ignored(self):
        """The client cannot invent credential names."""
        self._as(self.alice_token)
        res = self.client.post("/api/accounts/credentials", json={
            "platform": "ebay",
            "values": {"client_id": "ok", "is_admin": "1", "evil": "x"},
        })
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertIn("client_id", body["saved"])
        self.assertIn("is_admin", body["ignored"])
        self.assertIn("evil", body["ignored"])

    def test_unknown_platform_is_rejected(self):
        self._as(self.alice_token)
        res = self.client.post("/api/accounts/credentials", json={
            "platform": "not-a-marketplace", "values": {"a": "b"}})
        self.assertEqual(res.status_code, 400)

    def test_masked_placeholder_does_not_overwrite_a_stored_secret(self):
        """The browser never holds the real secret, so a replayed mask must not
        replace it with dots."""
        self._as(self.alice_token)
        self.client.post("/api/accounts/credentials", json={
            "platform": "ebay", "values": {"client_secret": "the-real-secret"}})

        res = self.client.post("/api/accounts/credentials", json={
            "platform": "ebay", "values": {"client_secret": "\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022cret"}})
        self.assertEqual(res.status_code, 200)
        self.assertIn("client_secret", res.json()["ignored"])

        row = self.db.query(UserMarketplaceCredential).filter(
            UserMarketplaceCredential.user_id == self.alice.id,
            UserMarketplaceCredential.credential_key == "client_secret",
        ).first()
        self.assertEqual(row.value, "the-real-secret")

    # ---------- encryption ----------

    def test_credentials_are_not_stored_in_plaintext(self):
        self._as(self.alice_token)
        self.client.post("/api/accounts/credentials", json={
            "platform": "ebay",
            "values": {"client_id": "plaintext-id", "client_secret": "plaintext-secret"},
        })

        # Read the raw column, bypassing the decryption layer entirely: this is
        # what someone with the database file (or a backup) would see.
        from sqlalchemy import text

        raw = {
            row[0]: row[1]
            for row in self.db.execute(text(
                "SELECT credential_key, value FROM user_marketplace_credentials "
                "WHERE user_id = :uid"
            ), {"uid": self.alice.id})
        }
        self.assertIn("client_secret", raw)
        self.assertNotIn("plaintext-secret", raw["client_secret"])
        self.assertTrue(
            secrets_crypto.is_encrypted(raw["client_secret"]),
            "secret should be stored encrypted",
        )
        self.assertNotIn("plaintext-id", raw.get("client_id", ""))

    def test_oauth_tokens_are_not_stored_in_plaintext(self):
        self.db.add(MarketplaceAccount(
            user_id=self.alice.id, platform="ebay", is_connected=True,
            access_token="super-secret-access-token",
            refresh_token="super-secret-refresh-token",
        ))
        self.db.commit()

        from sqlalchemy import text

        raw = self.db.execute(text(
            "SELECT access_token, refresh_token FROM marketplace_accounts "
            "WHERE user_id = :uid AND platform = 'ebay'"
        ), {"uid": self.alice.id}).fetchone()
        self.assertNotIn("super-secret-access-token", raw[0])
        self.assertNotIn("super-secret-refresh-token", raw[1])
        self.assertTrue(secrets_crypto.is_encrypted(raw[0]))

        # And the ORM still hands back the real value.
        row = self.db.query(MarketplaceAccount).filter(
            MarketplaceAccount.user_id == self.alice.id,
            MarketplaceAccount.platform == "ebay",
        ).first()
        self.assertEqual(row.access_token, "super-secret-access-token")

    def test_secrets_are_never_returned_by_the_api(self):
        self._as(self.alice_token)
        self.client.post("/api/accounts/credentials", json={
            "platform": "ebay", "values": {"client_secret": "do-not-leak-me"}})

        body = self.client.get("/api/accounts/credentials").text
        self.assertNotIn("do-not-leak-me", body)
        # A masked hint is fine and expected.
        self.assertIn("\u2022", body)

    def test_token_dict_never_includes_credentials(self):
        self.db.add(MarketplaceAccount(
            user_id=self.alice.id, platform="ebay", is_connected=True,
            access_token="ACCESS-SECRET-VALUE-AAA",
            refresh_token="REFRESH-SECRET-VALUE-BBB",
        ))
        self.db.commit()
        row = self.db.query(MarketplaceAccount).filter(
            MarketplaceAccount.user_id == self.alice.id,
            MarketplaceAccount.platform == "ebay",
        ).first()
        # Values chosen to be distinctive: a short marker like "tok" would also
        # match the substring inside "token_expires_at" and give a false result.
        serialised = str(row.to_dict())
        self.assertNotIn("ACCESS-SECRET-VALUE-AAA", serialised)
        self.assertNotIn("REFRESH-SECRET-VALUE-BBB", serialised)

    # ---------- adapters ----------

    def test_adapter_token_lookup_is_scoped_to_the_user(self):
        from src.adapters import get_adapter

        self.db.add(MarketplaceAccount(
            user_id=self.alice.id, platform="ebay", is_connected=True,
            access_token="alice-access",
        ))
        self.db.commit()

        adapter = get_adapter("ebay")
        alice_token = adapter.get_token(self.db, user_id=self.alice.id)
        bob_token = adapter.get_token(self.db, user_id=self.bob.id)

        self.assertIsNotNone(alice_token)
        self.assertEqual(alice_token["access_token"], "alice-access")
        self.assertIsNone(bob_token, "Bob must not receive Alice's token")

    def test_adapter_credential_resolution_prefers_the_user(self):
        from src.adapters import get_adapter

        self.db.add(UserMarketplaceCredential(
            user_id=self.alice.id, platform="ebay",
            credential_key="client_id", value="alice-own-client-id",
        ))
        self.db.commit()

        adapter = get_adapter("ebay")
        alice_creds = adapter.credentials_for(self.db, self.alice.id, ["client_id"])
        bob_creds = adapter.credentials_for(self.db, self.bob.id, ["client_id"])

        self.assertEqual(alice_creds["client_id"], "alice-own-client-id")
        self.assertNotEqual(
            bob_creds.get("client_id"), "alice-own-client-id",
            "Bob must never resolve Alice's client ID",
        )

    def test_ebay_client_id_falls_back_to_instance_config(self):
        """An instance configured through .env must keep working."""
        from src.adapters import get_adapter
        from src.config import config

        original = config.EBAY_CLIENT_ID
        config.EBAY_CLIENT_ID = "instance-wide-client-id"
        try:
            adapter = get_adapter("ebay")
            # No per-user value set, so the fallback applies.
            resolved = adapter.credentials_for(self.db, self.bob.id, ["client_id"])
            self.assertEqual(resolved["client_id"], "instance-wide-client-id")
        finally:
            config.EBAY_CLIENT_ID = original


class SyncedListingsGoToTheSyncingUsersTeamTest(unittest.TestCase):
    """A sync must not write into somebody else's workspace.

    store_listings used to fall back to ``db.query(Team).first()`` when no team
    was passed, and sync_all never passed one. A real sync put 500 of one
    seller's listings into "Default Team", which belonged to the local admin,
    while the syncing user had their own team.

    The rest of the suite could not have caught it: every other test works with
    a single team, so "the first team" and "the right team" were the same thing.
    This test deliberately creates two teams owned by different users.
    """

    @classmethod
    def setUpClass(cls):
        init_db()
        cls._client_cm = TestClient(app)
        cls.client = cls._client_cm.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls._client_cm.__exit__(None, None, None)

    def setUp(self):
        from src.models import Team, TeamMembership

        self.db = SessionLocal()
        # The decoy first: if anything still uses "the first team", it lands
        # here and the test fails.
        self.decoy = User(
            email=f"decoy.{secrets.token_hex(4)}@example.com",
            name="Decoy", provider="google", is_admin=False,
        )
        self.db.add(self.decoy)
        self.db.flush()
        self.decoy_team = Team(
            name=f"Decoy Team {secrets.token_hex(4)}",
            invite_code=secrets.token_urlsafe(16),
        )
        self.db.add(self.decoy_team)
        self.db.flush()
        self.db.add(TeamMembership(team_id=self.decoy_team.id,
                                   user_id=self.decoy.id, role="owner"))

        self.user = User(
            email=f"syncer.{secrets.token_hex(4)}@example.com",
            name="Syncer", provider="google", is_admin=False,
        )
        self.db.add(self.user)
        self.db.flush()
        self.own_team = Team(
            name=f"Own Team {secrets.token_hex(4)}",
            invite_code=secrets.token_urlsafe(16),
        )
        self.db.add(self.own_team)
        self.db.flush()
        self.db.add(TeamMembership(team_id=self.own_team.id,
                                   user_id=self.user.id, role="owner"))
        self.db.commit()

    def tearDown(self):
        from src.models import Team, TeamMembership

        self.db.rollback()
        ids = [self.user.id, self.decoy.id]
        teams = [self.own_team.id, self.decoy_team.id]
        self.db.query(MarketplaceAccount).filter(
            MarketplaceAccount.user_id.in_(ids)
        ).delete(synchronize_session=False)
        self.db.query(Listing).filter(
            Listing.team_id.in_(teams)
        ).delete(synchronize_session=False)
        self.db.query(TeamMembership).filter(
            TeamMembership.user_id.in_(ids)
        ).delete(synchronize_session=False)
        self.db.query(Team).filter(Team.id.in_(teams)).delete(synchronize_session=False)
        self.db.query(AuthSession).filter(
            AuthSession.user_id.in_(ids)
        ).delete(synchronize_session=False)
        self.db.query(User).filter(User.id.in_(ids)).delete(synchronize_session=False)
        self.db.commit()
        self.db.close()

    def test_listings_are_written_to_the_syncing_users_team(self):
        from src.adapters import get_adapter

        adapter = get_adapter("ebay")
        stored = adapter.store_listings(
            self.db,
            [{
                "platform": "ebay",
                "platform_listing_id": f"SYNC-{secrets.token_hex(4)}",
                "title": "Should belong to the syncer",
                "price_cents": 1000,
                "currency": "CAD",
            }],
            owner_user_id=self.user.id,
        )
        self.assertEqual(stored["added"], 1, stored)

        row = self.db.query(Listing).filter(
            Listing.platform_listing_id.like("SYNC-%")
        ).order_by(Listing.id.desc()).first()
        self.assertIsNotNone(row)
        self.assertEqual(row.team_id, self.own_team.id)
        self.assertNotEqual(
            row.team_id, self.decoy_team.id,
            "the listing was written into another user's team",
        )

    def test_a_user_with_no_team_is_refused_rather_than_guessed(self):
        """Storing nothing is better than storing into an arbitrary team."""
        from src.adapters import get_adapter

        from src.models import TeamMembership

        # Remove the syncer's membership, leaving them team-less.
        self.db.query(TeamMembership).filter(
            TeamMembership.user_id == self.user.id
        ).delete(synchronize_session=False)
        self.db.commit()

        adapter = get_adapter("ebay")
        stored = adapter.store_listings(
            self.db,
            [{"platform": "ebay", "platform_listing_id": "NOPE-1",
              "title": "Nowhere to go", "price_cents": 1}],
            owner_user_id=self.user.id,
        )
        self.assertEqual(stored["added"], 0)
        self.assertIsNotNone(adapter.last_error)
        self.assertIn("team", adapter.last_error.lower())

        # And nothing was written into the decoy team.
        count = self.db.query(Listing).filter(
            Listing.team_id == self.decoy_team.id
        ).count()
        self.assertEqual(count, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
