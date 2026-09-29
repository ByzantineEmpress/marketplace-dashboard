"""Marketplace OAuth: PKCE, state validation and the connect handshake.

Why this file exists: a user saved their Etsy keystring and shared secret, then
pressed Sync and was told "No valid access token — connect the account first".
Nothing in the UI could perform that connection, and even if it had, two
independent bugs would have broken it:

  * Etsy requires PKCE on every authorization flow. The adapter sent no
    ``code_challenge``, so the authorization request could never succeed.
  * Etsy requires ``x-api-key: <keystring>:<shared_secret>`` on every request.
    The adapter sent only the keystring.

Each test below pins one of those properties so they cannot regress quietly.
"""

import os
import secrets
import sys
import unittest
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Must precede the src imports: it fixes the ambient configuration the app
# reads at import time. See tests/_env.py for why that matters.
try:
    from tests import _env  # noqa: E402,F401  isort:skip
except ImportError:  # `tests` resolved to the directory, not the package
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import _env  # noqa: E402,F401  isort:skip


from starlette.testclient import TestClient
from src import oauth_pkce
from src.api.main import app
from src.database import init_db, SessionLocal
from src.models import AuthSession, User, UserMarketplaceCredential


class PkceTest(unittest.TestCase):
    """The PKCE primitive itself."""

    def test_challenge_matches_the_rfc7636_test_vector(self):
        """Appendix B of RFC 7636 gives a verifier and its S256 challenge.

        Using the published vector is stronger than checking our own round trip,
        which would pass even if we hashed the wrong thing consistently.
        """
        verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
        expected = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
        self.assertEqual(oauth_pkce.challenge_for(verifier), expected)

    def test_challenge_is_unpadded_base64url(self):
        challenge = oauth_pkce.challenge_for(oauth_pkce.new_verifier())
        self.assertNotIn("=", challenge)
        self.assertNotIn("+", challenge)
        self.assertNotIn("/", challenge)
        self.assertEqual(len(challenge), 43)  # sha256 -> 32 bytes -> 43 chars

    def test_verifier_is_within_the_permitted_length_range(self):
        verifier = oauth_pkce.new_verifier()
        self.assertGreaterEqual(len(verifier), 43)
        self.assertLessEqual(len(verifier), 128)

    def test_each_verifier_and_state_is_unique(self):
        self.assertEqual(len({oauth_pkce.new_verifier() for _ in range(50)}), 50)
        self.assertEqual(len({oauth_pkce.new_state() for _ in range(50)}), 50)

    def test_different_verifiers_give_different_challenges(self):
        a = oauth_pkce.challenge_for(oauth_pkce.new_verifier())
        b = oauth_pkce.challenge_for(oauth_pkce.new_verifier())
        self.assertNotEqual(a, b)


class EtsyAuthorizationUrlTest(unittest.TestCase):
    """Etsy's authorization URL must be complete enough to be accepted."""

    def setUp(self):
        from src.adapters import get_adapter

        self.adapter = get_adapter("etsy")

    def test_url_contains_the_required_pkce_parameters(self):
        challenge = oauth_pkce.challenge_for(oauth_pkce.new_verifier())
        url = self.adapter.get_authorization_url(
            state="test-state",
            credentials={"api_key": "my-keystring", "api_secret": "my-secret"},
            code_challenge=challenge,
        )
        query = parse_qs(urlparse(url).query)

        self.assertEqual(query["code_challenge"], [challenge])
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertEqual(query["response_type"], ["code"])
        self.assertEqual(query["state"], ["test-state"])
        self.assertEqual(query["scope"], ["listings_r"])

    def test_missing_challenge_is_refused_rather_than_silently_broken(self):
        """The original bug produced a plausible-looking URL that Etsy rejected,
        and the failure only surfaced much later as 'no valid access token'."""
        with self.assertRaises(ValueError):
            self.adapter.get_authorization_url(state="x", credentials={"api_key": "k"})

    def test_redirect_uri_is_url_encoded(self):
        """An unencoded redirect_uri contains :// and would break the query."""
        challenge = oauth_pkce.challenge_for(oauth_pkce.new_verifier())
        url = self.adapter.get_authorization_url(
            state="s", credentials={"api_key": "k"}, code_challenge=challenge)
        raw_redirect = parse_qs(urlparse(url).query)["redirect_uri"][0]
        self.assertTrue(raw_redirect.startswith("http"))
        self.assertIn("/api/auth/etsy/callback", raw_redirect)
        # The raw URL must not contain an unencoded scheme in the query.
        self.assertNotIn("redirect_uri=https://", url)

    def test_the_users_own_keystring_is_used(self):
        challenge = oauth_pkce.challenge_for(oauth_pkce.new_verifier())
        url = self.adapter.get_authorization_url(
            state="s", credentials={"api_key": "the-users-keystring"},
            code_challenge=challenge)
        self.assertEqual(
            parse_qs(urlparse(url).query)["client_id"], ["the-users-keystring"])


