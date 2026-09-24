"""Security audit integration tests.

Verifies hardening fixes implemented against OWASP Top 10 vulnerabilities:
- A01: Broken Access Control (Admin RBAC, IDOR on team deletion, Dev mode bypass)
- A02: Cryptographic Failures & Secret Masking (Masking API secrets/passwords, secure cookies)
- A03: Injection & Stored XSS (Upload restrictions: no SVG, magic bytes check)
- A04: Insecure Design (Upload size limits, DoS protection)
- A05: Security Misconfiguration (Security headers: CSP, nosniff, frame-options)
- A07: Identification and Authentication Failures (Login rate limiting, constant-time comparison)
"""

import io
import os
import sys
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from starlette.testclient import TestClient
from src.api.main import app
from src.config import config
from src.database import init_db, SessionLocal
from src.models import User, Team, TeamMembership, AuthSession, Listing


class SecurityAuditTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()
        cls._client_cm = TestClient(app)
        cls.client = cls._client_cm.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls._client_cm.__exit__(None, None, None)

    def setUp(self):
        self.db = SessionLocal()
        # Ensure admin user exists and is flagged as admin
        admin_email = f"{config.ADMIN_USERNAME}@local"
        self.admin_user = self.db.query(User).filter(User.email == admin_email).first()
        if not self.admin_user:
            self.admin_user = User(
                email=admin_email,
                name=config.ADMIN_USERNAME,
                provider="local",
                is_admin=True,
            )
            self.db.add(self.admin_user)
            self.db.commit()
            self.db.refresh(self.admin_user)

        # Create or fetch a non-admin user
        self.non_admin_user = self.db.query(User).filter(User.email == "regular_user@example.com").first()
        if not self.non_admin_user:
            self.non_admin_user = User(
                email="regular_user@example.com",
                name="Regular User",
                provider="google",
                is_admin=False,
            )
            self.db.add(self.non_admin_user)
            self.db.commit()
            self.db.refresh(self.non_admin_user)

    def tearDown(self):
        self.db.close()

    def _login_as(self, user: User) -> str:
        """Helper to create an active session for a specific user and set cookie."""
        import secrets
        token = secrets.token_hex(32)
        sess = AuthSession(
            token=token,
            user_id=user.id,
            expires_at=datetime.utcnow() + timedelta(days=1),
        )
        self.db.add(sess)
        self.db.commit()
        return token

    # ----------------------------------------------------------------------
    # 1. Admin RBAC & Access Control (OWASP A01)
    # ----------------------------------------------------------------------
    def test_admin_rbac_settings(self):
        """Verify non-admin users receive 403 Forbidden on settings endpoints."""
        token = self._login_as(self.non_admin_user)
        self.client.cookies.set("auth_token", token)

        # GET /api/settings should be forbidden for regular members
        res = self.client.get("/api/settings")
        self.assertEqual(res.status_code, 403)

        # POST /api/settings should be forbidden
        res = self.client.post("/api/settings", json={"APP_NAME": "Hacked"})
        self.assertEqual(res.status_code, 403)

        # POST /api/accounts/connect should be forbidden
        res = self.client.post("/api/accounts/connect", json={"platform": "ebay"})
        self.assertEqual(res.status_code, 403)

        # POST /api/accounts/sync should be forbidden
        res = self.client.post("/api/accounts/sync", json={"platform": "ebay"})
        self.assertEqual(res.status_code, 403)

        # Now test with admin user
        admin_token = self._login_as(self.admin_user)
        self.client.cookies.set("auth_token", admin_token)

        res = self.client.get("/api/settings")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertIn("settings", data)

    # ----------------------------------------------------------------------
    # 2. Secret Masking & Protection Against Overwrites (OWASP A02)
    # ----------------------------------------------------------------------
    def test_secret_masking(self):
        """Verify sensitive credentials are masked in GET and not overwritten in POST."""
        admin_token = self._login_as(self.admin_user)
        self.client.cookies.set("auth_token", admin_token)

        res = self.client.get("/api/settings")
        self.assertEqual(res.status_code, 200)
        settings = res.json().get("settings", {})
        
        # Passwords / secrets should be masked
        self.assertEqual(settings.get("ADMIN_PASSWORD"), "••••••••")

        # Submitting masked value should NOT overwrite the real password
        orig_pw = config.ADMIN_PASSWORD
        res = self.client.post("/api/settings", json={"ADMIN_PASSWORD": "••••••••"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(config.ADMIN_PASSWORD, orig_pw)

    # ----------------------------------------------------------------------
    # 3. Team Member Deletion Authorization / IDOR Protection (OWASP A01)
    # ----------------------------------------------------------------------
    def test_team_member_deletion_auth(self):
        """Verify ordinary members cannot delete other members from a team."""
        import secrets
        team_name = f"SecTeam_{secrets.token_hex(4)}"
        team = Team(name=team_name, invite_code=secrets.token_hex(8))
        self.db.add(team)
        self.db.flush()

        # admin is owner
        self.db.add(TeamMembership(team_id=team.id, user_id=self.admin_user.id, role="owner"))
        # regular member
        self.db.add(TeamMembership(team_id=team.id, user_id=self.non_admin_user.id, role="member"))
        
        # 3rd user
        victim_email = f"victim_{secrets.token_hex(4)}@example.com"
        victim = User(email=victim_email, name="Victim", provider="google", is_admin=False)
        self.db.add(victim)
        self.db.flush()
        self.db.add(TeamMembership(team_id=team.id, user_id=victim.id, role="member"))
        self.db.commit()

        # Regular user tries to delete victim
        reg_token = self._login_as(self.non_admin_user)
        self.client.cookies.set("auth_token", reg_token)
        res = self.client.delete(f"/api/teams/{team.id}/members/{victim.id}")
        self.assertEqual(res.status_code, 403)

        # Regular user CAN remove themselves (leave team)
        res = self.client.delete(f"/api/teams/{team.id}/members/{self.non_admin_user.id}")
        self.assertEqual(res.status_code, 200)

        # Admin user CAN delete victim
        admin_token = self._login_as(self.admin_user)
        self.client.cookies.set("auth_token", admin_token)
        res = self.client.delete(f"/api/teams/{team.id}/members/{victim.id}")
        self.assertEqual(res.status_code, 200)

    # ----------------------------------------------------------------------
    # 4. File Upload Hardening: No SVG, Size Limit, Magic Bytes (OWASP A03 / A04)
    # ----------------------------------------------------------------------
    def test_file_upload_security(self):
        """Verify SVG rejection, size limits, and image signature checks."""
        admin_token = self._login_as(self.admin_user)
        self.client.cookies.set("auth_token", admin_token)

        # 1. Reject SVG file (Stored XSS vector)
        svg_content = b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert('XSS')</script></svg>"
        res = self.client.post(
            "/api/upload",
            files={"file": ("malicious.svg", svg_content, "image/svg+xml")},
        )
        self.assertEqual(res.status_code, 400)
        self.assertIn("Unsupported image extension", res.json().get("error", ""))

        # 2. Reject file disguised as PNG with invalid magic bytes (e.g. PHP/HTML)
        fake_png = b"<?php echo 'malware'; ?>"
        res = self.client.post(
            "/api/upload",
            files={"file": ("fake.png", fake_png, "image/png")},
        )
        self.assertEqual(res.status_code, 400)
        self.assertIn("File content does not match a valid image format", res.json().get("error", ""))

        # 3. Reject file exceeding 15MB limit
        huge_file = b"\x89PNG\r\n\x1a\n" + b"\x00" * (16 * 1024 * 1024)
        res = self.client.post(
            "/api/upload",
            files={"file": ("huge.png", huge_file, "image/png")},
        )
        self.assertEqual(res.status_code, 413)

        # 4. Accept valid PNG file
        valid_png = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + b"\x00" * 20
        res = self.client.post(
            "/api/upload",
            files={"file": ("photo.png", valid_png, "image/png")},
        )
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json().get("ok"))
        self.assertTrue(res.json().get("url", "").startswith("/static/uploads/"))

    # ----------------------------------------------------------------------
    # 5. Security Headers (OWASP A05)
    # ----------------------------------------------------------------------
    def test_security_headers_present(self):
        """Verify OWASP-compliant security headers on all responses."""
        res = self.client.get("/login")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.headers.get("x-content-type-options"), "nosniff")
        self.assertEqual(res.headers.get("x-frame-options"), "DENY")
        self.assertIn("Content-Security-Policy", res.headers)
        self.assertIn("default-src 'self'", res.headers["Content-Security-Policy"])

    # ----------------------------------------------------------------------
    # 6. Google Dev Mode Isolation (OWASP A01)
    # ----------------------------------------------------------------------
    def test_google_dev_mode_isolation(self):
        """Verify dev picker and dev login are forbidden when GOOGLE_DEV_MODE is disabled."""
        orig_mode = config.GOOGLE_DEV_MODE
        try:
            config.GOOGLE_DEV_MODE = False
            res = self.client.get("/auth/google/dev-picker")
            self.assertEqual(res.status_code, 403)

            res = self.client.post("/auth/google/dev-login", data={"email": "hacker@test.com"})
            self.assertEqual(res.status_code, 403)
        finally:
            config.GOOGLE_DEV_MODE = orig_mode

    # ----------------------------------------------------------------------
    # 7. Login Rate Limiting (OWASP A07)
    # ----------------------------------------------------------------------
    def test_login_rate_limiting(self):
        """Verify login is rate limited after 5 failed attempts."""
        from src.api.routes import LOGIN_ATTEMPTS
        LOGIN_ATTEMPTS.clear()

        # Send 5 incorrect attempts
        for _ in range(5):
            res = self.client.post("/api/auth/login", json={"username": "admin", "password": "wrongpassword"})
            self.assertEqual(res.status_code, 401)

        # 6th attempt should be rate limited with 429
        res = self.client.post("/api/auth/login", json={"username": "admin", "password": "wrongpassword"})
        self.assertEqual(res.status_code, 429)
        self.assertIn("Too many failed login attempts", res.json().get("error", ""))

        LOGIN_ATTEMPTS.clear()

    # ----------------------------------------------------------------------
    # 8. Last Owner Deletion Protection (OWASP A01)
    # ----------------------------------------------------------------------
    def test_cannot_remove_last_owner(self):
        """Verify the last owner of a team cannot be removed."""
        import secrets
        team_name = f"SoloOwnerTeam_{secrets.token_hex(4)}"
        team = Team(name=team_name, invite_code=secrets.token_hex(8))
        self.db.add(team)
        self.db.flush()
        self.db.add(TeamMembership(team_id=team.id, user_id=self.admin_user.id, role="owner"))
        self.db.commit()

        admin_token = self._login_as(self.admin_user)
        self.client.cookies.set("auth_token", admin_token)

        res = self.client.delete(f"/api/teams/{team.id}/members/{self.admin_user.id}")
        self.assertEqual(res.status_code, 400)
        self.assertIn("Cannot remove the last owner", res.json().get("error", ""))


if __name__ == "__main__":
    unittest.main()

