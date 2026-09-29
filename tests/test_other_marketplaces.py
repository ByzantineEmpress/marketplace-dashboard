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
    def setUp(self):
        from src.adapters import get_adapter
        self.adapter = get_adapter("amazon")

    def test_missing_credentials_returns_empty_and_reports(self):
        from src.config import config

        old_client = config.AMAZON_CLIENT_ID
        old_refresh = config.AMAZON_REFRESH_TOKEN
        config.AMAZON_CLIENT_ID = ""
        config.AMAZON_REFRESH_TOKEN = ""
        try:
            rows = self.adapter.list_listings(db=None, user_id=None, credentials={})
        finally:
            config.AMAZON_CLIENT_ID = old_client
            config.AMAZON_REFRESH_TOKEN = old_refresh

        self.assertEqual(rows, [])
        self.assertIsNotNone(self.adapter.last_error)
        self.assertIn("incomplete", self.adapter.last_error.lower())

    def test_live_path_never_invents_price_or_image(self):
        """The SP-API inventory summary has no price or image, so the adapter
        must leave them empty instead of hardcoding a fake $29.99."""
        import inspect

        source = inspect.getsource(self.adapter.list_listings)
        self.assertNotIn("2999", source)
        self.assertNotIn("24.99", source)
        self.assertNotIn("placeholder.svg", source)
        self.assertNotIn("Ergonomic Desk Organizer", source)

    def test_a_non_200_token_response_is_reported_not_swallowed(self):
        import src.adapters.amazon as amazon_mod

        class FakeClient:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def post(self, *a, **kw):
                class R:
                    status_code = 401
                return R()

        real = amazon_mod.httpx.Client
        amazon_mod.httpx.Client = lambda *a, **kw: FakeClient()
        try:
            rows = self.adapter.list_listings(
                db=None, user_id=None,
                credentials={"client_id": "x", "refresh_token": "y"},
            )
        finally:
            amazon_mod.httpx.Client = real

        self.assertEqual(rows, [])
        self.assertIn("401", str(self.adapter.last_error or ""))


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
