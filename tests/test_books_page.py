"""The books page and its endpoints.

The interesting parts are the ones that could mislead: an unreadable photo must
say so rather than return a plausible-looking ISBN, and creating a draft has to be
as deliberate as publishing was, because it writes to a real eBay account.
"""

import secrets
import unittest
from datetime import datetime, timedelta

from tests import _env  # noqa: E402,F401  isort:skip  (must precede src imports)

from fastapi.testclient import TestClient

from src import books
from src.api.main import app
from src.database import SessionLocal, init_db
from src.models import AuthSession, Listing, User


class BooksPageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()

    def setUp(self):
        self.db = SessionLocal()
        self.user = User(email=f"books.{secrets.token_hex(4)}@example.com",
                         name="Books Tester", provider="google", is_admin=False)
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
        self.db.query(AuthSession).filter(AuthSession.user_id == uid).delete(synchronize_session=False)
        self.db.query(User).filter(User.id == uid).delete(synchronize_session=False)
        self.db.commit()
        self.db.close()

    def _client(self):
        client = TestClient(app)
        client.cookies.set("auth_token", self._token)
        return client

    def test_the_page_renders(self):
        with self._client() as client:
            res = client.get("/books")
        self.assertEqual(res.status_code, 200)
        self.assertIn("List a Book on eBay", res.text)
        for control in ("bp-file", "bp-isbn", "bp-create-btn", "bp-searches"):
            self.assertIn(control, res.text)

    def test_it_needs_a_session(self):
        with TestClient(app) as client:
            res = client.get("/books", follow_redirects=False)
        self.assertIn(res.status_code, (302, 307))

    def test_the_draft_endpoint_refuses_an_unconfirmed_request(self):
        with self._client() as client:
            res = client.post("/api/books/draft",
                              json={"draft": {"title": "x", "price": 5}})
        self.assertEqual(res.status_code, 400)
        self.assertIn("Not confirmed", res.json()["error"])

    def test_the_draft_endpoint_requires_a_title_and_price(self):
        with self._client() as client:
            no_title = client.post("/api/books/draft",
                                   json={"confirm": True, "draft": {"price": 5}})
            no_price = client.post("/api/books/draft",
                                   json={"confirm": True, "draft": {"title": "x"}})
        self.assertEqual(no_title.status_code, 400)
        self.assertEqual(no_price.status_code, 400)

    def test_exif_orientation_is_baked_into_the_pixels(self):
        """A phone records which way up it was held in EXIF, not in the pixels, so a
        photo that looks upright in the camera app arrives sideways on the listing."""
        import io as _io

        from PIL import Image

        from src.api.routes import _apply_exif_orientation

        # A 4x2 image tagged "rotated 90", which must come back 2x4.
        image = Image.new("RGB", (4, 2), "red")
        exif = image.getexif()
        exif[274] = 6                      # 274 = Orientation, 6 = rotate 270 CW
        buffer = _io.BytesIO()
        image.save(buffer, format="JPEG", exif=exif)

        out = _apply_exif_orientation(buffer.getvalue(), ".jpg")
        with Image.open(_io.BytesIO(out)) as result:
            self.assertEqual(result.size, (2, 4))

    def test_an_image_with_no_orientation_is_left_alone(self):
        """Re-encoding every upload would quietly recompress photos for nothing."""
        import io as _io

        from PIL import Image

        from src.api.routes import _apply_exif_orientation

        image = Image.new("RGB", (4, 2), "red")
        buffer = _io.BytesIO()
        image.save(buffer, format="JPEG")
        original = buffer.getvalue()
        self.assertEqual(_apply_exif_orientation(original, ".jpg"), original)

    def test_something_undecodable_is_passed_through(self):
        """A listing with a sideways photo beats a failed upload."""
        from src.api.routes import _apply_exif_orientation

        self.assertEqual(_apply_exif_orientation(b"not an image", ".jpg"),
                         b"not an image")

    def test_publishing_asks_through_a_styled_dialog(self):
        """A native confirm() cannot be styled, and this is the one action that puts
        something up for sale -- so it gets a dialog that shows the price plainly."""
        with self._client() as client:
            res = client.get("/books")
        self.assertIn("bp-confirm", res.text)
        self.assertIn("bp-confirm-price", res.text)
        self.assertIn("asking price", res.text)
        # It reuses the app's modal, so it inherits the phone bottom-sheet treatment.
        self.assertIn("modal-backdrop", res.text)

    def test_no_native_confirm_on_the_publish_path(self):
        """window.confirm cannot be styled at all, which is the whole complaint."""
        import pathlib
        js = pathlib.Path(__file__).resolve().parent.parent.joinpath(
            "static", "js", "books.js").read_text(encoding="utf-8")
        self.assertNotIn("window.confirm", js)
        self.assertIn("askToPublish", js)
        # The dialog is dismissed by cancel, by the X, by Escape and by tapping away.
        for escape in ('"bp-confirm-cancel"', '"bp-confirm-x"', "Escape",
                       "event.target === backdrop"):
            self.assertIn(escape, js)

    def test_the_price_is_shown_to_two_decimals(self):
        """The price is what is being agreed to, so it is formatted rather than
        echoed back as typed."""
        import pathlib
        js = pathlib.Path(__file__).resolve().parent.parent.joinpath(
            "static", "js", "books.js").read_text(encoding="utf-8")
        self.assertIn("toFixed(2)", js)

    def test_several_photos_can_be_taken_without_leaving_the_camera(self):
        """The camera app returns after every single shot, so a book needing a
        barcode, a cover and a title page means opening it three times."""
        with self._client() as client:
            res = client.get("/books")
        self.assertIn("bp-camera-btn", res.text)
        self.assertIn("pc-overlay", res.text)
        self.assertIn("pc-shutter", res.text)
        self.assertIn("pc-strip", res.text)   # what has been taken so far
        self.assertIn("playsinline", res.text)

        import pathlib
        js = pathlib.Path(__file__).resolve().parent.parent.joinpath(
            "static", "js", "books.js").read_text(encoding="utf-8")
        # Snapping must NOT close the overlay -- that is the entire request.
        snap_body = js.split("function snap(")[1].split("function finishCapture(")[0]
        self.assertNotIn("stopCapture()", snap_body)
        self.assertIn("shots.push(blob)", snap_body)
        # And finishing uploads every shot rather than just the last.
        finish = js.split("function finishCapture(")[1].split("function startCapture(")[0]
        self.assertIn("taken.forEach", finish)

    def test_cancelling_capture_discards_the_shots(self):
        """Leaving the camera must not upload photos taken after deciding against
        them."""
        import pathlib
        js = pathlib.Path(__file__).resolve().parent.parent.joinpath(
            "static", "js", "books.js").read_text(encoding="utf-8")
        cancel = js.split('"pc-cancel")) $("pc-cancel").addEventListener')[1][:220]
        self.assertIn("stopCapture()", cancel)
        self.assertIn("shots = []", cancel)

    def test_the_capture_camera_is_released_when_the_tab_hides(self):
        import pathlib
        js = pathlib.Path(__file__).resolve().parent.parent.joinpath(
            "static", "js", "books.js").read_text(encoding="utf-8")
        self.assertIn("stopScanner(); stopCapture();", js)

    def test_the_page_offers_a_live_scanner(self):
        """The camera-app round trip -- open it, take a photo, keep it, come back --
        is the slowest way to read a barcode that is already in front of the lens, and
        it leaves a junk photo on the phone each time."""
        with self._client() as client:
            res = client.get("/books")
        self.assertIn("bp-scan-btn", res.text)
        self.assertIn("bs-overlay", res.text)
        self.assertIn("bs-shutter", res.text)
        # playsinline is not optional: without it iOS takes the video fullscreen and
        # the overlay's own controls are unreachable.
        self.assertIn("playsinline", res.text)
        # The framing guide matches a barcode's shape rather than being a square.
        self.assertIn("bs-frame", res.text)

    def test_the_scanner_does_not_post_digits_as_an_image(self):
        """BarcodeDetector returns the digits themselves; posting them as a fake
        image file would send the server a text blob to decode as a photo."""
        import pathlib
        js = pathlib.Path(__file__).resolve().parent.parent.joinpath(
            "static", "js", "books.js").read_text(encoding="utf-8")
        self.assertIn("BarcodeDetector", js)
        self.assertNotIn('new Blob([codes[0].rawValue])', js)
        # The digits go to the lookup, which validates the check digit.
        self.assertIn("lookup(codes[0].rawValue)", js)

    def test_the_camera_is_torn_down_when_the_overlay_closes(self):
        """Otherwise the camera light stays on after the scanner is dismissed."""
        import pathlib
        js = pathlib.Path(__file__).resolve().parent.parent.joinpath(
            "static", "js", "books.js").read_text(encoding="utf-8")
        self.assertIn("getTracks().forEach", js)
        self.assertIn("track.stop()", js)
        self.assertIn("visibilitychange", js)

    def test_publishing_without_confirmation_is_refused(self):
        """Publish now is the irreversible one: it puts the book on sale."""
        with self._client() as client:
            res = client.post("/api/books/publish",
                              json={"draft": {"title": "x", "price": 5}})
        self.assertEqual(res.status_code, 400)
        self.assertIn("live eBay listing", res.json()["error"])

    def test_publishing_needs_a_title_and_a_price(self):
        with self._client() as client:
            no_title = client.post("/api/books/publish",
                                   json={"confirm": True, "draft": {"price": 5}})
            no_price = client.post("/api/books/publish",
                                   json={"confirm": True, "draft": {"title": "x"}})
        self.assertEqual(no_title.status_code, 400)
        self.assertEqual(no_price.status_code, 400)

    def test_the_page_offers_publishing_separately_from_drafting(self):
        """One button that sometimes drafts and sometimes publishes would be the
        worst of both, so the two are distinct controls."""
        with self._client() as client:
            res = client.get("/books")
        self.assertIn("bp-create-btn", res.text)
        self.assertIn("bp-publish-btn", res.text)
        self.assertIn("goes live", res.text)

    def test_the_lookup_reports_a_bad_isbn_rather_than_guessing(self):
        with self._client() as client:
            res = client.get("/api/books/lookup?isbn=9780241108259")
        self.assertEqual(res.status_code, 400)
        self.assertIn("check digit", res.json()["error"])

    def test_a_scan_with_no_file_is_refused(self):
        with self._client() as client:
            res = client.post("/api/books/scan")
        self.assertEqual(res.status_code, 422)  # missing required file


