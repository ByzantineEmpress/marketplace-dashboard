"""App settings model — stores all user-configurable settings in the database.

Replaces env-var config for user-facing settings. The only stable settings
(read from env) are SERVER_HOST, SERVER_PORT, and DATABASE_URL (which control
the Python/FastAPI bootstrap). Everything else lives in SQLite.
"""

from datetime import datetime
from cryptography.fernet import Fernet

from sqlalchemy import Column, Integer, String, Text, Boolean, DateTime, JSON, ForeignKey, UniqueConstraint
from sqlalchemy.orm import declarative_base

from src.database import Base

Base  # keep reference for linters

# Global Fernet instance — key derived from a secret + per-instance random salt
_fernet = None


def get_fernet():
    """Return the Fernet instance for symmetric encryption.

    Key is built from:
    1. A random per-install salt stored in DB (if available)
    2. The Fernet key is then derived and cached for the process lifetime.
    """
    global _fernet
    if _fernet is None:
        # Try to load from settings if already saved
        try:
            from src.database import SessionLocal
            from src.models import AppSettings
            db = SessionLocal()
            try:
                s = db.query(AppSettings).first()
                if s and s.encryption_key:
                    _fernet = Fernet(s.encryption_key.encode())
                else:
                    key = Fernet.generate_key()
                    if s is None:
                        s = AppSettings()
                        db.add(s)
                    s.encryption_key = key.decode()
                    db.commit()
                    _fernet = Fernet(key)
            finally:
                db.close()
        except Exception:
            _fernet = Fernet(Fernet.generate_key())
    return _fernet


def encrypt_value(value: str) -> str:
    """Encrypt a string value with Fernet symmetric encryption."""
    if not value:
        return ""
    f = get_fernet()
    return f.encrypt(value.encode()).decode()


def decrypt_value(encrypted: str) -> str:
    """Decrypt an Fernet-encrypted string."""
    if not encrypted:
        return ""
    f = get_fernet()
    return f.decrypt(encrypted.encode()).decode()


# ------------------------------------------------------------------ #


