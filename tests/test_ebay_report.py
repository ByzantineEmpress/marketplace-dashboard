"""eBay listing sync via the Sell Feed Active Inventory Report.

eBay has no simple REST endpoint for a seller's active listings, so the adapter
downloads the Active Inventory Report: create a task, poll, download a zipped
XML. The report gives ItemID, price, currency and quantity — not titles or
photos — so titles are generated and images left empty.
"""

import io
import unittest
import zipfile

from tests import _env  # noqa: E402,F401  isort:skip  (must precede src imports)

from src.adapters import get_adapter


class EbayReportParserTest(unittest.TestCase):
    def setUp(self):
        self.adapter = get_adapter("ebay")

    def _zip_xml(self, xml: str) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("activeinventory.xml", xml)
        return buf.getvalue()

    XML = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<BulkDataExchangeResponses xmlns="urn:ebay:apis:eBLBaseComponents">'
        '<ActiveInventoryReport>'
        '<SKUDetails><Price currencyID="CAD">60.0</Price><Quantity>1</Quantity>'
        '<ItemID>800657056078</ItemID></SKUDetails>'
        '<SKUDetails><Price currencyID="CAD">129.99</Price><Quantity>3</Quantity>'
        '<ItemID>800657078639</ItemID></SKUDetails>'
        '</ActiveInventoryReport></BulkDataExchangeResponses>'
    )

    def test_parses_price_currency_quantity_and_generates_title(self):
        rows = self.adapter._parse_active_inventory_report(self._zip_xml(self.XML))
        self.assertEqual(len(rows), 2)

        first = rows[0]
        self.assertEqual(first["platform_listing_id"], "800657056078")
        self.assertEqual(first["price_cents"], 6000)
        self.assertEqual(first["currency"], "CAD")
        self.assertEqual(first["available_quantity"], 1)
        self.assertIn("800657056078", first["title"])
        self.assertEqual(first["original_url"], "https://www.ebay.com/itm/800657056078")
        self.assertEqual(first["status"], "active")
        self.assertEqual(first["image_url"], "")
        self.assertEqual(first["platform"], "ebay")

    def test_garbage_input_yields_nothing(self):
        self.assertEqual(self.adapter._parse_active_inventory_report(b"not a zip"), [])

    def test_it_uses_models_columns_only(self):
        from src.models import Listing

        columns = {c.name for c in Listing.__table__.columns}
        rows = self.adapter._parse_active_inventory_report(self._zip_xml(self.XML))
        for row in rows:
            unknown = [k for k in row if k not in columns]
            self.assertEqual(unknown, [], f"not Listing columns: {unknown}")


class EbayReportFetchTest(unittest.TestCase):
    """The async create → poll → download flow, with a stubbed HTTP layer."""

    def setUp(self):
        self.adapter = get_adapter("ebay")

    def _report_bytes(self):
        xml = (
            '<?xml version="1.0"?>'
            '<BulkDataExchangeResponses xmlns="urn:ebay:apis:eBLBaseComponents">'
            '<ActiveInventoryReport>'
            '<SKUDetails><Price currencyID="CAD">50.0</Price><Quantity>1</Quantity>'
            '<ItemID>123</ItemID></SKUDetails>'
            '</ActiveInventoryReport></BulkDataExchangeResponses>'
        )
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("r.xml", xml)
        return buf.getvalue()

    def test_fetches_the_report_and_deduplicates(self):
        import src.adapters.ebay as ebay_mod

        report = self._report_bytes()
        created = []

        class CreateResp:
            status_code = 202
            headers = {"location": "https://api.ebay.com/sell/feed/v1/task/task-42"}

        class PollResp:
            status_code = 200
            def json(self):
                return {"status": "COMPLETED"}

        class DownloadResp:
            status_code = 200
            content = report

        def fake_post(url, **kw):
            created.append((url, kw.get("json")))
            return CreateResp()

        def fake_get(url, **kw):
            if "/download_result_file" in url:
                return DownloadResp()
            return PollResp()

        real_post, real_get = ebay_mod.httpx.post, ebay_mod.httpx.get
        ebay_mod.httpx.post, ebay_mod.httpx.get = fake_post, fake_get
        try:
            rows = self.adapter._fetch_active_inventory_report(
                {"Authorization": "Bearer t"}, 500, "EBAY_CA")
        finally:
            ebay_mod.httpx.post, ebay_mod.httpx.get = real_post, real_get
            self.adapter.last_error = None

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["platform_listing_id"], "123")
        self.assertEqual(rows[0]["price_cents"], 5000)
        # Exactly one task, for the requested marketplace.
        self.assertEqual(len(created), 1)
        self.assertIn("inventory_task", created[0][0])
        self.assertEqual(created[0][1]["marketplaceId"], "EBAY_CA")
        self.assertEqual(created[0][1]["feedType"], "LMS_ACTIVE_INVENTORY_REPORT")

    def test_a_failed_task_sets_last_error(self):
        import src.adapters.ebay as ebay_mod

        class CreateResp:
            status_code = 403

        real_post = ebay_mod.httpx.post
        ebay_mod.httpx.post = lambda *a, **kw: CreateResp()
        try:
            rows = self.adapter._fetch_active_inventory_report(
                {"Authorization": "Bearer t"}, 500, "EBAY_CA")
        finally:
            ebay_mod.httpx.post = real_post

        self.assertEqual(rows, [])
        self.assertIsNotNone(self.adapter.last_error)
        self.adapter.last_error = None


