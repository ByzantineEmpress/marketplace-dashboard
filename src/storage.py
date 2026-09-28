"""Where listing images are stored.

Two backends, chosen by ``UPLOAD_BACKEND``:

* ``local`` — ``static/uploads/`` on disk. Simple, but only durable if that
  path is a persistent volume. On an ephemeral container filesystem the images
  disappear on every redeploy.
* ``s3``    — an S3 (or S3-compatible) bucket.

Both return a URL the front-end can use directly, so callers do not care which
is in play. Validation (extension allow-list, size cap, magic-byte sniffing and
path-traversal defence) stays in the route — this module only stores bytes.
"""

import os
import uuid

from src.config import config

LOCAL_URL_PREFIX = "/static/uploads/"


class StorageError(Exception):
    """Raised when an image could not be stored."""


def backend() -> str:
    """Which backend is configured: "s3" or "local"."""
    if (config.UPLOAD_BACKEND or "").strip().lower() == "s3" and config.S3_BUCKET:
        return "s3"
    return "local"


def build_object_key(extension: str) -> str:
    """A collision-resistant, attacker-independent object name."""
    return f"{uuid.uuid4().hex[:12]}{extension}"


# ------------------------------------------------------------------ #
#  Local filesystem
# ------------------------------------------------------------------ #

def uploads_dir() -> str:
    return os.path.realpath(os.path.join("static", "uploads"))


def _save_local(contents: bytes, extension: str) -> str:
    directory = uploads_dir()
    os.makedirs(directory, exist_ok=True)
    name = build_object_key(extension)
    dest = os.path.realpath(os.path.join(directory, name))
    # Defence in depth: the generated name cannot contain separators, but never
    # write outside the uploads directory even if that changes.
    if not dest.startswith(directory + os.sep):
        raise StorageError("Invalid file path")
    with open(dest, "wb") as handle:
        handle.write(contents)
    return f"{LOCAL_URL_PREFIX}{name}"


# ------------------------------------------------------------------ #
#  S3 / S3-compatible
# ------------------------------------------------------------------ #

# Maps an extension to the content type S3 should serve it as.
_CONTENT_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}


def _s3_client():
    """Create a boto3 S3 client, or raise StorageError with a clear reason."""
    try:
        import boto3
        from botocore.exceptions import BotoCoreError, NoCredentialsError
    except ImportError as exc:  # pragma: no cover - depends on install
        raise StorageError(
            "boto3 is not installed; add it to requirements.txt to use S3 uploads"
        ) from exc

    kwargs = {}
    if config.S3_REGION:
        kwargs["region_name"] = config.S3_REGION
    if config.S3_ENDPOINT_URL:
        kwargs["endpoint_url"] = config.S3_ENDPOINT_URL
    # Explicit credentials if provided; otherwise boto3 falls back to its own
    # chain (environment, ~/.aws/credentials, or an instance role — the last
    # being the right answer on EC2/Lightsail).
    if config.S3_ACCESS_KEY_ID and config.S3_SECRET_ACCESS_KEY:
        kwargs["aws_access_key_id"] = config.S3_ACCESS_KEY_ID
        kwargs["aws_secret_access_key"] = config.S3_SECRET_ACCESS_KEY
    try:
        return boto3.client("s3", **kwargs)
    except (BotoCoreError, NoCredentialsError) as exc:
        raise StorageError(f"Could not create an S3 client: {exc}") from exc


def public_url_for(key: str) -> str:
    """The URL a browser should use for an object."""
    base = (config.S3_PUBLIC_BASE_URL or "").rstrip("/")
    if base:
        return f"{base}/{key}"
    region = config.S3_REGION or "us-east-1"
    return f"https://{config.S3_BUCKET}.s3.{region}.amazonaws.com/{key}"


def _save_s3(contents: bytes, extension: str, client=None) -> str:
    key = build_object_key(extension)
    client = client or _s3_client()
    extra = {"ContentType": _CONTENT_TYPES.get(extension, "application/octet-stream")}
    if config.S3_FORCE_DOWNLOAD:
        extra["ContentDisposition"] = "attachment"
    try:
        client.put_object(
            Bucket=config.S3_BUCKET,
            Key=key,
            Body=contents,
            CacheControl="public, max-age=31536000, immutable",
            **extra,
        )
    except Exception as exc:
        # Surface the provider's own message; a misconfigured bucket or missing
        # permission is the usual cause and the message says which.
        raise StorageError(f"S3 upload failed: {type(exc).__name__}: {exc}") from exc
    return public_url_for(key)


# ------------------------------------------------------------------ #
#  Public interface
# ------------------------------------------------------------------ #

def save_image(contents: bytes, extension: str, client=None) -> str:
    """Store image bytes and return the URL to serve them from.

    ``client`` lets tests inject a stub S3 client instead of hitting AWS.
    """
    if backend() == "s3":
        return _save_s3(contents, extension, client=client)
    return _save_local(contents, extension)


def is_local_url(url: str) -> bool:
    """True when a URL points at the local uploads directory."""
    return bool(url) and url.startswith(LOCAL_URL_PREFIX)


def local_path_for(url: str) -> str:
    """Resolve a /static/uploads/... URL to an absolute path on disk."""
    name = url[len(LOCAL_URL_PREFIX):]
    return os.path.realpath(os.path.join(uploads_dir(), os.path.basename(name)))