class ConnectHandshakeTest(unittest.TestCase):
    """POST /api/accounts/connect and the callback it leads to."""

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
        self.user = User(
            email=f"oauth.{secrets.token_hex(4)}@example.com",
            name="Oauth User", provider="google", is_admin=False,
        )
        self.db.add(self.user)
        self.db.flush()
        self.token = secrets.token_urlsafe(48)
        self.db.add(AuthSession(
            token=self.token, user_id=self.user.id,
            expires_at=datetime.utcnow() + timedelta(hours=1)))
        self.db.commit()
        self.client.cookies.clear()
        self.client.cookies.set("auth_token", self.token)

    def tearDown(self):
        self.db.rollback()
        self.db.query(UserMarketplaceCredential).filter(
            UserMarketplaceCredential.user_id == self.user.id
        ).delete(synchronize_session=False)
        self.db.query(AuthSession).filter(
            AuthSession.user_id == self.user.id
        ).delete(synchronize_session=False)
        self.db.query(User).filter(User.id == self.user.id).delete(synchronize_session=False)
        self.db.commit()
        self.db.close()

    def _save_etsy_keys(self):
        return self.client.post("/api/accounts/credentials", json={
            "platform": "etsy",
            "values": {"api_key": "e2e-keystring", "api_secret": "e2e-shared-secret"},
        })

    def test_connect_refuses_before_credentials_are_saved(self):
        res = self.client.post("/api/accounts/connect", json={"platform": "etsy"})
        self.assertEqual(res.status_code, 400)
        body = res.json()
        self.assertFalse(body["ok"])
        self.assertIn("api_key", body["error"])

    def test_connect_returns_a_url_with_pkce_and_sets_its_cookies(self):
        self._save_etsy_keys()
        res = self.client.post("/api/accounts/connect", json={"platform": "etsy"})
        self.assertEqual(res.status_code, 200, res.text)
        body = res.json()
        self.assertTrue(body["ok"])

        query = parse_qs(urlparse(body["auth_url"]).query)
        self.assertIn("code_challenge", query)
        self.assertEqual(query["code_challenge_method"], ["S256"])

        # The verifier and state must be held server-side, ready for the
        # callback, and must not be readable by scripts on the page.
        self.assertIn(oauth_pkce.STATE_COOKIE, res.cookies)
        self.assertIn(oauth_pkce.VERIFIER_COOKIE, res.cookies)
        state = res.cookies[oauth_pkce.STATE_COOKIE]
        verifier = res.cookies[oauth_pkce.VERIFIER_COOKIE]

        # And the challenge in the URL must correspond to the stored verifier,
        # or the token exchange will fail at Etsy.
        self.assertEqual(query["code_challenge"][0], oauth_pkce.challenge_for(verifier))
        self.assertEqual(query["state"][0], state)

    def test_connect_uses_the_signed_in_users_own_keystring(self):
        self._save_etsy_keys()
        res = self.client.post("/api/accounts/connect", json={"platform": "etsy"})
        query = parse_qs(urlparse(res.json()["auth_url"]).query)
        self.assertEqual(query["client_id"], ["e2e-keystring"])

    def test_connect_rejects_an_unknown_platform(self):
        res = self.client.post("/api/accounts/connect", json={"platform": "nope"})
        self.assertEqual(res.status_code, 400)

    def test_callback_refuses_when_state_does_not_match(self):
        """A callback this server did not start must not exchange the code."""
        self._save_etsy_keys()
        self.client.post("/api/accounts/connect", json={"platform": "etsy"})

        res = self.client.get(
            "/api/auth/etsy/callback?code=attacker-code&state=not-the-state",
            follow_redirects=False,
        )
        self.assertEqual(res.status_code, 200)
        self.assertIn("state mismatch", res.text.lower())
        # The one-shot cookies are cleared even on failure, so a stale state
        # cannot be replayed.
        self.assertIn("marketplace_oauth_state", res.headers.get("set-cookie", ""))

    def test_callback_refuses_without_any_state_cookie(self):
        self.client.cookies.clear()
        self.client.cookies.set("auth_token", self.token)
        res = self.client.get(
            "/api/auth/etsy/callback?code=some-code&state=whatever",
            follow_redirects=False,
        )
        self.assertEqual(res.status_code, 200)
        self.assertIn("state mismatch", res.text.lower())


class EtsyApiKeyHeaderTest(unittest.TestCase):
    """Etsy wants the credential pair, not the keystring alone."""

    def test_headers_include_the_shared_secret(self):
        """Asserted against the source: the header is built inside list_listings,
        which needs a live token to reach. Reading the source keeps the check
        meaningful without stubbing the whole call."""
        import pathlib

        source = (pathlib.Path(__file__).resolve().parent.parent
                  / "src" / "adapters" / "etsy.py").read_text(encoding="utf-8")

        # Every x-api-key must be the pair, never a bare api_key.
        self.assertIn('"x-api-key": f"{api_key}:{api_secret}"', source)
        self.assertNotIn('"x-api-key": api_key,', source)
        self.assertIn('"x-api-key": f"{api_key}:{api_secret}"} if api_key and api_secret else {}', source)

    def test_refresh_sends_the_credential_pair(self):
        import pathlib

        source = (pathlib.Path(__file__).resolve().parent.parent
                  / "src" / "adapters" / "etsy.py").read_text(encoding="utf-8")
        # HTTP Basic is not what Etsy documents for the token endpoint.
        self.assertNotIn("auth=(api_key, api_secret)", source)
        self.assertIn('"grant_type": "refresh_token"', source)
        self.assertIn('"client_id": api_key', source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
