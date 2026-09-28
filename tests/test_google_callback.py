"""Exercise the REAL Google OAuth callback without contacting Google.

The live callback exchanges a code for a token and fetches the userinfo
profile before routing the user. That path had no automated coverage, so a
regression there would only show up in a browser. This stubs httpx so the
whole callback body runs for real.
"""
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from starlette.testclient import TestClient
from src.api.main import app
from src.config import config
from src.database import SessionLocal
from src.models import AuthSession, TeamMembership, User


class FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


def make_fake_client(token_payload, userinfo_payload):
    """Return a patch target whose AsyncClient mimics Google's endpoints."""
    async def fake_post(url, data=None, **kwargs):
        return FakeResponse(200, token_payload)

    async def fake_get(url, headers=None, **kwargs):
        return FakeResponse(200, userinfo_payload)

    instance = MagicMock()
    instance.post = AsyncMock(side_effect=fake_post)
    instance.get = AsyncMock(side_effect=fake_get)

    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=instance)
    cm.__aexit__ = AsyncMock(return_value=False)

    factory = MagicMock(return_value=cm)
    return factory


class GoogleCallbackRoutingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from src.database import init_db
        init_db()
        cls.client = TestClient(app)
        cls.client.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)

    def _callback(self, email, name="Callback Person"):
        """Drive GET /auth/google/callback with stubbed Google responses."""
        cls = type(self)
        client = cls.client
        # Establish the CSRF state cookie the callback expects.
        start = client.get("/auth/google?live=1", follow_redirects=False)
        state = start.cookies.get("google_oauth_state")
        self.assertTrue(state, "no oauth state cookie was set")
        factory = make_fake_client(
            {"access_token": "fake-access-token"},
            {"email": email, "email_verified": True, "name": name},
        )
        with patch("src.api.routes.httpx.AsyncClient", factory):
            return client.get(
                f"/auth/google/callback?code=fake-code&state={state}",
                follow_redirects=False,
            )

    def test_new_google_user_is_sent_to_onboarding_not_into_a_team(self):
        """A first-time Google sign-in must not land in anyone else's team."""
        email = "callback.brandnew@example.com"
        db = SessionLocal()
        try:
            existing = db.query(User).filter(User.email == email).first()
            if existing:
                db.query(TeamMembership).filter(
                    TeamMembership.user_id == existing.id).delete()
                db.query(AuthSession).filter(
                    AuthSession.user_id == existing.id).delete()
                db.delete(existing)
                db.commit()
        finally:
            db.close()

        self.client.cookies.clear()
        res = self._callback(email, "Brand New")
        self.assertEqual(res.status_code, 303)
        self.assertEqual(res.headers["location"], "/onboarding",
                         "new Google user should be routed to onboarding")
        # Session cookie must be usable
        set_cookie = res.headers.get("set-cookie", "")
        self.assertIn("auth_token=", set_cookie)
        self.assertIn("samesite=lax", set_cookie.lower())

        db = SessionLocal()
        try:
            user = db.query(User).filter(User.email == email).first()
            self.assertIsNotNone(user, "callback did not create the user")
            self.assertEqual(user.provider, "google")
            self.assertEqual(
                db.query(TeamMembership).filter(TeamMembership.user_id == user.id).count(),
                0,
                "new Google sign-in must not be enrolled in any team",
            )
            # Cleanup
            db.query(AuthSession).filter(AuthSession.user_id == user.id).delete()
            db.delete(user)
            db.commit()
        finally:
            db.close()

    def test_returning_google_user_with_a_team_goes_to_dashboard(self):
        """A user who already owns a team skips onboarding."""
        email = "callback.returning@example.com"
        db = SessionLocal()
        try:
            user = db.query(User).filter(User.email == email).first()
            if user is None:
                user = User(email=email, name="Returning", provider="google")
                db.add(user)
                db.flush()
            if db.query(TeamMembership).filter(TeamMembership.user_id == user.id).count() == 0:
                from src.models import Team
                team = Team(name="Callback Returning Team")
                db.add(team)
                db.flush()
                db.add(TeamMembership(team_id=team.id, user_id=user.id, role="owner"))
            db.commit()
            user_id = user.id
        finally:
            db.close()

        self.client.cookies.clear()
        res = self._callback(email, "Returning")
        self.assertEqual(res.status_code, 303)
        self.assertEqual(res.headers["location"], "/dashboard")

        db = SessionLocal()
        try:
            from src.models import Team
            db.query(TeamMembership).filter(TeamMembership.user_id == user_id).delete()
            db.query(AuthSession).filter(AuthSession.user_id == user_id).delete()
            db.query(Team).filter(Team.name == "Callback Returning Team").delete()
            db.query(User).filter(User.id == user_id).delete()
            db.commit()
        finally:
            db.close()

    def test_unverified_google_email_is_refused(self):
        """An unverified address must not create an account."""
        email = "callback.unverified@example.com"
        self.client.cookies.clear()
        start = self.client.get("/auth/google?live=1", follow_redirects=False)
        state = start.cookies.get("google_oauth_state")
        factory = make_fake_client(
            {"access_token": "fake"},
            {"email": email, "email_verified": False, "name": "Unverified"},
        )
        with patch("src.api.routes.httpx.AsyncClient", factory):
            res = self.client.get(
                f"/auth/google/callback?code=c&state={state}", follow_redirects=False)
        self.assertEqual(res.status_code, 303)
        self.assertIn("/login?error=", res.headers["location"])
        self.assertIn("not+verified", res.headers["location"].replace("%20", "+").lower())

        db = SessionLocal()
        try:
            self.assertIsNone(db.query(User).filter(User.email == email).first(),
                              "unverified email must not create a user")
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
