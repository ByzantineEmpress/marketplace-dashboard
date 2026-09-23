"""Automated integration tests using FastAPI / Starlette TestClient.

Tests the full application end-to-end in-process without requiring
a separate server process or external dependencies.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from starlette.testclient import TestClient
from src.api.main import app
from src.config import config
from src.database import init_db, SessionLocal
from src.models import Listing, Team, TeamMembership, User


class MarketplaceApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()
        cls._client_cm = TestClient(app)
        cls.client = cls._client_cm.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls._client_cm.__exit__(None, None, None)

    def test_01_login_page_renders(self):
        """Verify the login page renders with modern theme toggle."""
        response = self.client.get("/login")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Marketplace Dashboard", response.text)
        self.assertIn("username", response.text)
        self.assertIn("theme-toggle", response.text)

    def test_02_admin_auth_flow(self):
        """Verify password login, session creation, and authenticated routes."""
        # 1. Login with credentials
        login_res = self.client.post(
            "/api/auth/login",
            json={"username": config.ADMIN_USERNAME, "password": config.ADMIN_PASSWORD},
        )
        self.assertEqual(login_res.status_code, 200)
        data = login_res.json()
        self.assertTrue(data.get("ok"))
        self.assertIn("token", data)
        self.assertEqual(len(data["token"]), 64)

        # 2. Check protected pages with session cookie
        dash_res = self.client.get("/dashboard")
        self.assertEqual(dash_res.status_code, 200)
        self.assertIn("listing-grid", dash_res.text)
        self.assertIn("listing-modal", dash_res.text)

        admin_res = self.client.get("/admin")
        self.assertEqual(admin_res.status_code, 200)
        self.assertIn("Marketplace API Configuration", admin_res.text)

    def test_03_settings_endpoints(self):
        """Verify reading and updating settings via the JSON API."""
        # Ensure authenticated
        self.client.post(
            "/api/auth/login",
            json={"username": config.ADMIN_USERNAME, "password": config.ADMIN_PASSWORD},
        )

        # Get settings
        get_res = self.client.get("/api/settings")
        self.assertEqual(get_res.status_code, 200)
        s = get_res.json().get("settings", {})
        self.assertIn("ADMIN_USERNAME", s)
        self.assertIn("EBAY_CLIENT_ID", s)
        self.assertIn("ETSY_API_KEY", s)

        # Update marketplace credentials
        update_res = self.client.post(
            "/api/settings",
            json={
                "EBAY_CLIENT_ID": "test-ebay-client-id",
                "ETSY_API_KEY": "test-etsy-keystring",
            },
        )
        self.assertEqual(update_res.status_code, 200)
        self.assertTrue(update_res.json().get("ok"))
        self.assertIn("EBAY_CLIENT_ID", update_res.json().get("updated", []))

        # Verify config reflects update
        self.assertEqual(config.EBAY_CLIENT_ID, "test-ebay-client-id")
        self.assertEqual(config.EBUY_CLIENT_ID, "test-ebay-client-id")

    def test_04_listings_and_stats(self):
        """Verify listings query and dashboard stats calculation."""
        self.client.post(
            "/api/auth/login",
            json={"username": config.ADMIN_USERNAME, "password": config.ADMIN_PASSWORD},
        )

        listings_res = self.client.get("/api/listings?page=1&page_size=10")
        self.assertEqual(listings_res.status_code, 200)
        data = listings_res.json()
        self.assertIn("total", data)
        self.assertIn("listings", data)
        self.assertIsInstance(data["listings"], list)

        stats_res = self.client.get("/api/stats")
        self.assertEqual(stats_res.status_code, 200)
        stats = stats_res.json()
        self.assertIn("total_listings", stats)
        self.assertIn("active_listings", stats)
        self.assertIn("total_value_cents", stats)

    def test_05_teams_management(self):
        """Verify team creation and member querying."""
        self.client.post(
            "/api/auth/login",
            json={"username": config.ADMIN_USERNAME, "password": config.ADMIN_PASSWORD},
        )

        # Get existing teams
        teams_res = self.client.get("/api/teams")
        self.assertEqual(teams_res.status_code, 200)
        teams = teams_res.json()
        self.assertTrue(any(t["name"] == "Default Team" for t in teams))

        # Create a new test team (idempotent name)
        test_team_name = "Test CI Team"
        db = SessionLocal()
        existing = db.query(Team).filter(Team.name == test_team_name).first()
        if existing:
            for m in db.query(TeamMembership).filter(TeamMembership.team_id == existing.id):
                db.delete(m)
            db.delete(existing)
            db.commit()
        db.close()

        create_res = self.client.post("/api/teams", json={"name": test_team_name})
        self.assertEqual(create_res.status_code, 200)
        self.assertTrue(create_res.json().get("ok"))

    def test_06_plugins_endpoint(self):
        """Verify loaded plugins discovery metadata."""
        self.client.post(
            "/api/auth/login",
            json={"username": config.ADMIN_USERNAME, "password": config.ADMIN_PASSWORD},
        )
        res = self.client.get("/api/plugins")
        self.assertEqual(res.status_code, 200)
        plugins = res.json()
        self.assertIsInstance(plugins, list)
        self.assertTrue(any(p["name"] == "Price Alerts" for p in plugins))

    def test_07_logout_flow(self):
        """Verify logout clears auth session."""
        self.client.post(
            "/api/auth/login",
            json={"username": config.ADMIN_USERNAME, "password": config.ADMIN_PASSWORD},
        )
        logout_res = self.client.post("/api/auth/logout")
        self.assertEqual(logout_res.status_code, 200)

        # Subsequent protected query should fail with 401
        protected_res = self.client.get("/api/settings")
        self.assertEqual(protected_res.status_code, 401)

    def test_08_google_dev_auth_flow(self):
        """Verify local Google OAuth dev simulation and session issuance."""
        # 0. Unconfigured state: when dev mode is off and no client ID, redirect with error
        orig_dev_mode = config.GOOGLE_DEV_MODE
        orig_client_id = config.GOOGLE_CLIENT_ID
        try:
            config.GOOGLE_DEV_MODE = False
            config.GOOGLE_CLIENT_ID = ""
            res_unconfigured = self.client.get("/auth/google", follow_redirects=False)
            self.assertEqual(res_unconfigured.status_code, 302)
            self.assertIn("not+configured", res_unconfigured.headers["location"])
        finally:
            config.GOOGLE_DEV_MODE = orig_dev_mode
            config.GOOGLE_CLIENT_ID = orig_client_id

        # 1. Initiating Google auth redirects to dev picker when credentials are dummy/dev mode
        config.GOOGLE_DEV_MODE = True
        res_init = self.client.get("/auth/google", follow_redirects=False)
        self.assertEqual(res_init.status_code, 302)
        self.assertIn("/auth/google/dev-picker", res_init.headers["location"])

        # 2. Dev picker page renders properly
        res_picker = self.client.get("/auth/google/dev-picker")
        self.assertEqual(res_picker.status_code, 200)
        self.assertIn("Google Sign-In", res_picker.text)
        self.assertIn("Local Test Mode", res_picker.text)

        # 3. Invalid email rejected with redirect error
        res_invalid = self.client.post(
            "/auth/google/dev-login",
            data={"email": "notanemail", "name": "Fake"},
            follow_redirects=False,
        )
        self.assertEqual(res_invalid.status_code, 303)
        self.assertIn("error=", res_invalid.headers["location"])

        # 3b. Whitelist restriction test
        orig_allowed = config.GOOGLE_ALLOWED_EMAILS
        try:
            config.GOOGLE_ALLOWED_EMAILS = ["allowed@example.com"]
            res_denied = self.client.post(
                "/auth/google/dev-login",
                data={"email": "denied@example.com", "name": "Denied"},
                follow_redirects=False,
            )
            self.assertEqual(res_denied.status_code, 303)
            self.assertIn("not+allowed", res_denied.headers["location"])
        finally:
            config.GOOGLE_ALLOWED_EMAILS = orig_allowed

        # 4. Valid test user login sets auth_token cookie and creates session
        test_email = "test.developer@example.com"
        res_login = self.client.post(
            "/auth/google/dev-login",
            data={"email": test_email, "name": "Test Developer"},
            follow_redirects=False,
        )
        self.assertEqual(res_login.status_code, 303)
        self.assertEqual(res_login.headers["location"], "/dashboard")
        self.assertIn("auth_token", res_login.cookies)

        # 5. Accessing dashboard and protected APIs with the session cookie
        dash_res = self.client.get("/dashboard")
        self.assertEqual(dash_res.status_code, 200)
        self.assertIn("listing-grid", dash_res.text)

        # 6. Verify user created in DB and assigned to Default Team
        db = SessionLocal()
        try:
            created_user = db.query(User).filter(User.email == test_email).first()
            self.assertIsNotNone(created_user)
            self.assertEqual(created_user.provider, "google")
            self.assertEqual(created_user.name, "Test Developer")
            membership = (
                db.query(TeamMembership)
                .filter(TeamMembership.user_id == created_user.id)
                .first()
            )
            self.assertIsNotNone(membership)
        finally:
            db.close()

    def test_09_team_invites_and_join_flow(self):
        """Verify team creation, shareable invite token generation, and join flow."""
        # 1. Login as admin
        self.client.post(
            "/api/auth/login",
            json={"username": config.ADMIN_USERNAME, "password": config.ADMIN_PASSWORD},
        )

        # Clean up any preexisting team with test name
        team_name = "Collab Squad Test"
        db = SessionLocal()
        existing = db.query(Team).filter(Team.name == team_name).first()
        if existing:
            for m in db.query(TeamMembership).filter(TeamMembership.team_id == existing.id):
                db.delete(m)
            db.delete(existing)
            db.commit()
        db.close()

        # 2. Create team
        res = self.client.post("/api/teams", json={"name": team_name})
        self.assertEqual(res.status_code, 200)
        team_data = res.json()["team"]
        invite_code = team_data.get("invite_code")
        self.assertTrue(bool(invite_code))
        self.assertIn("/join/", team_data.get("invite_url", ""))

        # 3. Regenerate invite code
        regen_res = self.client.post(f"/api/teams/{team_data['id']}/invite-code")
        self.assertEqual(regen_res.status_code, 200)
        new_invite_code = regen_res.json()["invite_code"]
        self.assertNotEqual(invite_code, new_invite_code)

        # 4. Unauthenticated user visits invite link
        self.client.cookies.clear()
        join_res = self.client.get(f"/join/{new_invite_code}", follow_redirects=False)
        self.assertEqual(join_res.status_code, 302)
        self.assertIn("/login?invited_to=", join_res.headers["location"])
        self.assertIn("Collab", join_res.headers["location"])
        self.assertEqual(join_res.cookies.get("pending_invite"), new_invite_code)

        # 5. User signs in with Google dev login with pending invite cookie present
        invited_email = "newmember@example.com"
        # Ensure cookie is kept in client session
        self.client.cookies.set("pending_invite", new_invite_code)
        login_res = self.client.post(
            "/auth/google/dev-login",
            data={"email": invited_email, "name": "New Member"},
            follow_redirects=False,
        )
        self.assertEqual(login_res.status_code, 303)
        self.assertEqual(login_res.headers["location"], f"/dashboard?team={team_data['id']}")

        # Verify DB membership
        db = SessionLocal()
        try:
            invited_user = db.query(User).filter(User.email == invited_email).first()
            self.assertIsNotNone(invited_user)
            membership = (
                db.query(TeamMembership)
                .filter(
                    TeamMembership.user_id == invited_user.id,
                    TeamMembership.team_id == team_data["id"],
                )
                .first()
            )
            self.assertIsNotNone(membership)
            self.assertEqual(membership.role, "member")
        finally:
            db.close()

    def test_10_manual_listings_and_local_sales(self):
        """Verify creating manual listings, local cash sales, and mark-sold action."""
        # Authenticate
        self.client.post(
            "/api/auth/login",
            json={"username": config.ADMIN_USERNAME, "password": config.ADMIN_PASSWORD},
        )

        # 1. Create a manual local sale listing
        payload = {
            "title": "Vintage Mid-Century Table",
            "price": 120.50,
            "currency": "USD",
            "platform": "local",
            "status": "active",
            "quantity": 1,
            "sku": "VINTAGE-TBL-01",
            "category": "Furniture",
            "description": "Cash on pickup at warehouse",
        }
        res = self.client.post("/api/listings/manual", json=payload)
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data.get("ok"))
        listing = data["listing"]
        listing_id = listing["id"]
        self.assertEqual(listing["title"], "Vintage Mid-Century Table")
        self.assertEqual(listing["price_cents"], 12050)
        self.assertEqual(listing["platform"], "local")
        self.assertEqual(listing["status"], "active")
        self.assertFalse(listing["is_sold"])

        # 2. Query listings with platform=local filter
        list_res = self.client.get("/api/listings?platform=local")
        self.assertEqual(list_res.status_code, 200)
        items = list_res.json()["listings"]
        self.assertTrue(any(i["id"] == listing_id for i in items))

        # 3. Mark as sold fast action
        sold_res = self.client.post(f"/api/listings/{listing_id}/mark-sold")
        self.assertEqual(sold_res.status_code, 200)
        sold_data = sold_res.json()
        self.assertTrue(sold_data.get("ok"))
        self.assertEqual(sold_data["listing"]["status"], "sold")
        self.assertTrue(sold_data["listing"]["is_sold"])

        # 4. Check stats endpoint includes the sold listing
        stats_res = self.client.get("/api/stats")
        self.assertEqual(stats_res.status_code, 200)
        self.assertGreaterEqual(stats_res.json()["sold_listings"], 1)

        # 5. Delete listing
        del_res = self.client.delete(f"/api/listings/{listing_id}")
        self.assertEqual(del_res.status_code, 200)
        self.assertTrue(del_res.json().get("ok"))

        # Confirm deleted
        db = SessionLocal()
        try:
            self.assertIsNone(db.get(Listing, listing_id))
        finally:
            db.close()

    def test_11_poshmark_and_amazon_adapters(self):
        """Verify registration and configuration for Poshmark and Amazon SP-API."""
        from src.adapters import get_adapter

        # Check adapter factory returns correct instances
        posh = get_adapter("poshmark")
        self.assertIsNotNone(posh)
        self.assertEqual(posh.PLATFORM, "poshmark")
        self.assertEqual(posh.get_platform_name(), "Poshmark")

        amzn = get_adapter("amazon")
        self.assertIsNotNone(amzn)
        self.assertEqual(amzn.PLATFORM, "amazon")
        self.assertEqual(amzn.get_platform_name(), "Amazon")

        # Authenticate
        self.client.post(
            "/api/auth/login",
            json={"username": config.ADMIN_USERNAME, "password": config.ADMIN_PASSWORD},
        )

        # Update settings for both platforms
        update_res = self.client.post(
            "/api/settings",
            json={
                "POSHMARK_USERNAME": "test_closet_seller",
                "POSHMARK_API_KEY": "posh_test_key_123",
                "AMAZON_SELLER_ID": "A_TEST_SELLER_ID",
                "AMAZON_CLIENT_ID": "amzn_app_client_id",
                "AMAZON_CLIENT_SECRET": "amzn_app_secret",
                "AMAZON_REFRESH_TOKEN": "amzn_refresh_token_xyz",
            },
        )
        self.assertEqual(update_res.status_code, 200)
        updated = update_res.json().get("updated", [])
        self.assertIn("POSHMARK_USERNAME", updated)
        self.assertIn("AMAZON_SELLER_ID", updated)
        self.assertEqual(config.POSHMARK_USERNAME, "test_closet_seller")
        self.assertEqual(config.AMAZON_SELLER_ID, "A_TEST_SELLER_ID")

        # Verify redirect URIs present in settings GET response
        settings_res = self.client.get("/api/settings")
        self.assertEqual(settings_res.status_code, 200)
        s_data = settings_res.json()
        self.assertIn("poshmark_redirect_uri", s_data)
        self.assertIn("amazon_redirect_uri", s_data)

    def test_12_live_google_oauth_redirect(self):
        """Verify redirect to accounts.google.com when live Google OAuth is enabled."""
        orig_dev_mode = config.GOOGLE_DEV_MODE
        orig_client_id = config.GOOGLE_CLIENT_ID
        try:
            # Set real-looking Google OAuth client ID
            config.GOOGLE_DEV_MODE = False
            config.GOOGLE_CLIENT_ID = "1234567890-testclient.apps.googleusercontent.com"

            res = self.client.get("/auth/google", follow_redirects=False)
            self.assertEqual(res.status_code, 302)
            loc = res.headers.get("location", "")
            self.assertIn("accounts.google.com/oauth2/v2/auth", loc)
            self.assertIn(config.GOOGLE_CLIENT_ID, loc)
            self.assertIn("scope=openid%20email%20profile", loc)
            self.assertIn("google_oauth_state", res.cookies)

            # ?live=1 forces live redirect even when GOOGLE_DEV_MODE is True
            config.GOOGLE_DEV_MODE = True
            res_forced = self.client.get("/auth/google?live=1", follow_redirects=False)
            self.assertEqual(res_forced.status_code, 302)
            self.assertIn("accounts.google.com/oauth2/v2/auth", res_forced.headers.get("location", ""))
        finally:
            config.GOOGLE_DEV_MODE = orig_dev_mode
            config.GOOGLE_CLIENT_ID = orig_client_id


if __name__ == "__main__":
    unittest.main()

