"""The drafts page and its endpoints.

eBay's Seller Hub drafts list does not show offers created through the Inventory
API, so this page is the only place a draft can be seen before it is published.
That makes two things worth pinning:

  * publishing is irreversible from here and must not happen without confirmation;
  * a failed fetch must say so, because "Loading your drafts..." forever is exactly
    how the settings page hid a failure for a whole deploy.
"""

import secrets
import unittest
from datetime import datetime, timedelta

from tests import _env  # noqa: E402,F401  isort:skip  (must precede src imports)

from fastapi.testclient import TestClient

from src.api.main import app
from src.database import SessionLocal, init_db
from src.models import AuthSession, User


class DraftsPageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()

    def setUp(self):
        self.db = SessionLocal()
        self.user = User(email=f"drafts.{secrets.token_hex(4)}@example.com",
                         name="Drafts Tester", provider="google", is_admin=False)
        self.db.add(self.user)
        self.db.commit()

        token = secrets.token_urlsafe(48)
        self.db.add(AuthSession(token=token, user_id=self.user.id,
                                expires_at=datetime.utcnow() + timedelta(hours=1)))
        self.db.commit()
        self._token = token

    def tearDown(self):
        uid = self.user.id
        self.db.rollback()
        self.db.query(AuthSession).filter(AuthSession.user_id == uid).delete(
            synchronize_session=False)
        self.db.query(User).filter(User.id == uid).delete(synchronize_session=False)
        self.db.commit()
        self.db.close()

    def _client(self):
        client = TestClient(app)
        client.cookies.set("auth_token", self._token)
        return client

    def test_the_page_renders(self):
        with self._client() as client:
            res = client.get("/books/drafts")
        self.assertEqual(res.status_code, 200)
        self.assertIn("Book Drafts", res.text)
        self.assertIn("bd-drafts", res.text)
        self.assertIn("book-drafts.js", res.text)

    def test_it_needs_a_session(self):
        with TestClient(app) as client:
            res = client.get("/books/drafts", follow_redirects=False)
        self.assertIn(res.status_code, (302, 307))

    def test_publishing_without_confirmation_is_refused(self):
        """This is the irreversible one: it puts the item on sale."""
        with self._client() as client:
            res = client.post("/api/books/drafts/12345/publish", json={})
        self.assertEqual(res.status_code, 400)
        self.assertIn("live eBay listing", res.json()["error"])

    def test_listing_drafts_without_an_ebay_account_says_so(self):
        with self._client() as client:
            res = client.get("/api/books/drafts")
        # This test database has no eBay account connected for the caller.
        data = res.json()
        self.assertIn("drafts", data)
        if not data.get("ok"):
            self.assertIn("eBay", data["error"])


