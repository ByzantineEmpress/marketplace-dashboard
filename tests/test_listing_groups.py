"""Listing groups: link the same item across marketplaces.

A group is one physical unit listed on several channels. Its stock and COGS are
counted once, not once per channel. This suite covers the backend contract:
grouping, ungrouping, shared COGS, the "delist elsewhere" flag, and — critically
— that grouping never leaks across teams or users.
"""

import secrets
import unittest
from datetime import datetime, timedelta

from tests import _env  # noqa: E402,F401  isort:skip  (must precede src imports)

from fastapi.testclient import TestClient

from src.api.main import app
from src.database import SessionLocal, init_db
from src.models import (
    AuthSession,
    Listing,
    ListingGroup,
    Team,
    TeamMembership,
    User,
)


class ListingGroupTest(unittest.TestCase):
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
        self.user = User(
            email=f"group.{secrets.token_hex(4)}@example.com",
            name="Group Tester", provider="google", is_admin=False,
        )
        self.db.add(self.user)
        self.db.flush()
        self.team = Team(
            name=f"Group Team {secrets.token_hex(4)}",
            invite_code=secrets.token_urlsafe(16),
        )
        self.db.add(self.team)
        self.db.flush()
        self.db.add(TeamMembership(team_id=self.team.id,
                                   user_id=self.user.id, role="owner"))

        self.token = secrets.token_urlsafe(48)
        self.db.add(AuthSession(
            token=self.token, user_id=self.user.id,
            expires_at=datetime.utcnow() + timedelta(hours=1),
        ))
        self.db.commit()

        self.client.cookies.clear()
        self.client.cookies.set("auth_token", self.token)

        # Clean up any rows a previous (interrupted) run left behind.
        self._scratch = {"users": [self.user.id], "teams": [self.team.id]}

    def tearDown(self):
        db = self.db
        db.rollback()
        teams = self._scratch["teams"]
        users = self._scratch["users"]
        db.query(Listing).filter(Listing.team_id.in_(teams)).delete(
            synchronize_session=False)
        db.query(TeamMembership).filter(
            TeamMembership.user_id.in_(users)).delete(synchronize_session=False)
        db.query(AuthSession).filter(
            AuthSession.user_id.in_(users)).delete(synchronize_session=False)
        db.query(Team).filter(Team.id.in_(teams)).delete(synchronize_session=False)
        db.query(User).filter(User.id.in_(users)).delete(synchronize_session=False)
        # Groups left dangling by ungroup tests.
        db.commit()
        db.close()

    def _listing(self, platform, purchase=0, status="active", is_sold=False, qty=1):
        row = Listing(
            platform=platform,
            platform_listing_id=f"G-{platform}-{secrets.token_hex(6)}",
            title=f"{platform.title()} item",
            price_cents=10000,
            purchase_price_cents=purchase,
            status=status,
            is_sold=is_sold,
            available_quantity=qty,
            team_id=self.team.id,
        )
        self.db.add(row)
        self.db.commit()
        return row

    def _group_ids(self, group_id):
        return {l.id for l in self.db.query(Listing)
                .filter(Listing.group_id == group_id).all()}

    # -- grouping --

    def test_grouping_links_listings_and_shares_cost(self):
        ebay = self._listing("ebay", purchase=4000)
        etsy = self._listing("etsy", purchase=0)

        res = self.client.post("/api/listings/group",
                               json={"listing_ids": [ebay.id, etsy.id]})
        self.assertEqual(res.status_code, 200, res.text)
        gid = res.json()["group"]["id"]

        members = self._group_ids(gid)
        self.assertEqual(members, {ebay.id, etsy.id})
        # Cost seeded from the member that already had one, so it is counted once.
        self.assertEqual(res.json()["group"]["purchase_price_cents"], 4000)

    def test_grouping_requires_two_listings(self):
        ebay = self._listing("ebay")
        res = self.client.post("/api/listings/group",
                               json={"listing_ids": [ebay.id]})
        self.assertEqual(res.status_code, 400)
        self.assertIn("two", res.json()["error"])

    def test_grouping_cannot_span_teams(self):
        ebay = self._listing("ebay")

        other_team = Team(name=f"Other {secrets.token_hex(4)}",
                          invite_code=secrets.token_urlsafe(16))
        self.db.add(other_team)
        self.db.flush()
        self._scratch["teams"].append(other_team.id)
        # The caller IS a member of both teams; the point of this test is that a
        # group still cannot span them, not that the caller lacks access.
        self.db.add(TeamMembership(team_id=other_team.id,
                                   user_id=self.user.id, role="member"))
        foreign = Listing(
            platform="etsy", platform_listing_id=f"G-etsy-{secrets.token_hex(6)}",
            title="Foreign", price_cents=100, team_id=other_team.id,
        )
        self.db.add(foreign)
        self.db.commit()

        res = self.client.post("/api/listings/group",
                               json={"listing_ids": [ebay.id, foreign.id]})
        self.assertEqual(res.status_code, 400, res.text)
        self.assertIn("same team", res.json()["error"])

    def test_grouping_reuses_an_existing_group(self):
        ebay = self._listing("ebay")
        etsy = self._listing("etsy")
        fb = self._listing("facebook")
        self.client.post("/api/listings/group",
                         json={"listing_ids": [ebay.id, etsy.id]})

        # Group the existing pair together with a third listing.
        res = self.client.post("/api/listings/group",
                               json={"listing_ids": [etsy.id, fb.id]})
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(res.json()["group"]["member_count"], 3)

    def test_another_users_listing_cannot_be_grouped(self):
        ebay = self._listing("ebay")

        other = User(email=f"other.{secrets.token_hex(4)}@example.com",
                     name="Other", provider="google", is_admin=False)
        self.db.add(other)
        self.db.flush()
        self._scratch["users"].append(other.id)
        other_team = Team(name=f"OtherT {secrets.token_hex(4)}",
                          invite_code=secrets.token_urlsafe(16))
        self.db.add(other_team)
        self.db.flush()
        self._scratch["teams"].append(other_team.id)
        self.db.add(TeamMembership(team_id=other_team.id,
                                   user_id=other.id, role="owner"))
        foreign = Listing(
            platform="etsy", platform_listing_id=f"G-etsy-{secrets.token_hex(6)}",
            title="Theirs", price_cents=100, team_id=other_team.id,
        )
        self.db.add(foreign)
        self.db.commit()

        res = self.client.post("/api/listings/group",
                               json={"listing_ids": [ebay.id, foreign.id]})
        self.assertEqual(res.status_code, 403, res.text)

    # -- the "delist elsewhere" flag --

    def test_sold_on_one_channel_flags_delist_elsewhere(self):
        ebay = self._listing("ebay", status="sold", is_sold=True)
        etsy = self._listing("etsy", status="active")

        res = self.client.post("/api/listings/group",
                               json={"listing_ids": [ebay.id, etsy.id]})
        g = res.json()["group"]
        self.assertTrue(g["needs_delist"])
        self.assertEqual(g["sold_platforms"], ["ebay"])
        self.assertEqual(g["active_platforms"], ["etsy"])
        self.assertFalse(g["all_sold"])

    def test_all_sold_is_not_a_delist_alert(self):
        ebay = self._listing("ebay", status="sold", is_sold=True)
        etsy = self._listing("etsy", status="sold", is_sold=True)

        res = self.client.post("/api/listings/group",
                               json={"listing_ids": [ebay.id, etsy.id]})
        g = res.json()["group"]
        self.assertFalse(g["needs_delist"])
        self.assertTrue(g["all_sold"])

    # -- ungrouping --

    def test_ungrouping_removes_one_member(self):
        ebay = self._listing("ebay")
        etsy = self._listing("etsy")
        fb = self._listing("facebook")
        gid = self.client.post("/api/listings/group",
                               json={"listing_ids": [ebay.id, etsy.id, fb.id]}
                               ).json()["group"]["id"]

        res = self.client.post(f"/api/listings/{etsy.id}/ungroup")
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(self._group_ids(gid), {ebay.id, fb.id})
        self.assertIsNone(self.db.get(Listing, etsy.id).group_id)

    def test_ungrouping_down_to_one_dissolves_the_group(self):
        ebay = self._listing("ebay")
        etsy = self._listing("etsy")
        gid = self.client.post("/api/listings/group",
                               json={"listing_ids": [ebay.id, etsy.id]}
                               ).json()["group"]["id"]

        self.client.post(f"/api/listings/{etsy.id}/ungroup")
        self.assertIsNone(self.db.get(Listing, ebay.id).group_id)
        self.assertIsNone(self.db.get(ListingGroup, gid))

    # -- shared COGS update --

    def test_updating_group_cost_applies_once(self):
        ebay = self._listing("ebay")
        etsy = self._listing("etsy")
        gid = self.client.post("/api/listings/group",
                               json={"listing_ids": [ebay.id, etsy.id]}
                               ).json()["group"]["id"]

        res = self.client.put(f"/api/listings/group/{gid}",
                              json={"purchase_price": 42.50,
                                    "available_quantity": 3})
        self.assertEqual(res.status_code, 200, res.text)
        g = res.json()["group"]
        self.assertEqual(g["purchase_price_cents"], 4250)
        self.assertEqual(g["available_quantity"], 3)
        self.assertEqual(g["total_cost_cents"], 4250)
        self.assertFalse(g["missing_cost"])


