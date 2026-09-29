"""The "missing cost" flag: listings with no purchase price recorded.

Synced listings arrive with no cost data at all. That is the set that needs
attention, because a listing with no COGS shows its entire sale price as
profit â€” a fake 100% margin. The flag exists to make those visible and
filterable.

The rule is the PURCHASE PRICE, not total cost: a listing whose only recorded
cost is a repair part still needs its purchase price entered, so a total-cost
rule would hide it.
"""

import secrets
import unittest

from tests import _env  # noqa: E402,F401  isort:skip  (must precede src imports)

from src.api.main import app
from src.database import SessionLocal, init_db
from src.models import (
    AuthSession,
    Listing,
    Team,
    TeamMembership,
    User,
)
from fastapi.testclient import TestClient


class MissingCostFlagTest(unittest.TestCase):
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
            email=f"cost.{secrets.token_hex(4)}@example.com",
            name="Cost Tester", provider="google", is_admin=False,
        )
        self.db.add(self.user)
        self.db.flush()
        self.team = Team(
            name=f"Cost Team {secrets.token_hex(4)}",
            invite_code=secrets.token_urlsafe(16),
        )
        self.db.add(self.team)
        self.db.flush()
        self.db.add(TeamMembership(team_id=self.team.id,
                                   user_id=self.user.id, role="owner"))
        self.db.commit()

    def tearDown(self):
        ids, teams = [self.user.id], [self.team.id]
        self.db.rollback()
        self.db.query(Listing).filter(Listing.team_id.in_(teams)).delete(
            synchronize_session=False)
        self.db.query(TeamMembership).filter(
            TeamMembership.user_id.in_(ids)).delete(synchronize_session=False)
        self.db.query(Team).filter(Team.id.in_(teams)).delete(synchronize_session=False)
        self.db.query(AuthSession).filter(
            AuthSession.user_id.in_(ids)).delete(synchronize_session=False)
        self.db.query(User).filter(User.id.in_(ids)).delete(synchronize_session=False)
        self.db.commit()
        self.db.close()

    def _make(self, purchase_cents, parts_cents=0, title="x"):
        row = Listing(
            platform="etsy",
            platform_listing_id=f"MC-{secrets.token_hex(6)}",
            title=title,
            price_cents=10000,
            purchase_price_cents=purchase_cents,
            parts_cost_cents=parts_cents,
            status="active",
            team_id=self.team.id,
        )
        self.db.add(row)
        self.db.commit()
        return row

    # -- the property itself --

    def test_a_listing_with_no_purchase_price_is_flagged(self):
        row = self._make(0)
        self.assertTrue(row.missing_cost)
        self.assertFalse(row.has_cost)

    def test_a_listing_with_a_purchase_price_is_not_flagged(self):
        row = self._make(2500)
        self.assertFalse(row.missing_cost)
        self.assertTrue(row.has_cost)

    def test_a_parts_cost_alone_does_not_clear_the_flag(self):
        """The purchase price is still missing, so it must still be flagged."""
        row = self._make(0, parts_cents=500)
        self.assertTrue(row.missing_cost)

    def test_the_flag_is_serialised_for_the_api(self):
        row = self._make(0)
        self.assertTrue(row.to_dict()["missing_cost"])
        row2 = self._make(999)
        self.assertFalse(row2.to_dict()["missing_cost"])

    # -- the API filter --

    def _login(self):
        """Create a session for self.user and act as them.

        Mirrors test_marketplace_accounts: a real AuthSession row plus the
        auth_token cookie the app reads.
        """
        from datetime import datetime, timedelta

        token = secrets.token_urlsafe(48)
        self.db.add(AuthSession(
            token=token, user_id=self.user.id,
            expires_at=datetime.utcnow() + timedelta(hours=1),
        ))
        self.db.commit()
        self.client.cookies.clear()
        self.client.cookies.set("auth_token", token)

    def test_the_api_filter_returns_only_flagged_listings(self):
        """Server-side filtering matters: the list is paginated, so filtering in
        the browser would only ever see the current page."""
        flagged = self._make(0, title="No price here")
        priced = self._make(4200, title="Has a price")
        self._login()

        res = self.client.get("/api/listings?missing_cost=true&page_size=200")
        self.assertEqual(res.status_code, 200, res.text)
        ids = [l["id"] for l in res.json()["listings"]]
        self.assertIn(flagged.id, ids)
        self.assertNotIn(priced.id, ids)
        # Everything returned must actually be flagged.
        for item in res.json()["listings"]:
            self.assertTrue(item["missing_cost"], item)

    def test_the_filter_combines_with_the_others(self):
        """It is a checkbox precisely so it narrows rather than replaces."""
        self._make(0, title="Etsy no price")
        kept = Listing(
            platform="ebay",
            platform_listing_id=f"MC-{secrets.token_hex(6)}",
            title="eBay no price",
            price_cents=5000,
            purchase_price_cents=0,
            status="active",
            team_id=self.team.id,
        )
        self.db.add(kept)
        self.db.commit()
        self._login()

        res = self.client.get("/api/listings?missing_cost=true&platform=ebay&page_size=200")
        self.assertEqual(res.status_code, 200, res.text)
        items = res.json()["listings"]
        self.assertTrue(items, "expected the eBay listing")
        for item in items:
            self.assertEqual(item["platform"], "ebay")
            self.assertTrue(item["missing_cost"])

    def test_other_users_listings_are_still_excluded(self):
        """The new filter must not widen tenancy scoping."""
        mine = self._make(0, title="Mine, no price")

        other = User(email=f"other.{secrets.token_hex(4)}@example.com",
                     name="Other", provider="google", is_admin=False)
        self.db.add(other)
        self.db.flush()
        other_team = Team(name=f"Other {secrets.token_hex(4)}",
                          invite_code=secrets.token_urlsafe(16))
        self.db.add(other_team)
        self.db.flush()
        self.db.add(TeamMembership(team_id=other_team.id,
                                   user_id=other.id, role="owner"))
        theirs = Listing(
            platform="etsy", platform_listing_id=f"MC-{secrets.token_hex(6)}",
            title="Theirs, no price", price_cents=1000,
            purchase_price_cents=0, status="active", team_id=other_team.id,
        )
        self.db.add(theirs)
        self.db.commit()
        self._login()

        res = self.client.get("/api/listings?missing_cost=true&page_size=200")
        ids = [l["id"] for l in res.json()["listings"]]
        self.assertIn(mine.id, ids)
        self.assertNotIn(theirs.id, ids, "another user's listing leaked in")

        self.db.query(Listing).filter(Listing.id == theirs.id).delete()
        self.db.query(TeamMembership).filter(
            TeamMembership.user_id == other.id).delete()
        self.db.query(Team).filter(Team.id == other_team.id).delete()
        self.db.query(AuthSession).filter(AuthSession.user_id == other.id).delete()
        self.db.query(User).filter(User.id == other.id).delete()
        self.db.commit()