class EbayBrowseEnrichmentTest(unittest.TestCase):
    def setUp(self):
        self.adapter = get_adapter("ebay")

    def test_fetch_browse_item_parses_title_image_and_category(self):
        import src.adapters.ebay as ebay_mod

        class FakeResp:
            status_code = 200
            def json(self):
                return {
                    "title": "Vintage Projector",
                    "image": {"imageUrl": "https://i.ebayimg.com/a.jpg"},
                    "additionalImages": [{"imageUrl": "https://i.ebayimg.com/b.jpg"}],
                    "categoryPath": "Cameras|Vintage",
                    "description": "Works.",
                }

        requested = []

        def fake_get(url, **kw):
            requested.append(url)
            return FakeResp()

        real = ebay_mod.httpx.get
        ebay_mod.httpx.get = fake_get
        try:
            detail = self.adapter._fetch_browse_item("800657056078",
                                                     {"Authorization": "Bearer t"})
        finally:
            ebay_mod.httpx.get = real

        self.assertEqual(detail["title"], "Vintage Projector")
        self.assertEqual(detail["image_url"], "https://i.ebayimg.com/a.jpg")
        self.assertEqual(detail["images"],
                         ["https://i.ebayimg.com/a.jpg", "https://i.ebayimg.com/b.jpg"])
        self.assertEqual(detail["category"], "Cameras|Vintage")
        # The Browse item id uses the v1|ItemID|0 form.
        self.assertIn("v1|800657056078|0", requested[0])

    def test_enrich_listings_fills_title_and_image(self):
        import src.adapters.ebay as ebay_mod

        class FakeResp:
            status_code = 200
            def json(self):
                return {"title": "Real Title", "image": {"imageUrl": "https://x/img.jpg"},
                        "additionalImages": [], "categoryPath": "Cat", "description": ""}

        def fake_get(url, **kw):
            return FakeResp()

        real = ebay_mod.httpx.get
        ebay_mod.httpx.get = fake_get
        try:
            rows = self.adapter._enrich_listings(
                [{"platform_listing_id": "123", "title": "eBay listing 123",
                  "price_cents": 100, "currency": "CAD", "image_url": "",
                  "images_json": [], "category": "", "description": "",
                  "platform": "ebay"}],
                {"Authorization": "Bearer t"}, "EBAY_CA", 10)
        finally:
            ebay_mod.httpx.get = real

        self.assertEqual(rows[0]["title"], "Real Title")
        self.assertEqual(rows[0]["image_url"], "https://x/img.jpg")
        self.assertEqual(rows[0]["images_json"], ["https://x/img.jpg"])

    def test_fetch_getitem_parses_watch_count(self):
        import src.adapters.ebay as ebay_mod

        xml = ('<?xml version="1.0"?>'
               '<GetItemResponse xmlns="urn:ebay:apis:eBLBaseComponents">'
               '<Item><WatchCount>7</WatchCount></Item>'
               '</GetItemResponse>')

        class FakeResp:
            status_code = 200
            content = xml.encode()

        requested = []

        def fake_post(url, **kw):
            requested.append((url, kw.get("content")))
            return FakeResp()

        real = ebay_mod.httpx.post
        ebay_mod.httpx.post = fake_post
        try:
            out = self.adapter._fetch_getitem("800657056078", "tok", "EBAY_CA")
        finally:
            ebay_mod.httpx.post = real

        self.assertEqual(out["watchers_count"], 7)
        # IncludeWatchCount must be set, otherwise eBay omits WatchCount.
        self.assertIn("IncludeWatchCount", requested[0][1])
        self.assertIn("800657056078", requested[0][1])


if __name__ == "__main__":
    unittest.main(verbosity=2)
