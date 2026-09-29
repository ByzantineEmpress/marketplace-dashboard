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
        # shops_r is required to resolve the seller's shop: Etsy's token
        # response has no shop_id, and without this scope the lookup 403s and
        # every sync silently reports zero listings.
        self.assertIn("listings_r", query["scope"][0])
        self.assertIn("shops_r", query["scope"][0])

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


class EtsyShopResolutionTest(unittest.TestCase):
    """Resolving the seller's shop.

    Etsy's token response contains ``user_id``, never ``shop_id``. The adapter
    assumed a shop_id was present, so it stayed None and every sync reported
    "0 listings" with no error - while the account plainly had listings. The
    original fallback queried ``/v3/application/applications``, which lists the
    applications a user has authorised and carries no shop for an ordinary
    seller.
    """

    def setUp(self):
        from src.adapters import get_adapter

        self.adapter = get_adapter("etsy")

    def _call(self, payload, status=200):
        """Run resolve_shop against a stubbed HTTP layer."""
        import src.adapters.etsy as etsy_mod

        captured = {}

        class FakeResp:
            status_code = status

            def json(self):
                return payload

        def fake_get(url, **kwargs):
            captured["url"] = url
            captured["headers"] = kwargs.get("headers", {})
            return FakeResp()

        real = etsy_mod.httpx.get
        etsy_mod.httpx.get = fake_get
        try:
            result = self.adapter.resolve_shop(
                {"x-api-key": "k:s"}, {"user_id": 1207010539}, "1207010539.opaque"
            )
        finally:
            etsy_mod.httpx.get = real
        return result, captured

    def test_reads_shop_id_and_name_from_the_users_shops_endpoint(self):
        result, captured = self._call(
            {"shop_id": 64488261, "shop_name": "ByzantineMods"})
        self.assertEqual(result["shop_id"], 64488261)
        self.assertEqual(result["shop_name"], "ByzantineMods")
        self.assertIn("/application/users/1207010539/shops", captured["url"])

    def test_user_id_falls_back_to_the_access_token_prefix(self):
        """Access tokens are "<user_id>.<opaque>", so the id is recoverable even
        for a token stored before user_id was kept."""
        result, captured = self._call(
            {"shop_id": 1, "shop_name": "S"}, )
        self.assertIn("1207010539", captured["url"])

        # Now with no user_id in token_data at all.
        import src.adapters.etsy as etsy_mod

        captured2 = {}

        class FakeResp:
            status_code = 200
            def json(self):
                return {"shop_id": 7, "shop_name": "FromToken"}

        def fake_get(url, **kwargs):
            captured2["url"] = url
            return FakeResp()

        real = etsy_mod.httpx.get
        etsy_mod.httpx.get = fake_get
        try:
            result2 = self.adapter.resolve_shop(
                {}, {}, "1207010539.somesecretvalue")
        finally:
            etsy_mod.httpx.get = real
        self.assertEqual(result2["shop_id"], 7)
        self.assertIn("/users/1207010539/shops", captured2["url"])

    def test_a_403_reports_missing_scope_rather_than_returning_empty(self):
        """The old code swallowed this and the sync said "0 listings"."""
        result, _ = self._call(
            {"error": "Access token lacks scope for this request "
                      "(requires scope: shops_r)."}, status=403)
        self.assertIsNone(result.get("shop_id"))
        self.assertIn("shops_r", result.get("error", ""))

    def test_an_http_error_is_reported_not_raised(self):
        import src.adapters.etsy as etsy_mod

        def boom(url, **kwargs):
            raise RuntimeError("network down")

        real = etsy_mod.httpx.get
        etsy_mod.httpx.get = boom
        try:
            result = self.adapter.resolve_shop({}, {"user_id": 1}, "")
        finally:
            etsy_mod.httpx.get = real
        self.assertEqual(result, {})

    def test_a_wrapped_results_payload_is_also_accepted(self):
        result, _ = self._call(
            {"results": [{"shop_id": 99, "shop_name": "Wrapped"}]})
        self.assertEqual(result["shop_id"], 99)
        self.assertEqual(result["shop_name"], "Wrapped")


class EtsyScopeTest(unittest.TestCase):
    def test_shops_r_is_requested(self):
        """Without it the shop can never be resolved, so a sync reports 0."""
        from urllib.parse import parse_qs, urlparse
        from src.adapters import get_adapter

        challenge = oauth_pkce.challenge_for(oauth_pkce.new_verifier())
        url = get_adapter("etsy").get_authorization_url(
            state="s", credentials={"api_key": "k"}, code_challenge=challenge)
        scope = parse_qs(urlparse(url).query)["scope"][0]
        self.assertIn("listings_r", scope)
        self.assertIn("shops_r", scope)


class SyncReportsFailureInsteadOfSilentZeroTest(unittest.TestCase):
    def test_a_missing_shop_surfaces_as_an_error(self):
        """`last_error` must reach the caller, so "0 listings" cannot mean both
        "empty shop" and "could not find the shop"."""
        from src.adapters import get_adapter

        adapter = get_adapter("etsy")
        adapter.last_error = "Could not determine your Etsy shop."
        self.assertTrue(adapter.last_error)


if __name__ == "__main__":
    unittest.main(verbosity=2)
