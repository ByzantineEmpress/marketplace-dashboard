"""eBay OAuth: the authorize URL needs a RuName, not a callback URL.

Reported as eBay's opaque error page:

    auth2.ebay.com/oauth2/errorOauth?errorid=temporarily_unavailable
    "The authorization server is currently unable to handle the request."

Two real bugs caused it:

1. The scopes were not real eBay scopes. The code sent
   ``https://api.ebay.com/auth/oauth/sell_inventory_readonly``; eBay's actual
   scope URNs are ``https://api.ebay.com/oauth/api_scope/sell.inventory.readonly``.
   An invalid scope makes eBay reject the whole authorize request.

2. ``redirect_uri`` was the callback URL. eBay requires the RuName ("redirect
   URL name") there, and reads the callback URL from the "Auth Accepted URL"
   configured against that RuName. Per eBay's own Quick OAuth Guide:

       redirectUri - OAuth Enabled RuName for the clientId
       redirectUrl - Auth Accepted URL associated with the redirectUri

Neither failure names its cause, which is why this needed the docs rather than
the error message.
"""

import unittest
from urllib.parse import parse_qs, urlparse

from tests import _env  # noqa: E402,F401  isort:skip  (must precede src imports)

from src.adapters import get_adapter


class EbayAuthorizeUrlTest(unittest.TestCase):
    def setUp(self):
        self.adapter = get_adapter("ebay")
        self.creds = {"client_id": "MyApp-XXXX", "ru_name": "My_Name-MyApp-abcdef"}

    def _query(self):
        url = self.adapter.get_authorization_url(state="st4te", credentials=self.creds)
        return parse_qs(urlparse(url).query)

    def test_redirect_uri_is_the_runame_not_the_callback_url(self):
        q = self._query()
        self.assertEqual(q["redirect_uri"], ["My_Name-MyApp-abcdef"])
        self.assertNotIn("/api/auth/ebay/callback", q["redirect_uri"][0])

    def test_scopes_use_the_real_ebay_urns(self):
        scope = self._query()["scope"][0]
        self.assertIn("https://api.ebay.com/oauth/api_scope", scope)
        self.assertIn(
            "https://api.ebay.com/oauth/api_scope/sell.inventory.readonly", scope)

    def test_scopes_are_space_separated_then_encoded(self):
        """eBay wants a space-separated list, URL-encoded. parse_qs decodes it
        back to spaces, so the split is the check."""
        scope = self._query()["scope"][0]
        self.assertGreaterEqual(len(scope.split(" ")), 2)
        for part in scope.split(" "):
            self.assertTrue(part.startswith("https://api.ebay.com/oauth/api_scope"),
                            f"not an eBay scope URN: {part}")

    def test_the_dead_scope_format_is_gone_from_the_source(self):
        """Guards the exact strings that failed, the way the Etsy suite guards
        its public-feed endpoint."""
        import pathlib
        source = (pathlib.Path(__file__).resolve().parent.parent
                  / "src" / "adapters" / "ebay.py").read_text(encoding="utf-8")
        for line in source.splitlines():
            stripped = line.strip()
            if "/auth/oauth/" in stripped:
                self.assertTrue(
                    stripped.startswith("#") or stripped.startswith("-"),
                    f"a live reference to the old scope format remains: {stripped}",
                )

    def test_other_params_are_intact(self):
        q = self._query()
        self.assertEqual(q["client_id"], ["MyApp-XXXX"])
        self.assertEqual(q["response_type"], ["code"])
        self.assertEqual(q["state"], ["st4te"])

    def test_a_missing_runame_fails_loudly_before_eBay(self):
        """eBay's own error for this is 'temporarily_unavailable', which says
        nothing about the cause, so the adapter refuses first."""
        from src.config import config

        original = config.EBAY_RUNAME
        config.EBAY_RUNAME = ""
        try:
            with self.assertRaises(ValueError) as ctx:
                self.adapter.get_authorization_url(
                    state="s", credentials={"client_id": "MyApp-XXXX"})
        finally:
            config.EBAY_RUNAME = original
        self.assertIn("RuName", str(ctx.exception))


class EbayTokenExchangeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from src.database import init_db
        init_db()

    def setUp(self):
        self.adapter = get_adapter("ebay")
        self.creds = {
            "client_id": "MyApp-XXXX",
            "client_secret": "secret",
            "ru_name": "My_Name-MyApp-abcdef",
        }

    def tearDown(self):
        # The exchange stores tokens; remove the row it creates.
        from src.database import SessionLocal
        from src.models import MarketplaceAccount
        db = SessionLocal()
        try:
            db.query(MarketplaceAccount).filter(
                MarketplaceAccount.user_id.is_(None),
                MarketplaceAccount.platform == "ebay",
            ).delete(synchronize_session=False)
            db.commit()
        finally:
            db.close()

    def test_the_exchange_repeats_the_runame(self):
        """eBay requires the same redirect_uri on the token request."""
        import src.adapters.ebay as ebay_mod

        posted = {}

        class FakeResp:
            status_code = 200
            def json(self):
                return {"access_token": "t", "refresh_token": "r", "expires_in": 7200}
            def raise_for_status(self):
                pass

        def fake_post(url, **kw):
            posted["data"] = kw.get("data")
            return FakeResp()

        real = ebay_mod.httpx.post
        ebay_mod.httpx.post = fake_post
        try:
            self.adapter.handle_callback(code="c0de", state="s",
                                         credentials=self.creds, user_id=None)
        finally:
            ebay_mod.httpx.post = real

        self.assertEqual(posted["data"]["redirect_uri"], "My_Name-MyApp-abcdef")
        self.assertEqual(posted["data"]["grant_type"], "authorization_code")

    def test_it_accepts_the_shared_callback_kwargs(self):
        """The shared route passes code_verifier and seller_id to every adapter.
        Accepting them explicitly avoids the route's TypeError fallback, which
        would otherwise swallow a real error too."""
        import inspect
        sig = inspect.signature(self.adapter.handle_callback)
        self.assertIn("code_verifier", sig.parameters)
        self.assertIn("seller_id", sig.parameters)


if __name__ == "__main__":
    unittest.main(verbosity=2)