class GroupStatsDedupTest(unittest.TestCase):
    """Stats must count a group once, not once per channel."""

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
        self.user = User(
            email=f"gstat.{secrets.token_hex(4)}@example.com",
            name="Stat Tester", provider="google", is_admin=False,
        )
        self.db.add(self.user)
        self.db.flush()
        self.team = Team(name=f"StatT {secrets.token_hex(4)}",
                         invite_code=secrets.token_urlsafe(16))
        self.db.add(self.team)
        self.db.flush()
        self.db.add(TeamMembership(team_id=self.team.id,
                                   user_id=self.user.id, role="owner"))
        self.token = secrets.token_urlsafe(48)
        self.db.add(AuthSession(token=self.token, user_id=self.user.id,
                                expires_at=datetime.utcnow() + timedelta(hours=1)))
        self.db.commit()
        self.client.cookies.clear()
        self.client.cookies.set("auth_token", self.token)

    def tearDown(self):
        db = self.db
        db.rollback()
        db.query(Listing).filter(Listing.team_id == self.team.id).delete(
            synchronize_session=False)
        db.query(TeamMembership).filter(
            TeamMembership.user_id == self.user.id).delete(synchronize_session=False)
        db.query(AuthSession).filter(AuthSession.user_id == self.user.id).delete(
            synchronize_session=False)
        db.query(Team).filter(Team.id == self.team.id).delete(synchronize_session=False)
        db.query(User).filter(User.id == self.user.id).delete(synchronize_session=False)
        db.commit()
        db.close()

    def _mk(self, platform, price=10000):
        row = Listing(platform=platform,
                      platform_listing_id=f"S-{platform}-{secrets.token_hex(5)}",
                      title=platform, price_cents=price,
                      status="active", team_id=self.team.id)
        self.db.add(row)
        self.db.commit()
        return row

    def test_grouped_listings_do_not_double_count_value(self):
        ebay = self._mk("ebay", price=10000)
        etsy = self._mk("etsy", price=10000)
        self.client.post("/api/listings/group",
                         json={"listing_ids": [ebay.id, etsy.id]})

        stats = self.client.get("/api/stats").json()
        # Two listings, one group → counted once.
        self.assertEqual(stats["total_listings"], 1)
        self.assertEqual(stats["active_listings"], 1)
        self.assertEqual(stats["total_value_cents"], 10000)

    def test_ungrouped_listings_still_count_separately(self):
        self._mk("ebay", price=10000)
        self._mk("etsy", price=10000)
        stats = self.client.get("/api/stats").json()
        self.assertEqual(stats["total_listings"], 2)
        self.assertEqual(stats["total_value_cents"], 20000)


if __name__ == "__main__":
    unittest.main(verbosity=2)
