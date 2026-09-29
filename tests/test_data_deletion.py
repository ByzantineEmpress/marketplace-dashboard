"""Data deletion must actually erase rows, not just flip flags.

The privacy policy promises erasure, and eBay requires actual deletion on its
account-deletion notifications. These tests pin the full cascade for both entry
points: a user deleting their own account, and eBay's notification deleting a
matched eBay connection.

All ids are captured as plain integers before deletion, because re-reading an
ORM object's attribute after its row is gone is itself an error.
"""

import secrets
import unittest
from datetime import datetime, timedelta

from tests import _env  # noqa: E402,F401  isort:skip  (must precede src imports)

from fastapi.testclient import TestClient

from src.api.main import app
from src.config import config
from src.data_deletion import delete_ebay_connection_data, delete_user_data
from src.database import SessionLocal, init_db
from src.models import (
    AuthSession,
    Listing,
    MarketplaceAccount,
    Team,
    TeamJoinRequest,
    TeamMembership,
    User,
    UserMarketplaceCredential,
)


class DeleteUserDataTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()
        cls._cm = TestClient(app)
        cls.client = cls._cm.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls._cm.__exit__(None, None, None)

    def setUp(self):
        self.db = SessionLocal()
        self.users = []
        self.teams = []

    def tearDown(self):
        db = self.db
        db.rollback()
        for tid in self.teams:
            db.query(Listing).filter(Listing.team_id == tid).delete(synchronize_session=False)
            db.query(TeamJoinRequest).filter(TeamJoinRequest.team_id == tid).delete(
                synchronize_session=False)
            db.query(TeamMembership).filter(TeamMembership.team_id == tid).delete(
                synchronize_session=False)
            db.query(Team).filter(Team.id == tid).delete(synchronize_session=False)
        for uid in self.users:
            db.query(MarketplaceAccount).filter(MarketplaceAccount.user_id == uid).delete(
                synchronize_session=False)
            db.query(UserMarketplaceCredential).filter(
                UserMarketplaceCredential.user_id == uid).delete(synchronize_session=False)
            db.query(AuthSession).filter(AuthSession.user_id == uid).delete(
                synchronize_session=False)
            db.query(TeamJoinRequest).filter(
                TeamJoinRequest.requester_user_id == uid).delete(synchronize_session=False)
            db.query(User).filter(User.id == uid).delete(synchronize_session=False)
        db.commit()
        db.close()

    def _user(self, provider="password", email=None):
        user = User(email=email or f"del.{secrets.token_hex(4)}@example.com",
                    name="Delete Me", provider=provider, is_admin=False)
        self.db.add(user)
        self.db.flush()
        self.users.append(user.id)
        return user.id

    def _team(self, uid, role="owner"):
        team = Team(name=f"T {secrets.token_hex(4)}",
                    invite_code=secrets.token_urlsafe(16))
        self.db.add(team)
        self.db.flush()
        self.teams.append(team.id)
        self.db.add(TeamMembership(team_id=team.id, user_id=uid, role=role))
        return team.id

    def test_deletes_user_and_everything_they_solely_own(self):
        uid = self._user()
        sole_team_id = self._team(uid)
        listing = Listing(platform="etsy",
                          platform_listing_id=f"L-{secrets.token_hex(4)}",
                          title="mine", price_cents=100, team_id=sole_team_id)
        self.db.add(listing)
        self.db.flush()
        listing_id = listing.id
        acct = MarketplaceAccount(platform="etsy", user_id=uid,
                                  is_connected=True, access_token="tok")
        self.db.add(acct)
        self.db.flush()
        acct_id = acct.id
        self.db.add(UserMarketplaceCredential(user_id=uid, platform="etsy",
                                              credential_key="api_key", value="k"))
        self.db.add(AuthSession(token=secrets.token_urlsafe(48), user_id=uid,
                                expires_at=datetime.utcnow() + timedelta(hours=1)))
        self.db.commit()

        result = delete_user_data(self.db, uid)
        self.assertTrue(result["deleted"], result)
        self.assertEqual(result["listings"], 1)
        self.assertEqual(result["teams"], 1)
        self.assertEqual(result["marketplace_accounts"], 1)
        self.assertEqual(result["credentials"], 1)
        self.assertEqual(result["sessions"], 1)

        self.assertIsNone(self.db.get(User, uid))
        self.assertIsNone(self.db.get(Team, sole_team_id))
        self.assertIsNone(self.db.get(Listing, listing_id))
        self.assertIsNone(self.db.get(MarketplaceAccount, acct_id))
        self.assertEqual(self.db.query(UserMarketplaceCredential).filter(
            UserMarketplaceCredential.user_id == uid).count(), 0)
        self.assertEqual(self.db.query(AuthSession).filter(
            AuthSession.user_id == uid).count(), 0)

    def test_a_shared_team_survives_but_membership_does_not(self):
        uid = self._user()
        shared_team_id = self._team(uid, role="member")
        other_id = self._user()
        self.db.add(TeamMembership(team_id=shared_team_id, user_id=other_id, role="owner"))
        listing = Listing(platform="etsy", platform_listing_id=f"L-{secrets.token_hex(4)}",
                          title="shared", price_cents=100, team_id=shared_team_id)
        self.db.add(listing)
        self.db.flush()
        listing_id = listing.id
        self.db.commit()

        result = delete_user_data(self.db, uid)
        self.assertTrue(result["deleted"])
        self.assertIsNotNone(self.db.get(Team, shared_team_id))
        self.assertIsNotNone(self.db.get(Listing, listing_id))
        self.assertIsNotNone(self.db.get(User, other_id))
        self.assertEqual(self.db.query(TeamMembership).filter(
            TeamMembership.team_id == shared_team_id,
            TeamMembership.user_id == uid).count(), 0)

    def test_deletes_join_requests_both_directions(self):
        uid = self._user()
        other_id = self._user()
        other_team_id = self._team(other_id)

        # The user asked to join someone else's team.
        self.db.add(TeamJoinRequest(team_id=other_team_id, requester_user_id=uid,
                                    owner_email=f"o.{secrets.token_hex(4)}@example.com"))

        # Someone asked to join a team this user owns (owner_email == user.email).
        user = self.db.get(User, uid)
        user.email = f"owner.{secrets.token_hex(4)}@example.com"
        self.db.commit()
        self.db.add(TeamJoinRequest(team_id=other_team_id, requester_user_id=other_id,
                                    owner_email=user.email))
        self.db.commit()

        delete_user_data(self.db, uid)
        self.assertEqual(self.db.query(TeamJoinRequest).filter(
            TeamJoinRequest.requester_user_id == uid).count(), 0)
        self.assertEqual(self.db.query(TeamJoinRequest).filter(
            TeamJoinRequest.owner_email == user.email).count(), 0)

    def test_the_local_admin_cannot_be_deleted(self):
        # provider="local" is the bootstrap admin; the guard keys on provider.
        uid = self._user(provider="local")
        self.db.commit()
        result = delete_user_data(self.db, uid)
        self.assertFalse(result["deleted"])
        self.assertIsNotNone(self.db.get(User, uid))


class DeleteOwnAccountEndpointTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()
        cls._cm = TestClient(app)
        cls.client = cls._cm.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls._cm.__exit__(None, None, None)

    def test_delete_account_endpoint(self):
        db = SessionLocal()
        user = User(email=f"acct.{secrets.token_hex(4)}@example.com",
                    name="Bye", provider="password", is_admin=False)
        db.add(user)
        db.flush()
        uid = user.id
        token = secrets.token_urlsafe(48)
        db.add(AuthSession(token=token, user_id=uid,
                           expires_at=datetime.utcnow() + timedelta(hours=1)))
        db.commit()
        db.close()

        self.client.cookies.clear()
        self.client.cookies.set("auth_token", token)

        res = self.client.delete("/api/account")
        self.assertEqual(res.status_code, 200, res.text)
        self.assertTrue(res.json()["ok"])
        # The cookie is cleared so the now-deleted session cannot be reused.
        self.assertIn("auth_token", res.headers.get("set-cookie", "").lower())

        db = SessionLocal()
        try:
            self.assertIsNone(db.get(User, uid))
        finally:
            db.close()


class DeleteEbayConnectionDataTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()

    def test_deletes_account_credentials_and_sole_team_listings(self):
        db = SessionLocal()
        user = User(email=f"ebaydel.{secrets.token_hex(4)}@example.com",
                    name="eBay Seller", provider="password", is_admin=False)
        db.add(user)
        db.flush()
        uid = user.id
        team = Team(name=f"EB {secrets.token_hex(4)}",
                    invite_code=secrets.token_urlsafe(16))
        db.add(team)
        db.flush()
        team_id = team.id
        db.add(TeamMembership(team_id=team_id, user_id=uid, role="owner"))
        ebay_listing = Listing(platform="ebay",
                               platform_listing_id=f"EB-{secrets.token_hex(4)}",
                               title="my eBay item", price_cents=100, team_id=team_id)
        etsy_listing = Listing(platform="etsy",
                               platform_listing_id=f"ET-{secrets.token_hex(4)}",
                               title="my Etsy item", price_cents=100, team_id=team_id)
        db.add_all([ebay_listing, etsy_listing])
        db.flush()
        ebay_listing_id = ebay_listing.id
        etsy_listing_id = etsy_listing.id
        acct = MarketplaceAccount(platform="ebay", user_id=uid, is_connected=True,
                                  access_token="tok", refresh_token="rt")
        db.add(acct)
        db.flush()
        acct_id = acct.id
        db.add(UserMarketplaceCredential(user_id=uid, platform="ebay",
                                         credential_key="client_id", value="cid"))
        db.add(UserMarketplaceCredential(user_id=uid, platform="etsy",
                                         credential_key="api_key", value="k"))
        db.commit()

        result = delete_ebay_connection_data(db, acct)
        self.assertEqual(result["marketplace_account"], 1)
        self.assertEqual(result["listings"], 1)      # only the eBay listing
        self.assertEqual(result["credentials"], 1)   # only the eBay credential

        self.assertIsNone(db.get(MarketplaceAccount, acct_id))
        self.assertIsNone(db.get(Listing, ebay_listing_id))
        self.assertIsNotNone(db.get(Listing, etsy_listing_id))
        self.assertEqual(db.query(UserMarketplaceCredential).filter(
            UserMarketplaceCredential.user_id == uid,
            UserMarketplaceCredential.platform == "ebay").count(), 0)

        # cleanup
        db.query(Listing).filter(Listing.team_id == team_id).delete(synchronize_session=False)
        db.query(UserMarketplaceCredential).filter(
            UserMarketplaceCredential.user_id == uid).delete(synchronize_session=False)
        db.query(TeamMembership).filter(TeamMembership.user_id == uid).delete(
            synchronize_session=False)
        db.query(Team).filter(Team.id == team_id).delete(synchronize_session=False)
        db.query(User).filter(User.id == uid).delete(synchronize_session=False)
        db.commit()
        db.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
