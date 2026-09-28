"""Automated integration tests using FastAPI / Starlette TestClient.

Tests the full application end-to-end in-process without requiring
a separate server process or external dependencies.
"""

import os
import secrets
import sys
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Must precede the src imports: it fixes the ambient configuration the app
# reads at import time. See tests/_env.py for why that matters.
try:
    from tests import _env  # noqa: E402,F401  isort:skip
except ImportError:  # `tests` resolved to the directory, not the package
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import _env  # noqa: E402,F401  isort:skip


from starlette.testclient import TestClient
from src.api.main import app
from src.api.routes import INVITE_CODE_MIN_LENGTH
from src.config import config
from src.database import init_db, SessionLocal
from src.models import (
    AuthSession,
    Listing,
    PendingSignup,
    Team,
    TeamJoinRequest,
    TeamMembership,
    User,
)


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
        self.assertIn("open-teams-modal-btn", dash_res.text)
        self.assertIn("add-manual-listing-btn", dash_res.text)
        self.assertIn("manual-listing-modal", dash_res.text)
        self.assertIn("teams-modal", dash_res.text)

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
        # A brand-new account is NOT auto-enrolled into any team: it is sent to
        # the onboarding chooser instead, so it can never land in someone
        # else's shared inventory by signing in.
        self.assertEqual(res_login.headers["location"], "/onboarding")
        self.assertIn("auth_token", res_login.cookies)
        # The session cookie must be SameSite=lax, not strict: it is set on a
        # response to a navigation arriving from accounts.google.com, and a
        # strict cookie is withheld on the following /dashboard request, which
        # bounces the user to /login?error=auth_required. TestClient does not
        # enforce SameSite, so assert the attribute itself.
        set_cookie = res_login.headers.get("set-cookie", "")
        self.assertIn("auth_token=", set_cookie)
        self.assertIn("samesite=lax", set_cookie.lower())
        self.assertIn("httponly", set_cookie.lower())

        # 5. The new account owns no team yet
        db = SessionLocal()
        try:
            created_user = db.query(User).filter(User.email == test_email).first()
            self.assertIsNotNone(created_user)
            self.assertEqual(created_user.provider, "google")
            self.assertEqual(created_user.name, "Test Developer")
            self.assertEqual(
                db.query(TeamMembership)
                .filter(TeamMembership.user_id == created_user.id)
                .count(),
                0,
                "a fresh sign-in must not be placed in any team",
            )
        finally:
            db.close()

        # 6. Onboarding page renders both choices
        onb = self.client.get("/onboarding")
        self.assertEqual(onb.status_code, 200)
        self.assertIn("Create my own workspace", onb.text)
        self.assertIn("Ask to join a team", onb.text)

        # 7. Choosing "my own workspace" creates it and lands on the dashboard
        made = self.client.post("/api/onboarding/create-team")
        self.assertEqual(made.status_code, 200)
        self.assertTrue(made.json()["ok"])
        self.assertEqual(made.json()["redirect"], "/dashboard")

        db = SessionLocal()
        try:
            created_user = db.query(User).filter(User.email == test_email).first()
            memberships = (
                db.query(TeamMembership)
                .filter(TeamMembership.user_id == created_user.id)
                .all()
            )
            self.assertEqual(len(memberships), 1)
            self.assertEqual(memberships[0].role, "owner")
            own_team = db.query(Team).filter(Team.id == memberships[0].team_id).first()
            self.assertIn("Workspace", own_team.name)
            # The creator is recorded so others can request access by email.
            self.assertEqual((own_team.created_by_email or "").lower(), test_email)
        finally:
            db.close()

        # 8. Accessing dashboard and protected APIs with the session cookie
        dash_res = self.client.get("/dashboard")
        self.assertEqual(dash_res.status_code, 200)
        self.assertIn("listing-grid", dash_res.text)

        # 9. A user who already has a team skips onboarding entirely
        again = self.client.get("/onboarding", follow_redirects=False)
        self.assertEqual(again.status_code, 302)
        self.assertEqual(again.headers["location"], "/dashboard")

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

        # 4b. The remembered invite must be short-lived (15 minutes) and the
        #     code itself must carry full cryptographic entropy.
        set_cookie = join_res.headers.get("set-cookie", "")
        self.assertIn("pending_invite=", set_cookie)
        self.assertIn("max-age=900", set_cookie.lower())
        self.assertIn("httponly", set_cookie.lower())
        self.assertIn("samesite=lax", set_cookie.lower())
        # 16 bytes -> 22 url-safe chars, and never a short/weak code
        self.assertEqual(len(new_invite_code), 22)
        self.assertGreaterEqual(len(new_invite_code), INVITE_CODE_MIN_LENGTH)

        # 4c. A too-short / malformed code must be rejected outright
        short_res = self.client.get("/join/abc123", follow_redirects=False)
        self.assertEqual(short_res.status_code, 302)
        self.assertIn("error=Invalid", short_res.headers["location"])

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

    def test_12b_admin_assignment_is_admin_only(self):
        """Only admins may list users or grant admin rights.

        The /api/users endpoints expose every account's email address and can
        hand out admin access, so a non-admin must be refused.
        """
        # 1. Admin (local account) can list users
        self.client.post(
            "/api/auth/login",
            json={"username": config.ADMIN_USERNAME, "password": config.ADMIN_PASSWORD},
        )
        res = self.client.get("/api/users")
        self.assertEqual(res.status_code, 200)
        payload = res.json()
        self.assertIn("users", payload)
        self.assertGreaterEqual(payload["total"], 1)
        admin_row = next(u for u in payload["users"] if u["provider"] == "local")
        self.assertTrue(admin_row["is_admin"])

        # 2. The local admin can never be demoted via the API
        res_locked = self.client.post(
            f"/api/users/{admin_row['id']}/admin", json={"is_admin": False}
        )
        self.assertEqual(res_locked.status_code, 400)

        # 3. Unknown users are a 404, not a 500
        res_missing = self.client.post("/api/users/99999999/admin", json={"is_admin": True})
        self.assertEqual(res_missing.status_code, 404)

        # 4. A non-admin session is refused with 403
        db = SessionLocal()
        try:
            plain = db.query(User).filter(User.provider == "google", User.is_admin == False).first()  # noqa: E712
            if plain is None:
                self.skipTest("no non-admin google user available")
            plain_token = secrets.token_urlsafe(48)
            db.add(AuthSession(
                token=plain_token,
                user_id=plain.id,
                expires_at=datetime.utcnow() + timedelta(days=1),
            ))
            db.commit()
            plain_id = plain.id
        finally:
            db.close()

        self.client.cookies.clear()
        self.client.cookies.set("auth_token", plain_token)
        self.assertEqual(self.client.get("/api/users").status_code, 403)
        self.assertEqual(
            self.client.post(f"/api/users/{admin_row['id']}/admin", json={"is_admin": True}).status_code,
            403,
        )
        # A non-admin also cannot promote themselves
        self.assertEqual(
            self.client.post(f"/api/users/{plain_id}/admin", json={"is_admin": True}).status_code,
            403,
        )

        # 5. Clean up the extra session so later tests start clean
        db = SessionLocal()
        try:
            db.query(AuthSession).filter(AuthSession.token == plain_token).delete()
            db.commit()
        finally:
            db.close()

    def test_12c_join_requests_are_sanitized_rate_limited_and_owner_scoped(self):
        """Ask-to-join: sanitisation, rate limits, and owner-only approval.

        Covers the request-by-owner-email flow added for onboarding.
        """
        admin_email = f"{config.ADMIN_USERNAME}@local"
        requester_email = "join.request.candidate@example.com"

        db = SessionLocal()
        try:
            owner = db.query(User).filter(User.provider == "local").first()
            self.assertIsNotNone(owner)
            # A team owned by the local admin, discoverable by creator email.
            team = db.query(Team).filter(Team.name == "Test Join Flow Team").first()
            if team is None:
                team = Team(name="Test Join Flow Team", invite_code=secrets.token_urlsafe(16),
                            created_by_email=admin_email)
                db.add(team)
                db.flush()
                db.add(TeamMembership(team_id=team.id, user_id=owner.id, role="owner"))
            else:
                team.created_by_email = admin_email

            requester = db.query(User).filter(User.email == requester_email).first()
            if requester is None:
                requester = User(email=requester_email, name="Join Candidate", provider="google")
                db.add(requester)
                db.flush()

            requester_token = secrets.token_urlsafe(48)
            db.add(AuthSession(token=requester_token, user_id=requester.id,
                               expires_at=datetime.utcnow() + timedelta(days=1)))
            db.commit()
            team_id, requester_id = team.id, requester.id
        finally:
            db.close()

        try:
            # --- requester submits, with markup and control characters ---
            self.client.cookies.clear()
            self.client.cookies.set("auth_token", requester_token)
            nasty = "<script>alert(1)</script>\n\x00 I am Jamie\r\n<img src=x onerror=alert(2)>"
            res = self.client.post("/api/onboarding/request-access", json={
                "owner_email": admin_email,
                "description": nasty,
            })
            self.assertEqual(res.status_code, 200)
            self.assertTrue(res.json()["ok"])
            # The response must not reveal whether a team matched.
            self.assertNotIn("team", res.json())
            self.assertNotIn("Test Join Flow Team", res.text)

            db = SessionLocal()
            try:
                stored = (
                    db.query(TeamJoinRequest)
                    .filter(
                        TeamJoinRequest.team_id == team_id,
                        TeamJoinRequest.requester_user_id == requester_id,
                    )
                    .order_by(TeamJoinRequest.id.desc())
                    .first()
                )
                self.assertIsNotNone(stored, "request was not stored")
                self.assertEqual(stored.status, "pending")
                # Sanitised: no angle brackets, no control characters, single line
                self.assertNotIn("<", stored.description)
                self.assertNotIn(">", stored.description)
                self.assertNotIn("\n", stored.description)
                self.assertNotIn("\r", stored.description)
                self.assertNotIn("\x00", stored.description)
                self.assertIn("alert(1)", stored.description)  # text survives, markup does not
                self.assertLessEqual(len(stored.description), 300)
            finally:
                db.close()

            # --- a short description is rejected ---
            short = self.client.post("/api/onboarding/request-access", json={
                "owner_email": admin_email, "description": "x",
            })
            self.assertEqual(short.status_code, 400)

            # --- a malformed email is rejected ---
            bad_email = self.client.post("/api/onboarding/request-access", json={
                "owner_email": "not-an-email", "description": "hello there",
            })
            self.assertEqual(bad_email.status_code, 400)

            # --- an unknown but valid address gets the SAME generic answer,
            #     so this endpoint cannot enumerate team owners ---
            unknown = self.client.post("/api/onboarding/request-access", json={
                "owner_email": "nobody.owns.this@example.com", "description": "hello there",
            })
            # may be 429 if the burst limit already tripped; otherwise identical
            self.assertIn(unknown.status_code, (200, 429))

            # --- rate limit: the per-account burst is 1 per 10 minutes, so a
            #     second request from the same account must be refused ---
            second = self.client.post("/api/onboarding/request-access", json={
                "owner_email": admin_email, "description": "another attempt",
            })
            self.assertEqual(second.status_code, 429)
            self.assertIn("retry_after_seconds", second.json())
            self.assertIn("Retry-After", second.headers)

            # --- a non-owner may NOT read or decide requests ---
            non_owner = self.client.get("/api/join-requests")
            self.assertEqual(non_owner.status_code, 200)
            self.assertEqual(non_owner.json()["pending_count"], 0)
            self.assertEqual(non_owner.json()["requests"], [])
            forbidden = self.client.post(f"/api/join-requests/{stored.id}/approve")
            self.assertEqual(forbidden.status_code, 403)

            # --- the owner sees it, including the sanitised note ---
            self.client.post(
                "/api/auth/login",
                json={"username": config.ADMIN_USERNAME, "password": config.ADMIN_PASSWORD},
            )
            owner_view = self.client.get("/api/join-requests")
            self.assertEqual(owner_view.status_code, 200)
            payload = owner_view.json()
            mine = [r for r in payload["requests"] if r["id"] == stored.id]
            self.assertEqual(len(mine), 1, "owner cannot see the pending request")
            self.assertEqual(mine[0]["requester_email"], requester_email)
            self.assertIn("alert(1)", mine[0]["description"])
            self.assertGreaterEqual(payload["pending_count"], 1)

            # --- approve adds the requester to that team only ---
            approve = self.client.post(f"/api/join-requests/{stored.id}/approve")
            self.assertEqual(approve.status_code, 200)
            self.assertEqual(approve.json()["status"], "approved")

            db = SessionLocal()
            try:
                membership = (
                    db.query(TeamMembership)
                    .filter(TeamMembership.team_id == team_id,
                            TeamMembership.user_id == requester_id)
                    .first()
                )
                self.assertIsNotNone(membership, "approval did not add the member")
                self.assertEqual(membership.role, "member")
            finally:
                db.close()

            # --- deciding twice is refused ---
            again = self.client.post(f"/api/join-requests/{stored.id}/approve")
            self.assertEqual(again.status_code, 400)

            # --- the requester now belongs to exactly one team ---
            db = SessionLocal()
            try:
                count = (db.query(TeamMembership)
                         .filter(TeamMembership.user_id == requester_id).count())
                self.assertEqual(count, 1, "approval must add exactly one membership")
            finally:
                db.close()
        finally:
            # --- cleanup so later tests are unaffected ---
            db = SessionLocal()
            try:
                db.query(TeamJoinRequest).filter(TeamJoinRequest.team_id == team_id).delete()
                db.query(TeamMembership).filter(TeamMembership.team_id == team_id).delete()
                db.query(AuthSession).filter(AuthSession.user_id == requester_id).delete()
                db.query(Team).filter(Team.id == team_id).delete()
                db.query(User).filter(User.id == requester_id).delete()
                db.commit()
            finally:
                db.close()
            self.client.cookies.clear()

    def test_12d_description_sanitizer(self):
        """Direct unit checks on the join-request description sanitizer."""
        from src.api.routes import (
            DESCRIPTION_MAX_LENGTH,
            _sanitize_description,
        )

        # Markup is removed entirely; harmless wording is preserved.
        self.assertEqual(
            _sanitize_description("<b>Jamie</b> from the workshop"),
            "bJamie/b from the workshop",
        )
        # Control characters and newlines cannot survive.
        self.assertNotIn("\n", _sanitize_description("line one\nline two"))
        self.assertNotIn("\r", _sanitize_description("cr\rhere"))
        self.assertNotIn("\t", _sanitize_description("tab\there"))
        self.assertNotIn("\x00", _sanitize_description("nul\x00here"))
        # No angle brackets survive, so no markup can be stored.
        for probe in ("<script>alert(1)</script>", "<img src=x onerror=alert(1)>", "a<b>c"):
            cleaned = _sanitize_description(probe)
            self.assertNotIn("<", cleaned)
            self.assertNotIn(">", cleaned)
        # Whitespace is collapsed. Note tabs/newlines are *deleted* rather than
        # converted to spaces, so "b\tc" becomes "bc" — deliberate: a
        # multi-line paste must not silently become two words.
        self.assertEqual(_sanitize_description("a   b"), "a b")
        self.assertEqual(_sanitize_description("a b\tc"), "a bc")
        self.assertLessEqual(len(_sanitize_description("x" * 5000)), DESCRIPTION_MAX_LENGTH)
        # Non-strings never raise and never become text.
        for bad in (None, 123, [], {}, object()):
            self.assertEqual(_sanitize_description(bad), "")

    def test_12e_password_signup_requires_email_verification(self):
        """Self-serve signup withholds the account until the address is confirmed.

        Full behaviour (tokens, expiry, resends) lives in
        tests/test_email_verification.py; this checks the pieces that matter
        for the rest of the API surface.
        """
        import re as _re

        from src import mailer
        from src.api import routes as routes_module

        # Signup is rate limited per IP; clear the buckets so this test is not
        # at the mercy of how many other tests created accounts.
        routes_module._rate_prune()
        self.client.cookies.clear()
        outbox = []
        mailer.OUTBOX = outbox

        email = f"signup_{secrets.token_hex(4)}@example.com"
        password = "correct-horse-battery"

        try:
            # --- the page renders, with a CSRF token the POST will need ---
            page = self.client.get("/signup")
            self.assertEqual(page.status_code, 200)
            self.assertIn("Create your account", page.text)
            csrf = _re.search(r'name="csrf_token"\s+value="([^"]+)"', page.text)
            self.assertIsNotNone(csrf, "signup form has no CSRF token")
            token = csrf.group(1)

            # --- weak password is refused ---
            weak = self.client.post("/api/auth/signup", data={
                "csrf_token": token, "email": email, "name": "Signup Person",
                "password": "short", "confirm_password": "short",
            }, follow_redirects=False)
            self.assertEqual(weak.status_code, 303)
            self.assertIn("error=", weak.headers["location"])

            # --- mismatched confirmation is refused ---
            mismatch = self.client.post("/api/auth/signup", data={
                "csrf_token": token, "email": email, "name": "Signup Person",
                "password": password, "confirm_password": password + "x",
            }, follow_redirects=False)
            self.assertEqual(mismatch.status_code, 303)
            self.assertIn("error=", mismatch.headers["location"])

            # --- malformed email is refused ---
            bad_email = self.client.post("/api/auth/signup", data={
                "csrf_token": token, "email": "not-an-email", "name": "X",
                "password": password, "confirm_password": password,
            }, follow_redirects=False)
            self.assertEqual(bad_email.status_code, 303)
            self.assertIn("error=", bad_email.headers["location"])

            # --- nothing above should have created a pending signup or mail ---
            self.assertEqual(len(outbox), 0)

            # --- the real thing: pending, not an account ---
            created = self.client.post("/api/auth/signup", data={
                "csrf_token": token, "email": email, "name": "Signup Person",
                "password": password, "confirm_password": password,
            }, follow_redirects=False)
            self.assertEqual(created.status_code, 303)
            self.assertIn("/verify-pending", created.headers["location"])
            # No session is issued while unverified.
            self.assertNotIn("auth_token=", created.headers.get("set-cookie", ""))
            self.assertEqual(len(outbox), 1)
            self.assertEqual(outbox[0]["to"], email)

            db = SessionLocal()
            try:
                # The key guarantee: no user row exists yet.
                self.assertIsNone(db.query(User).filter(User.email == email).first(),
                                  "signup created a user before verification")
                pending = db.query(PendingSignup).filter(
                    PendingSignup.email == email).first()
                self.assertIsNotNone(pending)
                # Stored as a digest, never the raw token.
                self.assertEqual(len(pending.token_hash), 64)
                self.assertTrue(pending.password_hash.startswith("$2b$"))
                self.assertNotIn(password, pending.password_hash)
                pending_id = pending.id
            finally:
                db.close()

            # --- an unverified account cannot sign in ---
            self.client.cookies.clear()
            login = self.client.post(
                "/api/auth/login", json={"username": email, "password": password})
            self.assertEqual(login.status_code, 401)

            # --- the emitted link completes the signup ---
            link = _re.search(r"/verify-email\?token=([A-Za-z0-9_\-%]+)", outbox[0]["body"])
            self.assertIsNotNone(link, "no verification link in the email")
            verified = self.client.get(f"/verify-email?token={link.group(1)}",
                                       follow_redirects=False)
            self.assertEqual(verified.status_code, 302)
            self.assertIn("verified=1", verified.headers["location"])

            db = SessionLocal()
            try:
                user = db.query(User).filter(User.email == email).first()
                self.assertIsNotNone(user, "verification did not create the user")
                # Must not carry the admin-implying provider.
                self.assertEqual(user.provider, "password")
                self.assertFalse(bool(user.is_admin), "signup granted admin")
                self.assertEqual(
                    db.query(TeamMembership).filter(
                        TeamMembership.user_id == user.id).count(),
                    0, "signup must not place the account in any team")
                user_id = user.id
            finally:
                db.close()

            # --- now they can sign in, and are sent to onboarding ---
            self.client.cookies.clear()
            good = self.client.post("/api/auth/login",
                                    json={"username": email, "password": password})
            self.assertEqual(good.status_code, 200)
            self.assertTrue(good.json()["ok"])
            self.assertEqual(len(good.json()["token"]), 64)
            self.client.cookies.set("auth_token", good.json()["token"])
            onb = self.client.get("/onboarding", follow_redirects=False)
            self.assertEqual(onb.status_code, 200)

            # --- and are refused admin-only endpoints ---
            self.assertEqual(self.client.get("/api/users").status_code, 403)
            self.assertEqual(self.client.get("/api/settings").status_code, 403)
            self.assertEqual(
                self.client.post("/api/users/1/admin", json={"is_admin": True}).status_code, 403)

            # --- a wrong password is rejected ---
            self.client.cookies.clear()
            bad = self.client.post("/api/auth/login",
                                   json={"username": email, "password": "wrong"})
            self.assertEqual(bad.status_code, 401)

            # --- a Google account cannot be signed into with a password ---
            db = SessionLocal()
            try:
                g = User(email=f"google_acct_{secrets.token_hex(4)}@example.com",
                         name="Google Acct", provider="google", password_hash=None)
                db.add(g)
                db.commit()
                g_email, g_id = g.email, g.id
            finally:
                db.close()
            self.client.cookies.clear()
            self.assertEqual(
                self.client.post("/api/auth/login",
                                 json={"username": g_email, "password": ""}).status_code,
                401)

            # --- cleanup ---
            db = SessionLocal()
            try:
                db.query(AuthSession).filter(
                    AuthSession.user_id.in_([user_id, g_id])).delete(synchronize_session=False)
                db.query(TeamMembership).filter(
                    TeamMembership.user_id.in_([user_id, g_id])).delete(synchronize_session=False)
                db.query(PendingSignup).filter(PendingSignup.id == pending_id).delete(
                    synchronize_session=False)
                db.query(User).filter(User.id.in_([user_id, g_id])).delete(synchronize_session=False)
                db.query(Team).filter(Team.created_by_email == email).delete(synchronize_session=False)
                db.commit()
            finally:
                db.close()
        finally:
            mailer.OUTBOX = None
            routes_module._rate_prune()
            routes_module.LOGIN_ATTEMPTS.clear()
            self.client.cookies.clear()

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
            self.assertIn("accounts.google.com/o/oauth2/v2/auth", loc)
            self.assertIn(config.GOOGLE_CLIENT_ID, loc)
            self.assertIn("scope=openid%20email%20profile", loc)
            self.assertIn("google_oauth_state", res.cookies)

            # ?live=1 forces live redirect even when GOOGLE_DEV_MODE is True
            config.GOOGLE_DEV_MODE = True
            res_forced = self.client.get("/auth/google?live=1", follow_redirects=False)
            self.assertEqual(res_forced.status_code, 302)
            self.assertIn("accounts.google.com/o/oauth2/v2/auth", res_forced.headers.get("location", ""))
        finally:
            config.GOOGLE_DEV_MODE = orig_dev_mode
            config.GOOGLE_CLIENT_ID = orig_client_id


    def test_13_cost_parts_and_cad_currency(self):
        """Verify CAD default currency, bought-for cost, repair parts tracking, and profit calculations."""
        # Ensure authenticated
        self.client.post(
            "/api/auth/login",
            json={"username": config.ADMIN_USERNAME, "password": config.ADMIN_PASSWORD},
        )

        # 1. Create a manual listing with purchase price and repair parts in CAD
        create_res = self.client.post(
            "/api/listings/manual",
            json={
                "title": "Nintendo Switch OLED - Console with New Power Supply",
                "price": 280.00,
                "purchase_price": 120.00,
                "parts": [
                    {"description": "OEM AC Adapter Power Supply", "cost": 30.00},
                    {"description": "Replacement Joy-Con Rail", "cost": 15.00},
                ],
                "quantity": 1,
                "platform": "local",
                "status": "active",
                "sku": "NSW-001",
                "description": "Refurbished console tested and fully working",
            },
        )
        self.assertEqual(create_res.status_code, 200)
        data = create_res.json()
        self.assertTrue(data.get("ok"))
        listing = data.get("listing", {})
        listing_id = listing["id"]

        self.assertEqual(listing["currency"], "CAD")
        self.assertEqual(listing["price_cents"], 28000)
        self.assertEqual(listing["purchase_price_cents"], 12000)
        self.assertEqual(listing["purchase_price"], 120.00)
        self.assertEqual(listing["parts_cost_cents"], 4500)
        self.assertEqual(listing["parts_cost"], 45.00)
        self.assertEqual(len(listing["parts"]), 2)
        self.assertEqual(listing["total_cost_cents"], 16500)
        self.assertEqual(listing["total_cost"], 165.00)
        self.assertEqual(listing["net_profit_cents"], 11500)
        self.assertEqual(listing["net_profit"], 115.00)
        self.assertAlmostEqual(listing["profit_margin_pct"], 41.1, places=1)

        # 2. Update an existing listing's costs and parts via PUT /api/listings/{id}
        update_res = self.client.put(
            f"/api/listings/{listing_id}",
            json={
                "purchase_price": 110.00,
                "price": 290.00,
                "parts": [
                    {"description": "OEM AC Adapter Power Supply", "cost": 25.00},
                ],
            },
        )
        self.assertEqual(update_res.status_code, 200)
        up_data = update_res.json()
        self.assertTrue(up_data.get("ok"))
        up_listing = up_data.get("listing", {})
        self.assertEqual(up_listing["purchase_price"], 110.00)
        self.assertEqual(up_listing["parts_cost"], 25.00)
        self.assertEqual(up_listing["total_cost"], 135.00)
        self.assertEqual(up_listing["net_profit"], 155.00)

        # 3. Verify stats reflect total costs
        stats_res = self.client.get("/api/stats")
        self.assertEqual(stats_res.status_code, 200)
        s = stats_res.json()
        self.assertEqual(s.get("currency"), "CAD")
        self.assertGreaterEqual(s.get("total_cost_cents", 0), 13500)

        # Cleanup
        self.client.delete(f"/api/listings/{listing_id}")

    def test_14_team_invite_email_dispatch(self):
        """Verify team invite generation, invite link, email template, and SMTP response."""
        import time
        # Ensure authenticated
        self.client.post(
            "/api/auth/login",
            json={"username": config.ADMIN_USERNAME, "password": config.ADMIN_PASSWORD},
        )

        # Get existing teams
        teams_res = self.client.get("/api/teams")
        self.assertEqual(teams_res.status_code, 200)
        teams = teams_res.json()
        self.assertGreaterEqual(len(teams), 1)
        team_id = teams[0]["id"]

        test_email = f"collab_{int(time.time() * 1000)}@example.com"
        try:
            # Add / invite collaborator by email
            invite_res = self.client.post(
                f"/api/teams/{team_id}/members",
                json={"email": test_email},
            )
            self.assertEqual(invite_res.status_code, 200)
            inv_data = invite_res.json()
            self.assertTrue(inv_data.get("ok"))
            self.assertIn("invite_url", inv_data)
            self.assertIn("/join/", inv_data["invite_url"])
            self.assertIn("invite_subject", inv_data)
            self.assertIn("invite_body", inv_data)
            self.assertIn("team_name", inv_data)
        finally:
            db = SessionLocal()
            try:
                u = db.query(User).filter(User.email == test_email).first()
                if u:
                    db.query(TeamMembership).filter(TeamMembership.user_id == u.id).delete()
                    db.delete(u)
                    db.commit()
            finally:
                db.close()

    def test_15_multi_platform_cross_listing_and_image_upload(self):
        """Verify multi-platform cross-listing, image file upload, and filtering."""
        import os
        import io

        # 1. Login as admin
        self.client.post(
            "/api/auth/login",
            json={"username": config.ADMIN_USERNAME, "password": config.ADMIN_PASSWORD},
        )

        # 2. Test file upload with disallowed extension
        res_bad = self.client.post(
            "/api/upload",
            files={"file": ("malicious.exe", io.BytesIO(b"executable content"), "application/octet-stream")},
        )
        self.assertEqual(res_bad.status_code, 400)
        self.assertFalse(res_bad.json().get("ok"))

        # 3. Test valid image file upload
        test_img_bytes = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15c4\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
        res_upload = self.client.post(
            "/api/upload",
            files={"file": ("controller.png", io.BytesIO(test_img_bytes), "image/png")},
        )
        self.assertEqual(res_upload.status_code, 200)
        up_data = res_upload.json()
        self.assertTrue(up_data.get("ok"))
        uploaded_url = up_data.get("url")
        self.assertTrue(uploaded_url.startswith("/static/uploads/"))

        # Verify uploaded file exists on disk
        local_path = uploaded_url.lstrip("/")
        self.assertTrue(os.path.exists(local_path))

        listing_id = None
        try:
            # 4. Create manual listing cross-listed on eBay, Etsy, and Facebook
            create_res = self.client.post(
                "/api/listings/manual",
                json={
                    "title": "Custom LED Modded Pro Controller",
                    "price": 149.99,
                    "purchase_price": 45.00,
                    "parts": [
                        {"description": "Hall Effect Joysticks", "cost": 15.00},
                        {"description": "RGB LED Kit", "cost": 12.50},
                    ],
                    "platforms": ["ebay", "etsy", "facebook"],
                    "image_url": uploaded_url,
                    "quantity": 1,
                    "status": "active",
                },
            )
            self.assertEqual(create_res.status_code, 200)
            data = create_res.json()
            self.assertTrue(data.get("ok"))
            listing = data.get("listing", {})
            listing_id = listing["id"]

            self.assertEqual(listing["image_url"], uploaded_url)
            self.assertEqual(listing["platforms"], ["ebay", "etsy", "facebook"])
            self.assertEqual(listing["platform"], "ebay")

            # 5. Verify platform filter works across multi-listed items
            # Filter by facebook -> should include this listing
            fb_res = self.client.get("/api/listings?platform=facebook")
            self.assertEqual(fb_res.status_code, 200)
            fb_ids = [l["id"] for l in fb_res.json().get("listings", [])]
            self.assertIn(listing_id, fb_ids)

            # Filter by etsy -> should include this listing
            etsy_res = self.client.get("/api/listings?platform=etsy")
            self.assertEqual(etsy_res.status_code, 200)
            etsy_ids = [l["id"] for l in etsy_res.json().get("listings", [])]
            self.assertIn(listing_id, etsy_ids)

            # Filter by poshmark -> should NOT include this listing yet
            posh_res = self.client.get("/api/listings?platform=poshmark")
            self.assertEqual(posh_res.status_code, 200)
            posh_ids = [l["id"] for l in posh_res.json().get("listings", [])]
            self.assertNotIn(listing_id, posh_ids)

            # 6. Update listing to also cross-list on Poshmark and update image via PUT
            new_image_url = "https://images.unsplash.com/photo-example"
            update_res = self.client.put(
                f"/api/listings/{listing_id}",
                json={
                    "platforms": ["ebay", "etsy", "facebook", "poshmark"],
                    "image_url": new_image_url,
                },
            )
            self.assertEqual(update_res.status_code, 200)
            up_listing = update_res.json().get("listing", {})
            self.assertEqual(up_listing["platforms"], ["ebay", "etsy", "facebook", "poshmark"])
            self.assertEqual(up_listing["image_url"], new_image_url)

            # Now filter by poshmark -> should include this listing
            posh_res2 = self.client.get("/api/listings?platform=poshmark")
            self.assertEqual(posh_res2.status_code, 200)
            posh_ids2 = [l["id"] for l in posh_res2.json().get("listings", [])]
            self.assertIn(listing_id, posh_ids2)

        finally:
            if listing_id:
                self.client.delete(f"/api/listings/{listing_id}")
            if os.path.exists(local_path):
                os.remove(local_path)

    def test_16_metrics_time_range_filtering(self):
        """Verify top metrics can be filtered by 7 days, 30 days, and all time."""
        from datetime import datetime, timedelta
        # 1. Login as admin
        self.client.post(
            "/api/auth/login",
            json={"username": config.ADMIN_USERNAME, "password": config.ADMIN_PASSWORD},
        )

        db = SessionLocal()
        created_ids = []
        try:
            # Create a recent sale (< 7 days ago, e.g. 2 days ago)
            recent_sold = Listing(
                platform="local",
                platform_listing_id="TEST-METRIC-RECENT",
                title="Recent Sale Controller",
                price_cents=10000,  # $100.00
                purchase_price_cents=4000,  # $40.00
                parts_cost_cents=1000,  # $10.00
                status="sold",
                is_sold=True,
                sold_at=datetime.utcnow() - timedelta(days=2),
                created_at=datetime.utcnow() - timedelta(days=2),
                updated_at=datetime.utcnow() - timedelta(days=2),
            )
            # Create a mid-range sale (between 7 and 30 days ago, e.g. 15 days ago)
            mid_sold = Listing(
                platform="local",
                platform_listing_id="TEST-METRIC-MID",
                title="Mid Sale Console",
                price_cents=20000,  # $200.00
                purchase_price_cents=10000,  # $100.00
                parts_cost_cents=2000,  # $20.00
                status="sold",
                is_sold=True,
                sold_at=datetime.utcnow() - timedelta(days=15),
                created_at=datetime.utcnow() - timedelta(days=15),
                updated_at=datetime.utcnow() - timedelta(days=15),
            )
            # Create an old sale (> 30 days ago, e.g. 45 days ago)
            old_sold = Listing(
                platform="local",
                platform_listing_id="TEST-METRIC-OLD",
                title="Old Sale Game",
                price_cents=5000,  # $50.00
                purchase_price_cents=1500,  # $15.00
                parts_cost_cents=500,  # $5.00
                status="sold",
                is_sold=True,
                sold_at=datetime.utcnow() - timedelta(days=45),
                created_at=datetime.utcnow() - timedelta(days=45),
                updated_at=datetime.utcnow() - timedelta(days=45),
            )
            db.add_all([recent_sold, mid_sold, old_sold])
            db.commit()
            created_ids = [recent_sold.id, mid_sold.id, old_sold.id]

            # 2. Query stats for 7 days
            res_7d = self.client.get("/api/stats?days=7")
            self.assertEqual(res_7d.status_code, 200)
            data_7d = res_7d.json()
            self.assertEqual(data_7d["period"], "7d")
            self.assertEqual(data_7d["days"], 7)

            # 3. Query stats for 30 days
            res_30d = self.client.get("/api/stats?days=30")
            self.assertEqual(res_30d.status_code, 200)
            data_30d = res_30d.json()
            self.assertEqual(data_30d["period"], "30d")
            self.assertEqual(data_30d["days"], 30)

            # 4. Query stats for all time
            res_all = self.client.get("/api/stats?days=all")
            self.assertEqual(res_all.status_code, 200)
            data_all = res_all.json()
            self.assertEqual(data_all["period"], "all")
            self.assertIsNone(data_all["days"])

            # 5. Verify monotonically increasing sold counts and profits:
            # 7d <= 30d <= all
            self.assertGreaterEqual(data_30d["sold_listings"], data_7d["sold_listings"])
            self.assertGreaterEqual(data_all["sold_listings"], data_30d["sold_listings"])
            self.assertGreaterEqual(data_30d["sold_revenue_cents"], data_7d["sold_revenue_cents"])
            self.assertGreaterEqual(data_all["sold_revenue_cents"], data_30d["sold_revenue_cents"])
            self.assertGreaterEqual(data_30d["sold_profit_cents"], data_7d["sold_profit_cents"])
            self.assertGreaterEqual(data_all["sold_profit_cents"], data_30d["sold_profit_cents"])

            # 6. Verify timeline structure and bucket counts for chart
            self.assertIn("timeline", data_7d)
            self.assertEqual(data_7d["timeline"]["type"], "daily")
            self.assertEqual(len(data_7d["timeline"]["points"]), 7)
            self.assertIn("revenue", data_7d["timeline"]["points"][0])
            self.assertIn("profit", data_7d["timeline"]["points"][0])
            self.assertIn("label", data_7d["timeline"]["points"][0])

            self.assertIn("timeline", data_30d)
            self.assertEqual(data_30d["timeline"]["type"], "daily")
            self.assertEqual(len(data_30d["timeline"]["points"]), 30)

            self.assertIn("timeline", data_all)
            self.assertEqual(data_all["timeline"]["type"], "monthly")
            self.assertGreaterEqual(len(data_all["timeline"]["points"]), 6)

        finally:
            for lid in created_ids:
                item = db.get(Listing, lid)
                if item:
                    db.delete(item)
            db.commit()
            db.close()

    def test_17_write_off_and_restore_listing(self):
        """Verify write-off and restore endpoints, status updates, and stats exclusion."""
        from datetime import datetime
        from src.database import SessionLocal
        from src.models import Listing

        # 1. Login as admin
        self.client.post(
            "/api/auth/login",
            json={"username": config.ADMIN_USERNAME, "password": config.ADMIN_PASSWORD},
        )

        db = SessionLocal()
        item = Listing(
            platform="local",
            platform_listing_id="TEST-WRITE-OFF-1",
            title="Broken Vintage Gameboy",
            price_cents=4500,  # $45.00
            purchase_price_cents=2000,  # $20.00
            parts_cost_cents=1000,  # $10.00
            status="active",
            is_sold=False,
            available_quantity=1,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        db.add(item)
        db.commit()
        listing_id = item.id
        db.close()

        try:
            # 2. Check stats before write-off
            stats_before = self.client.get("/api/stats").json()
            active_before = stats_before["active_listings"]

            # 3. Call write-off endpoint
            res_wo = self.client.post(f"/api/listings/{listing_id}/write-off")
            self.assertEqual(res_wo.status_code, 200)
            data_wo = res_wo.json()
            self.assertTrue(data_wo["ok"])
            listing_wo = data_wo["listing"]
            self.assertEqual(listing_wo["status"], "written_off")
            self.assertFalse(listing_wo["is_sold"])
            self.assertEqual(listing_wo["available_quantity"], 0)
            self.assertIsNotNone(listing_wo["written_off_at"])

            # 4. Check stats after write-off: active count decreased, written_off count increased
            stats_after = self.client.get("/api/stats").json()
            self.assertEqual(stats_after["active_listings"], active_before - 1)
            self.assertGreaterEqual(stats_after["written_off_listings"], 1)
            self.assertGreaterEqual(stats_after["written_off_cost_cents"], 3000)

            # 5. Filter listings by status=written_off
            res_filter = self.client.get("/api/listings?status=written_off")
            self.assertEqual(res_filter.status_code, 200)
            listings = res_filter.json().get("listings", [])
            self.assertTrue(any(l["id"] == listing_id for l in listings))

            # 6. Call restore endpoint
            res_res = self.client.post(f"/api/listings/{listing_id}/restore")
            self.assertEqual(res_res.status_code, 200)
            data_res = res_res.json()
            self.assertTrue(data_res["ok"])
            listing_res = data_res["listing"]
            self.assertEqual(listing_res["status"], "active")
            self.assertFalse(listing_res["is_sold"])
            self.assertGreaterEqual(listing_res["available_quantity"], 1)
            self.assertIsNone(listing_res["written_off_at"])

            # 7. Check stats after restore
            stats_restored = self.client.get("/api/stats").json()
            self.assertEqual(stats_restored["active_listings"], active_before)

        finally:
            db_clean = SessionLocal()
            del_item = db_clean.get(Listing, listing_id)
            if del_item:
                db_clean.delete(del_item)
                db_clean.commit()
            db_clean.close()


if __name__ == "__main__":
    unittest.main()


