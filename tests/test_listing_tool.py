"""The multi-platform listing tool.

Two things matter more than the happy path here, because this tool puts REAL
listings on REAL accounts:

* it must never publish without an explicit confirmation, and
* one marketplace refusing must not hide what the others did.

Everything else is form-filling.
"""

import secrets
import unittest
from datetime import datetime, timedelta

from tests import _env  # noqa: E402,F401  isort:skip  (must precede src imports)

from fastapi.testclient import TestClient

from src import listing_tool
from src.api.main import app
from src.database import SessionLocal, init_db
from src.models import AuthSession, MarketplaceAccount, Team, TeamMembership, User


class _FakeAdapter:
    def __init__(self, error=None, listing_id="L1"):
        self.error = error
        self.listing_id = listing_id
        self.seen = None

    def publish_listing(self, db, draft, user_id=None, credentials=None):
        self.seen = draft
        if self.error:
            raise RuntimeError(self.error)
        return {"ok": True, "platform": "x", "listing_id": self.listing_id,
                "url": f"https://example.test/{self.listing_id}"}


class ListingToolTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()

    def setUp(self):
        import src.adapters as adapters_mod

        self.adapters_mod = adapters_mod
        self._real = adapters_mod.get_adapter
        self.fakes = {}

        self.db = SessionLocal()
        self.user = User(email=f"lister.{secrets.token_hex(4)}@example.com",
                         name="Lister", provider="google", is_admin=False)
        self.db.add(self.user)
        self.db.flush()
        self.team = Team(name=f"Lister {secrets.token_hex(4)}",
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
        self._token = token

    def tearDown(self):
        self.adapters_mod.get_adapter = self._real
        uid, tid = self.user.id, self.team.id
        self.db.rollback()
        self.db.query(MarketplaceAccount).filter(
            MarketplaceAccount.user_id == uid).delete(synchronize_session=False)
        self.db.query(TeamMembership).filter(TeamMembership.user_id == uid).delete(synchronize_session=False)
        self.db.query(Team).filter(Team.id == tid).delete(synchronize_session=False)
        self.db.query(AuthSession).filter(AuthSession.user_id == uid).delete(synchronize_session=False)
        self.db.query(User).filter(User.id == uid).delete(synchronize_session=False)
        self.db.commit()
        self.db.close()

    def _connect(self, platform, connected=True):
        self.db.add(MarketplaceAccount(user_id=self.user.id, platform=platform,
                                       is_connected=connected,
                                       shop_name=f"{platform} shop"))
        self.db.commit()

    def _install(self, mapping):
        self.fakes = mapping
        self.adapters_mod.get_adapter = lambda platform: mapping[platform]

    # -- preflight ---------------------------------------------------------

    def test_an_unconnected_platform_is_reported_plainly(self):
        report = listing_tool.preflight(self.db, self.user.id)
        self.assertIn("ebay", report["platforms"])
        ebay = report["platforms"]["ebay"]
        self.assertFalse(ebay["connected"])
        self.assertFalse(ebay["ready"])
        self.assertTrue(ebay["problems"])

    def test_a_failing_check_does_not_hide_the_other_platform(self):
        """preflight touches two live APIs; one blowing up must not blank the page."""
        self._connect("ebay")
        self._connect("etsy")

        class Boom:
            def get_token(self, *a, **k):
                raise RuntimeError("network down")

        self._install({"ebay": Boom(), "etsy": Boom()})
        report = listing_tool.preflight(self.db, self.user.id)

        self.assertFalse(report["platforms"]["ebay"]["ready"])
        self.assertIn("Could not check", report["platforms"]["ebay"]["problems"][0])
        self.assertIn("etsy", report["platforms"])

    # -- publish -----------------------------------------------------------

    def test_publishing_reports_each_platform_independently(self):
        self._connect("ebay")
        self._connect("etsy")
        self._install({
            "ebay": _FakeAdapter(error="eBay said no"),
            "etsy": _FakeAdapter(listing_id="ETSY9"),
        })

        result = listing_tool.publish(self.db, self.user.id,
                                      {"title": "Thing", "price": 10}, ["ebay", "etsy"])

        self.assertEqual(result["published"], ["etsy"])
        self.assertEqual(result["failed"], ["ebay"])
        self.assertIn("eBay said no", result["results"]["ebay"]["error"])
        # The etsy result survives the ebay failure intact.
        self.assertEqual(result["results"]["etsy"]["listing_id"], "ETSY9")

    def test_a_disconnected_platform_is_not_attempted(self):
        self._connect("ebay", connected=False)
        self._install({"ebay": _FakeAdapter()})
        result = listing_tool.publish(self.db, self.user.id, {"title": "x"}, ["ebay"])
        self.assertFalse(result["results"]["ebay"]["ok"])
        self.assertIn("not connected", result["results"]["ebay"]["error"])

    def test_an_unknown_platform_is_ignored(self):
        self._connect("ebay")
        self._install({"ebay": _FakeAdapter()})
        result = listing_tool.publish(self.db, self.user.id, {"title": "x"},
                                      ["ebay", "myspace"])
        self.assertNotIn("myspace", result["results"])

    def test_the_draft_reaches_the_adapter_unchanged(self):
        self._connect("ebay")
        adapter = _FakeAdapter()
        self._install({"ebay": adapter})
        draft = {"title": "Thing", "price": 12.5, "taxonomy_id": "1"}
        listing_tool.publish(self.db, self.user.id, draft, ["ebay"])
        self.assertEqual(adapter.seen, draft)

    # -- the endpoint ------------------------------------------------------

    def _client(self):
        client = TestClient(app)
        client.cookies.set("auth_token", self._token)
        return client

    def test_the_endpoint_refuses_an_unconfirmed_publish(self):
        """This creates live listings. A request without the flag must not."""
        self._connect("ebay")
        self._install({"ebay": _FakeAdapter()})
        with self._client() as client:
            res = client.post("/api/beta/listing/publish",
                              json={"draft": {"title": "x", "price": 5}})
        self.assertEqual(res.status_code, 400)
        self.assertIn("Not confirmed", res.json()["error"])

    def test_the_endpoint_requires_a_title_and_a_price(self):
        self._connect("ebay")
        self._install({"ebay": _FakeAdapter()})
        with self._client() as client:
            no_title = client.post("/api/beta/listing/publish",
                                   json={"confirm": True, "draft": {"price": 5}})
            no_price = client.post("/api/beta/listing/publish",
                                   json={"confirm": True, "draft": {"title": "x"}})
        self.assertEqual(no_title.status_code, 400)
        self.assertEqual(no_price.status_code, 400)

    def test_a_confirmed_publish_reaches_the_adapter(self):
        self._connect("ebay")
        adapter = _FakeAdapter(listing_id="EBAY7")
        self._install({"ebay": adapter})
        with self._client() as client:
            res = client.post("/api/beta/listing/publish",
                              json={"confirm": True, "platforms": ["ebay"],
                                    "draft": {"title": "Thing", "price": 9.99}})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["published"], ["ebay"])
        self.assertEqual(adapter.seen["title"], "Thing")

    def test_the_page_renders(self):
        with self._client() as client:
            res = client.get("/beta/listing")
        self.assertEqual(res.status_code, 200)
        self.assertIn("Multi-Platform Lister", res.text)
        # The publish control and the readiness panel are the two things the page
        # is for; a template that lost either would still return 200.
        self.assertIn("publish-btn", res.text)
        self.assertIn("preflight-body", res.text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
