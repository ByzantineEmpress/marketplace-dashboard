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
    sold_at = Column(DateTime, nullable=True, index=True)
    written_off_at = Column(DateTime, nullable=True, index=True)
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
            "sold_at": self.sold_at.isoformat() if self.sold_at else None,
            "written_off_at": self.written_off_at.isoformat() if self.written_off_at else None,
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
    # "google"      — signed in via Google OAuth
    # "local"       — the bootstrap administrator from .env (ADMIN_USERNAME)
    # "password"    — a self-serve account with an email + password
    #
    # NOTE: "local" is treated as admin by check_admin() for backwards
    # compatibility, so self-serve accounts must never be created with it —
    # that would hand every new signup full admin rights.
    provider = Column(String(20), nullable=False, default="password")
    # bcrypt hash for provider == "password". Null for OAuth accounts.
    password_hash = Column(String(255), nullable=True)
    is_admin = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id,
            "email": self.email,
            "name": self.name or self.email,
            "provider": self.provider,
            "is_admin": bool(self.is_admin or self.provider == "local"),
        }


class RateLimitEvent(Base):
    """One recorded rate-limited action, used to enforce limits across workers.

    Kept in the database rather than in process memory: with more than one
    uvicorn worker (or more than one container) an in-memory counter is
    per-process, so every limit is silently multiplied by the number of
    workers. That turns "5 signups per day" into "5 per worker per day" and
    makes the limit meaningless.

    Rows are short-lived — anything older than the longest window is pruned
    as part of each check.
    """

    __tablename__ = "rate_limit_events"

    id = Column(Integer, primary_key=True, index=True)
    # "<limit name>:<subject key>", e.g. "signup_ip_daily:203.0.113.7"
    bucket = Column(String(200), nullable=False, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)


class PendingSignup(Base):
    """A signup that is waiting for its email address to be confirmed.

    Deliberately separate from ``users``: while a signup is pending, **no**
    user, team or membership exists. Only clicking the emailed link creates
    the real account. This also reserves the address, so two people cannot
    race for the same one.
    """

    __tablename__ = "pending_signups"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String(255), unique=True, nullable=False, index=True)
    name = Column(String(200), nullable=True)
    # bcrypt hash; the plaintext password is never stored or emailed.
    password_hash = Column(String(255), nullable=False)
    # SHA-256 of the token that was emailed. The raw token is never stored, so
    # a leaked database cannot be used to confirm someone else's address.
    token_hash = Column(String(64), nullable=False, index=True)
    expires_at = Column(DateTime, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    # How many times the confirmation mail has been sent for this signup.
    send_count = Column(Integer, nullable=False, default=1)
    last_sent_at = Column(DateTime, nullable=True)

    def is_expired(self) -> bool:
        return datetime.utcnow() > self.expires_at


class Team(Base):
    """A group of users who share one inventory."""

    __tablename__ = "teams"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False)
    invite_code = Column(String(32), unique=True, nullable=True, index=True)
    # Email of whoever created the team. Used to let a prospective member ask
    # to join by naming the team creator, without disclosing team names.
    created_by_email = Column(String(255), nullable=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "invite_code": self.invite_code,
            "created_by_email": self.created_by_email,
        }


class TeamJoinRequest(Base):
    """A pending request from a user asking to join someone else's team.

    The requester names the team creator's email address and supplies a short
    description of who they are. The owner approves or denies it. Deliberately
    does NOT reveal the team name to the requester before approval.
    """

    __tablename__ = "team_join_requests"

    STATUS_PENDING = "pending"
    STATUS_APPROVED = "approved"
    STATUS_DENIED = "denied"

    id = Column(Integer, primary_key=True, index=True)
    team_id = Column(Integer, ForeignKey("teams.id", ondelete="CASCADE"), nullable=False, index=True)
    requester_user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    # Denormalised so the owner can be found without walking memberships, and
    # so a request stays attributable if the owner later changes.
    owner_email = Column(String(255), nullable=False, index=True)
    # Sanitised, length-capped free text. Rendered with autoescaping on.
    description = Column(String(400), nullable=False, default="")
    status = Column(String(20), nullable=False, default="pending", index=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    decided_at = Column(DateTime, nullable=True)

    def to_dict(self):
        return {
            "id": self.id,
            "team_id": self.team_id,
            "requester_user_id": self.requester_user_id,
            "owner_email": self.owner_email,
            "description": self.description,
            "status": self.status,
            "created_at": self.created_at.isoformat() if self.created_at else None,
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
