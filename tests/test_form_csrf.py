"""Browser forms must bind submissions to a token issued to the same browser."""
import re
import unittest

from tests import _env  # noqa: F401 - configure the isolated test database
from starlette.testclient import TestClient
from src.api.main import app
from src.config import config


class FormCsrfTest(unittest.TestCase):
    def test_browser_forms_require_matching_token(self):
        old_mode = config.GOOGLE_DEV_MODE
        config.GOOGLE_DEV_MODE = True
        try:
            with TestClient(app) as client:
                for page, endpoint in (("/login", "/api/auth/login"),
                                       ("/signup", "/api/auth/signup"),
                                       ("/auth/google/dev-picker", "/auth/google/dev-login")):
                    with self.subTest(endpoint=endpoint):
                        response = client.get(page)
                        token = re.search(r'name="csrf_token" value="([^"]+)"', response.text).group(1)
                        self.assertIn("httponly", response.headers["set-cookie"].lower())
                        self.assertIn("samesite=strict", response.headers["set-cookie"].lower())
                        self.assertEqual(client.post(endpoint, data={}, follow_redirects=False).status_code, 403)
                        self.assertEqual(client.post(endpoint, data={"csrf_token": "a" * 32}, follow_redirects=False).status_code, 403)
                        self.assertEqual(client.post(endpoint, data={"csrf_token": token}, follow_redirects=False).status_code, 303)
        finally:
            config.GOOGLE_DEV_MODE = old_mode