class AppSettings(Base):
    """Application settings — all user-configurable options stored in DB.

    Instead of reading from .env, the user sets everything via the UI.
    The only env-var settings are SERVER_HOST, SERVER_PORT, DATABASE_URL.
    """
    __tablename__ = "app_settings"

    id = Column(Integer, primary_key=True, index=True)

    # --- Admin credentials ---
    admin_username = Column(String(100), nullable=True)
    admin_password_hash = Column(String(255), nullable=True)

    # --- Encrypted credentials (Fernet-encrypted) ---
    ebay_client_id_enc = Column(Text, nullable=True)
    ebay_client_secret_enc = Column(Text, nullable=True)
    etsy_api_key_enc = Column(Text, nullable=True)
    etsy_api_secret_enc = Column(Text, nullable=True)

    # --- Callback URLs (stored for OAuth flow) ---
    ebay_callback_url = Column(String(500), nullable=True, default="http://localhost:8000/api/auth/ebay/callback")
    etsy_callback_url = Column(String(500), nullable=True, default="http://localhost:8000/api/auth/etsy/callback")

    # --- Preferences ---
    default_page_size = Column(Integer, default=50)
    auto_refresh_enabled = Column(Boolean, default=True)
    auto_refresh_interval_sec = Column(Integer, default=300)  # 5 minutes
    show_sold_items = Column(Boolean, default=True)
    default_sort_field = Column(String(50), default="created_at")
    default_sort_order = Column(String(4), default="desc")  # asc or desc

    # --- System settings ---
    app_name = Column(String(100), default="Marketplace Dashboard")
    app_version = Column(String(20), default="0.1.0")

    # --- Setup state ---
    setup_completed = Column(Boolean, default=False)

    # --- Encrypted salt/secret for key derivation ---
    encryption_salt = Column(String(50), nullable=True)
    encryption_key = Column(String(100), nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def to_dict(self):
        """Serialize to dict (excludes sensitive data)."""
        return {
            "admin_username": self.admin_username,
            "ebay_connected": bool(self.ebay_client_id_enc),
            "etsy_connected": bool(self.etsy_api_key_enc),
            "default_page_size": self.default_page_size,
            "auto_refresh_enabled": self.auto_refresh_enabled,
            "auto_refresh_interval_sec": self.auto_refresh_interval_sec,
            "show_sold_items": self.show_sold_items,
            "default_sort_field": self.default_sort_field,
            "default_sort_order": self.default_sort_order,
            "app_name": self.app_name,
            "setup_completed": self.setup_completed,
            "ebay_callback_url": self.ebay_callback_url,
            "etsy_callback_url": self.etsy_callback_url,
        }


class EncryptedCredential(Base):
    """Generic encrypted credential storage.

    Use this for any extra encrypted values that don't fit in AppSettings.
    """
    __tablename__ = "encrypted_credentials"

    id = Column(Integer, primary_key=True, index=True)
    key_name = Column(String(100), unique=True, nullable=False)  # e.g. "ebay_refresh_token"
    encrypted_value = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def get_value(self) -> str:
        return decrypt_value(self.encrypted_value)

    def set_value(self, value: str):
        self.encrypted_value = encrypt_value(value)
        self.updated_at = datetime.utcnow()


# ------------------------------------------------------------------ #
#  Listing — one item sold on a marketplace (eBay, Etsy, ...)       #
#  The ``platform`` + ``platform_listing_id`` pair is the natural    #
#  key: the same item can exist on multiple platforms, and a sync    #
#  upserts on this pair.                                             #
# ------------------------------------------------------------------ #


class Listing(Base):
    """A single marketplace listing (one row per platform per item)."""

    __tablename__ = "listings"

    id = Column(Integer, primary_key=True, index=True)

    # Which marketplace + platform-side id
    platform = Column(String(20), nullable=False, index=True)          # primary platform: "ebay" | "etsy" | "facebook" | "local"
    platforms_json = Column(JSON, nullable=True)                       # cross-listed platforms: ["ebay", "etsy", "facebook"]
    platform_listing_id = Column(String(64), nullable=False, index=True)

    # Display data
    title = Column(String(300), nullable=False, default="")
    description = Column(Text, nullable=True)
    image_url = Column(Text, nullable=True)        # primary image
    images_json = Column(JSON, nullable=True)      # list of all image URLs
    original_url = Column(Text, nullable=True)     # deep link to the live listing

    # Pricing
    price_raw = Column(String(50), nullable=True)  # e.g. "CAD 19.99"
    price_cents = Column(Integer, nullable=False, default=0)
    currency = Column(String(10), nullable=True, default="CAD")

    # Cost & Investment Tracking (COGS)
    purchase_price_cents = Column(Integer, nullable=False, default=0)  # What we bought it for
    parts_cost_cents = Column(Integer, nullable=False, default=0)      # Total parts/repair cost
    parts_json = Column(JSON, nullable=True)                          # [{"description": "Power supply", "cost_cents": 2500}]

    # State
    status = Column(String(20), nullable=False, default="active", index=True)
    is_sold = Column(Boolean, nullable=False, default=False, index=True)
    available_quantity = Column(Integer, nullable=False, default=0)
    views_count = Column(Integer, nullable=False, default=0)

    # Platform-specific extras
    sku = Column(String(100), nullable=True)
    category = Column(String(200), nullable=True)
    inventory_item_id = Column(String(64), nullable=True)   # eBay inventory item id
    tags = Column(JSON, nullable=True)                      # e.g. Etsy tags
    materials = Column(JSON, nullable=True)                 # e.g. Etsy materials

    # Team ownership — listings in a team are shared with that team's
    # members. NULL means "not assigned to a team yet" (treated as
    # shared — e.g. items imported by a marketplace sync).
    team_id = Column(Integer, ForeignKey("teams.id"), nullable=True, index=True)

    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    last_fetched = Column(DateTime, nullable=True)

    @property
    def total_cost_cents(self) -> int:
        return (self.purchase_price_cents or 0) + (self.parts_cost_cents or 0)

    @property
    def net_profit_cents(self) -> int:
        return (self.price_cents or 0) - self.total_cost_cents

    @property
    def profit_margin_pct(self) -> float:
        if not self.price_cents or self.price_cents <= 0:
            return 0.0
        return round((self.net_profit_cents / self.price_cents) * 100, 1)

    @property
    def platforms(self) -> list:
        if self.platforms_json and isinstance(self.platforms_json, list) and len(self.platforms_json) > 0:
            return self.platforms_json
        return [self.platform] if self.platform else []

    def to_dict(self):
        """Serialise for the JSON API (consumed by the dashboard UI)."""
        return {
            "id": self.id,
            "platform": self.platform,
            "platforms": self.platforms,
            "platform_listing_id": self.platform_listing_id,
            "team_id": self.team_id,
            "title": self.title,
            "description": self.description,
            "price_raw": self.price_raw,
            "price_cents": self.price_cents,
            "currency": self.currency or "CAD",
            "purchase_price_cents": self.purchase_price_cents or 0,
            "purchase_price": round((self.purchase_price_cents or 0) / 100, 2),
            "parts_cost_cents": self.parts_cost_cents or 0,
            "parts_cost": round((self.parts_cost_cents or 0) / 100, 2),
            "parts": self.parts_json or [],
            "total_cost_cents": self.total_cost_cents,
            "total_cost": round(self.total_cost_cents / 100, 2),
            "net_profit_cents": self.net_profit_cents,
            "net_profit": round(self.net_profit_cents / 100, 2),
            "profit_margin_pct": self.profit_margin_pct,
            "status": self.status,
            "is_sold": bool(self.is_sold),
            "image_url": self.image_url,
            "images": self.images_json or [],
            "original_url": self.original_url,
            "sku": self.sku,
            "category": self.category,
            "inventory_item_id": self.inventory_item_id,
            "available_quantity": self.available_quantity,
            "views_count": self.views_count,
            "tags": self.tags or [],
            "materials": self.materials or [],
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "last_fetched": self.last_fetched.isoformat() if self.last_fetched else None,
        }


class MarketplaceAccount(Base):
    """OAuth account/connection for one marketplace platform.

    One row per platform. Tokens are stored here so the adapters can
    authenticate without re-running the OAuth flow each run.
    """

    __tablename__ = "marketplace_accounts"

    id = Column(Integer, primary_key=True, index=True)
    platform = Column(String(20), nullable=False, unique=True, index=True)

    shop_id = Column(String(100), nullable=True)
    shop_name = Column(String(100), nullable=True)

    # OAuth tokens
    access_token = Column(Text, nullable=True)
    refresh_token = Column(Text, nullable=True)
    token_expires_at = Column(DateTime, nullable=True)
    token_data = Column(JSON, nullable=True)   # raw token response (extra claims)

    is_connected = Column(Boolean, nullable=False, default=False)
    last_synced = Column(DateTime, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def to_dict(self):
        """Serialise for the JSON API — never includes raw tokens."""
        return {
            "platform": self.platform,
            "shop_id": self.shop_id,
            "shop_name": self.shop_name,
            "is_connected": bool(self.is_connected),
            "token_expires_at": self.token_expires_at.isoformat() if self.token_expires_at else None,
            "last_synced": self.last_synced.isoformat() if self.last_synced else None,
        }


# ------------------------------------------------------------------ #
#  Teams — let several people share the same inventory.              #
#                                                                     #
#  A User signs in (Google, or the local admin account). Users       #
#  belong to one or more Teams via TeamMembership. Listings live in  #
#  a team (Listing.team_id); everyone in that team sees them.        #
# ------------------------------------------------------------------ #


class User(Base):
    """A person who can sign in and view inventory."""

    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String(255), unique=True, nullable=False, index=True)
    name = Column(String(200), nullable=True)
    # "google" (signed in via Google OAuth) or "local" (username/password)
    provider = Column(String(20), nullable=False, default="local")
    created_at = Column(DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id,
            "email": self.email,
            "name": self.name or self.email,
            "provider": self.provider,
        }


class Team(Base):
    """A group of users who share one inventory."""

    __tablename__ = "teams"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), unique=True, nullable=False)
    invite_code = Column(String(32), unique=True, nullable=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "invite_code": self.invite_code,
        }


class TeamMembership(Base):
    """Links a user to a team (one row per user per team)."""

    __tablename__ = "team_members"
    __table_args__ = (UniqueConstraint("team_id", "user_id", name="uq_team_user"),)

    id = Column(Integer, primary_key=True, index=True)
    team_id = Column(Integer, ForeignKey("teams.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    # "owner" can add/remove members; "member" just participates
    role = Column(String(20), nullable=False, default="member")


class AuthSession(Base):
    """One login session: ties the opaque cookie token to a user.

    Stored in the DB (not memory) so a server restart doesn't log
    everyone out — the cookie is still valid until it expires.
    """

    __tablename__ = "auth_sessions"

    id = Column(Integer, primary_key=True, index=True)
    token = Column(String(64), unique=True, nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    expires_at = Column(DateTime, nullable=False)


class RunState(Base):
    """Tracks one-time run state (setup wizard, migrations, etc.).

    Prevents showing the setup wizard after the user has already completed it.
    """
    __tablename__ = "run_state"

    id = Column(Integer, primary_key=True, index=True)
    state_key = Column(String(100), unique=True, nullable=False)  # e.g. "setup_wizard_completed"
    state_value = Column(String(500), nullable=True)  # JSON or plain text
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
