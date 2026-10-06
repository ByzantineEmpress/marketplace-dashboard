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
from src.models import AuthSession, User


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