class MissingCostUiWiringTest(unittest.TestCase):
    """The flag has to be reachable: a control to filter, and a marker on cards."""

    def _read(self, *parts):
        import pathlib
        root = pathlib.Path(__file__).resolve().parent.parent
        return (root.joinpath(*parts)).read_text(encoding="utf-8")

    def test_the_filter_control_exists_in_the_template(self):
        html = self._read("templates", "dashboard.html")
        self.assertIn('id="filter-missing-cost"', html)

    def test_the_js_reads_the_control_and_sends_the_parameter(self):
        js = self._read("static", "js", "dashboard.js")
        self.assertIn('getElementById("filter-missing-cost")', js)
        self.assertIn('params.set("missing_cost", "true")', js)

    def test_the_js_renders_a_badge_and_the_missing_line(self):
        js = self._read("static", "js", "dashboard.js")
        self.assertIn("card-status--missing-cost", js)
        self.assertIn("card-cost-missing", js)
        # Must not print a profit figure when there is no cost basis.
        self.assertIn("profit unknown", js)

    def test_the_css_for_the_flag_exists(self):
        css = self._read("static", "css", "style.css")
        self.assertIn(".card-status--missing-cost", css)
        self.assertIn(".card-cost-missing", css)
        self.assertIn(".listing-card--missing-cost", css)
        self.assertIn(".filter-check", css)

    def test_the_empty_state_counts_the_new_filter(self):
        """Otherwise a "missing cost" filter with no matches shows the
        "no listings synced yet" state, which is confusing."""
        js = self._read("static", "js", "dashboard.js")
        self.assertIn("state.missingCost", js)


if __name__ == "__main__":
    unittest.main(verbosity=2)
