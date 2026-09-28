"""Client-scheme detection behind a proxy.

The app cannot see the client's TLS version: Cloudflare terminates TLS and
uvicorn's ASGI scope carries no TLS information, so the origin only ever
receives plain HTTP. Minimum TLS version is therefore an edge setting.

What the app *can* check is whether a request arrived over HTTPS at all, which
is what these tests cover. The dangerous direction is a false "https" — that
would silence the warning about traffic bypassing Cloudflare.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from starlette.testclient import TestClient

from src.api import main as main_module
from src.api.main import _client_scheme, app
from src.config import config


class FakeRequest:
    def __init__(self, headers=None, scheme="http"):
        self.headers = {k.lower(): v for k, v in (headers or {}).items()}
        self.url = type("URL", (), {"scheme": scheme})()


class ClientSchemeTest(unittest.TestCase):
    def test_x_forwarded_proto_wins(self):
        self.assertEqual(
            _client_scheme(FakeRequest({"x-forwarded-proto": "https"})), "https")
        self.assertEqual(
            _client_scheme(FakeRequest({"x-forwarded-proto": "http"})), "http")

    def test_only_the_first_hop_of_a_forwarded_chain_counts(self):
        self.assertEqual(
            _client_scheme(FakeRequest({"x-forwarded-proto": "https, http"})),
            "https")
        self.assertEqual(
            _client_scheme(FakeRequest({"x-forwarded-proto": "http, https"})),
            "http")

    def test_whitespace_and_case_are_normalised(self):
        self.assertEqual(
            _client_scheme(FakeRequest({"x-forwarded-proto": "  HTTPS  "})), "https")

    def test_cf_visitor_json_is_parsed(self):
        """Cloudflare's CF-Visitor is a JSON object, not a bare scheme."""
        self.assertEqual(
            _client_scheme(FakeRequest({"cf-visitor": '{"scheme":"https"}'})), "https")
        self.assertEqual(
            _client_scheme(FakeRequest({"cf-visitor": '{"scheme":"http"}'})), "http")

    def test_malformed_cf_visitor_is_not_read_as_secure(self):
        """Fail safe: junk must not be mistaken for TLS."""
        for value in ('not-json', '{}', '', '{"scheme":null}', '{"other":"x"}',
                      'null', '[]', '{"scheme":123}'):
            got = _client_scheme(FakeRequest({"cf-visitor": value}))
            self.assertNotEqual(got, "https",
                                f"CF-Visitor {value!r} was read as HTTPS")

    def test_falls_back_to_request_scheme(self):
        self.assertEqual(_client_scheme(FakeRequest({}, scheme="http")), "http")

    def test_x_forwarded_proto_beats_cf_visitor(self):
        self.assertEqual(
            _client_scheme(FakeRequest({"x-forwarded-proto": "https",
                                        "cf-visitor": '{"scheme":"http"}'})),
            "https")


class FakeASGIRequest:
    """Just enough of a Request for the middleware.

    Starlette 0.38's TestClient cannot set the client address, so the
    middleware is invoked directly rather than through a real request.
    """

    def __init__(self, peer, scheme="http", forwarded=None, cf_visitor=None):
        headers = {}
        if forwarded:
            headers["x-forwarded-proto"] = forwarded
        if cf_visitor:
            headers["cf-visitor"] = cf_visitor
        self.headers = headers
        self.client = type("Client", (), {"host": peer})()
        self.url = type("URL", (), {"scheme": scheme})()


async def _run_middleware(request):
    """Invoke the plain-HTTP middleware with a request and a no-op handler."""
    called = {"n": 0}

    async def call_next(_req):
        called["n"] += 1
        return "response"

    result = await main_module.warn_on_plain_http(request, call_next)
    return result, called["n"]