class DraftAdapterTest(unittest.TestCase):
    """The adapter's draft handling, without touching the network."""

    def setUp(self):
        import src.adapters.ebay as ebay_mod
        from src.adapters import get_adapter

        self.mod = ebay_mod
        self.adapter = get_adapter("ebay")
        self._get, self._post, self._delete = (
            ebay_mod.httpx.get, ebay_mod.httpx.post, ebay_mod.httpx.delete)
        self.token = {"access_token": "tok"}

    def tearDown(self):
        self.mod.httpx.get = self._get
        self.mod.httpx.post = self._post
        self.mod.httpx.delete = self._delete

    class _Resp:
        def __init__(self, status_code=200, payload=None, content=b"x"):
            self.status_code = status_code
            self._payload = payload if payload is not None else {}
            self.content = content
            self.text = ""

        def json(self):
            return self._payload

    def test_a_published_offer_is_not_listed_as_a_draft(self):
        """It kept showing listings that had already gone live, with a Publish
        button on something already for sale."""
        def fake_get(url, **kwargs):
            if "/inventory_item" in url:
                return self._Resp(200, {"inventoryItems": [
                    {"sku": "BOOK-1", "product": {"title": "A Book"}}]})
            if "/offer/" in url:
                return self._Resp(200, {"offerId": "9", "status": "PUBLISHED",
                                        "pricingSummary": {"price": {
                                            "value": "12.34", "currency": "CAD"}}})
            return self._Resp(200, {"offers": [{"offerId": "9"}]})

        self.mod.httpx.get = fake_get
        self.assertEqual(self.adapter.list_book_drafts(self.token, "EBAY_CA"), [])

    def test_an_unpublished_offer_is_listed(self):
        def fake_get(url, **kwargs):
            if "/inventory_item" in url:
                return self._Resp(200, {"inventoryItems": [
                    {"sku": "BOOK-1", "product": {"title": "A Book"}}]})
            if "/offer/" in url:
                return self._Resp(200, {"offerId": "9", "status": "UNPUBLISHED",
                                        "pricingSummary": {"price": {
                                            "value": "12.34", "currency": "CAD"}}})
            return self._Resp(200, {"offers": [{"offerId": "9"}]})

        self.mod.httpx.get = fake_get
        drafts = self.adapter.list_book_drafts(self.token, "EBAY_CA")
        self.assertEqual([d["offer_id"] for d in drafts], ["9"])

    def test_editing_the_package_merges_it_onto_the_item(self):
        """Weight and dimensions are priced into a calculated-shipping quote, so a
        wrong weight quotes the buyer the wrong postage. They live on the inventory
        item, and PUT on an item replaces it -- so everything else must survive."""
        puts = []

        def fake_get(url, **kwargs):
            if "/inventory_item/" in url:
                return self._Resp(200, {
                    "condition": "USED_GOOD",
                    "packageWeightAndSize": {
                        "weight": {"value": 1.0, "unit": "KILOGRAM"},
                        "dimensions": {"length": 25, "width": 25, "height": 10,
                                       "unit": "CENTIMETER"},
                    },
                    "product": {"title": "A Book", "aspects": {"Author": ["X"]}},
                })
            return self._Resp(200, {"offerId": "9", "sku": "BOOK-1",
                                    "availableQuantity": 1})

        def fake_put(url, **kwargs):
            puts.append((url, kwargs.get("json") or {}))
            return self._Resp(204)

        self.mod.httpx.get = fake_get
        self.mod.httpx.put = fake_put
        self.adapter.update_draft(self.token, "EBAY_CA", "9", "BOOK-1",
                                  {"weight_kg": 2.5, "length_cm": 30})

        item = [body for url, body in puts if "inventory_item" in url][0]
        package = item["packageWeightAndSize"]
        self.assertEqual(package["weight"]["value"], 2.5)
        self.assertEqual(package["weight"]["unit"], "KILOGRAM")
        self.assertEqual(package["dimensions"]["length"], 30)
        # The dimensions not being edited keep their values rather than resetting.
        self.assertEqual(package["dimensions"]["width"], 25)
        self.assertEqual(package["dimensions"]["height"], 10)
        self.assertEqual(package["dimensions"]["unit"], "CENTIMETER")
        # And the rest of the item survives.
        self.assertEqual(item["product"]["title"], "A Book")
        self.assertEqual(item["product"]["aspects"], {"Author": ["X"]})
        # The offer is untouched by a package-only edit.
        self.assertEqual([u for u, _ in puts if "offer/" in u], [])

    def test_a_nonsense_package_value_falls_back_rather_than_zeroing(self):
        """A blank or negative weight would make the listing unshippable, so it
        falls back to the book default instead of being sent as-is."""
        puts = []

        def fake_get(url, **kwargs):
            if "/inventory_item/" in url:
                return self._Resp(200, {"product": {"title": "A Book"}})
            return self._Resp(200, {"offerId": "9", "sku": "BOOK-1"})

        def fake_put(url, **kwargs):
            puts.append((url, kwargs.get("json") or {}))
            return self._Resp(204)

        self.mod.httpx.get = fake_get
        self.mod.httpx.put = fake_put
        self.adapter.update_draft(self.token, "EBAY_CA", "9", "BOOK-1",
                                  {"weight_kg": "", "length_cm": -5})

        item = [body for url, body in puts if "inventory_item" in url][0]
        package = item["packageWeightAndSize"]
        self.assertEqual(package["weight"]["value"], 1.0)     # the book default
        self.assertEqual(package["dimensions"]["length"], 25)

    def test_the_package_is_returned_for_the_editor(self):
        def fake_get(url, **kwargs):
            if "/inventory_item/" in url:
                return self._Resp(200, {
                    "packageWeightAndSize": {
                        "weight": {"value": 3.0, "unit": "KILOGRAM"},
                        "dimensions": {"length": 40, "width": 20, "height": 5,
                                       "unit": "CENTIMETER"},
                    },
                    "product": {"title": "A Book"},
                })
            return self._Resp(200, {"offerId": "9", "sku": "BOOK-1"})

        self.mod.httpx.get = fake_get
        draft = self.adapter.get_draft(self.token, "EBAY_CA", "9")
        self.assertEqual(draft["weight_kg"], 3.0)
        self.assertEqual(draft["length_cm"], 40)
        self.assertEqual(draft["width_cm"], 20)
        self.assertEqual(draft["height_cm"], 5)

    def test_only_unpublished_offers_come_back(self):
        def fake_get(url, **kwargs):
            if "/inventory_item" in url:
                return self._Resp(200, {"inventoryItems": [
                    {"sku": "BOOK-1", "product": {"title": "A Book"}}]})
            if "/offer/" in url:
                return self._Resp(200, {"offerId": "9", "status": "UNPUBLISHED",
                                        "categoryId": "261186",
                                        "pricingSummary": {"price": {
                                            "value": "12.34", "currency": "CAD"}}})
            return self._Resp(200, {"offers": [{"offerId": "9"}]})

        self.mod.httpx.get = fake_get
        drafts = self.adapter.list_book_drafts(self.token, "EBAY_CA")
        self.assertEqual(len(drafts), 1)
        self.assertEqual(drafts[0]["offer_id"], "9")
        self.assertEqual(drafts[0]["title"], "A Book")
        self.assertEqual(drafts[0]["price"], "12.34")
        self.assertEqual(drafts[0]["category_id"], "261186")

    def test_a_failed_publish_raises_with_ebays_own_words(self):
        self.mod.httpx.post = lambda url, **k: self._Resp(
            400, {"errors": [{"errorId": 99999, "message": "nope"}]})
        with self.assertRaises(RuntimeError) as caught:
            self.adapter.publish_offer(self.token, "EBAY_CA", "9")
        self.assertIn("HTTP 400", str(caught.exception))

    def test_the_known_publish_errors_say_what_to_do(self):
        """eBay answers with an errorId and a category and leaves the seller to
        work it out. The two that have actually blocked this seller now say what
        to do about them."""
        def refusing(error_id, message):
            self.mod.httpx.post = lambda url, **k: self._Resp(
                400, {"errors": [{"errorId": error_id, "message": message}]})
            with self.assertRaises(RuntimeError) as caught:
                self.adapter.publish_offer(self.token, "EBAY_CA", "9")
            return str(caught.exception)

        no_photo = refusing(25002, "Add at least 1 photo.")
        self.assertIn("Add at least 1 photo", no_photo)  # eBay's words, kept verbatim
        self.assertIn("item specific", no_photo.lower())

        # 25002 must NOT be paraphrased into a specific cause. It reported "add a
        # photo" on one publish and "Language is missing" on the next, on the same
        # draft, so a hardcoded reading sends the seller after the wrong thing.
        language = refusing(25002, "The item specific Language is missing.")
        self.assertIn("Language is missing", language)
        self.assertNotIn("no photo", language.lower())

        no_shipping = refusing(25007, "invalid data in the Fulfillment policy")
        self.assertIn("shipping", no_shipping.lower())
        self.assertIn("Business Policies", no_shipping)

    def test_an_untranslated_publish_error_still_carries_the_raw_text(self):
        """Anything not worth paraphrasing must not be swallowed."""
        self.mod.httpx.post = lambda url, **k: self._Resp(
            400, {"errors": [{"errorId": 12345, "message": "something new"}]})
        with self.assertRaises(RuntimeError) as caught:
            self.adapter.publish_offer(self.token, "EBAY_CA", "9")
        self.assertIn("12345", str(caught.exception))
        self.assertIn("something new", str(caught.exception))

    def test_publish_returns_the_listing_id_and_url(self):
        self.mod.httpx.post = lambda url, **k: self._Resp(
            200, {"listingId": "1234567890"})
        result = self.adapter.publish_offer(self.token, "EBAY_CA", "9")
        self.assertEqual(result["listing_id"], "1234567890")
        self.assertIn("1234567890", result["url"])

    def test_delete_removes_the_item_only_after_the_offer(self):
        order = []

        def fake_delete(url, **kwargs):
            order.append("item" if "inventory_item" in url else "offer")
            return self._Resp(204)

        self.mod.httpx.delete = fake_delete
        self.adapter.delete_draft(self.token, "EBAY_CA", "9", sku="BOOK-1")
        self.assertEqual(order, ["offer", "item"],
                         "an item cannot be withdrawn while an offer references it")

    def test_a_failed_item_delete_still_counts_as_deleted(self):
        """The offer is gone, which is what was asked. A stray inventory item is
        harmless and must not make the delete look failed."""
        def fake_delete(url, **kwargs):
            if "inventory_item" in url:
                raise RuntimeError("boom")
            return self._Resp(204)

        self.mod.httpx.delete = fake_delete
        self.assertTrue(self.adapter.delete_draft(self.token, "EBAY_CA", "9",
                                                  sku="BOOK-1"))


    def test_photos_are_merged_without_wiping_the_item(self):
        """Photos live on the inventory ITEM and PUT on an item REPLACES it, so the
        title and aspects must survive the merge. This is the same trap that emptied
        a draft's price and description when the merchant location was attached."""
        sent = {}

        def fake_get(url, **kwargs):
            return self._Resp(200, {
                "sku": "BOOK-1", "condition": "USED_GOOD",
                "product": {"title": "A Book", "description": "Author: Someone",
                            "aspects": {"Author": ["Someone"]}},
            })

        def fake_put(url, **kwargs):
            sent.update(kwargs.get("json") or {})
            return self._Resp(204)

        self.mod.httpx.get = fake_get
        self.mod.httpx.put = fake_put
        stored = self.adapter.set_draft_images(
            self.token, "EBAY_CA", "BOOK-1", ["/static/uploads/a.jpg"])

        product = sent.get("product") or {}
        self.assertEqual(product.get("title"), "A Book")
        self.assertEqual(product.get("aspects"), {"Author": ["Someone"]})
        self.assertEqual(len(stored), 1)
        self.assertTrue(stored[0].startswith("http"), stored[0])
        # Read-only fields eBay returns but will not accept back.
        self.assertNotIn("sku", sent)

    def test_adding_photos_to_an_unreadable_draft_raises(self):
        self.mod.httpx.get = lambda url, **k: self._Resp(404, {}, "nope")
        with self.assertRaises(RuntimeError):
            self.adapter.set_draft_images(self.token, "EBAY_CA", "BOOK-1",
                                          ["/static/uploads/a.jpg"])

    def test_saving_merges_into_both_objects_without_wiping_them(self):
        """A draft is TWO objects and both PUTs replace: the item carries the title,
        description and photos, the offer carries the price, quantity and category.
        Sending only the changed fields would wipe the rest of that object, which is
        how an earlier partial update emptied a draft's price and description."""
        puts = []

        def fake_get(url, **kwargs):
            if "/inventory_item/" in url:
                return self._Resp(200, {
                    "condition": "USED_GOOD",
                    "packageWeightAndSize": {"weight": {"value": 1.0,
                                                        "unit": "KILOGRAM"}},
                    "product": {"title": "Old Title", "description": "old",
                                "aspects": {"Author": ["Someone"]}},
                })
            return self._Resp(200, {
                "offerId": "9", "sku": "BOOK-1", "status": "UNPUBLISHED",
                "format": "FIXED_PRICE", "availableQuantity": 1,
                "categoryId": "261186", "merchantLocationKey": "Home",
                "listingPolicies": {"eBayPlusIfEligible": False},
                "pricingSummary": {"price": {"value": "5.00", "currency": "CAD"}},
                "listingDescription": "old",
            })

        def fake_put(url, **kwargs):
            puts.append(("item" if "inventory_item" in url else "offer",
                         kwargs.get("json") or {}))
            return self._Resp(204)

        self.mod.httpx.get = fake_get
        self.mod.httpx.put = fake_put
        self.adapter.update_draft(self.token, "EBAY_CA", "9", "BOOK-1",
                                  {"title": "New Title", "price": 12.5})

        sent = dict(puts)
        item = sent["item"]
        # The title changed and the aspects on the same object survived.
        self.assertEqual(item["product"]["title"], "New Title")
        self.assertEqual(item["product"]["aspects"], {"Author": ["Someone"]})
        self.assertEqual(item["condition"], "USED_GOOD")
        self.assertNotIn("sku", item)

        offer = sent["offer"]
        # The price changed and the offer's other fields survived.
        self.assertEqual(offer["pricingSummary"]["price"]["value"], "12.50")
        self.assertEqual(offer["categoryId"], "261186")
        self.assertEqual(offer["merchantLocationKey"], "Home")
        self.assertEqual(offer["availableQuantity"], 1)
        self.assertNotIn("offerId", offer)
        self.assertNotIn("status", offer)

    def test_editing_only_the_price_does_not_touch_the_item(self):
        """Anything the form does not show must survive untouched."""
        puts = []

        def fake_get(url, **kwargs):
            return self._Resp(200, {
                "offerId": "9", "sku": "BOOK-1", "availableQuantity": 3,
                "pricingSummary": {"price": {"value": "5.00", "currency": "CAD"}},
            })

        def fake_put(url, **kwargs):
            puts.append("item" if "inventory_item" in url else "offer")
            return self._Resp(204)

        self.mod.httpx.get = fake_get
        self.mod.httpx.put = fake_put
        self.adapter.update_draft(self.token, "EBAY_CA", "9", "BOOK-1",
                                  {"price": 9.99})
        self.assertEqual(puts, ["offer"])

    def test_a_description_edit_reaches_both_objects(self):
        """The offer carries the listing description and the item the catalogue one.
        They are set to the same text, so an edit has to reach both or the page and
        eBay would disagree."""
        puts = []

        def fake_get(url, **kwargs):
            if "/inventory_item/" in url:
                return self._Resp(200, {"product": {"title": "T"}})
            return self._Resp(200, {"offerId": "9", "sku": "BOOK-1"})

        def fake_put(url, **kwargs):
            puts.append(("item" if "inventory_item" in url else "offer",
                         kwargs.get("json") or {}))
            return self._Resp(204)

        self.mod.httpx.get = fake_get
        self.mod.httpx.put = fake_put
        self.adapter.update_draft(self.token, "EBAY_CA", "9", "BOOK-1",
                                  {"description": "new text"})

        sent = dict(puts)
        self.assertEqual(sent["item"]["product"]["description"], "new text")
        self.assertEqual(sent["offer"]["listingDescription"], "new text")

    def test_a_rejected_save_raises_rather_than_looking_ok(self):
        def fake_get(url, **kwargs):
            return self._Resp(200, {"offerId": "9", "sku": "BOOK-1"})

        self.mod.httpx.get = fake_get
        self.mod.httpx.put = lambda url, **k: self._Resp(400, {}, "no")
        with self.assertRaises(RuntimeError):
            self.adapter.update_draft(self.token, "EBAY_CA", "9", "BOOK-1",
                                      {"price": 1})

    def test_an_empty_photo_list_resolves_to_nothing(self):
        """eBay requires at least one photo to publish, so an empty list has to
        come out empty rather than as a list containing a blank URL."""
        self.assertEqual(self.adapter.absolute_image_urls([]), [])
        self.assertEqual(self.adapter.absolute_image_urls([""]), [])
        self.assertEqual(self.adapter.absolute_image_urls([None]), [])
        self.assertEqual(self.adapter.absolute_image_urls(["   "]), [])


