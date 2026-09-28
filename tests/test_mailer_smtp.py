"""Real SMTP delivery test.

Everything else stubs mail out through ``mailer.OUTBOX``. This one runs an
actual (tiny) SMTP server on a loopback socket and lets ``smtplib`` talk to
it, so the network path, STARTTLS decision, login step and MIME message
construction are all genuinely exercised.

The server is written with plain sockets rather than ``smtpd`` (deprecated,
and removed in Python 3.12) or ``aiosmtpd`` (an extra dependency), so the
test adds nothing to requirements.txt.
"""
import os
import re
import socket
import sys
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Must precede the src imports: it fixes the ambient configuration the app
# reads at import time. See tests/_env.py for why that matters.
try:
    from tests import _env  # noqa: E402,F401  isort:skip
except ImportError:  # `tests` resolved to the directory, not the package
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import _env  # noqa: E402,F401  isort:skip


from src import mailer
from src.config import config


class MiniSMTPServer:
    """Minimal SMTP server that records the messages it receives."""

    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.port = self.sock.getsockname()[1]
        self.messages = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        while not self._stop.is_set():
            try:
                self.sock.settimeout(0.5)
                conn, _ = self.sock.accept()
            except (socket.timeout, OSError):
                continue
            try:
                self._handle(conn)
            except Exception:
                pass
            finally:
                conn.close()

    def _handle(self, conn):
        conn.settimeout(5)
        conn.sendall(b"220 mini SMTP ready\r\n")
        f = conn.makefile("rb")
        data_lines, in_data = [], False
        while True:
            raw = f.readline()
            if not raw:
                break
            line = raw.decode("utf-8", "replace")
            if in_data:
                if line.rstrip("\r\n") == ".":
                    self.messages.append({"content": "".join(data_lines)})
                    data_lines, in_data = [], False
                    conn.sendall(b"250 OK queued\r\n")
                    continue
                # Undo dot-stuffing.
                if line.startswith(".."):
                    line = line[1:]
                data_lines.append(line)
                continue
            upper = line.upper()
            if upper.startswith("EHLO") or upper.startswith("HELO"):
                conn.sendall(b"250-mini\r\n250 OK\r\n")
            elif upper.startswith("MAIL FROM"):
                conn.sendall(b"250 OK\r\n")
            elif upper.startswith("RCPT TO"):
                conn.sendall(b"250 OK\r\n")
            elif upper.startswith("DATA"):
                in_data = True
                conn.sendall(b"354 End data with <CR><LF>.<CR><LF>\r\n")
            elif upper.startswith("QUIT"):
                conn.sendall(b"221 Bye\r\n")
                break
            elif upper.startswith("STARTTLS"):
                # Not offered; the client is configured without TLS here.
                conn.sendall(b"502 Not implemented\r\n")
            else:
                conn.sendall(b"250 OK\r\n")

    def stop(self):
        self._stop.set()
        try:
            self.sock.close()
        except OSError:
            pass
        self._thread.join(timeout=3)


class MiniHttpServer:
    """Tiny HTTP server standing in for an email provider's API.

    Records the last request (headers + JSON body) so tests can assert the
    exact payload shape each provider expects.
    """

    def __init__(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        outer = self
        self.last_request = None

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass  # keep test output clean

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length).decode("utf-8", "replace")
                try:
                    import json as _json
                    parsed = _json.loads(raw) if raw else {}
                except Exception:
                    parsed = {}
                outer.last_request = {
                    "path": self.path,
                    "headers": {k.lower(): v for k, v in self.headers.items()},
                    "json": parsed,
                }
                if self.path.startswith("/reject"):
                    body = b'{"message":"domain not verified"}'
                    self.send_response(400)
                elif self.path.startswith("/ratelimited"):
                    body = b'{"error":"too many requests"}'
                    self.send_response(429)
                else:
                    body = b'{"id":"msg_1"}'
                    self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def reset(self):
        self.last_request = None

    def stop(self):
        try:
            self._server.shutdown()
            self._server.server_close()
        except Exception:
            pass


class _UsageServer:
    """Local stand-in for Resend's GET /usage."""

    def __init__(self, status=200, payload=None, text=None):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        self.status = status
        self.payload = payload or {}
        self.text = text
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                if outer.text is not None:
                    body = outer.text.encode()
                else:
                    import json as _json
                    body = _json.dumps(outer.payload).encode()
                self.send_response(outer.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self):
        try:
            self._server.shutdown()
            self._server.server_close()
        except Exception:
            pass