class DeletionNotificationTest(unittest.TestCase):
    """eBay sends account-deletion notifications for every account that has
    authorised the app, and the endpoint is reachable by anyone who knows the URL --
    eBay does not sign the POST. Storing them all put 16,879 rows in the database,
    not one of which concerned a user of this instance."""

    def test_an_unmatched_notification_is_not_stored(self):
        import pathlib
        routes = pathlib.Path(__file__).resolve().parent.parent.joinpath(
            "src", "api", "routes.py").read_text(encoding="utf-8")
        block = routes.split("async def ebay_account_deletion_notification")[1]
        block = block.split("@api_router.post")[0]
        # The insert must be behind the match test, not run unconditionally.
        self.assertIn("if disconnected:", block)
        self.assertLess(block.index("if disconnected:"), block.index("db.add(record)"))
        # And it must still acknowledge, or eBay retries forever.
        self.assertIn("acknowledged", block)


class BulkCostTest(unittest.TestCase):
    """Setting the cost of goods on several listings at once.

    Two modes because both are real ways of buying stock: 30 books at $2 each, or
    $60 paid for the lot and divided. The dividing case is where a wrong answer is
    expensive -- it decides the profit on every one of them.
    """

    @classmethod
    def setUpClass(cls):
        init_db()

    def setUp(self):
        import secrets
        from datetime import datetime, timedelta

        from src.models import AuthSession, Team, TeamMembership, User

        self.db = SessionLocal()
        self.user = User(email=f"bulk.{secrets.token_hex(4)}@example.com",
                         name="Bulk Tester", provider="google", is_admin=False)
        self.db.add(self.user)
        self.db.commit()

        self.team = Team(name="Bulk Team",
                         invite_code=__import__("secrets").token_urlsafe(16),
                         created_by_email=self.user.email)
        self.db.add(self.team)
        self.db.commit()
        self.db.add(TeamMembership(team_id=self.team.id, user_id=self.user.id,
                               role="owner"))
        self.db.commit()

        self.listings = []
        for n in range(30):
            row = Listing(platform="ebay", platform_listing_id=f"bulk-{n}",
                          title=f"Book {n}", team_id=self.team.id,
                          purchase_price_cents=0)
            self.db.add(row)
            self.listings.append(row)
        self.db.commit()
        self.ids = [row.id for row in self.listings]

        token = secrets.token_urlsafe(48)
        self.db.add(AuthSession(token=token, user_id=self.user.id,
                                expires_at=datetime.utcnow() + timedelta(hours=1)))
        self.db.commit()
        self._token = token

    def tearDown(self):
        import secrets as _s
        uid, tid = self.user.id, self.team.id
        self.db.rollback()
        self.db.query(Listing).filter(Listing.team_id == tid).delete(
            synchronize_session=False)
        from src.models import AuthSession, Team, TeamMembership, User
        self.db.query(TeamMembership).filter(
            TeamMembership.team_id == tid).delete(synchronize_session=False)
        self.db.query(AuthSession).filter(AuthSession.user_id == uid).delete(
            synchronize_session=False)
        self.db.query(Team).filter(Team.id == tid).delete(synchronize_session=False)
        self.db.query(User).filter(User.id == uid).delete(synchronize_session=False)
        self.db.commit()
        self.db.close()

    def _client(self):
        from fastapi.testclient import TestClient
        from src.api.main import app
        client = TestClient(app)
        client.cookies.set("auth_token", self._token)
        return client

    def _costs(self):
        from src.models import Listing as L
        self.db.expire_all()
        return [r.purchase_price_cents for r in
                self.db.query(L).filter(L.id.in_(self.ids)).all()]

    def test_the_same_cost_lands_on_every_selected_listing(self):
        """I select 30 books that all cost $2, so every one of them is $2."""
        with self._client() as client:
            res = client.post("/api/listings/bulk-cost",
                              json={"listing_ids": self.ids, "mode": "each",
                                    "amount": 2})
        self.assertEqual(res.status_code, 200, res.text)
        data = res.json()
        self.assertEqual(data["updated"], 30)
        self.assertEqual(data["per_listing_cents"], 200)
        self.assertEqual(set(self._costs()), {200})

    def test_a_total_is_divided_between_them(self):
        """I paid $60 for the lot, so each of the 30 cost $2."""
        with self._client() as client:
            res = client.post("/api/listings/bulk-cost",
                              json={"listing_ids": self.ids, "mode": "total",
                                    "amount": 60})
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(res.json()["per_listing_cents"], 200)
        self.assertEqual(set(self._costs()), {200})

    def test_a_total_that_does_not_divide_does_not_invent_a_remainder(self):
        """A third of a dollar is 33c and a cent goes unassigned, rather than one
        listing silently costing more than the rest."""
        with self._client() as client:
            res = client.post("/api/listings/bulk-cost",
                              json={"listing_ids": self.ids[:3], "mode": "total",
                                    "amount": 1.00})
        self.assertEqual(res.json()["per_listing_cents"], 33)
        self.assertEqual(res.json()["total_cents"], 99)
        self.assertEqual(res.json()["entered_cents"], 100)

    def test_listings_on_another_team_are_refused(self):
        """Tenant isolation, and refused rather than partly applied: applying to the
        ones that matched would leave the caller believing all thirty changed."""
        import secrets as _s
        from src.models import Team as T, User as U

        other_user = U(email=f"other.{_s.token_hex(4)}@example.com", name="Other",
                       provider="google", is_admin=False)
        self.db.add(other_user)
        self.db.commit()
        other_team = T(name="Other Team",
                           invite_code=_s.token_urlsafe(16),
                           created_by_email=other_user.email)
        self.db.add(other_team)
        self.db.commit()
        stranger = Listing(platform="ebay", platform_listing_id="not-mine",
                           title="Not mine", team_id=other_team.id,
                           purchase_price_cents=0)
        self.db.add(stranger)
        self.db.commit()

        try:
            with self._client() as client:
                res = client.post("/api/listings/bulk-cost",
                                  json={"listing_ids": self.ids[:2] + [stranger.id],
                                        "mode": "each", "amount": 5})
            self.assertEqual(res.status_code, 404)
            self.db.expire_all()
            untouched = (self.db.query(Listing)
                         .filter(Listing.id.in_(self.ids[:2])).all())
            self.assertEqual([r.purchase_price_cents for r in untouched], [0, 0],
                             "a partial match was applied anyway")
        finally:
            self.db.query(Listing).filter(Listing.id == stranger.id).delete(
                synchronize_session=False)
            self.db.query(T).filter(T.id == other_team.id).delete(
                synchronize_session=False)
            self.db.query(U).filter(U.id == other_user.id).delete(
                synchronize_session=False)
            self.db.commit()

    def test_rubbish_is_refused(self):
        for payload in ({"listing_ids": [], "mode": "each", "amount": 1},
                        {"listing_ids": self.ids, "mode": "sideways", "amount": 1},
                        {"listing_ids": self.ids, "mode": "each", "amount": -5},
                        {"listing_ids": self.ids, "mode": "each", "amount": "abc"}):
            with self._client() as client:
                res = client.post("/api/listings/bulk-cost", json=payload)
            self.assertEqual(res.status_code, 400, payload)


