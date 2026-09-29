"""Guard rails for the non-Etsy marketplace adapters.

The Etsy adapter once imported 900 pages of other people's listings from a
public feed, and its images never arrived because the response shape was read
wrong. This file checks the OTHER adapters for the same two failure modes:

  * never return fabricated/demo inventory, and
  * never let a hardcoded marketplace silently drop a seller's real listings.
"""

import secrets
import unittest

from tests import _env  # noqa: E402,F401  isort:skip  (must precede src imports)

from src.database import SessionLocal, init_db
from src.models import Team, TeamMembership, User


class AmazonDoesNotFabricateInventoryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()

    def setUp(self):
        from src.adapters import get_adapter
        self.adapter = get_adapter("amazon")

    def test_missing_connection_returns_empty_and_reports(self):
        rows = self.adapter.list_listings(db=None, user_id=None, credentials={})
        self.assertEqual(rows, [])
        self.assertIsNotNone(self.adapter.last_error)
        self.assertIn("connect", self.adapter.last_error.lower())

    def test_live_path_never_invents_price_or_image(self):
        """The SP-API inventory summary has no price or image, so the adapter
        must leave them empty instead of hardcoding a fake $29.99."""
        import inspect

        source = inspect.getsource(self.adapter.list_listings)
        self.assertNotIn("2999", source)
        self.assertNotIn("24.99", source)
        self.assertNotIn("placeholder.svg", source)
        self.assertNotIn("Ergonomic Desk Organizer", source)


class AmazonOAuthFlowTest(unittest.TestCase):
    """The callback must actually exchange the SP-API code for tokens.

    The previous implementation marked the account connected and stored a
    shop_name only - no token exchange, no refresh token - so a sync could never
    reach Amazon.
    """

    @classmethod
    def setUpClass(cls):
        init_db()

    def setUp(self):
        from src.adapters import get_adapter
        self.adapter = get_adapter("amazon")

    def test_authorization_url_includes_the_redirect_uri(self):
        from urllib.parse import parse_qs, urlparse

        url = self.adapter.get_authorization_url(
            state="s", credentials={"client_id": "amzn1.application"})
        query = parse_qs(urlparse(url).query)
        self.assertEqual(query["application_id"], ["amzn1.application"])
        self.assertIn("redirect_uri", query)
        self.assertIn("/api/auth/amazon/callback", query["redirect_uri"][0])
        self.assertEqual(query["version"], ["beta"])

    def test_handle_callback_exchanges_the_code_and_stores_tokens(self):
        import src.adapters.amazon as amazon_mod

        posted = {}

        class FakeResp:
            status_code = 200
            def json(self):
                return {
                    "access_token": "Atza|access",
                    "refresh_token": "Atzr|refresh",
                    "token_type": "bearer",
                    "expires_in": 3600,
                }

        def fake_post(url, **kw):
            posted["url"] = url
            posted["data"] = kw.get("data")
            return FakeResp()

        real = amazon_mod.httpx.post
        amazon_mod.httpx.post = fake_post
        db = SessionLocal()
        try:
            result = self.adapter.handle_callback(
                code="spapi-code", state="s",
                credentials={"client_id": "cid", "client_secret": "csec"},
                user_id=1, seller_id="A_SELLER",
            )
        finally:
            amazon_mod.httpx.post = real
            db.close()

        self.assertTrue(result["success"], result)
        self.assertEqual(posted["data"]["grant_type"], "authorization_code")
        self.assertEqual(posted["data"]["code"], "spapi-code")
        self.assertEqual(posted["data"]["client_id"], "cid")
        self.assertEqual(posted["data"]["client_secret"], "csec")

        # Tokens were persisted on an account row.
        from src.models import MarketplaceAccount
        db2 = SessionLocal()
        try:
            acct = db2.query(MarketplaceAccount).filter_by(
                platform="amazon", user_id=1).first()
            self.assertIsNotNone(acct)
            self.assertEqual(acct.access_token, "Atza|access")
            self.assertEqual(acct.refresh_token, "Atzr|refresh")
            self.assertEqual(acct.shop_name, "A_SELLER")
        finally:
            db2.query(MarketplaceAccount).filter_by(
                platform="amazon", user_id=1).delete()
            db2.commit()
            db2.close()

    def test_handle_callback_reports_a_failed_exchange(self):
        import src.adapters.amazon as amazon_mod

        class FakeResp:
            status_code = 400

        def fake_post(url, **kw):
            return FakeResp()

        real = amazon_mod.httpx.post
        amazon_mod.httpx.post = fake_post
        try:
            result = self.adapter.handle_callback(
                code="bad", state="s",
                credentials={"client_id": "cid", "client_secret": "csec"},
                user_id=1)
        finally:
            amazon_mod.httpx.post = real

        self.assertFalse(result["success"])
        self.assertIn("400", str(result.get("error", "")))


class PoshmarkDoesNotFabricateInventoryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()

    def setUp(self):
        from src.adapters import get_adapter
        self.adapter = get_adapter("poshmark")

    def test_missing_username_returns_empty_and_reports(self):
        from src.config import config

        old = config.POSHMARK_USERNAME
        config.POSHMARK_USERNAME = ""
        try:
            rows = self.adapter.list_listings(db=None, user_id=None, credentials={})
        finally:
            config.POSHMARK_USERNAME = old

        self.assertEqual(rows, [])
        self.assertIsNotNone(self.adapter.last_error)
        self.assertIn("closet", self.adapter.last_error.lower())

    def test_no_demo_inventory_in_the_source(self):
        import inspect

        source = inspect.getsource(self.adapter.list_listings)
        self.assertNotIn("POSH-DEMO", source)
        self.assertNotIn("Vintage Wool Trench Coat", source)

    def test_a_failed_fetch_is_reported_not_swallowed(self):
        import src.adapters.poshmark as posh_mod

        class FakeClient:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def get(self, *a, **kw):
                class R:
                    status_code = 500
                return R()

        real = posh_mod.httpx.Client
        posh_mod.httpx.Client = lambda *a, **kw: FakeClient()
        try:
            rows = self.adapter.list_listings(
                db=None, user_id=None, credentials={"username": "someone"})
        finally:
            posh_mod.httpx.Client = real

        self.assertEqual(rows, [])
        self.assertIn("500", str(self.adapter.last_error or ""))


class EbayUsesTheItemsMarketplaceTest(unittest.TestCase):
    def setUp(self):
        from src.adapters import get_adapter
        self.adapter = get_adapter("ebay")

    def test_marketplace_is_threaded_not_hardcoded(self):
        """The Marketplace Listing API needs the item's own marketplace. A
        hardcoded EBAY_US would silently drop every EBAY_CA / EBAY_GB item."""
        import src.adapters.ebay as ebay_mod

        captured = []

        class FakeResp:
            status_code = 200
            def json(self):
                return {"marketplaceListings": []}

        def fake_get(url, **kw):
            captured.append(kw.get("params", {}).get("marketplace_id"))
            return FakeResp()

        real = ebay_mod.httpx.get
        ebay_mod.httpx.get = fake_get
        try:
            # A Canadian item must be looked up on EBAY_CA.
            self.adapter._get_listing_details(
                {"id": "ITEM-1"}, {"Authorization": "Bearer t"}, "EBAY_CA")
        finally:
            ebay_mod.httpx.get = real

        self.assertEqual(captured, ["EBAY_CA"])

    def test_inventory_items_carry_their_marketplace(self):
        import src.adapters.ebay as ebay_mod

        class FakeResp:
            status_code = 200
            def json(self):
                return {"inventoryItems": [{"id": "ITEM-1"}]}
            def raise_for_status(self):
                pass

        requested = []

        def fake_get(url, **kw):
            requested.append(kw.get("params", {}).get("marketplace_id"))
            return FakeResp()

        real = ebay_mod.httpx.get
        ebay_mod.httpx.get = fake_get
        try:
            items = self.adapter._fetch_inventory_items(
                {"Authorization": "Bearer t"}, 500)
        finally:
            ebay_mod.httpx.get = real

        # First marketplace in the list is queried first.
        self.assertTrue(items)
        self.assertEqual(items[0][0], ebay_mod.EBAY_MARKETPLACES[0])
        self.assertEqual(items[0][1]["id"], "ITEM-1")


if __name__ == "__main__":
    unittest.main(verbosity=2)
