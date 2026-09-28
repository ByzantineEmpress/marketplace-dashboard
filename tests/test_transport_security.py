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


class PlainHttpWarningTest(unittest.TestCase):
    """The warning must fire on plain HTTP and stay quiet on HTTPS."""

    def setUp(self):
        self._orig = config.REQUIRE_HTTPS
        config.REQUIRE_HTTPS = True
        main_module._plain_http_seen = False

    def tearDown(self):
        config.REQUIRE_HTTPS = self._orig
        main_module._plain_http_seen = False

    def test_plain_http_request_triggers_the_warning_once(self):
        client = TestClient(app)
        client.get("/login", headers={"x-forwarded-proto": "http"})
        self.assertTrue(main_module._plain_http_seen,
                        "plain HTTP did not raise the warning flag")

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
