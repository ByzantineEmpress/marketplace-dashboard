"""Sold data: pulling completed orders and recording them as sales.

Every listing endpoint these adapters call reads ACTIVE items, and an item leaves
those endpoints the moment it sells. Orders are therefore the only source of sold
data, and without them a sale is invisible to the dashboard until someone marks
it by hand.
"""

import secrets
import unittest

from tests import _env  # noqa: E402,F401  isort:skip  (must precede src imports)

from src.adapters import get_adapter
from src.database import SessionLocal, init_db
from src.models import Listing, Team, TeamMembership, User


class EbayOrderParsingTest(unittest.TestCase):
    """A faithful slice of an eBay Fulfillment order response."""

    ORDER = {
        "orderId": "02-15233-11166",
        "creationDate": "2026-09-26T18:00:59.000Z",
        "orderPaymentStatus": "PAID",
        "lineItems": [
            {
                "legacyItemId": "800657064053",
                "title": "Memorex 4 Head VHS VCR MVR-4049 Works No Remote",
                "quantity": 1,
                "lineItemCost": {"value": "32.0", "currency": "CAD"},
            },
        ],
    }

    def setUp(self):
        self.adapter = get_adapter("ebay")

    def test_a_line_item_becomes_a_sale(self):
        sales = self.adapter._sales_from_order(self.ORDER)
        self.assertEqual(len(sales), 1)
        sale = sales[0]
        self.assertEqual(sale["platform_listing_id"], "800657064053")
        self.assertEqual(sale["price_cents"], 3200)
        self.assertEqual(sale["currency"], "CAD")
        self.assertEqual(sale["title"], "Memorex 4 Head VHS VCR MVR-4049 Works No Remote")
        self.assertEqual(sale["sold_at"].isoformat(), "2026-09-26T18:00:59")

    def test_line_item_cost_is_taken_as_the_line_total(self):
        """Realised revenue sums price_cents across sold rows, so a per-unit
        price would understate any multi-quantity sale."""
        order = dict(self.ORDER, lineItems=[dict(
            self.ORDER["lineItems"][0], quantity=3,
            lineItemCost={"value": "90.0", "currency": "CAD"})])
        sales = self.adapter._sales_from_order(order)
        self.assertEqual(sales[0]["price_cents"], 9000)
        self.assertEqual(sales[0]["quantity"], 3)

    def test_a_line_without_an_item_id_is_skipped(self):
        order = dict(self.ORDER, lineItems=[{"title": "no id", "lineItemCost": {"value": "5"}}])
        self.assertEqual(self.adapter._sales_from_order(order), [])

    def test_a_missing_date_does_not_raise(self):
        order = dict(self.ORDER, creationDate="")
        self.assertIsNone(self.adapter._sales_from_order(order)[0]["sold_at"])

    def test_a_platform_without_a_sales_api_is_a_noop(self):
        """Poshmark and Amazon have no orders API; the route calls sync_sales
        unconditionally, so it must not raise."""
        res = get_adapter("poshmark").sync_sales(None)
        self.assertFalse(res["supported"])
        self.assertEqual(res["fetched"], 0)


class RecordSalesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()

    def setUp(self):
        self.db = SessionLocal()
        self.user = User(email=f"sales.{secrets.token_hex(4)}@example.com",
                         name="Sales Tester", provider="google", is_admin=False)
        self.db.add(self.user)
        self.db.flush()
        self.team = Team(name=f"Sales {secrets.token_hex(4)}",
                         invite_code=secrets.token_urlsafe(16))
        self.db.add(self.team)
        self.db.flush()
        self.db.add(TeamMembership(team_id=self.team.id, user_id=self.user.id,
                                   role="owner"))
        self.db.commit()
        self.adapter = get_adapter("ebay")

    def tearDown(self):
        uid, tid = self.user.id, self.team.id
        self.db.rollback()
        self.db.query(Listing).filter(Listing.team_id == tid).delete(synchronize_session=False)
        self.db.query(TeamMembership).filter(TeamMembership.user_id == uid).delete(synchronize_session=False)
        self.db.query(Team).filter(Team.id == tid).delete(synchronize_session=False)
        self.db.query(User).filter(User.id == uid).delete(synchronize_session=False)
        self.db.commit()
        self.db.close()

    def _listing(self, item_id, title="Thing", price=5000):
        row = Listing(platform="ebay", platform_listing_id=item_id, title=title,
                      price_cents=price, currency="CAD", status="active",
                      team_id=self.team.id)
        self.db.add(row)
        self.db.commit()
        return row

    def test_a_known_item_is_marked_sold_with_the_real_price_and_date(self):
        from datetime import datetime

        row = self._listing("800657064053", title="Memorex VCR", price=5000)
        sold_at = datetime(2026, 9, 26, 18, 0, 59)
        out = self.adapter.record_sales(self.db, [{
            "platform_listing_id": "800657064053", "title": "Memorex VCR",
            "price_cents": 3200, "currency": "CAD", "sold_at": sold_at,
        }], owner_user_id=self.user.id)

        self.assertEqual(out, {"recorded": 1, "created": 0})
        self.db.refresh(row)
        self.assertTrue(row.is_sold)
        self.assertEqual(row.status, "sold")
        self.assertEqual(row.sold_at, sold_at)
        # The sale price, not the asking price, is what revenue must reflect.
        self.assertEqual(row.price_cents, 3200)
        self.assertEqual(row.available_quantity, 0)

    def test_an_unknown_item_is_still_recorded(self):
        """An item bought and sold between two syncs is never in the active
        feed, so dropping it would silently lose the revenue."""
        out = self.adapter.record_sales(self.db, [{
            "platform_listing_id": "800590825148", "title": "ASUS GTX 1080",
            "price_cents": 4500, "currency": "CAD", "sold_at": None,
        }], owner_user_id=self.user.id)

        self.assertEqual(out, {"recorded": 0, "created": 1})
        row = self.db.query(Listing).filter_by(
            platform="ebay", platform_listing_id="800590825148").first()
        self.assertIsNotNone(row)
        self.assertTrue(row.is_sold)
        self.assertEqual(row.price_cents, 4500)
        self.assertEqual(row.title, "ASUS GTX 1080")
        # The sale must land in the syncing user's team, never someone else's.
        self.assertEqual(row.team_id, self.team.id)

    def test_a_generated_placeholder_title_is_replaced_by_the_real_one(self):
        row = self._listing("800591216326", title="eBay listing 800591216326")
        self.adapter.record_sales(self.db, [{
            "platform_listing_id": "800591216326", "title": "XPG GAMMIX D10 16GB DDR4",
            "price_cents": 6000, "currency": "CAD", "sold_at": None,
        }], owner_user_id=self.user.id)
        self.db.refresh(row)
        self.assertEqual(row.title, "XPG GAMMIX D10 16GB DDR4")

    def test_a_real_title_is_not_overwritten(self):
        row = self._listing("800570000001", title="My own careful title")
        self.adapter.record_sales(self.db, [{
            "platform_listing_id": "800570000001", "title": "eBay's version",
            "price_cents": 100, "currency": "CAD", "sold_at": None,
        }], owner_user_id=self.user.id)
        self.db.refresh(row)
        self.assertEqual(row.title, "My own careful title")


if __name__ == "__main__":
    unittest.main(verbosity=2)
