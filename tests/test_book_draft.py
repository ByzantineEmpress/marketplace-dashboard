"""Creating a book draft on eBay.

An unpublished offer IS an eBay draft: it lands in Seller Hub, editable and
publishable, and nothing goes live. That is what makes this testable at all.

Two traps are pinned here because each cost a round of diagnosis:

* the Inventory API requires a Content-Language header, and its absence is
  reported as "errorId 25709 Invalid value for header Content-Language" -- which
  reads like a permissions problem and is not one;
* an offer is accepted WITHOUT business policies, which matters because this
  seller account is not eligible for them at all ("User is not eligible for
  Business Policy"). Requiring the container would break the only working path.
"""

import unittest

from tests import _env  # noqa: E402,F401  isort:skip  (must precede src imports)

from src.adapters import get_adapter


class _Resp:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text

    def json(self):
        return self._payload


class BookDraftTest(unittest.TestCase):
    def setUp(self):
        import src.adapters.ebay as ebay_mod

        self.mod = ebay_mod
        self.adapter = get_adapter("ebay")
        self.calls = []
        self._put, self._post = ebay_mod.httpx.put, ebay_mod.httpx.post
        self._get = ebay_mod.httpx.get

        # A token and a resolved marketplace, without touching the network.
        self.adapter.get_token = lambda *a, **k: {"access_token": "tok"}
        self.adapter._resolve_marketplace = lambda *a, **k: "EBAY_CA"
        self._real_store = None

    def tearDown(self):
        self.mod.httpx.put, self.mod.httpx.post = self._put, self._post
        self.mod.httpx.get = self._get

    def _serve(self, put_status=204, offer_status=201, offer_id="123",
               locations=None):
        if locations is None:
            locations = [{"merchantLocationKey": "FrederictonHome",
                          "merchantLocationStatus": "ENABLED"}]

        def fake_get(url, **kwargs):
            self.calls.append(("GET", url, kwargs))
            if "/location" in url:
                return _Resp(200, {"locations": locations})
            return _Resp(200, {})

        def fake_put(url, **kwargs):
            self.calls.append(("PUT", url, kwargs))
            return _Resp(put_status, {}, "item rejected")

        def fake_post(url, **kwargs):
            self.calls.append(("POST", url, kwargs))
            return _Resp(offer_status, {"offerId": offer_id} if offer_id else {},
                         "offer rejected")

        self.mod.httpx.get = fake_get
        self.mod.httpx.put, self.mod.httpx.post = fake_put, fake_post

    DRAFT = {
        "title": "The Stone Flower", "description": "Author: Alan Scholefield",
        "price": 24.99, "currency": "CAD",
        "images": ["https://example.test/cover.jpg"],
        "aspects": {"Author": ["Alan Scholefield"]},
    }

    def test_the_header_that_the_inventory_api_requires_is_sent(self):
        self._serve()
        self.adapter.create_book_draft(None, self.DRAFT, user_id=1)

        # Scoped to the Inventory API on purpose: Content-Language is its
        # requirement, and the Taxonomy reads that resolve the category have no
        # business sending it.
        inventory = [c for c in self.calls
                     if "/sell/inventory/" in str(c[1]) and c[0] in ("PUT", "POST")]
        self.assertTrue(inventory, "no inventory calls were made")
        for method, url, kwargs in inventory:
            headers = kwargs.get("headers") or {}
            self.assertEqual(headers.get("Content-Language"), "en-CA",
                             f"{method} {url} went out without Content-Language")

    def test_the_weight_and_dimensions_are_what_was_specified(self):
        self._serve()
        self.adapter.create_book_draft(None, self.DRAFT, user_id=1)

        put = [c for c in self.calls if c[0] == "PUT"][0]
        package = put[2]["json"]["packageWeightAndSize"]
        self.assertEqual(package["weight"], {"value": 1.0, "unit": "KILOGRAM"})
        self.assertEqual(package["dimensions"],
                         {"length": 25, "width": 25, "height": 10,
                          "unit": "CENTIMETER"})
        # packageType was rejected as an invalid enum; it must not creep back.
        self.assertNotIn("packageType", package)

    def test_no_business_policies_are_sent_when_the_seller_has_none(self):
        self._serve()
        self.adapter.create_book_draft(None, self.DRAFT, user_id=1)

        post = [c for c in self.calls if c[0] == "POST"][0]
        self.assertNotIn("listingPolicies", post[2]["json"],
                         "an offer with empty policies is rejected; omit it")

    def test_business_policies_are_sent_when_they_are_supplied(self):
        self._serve()
        draft = dict(self.DRAFT, policies={"fulfillment_policy_id": "F1",
                                           "payment_policy_id": "P1",
                                           "return_policy_id": "R1"})
        self.adapter.create_book_draft(None, draft, user_id=1)

        post = [c for c in self.calls if c[0] == "POST"][0]
        policies = post[2]["json"]["listingPolicies"]
        self.assertEqual(policies["fulfillmentPolicyId"], "F1")

    def test_it_returns_a_draft_that_is_not_live(self):
        self._serve()
        result = self.adapter.create_book_draft(None, self.DRAFT, user_id=1)

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "UNPUBLISHED")
        self.assertEqual(result["offer_id"], "123")
        self.assertIn("sh/lst/drafts", result["draft_url"])

    def test_no_us_parent_category_is_used(self):
        """267 is the US Books PARENT and is not a leaf. eBay accepts an offer
        built on it -- 201, no complaint -- and then never surfaces it, so the
        draft existed over the API and nowhere in Seller Hub."""
        self._serve()
        self.adapter.create_book_draft(None, self.DRAFT, user_id=1)
        post = [c for c in self.calls if c[0] == "POST"][0]
        self.assertNotEqual(post[2]["json"]["categoryId"], "267")
        self.assertTrue(post[2]["json"]["categoryId"])

    def test_a_leaf_from_the_taxonomy_is_preferred(self):
        self._serve()
        self.adapter.suggest_categories = lambda *a, **k: [
            {"id": "261186", "name": "Books", "path": "Books & Magazines > Books"},
        ]
        self.adapter.category_aspects = lambda *a, **k: {"aspects": []}
        self.adapter.create_book_draft(None, self.DRAFT, user_id=1)
        post = [c for c in self.calls if c[0] == "POST"][0]
        self.assertEqual(post[2]["json"]["categoryId"], "261186")

    def test_a_caller_supplied_category_wins(self):
        self._serve()
        self.adapter.create_book_draft(
            None, dict(self.DRAFT, category_id="29223"), user_id=1)
        post = [c for c in self.calls if c[0] == "POST"][0]
        self.assertEqual(post[2]["json"]["categoryId"], "29223")

    def test_the_sellers_location_is_attached_to_the_offer(self):
        """An offer with no location is incomplete: it existed over the API but
        never showed in Seller Hub's drafts, so the seller saw nothing created."""
        self._serve()
        self.adapter.create_book_draft(None, self.DRAFT, user_id=1)

        post = [c for c in self.calls if c[0] == "POST"][0]
        self.assertEqual(post[2]["json"]["merchantLocationKey"], "FrederictonHome")

    def test_only_an_enabled_location_is_used(self):
        self._serve(locations=[
            {"merchantLocationKey": "Disabled", "merchantLocationStatus": "DISABLED"},
            {"merchantLocationKey": "Live", "merchantLocationStatus": "ENABLED"},
        ])
        self.adapter.create_book_draft(None, self.DRAFT, user_id=1)
        post = [c for c in self.calls if c[0] == "POST"][0]
        self.assertEqual(post[2]["json"]["merchantLocationKey"], "Live")

    def test_an_account_with_no_location_still_creates_a_draft(self):
        """Better an incomplete draft than a failure: the seller can add a location
        in eBay, and refusing outright would block the whole page."""
        self._serve(locations=[])
        result = self.adapter.create_book_draft(None, self.DRAFT, user_id=1)
        self.assertTrue(result["ok"])
        post = [c for c in self.calls if c[0] == "POST"][0]
        self.assertNotIn("merchantLocationKey", post[2]["json"])

    def test_a_location_lookup_failure_does_not_stop_the_draft(self):
        self._serve()
        self.mod.httpx.get = lambda url, **k: _Resp(500, {}, "boom")
        result = self.adapter.create_book_draft(None, self.DRAFT, user_id=1)
        self.assertTrue(result["ok"])

    def test_the_item_is_created_before_the_offer(self):
        """The offer cannot exist without its inventory item. The location lookup
        in between is a read and does not matter to the ordering."""
        self._serve()
        self.adapter.create_book_draft(None, self.DRAFT, user_id=1)
        mutations = [c[0] for c in self.calls if c[0] in ("PUT", "POST")]
        self.assertEqual(mutations, ["PUT", "POST"])

    def test_a_rejected_item_stops_before_the_offer(self):
        self._serve(put_status=400)
        with self.assertRaises(RuntimeError) as caught:
            self.adapter.create_book_draft(None, self.DRAFT, user_id=1)
        self.assertIn("HTTP 400", str(caught.exception))
        self.assertEqual([c[0] for c in self.calls], ["PUT"],
                         "an offer must not be attempted after a failed item")

    def test_a_missing_offer_id_is_reported_not_ignored(self):
        self._serve(offer_id=None)
        with self.assertRaises(RuntimeError):
            self.adapter.create_book_draft(None, self.DRAFT, user_id=1)

    def test_an_over_long_title_is_cut_rather_than_left_to_ebay(self):
        self._serve()
        long_title = "A" * 200
        self.adapter.create_book_draft(None, dict(self.DRAFT, title=long_title),
                                       user_id=1)
        put = [c for c in self.calls if c[0] == "PUT"][0]
        self.assertEqual(len(put[2]["json"]["product"]["title"]), 80)


if __name__ == "__main__":
    unittest.main(verbosity=2)
