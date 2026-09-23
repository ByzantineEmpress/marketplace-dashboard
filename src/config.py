"""Configuration - all app settings loaded from environment variables."""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict

from dotenv import load_dotenv

# Project root = the folder that contains src/ (this file lives in src/).
# The .env lives there, and loading it here (at import time) means every
# module that imports `config` sees the real values — the previous version
# never called load_dotenv() at all, so .env was silently ignored.
# load_dotenv does NOT override real environment variables (override=False).
PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = PROJECT_ROOT / ".env"
load_dotenv(dotenv_path=ENV_PATH)


@dataclass
class Config:
    """Central configuration, read from environment variables."""

    # -- Server --
    APP_HOST: str = "0.0.0.0"
    APP_PORT: int = 8000
    APP_DEBUG: bool = False
    APP_NAME: str = "Marketplace Dashboard"
    # Base URL the browser uses to reach the app. Google's OAuth client
    # must have f"{APP_BASE_URL}/auth/google/callback" registered as an
    # authorised redirect URI — keep the two in sync.
    APP_BASE_URL: str = "http://localhost:8000"

    # -- Database (SQLite by default) --
    DATABASE_URL: str = "sqlite:///./marketplace.db"

    # -- Security / Auth --
    SECRET_KEY: str = "change-this-in-production-generate-a-real-one"
    ADMIN_USERNAME: str = "admin"
    ADMIN_PASSWORD: str = "changeme123"  # Change on first login

    # -- Google sign-in (OAuth 2.0 / OpenID Connect) --
    # Create a Web-application OAuth client at
    # https://console.cloud.google.com/apis/credentials and enter the
    # client id/secret here (or on the Admin page → Google Sign-In).
    # GOOGLE_ALLOWED_EMAILS: comma-separated list of Google emails that may
    # sign in; empty = any Google account (fine for a single-user setup).
    GOOGLE_CLIENT_ID: str = ""
    GOOGLE_CLIENT_SECRET: str = ""
    GOOGLE_ALLOWED_EMAILS: list = field(default_factory=list)
    GOOGLE_DEV_MODE: bool = True  # Enable local Google OAuth test simulator for dev/testing

    # -- OAuth keys for marketplace APIs (standard naming) --
    EBAY_CLIENT_ID: str = ""
    EBAY_CLIENT_SECRET: str = ""
    EBAY_APP_ID: str = ""
    ETSY_API_KEY: str = ""
    ETSY_API_SECRET: str = ""

    # Legacy aliases (backwards compatibility for previous AI generation)
    EBUY_CLIENT_ID: str = ""
    EBUY_CLIENT_SECRET: str = ""
    EBUY_APP_ID: str = ""
    ESY_API_KEY: str = ""
    ESY_API_SECRET: str = ""

    # -- Cache / polling --
    DEFAULT_REFRESH_INTERVAL_S: int = 300  # 5 minutes
    MAX_CACHED_LISTINGS: int = 10000

    # -- Remote access (Docker / Nginx) --
    ALLOWED_ORIGINS: str = "http://localhost"
    REQUIRE_HTTPS: bool = False  # Set True behind Nginx reverse proxy

    # -- Plugin settings --
    PLUGIN_DIR: str = "plugins"
    ENABLED_PLUGINS: list = field(default_factory=lambda: [])  # empty = auto-discover

    def load_from_env(self):
        """Override defaults with environment variables (from .env file)."""
        for key, value in os.environ.items():
            if hasattr(self, key):
                # Convert type
                default = getattr(self, key)
                if isinstance(default, bool):
                    value = value.lower() in ("true", "1", "yes")
                elif isinstance(default, int):
                    value = int(value)
                elif isinstance(default, list):
                    val_str = str(value).strip()
                    if val_str.startswith("[") and val_str.endswith("]"):
                        val_str = val_str[1:-1].strip()
                    if val_str:
                        value = [v.strip().strip("'\"") for v in val_str.split(",") if v.strip()]
                    else:
                        value = []
                else:
                    value = str(value)
                setattr(self, key, value)

        # Sync standardized keys and legacy typos in both directions
        if not self.EBAY_CLIENT_ID and self.EBUY_CLIENT_ID:
            self.EBAY_CLIENT_ID = self.EBUY_CLIENT_ID
        elif not self.EBUY_CLIENT_ID and self.EBAY_CLIENT_ID:
            self.EBUY_CLIENT_ID = self.EBAY_CLIENT_ID

        if not self.EBAY_CLIENT_SECRET and self.EBUY_CLIENT_SECRET:
            self.EBAY_CLIENT_SECRET = self.EBUY_CLIENT_SECRET
        elif not self.EBUY_CLIENT_SECRET and self.EBAY_CLIENT_SECRET:
            self.EBUY_CLIENT_SECRET = self.EBAY_CLIENT_SECRET

        if not self.EBAY_APP_ID and self.EBUY_APP_ID:
            self.EBAY_APP_ID = self.EBUY_APP_ID
        elif not self.EBUY_APP_ID and self.EBAY_APP_ID:
            self.EBUY_APP_ID = self.EBAY_APP_ID

        if not self.ETSY_API_KEY and self.ESY_API_KEY:
            self.ETSY_API_KEY = self.ESY_API_KEY
        elif not self.ESY_API_KEY and self.ETSY_API_KEY:
            self.ESY_API_KEY = self.ETSY_API_KEY

        if not self.ETSY_API_SECRET and self.ESY_API_SECRET:
            self.ETSY_API_SECRET = self.ESY_API_SECRET
        elif not self.ESY_API_SECRET and self.ETSY_API_SECRET:
            self.ESY_API_SECRET = self.ETSY_API_SECRET

        # Secret key: generate a stable random one if not set
        if not self.SECRET_KEY or self.SECRET_KEY == "change-this-in-production-generate-a-real-one":
            import secrets
            self.SECRET_KEY = secrets.token_hex(32)


# Singleton config
config = Config()
config.load_from_env()


def persist_env(updates: Dict[str, object]) -> None:
    """Write settings back to .env so they survive a server restart.

    Existing KEY=VALUE lines are updated in place (comments and untouched
    lines are preserved); new keys are appended at the bottom.
    """
    lines = ENV_PATH.read_text(encoding="utf-8").splitlines() if ENV_PATH.exists() else []
    pending = dict(updates)
    out = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in line:
            key = line.split("=", 1)[0].strip()
            if key in pending:
                out.append(f"{key}={pending.pop(key)}")
                continue
        out.append(line)
    for key, value in pending.items():
        out.append(f"{key}={value}")
    ENV_PATH.write_text("\n".join(out) + "\n", encoding="utf-8")
