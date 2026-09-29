"""Sold data: pulling completed orders and recording them as sales.

Every listing endpoint these adapters call reads ACTIVE items, and an item leaves
those endpoints the moment it sells. Orders are therefore the only source of sold
data, and without them a sale is invisible to the dashboard until someone marks
it by hand.
"""

import secrets
import unittest
from datetime import datetime, timedelta

from tests import _env  # noqa: E402,F401  isort:skip  (must precede src imports)

from fastapi.testclient import TestClient

from src.adapters import get_adapter
from src.api.main import app
from src.database import SessionLocal, init_db
from src.models import AuthSession, Listing, Team, TeamMembership, User


class EbayOrderParsingTest(unittest.TestCase):
    """A faithful slice of an eBay Fulfillment order response.

    The money figures are the real ones from a live order: item 32.00 + buyer
    shipping 19.45 = 51.45, of which eBay took 9.56, leaving a 41.89 payout.
    """

    ORDER = {
        "orderId": "02-15233-11166",
        "creationDate": "2026-09-26T18:00:59.000Z",
        "orderPaymentStatus": "PAID",
        "pricingSummary": {
            "priceSubtotal": {"value": "32.0", "currency": "CAD"},
            "deliveryCost": {"value": "19.45", "currency": "CAD"},
            "total": {"value": "51.45", "currency": "CAD"},
        },
        "totalMarketplaceFee": {"value": "9.56", "currency": "CAD"},
        "paymentSummary": {"totalDueSeller": {"value": "41.89", "currency": "CAD"}},
        "lineItems": [
            {
                "legacyItemId": "800657064053",
                "title": "Memorex 4 Head VHS VCR MVR-4049 Works No Remote",
                "quantity": 1,
                "lineItemCost": {"value": "32.0", "currency": "CAD"},
                "deliveryCost": {"shippingCost": {"value": "19.45", "currency": "CAD"}},
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

    def test_fees_shipping_and_payout_are_captured(self):
        """The whole point: a sale price is not revenue. These three make profit
        real instead of a best case."""
        sale = self.adapter._sales_from_order(self.ORDER)[0]
        self.assertEqual(sale["fees_cents"], 956)
        self.assertEqual(sale["shipping_charged_cents"], 1945)
        self.assertEqual(sale["net_payout_cents"], 4189)
        # price + shipping - fees == payout, i.e. the figures reconcile.
        self.assertEqual(
            sale["price_cents"] + sale["shipping_charged_cents"] - sale["fees_cents"],
            sale["net_payout_cents"])

    def test_a_multi_item_order_allocates_money_exactly(self):
        """Fees and payout are reported per ORDER, so they must be split across
        the items without losing or inventing a penny."""
        order = dict(self.ORDER, lineItems=[
            dict(self.ORDER["lineItems"][0],
                 legacyItemId="111", lineItemCost={"value": "10.0", "currency": "CAD"},
                 deliveryCost={}),
            dict(self.ORDER["lineItems"][0],
                 legacyItemId="222", lineItemCost={"value": "30.0", "currency": "CAD"},
                 deliveryCost={}),
        ])
        sales = self.adapter._sales_from_order(order)
        self.assertEqual(len(sales), 2)
        self.assertEqual(sum(s["fees_cents"] for s in sales), 956)
        self.assertEqual(sum(s["net_payout_cents"] for s in sales), 4189)
        self.assertEqual(sum(s["shipping_charged_cents"] for s in sales), 1945)
        # The 10/30 split should give the smaller item roughly a quarter.
        self.assertEqual(sales[0]["net_payout_cents"], 1047)
        self.assertEqual(sales[1]["net_payout_cents"], 4189 - 1047)

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

    def test_an_order_with_no_money_blocks_does_not_raise(self):
        """Nothing here may crash a sync: absent fee data must degrade to zero."""
        order = {"orderId": "x", "creationDate": "2026-09-01T00:00:00.000Z",
                 "lineItems": [{"legacyItemId": "9", "title": "t", "quantity": 1,
                                "lineItemCost": {"value": "5.0"}}]}
        sale = self.adapter._sales_from_order(order)[0]
        self.assertEqual(sale["fees_cents"], 0)
        self.assertEqual(sale["net_payout_cents"], 0)

    def test_a_platform_without_a_sales_api_is_a_noop(self):
        """Poshmark and Amazon have no orders API; the route calls sync_sales
        unconditionally, so it must not raise."""
        res = get_adapter("poshmark").sync_sales(None)
        self.assertFalse(res["supported"])
        self.assertEqual(res["fetched"], 0)


class EbayShippingCostTest(unittest.TestCase):
    """Actual postage paid, which only the payment ledger records.

    An order says what the BUYER paid for shipping, never what the seller paid to
    ship it. A label bought through eBay is its own money movement.
    """

    def setUp(self):
        self.adapter = get_adapter("ebay")

    def _with_ledger(self, transactions):
        import src.adapters.ebay as ebay_mod

        class Resp:
            status_code = 200
            def json(self):
                return {"transactions": transactions}

        real = ebay_mod.httpx.get
        ebay_mod.httpx.get = lambda *a, **kw: Resp()
        try:
            return self.adapter._fetch_shipping_costs(
                {"Authorization": "Bearer t"}, "2026-01-01T00:00:00.000Z")
        finally:
            ebay_mod.httpx.get = real

    def test_label_transactions_become_a_cost_per_order(self):
        costs = self._with_ledger([
            {"transactionType": "SHIPPING_LABEL", "orderId": "02-1",
             "bookingEntry": "DEBIT", "amount": {"value": "15.50", "currency": "CAD"}},
            # A sale is money IN and must not be mistaken for postage.
            {"transactionType": "SALE", "orderId": "02-1",
             "amount": {"value": "32.0", "currency": "CAD"}},
            # One order can carry several labels; they accumulate.
            {"transactionType": "SHIPPING_LABEL", "orderId": "02-1",
             "bookingEntry": "DEBIT", "amount": {"value": "3.25", "currency": "CAD"}},
            {"transactionType": "REFUND", "orderId": "02-2",
             "amount": {"value": "5.0", "currency": "CAD"}},
        ])
        # Labels are reported as POSITIVE amounts marked DEBIT, so direction comes
        # from bookingEntry and the cost is the sum of them.
        self.assertEqual(costs, {"02-1": 1875})

    def test_a_reversed_label_reduces_the_cost(self):
        """A CREDIT is a refunded label, not a second charge."""
        costs = self._with_ledger([
            {"transactionType": "SHIPPING_LABEL", "orderId": "02-1",
             "bookingEntry": "DEBIT", "amount": {"value": "20.00", "currency": "CAD"}},
            {"transactionType": "SHIPPING_LABEL", "orderId": "02-1",
             "bookingEntry": "CREDIT", "amount": {"value": "20.00", "currency": "CAD"}},
        ])
        self.assertEqual(costs, {"02-1": 0})

    def test_a_denied_scope_leaves_costs_empty(self):
        import src.adapters.ebay as ebay_mod

        class Denied:
            status_code = 403

        real = ebay_mod.httpx.get
        ebay_mod.httpx.get = lambda *a, **kw: Denied()
        try:
            self.assertEqual(
                self.adapter._fetch_shipping_costs({"Authorization": "Bearer t"}, "x"), {})
        finally:
            ebay_mod.httpx.get = real

    def test_an_order_without_a_label_gets_no_cost(self):
        """Shipping outside eBay must leave the manual figure alone rather than
        asserting a zero."""
        sales = [{"order_id": "o1", "price_cents": 1000},
                 {"order_id": "o1", "price_cents": 3000},
                 {"order_id": "o2", "price_cents": 500}]
        attached = self.adapter._attach_shipping_costs(sales, {"o1": 1000, "o2": 0})
        self.assertEqual(attached, 2)
        self.assertEqual(sales[0]["shipping_cost_cents"], 250)
        self.assertEqual(sales[1]["shipping_cost_cents"], 750)
        self.assertEqual(sales[0]["shipping_cost_cents"] + sales[1]["shipping_cost_cents"], 1000)
        self.assertNotIn("shipping_cost_cents", sales[2])


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

    def test_actual_profit_uses_the_payout_not_the_sale_price(self):
        """The headline number. A 32.00 sale with a 9.56 fee and no recorded cost
        is NOT 32.00 of profit."""
        row = self._listing("800657064053", title="Memorex VCR", price=3200)
        self.adapter.record_sales(self.db, [{
            "platform_listing_id": "800657064053", "title": "Memorex VCR",
            "price_cents": 3200, "currency": "CAD", "sold_at": None,
            "fees_cents": 956, "shipping_charged_cents": 1945,
            "net_payout_cents": 4189,
        }], owner_user_id=self.user.id)

        self.db.refresh(row)
        self.assertEqual(row.fees_cents, 956)
        self.assertEqual(row.shipping_charged_cents, 1945)
        self.assertEqual(row.net_payout_cents, 4189)
        # Payout is revenue; the sale price is not.
        self.assertEqual(row.actual_revenue_cents, 4189)
        self.assertEqual(row.actual_profit_cents, 4189)

    def test_postage_the_seller_paid_is_subtracted(self):
        """A payout already nets off marketplace fees and the buyer's shipping,
        so the seller's own postage is the remaining deduction."""
        row = self._listing("800657064060", title="VCR", price=3200)
        row.net_payout_cents = 4189
        row.purchase_price_cents = 1000
        row.shipping_cost_cents = 1500
        self.db.commit()
        self.db.refresh(row)
        self.assertEqual(row.actual_profit_cents, 4189 - 1000 - 1500)

    def test_a_marketplace_label_cost_is_recorded(self):
        """A label bought through the platform is authoritative, so it is applied
        and the profit drops to match."""
        row = self._listing("800657064070", title="Shipped by eBay", price=3200)
        self.adapter.record_sales(self.db, [{
            "platform_listing_id": "800657064070", "title": "Shipped by eBay",
            "price_cents": 3200, "currency": "CAD", "sold_at": None,
            "net_payout_cents": 4189, "shipping_cost_cents": 1550,
        }], owner_user_id=self.user.id)

        self.db.refresh(row)
        self.assertEqual(row.shipping_cost_cents, 1550)
        self.assertEqual(row.actual_profit_cents, 4189 - 1550)

    def test_a_manual_postage_figure_survives_when_no_label_is_recorded(self):
        """Shipping outside the platform must not be reset to zero by a sync."""
        row = self._listing("800657064071", title="Shipped myself", price=3200)
        row.shipping_cost_cents = 1200
        self.db.commit()

        self.adapter.record_sales(self.db, [{
            "platform_listing_id": "800657064071", "title": "Shipped myself",
            "price_cents": 3200, "currency": "CAD", "sold_at": None,
            "net_payout_cents": 4189,
        }], owner_user_id=self.user.id)

        self.db.refresh(row)
        self.assertEqual(row.shipping_cost_cents, 1200)

    def test_actual_profit_falls_back_to_the_price_before_a_sale(self):
        """An active listing has no payout yet, so the listed price is the best
        available estimate rather than a zero."""
        row = self._listing("800570000020", title="Still listed", price=2500)
        self.assertEqual(row.actual_revenue_cents, 2500)
        self.assertEqual(row.actual_profit_cents, 2500)

    def test_a_real_title_is_not_overwritten(self):
        row = self._listing("800570000001", title="My own careful title")
        self.adapter.record_sales(self.db, [{
            "platform_listing_id": "800570000001", "title": "eBay's version",
            "price_cents": 100, "currency": "CAD", "sold_at": None,
        }], owner_user_id=self.user.id)
        self.db.refresh(row)
        self.assertEqual(row.title, "My own careful title")

    # -- pictures --

    def test_a_recorded_sale_keeps_its_picture(self):
        url = "https://i.ebayimg.com/images/g/HOA/s-l1600.jpg"
        self.adapter.record_sales(self.db, [{
            "platform_listing_id": "800590825148", "title": "ASUS GTX 1080",
            "price_cents": 4500, "currency": "CAD", "sold_at": None,
            "image_url": url, "images": [url],
        }], owner_user_id=self.user.id)

        row = self.db.query(Listing).filter_by(
            platform="ebay", platform_listing_id="800590825148").first()
        self.assertEqual(row.image_url, url)
        self.assertEqual(row.images_json, [url])

    def test_a_missing_picture_is_filled_and_an_existing_one_kept(self):
        row = self._listing("800570000003", title="No photo yet")
        self.adapter.record_sales(self.db, [{
            "platform_listing_id": "800570000003", "title": "x",
            "price_cents": 100, "currency": "CAD", "sold_at": None,
            "image_url": "https://first.jpg", "images": ["https://first.jpg"],
        }], owner_user_id=self.user.id)
        self.db.refresh(row)
        self.assertEqual(row.image_url, "https://first.jpg")

        # A later run must not clobber a picture the row already has.
        self.adapter.record_sales(self.db, [{
            "platform_listing_id": "800570000003", "title": "x",
            "price_cents": 100, "currency": "CAD", "sold_at": None,
            "image_url": "https://second.jpg", "images": ["https://second.jpg"],
        }], owner_user_id=self.user.id)
        self.db.refresh(row)
        self.assertEqual(row.image_url, "https://first.jpg")

    def test_attach_images_only_fetches_what_is_missing(self):
        row = self._listing("800570000010", title="Already photographed")
        row.image_url = "https://i.ebayimg.com/existing.jpg"
        self.db.commit()

        calls = []

        def fake_browse(item_id, headers):
            calls.append(item_id)
            return {"image_url": "https://i.ebayimg.com/new.jpg",
                    "images": ["https://i.ebayimg.com/new.jpg"]}

        self.adapter._fetch_browse_item = fake_browse
        sales = [{"platform_listing_id": "800570000010"},
                 {"platform_listing_id": "800570000011"}]
        attached = self.adapter._attach_sale_images(self.db, sales, {}, "tok", "EBAY_CA")

        self.assertEqual(attached, 1)
        # The row that already had a picture must not be looked up again.
        self.assertEqual(calls, ["800570000011"])
        self.assertEqual(sales[1]["image_url"], "https://i.ebayimg.com/new.jpg")

    def test_attach_images_falls_back_to_getitem(self):
        calls = []

        def no_browse(item_id, headers):
            calls.append(("browse", item_id))
            return {}

        def fake_getitem(item_id, token, marketplace):
            calls.append(("getitem", item_id))
            return {"image_url": "https://fallback.jpg", "images": ["https://fallback.jpg"]}

        self.adapter._fetch_browse_item = no_browse
        self.adapter._fetch_getitem = fake_getitem
        sales = [{"platform_listing_id": "800570000012"}]
        attached = self.adapter._attach_sale_images(self.db, sales, {}, "tok", "EBAY_CA")

        self.assertEqual(attached, 1)
        self.assertEqual([c[0] for c in calls], ["browse", "getitem"])
        self.assertEqual(sales[0]["image_url"], "https://fallback.jpg")


class RealisedProfitBasisTest(unittest.TestCase):
    """The Realized Profit KPI must use the payout, not the listed price, and must
    count postage the seller paid.

    Summing price_cents overstated every sale by the marketplace fee, and leaving
    postage out of cost overstated it a second time by the price of the label. The
    figures here are a real sale: CAD 110.00 item, 26.54 fee, 37.56 postage charged
    to the buyer, 31.25 label paid, 20.00 paid for the item, 121.02 payout.
    """

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
        self.user = User(email=f"profit.{secrets.token_hex(4)}@example.com",
                         name="Profit Tester", provider="google", is_admin=False)
        self.db.add(self.user)
        self.db.flush()
        self.team = Team(name=f"Profit {secrets.token_hex(4)}",
                         invite_code=secrets.token_urlsafe(16))
        self.db.add(self.team)
        self.db.flush()
        self.db.add(TeamMembership(team_id=self.team.id, user_id=self.user.id,
                                   role="owner"))
        self.db.commit()

        token = secrets.token_urlsafe(48)
        self.db.add(AuthSession(token=token, user_id=self.user.id,
                                expires_at=datetime.utcnow() + timedelta(hours=1)))
        self.db.commit()
        self.client.cookies.clear()
        self.client.cookies.set("auth_token", token)

    def tearDown(self):
        uid, tid = self.user.id, self.team.id
        self.db.rollback()
        self.db.query(Listing).filter(Listing.team_id == tid).delete(synchronize_session=False)
        self.db.query(TeamMembership).filter(TeamMembership.user_id == uid).delete(synchronize_session=False)
        self.db.query(Team).filter(Team.id == tid).delete(synchronize_session=False)
        self.db.query(AuthSession).filter(AuthSession.user_id == uid).delete(synchronize_session=False)
        self.db.query(User).filter(User.id == uid).delete(synchronize_session=False)
        self.db.commit()
        self.db.close()

    def _sold(self, item_id, **kw):
        row = Listing(platform="ebay", platform_listing_id=item_id,
                      title="Sold thing", currency="CAD", status="sold",
                      is_sold=True, sold_at=datetime.utcnow(),
                      team_id=self.team.id, **kw)
        self.db.add(row)
        self.db.commit()
        return row

    def test_profit_uses_the_payout_and_counts_postage(self):
        self._sold("800531403439", price_cents=11000, fees_cents=2654,
                   shipping_charged_cents=3756, shipping_cost_cents=3125,
                   net_payout_cents=12102, purchase_price_cents=2000)

        data = self.client.get("/api/stats?days=all").json()

        # Payout 121.02 - item 20.00 - label 31.25 = 69.77, NOT 110 - 20 = 90.00.
        self.assertEqual(data["sold_revenue_cents"], 12102)
        self.assertEqual(data["sold_cost_cents"], 2000 + 3125)
        self.assertEqual(data["sold_profit_cents"], 12102 - 2000 - 3125)

    def test_the_listed_price_is_the_fallback_when_there_is_no_payout(self):
        """A sale marked by hand has no payout to use, so the price stands in
        rather than being dropped to zero."""
        self._sold("800570000099", price_cents=5000, purchase_price_cents=1000)

        data = self.client.get("/api/stats?days=all").json()
        self.assertEqual(data["sold_revenue_cents"], 5000)
        self.assertEqual(data["sold_cost_cents"], 1000)
        self.assertEqual(data["sold_profit_cents"], 4000)

    def test_the_timeline_uses_the_same_basis_as_the_totals(self):
        """The chart and the KPI disagreeing is worse than either being wrong."""
        self._sold("800531403439", price_cents=11000, fees_cents=2654,
                   shipping_cost_cents=3125, net_payout_cents=12102,
                   purchase_price_cents=2000)

        data = self.client.get("/api/stats?days=all").json()
        points = data["timeline"]["points"]
        chart_revenue = sum(p.get("revenue_cents", 0) for p in points)
        chart_profit = sum(p.get("profit_cents", 0) for p in points)
        self.assertGreater(chart_revenue, 0)
        self.assertEqual(chart_profit, data["sold_profit_cents"])


class EtsyReceiptTest(unittest.TestCase):
    """Etsy sold data comes from receipts, and its fees from the payment ledger.

    A receipt carries the sale but no fees at all; the ledger has the fees and
    points at the receipt. Figures below are a real receipt.
    """

    RECEIPT = {
        "receipt_id": 4172298896,
        "status": "Completed",
        "created_timestamp": 1789184386,
        "grandtotal": {"amount": 41027, "divisor": 100, "currency_code": "CAD"},
        "total_shipping_cost": {"amount": 10297, "divisor": 100, "currency_code": "CAD"},
        "transactions": [
            {"transaction_id": 5213754614, "listing_id": 4568130719, "quantity": 1,
             "price": {"amount": 27000, "divisor": 100},
             "title": "Refurbished Modded Original Xbox Console"},
        ],
    }

    def setUp(self):
        self.adapter = get_adapter("etsy")

    def test_a_receipt_becomes_a_sale(self):
        sale = self.adapter._sales_from_receipt(self.RECEIPT, {})[0]
        self.assertEqual(sale["platform_listing_id"], "4568130719")
        self.assertEqual(sale["price_cents"], 27000)
        self.assertEqual(sale["shipping_charged_cents"], 10297)
        self.assertEqual(sale["currency"], "CAD")
        self.assertEqual(sale["sold_at"].isoformat(), "2026-09-12T03:39:46")

    def test_ledger_fees_reduce_the_payout(self):
        """Etsy reports gross and its charges separately and has no payout field,
        so net is derived. Without this a 410.27 receipt would count as 410.27 of
        revenue with the 65.90 of charges ignored."""
        sale = self.adapter._sales_from_receipt(self.RECEIPT, {4172298896: 6590})[0]
        self.assertEqual(sale["fees_cents"], 6590)
        self.assertEqual(sale["net_payout_cents"], 41027 - 6590)

    def test_no_ledger_fees_means_gross_is_the_payout(self):
        sale = self.adapter._sales_from_receipt(self.RECEIPT, {})[0]
        self.assertEqual(sale["fees_cents"], 0)
        self.assertEqual(sale["net_payout_cents"], 41027)

    def test_a_multi_item_receipt_splits_money_exactly(self):
        receipt = dict(self.RECEIPT, transactions=[
            dict(self.RECEIPT["transactions"][0], listing_id=111,
                 price={"amount": 10000, "divisor": 100}),
            dict(self.RECEIPT["transactions"][0], listing_id=222,
                 price={"amount": 30000, "divisor": 100}),
        ])
        sales = self.adapter._sales_from_receipt(receipt, {4172298896: 6590})
        self.assertEqual(len(sales), 2)
        self.assertEqual(sum(s["fees_cents"] for s in sales), 6590)
        self.assertEqual(sum(s["net_payout_cents"] for s in sales), 41027 - 6590)
        self.assertEqual(sum(s["shipping_charged_cents"] for s in sales), 10297)

    def test_a_receipt_with_no_lines_is_skipped(self):
        receipt = dict(self.RECEIPT, transactions=[])
        self.assertEqual(self.adapter._sales_from_receipt(receipt, {}), [])

    def test_a_cancelled_receipt_is_not_a_sale(self):
        """Cancelled orders never completed, so they must not become revenue."""
        import src.adapters.etsy as etsy_mod

        payload = {"results": [
            dict(self.RECEIPT, receipt_id=1),
            dict(self.RECEIPT, receipt_id=2, status="Canceled"),
        ]}

        class Resp:
            status_code = 200
            def json(self):
                return payload

        real = etsy_mod.httpx.get
        etsy_mod.httpx.get = lambda *a, **kw: Resp()
        try:
            receipts = self.adapter._fetch_receipts({}, 1, cutoff=0)
        finally:
            etsy_mod.httpx.get = real
        self.assertEqual([r["receipt_id"] for r in receipts], [1])

    def test_label_costs_attach_to_the_right_receipt(self):
        """Etsy records a label against the LABEL id, not the receipt, so the
        link is made by matching the label's purchase time to the receipt's
        shipment notification."""
        entries = [
            # Label charge, then an adjustment to the SAME label days later.
            {"amount": -13036, "reference_type": "shipping_label",
             "reference_id": 1, "ledger_type": "shipping_labels", "create_date": 1789228357},
            {"amount": -607, "reference_type": "shipping_label",
             "reference_id": 1, "ledger_type": "shipping_label_adjustment",
             "create_date": 1789473617},
            {"amount": -1803, "reference_type": "shipping_label",
             "reference_id": 2, "ledger_type": "shipping_labels", "create_date": 1789062361},
            # A sale is money IN and must never count as postage.
            {"amount": 41027, "reference_type": "shop_payment",
             "ledger_type": "PAYMENT_GROSS", "create_date": 1789184394},
        ]
        receipts = [
            {"receipt_id": 111, "created_timestamp": 1789184386,
             "shipments": [{"shipment_notification_timestamp": 1789228800}]},
            {"receipt_id": 222, "created_timestamp": 1789042240,
             "shipments": [{"shipment_notification_timestamp": 1789062400}]},
        ]

        costs = self.adapter._label_costs_by_receipt(entries, receipts)

        # The adjustment rides along with its own label rather than landing on
        # whichever receipt happens to be nearest.
        self.assertEqual(costs[111], 13036 + 607)
        self.assertEqual(costs[222], 1803)

    def test_an_unrelated_label_is_left_unattached(self):
        """A label bought days from any shipment must not be pinned on the
        nearest receipt: that would silently inflate its postage."""
        entries = [{"amount": -5000, "reference_type": "shipping_label",
                    "reference_id": 9, "ledger_type": "shipping_labels",
                    "create_date": 1788000000}]
        receipts = [{"receipt_id": 111, "created_timestamp": 1789184386,
                     "shipments": [{"shipment_notification_timestamp": 1789228800}]}]
        self.assertEqual(self.adapter._label_costs_by_receipt(entries, receipts), {})

    def test_label_cost_reaches_the_sale(self):
        sale = self.adapter._sales_from_receipt(
            self.RECEIPT, {4172298896: 6590}, {4172298896: 13643})[0]
        self.assertEqual(sale["shipping_cost_cents"], 13643)
        # The buyer's postage and the seller's label are different figures.
        self.assertEqual(sale["shipping_charged_cents"], 10297)

    def test_a_multi_item_receipt_splits_label_costs_exactly(self):
        receipt = dict(self.RECEIPT, transactions=[
            dict(self.RECEIPT["transactions"][0], listing_id=111,
                 price={"amount": 10000, "divisor": 100}),
            dict(self.RECEIPT["transactions"][0], listing_id=222,
                 price={"amount": 30000, "divisor": 100}),
        ])
        sales = self.adapter._sales_from_receipt(receipt, {}, {4172298896: 1000})
        self.assertEqual(sum(s["shipping_cost_cents"] for s in sales), 1000)

    def test_etsy_implements_its_own_sales_sync(self):
        """It used to inherit the base no-op, which is why Etsy solds never
        appeared even though the scope was granted."""
        from src.adapters.base import MarketplaceAdapter

        self.assertIsNot(type(self.adapter).sync_sales, MarketplaceAdapter.sync_sales)


class EstimatedFeeTest(unittest.TestCase):
    """An active listing shows a net with the platform's cut taken off.

    eBay exposes no category final-value-fee rate through its API, so the estimate
    is built from the fees this account has actually been charged: per category
    where that category has history, per platform otherwise.
    """

    @classmethod
    def setUpClass(cls):
        init_db()
        from src.api.routes import _estimated_fees, _observed_fee_rates  # noqa: F401
        cls.rates_for = staticmethod(_observed_fee_rates)
        cls.estimate = staticmethod(_estimated_fees)

    def setUp(self):
        self.db = SessionLocal()
        self.user = User(email=f"fees.{secrets.token_hex(4)}@example.com",
                         name="Fee Tester", provider="google", is_admin=False)
        self.db.add(self.user)
        self.db.flush()
        self.team = Team(name=f"Fees {secrets.token_hex(4)}",
                         invite_code=secrets.token_urlsafe(16))
        self.db.add(self.team)
        self.db.flush()
        self.db.add(TeamMembership(team_id=self.team.id, user_id=self.user.id, role="owner"))
        self.db.commit()

    def tearDown(self):
        tid = self.team.id
        self.db.rollback()
        self.db.query(Listing).filter(Listing.team_id == tid).delete(synchronize_session=False)
        self.db.query(TeamMembership).filter(TeamMembership.team_id == tid).delete(synchronize_session=False)
        self.db.query(Team).filter(Team.id == tid).delete(synchronize_session=False)
        self.db.query(User).filter(User.id == self.user.id).delete(synchronize_session=False)
        self.db.commit()
        self.db.close()

    def _listing(self, **kw):
        kw.setdefault("platform", "ebay")
        kw.setdefault("currency", "CAD")
        kw.setdefault("title", "Thing")
        row = Listing(team_id=self.team.id, **kw)
        self.db.add(row)
        self.db.commit()
        return row

    def test_the_rate_comes_from_what_was_actually_charged(self):
        """110.00 sold, 26.54 of fees -> 24.1%, which the next listing inherits."""
        self._listing(platform_listing_id="a", status="sold", is_sold=True,
                      price_cents=11000, fees_cents=2654,
                      category="Computers|GPUs")
        rates = self.rates_for(self.db, [self.team.id])
        leaf = "GPUs"
        self.assertAlmostEqual(rates[("ebay", leaf)], 2654 / 11000, places=6)

    def test_a_category_rate_beats_the_platform_average(self):
        """Category is what the fee actually depends on, so its own history wins."""
        self._listing(platform_listing_id="a", status="sold", is_sold=True,
                      price_cents=10000, fees_cents=1000, category="Computers|GPUs")
        self._listing(platform_listing_id="b", status="sold", is_sold=True,
                      price_cents=10000, fees_cents=3000, category="Cameras|VCRs")

        rates = self.rates_for(self.db, [self.team.id])
        self.assertAlmostEqual(rates[("ebay", "GPUs")], 0.10, places=6)
        self.assertAlmostEqual(rates[("ebay", "VCRs")], 0.30, places=6)
        # Platform-wide blends both.
        self.assertAlmostEqual(rates[("ebay", None)], 0.20, places=6)

        gpu = self._listing(platform_listing_id="c", status="active",
                            price_cents=5000, category="Computers|GPUs")
        self.assertEqual(self.estimate(gpu, rates), 500)

    def test_a_category_with_no_history_falls_back_to_the_platform(self):
        self._listing(platform_listing_id="a", status="sold", is_sold=True,
                      price_cents=10000, fees_cents=2000, category="Computers|GPUs")
        rates = self.rates_for(self.db, [self.team.id])

        unknown = self._listing(platform_listing_id="b", status="active",
                                price_cents=10000, category="Cameras|Something New")
        self.assertEqual(self.estimate(unknown, rates), 2000)

    def test_history_from_another_platform_is_never_borrowed(self):
        """eBay's fee is not Etsy's, so a new Etsy listing must not inherit one."""
        self._listing(platform_listing_id="a", status="sold", is_sold=True,
                      price_cents=10000, fees_cents=2500, category="Computers|GPUs")
        rates = self.rates_for(self.db, [self.team.id])

        etsy = self._listing(platform="etsy", platform_listing_id="b", status="active",
                             price_cents=10000, category="Computers|GPUs")
        self.assertIsNone(self.estimate(etsy, rates))

    def test_no_estimate_without_a_basis(self):
        self.assertEqual(self.rates_for(self.db, [self.team.id]), {})
        row = self._listing(platform_listing_id="a", status="active", price_cents=10000)
        self.assertIsNone(self.estimate(row, {}))

    def test_a_sold_listing_is_not_given_an_estimate(self):
        """Its fee is already known, so an estimate would compete with the truth."""
        self._listing(platform_listing_id="a", status="sold", is_sold=True,
                      price_cents=10000, fees_cents=2500, category="Computers|GPUs")
        rates = self.rates_for(self.db, [self.team.id])
        sold = self.db.query(Listing).filter(Listing.team_id == self.team.id).first()
        self.assertIsNone(self.estimate(sold, rates))

    def test_a_listing_with_no_price_gets_no_estimate(self):
        self._listing(platform_listing_id="a", status="sold", is_sold=True,
                      price_cents=10000, fees_cents=2500, category="Computers|GPUs")
        rates = self.rates_for(self.db, [self.team.id])
        free = self._listing(platform_listing_id="b", status="active", price_cents=0)
        self.assertIsNone(self.estimate(free, rates))


if __name__ == "__main__":
    unittest.main(verbosity=2)