class ProviderUsageTest(unittest.TestCase):
    """Quota reporting: parsing, and the restricted-key case."""

    def setUp(self):
        import src.mailer as m
        self._orig_httpx_client = m.httpx.Client
        self._orig = (config.MAIL_BACKEND, config.MAIL_PROVIDER,
                      config.MAIL_API_KEY, config.MAIL_FROM)
        config.MAIL_BACKEND = "http"
        config.MAIL_PROVIDER = "resend"
        config.MAIL_API_KEY = "re_test"
        config.MAIL_FROM = "noreply@mail.example.com"
        self._servers = []

    def tearDown(self):
        # Restore the patched httpx.Client so nothing leaks into later tests.
        import src.mailer as m
        m.httpx.Client = self._orig_httpx_client
        (config.MAIL_BACKEND, config.MAIL_PROVIDER,
         config.MAIL_API_KEY, config.MAIL_FROM) = self._orig
        for s in self._servers:
            s.stop()

    def _serve_usage(self, status=200, payload=None, text=None):
        server = _UsageServer(status=status, payload=payload, text=text)
        self._servers.append(server)
        # Redirect the usage call at our local server.
        import src.mailer as m
        self._patch_usage_url(server.port)
        return server

    def _patch_usage_url(self, port):
        """Point the hard-coded Resend usage URL at a local server."""
        import src.mailer as m
        original = self._orig_httpx_client

        class PatchedClient(original):
            def get(self, url, **kwargs):
                if url == "https://api.resend.com/usage":
                    url = f"http://127.0.0.1:{port}/usage"
                return super().get(url, **kwargs)

        m.httpx.Client = PatchedClient

    def test_parses_usage_and_flags_critical(self):
        payload = {
            "object": "usage",
            "emails": {
                "daily": {"used": 90, "limit": 100, "sent": 88, "received": 2,
                          "resets_at": "2026-07-29T00:00:00.000Z"},
                "monthly": {"used": 900, "limit": 3000, "sent": 880, "received": 20,
                            "resets_at": "2026-08-01T00:00:00.000Z"},
            },
        }
        self._serve_usage(payload=payload)
        usage, reason = mailer.fetch_provider_usage()
        self.assertIsNotNone(usage, reason)
        self.assertEqual(usage["provider"], "resend")
        self.assertEqual(usage["daily"]["limit"], 100)
        self.assertEqual(usage["monthly"]["used"], 900)
        # 90/100 = 90% -> critical
        self.assertTrue(usage["critical"])

    def test_low_usage_is_not_critical(self):
        payload = {"emails": {
            "daily": {"used": 5, "limit": 100},
            "monthly": {"used": 20, "limit": 3000},
        }}
        self._serve_usage(payload=payload)
        usage, _ = mailer.fetch_provider_usage()
        self.assertIsNotNone(usage)
        self.assertFalse(usage["critical"])

    def test_null_limit_is_unlimited_not_critical(self):
        """Paid plans report limit=null for daily; that must not divide by zero."""
        payload = {"emails": {
            "daily": {"used": 5000, "limit": None},
            "monthly": {"used": 5000, "limit": 50000},
        }}
        self._serve_usage(payload=payload)
        usage, _ = mailer.fetch_provider_usage()
        self.assertIsNotNone(usage)
        self.assertFalse(usage["critical"])

    def test_restricted_key_reports_actionable_message(self):
        self._serve_usage(
            status=401,
            text='{"statusCode":401,"message":"This API key is restricted to only send emails","name":"restricted_api_key"}',
        )
        usage, reason = mailer.fetch_provider_usage()
        self.assertIsNone(usage)
        self.assertIn("Full access", reason)
        # It must make clear that sending still works.
        self.assertIn("sending works", reason.lower())

    def test_plain_401_is_reported_separately(self):
        self._serve_usage(status=401, text='{"message":"invalid api key"}')
        usage, reason = mailer.fetch_provider_usage()
        self.assertIsNone(usage)
        self.assertIn("rejected", reason.lower())

    def test_server_error_is_reported(self):
        self._serve_usage(status=500, text="{}")
        usage, reason = mailer.fetch_provider_usage()
        self.assertIsNone(usage)
        self.assertIn("500", reason)

    def test_missing_quota_section_is_reported(self):
        self._serve_usage(payload={"object": "usage"})
        usage, reason = mailer.fetch_provider_usage()
        self.assertIsNone(usage)
        self.assertIn("no email quota", reason.lower())

    def test_non_resend_provider_is_reported(self):
        config.MAIL_PROVIDER = "brevo"
        usage, reason = mailer.fetch_provider_usage()
        self.assertIsNone(usage)
        self.assertIn("not implemented", reason.lower())

    def test_smtp_transport_reports_unavailable(self):
        config.MAIL_BACKEND = "smtp"
        usage, reason = mailer.fetch_provider_usage()
        self.assertIsNone(usage)
        self.assertIn("provider API", reason)


class RealSmtpDeliveryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = MiniSMTPServer()
        cls._orig = (config.MAIL_BACKEND, config.MAIL_FROM,
                     config.SMTP_HOST, config.SMTP_PORT, config.SMTP_USER,
                     config.SMTP_PASSWORD, config.SMTP_FROM, config.SMTP_USE_TLS)
        config.MAIL_BACKEND = "smtp"
        config.SMTP_HOST = "127.0.0.1"
        config.SMTP_PORT = cls.server.port
        config.SMTP_USER = ""
        config.SMTP_PASSWORD = ""
        config.SMTP_FROM = "noreply@marketplace.test"
        config.SMTP_USE_TLS = False
        mailer.OUTBOX = None  # force the real network path

    @classmethod
    def tearDownClass(cls):
        (config.MAIL_BACKEND, config.MAIL_FROM,
         config.SMTP_HOST, config.SMTP_PORT, config.SMTP_USER,
         config.SMTP_PASSWORD, config.SMTP_FROM, config.SMTP_USE_TLS) = cls._orig
        cls.server.stop()

    def _decoded(self, index):
        import quopri
        content = self.server.messages[index]["content"]
        text = quopri.decodestring(content.encode()).decode("utf-8", "replace")
        return text.replace("=\n", "").replace("\n", ""), text

    def test_verification_email_really_sends_over_smtp(self):
        before = len(self.server.messages)
        ok, err = mailer.send_verification_email(
            "newuser@example.com", "New User",
            "http://localhost:8000/verify-email?token=REALTOKEN123", 60)
        self.assertTrue(ok, f"send failed: {err}")
        self.assertEqual(len(self.server.messages), before + 1)

        flat, _ = self._decoded(before)
        self.assertIn("newuser@example.com", flat)
        self.assertIn("Confirm your email", flat)
        # The link survives MIME transfer encoding intact.
        self.assertIn("verify-email?token=REALTOKEN123", flat)
        self.assertIn("60 minutes", flat)

    def test_test_email_sends_over_smtp(self):
        before = len(self.server.messages)
        ok, err = mailer.send_test_email("admin@example.com")
        self.assertTrue(ok, f"send failed: {err}")
        flat, _ = self._decoded(before)
        self.assertIn("admin@example.com", flat)
        self.assertIn("test email", flat)

    def test_missing_recipient_fails_cleanly(self):
        ok, err = mailer.send_email("", "subject", "body")
        self.assertFalse(ok)
        self.assertIn("recipient", err.lower())

    def test_unreachable_server_fails_cleanly(self):
        """A dead mail server must return an error, never raise."""
        original_port = config.SMTP_PORT
        # Port 1 on loopback is not listening.
        config.SMTP_PORT = 1
        try:
            ok, err = mailer.send_test_email("admin@example.com")
        finally:
            config.SMTP_PORT = original_port
        self.assertFalse(ok)
        self.assertTrue(err, "an error message should explain the failure")

    def test_smtp_configured_requires_host_and_sender(self):
        original = (config.MAIL_BACKEND, config.MAIL_FROM, config.SMTP_HOST,
                    config.SMTP_USER, config.SMTP_FROM)
        try:
            config.MAIL_BACKEND = "smtp"
            config.MAIL_FROM = ""       # no global sender...
            config.SMTP_FROM = ""       # ...and none on SMTP either
            config.SMTP_USER = ""
            config.SMTP_HOST = ""
            self.assertFalse(mailer.smtp_configured())
            # A host alone is not enough — there must be a sender somewhere.
            config.SMTP_HOST = "127.0.0.1"
            self.assertFalse(mailer.smtp_configured())
            # Either sender source is sufficient.
            config.SMTP_FROM = "noreply@marketplace.test"
            self.assertTrue(mailer.smtp_configured())
            config.SMTP_FROM = ""
            config.MAIL_FROM = "noreply@mail.example.com"
            self.assertTrue(mailer.smtp_configured())
            self.assertEqual(mailer.from_address(), "noreply@mail.example.com")
        finally:
            (config.MAIL_BACKEND, config.MAIL_FROM, config.SMTP_HOST,
             config.SMTP_USER, config.SMTP_FROM) = original


