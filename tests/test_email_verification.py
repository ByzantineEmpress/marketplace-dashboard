"""Email-verification tests for self-serve signup.

These drive the real endpoints. Outbound mail is captured through
``src.mailer.OUTBOX`` instead of being sent, so the tests assert on the actual
message contents — including the confirmation link — with no mail server and
no network.
"""
import os
import re
import secrets
import sys
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from starlette.testclient import TestClient
from src import mailer
from src.api import routes as routes_module
from src.api.main import app
from src.config import config
from src.database import SessionLocal, init_db
from src.models import AuthSession, PendingSignup, Team, TeamMembership, User


class EmailVerificationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()
        cls.client = TestClient(app)
        cls.client.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)

    def setUp(self):
        # Capture mail in-process.
        self.outbox = []
        mailer.OUTBOX = self.outbox
        # Signup/resend are rate limited per IP; each test starts clean.
        routes_module._rate_prune()
        routes_module.LOGIN_ATTEMPTS.clear()
        self.client.cookies.clear()
        self._cleanup_emails = []

    def tearDown(self):
        mailer.OUTBOX = None
        db = SessionLocal()
        try:
            for email in self._cleanup_emails:
                db.query(PendingSignup).filter(PendingSignup.email == email).delete()
                user = db.query(User).filter(User.email == email).first()
                if user:
                    db.query(AuthSession).filter(AuthSession.user_id == user.id).delete()
                    db.query(TeamMembership).filter(
                        TeamMembership.user_id == user.id).delete()
                    db.query(Team).filter(Team.created_by_email == email).delete()
                    db.delete(user)
            db.commit()
        finally:
            db.close()
        routes_module._rate_prune()
        routes_module.LOGIN_ATTEMPTS.clear()

    # -- helpers ----------------------------------------------------------

    def _csrf(self, path):
        html = self.client.get(path).text
        m = re.search(r'name="csrf_token"\s+value="([^"]+)"', html)
        self.assertIsNotNone(m, f"no csrf token on {path}")
        return m.group(1)

    def _signup(self, email, password="verify-me-password", name="Verify Person",
                follow_redirects=False):
        self._cleanup_emails.append(email)
        return self.client.post("/api/auth/signup", data={
            "csrf_token": self._csrf("/signup"),
            "email": email, "name": name,
            "password": password, "confirm_password": password,
        }, follow_redirects=follow_redirects)

    def _link_token(self, mail):
        m = re.search(r"/verify-email\?token=([A-Za-z0-9_\-%]+)", mail["body"])
        self.assertIsNotNone(m, "no verification link in the email body")
        return m.group(1)

    # -- tests ------------------------------------------------------------

    def test_signup_creates_nothing_until_the_link_is_clicked(self):
        """The core promise: no user, team or membership before verification."""
        email = f"pending_{secrets.token_hex(4)}@example.com"

        res = self._signup(email)
        self.assertEqual(res.status_code, 303)
        self.assertIn("/verify-pending", res.headers["location"])
        # No session is issued — you cannot be "logged in" while unverified.
        self.assertNotIn("auth_token=", res.headers.get("set-cookie", ""))

        db = SessionLocal()
        try:
            self.assertIsNone(db.query(User).filter(User.email == email).first(),
                              "a user row was created before verification")
            pending = db.query(PendingSignup).filter(PendingSignup.email == email).first()
            self.assertIsNotNone(pending, "no pending signup was recorded")
            self.assertGreater(pending.expires_at, datetime.utcnow())
            # Token is stored only as a digest.
            self.assertEqual(len(pending.token_hash), 64)
        finally:
            db.close()

        # A confirmation email was captured, addressed to the signup address.
        self.assertEqual(len(self.outbox), 1)
        mail = self.outbox[0]
        self.assertEqual(mail["to"], email)
        self.assertIn("Confirm your email", mail["subject"])
        self.assertIn("60 minutes", mail["body"])

    def test_raw_token_is_never_stored(self):
        """A database dump must not yield a usable confirmation link."""
        email = f"nodump_{secrets.token_hex(4)}@example.com"
        self._signup(email)
        token = self._link_token(self.outbox[0])

        db = SessionLocal()
        try:
            pending = db.query(PendingSignup).filter(PendingSignup.email == email).first()
            self.assertNotEqual(pending.token_hash, token)
            self.assertNotIn(token, pending.token_hash)
        finally:
            db.close()

    def test_verification_creates_the_account_and_consumes_the_token(self):
        email = f"verify_{secrets.token_hex(4)}@example.com"
        password = "verify-me-password"
        self._signup(email, password=password)
        token = self._link_token(self.outbox[0])

        res = self.client.get(f"/verify-email?token={token}", follow_redirects=False)
        self.assertEqual(res.status_code, 302)
        self.assertIn("verified=1", res.headers["location"])

        db = SessionLocal()
        try:
            user = db.query(User).filter(User.email == email).first()
            self.assertIsNotNone(user, "verification did not create the account")
            self.assertEqual(user.provider, "password")
            self.assertFalse(bool(user.is_admin))
            # The pending row is consumed — single use.
            self.assertIsNone(
                db.query(PendingSignup).filter(PendingSignup.email == email).first())
            # Still no team: onboarding has not run yet.
            self.assertEqual(
                db.query(TeamMembership).filter(TeamMembership.user_id == user.id).count(),
                0)
        finally:
            db.close()

        # The link cannot be replayed.
        again = self.client.get(f"/verify-email?token={token}", follow_redirects=False)
        self.assertEqual(again.status_code, 302)
        self.assertIn("error=", again.headers["location"])

        # And the new account can actually sign in and is sent to onboarding.
        self.client.cookies.clear()
        login = self.client.post("/api/auth/login",
                                 json={"username": email, "password": password})
        self.assertEqual(login.status_code, 200)
        self.assertTrue(login.json()["ok"])
        onb = self.client.get("/onboarding", follow_redirects=False)
        self.assertEqual(onb.status_code, 200)

    def test_expired_token_is_refused_and_cleaned_up(self):
        email = f"expired_{secrets.token_hex(4)}@example.com"
        self._signup(email)
        token = self._link_token(self.outbox[0])

        db = SessionLocal()
        try:
            pending = db.query(PendingSignup).filter(PendingSignup.email == email).first()
            pending.expires_at = datetime.utcnow() - timedelta(minutes=1)
            db.commit()
        finally:
            db.close()

        res = self.client.get(f"/verify-email?token={token}", follow_redirects=False)
        self.assertEqual(res.status_code, 302)
        self.assertIn("expired=1", res.headers["location"])

        db = SessionLocal()
        try:
            self.assertIsNone(db.query(User).filter(User.email == email).first(),
                              "an expired link created an account")
            self.assertIsNone(
                db.query(PendingSignup).filter(PendingSignup.email == email).first(),
                "expired pending signup was not cleaned up")
        finally:
            db.close()

    def test_bogus_token_is_refused(self):
        res = self.client.get("/verify-email?token=" + secrets.token_urlsafe(32),
                              follow_redirects=False)
        self.assertEqual(res.status_code, 302)
        self.assertIn("error=", res.headers["location"])
        no_token = self.client.get("/verify-email", follow_redirects=False)
        self.assertEqual(no_token.status_code, 302)
        self.assertIn("error=", no_token.headers["location"])

    def test_existing_account_email_cannot_be_signed_up_again(self):
        """Re-signup for a known address must not disclose or overwrite it."""
        email = f"taken_{secrets.token_hex(4)}@example.com"
        db = SessionLocal()
        try:
            db.add(User(email=email, name="Existing", provider="google"))
            db.commit()
        finally:
            db.close()
        self._cleanup_emails.append(email)

        res = self._signup(email)
        self.assertEqual(res.status_code, 303)
        self.assertIn("error=", res.headers["location"])
        self.assertEqual(len(self.outbox), 0, "no mail should be sent for a taken address")

    def test_resend_is_rate_limited_and_issues_a_fresh_token(self):
        email = f"resend_{secrets.token_hex(4)}@example.com"
        self._signup(email)
        first_token = self._link_token(self.outbox[0])

        # The resend endpoint rotates the token.
        resend = self.client.post("/api/auth/resend-verification",
                                  data={"email": email}, follow_redirects=False)
        self.assertEqual(resend.status_code, 303)
        self.assertEqual(len(self.outbox), 2)
        second_token = self._link_token(self.outbox[1])
        self.assertNotEqual(first_token, second_token)

        db = SessionLocal()
        try:
            pending = db.query(PendingSignup).filter(PendingSignup.email == email).first()
            self.assertEqual(pending.send_count, 2)
        finally:
            db.close()

        # The previous link is now dead, the new one works.
        stale = self.client.get(f"/verify-email?token={first_token}", follow_redirects=False)
        self.assertIn("error=", stale.headers["location"])
        fresh = self.client.get(f"/verify-email?token={second_token}", follow_redirects=False)
        self.assertIn("verified=1", fresh.headers["location"])

    def test_resend_is_capped_per_address(self):
        from src.email_verification import MAX_VERIFICATION_SENDS
        email = f"cap_{secrets.token_hex(4)}@example.com"
        self._signup(email)

        # Burn the per-address hourly allowance, then expect 429.
        routes_module._rate_prune()
        codes = []
        for _ in range(MAX_VERIFICATION_SENDS + 2):
            r = self.client.post("/api/auth/resend-verification",
                                 json={"email": email})
            codes.append(r.status_code)
        self.assertIn(429, codes[-2:] or [None],
                      f"resend was never rate limited: {codes}")

    def test_unknown_address_gets_the_same_answer_on_resend(self):
        """Resend must not reveal whether a signup exists."""
        known = f"known_{secrets.token_hex(4)}@example.com"
        self._signup(known)
        routes_module._rate_prune()

        a = self.client.post("/api/auth/resend-verification", json={"email": known})
        routes_module._rate_prune()
        b = self.client.post("/api/auth/resend-verification",
                             json={"email": "definitely.not.here@example.com"})
        self.assertEqual(a.status_code, b.status_code)
        self.assertEqual(a.json(), b.json())

    def test_verification_rejected_when_mail_is_not_configured(self):
        """With no SMTP, signup must fail loudly rather than pretend."""
        mailer.OUTBOX = None  # force the real send path
        original = (config.SMTP_HOST, config.SMTP_USER, config.SMTP_FROM)
        config.SMTP_HOST = ""
        config.SMTP_USER = ""
        config.SMTP_FROM = ""
        try:
            email = f"nosmtp_{secrets.token_hex(4)}@example.com"
            self._cleanup_emails.append(email)
            res = self.client.post("/api/auth/signup", data={
                "csrf_token": self._csrf("/signup"),
                "email": email, "name": "No SMTP",
                "password": "some-password-1", "confirm_password": "some-password-1",
            }, follow_redirects=False)
            self.assertEqual(res.status_code, 303)
            self.assertIn("error=", res.headers["location"])
            self.assertIn("/signup", res.headers["location"])
            db = SessionLocal()
            try:
                self.assertIsNone(db.query(User).filter(User.email == email).first())
            finally:
                db.close()
        finally:
            config.SMTP_HOST, config.SMTP_USER, config.SMTP_FROM = original
            mailer.OUTBOX = self.outbox

    def test_weak_password_still_rejected_before_any_mail(self):
        email = f"weak_{secrets.token_hex(4)}@example.com"
        self._cleanup_emails.append(email)
        res = self.client.post("/api/auth/signup", data={
            "csrf_token": self._csrf("/signup"),
            "email": email, "name": "Weak",
            "password": "short", "confirm_password": "short",
        }, follow_redirects=False)
        self.assertEqual(res.status_code, 303)
        self.assertIn("error=", res.headers["location"])
        self.assertEqual(len(self.outbox), 0, "no mail for a rejected signup")

        db = SessionLocal()
        try:
            self.assertIsNone(
                db.query(PendingSignup).filter(PendingSignup.email == email).first())
        finally:
            db.close()

    # -- message presentation -------------------------------------------

    def test_verification_email_is_multipart_with_a_button(self):
        """The email must carry both parts, with a working call to action."""
        email = f"format_{secrets.token_hex(4)}@example.com"
        self._signup(email, name="Format Person")
        self.assertEqual(len(self.outbox), 1)
        mail = self.outbox[0]

        # Both representations exist.
        self.assertIn("Confirm your email address", mail["body"])
        self.assertTrue(mail["html_body"], "no HTML part was produced")
        html = mail["html_body"]

        # It is a real HTML document with a clickable CTA, not a bare link.
        self.assertIn("<!DOCTYPE html>", html)
        self.assertIn("<a href=", html)
        token_url = re.search(r"/verify-email\?token=[A-Za-z0-9_\-%]+", mail["body"])
        self.assertIsNotNone(token_url)
        self.assertIn(token_url.group(0), html, "the CTA link is missing from the HTML part")

        # And the plain-text part still carries the raw link for text clients.
        self.assertIn("http", mail["body"])

    def test_sender_has_a_display_name_not_a_bare_address(self):
        original = config.MAIL_FROM
        config.MAIL_FROM = f"{config.APP_NAME} <noreply@send.example.com>"
        try:
            email = f"from_{secrets.token_hex(4)}@example.com"
            self._signup(email)
            mail = self.outbox[0]
            # Recipients should see a name, not just an address.
            self.assertEqual(mail["from"], f"{config.APP_NAME} <noreply@send.example.com>")
            # Provider APIs need the bare address for the envelope.
            self.assertEqual(mailer.from_email(), "noreply@send.example.com")
            self.assertEqual(mailer.from_display_name(), config.APP_NAME)
        finally:
            config.MAIL_FROM = original

    def test_recipient_name_cannot_inject_html(self):
        """The display name is attacker-controlled at signup; escape it."""
        email = f"escape_{secrets.token_hex(4)}@example.com"
        payload = '<script>alert(1)</script><img src=x onerror=alert(2)>'
        self._signup(email, name=payload)
        html = self.outbox[0]["html_body"]

        # No injected tags survive.
        self.assertNotIn("<script>", html)
        self.assertNotIn("<img", html)
        self.assertIn("&lt;script&gt;", html)
        # The text is still present, just inert.
        self.assertIn("alert(1)", html)

    def test_html_body_escapes_name_when_absent(self):
        """A signup with no name must still render a valid greeting."""
        email = f"noname_{secrets.token_hex(4)}@example.com"
        self._signup(email, name="")
        html = self.outbox[0]["html_body"]
        self.assertIn("Hi,", html)
        self.assertNotIn("<script", html)

    # -- login page wording ------------------------------------------------

    def test_login_page_asks_for_the_email_address(self):
        """Signup collects an email, so login must not say 'Username'.

        This was a real bug: people who signed up with email+password typed
        something else into a field labelled "Username" and got a bare
        "Invalid credentials".
        """
        res = self.client.get("/login")
        self.assertEqual(res.status_code, 200)
        self.assertIn("Email address", res.text)
        self.assertIn("you@example.com", res.text)
        # The hint must mention the admin case, since that account really does
        # sign in with a username.
        self.assertIn("administrator signs", res.text)

    def test_login_page_prefills_the_verified_address(self):
        """After verifying, the address should already be filled in."""
        res = self.client.get("/login?verified=1&email=someone%40example.com")
        self.assertEqual(res.status_code, 200)
        self.assertIn('value="someone@example.com"', res.text)
        self.assertIn("email address is confirmed", res.text)

    def test_login_page_escapes_the_prefilled_address(self):
        """The prefill reflects a query parameter — it must be escaped."""
        from urllib.parse import quote

        nasty = '" onfocus="alert(1)" autofocus x="'
        res = self.client.get(f"/login?verified=1&email={quote(nasty)}")
        self.assertEqual(res.status_code, 200)
        # The attribute must not be broken out of.
        self.assertNotIn('onfocus="alert(1)"', res.text)

    def test_failed_form_login_explains_what_to_try(self):
        """A generic 401 is unhelpful; the form path should guide the user."""
        res = self.client.post(
            "/api/auth/login",
            data={"username": "nobody@example.com", "password": "wrong"},
            follow_redirects=False,
        )
        self.assertEqual(res.status_code, 303)
        location = res.headers["location"]
        self.assertIn("error=", location)
        # Still must not reveal whether the address exists.
        self.assertNotIn("no+such", location.lower())
        self.assertNotIn("not+found", location.lower())
        self.assertIn("Google", location)

    # -- proxy / rate-limit correctness ------------------------------------

    def test_client_ip_prefers_cf_connecting_ip(self):
        """Behind Cloudflare every request shares one edge IP.

        If the app keyed rate limits on the raw socket address, all visitors
        would share a single bucket — one abuser could exhaust it for everyone,
        and everyone would throttle each other.
        """
        from src.api.routes import client_ip

        class FakeRequest:
            def __init__(self, headers, host="203.0.113.9"):
                self.headers = headers
                self.client = type("C", (), {"host": host})()

        # Cloudflare's header wins over both other sources.
        self.assertEqual(
            client_ip(FakeRequest({"cf-connecting-ip": "198.51.100.7",
                                   "x-forwarded-for": "10.0.0.1"})),
            "198.51.100.7",
        )
        # Falls back to the first (left-most) XFF entry, ignoring spoofable rest.
        self.assertEqual(
            client_ip(FakeRequest({"x-forwarded-for": "198.51.100.8, 10.0.0.1, 10.0.0.2"})),
            "198.51.100.8",
        )
        # X-Real-IP next.
        self.assertEqual(
            client_ip(FakeRequest({"x-real-ip": "198.51.100.9"})), "198.51.100.9"
        )
        # Finally the socket peer.
        self.assertEqual(client_ip(FakeRequest({})), "203.0.113.9")
        # Never returns an empty string (that would collapse every bucket).
        self.assertEqual(client_ip(FakeRequest({"cf-connecting-ip": "  "})), "203.0.113.9")

    def test_signup_rate_limit_is_per_client_not_per_proxy(self):
        """Two CF-Connecting-IPs must not share one signup bucket."""
        routes_module._rate_prune()
        codes = []
        # Same socket peer implied, different real clients.
        for i, ip in enumerate(("198.51.100.10", "198.51.100.11", "198.51.100.12")):
            email = f"perip_{i}_{secrets.token_hex(3)}@example.com"
            self._cleanup_emails.append(email)
            res = self.client.post(
                "/api/auth/signup",
                data={"csrf_token": self._csrf("/signup"), "email": email,
                      "name": "Per IP", "password": "per-ip-password-1",
                      "confirm_password": "per-ip-password-1"},
                headers={"CF-Connecting-IP": ip},
                follow_redirects=False,
            )
            codes.append(res.status_code)
            self.assertIn("/verify-pending", res.headers.get("location", ""),
                          f"client {ip} was throttled as if it shared an IP")
        self.assertTrue(all(c == 303 for c in codes), codes)


if __name__ == "__main__":
    unittest.main(verbosity=2)