class PlainHttpWarningTest(unittest.TestCase):
    """The warning must fire on real plain HTTP and stay quiet otherwise."""

    def setUp(self):
        self._orig = config.REQUIRE_HTTPS
        config.REQUIRE_HTTPS = True
        main_module._plain_http_seen = False

    def tearDown(self):
        config.REQUIRE_HTTPS = self._orig
        main_module._plain_http_seen = False

    def _drive(self, peer, forwarded="http"):
        import asyncio
        return asyncio.get_event_loop().run_until_complete(
            _run_middleware(FakeASGIRequest(peer, forwarded=forwarded)))

    def test_direct_plain_http_triggers_the_warning(self):
        self._drive("203.0.113.5")
        self.assertTrue(main_module._plain_http_seen,
                        "a direct plain-HTTP request did not raise the warning")

    def test_healthcheck_from_loopback_is_not_flagged(self):
        """The container healthcheck curls localhost over plain HTTP.

        Flagging that would fire the warning on every deployment and train the
        operator to ignore the one warning that matters.
        """
        for peer in ("127.0.0.1", "::1", "localhost"):
            main_module._plain_http_seen = False
            self._drive(peer)
            self.assertFalse(main_module._plain_http_seen,
                             f"peer {peer} was treated as external plain HTTP")

    def test_proxied_https_request_is_not_flagged(self):
        self._drive("203.0.113.5", forwarded="https")
        self.assertFalse(main_module._plain_http_seen)

    def test_cf_visitor_https_is_not_flagged(self):
        import asyncio
        asyncio.get_event_loop().run_until_complete(
            _run_middleware(FakeASGIRequest("203.0.113.5",
                                            cf_visitor='{"scheme":"https"}')))
        self.assertFalse(main_module._plain_http_seen)

    def test_middleware_always_passes_the_request_through(self):
        """It warns; it must never block."""
        result, calls = self._drive("203.0.113.5")
        self.assertEqual(result, "response")
        self.assertEqual(calls, 1)

    def test_no_warning_when_https_is_not_required(self):
        """Local development over http:// must not spam the log."""
        config.REQUIRE_HTTPS = False
        self._drive("203.0.113.5")
        self.assertFalse(main_module._plain_http_seen)

    def test_security_headers_still_applied(self):
        client = TestClient(app)
        res = client.get("/login", headers={"x-forwarded-proto": "https"})
        self.assertEqual(res.headers.get("x-content-type-options"), "nosniff")
        self.assertEqual(res.headers.get("x-frame-options"), "DENY")
        # HSTS only when HTTPS is enforced.
        self.assertIn("max-age=63072000", res.headers.get("strict-transport-security", ""))

    def test_hsts_disabled_when_https_not_required(self):
        config.REQUIRE_HTTPS = False
        client = TestClient(app)
        res = client.get("/login")
        self.assertEqual(res.headers.get("strict-transport-security"), "max-age=0")

    def test_https_request_does_not_trigger_the_warning(self):
        client = TestClient(app)
        client.get("/login", headers={"x-forwarded-proto": "https"})
        self.assertFalse(main_module._plain_http_seen,
                         "an HTTPS request was treated as plain HTTP")

    def test_no_warning_when_https_is_not_required(self):
        """Local development over http:// must not spam the log."""
        config.REQUIRE_HTTPS = False
        client = TestClient(app)
        client.get("/login", headers={"x-forwarded-proto": "http"})
        self.assertFalse(main_module._plain_http_seen)

    def test_warning_does_not_block_the_request(self):
        """It reports; it must not break serving."""
        client = TestClient(app)
        res = client.get("/login", headers={"x-forwarded-proto": "http"})
        self.assertEqual(res.status_code, 200)

    def test_security_headers_still_applied(self):
        client = TestClient(app)
        res = client.get("/login", headers={"x-forwarded-proto": "https"})
        self.assertEqual(res.headers.get("x-content-type-options"), "nosniff")
        self.assertEqual(res.headers.get("x-frame-options"), "DENY")
        # HSTS only when HTTPS is enforced.
        self.assertIn("max-age=63072000", res.headers.get("strict-transport-security", ""))

    def test_hsts_disabled_when_https_not_required(self):
        config.REQUIRE_HTTPS = False
        client = TestClient(app)
        res = client.get("/login")
        self.assertEqual(res.headers.get("strict-transport-security"), "max-age=0")


if __name__ == "__main__":
    unittest.main(verbosity=2)