class ScanTest(unittest.TestCase):
    """The scan endpoint is only as good as what it refuses."""

    def test_an_empty_upload_is_refused(self):
        result = books.isbn_from_image(b"")
        self.assertIsNone(result["isbn"])
        self.assertIn("No image", result["error"])

    def test_bytes_that_are_not_an_image_do_not_crash(self):
        """A wrong file type must return an error, not a 500."""
        result = books.isbn_from_image(b"this is not an image at all")
        self.assertIsNone(result["isbn"])
        self.assertTrue(result["error"])

    def test_candidates_are_pulled_from_ocr_text(self):
        """ISBN lines are tried first, and the check digit is what decides."""
        text = ("Some copyright page text\n"
                "ISBN 978-0-14-032872-1\n"
                "Printed in England\n")
        candidates = books._candidates_from_text(text)
        self.assertIn("9780140328721", candidates)

    def test_obvious_ocr_confusions_are_offered_as_variants(self):
        """A real OCR read of an ISBN often swaps O for 0 and l for 1."""
        text = "ISBN 978-O14-O32872-l"
        candidates = books._candidates_from_text(text)
        self.assertTrue(candidates, "nothing was even proposed")
        fixed = [c.translate(books._OCR_CONFUSIONS) for c in candidates]
        self.assertTrue(any(_valid(c) for c in fixed),
                        f"no variant validated: {candidates}")

    def test_no_isbn_in_the_text_yields_nothing(self):
        self.assertEqual(books._candidates_from_text("no numbers here at all"), [])


def _valid(candidate: str) -> bool:
    try:
        books.normalise_isbn(candidate)
        return True
    except books.BooksError:
        return False


if __name__ == "__main__":
    unittest.main(verbosity=2)