class DraftPhotoRouteTest(unittest.TestCase):
    def test_it_needs_a_session(self):
        from fastapi.testclient import TestClient
        from src.api.main import app

        with TestClient(app) as client:
            res = client.post("/api/books/drafts/1/photos",
                              json={"sku": "x", "images": ["/a.jpg"]})
        self.assertIn(res.status_code, (401, 403))

    def test_no_photos_is_refused(self):
        import secrets
        from datetime import datetime, timedelta

        from fastapi.testclient import TestClient

        from src.database import SessionLocal, init_db
        from src.models import AuthSession, User

        init_db()
        db = SessionLocal()
        user = User(email=f"ph.{secrets.token_hex(4)}@example.com", name="P",
                    provider="google", is_admin=False)
        db.add(user)
        db.commit()
        token = secrets.token_urlsafe(48)
        db.add(AuthSession(token=token, user_id=user.id,
                           expires_at=datetime.utcnow() + timedelta(hours=1)))
        db.commit()
        uid = user.id
        db.close()

        try:
            client = TestClient(app)
            client.cookies.set("auth_token", token)
            res = client.post("/api/books/drafts/1/photos", json={"sku": "x"})
            self.assertEqual(res.status_code, 400)
            self.assertIn("No photos", res.json()["error"])
        finally:
            db = SessionLocal()
            db.query(AuthSession).filter(AuthSession.user_id == uid).delete(
                synchronize_session=False)
            db.query(User).filter(User.id == uid).delete(synchronize_session=False)
            db.commit()
            db.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