class HttpProviderMailTest(unittest.TestCase):
    """The HTTP provider path: request shape, auth header, error handling.

    A tiny local HTTP server stands in for Resend/Brevo/Postmark, so the
    payload each provider expects can be asserted exactly.
    """

    @classmethod
    def setUpClass(cls):
        cls.server = MiniHttpServer()
        cls._orig = (config.MAIL_BACKEND, config.MAIL_PROVIDER, config.MAIL_API_KEY,
                     config.MAIL_API_URL, config.MAIL_FROM)
        mailer.OUTBOX = None

    @classmethod
    def tearDownClass(cls):
        (config.MAIL_BACKEND, config.MAIL_PROVIDER, config.MAIL_API_KEY,
         config.MAIL_API_URL, config.MAIL_FROM) = cls._orig
        cls.server.stop()

    def setUp(self):
        self.server.reset()
        config.MAIL_BACKEND = "http"
        config.MAIL_FROM = "noreply@mail.example.com"

    def test_http_backend_is_selected(self):
        config.MAIL_PROVIDER = "resend"
        config.MAIL_API_KEY = "test-key"
        self.assertEqual(mailer.mail_backend(), "http")
        self.assertEqual(mailer.from_address(), "noreply@mail.example.com")

    def test_generic_provider_posts_expected_json(self):
        config.MAIL_PROVIDER = "generic"
        config.MAIL_API_KEY = "secret-key"
        config.MAIL_API_URL = f"http://127.0.0.1:{self.server.port}/send"

        ok, err = mailer.send_verification_email(
            "signup@example.com", "Signup Person",
            "http://localhost:8000/verify-email?token=HTTPTOKEN", 60)
        self.assertTrue(ok, f"send failed: {err}")

        req = self.server.last_request
        self.assertIsNotNone(req, "no request reached the provider")
        self.assertEqual(req["headers"].get("authorization"), "Bearer secret-key")
        self.assertEqual(req["json"]["from"], "noreply@mail.example.com")
        self.assertEqual(req["json"]["to"], ["signup@example.com"])
        self.assertIn("Confirm your email", req["json"]["subject"])
        self.assertIn("verify-email?token=HTTPTOKEN", req["json"]["text"])

    def test_resend_payload_and_endpoint_shape(self):
        """Resend expects {from,to[],subject,text} and a Bearer token."""
        config.MAIL_PROVIDER = "resend"
        config.MAIL_API_KEY = "re_test"
        # Point the resend adapter at our local server.
        original_url = mailer._provider_url
        mailer._provider_url = lambda provider: f"http://127.0.0.1:{self.server.port}/emails"
        try:
            ok, err = mailer.send_test_email("admin@example.com")
        finally:
            mailer._provider_url = original_url
        self.assertTrue(ok, f"send failed: {err}")
        req = self.server.last_request
        self.assertEqual(req["headers"].get("authorization"), "Bearer re_test")
        self.assertIn("to", req["json"])
        self.assertEqual(req["json"]["to"], ["admin@example.com"])
        self.assertIn("text", req["json"])
        self.assertIn("subject", req["json"])

    def test_brevo_payload_shape(self):
        config.MAIL_PROVIDER = "brevo"
        config.MAIL_API_KEY = "brevo-key"
        original_url = mailer._provider_url
        mailer._provider_url = lambda provider: f"http://127.0.0.1:{self.server.port}/smtp"
        try:
            ok, err = mailer.send_test_email("admin@example.com")
        finally:
            mailer._provider_url = original_url
        self.assertTrue(ok, f"send failed: {err}")
        req = self.server.last_request
        self.assertEqual(req["headers"].get("api-key"), "brevo-key")
        # Brevo nests the sender and uses textContent.
        self.assertEqual(req["json"]["sender"]["email"], "noreply@mail.example.com")
        self.assertEqual(req["json"]["to"], [{"email": "admin@example.com"}])
        self.assertIn("textContent", req["json"])

    def test_postmark_payload_shape(self):
        config.MAIL_PROVIDER = "postmark"
        config.MAIL_API_KEY = "pm-token"
        original_url = mailer._provider_url
        mailer._provider_url = lambda provider: f"http://127.0.0.1:{self.server.port}/email"
        try:
            ok, err = mailer.send_test_email("admin@example.com")
        finally:
            mailer._provider_url = original_url
        self.assertTrue(ok, f"send failed: {err}")
        req = self.server.last_request
        self.assertEqual(req["headers"].get("x-postmark-server-token"), "pm-token")
        # Postmark uses PascalCase keys.
        self.assertEqual(req["json"]["From"], "noreply@mail.example.com")
        self.assertEqual(req["json"]["To"], "admin@example.com")
        self.assertIn("TextBody", req["json"])

    def test_provider_rejection_is_reported_not_raised(self):
        config.MAIL_PROVIDER = "generic"
        config.MAIL_API_KEY = "k"
        config.MAIL_API_URL = f"http://127.0.0.1:{self.server.port}/reject"
        ok, err = mailer.send_test_email("admin@example.com")
        self.assertFalse(ok)
        self.assertIn("400", err)
        # The provider's own message should be surfaced to the admin.
        self.assertIn("domain not verified", err.lower())

    def test_transient_failure_is_labelled_temporary(self):
        config.MAIL_PROVIDER = "generic"
        config.MAIL_API_KEY = "k"
        config.MAIL_API_URL = f"http://127.0.0.1:{self.server.port}/ratelimited"
        ok, err = mailer.send_test_email("admin@example.com")
        self.assertFalse(ok)
        self.assertIn("temporary", err.lower())

    def test_unreachable_provider_fails_cleanly(self):
        config.MAIL_PROVIDER = "generic"
        config.MAIL_API_KEY = "k"
        config.MAIL_API_URL = "http://127.0.0.1:1/send"
        ok, err = mailer.send_test_email("admin@example.com")
        self.assertFalse(ok)
        self.assertIn("could not reach", err.lower())

    def test_generic_without_url_is_a_clear_error(self):
        config.MAIL_PROVIDER = "generic"
        config.MAIL_API_KEY = "k"
        config.MAIL_API_URL = ""
        ok, err = mailer.send_test_email("admin@example.com")
        self.assertFalse(ok)
        self.assertIn("MAIL_API_URL", err)

    def test_unknown_provider_is_a_clear_error(self):
        config.MAIL_PROVIDER = "not-a-provider"
        config.MAIL_API_KEY = "k"
        ok, err = mailer.send_test_email("admin@example.com")
        self.assertFalse(ok)
        self.assertIn("unknown", err.lower())

    def test_missing_from_address_is_refused(self):
        config.MAIL_PROVIDER = "generic"
        config.MAIL_API_KEY = "k"
        config.MAIL_API_URL = f"http://127.0.0.1:{self.server.port}/send"
        original = config.MAIL_FROM
        config.MAIL_FROM = ""
        try:
            ok, err = mailer.send_test_email("admin@example.com")
        finally:
            config.MAIL_FROM = original
        self.assertFalse(ok)
        self.assertIn("sender", err.lower())

    def test_backend_is_disabled_when_nothing_is_configured(self):
        original = (config.MAIL_BACKEND, config.MAIL_PROVIDER, config.MAIL_API_KEY,
                    config.MAIL_API_URL, config.SMTP_HOST)
        try:
            config.MAIL_BACKEND = ""
            config.MAIL_PROVIDER = ""
            config.MAIL_API_KEY = ""
            config.SMTP_HOST = ""
            self.assertEqual(mailer.mail_backend(), "")
            self.assertFalse(mailer.smtp_configured())
            ok, err = mailer.send_test_email("admin@example.com")
            self.assertFalse(ok)
            self.assertIn("not configured", err.lower())
        finally:
            (config.MAIL_BACKEND, config.MAIL_PROVIDER, config.MAIL_API_KEY,
             config.MAIL_API_URL, config.SMTP_HOST) = original


if __name__ == "__main__":
    unittest.main(verbosity=2)
