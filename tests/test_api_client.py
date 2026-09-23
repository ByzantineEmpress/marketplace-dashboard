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
from src.models import Team, TeamMembership, User


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


if __name__ == "__main__":
    unittest.main()
