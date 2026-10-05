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
            400, {"errors": [{"message": "nope"}]})
        with self.assertRaises(RuntimeError) as caught:
            self.adapter.publish_offer(self.token, "EBAY_CA", "9")
        self.assertIn("HTTP 400", str(caught.exception))

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
