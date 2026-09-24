"""Multi-tenancy and data isolation integration tests.

Verifies strict tenant boundaries:
- Complete isolation between Tenant A and Tenant B.
- No leakage of listings, stats, sales, profit, or team names.
- Prevention of IDOR on listing updates, deletions, sales, and write-offs.
- Automatic isolated workspace creation for new self-serve tenants.
- Correct scoped access for invite-based onboarding.
"""

import os
import sys
import unittest
import secrets
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from starlette.testclient import TestClient
from src.api.main import app
from src.config import config
from src.database import init_db, SessionLocal
from src.models import User, Team, TeamMembership, AuthSession, Listing


class MultiTenancyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()
        cls._client_cm = TestClient(app)
        cls.client = cls._client_cm.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls._client_cm.__exit__(None, None, None)

    def setUp(self):
        self.db = SessionLocal()
        
        # 1. Create Tenant A (Alice)
        self.alice = User(
            email=f"alice_{secrets.token_hex(4)}@tenant-a.com",
            name="Alice",
            provider="google",
            is_admin=False,
        )
        self.db.add(self.alice)
        self.db.flush()

        self.team_a = Team(name=f"Tenant A Team {secrets.token_hex(4)}", invite_code=secrets.token_urlsafe(16))
        self.db.add(self.team_a)
        self.db.flush()
        self.db.add(TeamMembership(team_id=self.team_a.id, user_id=self.alice.id, role="owner"))

        # 2. Create Tenant B (Bob)
        self.bob = User(
            email=f"bob_{secrets.token_hex(4)}@tenant-b.com",
            name="Bob",
            provider="google",
            is_admin=False,
        )
        self.db.add(self.bob)
        self.db.flush()

        self.team_b = Team(name=f"Tenant B Team {secrets.token_hex(4)}", invite_code=secrets.token_urlsafe(16))
        self.db.add(self.team_b)
        self.db.flush()
        self.db.add(TeamMembership(team_id=self.team_b.id, user_id=self.bob.id, role="owner"))

        # 3. Seed listings for Tenant A
        self.listing_a = Listing(
            platform="local",
            platform_listing_id=f"ITEM-A-{secrets.token_hex(4)}",
            title="Alice's Secret Modded Controller",
            price_cents=15000,  # $150.00
            purchase_price_cents=4000,  # $40.00
            parts_cost_cents=2000,      # $20.00
            status="active",
            is_sold=False,
            team_id=self.team_a.id,
        )
        self.db.add(self.listing_a)

        # 4. Seed listing for Tenant B
        self.listing_b = Listing(
            platform="ebay",
            platform_listing_id=f"ITEM-B-{secrets.token_hex(4)}",
            title="Bob's Vintage Keyboard",
            price_cents=8000,
            status="active",
            is_sold=False,
            team_id=self.team_b.id,
        )
        self.db.add(self.listing_b)
        self.db.commit()

    def tearDown(self):
        self.db.close()

    def _login_as(self, user: User) -> str:
        token = secrets.token_hex(32)
        sess = AuthSession(
            token=token,
            user_id=user.id,
            expires_at=datetime.utcnow() + timedelta(days=1),
        )
        self.db.add(sess)
        self.db.commit()
        return token

    # ------------------------------------------------------------------
    # 1. Listings & Metrics Isolation
    # ------------------------------------------------------------------
    def test_tenant_cannot_see_other_tenant_listings(self):
        """Bob should never see Alice's listings in GET /listings."""
        bob_token = self._login_as(self.bob)
        self.client.cookies.set("auth_token", bob_token)

        res = self.client.get("/api/listings")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        listings = data.get("listings", [])
        
        titles = [item["title"] for item in listings]
        self.assertIn("Bob's Vintage Keyboard", titles)
        self.assertNotIn("Alice's Secret Modded Controller", titles)

    def test_tenant_cannot_query_other_team_via_param(self):
        """Bob should not be able to view Alice's listings using ?team=team_a_id."""
        bob_token = self._login_as(self.bob)
        self.client.cookies.set("auth_token", bob_token)

        res = self.client.get(f"/api/listings?team={self.team_a.id}")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data.get("total"), 0)
        self.assertEqual(len(data.get("listings", [])), 0)

    def test_tenant_cannot_see_other_tenant_stats(self):
        """Bob's stats should only count Bob's listings and not Alice's financial margins."""
        bob_token = self._login_as(self.bob)
        self.client.cookies.set("auth_token", bob_token)

        res = self.client.get("/api/stats")
        self.assertEqual(res.status_code, 200)
        stats = res.json()

        # Alice's active value is $150 (15000 cents); Bob's is $80 (8000 cents)
        self.assertEqual(stats["total_listings"], 1)
        self.assertEqual(stats["total_value_cents"], 8000)
        self.assertNotEqual(stats["total_value_cents"], 23000)

    # ------------------------------------------------------------------
    # 2. Mutation & IDOR Protections
    # ------------------------------------------------------------------
    def test_tenant_cannot_mark_sold_other_tenant_listing(self):
        """Bob cannot mark Alice's listing as sold."""
        bob_token = self._login_as(self.bob)
        self.client.cookies.set("auth_token", bob_token)

        res = self.client.post(f"/api/listings/{self.listing_a.id}/mark-sold")
        self.assertEqual(res.status_code, 404)

        # Confirm listing status didn't change
        self.db.refresh(self.listing_a)
        self.assertFalse(self.listing_a.is_sold)
        self.assertEqual(self.listing_a.status, "active")

    def test_tenant_cannot_write_off_other_tenant_listing(self):
        """Bob cannot write off Alice's listing."""
        bob_token = self._login_as(self.bob)
        self.client.cookies.set("auth_token", bob_token)

        res = self.client.post(f"/api/listings/{self.listing_a.id}/write-off")
        self.assertEqual(res.status_code, 404)

        self.db.refresh(self.listing_a)
        self.assertEqual(self.listing_a.status, "active")

    def test_tenant_cannot_delete_other_tenant_listing(self):
        """Bob cannot delete Alice's listing."""
        bob_token = self._login_as(self.bob)
        self.client.cookies.set("auth_token", bob_token)

        res = self.client.delete(f"/api/listings/{self.listing_a.id}")
        self.assertEqual(res.status_code, 404)

        # Confirm listing still exists
        self.assertIsNotNone(self.db.get(Listing, self.listing_a.id))

    def test_tenant_cannot_update_other_tenant_listing(self):
        """Bob cannot modify Alice's listing."""
        bob_token = self._login_as(self.bob)
        self.client.cookies.set("auth_token", bob_token)

        res = self.client.put(
            f"/api/listings/{self.listing_a.id}",
            json={"price": 1.0, "title": "Tampered Title"},
        )
        self.assertEqual(res.status_code, 404)

        self.db.refresh(self.listing_a)
        self.assertEqual(self.listing_a.price_cents, 15000)
        self.assertNotEqual(self.listing_a.title, "Tampered Title")

    def test_tenant_cannot_move_listing_to_foreign_team(self):
        """Bob cannot move his listing into Alice's team."""
        bob_token = self._login_as(self.bob)
        self.client.cookies.set("auth_token", bob_token)

        res = self.client.put(
            f"/api/listings/{self.listing_b.id}",
            json={"team_id": self.team_a.id},
        )
        self.assertEqual(res.status_code, 404)

        self.db.refresh(self.listing_b)
        self.assertEqual(self.listing_b.team_id, self.team_b.id)

    def test_tenant_cannot_unassign_listing(self):
        """Listings cannot be unassigned to null team_id."""
        bob_token = self._login_as(self.bob)
        self.client.cookies.set("auth_token", bob_token)

        res = self.client.put(
            f"/api/listings/{self.listing_b.id}",
            json={"team_id": None},
        )
        self.assertEqual(res.status_code, 400)

        self.db.refresh(self.listing_b)
        self.assertEqual(self.listing_b.team_id, self.team_b.id)

    # ------------------------------------------------------------------
    # 3. Onboarding & Workspace Isolation
    # ------------------------------------------------------------------
    def test_new_self_serve_user_gets_isolated_workspace(self):
        """A new Google user signing up without an invite gets their own private workspace."""
        new_email = f"clara_{secrets.token_hex(4)}@test.com"
        res = self.client.post("/auth/google/dev-login", data={"email": new_email, "name": "Clara"})
        self.assertEqual(res.status_code, 200)

        clara_user = self.db.query(User).filter(User.email == new_email).first()
        self.assertIsNotNone(clara_user)

        # Verify Clara belongs to her own team and NOT Alice's or Bob's
        clara_memberships = self.db.query(TeamMembership).filter(TeamMembership.user_id == clara_user.id).all()
        self.assertEqual(len(clara_memberships), 1)

        clara_team_id = clara_memberships[0].team_id
        self.assertNotEqual(clara_team_id, self.team_a.id)
        self.assertNotEqual(clara_team_id, self.team_b.id)
        self.assertEqual(clara_memberships[0].role, "owner")

        # Clara should see 0 listings initially
        clara_token = self._login_as(clara_user)
        self.client.cookies.set("auth_token", clara_token)
        list_res = self.client.get("/api/listings")
        self.assertEqual(list_res.json()["total"], 0)

    def test_invite_onboarding_joins_specific_team_only(self):
        """A new user accepting Alice's invite joins Alice's team and CANNOT see Bob's team."""
        david_email = f"david_{secrets.token_hex(4)}@test.com"
        # Simulate visiting Alice's join link
        self.client.get(f"/join/{self.team_a.invite_code}")

        # Complete sign-in
        res = self.client.post("/auth/google/dev-login", data={"email": david_email, "name": "David"})
        self.assertEqual(res.status_code, 200)

        david_user = self.db.query(User).filter(User.email == david_email).first()
        self.assertIsNotNone(david_user)

        # David should belong to Team A as member
        memberships = self.db.query(TeamMembership).filter(TeamMembership.user_id == david_user.id).all()
        self.assertEqual(len(memberships), 1)
        self.assertEqual(memberships[0].team_id, self.team_a.id)
        self.assertEqual(memberships[0].role, "member")

        # David sees Alice's listings, but NOT Bob's
        david_token = self._login_as(david_user)
        self.client.cookies.set("auth_token", david_token)
        list_res = self.client.get("/api/listings")
        titles = [item["title"] for item in list_res.json()["listings"]]
        self.assertIn("Alice's Secret Modded Controller", titles)
        self.assertNotIn("Bob's Vintage Keyboard", titles)


if __name__ == "__main__":
    unittest.main()
