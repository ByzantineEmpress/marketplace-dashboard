"""Syncing every marketplace at once, and the timer that does it unattended.

Two things are easy to get wrong here and are what these cover:

* a sync that fails on one platform must not take the others down with it, and
  the result must say which one broke rather than hiding it behind a bare "ok";
* the background timer must never start by itself. A scheduled job that reaches
  out to live marketplaces would spend API quota and write to the database during
  a test run or on a developer's machine.
"""

import secrets
import unittest
from datetime import datetime, timedelta

from tests import _env  # noqa: E402,F401  isort:skip  (must precede src imports)

from fastapi.testclient import TestClient

from src.api.main import app
from src.config import config
from src.database import SessionLocal, init_db
from src.marketplace_sync import (connected_platforms, sync_all_for_user,
                                  sync_platform)
from src.models import (AuthSession, MarketplaceAccount, Team, TeamMembership,
                        User, UserMarketplaceCredential)


class _FakeAdapter:
    """An adapter that records what it was asked to do."""

    def __init__(self, listings=None, sales=None, raise_on_listings=False):
        self.calls = []
        self._listings = listings if listings is not None else {"success": True}
        self._sales = sales if sales is not None else {"supported": True, "created": 0}
        self._raise = raise_on_listings

    def sync_all(self, db, user_id=None, credentials=None):
        self.calls.append("sync_all")
        if self._raise:
            raise RuntimeError("marketplace exploded")
        return dict(self._listings)

    def sync_sales(self, db, user_id=None, credentials=None):
        self.calls.append("sync_sales")
        return dict(self._sales)


class SyncAllTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()

    def setUp(self):
        import src.adapters as adapters_mod

        # Captured from the module the patch is applied to, and restored in
        # tearDown unconditionally. Getting this wrong leaks a fake adapter into
        # every suite that runs afterwards, which shows up as unrelated failures
        # that move around with collection order.
        self.adapters_mod = adapters_mod
        self._real_get_adapter = adapters_mod.get_adapter
        self.adapters = {}

        self.db = SessionLocal()
        self.user = User(email=f"sync.{secrets.token_hex(4)}@example.com",
                         name="Sync Tester", provider="google", is_admin=False)
        self.db.add(self.user)
        self.db.flush()
        self.team = Team(name=f"Sync {secrets.token_hex(4)}",
                         invite_code=secrets.token_urlsafe(16))
        self.db.add(self.team)
        self.db.flush()
        self.db.add(TeamMembership(team_id=self.team.id, user_id=self.user.id,
                                   role="owner"))
        self.db.commit()

    def tearDown(self):
        # Unconditional: an assertion failure mid-test must not leave the fake
        # adapter installed for the rest of the run.
        self.adapters_mod.get_adapter = self._real_get_adapter

        uid, tid = self.user.id, self.team.id
        self.db.rollback()
        self.db.query(UserMarketplaceCredential).filter(
            UserMarketplaceCredential.user_id == uid).delete(synchronize_session=False)
        self.db.query(MarketplaceAccount).filter(
            MarketplaceAccount.user_id == uid).delete(synchronize_session=False)
        self.db.query(TeamMembership).filter(TeamMembership.user_id == uid).delete(synchronize_session=False)
        self.db.query(Team).filter(Team.id == tid).delete(synchronize_session=False)
        self.db.query(AuthSession).filter(AuthSession.user_id == uid).delete(synchronize_session=False)
        self.db.query(User).filter(User.id == uid).delete(synchronize_session=False)
        self.db.commit()
        self.db.close()

    def _connect(self, platform, with_credentials=True):
        self.db.add(MarketplaceAccount(user_id=self.user.id, platform=platform,
                                       shop_name=f"{platform} shop"))
        if with_credentials:
            self.db.add(UserMarketplaceCredential(
                user_id=self.user.id, platform=platform,
                credential_key="api_key", value="k"))
        self.db.commit()

    def _install(self, mapping):
        self.adapters = mapping
        self.adapters_mod.get_adapter = lambda platform: mapping[platform]

    # -- which platforms are synced ----------------------------------------

    def test_only_connected_platforms_are_synced(self):
        """A saved API key is not a connection. Syncing a platform that was never
        authorised would only produce an error the user cannot act on."""
        self._connect("ebay")
        self.db.add(UserMarketplaceCredential(
            user_id=self.user.id, platform="etsy",
            credential_key="api_key", value="k"))
        self.db.commit()

        self.assertEqual(connected_platforms(self.db, self.user.id), ["ebay"])

    def test_each_platform_is_synced_once(self):
        self._connect("ebay")
        self._connect("etsy")
        self.assertEqual(connected_platforms(self.db, self.user.id), ["ebay", "etsy"])

    # -- one platform failing must not sink the rest -----------------------

    def test_a_broken_platform_does_not_stop_the_others(self):
        self._connect("ebay")
        self._connect("etsy")
        good = _FakeAdapter(listings={"success": True, "listings_added": 3})
        bad = _FakeAdapter(raise_on_listings=True)
        self._install({"ebay": bad, "etsy": good})

        summary = sync_all_for_user(self.db, self.user.id)

        self.assertIn("etsy", summary["results"])
        self.assertTrue(summary["results"]["etsy"]["success"])
        self.assertEqual(summary["results"]["etsy"]["listings_added"], 3)
        # The failure is reported, not swallowed.
        self.assertFalse(summary["results"]["ebay"]["success"])
        self.assertIn("exploded", summary["results"]["ebay"]["error"])
        # And the healthy platform still ran its sales pass.
        self.assertIn("sync_sales", good.calls)

    def test_sales_are_pulled_even_when_no_listings_changed(self):
        """An account whose items have all sold has no active listings but plenty
        of sales, and a listing endpoint drops an item the moment it sells."""
        self._connect("ebay")
        adapter = _FakeAdapter(listings={"success": True, "listings_added": 0},
                               sales={"supported": True, "created": 2})
        self._install({"ebay": adapter})

        result = sync_platform(self.db, self.user.id, "ebay")

        self.assertEqual(adapter.calls, ["sync_all", "sync_sales"])
        self.assertEqual(result["sales"]["created"], 2)

    def test_a_sales_failure_is_captured_not_raised(self):
        self._connect("ebay")

        class HalfBroken(_FakeAdapter):
            def sync_sales(self, db, user_id=None, credentials=None):
                raise RuntimeError("orders API down")

        self._install({"ebay": HalfBroken()})
        result = sync_platform(self.db, self.user.id, "ebay")

        # The listings result survives; only the sales half reports the error.
        self.assertEqual(result["sales"]["success"], False)
        self.assertIn("orders API down", result["sales"]["error"])

    def test_reported_errors_mark_the_platform_unsuccessful(self):
        """A sync that lists errors while reporting success is the dangerous case:
        it looks fine on the dashboard having changed nothing."""
        self._connect("ebay")
        self._install({"ebay": _FakeAdapter(
            listings={"success": True, "errors": ["token expired"]})})
        result = sync_platform(self.db, self.user.id, "ebay")
        self.assertFalse(result["success"])

    # -- the endpoint -------------------------------------------------------

    def test_the_endpoint_says_so_when_nothing_is_connected(self):
        token = secrets.token_urlsafe(48)
        self.db.add(AuthSession(token=token, user_id=self.user.id,
                                expires_at=datetime.utcnow() + timedelta(hours=1)))
        self.db.commit()

        with TestClient(app) as client:
            client.cookies.set("auth_token", token)
            res = client.post("/api/accounts/sync-all")

        self.assertEqual(res.status_code, 400)
        self.assertIn("No marketplaces connected", res.json()["error"])

    # -- the timer ----------------------------------------------------------

    def test_scheduled_syncing_is_off_by_default(self):
        """It must never start itself: unattended API calls during a test run
        would spend quota and write to the database."""
        self.assertFalse(config.SCHEDULED_SYNC_ENABLED)

    def test_the_timer_does_not_start_when_disabled(self):
        """Called with no event loop at all, which is the point: the disabled path
        must return before it touches asyncio.

        Deliberately not wrapped in ``asyncio.run`` — that sets the thread's
        current loop to None on exit, and any suite running later in the same
        process that calls ``asyncio.get_event_loop()`` then fails with "no current
        event loop", several files away from the cause.
        """
        from src import scheduler

        class FakeApp:
            class state:  # noqa: N801 - stands in for Starlette's state object
                pass

        task = scheduler.start(FakeApp())
        self.assertIsNone(task, "the scheduler started while disabled")

    def test_interval_and_delay_have_sane_floors(self):
        """A zero interval would turn the loop into a request flood."""
        self.assertGreaterEqual(config.SCHEDULED_SYNC_INTERVAL_MINUTES, 1)
        self.assertGreaterEqual(config.SCHEDULED_SYNC_STARTUP_DELAY_SECONDS, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
