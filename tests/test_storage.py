"""Image storage backends.

The S3 path is exercised with an injected stub client, so no AWS credentials
or network access are needed and nothing is ever uploaded for real.
"""
import os
import secrets
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import storage
from src.config import config


class StubS3Client:
    """Records put_object calls and can be made to fail on demand."""

    def __init__(self, fail_with=None):
        self.calls = []
        self.fail_with = fail_with

    def put_object(self, **kwargs):
        if self.fail_with:
            raise self.fail_with
        self.calls.append(kwargs)
        return {"ResponseMetadata": {"HTTPStatusCode": 200}}


class StorageBackendTest(unittest.TestCase):
    def setUp(self):
        self._orig = (config.UPLOAD_BACKEND, config.S3_BUCKET, config.S3_REGION,
                      config.S3_PUBLIC_BASE_URL, config.S3_ENDPOINT_URL,
                      config.S3_FORCE_DOWNLOAD)
        config.S3_REGION = "us-east-1"
        config.S3_PUBLIC_BASE_URL = ""
        config.S3_ENDPOINT_URL = ""
        config.S3_FORCE_DOWNLOAD = False

    def tearDown(self):
        (config.UPLOAD_BACKEND, config.S3_BUCKET, config.S3_REGION,
         config.S3_PUBLIC_BASE_URL, config.S3_ENDPOINT_URL,
         config.S3_FORCE_DOWNLOAD) = self._orig

    def _use_s3(self):
        config.UPLOAD_BACKEND = "s3"
        config.S3_BUCKET = "marketplace-test-bucket"

    def _use_local(self):
        config.UPLOAD_BACKEND = "local"
        config.S3_BUCKET = ""

    # -- backend selection -------------------------------------------------

    def test_defaults_to_local(self):
        self._use_local()
        self.assertEqual(storage.backend(), "local")

    def test_s3_requires_a_bucket(self):
        """Naming s3 but forgetting the bucket must not silently upload nowhere."""
        config.UPLOAD_BACKEND = "s3"
        config.S3_BUCKET = ""
        self.assertEqual(storage.backend(), "local",
                         "fell through to s3 without a bucket configured")

    def test_s3_selected_when_configured(self):
        self._use_s3()
        self.assertEqual(storage.backend(), "s3")

    # -- local backend -----------------------------------------------------

    def test_local_save_writes_the_file_and_returns_a_static_url(self):
        self._use_local()
        url = storage.save_image(b"\x89PNG\r\n\x1a\npayload", ".png")
        try:
            self.assertTrue(url.startswith("/static/uploads/"))
            self.assertTrue(url.endswith(".png"))
            self.assertTrue(os.path.exists(storage.local_path_for(url)))
        finally:
            path = storage.local_path_for(url)
            if os.path.exists(path):
                os.remove(path)

    def test_local_object_names_are_random(self):
        self._use_local()
        a = storage.build_object_key(".png")
        b = storage.build_object_key(".png")
        self.assertNotEqual(a, b)
        # No path separators, so it can never escape the uploads directory.
        for name in (a, b):
            self.assertNotIn("/", name)
            self.assertNotIn("\\", name)

    def test_local_path_helper_strips_directories(self):
        """A crafted URL must not resolve outside uploads/."""
        self._use_local()
        resolved = storage.local_path_for("/static/uploads/../../etc/passwd")
        self.assertTrue(resolved.startswith(storage.uploads_dir()))

    # -- s3 backend --------------------------------------------------------

    def test_s3_upload_puts_the_object_with_a_content_type(self):
        self._use_s3()
        client = StubS3Client()
        url = storage.save_image(b"\x89PNG\r\n\x1a\npayload", ".png", client=client)

        self.assertEqual(len(client.calls), 1)
        call = client.calls[0]
        self.assertEqual(call["Bucket"], "marketplace-test-bucket")
        self.assertEqual(call["ContentType"], "image/png")
        self.assertEqual(call["Body"], b"\x89PNG\r\n\x1a\npayload")
        # Immutable caching is safe because object names are random.
        self.assertIn("immutable", call["CacheControl"])
        # Default public URL shape.
        self.assertTrue(url.startswith(
            "https://marketplace-test-bucket.s3.us-east-1.amazonaws.com/"))
        self.assertTrue(url.endswith(".png"))

    def test_content_type_per_extension(self):
        self._use_s3()
        cases = {
            ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
            ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
        }
        for ext, expected in cases.items():
            client = StubS3Client()
            storage.save_image(b"data", ext, client=client)
            self.assertEqual(client.calls[0]["ContentType"], expected, ext)

    def test_unknown_extension_falls_back_to_octet_stream(self):
        self._use_s3()
        client = StubS3Client()
        storage.save_image(b"data", ".xyz", client=client)
        self.assertEqual(client.calls[0]["ContentType"], "application/octet-stream")

    def test_custom_public_base_url_is_used(self):
        self._use_s3()
        config.S3_PUBLIC_BASE_URL = "https://cdn.example.com/images/"
        client = StubS3Client()
        url = storage.save_image(b"data", ".png", client=client)
        self.assertTrue(url.startswith("https://cdn.example.com/images/"),
                        f"custom base URL ignored: {url}")
        # The trailing slash must not be doubled.
        self.assertNotIn("images//", url)

    def test_force_download_sets_content_disposition(self):
        self._use_s3()
        config.S3_FORCE_DOWNLOAD = True
        client = StubS3Client()
        storage.save_image(b"data", ".png", client=client)
        self.assertEqual(client.calls[0]["ContentDisposition"], "attachment")

    def test_s3_failure_raises_storage_error_with_provider_detail(self):
        """A bucket misconfiguration must surface, not be swallowed."""
        self._use_s3()
        client = StubS3Client(fail_with=Exception("AccessDenied: not authorised"))
        with self.assertRaises(storage.StorageError) as ctx:
            storage.save_image(b"data", ".png", client=client)
        message = str(ctx.exception)
        self.assertIn("AccessDenied", message)
        self.assertIn("S3 upload failed", message)

    def test_s3_missing_boto3_reports_clearly(self):
        """If boto3 is absent the error should say so, not crash obscurely."""
        self._use_s3()
        import builtins
        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name.startswith("boto3") or name.startswith("botocore"):
                raise ImportError("No module named 'boto3'")
            return real_import(name, *args, **kwargs)

        builtins.__import__ = fake_import
        try:
            with self.assertRaises(storage.StorageError) as ctx:
                storage.save_image(b"data", ".png")
        finally:
            builtins.__import__ = real_import
        self.assertIn("boto3", str(ctx.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
